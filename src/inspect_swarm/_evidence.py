import copy
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from inspect_ai.event import (
    CommunicationDelivery,
    CommunicationEvent,
    CommunicationKind,
    CommunicationRecord,
    CommunicationRejection,
    CommunicationSend,
    CommunicationWait,
    Event,
    SwarmEvent,
    Timeline,
    timeline_build,
)
from inspect_ai.log import transcript
from inspect_ai.util import store
from pydantic import JsonValue

from ._bus._record import Record, current_via, thaw_json
from ._ledger import SpanPlaces, SwarmLedger
from ._metrics import SwarmMetrics

if TYPE_CHECKING:
    from ._channel._channel import WaitSpec

LEDGER_KEY = "inspect_swarm:ledger"
METRICS_KEY = "inspect_swarm:metrics"


# The swarm's evidence other than communication: a `SwarmEvent` whose `data` has the
# format version and the kind's fields. A member's result names the member and is
# written in its span; the final event names the chosen member (None without one) and
# is written in the swarm's span. Never member text.
def swarm_event(
    kind: str,
    *,
    member: str | None,
    version: int = 1,
    **data: JsonValue,
) -> None:
    # synchronous, so that the writes in `finally` blocks run under cancellation too
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        SwarmEvent(kind=kind, member=member, data={"version": version, **data})
    )


# The turn seam's evidence, in the member's span, at a boundary where its callbacks ran:
# one `boundary` event, with what ended the chain, and one `changed` event for each
# callback whose result differed from what it received (origin `swarm` for the stale
# summaries the runtime removed), with message ids and hashes, never text. `applied` says
# whether the change is in the member's live conversation.
def seam_boundary(
    member: str,
    *,
    boundary: int,
    after: str,
    origins: list[str],
    held_for: float,
    outcome: str,
    messages: tuple[int, int],
    detail: Mapping[str, JsonValue],
) -> None:
    # `detail` is the origin and the error's type, for the outcomes that have them
    _seam_event(
        "boundary",
        member,
        {
            "boundary": boundary,
            "after": after,
            "origins": list[JsonValue](origins),
            "held_for": held_for,
            "outcome": outcome,
            "messages": {"before": messages[0], "after": messages[1]},
            **detail,
        },
    )


def seam_changed(
    member: str,
    *,
    boundary: int,
    origin: str,
    change: Mapping[str, JsonValue],
    applied: bool,
) -> None:
    _seam_event(
        "changed",
        member,
        {"boundary": boundary, "origin": origin, **change, "applied": applied},
    )


def _seam_event(kind: str, member: str, data: dict[str, JsonValue]) -> None:
    # synchronous, so that it is written however the chain ends
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        SwarmEvent(kind=kind, member=member, data={"version": 1, **data})
    )


# Communication evidence: a `CommunicationEvent` with source "inspect_swarm" for each
# record that reached the bus (`sent`), for each one it delivered (`delivered`), for
# each read of a channel's content (`read`) and for each wait (`waited`), written in the
# span the channel's tool runs in. Its `agent` and `agent_span_id` are the acting member
# and its member span, even from a member's descendant; `loop_span_id` is the agent span
# of the loop that acted, and `nested` whether that loop is not the member's own. What
# the typed fields do not hold is in `data`, with the format version. A read or wait's
# `data.via`, and a send's `data.record.via`, is "bridge" for a bridged member's call
# through swarm_bridged_tools(), whose CLI is one loop.
def _comm(
    kind: CommunicationKind,
    *,
    agent: str,
    agent_span_id: str | None,
    loop: str | None,
    nested: bool,
    channel: str | None,
    data: dict[str, JsonValue],
    record_ids: list[str],
    send: CommunicationSend | None = None,
    delivery: CommunicationDelivery | None = None,
    wait: CommunicationWait | None = None,
) -> None:
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        CommunicationEvent(
            kind=kind,
            source="inspect_swarm",
            agent=agent,
            agent_span_id=agent_span_id,
            loop_span_id=loop,
            nested=nested,
            channel=channel,
            record_ids=record_ids,
            send=send,
            delivery=delivery,
            wait=wait,
            data={"version": 1, **data},
        )
    )


