import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from inspect_ai.event import Event, ModelEvent, SpanBeginEvent
from inspect_ai.log._samples import sample_active
from inspect_ai.log._transcript import transcript
from inspect_ai.model import ModelAPI, ModelUsage
from inspect_ai.model._model import sample_model_usage
from inspect_ai.util import Limit, current_span_id, sample_limits
from pydantic import BaseModel

from ._budget import Budget, CostPair, SwarmCostNode, SwarmTokenNode

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


# A cost pair's figures: its token-tree node's usage (every non-suspended call recorded
# in its context) and its accumulated cost (priced calls, suspended ones included).
class Meter(BaseModel):
    usage: ModelUsage
    cost: float
    # cost recorded while token metering was suspended (approval and review calls)
    suspended_cost: float


# Counts of the model events in an interval.
class Calls(BaseModel):
    # completed, non-cache model calls
    calls: int = 0
    # events with cache="read": charged zero
    cache_replays: int = 0
    # attempts that ended in an error, cancellation included, and the requests a
    # provider's client retried inside one call: usage unknown
    errored: int = 0
    # completed with output but no usage
    no_usage: int = 0
    # completed with usage whose total_cost is None
    unpriced: int = 0

    def count(self, call: "CallRecord") -> None:
        if call.cache_read:
            self.cache_replays += 1
            return
        # each retry follows a failed request whose usage the event does not keep
        self.errored += call.retries
        if call.errored or call.pending:
            # an event still pending once the swarm has drained never completed
            self.errored += 1
        else:
            self.calls += 1
            if call.usage is None:
                self.no_usage += 1
            elif call.usage.total_cost is None:
                # read when the swarm ends: Inspect prices a call's usage in place,
                # after its event's last update when the call reaches a limit
                self.unpriced += 1


class MemberLedger(BaseModel):
    name: str
    # the member's status, or None for a member never started
    status: str | None
    bridged: bool
    # the member's lever
    meter: Meter
    # events in the member's spans
    calls: Calls
    # the member's scope was cancelled (hard stop, time limit, bridged branch)
    cancelled: bool


# Realized usage and cost of one run of the swarm, and how complete they are. Written
# once, when the swarm ends, as an InfoEvent and a store entry.
class SwarmLedger(BaseModel):
    version: int = 1
    # as configured
    budget: Budget
    # derived or explicit, per kind
    caps: dict[str, float | None]
    # at swarm start
    sample_limits: dict[str, float | None]
    members: list[MemberLedger]
    # the runtime lever: controller and in-run verifier calls
    runtime: Meter
    # the final meter
    final: Meter
    # members, runtime, final step, anything else in the swarm's context
    swarm: Meter
    # every event in the swarm's span tree: members, runtime, final step
    calls: Calls
    # per-call count from the swarm meter's token-tree record()
    unpriced_calls_recorded: int
    # the sample's model usage over the swarm's lifetime
    sample_delta: ModelUsage
    # tokens: sample_delta minus swarm.usage; cost: sample_delta's cost minus swarm.cost
    unattributed: ModelUsage
    # models in sample_delta with tokens but no cost
    unpriced_models: list[str]
    # loaded ModelAPI classes that override compact(), at start or end
    native_compaction_classes: list[str]
    # per capped kind: usage at the end minus the cap, if positive
    overshoot: dict[str, float]
    # the reason of the swarm's own stop, the first one begun; None when none began
    stop_reason: str | None
    # the sample limit (sample_limit:<type>) or operator interrupt that ended the swarm,
    # even during its stop; "unknown" when an outside cause may have ended it but
    # cannot be attributed; None when nothing from outside ended it
    ended_by: str | None
    # a foreign error or outside cancellation ended the swarm
    interrupted: bool
    # the final step was cancelled
    final_cancelled: bool
    # the sample resumed from a checkpoint
    resumed: bool
    total_complete: bool
    attribution_complete: bool


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


# The spans and model events the transcript reports during the swarm's interval, kept
# as they happen rather than read back from the history: a bounded transcript serializes
# an event at its last update, which can come before Inspect prices its usage.
class EventLog:
    def __init__(self) -> None:
        self.parents: dict[str, str | None] = {}
        self.calls: dict[str, CallRecord] = {}

    def __call__(self, event: Event) -> None:
        if isinstance(event, SpanBeginEvent):
            self.parents[event.id] = event.parent_id
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
            self._span_id,
            {m.name: m.span_id for m in members if m.span_id is not None},
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
            )
            for m in members
        ]
        total_complete = (
            not any(m.cancelled for m in members)
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


# Counts the model events in the swarm's span tree, and those in each member's spans.
# With no swarm span (the swarm called outside any span), every event counts.
def count_calls(
    events: EventLog, swarm_span: str | None, member_spans: Mapping[str, str]
) -> tuple[Calls, dict[str, Calls]]:
    owners = {span: name for name, span in member_spans.items()}
    parents = events.parents
    places: dict[str | None, tuple[bool, str | None]] = {}

    def place(span: str | None) -> tuple[bool, str | None]:
        # whether a span is in the swarm's tree, and the member whose spans hold it
        if span not in places:
            if span is None or span == swarm_span:
                found = (swarm_span is None or span == swarm_span, None)
            elif span in owners:
                found = (place(parents.get(span))[0], owners[span])
            elif span in parents:
                found = place(parents[span])
            else:
                # begun before the swarm started, and not the swarm's span
                found = (swarm_span is None, None)
            places[span] = found
        return places[span]

    total = Calls()
    per_member = {name: Calls() for name in member_spans}
    for call in events.calls.values():
        inside, member = place(call.span_id)
        if inside:
            total.count(call)
        if member is not None:
            per_member[member].count(call)
    return total, per_member


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
    cost = _cost_cap(caps)
    tokens = _token_cap(caps)
    return {
        "cost": cost.limit if cost is not None else None,
        "tokens": tokens.limit if tokens is not None else None,
        "time": time_cap,
    }


def _overshoot(
    caps: Sequence[Limit], time_cap: float | None, elapsed: float
) -> dict[str, float]:
    usage: dict[str, tuple[float, float | None]] = {"time": (elapsed, time_cap)}
    cost = _cost_cap(caps)
    if cost is not None:
        usage["cost"] = (cost.usage, cost.limit)
    tokens = _token_cap(caps)
    if tokens is not None:
        usage["tokens"] = (tokens.usage, tokens.limit)
    return {
        kind: value - cap
        for kind, (value, cap) in usage.items()
        if cap is not None and value > cap
    }


def _cost_cap(caps: Sequence[Limit]) -> SwarmCostNode | None:
    return next(
        (n for n in caps if isinstance(n, SwarmCostNode) and n.role == "cap"), None
    )


def _token_cap(caps: Sequence[Limit]) -> SwarmTokenNode | None:
    # a lone token node; the cost cap pair's token node is not it
    return next(
        (
            n
            for n in caps
            if isinstance(n, SwarmTokenNode) and n.role == "cap" and n.pair is None
        ),
        None,
    )
