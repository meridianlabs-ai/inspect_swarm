import inspect
from collections.abc import Mapping, Sequence
from typing import Literal, cast

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
from inspect_ai.util import Limit, sample_limits
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
    rederive_time_cap,
    share_nodes,
    split_sentence,
)
from ._channel._channel import Channel
from ._channel.filesystem import FilesystemChannel
from ._channel.messages import is_builtin_messages
from ._channel.shared_memory import is_builtin_board
from ._controller._control import SwarmRun
from ._controller._controller import Controller, MemberInfo
from ._controller.leaderless import leaderless
from ._evidence import PHASE_NAMES
from ._final import check_chain, final_chain
from ._isolation import (
    UserIsolation,
    check_isolated_roster,
    isolation_preamble,
    resolve_isolation,
    setup_isolation,
)
from ._member import (
    Member,
    is_factory,
    member,
    member_names,
    member_sandbox,
    persistent_refusal,
)
from ._record import forget_record
from ._registry import resolve
from ._result import ResultSpec


@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],
    *,
    controller: str | Controller = "leaderless",
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),
    budget: Budget = Budget(),  # noqa: B008
    result: ResultSpec | None = None,
    isolation: UserIsolation | Literal["users"] | None = None,
) -> Agent:
    """A swarm of agents working on one sample.

    The swarm adds one timeline to the sample, rooted at the agent span it runs in, with each member as a child, so the log viewer shows the swarm as one agent with its members as lanes. The timeline is named after that span (`swarm` when the swarm runs as a solver or with `run()`); a later swarm in the same sample gets `swarm-2`, `swarm-3`, and so on. The span must be an agent span not named `init`, `solvers` or `scorers`, and two swarms running at the same time each need their own: `run(swarm(...), input)` gives a swarm its own span. Member names must be unique across the swarms in a sample. To run swarms more than once in one sample, build them with distinct member names. A member may not be named `init`, `solvers` or `scorers`: Inspect's timelines read spans with those names as the sample's phases.

    Args:
        members: The roster: built agents or `member()` records. A bare agent is `member(agent)`. A factory-form `member()` may stand for several members (`count`).
        controller: How the swarm runs: who starts, when and with what input, and when the swarm is done. A name resolves to a registered controller with its default parameters.
        channels: How members share information. A name resolves to a registered channel with its default parameters. `[]` means none: members get no channel instructions, though a shared sandbox is still shared.
        budget: The swarm-wide cap.
        result: The task's result contract, from `answer_result()` or `artifact_result()`. With it, the swarm records each submission's answer key and the verifier's verdict on it, and the controller's default `final` chain uses them to choose the answer. It holds the task's callables, so the log records only its type name: rebuild the swarm through its task, or a registered builder, rather than from the log.
        isolation: How members are kept apart in the sandbox they share. `None`: members share it with no enforcement. `users()`, or `"users"` for its defaults: each member runs as its own Linux user, with a private home and the shared directories it lists (see `users()`). Not with members that have their own sandboxes.

    Returns:
        The swarm, as an agent.

    Raises:
        TypeError: If `members` holds a bare `@agent` factory, an ACP agent, or anything other than agents and `member()` records, `result` is the type name a log records for it, or an argument has the wrong type.
        PrerequisiteError: If the returned agent runs in a sample with checkpointing on: checkpoint and resume are not supported for swarms yet.
        ValueError: If member names collide, two member names differ only in case, a member is named `init`, `solvers` or `scorers`, the same agent object is in two members, a channel is given twice, two channels share a record name or prefix, a name does not resolve, the controller or a channel refuses the roster, a bridged member is in a swarm with a channel that has records other than `messages` and `shared_memory` (`swarm_bridged_tools()` serves only those two), a member chooses its own name (`choose_name=True`) beside a channel other than `filesystem`, some members have their own sandbox and others do not, two members have the same sandbox, a member lists another member's sandbox in `visible_sandboxes`, a member is persistent while `result` has an `answer_key` or a `verifier`, the controller's `final` chain has `verify` without a verifier in `result`, `vote` without an answer key, `synthesize` without a result contract of kind `answer` or more than once, or a reporter, which the swarm does not run yet, the `filesystem` channel is given without `shared_volume=True` while members have their own sandboxes, or with it while members share the sample's sandbox; under users isolation, if a member name is not a valid user-name suffix, a member chooses its own name or has its own sandbox, or the `filesystem` channel's `scratch` is not `home`, its `shared` is not a `rw` share or its notes file is in none; or, when the returned agent runs, if it runs in a span that is not an agent span, in an agent span named `init`, `solvers` or `scorers`, or in the span of another swarm still running, if the sample already has a swarm member named after one of its members, or does not have a sandbox a member names.
        RuntimeError: Under users isolation, when the returned agent runs, if it runs inside a sandbox identity scope (such as a member of a swarm with users isolation), another swarm with users isolation in the sample has set up the same sandbox, the sandbox cannot run commands as other users, the image lacks a tool setup needs, a path is unsafe, or the self-test finds a boundary that does not hold.
    """
    records = _members(members)
    names = member_names(records)
    _check_distinct_agents(records, names)
    _check_phase_names(names)
    _check_case_names(names)
    _check_persistent(records, names)
    roster = [
        MemberInfo(name=name, role=record.role, choose_name=record.choose_name)
        for record, record_names in zip(records, names, strict=True)
        for name in record_names
    ]

    resolved_channels = _channels(channels)
    resolved_controller = resolve("swarm_controller", controller)
    if resolved_controller.check is not None:
        resolved_controller.check(roster)
    _check_bus_channels(resolved_channels, records, names, roster)
    _check_chosen_names(resolved_channels, roster)
    choosers = any(info.choose_name for info in roster)
    resolved_budget = _budget(budget)
    runs = [
        (record, name)
        for record, record_names in zip(records, names, strict=True)
        for name in record_names
    ]
    own_sandboxes = _check_member_sandboxes(runs, resolved_channels)
    resolved_result = _result(result)
    _check_result_members(resolved_result, records, names)
    chain = final_chain(resolved_controller.final, resolved_result)
    check_chain(chain, resolved_result)
    resolved_isolation = resolve_isolation(isolation)
    if resolved_isolation is not None:
        filesystems = [c for c in resolved_channels if isinstance(c, FilesystemChannel)]
        check_isolated_roster(
            resolved_isolation,
            roster,
            own_sandboxes,
            (
                (filesystems[0].shared, filesystems[0].notes, filesystems[0].scratch)
                if filesystems
                else None
            ),
        )

    async def execute(state: AgentState) -> AgentState:
        # first, before anything this run can fail at, so that it never leaves an
        # earlier swarm's record as its own
        forget_record()
        _check_sandbox_names(runs)
        # under users isolation the time cap is checked after setup, which takes time
        caps = derive_caps(
            resolved_budget, len(roster), check_time=resolved_isolation is None
        )
        # under derive=False the sample's cost limit can apply with no swarm cap, and an
        # unpriced member's calls would never advance it
        if caps.cost is not None or sample_limits().cost.limit is not None:
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
                choosers,
                split_sentence(resolved_budget, caps, i, len(roster)),
                resolved_isolation,
            )
            for i, info in enumerate(roster)
        }
        isolation = None
        if resolved_isolation is not None:
            isolation = await setup_isolation(
                resolved_isolation, [info.name for info in roster]
            )
            caps = rederive_time_cap(resolved_budget, caps)
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
            chain=chain,
            result=resolved_result,
            isolation=isolation,
        ).run(resolved_controller.run)
        return state

    return execute


