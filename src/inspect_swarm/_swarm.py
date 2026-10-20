import inspect
from collections.abc import Mapping, Sequence
from typing import cast

from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.registry import (
    is_model_dict,
    registry_info,
    registry_lookup,
    registry_params,
    registry_unqualified_name,
)
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.model import Model, get_model
from inspect_ai.util import Limit
from inspect_ai.util._sandbox.context import (
    sandbox_environments_context_var,  # pyright: ignore[reportPrivateUsage]
)

from ._budget import (
    Budget,
    CostPair,
    SwarmTokenNode,
    check_cost_data,
    derive_caps,
    is_bridged,
    share_nodes,
    split_sentence,
)
from ._channel._channel import Channel
from ._controller._control import SwarmRun
from ._controller._controller import Controller, MemberInfo
from ._member import Member, is_factory, member, member_names, member_sandbox
from ._registry import resolve

_FILESYSTEM = "inspect_swarm/filesystem"


@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],
    *,
    controller: str | Controller = "leaderless",
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),
    budget: Budget = Budget(),  # noqa: B008
) -> Agent:
    """A swarm of agents working on one sample.

    Each member gets a timeline in the sample named after it, so member names must be unique across the swarm runs and other timelines in a sample. To run swarms more than once in one sample, build them with distinct member names. A member may not be named `init`, `solvers` or `scorers`: Inspect's timelines read spans with those names as the sample's phases.

    Args:
        members: The roster: built agents or `member()` records. A bare agent is `member(agent)`. A factory-form `member()` may stand for several members (`count`).
        controller: How the swarm runs: who starts, when and with what input, and when the swarm is done. A name resolves to a registered controller with its default parameters.
        channels: How members share information. A name resolves to a registered channel with its default parameters. `[]` means none: members get no channel instructions, though a shared sandbox is still shared.
        budget: The swarm-wide cap.

    Returns:
        The swarm, as an agent.

    Raises:
        TypeError: If `members` holds a bare `@agent` factory, an ACP agent, or anything other than agents and `member()` records, or an argument has the wrong type.
        ValueError: If member names collide, a member is named `init`, `solvers` or `scorers`, the same agent object is in two members, a channel is given twice, two channels share a record name or prefix, a name does not resolve, the controller or a channel refuses the roster, a bridged member is in a swarm with a channel that has records (bridged members cannot use them yet), some members have their own sandbox and others do not, two members have the same sandbox, a member lists another member's sandbox in `visible_sandboxes`, or the `filesystem` channel is given while members have their own sandboxes; or, when the returned agent runs, if the sample already has a timeline named after one of its members or does not have a sandbox a member names.
    """
    records = _members(members)
    names = member_names(records)
    _check_distinct_agents(records, names)
    _check_phase_names(names)
    roster = [
        MemberInfo(name=name, role=record.role)
        for record, record_names in zip(records, names, strict=True)
        for name in record_names
    ]

    resolved_channels = _channels(channels)
    resolved_controller = resolve("swarm_controller", controller)
    if resolved_controller.check is not None:
        resolved_controller.check(roster)
    _check_bus_channels(resolved_channels, records, names, roster)
    resolved_budget = _budget(budget)
    runs = [
        (record, name)
        for record, record_names in zip(records, names, strict=True)
        for name in record_names
    ]
    own_sandboxes = _check_member_sandboxes(runs, resolved_channels)

    async def execute(state: AgentState) -> AgentState:
        _check_sandbox_names(runs)
        caps = derive_caps(resolved_budget, len(roster))
        if caps.cost is not None:
            models = [_model(record) for record in records]
            check_cost_data(
                [model for model, _ in models],
                built={str(model) for model, logged in models if logged},
            )
        cap_nodes: list[Limit] = []
        if caps.cost is not None:
            cap_nodes.extend(CostPair(caps.cost, owner=None, role="cap").nodes)
        if caps.tokens is not None:
            cap_nodes.append(
                SwarmTokenNode(caps.tokens, caps.token_type, owner=None, role="cap")
            )
        shares = share_nodes(resolved_budget, caps, [info.name for info in roster])
        # here rather than at construction: the split is stated only under a cap
        preambles = {
            info.name: _preamble(
                info,
                len(roster),
                resolved_channels,
                own_sandboxes,
                split_sentence(resolved_budget, caps, i, len(roster)),
            )
            for i, info in enumerate(roster)
        }
        await SwarmRun(
            runs,
            state=state,
            input=state.messages,
            preambles=preambles,
            caps=cap_nodes,
            time=caps.time,
            grace=resolved_budget.grace,
            budget=resolved_budget,
            shares=shares,
            channels=resolved_channels,
        ).run(resolved_controller.run)
        return state

    return execute


