"""What the local page shows, read from the files: the runs (manifest, execution, results, report,
transcripts, the snapshot's hazards and cases) and the packs (their YAML).

No pack code runs here: hazards, cases, targets and the pack's settings are data files, validated
by the core's own models. A run is named by its id only, never by a path: the id must have the
run folder's shape and name a folder directly under the runs folder.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from auditkit import runfolder, runner
from auditkit.cli import default_budget
from auditkit.pack import (
    PACK_NAME,
    PackError,
    pack_dir,
    pack_file,
    pack_files,
    pack_path,
    read_config,
    read_model,
)
from auditkit.report import CANNOT_SHOW, criterion
from auditkit.runfolder import IntegrityError
from auditkit.schema import (
    Case,
    Hazard,
    HazardFile,
    PackConfig,
    Results,
    RunManifest,
    Target,
    Trial,
)

RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}(?:_\d{1,4})?")  # runfolder.STAMP, maybe _2, _3…
MAX_TEXT = 64_000_000  # bytes of a report or results file the page reads (results: ~10 KB a trial)


class NotFound(Exception):
    """No such run, pack, case or trial."""


class Unreadable(Exception):
    """The files are there but do not read: a folder that fails its own record, a broken pack."""


# ── Files ─────────────────────────────────────────────────────────────────────────────────────────


def run_dir(runs_dir: Path, run_id: str) -> Path:
    """The folder of run `run_id`: a run id, naming a real folder (no symlink) directly under the
    runs folder, with a manifest."""
    if not RUN_ID.fullmatch(run_id):
        raise NotFound(f"no run {run_id[:80]!r}")
    root = Path(runs_dir)
    path = root / run_id
    try:
        ok = (
            path.is_dir()
            and not path.is_symlink()
            and path.resolve().parent == root.resolve()
            and _regular(path / runfolder.MANIFEST)
        )
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise NotFound(f"no run {run_id}")
    return path


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _read(path: Path) -> bytes | None:
    """A regular file of a run folder (a symlink is not read), or None when there is none."""
    if not _regular(path):
        return None
    data = path.read_bytes()
    if len(data) > MAX_TEXT:
        raise Unreadable(f"{path.name}: larger than {MAX_TEXT} bytes")
    return data


def _record(path: Path) -> tuple[RunManifest, Any]:
    try:
        manifest = runfolder.read_manifest(path)
        return manifest, runfolder.read_execution(path, manifest)
    except IntegrityError as e:
        raise Unreadable(f"integrity error: {e}") from e


def _results(path: Path) -> tuple[Results | None, str | None]:
    """The run's `results.json`, or None with the reason it cannot be shown."""
    data = _read(path / runfolder.RESULTS)
    if data is None:
        return None, None
    try:
        return Results.model_validate_json(data), None
    except ValidationError:
        return None, "results.json does not read with this harness: replay the run to regrade it"


# ── The report's own sentences ────────────────────────────────────────────────────────────────────

_MARKUP = re.compile(r"\\(.)|\*\*|`")  # the escapes, bold and code spans of report.inline()
_BANNER = re.compile(r"^\*\*⚠ (.+?)\.\*\* (.*)$")
_NUMBERED = re.compile(r"^\d+\. (.*)$")


def plain(markdown: str) -> str:
    """A report line as text: escapes undone, bold and code marks dropped. The page shows it as
    text, never as HTML."""
    return _MARKUP.sub(lambda m: m.group(1) or "", markdown)


def report_parts(text: str) -> dict[str, Any]:
    """From page one of report.md: the banners, the top findings (with their excerpts), what the
    evidence cannot show, and the Graded line, in the report's own words."""
    banners: list[dict[str, str]] = []
    findings: list[dict[str, Any]] = []
    limits: list[str] = []
    graded = None
    section = "head"
    for line in text.splitlines():
        if section == "head":
            if line.startswith("- **Graded** "):
                graded = plain(line.removeprefix("- **Graded** "))
            elif match := _BANNER.match(line):
                banners.append({"title": plain(match[1]), "text": plain(match[2])})
            elif line == "**Top findings**":
                section = "findings"
        elif section == "findings":
            if line.startswith(CANNOT_SHOW):
                section = "limits"
            elif match := _NUMBERED.match(line):
                findings.append({"text": plain(match[1]), "excerpt": []})
            elif line.startswith("   ") and findings:
                findings[-1]["excerpt"].append(plain(line.strip().removeprefix("> ")))
            elif line.strip():
                findings.append({"text": plain(line), "excerpt": []})
        elif line.startswith("> "):
            limits.append(plain(line[2:]))
        else:
            break
    return {"banners": banners, "findings": findings, "limits": limits, "graded": graded}