def baseline(member: Member | Agent, result: ResultSpec | None = None) -> Agent:
    """A single agent run as a swarm of one, the baseline arm to compare a swarm with.

    It is `swarm(members=member, controller=leaderless(final="first"), channels=[], result=result)`. It writes the same result record as a swarm, with the same clock and the same answer key and verdict for the submission, so `member_scores()` and the analysis read every arm the same way. Unlike a plain agent, it returns an empty output when its member does not submit.

    Args:
        member: The agent, or a `member()` record of one member.
        result: The task's result contract, as the swarm arm passes it.

    Returns:
        The swarm of one, as an agent.

    Raises:
        ValueError: If `member` stands for more than one member (`count`), or for any reason `swarm()` refuses it.
        TypeError: For any reason `swarm()` refuses its arguments.
    """
    count = sum(len(names) for names in member_names(_members(member)))
    if count != 1:
        raise ValueError(
            f"baseline() runs one member, and member= stands for {count}. Use swarm() for more than one."
        )
    return swarm(
        members=member,
        controller=leaderless(final="first"),
        channels=[],
        result=result,
    )


def _preamble(
    member: MemberInfo,
    size: int,
    channels: Sequence[Channel],
    own_sandboxes: bool,
    choosers: bool,
    split: str | None,
    isolation: UserIsolation | None,
) -> str:
    if member.choose_name:
        intro = (
            f"You are one of {size} agents in a swarm working on this task at the same time. Choose a name for yourself and tell the other members what it is."
            if size > 1
            else "You are the only agent in this swarm. Choose a name for yourself."
        )
    else:
        intro = (
            f"You are {member.name}, one of {size} agents in a swarm working on this task at the same time."
            if size > 1
            else f"You are {member.name}, the only agent in this swarm."
        )
    parts = [intro]
    if member.role is not None:
        parts.append(f"Your role: {member.role}.")
    if split is not None:
        parts.append(split)
    if own_sandboxes:
        if any(isinstance(c, FilesystemChannel) and c.shared_volume for c in channels):
            parts.append(
                "You work in your own sandbox, which no other member can reach, except for the shared directory described below."
            )
        else:
            parts.append(
                "You work in your own sandbox, which no other member can reach."
            )
    for channel in channels:
        if choosers:
            # beside a member that chooses, only the filesystem channel is accepted
            assert isinstance(channel, FilesystemChannel)
            text = channel.member_instructions(member, choosers=True)
        else:
            text = channel.instructions(member)
        if text:
            parts.append(text)
    if isolation is not None:
        parts.append(isolation_preamble(isolation, member.name))
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
            f"A swarm cost cap or a sample cost limit requires cost data for every member's model, and {name} uses the default of its model parameter, which the swarm does not check. Give the member its model explicitly."
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
            f"A swarm cost cap or a sample cost limit requires cost data for every member's model, and {name} is given a model argument that is not a model name or Model, which the swarm cannot check. Give the member a model name or Model."
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