def _preamble(
    member: MemberInfo,
    size: int,
    channels: Sequence[Channel],
    own_sandboxes: bool,
    split: str | None,
) -> str:
    parts = [
        f"You are {member.name}, one of {size} agents in a swarm working on this task at the same time."
        if size > 1
        else f"You are {member.name}, the only agent in this swarm."
    ]
    if member.role is not None:
        parts.append(f"Your role: {member.role}.")
    if split is not None:
        parts.append(split)
    if own_sandboxes:
        parts.append("You work in your own sandbox, which no other member can reach.")
    for channel in channels:
        text = channel.instructions(member)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _model(record: Member) -> tuple[str | Model, bool]:
    # the member's configured model, else the sample's, and whether it is known only
    # from a built agent's logged params; a model the agent resolves itself at run time
    # cannot be checked. The logged dict's name is the string metering prices it under.
    if record.agent is not None:
        params = registry_params(record.agent)
        factory = registry_lookup("agent", registry_info(record.agent).name)
    else:
        params, factory = record.args, record.factory
    if "model" not in params and _default_model(factory) is not None:
        # the logged params and a factory's args leave defaults out; no registered
        # agent defaults its model to anything but None
        name = registry_info(record.agent or factory).name
        raise PrerequisiteError(
            f"A swarm cost cap requires cost data for every member's model, and {name} uses the default of its model parameter, which the swarm does not check. Give the member its model explicitly."
        )
    model = params.get("model")
    if is_model_dict(model):
        return model["model"], True
    if isinstance(model, str | Model):
        return model, False
    if model is not None:
        # an agent or other object the member delegates its calls to
        name = registry_info(record.agent or factory).name
        raise PrerequisiteError(
            f"A swarm cost cap requires cost data for every member's model, and {name} is given a model argument that is not a model name or Model, which the swarm cannot check. Give the member a model name or Model."
        )
    return get_model(), False


def _default_model(factory: object) -> object:
    if not callable(factory):
        return None
    parameter = inspect.signature(factory).parameters.get("model")
    if parameter is None or parameter.default is inspect.Parameter.empty:
        return None
    return parameter.default


def _members(members: object) -> list[Member]:
    # a single member, or a sequence; a mapping is a member given as data
    items: list[object] = (
        list(cast(Sequence[object], members))
        if isinstance(members, Sequence) and not isinstance(members, str)
        else [members]
    )
    if not items:
        raise ValueError("members= must name at least one member.")
    return [_member(item) for item in items]


def _member(item: object) -> Member:
    if isinstance(item, Member):
        # rerun the record's checks: its args or the registry may have changed since
        return Member.model_validate(item)
    if isinstance(item, Mapping):
        return Member.model_validate(dict(cast(Mapping[str, object], item)))
    if is_factory(item):
        name = registry_unqualified_name(item)
        raise TypeError(
            f"members= takes agents or member() records: write member({name}, args={{...}})."
        )
    if callable(item):
        return member(item)  # pyright: ignore[reportArgumentType]
    raise TypeError(
        f"members= takes agents or member() records, got a {type(item).__name__}."
    )


# span names Inspect's timeline_build() reads as the sample's phases, wherever they are
_PHASE_NAMES = ("init", "solvers", "scorers")


def _check_phase_names(names: Sequence[list[str]]) -> None:
    for name in (name for record_names in names for name in record_names):
        if name in _PHASE_NAMES:
            raise ValueError(
                f"members=: a member is named {name!r}, which Inspect's timelines read as a phase of the sample, so the member's timeline would lose its agents. Give the member another name with member(..., name=...)."
            )


def _check_distinct_agents(
    records: Sequence[Member], names: Sequence[list[str]]
) -> None:
    # one agent object, one member, so that no agent object runs twice at once in a sample
    seen: dict[int, str] = {}
    for record, record_names in zip(records, names, strict=True):
        if record.agent is None:
            continue
        name = record_names[0]
        other = seen.setdefault(id(record.agent), name)
        if other != name:
            raise ValueError(
                f"members=: members {other!r} and {name!r} are the same agent object, which would run twice at once in each sample. Build a separate agent for each, or pass the factory with count, member(factory, args={{...}}, count=2)."
            )


