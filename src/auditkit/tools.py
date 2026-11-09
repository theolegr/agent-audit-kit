"""Mocked tools: what a pack's `@tool` functions receive, and how the harness executes them.

Tools are pure given `(arguments, env)`: no clock, no randomness. Replay never re-executes a tool.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from auditkit.schema import ToolCall


@dataclass
class ToolEnv:
    state: dict[str, Any]  # deep copy of Case.environment, per trial: mocks can hold state
    context: Mapping[str, str]  # Case.context (tenant, user, locale, today)
    policy: Mapping[str, Any]  # Target.tool_policy (e.g. enforce_tenant_scope: false)


ToolFn = Callable[[dict[str, Any], ToolEnv], Any]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema of the arguments, offered to the agent as is
    side_effect: bool
    fn: ToolFn


def now() -> datetime:
    return datetime.now(UTC)


# pydantic cannot write a value about 255 levels deep, nor read back a trial file about 200 levels
# deep, where a tool call's arguments and result start 6 levels down: a wide margin below both
MAX_DEPTH = 100
# How the error of a call whose arguments `execute_tool` refuses starts. A mock's own error starts
# with its exception's name instead, so this prefix tells a refused call from any other error.
REFUSED_ARGUMENTS = "arguments are not strict JSON"


def _strict_json(value: Any) -> str:
    """`value` as JSON that a transcript saves as is: no NaN or Infinity (pydantic writes null), no
    lone surrogate (UTF-8 cannot encode it), no nesting deeper than MAX_DEPTH (pydantic cannot save
    or read it back). Raises TypeError or ValueError otherwise."""
    stack = [(value, 1)]  # walked, not recursed: json.dumps never sees a value too deep for it
    while stack:
        item, level = stack.pop()
        if isinstance(item, dict | list | tuple):
            if level > MAX_DEPTH:
                raise ValueError(f"nested more than {MAX_DEPTH} levels deep")
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, level + 1) for child in children)
    text = json.dumps(value, allow_nan=False, ensure_ascii=False)
    text.encode("utf-8")
    return text


def execute_tool(
    env: ToolEnv,
    tools: Mapping[str, ToolDef],
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    clock: Callable[[], datetime] = now,
) -> ToolCall:
    """Run one mocked tool call. An unknown tool, an exception or a result that is not JSON becomes
    `is_error=True` with `{"error": …}`: it goes back to the agent and stays in the transcript.
    So do arguments that are not strict JSON: the tool does not run, and the error holds them."""
    called_at = clock()
    tool = tools.get(name)
    try:
        _strict_json(arguments)
        bad_arguments = None
    except (TypeError, ValueError) as e:  # UnicodeEncodeError is a ValueError
        bad_arguments = f"{type(e).__name__}: {e}"
    if bad_arguments is not None:
        # saved as is, they would change (NaN → null) or not save at all (a lone surrogate): the
        # error keeps them as text (repr escapes surrogates) and `arguments` records none
        reason = f"{REFUSED_ARGUMENTS} ({bad_arguments}): {arguments!r}"
        result, is_error, arguments = {"error": reason}, True, {}
    elif tool is None:
        result, is_error = {"error": f"unknown tool: {name}"}, True
    else:
        try:
            # the tool gets its own copy: the recorded arguments stay what the agent sent
            raw = tool.fn(copy.deepcopy(arguments), env)
            # a strict JSON round-trip: the result saves as is, and is detached from env.state
            result, is_error = json.loads(_strict_json(raw)), False
        except Exception as e:  # a mock that breaks is evidence, not a harness crash
            result, is_error = {"error": f"{type(e).__name__}: {e}"}, True
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        result=result,
        is_error=is_error,
        called_at=called_at,
        returned_at=clock(),
    )


def refused_arguments(call: ToolCall) -> str | None:
    """The text in which `call`'s error keeps the arguments `execute_tool` refused (the reason,
    then their repr), with repr's escapes undone: a line break, a zero-width space or a lone
    surrogate the agent wrote reads as itself, not as a backslash and letters that would split a
    string around it. None when its arguments were not refused, a mock's own error included."""
    error = call.result.get("error") if call.is_error and isinstance(call.result, dict) else None
    if isinstance(error, str) and error.startswith(REFUSED_ARGUMENTS):
        # unicode_escape reads bytes as Latin-1, so what lies beyond goes in as an escape too; a
        # backslash the agent wrote, which repr doubled, comes back single
        kept = error.removeprefix(REFUSED_ARGUMENTS).encode("latin-1", "backslashreplace")
        return kept.decode("unicode_escape")
    return None
