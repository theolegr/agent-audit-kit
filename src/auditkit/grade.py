"""Grading: the raw outcome, one trial's check results and status, a run's verdicts.

An outcome is computed at grading time, never stored in a transcript: a transcript holds raw
evidence only. The live runner and `--replay` grade through the same functions.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from auditkit import __version__, stats
from auditkit.checks import CheckContext, NoOutcome, StimulusNotDelivered
from auditkit.pack import BoundCheck, Pack
from auditkit.schema import (
    CaseHazardStatus,
    CaseStats,
    CheckOutcome,
    CheckResult,
    EvidenceRef,
    Hazard,
    HazardVerdict,
    LintIssue,
    OutcomeSource,
    Results,
    Target,
    Transcript,
    Trial,
    TrialGrade,
)
from auditkit.tools import now

FENCE = re.compile(r"```[A-Za-z0-9_+-]*(.*?)```", re.DOTALL)  # ```json … ```, or a bare fence


def _not_json(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


_DECODER = json.JSONDecoder(parse_constant=_not_json)  # NaN and Infinity are refused


# ── The raw outcome ───────────────────────────────────────────────────────────────────────────────


def _last_object(text: str) -> dict[str, Any] | None:
    """The last top-level JSON object in free text. Each `{` is tried in turn and an object found
    is skipped whole, so an object nested in it is never taken for the last one."""
    found, start = None, text.find("{")
    while start != -1:
        try:
            found, end = _DECODER.raw_decode(text, start)  # a value that starts with { is an object
        except (ValueError, RecursionError):
            start = text.find("{", start + 1)
            continue
        start = text.find("{", end)
    return found


def extract_outcome(transcript: Transcript, source: OutcomeSource) -> dict[str, Any] | None:
    """The raw final outcome, before the pack parses it; None when there is none.
    - `final_json`: the last JSON object in the last agent turn, looked for in fenced blocks first;
    - `tool_call`: the arguments of the named tool's last call;
    - `response_field`: the last agent turn's `final_output`;
    - `none`: never one.
    A `ValueError` when the outcome nests too deep to copy."""
    last = transcript.last_agent_turn()
    raw: dict[str, Any] | None = None
    if source.source == "final_json" and last is not None:
        fenced = (_last_object(block) for block in reversed(FENCE.findall(last.text)))
        raw = next((found for found in fenced if found is not None), None)
        if raw is None:
            raw = _last_object(last.text)
    elif source.source == "tool_call":
        calls = transcript.tool_calls(source.tool_name)
        raw = calls[-1][1].arguments if calls else None
    elif source.source == "response_field" and last is not None:
        raw = last.final_output
    try:
        return copy.deepcopy(raw)  # parse_outcome may edit it: the evidence stays as recorded
    except RecursionError:
        raise ValueError("the final outcome nests too deep") from None


def _describe(e: ValueError) -> str:
    """A parse error on one line: pydantic's own message spans lines and ends with a URL."""
    if isinstance(e, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'outcome'}: {err['msg']}"
            for err in e.errors(include_url=False)
        )
    return str(e)


def _outcome(
    pack: Pack, target: Target, trial: Trial
) -> tuple[BaseModel | None, str | None, str | None]:
    """(outcome, outcome_error, parser_error). A `ValueError` from the pack's `parse_outcome`
    (pydantic's ValidationError is one) means the agent's output does not parse. Any other
    exception, or a result that is not a model, is a bug in the pack: a harness error."""
    source = target.outcome
    try:
        raw = extract_outcome(trial.transcript, source)
    except ValueError as e:  # nested too deep to copy: the agent's output
        return None, str(e), None
    if raw is None:
        missing = None if source.source == "none" else f"no final outcome found ({source.source})"
        return None, missing, None
    try:
        parsed = pack.module.parse_outcome(raw, pack.data)
    except ValueError as e:
        return None, f"the final outcome does not parse: {_describe(e)}", None
    except Exception as e:
        return None, None, f"the pack's parse_outcome raised {type(e).__name__}: {e}"
    if not isinstance(parsed, BaseModel):
        kind = type(parsed).__name__
        return None, None, f"the pack's parse_outcome returned {kind}, not a model"
    return parsed, None, None


