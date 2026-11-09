"""Test-data lint: every issue of a pack at once, before any API call.

Built on the loader's step functions, but never stops at the first problem: each rule reports what
it finds and the others still run. An exception raised by pack code becomes an issue, not a crash.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from math import isfinite
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from auditkit import generic_checks
from auditkit.checks import Applies, CheckDef, collect
from auditkit.pack import (
    PackError,
    check_spec,
    dependency_closure,
    import_pack_module,
    pack_dir,
    pack_file,
    pack_files,
    pack_path,
    protocol_errors,
    read_config,
    read_model,
    read_yaml,
)
from auditkit.schema import (
    KINDS,
    Case,
    CheckSpec,
    Control,
    Hazard,
    HazardFile,
    LintIssue,
    Target,
    json_scalars,
)
from auditkit.stats import cases_needed, percent

COVERAGE = {"L16", "L20", "L21"}  # warnings, not errors, when replaying an old run
MIN_SECRET_LENGTH = 6  # canaries and forbidden strings: shorter ones match by chance
# the values of each typed side-channel key, as the simulator reads them; null is always allowed
OF_TYPE: dict[str, Callable[[Any], bool]] = {**KINDS, "string": lambda v: isinstance(v, str)}


class _Issues:
    def __init__(self, packs_dir: Path, coverage_as_warnings: bool) -> None:
        self.packs_dir = packs_dir
        self.coverage_as_warnings = coverage_as_warnings
        self.found: dict[tuple[str, str, str, str], LintIssue] = {}  # the same issue counts once

    def where(self, path: Path, item: str = "") -> str:
        try:
            shown = path.relative_to(self.packs_dir).as_posix()
        except ValueError:
            shown = str(path)
        return f"{shown} {item}".strip()

    def add(self, code: str, level: str, where: str, message: str) -> None:
        if level == "error" and self.coverage_as_warnings and code in COVERAGE:
            level = "warning"
        issue = LintIssue(code=code, level=level, where=where, message=message)
        self.found.setdefault((code, level, where, message), issue)

    def error(self, code: str, where: str, message: str) -> None:
        self.add(code, "error", where, message)

    def warning(self, code: str, where: str, message: str) -> None:
        self.add(code, "warning", where, message)


def lint_pack(
    packs_dir: Path,
    name: str,
    *,
    variant: str = "default",
    target: str | None = None,
    repeats: int | None = None,
    max_turns: int | None = None,
    core_checks: Sequence[CheckDef] | None = None,
    coverage_as_warnings: bool = False,
) -> list[LintIssue]:
    """Every issue of one pack, errors first. Files, ids, references and targets are checked for
    every target and every variant's data; cases and coverage for `variant` and `target` (default:
    the pack's `default_target`); feasibility for `repeats` (default: the pack's)."""
    packs_dir = Path(packs_dir)
    out = _Issues(packs_dir, coverage_as_warnings)
    _lint(out, packs_dir, name, variant, target, repeats, max_turns, core_checks)
    return sorted(
        out.found.values(), key=lambda i: (i.level != "error", i.code, i.where, i.message)
    )


def _lint(
    out: _Issues,
    packs_dir: Path,
    name: str,
    variant: str,
    target_name: str | None,
    repeats: int | None,
    max_turns: int | None,
    core_checks: Sequence[CheckDef] | None,
) -> None:
    # ── L01-L03: the folder, pack.yaml, dependencies ──────────────────────────────────────────────
    try:
        folder = pack_dir(packs_dir, name)
        config = read_config(packs_dir, name)
    except PackError as e:
        out.error("L01", name, str(e))
        return
    pack_yaml = out.where(folder / "pack.yaml")
    try:
        closure: list[str] | None = dependency_closure(packs_dir, name)
    except PackError as e:
        out.error("L03", pack_yaml, str(e))
        closure = None
    for current in closure or [name]:
        try:
            pack_files(packs_dir / current)
        except PackError as e:  # a symlink: no file of the pack can be trusted
            out.error("L01", current, str(e))
            return
    allowed = {p.resolve() for p in pack_files(folder)}

    if "default" in config.variants:
        out.error("L01", pack_yaml, "'default' is the built-in variant (cases/ + data)")
    if variant == "default":
        cases_dir, excluded = "cases", []
    elif variant in config.variants:
        cases_dir, excluded = (
            config.variants[variant].cases_dir,
            config.variants[variant].exclude_hazards,
        )
    else:
        out.error("L01", pack_yaml, f"no variant {variant!r}")
        return

    # ── Hazards and controls (L01, L02, L10, L11) ─────────────────────────────────────────────────
    hazards_path = folder / "hazards.yaml"
    try:
        hazard_list = read_model(pack_file(hazards_path, allowed), HazardFile).hazards
    except PackError as e:
        out.error("L01", out.where(hazards_path), str(e))
        hazard_list = []
    hazards: dict[str, Hazard] = {}
    controls: dict[str, Control] = {}
    severities = {s.name for s in config.severity_scale}
    for h in hazard_list:
        where = out.where(hazards_path, h.id)
        if h.id in hazards:
            out.error("L02", where, f"hazard {h.id} defined twice")
        hazards[h.id] = h
        for c in h.controls:
            if c.id in controls:
                out.error("L02", where, f"control {c.id} defined twice")
            controls[c.id] = c
        if h.severity not in severities:
            out.error("L11", where, f"severity {h.severity!r} is not in the severity_scale")
    for hid in excluded:
        if hid not in hazards:
            out.error("L10", pack_yaml, f"variant {variant} excludes unknown hazard {hid}")
    active = {k: h for k, h in hazards.items() if k not in excluded}

    # ── Cases of the variant (L01, L02) ───────────────────────────────────────────────────────────
    cases: dict[str, Case] = {}
    case_files: dict[str, Path] = {}
    try:
        cases_path = pack_path(folder, folder, cases_dir)
        if not cases_path.is_dir():
            raise PackError(f"{cases_path}: no such cases folder")
        found = sorted(cases_path.glob("*.yaml"))
    except PackError as e:
        out.error("L01", pack_yaml, str(e))
        found = []
    for path in found:
        try:
            case = _read_case(out, pack_file(path, allowed))
        except PackError as e:
            out.error("L01", out.where(path), str(e))
            continue
        if case.id in cases:
            out.error("L02", out.where(path), f"case {case.id} defined twice")
            continue
        if path.stem != case.id:
            out.error("L02", out.where(path), f"the file name differs from the case id {case.id!r}")
        cases[case.id] = case
        case_files[case.id] = path
    if not found:
        out.error("L21", pack_yaml, f"variant {variant}: no case file in {cases_dir!r}")

    # ── Targets (L01, L02, L05) ───────────────────────────────────────────────────────────────────
    targets_path = folder / "targets"
    targets: dict[str, Target] = {}
    target_files: dict[str, Path] = {}
    for path in sorted(targets_path.glob("*.yaml")):
        try:
            t = read_model(pack_file(path, allowed), Target)
        except PackError as e:
            out.error("L01", out.where(path), str(e))
            continue
        if t.name in targets:
            out.error("L02", out.where(path), f"target {t.name} defined twice")
            continue
        targets[t.name] = t
        target_files[t.name] = path
    selected_name = target_name or config.default_target
    selected = targets.get(selected_name)
    if selected is None:
        out.error("L05", out.where(targets_path), f"no target {selected_name!r}")

    # ── The pack's code (L03) ─────────────────────────────────────────────────────────────────────
    module = None
    if closure is not None:
        try:
            module = import_pack_module(packs_dir, name)
        except PackError as e:
            out.error("L03", out.where(folder / "pack.py"), str(e))
    if module is not None and not _protocol_ok(out, folder, module):
        module = None

    # ── Data of every variant (L04) ───────────────────────────────────────────────────────────────
    data: dict[str, BaseModel] = {}
    by_variant = {"default": dict(config.data)}
    by_variant |= {
        k: {**config.data, **v.data} for k, v in config.variants.items() if k != "default"
    }
    for vname, files in by_variant.items():
        for key, file in sorted(files.items()):
            try:
                path = pack_file(pack_path(folder, folder, file), allowed)
            except PackError as e:
                out.error("L04", pack_yaml, f"variant {vname}, data {key}: {e}")
                continue
            if module is None:
                continue
            if key not in module.DATA_MODELS:
                out.error("L04", pack_yaml, f"data {key!r}: DATA_MODELS has no model for it")
                continue
            try:
                value = read_model(path, module.DATA_MODELS[key])
            except PackError as e:
                out.error("L04", out.where(path), str(e))
                continue
            if vname == variant:
                data[key] = value

    # ── Checks and tools (L02, L10, L23) ──────────────────────────────────────────────────────────
    specs: list[tuple[CheckSpec, Applies]] = []
    tools: set[str] = set()
    if module is not None:
        specs, tools = _checks_and_tools(out, config, module, core_checks, pack_yaml)

    # ── References (L10) ──────────────────────────────────────────────────────────────────────────
    for case_id, case in cases.items():
        where = out.where(case_files[case_id])
        for hid in case.hazards:
            if hid not in hazards:
                out.error("L10", where, f"unknown hazard {hid}")
            elif hid in excluded:
                out.error("L10", where, f"tags {hid}, which variant {variant} excludes")
        for s in case.stimuli:
            if s.tool is not None and module is not None and s.tool not in tools:
                out.error("L10", where, f"stimulus {s.id}: unknown tool {s.tool!r}")
    for spec, _ in specs:
        for hid in spec.hazards:
            if hid not in hazards:
                out.error("L10", spec.id, f"links unknown hazard {hid}")
        for cid in spec.controls:
            if cid not in controls:
                out.error("L10", spec.id, f"links unknown control {cid}")
            elif cid.split(".")[0] not in spec.hazards:
                out.error(
                    "L10", spec.id, f"control {cid} belongs to a hazard the check does not link"
                )
    for tname, t in targets.items():
        for cid in t.control_implementations:
            if cid not in controls:
                out.error("L10", out.where(target_files[tname]), f"unknown control {cid}")

    # ── Targets (L05, L06) ────────────────────────────────────────────────────────────────────────
    for tname, t in targets.items():
        where = out.where(target_files[tname])
        _lint_target(out, where, t, folder, targets_path, allowed, tools if module else None)
        for cid, implementation in t.control_implementations.items():
            control = controls.get(cid)
            if implementation == "prompt" and control and control.type == "inherent_design":
                out.error(
                    "L06", where, f"{cid} is typed inherent_design but implemented in a prompt"
                )

    # ── Cases: ground truth, expectations, the pack's own rules, stimuli (L12-L15) ────────────────
    expected: dict[str, Any] = {}
    for case_id, case in cases.items():
        where = out.where(case_files[case_id])
        cap = case.max_turns or max_turns or config.max_turns
        _lint_stimuli(out, where, case, config.side_channel, cap)
        if module is None:
            continue
        exp = _case_expectations(out, where, module, case, data)
        if exp is not None:
            expected[case_id] = exp

    # ── Coverage of the selected variant and target (L16, L20, L21, L24-L27, L31) ─────────────────
    if module is None:
        return
    reps = repeats or config.default_repeats
    _lint_coverage(out, active, cases, case_files, expected, specs, selected, reps)


def _read_case(out: _Issues, path: Path) -> Case:
    """The case in `path`. A stimulus id given twice is an L02, not an invalid file: the later
    stimuli of that id are left out before validation, so the case still counts."""
    raw = read_yaml(path)
    if isinstance(raw, dict) and isinstance(raw.get("stimuli"), list):
        seen: set[str] = set()
        kept = []
        for s in raw["stimuli"]:
            sid = s.get("id") if isinstance(s, dict) else None
            if isinstance(sid, str):
                if sid in seen:
                    out.error("L02", out.where(path), f"stimulus {sid} defined twice")
                    continue
                seen.add(sid)
            kept.append(s)
        raw = {**raw, "stimuli": kept}
    try:
        return Case.model_validate(raw)
    except ValidationError as e:  # as read_model reports it
        raise PackError(f"{path}: {e}") from e


def _protocol_ok(out: _Issues, folder: Path, module: Any) -> bool:
    errors = protocol_errors(module)
    for message in errors:
        out.error("L03", out.where(folder / "pack.py"), message)
    return not errors


def _checks_and_tools(
    out: _Issues,
    config: Any,
    module: Any,
    core_checks: Sequence[CheckDef] | None,
    pack_yaml: str,
) -> tuple[list[tuple[CheckSpec, Applies]], set[str]]:
    pack_defs, tool_defs = collect(module)
    core_defs = collect(generic_checks)[0] if core_checks is None else list(core_checks)
    core = {f"core.{d.id}": d for d in core_defs}
    for check_id in config.generic_checks:
        if check_id not in core:
            out.error("L10", pack_yaml, f"generic_checks binds unknown core check {check_id}")
        elif core[check_id].kind == "validity":
            out.error(
                "L10", pack_yaml, f"generic_checks binds {check_id}, a validity check (always on)"
            )

    specs: dict[str, tuple[CheckSpec, Applies]] = {}

    def add(d: CheckDef, check_id: str, hazards: Sequence[str], controls: Sequence[str], origin):
        try:
            spec = check_spec(d, check_id, hazards, controls, origin)
        except PackError as e:
            out.error("L23", check_id, str(e))
            return
        if spec.id in specs:
            out.error("L02", check_id, f"check {spec.id} defined twice")
            return
        specs[spec.id] = (spec, d.applies)

    for d in core_defs:
        binding = config.generic_checks.get(f"core.{d.id}")
        if d.kind == "validity":
            add(d, f"core.{d.id}", (), (), "core")
        elif binding is not None:
            add(d, f"core.{d.id}", binding.hazards, binding.controls, "core")
    for d in pack_defs:
        add(d, f"{config.name}.{d.id}", d.hazards, d.controls, "pack")

    tools: set[str] = set()
    for t in tool_defs:
        if t.name in tools:
            out.error("L02", "pack.py", f"tool {t.name} defined twice")
        tools.add(t.name)
    return [specs[k] for k in sorted(specs)], tools


def _lint_target(
    out: _Issues,
    where: str,
    t: Target,
    folder: Path,
    targets_path: Path,
    allowed: set[Path],
    tools: set[str] | None,
) -> None:
    if t.kind != "prompted_model":
        out.error("L05", where, f"kind {t.kind}: designed, not built")
    elif t.system_prompt_file is None:
        out.error("L05", where, "a prompted_model target needs a system_prompt_file")
    else:
        try:
            pack_file(pack_path(folder, targets_path, t.system_prompt_file), allowed)
        except PackError as e:
            out.error("L05", where, f"system_prompt_file: {e}")
    if tools is not None:
        for name in t.tools:
            if name not in tools:
                out.error("L05", where, f"offers unknown tool {name!r}")
        if t.outcome.source == "tool_call" and t.outcome.tool_name not in tools:
            out.error("L05", where, f"outcome tool {t.outcome.tool_name!r} is not a pack tool")
    if t.end_when == "outcome":
        if t.outcome.source == "none":
            out.error("L05", where, "end_when: outcome needs an outcome source other than none")
        if not {"final_outcome", "tool_calls"} & set(t.exposes):
            out.error("L05", where, "end_when: outcome needs final_outcome or tool_calls exposed")


def _leaves(value: Any, path: str) -> Iterator[tuple[str, Any]]:
    """(path, value) of every leaf of a YAML value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def _keys(value: Any, path: str) -> Iterator[tuple[str, Any]]:
    """(path, key) of every mapping key of a YAML value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key
            yield from _keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _keys(item, f"{path}[{index}]")


def _lint_stimuli(
    out: _Issues, where: str, case: Case, side_channel: Mapping[str, str], cap: int
) -> None:
    leaves = list(_leaves(case.environment, "environment"))
    for path, value in leaves:  # mocked tools return these values: a result must be JSON
        json_ok = isinstance(value, str | int | bool | None) or (
            isinstance(value, float) and isfinite(value)
        )
        if not json_ok:
            out.error("L15", where, f"{path} is a {type(value).__name__}, not JSON: quote it")
    for path, key in _keys(case.environment, "environment"):
        if not isinstance(key, str):  # json.dumps refuses a date, rewrites an int, bool or None
            message = f"key {path}.{key} is not a string ({type(key).__name__}): quote it"
            out.error("L15", where, message)
    texts = [value for _, value in leaves if isinstance(value, str)]
    scalars = list(json_scalars(case.environment))  # a result carries numbers and keys too
    for s in case.stimuli:
        label = f"stimulus {s.id}"
        if s.on_hidden is not None and not set(s.on_hidden) <= set(side_channel):
            extra = sorted(set(s.on_hidden) - set(side_channel))
            out.error("L15", where, f"{label}: on_hidden keys {extra} are not in side_channel")
        for key, value in (s.on_hidden or {}).items():  # another type never matches: no delivery
            kind = side_channel.get(key, "any")
            if value is not None and kind in OF_TYPE and not OF_TYPE[kind](value):
                message = f"{label}: on_hidden {key}={value!r} is neither null nor of type {kind}"
                out.error("L15", where, message)
        if s.at_turn is not None and s.at_turn > cap:
            out.error("L15", where, f"{label}: at_turn {s.at_turn} > max_turns {cap}")
        # counted as the check reads it, separators dropped: "P-I-N-E" is "pine", in any "spine"
        if s.canary is not None and len(generic_checks.fold_canary(s.canary)) < MIN_SECRET_LENGTH:
            message = f"{label}: canary shorter than {MIN_SECRET_LENGTH} letters or digits"
            out.error("L15", where, message)
        # read as core.injection_canary_absent reads the agent's output, by the same function: the
        # schema refuses a plain copy in the text, not "P-I-N-E-A-P-P-L-E", which the check reads
        on_tool = s.channel == "tool_result"
        payload, field = (scalars, "environment") if on_tool else ([s.text or ""], "text")
        if s.canary and any(generic_checks.holds_canary(text, s.canary) for text in payload):
            out.error("L15", where, f"{label}: the canary appears in the {field} (payload)")
        if on_tool and s.marker and not any(s.marker in text for text in texts):
            out.error("L15", where, f"{label}: marker {s.marker!r} is not in the environment")
    # counted as core.forbidden_strings_absent reads it, like a canary: "R-9001" is "r9001", in any
    # "for 9001", and a string of invisible characters is "", in every turn
    for secret in case.forbidden_strings:
        if len(generic_checks.fold_canary(secret)) < MIN_SECRET_LENGTH:
            message = f"shorter than {MIN_SECRET_LENGTH} letters or digits"
            out.error("L15", where, f"forbidden string {secret!r} {message}")


def _case_expectations(
    out: _Issues, where: str, module: Any, case: Case, data: Mapping[str, BaseModel]
) -> Any:
    try:
        gt = module.GroundTruth.model_validate(case.ground_truth)
    except ValidationError as e:
        out.error("L12", where, f"ground_truth: {e}")
        return None
    except Exception as e:  # the pack model's own validator broke
        out.error("L12", where, f"ground_truth: {type(e).__name__}: {e}")
        return None
    try:
        expected = module.derive_expectations(case, gt, data)
    except Exception as e:  # pack code: reported, never raised
        out.error("L13", where, f"derive_expectations: {type(e).__name__}: {e}")
        return None
    if not isinstance(expected, module.Expected):
        out.error("L13", where, "derive_expectations did not return an Expected")
        return None
    lint_case: Callable[..., Sequence[str]] | None = getattr(module, "lint_case", None)
    if lint_case is not None:
        try:
            messages = lint_case(case, gt, expected, data)
            for message in [messages] if isinstance(messages, str) else messages:
                out.error("L14", where, str(message))
        except Exception as e:
            out.error("L14", where, f"lint_case: {type(e).__name__}: {e}")
    return expected


def _lint_coverage(
    out: _Issues,
    hazards: Mapping[str, Hazard],
    cases: Mapping[str, Case],
    case_files: Mapping[str, Path],
    expected: Mapping[str, Any],
    specs: Sequence[tuple[CheckSpec, Applies]],
    target: Target | None,
    repeats: int,
) -> None:
    linked = {hid: [(s, a) for s, a in specs if hid in s.hazards] for hid in hazards}

    for case_id, case in cases.items():  # L16: no padding tags
        if case_id not in expected:
            continue  # its expectations failed: reported above
        for hid in case.hazards:
            if hid not in hazards:
                continue
            try:
                applies = any(a(case, expected[case_id]) for _, a in linked[hid])
            except Exception as e:
                out.error(
                    "L16", out.where(case_files[case_id]), f"applies: {type(e).__name__}: {e}"
                )
                continue
            if not applies:
                out.error(
                    "L16", out.where(case_files[case_id]), f"tags {hid}, but no check of it applies"
                )

    for hid, h in hazards.items():
        if h.deferred_reason:
            continue
        counting = [s for s, _ in linked[hid] if s.kind != "validity"]
        if not counting:
            out.error("L20", hid, "no conformance or hazard check links this hazard")
        tagged = sum(hid in c.hazards for c in cases.values())
        if not tagged:
            out.error("L21", hid, "no case of the variant tags this hazard")
        for control in h.controls:
            if not any(control.id in s.controls for s, _ in specs):
                out.warning("L24", hid, f"control {control.id} is verified by no check")
            if target is not None and control.id not in target.control_implementations:
                out.warning("L26", hid, f"control {control.id}: implementation not declared")
        acc = h.acceptance
        bound = cases_needed(0, 0, acc.max_rate, acc.confidence)  # None: beyond 10,000
        need = None if bound is None else max(bound, acc.min_cases)
        if need is None or tagged < need:
            out.warning(
                "L25",
                hid,
                f"{tagged} case(s) tagged; met needs "
                f"{'over 10,000' if need is None else f'≥ {need}'} failure-free cases at "
                f"≤ {percent(acc.max_rate)} / {percent(acc.confidence)}",
            )
        if repeats < acc.min_valid_trials_per_case:
            out.warning(
                "L25",
                hid,
                f"{repeats} repeat(s) < {acc.min_valid_trials_per_case} valid trials per case: "
                "can be not met, never met",
            )
        if counting and not any(s.method == "deterministic" for s in counting):
            out.warning("L31", hid, "heuristic checks only: it cannot be met")

    if target is not None:  # L27: what the target cannot show
        exposes = set(target.exposes) | {"side_channel", "ground_truth"}
        for spec, _ in specs:
            if spec.hazards and (missing := sorted(set(spec.needs) - exposes)):
                out.warning(
                    "L27", spec.id, f"needs {', '.join(missing)}, not exposed by {target.name}"
                )
