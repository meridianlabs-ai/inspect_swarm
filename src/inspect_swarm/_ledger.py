import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import anyio
from inspect_ai.event import (
    Event,
    InfoEvent,
    ModelEvent,
    ScoreEvent,
    SpanBeginEvent,
    ToolEvent,
)
from inspect_ai.log._samples import sample_active
from inspect_ai.log._transcript import transcript
from inspect_ai.model import ModelAPI, ModelUsage
from inspect_ai.model._model import sample_model_usage
from inspect_ai.scorer import Value
from inspect_ai.util import Limit, current_span_id, sample_limits
from inspect_ai.util._span import AGENT_SPAN_TYPE
from pydantic import BaseModel, ConfigDict, JsonValue

from ._budget import Budget, CostPair, cost_node, token_node
from ._fence import utf8_bytes

logger = logging.getLogger(__name__)

# The relative tolerance on the unattributed cost. The sample's cost over the interval
# is the difference of two cumulative sums, which carry the rounding error of the whole
# sample's cost so far, and the meters sum per call; so the tolerance is relative to the
# cumulative cost of the models in the interval, not to the interval's cost.
COST_TOLERANCE = 1e-9

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_tokens_cache_write",
    "input_tokens_cache_read",
    "reasoning_tokens",
)


class Meter(BaseModel):
    """Usage and cost recorded by one of the swarm's meters.

    A meter counts the model calls made in its context: one member's, the runtime's, the final step's, or the whole swarm's.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)

    usage: ModelUsage
    """Token usage of the calls recorded, except those made while token metering was suspended (approval and review calls)."""

    cost: float
    """Cost in dollars of the priced calls recorded, suspended calls included."""

    suspended_cost: float
    """The part of `cost` recorded while token metering was suspended (approval and review calls)."""


class Calls(BaseModel):
    """Counts of the model events in the swarm, or in one member's spans.

    `errored`, `no_usage` and `unpriced` count calls whose cost is missing, so any of them above zero makes the cost a lower bound.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)

    calls: int = 0
    """Model calls that completed, cache replays excluded."""

    cache_replays: int = 0
    """Model events replayed from the cache (`cache="read"`). They cost nothing."""

    errored: int = 0
    """Model calls that ended in an error or were cancelled, plus the requests a provider's client retried inside one call. Their usage is unknown."""

    no_usage: int = 0
    """Model calls that completed with output but no usage."""

    unpriced: int = 0
    """Model calls that completed with usage but no cost (`total_cost` is `None`)."""


class MemberLedger(BaseModel):
    """One member's entry in the swarm's ledger."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    name: str
    """The member's name."""

    status: str | None
    """How the member ended (`submitted`, `swarm_cap`, ...), or `None` for a member never started."""

    bridged: bool
    """The member runs a CLI agent through Inspect's agent bridge."""

    meter: Meter
    """Usage and cost of the calls made in the member's context."""

    calls: Calls
    """Model events in the member's spans."""

    cancelled: bool
    """The member's agent was cancelled, or the swarm's hard stop cancelled its background drain after the agent had returned. A hard stop during the drain keeps the agent's status, so `cancelled` can be `True` with `status="submitted"`. The agent is cancelled by the hard stop after a grace period, a member time limit or a cancellation from outside the swarm. A bridged member is also cancelled at once when it reaches a cap, its share or one of its own limits, or makes a call after the swarm stopped it."""

    background_started: int = 0
    """Tasks the member's agent started with `background()`, such as `deepagent()` background subagents."""

    background_cancelled: int = 0
    """Of `background_started`, the tasks that were cancelled: still running when the member's drain ended, or cancelled by an error from the member's work or by the hard stop."""