# ── One trial ─────────────────────────────────────────────────────────────────────────────────────


def check_context(pack: Pack, target: Target, trial: Trial, *, max_turns: int) -> CheckContext:
    """What every check of `trial` receives, its outcome parsed once; pack tests use it to call
    one check directly. `max_turns` is the run's user-turn cap (a case's own wins)."""
    if trial.case_id not in pack.cases:
        raise ValueError(f"trial of case {trial.case_id}: pack {pack.config.name} has no such case")
    case = pack.cases[trial.case_id]
    outcome, outcome_error, parser_error = _outcome(pack, target, trial)
    return CheckContext(
        case=case,
        ground_truth=pack.ground_truth[case.id],
        expected=pack.expected[case.id],
        data=pack.data,
        trial=trial,
        side_channel_keys=list(pack.config.side_channel),
        outcome=outcome,
        outcome_error=outcome_error,
        parser_error=parser_error,
        target=target,
        max_turns=case.max_turns or max_turns,
    )


def _error(check_id: str, note: str) -> CheckResult:
    return CheckResult(check_id=check_id, outcome="error", note=note, fault="harness")


def run_check(bound: BoundCheck, ctx: CheckContext) -> CheckResult | None:
    """One check on one trial. None means no row: the check does not apply, or it needs an
    outcome there is none of (a missing outcome is counted once, by `core.output_schema_valid`)."""
    spec = bound.spec
    try:
        if not bound.applies(ctx.case, ctx.expected):
            return None
    except Exception as e:
        return _error(spec.id, f"applies raised {type(e).__name__}: {e}")
    if missing := sorted(set(spec.needs) - set(ctx.trial.exposes)):
        note = f"the target does not expose {', '.join(missing)}"
        return CheckResult(check_id=spec.id, outcome="not_evaluable", note=note)
    # every check gets its own copy of the evidence: one that edits it cannot change the next one's
    try:
        trial = ctx.trial.model_copy(deep=True)
        outcome = ctx.outcome.model_copy(deep=True) if ctx.outcome is not None else None
    except RecursionError:
        return _error(spec.id, "the evidence nests too deep to copy")
    try:
        out = bound.fn(trial.transcript, dataclasses.replace(ctx, trial=trial, outcome=outcome))
    except NoOutcome:
        return None
    except StimulusNotDelivered as e:
        return CheckResult(check_id=spec.id, outcome="invalid", note=str(e), fault="test_data")
    except Exception as e:  # a check that breaks is a harness error, kept as evidence
        return _error(spec.id, f"{type(e).__name__}: {e}")
    if not isinstance(out, CheckOutcome):
        return _error(spec.id, f"returned {type(out).__name__}, not a CheckOutcome")
    try:  # validated again: a model can be edited after it was built
        out = CheckOutcome.model_validate(dict(out))
    except ValidationError as e:
        return _error(spec.id, f"returned an inconsistent outcome: {_describe(e)}")
    if out.outcome == "not_evaluable":
        return _error(spec.id, "returned not_evaluable, which only the observability gate gives")
    turns = len(ctx.trial.transcript.turns)
    if out.evidence_turn is not None and not 0 <= out.evidence_turn < turns:
        return _error(spec.id, f"evidence turn {out.evidence_turn} is not in the transcript")
    if spec.kind == "validity" and out.outcome == "fail":
        return _error(spec.id, f"a validity check returned fail: {out.note}")
    return CheckResult(check_id=spec.id, **out.model_dump())


