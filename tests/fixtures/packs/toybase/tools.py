"""Shared mocked tools, imported by toypack as `from packs.toybase.tools import …`."""

from typing import Any

from auditkit import ToolEnv, tool


@tool(
    description="Look up the status of a record.",
    parameters={
        "type": "object",
        "properties": {"record": {"type": "string"}},
        "required": ["record"],
    },
)
def lookup_record(arguments: dict[str, Any], env: ToolEnv) -> dict[str, Any]:
    records = env.state.get("records", {})
    record = arguments["record"]
    if record not in records:
        return {"error": f"no record {record}"}
    return {"record": record, **records[record]}
