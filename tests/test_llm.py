"""The model client against a fake SDK, and the budget meter: what a request holds (no
sampling parameter, the role's effort, a forced tool where the model accepts one, thinking sent
back unchanged), what a response becomes, how errors are charged, and the cap."""

import sys
import types

import pytest

from auditkit.cost import cost_usd
from auditkit.llm import (
    AnthropicClient,
    BudgetExceeded,
    Message,
    MeteredClient,
    ProviderError,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    reservation_usd,
)
from auditkit.testing import ScriptedLLM, say

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
FLAG = ToolSpec("flag_risk", "Flag the intake.", {"type": "object", "properties": {}})


class FakeStatusError(Exception):
    def __init__(self, status_code: int, message: str = "bad") -> None:
        super().__init__(message)
        self.status_code, self.message = status_code, message


class FakeConnectionError(Exception):
    pass


def block(kind, **fields):
    return types.SimpleNamespace(type=kind, **fields)


def response(*content, stop_reason="end_turn", cache_read=None):
    usage = types.SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=cache_read,
    )
    return types.SimpleNamespace(
        content=list(content),
        model="claude-haiku-5-5-20261001",
        stop_reason=stop_reason,
        usage=usage,
        _request_id="req_42",
    )


@pytest.fixture
def sdk(monkeypatch):
    """A fake `anthropic` module: `sdk.sent` holds the requests, `sdk.answer` the next reply."""
    fake = types.SimpleNamespace(
        APIStatusError=FakeStatusError, APIConnectionError=FakeConnectionError, sent=[]
    )
    fake.answer = response(block("text", text="Hello."))

    class Anthropic:
        def __init__(self, *, max_retries):
            fake.max_retries = max_retries
            self.messages = self

        def create(self, **request):
            fake.sent.append(request)
            if isinstance(fake.answer, Exception):
                raise fake.answer
            return fake.answer

    fake.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return fake


def ask(client, *, model=HAIKU, messages=None, tools=(), force_tool=None, role="agent"):
    return client.complete(
        role=role,
        model=model,
        system="You are a test agent.",
        messages=messages or [Message(role="user", blocks=[TextBlock(text="Hi")])],
        tools=tools,
        force_tool=force_tool,
        max_tokens=4096,
        effort="medium",
    )


def test_a_request_sends_the_effort_and_never_a_sampling_parameter(sdk):
    ask(AnthropicClient(), tools=[FLAG])
    (request,) = sdk.sent
    assert request["output_config"] == {"effort": "medium"}
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(request)
    assert request["tools"] == [
        {"name": "flag_risk", "description": "Flag the intake.", "input_schema": FLAG.parameters}
    ]
    assert request["system"] == "You are a test agent." and request["max_tokens"] == 4096
    assert "tool_choice" not in request  # no forced tool: the model decides
    assert sdk.max_retries == 2  # the SDK retries 408, 409, 429 and 5xx itself


def test_a_forced_tool_is_sent_only_to_a_model_that_accepts_one(sdk):
    client = AnthropicClient()
    ask(client, tools=[FLAG], force_tool="flag_risk")
    ask(client, model=SONNET, tools=[FLAG], force_tool="flag_risk")
    assert sdk.sent[0]["tool_choice"] == {"type": "tool", "name": "flag_risk"}
    assert "tool_choice" not in sdk.sent[1]  # Sonnet 5.5 answers a forced tool with a 400


def test_the_history_goes_out_as_sent_thinking_blocks_included(sdk):
    history = [
        Message(role="user", blocks=[TextBlock(text="Hi")]),
        Message(
            role="assistant",
            blocks=[
                ThinkingBlock(thinking="", signature="sig-1"),
                ToolUseBlock(id="toolu_1", name="flag_risk", input={"reason": "item 9"}),
            ],
        ),
        Message(
            role="user",
            blocks=[ToolResultBlock(tool_use_id="toolu_1", content='{"status": "flagged"}')],
        ),
    ]
    ask(AnthropicClient(), messages=history, tools=[FLAG])
    assert sdk.sent[0]["messages"][1:] == [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "flag_risk",
                    "input": {"reason": "item 9"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"status": "flagged"}',
                    "is_error": False,
                }
            ],
        },
    ]


