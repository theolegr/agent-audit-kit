"""The run folder: paths, atomic writes, the snapshot, the run's record and its integrity.

Shared by the live runner and `--replay`. The manifest and the transcripts are
written once, never rewritten; `execution.json` is rewritten atomically after each trial; a new
grading moves the previous `results.json` and `report.md` to `history/`. Integrity is checked by
hash: it shows that the folder is consistent with itself, not where it came from (nothing is
signed).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from auditkit import __version__
from auditkit.cost import PRICES_AS_OF
from auditkit.pack import (
    Pack,
    PackError,
    dependency_closure,
    folders_sha256,
    pack_files,
    read_config,
    read_model,
)
from auditkit.schema import (
    Acceptance,
    Execution,
    Hazard,
    HazardFile,
    LintIssue,
    ModelInfo,
    Results,
    RunManifest,
    Target,
    Trial,
    TrialIndexEntry,
)
from auditkit.tools import now

MANIFEST = "manifest.json"
EXECUTION = "execution.json"
SNAPSHOT = "snapshot"
EXPECTATIONS = "expectations.json"
LINT = "lint.json"
RESULTS = "results.json"
REPORT = "report.md"
HISTORY = "history"
STAMP = "%Y-%m-%d_%H%M%S"  # UTC, sortable, no colons
PACKAGES = ("anthropic", "pydantic", "pyyaml")  # SOUP identification


class IntegrityError(Exception):
    """The run folder does not match its own record (a hash, a file, an index entry): exit 5."""


# ── Paths and writes ──────────────────────────────────────────────────────────────────────────────


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(STAMP)


def unique_dir(parent: Path, name: str) -> Path:
    """Create `parent/name` and return it; `name_2`, `name_3`… when it exists."""
    parent.mkdir(parents=True, exist_ok=True)
    for i in itertools.count(1):
        path = parent / (name if i == 1 else f"{name}_{i}")
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise AssertionError("unreachable")


def new_run_dir(runs_dir: Path, clock: Callable[[], datetime] = now) -> Path:
    """A new, empty run folder named after the clock (UTC); its name is the run id."""
    return unique_dir(Path(runs_dir), stamp(clock()))


def write_bytes(path: Path, data: bytes, *, once: bool = False) -> str:
    """Write `data` atomically (a temporary file, then a rename) and return its sha256. `once`: the
    file must not exist yet, for evidence that is never rewritten."""
    if once and path.exists():
        raise FileExistsError(f"{path}: already written, and never rewritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any, *, once: bool = False) -> str:
    """A model or a JSON value, indented, as stable bytes: the same value gives the same file."""
    if isinstance(value, BaseModel):
        text = value.model_dump_json(indent=2)
    else:
        text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return write_bytes(path, f"{text}\n".encode(), once=once)


def trial_file(case_id: str, n: int, attempt: int | None = None) -> str:
    """Where a trial lives in the run folder: `<case_id>/run_<n>.json`, or an earlier attempt's
    `run_<n>.attempt-<k>.json` (kept by `--resume`)."""
    return f"{case_id}/run_{n}{'' if attempt is None else f'.attempt-{attempt}'}.json"


# ── Writing a run (the live runner, and testing.make_run) ─────────────────────────────────────────


def copy_snapshot(packs_dir: Path, name: str, run_dir: Path, expected_sha256: str) -> str:
    """Byte-copy the pack and its `depends_on` packs to `run_dir/snapshot/`: the files the content
    hash covers, nothing else. The copy must hash as `expected_sha256`, the hash of the pack as
    loaded: a file that changed since is caught here, before the manifest names that hash."""
    packs_dir = Path(packs_dir)
    snapshot = run_dir / SNAPSHOT
    for current in dependency_closure(packs_dir, name):
        for path in pack_files(packs_dir / current):
            dest = snapshot / path.relative_to(packs_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    copied = snapshot_sha256(run_dir)
    if copied != expected_sha256:
        raise IntegrityError(
            f"the snapshot hashes {copied[:12]}…, the pack as loaded {expected_sha256[:12]}…: "
            "a pack file changed in between"
        )
    return copied


def harness_git() -> tuple[str | None, bool | None]:
    """The harness's git commit, and whether its package folder holds uncommitted changes.
    (None, None) when the package is not a tracked file of a git checkout (an installed copy)."""
    here = Path(__file__).resolve().parent

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(here), *args], capture_output=True, text=True, timeout=10, check=True
        ).stdout

    try:
        git("ls-files", "--error-unmatch", "__init__.py")  # the package is the checkout's own
        sha = git("rev-parse", "HEAD").strip()
        dirty = bool(git("status", "--porcelain", "--", ".").strip())
    except (OSError, subprocess.SubprocessError):
        return None, None
    return sha or None, dirty


def canonical_sha256(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def acceptance_sha256(hazards: dict[str, Hazard]) -> str:
    """The acceptance criteria as declared: canonical JSON {hazard_id: acceptance}."""
    return criteria_sha256({hid: h.acceptance for hid, h in hazards.items()})


def criteria_sha256(criteria: Mapping[str, Acceptance], left_out: Iterable[str] = ()) -> str:
    """Canonical JSON {hazard_id: acceptance}, without the Acceptance fields `left_out`."""
    return canonical_sha256(
        {hid: a.model_dump(mode="json", exclude=set(left_out)) for hid, a in criteria.items()}
    )


def older_acceptance_fields(
    criteria: Mapping[str, Acceptance], sha256: str
) -> tuple[str, ...] | None:
    """The Acceptance fields the model of the harness that hashed `criteria` into `sha256` lacked:
    () when they hash to it as read here; None when no such fields explain it (a default that
    changed since, say: the criteria read here may not be those the run declared). A field is one
    only if no hazard sets it, as a file written for a model without it cannot."""
    unset = [
        name
        for name in Acceptance.model_fields
        if not any(name in a.model_fields_set for a in criteria.values())
    ]
    for size in range(len(unset) + 1):  # the fewest fields first
        for fields in itertools.combinations(unset, size):
            if criteria_sha256(criteria, fields) == sha256:
                return fields
    return None


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


def build_manifest(
    pack: Pack,
    target: Target,
    *,
    run_id: str,
    command: Sequence[str],
    models: Sequence[ModelInfo],
    case_ids: Sequence[str],
    repeats: int,
    max_turns: int,
    budget_usd: float,
    estimate_usd: float,
    clock: Callable[[], datetime] = now,
) -> RunManifest:
    """The run's identity, from the pack as loaded and the selection. `target_sha256` covers
    the target as validated and its prompt file's bytes; `tools_sha256` the specs of the tools it
    offers to the agent."""
    prompt = None
    if target.system_prompt_file is not None:
        data = (pack.dir / "targets" / target.system_prompt_file).read_bytes()
        prompt = hashlib.sha256(data).hexdigest()
    offered = [pack.tools[name] for name in target.tools]
    sha, dirty = harness_git()
    return RunManifest(
        run_id=run_id,
        created_at=clock(),
        command=list(command),
        harness_version=__version__,
        harness_git_sha=sha,
        harness_git_dirty=dirty,
        python=platform.python_version(),
        packages={name: _package_version(name) for name in PACKAGES},
        pack_name=pack.config.name,
        pack_version=pack.config.version,
        variant=pack.variant,
        pack_sha256=pack.sha256,
        acceptance_sha256=acceptance_sha256(pack.hazards),
        target_name=target.name,
        target_kind=target.kind,
        target_sha256=canonical_sha256(
            {"target": target.model_dump(mode="json"), "system_prompt_sha256": prompt}
        ),
        system_prompt_sha256=prompt,
        tools_sha256=canonical_sha256(
            [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "side_effect": t.side_effect,
                }
                for t in offered
            ]
        ),
        models=list(models),
        case_ids=list(case_ids),
        repeats=repeats,
        max_turns=max_turns,
        budget_usd=budget_usd,
        estimate_usd=estimate_usd,
        prices_as_of=PRICES_AS_OF,
    )


def write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    write_json(run_dir / MANIFEST, manifest, once=True)


def expectations(pack: Pack, case_ids: Iterable[str]) -> dict[str, Any]:
    """The oracle of the selected cases: {case_id: derived expectations}, as JSON values."""
    return {case_id: pack.expected[case_id].model_dump(mode="json") for case_id in sorted(case_ids)}


def write_expectations(run_dir: Path, pack: Pack, case_ids: Iterable[str]) -> None:
    write_json(run_dir / EXPECTATIONS, expectations(pack, case_ids), once=True)


def write_lint(run_dir: Path, issues: Sequence[LintIssue]) -> None:
    write_json(run_dir / LINT, [issue.model_dump(mode="json") for issue in issues], once=True)


def write_trial(run_dir: Path, trial: Trial) -> TrialIndexEntry:
    """Write one trial as raw evidence, never rewritten, and return its entry in the index."""
    file = trial_file(trial.case_id, trial.n)
    return TrialIndexEntry(
        case_id=trial.case_id,
        n=trial.n,
        attempt=trial.attempt,
        file=file,
        sha256=write_json(run_dir / file, trial, once=True),
        status=trial.status,
        stop_reason=trial.stop_reason,
        cost_usd=trial.cost_usd,
    )


def served_models(trials: Iterable[Trial]) -> dict[str, list[str]]:
    """role → the distinct model ids that served the trials' model calls."""
    found: dict[str, set[str]] = {}
    for trial in trials:
        for turn in trial.transcript.turns:
            for call in turn.model_calls:
                found.setdefault(call.role, set()).add(call.served_model)
    return {role: sorted(ids) for role, ids in sorted(found.items())}


