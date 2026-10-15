from collections.abc import Sequence
from typing import cast

from inspect_ai._util.registry import registry_info, registry_unqualified_name
from inspect_ai.agent import Agent, AgentState, agent

from ._budget import Budget
from ._channel._channel import Channel
from ._controller._controller import Controller, MemberInfo
from ._member import (
    Member,
    check_loggable,
    check_member,
    is_factory,
    member,
    member_names,
)
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


# Inspect logs swarm()'s arguments as given, so each is checked as given, before
# it is converted: see check_loggable().
def _members(members: object) -> list[Member]:
    # a single member, or a list or tuple; a dict is a member's logged form
    if type(members) in (list, tuple):
        items = list(cast(Sequence[object], members))
        if not items:
            raise ValueError("members= must name at least one member.")
        return [_member(item, f"members[{i}]") for i, item in enumerate(items)]
    return [_member(members, "members")]


def _member(item: object, path: str) -> Member:
    if isinstance(item, Member):
        # rerun the record's checks: its args or the registry may have changed since
        record = Member.model_validate(item)
    elif type(item) is dict:
        check_loggable(cast(object, item), path)
        record = Member.model_validate(item)
    elif is_factory(item):
        name = registry_unqualified_name(item)
        raise TypeError(
            f"members= takes agents or member() records: write member({name}, args={{...}})."
        )
    elif callable(item):
        record = member(item)  # pyright: ignore[reportArgumentType]
    else:
        check_loggable(item, path)
        raise TypeError(
            f"members= takes agents or member() records, got a {type(item).__name__}."
        )
    check_member(record, path)
    return record


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
    check_loggable(channels, "channels")
    # check_loggable() has refused list and tuple subclasses; resolve() refuses other items
    items = cast(
        Sequence[str | Channel],
        channels if isinstance(channels, list | tuple) else [channels],
    )
    resolved = [resolve("swarm_channel", item) for item in items]
    names = [registry_info(channel).name for channel in resolved]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"channels=: each channel may be given once; {', '.join(duplicates)} is given more than once."
        )
    return resolved


def _budget(budget: object) -> Budget:
    # replay passes a pydantic argument back as its logged dict
    if isinstance(budget, Budget):
        return budget
    check_loggable(budget, "budget")
    if type(budget) is dict:
        return Budget.model_validate(budget)
    raise TypeError(
        f"budget= must be a Budget or its logged dict, got a {type(budget).__name__}."
    )
