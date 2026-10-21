import logging
import math
from collections.abc import Callable, Mapping, Sequence, Set
from contextvars import ContextVar
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Literal

import anyio
from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.registry import is_registry_object, registry_info
from inspect_ai.agent import Agent, AgentChannel
from inspect_ai.agent._bridge.util import in_bridge_model_generate
from inspect_ai.model import Model, ModelUsage
from inspect_ai.model._model_info import (
    _get_model_info_direct,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util import Limit, LimitExceededError, sample_limits
from inspect_ai.util._limit import (
    _CostLimit,  # pyright: ignore[reportPrivateUsage]
    _MessageLimit,  # pyright: ignore[reportPrivateUsage]
    _TokenLimit,  # pyright: ignore[reportPrivateUsage]
    _TurnLimit,  # pyright: ignore[reportPrivateUsage]
    token_limit_fields,
    token_limit_tree,
)
from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)


class Budget(BaseModel):
    """The swarm-wide budget.

    By default the swarm derives its caps from the sample's cost, token and time limits when it starts, holding back `reserve` of each. The caps are stopping rules: when one is reached the swarm stops its members, gives them up to `grace` seconds to finish the calls they have in flight, and then cancels the members still running. If the sample has a time limit, the grace period is capped at half its remaining time.

    Every field is a plain value, so each can be a task parameter, and two budgets that differ in any field are logged as different arguments.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", use_attribute_docstrings=True
    )

    cost: float | None = None
    """Swarm cost cap in dollars. `None` derives it from the sample's cost limit."""

    tokens: int | str | None = None
    """Swarm token cap. A string such as `"output:1m"` is parsed by `parse_token_limit()` and keeps its metering type. `None` derives it from the sample's token limit, with that limit's metering type."""

    time: float | None = None
    """Seconds after which the swarm stops its members. `None` derives it from the sample's time limit."""

    reserve: float = 0.05
    """Fraction of each sample limit held back when a cap is derived."""

    split: Literal["pool", "equal"] | tuple[float, ...] = "pool"
    """How the cost and token caps are divided between members: one shared pool, equal shares, or one weight per member in roster order. A member that uses up its share stops, and its unused share is not given to the others."""

    grace: float = 120.0
    """Seconds members have, at most, to finish calls already in flight after a stop. If the sample has a time limit, this period is capped at half its remaining time."""

    @model_validator(mode="after")
    def _check_fields(self) -> "Budget":
        for name, value in (("cost", self.cost), ("time", self.time)):
            if value is not None and not (math.isfinite(value) and value > 0):
                raise ValueError(
                    f"Budget.{name} must be None or a finite number greater than zero, got {value}."
                )
        if self.tokens is not None:
            try:
                tokens, _ = _token_cap(self.tokens)
            except ValueError as ex:
                raise ValueError(
                    f"Budget.tokens is not a valid token limit: {ex}"
                ) from ex
            if tokens <= 0:
                raise ValueError(
                    f"Budget.tokens must be greater than zero, got {self.tokens!r}."
                )
        if not (math.isfinite(self.reserve) and 0 <= self.reserve < 1):
            raise ValueError(
                f"Budget.reserve must be at least 0 and less than 1, got {self.reserve}."
            )
        if not (math.isfinite(self.grace) and self.grace >= 0):
            raise ValueError(
                f"Budget.grace must be a finite number of seconds, at least 0, got {self.grace}."
            )
        if isinstance(self.split, tuple):
            if not self.split:
                raise ValueError("Budget.split must not be an empty tuple of weights.")
            if not all(math.isfinite(w) and w > 0 for w in self.split):
                raise ValueError(
                    f"Budget.split weights must be finite numbers greater than zero, got {self.split}."
                )
        return self


def _token_cap(tokens: int | str) -> tuple[int, str]:
    # a token cap and its metering type, through Inspect's own parser
    fields = token_limit_fields(tokens)
    assert fields.tokens is not None
    return fields.tokens, fields.type or "all"


