import functools
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from typing import Any, cast
from uuid import uuid4

import anyio
import pytest
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import (
    Agent,
    AgentState,
    agent,
    agent_with,
    deepagent,
    handoff,
    react,
    research,
)
from inspect_ai.dataset import Sample
from inspect_ai.event import CommunicationEvent, CompactionEvent
from inspect_ai.model import (
    ChatMessage,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    CompactionTrim,
    Model,
    ModelOutput,
)
from inspect_ai.model._trim import partition_messages
from inspect_ai.scorer import includes
from inspect_ai.tool import Tool, ToolCall, ToolError, tool
from inspect_ai.util import time_limit

from inspect_swarm import (
    Boundary,
    Budget,
    ControlEvent,
    Controller,
    Member,
    MemberEnded,
    MemberIdle,
    MemberNews,
    StopReason,
    SwarmControl,
    leaderless,
    member,
    messages,
    shared_memory,
    swarm,
    swarm_controller,
    swarm_tools,
)
from inspect_swarm import _member as member_module
from inspect_swarm._bus._address import resolve
from inspect_swarm._controller._control import SwarmRun

from .swarm_runtime import Script, generator, output, scripted_model, wait
from .test_shared_memory import _Member  # pyright: ignore[reportPrivateUsage]
from .test_turn_seam import (
    Result,
    _boundaries,  # pyright: ignore[reportPrivateUsage]
    _call,  # pyright: ignore[reportPrivateUsage]
    _changes,  # pyright: ignore[reportPrivateUsage]
    _controller,  # pyright: ignore[reportPrivateUsage]
    _eval,  # pyright: ignore[reportPrivateUsage]
    _metrics,  # pyright: ignore[reportPrivateUsage]
    _raised,  # pyright: ignore[reportPrivateUsage]
    _Ran,  # pyright: ignore[reportPrivateUsage]
    _react,  # pyright: ignore[reportPrivateUsage]
    _Recording,  # pyright: ignore[reportPrivateUsage]
    _run,  # pyright: ignore[reportPrivateUsage]
    _statuses,  # pyright: ignore[reportPrivateUsage]
    _submit,  # pyright: ignore[reportPrivateUsage]
    _swarm_events,  # pyright: ignore[reportPrivateUsage]
    _user,  # pyright: ignore[reportPrivateUsage]
    work,
)

Run = Callable[[SwarmControl], Awaitable[StopReason]]


def _calls(*calls: tuple[str, dict[str, Any]]) -> ModelOutput:
    # one assistant message with several tool calls
    result = _call(calls[0][0], **calls[0][1])
    result.message.tool_calls = [
        ToolCall(id=uuid4().hex, function=function, arguments=arguments)
        for function, arguments in calls
    ]
    return result


def _refused_submit() -> ModelOutput:
    # a refusal that makes a valid submit call
    result = _submit("refused")
    result.choices[0].stop_reason = "content_filter"
    return result


def _waking(*inputs: str, name: str = "a") -> Run:
    # starts `name`, wakes it with each input in turn after each answer, then returns
    async def run(control: SwarmControl) -> StopReason:
        pending = list(inputs)
        control.start(name)
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and pending:
                    control.wake(event.member, pending.pop(0))
        return "quiescence"

    return run


def _events_of(control: SwarmControl, seen: list[Any]) -> Run:
    # records every event and the member's state when it was read, waking no one
    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async with aclosing(control.events()) as events:
            async for event in events:
                seen.append(event)
        return "quiescence"

    return run


def _answer_events(ran: _Ran) -> list[dict[str, Any]]:
    return [{"member": e.member, **e.data} for e in _swarm_events(ran, "answer")]


@agent
def react_builder(model: Model) -> Agent:
    """A documented member builder: returns the loop as it is."""
    return react(model=model, tools=[work()])


@agent
def wrapping_builder(model: Model) -> Agent:
    """A builder whose agent awaits a loop and then checks its result."""
    inner = react(model=model, tools=[work()])

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        if state.output.completion != "ok":
            raise ValueError("not ok")
        return state

    return execute


@agent
def background_builder(model: Model) -> Agent:
    """A builder that returns deepagent() with background dispatch."""
    return deepagent(model=model, background=4, memory=False, todo_write=False)


def _background(model: Model) -> Agent:
    return deepagent(model=model, background=True, memory=False)


def _wraps_copy(model: Model) -> Agent:
    # a functools.wraps copy of react() that checks the result after the loop
    inner = react(model=model, tools=[work()])

    @functools.wraps(inner)
    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        assert state.output.completion
        return state

    return execute


# ---------------------------------------------------------------------------
# Answer, idle, wake


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["react", "no_submit", "deepagent"])
async def test_a_persistent_member_answers_is_held_idle_and_a_wake_continues_its_conversation(
    kind: str,
) -> None:
    if kind == "no_submit":
        script = _Recording([output("one"), output("two")])
        agent_ = _react(script, submit=False)
    else:
        script = _Recording([_submit("one"), _submit("two")])
        agent_ = (
            _react(script)
            if kind == "react"
            else deepagent(model=scripted_model(script), memory=False, todo_write=False)
        )
    seen: list[tuple[Any, str, bool]] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async with aclosing(control.events()) as events:
            async for event in events:
                handle = control.member("a")
                seen.append((event, handle.state, handle.held))
                if isinstance(event, MemberIdle) and event.answer == 0:
                    control.wake("a", "the next task")
        assert control.member("a").answers == 2
        return "quiescence"

    ran = await _run([member(agent_, name="a", persistent=True)], run)
    # the controller returned at quiescence, and the stop ended the idle member
    assert seen == [
        (MemberIdle("a", 0, False), "idle", True),
        (MemberIdle("a", 1, False), "idle", True),
    ]
    first, second = script.inputs
    # the first activation's messages, its answer among them, then the wake input
    assert [m.id for m in second[: len(first)]] == [m.id for m in first]
    answer = second[len(first)]
    assert answer.role == "assistant" and "one" in answer.text
    if kind != "no_submit":
        assert second[len(first) + 1].role == "tool"
    assert second[-1].role == "user" and second[-1].text == "the next task"
    assert sum(isinstance(m, ChatMessageSystem) for m in second) == 1
    [result] = ran.record.members
    assert (result.persistent, len(result.answer_times), result.status) == (
        True,
        2,
        "submitted",
    )
    assert ran.record.final.model_dump() == {
        "mode": "first",
        "member": "a",
        "reason": None,
        "provisional": False,
        "answer": 0,
    }
    assert "one" in ran.sample.output.completion


