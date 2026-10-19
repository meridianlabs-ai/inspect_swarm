from contextlib import nullcontext
from typing import Any, Literal
from uuid import uuid4

import anyio
import pytest
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.model import (
    ChatMessageTool,
    ChatMessageUser,
    Model,
    ModelCost,
    ModelInfo,
    ModelOutput,
    execute_tools,
    get_model,
    set_model_info,
)
from inspect_ai.tool import Tool, tool
from inspect_ai.util import (
    Limit,
    LimitExceededError,
    cost_limit,
    message_limit,
    time_limit,
    token_limit,
    turn_limit,
)
from inspect_ai.util._limit import suspend_token_limit

from inspect_swarm import (
    Budget,
    Controller,
    MemberHandle,
    StopReason,
    SwarmControl,
    member,
    swarm,
    swarm_controller,
)
from inspect_swarm._budget import CostPair
from inspect_swarm._controller._control import SwarmRun

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Script,
    generator,
    run_swarm,
    sample,
    scripted_model,
    sleeper,
    statuses,
)
from .test_budget import _bridge_generate, _GenerateCompaction


def _controller(run: Any, name: str) -> Controller:
    @swarm_controller(name=name)
    def factory() -> Controller:
        return Controller(run)

    return factory()


# ---------------------------------------------------------------------------
# Member limits


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limit,calls,expected",
    [
        (lambda: token_limit(2 * TOKENS_PER_CALL), None, "member_limit:token"),
        # the metering type is kept: these calls use no input tokens
        (lambda: token_limit(1, type="input"), 3, "submitted"),
        (lambda: cost_limit(1.5), None, "member_limit:cost"),
        (lambda: turn_limit(2), None, "member_limit:turn"),
        (lambda: message_limit(4), None, "member_limit:message"),
        (lambda: time_limit(0.05), None, "member_limit:time"),
    ],
)
async def test_member_limits_are_recreated_for_each_member_and_run(
    limit: Any, calls: int | None, expected: str
) -> None:
    model = scripted_model(Script(delay=0.001), cost=1.0)
    agent_swarm = swarm(
        member(
            generator,
            args={"model": model, "calls": calls},
            count=3,
            limits=[limit()],
        ),
        channels=[],
    )
    # the same swarm runs twice, as for a second sample: no limit object is reused
    for _ in range(2):
        stop = await run_swarm(agent_swarm)
        assert stop.reason == "all_done"
        assert set(statuses(stop).values()) == {expected}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sample_limit,member_limit,expected",
    [
        pytest.param(1, 10, "submitted", id="member-limit-above-sample"),
        pytest.param(10, 1, "member_limit:message", id="member-limit-below-sample"),
        pytest.param(10, 10, "submitted", id="both-above"),
    ],
)
async def test_a_member_message_limit_applies_from_its_first_message(
    sample_limit: int, member_limit: int, expected: str
) -> None:
    # each member starts with two messages (the input and its preamble) and adds none:
    # its own message limit, not the sample's, decides whether that is allowed
    stop = await run_swarm(
        swarm(
            [
                member(
                    generator(scripted_model(), calls=0),
                    name="limited",
                    limits=[message_limit(member_limit)],
                ),
                member(
                    generator(scripted_model(), calls=0),
                    name="sibling",
                    limits=[message_limit(10)],
                ),
            ],
            channels=[],
        ),
        messages=sample_limit,
    )
    assert statuses(stop) == {"limited": expected, "sibling": "submitted"}


# ---------------------------------------------------------------------------
# Stopping


@pytest.mark.anyio
async def test_the_time_cap_stops_members_softly() -> None:
    stop = await run_swarm(
        swarm(
            generator(scripted_model(Script(delay=0.01)), calls=None),
            channels=[],
            budget=Budget(time=0.05),
        )
    )
    assert stop.reason == "swarm_time"
    assert statuses(stop) == {"generator": "stopped"}


@pytest.mark.anyio
async def test_grace_is_clipped_to_half_the_samples_remaining_time() -> None:
    # stopped at 0.1 s with 0.5 s of the sample left: 0.25 s of grace, not 100 s, so
    # the hard stop comes before the sample's time limit
    stop = await run_swarm(
        swarm(sleeper(), channels=[], budget=Budget(time=0.1, grace=100)),
        time=0.6,
    )
    assert stop.reason == "swarm_time"
    assert statuses(stop) == {"sleeper": "cancelled"}


