from collections.abc import Mapping

from inspect_ai.log import transcript
from pydantic import JsonValue

from ._bus._policy import CommDecision
from ._bus._record import Record

# Communication evidence: an InfoEvent for each record that reached the bus (`sent`) and
# for each one it delivered (`delivered`), written in the span the channel's tool runs in.


def comm_sent(
    record: Record,
    decision: CommDecision | None,
    original_payload: str | None,
    rejected: tuple[str, str] | None,
) -> None:
    # every attempt that reached the bus, rejected or not; `rejected` is the control
    # that refused it and the reason the sender got
    data: dict[str, JsonValue] = {
        **_comm("sent", record),
        "record": _record(record),
        "decision": None
        if decision is None
        else {"action": decision.action, "explanation": decision.explanation},
        "rejected": None
        if rejected is None
        else {"control": rejected[0], "reason": rejected[1]},
    }
    if original_payload is not None:
        data["original_payload"] = original_payload
    transcript().info(data, source="inspect_swarm")


def comm_delivered(
    record: Record,
    seq: int,
    notify: list[str],
    result: Mapping[str, JsonValue] | None,
) -> None:
    transcript().info(
        {
            **_comm("delivered", record),
            "record": record.id,
            "recipients": list(record.recipients),
            "seq": seq,
            "notify": list(notify),
            "result": None if result is None else dict(result),
        },
        source="inspect_swarm",
    )


def _comm(kind: str, record: Record) -> dict[str, JsonValue]:
    return {
        "version": 1,
        "type": "comm",
        "kind": kind,
        "channel": record.channel,
        "member": record.sender,
        "span_id": record.sender_span_id,
    }


def _record(record: Record) -> dict[str, JsonValue]:
    return {
        "id": record.id,
        "channel": record.channel,
        "kind": record.kind,
        "origin": record.origin,
        "sender": record.sender,
        "to": list(record.to),
        "recipients": list(record.recipients),
        "routed_out": list(record.routed_out),
        "payload": record.payload,
        "op": None if record.op is None else dict(record.op),
        "wake": record.wake,
        "reply_to": record.reply_to,
        "via": record.via,
        "sender_span_id": record.sender_span_id,
        "tool_span_id": record.tool_span_id,
    }