@pytest.mark.anyio
async def test_a_callback_sees_the_wake_and_its_change_applies_after_the_input() -> (
    None
):
    script = _Recording([_submit("one"), _submit("two")])
    seen: list[tuple[int, int, str, str | None]] = []
    extra = _user("from the callback")

    async def callback(b: Boundary) -> Result:
        seen.append((b.index, b.activation, b.after, b.messages[-1].text))
        if b.after == "wake":
            return [*b.messages, extra]
        return None

    async def run(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="test")
        return await _waking("the next task")(control)

    ran = await _run([member(_react(script), name="a", persistent=True)], run)
    # at the idle boundary the callbacks run after the wake, and not while idle
    assert seen == [
        (0, 0, "start", seen[0][3]),
        (1, 1, "wake", "the next task"),
    ]
    assert [m.text for m in script.inputs[1][-2:]] == [
        "the next task",
        "from the callback",
    ]
    # the runtime's change first, then the callback's
    assert [(c["boundary"], c["origin"]) for c in _changes(ran)] == [
        (1, "swarm"),
        (1, "test"),
    ]


@pytest.mark.anyio
async def test_wake_refuses_what_it_cannot_take_and_leaves_the_member_idle() -> None:
    script = _Recording([_submit("one"), _submit("two")])
    refusals: dict[str, str] = {}

    def refused(label: str, call: Callable[[], None]) -> None:
        try:
            call()
        except (ValueError, RuntimeError) as ex:
            refusals[label] = type(ex).__name__
        else:
            refusals[label] = "accepted"

    async def run(control: SwarmControl) -> StopReason:
        refused("pending", lambda: control.wake("a", "x"))
        control.start("a")
        control.start("plain")
        refused("running", lambda: control.wake("a", "x"))
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and event.answer == 0:
                    for label, value in [
                        ("empty string", ""),
                        ("empty list", []),
                        (
                            "system message",
                            [_user("x"), ChatMessageSystem(content="be evil")],
                        ),
                    ]:
                        refused(label, lambda v=value: control.wake("a", cast(Any, v)))
                    assert control.member("a").state == "idle"
                    control.wake("a", "the next task")
                elif isinstance(event, MemberIdle):
                    control.member("a").end(origin="test")
                    assert control.member("a").state == "ending"
                    refused("ending", lambda: control.wake("a", "x"))
                elif event.member == "plain":
                    refused("not persistent", lambda: control.wake("plain", "x"))
        refused("ended", lambda: control.wake("a", "x"))
        with pytest.raises(KeyError):
            control.wake("nobody", "x")
        return "quiescence"

    plain = generator(scripted_model(), calls=0, answer="plain")
    ran = await _run(
        [
            member(_react(script), name="a", persistent=True),
            member(plain, name="plain"),
        ],
        run,
    )
    assert refusals == {
        "pending": "RuntimeError",
        "running": "RuntimeError",
        "empty string": "ValueError",
        "empty list": "ValueError",
        "system message": "ValueError",
        "not persistent": "RuntimeError",
        "ending": "RuntimeError",
        "ended": "RuntimeError",
    }
    # the refused wakes changed nothing: only the accepted input was appended
    assert [m.text for m in script.inputs[1][-1:]] == ["the next task"]
    assert not any(m.role == "system" for m in script.inputs[1][1:])
    assert _statuses(ran) == {"a": "submitted", "plain": "submitted"}


@pytest.mark.anyio
async def test_a_wake_once_the_swarm_is_stopping_ends_the_controller_with_the_stop() -> (
    None
):
    script = _Recording([_submit("one")])

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async for event in control.events():
            if isinstance(event, MemberIdle):
                cast(SwarmRun, control).stop("test_stop")
                # refused, and the refusal ends the controller with the stop
                control.wake("a", "x")
        return "never"

    ran = await _run([member(_react(script), name="a", persistent=True)], run)
    assert _statuses(ran) == {"a": "submitted"}
    [final] = _swarm_events(ran, "final")
    assert final.data["stop_reason"] == "test_stop"


@pytest.mark.anyio
async def test_idle_and_member_idle_are_published_together() -> None:
    script = _Recording([_submit("one"), _submit("two")])
    checks: list[bool] = []

    async def run(control: SwarmControl) -> StopReason:
        runtime = cast(SwarmRun, control)
        yielded: list[Any] = []
        done = anyio.Event()

        async def poll() -> None:
            while not done.is_set():
                if control.member("a").state == "idle":
                    answer = control.member("a").answers - 1
                    checks.append(
                        MemberIdle("a", answer, False) in [*runtime._queue, *yielded]  # pyright: ignore[reportPrivateUsage]
                    )
                await anyio.lowlevel.checkpoint()

        async with anyio.create_task_group() as tg:
            tg.start_soon(poll)
            control.start("a")
            async with aclosing(control.events()) as events:
                async for event in events:
                    yielded.append(event)
                    if isinstance(event, MemberIdle) and event.answer == 0:
                        control.wake("a", "again")
            done.set()
        return "quiescence"

    await _run([member(_react(script), name="a", persistent=True)], run)
    assert checks and all(checks)


@pytest.mark.anyio
async def test_a_member_idle_event_can_be_stale_and_the_state_decides() -> None:
    scripts = {name: _Recording([_submit(name)]) for name in ("a", "b")}
    seen: list[tuple[Any, str]] = []
    refused: list[str] = []
    # the member whose event comes first, and the other one, which its handling ends
    order: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        control.start("b")
        while not all(m.state == "idle" for m in control.members):
            await anyio.sleep(0.01)
        async with aclosing(control.events()) as events:
            async for event in events:
                seen.append((event, control.member(event.member).state))
                if isinstance(event, MemberIdle) and not order:
                    order.extend([event.member, "b" if event.member == "a" else "a"])
                    control.member(order[1]).end(origin="test")
                elif isinstance(event, MemberIdle):
                    try:
                        control.wake(event.member, "x")
                    except RuntimeError as ex:
                        refused.append(str(ex))
        return "quiescence"

    await _run(
        [
            member(_react(script), name=name, persistent=True)
            for name, script in scripts.items()
        ],
        run,
    )
    first, second = order
    assert seen == [
        (MemberIdle(first, 0, False), "idle"),
        (MemberIdle(second, 0, False), "ending"),
        (MemberEnded(second, "submitted", True), "ended"),
    ]
    assert len(refused) == 1 and "ending" in refused[0]


