"""Operator messages and stops: the swarm's operator group, called as the control server calls it, and once over the control channel's socket."""

import asyncio
import contextvars
import gc
import os
import tempfile
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar
from uuid import uuid4

import anyio
import httpx
import pytest
import trio
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._control.discovery import list_discovered_servers
from inspect_ai._control.operator import OperatorGroup, OperatorOutcome
from inspect_ai.agent import (
    Agent,
    AgentState,
    agent,
    as_tool,
    deepagent,
    general,
    react,
    run,
)
from inspect_ai.dataset import Sample
from inspect_ai.event import CommunicationEvent, Event, ModelEvent, SwarmEvent
from inspect_ai.log import EvalSample, transcript
from inspect_ai.log._samples import ActiveSample, active_samples, sample_active
from inspect_ai.model import (
    ChatMessage,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelOutput,
)
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import MCPServer, Tool, ToolChoice, ToolInfo, ToolSource, tool
from inspect_ai.util import background, message_limit, time_limit

from inspect_swarm import (
    Boundary,
    BoundaryCallback,
    Budget,
    ControlEvent,
    MemberEnded,
    MemberIdle,
    SwarmControl,
    leaderless,
    member,
    shared_memory,
    swarm,
    swarm_result,
    swarm_tools,
)
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._record import SwarmResultRecord

from .swarm_runtime import (
    Script,
    controller,
    generator,
    scripted_model,
    sleeper,
    wait,
)
from .test_background_members import _bridged_call, _Deep, _dispatch, _hold
from .test_turn_seam import no_state_loop, two_loops

_P = ParamSpec("_P")
_T = TypeVar("_T")


