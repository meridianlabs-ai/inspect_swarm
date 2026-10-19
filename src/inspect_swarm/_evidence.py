from inspect_ai.log import transcript
from inspect_ai.util import store
from pydantic import JsonValue

from ._ledger import SwarmLedger

LEDGER_KEY = "inspect_swarm:ledger"


# The swarm's evidence: an InfoEvent with source "inspect_swarm" whose payload has the
# format version, the evidence type and kind, a member and a span id. A member's result
# names the member and is written in its span; the final event names the chosen member
# (None without one) and is written in the swarm's span. Never member text.
def evidence(
    type: str,
    kind: str,
    *,
    member: str | None,
    span_id: str | None,
    **fields: JsonValue,
) -> None:
    transcript().info(
        {
            "version": 1,
            "type": type,
            "kind": kind,
            "member": member,
            "span_id": span_id,
            **fields,
        },
        source="inspect_swarm",
    )


def write_ledger(ledger: SwarmLedger) -> None:
    # synchronous, so that it runs under cancellation too; scorers read the store entry
    data = ledger.model_dump(mode="json")
    transcript().info({"type": "ledger", **data}, source="inspect_swarm")
    store().set(LEDGER_KEY, data)
