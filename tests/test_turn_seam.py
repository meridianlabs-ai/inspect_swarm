import json
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import anyio
import pytest
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import (
    Agent,
    AgentChannel,
    AgentState,
    AgentSubmit,
    agent,
    agent_channel,
    as_tool,
    deepagent,
    react,
    research,
)
from inspect_ai.agent._channel import Cancel, UserMessage, current_agent_channel
from inspect_ai.dataset import Sample
from inspect_ai.event import (
    CommunicationEvent,
    CompactionEvent,
    Event,
    ModelEvent,
    SwarmEvent,
    ToolEvent,
)
from inspect_ai.log import EvalSample, transcript
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    CompactionAuto,
    CompactionSummary,
    GenerateConfig,
    Model,
    ModelOutput,
    ModelUsage,
    get_model,
)
from inspect_ai.tool import (
    MCPServer,
    Tool,
    ToolChoice,
    ToolDef,
    ToolInfo,
    ToolSource,
    tool,
)
from inspect_ai.util import (
    LimitExceededError,
    background,
    message_limit,
    suspend_working_limit,
    time_limit,
    token_limit,
)

from inspect_swarm import (
    Boundary,
    BoundaryCallback,
    Budget,
    MemberHandle,
    StopReason,
    SwarmControl,
    member,
    shared_memory,
    swarm,
    swarm_result,
    swarm_tools,
)
from inspect_swarm._controller._control import SwarmRun
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._metrics import SwarmMetrics
from inspect_swarm._record import SwarmResultRecord
from inspect_swarm._seam import message_hash

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Script,
    controller,
    generator,
    output,
    scripted_model,
    sleeper,
    wait,
)
from .test_binding import binding_test_loop, binding_test_member

Result = list[ChatMessage] | Literal["end"] | None


class _Recording(Script):
    """A script that keeps each call's input, and names its member in a shared log of call starts."""

    def __init__(
        self,
        outputs: Sequence[ModelOutput] = (),
        *,
        name: str = "",
        log: list[str] | None = None,
        gate: anyio.Event | None = None,
    ) -> None:
        super().__init__(outputs, gate=gate)
        self.name = name
        self.log = log
        self.inputs: list[list[ChatMessage]] = []

    async def __call__(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        self.inputs.append(list(input))
        if self.log is not None:
            self.log.append(self.name)
        return await super().__call__(input, tools, tool_choice, config)


def _call(function: str, **args: Any) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm", function, args)


def _submit(answer: str = "done") -> ModelOutput:
    return _call("submit", answer=answer)


@tool
def work() -> Tool:
    async def execute(seconds: float = 0) -> str:
        """Work for a while.

        Args:
            seconds: How long the work takes.
        """
        await anyio.sleep(seconds)
        return "worked"

    return execute


def _react(
    script: Script,
    *,
    tools: Sequence[Tool | ToolDef | ToolSource] = (),
    **kwargs: Any,
) -> Agent:
    return react(model=scripted_model(script), tools=[work(), *tools], **kwargs)


async def _drain(control: SwarmControl) -> StopReason:
    async with aclosing(control.events()) as events:
        async for _ in events:
            pass
    return "all_done"


def _controller(run: Callable[[SwarmControl], Awaitable[StopReason]]) -> Any:
    return controller(run, f"seam_test_{uuid4().hex}")


@dataclass
class _Ran:
    """A sample that ran a swarm through `eval_async()`."""

    sample: EvalSample

    @property
    def events(self) -> list[Event]:
        return self.sample.events

    @property
    def record(self) -> SwarmResultRecord:
        record = swarm_result(self.sample)
        assert record is not None
        return record


async def _eval(solver: Agent, **task: Any) -> EvalSample:
    # react needs a running sample
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=solver, **task),
        model="mockllm/model",
        log_dir=tempfile.mkdtemp(),
    )
    assert log.samples, log.error
    return log.samples[0]


def _swarm(
    members: list[Any],
    run_controller: Callable[[SwarmControl], Awaitable[StopReason]],
    *,
    channels: list[Any] | None = None,
    budget: Budget | None = None,
) -> Agent:
    return swarm(
        members,
        controller=_controller(run_controller),
        channels=channels if channels is not None else [],
        budget=budget or Budget(),
    )


async def _run(
    members: list[Any],
    run_controller: Callable[[SwarmControl], Awaitable[StopReason]],
    *,
    channels: list[Any] | None = None,
    budget: Budget | None = None,
) -> _Ran:
    sample = await _eval(
        _swarm(members, run_controller, channels=channels, budget=budget)
    )
    assert sample.error is None, sample.error.traceback
    return _Ran(sample)


async def _raised(
    members: list[Any],
    run_controller: Callable[[SwarmControl], Awaitable[StopReason]],
    error: type[BaseException],
    match: str,
) -> _Ran:
    # a swarm that a member's error leaves, as the sample's error
    sample = await _eval(_swarm(members, run_controller))
    assert sample.error is not None
    assert error.__name__ in sample.error.traceback, sample.error.traceback
    assert re.search(match, sample.error.message), sample.error.message
    return _Ran(sample)


def _starting(
    callbacks: Mapping[str, Sequence[tuple[str, BoundaryCallback]]],
) -> Callable[[SwarmControl], Awaitable[StopReason]]:
    # a controller that registers these callbacks on pending members, starts every
    # member and waits for them to end
    async def run_controller(control: SwarmControl) -> StopReason:
        for name, entries in callbacks.items():
            for origin, callback in entries:
                control.member(name).on_boundary(callback, origin=origin)
        for m in control.members:
            control.start(m)
        return await _drain(control)

    return run_controller


def _swarm_events(ran: _Ran, kind: str) -> list[SwarmEvent]:
    return [e for e in ran.events if isinstance(e, SwarmEvent) and e.kind == kind]


def _boundaries(ran: _Ran) -> list[dict[str, Any]]:
    return [e.data for e in _swarm_events(ran, "boundary")]


def _changes(ran: _Ran) -> list[dict[str, Any]]:
    return [e.data for e in _swarm_events(ran, "changed")]


def _metrics(ran: _Ran) -> SwarmMetrics:
    [event] = _swarm_events(ran, "metrics")
    return SwarmMetrics.model_validate(event.data)


def _ledger(ran: _Ran) -> SwarmLedger:
    [event] = _swarm_events(ran, "ledger")
    return SwarmLedger.model_validate(event.data)


def _statuses(ran: _Ran) -> dict[str, str | None]:
    return {m.name: m.status for m in ran.record.members}


def _user(text: str) -> ChatMessageUser:
    return ChatMessageUser(content=text)


def _without_id(message: ChatMessageUser) -> ChatMessageUser:
    message.id = None
    return message


# ---------------------------------------------------------------------------
# Boundaries and the callback contract


@pytest.mark.anyio
async def test_a_callback_holds_a_pending_member_at_boundary_0_and_its_message_reaches_the_model() -> (
    None
):
    script = _Recording([_call("work"), _submit()])
    seen: list[tuple[int, str, bool, int]] = []
    handle: list[MemberHandle] = []
    added = _user("from the controller")

    async def callback(b: Boundary) -> Result:
        seen.append((b.index, b.after, handle[0].held, script.started))
        if b.index == 0:
            await anyio.sleep(0.05)
            return [*b.messages, added]
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        handle.append(control.member("a"))
        control.member("a").on_boundary(callback, origin="test")
        assert not control.member("a").held
        control.start("a")
        return await _drain(control)

    outcome = await _run([member(_react(script), name="a")], run_controller)
    assert _statuses(outcome) == {"a": "submitted"}
    # held while its callbacks ran, before its first model call and after its tool turn
    assert seen == [(0, "start", True, 0), (1, "tools", True, 1)]
    assert script.inputs[0][-1].id == added.id
    [first, second] = _boundaries(outcome)
    assert first["outcome"] == "continue" and first["origins"] == ["test"]
    assert first["held_for"] >= 0.05
    assert first["messages"]["after"] == first["messages"]["before"] + 1
    [change] = _changes(outcome)
    assert change["added"] == [
        {
            "id": added.id,
            "hash": message_hash(added),
            "index": first["messages"]["before"],
        }
    ]
    assert change["applied"] is True
    assert second["after"] == "tools"
    metrics = _metrics(outcome).members[0]
    assert metrics.holds == 2 and metrics.held_time >= 0.05


@pytest.mark.anyio
async def test_after_is_reply_for_a_loop_that_continues_after_a_reply() -> None:
    script = _Recording([output("one"), output("two")])
    afters: list[str] = []

    async def keep_going(state: AgentState) -> bool | AgentState:
        # continue after the first reply with nothing added, then stop
        return state if len(afters) < 2 else False

    async def callback(b: Boundary) -> Result:
        afters.append(b.after)
        return None

    outcome = await _run(
        [member(_react(script, submit=False, on_continue=keep_going), name="a")],
        _starting({"a": [("test", callback)]}),
    )
    assert afters == ["start", "reply"]
    assert _statuses(outcome) == {"a": "submitted"}