class _Recording(Script):
    """A script that keeps each call's input."""

    def __init__(self, outputs: Sequence[ModelOutput] = ()) -> None:
        super().__init__(outputs)
        self.inputs: list[list[ChatMessage]] = []

    async def __call__(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        self.inputs.append(list(input))
        return await super().__call__(input, tools, tool_choice, config)


def _call(function: str, **args: Any) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm", function, args)


def _submit(answer: str = "done") -> ModelOutput:
    return _call("submit", answer=answer)


def _slow(started: anyio.Event, release: anyio.Event | None = None) -> Tool:
    """A tool that signals when it starts, then waits for `release` or for `seconds`."""

    @tool
    def slow() -> Tool:
        async def execute(seconds: float) -> str:
            """Work slowly.

            Args:
                seconds: How long the work takes.
            """
            started.set()
            if release is not None:
                await release.wait()
            else:
                await anyio.sleep(seconds)
            return "done"

        return execute

    return slow()


def _react(script: Script, *tools: Tool | ToolSource) -> Agent:
    return react(model=scripted_model(script), tools=list(tools))


def _after(
    ready: anyio.Event, inner: Agent, entered: anyio.Event | None = None
) -> Agent:
    """`inner`, once `ready` is set: a member still in its setup, before its agent loop opens. Sets `entered` when the setup begins."""

    @agent
    def setup() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            if entered is not None:
                entered.set()
            await ready.wait()
            return await inner(state)

        return execute

    return setup()


def _ctl(
    run_controller: Callable[[SwarmControl], Awaitable[str]],
) -> Any:
    return controller(run_controller, f"operator_test_{uuid4().hex}")


async def _drain(control: SwarmControl) -> str:
    async with aclosing(control.events()) as events:
        async for _ in events:
            pass
    return "all_done"


def _starting(
    names: Sequence[str] | None = None,
    callbacks: dict[str, BoundaryCallback] | None = None,
) -> Callable[[SwarmControl], Awaitable[str]]:
    # starts the named members (all of them by default), registering `callbacks` first,
    # and waits for them to end
    async def run_controller(control: SwarmControl) -> str:
        for name, callback in (callbacks or {}).items():
            control.member(name).on_boundary(callback, origin="test")
        for m in control.members:
            if names is None or m.name in names:
                control.start(m)
        return await _drain(control)

    return run_controller


@dataclass
class _Ran:
    sample: EvalSample

    @property
    def events(self) -> list[Event]:
        return self.sample.events

    @property
    def record(self) -> SwarmResultRecord:
        record = swarm_result(self.sample)
        assert record is not None
        return record

    def swarm_events(self, kind: str) -> list[SwarmEvent]:
        return [e for e in self.events if isinstance(e, SwarmEvent) and e.kind == kind]

    def messages(self) -> list[dict[str, Any]]:
        return [e.data for e in self.swarm_events("operator_message")]

    @property
    def ledger(self) -> SwarmLedger:
        [event] = self.swarm_events("ledger")
        return SwarmLedger.model_validate(event.data)

    def status(self, name: str) -> str | None:
        return next(m.status for m in self.record.members if m.name == name)

    def end_origins(self) -> dict[str, tuple[Any, Any, Any]]:
        # each member's end origin in its member_result event, record and ledger line
        events = {
            e.member: e.data["end_origin"] for e in self.swarm_events("member_result")
        }
        ledger = {m.name: m.end_origin for m in self.ledger.members}
        return {
            m.name: (events.get(m.name), m.end_origin, ledger[m.name])
            for m in self.record.members
        }


# The server's handlers call the group's methods in their own task and context, not the
# sample's: an empty context here.
def _act(func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    return contextvars.Context().run(func, *args, **kwargs)


async def _until(condition: Callable[[], bool]) -> None:
    while not condition():
        await anyio.sleep(0.01)


async def _group(name: str = "swarm") -> OperatorGroup:
    found: list[OperatorGroup] = []

    def registered() -> bool:
        for active in active_samples():
            group = active.operator_groups.get(name)
            if group is not None:
                found.append(group)
                return True
        return False

    await _until(registered)
    return found[0]


async def _run(
    solver: Agent,
    drive: Callable[[OperatorGroup], Awaitable[None]],
    *,
    group: str = "swarm",
    errors: bool = False,
    **task: Any,
) -> _Ran:
    # `errors`: the sample may end with an error, for the test to check
    result: list[EvalSample] = []

    async def evaluate() -> None:
        [log] = await eval_async(
            Task(dataset=[Sample(input="task")], solver=solver, **task),
            model="mockllm/model",
            log_dir=tempfile.mkdtemp(),
            ctl_server=False,
        )
        assert log.samples, log.error
        result.append(log.samples[0])

    with anyio.fail_after(60):
        async with anyio.create_task_group() as tg:
            tg.start_soon(evaluate)
            await drive(await _group(group))
    [sample] = result
    if not errors:
        assert sample.error is None, sample.error.traceback
    return _Ran(sample)


def _swarm(
    members: list[Any],
    run_controller: Callable[[SwarmControl], Awaitable[str]] | None = None,
    *,
    grace: float = 120.0,
) -> Agent:
    return swarm(
        members,
        controller=_ctl(run_controller or _starting()),
        channels=[],
        budget=Budget(grace=grace),
    )


def _model_events(ran: _Ran) -> list[int]:
    return [i for i, e in enumerate(ran.events) if isinstance(e, ModelEvent)]


def _event_index(ran: _Ran, data: dict[str, Any]) -> int:
    return next(
        i
        for i, e in enumerate(ran.events)
        if isinstance(e, SwarmEvent) and e.kind == "operator_message" and e.data == data
    )


# ---------------------------------------------------------------------------
# Messages


@pytest.mark.anyio
async def test_message_with_interrupt_cancels_a_long_tool_call() -> None:
    started = anyio.Event()
    script = _Recording([_call("slow", seconds=30), _submit()])

    async def drive(group: OperatorGroup) -> None:
        await started.wait()
        a = group.agents["a"]
        assert _act(a.info).activity == "in_turn"
        # a preview takes no id, posts nothing and interrupts nothing
        preview = _act(a.message, "Not this.", interrupt=True, dry_run=True)
        assert preview.changed and preview.detail.startswith("Dry run: Posted, and")
        assert _act(a.info).activity == "in_turn"
        outcome = _act(a.message, "Change course.", interrupt=True, dry_run=False)
        assert (outcome.changed, outcome.refused) == (True, None)
        assert "interrupted" in outcome.detail

    with anyio.fail_after(20):
        ran = await _run(
            _swarm([member(_react(script, _slow(started)), name="a")]), drive
        )

    assert ran.status("a") == "submitted"
    *_, repair, redirect = script.inputs[1]
    assert isinstance(repair, ChatMessageTool)
    assert (repair.function, repair.text) == ("slow", "Tool call cancelled by user.")
    assert isinstance(redirect, ChatMessageUser)
    assert (redirect.text, redirect.source) == ("Change course.", "operator")
    assert not any("Not this." in str(m.content) for m in script.inputs[1])
    [message] = ran.messages()
    assert message == {
        "version": 1,
        "id": "op-1",
        "message_id": message["message_id"],
        "interrupt": True,
        "in_turn": True,
        "outcome": "drained",
        "boundary": 1,
        "recovery": True,
    }
    # written when the loop drained it, before the turn that follows
    assert _event_index(ran, message) < _model_events(ran)[1]


@pytest.mark.anyio
async def test_an_interrupt_cancels_a_subagent_and_spares_background_work() -> None:
    child_started, finished, sibling = anyio.Event(), anyio.Event(), anyio.Event()
    log: list[str] = []

    @tool
    def spawn() -> Tool:
        async def execute() -> str:
            """Start background work."""

            async def work() -> None:
                try:
                    await finished.wait()
                    log.append("background finished")
                except anyio.get_cancelled_exc_class():
                    log.append("background cancelled")
                    raise

            background(work)
            return "started"

        return execute

    # the child loop binds its own channel; the message goes to the parent's
    child_script = _Recording([_call("slow", seconds=30)])
    child = react(
        name="child",
        description="A subagent.",
        model=scripted_model(child_script),
        tools=[_slow(child_started)],
    )
    script = _Recording([_call("spawn"), _call("child", input="Work."), _submit()])

    @agent
    def waits_for_sibling() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            await sibling.wait()
            state.output = ModelOutput.from_content("mockllm", "answer")
            return state

        return execute

    async def drive(group: OperatorGroup) -> None:
        await child_started.wait()
        a = group.agents["a"]
        outcome = _act(a.message, "Change course.", interrupt=True, dry_run=False)
        assert (outcome.changed, outcome.refused) == (True, None)
        # the parent's next turn comes before the background work and the sibling end
        await _until(lambda: len(script.inputs) == 3)
        finished.set()
        sibling.set()

    ran = await _run(
        _swarm(
            [
                member(
                    react(
                        model=scripted_model(script), tools=[spawn(), as_tool(child)]
                    ),
                    name="a",
                ),
                member(waits_for_sibling(), name="b"),
            ]
        ),
        drive,
    )

    assert (ran.status("a"), ran.status("b")) == ("submitted", "submitted")
    # the child was cancelled in its tool call and never saw the message
    assert len(child_script.inputs) == 1
    assert all(m.text != "Change course." for m in child_script.inputs[0])
    *_, repair, redirect = script.inputs[2]
    assert isinstance(repair, ChatMessageTool)
    assert (repair.function, repair.text) == ("child", "Tool call cancelled by user.")
    assert isinstance(redirect, ChatMessageUser)
    assert (redirect.text, redirect.source) == ("Change course.", "operator")
    # the member's background work outlived the interrupt
    assert log == ["background finished"]
    [a] = [m for m in ran.ledger.members if m.name == "a"]
    assert (a.background_started, a.background_cancelled) == (1, 0)


@pytest.mark.anyio
async def test_messages_without_interrupt_reach_the_turn_after_the_tool() -> None:
    started, release = anyio.Event(), anyio.Event()
    script = _Recording([_call("slow", seconds=0), _submit()])

    async def drive(group: OperatorGroup) -> None:
        await started.wait()
        a = group.agents["a"]
        first = _act(a.message, "First.", interrupt=False, dry_run=False)
        second = _act(a.message, "Second.", interrupt=False, dry_run=False)
        assert first.detail == second.detail
        assert "once this turn's tool calls finish" in first.detail
        release.set()

    ran = await _run(
        _swarm([member(_react(script, _slow(started, release)), name="a")]), drive
    )

    # the tool ran to completion; the two messages, coalesced, follow its result
    *_, result, merged = script.inputs[1]
    assert isinstance(result, ChatMessageTool) and result.text == "done"
    assert isinstance(merged, ChatMessageUser) and merged.text == "First.\n\nSecond."
    messages = ran.messages()
    assert [
        (m["id"], m["outcome"], m["boundary"], m["recovery"]) for m in messages
    ] == [
        ("op-1", "drained", 1, False),
        ("op-2", "drained", 1, False),
    ]
    assert len({m["message_id"] for m in messages}) == 2
    assert _event_index(ran, messages[1]) < _model_events(ran)[1]


@pytest.mark.anyio
async def test_a_message_to_a_held_member_is_drained_at_that_boundary() -> None:
    held, release = anyio.Event(), anyio.Event()
    script = _Recording([_call("slow", seconds=0), _submit()])

    async def hold(boundary: Boundary) -> None:
        if boundary.index == 1:
            held.set()
            await release.wait()

    async def drive(group: OperatorGroup) -> None:
        await held.wait()
        a = group.agents["a"]
        assert _act(a.info).activity == "held"
        outcome = _act(a.message, "While held.", interrupt=True, dry_run=False)
        assert "held at a turn boundary" in outcome.detail
        release.set()

    ran = await _run(
        _swarm(
            [member(_react(script, _slow(anyio.Event())), name="a")],
            _starting(callbacks={"a": hold}),
        ),
        drive,
    )

    assert script.inputs[1][-1].text == "While held."
    [message] = ran.messages()
    assert (message["in_turn"], message["boundary"], message["recovery"]) == (
        False,
        1,
        False,
    )


@pytest.mark.anyio
async def test_a_message_during_setup_is_kept_until_the_loop_starts() -> None:
    ready = anyio.Event()
    script = _Recording([_submit()])

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await _until(lambda: _act(a.info).state == "running")
        assert _act(a.info).activity == "unknown"
        outcome = _act(a.message, "Before you start.", interrupt=True, dry_run=False)
        assert outcome.detail.startswith("Kept")
        assert "no turn to interrupt" in outcome.detail
        ready.set()

    ran = await _run(_swarm([member(_after(ready, _react(script)), name="a")]), drive)

    assert script.inputs[0][-1].text == "Before you start."
    [message] = ran.messages()
    assert (message["in_turn"], message["boundary"], message["recovery"]) == (
        False,
        0,
        False,
    )


@agent
def returns_then_waits(inner: Agent) -> Agent:
    """Run `inner`'s agent loop to its end, then wait, still running."""

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        await anyio.sleep_forever()
        return state

    return execute


@agent
def answers_now() -> Agent:
    """Answer at once, with no model call."""

    async def execute(state: AgentState) -> AgentState:
        state.output = ModelOutput.from_content("mockllm", "answer")
        return state

    return execute


@pytest.mark.anyio
async def test_messages_are_refused_where_no_loop_can_take_them() -> None:
    refusals = {
        "pending": "The member has not started.",
        "ended": "The member has ended.",
        "bridged": "The member is bridged: its CLI reads no agent channel, so it takes only stop.",
        "returned": "The member's agent loop has ended.",
    }

    bridged = refusals["bridged"]
    pauses = {
        "pending": (None, None),
        "ended": ("The member has ended.", None),
        "bridged": (bridged, bridged),
        "returned": (
            "The member cannot be paused: its agent loop has ended, and a later loop cannot be held.",
            None,
        ),
    }

    async def drive(group: OperatorGroup) -> None:
        agents = group.agents
        await _until(lambda: _act(agents["ended"].info).state == "ended")
        await _until(
            lambda: _act(agents["returned"].info).actions["message"] is not None
        )
        for name, reason in refusals.items():
            info = _act(agents[name].info)
            pause, resume = pauses[name]
            assert info.actions == {
                "message": reason,
                "pause": pause,
                "resume": resume,
                "stop": None,
            }, name
            outcome = _act(agents[name].message, "x", interrupt=False, dry_run=False)
            assert (outcome.changed, outcome.refused) == (False, reason), name
        assert _act(agents["returned"].pause, dry_run=False).refused is not None
        _act(group.stop, dry_run=False)

    ran = await _run(
        _swarm(
            [
                member(sleeper(), name="pending"),
                member(answers_now(), name="ended"),
                member(sleeper(), name="bridged", bridged=True),
                member(
                    returns_then_waits(_react(Script([_submit()]))), name="returned"
                ),
            ],
            _starting(["ended", "bridged", "returned"]),
            grace=0.1,
        ),
        drive,
    )

    assert ran.messages() == []


# A member that is ending takes no more turns: the gate ends it at its next boundary
# before it drains a message, so a message is refused once its end is requested, by the
# operator or the controller, or once the swarm is stopping.
@pytest.mark.anyio
@pytest.mark.parametrize(
    "by,reason",
    [
        ("operator", "The member is ending: it takes no more turns."),
        ("controller", "The member is ending: it takes no more turns."),
        ("swarm", "The swarm is stopping: its members take no more turns."),
    ],
)
async def test_messages_are_refused_once_the_member_is_ending(
    by: str, reason: str
) -> None:
    started, release, end_now = anyio.Event(), anyio.Event(), anyio.Event()

    async def run_controller(control: SwarmControl) -> str:
        control.start("a")
        if by == "controller":
            await end_now.wait()
            control.member("a").end(origin="test")
        return await _drain(control)

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        try:
            assert _act(a.info).actions["message"] is None
            if by == "operator":
                assert _act(a.stop, dry_run=False).changed
            elif by == "controller":
                end_now.set()
                await _until(lambda: _act(a.info).actions["message"] is not None)
            else:
                assert _act(group.stop, dry_run=False).changed
            assert _act(a.info).actions["message"] == reason
            for dry_run in (True, False):
                for interrupt in (False, True):
                    outcome = _act(a.message, "x", interrupt=interrupt, dry_run=dry_run)
                    assert (outcome.changed, outcome.refused) == (False, reason)
        finally:
            release.set()

    ran = await _run(
        _swarm(
            [
                member(
                    _react(
                        Script([_call("slow", seconds=30)]), _slow(started, release)
                    ),
                    name="a",
                )
            ],
            run_controller,
            grace=60,
        ),
        drive,
    )

    assert ran.messages() == []
    assert ran.status("a") == "stopped"


@pytest.mark.anyio
async def test_messages_a_member_never_drained_are_recorded_when_it_ends() -> None:
    started = anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        agents = group.agents
        await started.wait()
        await _until(lambda: _act(agents["channelless"].info).state == "running")
        assert _act(
            agents["channelless"].message, "x", interrupt=False, dry_run=False
        ).changed
        assert _act(
            agents["in_tool"].message, "y", interrupt=False, dry_run=False
        ).changed
        # cancelled in the tool when the grace runs out
        _act(agents["in_tool"].stop, dry_run=False)
        _act(agents["channelless"].stop, dry_run=False)

    ran = await _run(
        _swarm(
            [
                member(sleeper(), name="channelless"),
                member(
                    _react(Script([_call("slow", seconds=30)]), _slow(started)),
                    name="in_tool",
                ),
            ],
            grace=0.1,
        ),
        drive,
    )

    assert (ran.status("channelless"), ran.status("in_tool")) == (
        "cancelled",
        "cancelled",
    )
    for event in ran.swarm_events("operator_message"):
        assert (event.data["outcome"], event.data["boundary"]) == ("not_drained", None)
        assert event.data["recovery"] is None
        # before the member's result
        result = next(
            e for e in ran.swarm_events("member_result") if e.member == event.member
        )
        assert ran.events.index(event) < ran.events.index(result)
    assert sorted(e.member or "" for e in ran.swarm_events("operator_message")) == [
        "channelless",
        "in_tool",
    ]


# Drained is not delivered: what the member's conversation does after the drain is the
# loop's and the seam's, and the evidence says only that the loop took the message.


@pytest.mark.anyio
@pytest.mark.parametrize(("interrupt", "sends"), [(False, 1), (True, 1), (True, 2)])
async def test_a_message_drained_into_a_conversation_at_its_limit(
    interrupt: bool, sends: int
) -> None:
    started, release = anyio.Event(), anyio.Event()
    script = _Recording([_call("slow", seconds=30), _submit()])
    texts = [f"Redirect {i}." for i in range(sends)]

    async def drive(group: OperatorGroup) -> None:
        await started.wait()
        a = group.agents["a"]
        for text in texts:
            _act(a.message, text, interrupt=interrupt, dry_run=False)
        release.set()

    # react's system prompt, the input and the preamble, then the model's tool call and
    # its result (or the repair): the drained message is one more than the limit allows
    ran = await _run(
        _swarm(
            [
                member(
                    _react(script, _slow(started, release)),
                    name="a",
                    limits=[message_limit(5)],
                )
            ]
        ),
        drive,
    )

    assert ran.status("a") == "member_limit:message"
    assert [
        (m["id"], m["outcome"], m["boundary"], m["recovery"]) for m in ran.messages()
    ] == [(f"op-{i + 1}", "drained", 1, interrupt) for i in range(sends)]
    # no model call after the drain saw it
    assert len(script.inputs) == 1
    assert not any(text in m.text for text in texts for m in script.inputs[0])


@pytest.mark.anyio
async def test_a_redirect_a_boundary_callback_removes_stays_drained() -> None:
    started = anyio.Event()
    script = _Recording([_call("slow", seconds=30), _submit()])

    async def without_operator(b: Boundary) -> list[ChatMessage] | None:
        kept = [m for m in b.messages if m.source != "operator"]
        return kept if len(kept) < len(b.messages) else None

    async def drive(group: OperatorGroup) -> None:
        await started.wait()
        _act(group.agents["a"].message, "Redirect.", interrupt=True, dry_run=False)

    ran = await _run(
        _swarm(
            [member(_react(script, _slow(started)), name="a")],
            _starting(callbacks={"a": without_operator}),
        ),
        drive,
    )

    [message] = ran.messages()
    assert (message["outcome"], message["boundary"], message["recovery"]) == (
        "drained",
        1,
        True,
    )
    [changed] = [e.data for e in ran.swarm_events("changed")]
    assert (changed["boundary"], changed["origin"]) == (1, "test")
    assert changed["removed"] == [message["message_id"]]
    # the repair stays; the redirect reaches no model call
    assert isinstance(script.inputs[1][-1], ChatMessageTool)
    assert not any(m.text == "Redirect." for m in script.inputs[1])


# ---------------------------------------------------------------------------
# Stopping members


@tool
def blocked() -> Tool:
    async def execute() -> str:
        """Never finish."""
        await anyio.sleep_forever()
        return ""

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["sleeper", "bridged", "react_in_setup"])
async def test_a_member_stopped_while_pending_ends_when_started(kind: str) -> None:
    go = anyio.Event()
    script = Script()
    target = {
        "sleeper": member(sleeper(), name="a"),
        "bridged": member(sleeper(), name="a", bridged=True),
        # its setup never finishes
        "react_in_setup": member(_after(anyio.Event(), _react(script)), name="a"),
    }[kind]

    async def run_controller(control: SwarmControl) -> str:
        await go.wait()
        control.start("a")
        return await _drain(control)

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        assert _act(a.info).activity == "not_started"
        first = _act(a.stop, dry_run=False)
        assert first.changed and "when it is started" in first.detail
        assert not _act(a.stop, dry_run=False).changed
        go.set()

    # no swarm or sample deadline: nothing waits for one
    with anyio.fail_after(10):
        ran = await _run(_swarm([target], run_controller), drive)

    assert ran.status("a") == "stopped"
    assert script.started == 0
    assert ran.end_origins() == {"a": ("operator", "operator", "operator")}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("between_tool_calls", "stopped"),
        ("in_setup", "cancelled"),
    ],
)
async def test_a_running_member_stopped_ends_at_a_boundary_or_by_the_grace(
    kind: str, status: str
) -> None:
    # set once the member is running in its own task: its tool call, or its setup, which
    # never finishes, so it never reaches a boundary
    started = anyio.Event()
    script = Script([_call("slow", seconds=0.2), _call("slow", seconds=0.2), _submit()])
    target = {
        "between_tool_calls": member(_react(script, _slow(started)), name="a"),
        "in_setup": member(
            _after(anyio.Event(), _react(script), entered=started), name="a"
        ),
    }[kind]

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        outcome = _act(a.stop, dry_run=False)
        assert outcome.changed and "next turn boundary" in outcome.detail

    # a grace the short tool call fits in, or one the others outlast
    grace = 10.0 if kind == "between_tool_calls" else 0.2
    ran = await _run(_swarm([target], grace=grace), drive)

    assert ran.status("a") == status
    assert ran.end_origins() == {"a": ("operator", "operator", "operator")}
    if kind == "between_tool_calls":
        # its next model call was never made
        assert script.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("submits", [True, False])