@pytest.mark.anyio
async def test_wake_messages_get_fresh_ids_and_no_source_and_the_callers_list_is_unchanged() -> (
    None
):
    script = _Recording([_submit("one"), _submit("two"), _submit("three")])
    task = ChatMessageUser(content="again", source="input")
    given = [task]

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and event.answer < 2:
                    control.wake("a", given)
        return "quiescence"

    ran = await _run([member(_react(script), name="a", persistent=True)], run)
    assert given == [task] and task.source == "input" and task.text == "again"
    woken = [script.inputs[1][-1], script.inputs[2][-1]]
    assert [m.text for m in woken] == ["again", "again"]
    ids = [m.id for m in woken]
    assert len(set(ids)) == 2 and task.id not in ids
    assert all(m.source is None for m in woken)
    # nothing in the conversation repeats an id
    assert len({m.id for m in script.inputs[2]}) == len(script.inputs[2])
    # the starting input is still the only input-marked message group
    assert [m.text for m in script.inputs[2] if m.source == "input"][0] == "task"
    assert [c["added"][0]["id"] for c in _changes(ran)] == ids


@pytest.mark.anyio
async def test_an_input_marked_wake_task_stays_last_through_a_forced_compaction() -> (
    None
):
    # the woken generation overflows the context window, which forces a compaction
    overflow = output("too long")
    overflow.choices[0].stop_reason = "model_length"
    script = _Recording([_call("work"), _submit("one"), overflow, _submit("two")])
    task = ChatMessageUser(content="the next task", source="input")

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and event.answer == 0:
                    control.wake("a", [task])
        return "quiescence"

    agent_ = _react(script, compaction=CompactionTrim(preserve=1.0))
    ran = await _run([member(agent_, name="a", persistent=True)], run)
    [compaction] = [e for e in ran.events if isinstance(e, CompactionEvent)]
    assert compaction.type == "trim"
    # the generation after the compaction, given the compacted conversation
    woken = script.inputs[-1]
    assert len(script.inputs) == 4
    # the wake task is still after the first activation's history, its answer included
    assert woken[-1].role == "user" and woken[-1].text == "the next task"
    assert any(
        isinstance(m, ChatMessageTool) and m.function == "submit" for m in woken[:-1]
    )
    assert woken[-1].source is None
    # only the starting input is in the preserved input group
    starting = [m.id for m in script.inputs[0] if m.source == "input"]
    assert [m.id for m in partition_messages(woken).input] == starting
    assert task.source == "input"
    assert len(ran.record.members[0].answer_times) == 2


# ---------------------------------------------------------------------------
# Endings


@pytest.mark.anyio
async def test_a_stop_while_idle_ends_the_member_submitted_with_its_last_answer() -> (
    None
):
    script = _Recording([_submit("one"), _submit("two")])
    ran = await _run(
        [member(_react(script), name="a", persistent=True)], _waking("next")
    )
    # the controller returned at quiescence, and the stop ended the idle member
    assert _statuses(ran) == {"a": "submitted"}
    [result] = ran.record.members
    assert len(result.answer_times) == 2
    [event] = [e for e in _boundaries(ran) if e["outcome"] == "stop"]
    assert event["after"] == "tools"


@tool
def end_member(control: list[SwarmControl]) -> Tool:
    async def execute(name: str) -> str:
        """End a member.

        Args:
            name: The member.
        """
        control[0].member(name).end(origin="test")
        return "ending"

    return execute


@pytest.mark.anyio
async def test_an_end_during_the_final_submit_ends_the_member_stopped_unrecorded() -> (
    None
):
    handle: list[SwarmControl] = []
    script = _Recording(
        [_calls(("end_member", {"name": "a"}), ("submit", {"answer": "late"}))]
    )

    async def run(control: SwarmControl) -> StopReason:
        handle.append(control)
        return await _waking()(control)

    ran = await _run(
        [member(_react(script, tools=[end_member(handle)]), name="a", persistent=True)],
        run,
    )
    assert _statuses(ran) == {"a": "stopped"}
    assert ran.record.members[0].answer_times == []
    assert _answer_events(ran) == []


@pytest.mark.anyio
async def test_an_answer_in_the_grace_period_is_recorded_and_the_member_ends_submitted() -> (
    None
):
    # its turn's tool call is running when the stop begins, and its submit follows
    slow = Script([_calls(("work", {"seconds": 0.3}), ("submit", {"answer": "late"}))])
    fast = generator(scripted_model(Script(delay=0.1)), calls=1, answer="first")

    async def run(control: SwarmControl) -> StopReason:
        control.start("slow")
        control.start("fast")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberEnded) and event.member == "fast":
                    break
        return "submitted"

    ran = await _run(
        [
            member(_react(slow), name="slow", persistent=True),
            member(fast, name="fast"),
        ],
        run,
    )
    assert _statuses(ran) == {"slow": "submitted", "fast": "submitted"}
    assert len(ran.record.members[0].answer_times) == 1


@pytest.mark.anyio
async def test_a_stop_while_woken_ends_the_member_stopped_and_its_answers_stay() -> (
    None
):
    started = anyio.Event()

    def respond(input: list[ChatMessage]) -> ModelOutput:
        if input[-1].text == "next":
            started.set()
            return _call("work", seconds=0.2)
        return _submit("one")

    script = Script(respond=respond)

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async for event in control.events():
            if isinstance(event, MemberIdle):
                control.wake("a", "next")
                await started.wait()
                return "enough"
        return "never"

    ran = await _run(
        [member(_react(script), name="a", persistent=True)],
        run,
        channels=[],
    )
    assert _statuses(ran) == {"a": "stopped"}
    assert ran.record.final.member == "a" and ran.record.final.answer == 0
    assert "one" in ran.sample.output.completion