@pytest.mark.anyio
async def test_callbacks_run_in_order_unregister_at_the_next_boundary_and_their_copies_change_nothing() -> (
    None
):
    script = _Recording([_call("work"), _call("work"), _submit()])
    first_added = _user("first")
    second_added = _user("second")
    received: list[list[str | None]] = []
    removed_at: list[int] = []
    unregister: list[Callable[[], None]] = []

    async def first(b: Boundary) -> Result:
        return [*b.messages, first_added] if b.index == 0 else None

    async def second(b: Boundary) -> Result:
        received.append([m.id for m in b.messages])
        return [*b.messages, second_added] if b.index == 0 else None

    async def removed(b: Boundary) -> Result:
        removed_at.append(b.index)
        unregister[0]()
        return None

    async def in_place(b: Boundary) -> Result:
        # changing the copy changes nothing
        b.messages.extend(_user("in place") for _ in range(12))
        b.messages[0].content = "rewritten in place"
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        handle = control.member("a")
        handle.on_boundary(first, origin="first")
        handle.on_boundary(second, origin="second")
        unregister.append(handle.on_boundary(removed, origin="removed"))
        handle.on_boundary(in_place, origin="in_place")
        control.start("a")
        return await _drain(control)

    outcome = await _run(
        # the in-place copy grows past the limit; the member's conversation does not
        [member(_react(script), name="a", limits=[message_limit(12)])],
        run_controller,
    )
    assert _statuses(outcome) == {"a": "submitted"}
    # the second callback received the first's result
    assert received[0][-1] == first_added.id
    # unregistered during boundary 0, it did not run at boundary 1
    assert removed_at == [0]
    model_input = script.inputs[0]
    assert [m.id for m in model_input[-2:]] == [first_added.id, second_added.id]
    assert "in place" not in [m.text for m in model_input]
    assert model_input[0].text != "rewritten in place"
    assert [c["origin"] for c in _changes(outcome)] == ["first", "second"]
    assert _boundaries(outcome)[0]["origins"] == [
        "first",
        "second",
        "removed",
        "in_place",
    ]


@pytest.mark.anyio
async def test_a_scheduler_alternates_two_members_while_a_pause_holds_one() -> None:
    calls: list[str] = []
    scripts = {
        name: _Recording(
            [_call("work"), _call("work"), _call("work"), _submit()],
            name=name,
            log=calls,
        )
        for name in ("a", "b")
    }
    paused_seen: list[tuple[int, bool]] = []
    resume = anyio.Event()

    async def run_controller(control: SwarmControl) -> StopReason:
        turn = ["a"]
        changed = [anyio.Event()]

        def flip(name: str) -> None:
            turn[0] = "b" if name == "a" else "a"
            changed[0].set()
            changed[0] = anyio.Event()

        def scheduler(name: str) -> Callable[[Boundary], Awaitable[Result]]:
            other = "b" if name == "a" else "a"

            async def callback(b: Boundary) -> Result:
                # one turn per activation: having taken its turn, the member parks
                # and the other goes
                if b.index:
                    flip(name)
                with suspend_working_limit():
                    while turn[0] != name and control.member(other).state != "ended":
                        await changed[0].wait()
                return None

            return callback

        async def pause(b: Boundary) -> Result:
            # an operator pauses `a` at its second boundary, after the scheduler let it go
            if b.index == 1:
                paused_seen.append((len(calls), control.member("b").held))
                with suspend_working_limit():
                    await resume.wait()
            return None

        control.member("a").on_boundary(scheduler("a"), origin="scheduler")
        control.member("a").on_boundary(pause, origin="operator")
        control.member("b").on_boundary(scheduler("b"), origin="scheduler")
        control.start("a")
        control.start("b")
        # `b` takes its turn, then waits for `a`, which the pause holds
        while not (paused_seen and control.member("b").held):
            await anyio.sleep(0.01)
        await anyio.sleep(0.05)
        assert calls == ["a", "b"]
        resume.set()
        # an end wakes the scheduler's waits
        async with aclosing(control.events()) as events:
            async for _ in events:
                changed[0].set()
                changed[0] = anyio.Event()
        return "all_done"

    outcome = await _run(
        [member(_react(s), name=name) for name, s in scripts.items()],
        run_controller,
    )
    assert _statuses(outcome) == {"a": "submitted", "b": "submitted"}
    assert calls == ["a", "b"] * 4


# ---------------------------------------------------------------------------
# Result checks and the message limit


def _invalid(kind: str, b: Boundary) -> list[ChatMessage]:
    match kind:
        case "empty":
            return []
        case "system-only":
            return [ChatMessageSystem(content="s")]
        case "assistant-and-tool":
            return [
                ChatMessageAssistant(content="a"),
                ChatMessageTool(content="t", tool_call_id="x"),
            ]
        case "duplicate-id":
            return [*b.messages, b.messages[-1]]
        case _:
            return [*b.messages, _without_id(_user("x"))]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind", ["empty", "system-only", "assistant-and-tool", "duplicate-id", "no-id"]
)
async def test_an_invalid_result_ends_the_member_errored(kind: str) -> None:
    script = _Recording([_submit()])

    async def callback(b: Boundary) -> Result:
        return _invalid(kind, b)

    transcript = await _raised(
        [member(_react(script), name="a")],
        _starting({"a": [("bad", callback)]}),
        ValueError,
        "Member 'a': the boundary callback with origin 'bad'",
    )
    assert script.started == 0
    [boundary] = _boundaries(transcript)
    assert boundary["outcome"] == "error"
    assert boundary["origin"] == "bad" and boundary["error"] == "ValueError"


@pytest.mark.anyio
@pytest.mark.parametrize("rewrite", ["one-user-left", "users-replaced"])
async def test_a_rewrite_that_keeps_a_user_message_continues(rewrite: str) -> None:
    script = _Recording([_submit()])
    new = _user("new task")

    async def callback(b: Boundary) -> Result:
        if b.index:
            return None
        if rewrite == "one-user-left":
            # the preamble alone stays
            return [m for m in b.messages if not isinstance(m, ChatMessageUser)] + [
                b.messages[-1]
            ]
        return [m for m in b.messages if not isinstance(m, ChatMessageUser)] + [new]

    outcome = await _run(
        [member(_react(script), name="a")], _starting({"a": [("test", callback)]})
    )
    assert _statuses(outcome) == {"a": "submitted"}
    users = [m for m in script.inputs[0] if isinstance(m, ChatMessageUser)]
    assert len(users) == 1
    if rewrite == "users-replaced":
        assert users[0].id == new.id


@pytest.mark.anyio
@pytest.mark.parametrize("then", ["trim", "end", "nothing"])
async def test_a_staged_conversation_over_the_message_limit(then: str) -> None:
    script = _Recording([_submit()])
    extra = [_user("one"), _user("two")]

    async def grow(b: Boundary) -> Result:
        return [*b.messages, *extra] if b.index == 0 else None

    async def after_grow(b: Boundary) -> Result:
        if b.index:
            return None
        if then == "trim":
            # back under the limit: the preamble swapped for the second extra message
            return [*b.messages[:-3], b.messages[-1]]
        if then == "end":
            return "end"
        return None

    outcome = await _run(
        # react's system prompt, the input and the preamble, with room for one more
        [member(_react(script), name="a", limits=[message_limit(4)])],
        _starting({"a": [("grow", grow), ("then", after_grow)]}),
    )
    [boundary] = _boundaries(outcome)
    changes = _changes(outcome)
    if then == "trim":
        assert boundary["outcome"] == "continue"
        assert [m.id for m in script.inputs[0]][-1] == extra[1].id
        assert len(script.inputs[0]) == 3
        assert all(c["applied"] for c in changes)
    elif then == "end":
        assert _statuses(outcome) == {"a": "stopped"}
        assert boundary["outcome"] == "end" and boundary["origin"] == "then"
        assert script.started == 0
        assert not any(c["applied"] for c in changes)
    else:
        # the limit refuses the append when it is applied
        assert _statuses(outcome) == {"a": "member_limit:message"}
        assert boundary["outcome"] == "rejected"
        assert boundary["error"] == "LimitExceededError"
        assert boundary["messages"]["after"] == boundary["messages"]["before"]
        assert [c["applied"] for c in changes] == [False]
        assert script.started == 0


# ---------------------------------------------------------------------------
# Ending a member


@pytest.mark.anyio
async def test_a_callback_returning_end_stops_the_member_and_the_swarm_goes_on() -> (
    None
):
    a = _Recording([_call("work"), _submit()])
    b = _Recording([_submit("b's answer")])

    async def callback(bd: Boundary) -> Result:
        return "end" if bd.index == 1 else None

    outcome = await _run(
        [member(_react(a), name="a"), member(_react(b), name="b")],
        _starting({"a": [("ender", callback)]}),
    )
    assert _statuses(outcome) == {"a": "stopped", "b": "submitted"}
    assert a.started == 1
    assert outcome.record.final.member == "b"
    assert _boundaries(outcome)[-1]["outcome"] == "end"


def slow_submit(release: anyio.Event, running: anyio.Event) -> ToolDef:
    # a submit tool that runs until released; a ToolDef, since a registered tool's
    # arguments are logged
    async def execute(answer: str) -> str:
        """Submit an answer.

        Args:
            answer: The answer.
        """
        running.set()
        await release.wait()
        return answer

    return ToolDef(execute, name="submit")