async def test_a_member_stopped_while_its_background_work_drains(submits: bool) -> None:
    returning = anyio.Event()

    @agent
    def returns_with_background() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            background(anyio.sleep, 0.5)
            # an output cut off at the model's length is no submission
            state.output = ModelOutput.from_content(
                "mockllm", "answer", stop_reason="stop" if submits else "model_length"
            )
            returning.set()
            return state

        return execute

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await returning.wait()
        # once its drain has begun, after its status was worked out
        await _until(
            lambda: "background work drains" in _act(a.stop, dry_run=True).detail
        )
        assert _act(a.stop, dry_run=False).changed

    ran = await _run(
        swarm(
            [member(returns_with_background(), name="a")],
            controller=leaderless(stop_on_submission=False),
            channels=[],
            budget=Budget(grace=5),
        ),
        drive,
    )

    if submits:
        # the stop ends work only: the submission it returned stands, and decided how
        # it ended
        assert ran.status("a") == "submitted"
        assert ran.record.final.member == "a"
        assert ran.end_origins() == {"a": (None, None, None)}
    else:
        assert ran.status("a") == "stopped"
        assert ran.record.final.member is None
        assert ran.end_origins() == {"a": ("operator", "operator", "operator")}


@pytest.mark.anyio
@pytest.mark.parametrize("when", ["idle", "after_wake"])
async def test_a_persistent_member_the_operator_stops_keeps_its_answer(
    when: str,
) -> None:
    idle, stopped, started = anyio.Event(), anyio.Event(), anyio.Event()
    script = Script([_submit("first"), _call("slow", seconds=30)])

    async def run_controller(control: SwarmControl) -> str:
        control.start("p")
        control.start("q")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle):
                    if when == "idle":
                        # chosen before the stop, and kept by it
                        control.choose_answer("p")
                    else:
                        control.wake("p", "More.")
                    idle.set()
        await stopped.wait()
        await _drain(control)
        if when == "after_wake":
            # chosen once the stop has ended it
            control.choose_answer("p")
        return "all_done"

    async def drive(group: OperatorGroup) -> None:
        await idle.wait()
        if when == "after_wake":
            await started.wait()
        assert _act(group.agents["p"].stop, dry_run=False).changed
        stopped.set()

    ran = await _run(
        _swarm(
            [
                member(_react(script, _slow(started)), name="p", persistent=True),
                member(answers_now(), name="q"),
            ],
            run_controller,
            grace=0.2,
        ),
        drive,
    )

    # an idle member ends with its last answer, as for a controller's end()
    assert ran.status("p") == ("submitted" if when == "idle" else "cancelled")
    assert ran.end_origins()["p"] == ("operator", "operator", "operator")
    final = ran.record.final
    assert (final.mode, final.member, final.answer) == ("controller", "p", 0)


@pytest.mark.anyio
@pytest.mark.usefixtures("warm_inspect_caches")
@pytest.mark.parametrize("picked", [False, True])
async def test_a_stopped_member_whose_limit_ends_it_first_keeps_its_answer(
    picked: bool,
) -> None:
    idle, started = anyio.Event(), anyio.Event()
    script = Script([_submit("first"), _call("slow", seconds=30)])

    async def run_controller(control: SwarmControl) -> str:
        control.start("p")
        control.start("q")
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle):
                    if picked:
                        control.choose_answer("p")
                    control.wake("p", "More.")
                    idle.set()
        return "all_done"

    async def drive(group: OperatorGroup) -> None:
        await idle.wait()
        await started.wait()
        assert _act(group.agents["p"].stop, dry_run=False).changed

    # its time limit runs out in the tool call, long before the stop's grace
    ran = await _run(
        _swarm(
            [
                member(
                    _react(script, _slow(started)),
                    name="p",
                    persistent=True,
                    limits=[time_limit(2)],
                ),
                # answers after p, so the first rule would choose p's answer
                member(_after(idle, answers_now()), name="q"),
            ],
            run_controller,
            grace=60,
        ),
        drive,
    )

    assert ran.status("p") == "member_limit:time"
    # the limit decided its ending, and its answer stands
    final = ran.record.final
    assert (final.mode, final.member, final.answer) == (
        "controller" if picked else "first",
        "p",
        0,
    )
    [p] = [m for m in ran.record.members if m.name == "p"]
    assert (p.end_origin, len(p.answer_times)) == (None, 1)


@agent
def holds_in_cleanup(
    entered: anyio.Event, cleaning: anyio.Event, release: anyio.Event
) -> Agent:
    """Wait; when cancelled, run a shielded cleanup that waits for `release`."""

    async def execute(state: AgentState) -> AgentState:
        try:
            entered.set()
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                cleaning.set()
                await release.wait()
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("by", ["controller", "member", "caller", "task"])
async def test_a_stop_while_the_ended_swarm_unwinds_its_members_changes_nothing(
    by: str, anyio_backend: str
) -> None:
    if by == "task" and anyio_backend != "asyncio":
        pytest.skip("cancels an asyncio task directly")
    entered, cleaning, release = anyio.Event(), anyio.Event(), anyio.Event()
    # the caller's scope around the swarm, or the asyncio task it runs in
    scope = anyio.CancelScope()
    tasks: list[asyncio.Task[AgentState]] = []

    @agent
    def cancellable(inner: Agent) -> Agent:
        async def execute(state: AgentState) -> AgentState:
            if by == "task":
                tasks.append(asyncio.ensure_future(inner(state)))
                try:
                    return await tasks[0]
                except asyncio.CancelledError:
                    # only the swarm's task was cancelled, not this one
                    return state
            with scope:
                state = await inner(state)
            return state

        return execute

    async def run_controller(control: SwarmControl) -> str:
        for m in control.members:
            control.start(m)
        await entered.wait()
        if by == "controller":
            raise ValueError("controller failed")
        return await _drain(control)

    def refused(group: OperatorGroup) -> None:
        a = group.agents["a"]
        for dry_run in (True, False):
            for outcome in (
                _act(a.stop, dry_run=dry_run),
                _act(group.stop, dry_run=dry_run),
            ):
                assert (outcome.changed, outcome.refused) == (False, None)
                assert "ending after an error or a cancellation" in outcome.detail

    async def drive(group: OperatorGroup) -> None:
        await entered.wait()
        try:
            if by in ("caller", "task"):
                if by == "caller":
                    scope.cancel()
                else:
                    tasks[0].cancel()
                # at once, before any task in the swarm has run since
                refused(group)
            # the error or the cancellation is ending the runtime, which cancelled the
            # member: it is still running its cleanup, and still listed
            await cleaning.wait()
            assert _act(group.agents["a"].info).state == "running"
            refused(group)
        finally:
            # a shielded cleanup that never ends would hold the test
            release.set()

    members = [member(holds_in_cleanup(entered, cleaning, release), name="a")]
    if by == "member":
        # fails once a is running
        members.append(member(_after(entered, fails_now()), name="b"))
    ran = await _run(
        cancellable(_swarm(members, run_controller, grace=60)),
        drive,
        errors=True,
    )

    error = {"controller": "controller failed", "member": "sibling failed"}.get(by)
    if error is None:
        assert ran.sample.error is None
    else:
        assert ran.sample.error is not None and error in ran.sample.error.message
    assert ran.status("a") == "cancelled"
    assert ran.end_origins()["a"] == (None, None, None)
    assert ran.ledger.stop_reason is None


