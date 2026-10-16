from inspect_ai.log import transcript
from inspect_ai.util import store

from ._ledger import SwarmLedger

LEDGER_KEY = "inspect_swarm:ledger"


def write_ledger(ledger: SwarmLedger) -> None:
    # synchronous, so that it runs under cancellation too; scorers read the store entry
    data = ledger.model_dump(mode="json")
    transcript().info({"type": "ledger", **data}, source="inspect_swarm")
    store().set(LEDGER_KEY, data)