@pytest.mark.anyio
@pytest.mark.parametrize("ended", [True, False])
@pytest.mark.parametrize("loop", ["submit", "reply"])
async def test_end_while_the_last_turn_runs_stops_the_member(
    ended: bool, loop: str
) -> None:
    release = anyio.Event()
    running = anyio.Event()
    if loop == "submit":
        script = _Recording([_submit("answer")])
        agent = _react(script, submit=AgentSubmit(tool=slow_submit(release, running)))
    else:
        # the reply that ends the loop is held in flight
        script = _Recording([output("answer")], gate=release)
        agent = _react(script, submit=False)

    async def run_controller(control: SwarmControl) -> StopReason:
        control.start("a")
        if loop == "submit":
            await running.wait()
        else:
            while not script.started:
                await anyio.sleep(0.01)
        if ended:
            control.member("a").end(origin="operator")
        release.set()
        return await _drain(control)

    outcome = await _run([member(agent, name="a")], run_controller)
    assert _statuses(outcome) == {"a": "stopped" if ended else "submitted"}
    assert outcome.record.final.member == (None if ended else "a")


@pytest.mark.anyio
@pytest.mark.parametrize("seconds,status", [(5.0, "cancelled"), (0.05, "stopped")])
async def test_end_during_a_tool_call_waits_out_the_grace(
    seconds: float, status: str
) -> None:
    script = _Recording([_call("work", seconds=seconds), _submit()])
    callback_ran: list[int] = []

    async def callback(b: Boundary) -> Result:
        callback_ran.append(b.index)
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="test")
        control.start("a")
        while not script.calls:
            await anyio.sleep(0.01)
        await anyio.sleep(0.01)
        control.member("a").end(origin="operator")
        # a second end changes nothing
        control.member("a").end(origin="other")
        return await _drain(control)

    outcome = await _run(
        [member(_react(script), name="a")], run_controller, budget=Budget(grace=0.3)
    )
    assert _statuses(outcome) == {"a": status}
    assert script.started == 1
    # the end came before its next boundary's callbacks
    assert callback_ran == [0]
    assert _ledger(outcome).members[0].cancelled is (status == "cancelled")


@pytest.mark.anyio
@pytest.mark.parametrize("operator_first", [True, False])
async def test_end_cancels_a_waiting_callback_and_no_change_of_the_chain_applies(
    operator_first: bool,
) -> None:
    a = _Recording([_call("work"), _submit()])
    b = _Recording([_call("work", seconds=0.1), _submit("b's")])
    ran: list[str] = []
    waiting = anyio.Event()
    lever_reason: list[str | None] = []

    async def scheduler(bd: Boundary) -> Result:
        if bd.index == 0:
            return None
        ran.append("scheduler")
        waiting.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            ran.append("scheduler cancelled")
            raise
        return None

    async def operator(bd: Boundary) -> Result:
        if bd.index:
            ran.append("operator")
            return [*bd.messages, _user("staged")]
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        handle = control.member("a")
        order = [("operator", operator), ("scheduler", scheduler)]
        for origin, callback in order if operator_first else reversed(order):
            handle.on_boundary(callback, origin=origin)
        control.start("a")
        control.start("b")
        await waiting.wait()
        assert handle.held
        handle.end(origin="operator")
        result = await _drain(control)
        lever_reason.append(cast(SwarmRun, control)._lookup("a").lever.reason)
        return result

    outcome = await _run(
        [member(_react(a), name="a"), member(_react(b), name="b")], run_controller
    )
    assert _statuses(outcome) == {"a": "stopped", "b": "submitted"}
    assert ran == (
        ["operator", "scheduler", "scheduler cancelled"]
        if operator_first
        else ["scheduler", "scheduler cancelled"]
    )
    assert lever_reason == ["ended by operator"]
    boundary = _boundaries(outcome)[-1]
    assert boundary["outcome"] == "end" and boundary["origin"] == "operator"
    assert not any(c["applied"] for c in _changes(outcome) if c["boundary"] == 1)
    assert a.started == 1


@pytest.mark.anyio
async def test_end_on_a_pending_member_ends_it_at_boundary_0() -> None:
    script = _Recording([_submit()])

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").end(origin="operator")
        control.start("a")
        return await _drain(control)

    outcome = await _run([member(_react(script), name="a")], run_controller)
    assert _statuses(outcome) == {"a": "stopped"}
    assert script.started == 0
    assert _boundaries(outcome) == []


@pytest.mark.anyio
@pytest.mark.parametrize("request_", ["end", "stop", None])
@pytest.mark.parametrize("returns", ["none", "list"])
async def test_an_end_or_stop_from_inside_the_chain_discards_it(
    request_: str | None, returns: str
) -> None:
    script = _Recording([_call("work"), _submit()])
    ran: list[str] = []
    staged = _user("staged")
    control_ref: list[SwarmControl] = []

    async def first(b: Boundary) -> Result:
        return [*b.messages, staged] if b.index == 0 else None

    async def requester(b: Boundary) -> Result:
        if b.index == 0:
            if request_ == "end":
                control_ref[0].member("a").end(origin="self")
            elif request_ == "stop":
                cast(SwarmRun, control_ref[0]).stop("controller:test")
        return [*b.messages, _user("mine")] if returns == "list" else None

    async def later(b: Boundary) -> Result:
        ran.append("later")
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control_ref.append(control)
        for origin, callback in (
            ("first", first),
            ("requester", requester),
            ("later", later),
        ):
            control.member("a").on_boundary(callback, origin=origin)
        control.start("a")
        return await _drain(control)

    outcome = await _run([member(_react(script), name="a")], run_controller)
    boundary = _boundaries(outcome)[0]
    if request_ is None:
        assert _statuses(outcome) == {"a": "submitted"}
        assert boundary["outcome"] == "continue"
        assert staged.id in [m.id for m in script.inputs[0]]
        assert all(c["applied"] for c in _changes(outcome))
    else:
        assert _statuses(outcome) == {"a": "stopped"}
        assert boundary["outcome"] == request_
        assert ran == []
        assert script.started == 0
        assert not any(c["applied"] for c in _changes(outcome))


@pytest.mark.anyio
@pytest.mark.parametrize("held", [True, False])
async def test_a_stop_ends_a_member_at_its_boundary_with_no_further_generation(
    held: bool,
) -> None:
    script = _Recording([_call("work", seconds=0.1), _submit()])

    async def park(b: Boundary) -> Result:
        if b.index == 1:
            with suspend_working_limit():
                await anyio.sleep_forever()
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        if held:
            control.member("a").on_boundary(park, origin="parker")
        control.start("a")
        if held:
            while not control.member("a").held or not script.calls:
                await anyio.sleep(0.01)
        else:
            # in the tool call when the stop begins
            while not script.calls:
                await anyio.sleep(0.01)
        return "done"

    outcome = await _run(
        [member(_react(script), name="a")], run_controller, budget=Budget(grace=5)
    )
    assert _statuses(outcome) == {"a": "stopped"}
    assert script.started == 1
    assert _ledger(outcome).members[0].cancelled is False
    if held:
        assert _boundaries(outcome)[-1]["outcome"] == "stop"


def _failing(input: list[ChatMessage]) -> ModelOutput:
    raise ValueError("the judge failed")


@pytest.mark.anyio
@pytest.mark.parametrize("call", ["in-flight", "completed", "failed", "none"])
@pytest.mark.parametrize("ending", ["stop", "end"])
async def test_only_a_callback_model_call_in_flight_at_the_cancellation_marks_the_ledger_line_cancelled(
    call: str, ending: str
) -> None:
    script = _Recording([_submit()])
    judges = {
        "in-flight": Script(gate=anyio.Event()),
        "completed": Script(),
        "failed": Script(respond=_failing),
    }
    judge = scripted_model(judges.get(call))

    async def callback(b: Boundary) -> Result:
        if call == "in-flight":
            await judge.generate("judge")
        elif call == "completed":
            await judge.generate("judge")
        elif call == "failed":
            # a failure the callback handles
            with pytest.raises(ValueError):
                await judge.generate("judge")
        await anyio.sleep_forever()
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="judge")
        control.start("a")
        while not control.member("a").held:
            await anyio.sleep(0.01)
        await anyio.sleep(0.05)
        if ending == "stop":
            return "done"
        control.member("a").end(origin="operator")
        return await _drain(control)

    outcome = await _run(
        [member(_react(script), name="a")], run_controller, budget=Budget(grace=5)
    )
    assert _statuses(outcome) == {"a": "stopped"}
    [line] = _ledger(outcome).members
    assert line.cancelled is (call == "in-flight")
    # the handled failure is still counted as a call whose cost is missing
    assert line.calls.errored == (1 if call in ("in-flight", "failed") else 0)


@agent
def slow_cleanup(inner: Agent, seconds: float, blocking: float = 0) -> Agent:
    """Run `inner`, then clean up, however it ended: block the event loop for `blocking` seconds, then wait `seconds`."""

    async def execute(state: AgentState) -> AgentState:
        try:
            return await inner(state)
        finally:
            time.sleep(blocking)
            await anyio.sleep(seconds)

    return execute