# ── Packs, from their data files ──────────────────────────────────────────────────────────────────


def pack_names(packs_dir: Path) -> list[str]:
    """The packs folder's packs: folders with a pack name and a pack.yaml."""
    root = Path(packs_dir)
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and PACK_NAME.fullmatch(d.name) and d.name != "core"
        if _regular(d / "pack.yaml")
    )


def _pack_data(
    packs_dir: Path, name: str
) -> tuple[PackConfig, list[Hazard], dict[str, list[Case]], list[Target]]:
    """A pack's settings, hazards, cases per variant and targets: data files only."""
    folder = pack_dir(packs_dir, name)
    config = read_config(packs_dir, name)
    allowed = {p.resolve() for p in pack_files(folder)}  # refuses a symlink in the pack
    hazards = read_model(pack_file(folder / "hazards.yaml", allowed), HazardFile).hazards
    dirs = {"default": "cases", **{k: v.cases_dir for k, v in config.variants.items()}}
    cases: dict[str, list[Case]] = {}
    for variant, cases_dir in dirs.items():
        path = pack_path(folder, folder, cases_dir)
        files = sorted(path.glob("*.yaml")) if path.is_dir() else []
        cases[variant] = [read_model(pack_file(p, allowed), Case) for p in files]
    targets = [
        read_model(pack_file(p, allowed), Target)
        for p in sorted((folder / "targets").glob("*.yaml"))
    ]
    return config, hazards, cases, targets


def _hazard(h: Hazard) -> dict[str, Any]:
    return {
        "id": h.id,
        "title": h.title,
        "severity": h.severity,
        "failure_mode": h.failure_mode,
        "harm": h.harm,
        "controls": [
            {"id": c.id, "requirement": c.requirement, "type": c.type} for c in h.controls
        ],
        "criterion": criterion(h.acceptance),
        "deferred_reason": h.deferred_reason,
    }


def list_packs(packs_dir: Path) -> list[dict[str, Any]]:
    out = []
    for name in pack_names(packs_dir):
        try:
            config = read_config(packs_dir, name)
        except PackError as e:
            out.append({"name": name, "error": str(e)})
            continue
        out.append({"name": name, "title": config.title, "version": config.version})
    return out


def budget_default() -> dict[str, Any]:
    source = "EVAL_BUDGET_USD" if os.environ.get("EVAL_BUDGET_USD") else "default"
    try:
        return {"usd": default_budget(), "source": source}
    except ValueError as e:
        return {"usd": None, "source": source, "error": str(e)}


def pack_parameters(packs_dir: Path, name: str) -> dict[str, Any]:
    """Everything a run of the pack would use: settings, models and budget (and where each comes
    from), targets, hazards with their controls and criteria, and the cases of each variant."""
    if name not in pack_names(packs_dir):
        raise NotFound(f"no pack {name[:80]!r}")
    try:
        config, hazards, cases, targets = _pack_data(packs_dir, name)
    except PackError as e:
        raise Unreadable(str(e)) from e
    excluded = {k: v.exclude_hazards for k, v in config.variants.items()}
    return {
        "name": name,
        "title": config.title,
        "version": config.version,
        "intended_use": config.intended_use,
        "out_of_scope": config.out_of_scope,
        "disclaimer": config.disclaimer,
        "depends_on": config.depends_on,
        "defaults": {
            "target": config.default_target,
            "repeats": config.default_repeats,
            "max_turns": config.max_turns,
        },
        "severity_scale": [s.model_dump() for s in config.severity_scale],
        "settings": {
            "side_channel": config.side_channel,
            "generic_checks": {k: v.model_dump() for k, v in config.generic_checks.items()},
            "data": config.data,
            "estimate": config.estimate.model_dump(),
        },
        "budget": budget_default(),
        "variants": [
            {
                "name": variant,
                "excluded_hazards": excluded.get(variant, []),
                "cases": [{"id": c.id, "title": c.title, "hazards": c.hazards} for c in found],
            }
            for variant, found in cases.items()
        ],
        "targets": [
            {
                "name": t.name,
                "kind": t.kind,
                "description": t.description,
                "exposes": t.exposes,
                "effort": t.effort,
                "max_tokens": t.max_tokens,
                "tools": t.tools,
                "models": [
                    {
                        "role": m.role,
                        "env_var": m.env_var,
                        "model": m.requested,
                        "source": m.env_var if os.environ.get(m.env_var) else "default",
                    }
                    for m in runner.model_infos(t)
                ],
            }
            for t in targets
        ],
        "hazards": [_hazard(h) for h in sorted(hazards, key=lambda h: h.id)],
    }


