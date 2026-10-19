from collections.abc import Callable, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.exception import TerminateSampleError
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent
from inspect_ai.log._samples import sample_active
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
    sample_limits,
    store,
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
from inspect_swarm._budget import CostPair, SwarmNode
from inspect_swarm._controller._control import SwarmRun
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._record import RECORD_KEY, SwarmResultRecord

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
        # the member's share, grouped and after a tool error, below the cap
        (grouped, [], Budget(tokens=4 * TOKENS_PER_CALL, split="equal"), "share"),
        (tool_loop, [], Budget(tokens=4 * TOKENS_PER_CALL, split="equal"), "share"),
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


# Foreign errors a member can raise, by kind.
_FOREIGN: dict[str, Callable[[], Exception]] = {
    "sample_limit": lambda: LimitExceededError(
        "token", value=1, limit=1, source=sample_limits().token
    ),
    "terminate": lambda: TerminateSampleError("terminated"),
    "crash": lambda: RuntimeError("foreign"),
}


@agent
def mixed(model: Model, foreign: Sequence[str] = ("crash",)) -> Agent:
    # captures a real limit error from the swarm's nodes, then raises it in a group
    # with foreign errors
    async def execute(state: AgentState) -> AgentState:
        try:
            while True:
                await model.generate(state.messages)
        except LimitExceededError as limit:
            errors = [_FOREIGN[kind]() for kind in foreign]
            raise ExceptionGroup("mixed", [limit, *errors]) from None

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


# ---------------------------------------------------------------------------
# Exhaustion: foreign errors and limits from outside the swarm


def _ledger() -> SwarmLedger:
    return SwarmLedger.model_validate(store().get(LEDGER_KEY))


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        return [leaf for e in group.exceptions for leaf in _leaves(e)]
    return [error]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "foreign,ended_by",
    [
        (["sample_limit"], "sample_limit:token"),
        (["terminate"], None),
        (["crash"], None),
        (["sample_limit", "crash"], "sample_limit:token"),
    ],
)
async def test_foreign_errors_propagate_without_the_swarms_leaves(
    foreign: list[str], ended_by: str | None
) -> None:
    async with sample():
        with pytest.raises(Exception) as raised:
            await swarm(
                member(mixed(scripted_model(), foreign=foreign)),
                channels=[],
                budget=Budget(tokens=TOKENS_PER_CALL),
            )(AgentState(messages=[ChatMessageUser(content="task")]))
        ledger = _ledger()
        expected = [type(_FOREIGN[kind]()) for kind in foreign]
    # without the cap's leaf; a single leaf bare, as collect() raises it
    assert [type(e) for e in _leaves(raised.value)] == expected
    assert isinstance(raised.value, ExceptionGroup) == (len(expected) > 1)
    # the cap began a stop, and a sample limit then ended the swarm
    assert (ledger.stop_reason, ledger.ended_by, ledger.interrupted) == (
        "swarm_cap",
        ended_by,
        True,
    )
    assert [m.status for m in ledger.members] == ["errored"]


@agent
def catcher(model: Model, caught: list[LimitExceededError]) -> Agent:
    # keeps the error its lever raised at the soft stop
    async def execute(state: AgentState) -> AgentState:
        try:
            while True:
                await model.generate(state.messages)
        except LimitExceededError as ex:
            caught.append(ex)
            raise

    return execute


@agent
def carrier(caught: list[LimitExceededError]) -> Agent:
    # raises another member's limit error in a group with a foreign one
    async def execute(state: AgentState) -> AgentState:
        while not caught:
            await anyio.sleep(0.001)
        raise ExceptionGroup("carried", [caught[0], RuntimeError("foreign")])

    return execute