def test_a_response_becomes_text_tool_uses_the_turn_to_keep_and_a_model_call(sdk):
    sdk.answer = response(
        block("thinking", thinking="", signature="sig-2"),
        block("text", text="Thank you. "),
        block("tool_use", id="toolu_9", name="flag_risk", input={"reason": "item 9"}),
        block("text", text="Call 988."),
        stop_reason="tool_use",
        cache_read=5,
    )
    c = ask(AnthropicClient(), tools=[FLAG])
    assert c.text == "Thank you. Call 988."
    assert c.tool_uses == [ToolUseBlock(id="toolu_9", name="flag_risk", input={"reason": "item 9"})]
    assert [b.type for b in c.message.blocks] == ["thinking", "text", "tool_use", "text"]
    call = c.call
    assert (call.role, call.requested_model, call.served_model) == (
        "agent",
        HAIKU,
        "claude-haiku-5-5-20261001",
    )
    assert (call.effort, call.max_tokens, call.stop_reason, call.request_id) == (
        "medium",
        4096,
        "tool_use",
        "req_42",
    )
    assert (call.usage.input_tokens, call.usage.output_tokens) == (105, 20)  # cache reads count
    assert call.cost_usd == cost_usd(HAIKU, 105, 20) and call.temperature is None


def test_a_refusal_comes_back_with_its_stop_reason_and_no_content(sdk):
    sdk.answer = response(stop_reason="refusal")
    c = ask(AnthropicClient())
    assert (c.text, c.tool_uses, c.message.blocks, c.call.stop_reason) == ("", [], [], "refusal")


@pytest.mark.parametrize(
    ("error", "fault"),
    [
        (FakeStatusError(400, "temperature is not supported"), "harness"),
        (FakeStatusError(404, "model not found"), "harness"),
        (FakeStatusError(401, "invalid x-api-key"), "harness"),
        (FakeStatusError(403, "permission denied"), "harness"),
        (FakeStatusError(429, "rate limited"), "provider"),
        (FakeStatusError(529, "overloaded"), "provider"),
        (FakeConnectionError("timed out"), "provider"),
    ],
)
def test_an_api_error_is_a_provider_error_charged_to_whoever_can_fix_it(sdk, error, fault):
    sdk.answer = error
    with pytest.raises(ProviderError) as raised:
        ask(AnthropicClient())
    assert raised.value.fault == fault


# ── The budget meter ──────────────────────────────────────────────────────────────────────────────


def metered(cap, *, spent=0.0, replies=3):
    calls = []
    inner = ScriptedLLM(agent=[say("ok")] * replies)
    meter = MeteredClient(
        inner, cap_usd=cap, spent_usd=spent, on_first_call=lambda: calls.append(1)
    )
    return meter, inner, calls


def test_the_meter_charges_each_call_its_real_cost_and_signals_the_first_call_once():
    meter, inner, first = metered(1.0)
    one = ask(meter)
    two = ask(meter)
    assert meter.spent_usd == pytest.approx(one.call.cost_usd + two.call.cost_usd)
    assert first == [1] and len(inner.requests) == 2


def test_a_call_whose_reservation_would_cross_the_cap_is_not_made():
    messages = [Message(role="user", blocks=[TextBlock(text="Hi")])]
    reserve = reservation_usd(HAIKU, "You are a test agent.", messages, (), 4096)
    assert reserve > cost_usd(HAIKU, 0, 4096)  # a whole max_tokens of output, and the input
    meter, inner, first = metered(reserve * 1.5, spent=reserve)
    with pytest.raises(BudgetExceeded, match="spent"):
        ask(meter)
    assert (
        inner.requests == [] and first == []
    )  # refused before the client and the first-call stamp
