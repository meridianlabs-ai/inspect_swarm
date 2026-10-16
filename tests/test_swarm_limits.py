from typing import Any

import anyio
import pytest
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.model import (
    ChatMessageTool,
    ChatMessageUser,
    Model,
    ModelOutput,
    execute_tools,
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
        assert stop["reason"] == "all_done"
        assert set(statuses(stop).values()) == {expected}


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
    assert stop["reason"] == "swarm_time"
    assert statuses(stop) == {"generator": "stopped"}


@pytest.mark.anyio
async def test_grace_is_clipped_to_half_the_samples_remaining_time() -> None:
    # stopped at 0.1 s with 0.5 s of the sample left: 0.25 s of grace, not 100 s, so
    # the hard stop comes before the sample's time limit
    stop = await run_swarm(
        swarm(sleeper(), channels=[], budget=Budget(time=0.1, grace=100)),
        time=0.6,
    )
    assert stop["reason"] == "swarm_time"
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
    assert stop["reason"] == "swarm_cap"
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
            input=[ChatMessageUser(content="task")],
            preambles={"a": "", "b": ""},
            caps=cap.nodes,
            time=None,
            grace=1.0,
        )
        await swarm_run.run(run)
    # each member's call is charged to its own lever and the cap, never the runtime's
    assert [m.lever.cost for m in swarm_run._members] == [10.0, 10.0]
    assert swarm_run._runtime_lever.cost == 10.0
    assert cap.cost == 30.0


# ---------------------------------------------------------------------------
# Errors from members: the wrapper classifies every leaf


@agent
def grouped(model: Model, crash: bool = False) -> Agent:
    # generates inside a task group of its own, so its errors arrive grouped
    async def execute(state: AgentState) -> AgentState:
        async def generate() -> None:
            while True:
                await model.generate(state.messages)

        async def fail() -> None:
            await anyio.sleep(0.01)
            raise RuntimeError("foreign")

        async with anyio.create_task_group() as tg:
            tg.start_soon(generate)
            if crash:
                tg.start_soon(fail)
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
    assert stop["reason"] == ("swarm_cap" if expected == "swarm_cap" else "all_done")
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


@pytest.mark.anyio
async def test_a_group_mixing_an_own_limit_and_a_foreign_error_raises_the_foreign_leaf() -> (
    None
):
    with pytest.raises(RuntimeError, match="foreign") as raised:
        await run_swarm(
            swarm(
                member(
                    grouped(scripted_model(Script(delay=0.02)), crash=True),
                    limits=[token_limit(TOKENS_PER_CALL)],
                ),
                channels=[],
            )
        )
    assert not isinstance(raised.value, LimitExceededError)
