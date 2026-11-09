"""Builders for the harness's tests and for pack tests: no long transcript written by hand.

`TranscriptBuilder().user("hi", hidden={…}).agent("hello", tool_calls=[…]).build()`,
`make_run(tmp_path, packs_dir, name, trials)` for a run folder that `--replay` grades, and
`ScriptedLLM` for a live run with no model behind it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from auditkit import runfolder, simulator
from auditkit.cost import DEFAULT_MODEL, cost_usd, estimate
from auditkit.lint import lint_pack
from auditkit.llm import Completion, Message, TextBlock, ToolUseBlock
from auditkit.pack import load_pack
from auditkit.schema import (
    Channel,
    Execution,
    ModelCall,
    ModelInfo,
    ToolCall,
    Transcript,
    Trial,
    Turn,
    Usage,
)

FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ALL_CHANNELS: list[Channel] = [
    "final_outcome",
    "ground_truth",
    "side_channel",
    "tool_calls",
    "turn_text",
]


class TranscriptBuilder:
    """Builds a transcript turn by turn. Times start at `FIXED_NOW`, one second per turn; tool
    calls get the ids `call_1`, `call_2`…"""

    def __init__(self) -> None:
        self._transcript = Transcript()
        self._calls = 0

    def _at(self) -> datetime:
        return FIXED_NOW + timedelta(seconds=len(self._transcript.turns))

    def user(
        self,
        text: str,
        *,
        hidden: Mapping[str, Any] | None = None,
        stimulus: str | None = None,
        source: Literal["simulator", "script"] | None = None,
        overridden_text: str | None = None,
        end_conversation: bool = False,
    ) -> TranscriptBuilder:
        """A user turn: the simulator's, or a scripted one that delivers `stimulus` (an id)."""
        self._transcript.append(
            Turn(
                index=0,  # numbered by Transcript.append
                role="user",
                text=text,
                at=self._at(),
                source=source or ("script" if stimulus else "simulator"),
                stimulus_id=stimulus,
                hidden=dict(hidden) if hidden is not None else None,
                overridden_text=overridden_text,
                end_conversation=end_conversation,
            )
        )
        return self

    def agent(
        self,
        text: str = "",
        *,
        tool_calls: Sequence[ToolCall | Mapping[str, Any]] = (),
        final_output: Mapping[str, Any] | None = None,
    ) -> TranscriptBuilder:
        """The agent's whole answer to the last user turn. A tool call is a `ToolCall`, or a mapping
        with a `name` and, optionally, `arguments`, `result` and `is_error`."""
        at = self._at()
        calls = [c if isinstance(c, ToolCall) else self._call(c, at) for c in tool_calls]
        self._transcript.append(
            Turn(
                index=0,
                role="agent",
                text=text,
                at=at,
                tool_calls=calls,
                final_output=dict(final_output) if final_output is not None else None,
            )
        )
        return self

    def _call(self, spec: Mapping[str, Any], at: datetime) -> ToolCall:
        self._calls += 1
        return ToolCall(
            id=f"call_{self._calls}",
            name=spec["name"],
            arguments=dict(spec.get("arguments", {})),
            result=spec.get("result"),
            is_error=spec.get("is_error", False),
            called_at=at,
            returned_at=at,
        )

    def build(self) -> Transcript:
        return self._transcript.model_copy(deep=True)

    def trial(
        self,
        case_id: str,
        *,
        n: int = 1,
        attempt: int = 1,
        status: Literal["completed", "aborted", "error"] = "completed",
        stop_reason: str = "outcome_emitted",
        exposes: Sequence[Channel] = ALL_CHANNELS,
        target_name: str = "target",
        run_id: str = "test-run",
        error: str | None = None,
        fault: Literal["harness", "provider"] | None = None,
    ) -> Trial:
        """The built transcript, as a trial of `case_id`."""
        transcript = self.build()
        return Trial.model_validate(
            {
                "run_id": run_id,
                "case_id": case_id,
                "n": n,
                "attempt": attempt,
                "target_name": target_name,
                "exposes": list(exposes),
                "status": status,
                "stop_reason": stop_reason,
                "error": error,
                "fault": fault,
                "started_at": FIXED_NOW,
                "finished_at": FIXED_NOW + timedelta(seconds=len(transcript.turns)),
                "transcript": transcript,
            }
        )


ExecutionStatus = Literal["running", "completed", "aborted_budget", "aborted_error", "interrupted"]