class SwarmLedger(BaseModel):
    """Realized usage and cost of one run of the swarm, and how complete they are.

    The swarm writes its ledger once, when it ends, to the sample store as `inspect_swarm:ledger` and as an `InfoEvent` with source `inspect_swarm`. Read it from a sample with `swarm_ledger()`.

    The store entry's shape follows `version`. This inspect_swarm reads version 1 only.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)

    version: Literal[1] = 1
    """Format version of the ledger."""

    budget: Budget
    """The swarm's budget as configured."""

    caps: dict[str, float | None]
    """The swarm's caps, derived or explicit, keyed by `cost` (dollars), `tokens` and `time` (seconds). `None` for a kind with no cap."""

    sample_limits: dict[str, float | None]
    """The sample's limits when the swarm started, keyed by `token`, `cost`, `message`, `turn`, `working` and `time`. `None` for a limit not set."""

    members: list[MemberLedger]
    """Each member, in roster order."""

    runtime: Meter
    """Calls made by the swarm's runtime, such as the controller's."""

    final: Meter
    """Calls made by the final step, which chooses the answer."""

    swarm: Meter
    """Every call in the swarm's context: the members, the runtime, the final step and anything else."""

    calls: Calls
    """Model events in the swarm's span tree: the members, the runtime and the final step."""

    unpriced_calls_recorded: int
    """Calls the swarm meter recorded with usage but no cost."""

    sample_delta: ModelUsage
    """The sample's model usage over the swarm's lifetime, summed over its models."""

    unattributed: ModelUsage
    """What the sample used over the swarm's lifetime that the swarm meter did not record: tokens are `sample_delta` minus `swarm.usage`, and `total_cost` is `sample_delta`'s cost minus `swarm.cost`."""

    unpriced_models: list[str]
    """Models in `sample_delta` with tokens but no cost."""

    native_compaction_classes: list[str]
    """Loaded `ModelAPI` classes that override `compact()`, when the swarm started or ended. Native compaction emits no event, so the ledger cannot count its calls."""

    overshoot: dict[str, float]
    """How far usage at the end went past each cap, keyed by `cost`, `tokens` and `time`. Only kinds over their cap appear."""

    stop_reason: str | None
    """The reason the swarm stopped its members, from the first stop begun. `None` when no stop began."""

    ended_by: str | None
    """The sample limit (`sample_limit:<type>`) or operator interrupt (`operator`) that ended the swarm, even during its own stop. `unknown` when an outside cause may have ended it but cannot be named. `None` when nothing from outside ended it."""

    interrupted: bool
    """The swarm did not finish: an error, a limit or a cancellation ended it before or during its final step."""

    final_cancelled: bool
    """The final step was cancelled."""

    resumed: bool
    """The sample resumed from a checkpoint, so the swarm restarted its caps from zero."""

    total_complete: bool
    """No call is known to be missing from the swarm's usage and cost: no member, member background task or final step was cancelled, the swarm was not interrupted, every call completed with priced usage, every model in `sample_delta` is priced, and no loaded model class compacts natively."""

    attribution_complete: bool
    """`total_complete`, and the swarm meter recorded all the sample used over the swarm's lifetime: `unattributed` is zero, to within rounding."""


# What the ledger needs to know about one member when the swarm ends.
@dataclass(frozen=True)
class MemberAccount:
    name: str
    status: str | None
    bridged: bool
    lever: CostPair
    # None for a member never started
    span_id: str | None
    cancelled: bool
    background_started: int
    background_cancelled: int


# A model event as it last stood, with its live usage object.
@dataclass(frozen=True)
class CallRecord:
    span_id: str | None
    cache_read: bool
    errored: bool
    pending: bool
    # retries inside the call, by the provider's client
    retries: int
    usage: ModelUsage | None


# A tool event as it last stood.
@dataclass(frozen=True)
class ToolRecord:
    span_id: str | None
    function: str
    # as canonical JSON, so that equal arguments compare equal whatever their key order
    arguments: str


# An intermediate score event, from one task scorer in a call to score().
@dataclass(frozen=True)
class ScoreRecord:
    span_id: str | None
    # the scorer's name, as score() names it
    scorer: str | None
    value: Value
    # the runtime's clock (anyio.current_time()) when it was reported
    time: float


# A communication evidence event (`comm`, written by `_evidence.py`): its kind, the
# acting member and the channel; for `sent` and `delivered`, the record's id; for
# `sent`, the control that rejected the record, if one did, and otherwise its payload's
# UTF-8 bytes; for `read`, whether it showed a listing.
@dataclass(frozen=True)
class CommRecord:
    kind: str
    member: str
    channel: str
    record: str | None
    rejected: str | None
    payload_bytes: int
    listing: bool