class _Server(MCPServer):
    """An MCP server with no tools, whose connection takes `opening` seconds to open and `closing` seconds to close."""

    def __init__(self, opening: float = 0, closing: float = 0) -> None:
        self.opening = opening
        self.closing = closing
        self.opened = False

    async def tools(self) -> list[Tool]:
        return []

    async def __aenter__(self) -> "_Server":
        await anyio.sleep(self.opening)
        self.opened = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await anyio.sleep(self.closing)


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["end()", "callback"])
@pytest.mark.parametrize("cleanup", ["finally", "mcp"])
@pytest.mark.parametrize("seconds,status", [(5.0, "cancelled"), (0.05, "stopped")])
async def test_ending_a_held_member_gives_its_cleanup_the_grace(
    ending: str, cleanup: str, seconds: float, status: str
) -> None:
    script = _Recording([_submit()])

    async def callback(b: Boundary) -> Result:
        if ending == "callback":
            return "end"
        await anyio.sleep_forever()
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="test")
        control.start("a")
        if ending == "end()":
            while not control.member("a").held:
                await anyio.sleep(0.01)
            control.member("a").end(origin="operator")
        return await _drain(control)

    if cleanup == "finally":
        agent = slow_cleanup(_react(script), seconds)
    else:
        agent = _react(script, tools=[_Server(closing=seconds)])
    with anyio.fail_after(3):
        outcome = await _run(
            [member(agent, name="a")], run_controller, budget=Budget(grace=0.3)
        )
    assert _statuses(outcome) == {"a": status}
    assert script.started == 0
    assert _ledger(outcome).members[0].cancelled is (status == "cancelled")


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["end()", "callback"])
@pytest.mark.parametrize("blocking,status", [(0.3, "cancelled"), (0.02, "stopped")])
async def test_the_end_grace_counts_from_the_request(
    ending: str, blocking: float, status: str
) -> None:
    script = _Recording([_submit()])

    async def callback(b: Boundary) -> Result:
        if ending == "callback":
            return "end"
        await anyio.sleep_forever()
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="test")
        control.start("a")
        if ending == "end()":
            while not control.member("a").held:
                await anyio.sleep(0.01)
            control.member("a").end(origin="operator")
        return await _drain(control)

    # cleanup that blocks the event loop, then waits briefly: past the deadline once
    # it yields when the blocking outlasts the grace
    agent_ = slow_cleanup(_react(script), 0.05, blocking=blocking)
    with anyio.fail_after(3):
        outcome = await _run(
            [member(agent_, name="a")], run_controller, budget=Budget(grace=0.15)
        )
    assert _statuses(outcome) == {"a": status}


@agent
def stops_itself(how: str) -> Agent:
    """Wait a moment, then stop itself with a custom limit error (`bare` or `grouped`), or `return` an answer."""

    async def execute(state: AgentState) -> AgentState:
        await anyio.sleep(0.2)
        if how == "return":
            state.output = ModelOutput.from_content("mockllm", "answer")
            return state

        def stop() -> LimitExceededError:
            return LimitExceededError("custom", value=1, limit=1, message="done")

        if how == "bare":
            raise stop()
        raise ExceptionGroup("stopped", [stop(), stop()])

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["bare", "grouped", "return"])
@pytest.mark.parametrize("ended", [True, False])
async def test_an_end_request_does_not_change_an_agents_own_stop(
    how: str, ended: bool
) -> None:
    async def run_controller(control: SwarmControl) -> StopReason:
        control.start("a")
        await anyio.sleep(0.05)
        if ended:
            control.member("a").end(origin="operator")
        return await _drain(control)

    outcome = await _run([member(stops_itself(how), name="a")], run_controller)
    if how == "return":
        assert _statuses(outcome) == {"a": "stopped" if ended else "submitted"}
    else:
        assert _statuses(outcome) == {"a": "member_limit:custom"}


@pytest.mark.anyio
async def test_a_stop_with_no_grace_ends_a_parked_member_with_no_generation() -> None:
    script = _Recording([_submit()])

    async def park(b: Boundary) -> Result:
        await anyio.sleep_forever()
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(park, origin="parker")
        control.start("a")
        while not control.member("a").held:
            await anyio.sleep(0.01)
        return "done"

    outcome = await _run(
        [member(_react(script), name="a")], run_controller, budget=Budget(grace=0)
    )
    # the hard stop may reach it before its wrapper records the end
    assert _statuses(outcome)["a"] in ("stopped", "cancelled")
    assert script.started == 0


def _cached_model(script: Script) -> Model:
    return get_model(
        f"mockllm/m-{uuid4().hex}",
        memoize=False,
        custom_outputs=script,
        config=GenerateConfig(cache=True),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["held", "mid-turn", "callback"])
@pytest.mark.parametrize("ending", ["stop", "end"])
@pytest.mark.parametrize("waiting", ["reply", "tools"])
async def test_no_cached_reply_or_tool_call_runs_after_a_stop_or_end(
    where: str,
    ending: str,
    waiting: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("INSPECT_CACHE_DIR", str(tmp_path))
    second = output("not yet") if waiting == "reply" else _call("work")
    script = Script([_call("work", seconds=0.5), second, _submit()])
    model = _cached_model(script)
    control_ref: list[SwarmControl] = []

    def request(control: SwarmControl) -> None:
        if ending == "stop":
            cast(SwarmRun, control).stop("controller:test")
        else:
            control.member("a").end(origin="operator")

    async def callback(b: Boundary) -> Result:
        if b.index == 1:
            if where == "callback":
                request(control_ref[0])
            else:
                await anyio.sleep_forever()
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control_ref.append(control)
        if where != "mid-turn":
            control.member("a").on_boundary(callback, origin="test")
        control.start("a")
        if where == "held":
            while not control.member("a").held:
                await anyio.sleep(0.01)
            request(control)
        elif where == "mid-turn":
            # the cached first reply has returned, and its tool call is running
            await anyio.sleep(0.2)
            request(control)
        return await _drain(control)

    # warm the cache: the whole conversation, every reply cached
    warm = await _run(
        [member(react(model=model, tools=[work()]), name="a")], _starting({})
    )
    assert _statuses(warm) == {"a": "submitted"}
    assert script.started == 3

    outcome = await _run(
        [member(react(model=model, tools=[work()]), name="a")],
        run_controller,
        budget=Budget(grace=5),
    )
    assert _statuses(outcome) == {"a": "stopped"}
    calls = [e for e in outcome.events if isinstance(e, ModelEvent)]
    # only the first reply, read from the cache
    assert [e.cache for e in calls] == ["read"]
    assert script.started == 3
    tools = [e for e in outcome.events if isinstance(e, ToolEvent)]
    assert [e.function for e in tools] == ["work"]
    assert _ledger(outcome).members[0].cancelled is False


# ---------------------------------------------------------------------------
# Interrupts


def stall(interrupted: anyio.Event, channels: list[AgentChannel]) -> ToolDef:
    # a tool that interrupts its own turn, as an operator's interrupt does
    async def execute() -> str:
        """Wait until interrupted."""
        channel = current_agent_channel()
        channels.append(channel)
        channel._interrupt(Cancel())
        interrupted.set()
        await anyio.sleep_forever()
        return "never"

    return ToolDef(execute, name="stall")


@pytest.mark.anyio
@pytest.mark.parametrize("then", ["redirect", "end", "stop", "time"])
@pytest.mark.parametrize("loop", ["submit", "reply"])
async def test_a_member_waiting_for_a_redirect_is_mid_turn_not_held(
    then: str, loop: str
) -> None:
    interrupted = anyio.Event()
    channels: list[AgentChannel] = []
    final = _submit() if loop == "submit" else output("answer")
    script = _Recording([_call("stall"), final])
    waiter = _Recording([_call("swarm_wait", timeout=1), _submit("waiter's")])
    ran: list[tuple[int, str, str]] = []

    async def callback(b: Boundary) -> Result:
        ran.append((b.index, b.after, b.messages[-1].text))
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        handle = control.member("a")
        handle.on_boundary(callback, origin="test")
        control.start("a")
        await interrupted.wait()
        # in after_cancel(), waiting for the redirect
        await anyio.sleep(0.05)
        assert not handle.held
        if then == "stop":
            return "done"
        if then == "end":
            handle.end(origin="operator")
        elif then == "redirect":
            # a wait in another member does not see it as waiting
            control.start("waiter")
            while control.member("waiter").state != "ended":
                await anyio.sleep(0.01)
            assert not handle.held
            channels[0]._post(UserMessage(message=ChatMessageUser(content="REDIRECT")))
        return await _drain(control)

    outcome = await _run(
        [
            member(
                _react(
                    script,
                    tools=[stall(interrupted, channels)],
                    submit=loop == "submit",
                ),
                name="a",
                limits=[time_limit(0.3)] if then == "time" else [],
            ),
            member(_react(waiter, tools=[swarm_tools()]), name="waiter"),
        ],
        run_controller,
        channels=[shared_memory()],
        budget=Budget(grace=0.1),
    )
    status = {
        "redirect": "submitted",
        "end": "cancelled",
        "stop": "cancelled",
        "time": "member_limit:time",
    }[then]
    assert _statuses(outcome)["a"] == status
    if then == "redirect":
        [waited] = [
            e
            for e in outcome.events
            if isinstance(e, CommunicationEvent) and e.kind == "waited"
        ]
        assert wait(waited).outcome == "timeout"
        # the redirect continued it to a boundary where the callback ran
        assert [r[:2] for r in ran] == [(0, "start"), (1, "user")]
        assert ran[1][2] == "REDIRECT"
    else:
        assert [r[:2] for r in ran] == [(0, "start")]
    assert _metrics(outcome).members[0].holds == len(ran)


# ---------------------------------------------------------------------------
# Time


@pytest.mark.anyio
@pytest.mark.parametrize(
    "work_seconds,status", [(0.2, "submitted"), (0.7, "member_limit:time")]
)
async def test_a_member_time_limit_does_not_run_while_held(
    work_seconds: float, status: str
) -> None:
    script = _Recording([_call("work", seconds=work_seconds), _submit()])

    async def hold(b: Boundary) -> Result:
        if b.index == 0:
            await anyio.sleep(0.8)
        return None

    outcome = await _run(
        [member(_react(script), name="a", limits=[time_limit(0.5)])],
        _starting({"a": [("hold", hold)]}),
    )
    assert _statuses(outcome) == {"a": status}


@agent
def busy_first(inner: Agent, seconds: float) -> Agent:
    """Block the event loop for `seconds` before running `inner`."""

    async def execute(state: AgentState) -> AgentState:
        import time

        time.sleep(seconds)
        return await inner(state)

    return execute


@tool
def busy() -> Tool:
    async def execute() -> str:
        """Block the event loop for a while."""
        import time

        time.sleep(0.3)
        return "busy"

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["before-boundary-0", "in-a-tool"])
@pytest.mark.parametrize("limited", [True, False])
async def test_an_overdue_member_time_limit_ends_the_member_without_a_hold(
    where: str, limited: bool
) -> None:
    script = _Recording([_call("busy"), _submit()])
    ran: list[int] = []
    inner = _react(script, tools=[busy()])
    agent = busy_first(inner, 0.3) if where == "before-boundary-0" else inner

    async def callback(b: Boundary) -> Result:
        ran.append(b.index)
        return None

    outcome = await _run(
        [member(agent, name="a", limits=[time_limit(0.15)] if limited else [])],
        _starting({"a": [("test", callback)]}),
    )
    if not limited:
        assert _statuses(outcome) == {"a": "submitted"}
        assert ran == [0, 1]
    elif where == "before-boundary-0":
        assert _statuses(outcome) == {"a": "member_limit:time"}
        assert ran == [] and script.started == 0
    else:
        assert _statuses(outcome) == {"a": "member_limit:time"}
        assert ran == [0] and script.started == 1