@pytest.mark.anyio
@pytest.mark.parametrize("by", ["member", "controller"])
async def test_a_stop_once_an_error_is_known_changes_nothing(
    by: str, anyio_backend: str
) -> None:
    outcomes: list[OperatorOutcome] = []
    messages: list[OperatorOutcome] = []
    # the error waits until the driver has found the group
    found = anyio.Event()

    def stops() -> None:
        # as a control handler would, between steps of the swarm's tasks
        [group] = [
            active.operator_groups["swarm"]
            for active in active_samples()
            if "swarm" in active.operator_groups
        ]
        a = group.agents["a"]
        for dry_run in (True, False):
            outcomes.append(_act(a.stop, dry_run=dry_run))
            outcomes.append(_act(group.stop, dry_run=dry_run))
            messages.append(_act(a.message, "x", interrupt=False, dry_run=dry_run))

    def on_event(event: Event) -> None:
        # b's errored result is published, and its error has not yet left its task
        if (
            isinstance(event, SwarmEvent)
            and event.kind == "member_result"
            and event.data["status"] == "errored"
            and not outcomes
        ):
            stops()

    async def run_controller(control: SwarmControl) -> str:
        control.start("a")
        if by == "member":
            transcript()._subscribe(on_event)  # pyright: ignore[reportPrivateUsage]
            control.start("b")
            return await _drain(control)
        await found.wait()
        # runs once the error has left the controller's task: on asyncio before the
        # task group's callback for it, on trio once the task group has it
        if anyio_backend == "asyncio":
            asyncio.get_running_loop().call_soon(stops)
        else:
            trio.lowlevel.current_trio_token().run_sync_soon(stops)
        raise ValueError("controller failed")

    members = [member(sleeper(), name="a")]
    if by == "member":
        members.append(member(_after(found, fails_now()), name="b"))

    async def drive(group: OperatorGroup) -> None:
        found.set()
        await _until(lambda: bool(outcomes))

    ran = await _run(_swarm(members, run_controller, grace=60), drive, errors=True)

    assert len(outcomes) == 4
    for outcome in outcomes:
        assert (outcome.changed, outcome.refused) == (False, None)
        assert "ending after an error or a cancellation" in outcome.detail
    # a message too: the member is being cancelled and takes no more turns
    assert len(messages) == 2
    for outcome in messages:
        assert outcome.changed is False and outcome.refused is not None
        assert "ending after an error or a cancellation" in outcome.refused
    assert ran.messages() == []
    error = "controller failed" if by == "controller" else "sibling failed"
    assert ran.sample.error is not None and error in ran.sample.error.message
    assert ran.status("a") == "cancelled"
    assert ran.ledger.stop_reason is None


# A cancellation raised by this backend, caught in the scope that was cancelled.
async def _cancellation() -> BaseException:
    with anyio.CancelScope() as scope:
        scope.cancel()
        try:
            await anyio.sleep(0)
        except anyio.get_cancelled_exc_class() as ex:
            return ex
    raise AssertionError("the scope was not cancelled")


@agent
def fails_with_background(
    where: str, started: anyio.Event, cleaning: anyio.Event, release: anyio.Event
) -> Agent:
    """Start background work whose cancellation runs a shielded cleanup that waits for `release`, then fail: the agent itself, another background task while the agent runs, one while its background work drains, or the agent with a group of an error and a cancellation (`mixed`) or of the cancellation alone (`cancelled`)."""

    async def cleans_up() -> None:
        try:
            started.set()
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                cleaning.set()
                await release.wait()

    async def fails() -> None:
        raise ValueError("member failed")

    async def execute(state: AgentState) -> AgentState:
        background(cleans_up)
        await started.wait()
        if where == "agent":
            raise ValueError("member failed")
        if where in ("mixed", "cancelled"):
            # a group with a real cancellation of this backend, and for "mixed" an error
            leaves: list[BaseException] = [await _cancellation()]
            if where == "mixed":
                leaves.insert(0, ValueError("member failed"))
            raise BaseExceptionGroup("member failed", leaves)
        background(fails)
        if where == "worker":
            await anyio.sleep_forever()
        # "drain": the failing task first runs once the agent has returned
        state.output = ModelOutput.from_content("mockllm", "answer")
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["agent", "worker", "drain"])
async def test_a_stop_while_a_failing_member_cleans_up_changes_nothing(
    where: str,
) -> None:
    started, cleaning, release = anyio.Event(), anyio.Event(), anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        # the error is raised, and the member's background work is in its cleanup
        await cleaning.wait()
        try:
            for name in ("a", "b"):
                for dry_run in (True, False):
                    outcome = _act(group.agents[name].stop, dry_run=dry_run)
                    assert (outcome.changed, outcome.refused) == (False, None)
                    assert "ending after an error" in outcome.detail
            for dry_run in (True, False):
                outcome = _act(group.stop, dry_run=dry_run)
                assert (outcome.changed, outcome.refused) == (False, None)
        finally:
            release.set()

    ran = await _run(
        _swarm(
            [
                member(sleeper(), name="a"),
                member(
                    fails_with_background(where, started, cleaning, release), name="b"
                ),
            ],
            grace=60,
        ),
        drive,
        errors=True,
    )

    assert ran.sample.error is not None
    assert "member failed" in ran.sample.error.message
    assert (ran.status("a"), ran.status("b")) == ("cancelled", "errored")
    assert ran.ledger.stop_reason is None


# Stops during teardown are best effort: one that reaches the run before the stop guard
# knows of the failure is accepted, and a failed run's record keeps that ending
# (decision: Ransom, 2026-11-05). Here b's agent raises a group of an error and a
# cancellation, which the guard does not see before b's background cleanup ends. The
# control raises the cancellation alone: b ends cancelled, the run does not fail, and
# the swarm's stop is recorded.
@pytest.mark.anyio
@pytest.mark.parametrize("leaves", ["mixed", "cancelled"])
async def test_a_stop_during_teardown_leaves_a_failed_runs_record_unchanged(
    leaves: str,
) -> None:
    started, cleaning, release = anyio.Event(), anyio.Event(), anyio.Event()
    outcomes: list[OperatorOutcome] = []

    async def drive(group: OperatorGroup) -> None:
        # the group is raised, and the member's background work is in its cleanup
        await cleaning.wait()
        try:
            for name in ("a", "b"):
                outcomes.append(_act(group.agents[name].stop, dry_run=False))
            outcomes.append(_act(group.stop, dry_run=False))
        finally:
            release.set()

    ran = await _run(
        _swarm(
            [
                member(sleeper(), name="a"),
                member(
                    fails_with_background(leaves, started, cleaning, release), name="b"
                ),
            ],
            grace=60,
        ),
        drive,
        errors=True,
    )

    assert [o.refused for o in outcomes] == [None, None, None]
    assert ran.end_origins() == {"a": (None, None, None), "b": (None, None, None)}
    if leaves == "mixed":
        assert ran.sample.error is not None
        assert "member failed" in ran.sample.error.message
        assert (ran.status("a"), ran.status("b")) == ("cancelled", "errored")
        assert ran.ledger.interrupted
        assert ran.ledger.stop_reason is None
    else:
        assert ran.sample.error is None
        assert (ran.status("a"), ran.status("b")) == ("cancelled", "cancelled")
        assert not ran.ledger.interrupted
        assert ran.ledger.stop_reason == "operator"


# The rule covers the run's own ending only: each member keeps the ending it had
# (decision: Ransom, 2026-11-05). A held member that a late stop ends records `stopped`
# for the operator in its event, result and ledger line, while the failed run's ledger
# has no `stop_reason`.
@pytest.mark.anyio
async def test_a_member_a_stop_ends_during_teardown_keeps_that_ending() -> None:
    held, release_hold = anyio.Event(), anyio.Event()
    started, cleaning, release = anyio.Event(), anyio.Event(), anyio.Event()
    outcomes: list[OperatorOutcome] = []

    async def hold(boundary: Boundary) -> None:
        held.set()
        await release_hold.wait()

    async def drive(group: OperatorGroup) -> None:
        # a is held, and b's group is raised with its background work in its cleanup
        await held.wait()
        await cleaning.wait()
        try:
            outcomes.append(_act(group.agents["a"].stop, dry_run=False))
            await _until(lambda: _act(group.agents["a"].info).state == "ended")
            outcomes.append(_act(group.stop, dry_run=False))
        finally:
            release_hold.set()
            release.set()

    ran = await _run(
        _swarm(
            [
                member(_react(Script([_submit()])), name="a"),
                member(
                    _after(
                        held,
                        fails_with_background("mixed", started, cleaning, release),
                    ),
                    name="b",
                ),
            ],
            _starting(callbacks={"a": hold}),
            grace=60,
        ),
        drive,
        errors=True,
    )

    assert [o.refused for o in outcomes] == [None, None]
    assert outcomes[0].changed
    assert ran.sample.error is not None
    assert "member failed" in ran.sample.error.message
    assert ran.ledger.interrupted
    assert ran.ledger.stop_reason is None
    [event] = [e for e in ran.swarm_events("member_result") if e.member == "a"]
    [line] = [m for m in ran.ledger.members if m.name == "a"]
    assert (event.data["status"], ran.status("a"), line.status) == ("stopped",) * 3
    assert ran.end_origins() == {
        "a": ("operator", "operator", "operator"),
        "b": (None, None, None),
    }
    assert ran.status("b") == "errored"


@agent
def fails_now() -> Agent:
    """Raise an error at once."""

    async def execute(state: AgentState) -> AgentState:
        raise RuntimeError("sibling failed")

    return execute


@agent
def returns_while_working(
    entered: anyio.Event, stopped: anyio.Event, returning: anyio.Event
) -> Agent:
    """Start background work that never ends, then return once `stopped` is set."""

    async def execute(state: AgentState) -> AgentState:
        background(anyio.sleep_forever)
        entered.set()
        await stopped.wait()
        state.output = ModelOutput.from_content("mockllm", "answer")
        returning.set()
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["setup", "drain"])
async def test_a_cancellation_that_beats_the_stop_is_not_its_doing(
    where: str,
) -> None:
    entered, go = anyio.Event(), anyio.Event()
    stopped, returning = anyio.Event(), anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        await entered.wait()
        assert _act(group.agents["a"].stop, dry_run=False).changed
        if where == "drain":
            # the stop turns its return into `stopped`, and its background work drains
            stopped.set()
            await returning.wait()
        # a sibling's error cancels the member long before the stop's grace
        go.set()

    ran = await _run(
        _swarm(
            [
                member(
                    _after(anyio.Event(), sleeper(), entered=entered)
                    if where == "setup"
                    else returns_while_working(entered, stopped, returning),
                    name="a",
                ),
                member(_after(go, fails_now()), name="b"),
            ],
            grace=60,
        ),
        drive,
        errors=True,
    )

    assert ran.sample.error is not None and "sibling failed" in ran.sample.error.message
    assert ran.status("a") == "cancelled"
    assert ran.end_origins()["a"] == (None, None, None)