@pytest.mark.anyio
async def test_the_first_stop_wins() -> None:
    # two members reach the cap, and the time cap fires during grace
    spender = member(
        generator, args={"model": scripted_model(), "calls": None}, count=2
    )
    stop = await run_swarm(
        swarm(
            [spender, sleeper()],
            channels=[],
            budget=Budget(tokens=2 * TOKENS_PER_CALL, time=0.1, grace=0.3),
        )
    )
    assert stop.reason == "swarm_cap"
    assert statuses(stop) == {
        "generator-1": "swarm_cap",
        "generator-2": "swarm_cap",
        "sleeper": "cancelled",
    }


@agent
def approver(model: Model) -> Agent:
    # model calls an approval or review callback makes, with token metering suspended
    async def execute(state: AgentState) -> AgentState:
        with suspend_token_limit():
            await model.generate("approve?")
            await model.generate("approve again?")
        return state

    return execute


@pytest.mark.anyio
async def test_the_soft_stop_refuses_the_next_suspended_call() -> None:
    gate = anyio.Event()
    script = Script(gate=gate)

    async def run(control: SwarmControl) -> StopReason:
        control.start("approver")
        while script.started < 1:
            await anyio.sleep(0)
        gate.set()
        return "done"

    stop = await run_swarm(
        swarm(
            approver(scripted_model(script, cost=1.0)),
            controller=_controller(run, "limits_test_approval"),
            channels=[],
        )
    )
    assert statuses(stop) == {"approver": "stopped"}
    # the dispatched call completed; the next was refused by the lever's stop gate
    assert (script.started, script.calls) == (1, 1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "controller_calls,expected", [(0, "submitted"), (1, "swarm_cap")]
)
async def test_a_controllers_model_calls_count_against_the_swarm_cap(
    controller_calls: int, expected: str
) -> None:
    model = scripted_model(cost=1.0)

    async def run(control: SwarmControl) -> StopReason:
        for _ in range(controller_calls):
            await model.generate("plan")
        control.start("generator")
        async for _ in control.events():
            pass
        return "all_done"

    stop = await run_swarm(
        swarm(
            generator(model, calls=1),
            controller=_controller(run, f"limits_test_controller_{controller_calls}"),
            channels=[],
            budget=Budget(cost=1.5),
        )
    )
    assert statuses(stop) == {"generator": expected}


@pytest.mark.anyio
@pytest.mark.parametrize("staged", [False, True])
async def test_the_runtime_lever_and_member_levers_are_siblings(staged: bool) -> None:
    model = scripted_model(cost=10.0)

    async def run(control: SwarmControl) -> StopReason:
        await model.generate("plan")
        for m in control.members:
            control.start(m)
            if staged:
                async for _ in control.events():
                    pass
        async for _ in control.events():
            pass
        return "all_done"

    record = member(generator, args={"model": model}, count=2)
    cap = CostPair(100.0, owner=None, role="cap")
    async with sample():
        swarm_run = SwarmRun(
            [(record, "a"), (record, "b")],
            state=AgentState(messages=[]),
            input=[ChatMessageUser(content="task")],
            preambles={"a": "", "b": ""},
            caps=cap.nodes,
            time=None,
            grace=1.0,
            budget=Budget(),
        )
        await swarm_run.run(run)
    # each member's call is charged to its own lever and the cap, never the runtime's
    assert [m.lever.cost for m in swarm_run._members] == [10.0, 10.0]
    assert swarm_run._runtime_lever.cost == 10.0
    assert cap.cost == 30.0


# ---------------------------------------------------------------------------
# Errors from members: the wrapper classifies every leaf


@agent
def grouped(model: Model) -> Agent:
    # generates inside a task group of its own, so its errors arrive grouped
    async def execute(state: AgentState) -> AgentState:
        async def generate() -> None:
            while True:
                await model.generate(state.messages)

        async with anyio.create_task_group() as tg:
            tg.start_soon(generate)
        return state

    return execute


@tool
def spend(model: Model) -> Tool:
    async def execute() -> str:
        """Ask the model."""
        while True:
            await model.generate("again")

    return execute