def _check_phase_names(names: Sequence[list[str]]) -> None:
    for name in (name for record_names in names for name in record_names):
        if name in PHASE_NAMES:
            raise ValueError(
                f"members=: a member is named {name!r}, which Inspect's timelines read as a phase of the sample. Give the member another name with member(..., name=...)."
            )


# Inspect's timelines lowercase span names, so members whose names differ only in case
# would share one lane name in the swarm's timeline.
def _check_case_names(names: Sequence[list[str]]) -> None:
    seen: dict[str, str] = {}
    for name in (name for record_names in names for name in record_names):
        other = seen.setdefault(name.lower(), name)
        if other != name:
            raise ValueError(
                f"members=: members {other!r} and {name!r} differ only in case, and Inspect's timelines lowercase member names, so the log viewer would show them as one. Give the members names that differ in more than case with member(..., name=...)."
            )


# Persistent members' agents, before any model call: a built agent must be a loop that
# can persist (a factory's is checked when the member builds it), and no persistent
# member is declared bridged, whatever its agent.
def _check_persistent(records: Sequence[Member], names: Sequence[list[str]]) -> None:
    for record, record_names in zip(records, names, strict=True):
        if not record.persistent:
            continue
        if record.agent is not None:
            refusal = persistent_refusal(record.agent, record_names[0])
            if refusal is not None:
                raise refusal
        if record.bridged:
            raise ValueError(
                f"Member {record_names[0]!r} is persistent and declared bridged. A bridged member's CLI runs its own turns, so it cannot be held after an answer; a persistent member's agent must be a react() or a deepagent() loop without background dispatch, or a builder that returns one."
            )