@agent
def returns_when(entered: anyio.Event, release: anyio.Event) -> Agent:
    """Return an answer once `release` is set, with no turn boundary before it."""

    async def execute(state: AgentState) -> AgentState:
        entered.set()
        await release.wait()
        state.output = ModelOutput.from_content("mockllm", "answer")
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("stop", [True, False])
async def test_a_stop_after_the_swarms_stop_decides_a_normal_return(
    stop: bool,
) -> None:
    entered, release = anyio.Event(), anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        await entered.wait()
        # the swarm's stop closes the member's lever first, with its grace ahead
        assert _act(group.stop, dry_run=False).changed
        if stop:
            assert _act(group.agents["a"].stop, dry_run=False).changed
        release.set()

    ran = await _run(
        _swarm([member(returns_when(entered, release), name="a")], grace=60),
        drive,
    )

    if stop:
        assert ran.status("a") == "stopped"
        assert ran.record.final.member is None
        assert ran.end_origins() == {"a": ("operator", "operator", "operator")}
    else:
        assert ran.status("a") == "submitted"
        assert ran.record.final.member == "a"
        assert ran.end_origins() == {"a": (None, None, None)}


@pytest.mark.anyio
async def test_a_stop_after_the_swarms_stop_leaves_a_pending_member_unchanged() -> None:
    entered, release = anyio.Event(), anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        await entered.wait()
        try:
            assert _act(group.stop, dry_run=False).changed
            # no member starts once the swarm is stopping
            for dry_run in (True, False):
                outcome = _act(group.agents["b"].stop, dry_run=dry_run)
                assert (outcome.changed, outcome.refused) == (False, None)
                assert "never will" in outcome.detail
        finally:
            release.set()

    ran = await _run(
        _swarm(
            [
                member(returns_when(entered, release), name="a"),
                member(answers_now(), name="b"),
            ],
            _starting(["a"]),
            grace=60,
        ),
        drive,
    )

    [b] = [m for m in ran.record.members if m.name == "b"]
    assert (b.state, b.end_origin) == ("pending", None)


class _Cleanup(MCPServer):
    """A tool source whose teardown, when the loop exits, waits for `release`, shielded from cancellation unless `shield` is false."""

    def __init__(
        self, cleaning: anyio.Event, release: anyio.Event, shield: bool = True
    ) -> None:
        self._cleaning, self._release, self._shield = cleaning, release, shield

    async def tools(self) -> list[Tool]:
        return []

    async def __aexit__(self, *exc: object) -> None:
        self._cleaning.set()
        with anyio.CancelScope(shield=self._shield):
            await self._release.wait()


@pytest.mark.anyio
async def test_a_stop_while_an_idle_member_cleans_up_after_the_swarms_stop() -> None:
    cleaning, release = anyio.Event(), anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        try:
            # once p is idle the swarm stops at quiescence, which ends p's loop, and the
            # loop tears down its tools
            await cleaning.wait()
            assert _act(group.info).state == "stopping"
            assert _act(group.agents["p"].stop, dry_run=False).changed
        finally:
            # a shielded teardown that never ends would hold the test
            release.set()

    ran = await _run(
        _swarm(
            [
                member(
                    react(
                        model=scripted_model(Script([_submit("first")])),
                        tools=[_Cleanup(cleaning, release)],
                    ),
                    name="p",
                    persistent=True,
                )
            ],
            grace=60,
        ),
        drive,
    )

    # the swarm's stop decided its ending, and its answer stands
    assert ran.status("p") == "submitted"
    assert ran.record.final.member == "p"
    assert ran.end_origins() == {"p": (None, None, None)}


@pytest.mark.anyio
@pytest.mark.parametrize("by", ["controller", "operator"])
async def test_an_idle_answer_the_controller_has_not_read_yet(by: str) -> None:
    # p answers and is idle; it is ended before the controller reads its MemberIdle,
    # and its cleanup outlasts the grace, so it is cancelled
    cleaning, release, read = anyio.Event(), anyio.Event(), anyio.Event()
    events: list[ControlEvent] = []

    async def run_controller(control: SwarmControl) -> str:
        control.start("p")
        if by == "controller":
            await _until(lambda: control.member("p").state == "idle")
            control.member("p").end(origin="scheduler")
        else:
            await read.wait()
        async with aclosing(control.events()) as stream:
            async for event in stream:
                events.append(event)
        return "all_done"

    async def drive(group: OperatorGroup) -> None:
        if by == "operator":
            await _until(lambda: _act(group.agents["p"].info).state == "idle")
            assert _act(group.agents["p"].stop, dry_run=False).changed
            read.set()
        await cleaning.wait()

    ran = await _run(
        _swarm(
            [
                member(
                    react(
                        model=scripted_model(Script([_submit("first")])),
                        tools=[_Cleanup(cleaning, release, shield=False)],
                    ),
                    name="p",
                    persistent=True,
                )
            ],
            run_controller,
            grace=0.2,
        ),
        drive,
    )

    assert ran.status("p") == "cancelled"
    # the answer stands: the controller hears it, and it is the swarm's answer
    assert events == [
        MemberIdle("p", 0, news=False),
        MemberEnded("p", "cancelled", False),
    ]
    assert ran.record.final.member == "p"
    origin = "scheduler" if by == "controller" else "operator"
    assert ran.end_origins() == {"p": (origin, origin, origin)}


@pytest.mark.anyio
async def test_a_stop_after_the_swarms_stop_ends_a_held_member_for_the_operator() -> (
    None
):
    held, release = anyio.Event(), anyio.Event()

    async def hold(boundary: Boundary) -> None:
        held.set()
        await release.wait()

    async def drive(group: OperatorGroup) -> None:
        await held.wait()
        try:
            # both before the callback unwinds: the swarm's stop closes the lever
            # first, and the boundary ends the member for the operator's request
            assert _act(group.stop, dry_run=False).changed
            assert _act(group.agents["a"].stop, dry_run=False).changed
        finally:
            release.set()

    ran = await _run(
        _swarm(
            [member(_react(Script([_submit()])), name="a")],
            _starting(callbacks={"a": hold}),
            grace=60,
        ),
        drive,
    )

    assert ran.status("a") == "stopped"
    assert ran.record.final.member is None
    assert ran.end_origins() == {"a": ("operator", "operator", "operator")}


# ---------------------------------------------------------------------------
# The swarm


@pytest.mark.anyio
async def test_stopping_the_swarm_is_its_own_stop_in_its_own_context() -> None:
    started = anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        await started.wait()
        await _until(lambda: _act(group.agents["a"].info).state == "ended")
        dry = _act(group.stop, dry_run=True)
        assert dry.changed and dry.detail.startswith("Dry run:")
        assert _act(group.info).state == "running"
        assert _act(group.stop, dry_run=False).changed
        info = _act(group.info)
        assert (info.name, info.state, info.stop, info.paused) == (
            "swarm",
            "stopping",
            "operator",
            False,
        )
        again = _act(group.stop, dry_run=False)
        assert not again.changed and again.refused is None
        assert _act(group.pause, dry_run=False).refused is not None

    # the effective grace is clipped to half the sample's remaining time, which only the
    # sample's context sees: from the server's context, b's 30 s tool would outlast the
    # sample's time limit
    ran = await _run(
        swarm(
            [
                member(_react(Script([_submit("42")])), name="a"),
                member(
                    _react(Script([_call("slow", seconds=30)]), _slow(started)),
                    name="b",
                ),
            ],
            controller=leaderless(stop_on_submission=False),
            channels=[],
        ),
        drive,
        time_limit=3,
    )

    assert ran.ledger.stop_reason == "operator"
    assert ran.ledger.ended_by is None
    assert ran.record.final.member == "a" and not ran.record.final.provisional
    assert ran.status("b") == "cancelled"
    assert ran.end_origins() == {"a": (None, None, None), "b": (None, None, None)}


# A run whose result record or metrics cannot be written fails with that error, so its
# ledger has no operator `stop_reason` either (decision: Ransom, 2026-11-05). The
# controller has chosen a's answer, with a reason the record may reject, and b still
# works when the operator stops the swarm.
@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["none", "record", "metrics"])
async def test_a_stopped_swarm_whose_record_or_metrics_fail_keeps_that_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    started, chosen = anyio.Event(), anyio.Event()

    async def run_controller(control: SwarmControl) -> str:
        for m in control.members:
            control.start(m)
        async with aclosing(control.events()) as events:
            async for event in events:
                if isinstance(event, MemberEnded) and event.member == "a":
                    break
        # an attachment reference, which the record's guard refuses
        control.choose_answer(
            "a", reason="tc://artifact" if failure == "record" else "ok"
        )
        chosen.set()
        await anyio.sleep_forever()
        return "unreachable"

    def fail(*args: Any) -> None:
        raise OSError("disk full")

    if failure == "metrics":
        monkeypatch.setattr("inspect_swarm._controller._control.write_metrics", fail)

    async def drive(group: OperatorGroup) -> None:
        await started.wait()
        await chosen.wait()
        assert _act(group.stop, dry_run=False).changed

    ran = await _run(
        _swarm(
            [
                member(_react(Script([_submit("42")])), name="a"),
                member(
                    _react(Script([_call("slow", seconds=30)]), _slow(started)),
                    name="b",
                ),
            ],
            run_controller,
            grace=0.5,
        ),
        drive,
        errors=True,
    )

    error = ran.sample.error
    ledger = ran.ledger
    assert ledger.ended_by is None and not ledger.interrupted
    assert [(m.name, m.status) for m in ledger.members] == [
        ("a", "submitted"),
        ("b", "cancelled"),
    ]
    if failure == "none":
        assert error is None
        assert ledger.stop_reason == "operator"
        assert ran.record.final.member == "a" and ran.record.final.reason == "ok"
    else:
        assert error is not None
        assert ("disk full" if failure == "metrics" else "cannot be written") in (
            error.message
        )
        assert ledger.stop_reason is None


