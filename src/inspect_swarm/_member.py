import inspect
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack, ExitStack
from typing import Any, assert_never, cast

from inspect_ai._util.registry import (
    extract_named_params,
    has_registry_params,
    is_registry_object,
    registry_info,
    registry_log_name,
    registry_lookup,
    registry_params,
    registry_unqualified_name,
    registry_value,
)
from inspect_ai.agent import Agent, AgentState
from inspect_ai.model import ChatMessage, StopReason
from inspect_ai.util import (
    Limit,
    LimitExceededError,
    cost_limit,
    current_span_id,
    message_limit,
    span,
    time_limit,
    token_limit,
    tool_state_scope,
    turn_limit,
)
from inspect_ai.util._limit import (
    _CostLimit,  # pyright: ignore[reportPrivateUsage]
    _MessageLimit,  # pyright: ignore[reportPrivateUsage]
    _TimeLimit,  # pyright: ignore[reportPrivateUsage]
    _TokenLimit,  # pyright: ignore[reportPrivateUsage]
    _TurnLimit,  # pyright: ignore[reportPrivateUsage]
    _WorkingLimit,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._span import AGENT_SPAN_TYPE
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_serializer,
    model_validator,
)

from ._budget import (
    CostPair,
    Lever,
    MemberContext,
    SwarmMessageNode,
    SwarmNode,
    SwarmTokenNode,
    SwarmTurnNode,
    current_member,
)

AgentFactory = Callable[..., Agent]
"""A registered `@agent` factory, such as `deepagent`."""

# the limit kinds a member takes, by the name its data gives them
_LIMIT_KINDS: dict[str, type[Limit]] = {
    "token": _TokenLimit,
    "cost": _CostLimit,
    "turn": _TurnLimit,
    "message": _MessageLimit,
    "time": _TimeLimit,
}

_DEEPAGENT = "inspect_ai/deepagent"

# characters a tool-state scope name may not contain
_RESERVED = "/#"