async def _eval_sample(solver: Agent, tmp_path: Path) -> EvalSample:
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=solver),
        model="mockllm/model",
        log_dir=str(tmp_path),
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    return log.samples[0]


@pytest.mark.anyio
@pytest.mark.parametrize("marked", ["both", "neither", "one"])
async def test_a_callback_marks_its_own_waits_as_waiting_time(
    marked: str, tmp_path: Path
) -> None:
    hold = 0.6

    def callback(mark: bool) -> Callable[[Boundary], Awaitable[Result]]:
        async def execute(b: Boundary) -> Result:
            if b.index == 0:
                if mark:
                    with suspend_working_limit():
                        await anyio.sleep(hold)
                else:
                    await anyio.sleep(hold)
            return None

        return execute

    marks = {"both": (True, True), "neither": (False, False), "one": (True, False)}[
        marked
    ]
    sample_ = await _eval_sample(
        swarm(
            [
                member(_react(_Recording([_submit()])), name="a"),
                member(_react(_Recording([_submit()])), name="b"),
            ],
            controller=_controller(
                _starting(
                    {
                        "a": [("test", callback(marks[0]))],
                        "b": [("test", callback(marks[1]))],
                    }
                )
            ),
            channels=[],
        ),
        tmp_path,
    )
    assert sample_.working_time is not None
    if marked == "both":
        assert sample_.working_time < hold / 2
    else:
        # an unmarked wait counts as working time, whatever the other one does
        assert sample_.working_time >= hold * 0.9


@pytest.mark.anyio
async def test_swarm_wait_returns_all_waiting_when_the_only_other_running_member_is_held() -> (
    None
):
    waiter = _Recording([_call("swarm_wait", timeout=30), _submit()])
    held_script = _Recording([_submit()])
    ended = anyio.Event()

    async def hold(b: Boundary) -> Result:
        with suspend_working_limit():
            await ended.wait()
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("held").on_boundary(hold, origin="hold")
        control.start("held")
        while not control.member("held").held:
            await anyio.sleep(0.01)
        control.start("waiter")
        async with aclosing(control.events()) as events:
            async for event in events:
                if event.member == "waiter":
                    ended.set()
        return "all_done"

    outcome = await _run(
        [
            member(_react(waiter, tools=[swarm_tools()]), name="waiter"),
            member(_react(held_script), name="held"),
        ],
        run_controller,
        channels=[shared_memory()],
    )
    assert _statuses(outcome) == {"waiter": "submitted", "held": "submitted"}
    [waited] = [
        e
        for e in outcome.events
        if isinstance(e, CommunicationEvent) and e.kind == "waited"
    ]
    assert wait(waited).outcome == "all_waiting"


# ---------------------------------------------------------------------------
# Refusals and supported members


@pytest.mark.anyio
async def test_on_boundary_refuses_unsupported_members_and_bad_origins() -> None:
    async def callback(b: Boundary) -> Result:
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        for name in ("bridged", "background", "background-4"):
            with pytest.raises(ValueError, match="cannot be held"):
                control.member(name).on_boundary(callback, origin="test")
        for origin in ("", "a b", "x" * 65, "swarm"):
            with pytest.raises(ValueError, match="origin"):
                control.member("plain").on_boundary(callback, origin=origin)
            with pytest.raises(ValueError, match="origin"):
                control.member("plain").end(origin=origin)
        control.start("plain")
        await _drain(control)
        with pytest.raises(RuntimeError, match="has ended"):
            control.member("plain").on_boundary(callback, origin="test")
        # a second end, of an ended member, changes nothing
        control.member("plain").end(origin="late")
        cast(SwarmRun, control).stop("controller:test")
        with pytest.raises(RuntimeError, match="stopping"):
            control.member("other").on_boundary(callback, origin="test")
        return "done"

    outcome = await _run(
        [
            member(generator(scripted_model()), name="plain"),
            member(generator(scripted_model()), name="other"),
            member(generator(scripted_model()), name="bridged", bridged=True),
            member(deepagent, args={"background": True}, name="background"),
            member(deepagent, args={"background": 4}, name="background-4"),
        ],
        run_controller,
    )
    assert _statuses(outcome)["plain"] == "submitted"


@agent
def no_state_loop(model: Model) -> Agent:
    """A custom loop that calls before_turn() without its state."""

    async def execute(state: AgentState) -> AgentState:
        async with agent_channel() as ch:
            state.messages.extend(await ch.before_turn(state.messages))
            state.output = await model.generate(state.messages)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize(
    "build,unregister",
    [
        pytest.param(lambda: generator(scripted_model()), False, id="generator"),
        pytest.param(
            lambda: generator(scripted_model()), True, id="generator-unregistered"
        ),
        pytest.param(lambda: sleeper(0), False, id="sleeper"),
        pytest.param(
            lambda: generator(scripted_model(), calls=0, answer="x"),
            False,
            id="constant",
        ),
    ],
)
async def test_a_member_with_callbacks_that_never_reaches_a_boundary_ends_errored(
    build: Callable[[], Agent], unregister: bool
) -> None:
    ran: list[int] = []

    async def callback(b: Boundary) -> Result:
        ran.append(b.index)
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        remove = control.member("a").on_boundary(callback, origin="test")
        if unregister:
            remove()
        control.start("a")
        return await _drain(control)

    transcript = await _raised(
        [member(build(), name="a")], run_controller, RuntimeError, "never reached"
    )
    assert ran == []
    assert _boundaries(transcript) == []
    assert transcript.record.final.member is None
    assert _statuses(transcript) == {"a": "errored"}


@pytest.mark.anyio
async def test_a_member_limit_ending_an_unsupervised_member_is_an_error() -> None:
    async def callback(b: Boundary) -> Result:
        return None

    await _raised(
        [
            member(
                generator(scripted_model(), calls=None),
                name="a",
                limits=[token_limit(TOKENS_PER_CALL)],
            )
        ],
        _starting({"a": [("test", callback)]}),
        RuntimeError,
        "never reached",
    )


