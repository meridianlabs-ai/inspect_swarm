import copy
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from inspect_ai.event import (
    CommunicationDelivery,
    CommunicationEvent,
    CommunicationKind,
    CommunicationRecord,
    CommunicationRejection,
    CommunicationSend,
    CommunicationWait,
    SpanBeginEvent,
    SwarmEvent,
    Timeline,
    TimelineSpan,
    timeline_build,
)
from inspect_ai.log import Transcript, transcript
from inspect_ai.util import store
from inspect_ai.util._span import AGENT_SPAN_TYPE
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


# Users isolation's evidence, in the swarm's span: `isolation` when the swarm starts
# (the sandbox, each member's user and the census roots) and `census` once its members
# have ended (who owns the entries under those roots).
def isolation_event(
    kind: Literal["isolation", "census"], data: Mapping[str, JsonValue]
) -> None:
    # synchronous, as the other evidence is
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        SwarmEvent(kind=kind, member=None, data={"version": 1, **data})
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
    files: list[JsonValue] | None = None,
) -> None:
    # a loop's tool was shown peer content, from the channel's state, never from the
    # content: `records` are the records the result concerns, `depends_on` those whose
    # content or news it showed that no earlier result of the loop had, and the shared
    # memory's `files` the versions it showed
    shown: dict[str, JsonValue] = {} if files is None else {"files": files}
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


# span names Inspect's timeline_build() reads as the sample's phases, wherever they are
PHASE_NAMES = ("init", "solvers", "scorers")

# the spans a running swarm is rooted at: one swarm at a time in each
_active_spans: set[str] = set()


# One timeline for the swarm, rooted at the swarm's span with each member's span as a
# child, so that the viewer shows the swarm as one agent with its members as lanes. It
# is named after the swarm's span, or the first of `name-2`, `name-3`, ... not yet a
# timeline in the sample. Built when the swarm starts, which refuses a span that cannot
# root it; opened, which adds it, once the swarm's other checks pass; filled when the
# swarm ends, never raising over its outcome; closed however the swarm ends. A swarm run
# outside every span, as only a test does, has no span to root it at and gets none.
class SwarmTimeline:
    def __init__(self, span_id: str | None) -> None:
        self._timeline: Timeline | None = None
        self._begin: SpanBeginEvent | None = None
        if span_id is None:
            return
        begin = _span_begin(span_id)
        if begin.type != AGENT_SPAN_TYPE or begin.name in PHASE_NAMES:
            raise ValueError(
                f"swarm(): the swarm runs in the span {begin.name!r} of type {begin.type!r}, and its timeline must be rooted at an agent span not named init, solvers or scorers. Run the swarm with run(swarm(...), input), or use it as the task's solver."
            )
        if span_id in _active_spans:
            raise ValueError(
                f"swarm(): another swarm is running in the agent span {begin.name!r}, and each running swarm needs a span of its own. Run each swarm with run(swarm(...), input), which gives it its own span."
            )
        self._begin = begin

    def open(self) -> None:
        if self._begin is None:
            return
        _active_spans.add(self._begin.id)
        # this swarm's events are those reported from here on: the span may hold an
        # earlier swarm's too, as when one agent awaits two swarms
        self._start = transcript().history.event_count
        taken = {t.name for t in transcript().timelines}
        name, n = self._begin.name, 1
        while name in taken:
            n += 1
            name = f"{self._begin.name}-{n}"
        self._timeline = Timeline(
            name=name,
            description="Swarm",
            root=TimelineSpan(
                id=self._begin.id, name=self._begin.name, span_type="agent"
            ),
        )
        transcript().add_timeline(self._timeline)

    def close(self) -> None:
        if self._begin is not None:
            _active_spans.discard(self._begin.id)

    def fill(self, places: SpanPlaces, member_spans: Collection[str]) -> None:
        if self._timeline is None or self._begin is None:
            return
        events = [self._begin] + [
            event
            for event in transcript().history.events_from(self._start)
            if places(event.span_id)[0]
        ]
        # with the span's begin, the built root's child is the swarm's span
        root = timeline_build(events, name=self._timeline.name).root
        self._timeline.root = next(
            (
                c
                for c in root.content
                if isinstance(c, TimelineSpan) and c.id == self._begin.id
            ),
            self._timeline.root,
        )
        # Inspect's builder marks a short agent with a prompt of its own, under a
        # parent that calls tools, as a utility helper, which the viewer hides; a
        # member is one of the swarm's agents however short its run
        _show_members(self._timeline.root, set(member_spans))


def _show_members(node: TimelineSpan, member_spans: set[str]) -> None:
    for item in node.content:
        if isinstance(item, TimelineSpan):
            if item.id in member_spans:
                item.utility = False
            _show_members(item, member_spans)


# The begin event of the span `span_id`, which the swarm runs in, searched back from the
# latest event.
def _span_begin(span_id: str) -> SpanBeginEvent:
    history = transcript().history
    end = history.event_count
    while end > 0:
        start = max(0, end - 64)
        for event in reversed(history.events_from(start, end - start)):
            if isinstance(event, SpanBeginEvent) and event.id == span_id:
                return event
        end = start
    raise RuntimeError(
        f"The swarm's span {span_id!r} has no begin event in the sample."
    )


# The member names of every swarm that has started in each sample, by the sample's
# transcript. A member name is the member's own across the sample: the analyses tell a
# swarm's evidence from a nested swarm's by member name, and a member's default tool
# state is scoped by its name.
_member_names: WeakKeyDictionary[Transcript, set[str]] = WeakKeyDictionary()


def claim_member_names(members: Sequence[str]) -> None:
    claimed = _member_names.setdefault(transcript(), set())
    taken = [name for name in members if name in claimed]
    if taken:
        raise ValueError(
            f"The sample already has a swarm member named {', '.join(repr(n) for n in taken)}, as when two swarms with the same member names run in one sample. Give the members other names with member(..., name=...)."
        )
    claimed.update(members)