# The swarm's caps, derived or explicit, fixed at swarm start.
@dataclass(frozen=True)
class Caps:
    cost: float | None
    tokens: int | None
    token_type: str
    time: float | None


def derive_caps(budget: Budget, member_count: int) -> Caps:
    # run at swarm start, inside the sample
    if isinstance(budget.split, tuple) and len(budget.split) != member_count:
        raise ValueError(
            f"Budget.split has {len(budget.split)} weights but the swarm has {member_count} members."
        )
    limits = sample_limits()
    if limits.working.limit is not None:
        logger.warning(
            "The sample has a working limit, which is unreliable under a swarm: members waiting on each other's model connections barely advance it. Use a time limit to bound the swarm."
        )

    sample_tokens = limits.token
    sample_token_type = (
        sample_tokens.type if isinstance(sample_tokens, _TokenLimit) else "all"
    )
    tokens: int | None
    if budget.tokens is not None:
        tokens, token_type = _token_cap(budget.tokens)
        # an explicit cap is compared with the sample's only when both meter the same tokens
        if token_type == sample_token_type:
            _check_explicit("token", tokens, sample_tokens)
    else:
        token_type = sample_token_type
        derived = _derived("token", sample_tokens, budget.reserve)
        tokens = None if derived is None else math.floor(derived)
        if tokens is not None and tokens <= 0:
            raise _not_positive("token", sample_tokens, tokens)

    return Caps(
        cost=_cap("cost", budget.cost, limits.cost, budget.reserve),
        tokens=tokens,
        token_type=token_type,
        time=_cap("time", budget.time, limits.time, budget.reserve),
    )


def _cap(
    kind: str, explicit: float | None, sample: Limit, reserve: float
) -> float | None:
    if explicit is not None:
        _check_explicit(kind, explicit, sample)
        return explicit
    return _derived(kind, sample, reserve)


def _derived(kind: str, sample: Limit, reserve: float) -> float | None:
    if sample.limit is None:
        return None
    cap = sample.limit * (1 - reserve) - sample.usage
    if cap <= 0:
        raise _not_positive(kind, sample, cap)
    return cap


def _not_positive(kind: str, sample: Limit, cap: float) -> ValueError:
    return ValueError(
        f"The swarm's {kind} cap, derived from the sample's {kind} limit of {sample.limit:,}, would be {cap:,}: the sample has already used {sample.usage:,}. Raise the sample's {kind} limit or lower Budget.reserve."
    )


def _check_explicit(kind: str, cap: float, sample: Limit) -> None:
    if sample.limit is None:
        return
    remaining = sample.limit - sample.usage
    if cap > remaining:
        logger.warning(
            f"The swarm's {kind} cap ({cap:,}) is above the sample's remaining {kind} limit ({remaining:,}). The sample's limit will end the swarm first, without a final step."
        )


def share_nodes(
    budget: Budget, caps: Caps, names: Sequence[str]
) -> dict[str, list[Limit]]:
    # each member's share of the cost and token caps, as the nodes its wrapper enters
    # below its lever; a pool has none. Run at swarm start, after derive_caps().
    if budget.split == "pool":
        return {}
    if caps.cost is None and caps.tokens is None:
        raise ValueError(
            f"Budget.split={budget.split!r} divides the swarm's cost and token caps, but the swarm has neither. Set a cost or token limit on the sample or in the Budget, or use split='pool'."
        )
    fractions = _fractions(budget.split, len(names))
    costs = None if caps.cost is None else _cost_shares(caps.cost, fractions)
    tokens = None if caps.tokens is None else _token_shares(caps.tokens, fractions)
    shares: dict[str, list[Limit]] = {}
    for i, name in enumerate(names):
        nodes: list[Limit] = []
        if costs is not None:
            nodes.extend(CostPair(costs[i], owner=name, role="share").nodes)
        if tokens is not None:
            nodes.append(
                SwarmTokenNode(tokens[i], caps.token_type, owner=name, role="share")
            )
        shares[name] = nodes
    return shares