# The spans, model events, tool events, intermediate score events and communication
# evidence the transcript reports during the swarm's interval, kept as they happen
# rather than read back from the history: a bounded transcript serializes an event at
# its last update, which can come before Inspect prices its usage, and can evict it.
# Tool, score and communication events are kept in the order they began.
class EventLog:
    def __init__(self) -> None:
        self.parents: dict[str, str | None] = {}
        self.agent_spans: set[str] = set()
        self.calls: dict[str, CallRecord] = {}
        self.tools: dict[str, ToolRecord] = {}
        self.scores: list[ScoreRecord] = []
        self.comms: list[CommRecord] = []

    def __call__(self, event: Event) -> None:
        if isinstance(event, SpanBeginEvent):
            self.parents[event.id] = event.parent_id
            if event.type == AGENT_SPAN_TYPE:
                self.agent_spans.add(event.id)
        elif isinstance(event, ModelEvent):
            # the transcript gives every event a uuid before it reports it
            assert event.uuid is not None
            self.calls[event.uuid] = CallRecord(
                span_id=event.span_id,
                cache_read=event.cache == "read",
                errored=event.error is not None,
                pending=bool(event.pending),
                retries=event.retries or 0,
                usage=event.output.usage,
            )
        elif isinstance(event, ToolEvent):
            assert event.uuid is not None
            self.tools[event.uuid] = ToolRecord(
                span_id=event.span_id,
                function=event.function,
                arguments=json.dumps(
                    event.arguments, sort_keys=True, separators=(",", ":")
                ),
            )
        elif isinstance(event, ScoreEvent) and event.intermediate:
            # score() reports its events from the scoring task, so the clock is there
            self.scores.append(
                ScoreRecord(
                    span_id=event.span_id,
                    scorer=event.scorer,
                    value=event.score.value,
                    time=anyio.current_time(),
                )
            )
        elif (
            isinstance(event, InfoEvent)
            and event.source == "inspect_swarm"
            and isinstance(event.data, dict)
            and event.data.get("type") == "comm"
        ):
            self.comms.append(_comm_record(event.data))


def _comm_record(data: dict[str, JsonValue]) -> CommRecord:
    kind = cast(str, data["kind"])
    record = data.get("record")
    rejected = data.get("rejected")
    payload_bytes = 0
    if kind == "sent":
        # the full record, which its id stands for; only an accepted payload is
        # measured, since a rejected one can be text no UTF-8 encoder takes
        assert isinstance(record, dict)
        if isinstance(rejected, dict):
            rejected = rejected["control"]
        else:
            payload_bytes = utf8_bytes(cast(str, record["payload"]))
        record = record["id"]
    return CommRecord(
        kind=kind,
        member=cast(str, data["member"]),
        channel=cast(str, data["channel"]),
        record=cast(str | None, record),
        rejected=cast(str | None, rejected),
        payload_bytes=payload_bytes,
        listing=data.get("listing") is True,
    )


