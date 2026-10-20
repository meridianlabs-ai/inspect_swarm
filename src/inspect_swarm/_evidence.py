from collections.abc import Sequence

from inspect_ai.event import Event, Timeline, timeline_build
from inspect_ai.log import transcript
from inspect_ai.util import store
from pydantic import JsonValue

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