def _fractions(
    split: Literal["equal"] | tuple[float, ...], count: int
) -> list[Fraction]:
    # each member's fraction of the caps, exact: large weights neither overflow nor
    # round a share away
    weights = [1.0] * count if split == "equal" else list(split)
    assert len(weights) == count
    total = sum(Fraction(w) for w in weights)
    return [Fraction(w) / total for w in weights]


def split_sentence(budget: Budget, caps: Caps, index: int, count: int) -> str | None:
    # the preamble's statement of the split, for the member at `index` in roster order;
    # None when the swarm has one member or no cap. Run at swarm start, after
    # share_nodes().
    if count == 1 or (caps.cost is None and caps.tokens is None and caps.time is None):
        return None
    if budget.split == "pool":
        return "The budget is shared by all members."
    kinds = " and ".join(
        kind
        for kind, cap in (("cost", caps.cost), ("token", caps.tokens))
        if cap is not None
    )
    if budget.split == "equal":
        return f"The swarm's {kinds} budget is split equally between the members; you stop when your share is used up."
    share = float(_fractions(budget.split, count)[index]) * 100
    return f"The swarm's {kinds} budget is split between the members in fixed shares; yours is {share:.3g}% of it, and you stop when it is used up."


def _cost_shares(cap: float, fractions: Sequence[Fraction]) -> list[float]:
    shares = [float(Fraction(cap) * f) for f in fractions]
    if 0 in shares:
        raise ValueError(
            f"Budget.split gives a member a cost share of zero: its weight is too small a fraction of the total to divide the swarm's cost cap of ${cap:,}. Use weights closer together."
        )
    return shares


def _token_shares(cap: int, fractions: Sequence[Fraction]) -> list[int]:
    # whole shares that sum to the cap: each member's floor, then one token at a time to
    # the largest fractional parts, ties by roster order
    exact = [cap * f for f in fractions]
    shares = [math.floor(x) for x in exact]
    by_fraction = sorted(range(len(exact)), key=lambda i: (shares[i] - exact[i], i))
    for i in by_fraction[: cap - sum(shares)]:
        shares[i] += 1
    if 0 in shares:
        raise ValueError(
            f"The swarm's token cap of {cap:,} is too small to split between its {len(fractions)} members: a member's share would be zero tokens. Raise the token cap, or use split='pool'."
        )
    return shares


def check_cost_data(
    models: Sequence[str | Model], *, built: Set[str] = frozenset()
) -> None:
    # run at swarm start when a cost cap applies, as Inspect does for the task's models.
    # The lookup is the one metering uses at generation time, cached by the model's
    # string, so the check and the price charged agree. Two configurations that share
    # a model string share that cached price, as they do under Inspect's cost_limit.
    # `built` names the models of built members, known only by their logged name.
    missing = list(
        dict.fromkeys(
            str(model)
            for model in models
            if (info := _get_model_info_direct(model)) is None or info.cost is None
        )
    )
    if missing:
        remedy = "Use set_model_cost() or --model-cost-config to configure pricing."
        if any(name in built for name in missing):
            remedy += " A built member's model is looked up by its name alone; to look it up as configured, use the member's factory form, member(factory, args={'model': ...})."
        raise PrerequisiteError(
            f"A swarm cost cap requires cost data for every member's model. Missing cost data for: {', '.join(missing)}. {remedy}"
        )


def is_bridged(agent: Agent, bridged: bool | None) -> bool:
    # an explicit declaration wins; otherwise inspect_swe's agents are bridged
    if bridged is not None:
        return bridged
    return is_registry_object(agent) and registry_info(agent).name.startswith(
        "inspect_swe/"
    )