# The ledger's view of the swarm's interval: what it reads when the swarm starts, and
# the ledger it builds from that and the runtime's state when the swarm ends.
class LedgerInterval:
    def __init__(self) -> None:
        self._events = EventLog()
        self._unsubscribe = transcript()._subscribe(  # pyright: ignore[reportPrivateUsage]
            self._events
        )
        self._span_id = current_span_id()
        self._start_usage = _usage_snapshot()
        self._start_time = time.monotonic()
        self._classes = native_compaction_classes()
        limits = sample_limits()
        self._sample_limits: dict[str, float | None] = {
            "token": limits.token.limit,
            "cost": limits.cost.limit,
            "message": limits.message.limit,
            "turn": limits.turn.limit,
            "working": limits.working.limit,
            "time": limits.time.limit,
        }
        self._resumed = sample_resumed()

    @property
    def events(self) -> EventLog:
        return self._events

    # where the spans of the interval lie, given the spans of the members that entered one
    def places(self, member_spans: Mapping[str, str]) -> "SpanPlaces":
        return SpanPlaces(self._events.parents, self._span_id, member_spans)

    def ledger(
        self,
        *,
        budget: Budget,
        caps: Sequence[Limit],
        time_cap: float | None,
        members: Sequence[MemberAccount],
        runtime: CostPair,
        final: CostPair,
        swarm: CostPair,
        stop_reason: str | None,
        ended_by: str | None,
        interrupted: bool,
        final_cancelled: bool,
    ) -> SwarmLedger:
        # synchronous, so that it runs under cancellation too
        self._unsubscribe()
        calls, member_calls = count_calls(
            self._events,
            self.places({m.name: m.span_id for m in members if m.span_id is not None}),
        )
        delta, unpriced_models, cost_scale = _usage_delta(
            self._start_usage, _usage_snapshot()
        )
        swarm_meter = _meter(swarm)
        unattributed = _minus(delta, swarm_meter.usage)
        unattributed.total_cost = (delta.total_cost or 0.0) - swarm_meter.cost
        tolerance = COST_TOLERANCE * cost_scale
        if any((getattr(unattributed, f) or 0) < 0 for f in _TOKEN_FIELDS) or (
            unattributed.total_cost < -tolerance
        ):
            # the meters hold more than the sample recorded: a call counted twice
            logger.error(
                f"The swarm's ledger does not reconcile with the sample's usage: the swarm meter recorded more than the sample did over the swarm's lifetime. Unattributed: {unattributed.model_dump(exclude_none=True)}."
            )
        classes = sorted(self._classes | native_compaction_classes())
        member_ledgers = [
            MemberLedger(
                name=m.name,
                status=m.status,
                bridged=m.bridged,
                meter=_meter(m.lever),
                calls=member_calls.get(m.name, Calls()),
                cancelled=m.cancelled,
                background_started=m.background_started,
                background_cancelled=m.background_cancelled,
            )
            for m in members
        ]
        total_complete = (
            # a cancelled agent loop, background ones included, may have had work in
            # flight that left no trace
            not any(m.cancelled or m.background_cancelled for m in members)
            and not interrupted
            and not final_cancelled
            and calls.errored == 0
            and calls.no_usage == 0
            and calls.unpriced == 0
            and swarm.token_node.unpriced_calls == 0
            and not unpriced_models
            and not classes
        )
        attribution_complete = (
            total_complete
            and all((getattr(unattributed, f) or 0) == 0 for f in _TOKEN_FIELDS)
            and abs(unattributed.total_cost) <= tolerance
        )
        cap_values = _cap_values(caps, time_cap)
        return SwarmLedger(
            budget=budget,
            caps=cap_values,
            sample_limits=self._sample_limits,
            members=member_ledgers,
            runtime=_meter(runtime),
            final=_meter(final),
            swarm=swarm_meter,
            calls=calls,
            unpriced_calls_recorded=swarm.token_node.unpriced_calls,
            sample_delta=delta,
            unattributed=unattributed,
            unpriced_models=unpriced_models,
            native_compaction_classes=classes,
            overshoot=_overshoot(caps, time_cap, time.monotonic() - self._start_time),
            stop_reason=stop_reason,
            ended_by=ended_by,
            interrupted=interrupted,
            final_cancelled=final_cancelled,
            resumed=self._resumed,
            total_complete=total_complete,
            attribution_complete=attribution_complete,
        )


# Where each span lies: whether it is in the swarm's tree, and the member whose spans
# hold it. With no swarm span (the swarm called outside any span), every span is in it.
class SpanPlaces:
    def __init__(
        self,
        parents: Mapping[str, str | None],
        swarm_span: str | None,
        member_spans: Mapping[str, str],
    ) -> None:
        self._parents = parents
        self._swarm_span = swarm_span
        self._owners = {span: name for name, span in member_spans.items()}
        self._places: dict[str | None, tuple[bool, str | None]] = {}

    def __call__(self, span: str | None) -> tuple[bool, str | None]:
        if span not in self._places:
            if span is None or span == self._swarm_span:
                found = (self._swarm_span is None or span == self._swarm_span, None)
            elif span in self._owners:
                found = (self(self._parents.get(span))[0], self._owners[span])
            elif span in self._parents:
                found = self(self._parents[span])
            else:
                # begun before the swarm started, and not the swarm's span
                found = (self._swarm_span is None, None)
            self._places[span] = found
        return self._places[span]


# Counts the model events in the swarm's span tree, and those in each member's spans.
def count_calls(events: EventLog, places: SpanPlaces) -> tuple[Calls, dict[str, Calls]]:
    total = Calls()
    per_member: dict[str, Calls] = {}
    for call in events.calls.values():
        inside, member = places(call.span_id)
        if inside:
            _count(total, call)
        if member is not None:
            _count(per_member.setdefault(member, Calls()), call)
    return total, per_member


# Counts one model event, as it last stood.
def _count(calls: Calls, call: CallRecord) -> None:
    if call.cache_read:
        calls.cache_replays += 1
        return
    # each retry follows a failed request whose usage the event does not keep
    calls.errored += call.retries
    if call.errored or call.pending:
        # an event still pending once the swarm has drained never completed
        calls.errored += 1
    else:
        calls.calls += 1
        if call.usage is None:
            calls.no_usage += 1
        elif call.usage.total_cost is None:
            # read when the swarm ends: Inspect prices a call's usage in place,
            # after its event's last update when the call reaches a limit
            calls.unpriced += 1