@pytest.mark.anyio
async def test_members_without_callbacks_end_as_before() -> None:
    outcome = await _run(
        [
            member(generator(scripted_model()), name="generator"),
            member(sleeper(0), name="sleeper"),
            member(
                generator(scripted_model(), calls=None),
                name="limited",
                limits=[token_limit(TOKENS_PER_CALL)],
            ),
        ],
        _starting({}),
    )
    assert _statuses(outcome) == {
        "generator": "submitted",
        "sleeper": "submitted",
        "limited": "member_limit:token",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("with_callback", [True, False])
async def test_a_react_member_whose_setup_exceeds_its_message_limit(
    with_callback: bool,
) -> None:
    async def callback(b: Boundary) -> Result:
        return None

    members = [
        # react's system prompt added to the input and the preamble
        member(_react(_Recording([_submit()])), name="a", limits=[message_limit(2)]),
        member(_react(_Recording([_call("work", seconds=0.2), _submit()])), name="b"),
    ]
    callbacks = {"a": [("test", callback)]} if with_callback else {}
    if with_callback:
        await _raised(members, _starting(callbacks), RuntimeError, "never reached")
    else:
        outcome = await _run(members, _starting(callbacks))
        assert _statuses(outcome) == {"a": "member_limit:message", "b": "submitted"}


@pytest.mark.anyio
async def test_a_react_member_with_a_message_limit_reaches_its_gate_and_submits() -> (
    None
):
    ran: list[int] = []

    async def callback(b: Boundary) -> Result:
        ran.append(b.index)
        return None

    outcome = await _run(
        [member(_react(_Recording([_submit()])), name="a", limits=[message_limit(10)])],
        _starting({"a": [("test", callback)]}),
    )
    assert _statuses(outcome) == {"a": "submitted"}
    assert ran == [0]


@pytest.mark.anyio
async def test_a_loop_that_ends_at_a_reply_after_its_first_boundary_submits() -> None:
    async def callback(b: Boundary) -> Result:
        return None

    outcome = await _run(
        [member(_react(_Recording([output("answer")]), submit=False), name="a")],
        _starting({"a": [("test", callback)]}),
    )
    assert _statuses(outcome) == {"a": "submitted"}


@pytest.mark.anyio
async def test_a_loop_that_does_not_pass_its_state_is_refused_with_callbacks() -> None:
    async def callback(b: Boundary) -> Result:
        return None

    await _raised(
        [member(no_state_loop(scripted_model()), name="a")],
        _starting({"a": [("test", callback)]}),
        RuntimeError,
        "does not pass its state",
    )
    outcome = await _run(
        [member(no_state_loop(scripted_model()), name="a")], _starting({})
    )
    assert _statuses(outcome) == {"a": "submitted"}


@tool
def start_background(seconds: float, wait: float) -> Tool:
    async def execute() -> str:
        """Start background work, then wait."""
        background(anyio.sleep, seconds)
        await anyio.sleep(wait)
        return "started"

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("finished", [False, True])
async def test_a_member_with_background_work_running_cannot_be_held(
    finished: bool,
) -> None:
    script = _Recording([_call("start_background"), _submit()])
    ran: list[int] = []

    async def callback(b: Boundary) -> Result:
        ran.append(b.index)
        return None

    # the task outlives its tool call, or finishes within it
    tool_ = start_background(0.05, 0.2) if finished else start_background(30, 0)
    members = [member(_react(script, tools=[tool_]), name="a")]
    if finished:
        outcome = await _run(members, _starting({"a": [("test", callback)]}))
        assert _statuses(outcome) == {"a": "submitted"}
        assert ran == [0, 1]
    else:
        await _raised(
            members,
            _starting({"a": [("test", callback)]}),
            RuntimeError,
            "background work running",
        )
        assert ran == [0]


@pytest.mark.anyio
async def test_a_callback_cannot_start_background_work_while_the_member_is_held() -> (
    None
):
    async def callback(b: Boundary) -> Result:
        background(anyio.sleep, 0)
        return None

    await _raised(
        [member(_react(_Recording([_submit()])), name="a")],
        _starting({"a": [("test", callback)]}),
        RuntimeError,
        "held at a turn boundary",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "ending,opening,status",
    [
        ("stop", 5.0, "cancelled"),
        ("stop", 0.1, "stopped"),
        ("end", 5.0, "cancelled"),
        ("end", 0.1, "stopped"),
    ],
)
@pytest.mark.parametrize("with_callback", [True, False])
async def test_ending_a_member_still_opening_its_mcp_server_is_as_without_the_seam(
    ending: str, opening: float, status: str, with_callback: bool
) -> None:
    server = _Server(opening=opening)
    script = _Recording([_submit()])
    ran: list[int] = []

    async def callback(b: Boundary) -> Result:
        ran.append(b.index)
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        if with_callback:
            control.member("a").on_boundary(callback, origin="test")
        control.start("a")
        await anyio.sleep(0.05)
        # before boundary 0
        assert not server.opened
        if ending == "stop":
            return "done"
        control.member("a").end(origin="operator")
        return await _drain(control)

    with anyio.fail_after(3):
        outcome = await _run(
            [member(_react(script, tools=[server]), name="a")],
            run_controller,
            budget=Budget(grace=0.5),
        )
    # no RuntimeError for callbacks that never ran
    assert _statuses(outcome) == {"a": status}
    assert server.opened is (status == "stopped")
    assert script.started == 0
    assert ran == [] and _boundaries(outcome) == []


@agent
def cancelled_after(inner: Agent, seconds: float, caught: list[bool]) -> Agent:
    """Run `inner`, cancelled from outside after `seconds`."""

    async def execute(state: AgentState) -> AgentState:
        with anyio.move_on_after(seconds) as scope:
            state = await inner(state)
        caught.append(scope.cancelled_caught)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("with_callback", [True, False])
async def test_a_cancellation_from_outside_the_swarm_reaches_a_member_in_setup(
    with_callback: bool,
) -> None:
    server = _Server(opening=30)
    callbacks: dict[str, list[tuple[str, BoundaryCallback]]] = {}

    async def callback(b: Boundary) -> Result:
        return None

    if with_callback:
        callbacks["a"] = [("test", callback)]
    caught: list[bool] = []
    swarm_ = _swarm(
        [member(_react(_Recording([_submit()]), tools=[server]), name="a")],
        _starting(callbacks),
    )
    with anyio.fail_after(5):
        sample = await _eval(cancelled_after(swarm_, 0.2, caught))
    # it propagated to the scope outside the swarm, with no RuntimeError
    assert sample.error is None
    assert caught == [True]
    assert not server.opened


# ---------------------------------------------------------------------------
# Stale compaction summaries


@pytest.mark.anyio
@pytest.mark.parametrize(
    "edit,kept",
    [
        ("before", False),
        ("delete-before", False),
        ("after", True),
        ("summary", True),
    ],
)
async def test_a_rewrite_drops_the_summaries_it_made_stale(
    edit: str, kept: bool
) -> None:
    script = _Recording([_submit()])
    summary = ChatMessageUser(
        content="summary of earlier work", metadata={"summary": True}
    )
    earlier = ChatMessageAssistant(content="earlier reply")

    async def callback(b: Boundary) -> Result:
        if b.index:
            return None
        messages = list(b.messages)
        ids = [m.id for m in messages]
        task = ids.index(earlier.id) - 1
        match edit:
            case "before":
                messages[task] = messages[task].model_copy(update={"content": "edited"})
            case "delete-before":
                del messages[ids.index(earlier.id)]
            case "after":
                messages[-1] = messages[-1].model_copy(update={"content": "edited"})
            case _:
                i = ids.index(summary.id)
                messages[i] = messages[i].model_copy(update={"content": "new summary"})
        return messages

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="defence")
        control.start("a", input=[_user("task"), earlier, summary])
        return await _drain(control)

    outcome = await _run([member(_react(script), name="a")], run_controller)
    assert _statuses(outcome) == {"a": "submitted"}
    ids = [m.id for m in script.inputs[0]]
    assert (summary.id in ids) is kept
    swarm_changes = [c for c in _changes(outcome) if c["origin"] == "swarm"]
    if kept:
        assert swarm_changes == []
    else:
        [change] = swarm_changes
        assert change["removed"] == [summary.id] and change["applied"] is True
        assert "added" not in change


@pytest.mark.anyio
async def test_a_rewrite_whose_only_user_message_is_a_stale_summary_ends_errored() -> (
    None
):
    summary = ChatMessageUser(content="summary", metadata={"summary": True})

    async def callback(b: Boundary) -> Result:
        # only the system prompt and the summary are left: the input before the
        # summary is gone, so the summary is stale
        return [m for m in b.messages if not isinstance(m, ChatMessageUser)] + [
            m for m in b.messages if m.id == summary.id
        ]

    async def run_controller(control: SwarmControl) -> StopReason:
        control.member("a").on_boundary(callback, origin="defence")
        control.start("a", input=[_user("task"), summary])
        return await _drain(control)

    transcript = await _raised(
        [member(_react(_Recording([_submit()])), name="a")],
        run_controller,
        ValueError,
        "no user message left",
    )
    [boundary] = _boundaries(transcript)
    assert boundary["outcome"] == "error" and boundary["origin"] == "swarm"