@pytest.mark.anyio
async def test_the_runtime_removes_a_swarm_leaf_its_wrapper_did_not_own() -> None:
    # the carrier's wrapper owns neither leaf, so both reach the runtime, which
    # removes the other member's
    caught: list[LimitExceededError] = []

    async def run_members(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        return "enough"

    with pytest.raises(RuntimeError, match="foreign") as raised:
        await run_swarm(
            swarm(
                [
                    member(
                        catcher(scripted_model(Script(delay=0.001)), caught),
                        name="stopped",
                    ),
                    member(carrier(caught), name="carrier"),
                ],
                controller=_controller(run_members, "limits_test_carrier"),
                channels=[],
            )
        )
    assert type(raised.value) is RuntimeError
    [lever_stop] = caught
    assert lever_stop.type == "custom"


@agent
def interrupter() -> Agent:
    # the operator interrupts the sample, to be scored, while the member works
    async def execute(state: AgentState) -> AgentState:
        active = sample_active()
        assert active is not None
        active.interrupt("score")
        await anyio.sleep(3600)
        return state

    return execute


def _busy(limit: str) -> Agent:
    # the member still working when the sample's limit or the operator ends it
    match limit:
        case "message":
            return generator(scripted_model(Script(delay=0.01)), calls=None)
        case "operator":
            return interrupter()
        case _:
            return sleeper()


@pytest.mark.parametrize("limit", ["message", "time", "working", "operator"])
def test_a_sample_limit_leaves_the_ledger_and_the_provisional_answer(
    tmp_path: Path, limit: str
) -> None:
    async def run_members(control: SwarmControl) -> StopReason:
        control.start("early")
        async for _ in control.events():
            pass
        control.start("busy")
        # ends only when the busy member does, by the sample's message limit
        async for _ in control.events():
            pass
        return "all_done"

    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            [
                member(
                    generator(scripted_model(), calls=1, answer="early answer"),
                    name="early",
                ),
                member(_busy(limit), name="busy"),
            ],
            controller=_controller(run_members, f"limits_test_exhaustion_{limit}"),
            channels=[],
            # a time cap above the sample's limit, which ends the swarm first
            budget=Budget(time=60) if limit == "time" else Budget(),
        ),
        message_limit=6 if limit == "message" else None,
        time_limit=1 if limit == "time" else None,
        working_limit=1 if limit == "working" else None,
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.samples is not None
    [sample] = log.samples
    assert sample.limit is not None and sample.limit.type == limit
    # the provisional answer
    assert sample.output.completion == "early answer"
    record = SwarmResultRecord.model_validate(sample.store[RECORD_KEY])
    assert (record.final.member, record.final.provisional) == ("early", True)
    # the ledger
    ledger = SwarmLedger.model_validate(sample.store[LEDGER_KEY])
    if limit != "message":
        # no stop began; the busy member that raised the message limit has ended, which
        # can reach the controller first
        assert ledger.stop_reason is None
    assert ledger.ended_by == (
        "operator" if limit == "operator" else f"sample_limit:{limit}"
    )
    assert ledger.interrupted and not ledger.total_complete
    assert {m.name: m.status for m in ledger.members} == {
        "early": "submitted",
        # a limit it raised itself is a foreign error; the others cancel it
        "busy": "errored" if limit == "message" else "cancelled",
    }
    assert [
        e
        for e in sample.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("type") == "ledger"
    ]


@agent
def cleanup_crash() -> Agent:
    # works until cancelled, and its cleanup then fails
    async def execute(state: AgentState) -> AgentState:
        try:
            await anyio.sleep_forever()
        finally:
            raise RuntimeError("cleanup failed")

    return execute


@pytest.mark.anyio
async def test_a_cleanup_error_after_a_sample_limit_records_an_unknown_end() -> None:
    # the error and the sample's limit cannot be ordered from what they show, so
    # neither is recorded as the end; Inspect records the crash
    ledgers: list[SwarmLedger] = []
    # bare on asyncio, grouped with the cancellation on trio
    with pytest.raises(BaseException) as raised:
        async with sample(time=0.05):
            try:
                await swarm(
                    member(cleanup_crash()), channels=[], budget=Budget(time=60)
                )(AgentState(messages=[ChatMessageUser(content="task")]))
            finally:
                ledgers.append(_ledger())
    # the cleanup error still propagates
    assert "cleanup failed" in [str(e) for e in _leaves(raised.value)]
    [ledger] = ledgers
    assert (ledger.stop_reason, ledger.ended_by, ledger.interrupted) == (
        None,
        "unknown",
        True,
    )