def _channels(channels: str | Channel | Sequence[str | Channel]) -> list[Channel]:
    items = [channels] if isinstance(channels, str | Channel) else list(channels)
    resolved = [resolve("swarm_channel", item) for item in items]
    names = [registry_info(channel).name for channel in resolved]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"channels=: each channel may be given once; {', '.join(duplicates)} is given more than once."
        )
    return resolved


def _check_bus_channels(
    channels: Sequence[Channel],
    records: Sequence[Member],
    names: Sequence[list[str]],
    roster: Sequence[MemberInfo],
) -> None:
    # channels with records must not collide, and bridged members have no way to their
    # tools yet
    for channel in channels:
        if (channel.name is None) != (channel.prefix is None):
            raise ValueError(
                f"channels=: {registry_info(channel).name} must set both a record name and a record prefix, or neither."
            )
    for field in ("name", "prefix"):
        seen: dict[str, str] = {}
        for channel in channels:
            value = getattr(channel, field)
            if value is None:
                continue
            other = seen.setdefault(value, registry_info(channel).name)
            if other != registry_info(channel).name:
                raise ValueError(
                    f"channels=: {other} and {registry_info(channel).name} both have the record {field} {value!r}, so their records would collide."
                )
    for channel in channels:
        channel.check(roster)
    bus_channels = [c for c in channels if c.name is not None]
    if not bus_channels:
        return
    for record, record_names in zip(records, names, strict=True):
        agent = record.agent if record.agent is not None else record.factory
        if is_bridged(cast(Agent, agent), record.bridged):
            raise ValueError(
                f"swarm(): member {record_names[0]!r} is bridged, and bridged members cannot use bus channels such as {registry_info(bus_channels[0]).name} yet. Leave the channel out, or the member."
            )


# Whether the members have their own sandboxes. Then every member has one, no two
# share one or reach another's, and the filesystem channel, which describes a shared
# sandbox, is refused.
def _check_member_sandboxes(
    runs: Sequence[tuple[Member, str]], channels: Sequence[Channel]
) -> bool:
    sandboxes = {name: member_sandbox(record, name) for record, name in runs}
    own = [name for name, sandbox in sandboxes.items() if sandbox is not None]
    if not own:
        return False
    shared = [name for name, sandbox in sandboxes.items() if sandbox is None]
    if shared:
        raise ValueError(
            f"swarm(): member {own[0]!r} has its own sandbox and {shared[0]!r} does not. Give every member its own sandbox, with member(..., sandbox=...), or none."
        )
    if any(registry_info(channel).name == _FILESYSTEM for channel in channels):
        raise ValueError(
            f"swarm(): the filesystem channel describes a sandbox the members share, and member {own[0]!r} has its own. Leave the channel out, with channels=[] or other channels."
        )
    owners: dict[str, str] = {}
    for name, sandbox in sandboxes.items():
        assert sandbox is not None
        other = owners.setdefault(sandbox, name)
        if other != name:
            raise ValueError(
                f"swarm(): members {other!r} and {name!r} both have the sandbox {sandbox!r}, and a member's sandbox is its own. Give each member its own; for the copies of a counted member, sandbox='{{member}}' gives each the sandbox named after it."
            )
    for record, name in runs:
        for visible in record.visible_sandboxes:
            owner = owners.get(visible, name)
            if owner != name:
                raise ValueError(
                    f"swarm(): member {name!r} lists {visible!r} in visible_sandboxes, which is the sandbox of member {owner!r}. A member cannot reach another member's sandbox."
                )
    return True


# At swarm start: every sandbox a member names is one of the sample's.
def _check_sandbox_names(runs: Sequence[tuple[Member, str]]) -> None:
    environments = sandbox_environments_context_var.get(None) or {}
    for record, name in runs:
        sandbox = member_sandbox(record, name)
        if sandbox is None:
            continue
        for environment in (sandbox, *record.visible_sandboxes):
            if environment not in environments:
                raise ValueError(
                    f"swarm(): member {name!r} names the sandbox {environment!r}, which this sample does not have. The sample's sandboxes are: {', '.join(repr(e) for e in environments) or 'none'}."
                )


def _budget(budget: object) -> Budget:
    # a budget given as data (CLI -S, a flow spec, or a log on retry) is its fields
    if isinstance(budget, Budget):
        return budget
    if isinstance(budget, Mapping):
        return Budget.model_validate(dict(cast(Mapping[str, object], budget)))
    raise TypeError(
        f"budget= must be a Budget or a dict of its fields, got a {type(budget).__name__}."
    )