class _Summarizing(_Recording):
    """A script that answers each summary request with the next of `summaries` and keeps its input."""

    def __init__(
        self, outputs: Sequence[ModelOutput], summaries: Sequence[str]
    ) -> None:
        super().__init__(outputs)
        self.summaries = list(summaries)
        self.summarized: list[list[ChatMessage]] = []

    async def __call__(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        if input and "tasked with summarizing" in input[-1].text:
            self.summarized.append(list(input))
            return output(self.summaries.pop(0))
        return await super().__call__(input, tools, tool_choice, config)


def _overflow() -> ModelOutput:
    return ModelOutput.from_content("mockllm", "overflow", stop_reason="model_length")


@tool
def big() -> Tool:
    async def execute() -> str:
        """Return a long result."""
        return "filler " * 4000

    return execute


def _text(messages: Sequence[ChatMessage]) -> str:
    return "\n".join(m.text for m in messages)


@pytest.mark.anyio
@pytest.mark.parametrize("strategy", ["summary", "auto"])
@pytest.mark.parametrize("loop", ["submit", "reply"])
@pytest.mark.parametrize("change", ["edit", "delete"])
@pytest.mark.parametrize("trigger", ["overflow", "threshold"])
async def test_a_rewrite_before_a_real_summary_removes_it_and_the_next_compaction_sees_the_rewrite(
    strategy: str, loop: str, change: str, trigger: str
) -> None:
    note = _user("NOTE ORIGINAL")
    final = _submit() if loop == "submit" else output("answer")
    # the overflow forces the first compaction; the second is forced by another
    # overflow, or comes over the threshold after a long tool result
    second = _overflow() if trigger == "overflow" else _call("big")
    script = _Summarizing(
        [_overflow(), second, final],
        summaries=["SUMMARY ONE: NOTE ORIGINAL", "SUMMARY TWO"],
    )
    compaction = (
        CompactionSummary(threshold=1500)
        if strategy == "summary"
        else CompactionAuto(threshold=1500)
    )
    summaries: list[str] = []

    async def callback(b: Boundary) -> Result:
        if b.index == 0:
            return [*b.messages, note]
        summary = [m for m in b.messages if "summary" in (m.metadata or {})]
        if not summary or summaries:
            return None
        # the first boundary after the first compaction
        assert summary[0].id is not None
        summaries.append(summary[0].id)
        if change == "edit":
            return [
                m.model_copy(update={"content": "NOTE EDITED"})
                if m.id == note.id
                else m
                for m in b.messages
            ]
        return [m for m in b.messages if m.id != note.id]

    outcome = await _run(
        [
            member(
                _react(
                    script,
                    tools=[big()],
                    submit=loop == "submit",
                    compaction=compaction,
                ),
                name="a",
            )
        ],
        _starting({"a": [("test", callback)]}),
    )
    assert _statuses(outcome) == {"a": "submitted"}
    triggers = [
        (e.metadata or {}).get("trigger")
        for e in outcome.events
        if isinstance(e, CompactionEvent)
    ]
    assert triggers == ["forced", "forced" if trigger == "overflow" else "threshold"]
    # the summary made from the history the callback changed is removed, as the
    # runtime's change
    assert [c["removed"] for c in _changes(outcome) if c["origin"] == "swarm"] == [
        summaries
    ]
    first, second_summary = script.summarized
    assert "NOTE ORIGINAL" in _text(first)
    edited = "NOTE EDITED" if change == "edit" else None
    # the model's next input, and the next summarizer's, have the rewrite and neither
    # the original note nor the summary made from it
    for input in (script.inputs[1], second_summary):
        text = _text(input)
        assert "NOTE ORIGINAL" not in text
        if edited is not None:
            assert edited in text


# ---------------------------------------------------------------------------
# Binding


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["as_tool", "deepagent"])
@pytest.mark.parametrize("with_callback", [True, False])
async def test_a_nested_loop_is_never_held_and_a_child_with_swarm_tools_stays_the_bus_loop(
    kind: str, with_callback: bool
) -> None:
    child_script = _Recording(
        [_call("swarm_wait", timeout=5), _call("work"), _submit("child's")]
    )
    child_model = scripted_model(child_script)
    if kind == "as_tool":
        child = react(
            name="helper",
            description="A helper.",
            model=child_model,
            tools=[work(), swarm_tools()],
        )
        parent_script = _Recording([_call("helper", input="help"), _submit()])
        agent_ = react(model=scripted_model(parent_script), tools=[as_tool(child)])
    else:
        # a synchronous subagent, whose research child has the swarm's tools
        parent_script = _Recording([_call("agent", prompt="help"), _submit()])
        agent_ = deepagent(
            model=scripted_model(parent_script),
            subagents=[
                research(tools=[work()], extra_tools=[swarm_tools()], model=child_model)
            ],
            memory=False,
            todo_write=False,
        )
    ran: list[tuple[int, int]] = []

    async def callback(b: Boundary) -> Result:
        ran.append((b.index, child_script.started))
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        if with_callback:
            control.member("a").on_boundary(callback, origin="test")
        control.start("a")
        result = await _drain(control)
        context = cast(SwarmRun, control)._lookup("a").context
        # the bus bound the child, the seam the parent
        assert context.loop is not None and context.turn_loop is not None
        assert context.loop is not context.turn_loop
        return result

    outcome = await _run(
        [member(agent_, name="a")],
        run_controller,
        channels=[shared_memory(), binding_test_loop(), binding_test_member()],
    )
    assert _statuses(outcome) == {"a": "submitted"}
    # the child keeps its loop-reach tools, and its wait sees the member waiting
    offered = [
        {t.name for t in e.tools} & {"loop_write", "member_write"}
        for e in outcome.events
        if isinstance(e, ModelEvent) and e.model.endswith(child_model.name)
    ]
    assert offered == [{"loop_write", "member_write"}] * 3
    [waited] = [
        e
        for e in outcome.events
        if isinstance(e, CommunicationEvent) and e.kind == "waited"
    ]
    assert wait(waited).outcome == "all_waiting"
    # the parent's two boundaries; the child's three ran inside its turn
    assert ran == ([(0, 0), (1, 3)] if with_callback else [])
    assert _metrics(outcome).tools_unwired == []


@agent
def two_loops(first: Agent, second: Agent, warmup: str) -> Agent:
    """Run `first`, awaited or in background work, then `second`."""

    async def execute(state: AgentState) -> AgentState:
        if warmup == "background":
            done = anyio.Event()

            async def warm() -> None:
                await first(AgentState(messages=list(state.messages)))
                done.set()

            background(warm)
            await done.wait()
        else:
            state = await first(state)
        return await second(state)

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("warmup", ["awaited", "background"])
@pytest.mark.parametrize("with_callback", [True, False])
async def test_a_member_runs_one_held_loop(warmup: str, with_callback: bool) -> None:
    first_script = _Recording([_submit("warm")])
    second_script = _Recording([_submit("main")])
    ran: list[tuple[int, int]] = []

    async def callback(b: Boundary) -> Result:
        ran.append((b.index, second_script.started))
        return None

    members = [
        member(two_loops(_react(first_script), _react(second_script), warmup), name="a")
    ]
    callbacks = {"a": [("test", callback)]} if with_callback else {}
    if with_callback and warmup == "awaited":
        await _raised(members, _starting(callbacks), RuntimeError, "second agent loop")
        # the warmup bound the gate; the main loop was refused before generating
        assert ran == [(0, 0)]
        assert second_script.started == 0
        return
    outcome = await _run(members, _starting(callbacks))
    assert _statuses(outcome) == {"a": "submitted"}
    if with_callback:
        # the background warmup never bound; the main loop did
        assert ran == [(0, 0)]


@pytest.mark.anyio
async def test_a_stop_ends_a_member_at_a_successor_loops_boundary() -> None:
    first_script = _Recording([_submit("warm")])
    second_script = _Recording([_call("work", seconds=0.2), output("answer")])

    async def run_controller(control: SwarmControl) -> StopReason:
        control.start("a")
        while not second_script.calls:
            await anyio.sleep(0.01)
        return "done"

    outcome = await _run(
        [
            member(
                two_loops(
                    _react(first_script), _react(second_script, submit=False), "awaited"
                ),
                name="a",
            )
        ],
        run_controller,
        budget=Budget(grace=5),
    )
    assert _statuses(outcome) == {"a": "stopped"}
    assert second_script.started == 1


# ---------------------------------------------------------------------------
# Output and evidence


@agent
def max_tokens_model() -> Agent:
    """A custom model agent that returns a fresh state, cut off at max_tokens."""

    async def execute(state: AgentState, tools: list[Tool]) -> AgentState:
        reply = ModelOutput.from_content("custom", "cut off", stop_reason="max_tokens")
        reply.usage = ModelUsage(input_tokens=1, output_tokens=7, total_tokens=8)
        fresh = AgentState(messages=[*state.messages, reply.message])
        fresh.output = reply
        return fresh

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["end", "stop"])
async def test_the_boundary_output_is_the_loops_own_and_stays_the_members_output(
    ending: str,
) -> None:
    outputs: list[ModelOutput] = []
    runs: list[SwarmRun] = []

    async def run_controller(control: SwarmControl) -> StopReason:
        runs.append(cast(SwarmRun, control))

        async def callback(b: Boundary) -> Result:
            if b.index == 1:
                outputs.append(b.output)
                if ending == "end":
                    control.member("a").end(origin="operator")
                else:
                    cast(SwarmRun, control).stop("controller:test")
            return None

        control.member("a").on_boundary(callback, origin="test")
        control.start("a")
        return await _drain(control)

    outcome = await _run(
        [member(react(model=max_tokens_model()), name="a")], run_controller
    )
    # the output the member ended with
    kept = runs[0]._lookup("a").agent_state.output
    assert _statuses(outcome) == {"a": "stopped"}
    [boundary_output] = outputs
    assert boundary_output.stop_reason == "max_tokens"
    assert boundary_output.usage is not None
    assert boundary_output.usage.output_tokens == 7
    assert kept.completion == "cut off"
    assert kept.stop_reason == "max_tokens"


