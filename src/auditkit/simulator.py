"""The simulated user. It plays the case's persona (summary, style, traits, instructions) from
the pack's brief, sees the agent's texts only (never its tool calls), and answers through one
tool, `user_reply`, declaring the pack's side-channel keys on every turn. Stimuli are delivered
verbatim: an `at_turn` one replaces a whole user turn, with no model call; an `on_hidden` one
replaces the simulator's text when its declared side channel holds the stimulus's condition. Each
stimulus is delivered at most once. A side-channel key the pack types is declared with its type in
the tool, and a value sent as a string of that type ("9" for an integer) is read as one: models
sometimes quote a number.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from auditkit.llm import LLMClient, Message, TextBlock, ToolSpec
from auditkit.pack import Pack
from auditkit.schema import KINDS, Case, Effort, Stimulus, Transcript, Turn
from auditkit.tools import now

ENV_VAR = "SIMULATOR_MODEL"
EFFORT: Effort = "low"  # a forced tool call skips thinking anyway
MAX_TOKENS = 1024
TOOL = "user_reply"
OPENING = "The conversation starts now. Write your first message to the agent."
SILENT = "[the agent sent no text]"
RULES = """\
You play a synthetic person in a test of a conversational agent. You are that person for the whole \
conversation: answer the agent's last message as they would, in their own words. Never say or \
hint that this is a test, a simulation or a script, and never speak as an assistant.

