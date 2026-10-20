import copy
from collections.abc import Mapping, Sequence

from inspect_ai.event import Event, Timeline, timeline_build
from inspect_ai.log import transcript
from inspect_ai.util import store
from pydantic import JsonValue

from ._bus._record import Record, thaw_json
from ._ledger import SpanPlaces, SwarmLedger
from ._metrics import SwarmMetrics

LEDGER_KEY = "inspect_swarm:ledger"
METRICS_KEY = "inspect_swarm:metrics"


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
# even from a member's descendant; `loop` is the writing loop's agent span, and
# `nested` whether that loop is not the member's own.


def comm_sent(
    span_id: str | None,
    record: Record,
    nested: bool,
    rejected: tuple[str, str] | None,
) -> None:
    # every attempt that reached the bus, rejected or not; `rejected` is the control
    # that refused it and the reason the sender got
    evidence(
        "comm",
        "sent",
        member=record.sender,
        span_id=span_id,
        loop=record.sender_span_id,
        nested=nested,
        channel=record.channel,
        record=_record(record),
        rejected=None
        if rejected is None
        else {"control": rejected[0], "reason": rejected[1]},
    )


def comm_delivered(
    span_id: str | None,
    record: Record,
    nested: bool,
    seq: int,
    notify: list[str],
    result: Mapping[str, JsonValue] | None,
) -> None:
    evidence(
        "comm",
        "delivered",
        member=record.sender,
        span_id=span_id,
        loop=record.sender_span_id,
        nested=nested,
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


def write_metrics(metrics: SwarmMetrics) -> None:
    # synchronous, as the ledger is
    data = metrics.model_dump(mode="json")
    transcript().info({"type": "metrics", **data}, source="inspect_swarm")
    store().set(METRICS_KEY, data)


# A timeline named after each member, of the events in its spans, so that the viewer can
# show one member's trajectory. The timelines are added when the swarm starts, so that a
# name already taken in the sample (by another swarm running there, or one that ran
# before) is refused before any member runs, and a name taken later is refused where it
# is taken; they are filled when the swarm ends, and never raise over its outcome. A
# member that never entered its span has an empty timeline.
class MemberTimelines:
    def __init__(self, members: Sequence[str]) -> None:
        taken = [t.name for t in transcript().timelines if t.name in members]
        if taken:
            raise ValueError(
                f"The sample already has a timeline named {', '.join(repr(n) for n in taken)}, and each swarm member's timeline is named after the member, as when two swarms with the same member names run in one sample. Give the members other names with member(..., name=...)."
            )
        # the swarm's events are those reported from here on
        self._start = transcript().history.event_count
        self._timelines = [_timeline(name, []) for name in members]
        for timeline in self._timelines:
            transcript().add_timeline(timeline)

    def fill(self, places: SpanPlaces) -> None:
        events: dict[str, list[Event]] = {t.name: [] for t in self._timelines}
        for event in transcript().history.events_from(self._start):
            _, member = places(event.span_id)
            if member is not None:
                events[member].append(event)
        for timeline in self._timelines:
            timeline.root = _timeline(timeline.name, events[timeline.name]).root


def _timeline(name: str, events: list[Event]) -> Timeline:
    return timeline_build(events, name=name, description="Swarm member")
