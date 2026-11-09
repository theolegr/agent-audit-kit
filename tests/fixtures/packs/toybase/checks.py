"""A check for packs that use toybase's tools. Imported into a pack, it takes that pack's prefix."""

from auditkit import CheckContext, Transcript, check, failed, passed
from auditkit.schema import CheckOutcome


@check(
    id="record_looked_up",
    kind="conformance",
    method="deterministic",
    hazards=["H-01"],
    controls=["H-01.C1"],
    needs=["tool_calls"],
    description="The agent called lookup_record at least once.",
)
def record_looked_up(t: Transcript, ctx: CheckContext) -> CheckOutcome:
    agent = [turn for turn in t.turns if turn.role == "agent"]
    if any(call.name == "lookup_record" for turn in agent for call in turn.tool_calls):
        return passed()
    return failed("no lookup_record call", turn=agent[-1].index)