def grade_trial(
    pack: Pack, target: Target, trial: Trial, transcript_sha256: str, *, max_turns: int
) -> TrialGrade:
    """One trial's results and status. `max_turns` is the run's
    user-turn cap (a case's own wins). Results are kept as computed, even when the trial does not
    count: the report shows them for review."""
    ids: dict[str, Any] = {
        "case_id": trial.case_id,
        "n": trial.n,
        "attempt": trial.attempt,
        "transcript_sha256": transcript_sha256,
    }
    if trial.status == "aborted":  # stopped by the budget: in no denominator
        return TrialGrade(**ids, status="not_run", reasons=["stopped by the budget"])
    if trial.status == "error":
        fault, error = trial.fault or "fault not recorded", trial.error or "no message"
        return TrialGrade(**ids, status="error", reasons=[f"the trial errored ({fault}): {error}"])

    ctx = check_context(pack, target, trial, max_turns=max_turns)
    results = sorted(
        (r for bound in pack.checks if (r := run_check(bound, ctx)) is not None),
        key=lambda r: r.check_id,
    )
    validity = {bound.spec.id for bound in pack.checks if bound.spec.kind == "validity"}
    voiding = [r for r in results if r.check_id in validity and r.outcome in ("invalid", "error")]
    reasons = [f"{r.check_id}: {r.outcome} ({r.fault}): {r.note}" for r in voiding]
    if ctx.parser_error:
        reasons.append(ctx.parser_error)
    if any(r.outcome == "invalid" for r in voiding):
        status = "invalid"
    elif reasons:
        status = "error"
    else:
        status = "valid"
    return TrialGrade(
        **ids,
        status=status,
        reasons=reasons,
        outcome=ctx.outcome.model_dump(mode="json") if ctx.outcome is not None else None,
        results=results,
    )


# ── A run ────────────────────────────────────────────────────────────────────────────────────


def _case_stats(
    case_id: str, grades: Sequence[TrialGrade], k: int, validity: Collection[str]
) -> CaseStats:
    """One case over its applicable conformance and hazard checks (`validity`: the ids of the
    validity checks, left out: they decide whether a trial counts, not whether it passed). A valid
    trial is scored by the rule of a trial for a hazard: any fail → failed; else an error or
    invalid row, or no row but not_evaluable → unscored, in no denominator; else passed."""
    graded = [g for g in grades if g.status != "not_run"]  # budget-aborted: in no denominator
    valid = [g for g in graded if g.status == "valid"]
    scores = [
        stats.trial_status_for_hazard(
            "valid", [r.outcome for r in g.results if r.check_id not in validity]
        )
        for g in valid
    ]
    n = sum(s in ("pass", "fail") for s in scores)
    passes = scores.count("pass")
    return CaseStats(
        case_id=case_id,
        trials=len(graded),
        valid_trials=len(valid),
        invalid_trials=sum(g.status == "invalid" for g in graded),
        error_trials=sum(g.status == "error" for g in graded),
        unscored_trials=len(valid) - n,
        passed_trials=passes,
        k=k,
        pass_at_1=passes / n if n else None,
        pass_hat_k=stats.pass_hat_k(passes, n, k),
        pass_rate_interval=stats.cp_interval(passes, n) if n else None,
        mixed=0 < passes < n,
    )


def _failures(grades: Sequence[TrialGrade], check_ids: Collection[str]) -> list[EvidenceRef]:
    return [
        EvidenceRef(
            case_id=g.case_id, n=g.n, check_id=r.check_id, turn=r.evidence_turn, note=r.note
        )
        for g in grades
        for r in g.results
        if r.check_id in check_ids and r.outcome == "fail" and r.evidence_turn is not None
    ]


def _hazard(
    pack: Pack,
    hazard: Hazard,
    by_case: dict[str, list[TrialGrade]],
    exposes: Collection[str],
) -> tuple[list[CaseHazardStatus], HazardVerdict]:
    """The per-case statuses and the verdict for one hazard. The sample is the run's cases tagged
    with it; a failure elsewhere, or inside a trial that does not count, is shown, never counted."""
    linked = [bound.spec for bound in pack.checks if hazard.id in bound.spec.hazards]
    ids = {spec.id for spec in linked}
    tagged = [case_id for case_id in by_case if hazard.id in pack.cases[case_id].hazards]
    rows = []
    for case_id in tagged:
        statuses = [
            stats.trial_status_for_hazard(
                g.status, [r.outcome for r in g.results if r.check_id in ids]
            )
            for g in by_case[case_id]
            if g.status != "not_run"
        ]
        status, reason = stats.case_status(statuses, hazard.acceptance)
        rows.append(
            CaseHazardStatus(
                case_id=case_id,
                hazard_id=hazard.id,
                status=status,
                reason=reason,
                trial_statuses=statuses,
            )
        )
    grades = [g for case_grades in by_case.values() for g in case_grades]
    counted = [g for g in grades if g.status == "valid"]
    verdict = stats.hazard_verdict(hazard, linked, exposes, rows)
    return rows, verdict.model_copy(
        update={
            "failures": _failures([g for g in counted if g.case_id in tagged], ids),
            "outside_sample": _failures([g for g in counted if g.case_id not in tagged], ids),
            "invalid_trial_findings": _failures(
                [g for g in grades if g.status in ("invalid", "error")], ids
            ),
        }
    )