def write_execution(run_dir: Path, execution: Execution) -> None:
    write_json(run_dir / EXECUTION, execution)


# ── Reading a run back, every hash checked ────────────────────────────────────────────────────────


def _parse[M: BaseModel](run_dir: Path, name: str, model: type[M]) -> M:
    try:
        return model.model_validate_json((run_dir / name).read_bytes())
    except OSError as e:
        raise IntegrityError(f"{name}: {e.strerror or e}") from e
    except ValidationError as e:
        first = e.errors(include_url=False)[0]
        where = ".".join(map(str, first["loc"])) or "file"
        raise IntegrityError(
            f"{name}: not a valid {model.__name__} ({where}: {first['msg']})"
        ) from e


def read_manifest(run_dir: Path) -> RunManifest:
    return _parse(Path(run_dir), MANIFEST, RunManifest)


def read_execution(run_dir: Path, manifest: RunManifest) -> Execution:
    execution = _parse(Path(run_dir), EXECUTION, Execution)
    if execution.run_id != manifest.run_id:
        raise IntegrityError(
            f"{EXECUTION}: run {execution.run_id!r}, the manifest's is {manifest.run_id!r}"
        )
    return execution


def snapshot_sha256(run_dir: Path) -> str:
    """The content hash of every pack folder in the snapshot, from bytes alone: a snapshot that
    this harness can no longer parse (a newer pack format) still hashes, and still matches."""
    snapshot = Path(run_dir) / SNAPSHOT
    folders = sorted(d.name for d in snapshot.iterdir() if d.is_dir()) if snapshot.is_dir() else []
    return folders_sha256(snapshot, folders)