def comm_sent(
    span_id: str | None,
    record: Record,
    nested: bool,
    rejected: tuple[str, str] | None,
) -> None:
    # every attempt that reached the bus, rejected or not; `rejected` is the control
    # that refused it and the reason the sender got
    _comm(
        "sent",
        agent=record.sender,
        agent_span_id=span_id,
        loop=record.sender_span_id,
        nested=nested,
        channel=record.channel,
        record_ids=[record.id],
        send=CommunicationSend(
            record=CommunicationRecord(
                id=record.id,
                kind=record.kind,
                sender=record.sender,
                to=list(record.to),
                recipients=list(record.recipients),
                content=record.payload,
                reply_to=record.reply_to,
            ),
            rejected=None
            if rejected is None
            else CommunicationRejection(control=rejected[0], reason=rejected[1]),
        ),
        data={
            "record": {
                "routed_out": list(record.routed_out),
                "op": None if record.op is None else thaw_json(record.op),
                "wake": record.wake,
                "sender_span_id": record.sender_span_id,
                "tool_span_id": record.tool_span_id,
                "via": record.via,
            }
        },
    )


def comm_delivered(
    span_id: str | None,
    record: Record,
    nested: bool,
    seq: int,
    notify: list[str],
    result: Mapping[str, JsonValue] | None,
) -> None:
    _comm(
        "delivered",
        agent=record.sender,
        agent_span_id=span_id,
        loop=record.sender_span_id,
        nested=nested,
        channel=record.channel,
        record_ids=[record.id],
        delivery=CommunicationDelivery(recipients=list(record.recipients), seq=seq),
        data={
            "notify": list(notify),
            # a copy: the channel keeps its own
            "result": None if result is None else copy.deepcopy(dict(result)),
        },
    )


# the keys of a `read` event's `data` that the bus writes, which a channel's own fields
# cannot take
READ_KEYS = frozenset(
    {"version", "read", "tool_span_id", "depends_on", "listing", "via"}
)


def comm_read(
    span_id: str | None,
    member: str,
    channel: str,
    read: str,
    loop: str | None,
    nested: bool,
    tool_span_id: str | None,
    records: list[str],
    depends_on: list[str],
    listing: bool,
    fields: Mapping[str, JsonValue] | None = None,
) -> None:
    # a loop's tool was shown peer content, from the channel's state, never from the
    # content: `records` are the records the result concerns, `depends_on` those whose
    # content or news it showed that no earlier result of the loop had, and `fields`
    # the channel's own data, such as the shared memory's `files`, the versions it
    # showed; a copy, since the channel keeps its own
    shown: dict[str, JsonValue] = {} if fields is None else copy.deepcopy(dict(fields))
    _comm(
        "read",
        agent=member,
        agent_span_id=span_id,
        loop=loop,
        nested=nested,
        channel=channel,
        record_ids=list(records),
        data={
            "read": read,
            "tool_span_id": tool_span_id,
            "depends_on": list(depends_on),
            **shown,
            "listing": listing,
            "via": current_via.get(),
        },
    )


def comm_waited(
    span_id: str | None,
    spec: "WaitSpec",
    *,
    nested: bool,
    channels: list[str],
    timeout: dict[str, JsonValue],
    started: float,
    duration: float,
    idle: float,
    outcome: str,
    woken_by: list[int],
    files: list[JsonValue],
    depends_on: list[str],
    ended: list[JsonValue],
) -> None:
    # one swarm_wait, when it returns or is cancelled: what ended it, the commits that
    # released it for news (`delivered`'s `delivery.seq`), the files and ends its result
    # named, and the records whose news it showed that no earlier result of the loop
    # had; `idle` is the seconds of it during which its member counted as waiting
    _comm(
        "waited",
        agent=spec.member,
        agent_span_id=span_id,
        loop=spec.loop,
        nested=nested,
        channel=None,
        record_ids=[],
        wait=CommunicationWait(
            id=spec.id, channels=list(channels), outcome=outcome, duration=duration
        ),
        data={
            "path": spec.path,
            "file_id": spec.file_id,
            "timeout": timeout,
            "started": started,
            "idle": idle,
            "woken_by": list(woken_by),
            "files": files,
            "depends_on": list(depends_on),
            "ended": ended,
            "via": current_via.get(),
        },
    )


# The ledger and the metrics: a `SwarmEvent` whose `data` is the store entry, the
# model's dump with its own `version`.
def write_ledger(ledger: SwarmLedger) -> None:
    # synchronous, so that it runs under cancellation too; scorers read the store entry
    data = ledger.model_dump(mode="json")
    _write_dump("ledger", data)
    store().set(LEDGER_KEY, data)


def write_metrics(metrics: SwarmMetrics) -> None:
    # synchronous, as the ledger is
    data = metrics.model_dump(mode="json")
    _write_dump("metrics", data)
    store().set(METRICS_KEY, data)


def _write_dump(kind: str, data: dict[str, JsonValue]) -> None:
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        SwarmEvent(kind=kind, member=None, data=data)
    )


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
