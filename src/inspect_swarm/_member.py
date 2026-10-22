import inspect
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AsyncExitStack, ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, assert_never, cast, overload

import anyio
from inspect_ai._util.registry import (
    extract_named_params,
    has_registry_params,
    is_registry_object,
    registry_info,
    registry_log_name,
    registry_lookup,
    registry_unqualified_name,
    registry_value,
)
from inspect_ai.agent import Agent, AgentState
from inspect_ai.model import ChatMessage, StopReason
from inspect_ai.util import (
    Limit,
    LimitExceededError,
    SandboxEnvironment,
    cost_limit,
    current_span_id,
    message_limit,
    sandbox_default,
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
from inspect_ai.util._sandbox.context import (
    sandbox_environments_context_var,  # pyright: ignore[reportPrivateUsage]
    sandbox_with_environments_context_var,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._span import AGENT_SPAN_TYPE
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_serializer,
    model_validator,
)

from ._background import MemberBackground, self_stop
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

# characters a tool-state scope name may not contain
_RESERVED = "/#"

# the member's name in a sandbox name
_MEMBER_FIELD = "{member}"


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

    delivery: Literal["notify", "poll"] | None = None
    """The member's configured delivery mode: `notify` or `poll`. `None` selects `notify`. In `notify` mode, `swarm_on_continue()` adds notices to the member's conversation; in `poll` mode it adds none, and the member learns of changes from the status line that ends a shared memory result and from `swarm_wait` results."""

    sandbox: str | None = None
    """The member's own sandbox environment, where `{member}` stands for the member's name. `None` shares the sample's sandbox."""

    visible_sandboxes: list[str] = []
    """Other sandbox environments the member can reach by name, such as a shared service. Only with `sandbox`."""

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
        _check_sandbox(self.sandbox, self.visible_sandboxes)
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
        data["delivery"] = self.delivery
        data["sandbox"] = self.sandbox
        data["visible_sandboxes"] = list(self.visible_sandboxes)
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
    delivery: Literal["notify", "poll"] | None = None,
    sandbox: str | None = None,
    visible_sandboxes: Sequence[str] = (),
) -> Member:
    """A swarm member, with a name, role, limits or count.

    A member is a built agent, `member(react(prompt=...))`, or a registered `@agent` factory and its arguments, `member(deepagent, args={"tools": [bash()]}, count=4)`. Both must be registered with `@agent`, so that the log can name them and replay can find them. A built agent can also be passed to `swarm(members=...)` without `member()`.

    No agent object runs twice at the same time in one sample. A built agent is one member: it is built once, when the task is built, and shared by concurrent samples, as any Inspect solver is. The same agent object cannot be in two members. Several copies of one configuration (`count` above 1) need the factory form: the swarm calls `factory(**args)` once for each member in each sample, so each copy gets its own agent and whatever the factory builds. `member()` checks the arguments against the factory's signature but does not call it, so the factory's own checks run when the member is built.

    Names. Members left on their default name are numbered across the whole roster, not per record, so one member with different arguments takes two records: `[member(react, args={"prompt": a}, count=6), member(react, args={"prompt": b, "model": m})]` is `react-1` to `react-7`. When every copy differs, give each its own record, as in `[member(react, args={"prompt": p}) for p in prompts]`. Names given with `name` are not numbered across records, so give each record its own.

    Objects given to an agent are shared. Every member built from one record receives the same objects from `args`, and two built agents given the same tool object share it, across members and samples. Inspect's built-in tools keep their state in the sample store or the sandbox, so this matters only for a custom tool or helper that keeps state in its Python object (a counter, a cache, a client). To give each member its own, construct it inside a member builder, an `@agent` factory that builds the configured agent, and use the factory form.

    Tool state. Each member runs in its own tool-state scope, named after the member. Built-in tools that keep state in the sample store (`memory()`, `bash_session()`, `web_browser()`, the skill tool) give each member its own state when their `instance` is left at the default, so the default `memory()` is private to each member. An explicit `instance` is used as given: members, or the copies of one counted member, that name the same instance share its state, so a board shared on purpose is `memory(instance="board")`. The sample store itself is shared. State that custom tools or agent code keep in the store with no instance is shared unless they resolve their instance with `tool_state_instance()`. Built-in tool state that a setup solver prepared at the default instance before the swarm runs is not visible to the members.

    Submissions. A member that returns normally has submitted, and its output is its answer, unless the output's stop reason is `content_filter` or `model_length`. That is how `react()` returns after three refusals or a context overflow it could not recover from, so the member ends `unsubmitted` and the swarm never chooses its output as the answer. Other agents keep the rule that a normal return is a submission: an agent that gives up in some other way and returns is treated as having answered.

    Stopping itself. An agent that raises `LimitExceededError("custom", ...)` with no `source` to stop itself ends only its member, with status `member_limit:custom`; the other members continue and the swarm never chooses its output as the answer. This holds when the error is raised in a task group inside the agent, too.

    Sandboxes. Members share the sample's sandbox by default. With `sandbox`, a member works in its own: the swarm makes that environment the member's default, so `sandbox()` in its tools returns it, and hides the sample's other environments from the member except those in `visible_sandboxes`, such as a shared service. Tools that choose a sandbox without a name, such as `text_editor()`, `bash_session()` and the agent bridge, use the member's own; a visible service is reached only by name. `sandbox="{member}"` gives each copy of a counted member the environment named after it: `member(factory, name="agent", count=2, sandbox="{member}")` runs `agent-1` and `agent-2` in the environments of those names. The sample's sandbox configuration provides the environments (for Docker, a compose service for each), and the swarm checks that they exist when it starts. When one member has its own sandbox, every member must have one, no two the same, and the `filesystem` channel, which describes a shared sandbox, can be used only as `filesystem(shared_volume=True)`, for sandboxes that each mount one shared volume. A service in `visible_sandboxes` is reachable from any of the member's tools that take a sandbox name, so list only services every member may use.

    Scoring with member sandboxes. The task's scorers run after the swarm, outside every member, where `sandbox()` is `default`. A scorer that grades the members' work names each member's sandbox itself, as in `sandbox("agent-1")`, and the `sandbox` field of each member in the swarm's result record says which member used which.

    Background work belongs to the member. Work its agent starts with `background()`, such as `deepagent(background=...)` subagents, spends through the member's limits and share, and is the member's in the ledger. When the member's agent returns, the swarm stops the background work from starting model calls, gives it the swarm's grace period to finish calls and tool calls in flight, and cancels what is left. The member ends after that, with the status its agent ended with; `Budget.grace=0` cancels background work at once. A member time limit bounds its agent only: after the agent returns, its background work gets the grace period too, and the cancelled work's cleanup is awaited. Background work must be started by the agent while it runs; a factory that starts it while building the agent makes the member `errored`. A sourceless custom limit error raised in background work ends only the task that raised it; the member's agent and its other background work continue. Work started outside `background()` (an `asyncio` task, a process left running in the sandbox) is not stopped. Bridged members cannot have background work: `background()` raises in a member declared or found bridged, and a member that uses the agent bridge after starting background work (through a bridged subagent, for example) is cancelled and ends `errored`.

    ACP agents, which wait for their caller to drive them rather than returning, are refused: a built one when `member()` is called, and a factory's when the member is built.

    Args:
        agent: A built, registered agent, or a registered `@agent` factory.
        args: The factory's keyword arguments. Only for a factory.
        name: The member's name. Defaults to the agent's or factory's registry name, without its package; a default name that more than one member has is numbered across the roster. With `count` above 1 the members are `name-1` to `name-k`. It names the member's tool-state scope, so it cannot be empty or contain "/" or "#".
        role: The member's role, given to the controller and in the member's preamble.
        count: How many members this record stands for. Above 1 needs a factory.
        limits: The member's own token, cost, turn, message or time limits.
        bridged: Whether the agent is bridged (runs a CLI in the sandbox through Inspect's agent bridge). `None` counts inspect_swe's agents as bridged and others as native. Pass `True` for any other bridged agent: the swarm stops a bridged member by cancelling it, because a limit error raised inside the bridge would end the sample, and it must know before the member's first model request. Bridged members cannot use channels with tools and records yet, and `swarm()` refuses one beside such a channel. A bridged agent that is not declared and not in the `inspect_swe/` namespace is not refused: it cannot reach those channels' tools, so it runs without them. Pass `True` to have it refused instead.
        delivery: The member's configured delivery mode: `notify` or `poll`. `None` selects `notify`. In `notify` mode, `swarm_on_continue()` adds notices to the member's conversation; in `poll` mode it adds none, and the member learns of changes from the status line that ends a shared memory result and from `swarm_wait` results. A member declared or identified as bridged before it starts has effective mode `poll`. Bridge use detected later leaves its configured mode unchanged.
        sandbox: The name of the member's own sandbox environment. `{member}` stands for the member's name. `None` shares the sample's sandbox.
        visible_sandboxes: Other sandbox environments the member can reach by name, such as a shared service. Only with `sandbox`. It cannot include `default`, which inside the member is the member's own sandbox.

    Returns:
        The member record.

    Raises:
        TypeError: If `agent` is not registered with `@agent` or is an ACP agent, if a built agent is given `args` or `count` above 1, or if `args` do not match the factory's parameters.
        ValueError: If `count` is below 1, `name` is empty or contains "/" or "#", a limit is a working limit, `sandbox` is empty or has braces other than `{member}`, or `visible_sandboxes` is given without `sandbox`, names `default` or has an empty name.
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
            "delivery": delivery,
            "sandbox": sandbox,
            # a bare name is refused, rather than read as its characters
            "visible_sandboxes": visible_sandboxes
            if isinstance(visible_sandboxes, str)
            else list(visible_sandboxes),
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


# The member's own sandbox, by name, or None when it shares the sample's.
def member_sandbox(record: Member, name: str) -> str | None:
    if record.sandbox is None:
        return None
    return record.sandbox.replace(_MEMBER_FIELD, name)


_T = TypeVar("_T")


# A member's sandbox environments. Its own is the only one listed, so that Inspect's
# selections without a name (sandbox_with(), and the injected tools of text_editor(),
# bash_session(), sandbox MCP servers and the agent bridge, which take the first
# environment that already has them) find it; its visible sandboxes resolve only by
# name, through the lookups sandbox(name) and sandbox_with(name=) make.
class _MemberEnvironments(dict[str, SandboxEnvironment]):
    def __init__(
        self,
        own: str,
        environment: SandboxEnvironment,
        visible: Mapping[str, SandboxEnvironment],
    ) -> None:
        super().__init__({own: environment})
        self._visible = dict(visible)

    @overload
    def get(self, key: str, default: None = None, /) -> SandboxEnvironment | None: ...

    @overload
    def get(self, key: str, default: SandboxEnvironment, /) -> SandboxEnvironment: ...

    @overload
    def get(self, key: str, default: _T, /) -> SandboxEnvironment | _T: ...

    def get(self, key: str, default: object = None, /) -> object:
        return super().get(key, self._visible.get(key, default))

    def __getitem__(self, key: str) -> SandboxEnvironment:
        if super().__contains__(key):
            return super().__getitem__(key)
        return self._visible[key]

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or key in self._visible


# The member's own sandbox as its default, its visible sandboxes by name and no
# other, as LeanOpenProblems' members ran; with a sandbox_with() cache of its own, so
# that no lookup returns an environment it cannot see. The swarm checked the names
# when it started.
@contextmanager
def _member_sandboxes(
    sandbox: str | None, visible: Sequence[str]
) -> Generator[None, None, None]:
    if sandbox is None:
        yield
        return
    environments = sandbox_environments_context_var.get()
    environments_token = sandbox_environments_context_var.set(
        _MemberEnvironments(
            sandbox,
            environments[sandbox],
            {name: environments[name] for name in visible},
        )
    )
    with_token = sandbox_with_environments_context_var.set({})
    try:
        with sandbox_default(sandbox):
            yield
    finally:
        sandbox_with_environments_context_var.reset(with_token)
        sandbox_environments_context_var.reset(environments_token)


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


# What the member wrapper leaves on the member's run record: the state the agent
# returned, set before the drain, which may raise, and the counts of its background
# work, set after the owner has ended it.
@dataclass
class MemberOutcome:
    state: AgentState | None = None
    background_started: int = 0
    background_cancelled: int = 0


# The member wrapper. Runs the member on `state` with `input` as its conversation, in
# its tool-state scope, cancel scope and agent span, under its nodes and the swarm's
# caps, with its background work owned by the member; drains that work once the agent
# has returned; then calls `end` with its status and the state the agent returned,
# inside the member's span when the span was entered. The caller keeps `state`, as
# as_solver() does, for the output of a member that does not return. No error whose
# source is a swarm node leaves it: a cap becomes a stop of the swarm, and the member's
# own nodes a status, as does a sourceless custom limit error, which the agent raised
# to stop itself. Anything else (a foreign error) is raised, without the swarm's
# leaves, after `end` with `errored`; a cancellation from outside ends the member as
# `cancelled`.
async def run_member(
    record: Member,
    name: str,
    context: MemberContext,
    lever: Lever,
    nodes: list[Limit],
    caps: Sequence[Limit],
    input: list[ChatMessage],
    state: AgentState,
    grace: Callable[[], float],
    outcome: MemberOutcome,
    end: Callable[[str, AgentState], None],
) -> None:
    status = "cancelled"
    # holds the member's span open after its limits and cancel scope have exited, so
    # that their errors are classified inside it
    async with AsyncExitStack() as member_span:
        try:
            status = await _run_member(
                record,
                name,
                context,
                lever,
                nodes,
                caps,
                input,
                state,
                grace,
                outcome,
                member_span,
            )
        except Exception:
            status = "errored"
            raise
        finally:
            # the state the agent returned, kept through a drain that raised
            end(status, outcome.state if outcome.state is not None else state)


async def _run_member(
    record: Member,
    name: str,
    context: MemberContext,
    lever: Lever,
    nodes: list[Limit],
    caps: Sequence[Limit],
    input: list[ChatMessage],
    state: AgentState,
    grace: Callable[[], float],
    outcome: MemberOutcome,
    member_span: AsyncExitStack,
) -> str:
    token = current_member.set(context)
    status: str | None = None
    try:
        with (
            tool_state_scope(name),
            _member_sandboxes(member_sandbox(record, name), record.visible_sandboxes),
            context.scope,
        ):
            # inside the member's scopes, so the hard stop cancels its background work
            # and that work keeps the member's tool state and sandboxes; outside its
            # nodes, so a member time limit cancels only the agent
            owner = MemberBackground(context, contain=[*nodes, *caps])
            try:
                async with owner:
                    foreign: BaseException | None = None
                    try:
                        agent = build_member(record)
                        if owner.started:
                            # such work would miss the member's nodes and span
                            raise RuntimeError(
                                f"Member {name!r}: its factory started background work while building the agent. Background work must be started by the agent when it runs."
                            )
                        # entered before the member's conversation is built, as run()
                        # does not: a member message limit replaces the sample's for that
                        # conversation from its first message
                        with ExitStack() as entered:
                            for node in nodes:
                                entered.enter_context(node)
                            # the member's own copy of its input, as run() makes one
                            state.messages = [
                                message.model_copy(update={"source": "input"})
                                for message in input
                            ]
                            # entered inside the member's limits and cancel scope, as
                            # run() enters it, since a span-id provider may await
                            await member_span.enter_async_context(
                                span(name, type=AGENT_SPAN_TYPE)
                            )
                            context.span_id = current_span_id()
                            outcome.state, stopped = await _call_agent(agent, state)
                        status = _returned(context, outcome.state, stopped)
                    except Exception as ex:
                        cap, owned, foreign = classify_errors(ex, caps, nodes)
                        if foreign is None and cap:
                            # the cap began the swarm's stop when it was reached
                            status = "swarm_cap"
                        elif foreign is None:
                            assert owned is not None
                            status = _status_for(owned)
                    if foreign is not None:
                        # no drain: leaving the owner cancels the background work
                        raise foreign
                    await _drain(name, owner, lever, grace)
            finally:
                # after the owner has ended its work, so its cancellations are counted
                outcome.background_started = owner.started
                outcome.background_cancelled = owner.cancelled
        if context.refusal is not None:
            # bridge use found after the member started background work
            raise context.refusal
    finally:
        current_member.reset(token)

    if status is not None:
        return status
    # the agent was cancelled: a node that stopped a bridged member, or the hard stop
    return context.stop_cause or "cancelled"


# The member's background work, once its agent has returned: refused new model calls,
# released from its waits, given the effective grace to finish what it has in flight,
# then cancelled by leaving the owner.
async def _drain(
    name: str, owner: MemberBackground, lever: Lever, grace: Callable[[], float]
) -> None:
    # deferred: the bus imports the runtime, which imports this module
    from ._bus._bus import current_bus

    # a stop that already closed the lever keeps its reason
    lever.close("member_ended")
    # the member's waits end, and its later ones return at once
    bus = current_bus.get()
    if bus is not None:
        bus.member_ending(name)
    with anyio.move_on_after(grace()):
        await owner.wait()


def _returned(context: MemberContext, state: AgentState, stopped: bool) -> str:
    if context.stop_cause is not None:
        # a node that stopped a bridged member, whose cancellation may not have been
        # delivered when its last call was the one that stopped it, even if the agent
        # then stopped itself
        return context.stop_cause
    if stopped:
        return "member_limit:custom"
    if state.output.empty:
        return "submitted"
    return _return_status(state.output.stop_reason)


# The agent call: the state it returned, and whether it stopped itself. An error the
# agent raised to stop itself (a sourceless custom limit), bare or grouped with nothing
# else, returns the state it was given and True; a group that holds anything else is
# raised without it, and any other error as it is, for the wrapper to classify.
async def _call_agent(agent: Agent, state: AgentState) -> tuple[AgentState, bool]:
    try:
        return await agent(state), False
    except BaseException as ex:
        if self_stop(ex):
            return state, True
        if not isinstance(ex, BaseExceptionGroup):
            raise
        stops, rest = cast(BaseExceptionGroup[BaseException], ex).split(self_stop)
        if stops is None:
            raise
        if rest is None:
            return state, True
        remainder = rest
    raise remainder


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


def _check_sandbox(sandbox: str | None, visible: Sequence[str]) -> None:
    if sandbox is not None and (
        not sandbox or any(c in sandbox.replace(_MEMBER_FIELD, "") for c in "{}")
    ):
        raise ValueError(
            f"member(): sandbox must be a non-empty sandbox name, where {_MEMBER_FIELD} stands for the member's name and no other braces are used, got {sandbox!r}."
        )
    if not visible:
        return
    if sandbox is None:
        raise ValueError(
            "member(): visible_sandboxes is for a member with its own sandbox; give the member sandbox= as well. A member without one can reach every sandbox."
        )
    if not all(visible):
        raise ValueError("member(): visible_sandboxes cannot have an empty name.")
    if "default" in visible:
        raise ValueError(
            "member(): visible_sandboxes cannot name 'default': inside the member, sandbox('default') is the member's own sandbox. Give the service another name in the sample's sandbox configuration."
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