# A persistent member gives several submissions, one per answer, and the runtime keys
# and verifies a member's submission when it ends; until each answer is keyed and
# verified as it arrives, a contract that keys or verifies refuses persistent members.
def _check_result_members(
    result: ResultSpec | None, records: Sequence[Member], names: Sequence[list[str]]
) -> None:
    if result is None or (result.answer_key is None and result.verifier is None):
        return
    for record, record_names in zip(records, names, strict=True):
        if record.persistent:
            raise ValueError(
                f"Member {record_names[0]!r} is persistent, and the result contract has an answer_key or a verifier. The swarm does not yet key or verify each answer of a persistent member, so it refuses the two together: make the member not persistent, or leave answer_key and verifier out of the contract."
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
    # channels with records must not collide, and bridged members reach only the
    # built-in ones: swarm_bridged_tools() declares its tools before any channel opens
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
    custom = [
        c
        for c in channels
        if c.name is not None and not (is_builtin_messages(c) or is_builtin_board(c))
    ]
    if not custom:
        return
    for record, record_names in zip(records, names, strict=True):
        agent = record.agent if record.agent is not None else record.factory
        if is_bridged(cast(Agent, agent), record.bridged):
            raise ValueError(
                f"swarm(): member {record_names[0]!r} is bridged, and swarm_bridged_tools() serves only the messages and shared_memory channels, not {registry_info(custom[0]).name}. Leave the channel out, or the member."
            )


# A member that chooses its own name is never told the swarm's names, which channels
# with records show through their tools and the bus, and which a user channel's
# instructions may contain in a form no check can find. So beside one, only the
# built-in filesystem channel, whose instructions honour the flag, is accepted.
def _check_chosen_names(
    channels: Sequence[Channel], roster: Sequence[MemberInfo]
) -> None:
    chooser = next((info for info in roster if info.choose_name), None)
    if chooser is None:
        return
    for channel in channels:
        if type(channel) is not FilesystemChannel:
            raise ValueError(
                f"swarm(): member {chooser.name!r} chooses its own name (choose_name=True), which the swarm supports only with the filesystem channel or channels=[]; {registry_info(channel).name} is not supported with it. Leave the channel out, or set choose_name=False."
            )


# Whether the members have their own sandboxes. Then every member has one, no two
# share one or reach another's, and the filesystem channel must describe a shared
# volume rather than a shared sandbox; without them, it must not.
def _check_member_sandboxes(
    runs: Sequence[tuple[Member, str]], channels: Sequence[Channel]
) -> bool:
    sandboxes = {name: member_sandbox(record, name) for record, name in runs}
    own = [name for name, sandbox in sandboxes.items() if sandbox is not None]
    filesystems = [c for c in channels if isinstance(c, FilesystemChannel)]
    if not own:
        if any(c.shared_volume for c in filesystems):
            raise ValueError(
                "swarm(): filesystem(shared_volume=True) describes members with their own sandboxes that mount one shared volume, and these members share the sample's sandbox. Use filesystem() without shared_volume, or give each member its own sandbox with member(..., sandbox=...)."
            )
        return False
    shared = [name for name, sandbox in sandboxes.items() if sandbox is None]
    if shared:
        raise ValueError(
            f"swarm(): member {own[0]!r} has its own sandbox and {shared[0]!r} does not. Give every member its own sandbox, with member(..., sandbox=...), or none."
        )
    if any(not c.shared_volume for c in filesystems):
        raise ValueError(
            f"swarm(): the filesystem channel describes a sandbox the members share, and member {own[0]!r} has its own. If the members' sandboxes each mount one shared volume, use filesystem(shared_volume=True) with shared= where it is mounted. Otherwise leave the channel out, with channels=[] or other channels."
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


def _result(result: object) -> ResultSpec | None:
    # a log records the contract as its type name, which cannot rebuild its callables
    if result is None or isinstance(result, ResultSpec):
        return result
    if isinstance(result, str):
        raise TypeError(
            f"result= is {result!r}, the type name a log records for a result contract, which cannot be rebuilt from a log. Rebuild the swarm through its task, or through a registered @agent builder that passes result=."
        )
    raise TypeError(
        f"result= must be a ResultSpec from answer_result() or artifact_result(), got a {type(result).__name__}."
    )