# ── Runs ──────────────────────────────────────────────────────────────────────────────────────────


def _graded_by(graded: str | None) -> str | None:
    if graded is None:
        return None
    return "replay" if graded.startswith("by replay") else "run"


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """Every run folder, the latest first, with what the runs rail shows."""
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    out = []
    for name in sorted((p.name for p in root.iterdir() if RUN_ID.fullmatch(p.name)), reverse=True):
        try:
            path = run_dir(root, name)
        except NotFound:
            continue
        try:
            manifest, execution = _record(path)
            results, _ = _results(path)
            report = _read(path / runfolder.REPORT)
        except (Unreadable, OSError) as e:
            out.append({"run_id": name, "error": str(e)})
            continue
        graded = report_parts(report.decode("utf-8", "replace"))["graded"] if report else None
        out.append(
            {
                "run_id": name,
                "pack": manifest.pack_name,
                "variant": manifest.variant,
                "target": manifest.target_name,
                "cases": len(manifest.case_ids),
                "repeats": manifest.repeats,
                "spent_usd": execution.spent_usd,
                "budget_usd": manifest.budget_usd,
                "status": execution.status,
                "graded_at": results.graded_at.isoformat() if results else None,
                "graded_by": _graded_by(graded) if results else None,
            }
        )
    return out


def _snapshot(path: Path, manifest: RunManifest) -> tuple[PackConfig, list[Hazard], list[Case]]:
    """The run's own hazards (its variant's) and cases, from its snapshot."""
    snapshot = path / runfolder.SNAPSHOT
    config, hazards, cases, _ = _pack_data(snapshot, manifest.pack_name)
    variant = config.variants.get(manifest.variant)
    excluded = set(variant.exclude_hazards) if variant is not None else set()
    by_id = {c.id: c for c in cases.get(manifest.variant, [])}
    return (
        config,
        [h for h in hazards if h.id not in excluded],
        [by_id[c] for c in manifest.case_ids if c in by_id],
    )


def _latest_entries(execution: Any) -> dict[tuple[str, int], Any]:
    latest: dict[tuple[str, int], Any] = {}
    for entry in execution.trials:
        key = (entry.case_id, entry.n)
        if key not in latest or entry.attempt > latest[key].attempt:
            latest[key] = entry
    return latest


