from collections.abc import Mapping, Sequence
from typing import cast

from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.registry import (
    ModelDict,
    is_model_dict,
    registry_info,
    registry_params,
    registry_unqualified_name,
)
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.model import GenerateConfig, Model, get_model
from inspect_ai.model._model_info import MODEL_INFO_LOOKUP_API_KEY
from inspect_ai.util import Limit

from ._budget import Budget, CostPair, SwarmTokenNode, check_cost_data, derive_caps
from ._channel._channel import Channel
from ._controller._control import SwarmRun
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
        TypeError: If `members` holds a bare `@agent` factory, an ACP agent, or anything other than agents and `member()` records, or an argument has the wrong type.
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

    resolved_channels = _channels(channels)
    resolved_controller = resolve("swarm_controller", controller)
    if resolved_controller.check is not None:
        resolved_controller.check(roster)
    resolved_budget = _budget(budget)
    preambles = {
        info.name: _preamble(info, len(roster), resolved_channels) for info in roster
    }
    runs = [
        (record, name)
        for record, record_names in zip(records, names, strict=True)
        for name in record_names
    ]

    async def execute(state: AgentState) -> AgentState:
        caps = derive_caps(resolved_budget, len(roster))
        if caps.cost is not None:
            check_cost_data([_model(record) for record in records])
        cap_nodes: list[Limit] = []
        if caps.cost is not None:
            cap_nodes.extend(CostPair(caps.cost, owner=None, role="cap").nodes)
        if caps.tokens is not None:
            cap_nodes.append(
                SwarmTokenNode(caps.tokens, caps.token_type, owner=None, role="cap")
            )
        await SwarmRun(
            runs,
            input=state.messages,
            preambles=preambles,
            caps=cap_nodes,
            time=caps.time,
            grace=resolved_budget.grace,
        ).run(resolved_controller.run)
        return state

    return execute


def _preamble(member: MemberInfo, size: int, channels: Sequence[Channel]) -> str:
    parts = [
        f"You are {member.name}, one of {size} agents in a swarm working on this task at the same time."
        if size > 1
        else f"You are {member.name}, the only agent in this swarm."
    ]
    if member.role is not None:
        parts.append(f"Your role: {member.role}.")
    for channel in channels:
        text = channel.instructions(member)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _model(record: Member) -> str | Model:
    # the member's configured model, else the sample's; a model the agent resolves
    # itself at run time cannot be checked
    model = (
        registry_params(record.agent).get("model")
        if record.agent is not None
        else record.args.get("model")
    )
    if is_model_dict(model):
        return _lookup_model(model)
    return model if isinstance(model, str | Model) else get_model()


def _lookup_model(logged: ModelDict) -> Model:
    # A built agent's params hold its Model as the logged dict, which keeps its
    # configuration (base_url, config, model_args) but not its credentials. Rebuild it
    # with Inspect's placeholder credential, as get_model_info() resolves a name, so
    # that pricing is looked up under the canonical name of the configured model.
    try:
        return get_model(
            logged["model"],
            config=GenerateConfig(**logged["config"]),
            base_url=logged["base_url"],
            api_key=MODEL_INFO_LOOKUP_API_KEY,
            memoize=False,
            **logged["model_args"],
        )
    except Exception as ex:
        raise PrerequisiteError(
            f"A swarm cost cap requires cost data for every member's model, and the model of a built member, {logged['model']}, could not be resolved from its configuration to look it up: {ex}. Use the member's factory form, member(factory, args={{'model': ...}}), so the configured model itself is checked."
        ) from ex


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