@agent
def tool_loop(model: Model) -> Agent:
    # the cap is reached in a tool, which returns it as a tool error
    async def execute(state: AgentState) -> AgentState:
        tools = [spend(model)]
        call = ModelOutput.for_tool_call("mockllm", "spend", {})
        state.messages.append(call.message)
        result = await execute_tools(state.messages, tools)
        state.messages.extend(result.messages)
        [reply] = result.messages
        assert isinstance(reply, ChatMessageTool) and reply.error is not None
        assert reply.error.type == "limit"
        await model.generate(state.messages)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize(
    "agent_factory,limits,budget,expected",
    [
        # the member's own limit, bare and grouped
        (generator, [token_limit(TOKENS_PER_CALL)], Budget(), "member_limit:token"),
        (grouped, [token_limit(TOKENS_PER_CALL)], Budget(), "member_limit:token"),
        # the swarm cap, bare, grouped and after a tool error
        (generator, [], Budget(tokens=TOKENS_PER_CALL), "swarm_cap"),
        (grouped, [], Budget(tokens=TOKENS_PER_CALL), "swarm_cap"),
        (tool_loop, [], Budget(tokens=TOKENS_PER_CALL), "swarm_cap"),
    ],
)
async def test_the_swarms_own_errors_become_statuses(
    agent_factory: Any, limits: list[Limit], budget: Budget, expected: str
) -> None:
    kwargs: dict[str, Any] = {"calls": None} if agent_factory is generator else {}
    stop = await run_swarm(
        swarm(
            [
                member(
                    agent_factory(scripted_model(), **kwargs),
                    name="member",
                    limits=limits,
                ),
                # a sibling with nothing to do but return
                member(generator(scripted_model(), calls=0), name="sibling"),
            ],
            channels=[],
            budget=budget,
        )
    )
    assert stop.reason == ("swarm_cap" if expected == "swarm_cap" else "all_done")
    assert statuses(stop) == {"member": expected, "sibling": "submitted"}


@pytest.mark.anyio
async def test_a_grouped_lever_stop_gives_the_same_status_as_a_bare_one() -> None:
    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            return "done"
        raise AssertionError("no member ended")

    stop = await run_swarm(
        swarm(
            [
                member(grouped(scripted_model(Script(delay=0.001))), name="grouped"),
                member(
                    generator(scripted_model(Script(delay=0.001)), calls=None),
                    name="bare",
                ),
                member(generator(scripted_model(), calls=0), name="quick"),
            ],
            controller=_controller(run, "limits_test_lever_grouped"),
            channels=[],
        )
    )
    assert statuses(stop) == {
        "grouped": "stopped",
        "bare": "stopped",
        "quick": "submitted",
    }


@agent
def mixed(model: Model) -> Agent:
    # captures a real limit error from the swarm's nodes, then raises it in a group
    # with a foreign error
    async def execute(state: AgentState) -> AgentState:
        try:
            while True:
                await model.generate(state.messages)
        except LimitExceededError as limit:
            raise ExceptionGroup("mixed", [limit, RuntimeError("foreign")]) from None

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limits,budget",
    [
        pytest.param([token_limit(TOKENS_PER_CALL)], Budget(), id="member-limit"),
        pytest.param([], Budget(tokens=TOKENS_PER_CALL), id="swarm-cap"),
    ],
)
async def test_a_group_mixing_the_swarms_error_with_a_foreign_one_raises_only_the_foreign_leaf(
    limits: list[Limit], budget: Budget
) -> None:
    with pytest.raises(RuntimeError, match="foreign") as raised:
        await run_swarm(
            swarm(
                member(mixed(scripted_model()), limits=limits),
                channels=[],
                budget=budget,
            )
        )
    # the swarm's leaf was removed, so the foreign one escapes bare, not as a group
    assert type(raised.value) is RuntimeError


@tool
def ask(model: Model, suspended: bool = False) -> Tool:
    async def execute() -> str:
        """Ask the model once."""
        with suspend_token_limit() if suspended else nullcontext():
            return (await model.generate("question")).completion

    return execute


async def call_ask(model: Model, suspended: bool) -> ChatMessageTool:
    # one call of the ask tool, whose errors execute_tools returns as a tool message
    call = ModelOutput.for_tool_call("mockllm", "ask", {})
    result = await execute_tools([call.message], [ask(model, suspended)])
    [reply] = result.messages
    assert isinstance(reply, ChatMessageTool)
    return reply


@agent
def asker(model: Model, suspended: bool = False) -> Agent:
    # its last act is a tool call; it returns normally whatever the tool returned
    async def execute(state: AgentState) -> AgentState:
        state.messages.append(await call_ask(model, suspended))
        return state

    return execute


