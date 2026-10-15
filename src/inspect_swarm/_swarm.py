from collections.abc import Mapping, Sequence
from typing import cast

from inspect_ai._util.registry import registry_info, registry_unqualified_name
from inspect_ai.agent import Agent, AgentState, agent

from ._budget import Budget
from ._channel._channel import Channel
from ._controller._controller import Controller, MemberInfo
from ._member import Member, is_factory, member, member_names
from ._registry import resolve


@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],
    *,
    controller: str | Controller = "leaderless",
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),
    budget: Budget = Budget(),  # noqa: B008
) -> Agent:
    """A swarm of agents working on one sample.

    Args:
        members: The roster: built agents or `member()` records. A bare agent is `member(agent)`. A factory-form `member()` may stand for several members (`count`).
        controller: How the swarm runs: who starts, when and with what input, and when the swarm is done. A name resolves to a registered controller with its default parameters.
        channels: How members share information. A name resolves to a registered channel with its default parameters. `[]` means none: members get no channel instructions, though a shared sandbox is still shared.
        budget: The swarm-wide cap.

    Returns:
        The swarm, as an agent.

    Raises:
        TypeError: If `members` holds a bare `@agent` factory or anything other than agents and `member()` records, or an argument has the wrong type.
        ValueError: If member names collide, the same agent object is in two members, a channel is given twice, a name does not resolve, or the controller refuses the roster.
    """
    records = _members(members)
    names = member_names(records)
    _check_distinct_agents(records, names)
    roster = [
        MemberInfo(name=name, role=record.role)
        for record, record_names in zip(records, names, strict=True)
        for name in record_names
    ]

    _channels(channels)
    resolved_controller = resolve("swarm_controller", controller)
    if resolved_controller.check is not None:
        resolved_controller.check(roster)
    _budget(budget)

    async def execute(state: AgentState) -> AgentState:
        # the runtime that runs the roster comes with the controller surface
        raise NotImplementedError("The swarm runtime is not implemented yet.")

    return execute


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


def _budget(budget: object) -> Budget:
    # a budget given as data (CLI -S, a flow spec, or a log on retry) is its fields
    if isinstance(budget, Budget):
        return budget
    if isinstance(budget, Mapping):
        return Budget.model_validate(dict(cast(Mapping[str, object], budget)))
    raise TypeError(
        f"budget= must be a Budget or a dict of its fields, got a {type(budget).__name__}."
    )