def make_run(
    runs_dir: Path,
    packs_dir: Path,
    name: str,
    trials: Sequence[Trial],
    *,
    variant: str = "default",
    target: str | None = None,
    case_ids: Sequence[str] | None = None,
    repeats: int | None = None,
    max_turns: int | None = None,
    status: ExecutionStatus = "completed",
    budget_usd: float = 3.00,
    clock: Callable[[], datetime] = lambda: FIXED_NOW,
) -> Path:
    """A run folder holding `trials`, written as the live runner writes one, through the
    same `runfolder` functions: snapshot, manifest, expectations, lint warnings, transcripts and the
    execution index; no `results.json` (the first `--replay` grades it). Every trial is stamped with
    the run's id and target. Defaults: the pack's default target, every case of the variant, the
    pack's repeats and max_turns. Lint errors raise ValueError: a live run would not start."""
    packs_dir = Path(packs_dir)
    issues = lint_pack(
        packs_dir, name, variant=variant, target=target, repeats=repeats, max_turns=max_turns
    )
    if errors := [i for i in issues if i.level == "error"]:
        raise ValueError(f"lint errors, a live run would not start: {errors}")
    pack = load_pack(packs_dir, name, variant=variant)
    chosen = pack.targets[target or pack.config.default_target]
    selected = list(case_ids) if case_ids is not None else sorted(pack.cases)
    k = repeats or pack.config.default_repeats
    cap = max_turns or pack.config.max_turns
    models = [
        ModelInfo(
            role="agent",
            env_var=chosen.model_env,
            requested=DEFAULT_MODEL,
            effort=chosen.effort,
            max_tokens=chosen.max_tokens,
        ),
        ModelInfo(
            role="simulator",
            env_var=simulator.ENV_VAR,
            requested=DEFAULT_MODEL,
            effort=simulator.EFFORT,
            max_tokens=simulator.MAX_TOKENS,
        ),
    ]
    roles = {info.role: info.requested for info in models}
    planned = estimate(pack.config.estimate, trials=len(selected) * k, max_turns=cap, models=roles)

    run_dir = runfolder.new_run_dir(runs_dir, clock)
    run_id = run_dir.name
    runfolder.copy_snapshot(packs_dir, name, run_dir, pack.sha256)
    command = ["eval", "--domain", name, "--variant", variant, "--target", chosen.name]
    manifest = runfolder.build_manifest(
        pack,
        chosen,
        run_id=run_id,
        command=[*command, "--repeats", str(k), "--max-turns", str(cap)],
        models=models,
        case_ids=selected,
        repeats=k,
        max_turns=cap,
        budget_usd=budget_usd,
        estimate_usd=sum(role.typical_usd for role in planned),
        clock=clock,
    )
    runfolder.write_manifest(run_dir, manifest)
    runfolder.write_expectations(run_dir, pack, selected)
    runfolder.write_lint(run_dir, issues)
    stamped = [t.model_copy(update={"run_id": run_id, "target_name": chosen.name}) for t in trials]
    entries = [runfolder.write_trial(run_dir, trial) for trial in stamped]
    runfolder.write_execution(
        run_dir,
        Execution(
            run_id=run_id,
            status=status,
            first_call_at=min((t.started_at for t in stamped), default=None),
            updated_at=clock(),
            spent_usd=sum(t.cost_usd for t in stamped),
            served_models=runfolder.served_models(stamped),
            trials=entries,
        ),
    )
    return run_dir


# ── A model client with a script instead of a model ───────────────────────────────────────────────


class Reply:
    """One scripted model response: text and tool uses; `stop_reason` defaults to `tool_use` when
    there are tool uses, else `end_turn`. A `refusal` stops with no content."""

    def __init__(
        self,
        text: str = "",
        tool_uses: Sequence[tuple[str, Mapping[str, Any]]] = (),
        *,
        stop_reason: str | None = None,
        output_tokens: int = 50,
    ) -> None:
        self.text, self.tool_uses, self.output_tokens = text, list(tool_uses), output_tokens
        self.stop_reason = stop_reason or ("tool_use" if self.tool_uses else "end_turn")


def say(text: str) -> Reply:
    return Reply(text)


def use(name: str, arguments: Mapping[str, Any], text: str = "") -> Reply:
    return Reply(text, [(name, arguments)])


def user_says(message: str, hidden: Mapping[str, Any] | None = None, *, end: bool = False) -> Reply:
    """A simulator turn, as its `user_reply` tool call."""
    arguments: dict[str, Any] = {"message": message, "hidden": dict(hidden or {})}
    if end:
        arguments["end_conversation"] = True
    return use(simulator.TOOL, arguments)


REFUSAL = Reply(stop_reason="refusal", output_tokens=0)

Script = Sequence[Reply | Exception] | Callable[[dict[str, Any]], Reply]


class ScriptedLLM:
    """An `LLMClient` that plays scripts: per role, a list of replies used in order (an exception
    in it is raised), or a function from the request to a reply. Every request is recorded in
    `requests`, with its role, so a test can read what each side was sent. The served model is
    the requested one, unless `served` names another per role."""

    def __init__(
        self,
        *,
        agent: Script = (),
        simulator: Script = (),
        served: Mapping[str, str] | None = None,
    ) -> None:
        self.scripts: dict[str, Any] = {
            "agent": agent if callable(agent) else list(agent),
            "simulator": simulator if callable(simulator) else list(simulator),
        }
        self.served = dict(served or {})
        self.requests: list[dict[str, Any]] = []
        self._ids = 0

    def complete(self, **request: Any) -> Completion:
        request["messages"] = list(request["messages"])  # as sent: the caller's history grows
        self.requests.append(request)
        role, model = request["role"], request["model"]
        script = self.scripts[role]
        if callable(script):
            reply = script(request)
        elif script:
            reply = script.pop(0)
        else:
            raise AssertionError(f"the {role} script has no reply left")
        if isinstance(reply, Exception):
            raise reply
        blocks: list[Any] = [TextBlock(text=reply.text)] if reply.text else []
        for name, arguments in reply.tool_uses:
            self._ids += 1
            blocks.append(ToolUseBlock(id=f"toolu_{self._ids}", name=name, input=dict(arguments)))
        chars = len(request["system"]) + sum(
            len(b.model_dump_json()) for m in request["messages"] for b in m.blocks
        )
        usage = Usage(input_tokens=chars // 4 + 1, output_tokens=reply.output_tokens)
        call = ModelCall(
            role=role,
            requested_model=model,
            served_model=self.served.get(role, model),
            effort=request["effort"],
            max_tokens=request["max_tokens"],
            usage=usage,
            cost_usd=cost_usd(model, usage.input_tokens, usage.output_tokens),
            stop_reason=reply.stop_reason,
            request_id=f"req_{len(self.requests)}",
            latency_ms=1,
        )
        return Completion(
            text=reply.text,
            tool_uses=[b for b in blocks if isinstance(b, ToolUseBlock)],
            message=Message(role="assistant", blocks=blocks),
            call=call,
        )

    def sent_to(self, role: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["role"] == role]
