"""Mocked tool execution: results, errors and timestamps are recorded, never raised."""

from datetime import UTC, datetime, timedelta

import pytest

from auditkit import ToolEnv, tool
from auditkit.checks import TOOL_ATTR
from auditkit.schema import ToolCall
from auditkit.tools import execute_tool, refused_arguments

T0 = datetime(2026, 11, 8, 10, 15, tzinfo=UTC)


def ticking_clock():
    ticks = iter(T0 + timedelta(seconds=s) for s in range(100))
    return lambda: next(ticks)


@tool(description="Count calls.", parameters={"type": "object"}, side_effect=True)
def bump(arguments, env):
    env.state["count"] = env.state.get("count", 0) + 1
    return {
        "count": env.state["count"],
        "policy": env.policy.get("mode"),
        "who": env.context["user"],
    }


@tool(description="Return the shared list.", parameters={"type": "object"})
def shared(arguments, env):
    return env.state.setdefault("items", [])


@tool(description="Mutate the arguments.", parameters={"type": "object"})
def mutating(arguments, env):
    arguments["extra"] = True
    return {"ok": True}


@tool(description="Break.", parameters={"type": "object"})
def broken(arguments, env):
    raise KeyError("record")


@tool(description="Return something JSON cannot hold.", parameters={"type": "object"})
def not_json(arguments, env):
    return {"when": datetime.now(UTC)}


@tool(description="Return NaN, which strict JSON cannot hold.", parameters={"type": "object"})
def not_a_number(arguments, env):
    return {"x": float("nan")}


@tool(description="Return a lone surrogate, which UTF-8 cannot encode.", parameters={})
def lone_surrogate(arguments, env):
    return {"x": "\ud800"}


def nested(depth):
    """A list `depth` levels deep: json.dumps takes 300, pydantic cannot save or read it back."""
    value = []
    for _ in range(depth - 1):
        value = [value]
    return value


@tool(description="Return a list nested 300 levels deep.", parameters={"type": "object"})
def too_deep(arguments, env):
    return nested(300)


TOOLS = {
    f.__name__: getattr(f, TOOL_ATTR)
    for f in (bump, shared, mutating, broken, not_json, not_a_number, lone_surrogate, too_deep)
}


def env(**state):
    return ToolEnv(state=dict(state), context={"user": "u-17"}, policy={"mode": "strict"})


def run(e, name, arguments=None, clock=None):
    return execute_tool(e, TOOLS, "toolu_1", name, arguments or {}, clock=clock or ticking_clock())


def test_tool_call_is_recorded_with_result_and_timestamps():
    call = run(env(), "bump")
    assert (call.id, call.name, call.is_error) == ("toolu_1", "bump", False)
    assert call.result == {"count": 1, "policy": "strict", "who": "u-17"}
    assert call.called_at == T0
    assert call.returned_at == T0 + timedelta(seconds=1)
    assert call.executed_by == "harness_mock"


def test_tool_state_persists_within_one_environment():
    e = env()
    run(e, "bump")
    assert run(e, "bump").result["count"] == 2


def test_recorded_result_is_detached_from_tool_state():
    e = env()
    call = run(e, "shared")
    e.state["items"].append("later")
    assert call.result == []


def test_recorded_arguments_are_what_the_agent_sent():
    call = run(env(), "mutating", {"record": "R-1"})
    assert call.arguments == {"record": "R-1"}


def test_unknown_tool_becomes_an_error_result():
    call = run(env(), "nope")
    assert call.is_error
    assert call.result == {"error": "unknown tool: nope"}


def test_tool_exception_becomes_an_error_result():
    call = run(env(), "broken")
    assert call.is_error
    assert call.result == {"error": "KeyError: 'record'"}


def test_result_that_is_not_json_becomes_an_error_result():
    call = run(env(), "not_json")
    assert call.is_error
    assert call.result["error"].startswith("TypeError")


def test_result_with_nan_becomes_an_error_result():
    call = run(env(), "not_a_number")
    assert call.is_error
    assert call.result["error"].startswith("ValueError")


def test_result_with_a_lone_surrogate_becomes_an_error_result():
    call = run(env(), "lone_surrogate")
    assert call.is_error
    assert call.result["error"].startswith("UnicodeEncodeError")
    call.model_dump_json()  # the error result itself can be saved


def test_result_nested_too_deep_becomes_an_error_result():
    call = run(env(), "too_deep")
    assert call.is_error
    assert call.result["error"].startswith("ValueError")
    assert ToolCall.model_validate_json(call.model_dump_json()) == call


# NaN and Infinity would be saved as null; a lone surrogate cannot be saved, or only replaced;
# nor can a value nested 300 levels deep
@pytest.mark.parametrize(
    "arguments",
    [{"x": float("nan")}, {"x": float("-inf")}, {"x": "\ud800"}, {"\ud800": 1}, {"x": nested(300)}],
    ids=["nan", "infinity", "surrogate-value", "surrogate-key", "too-deep"],
)
def test_arguments_not_strict_json_become_an_error_saved_without_change(arguments):
    e = env()
    call = run(e, "bump", arguments)
    assert "count" not in e.state  # the tool did not run
    assert call.is_error
    assert call.result["error"].startswith("arguments are not strict JSON")
    assert repr(arguments) in call.result["error"]  # what the agent sent, recorded as text
    assert ToolCall.model_validate_json(call.model_dump_json()) == call


def test_refused_arguments_are_read_as_the_agent_wrote_them():
    # repr escapes a line break, a zero-width space and a lone surrogate, and doubles a
    # backslash: `refused_arguments` undoes it, so a string split by one reads whole
    note = "PINE\nAPPLE\u200b\ud800 \u0436 \U0001f600 C:\\new"
    kept = refused_arguments(run(env(), "bump", {"note": note, "x": float("nan")}))
    assert note in kept
    assert refused_arguments(run(env(), "broken")) is None  # a mock's own error