def _odd(value: str) -> ChatMessageUser:
    # a message Inspect accepts and generates with, though its JSON is not plain: an
    # opaque metadata value, or text with a lone surrogate
    if value == "opaque-metadata":
        return ChatMessageUser(content="odd message", metadata={"handle": object()})
    return ChatMessageUser(content="odd message \udc80")


def _as_logged(message: ChatMessage, value: str) -> ChatMessage:
    # the message as Inspect's safe serializer writes it
    if value == "opaque-metadata":
        return message.model_copy(update={"metadata": {"handle": None}})
    return message.model_copy(update={"content": "odd message \\udc80"})


@agent
def with_odd_message(inner: Agent, value: str) -> Agent:
    """Add an odd message to the conversation, then run `inner`."""

    async def execute(state: AgentState) -> AgentState:
        state.messages.append(_odd(value))
        return await inner(state)

    return execute


@agent
def capturing(inner: Agent, events: list[Event]) -> Agent:
    """Run `inner`, then keep the sample's events."""

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        events.extend(transcript().history.events_from(0))
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["opaque-metadata", "lone-surrogate"])
@pytest.mark.parametrize("changing", [False, True])
async def test_a_message_inspect_accepts_is_hashed_as_its_safe_serializer_writes_it(
    value: str, changing: bool, tmp_path: Path
) -> None:
    script = _Recording([_submit()])
    statuses: list[str | None] = []

    async def callback(b: Boundary) -> Result:
        return [*b.messages, _odd(value)] if changing and b.index == 0 else None

    async def run_controller(control: SwarmControl) -> StopReason:
        result = await _starting({"a": [("test", callback)]})(control)
        statuses.append(control.member("a").status)
        return result

    agent_ = _react(script) if changing else with_odd_message(_react(script), value)
    events: list[Event] = []
    # Inspect's log writer cannot write such a message in a model event's input, so
    # the sample is not logged and its events are kept as it runs
    [log] = await eval_async(
        Task(
            dataset=[Sample(input="task")],
            solver=capturing(
                _swarm([member(agent_, name="a")], run_controller), events
            ),
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        log_samples=False,
    )
    assert log.status == "success", log.error
    assert statuses == ["submitted"]
    seam = [e for e in events if isinstance(e, SwarmEvent)]
    assert [e.data["outcome"] for e in seam if e.kind == "boundary"] == ["continue"]
    [first, *_] = [e for e in events if isinstance(e, ModelEvent)]
    [odd] = [m for m in first.input if m.text.startswith("odd message")]
    assert message_hash(odd) == message_hash(_as_logged(odd, value))
    if changing:
        [change] = [e.data for e in seam if e.kind == "changed"]
        assert change["added"] == [
            {
                "id": odd.id,
                "hash": message_hash(odd),
                "index": first.input.index(odd),
            }
        ]


@pytest.mark.anyio
async def test_each_kind_of_change_is_recorded_by_id_and_hash_without_text() -> None:
    script = _Recording([_call("work"), _submit()])
    appended = _user("appended text")
    first = _user("first text")
    middle = _user("middle text")
    texts = ["appended text", "first text", "middle text", "replaced text"]

    def append(m: list[ChatMessage]) -> list[ChatMessage]:
        return [*m, appended]

    def insert_start(m: list[ChatMessage]) -> list[ChatMessage]:
        return [first, *m]

    def insert_middle(m: list[ChatMessage]) -> list[ChatMessage]:
        return [*m[:2], middle, *m[2:]]

    def delete(m: list[ChatMessage]) -> list[ChatMessage]:
        return [x for x in m if x.id != appended.id]

    def replace(m: list[ChatMessage]) -> list[ChatMessage]:
        return [*m[:-1], m[-1].model_copy(update={"content": "replaced text"})]

    def reorder(m: list[ChatMessage]) -> list[ChatMessage]:
        return [m[1], m[0], *m[2:]]

    def same(m: list[ChatMessage]) -> list[ChatMessage]:
        return list(m)

    steps = [append, insert_start, insert_middle, delete, replace, reorder, same]

    def callback(step: Callable[[list[ChatMessage]], list[ChatMessage]]) -> Any:
        async def execute(b: Boundary) -> Result:
            return step(b.messages) if b.index == 0 else None

        return execute

    outcome = await _run(
        [member(_react(script), name="a")],
        _starting({"a": [(step.__name__, callback(step)) for step in steps]}),
    )
    assert _statuses(outcome) == {"a": "submitted"}
    changes = {c["origin"]: c for c in _changes(outcome)}
    # no event for a callback that returned the same content
    assert list(changes) == [s.__name__ for s in steps[:-1]]
    assert changes["append"]["added"][0]["id"] == appended.id
    assert changes["insert_start"]["added"] == [
        {"id": first.id, "hash": message_hash(first), "index": 0}
    ]
    assert changes["insert_middle"]["added"][0]["index"] == 2
    assert changes["delete"]["removed"] == [appended.id]
    [replaced] = changes["replace"]["replaced"]
    assert "order" in changes["reorder"]
    assert all("order" not in c for o, c in changes.items() if o != "reorder")
    for c in changes.values():
        assert c["applied"] is True
    # the hashes match the messages in the next model input
    model_input = {m.id: message_hash(m) for m in script.inputs[0]}
    assert model_input[replaced["id"]] == replaced["after"]
    assert model_input[first.id] == changes["insert_start"]["added"][0]["hash"]
    model_events = [e for e in outcome.events if isinstance(e, ModelEvent)]
    logged = {m.id: message_hash(m) for m in model_events[0].input}
    assert logged[replaced["id"]] == replaced["after"]
    # never text
    evidence = json.dumps(
        [
            e.data
            for e in outcome.events
            if isinstance(e, SwarmEvent) and e.kind in ("boundary", "changed")
        ]
    )
    assert not any(text in evidence for text in texts)


@pytest.mark.anyio
@pytest.mark.parametrize("later", ["end", "error", "cancelled"])
async def test_a_change_is_not_applied_when_a_later_callback_ends_the_chain(
    later: str,
) -> None:
    script = _Recording([_submit()])
    staged = _user("staged")

    async def first(b: Boundary) -> Result:
        return [*b.messages, staged]

    async def second(b: Boundary) -> Result:
        if later == "end":
            return "end"
        if later == "error":
            raise KeyError("callback failed")
        await anyio.sleep_forever()
        return None

    run_controller = _starting({"a": [("first", first), ("second", second)]})
    members = [member(_react(script), name="a")]
    if later == "end":
        outcome = await _run(members, run_controller)
        assert _statuses(outcome) == {"a": "stopped"}
        transcript = outcome
    elif later == "error":
        transcript = await _raised(members, run_controller, KeyError, "callback failed")
    else:
        # the sample's time limit cancels the swarm from outside; the swarm's own time
        # cap would stop it first
        transcript = _Ran(
            await _eval(
                _swarm(members, run_controller, budget=Budget(time=100)),
                time_limit=1,
            )
        )
        assert transcript.sample.limit is not None
    [boundary] = _boundaries(transcript)
    assert boundary["outcome"] == later
    if later == "error":
        assert boundary["origin"] == "second" and boundary["error"] == "KeyError"
    [change] = _changes(transcript)
    assert change["origin"] == "first" and change["applied"] is False
    assert script.started == 0


@agent
def tuple_loop(model: Model) -> Agent:
    """A custom loop that passes its conversation as a tuple."""

    async def execute(state: AgentState) -> AgentState:
        async with agent_channel() as ch:
            await ch.before_turn(tuple(state.messages), state=state)
            state.output = await model.generate(state.messages)
        return state

    return execute


@pytest.mark.anyio
async def test_a_change_to_a_loop_that_passed_a_tuple_ends_the_member_errored() -> None:
    async def callback(b: Boundary) -> Result:
        return [*b.messages, _user("more")]

    transcript = await _raised(
        [member(tuple_loop(scripted_model()), name="a")],
        _starting({"a": [("test", callback)]}),
        TypeError,
        "own message list",
    )
    [boundary] = _boundaries(transcript)
    assert boundary["outcome"] == "rejected" and boundary["error"] == "TypeError"


@pytest.mark.anyio
async def test_tools_unwired_still_names_a_held_member_without_swarm_tools() -> None:
    async def callback(b: Boundary) -> Result:
        return None

    outcome = await _run(
        [member(_react(_Recording([_submit()])), name="a")],
        _starting({"a": [("test", callback)]}),
        channels=[shared_memory()],
    )
    assert _metrics(outcome).tools_unwired == ["a"]
    assert _metrics(outcome).members[0].holds == 1
