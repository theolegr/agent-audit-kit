"""The live runner: one trial's conversation loop, and a run's trials in round-robin order,
each written as raw evidence before the next one starts. Grading is the replay's own code, run on
the folder afterwards (cli), so a live verdict and a replayed one come from one function.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from auditkit import runfolder
from auditkit.adapters import AgentReply, PromptedModelAdapter
from auditkit.cost import DEFAULT_MODEL
from auditkit.grade import extract_outcome
from auditkit.llm import BudgetExceeded, LLMClient, MeteredClient, ProviderError
from auditkit.pack import Pack
from auditkit.schema import (
    Case,
    Execution,
    LintIssue,
    ModelInfo,
    RunManifest,
    Target,
    Transcript,
    Trial,
    Turn,
)
from auditkit.simulator import EFFORT, ENV_VAR, MAX_TOKENS, Simulator
from auditkit.tools import ToolEnv, now

Planned = tuple[str, int, int]  # (case id, n, attempt)


def model_infos(target: Target, env: Mapping[str, str] = os.environ) -> list[ModelInfo]:
    """The requested model of each role: its env var, else the code default."""
    return [
        ModelInfo(
            role="agent",
            env_var=target.model_env,
            requested=env.get(target.model_env) or DEFAULT_MODEL,
            effort=target.effort,
            max_tokens=target.max_tokens,
        ),
        ModelInfo(
            role="simulator",
            env_var=ENV_VAR,
            requested=env.get(ENV_VAR) or DEFAULT_MODEL,
            effort=EFFORT,
            max_tokens=MAX_TOKENS,
        ),
    ]


@dataclass
class RunContext:
    target: Target
    adapter: PromptedModelAdapter
    simulator: Simulator
    meter: MeteredClient
    run_id: str
    max_turns: int  # the run's user-turn cap; a case's own wins
    clock: Callable[[], datetime] = now


def agent_turn(reply: AgentReply, at: datetime) -> Turn:
    return Turn(
        index=0,  # numbered by Transcript.append
        role="agent",
        text=reply.text,
        at=at,
        tool_calls=reply.tool_calls,
        final_output=reply.final_output,
        model_calls=reply.model_calls,
    )


def run_trial(case: Case, n: int, attempt: int, rc: RunContext) -> Trial:
    """One conversation. Every way it can end is a stop reason; an exception becomes the
    trial's status, so the evidence gathered so far is always kept."""
    target, max_turns = rc.target, case.max_turns or rc.max_turns
    env = ToolEnv(
        state=copy.deepcopy(case.environment), context=dict(case.context), policy=target.tool_policy
    )
    session = rc.adapter.new_session(case.context, env)
    t, stop, status, error, fault = Transcript(), "max_turns", "completed", None, None
    started, spent = rc.clock(), rc.meter.spent_usd
    try:
        for user_no in range(1, max_turns + 1):
            user = t.append(rc.simulator.next_turn(case, t, user_no))
            if not user.text:
                stop = "simulator_failed"
                break
            if target.end_when == "simulator" and user.end_conversation:
                stop = "simulator_ended"
                break
            reply = session.send(user.text)
            t.append(agent_turn(reply, rc.clock()))
            if reply.tool_round_cap_hit:
                stop = "tool_round_cap"
                break
            if target.end_when == "outcome" and extract_outcome(t, target.outcome) is not None:
                stop = "outcome_emitted"
                break
    except BudgetExceeded:
        status, stop = "aborted", "budget"
        _keep_partial(t, session, rc)
    except ProviderError as e:
        status, stop, error, fault = "error", "error", str(e), e.fault
        _keep_partial(t, session, rc)
    except Exception as e:  # a harness bug: kept as evidence
        status, stop, error, fault = "error", "error", repr(e), "harness"
        _keep_partial(t, session, rc)
    finally:
        session.close()
    return Trial(
        run_id=rc.run_id,
        case_id=case.id,
        n=n,
        attempt=attempt,
        target_name=target.name,
        exposes=[*target.exposes, "side_channel", "ground_truth"],
        status=status,
        stop_reason=stop,
        error=error,
        fault=fault,
        started_at=started,
        finished_at=rc.clock(),
        cost_usd=round(rc.meter.spent_usd - spent, 8),
        transcript=t,
    )


def _keep_partial(t: Transcript, session: object, rc: RunContext) -> None:
    """An exchange cut short after a model call: what it did (charged calls, executed mock tools)
    becomes the trial's last agent turn, so the transcript holds every call the trial paid for."""
    partial = getattr(session, "pending", None)
    if partial is not None and partial.model_calls and t.turns and t.turns[-1].role == "user":
        t.append(agent_turn(partial, rc.clock()))


def planned(case_ids: Sequence[str], repeats: int) -> list[Planned]:
    """Round-robin: every case's trial 1, then every case's trial 2…: a budget stop still leaves
    balanced data."""
    return [(case_id, n, 1) for n in range(1, repeats + 1) for case_id in case_ids]