# What a node, and the bus and its tools, need to know about the member they act for.
# The member wrapper sets `current_member`, runs the member inside `scope` and records
# its span in `span_id`.
@dataclass(eq=False)
class MemberContext:
    name: str
    bridged: bool
    # the member's configured delivery mode; None for the default
    delivery: Literal["notify", "poll"] | None = None
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    stop_cause: str | None = None
    span_id: str | None = None
    # counted as native, but found bridged at its first marked bridge generation
    bridged_late: bool = False
    # the agent channel of the member's own loop, bound by swarm_tools() where it is
    # first resolved, and whether ACP was bound to that channel then
    loop: AgentChannel | None = None
    acp_bound: bool | None = None

    @property
    def delivery_effective(self) -> Literal["notify", "poll"]:
        # a bridged member has no react() hook to give notices; no M2 channel has a
        # mode of its own, so the default is notify. A native member found bridged
        # late (a bridged descendant) keeps its own loop's hook, and its mode.
        if self.bridged and not self.bridged_late:
            return "poll"
        return self.delivery or "notify"


current_member: ContextVar[MemberContext | None] = ContextVar(
    "inspect_swarm_current_member", default=None
)

NodeRole = Literal["meter", "cap", "share", "limit", "lever"]

_LimitType = Literal["token", "cost", "turn", "message", "custom"]


class SwarmNode(Limit):
    # the member that owns this node, or None for the swarm's shared nodes
    owner: str | None
    role: NodeRole
    # set by the runtime on the swarm's caps
    stop_swarm: Callable[[str], None] | None = None

    def detect_bridge(self) -> None:
        # every check, met or not, so a member found bridged by the bridge's mark stays
        # bridged for later checks that run before the mark (compaction, filters)
        member = current_member.get()
        if member is not None and not member.bridged and in_bridge_model_generate():
            member.bridged = True
            member.bridged_late = True

    def exceeded(
        self,
        type: _LimitType,
        kind: str,
        value: float,
        limit: float,
        values: str,
        reason: str | None = None,
    ) -> None:
        self.detect_bridge()
        if self.stop_swarm is not None:
            # here rather than where the error is caught: a tool or handoff may turn
            # it into a message, and its caller go on or return normally
            self.stop_swarm("swarm_cap")
        member = current_member.get()
        if member is not None and member.bridged:
            # a raise here would reach the sandbox service and end the sample
            if member.stop_cause is None:
                member.stop_cause = self.stop_cause(type)
            member.scope.cancel()
            return
        raise LimitExceededError(
            type,
            value=value,
            limit=limit,
            message=self._message(kind, values, reason),
            source=self,
        )

    def stop_cause(self, type: str) -> str:
        # the member status this node gives when it stops a member
        match self.role:
            case "cap" | "meter":
                return "swarm_cap"
            case "share":
                return "share"
            case "lever":
                return "stopped"
            case "limit":
                return f"member_limit:{type}"

    def _message(self, kind: str, values: str, reason: str | None) -> str:
        match self.role:
            case "lever":
                return f"Stopped by the swarm: {reason}"
            case "cap" | "meter":
                return f"Swarm {kind} cap reached. {values}"
            case "share" | "limit":
                return (
                    f"Swarm member '{self.owner}' {kind} {self.role} reached. {values}"
                )


class SwarmTokenNode(SwarmNode, _TokenLimit):
    def __init__(
        self,
        limit: int | None,
        type: str = "all",
        *,
        owner: str | None,
        role: NodeRole,
        pair: "CostPair | None" = None,
    ) -> None:
        super().__init__(limit, type)
        self.owner = owner
        self.role = role
        self.pair = pair
        self.unpriced_calls = 0

    @property
    def model_usage(self) -> ModelUsage:
        return self._usage

    def record(self, usage: ModelUsage) -> None:
        # runs for every call recorded in this context, before any token check
        super().record(usage)
        if usage.total_cost is None:
            self.unpriced_calls += 1
        elif self.pair is not None:
            self.pair.cost += usage.total_cost

    def _check_self(self, raise_for_equal: bool = False) -> None:
        self.detect_bridge()
        limit = self.limit
        if limit is not None:
            total = self._metering.value(self._usage)
            if total > limit or (raise_for_equal and total == limit):
                kind = "token" if self.type == "all" else f"token ({self.type})"
                self.exceeded(
                    "custom" if self.role == "lever" else "token",
                    kind,
                    total,
                    limit,
                    f"value: {total:,}; limit: {limit:,}",
                    self.pair.reason if self.pair is not None else None,
                )
                return
        if self.pair is not None:
            _check_pair_cost(self.pair, self, raise_for_equal)


