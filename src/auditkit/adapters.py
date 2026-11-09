"""Target adapters: the system under test, behind a session. Only the user's text crosses the
boundary: a session gets `Case.context` and the mocked tools' state, never the ground truth, the
stimuli, the side channel or a canary.
"""

from __future__ import annotations

import json
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from auditkit.llm import LLMClient, Message, TextBlock, ToolResultBlock, ToolSpec
from auditkit.schema import ModelCall, Target, ToolCall
from auditkit.tools import ToolDef, ToolEnv, execute_tool, now


@dataclass
class AgentReply:
    text: str
    tool_calls: list[ToolCall]  # executed: result and timestamps filled
    model_calls: list[ModelCall]
    final_output: dict[str, Any] | None = None  # only targets that return a structured result
    tool_round_cap_hit: bool = False


@dataclass
class PromptedSession:
    """One trial's conversation with a prompted model; it keeps its own history."""

    adapter: PromptedModelAdapter
    system: str
    env: ToolEnv
    history: list[Message] = field(default_factory=list)
    pending: AgentReply | None = None  # the exchange in progress, or the last one

    def send(self, text: str) -> AgentReply:
        """The agent's whole answer to `text`. It is built in place in `pending`, so a call that
        raises midway leaves what the exchange already did (calls charged, mock tools run)."""
        a, target = self.adapter, self.adapter.target
        self.history.append(Message(role="user", blocks=[TextBlock(text=text)]))
        reply = self.pending = AgentReply("", [], [])
        texts: list[str] = []
        for round_no in range(target.max_tool_rounds + 1):
            c = a.client.complete(
                role="agent",
                model=a.model,
                system=self.system,
                messages=self.history,
                tools=a.specs,
                max_tokens=target.max_tokens,
                effort=target.effort,
            )
            reply.model_calls.append(c.call)
            if c.message.blocks:  # a refusal can return no content: nothing to append
                self.history.append(c.message)
            if c.text:
                texts.append(c.text)
                reply.text = "\n\n".join(texts)
            if not c.tool_uses:
                return reply
            if c.call.stop_reason != "tool_use" or round_no == target.max_tool_rounds:
                # unrun calls still get a result, so the history stays valid as sent
                why = "not run: " + (
                    "the tool round cap was reached"
                    if c.call.stop_reason == "tool_use"
                    else f"the response stopped ({c.call.stop_reason})"
                )
                self._results([(u.id, {"error": why}, True) for u in c.tool_uses])
                reply.tool_round_cap_hit = c.call.stop_reason == "tool_use"
                return reply
            done = []
            for use in c.tool_uses:
                call = execute_tool(self.env, a.tools, use.id, use.name, use.input, clock=a.clock)
                reply.tool_calls.append(call)
                done.append((use.id, call.result, call.is_error))
            self._results(done)
        raise AssertionError("unreachable: the last round returns")

    def _results(self, results: list[tuple[str, Any, bool]]) -> None:
        blocks = [
            ToolResultBlock(
                tool_use_id=use_id, content=json.dumps(result, ensure_ascii=False), is_error=error
            )
            for use_id, result, error in results
        ]
        self.history.append(Message(role="user", blocks=blocks))

    def close(self) -> None:
        self.history.clear()


@dataclass
class PromptedModelAdapter:
    """`kind: prompted_model`: a system prompt (the target's prompt file, its $placeholders filled
    from `Case.context`), the pack tools the target offers, a model."""

    target: Target
    client: LLMClient
    model: str
    prompt: str  # the prompt file's text, unfilled
    tools: Mapping[str, ToolDef]  # the tools the target offers, by name
    clock: Callable[[], datetime] = now

    @property
    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(t.name, t.description, t.parameters) for t in self.tools.values()]

    def new_session(self, context: Mapping[str, str], env: ToolEnv) -> PromptedSession:
        system = string.Template(self.prompt).safe_substitute(context)
        return PromptedSession(self, system, env)