@pytest.mark.anyio
async def test_a_cancellation_the_sample_does_not_explain_records_an_unknown_end() -> (
    None
):
    ledgers: list[SwarmLedger] = []
    async with sample():
        with anyio.move_on_after(0.01):
            try:
                await swarm(sleeper(), channels=[])(
                    AgentState(messages=[ChatMessageUser(content="task")])
                )
            finally:
                ledgers.append(_ledger())
    [ledger] = ledgers
    assert (ledger.stop_reason, ledger.ended_by, ledger.interrupted) == (
        None,
        "unknown",
        True,
    )


@agent
def limited_cleanup(model: Model, owned: list[LimitExceededError]) -> Agent:
    # reaches its own limit in one task, still cleaning up when the sample's time limit
    # cancels it, while another task's cleanup fails
    async def execute(state: AgentState) -> AgentState:
        async def limited() -> None:
            try:
                await model.generate(state.messages)
            except LimitExceededError as ex:
                owned.append(ex)
                raise
            finally:
                # until the sample's time limit has cancelled the other task
                deadline = sample_limits().time
                assert deadline.limit is not None
                with anyio.CancelScope(shield=True):
                    while deadline.usage < deadline.limit + 0.05:
                        await anyio.sleep(0.005)

        async def failing_cleanup() -> None:
            try:
                await anyio.sleep_forever()
            finally:
                raise RuntimeError("cleanup failed")

        async with anyio.create_task_group() as tg:
            tg.start_soon(limited)
            tg.start_soon(failing_cleanup)
            await anyio.sleep_forever()
        return state

    return execute


@pytest.mark.anyio
async def test_a_cancellation_grouped_with_errors_loses_the_swarms_leaves() -> None:
    # on trio the cancellation stays in the member's group, which its wrapper does not
    # catch, so the runtime removes the member's limit
    owned: list[LimitExceededError] = []
    with pytest.raises(BaseException) as raised:
        async with sample(time=0.2):
            await swarm(
                member(
                    limited_cleanup(scripted_model(), owned),
                    limits=[token_limit(1)],
                ),
                channels=[],
                budget=Budget(time=60),
            )(AgentState(messages=[ChatMessageUser(content="task")]))
    # the member's limit was raised before the sample's time limit
    [limit] = owned
    assert isinstance(limit.source, SwarmNode)
    leaves = _leaves(raised.value)
    assert "cleanup failed" in [str(e) for e in leaves]
    assert not [
        e
        for e in leaves
        if isinstance(e, LimitExceededError) and isinstance(e.source, SwarmNode)
    ]


def test_the_swarms_messages_stay_within_a_small_sample_message_limit(
    tmp_path: Path,
) -> None:
    # each member's conversation fits the limit: the input, its preamble and two
    # replies; all three together, or with the swarm's, would not
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            member(generator, args={"model": scripted_model(), "calls": 2}, count=3),
            channels=[],
        ),
        message_limit=5,
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.samples is not None
    [sample] = log.samples
    assert sample.limit is None
    assert [m.role for m in sample.messages] == ["user", "assistant"]


@pytest.mark.anyio
async def test_the_ledger_is_written_after_the_final_step() -> None:
    # so that the final meter it reports covers the final step
    stop = await run_swarm(swarm(generator(scripted_model()), channels=[]))
    kinds = [
        e.data.get("kind", e.data.get("type"))
        for e in stop.transcript.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
    ]
    assert kinds[-2:] == ["final", "ledger"]


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


# ---------------------------------------------------------------------------
# Splits