async def _turns_left(state: AgentState, left: list[int]) -> bool:
    left[0] -= 1
    return left[0] > 0


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["no_submit", "react", "deepagent"])
async def test_a_cut_off_ends_the_member_unsubmitted_and_the_swarm_goes_on(
    shape: str,
) -> None:
    script = _Recording([_call("work"), _call("work")])

    async def stop(state: AgentState) -> bool:
        return False

    if shape == "no_submit":
        agent_ = _react(script, submit=False, on_continue=stop)
    elif shape == "react":
        agent_ = _react(script, on_continue=stop)
    else:
        agent_ = deepagent(
            model=scripted_model(script),
            tools=[work()],
            on_continue=stop,
            memory=False,
            todo_write=False,
        )
    later = generator(scripted_model(Script(delay=0.2)), calls=1, answer="b's")
    seen: list[Any] = []
    refused: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        await _events_of(control, seen)(control)
        try:
            control.choose_answer("a")
        except RuntimeError as ex:
            refused.append(str(ex))
        return "all_done"

    ran = await swarm_run(
        [member(agent_, name="a", persistent=True), member(later, name="b")],
        run,
        final="last",
    )
    assert MemberEnded("a", "unsubmitted", False) in seen
    assert not any(isinstance(e, MemberIdle) for e in seen)
    assert ran.record.members[0].answer_times == []
    assert _answer_events(ran) == []
    assert ran.record.final.member == "b"
    assert len(refused) == 1 and "not answered" in refused[0]


async def swarm_run(members: list[Any], run: Run, *, final: Any = None) -> _Ran:
    @swarm_controller(name=f"persistent_test_{uuid4().hex}")
    def factory() -> Controller:
        return Controller(run, final=final)

    sample = await _eval(swarm(members, controller=factory(), channels=[]))
    assert sample.error is None, sample.error.traceback
    return _Ran(sample)


@pytest.mark.anyio
async def test_a_turn_limiter_spans_activations_and_its_cut_off_is_never_recorded() -> (
    None
):
    left = [3]
    script = _Recording([_submit("one"), _call("work"), _call("work"), _call("work")])
    ran = await swarm_run(
        [
            member(
                _react(script, on_continue=functools.partial(_turns_left, left=left)),
                name="a",
                persistent=True,
            )
        ],
        _waking("next"),
        final="last",
    )
    assert _statuses(ran) == {"a": "unsubmitted"}
    # the held answer did not count against the limiter: three tool turns did
    assert left == [0] and len(script.inputs) == 4
    assert len(ran.record.members[0].answer_times) == 1
    assert ran.record.final.model_dump()["answer"] == 0
    assert "one" in ran.sample.output.completion


async def _scored(members: list[Any], run: Run) -> _Ran:
    # a swarm on a sample whose scorer react() checks its submissions against
    @swarm_controller(name=f"persistent_test_{uuid4().hex}")
    def factory() -> Controller:
        return Controller(run, final="last")

    [log] = await eval_async(
        Task(
            dataset=[Sample(input="task", target="right")],
            solver=swarm(members, controller=factory(), channels=[]),
            scorer=includes(),
        ),
        model="mockllm/model",
        log_dir=tempfile.mkdtemp(),
    )
    assert log.samples, log.error
    sample = log.samples[0]
    assert sample.error is None, sample.error.traceback
    return _Ran(sample)


@pytest.mark.anyio
async def test_a_checked_submission_is_an_answer_and_an_incorrect_one_is_not() -> None:
    # the first activation passes its check; the second submits an incorrect answer
    # with an attempt left, and its on_continue then cuts it off
    script = _Recording([_submit("right"), _submit("wrong"), _call("work")])
    continued: list[str] = []

    async def stop_once_woken(state: AgentState) -> bool:
        continued.append(state.output.completion)
        return False

    ran = await _scored(
        [
            member(
                _react(script, attempts=2, on_continue=stop_once_woken),
                name="a",
                persistent=True,
            )
        ],
        _waking("next"),
    )
    assert _statuses(ran) == {"a": "unsubmitted"}
    # the incorrect submission got the incorrect message, then on_continue ended it
    assert len(script.inputs) == 2 and len(continued) == 1
    assert len(ran.record.members[0].answer_times) == 1
    assert [e["answer"] for e in _answer_events(ran)] == [0]
    assert ran.record.final.answer == 0
    assert "right" in ran.sample.output.completion


@pytest.mark.anyio
async def test_a_reply_after_on_continue_false_is_an_answer() -> None:
    script = _Recording([output("one"), output("two")])

    async def stop(state: AgentState) -> bool:
        return False

    ran = await _run(
        [
            member(
                _react(script, submit=False, on_continue=stop),
                name="a",
                persistent=True,
            )
        ],
        _waking("next"),
    )
    assert _statuses(ran) == {"a": "submitted"}
    assert len(ran.record.members[0].answer_times) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("child_generates", [True, False])
async def test_a_submit_beside_a_handoff_is_never_an_answer(
    child_generates: bool,
) -> None:
    child_script = _Recording([_submit("child's")])

    @agent
    def silent() -> Agent:
        """A child that returns without generating."""

        async def execute(state: AgentState) -> AgentState:
            return state

        return execute

    child = (
        react(
            name="helper", description="A helper.", model=scripted_model(child_script)
        )
        if child_generates
        else agent_with(silent(), name="helper", description="A helper.")
    )
    script = _Recording(
        [
            _submit("one"),
            _calls(("submit", {"answer": "two"}), ("transfer_to_helper", {})),
        ]
    )
    ran = await swarm_run(
        [member(_react(script, tools=[handoff(child)]), name="a", persistent=True)],
        _waking("next"),
        final="last",
    )
    assert _statuses(ran) == {"a": "unsubmitted"}
    assert len(ran.record.members[0].answer_times) == 1
    assert [e["answer"] for e in _answer_events(ran)] == [0]
    assert ran.record.final.answer == 0
    assert "one" in ran.sample.output.completion


@pytest.mark.anyio
@pytest.mark.parametrize("activation", [0, 1])
async def test_a_refused_submission_is_not_an_answer(activation: int) -> None:
    outputs = (
        [_refused_submit()] if activation == 0 else [_submit("one"), _refused_submit()]
    )
    script = _Recording(outputs)
    seen: list[Any] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async with aclosing(control.events()) as events:
            async for event in events:
                seen.append(event)
                if isinstance(event, MemberIdle):
                    control.wake("a", "next")
        return "all_done"

    ran = await _run([member(_react(script), name="a", persistent=True)], run)
    assert _statuses(ran) == {"a": "unsubmitted"}
    assert seen[-1] == MemberEnded("a", "unsubmitted", False)
    assert len(script.inputs) == activation + 1
    assert len(ran.record.members[0].answer_times) == activation
    assert ran.record.final.member == ("a" if activation else None)