@pytest.mark.anyio
async def test_the_list_shows_roster_names_and_what_each_member_is_doing() -> None:
    started = anyio.Event()

    async def drive(group: OperatorGroup) -> None:
        await started.wait()
        await _until(lambda: _act(group.agents["done"].info).state == "ended")
        info = _act(group.info)
        assert (info.name, info.state, info.stop) == ("swarm", "running", None)
        assert [(a.name, a.state, a.activity) for a in info.agents] == [
            ("chooser", "running", "in_turn"),
            ("done", "ended", "ended"),
            ("waiting", "pending", "not_started"),
            ("sleeping", "running", "unknown"),
        ]
        assert info.agents[1].status == "submitted"
        _act(group.stop, dry_run=False)

    await _run(
        _swarm(
            [
                member(
                    _react(Script([_call("slow", seconds=30)]), _slow(started)),
                    name="chooser",
                    choose_name=True,
                ),
                member(answers_now(), name="done"),
                member(sleeper(), name="waiting"),
                member(sleeper(), name="sleeping"),
            ],
            _starting(["chooser", "done", "sleeping"]),
            grace=0.1,
        ),
        drive,
    )


@pytest.mark.anyio
async def test_a_swarm_in_a_member_is_its_own_group() -> None:
    async def drive(group: OperatorGroup) -> None:
        inner = await _group("swarm:outer")
        assert list(inner.agents) == ["inner"]
        assert list(group.agents) == ["outer"]
        _act(inner.agents["inner"].stop, dry_run=False)

    ran = await _run(
        _swarm(
            [member(_swarm([member(sleeper(), name="inner")], grace=0.1), name="outer")]
        ),
        drive,
    )

    assert ran.status("outer") == "submitted"


@pytest.mark.anyio
async def test_two_swarms_at_once_outside_any_member_are_refused() -> None:
    errors: list[str] = []
    left: list[tuple[list[str], int]] = []

    @agent
    def two_swarms() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            first = _swarm([member(sleeper(), name="a")], grace=0.1)
            second = _swarm([member(answers_now(), name="b")])
            async with anyio.create_task_group() as tg:
                tg.start_soon(run, first, "task")
                group = await _group()
                loggers = len(transcript()._event_loggers)
                try:
                    await run(second, "task")
                except ValueError as ex:
                    errors.append(str(ex))
                # the refused swarm claimed no timeline and left no subscription
                left.append(
                    (
                        [t.name for t in transcript().timelines],
                        len(transcript()._event_loggers) - loggers,
                    )
                )
                _act(group.stop, dry_run=False)
            # the same roster runs once the first swarm has ended
            await run(second, "task")
            return state

        return execute

    async def drive(group: OperatorGroup) -> None:
        pass

    ran = await _run(two_swarms(), drive)

    assert errors == ["The sample already has an operator group named 'swarm'."]
    assert left == [(["a"], 0)]
    assert [e.member for e in ran.swarm_events("member_result")] == ["a", "b"]


@pytest.mark.anyio
async def test_a_swarm_that_fails_to_start_unregisters_its_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspect_swarm._controller import _control

    real = _control.LedgerInterval
    # kept, with its traceback and so the failed run's frame: nothing but the run's own
    # cleanup unregisters the group
    errors: list[RuntimeError] = []
    registered: list[bool] = []
    # holds the second swarm's member until the driver has found its group
    found = anyio.Event()

    def fails_once(*args: Any, **kwargs: Any) -> Any:
        if not errors:
            raise RuntimeError("interval failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(_control, "LedgerInterval", fails_once)

    @agent
    def fails_then_runs() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            try:
                await run(_swarm([member(answers_now(), name="a")]), "task")
            except RuntimeError as ex:
                errors.append(ex)
            registered.append(
                any("swarm" in active.operator_groups for active in active_samples())
            )
            # a fresh roster: the failed swarm had claimed its member's timeline
            await run(_swarm([member(_after(found, answers_now()), name="b")]), "task")
            return state

        return execute

    async def drive(group: OperatorGroup) -> None:
        found.set()

    ran = await _run(fails_then_runs(), drive)

    assert [str(ex) for ex in errors] == ["interval failed"]
    assert registered == [False]
    assert [e.member for e in ran.swarm_events("member_result")] == ["b"]


# ---------------------------------------------------------------------------
# Pausing


def _boundaries(ran: _Ran) -> list[tuple[str, Any, Any]]:
    return [
        (e.member or "", e.data["boundary"], e.data["origins"])
        for e in ran.swarm_events("boundary")
    ]


def _activity(group: OperatorGroup, name: str) -> str:
    return _act(group.agents[name].info).activity


def _waits(ran: _Ran) -> list[str]:
    return [
        wait(e).outcome
        for e in ran.events
        if isinstance(e, CommunicationEvent) and e.kind == "waited"
    ]


_BACKGROUND = "The member cannot be paused: it starts background work, which would go on while it was held."
_BRIDGED = (
    "The member is bridged: its CLI reads no agent channel, so it takes only stop."
)


@pytest.mark.anyio
async def test_a_paused_member_is_held_at_its_next_boundary_until_resumed() -> None:
    started, release = anyio.Event(), anyio.Event()
    script = _Recording([_call("slow", seconds=0), _submit()])

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        dry = _act(a.pause, dry_run=True)
        assert (dry.changed, dry.detail) == (
            True,
            "Dry run: The member is held at its next turn boundary.",
        )
        assert not _act(a.info).paused
        assert _act(a.pause, dry_run=False).changed
        assert not _act(a.pause, dry_run=False).changed
        info = _act(a.info)
        assert (info.paused, info.activity) == (True, "in_turn")
        release.set()
        await _until(lambda: _activity(group, "a") == "held")
        # longer than the member's time limit, which does not run while it is held
        await anyio.sleep(1)
        assert script.calls == 1
        resumed = _act(a.resume, dry_run=False)
        assert (resumed.changed, resumed.detail) == (True, "Resumed.")
        assert not _act(a.resume, dry_run=False).changed
        assert not _act(a.info).paused

    ran = await _run(
        _swarm(
            [
                member(
                    _react(script, _slow(started, release)),
                    name="a",
                    limits=[time_limit(0.5)],
                )
            ]
        ),
        drive,
    )

    assert ran.status("a") == "submitted"
    assert _boundaries(ran) == [("a", 1, ["operator"])]
    [boundary] = ran.swarm_events("boundary")
    held_for: Any = boundary.data["held_for"]
    assert held_for >= 1
    # the hold is waiting time
    sample = ran.sample
    assert sample.total_time is not None and sample.working_time is not None
    assert sample.total_time - sample.working_time >= 0.9


# A pause before the member's loop has reached the gate is deferred, and the member is
# held at boundary 0 with no model call; once resumed, it can be paused again.
@pytest.mark.anyio
@pytest.mark.parametrize("when", ["pending", "setup", "setup_latch"])
async def test_a_pause_before_the_first_boundary_holds_the_member_there(
    when: str,
) -> None:
    ready, go, started, release = (anyio.Event() for _ in range(4))
    script = _Recording([_call("slow", seconds=0), _submit()])

    async def run_controller(control: SwarmControl) -> str:
        await go.wait()
        control.start("a")
        return await _drain(control)

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        if when != "pending":
            go.set()
            await _until(lambda: _act(a.info).state == "running")
        target = group if when == "setup_latch" else a
        outcome = _act(target.pause, dry_run=False)
        assert outcome.changed
        if target is a:
            assert outcome.detail.startswith("Deferred")
        info = _act(a.info)
        assert (info.paused, info.activity) == (
            True,
            "not_started" if when == "pending" else "unknown",
        )
        go.set()
        ready.set()
        await _until(lambda: _activity(group, "a") == "held")
        assert script.started == 0
        assert _act(target.resume, dry_run=False).changed
        await started.wait()
        _act(a.pause, dry_run=False)
        release.set()
        await _until(lambda: _activity(group, "a") == "held")
        _act(a.resume, dry_run=False)

    ran = await _run(
        _swarm(
            [member(_after(ready, _react(script, _slow(started, release))), name="a")],
            run_controller,
        ),
        drive,
    )

    assert ran.status("a") == "submitted"
    assert _boundaries(ran) == [("a", 0, ["operator"]), ("a", 1, ["operator"])]


@pytest.mark.anyio
async def test_a_member_paused_in_setup_and_then_stopped_ends_as_without_the_pause() -> (
    None
):
    ready = anyio.Event()
    script = _Recording([_submit()])

    async def drive(group: OperatorGroup) -> None:
        await _until(lambda: _act(group.agents["a"].info).state == "running")
        assert _act(group.agents["a"].pause, dry_run=False).changed
        _act(group.stop, dry_run=False)
        ready.set()

    ran = await _run(_swarm([member(_after(ready, _react(script)), name="a")]), drive)

    assert ran.status("a") == "stopped"
    assert script.started == 0 and _boundaries(ran) == []


@pytest.mark.anyio
async def test_an_interrupt_of_a_paused_member_holds_it_at_the_recovery_boundary() -> (
    None
):
    started = anyio.Event()
    script = _Recording([_call("slow", seconds=30), _submit()])

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        _act(a.pause, dry_run=False)
        outcome = _act(a.message, "Change course.", interrupt=True, dry_run=False)
        assert "turn was interrupted" in outcome.detail
        await _until(lambda: _activity(group, "a") == "held")
        assert len(script.inputs) == 1
        _act(a.resume, dry_run=False)

    with anyio.fail_after(20):
        ran = await _run(
            _swarm([member(_react(script, _slow(started)), name="a")]), drive
        )

    assert ran.status("a") == "submitted"
    *_, repair, redirect = script.inputs[1]
    assert isinstance(repair, ChatMessageTool) and repair.function == "slow"
    assert redirect.text == "Change course."
    [message] = ran.messages()
    assert (message["boundary"], message["recovery"]) == (1, True)
    assert _boundaries(ran) == [("a", 1, ["operator"])]


# A pause never makes a member error: one with no agent loop is never held, and one
# whose loop passes no state is found unpausable when its loop binds, and runs on.
@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["no_loop", "no_state"])
async def test_a_paused_member_that_cannot_be_held_runs_on(kind: str) -> None:
    ready, release = anyio.Event(), anyio.Event()
    model = scripted_model(Script([_submit()]))
    if kind == "no_loop":
        inner = generator(model, answer="x")
    else:
        inner = returns_when_set(no_state_loop(model), release)

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await _until(lambda: _act(a.info).state == "running")
        assert _act(a.pause, dry_run=False).detail.startswith("Deferred")
        ready.set()
        if kind == "no_state":
            reason = "The member cannot be paused: its agent loop does not pass its state, so it cannot be held at a turn boundary."
            await _until(lambda: _act(a.info).actions["pause"] == reason)
            assert not _act(a.info).paused
            assert _act(a.pause, dry_run=False).refused == reason
            # its flag stays as the operator set it
            assert _act(a.resume, dry_run=False).changed
            release.set()

    ran = await _run(_swarm([member(_after(ready, inner), name="a")]), drive)

    assert ran.status("a") == "submitted"
    assert _boundaries(ran) == []


@agent
def returns_when_set(inner: Agent, release: anyio.Event) -> Agent:
    """Run `inner`, then wait for `release` before returning."""

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        await release.wait()
        return state

    return execute


