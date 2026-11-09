"""What a check is, and the decorators packs use.

There is no global registry: `@check` and `@tool` only attach a definition to the function, and the
pack loader collects every attribute of a module that carries one (`collect`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

from pydantic import BaseModel

from auditkit.schema import (
    Case,
    Channel,
    CheckOutcome,
    Kind,
    Method,
    Stimulus,
    Target,
    Transcript,
    Trial,
    Turn,
)
from auditkit.tools import ToolDef, ToolFn

CheckFn = Callable[[Transcript, "CheckContext"], CheckOutcome]
Applies = Callable[[Case, BaseModel], bool]

CHECK_ATTR = "__auditkit_check__"
TOOL_ATTR = "__auditkit_tool__"
_BARE_ID = re.compile(r"^[a-z0-9_]+$")  # the loader adds the prefix: <pack>. or core.


class NoOutcome(Exception):
    """The trial has no parsed outcome: the check gives no row (counted once, by the core)."""


class StimulusNotDelivered(Exception):
    """A stimulus the check depends on never reached the agent: the check's result is invalid (test
    data), and the message, which says why, is its note."""


def _always(case: Case, expected: BaseModel) -> bool:
    return True


@dataclass(frozen=True)
class CheckDef:
    """What `@check` attaches. Hazard links of a core check come from the pack's binding."""

    id: str  # bare: no prefix
    kind: Kind
    method: Method
    hazards: tuple[str, ...]
    controls: tuple[str, ...]
    needs: tuple[Channel, ...]
    description: str
    applies: Applies
    fn: CheckFn


def check(
    *,
    id: str,
    kind: Kind,
    method: Method,
    hazards: Sequence[str] = (),
    controls: Sequence[str] = (),
    needs: Sequence[Channel] = ("turn_text",),
    description: str,
    applies: Applies = _always,
) -> Callable[[CheckFn], CheckFn]:
    if not _BARE_ID.fullmatch(id):
        raise ValueError(
            f"check id {id!r} must match {_BARE_ID.pattern}: the loader adds the prefix"
        )

    def attach(fn: CheckFn) -> CheckFn:
        setattr(
            fn,
            CHECK_ATTR,
            CheckDef(
                id=id,
                kind=kind,
                method=method,
                hazards=tuple(hazards),
                controls=tuple(controls),
                needs=tuple(needs),
                description=description,
                applies=applies,
                fn=fn,
            ),
        )
        return fn

    return attach


def tool(
    *,
    description: str,
    parameters: dict[str, Any],
    side_effect: bool = False,
    name: str | None = None,
) -> Callable[[ToolFn], ToolFn]:
    def attach(fn: ToolFn) -> ToolFn:
        tool_name = name or fn.__name__
        setattr(fn, TOOL_ATTR, ToolDef(tool_name, description, parameters, side_effect, fn))
        return fn

    return attach


def collect(module: ModuleType) -> tuple[list[CheckDef], list[ToolDef]]:
    """Every check and tool definition reachable as an attribute of `module`, imported ones
    included. The same function bound under two names counts once."""
    checks: dict[int, CheckDef] = {}
    tools: dict[int, ToolDef] = {}
    for value in vars(module).values():
        if (c := getattr(value, CHECK_ATTR, None)) is not None and isinstance(c, CheckDef):
            checks[id(c)] = c
        if (t := getattr(value, TOOL_ATTR, None)) is not None and isinstance(t, ToolDef):
            tools[id(t)] = t
    return sorted(checks.values(), key=lambda c: c.id), sorted(tools.values(), key=lambda t: t.name)


def passed(note: str = "", turn: int | None = None) -> CheckOutcome:
    return CheckOutcome(outcome="pass", note=note, evidence_turn=turn)


def failed(note: str, turn: int) -> CheckOutcome:
    return CheckOutcome(outcome="fail", note=note, evidence_turn=turn, fault="agent")


def invalid(note: str, fault: Literal["simulator", "test_data"] = "simulator") -> CheckOutcome:
    return CheckOutcome(outcome="invalid", note=note, fault=fault)


def not_delivered(stimulus: Stimulus, transcript: Transcript) -> str:
    """Why `stimulus` never reached the agent, as the note of an invalid result. A trigger that
    depends on the conversation is "a precondition not reached": the agent may be the cause, which
    the checks of the other hazards show."""
    if stimulus.channel == "tool_result":
        why = f"precondition not reached: no {stimulus.tool} call returned its marker"
    elif stimulus.at_turn is not None:
        users, turn = len(transcript.user_turns()), stimulus.at_turn
        why = (
            f"the conversation ended after {users} user turn(s), before user turn {turn}"
            if users < turn
            else f"user turn {turn} does not carry it"
        )
    else:
        condition = json.dumps(stimulus.on_hidden, sort_keys=True, ensure_ascii=False)
        why = f"precondition not reached: the simulator never declared {condition}"
    return f"stimulus {stimulus.id} not delivered: {why}"


@dataclass(frozen=True)
class CheckContext:
    case: Case
    ground_truth: BaseModel
    expected: BaseModel
    data: Mapping[str, BaseModel]
    trial: Trial  # stop_reason, exposes, the transcript
    side_channel_keys: list[str]
    outcome: BaseModel | None  # parsed final outcome, None if absent or invalid
    outcome_error: str | None  # why the agent's output gave no outcome
    parser_error: str | None  # the pack's parse_outcome broke: a harness error, never the agent's
    target: Target  # end_when, outcome source
    max_turns: int  # this trial's user-turn cap: Case.max_turns, else the run's

    def require_outcome(self) -> BaseModel:
        if self.outcome is None:
            raise NoOutcome(self.outcome_error or "no final outcome")
        return self.outcome

    def stimulus_turn(self, stimulus_id: str) -> Turn:
        """Where the case's stimulus `stimulus_id` reached the agent (`Transcript.stimulus_turn`).
        Raises `StimulusNotDelivered`, saying why, when it never did."""
        stimulus = next((s for s in self.case.stimuli if s.id == stimulus_id), None)
        if stimulus is None:
            raise ValueError(f"case {self.case.id} has no stimulus {stimulus_id!r}")
        turn = self.trial.transcript.stimulus_turn(stimulus)
        if turn is None:
            raise StimulusNotDelivered(not_delivered(stimulus, self.trial.transcript))
        return turn