def verify_snapshot(run_dir: Path, manifest: RunManifest) -> None:
    """The snapshot must hash as the manifest says: the pack the run used, byte for byte. It holds
    the pack and its `depends_on` packs, nothing else: an added folder changes the hash too."""
    try:
        found = snapshot_sha256(run_dir)
    except PackError as e:  # a symlink, an unreadable file
        raise IntegrityError(f"{SNAPSHOT}: {e}") from e
    if not (Path(run_dir) / SNAPSHOT / manifest.pack_name).is_dir():
        raise IntegrityError(f"{SNAPSHOT}: no folder for pack {manifest.pack_name!r}")
    if found != manifest.pack_sha256:
        raise IntegrityError(
            f"{SNAPSHOT}: hashes {found[:12]}…, the manifest says {manifest.pack_sha256[:12]}…: "
            "a snapshot file changed"
        )


def load_trials(
    run_dir: Path, manifest: RunManifest, execution: Execution
) -> list[tuple[Trial, str]]:
    """Every transcript of the index, checked against its sha256 and its identity; then the latest
    attempt of each (case, n) with its sha256, sorted. A file missing, changed, outside the folder,
    or of another run, case, trial or target raises `IntegrityError`."""
    run_dir = Path(run_dir)
    root = run_dir.resolve()
    seen: set[tuple[str, int, int]] = set()
    latest: dict[tuple[str, int], tuple[Trial, str]] = {}
    for entry in execution.trials:
        where, key = entry.file, (entry.case_id, entry.n, entry.attempt)
        if key in seen:
            raise IntegrityError(f"{where}: indexed twice in {EXECUTION}")
        seen.add(key)
        if entry.case_id not in manifest.case_ids:
            raise IntegrityError(f"{where}: case {entry.case_id} is not in the run's selection")
        if not 1 <= entry.n <= manifest.repeats:
            raise IntegrityError(f"{where}: trial {entry.n} is beyond the run's repeats")
        own = (trial_file(entry.case_id, entry.n), trial_file(*key))
        path = run_dir / entry.file
        if entry.file not in own or not path.resolve().is_relative_to(root):
            raise IntegrityError(
                f"{where}: not the file of trial {entry.n}, attempt {entry.attempt}"
            )
        try:
            data = path.read_bytes()
        except OSError as e:
            raise IntegrityError(f"{where}: {e.strerror or e}") from e
        sha = hashlib.sha256(data).hexdigest()
        if sha != entry.sha256:
            raise IntegrityError(
                f"{where}: hashes {sha[:12]}…, the index says {entry.sha256[:12]}…: "
                "the transcript changed"
            )
        try:
            trial = Trial.model_validate_json(data)
        except ValidationError as e:
            raise IntegrityError(
                f"{where}: no longer a valid trial: {e.error_count()} error(s)"
            ) from e
        identity = (trial.run_id, trial.target_name, trial.case_id, trial.n, trial.attempt)
        recorded = (manifest.run_id, manifest.target_name, *key)
        if identity != recorded or trial.status != entry.status:
            raise IntegrityError(
                f"{where}: holds run {trial.run_id}, target {trial.target_name}, case "
                f"{trial.case_id}, trial {trial.n}, attempt {trial.attempt}, {trial.status}; "
                "the manifest and the index say otherwise"
            )
        current = latest.get(key[:2])
        if current is None or trial.attempt > current[0].attempt:
            latest[key[:2]] = (trial, sha)
    return [latest[k] for k in sorted(latest)]