@pytest.mark.anyio
async def test_a_time_limit_reached_at_the_boundary_after_an_answer_refuses_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = anyio.Event()
    drain = member_module._drain  # pyright: ignore[reportPrivateUsage]

    async def held_drain(*args: Any) -> None:
        await cleanup.wait()
        await drain(*args)

    monkeypatch.setattr(member_module, "_drain", held_drain)
    script = _Recording([_submit("one")])
    seen: list[tuple[Any, str]] = []
    states: list[str] = []
    refused: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        gate = cast(SwarmRun, control)._lookup("a").gate  # pyright: ignore[reportPrivateUsage]
        record = gate.record_answer

        def overdue(output: ModelOutput) -> None:
            record(output)
            for node in gate.time_nodes:
                node._cancel_scope.deadline = anyio.current_time()  # pyright: ignore[reportPrivateUsage]

        gate.record_answer = overdue

        async def watch() -> None:
            while control.member("a").state != "ended":
                states.append(control.member("a").state)
                if control.member("a").state == "ending" and not refused:
                    try:
                        control.wake("a", "x")
                    except RuntimeError as ex:
                        refused.append(str(ex))
                    cleanup.set()
                await anyio.lowlevel.checkpoint()

        async with anyio.create_task_group() as tg:
            control.start("a")
            tg.start_soon(watch)
            async with aclosing(control.events()) as events:
                async for event in events:
                    seen.append((event, control.member("a").state))
        return "all_done"

    ran = await _run(
        [member(_react(script), name="a", persistent=True, limits=[time_limit(60)])],
        run,
    )
    assert seen == [(MemberEnded("a", "member_limit:time", False), "ended")]
    assert "idle" not in states and "ending" in states
    assert len(refused) == 1
    assert len(ran.record.members[0].answer_times) == 1
    assert ran.record.final.member == "a" and ran.record.final.answer == 0
    assert _boundaries(ran) == []


@pytest.mark.anyio
async def test_a_sample_limit_while_idle_ends_the_member_cancelled() -> None:
    script = _Recording([_submit("one")])

    @swarm_controller(name=f"persistent_waiting_{uuid4().hex}")
    def waiting() -> Controller:
        async def run(control: SwarmControl) -> StopReason:
            control.start("a")
            async for _ in control.events():
                pass
            await anyio.sleep(30)
            return "never"

        return Controller(run)

    sample = await _eval(
        swarm(
            [member(_react(script), name="a", persistent=True)],
            controller=waiting(),
            channels=[],
            # a cap of its own, so that the sample's limit is reached first
            budget=Budget(time=600),
        ),
        time_limit=1,
    )
    assert sample.limit is not None and sample.limit.type == "time"
    ran = _Ran(sample)
    [result] = ran.record.members
    assert (result.status, len(result.answer_times)) == ("cancelled", 1)


# ---------------------------------------------------------------------------
# Which agents can persist


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(generator, id="custom-loop"),
        pytest.param(_wraps_copy, id="wraps-copy"),
        pytest.param(wrapping_builder, id="wrapping-builder"),
        pytest.param(_background, id="background"),
        pytest.param(background_builder, id="background-builder"),
    ],
)
def test_swarm_refuses_a_built_agent_that_cannot_persist(
    build: Callable[[Model], Agent],
) -> None:
    agent_ = build(scripted_model())
    with pytest.raises(ValueError, match="Member 'a' is persistent"):
        swarm([member(agent_, name="a", persistent=True)])
    # not persistent, it is accepted
    swarm([member(agent_, name="a")])


def test_swarm_refuses_a_persistent_member_declared_bridged() -> None:
    with pytest.raises(ValueError, match="declared bridged"):
        swarm(
            [
                member(
                    react(model=scripted_model()),
                    name="a",
                    persistent=True,
                    bridged=True,
                )
            ]
        )


@agent
def failing_builder(model: Model) -> Agent:
    """A builder that raises before it returns an agent."""
    raise ValueError("cannot build the worker")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "factory,match",
    [
        (wrapping_builder, "Member 'a' is persistent, and its agent is not a react"),
        (background_builder, "Member 'a' is persistent, and its agent is deepagent"),
        (failing_builder, "cannot build the worker"),
    ],
)
async def test_a_factory_whose_agent_cannot_persist_is_refused_in_its_run(
    factory: Callable[..., Agent], match: str
) -> None:
    script = _Recording([_submit("one")])
    ran = await _raised(
        [
            member(
                factory,
                args={"model": scripted_model(script)},
                name="a",
                persistent=True,
            )
        ],
        _waking(),
        ValueError,
        match,
    )
    # the error itself, not one saying the agent never reached its gate
    assert ran.sample.error is not None
    assert "never reached a turn boundary" not in ran.sample.error.traceback
    # refused before its first generation
    assert script.started == 0
    assert _statuses(ran) == {"a": "errored"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "form",
    ["react", "named", "no_submit", "agent_with", "deepagent", "builder", "rebuilt"],
)
async def test_the_loops_and_their_builders_persist(form: str) -> None:
    outputs = (
        [output("one"), output("two")]
        if form == "no_submit"
        else [_submit("one"), _submit("two")]
    )
    script = _Recording(outputs)
    model = scripted_model(script)
    record: Member
    match form:
        case "react":
            record = member(react(model=model), name="a", persistent=True)
        case "named":
            record = member(
                react(name="worker", model=model), name="a", persistent=True
            )
        case "no_submit":
            record = member(react(model=model, submit=False), name="a", persistent=True)
        case "agent_with":
            record = member(
                agent_with(react(model=model), name="worker"), name="a", persistent=True
            )
        case "deepagent":
            record = member(
                deepagent(model=model, memory=False, todo_write=False),
                name="a",
                persistent=True,
            )
        case "builder":
            record = member(react_builder(model), name="a", persistent=True)
        case _:
            given = member(
                react_builder, args={"model": model}, name="a", persistent=True
            )
            dumped = given.model_dump()
            assert dumped["persistent"] is True
            dumped["args"] = {"model": model}
            record = Member.model_validate(dumped)
            assert record.persistent and record.factory is react_builder
    ran = await _run([record], _waking("next"))
    assert _statuses(ran) == {"a": "submitted"}
    assert len(ran.record.members[0].answer_times) == 2


