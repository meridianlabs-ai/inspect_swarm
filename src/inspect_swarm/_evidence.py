import copy
from collections.abc import Mapping

from inspect_ai.log import transcript
from inspect_ai.util import store
from pydantic import JsonValue

from ._bus._record import Record, thaw_json
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


# Communication evidence: a `comm` event for each record that reached the bus (`sent`)
# and for each one it delivered (`delivered`), written in the span the channel's tool
# runs in. Its `member` and `span_id` are the sender's and the sender's member span,
# even from a member's descendant; the record keeps the writing loop's span.


def comm_sent(
    span_id: str | None, record: Record, rejected: tuple[str, str] | None
) -> None:
    # every attempt that reached the bus, rejected or not; `rejected` is the control
    # that refused it and the reason the sender got
    evidence(
        "comm",
        "sent",
        member=record.sender,
        span_id=span_id,
        channel=record.channel,
        record=_record(record),
        rejected=None
        if rejected is None
        else {"control": rejected[0], "reason": rejected[1]},
    )


def comm_delivered(
    span_id: str | None,
    record: Record,
    seq: int,
    notify: list[str],
    result: Mapping[str, JsonValue] | None,
) -> None:
    evidence(
        "comm",
        "delivered",
        member=record.sender,
        span_id=span_id,
        channel=record.channel,
        record=record.id,
        recipients=list(record.recipients),
        seq=seq,
        notify=list(notify),
        # a copy: the channel keeps its own
        result=None if result is None else copy.deepcopy(dict(result)),
    )


def _record(record: Record) -> dict[str, JsonValue]:
    return {
        "id": record.id,
        "channel": record.channel,
        "kind": record.kind,
        "sender": record.sender,
        "to": list(record.to),
        "recipients": list(record.recipients),
        "routed_out": list(record.routed_out),
        "payload": record.payload,
        "op": None if record.op is None else thaw_json(record.op),
        "wake": record.wake,
        "reply_to": record.reply_to,
        "sender_span_id": record.sender_span_id,
        "tool_span_id": record.tool_span_id,
    }


def write_ledger(ledger: SwarmLedger) -> None:
    # synchronous, so that it runs under cancellation too; scorers read the store entry
    data = ledger.model_dump(mode="json")
    transcript().info({"type": "ledger", **data}, source="inspect_swarm")
    store().set(LEDGER_KEY, data)