def declared_acceptance(run_dir: Path, manifest: RunManifest) -> dict[str, Acceptance] | None:
    """The acceptance criteria the run declared, per hazard of its variant, read from the
    snapshot's data files (no pack code runs). None when they no longer parse with this harness."""
    snapshot = Path(run_dir) / SNAPSHOT
    try:
        config = read_config(snapshot, manifest.pack_name)
        hazards = read_model(snapshot / manifest.pack_name / "hazards.yaml", HazardFile).hazards
    except PackError:
        return None
    variant = config.variants.get(manifest.variant)
    excluded = set(variant.exclude_hazards) if variant is not None else set()
    return {h.id: h.acceptance for h in hazards if h.id not in excluded}


def read_expectations(run_dir: Path) -> Any:
    """`expectations.json` as recorded; None when it is missing or not JSON (a report banner)."""
    try:
        return json.loads((Path(run_dir) / EXPECTATIONS).read_bytes())
    except (OSError, ValueError):
        return None


# ── Gradings ─────────────────────────────────────────────────────────────────────────────────────


def _graded_stamp(results_path: Path) -> str:
    """The folder name of an earlier grading: its `graded_at`, or "unknown"."""
    try:
        return stamp(datetime.fromisoformat(json.loads(results_path.read_bytes())["graded_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def write_grading(run_dir: Path, results: Results, report: str) -> Path:
    """Write `results.json` and `report.md` into the run folder. An earlier pair moves first to
    `history/<its graded_at>/`: no grading is ever lost. Returns the folder written."""
    run_dir = Path(run_dir)
    previous = [run_dir / name for name in (RESULTS, REPORT) if (run_dir / name).exists()]
    if previous:
        dest = unique_dir(run_dir / HISTORY, _graded_stamp(run_dir / RESULTS))
        for path in previous:
            os.replace(path, dest / path.name)
    write_json(run_dir / RESULTS, results)
    write_bytes(run_dir / REPORT, report.encode())
    return run_dir


def write_regrade(
    run_dir: Path, results: Results, report: str, *, clock: Callable[[], datetime] = now
) -> Path:
    """`--regrade-current`: the pair goes to a new `regrade-<timestamp>/`; the run's own pair,
    graded with the run's own definitions, stays untouched. Returns the folder written."""
    dest = unique_dir(Path(run_dir), f"regrade-{stamp(clock())}")
    write_json(dest / RESULTS, results, once=True)
    write_bytes(dest / REPORT, report.encode(), once=True)
    return dest
