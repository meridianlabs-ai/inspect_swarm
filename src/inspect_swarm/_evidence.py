from collections.abc import Sequence

from inspect_ai.event import Event, timeline_build
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
# show one member's trajectory. `members` are the members that entered their span, in
# roster order; a member that did not has no events and no timeline.
def add_timelines(places: SpanPlaces, members: Sequence[str]) -> None:
    events: dict[str, list[Event]] = {name: [] for name in members}
    for event in transcript().events:
        _, member = places(event.span_id)
        if member is not None:
            events[member].append(event)
    for name in members:
        transcript().add_timeline(
            timeline_build(events[name], name=name, description="Swarm member")
        )