# Caps a single 50-token, $1 call reaches, and budgets it stays within.
_CAP_CASES = {
    "token cap": (Budget(tokens=TOKENS_PER_CALL // 2, grace=0.05), False),
    "cost cap": (Budget(cost=0.5, grace=0.05), False),
    "cost cap, suspended": (Budget(cost=0.5, grace=0.05), True),
    "below the caps": (Budget(tokens=10 * TOKENS_PER_CALL, cost=10.0), False),
}


def _waiting_sibling() -> Agent:
    # priced, and still in its call when a stop's short grace ends
    return generator(scripted_model(Script(delay=0.5), cost=0.0))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case", [*_CAP_CASES, "member limit"], ids=lambda c: c.replace(" ", "-")
)
async def test_a_cap_reached_in_a_members_last_tool_call_stops_the_swarm(
    case: str,
) -> None:
    # the tool returns the cap's error as a message and the member returns normally,
    # so only the cap itself can begin the stop
    budget, suspended = _CAP_CASES.get(case, (Budget(), False))
    limits = [token_limit(TOKENS_PER_CALL // 2)] if case == "member limit" else []
    stop = await run_swarm(
        swarm(
            [
                member(
                    asker(scripted_model(cost=1.0), suspended=suspended),
                    name="asker",
                    limits=limits,
                ),
                member(_waiting_sibling(), name="sibling"),
            ],
            channels=[],
            budget=budget,
        )
    )
    if case in ("below the caps", "member limit"):
        assert stop.reason == "all_done"
        assert statuses(stop) == {"asker": "submitted", "sibling": "submitted"}
    else:
        assert stop.reason == "swarm_cap"
        assert statuses(stop) == {"asker": "submitted", "sibling": "cancelled"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    ["token cap", "cost cap", "below the caps"],
    ids=lambda c: c.replace(" ", "-"),
)
async def test_a_cap_reached_in_a_controllers_tool_call_stops_the_swarm(
    case: str,
) -> None:
    budget, suspended = _CAP_CASES[case]
    model = scripted_model(cost=1.0)
    errors: list[str | None] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("sibling")
        reply = await call_ask(model, suspended)
        errors.append(reply.error.type if reply.error is not None else None)
        async for _ in control.events():
            pass
        return "all_done"

    stop = await run_swarm(
        swarm(
            member(_waiting_sibling(), name="sibling"),
            controller=_controller(
                run, f"limits_test_controller_tool_{case.replace(' ', '_')}"
            ),
            channels=[],
            budget=budget,
        )
    )
    if case == "below the caps":
        assert (stop.reason, errors) == ("all_done", [None])
        assert statuses(stop) == {"sibling": "submitted"}
    else:
        assert stop.reason == "swarm_cap"
        assert statuses(stop) == {"sibling": "cancelled"}


@pytest.mark.anyio
@pytest.mark.parametrize("form", ["built", "factory"])
@pytest.mark.parametrize("priced", ["name", "canonical"])
async def test_the_cost_check_looks_a_model_up_as_metering_does(
    form: str, priced: str
) -> None:
    # a live model (a factory's arg) is found under its name or its canonical name, as
    # metering finds it; a built member's model is known only by its logged name
    model = get_model(f"mockllm/m-{uuid4().hex}", memoize=False)
    assert model.canonical_name() != str(model)
    set_model_info(
        str(model) if priced == "name" else model.canonical_name(),
        ModelInfo(
            cost=ModelCost(input=0, output=1, input_cache_write=0, input_cache_read=0)
        ),
    )
    record = (
        member(generator(model))
        if form == "built"
        else member(generator, args={"model": model})
    )
    agent_swarm = swarm(record, channels=[], budget=Budget(cost=10.0))
    if form == "built" and priced == "canonical":
        with pytest.raises(PrerequisiteError, match="use the member's factory form"):
            await run_swarm(agent_swarm)
    else:
        stop = await run_swarm(agent_swarm)
        assert statuses(stop) == {"generator": "submitted"}


@pytest.mark.anyio
@pytest.mark.parametrize("form", ["built", "factory"])
async def test_the_cost_check_refuses_an_unpriced_model(form: str) -> None:
    model = scripted_model()
    record = (
        member(generator(model))
        if form == "built"
        else member(generator, args={"model": model})
    )
    with pytest.raises(PrerequisiteError, match="Missing cost data"):
        await run_swarm(swarm(record, channels=[], budget=Budget(cost=10.0)))


@agent
def default_model_generator(model: str = "mockllm/unpriced-default") -> Agent:
    # a factory whose model has a default other than None, which no registered agent has
    return generator(get_model(model, memoize=False))


@pytest.mark.anyio
@pytest.mark.parametrize("form", ["built", "factory"])
@pytest.mark.parametrize("model", ["default", "explicit"])
async def test_the_cost_check_refuses_a_member_using_its_default_model(
    form: str, model: str
) -> None:
    args = {"model": str(scripted_model(cost=1.0))} if model == "explicit" else {}
    record = (
        member(default_model_generator(**args))
        if form == "built"
        else member(default_model_generator, args=args)
    )
    agent_swarm = swarm(record, channels=[], budget=Budget(cost=10.0))
    if model == "default":
        with pytest.raises(PrerequisiteError, match="default of its model parameter"):
            await run_swarm(agent_swarm)
    else:
        stop = await run_swarm(agent_swarm)
        assert statuses(stop) == {"default_model_generator": "submitted"}


@agent
def delegator(model: Agent | Model) -> Agent:
    # react's model may be an agent it delegates its calls to
    if isinstance(model, Model):
        return generator(model)
    return model


@pytest.mark.anyio
@pytest.mark.parametrize("form", ["built", "factory"])
@pytest.mark.parametrize("model", ["agent", "model"])
async def test_the_cost_check_refuses_a_model_argument_it_cannot_price(
    form: str, model: str
) -> None:
    priced = scripted_model(cost=1.0)
    args = {"model": generator(priced) if model == "agent" else priced}
    record = (
        member(delegator(**args)) if form == "built" else member(delegator, args=args)
    )
    agent_swarm = swarm(record, channels=[], budget=Budget(cost=10.0))
    if model == "agent":
        with pytest.raises(PrerequisiteError, match="not a model name or Model"):
            await run_swarm(agent_swarm)
    else:
        stop = await run_swarm(agent_swarm)
        assert statuses(stop) == {"delegator": "submitted"}


@pytest.mark.parametrize("split", ["pool", "equal", (1.0, 2.0)])
def test_shares_are_refused_until_they_are_supported(
    split: Literal["pool", "equal"] | tuple[float, ...],
) -> None:
    members = member(generator, args={"model": scripted_model()}, count=2)
    budget = Budget(tokens=300, split=split)
    if split == "pool":
        swarm(members, channels=[], budget=budget)
    else:
        with pytest.raises(NotImplementedError, match="Budget.split"):
            swarm(members, channels=[], budget=budget)


@agent
def bridged_builder(model: Model, raised: list[str]) -> Agent:
    # a bridged agent outside inspect_swe, whose first request's bridge compaction
    # generates before the bridge's mark
    async def execute(state: AgentState) -> AgentState:
        try:
            await _bridge_generate(model, _GenerateCompaction(model, []))
        except LimitExceededError:
            # in a sandboxed sample this raise would reach the sandbox service and end
            # the sample
            raised.append("raised")
            raise
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("bridged", [True, None])
async def test_a_member_declared_bridged_is_cancelled_at_its_first_compaction(
    bridged: bool | None,
) -> None:
    raised: list[str] = []
    stop = await run_swarm(
        swarm(
            member(
                bridged_builder(scripted_model(), raised),
                name="cli",
                bridged=bridged,
            ),
            channels=[],
            budget=Budget(tokens=TOKENS_PER_CALL // 2),
        )
    )
    assert stop.reason == "swarm_cap"
    assert statuses(stop) == {"cli": "swarm_cap"}
    # declared, it is cancelled with no error raised in its code; undeclared, it is
    # counted native until the bridge's mark and the cap's error is raised
    assert raised == ([] if bridged else ["raised"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limits,budget,expected",
    [
        pytest.param([], Budget(tokens=TOKENS_PER_CALL // 2), "swarm_cap", id="cap"),
        pytest.param(
            [token_limit(TOKENS_PER_CALL // 2)],
            Budget(),
            "member_limit:token",
            id="member-limit",
        ),
        pytest.param([], Budget(), "submitted", id="control"),
    ],
)
async def test_a_bridged_member_stopped_by_its_last_call_is_not_submitted(
    limits: list[Limit], budget: Budget, expected: str
) -> None:
    # the node cancels the member rather than raising, and the member returns before
    # the cancellation reaches it
    handles: list[MemberHandle] = []

    async def run(control: SwarmControl) -> StopReason:
        handles.append(control.member("generator"))
        control.start("generator")
        async for _ in control.events():
            pass
        return "all_done"

    stop = await run_swarm(
        swarm(
            member(generator(scripted_model()), bridged=True, limits=limits),
            controller=_controller(
                run, f"limits_test_last_call_{expected.replace(':', '_')}"
            ),
            channels=[],
            budget=budget,
        )
    )
    assert statuses(stop) == {"generator": expected}
    [handle] = handles
    assert (handle.status, handle.submitted) == (expected, expected == "submitted")