def run_detail(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """One run as the Results screen shows it: facts, notices, the hazards × cases matrix with
    each hazard's verdict, controls and checks, the trials' grades, and the report's findings and
    limits. A snapshot or results file that does not read becomes a notice, not an error."""
    path = run_dir(runs_dir, run_id)
    manifest, execution = _record(path)
    notices: list[dict[str, str]] = []
    try:
        runfolder.verify_snapshot(path, manifest)
    except IntegrityError as e:
        notices.append(
            {"title": "Integrity error", "text": f"{e}. A replay refuses this folder (exit 5)."}
        )
    title, version, hazards, cases = manifest.pack_name, manifest.pack_version, [], []
    try:
        config, hazards, cases = _snapshot(path, manifest)
        title = config.title
    except PackError as e:
        notices.append({"title": "Snapshot unreadable", "text": str(e)})
    results, problem = _results(path)
    report = _read(path / runfolder.REPORT)
    parts = report_parts(report.decode("utf-8", "replace") if report and results else "")
    notices = [*parts["banners"], *notices]
    if problem:
        notices.append({"title": "Results unreadable", "text": problem})
    elif results is None:
        notices.append(
            {"title": "Not graded", "text": "No results.json yet: replay the run to grade it."}
        )

    verdicts = {v.hazard_id: v for v in results.hazards} if results else {}
    cells = {(c.case_id, c.hazard_id): c for c in results.case_hazards} if results else {}
    stats = {c.case_id: c for c in results.cases} if results else {}
    checks = results.checks if results else []
    statuses = Counter(g.status for g in results.trials) if results else Counter()
    entries = _latest_entries(execution)
    served = execution.served_models

    def cell(case_id: str, h: Hazard) -> dict[str, Any] | None:
        found = cells.get((case_id, h.id))
        if found is None:
            return None
        case_stats = stats.get(case_id)
        return {
            "status": found.status,
            "reason": found.reason,
            "trials": len(found.trial_statuses),
            "passed": found.trial_statuses.count("pass"),
            "valid": case_stats.valid_trials if case_stats else 0,
            "needed": h.acceptance.min_valid_trials_per_case,
        }

    def hazard(h: Hazard) -> dict[str, Any]:
        v = verdicts.get(h.id)
        return {
            **_hazard(h),
            "verdict": v.verdict if v else None,
            "reasons": v.reasons if v else [],
            "cases_tagged": v.cases_tagged if v else None,
            "cases_counted": v.cases_counted if v else None,
            "cases_failed": v.cases_failed if v else None,
            "observed_rate": v.observed_rate if v else None,
            "upper_bound": v.upper_bound if v else None,
            "confidence": h.acceptance.confidence,
            "checks": [
                {"id": c.id, "method": c.method, "kind": c.kind}
                for c in checks
                if h.id in c.hazards
            ],
            "cells": {c: cell(c, h) for c in manifest.case_ids},
        }

    trials = []
    for grade in results.trials if results else []:
        entry = entries.get((grade.case_id, grade.n))
        trials.append(
            {
                **grade.model_dump(mode="json", exclude={"transcript_sha256"}),
                "stop_reason": entry.stop_reason if entry else None,
                "cost_usd": entry.cost_usd if entry else None,
            }
        )
    return {
        "run_id": manifest.run_id,
        "pack": {"name": manifest.pack_name, "title": title, "version": version},
        "variant": manifest.variant,
        "target": manifest.target_name,
        "created_at": manifest.created_at.isoformat(),
        "status": execution.status,
        "facts": {
            "planned": len(manifest.case_ids) * manifest.repeats,
            "counted": statuses["valid"],
            "invalid": statuses["invalid"],
            "error": statuses["error"],
            "not_run": statuses["not_run"],
            "spent_usd": execution.spent_usd,
            "budget_usd": manifest.budget_usd,
            "estimate_usd": manifest.estimate_usd,
            "models": [
                {"role": m.role, "requested": m.requested, "served": served.get(m.role, [])}
                for m in manifest.models
            ],
            "graded_at": results.graded_at.isoformat() if results else None,
            "graded_by": _graded_by(parts["graded"]) if results else None,
            "graded": parts["graded"],
        },
        "notices": notices,
        "manifest": manifest.model_dump(mode="json"),
        "cases": [{"id": c.id, "title": c.title, "hazards": c.hazards} for c in cases],
        "case_ids": manifest.case_ids,
        "repeats": manifest.repeats,
        "hazards": [hazard(h) for h in sorted(hazards, key=lambda h: h.id)],
        "trials": trials,
        "case_stats": [c.model_dump(mode="json") for c in results.cases] if results else [],
        "findings": parts["findings"],
        "limits": parts["limits"],
        "has_report": report is not None,
    }


def transcript(runs_dir: Path, run_id: str, case_id: str, n: int) -> dict[str, Any]:
    """The latest attempt of trial `n` of `case_id`, read through the run's index with every
    hash checked (`runfolder.load_trials`)."""
    path = run_dir(runs_dir, run_id)
    manifest, execution = _record(path)
    if case_id not in manifest.case_ids or not 1 <= n <= manifest.repeats:
        raise NotFound(f"no trial {n} of case {case_id[:80]!r} in run {run_id}")
    try:
        trials: list[tuple[Trial, str]] = runfolder.load_trials(path, manifest, execution)
    except IntegrityError as e:
        raise Unreadable(f"integrity error: {e}") from e
    for trial, sha in trials:
        if (trial.case_id, trial.n) == (case_id, n):
            return {"sha256": sha, "trial": trial.model_dump(mode="json")}
    raise NotFound(f"trial {n} of case {case_id} has no transcript")


def report_text(runs_dir: Path, run_id: str) -> str:
    data = _read(run_dir(runs_dir, run_id) / runfolder.REPORT)
    if data is None:
        raise NotFound(f"run {run_id} has no report.md yet")
    return data.decode("utf-8", "replace")