# swarm_wait counts a member the operator's pause holds as active, judged by the
# callback holding it now: b's wait times out while the operator holds a, and returns
# all_waiting while another caller's callback does.
@pytest.mark.anyio
@pytest.mark.parametrize("order", ["operator", "scheduler_first", "operator_first"])
async def test_swarm_wait_counts_a_member_the_operator_holds_as_active(
    order: str,
) -> None:
    started, release, paused, mid, go = (anyio.Event() for _ in range(5))
    scheduling, scheduled = anyio.Event(), anyio.Event()
    if order == "operator":
        # timed out while the operator holds a, then news of a's end once resumed
        calls = [_call("swarm_wait", timeout=1), _call("swarm_wait", timeout=30)]
        outcomes = ["timeout", "news"]
    elif order == "scheduler_first":
        # released while the scheduler holds a; a wait started once the operator holds
        # it times out
        calls = [
            _call("swarm_wait", timeout=30),
            _call("slow", seconds=0),
            _call("swarm_wait", timeout=1),
        ]
        outcomes = ["all_waiting", "timeout"]
    else:
        # blocked while the operator holds a, and released when the scheduler's
        # callback holds it after the resume
        calls = [_call("swarm_wait", timeout=30)]
        outcomes = ["all_waiting"]
    waiter = _Recording([*calls, _submit()])

    async def schedule(boundary: Boundary) -> None:
        if boundary.index != 1:
            return
        scheduling.set()
        if order == "scheduler_first":
            await mid.wait()
        else:
            await _until(lambda: waiter.calls == len(calls) + 1)
        scheduled.set()

    async def run_controller(control: SwarmControl) -> str:
        if order == "scheduler_first":
            control.member("a").on_boundary(schedule, origin="scheduler")
        control.start("a")
        await paused.wait()
        if order == "operator_first":
            control.member("a").on_boundary(schedule, origin="scheduler")
        while not control.member("a").held:
            await anyio.sleep(0.01)
        control.start("b")
        return await _drain(control)

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        _act(a.pause, dry_run=False)
        paused.set()
        release.set()
        if order == "operator":
            await _until(lambda: waiter.calls == 2)
        elif order == "scheduler_first":
            # the operator holds a once the scheduler has returned
            await scheduled.wait()
            go.set()
            await _until(lambda: waiter.calls == 4)
        else:
            await _until(lambda: _activity(group, "b") == "in_turn")
            await anyio.sleep(0.3)
            assert not scheduling.is_set()
        _act(a.resume, dry_run=False)

    ran = await _run(
        swarm(
            [
                member(
                    _react(
                        _Recording([_call("slow", seconds=0), _submit()]),
                        _slow(started, release),
                    ),
                    name="a",
                ),
                member(_react(waiter, swarm_tools(), _slow(mid, go)), name="b"),
            ],
            controller=_ctl(run_controller),
            channels=[shared_memory()],
        ),
        drive,
    )

    assert _waits(ran) == outcomes
    assert ran.status("a") == ran.status("b") == "submitted"


@pytest.mark.anyio
async def test_the_swarm_pause_holds_every_member_and_its_resume_leaves_own_pauses() -> (
    None
):
    go, release = anyio.Event(), anyio.Event()
    started = {name: anyio.Event() for name in "abc"}
    scripts = {
        name: _Recording([_call("slow", seconds=0), _submit()]) for name in "abc"
    }

    async def run_controller(control: SwarmControl) -> str:
        control.start("a")
        control.start("b")
        await go.wait()
        control.start("c")
        return await _drain(control)

    async def drive(group: OperatorGroup) -> None:
        a, b = group.agents["a"], group.agents["b"]
        await started["a"].wait()
        await started["b"].wait()
        latched = _act(group.pause, dry_run=False)
        assert latched.changed and latched.detail.startswith("The swarm is paused")
        assert not _act(group.pause, dry_run=False).changed
        assert _act(group.info).paused
        _act(a.pause, dry_run=False)
        release.set()
        await _until(lambda: _activity(group, "a") == _activity(group, "b") == "held")
        go.set()
        await _until(lambda: _activity(group, "c") == "held")
        assert scripts["c"].started == 0
        # under the latch, resume --agent reports the member's own flag
        for agent_, changed in ((b, False), (a, True)):
            outcome = _act(agent_.resume, dry_run=False)
            assert outcome.changed == changed
            assert "The swarm's pause still holds it" in outcome.detail
            info = _act(agent_.info)
            assert (info.paused, info.activity, info.actions["resume"]) == (
                True,
                "held",
                None,
            )
        _act(a.pause, dry_run=False)
        resumed = _act(group.resume, dry_run=False)
        assert resumed.changed and resumed.detail.endswith(
            "These members stay paused on their own until resumed one by one: a."
        )
        assert not _act(group.resume, dry_run=False).changed
        await _until(
            lambda: _act(b.info).state == _act(group.agents["c"].info).state == "ended"
        )
        assert _activity(group, "a") == "held"
        _act(a.resume, dry_run=False)

    ran = await _run(
        _swarm(
            [
                member(_react(scripts[name], _slow(started[name], release)), name=name)
                for name in "abc"
            ],
            run_controller,
        ),
        drive,
    )

    assert sorted(_boundaries(ran)) == [
        ("a", 1, ["operator"]),
        ("b", 1, ["operator"]),
        ("c", 0, ["operator"]),
    ]


@agent
def works_in_background() -> Agent:
    """Start background work, then wait."""

    async def execute(state: AgentState) -> AgentState:
        background(anyio.sleep_forever)
        await anyio.sleep_forever()
        return state

    return execute


def _deepagent(background_: bool | int) -> Any:
    return member(
        deepagent,
        args={"model": scripted_model(), "background": background_},
        name=f"deep-{background_}",
    )


@pytest.mark.anyio
async def test_members_with_background_work_cannot_be_paused() -> None:
    reasons = {
        "deep-True": "The member cannot be paused: deepagent(background=True) runs background subagents, which would go on generating while its loop was held.",
        "deep-4": "The member cannot be paused: deepagent(background=4) runs background subagents, which would go on generating while its loop was held.",
        "worker": _BACKGROUND,
    }

    async def drive(group: OperatorGroup) -> None:
        agents = group.agents
        await _until(lambda: _act(agents["worker"].info).actions["pause"] is not None)
        for name, reason in reasons.items():
            assert _act(agents[name].info).actions["pause"] == reason, name
            outcome = _act(agents[name].pause, dry_run=False)
            assert (outcome.changed, outcome.refused) == (False, reason), name
        latched = _act(group.pause, dry_run=False)
        assert latched.changed
        assert latched.detail.endswith(
            "These members cannot be paused and keep running: "
            + "; ".join(
                f"{name} ({reason.removeprefix('The member cannot be paused: ')[:-1]})"
                for name, reason in reasons.items()
            )
            + "."
        )
        assert not any(a.paused for a in _act(group.info).agents)
        assert _act(
            agents["worker"].message, "x", interrupt=True, dry_run=False
        ).changed
        assert _act(agents["worker"].stop, dry_run=False).changed

    ran = await _run(
        _swarm(
            [
                _deepagent(True),
                _deepagent(4),
                member(works_in_background(), name="worker"),
            ],
            _starting(["worker"]),
            grace=0.1,
        ),
        drive,
    )

    assert ran.status("worker") == "cancelled"


def _owns_a_task_group(
    script: Script,
    started: anyio.Event,
    paused: anyio.Event,
    helper_task: Callable[[], Coroutine[Any, Any, None]],
    *tools: Tool,
) -> Agent:
    """A member whose wrapper owns a task group around its react loop, and passes it to a tool that starts `helper_task` in it and returns once `paused` is set; `tools` are the loop's other tools."""

    @agent
    def owns_a_task_group() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            async with anyio.create_task_group() as tg:

                @tool
                def helper() -> Tool:
                    async def execute() -> str:
                        """Start a helper that outlives this call."""
                        tg.start_soon(helper_task)
                        started.set()
                        await paused.wait()
                        return "started"

                    return execute

                state = await _react(script, helper(), *tools)(state)
            return state

        return execute

    return owns_a_task_group()


# Background work that starts while the operator's callback holds the member is
# admitted: the pause is withdrawn, the hold ends and the member runs to its end. While
# another caller's callback holds it, or holds it next with the work still running,
# the seam's rule applies to that caller.
@pytest.mark.anyio
@pytest.mark.parametrize("target", ["agent", "group", "both"])
@pytest.mark.parametrize("other", ["none", "returned", "holding", "after"])
async def test_background_work_started_while_paused_withdraws_the_pause(
    target: str, other: str
) -> None:
    started, release, finish, held_by_other, paused, proceed = (
        anyio.Event() for _ in range(6)
    )
    script = _Recording([_call("helper"), _submit()])
    ran_other: list[int] = []

    async def other_callback(boundary: Boundary) -> None:
        ran_other.append(boundary.index)
        if other == "holding" and boundary.index == 1:
            held_by_other.set()
            await anyio.sleep_forever()

    async def run_controller(control: SwarmControl) -> str:
        if other in ("returned", "holding"):
            control.member("a").on_boundary(other_callback, origin="test")
        control.start("a")
        if other == "after":
            await paused.wait()
            control.member("a").on_boundary(other_callback, origin="test")
            proceed.set()
        return await _drain(control)

    async def helper_task() -> None:
        await release.wait()
        background(finish.wait)

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        for paused_by in (a, group):
            if target in ("both", "agent" if paused_by is a else "group"):
                assert _act(paused_by.pause, dry_run=False).changed
        paused.set()
        if other != "after":
            proceed.set()
        await _until(lambda: _activity(group, "a") == "held")
        if other == "holding":
            await held_by_other.wait()
        release.set()
        if other in ("holding", "after"):
            return
        await _until(lambda: _act(a.info).actions["pause"] == _BACKGROUND)
        assert not _act(a.info).paused
        assert _act(a.pause, dry_run=False).refused == _BACKGROUND
        if target == "both":
            # neither pause can hold it now, so neither resume says the other still
            # does
            assert (
                _act(group.resume, dry_run=True).detail
                == "Dry run: The swarm is resumed."
            )
            assert _act(a.resume, dry_run=False).detail == "Resumed."
        finish.set()

    ran = await _run(
        _swarm(
            [
                member(
                    _owns_a_task_group(script, started, proceed, helper_task),
                    name="a",
                )
            ],
            run_controller,
        ),
        drive,
        errors=other in ("holding", "after"),
    )

    if other == "holding":
        error = ran.sample.error
        assert error is not None and "is held at a turn boundary" in error.message
        assert ran_other == [0, 1]
    elif other == "after":
        error = ran.sample.error
        assert error is not None and "has background work running" in error.message
        # the operator's callback held the member and let it go; the other caller's
        # never held it alongside the work
        assert ran_other == []
    else:
        assert ran.status("a") == "submitted"
        origins = ["test", "operator"] if other == "returned" else ["operator"]
        assert _boundaries(ran)[-1] == ("a", 1, origins)