def run_exposes(target: Target, trials: Sequence[Trial]) -> list[str]:
    """What the run's checks could see: the channels every trial recorded; with no trial, those a
    live trial of `target` records (its own, plus the harness's side-channel and ground truth)."""
    if not trials:
        return sorted({*target.exposes, "side_channel", "ground_truth"})
    return sorted(set.intersection(*(set(t.exposes) for t in trials)))


def grade_run(
    pack: Pack,
    target: Target,
    trials: Sequence[tuple[Trial, str]],
    *,
    run_id: str,
    case_ids: Sequence[str],
    repeats: int,
    max_turns: int,
    graded_against: Literal["snapshot", "current"] = "snapshot",
    expectations_match: bool = True,
    lint: Sequence[LintIssue] = (),
    harness_git_sha: str | None = None,
    clock: Callable[[], datetime] = now,
) -> Results:
    """Grade a run: every trial, then the per-case statistics and the per-hazard verdicts.

    `trials` pairs each trial with the sha256 of its file, one attempt per (case, n): the latest
    (picking the attempt files is the run folder's job). `case_ids` is the run's selection, so a
    case without a completed trial stays in the sample, excluded. `repeats` is the requested k,
    `max_turns` the run's user-turn cap."""
    if unknown := sorted(set(case_ids) - set(pack.cases)):
        raise ValueError(f"cases {unknown} are not in pack {pack.config.name}")
    seen: set[tuple[str, int]] = set()
    for trial, _ in trials:
        if trial.case_id not in case_ids:
            raise ValueError(f"a trial of case {trial.case_id}, which the run did not select")
        if trial.target_name != target.name:  # its outcome source and end_when would be another's
            raise ValueError(
                f"trial {trial.n} of case {trial.case_id} ran against target "
                f"{trial.target_name}, not {target.name}"
            )
        if (trial.case_id, trial.n) in seen:
            raise ValueError(
                f"trial {trial.n} of case {trial.case_id} given twice: pass its latest attempt only"
            )
        seen.add((trial.case_id, trial.n))

    grades = sorted(
        (grade_trial(pack, target, trial, sha, max_turns=max_turns) for trial, sha in trials),
        key=lambda g: (g.case_id, g.n),
    )
    by_case = {case_id: [g for g in grades if g.case_id == case_id] for case_id in sorted(case_ids)}
    exposes = run_exposes(target, [trial for trial, _ in trials])
    validity = {bound.spec.id for bound in pack.checks if bound.spec.kind == "validity"}
    case_hazards: list[CaseHazardStatus] = []
    verdicts: list[HazardVerdict] = []
    for hazard_id in sorted(pack.hazards):
        rows, verdict = _hazard(pack, pack.hazards[hazard_id], by_case, exposes)
        case_hazards += rows
        verdicts.append(verdict)
    return Results(
        run_id=run_id,
        graded_at=clock(),
        graded_against=graded_against,
        pack_sha256=pack.sha256,
        harness_version=__version__,
        harness_git_sha=harness_git_sha,
        expectations_match=expectations_match,
        checks=[bound.spec for bound in pack.checks],
        trials=grades,
        cases=[_case_stats(case_id, by_case[case_id], repeats, validity) for case_id in by_case],
        case_hazards=sorted(case_hazards, key=lambda row: (row.case_id, row.hazard_id)),
        hazards=verdicts,
        lint=sorted(lint, key=lambda issue: (issue.level, issue.code, issue.where, issue.message)),
    )