def start(
    pack: Pack,
    target: Target,
    *,
    packs_dir: Path,
    runs_dir: Path,
    case_ids: Sequence[str],
    repeats: int,
    max_turns: int,
    models: Sequence[ModelInfo],
    issues: Sequence[LintIssue],
    budget_usd: float,
    estimate_usd: float,
    command: Sequence[str],
    clock: Callable[[], datetime] = now,
) -> tuple[Path, RunManifest, Execution]:
    """The run folder, before any model call: snapshot, manifest, expectations, lint, and an
    execution record with status `running`."""
    run_dir = runfolder.new_run_dir(runs_dir, clock)
    runfolder.copy_snapshot(packs_dir, pack.config.name, run_dir, pack.sha256)
    manifest = runfolder.build_manifest(
        pack,
        target,
        run_id=run_dir.name,
        command=command,
        models=models,
        case_ids=case_ids,
        repeats=repeats,
        max_turns=max_turns,
        budget_usd=budget_usd,
        estimate_usd=estimate_usd,
        clock=clock,
    )
    runfolder.write_manifest(run_dir, manifest)
    runfolder.write_expectations(run_dir, pack, case_ids)
    runfolder.write_lint(run_dir, issues)
    execution = Execution(run_id=manifest.run_id, status="running", updated_at=clock())
    runfolder.write_execution(run_dir, execution)
    return run_dir, manifest, execution


def missing(manifest: RunManifest, trials: Sequence[tuple[Trial, str]]) -> list[Planned]:
    """The planned trials without a completed file, in round-robin order, each with its next
    attempt number (`--resume`)."""
    latest = {(trial.case_id, trial.n): trial for trial, _ in trials}
    out = []
    for case_id, n, _ in planned(manifest.case_ids, manifest.repeats):
        trial = latest.get((case_id, n))
        if trial is None:
            out.append((case_id, n, 1))
        elif trial.status != "completed":
            out.append((case_id, n, trial.attempt + 1))
    return out


def keep_earlier_attempt(
    run_dir: Path, execution: Execution, case_id: str, n: int, attempt: int
) -> None:
    """Just before trial `n` runs again as `attempt`, its earlier file becomes
    `run_<n>.attempt-<attempt - 1>.json`: kept, never deleted, and the index follows it. Already
    kept (a resume stopped between this rename and the new file): nothing to do."""
    if attempt == 1:
        return
    current = run_dir / runfolder.trial_file(case_id, n)
    kept = runfolder.trial_file(case_id, n, attempt - 1)
    if (run_dir / kept).exists():
        if current.exists():
            raise FileExistsError(f"{kept} exists: an earlier attempt would be overwritten")
        return
    current.rename(run_dir / kept)
    for entry in execution.trials:
        if (entry.case_id, entry.n, entry.attempt) == (case_id, n, attempt - 1):
            entry.file = kept
    runfolder.write_execution(run_dir, execution)


def _merge(served: dict[str, list[str]], trial: Trial) -> dict[str, list[str]]:
    found = {role: set(ids) for role, ids in served.items()}
    for role, ids in runfolder.served_models([trial]).items():
        found.setdefault(role, set()).update(ids)
    return {role: sorted(ids) for role, ids in sorted(found.items())}


def execute(
    run_dir: Path,
    pack: Pack,
    manifest: RunManifest,
    execution: Execution,
    client: LLMClient,
    *,
    todo: Sequence[Planned],
    cap_usd: float,
    spent_usd: float,
    clock: Callable[[], datetime] = now,
    on_trial: Callable[[Trial], None] | None = None,
) -> Execution:
    """Run `todo` in order with the manifest's models, behind the budget meter. A trial run again
    first keeps its earlier file as an attempt; after each trial, its file is written atomically,
    then the index. A budget stop ends the run (`aborted_budget`); an errored trial does not."""
    target = pack.targets[manifest.target_name]
    models = {m.role: m.requested for m in manifest.models}

    def first_call() -> None:
        if execution.first_call_at is None:
            execution.first_call_at = clock()
            runfolder.write_execution(run_dir, execution)

    meter = MeteredClient(client, cap_usd=cap_usd, spent_usd=spent_usd, on_first_call=first_call)
    prompt = ""
    if target.system_prompt_file is not None:
        prompt = (pack.dir / "targets" / target.system_prompt_file).read_text(encoding="utf-8")
    tools = {name: pack.tools[name] for name in target.tools}
    rc = RunContext(
        target=target,
        adapter=PromptedModelAdapter(target, meter, models["agent"], prompt, tools, clock),
        simulator=Simulator(
            pack, meter, models["simulator"], target.end_when == "simulator", clock
        ),
        meter=meter,
        run_id=manifest.run_id,
        max_turns=manifest.max_turns,
        clock=clock,
    )
    execution.status = "running"
    counted = meter.spent_usd  # the meter's spend already in execution.spent_usd
    try:
        for case_id, n, attempt in todo:
            keep_earlier_attempt(run_dir, execution, case_id, n, attempt)
            trial = run_trial(pack.cases[case_id], n, attempt, rc)
            execution.trials.append(runfolder.write_trial(run_dir, trial))
            execution.spent_usd = round(execution.spent_usd + trial.cost_usd, 8)
            counted = meter.spent_usd
            execution.served_models = _merge(execution.served_models, trial)
            execution.updated_at = clock()
            if trial.status == "aborted":
                execution.status = "aborted_budget"
            runfolder.write_execution(run_dir, execution)
            if on_trial is not None:
                on_trial(trial)
            if trial.status == "aborted":
                return execution
    except KeyboardInterrupt:  # a trial cut short has no file, but its charged calls count
        execution.spent_usd = round(execution.spent_usd + meter.spent_usd - counted, 8)
        execution.status, execution.updated_at = "interrupted", clock()
        runfolder.write_execution(run_dir, execution)
        raise
    execution.status, execution.updated_at = "completed", clock()
    runfolder.write_execution(run_dir, execution)
    return execution