class Member(BaseModel):
    """A swarm member: a built agent, or a registered `@agent` factory and its arguments.

    Create one with `member()`, which documents the fields and the member contract.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, extra="forbid")

    agent: Agent | None = None
    """A built, registered agent. Exactly one of `agent` and `factory` is set."""

    factory: AgentFactory | None = None
    """A registered `@agent` factory, called with `args` to build the member's agent."""

    args: dict[str, Any] = {}
    """The factory's keyword arguments."""

    name: str | None = None
    """The member's name. `None` uses the agent's or factory's unqualified registry name."""

    role: str | None = None
    """The member's role, given to the controller and in the member's preamble."""

    count: int = 1
    """How many members this record stands for. Above 1 needs the factory form."""

    limits: list[Limit] = []
    """The member's own limits."""

    bridged: bool | None = None
    """Whether the member's agent is bridged. `None` counts agents in the `inspect_swe/` namespace as bridged."""

    @field_validator("factory", mode="before")
    @classmethod
    def _resolve_factory(cls, value: object) -> object:
        # a member given as data (CLI -S, a flow spec, or a log on retry) names its
        # factory by registry name; the lookup also loads its package's entry point
        if isinstance(value, str):
            factory = registry_lookup("agent", value)
            if factory is None:
                raise ValueError(f"{value!r} is not a registered agent.")
            return factory
        return value

    @field_validator("limits", mode="before")
    @classmethod
    def _rebuild_limits(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            items = cast(Sequence[object], value)
            return [
                _limit_from_dict(cast(Mapping[str, Any], item))
                if isinstance(item, Mapping)
                else item
                for item in items
            ]
        return value

    @model_validator(mode="after")
    def _check(self) -> "Member":
        if self.count < 1:
            raise ValueError(f"member(): count must be at least 1, got {self.count}.")
        if self.name is not None and (
            not self.name or any(c in self.name for c in _RESERVED)
        ):
            # the member's name is its tool-state scope, which refuses these
            raise ValueError(
                f"member(): name must be non-empty and must not contain '/' or '#', got {self.name!r}."
            )
        if self.agent is not None:
            if self.factory is not None:
                raise TypeError("member(): give an agent or a factory, not both.")
            _check_agent(self.agent, self.args, self.count)
        elif self.factory is not None:
            _check_factory(self.factory, self.args)
        else:
            raise TypeError("member(): give an agent or a factory.")
        for limit in self.limits:
            _limit_kind(limit)
        return self

    @model_serializer
    def _serialize(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.agent is not None:
            data["agent"] = registry_value(self.agent)
        else:
            assert self.factory is not None
            data["factory"] = registry_log_name(self.factory)
            # as @agent logs a built agent's params
            data["args"] = extract_named_params(self.factory, False, **self.args)
        data["name"] = self.name
        data["role"] = self.role
        data["count"] = self.count
        data["limits"] = [_limit_dict(limit) for limit in self.limits]
        data["bridged"] = self.bridged
        return data


def member(
    agent: Agent | AgentFactory,
    args: Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    role: str | None = None,
    count: int = 1,
    limits: Sequence[Limit] | None = None,
    bridged: bool | None = None,
) -> Member:
    """A swarm member, with a name, role, limits or count.

    A member is a built agent, `member(react(prompt=...))`, or a registered `@agent` factory and its arguments, `member(deepagent, args={"tools": [bash()]}, count=4)`. Both must be registered with `@agent`, so that the log can name them and replay can find them. A built agent can also be passed to `swarm(members=...)` without `member()`.

    No agent object runs twice at the same time in one sample. A built agent is one member: it is built once, when the task is built, and shared by concurrent samples, as any Inspect solver is. The same agent object cannot be in two members. Several copies of one configuration (`count` above 1) need the factory form: the swarm calls `factory(**args)` once for each member in each sample, so each copy gets its own agent and whatever the factory builds. `member()` checks the arguments against the factory's signature but does not call it, so the factory's own checks run when the member is built.

    Objects given to an agent are shared. Every member built from one record receives the same objects from `args`, and two built agents given the same tool object share it, across members and samples. Inspect's built-in tools keep their state in the sample store or the sandbox, so this matters only for a custom tool or helper that keeps state in its Python object (a counter, a cache, a client). To give each member its own, construct it inside a member builder, an `@agent` factory that builds the configured agent, and use the factory form.

    Tool state. Each member runs in its own tool-state scope, named after the member. Built-in tools that keep state in the sample store (`memory()`, `bash_session()`, `web_browser()`, the skill tool) give each member its own state when their `instance` is left at the default, so the default `memory()` is private to each member. An explicit `instance` is used as given: members, or the copies of one counted member, that name the same instance share its state, so a board shared on purpose is `memory(instance="board")`. The sample store itself is shared. State that custom tools or agent code keep in the store with no instance is shared unless they resolve their instance with `tool_state_instance()`. Built-in tool state that a setup solver prepared at the default instance before the swarm runs is not visible to the members.

    Submissions. A member that returns normally has submitted, and its output is its answer, unless the output's stop reason is `content_filter` or `model_length`. That is how `react()` returns after three refusals or a context overflow it could not recover from, so the member ends `unsubmitted` and the swarm never chooses its output as the answer. Other agents keep the rule that a normal return is a submission: an agent that gives up in some other way and returns is treated as having answered.

    `deepagent()` members with background dispatch (`background=True` or an integer) are not supported yet and are refused. So are ACP agents, which wait for their caller to drive them rather than returning: a built one when `member()` is called, and a factory's when the member is built.

    Args:
        agent: A built, registered agent, or a registered `@agent` factory.
        args: The factory's keyword arguments. Only for a factory.
        name: The member's name. Defaults to the agent's or factory's registry name, without its package. With `count` above 1 the members are `name-1` to `name-k`. It names the member's tool-state scope, so it cannot be empty or contain "/" or "#".
        role: The member's role, given to the controller and in the member's preamble.
        count: How many members this record stands for. Above 1 needs a factory.
        limits: The member's own token, cost, turn, message or time limits.
        bridged: Whether the agent is bridged (runs a CLI in the sandbox through Inspect's agent bridge). `None` counts inspect_swe's agents as bridged and others as native. Pass `True` for any other bridged agent: the swarm stops a bridged member by cancelling it, because a limit error raised inside the bridge would end the sample, and it must know before the member's first model request.

    Returns:
        The member record.

    Raises:
        TypeError: If `agent` is not registered with `@agent` or is an ACP agent, if a built agent is given `args` or `count` above 1, or if `args` do not match the factory's parameters.
        ValueError: If `count` is below 1, `name` is empty or contains "/" or "#", a limit is a working limit, or a `deepagent()` member uses background dispatch.
    """
    form = "factory" if is_factory(agent) else "agent"
    return Member.model_validate(
        {
            form: agent,
            "args": dict(args or {}),
            "name": name,
            "role": role,
            "count": count,
            "limits": list(limits or []),
            "bridged": bridged,
        }
    )


# The names of the members each record stands for, in roster order: `name-1` to
# `name-k` for a counted record, and default names that repeat across the roster
# numbered the same way. Explicit names are never renumbered.
def member_names(members: Sequence[Member]) -> list[list[str]]:
    defaults = Counter(
        _default_name(m) for m in members if m.name is None for _ in range(m.count)
    )
    numbered: Counter[str] = Counter()
    names: list[list[str]] = []
    for m in members:
        base = m.name if m.name is not None else _default_name(m)
        if m.name is None and defaults[base] > 1:
            record_names: list[str] = []
            for _ in range(m.count):
                numbered[base] += 1
                record_names.append(f"{base}-{numbered[base]}")
        elif m.count > 1:
            record_names = [f"{base}-{i}" for i in range(1, m.count + 1)]
        else:
            record_names = [base]
        names.append(record_names)

    seen = Counter(name for record_names in names for name in record_names)
    duplicates = [name for name, n in seen.items() if n > 1]
    if duplicates:
        raise ValueError(
            f"members=: member names must be unique; {', '.join(repr(d) for d in duplicates)} is used more than once. Give the members distinct names with member(..., name=...)."
        )
    return names


# The member's agent: the built agent, or a new one from the factory. Only the
# runtime calls it, in the sample and the member's tool-state scope.
def build_member(record: Member) -> Agent:
    if record.agent is not None:
        return record.agent
    assert record.factory is not None
    agent = record.factory(**record.args)
    _check_not_acp(agent)
    return agent


# The member's lever and the nodes its wrapper enters: the lever's pair, its share of
# the swarm's caps, then its own limits re-created from their values, fresh for each
# run (limits are single-use).
def member_nodes(
    record: Member, name: str, share: Sequence[Limit]
) -> tuple[Lever, list[Limit]]:
    lever = Lever(owner=name)
    nodes = [*lever.nodes, *share]
    for limit in record.limits:
        if isinstance(limit, _TokenLimit):
            nodes.append(
                SwarmTokenNode(limit.limit, limit.type, owner=name, role="limit")
            )
        elif isinstance(limit, _CostLimit):
            nodes.extend(CostPair(limit.limit, owner=name, role="limit").nodes)
        elif isinstance(limit, _TurnLimit):
            nodes.append(SwarmTurnNode(limit.limit, owner=name))
        elif isinstance(limit, _MessageLimit):
            nodes.append(SwarmMessageNode(limit.limit, owner=name))
        else:
            # a time limit stays Inspect's: it cancels where it was entered, here
            nodes.append(time_limit(limit.limit))
    return lever, nodes


# The member wrapper. Runs the member on `state` with `input` as its conversation, in
# its tool-state scope, cancel scope and agent span, under its nodes and the swarm's
# caps, then calls `end` with its status and the state the agent returned, inside the
# member's span when the span was entered. The caller keeps `state`, as as_solver()
# does, for the output of a member that does not return. No error whose source is a
# swarm node leaves it: a cap becomes a stop of the swarm, and the member's own nodes a
# status. Anything else (a foreign error) is raised, without the swarm's leaves, after
# `end` with `errored`; a cancellation from outside ends the member as `cancelled`.
async def run_member(
    record: Member,
    name: str,
    context: MemberContext,
    nodes: list[Limit],
    caps: Sequence[Limit],
    input: list[ChatMessage],
    state: AgentState,
    end: Callable[[str, AgentState], None],
) -> None:
    status = "cancelled"
    # holds the member's span open after its limits and cancel scope have exited, so
    # that their errors are classified inside it
    async with AsyncExitStack() as member_span:
        try:
            status, state = await _run_member(
                record, name, context, nodes, caps, input, state, member_span
            )
        except Exception:
            status = "errored"
            raise
        finally:
            end(status, state)


async def _run_member(
    record: Member,
    name: str,
    context: MemberContext,
    nodes: list[Limit],
    caps: Sequence[Limit],
    input: list[ChatMessage],
    state: AgentState,
    member_span: AsyncExitStack,
) -> tuple[str, AgentState]:
    token = current_member.set(context)
    error: Exception | None = None
    try:
        with tool_state_scope(name), context.scope:
            agent = build_member(record)
            # entered before the member's conversation is built, as run() does not: a
            # member message limit replaces the sample's for that conversation from its
            # first message. Errors from these nodes are classified below.
            with ExitStack() as entered:
                for node in nodes:
                    entered.enter_context(node)
                # the member's own copy of its input, as run() makes one
                state.messages = [
                    message.model_copy(update={"source": "input"}) for message in input
                ]
                # entered inside the member's limits and cancel scope, as run() enters
                # it, since a span-id provider may await
                await member_span.enter_async_context(span(name, type=AGENT_SPAN_TYPE))
                context.span_id = current_span_id()
                state = await agent(state)
    except Exception as ex:
        error = ex
    finally:
        current_member.reset(token)

    if error is not None:
        cap, owned, foreign = classify_errors(error, caps, nodes)
        if foreign is not None:
            raise foreign
        if cap:
            # the cap began the swarm's stop when it was reached
            return "swarm_cap", state
        assert owned is not None
        return _status_for(owned), state
    if context.stop_cause is not None:
        # a node that stopped a bridged member, whose cancellation may not have been
        # delivered when its last call was the one that stopped it
        return context.stop_cause, state
    if context.scope.cancelled_caught:
        # the hard stop
        return "cancelled", state
    if state.output.empty:
        return "submitted", state
    return _return_status(state.output.stop_reason), state


def _return_status(stop_reason: StopReason) -> str:
    # a normal return is a submission unless its output stopped the way react() does
    # after three refusals or a context overflow it could not recover from
    match stop_reason:
        case "content_filter" | "model_length":
            return "unsubmitted"
        case "stop" | "tool_calls" | "max_tokens" | "unknown":
            return "submitted"
        case _:
            assert_never(stop_reason)


def _status_for(error: LimitExceededError) -> str:
    # the member's lever, share or own limit; a member time limit is Inspect's own node
    if isinstance(error.source, SwarmNode):
        return error.source.stop_cause(error.type)
    return f"member_limit:{error.type}"


# Splits an error from a member or the controller into its leaves: whether any is from
# one of the swarm's caps, the first from the caller's own nodes, and the rest, which is
# foreign: a single leaf bare, several as a group. A cancellation in the group, as trio
# keeps one beside errors, stays in the rest.
def classify_errors(
    error: BaseException, caps: Sequence[Limit], own: Sequence[Limit]
) -> tuple[bool, LimitExceededError | None, BaseException | None]:
    group = (
        cast(BaseExceptionGroup[BaseException], error)
        if isinstance(error, BaseExceptionGroup)
        else BaseExceptionGroup("swarm", [error])
    )
    cap_errors, rest = group.split(lambda e: _source_in(e, caps))
    owned, foreign = (
        rest.split(lambda e: _source_in(e, own)) if rest is not None else (None, None)
    )
    owned_leaf = _leaves(owned)[0] if owned is not None else None
    assert owned_leaf is None or isinstance(owned_leaf, LimitExceededError)
    return (
        cap_errors is not None,
        owned_leaf,
        unwrap(foreign) if foreign is not None else None,
    )


# A group with a single leaf as that leaf, as collect() raises it; others unchanged.
def unwrap(error: BaseException) -> BaseException:
    if isinstance(error, BaseExceptionGroup):
        leaves = _leaves(cast(BaseExceptionGroup[BaseException], error))
        if len(leaves) == 1:
            return leaves[0]
    return cast(BaseException, error)


def _source_in(error: BaseException, nodes: Sequence[Limit]) -> bool:
    return isinstance(error, LimitExceededError) and any(
        error.source is node for node in nodes
    )


def _leaves(group: BaseExceptionGroup[BaseException]) -> list[BaseException]:
    leaves: list[BaseException] = []
    for ex in group.exceptions:
        if isinstance(ex, BaseExceptionGroup):
            leaves.extend(_leaves(cast(BaseExceptionGroup[BaseException], ex)))
        else:
            leaves.append(ex)
    return leaves


def _check_not_acp(agent: object) -> None:
    # by name, so that inspect_swarm does not import inspect_swe
    for cls in type(agent).__mro__:
        if cls.__name__ == "ACPAgent" and cls.__module__.split(".")[0] == "inspect_swe":
            raise TypeError(
                f"{_name(agent)} is an ACP agent. ACP agents are interactive sessions driven by their caller and cannot be swarm members."
            )


def is_factory(value: object) -> bool:
    # a factory is the object registered under its own name; an agent it built is not
    return (
        is_registry_object(value, "agent")
        and registry_lookup("agent", registry_info(value).name) is value
    )


def _is_built_agent(value: object) -> bool:
    return (
        is_registry_object(value, "agent")
        and has_registry_params(value)
        and not is_factory(value)
    )


def _name(value: object) -> str:
    if is_registry_object(value):
        return registry_unqualified_name(value)
    return getattr(value, "__name__", None) or type(value).__name__


def _default_name(m: Member) -> str:
    return registry_unqualified_name(m.agent if m.agent is not None else m.factory)


def _not_registered(value: object) -> TypeError:
    return TypeError(
        f"member(): {_name(value)} is not a registered agent or @agent factory. Decorate the function that builds it with @agent, so that the log can name it and replay can find it."
    )


def _check_agent(agent: Agent, args: Mapping[str, Any], count: int) -> None:
    _check_not_acp(agent)
    if is_factory(agent):
        raise TypeError(
            f"member(): {_name(agent)} is a factory: pass it as member({_name(agent)}, args={{...}})."
        )
    if not _is_built_agent(agent):
        raise _not_registered(agent)
    name = _name(agent)
    if args:
        raise TypeError(
            f"member(): args are the factory's arguments: pass the factory, member({name}, args={{...}})."
        )
    if count > 1:
        raise TypeError(
            f"member(): count > 1 runs several copies at once, so each needs its own agent: pass the factory and its arguments, member({name}, args={{...}}, count={count})."
        )
    if registry_info(agent).name == _DEEPAGENT:
        _check_background(registry_params(agent).get("background", False), name)


def _check_factory(factory: AgentFactory, args: Mapping[str, Any]) -> None:
    if not is_factory(factory):
        raise _not_registered(factory)
    name = _name(factory)
    try:
        inspect.signature(factory).bind(**args)
    except TypeError as ex:
        raise TypeError(
            f"member(): args do not match the parameters of {name}: {ex}."
        ) from None
    if registry_info(factory).name == _DEEPAGENT:
        _check_background(args.get("background", False), name)


def _check_background(background: object, name: str) -> None:
    # True or an integer cap both enable background dispatch
    if background is not False:
        raise ValueError(
            f"member(): {name}(background={background!r}) is not supported in a swarm yet: background subagents outlive the member that started them. Use background=False."
        )


def _limit_kind(limit: Limit) -> str:
    if isinstance(limit, _WorkingLimit):
        raise ValueError(
            "member(): a working limit cannot be a member limit, because Inspect enforces working limits only for the whole sample. Use a time limit."
        )
    for kind, cls in _LIMIT_KINDS.items():
        if isinstance(limit, cls):
            return kind
    raise TypeError(
        f"member(): limits must be token, cost, turn, message or time limits, got {type(limit).__name__}."
    )


def _limit_dict(limit: Limit) -> dict[str, Any]:
    data: dict[str, Any] = {"limit": _limit_kind(limit), "value": limit.limit}
    if isinstance(limit, _TokenLimit):
        data["type"] = limit.type
    return data


def _limit_from_dict(data: Mapping[str, Any]) -> Limit:
    kind = data.get("limit")
    if "value" not in data:
        # a missing value would otherwise rebuild the limit as unlimited
        raise ValueError(
            f"A member limit dict needs a 'value' key (None for no limit), got {dict(data)!r}."
        )
    value = data["value"]
    match kind:
        case "token":
            return token_limit(value, type=data.get("type", "all"))
        case "cost":
            return cost_limit(value)
        case "turn":
            return turn_limit(value)
        case "message":
            return message_limit(value)
        case "time":
            return time_limit(value)
        case _:
            raise ValueError(
                f"{kind!r} is not a member limit kind; expected one of {', '.join(_LIMIT_KINDS)}."
            )