def test_persistent_is_logged_only_when_set() -> None:
    model = scripted_model()
    assert "persistent" not in member(react(model=model)).model_dump()
    assert (
        member(react(model=model), persistent=True).model_dump()["persistent"] is True
    )


# ---------------------------------------------------------------------------
# The reader rule


@pytest.mark.anyio
@pytest.mark.parametrize("same_turn", [True, False])
async def test_a_persistent_member_whose_board_only_a_subagent_reads_fails_with_a_waiting_channel(
    same_turn: bool,
) -> None:
    child_script = _Recording([_call("work"), _submit("child's")])
    child_model = scripted_model(child_script)
    parent_outputs = (
        [_calls(("agent", {"prompt": "look"}), ("submit", {"answer": "one"}))]
        if same_turn
        else [_call("agent", prompt="look"), _submit("one")]
    )
    parent = deepagent(
        model=scripted_model(_Recording(parent_outputs)),
        subagents=[
            research(tools=[work()], extra_tools=[swarm_tools()], model=child_model)
        ],
        memory=False,
        todo_write=False,
    )

    async def run(control: SwarmControl) -> StopReason:
        return await _waking()(control)

    sample = await _eval(
        swarm(
            [member(parent, name="a", persistent=True)],
            controller=_controller(run),
            channels=[shared_memory()],
        )
    )
    assert sample.error is not None
    assert "only through a subagent" in sample.error.message
    ran = _Ran(sample)
    assert _statuses(ran) == {"a": "errored"}
    assert ran.record.members[0].answer_times == []


@pytest.mark.anyio
async def test_a_persistent_member_whose_board_only_a_subagent_reads_persists_without_one() -> (
    None
):
    child_model = scripted_model(_Recording([_submit("c1"), _submit("c2")]))
    parent = deepagent(
        model=scripted_model(
            _Recording(
                [
                    _call("agent", prompt="look"),
                    _submit("one"),
                    _call("agent", prompt="look"),
                    _submit("two"),
                ]
            )
        ),
        subagents=[
            research(tools=[work()], extra_tools=[swarm_tools()], model=child_model)
        ],
        memory=False,
        todo_write=False,
    )
    ran = await _run([member(parent, name="a", persistent=True)], _waking("next"))
    assert _statuses(ran) == {"a": "submitted"}
    assert len(ran.record.members[0].answer_times) == 2


# ---------------------------------------------------------------------------
# Time, waits, quiescence


@pytest.mark.anyio
async def test_a_member_time_limit_does_not_run_while_idle() -> None:
    script = _Recording([_submit("one"), _submit("two")])

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and event.answer == 0:
                    await anyio.sleep(1)
                    control.wake("a", "next")
        return "quiescence"

    ran = await _run(
        [member(_react(script), name="a", persistent=True, limits=[time_limit(0.5)])],
        run,
    )
    assert _statuses(ran) == {"a": "submitted"}
    [metrics] = _metrics(ran).members
    assert metrics.awaiting_work_time >= 1


@pytest.mark.anyio
async def test_working_time_stops_while_every_member_is_idle() -> None:
    hold = 1.0
    script = _Recording([_submit("one"), _submit("two")])

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and event.answer == 0:
                    await anyio.sleep(hold)
                    control.wake("a", "next")
        return "quiescence"

    ran = await _run([member(_react(script), name="a", persistent=True)], run)
    total, working = ran.sample.total_time, ran.sample.working_time
    assert total is not None and working is not None
    # the controller's wait while the member was idle is waiting time
    assert total - working >= hold * 0.9


@pytest.mark.anyio
async def test_a_wait_is_released_when_every_other_member_is_idle() -> None:
    waiter = _Recording([_call("swarm_wait", timeout=30), _submit("waiter's")])
    ran = await _run(
        [
            member(_react(_Recording([_submit("one")])), name="a", persistent=True),
            member(_react(waiter, tools=[swarm_tools()]), name="b"),
        ],
        _all_started,
        channels=[shared_memory()],
    )
    [waited] = [
        e
        for e in ran.events
        if isinstance(e, CommunicationEvent) and e.kind == "waited"
    ]
    assert wait(waited).outcome == "all_waiting"
    assert _statuses(ran) == {"a": "submitted", "b": "submitted"}


async def _all_started(control: SwarmControl) -> StopReason:
    for m in control.members:
        control.start(m)
    async with aclosing(control.events()) as events:
        async for _ in events:
            pass
    return "quiescence"


@pytest.mark.anyio
async def test_the_iterator_waits_for_an_ending_member_and_ends_at_quiescence() -> None:
    script = _Recording([_submit("one"), _submit("two")])
    seen: list[tuple[Any, str]] = []
    after_quiescence: list[Any] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        control.start("b")
        async with aclosing(control.events()) as events:
            async for event in events:
                seen.append((event, control.member(event.member).state))
                if event == MemberIdle("b", 0, False):
                    control.member("b").end(origin="test")
                    assert control.member("b").state == "ending"
        # quiescent: a is idle; a new iterator reports its next event once woken
        assert control.member("a").state == "idle"
        control.wake("a", "next")
        async with aclosing(control.events()) as events:
            async for event in events:
                after_quiescence.append(event)
        return "quiescence"

    ran = await _run(
        [
            member(_react(script), name="a", persistent=True),
            member(
                _react(_Recording([_call("work", seconds=0.1), _submit("b's")])),
                name="b",
                persistent=True,
            ),
        ],
        run,
    )
    assert seen == [
        (MemberIdle("a", 0, False), "idle"),
        (MemberIdle("b", 0, False), "idle"),
        (MemberEnded("b", "submitted", True), "ended"),
    ]
    assert after_quiescence == [MemberIdle("a", 1, False)]
    assert _statuses(ran) == {"a": "submitted", "b": "submitted"}


