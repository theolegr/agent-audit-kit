"""The prompted-model adapter: the prompt filled from the case context, the tool loop and its
cap, refusals, and a history sent back as it came (thinking blocks included)."""

from pathlib import Path

import pytest

from auditkit.adapters import PromptedModelAdapter
from auditkit.llm import Completion, Message, TextBlock, ThinkingBlock, ToolUseBlock
from auditkit.pack import load_pack
from auditkit.testing import REFUSAL, ScriptedLLM, say, use
from auditkit.tools import ToolEnv

FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "packs"
MODEL = "claude-haiku-5-5"


@pytest.fixture
def toy():
    return load_pack(FIXTURE_PACKS, "toypack")


def session(pack, client, *, rounds=None, context=None):
    target = pack.targets["basic"]
    if rounds is not None:
        target = target.model_copy(update={"max_tool_rounds": rounds})
    prompt = (pack.dir / "targets" / target.system_prompt_file).read_text()
    tools = {name: pack.tools[name] for name in target.tools}
    adapter = PromptedModelAdapter(target, client, MODEL, prompt, tools)
    environment = pack.cases["toy-inject"].environment
    env = ToolEnv(state=dict(environment), context=context or {}, policy=target.tool_policy)
    return adapter.new_session(context or {"locale": "en-CA"}, env)


def test_the_prompt_is_filled_from_the_case_context_only(toy):
    llm = ScriptedLLM(agent=[say("Hello.")])
    session(toy, llm, context={"locale": "en-CA", "ground_truth": "R-2 shipped"}).send("Hi")
    system = llm.requests[0]["system"]
    assert "(en-CA)" in system and "$locale" not in system
    assert (llm.requests[0]["effort"], llm.requests[0]["max_tokens"]) == ("medium", 1024)


def test_a_tool_round_runs_the_mock_records_the_call_and_sends_the_result_back(toy):
    llm = ScriptedLLM(
        agent=[use("lookup_record", {"record": "R-2"}, "Let me check."), say("Shipped.")]
    )
    reply = session(toy, llm).send("Where is R-2?")
    assert reply.text == "Let me check.\n\nShipped." and not reply.tool_round_cap_hit
    (call,) = reply.tool_calls
    assert (call.name, call.arguments, call.is_error) == ("lookup_record", {"record": "R-2"}, False)
    assert call.result["status"] == "shipped" and call.called_at <= call.returned_at
    assert [c.stop_reason for c in reply.model_calls] == ["tool_use", "end_turn"]
    second = llm.requests[1]["messages"]
    assert [m.role for m in second] == ["user", "assistant", "user"]
    assert second[2].blocks[0].type == "tool_result" and "shipped" in second[2].blocks[0].content


def test_the_tool_round_cap_stops_the_reply_and_answers_the_unrun_call(toy):
    llm = ScriptedLLM(agent=[use("lookup_record", {"record": "R-2"})] * 3)
    s = session(toy, llm, rounds=1)
    reply = s.send("Where is R-2?")
    assert reply.tool_round_cap_hit and len(reply.tool_calls) == 1 and len(llm.requests) == 2
    last = s.history[-1].blocks[0]  # the history stays valid: every tool use has a result
    assert last.type == "tool_result" and last.is_error and "tool round cap" in last.content


def test_a_refusal_is_the_agent_s_answer_with_no_text(toy):
    llm = ScriptedLLM(agent=[REFUSAL, say("Hello again.")])
    s = session(toy, llm)
    reply = s.send("Hi")
    assert (reply.text, reply.tool_calls, reply.model_calls[0].stop_reason) == ("", [], "refusal")
    assert [m.role for m in s.history] == ["user"]  # no empty assistant turn is kept
    assert s.send("Hello?").text == "Hello again."


class Thinker:
    """A client whose reply opens with a thinking block, then calls a tool, then answers."""

    def __init__(self):
        self.requests, self.inner = (
            [],
            ScriptedLLM(agent=[use("lookup_record", {"record": "R-2"}), say("Shipped.")]),
        )

    def complete(self, **request):
        self.requests.append([m.model_copy(deep=True) for m in request["messages"]])
        c = self.inner.complete(**request)
        blocks = [
            ThinkingBlock(thinking="", signature=f"sig-{len(self.requests)}"),
            *c.message.blocks,
        ]
        return Completion(c.text, c.tool_uses, Message(role="assistant", blocks=blocks), c.call)


def test_thinking_blocks_go_back_unchanged_with_the_tool_results(toy):
    client = Thinker()
    session(toy, client).send("Where is R-2?")
    sent = client.requests[1][1]  # the second request's assistant turn
    assert sent.role == "assistant"
    assert sent.blocks[0] == ThinkingBlock(thinking="", signature="sig-1")
    assert isinstance(sent.blocks[1], ToolUseBlock) and sent.blocks[1].input == {"record": "R-2"}
    assert not any(isinstance(b, TextBlock) and b.text == "" for b in sent.blocks)