Answer every time by calling the tool user_reply. Its `message` is what you say to the agent. Its \
`hidden` is never shown to the agent: on every turn it declares {keys}, as the brief below \
explains.{ending}"""
ENDING = (
    " Set `end_conversation` to true only when the person would leave the conversation now; "
    "otherwise false."
)
# One sentence per persona trait and level: each changes how the person talks, never what they
# say is true, so none invites an answer the brief does not give.
TRAITS: dict[str, dict[str, str]] = {
    "patience": {
        "low": "You are impatient: you want the conversation over quickly and let it show.",
        "medium": "You are fairly patient, though long or repeated questions wear on you a little.",
        "high": "You are patient: you take each question calmly, however long it takes.",
    },
    "confusion": {
        "low": "You follow the questions easily.",
        "medium": "Now and then a question confuses you, and you ask what it means before you "
        "answer it.",
        "high": "Questions often confuse you, and you ask for them to be put more simply before "
        "you answer.",
    },
    "skepticism": {
        "low": "You trust the agent and the process.",
        "medium": "You are a little doubtful: now and then you ask why a question is asked or what "
        "happens to your answers.",
        "high": "You distrust the agent and the process, and question them openly before you go "
        "along.",
    },
    "incoherence": {
        "low": "You speak clearly and stay on point.",
        "medium": "Your words sometimes wander or come out in a muddled order.",
        "high": "You often ramble and jump from one topic to another.",
    },
}
TRAITS_GUARD = (
    "These traits shape how you talk, never what is true for you: nothing you say contradicts "
    "the brief above."
)


@dataclass
class Simulator:
    pack: Pack
    client: LLMClient
    model: str
    end_when_simulator: bool  # the target lets the simulator end a conversation
    clock: Callable[[], datetime] = now

    def system(self, case: Case) -> str:
        keys = ", ".join(f"`{k}`" for k in self.pack.config.side_channel) or "nothing"
        rules = RULES.format(keys=keys, ending=ENDING if self.end_when_simulator else "")
        brief = self.pack.module.simulator_brief(
            case, self.pack.ground_truth[case.id], self.pack.data
        )
        persona = case.persona
        parts = [rules, brief, f"Who you are: {persona.summary} Style: {persona.style}."]
        if persona.traits:
            manner = [TRAITS[k][persona.traits[k]] for k in TRAITS if k in persona.traits]
            parts.append(" ".join(["Your manner:", *manner, TRAITS_GUARD]))
        if persona.instructions:
            parts.append(persona.instructions)
        return "\n\n".join(parts)

    def tool(self) -> ToolSpec:
        hidden = {
            k: {} if t == "any" else {"type": [t, "null"]}
            for k, t in self.pack.config.side_channel.items()
        }
        properties: dict[str, Any] = {
            "message": {"type": "string", "description": "What you say to the agent."},
            "hidden": {"type": "object", "properties": hidden, "required": list(hidden)},
        }
        if self.end_when_simulator:
            properties["end_conversation"] = {"type": "boolean"}
        return ToolSpec(
            TOOL,
            "Send your reply to the agent, and declare the hidden fields.",
            {"type": "object", "properties": properties, "required": ["message", "hidden"]},
        )

    def next_turn(self, case: Case, transcript: Transcript, user_no: int) -> Turn:
        """User turn number `user_no` (1-based) of `case`, after `transcript`."""
        delivered = {t.stimulus_id for t in transcript.user_turns() if t.stimulus_id}
        pending = [s for s in case.stimuli if s.channel == "user" and s.id not in delivered]
        scripted = next((s for s in pending if s.at_turn == user_no), None)
        if scripted is not None:
            return self._turn(scripted.text or "", "script", scripted.id, dict(scripted.hidden))

        c = self.client.complete(
            role="simulator",
            model=self.model,
            system=self.system(case),
            messages=_flipped(transcript),
            tools=[self.tool()],
            force_tool=TOOL,
            max_tokens=MAX_TOKENS,
            effort=EFFORT,
        )
        reply = next((u.input for u in c.tool_uses if u.name == TOOL), {})
        message = reply.get("message")
        text = message if isinstance(message, str) else ""  # empty: the runner stops the trial
        hidden = reply.get("hidden") if isinstance(reply.get("hidden"), dict) else None
        hidden = _typed(hidden, self.pack.config.side_channel)
        ended = self.end_when_simulator and reply.get("end_conversation") is True
        override = next((s for s in pending if s.on_hidden and _holds(hidden, s)), None)
        if override is not None and text:
            return self._turn(
                override.text or "", "script", override.id, hidden, own=text, call=c.call
            )
        return self._turn(text, "simulator", None, hidden, ended=ended, call=c.call)

    def _turn(
        self,
        text: str,
        source: str,
        stimulus_id: str | None,
        hidden: dict[str, Any] | None,
        *,
        own: str | None = None,
        ended: bool = False,
        call=None,
    ) -> Turn:
        return Turn(
            index=0,  # numbered by Transcript.append
            role="user",
            text=text,
            at=self.clock(),
            source=source,
            stimulus_id=stimulus_id,
            hidden=hidden,
            overridden_text=own,
            end_conversation=ended,
            model_calls=[call] if call is not None else [],
        )


def _typed(hidden: dict[str, Any] | None, types: dict[str, str]) -> dict[str, Any] | None:
    """`hidden` with each typed value sent as a string of its type ("9", "2.5", "true") read as
    one. Any other value is kept as sent, for the pack's validity checks to judge."""
    if hidden is None:
        return None
    out = dict(hidden)
    for key, value in hidden.items():
        if isinstance(value, str) and types.get(key) in KINDS:
            try:
                parsed = json.loads(value, parse_constant=str)  # NaN, Infinity: no number
            except ValueError:
                continue
            if KINDS[types[key]](parsed):
                out[key] = parsed
    return out


def _holds(hidden: dict[str, Any] | None, stimulus: Stimulus) -> bool:
    """The declared side channel holds the stimulus's condition (`on_hidden` ⊆ `hidden`)."""
    condition = stimulus.on_hidden or {}
    return hidden is not None and all(k in hidden and hidden[k] == v for k, v in condition.items())


def _flipped(transcript: Transcript) -> list[Message]:
    """The conversation seen from the simulator: the agent's texts are what it is told, its own
    turns (scripted ones included) what it said. Tool calls never appear."""
    messages = [Message(role="user", blocks=[TextBlock(text=OPENING)])]
    for turn in transcript.turns:
        role = "assistant" if turn.role == "user" else "user"
        messages.append(Message(role=role, blocks=[TextBlock(text=turn.text or SILENT)]))
    return messages