class SwarmCostNode(SwarmNode, _CostLimit):
    def __init__(
        self,
        limit: float | None,
        *,
        owner: str | None,
        role: NodeRole,
        pair: "CostPair",
    ) -> None:
        super().__init__(limit)
        self.owner = owner
        self.role = role
        self.pair = pair

    @property
    def usage(self) -> float:
        return self.pair.cost

    def record(self, cost: float) -> None:
        # outside a token suspension the pair's token-tree node has already counted the call
        if self.parent is not None:
            self.parent.record(cost)
        if token_limit_tree.is_suspended():
            self.pair.cost += cost
            self.pair.suspended_cost += cost

    def _check_self(self, raise_for_equal: bool = False) -> None:
        self.detect_bridge()
        if self.pair.reason is not None:
            # the stop gate: refuses every call, including suspended ones
            cost = self.pair.cost
            self.exceeded("custom", "cost", cost, cost, "", self.pair.reason)
            return
        _check_pair_cost(self.pair, self, raise_for_equal)


def _check_pair_cost(pair: "CostPair", node: SwarmNode, raise_for_equal: bool) -> None:
    # the pair's cost check, run by whichever of its two nodes is being checked
    cost = pair.cost
    limit = pair.cost_node.limit
    if limit is not None and (cost > limit or (raise_for_equal and cost == limit)):
        node.exceeded(
            "cost", "cost", cost, limit, f"value: ${cost:,.4f}; limit: ${limit:,.4f}"
        )


# A token-tree node and a cost-tree node sharing one cost accumulator. The token-tree
# node counts every call recorded outside a token suspension, so an Inspect token node
# below it cannot hide a call's cost; the cost-tree node counts the calls made while
# token metering is suspended (approval and review).
class CostPair:
    def __init__(
        self, limit: float | None = None, *, owner: str | None, role: NodeRole
    ) -> None:
        self.cost = 0.0
        self.suspended_cost = 0.0
        self.reason: str | None = None
        self.token_node = SwarmTokenNode(None, owner=owner, role=role, pair=self)
        self.cost_node = SwarmCostNode(limit, owner=owner, role=role, pair=self)

    @property
    def nodes(self) -> list[Limit]:
        return [self.token_node, self.cost_node]


# A member's (or the runtime's) meter, with no limit until the runtime stops it.
class Lever(CostPair):
    def __init__(self, *, owner: str | None) -> None:
        super().__init__(owner=owner, role="lever")

    def close(self, reason: str) -> None:
        # refuses ordinary calls at their next check, and suspended ones through the gate
        if self.reason is None:
            self.reason = reason
            self.token_node.limit = int(self.token_node.usage)


class SwarmTurnNode(SwarmNode, _TurnLimit):
    def __init__(self, limit: int | None, *, owner: str) -> None:
        super().__init__(limit)
        self.owner = owner
        self.role = "limit"

    def _check_self(self) -> None:
        self.detect_bridge()
        limit = self.limit
        if limit is not None and self._turns > limit:
            self.exceeded(
                "turn",
                "turn",
                self._turns,
                limit,
                f"value: {self._turns:,}; limit: {limit:,}",
            )


class SwarmMessageNode(SwarmNode, _MessageLimit):
    def __init__(self, limit: int | None, *, owner: str) -> None:
        super().__init__(limit)
        self.owner = owner
        self.role = "limit"

    def check(self, count: int, raise_for_equal: bool) -> None:
        # a message node has no ancestors to check
        self.detect_bridge()
        limit = self.limit
        if limit is not None and (
            count > limit or (raise_for_equal and count == limit)
        ):
            self.exceeded(
                "message",
                "message",
                count,
                limit,
                f"count: {count:,}; limit: {limit:,}",
            )


