"""Model clients: the neutral messages we own, the Anthropic client and the budget meter.

Only this module imports `anthropic`, and only when an `AnthropicClient` is built: grading and
replay never load it (boundary tests 3 and 4). No sampling parameter is ever
sent: API version 2026-10-21 rejects `temperature`, `top_p` and `top_k` on every model, so a
role's depth and spend are set with `effort`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from auditkit.cost import cost_usd, price
from auditkit.schema import Effort, ModelCall, Usage

Role = Literal["agent", "simulator", "judge"]

# a forced tool_choice returns a 400 on these models (claude-api reference, 2026-10): they get
# "auto", and the caller's prompt names the tool
NO_FORCED_TOOL = frozenset(
    {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1"}
)
# the request or the configuration is wrong (bad request, key, permission, model): ours to fix
HARNESS_STATUSES = frozenset({400, 401, 403, 404, 413, 422})
RESERVE_CHARS_PER_TOKEN = 3  # Haiku 5.5 counts ~30% more tokens than the old 4-characters average


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBlock(_Strict):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(_Strict):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(_Strict):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str  # JSON-dumped result
    is_error: bool = False


class ThinkingBlock(_Strict):  # sent back unchanged with the turn that holds it
    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str


class RedactedThinkingBlock(_Strict):
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


Block = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock | RedactedThinkingBlock,
    Field(discriminator="type"),
]


class Message(_Strict):
    role: Literal["user", "assistant"]
    blocks: list[Block]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema of the input


@dataclass
class Completion:
    text: str  # the response's text blocks, joined
    tool_uses: list[ToolUseBlock]
    message: Message  # the assistant turn as returned, thinking included: append it to history
    call: ModelCall


class ProviderError(Exception):
    """A model call failed. `fault` is "harness" when the request or the configuration was wrong
    (a 400, 401, 403, 404, 413 or 422: ours to fix), "provider" otherwise (rate limit after the
    SDK's retries, 5xx, network)."""

    def __init__(self, message: str, fault: Literal["harness", "provider"] = "provider"):
        super().__init__(message)
        self.fault = fault


class BudgetExceeded(Exception):
    """The next call's reservation would take the run's spend above its cap: it is not made."""


class LLMClient(Protocol):
    def complete(
        self,
        *,
        role: Role,
        model: str,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        force_tool: str | None = None,
        max_tokens: int,
        effort: Effort,
    ) -> Completion: ...


def _wire_block(block: Block) -> dict[str, Any]:
    return block.model_dump(exclude_none=True)


def _wire(message: Message) -> dict[str, Any]:
    return {"role": message.role, "content": [_wire_block(b) for b in message.blocks]}


def _neutral(block: Any) -> Block | None:
    """A response content block in our own terms; a type we do not use is dropped."""
    kind = getattr(block, "type", None)
    if kind == "text":
        return TextBlock(text=block.text)
    if kind == "tool_use":
        return ToolUseBlock(id=block.id, name=block.name, input=dict(block.input or {}))
    if kind == "thinking":
        return ThinkingBlock(thinking=block.thinking or "", signature=block.signature)
    if kind == "redacted_thinking":
        return RedactedThinkingBlock(data=block.data)
    return None


class AnthropicClient:
    """The Messages API through the official SDK. The SDK retries 408, 409, 429 and 5xx itself."""

    def __init__(self, *, max_retries: int = 2) -> None:
        import anthropic  # the only import of the SDK: never on the grading or replay path

        self._sdk = anthropic
        self._client = anthropic.Anthropic(max_retries=max_retries)

    def complete(
        self,
        *,
        role: Role,
        model: str,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        force_tool: str | None = None,
        max_tokens: int,
        effort: Effort,
    ) -> Completion:
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [_wire(m) for m in messages],
            "output_config": {"effort": effort},
        }
        if tools:
            request["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        if force_tool is not None and model not in NO_FORCED_TOOL:
            request["tool_choice"] = {"type": "tool", "name": force_tool}
        started = time.monotonic()
        try:
            response = self._client.messages.create(**request)
        except self._sdk.APIStatusError as e:
            fault = "harness" if e.status_code in HARNESS_STATUSES else "provider"
            raise ProviderError(f"{type(e).__name__} ({e.status_code}): {e.message}", fault) from e
        except self._sdk.APIConnectionError as e:  # timeouts included
            raise ProviderError(f"{type(e).__name__}: {e}") from e
        latency = int((time.monotonic() - started) * 1000)
        blocks = [b for b in map(_neutral, response.content) if b is not None]
        usage = response.usage
        cached = (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0)
        tokens = Usage(input_tokens=usage.input_tokens + cached, output_tokens=usage.output_tokens)
        call = ModelCall(
            role=role,
            requested_model=model,
            served_model=response.model,
            effort=effort,
            max_tokens=max_tokens,
            usage=tokens,
            cost_usd=cost_usd(model, tokens.input_tokens, tokens.output_tokens),
            stop_reason=response.stop_reason,
            request_id=getattr(response, "_request_id", None),
            latency_ms=latency,
        )
        return Completion(
            text="".join(b.text for b in blocks if isinstance(b, TextBlock)),
            tool_uses=[b for b in blocks if isinstance(b, ToolUseBlock)],
            message=Message(role="assistant", blocks=blocks),
            call=call,
        )


def reservation_usd(
    model: str,
    system: str,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    max_tokens: int,
) -> float:
    """What a call may cost at most, nearly: its input at ~3 characters a token (an average, not a
    bound) and its whole `max_tokens` of output (a true worst case: thinking counts in it)."""
    text = system + json.dumps([_wire(m) for m in messages], ensure_ascii=False)
    text += json.dumps([t.parameters for t in tools])
    p = price(model)
    input_tokens = len(text) // RESERVE_CHARS_PER_TOKEN + 1
    return (input_tokens * p.input_per_mtok + max_tokens * p.output_per_mtok) / 1_000_000


class MeteredClient:
    """Wraps any client and enforces the run's cap: before each call it reserves the call's
    worst case and refuses it if the spend would cross the cap; after it, it charges the real
    usage. The only place where cost is enforced. `on_first_call` runs once, just before the
    first call is made."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        cap_usd: float,
        spent_usd: float = 0.0,
        on_first_call: Callable[[], None] | None = None,
    ) -> None:
        self.inner, self.cap_usd, self.spent_usd = inner, cap_usd, spent_usd
        self._on_first_call = on_first_call

    def complete(self, **request: Any) -> Completion:
        reserve = reservation_usd(
            request["model"],
            request["system"],
            request["messages"],
            request.get("tools", ()),
            request["max_tokens"],
        )
        if self.spent_usd + reserve > self.cap_usd:
            raise BudgetExceeded(
                f"the next {request['role']} call may cost ${reserve:.4f}: spent "
                f"${self.spent_usd:.4f} of the ${self.cap_usd:.2f} cap"
            )
        if self._on_first_call is not None:
            first, self._on_first_call = self._on_first_call, None
            first()
        completion = self.inner.complete(**request)
        self.spent_usd += completion.call.cost_usd
        return completion