# Every loaded ModelAPI class that overrides compact(), built-in or custom. Native
# compaction emits no event, so only the capability can be checked.
def native_compaction_classes() -> set[str]:
    found: set[str] = set()
    seen: set[type[ModelAPI]] = set()
    pending = list(ModelAPI.__subclasses__())
    while pending:
        cls = pending.pop()
        if cls in seen:
            continue
        seen.add(cls)
        if cls.compact is not ModelAPI.compact:
            found.add(f"{cls.__module__}.{cls.__qualname__}")
        pending.extend(cls.__subclasses__())
    return found


def sample_resumed() -> bool:
    # inspect_ai keeps the resume signal on the sample's checkpointer setup only
    active = sample_active()
    if active is None:
        return False
    resume = getattr(active.checkpointer, "_resume_checkpoint", None)
    return resume is not None and resume.attempt != "initial"


def _meter(pair: CostPair) -> Meter:
    return Meter(
        usage=pair.token_node.model_usage.model_copy(),
        cost=pair.cost,
        suspended_cost=pair.suspended_cost,
    )


def _usage_snapshot() -> dict[str, ModelUsage]:
    return {model: usage.model_copy() for model, usage in sample_model_usage().items()}


def _usage_delta(
    start: Mapping[str, ModelUsage], end: Mapping[str, ModelUsage]
) -> tuple[ModelUsage, list[str], float]:
    # the sample's usage over the interval, the models in it with tokens but no cost, and
    # the cumulative cost of the priced ones at the end
    total = ModelUsage()
    cost: float | None = None
    scale = 0.0
    unpriced: list[str] = []
    for model, usage in end.items():
        before = start.get(model, ModelUsage())
        delta = _minus(usage, before)
        # a provider may report component counts with a total of zero
        tokens = any(getattr(delta, f) for f in _TOKEN_FIELDS)
        if not tokens and usage.total_cost == before.total_cost:
            continue
        total = _plus(total, delta)
        if usage.total_cost is None:
            unpriced.append(model)
        else:
            cost = (cost or 0.0) + (usage.total_cost - (before.total_cost or 0.0))
            scale += abs(usage.total_cost)
    total.total_cost = cost
    return total, unpriced, scale


def _minus(a: ModelUsage, b: ModelUsage) -> ModelUsage:
    return _combine(a, b, -1)


def _plus(a: ModelUsage, b: ModelUsage) -> ModelUsage:
    return _combine(a, b, 1)


def _combine(a: ModelUsage, b: ModelUsage, sign: int) -> ModelUsage:
    # token fields only; an optional field stays None where neither has it
    def optional(x: int | None, y: int | None) -> int | None:
        return None if x is None and y is None else (x or 0) + sign * (y or 0)

    return ModelUsage(
        input_tokens=a.input_tokens + sign * b.input_tokens,
        output_tokens=a.output_tokens + sign * b.output_tokens,
        total_tokens=a.total_tokens + sign * b.total_tokens,
        input_tokens_cache_write=optional(
            a.input_tokens_cache_write, b.input_tokens_cache_write
        ),
        input_tokens_cache_read=optional(
            a.input_tokens_cache_read, b.input_tokens_cache_read
        ),
        reasoning_tokens=optional(a.reasoning_tokens, b.reasoning_tokens),
    )


def _cap_values(
    caps: Sequence[Limit], time_cap: float | None
) -> dict[str, float | None]:
    cost = cost_node(caps, "cap")
    tokens = token_node(caps, "cap")
    return {
        "cost": cost.limit if cost is not None else None,
        "tokens": tokens.limit if tokens is not None else None,
        "time": time_cap,
    }


def _overshoot(
    caps: Sequence[Limit], time_cap: float | None, elapsed: float
) -> dict[str, float]:
    usage: dict[str, tuple[float, float | None]] = {"time": (elapsed, time_cap)}
    cost = cost_node(caps, "cap")
    if cost is not None:
        usage["cost"] = (cost.usage, cost.limit)
    tokens = token_node(caps, "cap")
    if tokens is not None:
        usage["tokens"] = (tokens.usage, tokens.limit)
    return {
        kind: value - cap
        for kind, (value, cap) in usage.items()
        if cap is not None and value > cap
    }