@pytest.mark.anyio
@pytest.mark.parametrize(
    "budget, sample_tokens, calls",
    [
        # 100 tokens each
        (Budget(tokens=6 * TOKENS_PER_CALL, split="equal"), None, (1, 2, 2)),
        # the same cap derived from the sample's token limit
        (Budget(reserve=0.0, split="equal"), 6 * TOKENS_PER_CALL, (1, 2, 2)),
        # 100, 100 and 200 tokens
        (Budget(tokens=8 * TOKENS_PER_CALL, split=(1, 1, 2)), None, (1, 2, 4)),
        # $2, $2 and $8, at $1 a call
        (Budget(cost=12.0, split=(1, 1, 4)), None, (1, 2, 8)),
        # the same with weights large enough to overflow cap * weight
        (Budget(cost=12.0, split=(2.0**1020, 2.0**1020, 2.0**1022)), None, (1, 2, 8)),
    ],
)
async def test_members_stop_on_their_own_shares(
    budget: Budget, sample_tokens: int | None, calls: tuple[int, int, int]
) -> None:
    # "quitter" answers after one call and leaves the rest of its share unused; the
    # spenders generate until stopped, and neither gets what quitter left
    names = ["quitter", "spender-1", "spender-2"]
    scripts = [Script() for _ in names]
    stop = await run_swarm(
        swarm(
            [
                member(
                    generator(
                        scripted_model(script, cost=1.0),
                        calls=1 if name == "quitter" else None,
                    ),
                    name=name,
                )
                for name, script in zip(names, scripts, strict=True)
            ],
            channels=[],
            budget=budget,
        ),
        tokens=sample_tokens,
    )
    assert stop.reason == "all_done"
    assert statuses(stop) == {
        "quitter": "submitted",
        "spender-1": "share",
        "spender-2": "share",
    }
    assert tuple(script.calls for script in scripts) == calls


@pytest.mark.anyio
async def test_the_swarm_cap_wins_over_a_share_reached_at_the_same_check() -> None:
    # two members, each with half the cap; the second reaches its share and the cap
    # together, and the cap stops the swarm
    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
            async for _ in control.events():
                pass
        return "all_done"

    stop = await run_swarm(
        swarm(
            member(generator, args={"model": scripted_model(), "calls": None}, count=2),
            controller=_controller(run, "limits_test_staged_shares"),
            channels=[],
            budget=Budget(tokens=4 * TOKENS_PER_CALL, split="equal"),
        )
    )
    assert stop.reason == "swarm_cap"
    assert statuses(stop) == {"generator-1": "share", "generator-2": "swarm_cap"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "budget, match",
    [
        # the sample has no cost or token limit to derive a cap from
        (Budget(split="equal"), "neither"),
        (Budget(tokens=1, split="equal"), "token cap of 1 .* 2 members"),
        (Budget(tokens=300, split=(1, 2, 3)), "3 weights but the swarm has 2 members"),
    ],
)
async def test_a_split_the_swarm_cannot_make_is_refused_at_start(
    budget: Budget, match: str
) -> None:
    members = member(generator, args={"model": scripted_model()}, count=2)
    agent_swarm = swarm(members, channels=[], budget=budget)
    with pytest.raises(ValueError, match=match):
        await run_swarm(agent_swarm)


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
        # a 30-token share each, below the 60-token cap
        pytest.param([], Budget(tokens=60, split="equal"), "share", id="share"),
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

    # a split needs a second member, which is never started
    sibling = [member(sleeper(), name="sibling")] if budget.split != "pool" else []
    stop = await run_swarm(
        swarm(
            [
                member(generator(scripted_model()), bridged=True, limits=limits),
                *sibling,
            ],
            controller=_controller(
                run, f"limits_test_last_call_{expected.replace(':', '_')}"
            ),
            channels=[],
            budget=budget,
        )
    )
    assert statuses(stop) == {"generator": expected} | {m.name: None for m in sibling}
    [handle] = handles
    assert (handle.status, handle.submitted) == (expected, expected == "submitted")