@pytest.mark.anyio
@pytest.mark.parametrize("stop_on_submission", [True, False])
async def test_leaderless_with_persistent_members(stop_on_submission: bool) -> None:
    scripts = [
        _Recording([_submit("a's")]),
        Script([_call("work", seconds=0.3), _submit("b's")]),
    ]
    sample = await _eval(
        swarm(
            [
                member(_react(script), name=name, persistent=True)
                for name, script in zip("ab", scripts, strict=True)
            ],
            controller=leaderless(stop_on_submission=stop_on_submission),
            channels=[],
        )
    )
    ran = _Ran(sample)
    [final] = _swarm_events(ran, "final")
    if stop_on_submission:
        assert final.data["stop_reason"] == "submitted"
        assert _statuses(ran) == {"a": "submitted", "b": "stopped"}
    else:
        assert final.data["stop_reason"] == "quiescence"
        assert _statuses(ran) == {"a": "submitted", "b": "submitted"}
    assert ran.record.final.member == "a"


# ---------------------------------------------------------------------------
# Answers, evidence and metrics


@pytest.mark.anyio
@pytest.mark.parametrize("final", ["first", "last"])
async def test_first_and_last_choose_across_every_answer(final: str) -> None:
    scripts = {
        "a": Script([_submit("a0"), _submit("a1")], delay=0.05),
        "b": Script([_submit("b0"), _submit("b1")], delay=0.2),
    }

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        control.start("b")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and event.answer == 0:
                    control.wake(event.member, "next")
        return "quiescence"

    ran = await swarm_run(
        [member(_react(s), name=n, persistent=True) for n, s in scripts.items()],
        run,
        final=final,
    )
    expected = ("a", 0) if final == "first" else ("b", 1)
    assert (ran.record.final.member, ran.record.final.answer) == expected
    assert ran.sample.output.completion.endswith(f"{expected[0]}{expected[1]}")
    [event] = _swarm_events(ran, "final")
    assert event.data["answer"] == expected[1]


@pytest.mark.anyio
async def test_choose_answer_takes_the_latest_answer_and_keeps_it() -> None:
    script = _Recording([_submit("one"), _submit("two"), _submit("three")])
    refused: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        try:
            control.choose_answer("a")
        except RuntimeError as ex:
            refused.append(str(ex))
        control.start("a")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and event.answer < 2:
                    if event.answer == 1:
                        control.choose_answer("a", reason="the second")
                    control.wake("a", "next")
        return "quiescence"

    ran = await _run([member(_react(script), name="a", persistent=True)], run)
    assert len(refused) == 1 and "not answered" in refused[0]
    assert ran.record.final.model_dump() == {
        "mode": "controller",
        "member": "a",
        "reason": "the second",
        "provisional": False,
        "answer": 1,
    }
    assert "two" in ran.sample.output.completion
    assert ran.sample.messages[-1].text == ran.sample.output.completion


@pytest.mark.anyio
async def test_answers_holds_and_metrics_are_recorded() -> None:
    script = _Recording(
        [_submit("one"), _call("work"), _submit("two"), _call("work"), _submit("three")]
    )

    async def callback(b: Boundary) -> Result:
        if b.after == "wake":
            # after the wake: held, but not idle
            await anyio.sleep(0.1)
        return None

    async def run(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="test")
        return await _waking("next", "last")(control)

    ran = await swarm_run(
        [member(_react(script), name="a", persistent=True)], run, final="last"
    )
    assert _answer_events(ran) == [
        {"member": "a", "version": 1, "answer": i} for i in range(3)
    ]
    idle_holds = [b for b in _boundaries(ran) if b["after"] in ("wake", "tools")]
    assert [b["outcome"] for b in _boundaries(ran) if b["after"] == "wake"] == [
        "continue",
        "continue",
    ]
    assert all(
        set(b)
        <= {
            "version",
            "boundary",
            "after",
            "origins",
            "held_for",
            "outcome",
            "messages",
        }
        for b in idle_holds
    )
    [metrics] = _metrics(ran).members
    assert (metrics.activations, metrics.answers) == (3, 3)
    assert metrics.held_time >= metrics.awaiting_work_time + 0.2
    assert _metrics(ran).steps_to_answer == 5
    assert _metrics(ran).critical_steps_to_answer == 5
    # the wake's change names the input the next generation received
    swarm_changes = [c for c in _changes(ran) if c["origin"] == "swarm"]
    assert [c["added"][0]["id"] for c in swarm_changes] == [
        script.inputs[1][-1].id,
        script.inputs[3][-1].id,
    ]


@pytest.mark.anyio
async def test_steps_to_answer_end_at_the_chosen_answer() -> None:
    script = _Recording([_submit("one"), _call("work"), _submit("two")])
    ran = await swarm_run(
        [member(_react(script), name="a", persistent=True)],
        _waking("next"),
        final="first",
    )
    assert ran.record.final.answer == 0
    assert _metrics(ran).steps_to_answer == 1
    assert _metrics(ran).critical_steps_to_answer == 1


@pytest.mark.anyio
async def test_dispatch_gives_a_worker_each_task_in_turn() -> None:
    def respond(input: list[ChatMessage]) -> ModelOutput:
        return _submit(f"done: {input[-1].text}")

    tasks = ["one", "two", "three"]

    @swarm_controller(name=f"dispatch_{uuid4().hex}")
    def dispatch(tasks: list[str], worker: str = "worker") -> Controller:
        async def run(swarm: SwarmControl) -> StopReason:
            pending = list(tasks)
            swarm.start(worker, input=pending.pop(0))
            async with aclosing(swarm.events()) as events:
                async for event in events:
                    if isinstance(event, MemberIdle) and pending:
                        swarm.wake(event.member, input=pending.pop(0))
            return "quiescence" if swarm.member(worker).state == "idle" else "all_done"

        return Controller(run, final="last")

    sample = await _eval(
        swarm(
            [member(_react(Script(respond=respond)), name="worker", persistent=True)],
            controller=dispatch(tasks),
            channels=[],
        )
    )
    ran = _Ran(sample)
    assert len(ran.record.members[0].answer_times) == 3
    assert ran.record.final.answer == 2
    assert sample.output.completion.endswith("done: three")


# ---------------------------------------------------------------------------
# The bus


@pytest.mark.anyio
async def test_an_ending_member_refuses_addressed_records_and_an_idle_one_takes_them() -> (
    None
):
    members = {name: _Member(name) for name in ("a", "idle", "ending")}
    members["idle"].state = "idle"
    members["ending"].state = "ending"
    assert resolve("a", ["idle"], members) == ("idle",)
    with pytest.raises(ToolError, match="ending is ending"):
        resolve("a", ["ending"], members)