# A member found bridged after its pause was accepted, as when it runs a bridged
# subagent, is not held: the pause is withdrawn as the bridge is found, during its turn
# or while the pause holds it, and it runs on.
@pytest.mark.anyio
@pytest.mark.parametrize("target", ["agent", "group"])
@pytest.mark.parametrize("when", ["in_turn", "held"])
async def test_a_member_found_bridged_after_its_pause_is_not_held(
    target: str, when: str
) -> None:
    started, paused, bridged, release, running, finish = (
        anyio.Event() for _ in range(6)
    )
    script = _Recording([_call("helper"), _call("slow", seconds=0), _submit()])

    async def helper_task() -> None:
        await release.wait()
        await _bridged_call(scripted_model())
        bridged.set()

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        assert _act((a if target == "agent" else group).pause, dry_run=False).changed
        if when == "in_turn":
            release.set()
            await bridged.wait()
        paused.set()
        if when == "held":
            await _until(lambda: _activity(group, "a") == "held")
            release.set()
        # past the boundary, running
        await running.wait()
        info = _act(a.info)
        assert (info.paused, info.actions["pause"], info.actions["resume"]) == (
            False,
            _BRIDGED,
            _BRIDGED,
        )
        if target == "group":
            assert _act(group.resume, dry_run=False).detail == "The swarm is resumed."
        finish.set()

    ran = await _run(
        _swarm(
            [
                member(
                    _owns_a_task_group(
                        script,
                        started,
                        bridged if when == "in_turn" else paused,
                        helper_task,
                        _slow(running, finish),
                    ),
                    name="a",
                )
            ]
        ),
        drive,
    )

    assert ran.status("a") == "submitted"
    assert _boundaries(ran) == ([] if when == "in_turn" else [("a", 1, ["operator"])])


@agent
def background_deepagent(
    model: Model, tools: list[Tool], background: bool | int
) -> Agent:
    """A deepagent() with background dispatch, built by a builder of its own name."""
    return deepagent(
        model=model,
        tools=tools,
        subagents=[general()],
        todo_write=False,
        background=background,
    )


# The guard cannot see background dispatch through a builder of another name, so the
# pause is accepted, and withdrawn the first time the member dispatches.
@pytest.mark.anyio
@pytest.mark.parametrize("background_", [True, 4])
async def test_a_pause_of_a_built_background_deepagent_is_withdrawn_at_its_dispatch(
    background_: bool | int,
) -> None:
    gc.collect()
    finish = anyio.Event()
    plan = _Deep(parent=[_dispatch("child")], wait_before={0: ["go"]})
    hold = _hold(plan, [], until=lambda name: finish.wait())

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await _until(lambda: _activity(group, "a") == "in_turn")
        assert _act(a.info).actions["pause"] is None
        assert _act(a.pause, dry_run=False).changed
        plan.event("go").set()
        await plan.event("child").wait()
        assert _act(a.info).actions["pause"] == _BACKGROUND
        finish.set()

    ran = await _run(
        _swarm(
            [
                member(
                    background_deepagent(scripted_model(plan), [hold], background_),  # pyright: ignore[reportArgumentType]
                    name="a",
                )
            ]
        ),
        drive,
    )

    assert ran.status("a") == "submitted"
    assert _boundaries(ran) == []


# A pause registered while a member's first loop runs is withdrawn when that loop ends,
# so its second loop runs; another caller's callback still meets the seam's rule.
@pytest.mark.anyio
@pytest.mark.parametrize("with_other", [False, True])
async def test_a_pause_is_withdrawn_when_the_first_loop_ends(with_other: bool) -> None:
    go, started, release = anyio.Event(), anyio.Event(), anyio.Event()
    first = Script([_submit("warm")], gate=go)
    second = _Recording([_call("slow", seconds=0), _submit("main")])
    reason = "The member cannot be paused: its agent loop has ended, and a later loop cannot be held."

    async def other_callback(boundary: Boundary) -> None:
        return None

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await _until(lambda: first.started == 1)
        assert _act(a.pause, dry_run=False).changed
        go.set()
        if with_other:
            return
        await started.wait()
        info = _act(a.info)
        assert (info.actions["pause"], info.paused) == (reason, False)
        assert _act(a.pause, dry_run=False).refused == reason
        release.set()

    ran = await _run(
        _swarm(
            [
                member(
                    two_loops(
                        react(model=scripted_model(first)),
                        _react(second, _slow(started, release)),
                        "awaited",
                    ),
                    name="a",
                )
            ],
            _starting(callbacks={"a": other_callback} if with_other else None),
        ),
        drive,
        errors=with_other,
    )

    if with_other:
        error = ran.sample.error
        assert error is not None and "second agent loop" in error.message
    else:
        assert ran.status("a") == "submitted"
        assert _boundaries(ran) == []


# A pause reaches only the swarm it is addressed to: pausing a member that runs a
# swarm holds only its own loop, and the nested swarm's members through their group.
@pytest.mark.anyio
@pytest.mark.parametrize("nesting", ["tool", "member"])
async def test_a_pause_reaches_only_the_swarm_it_is_addressed_to(nesting: str) -> None:
    started, release = anyio.Event(), anyio.Event()
    inner = _Recording([_call("slow", seconds=0), _submit()])
    nested = _swarm([member(_react(inner, _slow(started, release)), name="inner")])

    @tool
    def nest() -> Tool:
        async def execute() -> str:
            """Run a swarm."""
            await run(nested, "task")
            return "done"

        return execute

    async def drive(group: OperatorGroup) -> None:
        outer = group.agents["outer"]
        inner_group = await _group("swarm:outer")
        await started.wait()
        outcome = _act(outer.pause, dry_run=False)
        assert outcome.changed
        if nesting == "member":
            assert outcome.detail.startswith("Deferred")
        else:
            # the outer member is mid-turn while its tool runs the swarm
            assert _activity(group, "outer") == "in_turn"
        assert _activity(inner_group, "inner") == "in_turn"
        assert _act(inner_group.pause, dry_run=False).changed
        release.set()
        await _until(lambda: _activity(inner_group, "inner") == "held")
        _act(inner_group.resume, dry_run=False)
        if nesting == "tool":
            await _until(lambda: _activity(group, "outer") == "held")
        _act(outer.resume, dry_run=False)

    outer_member = (
        member(_react(_Recording([_call("nest"), _submit()]), nest()), name="outer")
        if nesting == "tool"
        else member(nested, name="outer")
    )
    ran = await _run(_swarm([outer_member]), drive)

    assert ran.status("outer") == "submitted"
    expected = [("inner", 1, ["operator"])]
    if nesting == "tool":
        expected.append(("outer", 1, ["operator"]))
    assert _boundaries(ran) == expected


# The operator's callback runs after callbacks registered before it: a pause holds the
# member after a scheduler lets it go.
@pytest.mark.anyio
async def test_a_pause_holds_a_member_after_an_earlier_scheduler_releases_it() -> None:
    started, release, go = anyio.Event(), anyio.Event(), anyio.Event()
    script = _Recording([_call("slow", seconds=0), _submit()])
    released: list[int] = []

    async def schedule(boundary: Boundary) -> None:
        if boundary.index == 1:
            await go.wait()
            released.append(boundary.index)

    async def drive(group: OperatorGroup) -> None:
        a = group.agents["a"]
        await started.wait()
        _act(a.pause, dry_run=False)
        release.set()
        await _until(lambda: _activity(group, "a") == "held")
        go.set()
        await _until(lambda: released == [1])
        await anyio.sleep(0.2)
        assert _activity(group, "a") == "held" and script.calls == 1
        _act(a.resume, dry_run=False)

    ran = await _run(
        _swarm(
            [member(_react(script, _slow(started, release)), name="a")],
            _starting(callbacks={"a": schedule}),
        ),
        drive,
    )

    assert ran.status("a") == "submitted"
    assert _boundaries(ran) == [("a", 0, ["test"]), ("a", 1, ["test", "operator"])]


# ---------------------------------------------------------------------------
# Over the control channel


@scorer(metrics=[])
def always_one() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=1)

    return score


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_operator_actions_over_the_control_channel() -> None:
    started = anyio.Event()
    where: dict[str, Any] = {}
    script = _Recording([_call("slow", seconds=30), _call("slow", seconds=30)])

    @tool
    def slow() -> Tool:
        async def execute(seconds: float) -> str:
            """Work slowly.

            Args:
                seconds: How long the work takes.
            """
            active = sample_active()
            assert isinstance(active, ActiveSample)
            where.update(eval_id=active.eval_id, sample_id=active.sample.id)
            started.set()
            await anyio.sleep(seconds)
            return "done"

        return execute

    task = Task(
        dataset=[Sample(id="s1", input="task")],
        solver=swarm(
            [
                member(_react(Script([_submit("42")])), name="a"),
                member(_react(script, slow()), name="b"),
            ],
            controller=leaderless(stop_on_submission=False),
            channels=[],
            budget=Budget(grace=0.2),
        ),
        scorer=always_one(),
    )
    logs: list[Any] = []

    async def evaluate() -> None:
        logs.extend(
            await eval_async(
                task, model="mockllm/model", log_dir=tempfile.mkdtemp(), ctl_server=True
            )
        )

    async def post(client: httpx.AsyncClient, action: str, **params: Any) -> Any:
        response = await client.post(
            f"/evals/{where['eval_id']}/sample/agent/{action}",
            params={"sample_id": where["sample_id"], "epoch": 1, **params},
            json={"text": "Change course."} if action == "message" else None,
        )
        assert response.status_code == 200, response.text
        return response.json()

    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(evaluate)
            await started.wait()
            [server] = [s for s in list_discovered_servers() if s.pid == os.getpid()]
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(server.socket_path)),
                base_url="http://localhost",
            ) as client:
                sent = await post(client, "message", agent="b", interrupt="true")
                assert sent["changed"] and sent["target"] == {
                    "group": "swarm",
                    "agent": "b",
                }
                await _until(lambda: len(script.inputs) == 2)
                stopped = await post(client, "stop", group="swarm")
                assert stopped["changed"]

    [log] = logs
    assert log.status == "success"
    [sample] = log.samples
    assert sample.scores is not None and sample.scores["always_one"].value == 1
    *_, repair, redirect = script.inputs[1]
    assert isinstance(repair, ChatMessageTool) and repair.function == "slow"
    assert redirect.text == "Change course." and redirect.source == "operator"
    result = _Ran(sample)
    assert result.ledger.stop_reason == "operator"
    assert result.ledger.ended_by is None
    assert result.record.final.member == "a"