def cost_node(nodes: Sequence[Limit], role: NodeRole) -> SwarmCostNode | None:
    # the cost-tree half of the pair with this role, whose usage is the pair's cost
    return next(
        (n for n in nodes if isinstance(n, SwarmCostNode) and n.role == role), None
    )


def token_node(nodes: Sequence[Limit], role: NodeRole) -> SwarmTokenNode | None:
    # a lone token node with this role; a cost pair's token node is not it
    return next(
        (
            n
            for n in nodes
            if isinstance(n, SwarmTokenNode) and n.role == role and n.pair is None
        ),
        None,
    )


@dataclass(frozen=True)
class BudgetUsage:
    """One capped kind of the swarm's budget, and what has been used of it."""

    cap: float
    """The swarm's cap: dollars, tokens or seconds."""

    used: float
    """What the whole swarm has used of the cap so far."""

    share: float | None = None
    """The member's own share of the cap when the budget is split, else `None`. Time is never split."""

    share_used: float | None = None
    """What the member has used of its share, when it has one."""


@dataclass(frozen=True)
class SwarmBudget:
    """The swarm's budget as a member sees it, from `swarm_budget()`."""

    split: Literal["pool", "equal"] | tuple[float, ...]
    """How the cost and token caps are divided between members, as `Budget.split`."""

    cost: BudgetUsage | None
    """The cost cap in dollars, or `None` when the swarm has none."""

    tokens: BudgetUsage | None
    """The token cap, or `None` when the swarm has none."""

    token_type: str
    """The tokens the token cap meters: `all`, or a metering type such as `output`."""

    time: BudgetUsage | None
    """The time cap, and the seconds since the swarm started, or `None` when the swarm has none."""


# The budget of one run of the swarm, as `swarm_budget()` reads it: the configured
# budget, the cap nodes, the time cap and the run's start, and each member's context and
# share nodes. A member of another run with the same name has a context of its own.
@dataclass(frozen=True)
class BudgetState:
    budget: Budget
    caps: Sequence[Limit]
    time: float | None
    start: float
    members: Mapping[str, tuple[MemberContext, Sequence[Limit]]]


current_budget: ContextVar[BudgetState | None] = ContextVar(
    "inspect_swarm_current_budget", default=None
)


def swarm_budget() -> SwarmBudget:
    """The budget of the swarm the caller runs in.

    Reports the swarm's cap per kind and how much of it the whole swarm has used, and, when the budget is split and the caller is a member, the member's share and its usage. The caps are the ones that stop the swarm, which `sample_limits()` does not show: the swarm holds back part of each sample limit.

    Returns:
        The budget as the caller sees it.

    Raises:
        RuntimeError: If called outside a running swarm.
    """
    state = current_budget.get()
    if state is None:
        raise RuntimeError(
            "swarm_budget() was called outside a swarm; call it from a swarm member or its tools."
        )
    share: Sequence[Limit] = ()
    member = current_member.get()
    if member is not None:
        context, nodes = state.members.get(member.name, (None, ()))
        if context is member:
            share = nodes
    token_cap = token_node(state.caps, "cap")
    return SwarmBudget(
        split=state.budget.split,
        cost=_budget_usage(cost_node(state.caps, "cap"), cost_node(share, "share")),
        tokens=_budget_usage(token_cap, token_node(share, "share")),
        token_type=token_cap.type if token_cap is not None else "all",
        time=None
        if state.time is None
        else BudgetUsage(cap=state.time, used=anyio.current_time() - state.start),
    )


def _budget_usage(cap: Limit | None, share: Limit | None) -> BudgetUsage | None:
    if cap is None or cap.limit is None:
        return None
    return BudgetUsage(
        cap=cap.limit,
        used=cap.usage,
        share=share.limit if share is not None else None,
        share_used=share.usage if share is not None else None,
    )