# ---------------------------------------------------------------------------
# News for idle members


def _write(path: str = "/shared_memory/task.md", text: str = "do it") -> ModelOutput:
    return _call("shared_memory", command="create", path=path, file_text=text)


def _view(path: str = "/shared_memory/task.md") -> ModelOutput:
    return _call("shared_memory", command="view", path=path)


def _send(to: str, wake: bool = True) -> ModelOutput:
    return _call("send_message", to=to, message=f"hi {to}, wake={wake}", wake=wake)


def _lead_after_idle(
    seen: list[ControlEvent], workers: list[str], lead: str = "lead"
) -> Run:
    # starts the workers, and the lead once every worker is idle; records every event,
    # waking no one
    async def run(control: SwarmControl) -> StopReason:
        for name in workers:
            control.start(name)
        idle: set[str] = set()
        async with aclosing(control.events()) as events:
            async for event in events:
                seen.append(event)
                if isinstance(event, MemberIdle):
                    idle.add(event.member)
                    if idle == set(workers):
                        control.start(lead)
        return "quiescence"

    return run


@pytest.mark.anyio
@pytest.mark.parametrize("tools", [True, False])
async def test_a_write_while_a_worker_is_idle_is_news_to_it(tools: bool) -> None:
    # the worker's own write is not news to it; the lead's later write is, unless the
    # worker never resolved swarm_tools() and so cannot read the board
    worker = (
        _react(
            _Recording([_write("/shared_memory/own.md"), _submit("one")]),
            tools=[swarm_tools()],
        )
        if tools
        else _react(_Recording([_submit("one")]))
    )
    lead = _react(_Recording([_write(), _submit("lead's")]), tools=[swarm_tools()])
    seen: list[ControlEvent] = []
    ran = await _run(
        [
            member(worker, name="worker", persistent=True),
            member(lead, name="lead"),
        ],
        _lead_after_idle(seen, ["worker"]),
        channels=[shared_memory()],
    )
    delivered = [
        e
        for e in ran.events
        if isinstance(e, CommunicationEvent) and e.kind == "delivered"
    ]
    lead_write = delivered[-1]
    assert lead_write.agent == "lead"
    news = (
        [MemberNews("worker", "shared_memory", lead_write.record_ids[0], "lead")]
        if tools
        else []
    )
    assert seen == [
        MemberIdle("worker", 0, False),
        *news,
        MemberEnded("lead", "submitted", True),
    ]
    # the idle worker's own loop sees the record when it is woken, if it has one
    assert lead_write.data["notify"] == (["worker"] if tools else [])


@pytest.mark.anyio
async def test_only_a_waking_record_to_an_idle_members_own_loop_is_news() -> None:
    # a message with wake=False, and one to another member, are not news to "a"
    workers = ["a", "b"]
    lead = _react(
        _Recording([_send("a", wake=False), _send("b"), _send("a"), _submit("lead's")]),
        tools=[swarm_tools()],
    )
    seen: list[ControlEvent] = []
    await _run(
        [
            *(
                member(
                    _react(_Recording([_submit("one")]), tools=[swarm_tools()]),
                    name=name,
                    persistent=True,
                )
                for name in workers
            ),
            member(lead, name="lead"),
        ],
        _lead_after_idle(seen, workers),
        channels=[messages()],
    )
    news = [e for e in seen if isinstance(e, MemberNews)]
    assert news == [
        MemberNews("b", "messages", "m-2", "lead"),
        MemberNews("a", "messages", "m-3", "lead"),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("viewed", [True, False])
async def test_member_idle_says_whether_its_own_loop_has_unviewed_news(
    viewed: bool,
) -> None:
    # the lead writes before the worker starts; the worker answers with or without
    # viewing the file
    lead = _react(_Recording([_write(), _submit("lead's")]), tools=[swarm_tools()])
    worker = _react(
        _Recording([_view(), _submit("one")] if viewed else [_submit("one")]),
        tools=[swarm_tools()],
    )
    seen: list[ControlEvent] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("lead")
        async with aclosing(control.events()) as events:
            async for event in events:
                seen.append(event)
                if isinstance(event, MemberEnded):
                    control.start("worker")
        return "quiescence"

    await _run(
        [member(lead, name="lead"), member(worker, name="worker", persistent=True)],
        run,
        channels=[shared_memory()],
    )
    assert seen == [
        MemberEnded("lead", "submitted", True),
        MemberIdle("worker", 0, not viewed),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("reader", ["own_loop", "subagent"])
async def test_a_view_clears_news_only_for_the_loop_that_viewed(reader: str) -> None:
    # three activations: an answer before the lead's write, one after a wake on its
    # news in which `reader` views the file, and one more
    viewing = (
        [_view(), _submit("two")]
        if reader == "own_loop"
        else [_call("agent", prompt="look"), _submit("two")]
    )
    worker = deepagent(
        model=scripted_model(_Recording([_submit("one"), *viewing, _submit("three")])),
        tools=[swarm_tools()],
        subagents=[
            research(
                extra_tools=[swarm_tools()],
                model=scripted_model(_Recording([_view(), _submit("seen")])),
            )
        ],
        memory=False,
        todo_write=False,
    )
    lead = _react(_Recording([_write(), _submit("lead's")]), tools=[swarm_tools()])
    seen: list[ControlEvent] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("worker")
        async with aclosing(control.events()) as events:
            async for event in events:
                seen.append(event)
                if isinstance(event, MemberIdle) and event.answer == 0:
                    control.start("lead")
                elif isinstance(event, MemberNews):
                    control.wake(event.member, "look at the board")
                elif isinstance(event, MemberIdle) and event.answer == 1:
                    control.wake(event.member, "again")
        return "quiescence"

    await _run(
        [member(worker, name="worker", persistent=True), member(lead, name="lead")],
        run,
        channels=[shared_memory()],
    )
    # the subagent's view clears only its own loop's news
    still = reader == "subagent"
    assert [e for e in seen if not isinstance(e, MemberEnded)] == [
        MemberIdle("worker", 0, False),
        MemberNews("worker", "shared_memory", "w-1", "lead"),
        MemberIdle("worker", 1, still),
        MemberIdle("worker", 2, still),
    ]
