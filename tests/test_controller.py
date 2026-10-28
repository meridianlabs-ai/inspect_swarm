from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, as_solver
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent, SpanBeginEvent, StoreEvent
from inspect_ai.log import EvalSample
from inspect_ai.model import ChatMessage, Model, ModelOutput, execute_tools
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, memory, tool
from inspect_ai.util import store, store_from_events, token_limit, tool_state_instance

from inspect_swarm import (
    Budget,
    Controller,
    MemberEnded,
    MemberHandle,
    StopReason,
    SwarmControl,
    leaderless,
    member,
    swarm,
    swarm_controller,
)
from inspect_swarm._controller._control import SwarmRun
from inspect_swarm._record import RECORD_KEY

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Script,
    after,
    evidence,
    generator,
    run_swarm,
    sample,
    scripted_model,
    sleeper,
    statuses,
)


def _controller(run: Any, name: str) -> Controller:
    # a controller defined by a test, registered under a name of its own
    @swarm_controller(name=name)
    def factory() -> Controller:
        return Controller(run)

    return factory()


async def _start_all(control: SwarmControl) -> StopReason:
    for m in control.members:
        control.start(m)
    async for _ in control.events():
        pass
    return "all_done"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop_on_submission,reason,slow_status",
    [(True, "submitted", "stopped"), (False, "all_done", "submitted")],
)
async def test_leaderless_stops_at_the_first_submission_unless_told_not_to(
    stop_on_submission: bool, reason: str, slow_status: str
) -> None:
    refusal = ModelOutput.from_content("mockllm", "no", stop_reason="content_filter")
    first = generator(scripted_model(), calls=0, answer="first")
    # still generating when the first member submits, or answering after it
    slow = (
        generator(scripted_model(Script(delay=0.01)), calls=None)
        if stop_on_submission
        else after(["quick"], generator(scripted_model(), answer="second"))
    )
    stop = await run_swarm(
        swarm(
            [
                # end first, without a submission: neither stops the swarm
                member(generator(scripted_model(Script([refusal]))), name="refuser"),
                member(
                    generator(scripted_model(), calls=None),
                    name="limited",
                    limits=[token_limit(TOKENS_PER_CALL)],
                ),
                member(after(["refuser", "limited"], first), name="quick"),
                member(slow, name="slow"),
            ],
            controller=leaderless(stop_on_submission=stop_on_submission),
            channels=[],
            # ends the slow member if no submission stops it
            budget=Budget(time=30),
        )
    )
    assert stop.reason == reason
    assert statuses(stop) == {
        "refuser": "unsubmitted",
        "limited": "member_limit:token",
        "quick": "submitted",
        "slow": slow_status,
    }
    assert stop.record.final.member == "quick"
    assert stop.state.output.completion == "first"


@agent
def recorder(inputs: list[list[ChatMessage]]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        inputs.append(list(state.messages))
        return state

    return execute


@pytest.mark.anyio
async def test_a_custom_controller_starts_members_later_with_custom_input() -> None:
    inputs: list[list[ChatMessage]] = []
    seen: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("scout", input="look first")
        with pytest.raises(RuntimeError, match="already started"):
            control.start("scout")
        with pytest.raises(KeyError, match="nobody"):
            control.member("nobody")
        async for event in control.events():
            assert isinstance(event, MemberEnded)
            seen.append(event.member)
        # the iterator ended with nothing running; a later start needs a new events()
        assert [m.state for m in control.members] == ["ended", "pending"]
        control.start(control.member("worker"))
        async for event in control.events():
            assert isinstance(event, MemberEnded)
            seen.append(event.member)
        return "all_done"

    stop = await run_swarm(
        swarm(
            [
                member(recorder(inputs), name="scout"),
                member(recorder, args={"inputs": inputs}, name="worker"),
            ],
            controller=_controller(run, "controller_test_staged"),
            channels=[],
        ),
        input="the task",
    )
    assert seen == ["scout", "worker"]
    assert statuses(stop) == {"scout": "submitted", "worker": "submitted"}
    scout, worker = inputs
    # the given input, else the swarm's, then the member's preamble
    assert [m.text for m in scout[:-1]] == ["look first"]
    assert [m.text for m in worker[:-1]] == ["the task"]
    assert scout[-1].text.startswith("You are scout, one of 2 agents")
    assert worker[-1].text.startswith("You are worker, one of 2 agents")


@pytest.mark.anyio
async def test_events_allows_one_waiting_iterator() -> None:
    gate = anyio.Event()
    errors: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("generator")

        async def iterate() -> None:
            async for _ in control.events():
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(iterate)
            # until the first iterator holds the events, without depending on timing
            assert isinstance(control, SwarmRun)
            while not control._iterating:
                await anyio.sleep(0)
            try:
                await iterate()
            except RuntimeError as ex:
                errors.append(str(ex))
            gate.set()
        return "all_done"

    stop = await run_swarm(
        swarm(
            generator(scripted_model(Script(gate=gate))),
            controller=_controller(run, "controller_test_iterators"),
            channels=[],
        )
    )
    assert len(errors) == 1 and "one at a time" in errors[0]
    assert statuses(stop) == {"generator": "submitted"}


@pytest.mark.anyio
async def test_a_woken_iterator_keeps_the_event_that_woke_it() -> None:
    # the interval after a member's end wakes a waiting iterator and before it
    # resumes, driven by hand so that no scheduling order decides it
    async with sample():
        run = SwarmRun(
            [(member(generator(scripted_model())), "generator")],
            state=AgentState(messages=[]),
            input=[],
            preambles={"generator": ""},
            caps=[],
            time=None,
            grace=0,
            budget=Budget(),
        )
        [m] = run._members
        m.state = "running"
        received: list[MemberEnded] = []

        async def first() -> None:
            async for event in run.events():
                received.append(event)

        async with anyio.create_task_group() as tg:
            tg.start_soon(first)
            while not run._iterating:
                await anyio.sleep(0)
            run._end(m, "submitted")
            # a competing iterator, before the woken one has resumed
            with pytest.raises(RuntimeError, match="one at a time"):
                await run.events().__anext__()
    assert received == [MemberEnded("generator", "submitted", True)]


@pytest.mark.anyio
async def test_an_open_iterator_holds_the_events_until_it_ends_or_is_closed() -> None:
    received: list[str] = []
    refused: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        while any(m.state != "ended" for m in control.members):
            await anyio.sleep(0)
        # three events are queued; the first iterator takes one and stays open
        first = control.events()
        received.append((await first.__anext__()).member)
        try:
            await control.events().__anext__()
        except RuntimeError as ex:
            refused.append(str(ex))
        received.append((await first.__anext__()).member)
        # once it is closed, a new iterator takes the rest
        await first.aclose()
        async for event in control.events():
            received.append(event.member)
        return "all_done"

    stop = await run_swarm(
        swarm(
            member(generator, args={"model": scripted_model(), "calls": 0}, count=3),
            controller=_controller(run, "controller_test_open_iterator"),
            channels=[],
        )
    )
    assert len(refused) == 1 and "still open" in refused[0]
    assert sorted(received) == ["generator-1", "generator-2", "generator-3"]
    assert set(statuses(stop).values()) == {"submitted"}


@pytest.mark.anyio
async def test_returning_soft_stops_members_and_cancels_them_after_grace() -> None:
    gate = anyio.Event()
    in_flight = Script(gate=gate)

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for event in control.events():
            if event.member == "quick":
                while in_flight.started < 1:
                    await anyio.sleep(0)
                # released as the stop begins: the call in flight completes
                gate.set()
                return "enough"
        raise AssertionError("quick never ended")

    stop = await run_swarm(
        swarm(
            [
                member(generator(scripted_model(), calls=1), name="quick"),
                member(generator(scripted_model(in_flight), calls=None), name="busy"),
                member(sleeper(), name="stuck"),
            ],
            controller=_controller(run, "controller_test_returns"),
            channels=[],
            budget=Budget(grace=0.2),
        )
    )
    assert stop.reason == "controller:enough"
    assert statuses(stop) == {
        "quick": "submitted",
        "busy": "stopped",
        "stuck": "cancelled",
    }
    # the call in flight at the stop completed, and no other was started
    assert (in_flight.started, in_flight.calls) == (1, 1)


@agent
def scout(control: list[SwarmControl], model: Model, attempts: list[str]) -> Agent:
    # outlasts the stop, then tries to start a worker and to call its model
    async def execute(state: AgentState) -> AgentState:
        await anyio.sleep(0.3)
        try:
            control[0].start("worker-1")
        except RuntimeError as ex:
            attempts.append(str(ex))
        await model.generate(state.messages)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("trigger", ["swarm_time", "swarm_cap"])
async def test_a_stop_closes_scheduling_and_cancels_only_the_controller(
    trigger: str,
) -> None:
    control: list[SwarmControl] = []
    attempts: list[str] = []
    controller_acted: list[bool] = []
    scout_calls = Script()

    async def run(swarm: SwarmControl) -> StopReason:
        control.append(swarm)
        swarm.start("scout")
        if trigger == "swarm_cap":
            swarm.start("spender")
        async for event in swarm.events():
            if event.member == "scout":
                controller_acted.append(True)
                for m in swarm.members:
                    if m.state == "pending":
                        swarm.start(m)
        return "all_done"

    members = [
        member(scout(control, scripted_model(scout_calls), attempts), name="scout"),
        member(generator, args={"model": scripted_model()}, name="worker", count=2),
    ]
    budget = Budget(grace=5, time=0.05)
    if trigger == "swarm_cap":
        members.append(member(generator(scripted_model(), calls=None), name="spender"))
        budget = Budget(grace=5, tokens=TOKENS_PER_CALL)
    stop = await run_swarm(
        swarm(
            members,
            controller=_controller(run, f"controller_test_order_{trigger}"),
            channels=[],
            budget=budget,
        )
    )
    assert stop.reason == trigger
    # cancelling the controller left the scout running into grace, where it could
    # start no worker and dispatch no call: the reached cap, checked before the
    # scout's lever, or the lever refused it
    assert len(attempts) == 1 and "stopping" in attempts[0]
    assert scout_calls.started == 0
    assert controller_acted == []
    by_name = {m.name: m for m in stop.record.members}
    assert by_name["scout"].status == (
        "swarm_cap" if trigger == "swarm_cap" else "stopped"
    )
    for worker in ("worker-1", "worker-2"):
        assert (by_name[worker].state, by_name[worker].status) == ("pending", None)


@pytest.mark.anyio
@pytest.mark.parametrize("then", ["start", "raise"])
async def test_a_controller_woken_as_a_stop_begins_ends_with_the_stop(
    then: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a member's end wakes the controller and a stop begins before it resumes, so it
    # gets the event before the stop's cancellation; driven by hand so that no
    # scheduling order decides it
    end = SwarmRun._end

    def end_then_stop(self: SwarmRun, m: Any, status: str) -> None:
        end(self, m, status)
        if m.name == "first":
            self.stop("swarm_time")

    monkeypatch.setattr(SwarmRun, "_end", end_then_stop)

    async def run(control: SwarmControl) -> StopReason:
        control.start("first")
        async for _ in control.events():
            if then == "raise":
                raise ValueError("controller failed")
            control.start("second")
        return "all_done"

    agent_swarm = swarm(
        [
            member(generator(scripted_model()), name="first"),
            member(generator(scripted_model()), name="second"),
        ],
        controller=_controller(run, f"controller_test_woken_stop_{then}"),
        channels=[],
    )
    if then == "raise":
        # an error of the controller's own still fails the sample
        with pytest.raises(ValueError, match="controller failed"):
            await run_swarm(agent_swarm)
    else:
        stop = await run_swarm(agent_swarm)
        assert stop.reason == "swarm_time"
        assert statuses(stop) == {"first": "submitted", "second": None}


@pytest.mark.anyio
async def test_an_error_in_run_cancels_the_members_and_propagates() -> None:
    handles: list[MemberHandle] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("sleeper")
        handles.extend(control.members)
        await anyio.sleep(0)
        raise ValueError("controller failed")

    with pytest.raises(ValueError, match="controller failed"):
        await run_swarm(
            swarm(
                sleeper(),
                controller=_controller(run, "controller_test_raises"),
                channels=[],
            )
        )
    assert [(h.state, h.status) for h in handles] == [("ended", "cancelled")]


@agent
def crasher() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        raise RuntimeError("member crashed")

    return execute


@pytest.mark.anyio
async def test_a_foreign_member_error_cancels_the_rest_and_propagates() -> None:
    handles: list[MemberHandle] = []

    async def run(control: SwarmControl) -> StopReason:
        handles.extend(control.members)
        return await _start_all(control)

    with pytest.raises(RuntimeError, match="member crashed"):
        await run_swarm(
            swarm(
                [member(sleeper(), name="sleeper"), member(crasher(), name="crasher")],
                controller=_controller(run, "controller_test_foreign"),
                channels=[],
            )
        )
    assert {h.name: h.status for h in handles} == {
        "sleeper": "cancelled",
        "crasher": "errored",
    }


@pytest.mark.anyio
async def test_a_member_limit_ends_only_that_member() -> None:
    stop = await run_swarm(
        swarm(
            [
                member(
                    generator(scripted_model(), calls=None),
                    name="limited",
                    limits=[token_limit(2 * TOKENS_PER_CALL)],
                ),
                member(generator(scripted_model(), calls=3), name="free"),
            ],
            controller=leaderless(stop_on_submission=False),
            channels=[],
        )
    )
    assert stop.reason == "all_done"
    assert statuses(stop) == {"limited": "member_limit:token", "free": "submitted"}


@pytest.mark.anyio
async def test_no_member_text_reaches_the_controller() -> None:
    secret = "SECRET-ANSWER-1729"
    seen: list[str] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("generator")
        async for event in control.events():
            seen.append(repr(event))
        seen.extend(
            repr((m.name, m.role, m.state, m.status, m.submitted))
            for m in control.members
        )
        return "all_done"

    model = scripted_model(Script(outputs=[ModelOutput.from_content("m", secret)]))
    stop = await run_swarm(
        swarm(
            generator(model, calls=1),
            controller=_controller(run, "controller_test_no_text"),
            channels=[],
        )
    )
    assert statuses(stop) == {"generator": "submitted"}
    swarm_evidence = [
        *evidence(stop.transcript, "member_result"),
        *evidence(stop.transcript, "final"),
    ]
    assert seen and not any(secret in text for text in [*seen, repr(swarm_evidence)])


@pytest.mark.anyio
async def test_the_controller_and_the_record_see_which_members_choose_their_names() -> (
    None
):
    checked: list[tuple[str, bool]] = []
    handles: list[tuple[str, bool]] = []

    async def run(control: SwarmControl) -> StopReason:
        handles.extend((m.name, m.choose_name) for m in control.members)
        return await _start_all(control)

    @swarm_controller(name="controller_test_choose_name")
    def factory() -> Controller:
        return Controller(
            run,
            check=lambda roster: checked.extend(
                (m.name, m.choose_name) for m in roster
            ),
        )

    model = scripted_model()
    stop = await run_swarm(
        swarm(
            [
                member(generator(model, calls=1), name="lead"),
                member(generator(model, calls=1), name="w", choose_name=True),
            ],
            controller=factory(),
        )
    )
    expected = [("lead", False), ("w", True)]
    assert checked == handles == expected
    assert [(m.name, m.choose_name) for m in stop.record.members] == expected


@agent
def counted(builds: list[str | None], calls: list[str]) -> Agent:
    # built in the member's tool-state scope; counts its own calls in its closure
    scope = tool_state_instance(None)
    builds.append(scope)
    n = 0

    async def execute(state: AgentState) -> AgentState:
        nonlocal n
        n += 1
        calls.append(f"{scope} call {n}")
        return state

    return execute


def test_each_factory_member_gets_its_own_agent_in_each_sample(tmp_path: Path) -> None:
    builds: list[str | None] = []
    calls: list[str] = []
    started: list[list[str]] = []

    async def run(control: SwarmControl) -> StopReason:
        # per-sample state lives in run's locals and the handles it is given
        started.append([m.state for m in control.members])
        return await _start_all(control)

    [log] = eval(
        Task(
            dataset=[Sample(input="a"), Sample(input="b")],
            solver=swarm(
                # the same list object reaches every member: objects in args are shared
                member(counted, args={"builds": builds, "calls": calls}, count=3),
                controller=_controller(run, "controller_test_samples"),
                channels=[],
            ),
        ),
        model="mockllm/model",
        max_samples=2,
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success"
    names = ["@counted-1", "@counted-2", "@counted-3"]
    assert sorted(s or "" for s in builds) == sorted(names * 2)
    assert sorted(calls) == sorted(f"{name} call 1" for name in names * 2)
    assert started == [["pending"] * 3] * 2


@pytest.mark.anyio
async def test_built_members_each_run_their_own_agent() -> None:
    builds: list[str | None] = []
    calls: list[str] = []
    stop = await run_swarm(
        swarm(
            [
                member(counted(builds, calls), name="a"),
                member(counted(builds, calls), name="b"),
            ],
            channels=[],
        )
    )
    assert statuses(stop) == {"a": "submitted", "b": "submitted"}
    # built outside any scope, at construction; each object called once
    assert builds == [None, None]
    assert calls == ["None call 1", "None call 1"]


@agent
def unbuildable() -> Agent:
    raise ValueError("this factory cannot build")


class ACPAgent:
    """Stands in for inspect_swe's ACP agent class, which the swarm checks by name."""

    async def __call__(self, state: AgentState) -> AgentState:
        return state


ACPAgent.__module__ = "inspect_swe.acp.agent"


class _Interactive(ACPAgent):
    pass


@agent
def interactive() -> Agent:
    return _Interactive()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "factory,error,match",
    [
        (unbuildable, ValueError, "cannot build"),
        (interactive, TypeError, "ACP agents are interactive sessions"),
    ],
)
async def test_a_member_that_cannot_be_built_is_errored_and_fails_the_sample(
    factory: Any, error: type[Exception], match: str
) -> None:
    handles: list[MemberHandle] = []

    async def run(control: SwarmControl) -> StopReason:
        handles.extend(control.members)
        return await _start_all(control)

    with pytest.raises(error, match=match):
        await run_swarm(
            swarm(
                member(factory),
                controller=_controller(run, "controller_test_unbuildable"),
                channels=[],
            )
        )
    assert [h.status for h in handles] == ["errored"]


def test_a_built_acp_agent_is_refused_at_construction() -> None:
    with pytest.raises(TypeError, match="ACP agents are interactive sessions"):
        swarm(interactive(), channels=[])


@pytest.mark.parametrize("name", ["a/b", "a#b", ""])
def test_member_names_that_cannot_name_a_tool_state_scope_are_refused(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="must not contain"):
        member(generator, name=name)


class _Barrier:
    """Releases its waiters once `parties` have arrived."""

    def __init__(self, parties: int) -> None:
        self.parties = parties
        self.arrived = 0
        self.released = anyio.Event()

    async def wait(self) -> None:
        self.arrived += 1
        if self.arrived == self.parties:
            self.released.set()
        await self.released.wait()


@agent
def remembers(
    barrier: _Barrier, seen: dict[str, str], instance: str | None = None
) -> Agent:
    # writes a memory file named after its member, then lists what it can see
    async def execute(state: AgentState) -> AgentState:
        name = str(tool_state_instance(None)).removeprefix("@")
        tool = memory(instance=instance)
        await tool(command="create", path=f"/memories/{name}.md", file_text=name)
        await barrier.wait()
        seen[name] = str(await tool(command="view", path="/memories"))
        return state

    return execute


@pytest.mark.anyio
async def test_each_member_has_its_own_tool_state_unless_it_names_an_instance() -> None:
    barrier = _Barrier(5)
    seen: dict[str, str] = {}
    args: dict[str, Any] = {"barrier": barrier, "seen": seen}
    await run_swarm(
        swarm(
            [
                member(remembers(barrier, seen), name="solo"),
                member(remembers, args=args, name="copy", count=2),
                member(
                    remembers, args={**args, "instance": "team"}, name="team", count=2
                ),
            ],
            channels=[],
        )
    )
    visible = {
        name: {other for other in seen if f"{other}.md" in listing}
        for name, listing in seen.items()
    }
    assert visible == {
        "solo": {"solo"},
        "copy-1": {"copy-1"},
        "copy-2": {"copy-2"},
        "team-1": {"team-1", "team-2"},
        "team-2": {"team-1", "team-2"},
    }


@solver
def prepare() -> Solver:
    # a setup solver: an environment value in the store, and default-instance memory
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        store().set("env", "prepared")
        await memory()(command="create", path="/memories/setup.md", file_text="x")
        return state

    return solve


@agent
def environment_user(seen: dict[str, Any], change: bool) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        name = str(tool_state_instance(None)).removeprefix("@")
        seen[name] = (
            store().get("env"),
            "setup.md" in str(await memory()(command="view", path="/memories")),
        )
        if change:
            store().set("env", f"changed by {name}")
        return state

    return execute


@scorer(metrics=[])
def env_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=0, answer=store().get("env"))

    return score


def test_the_sample_store_is_shared_and_setup_tool_state_is_not(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def run(control: SwarmControl) -> StopReason:
        for name in ("first", "second"):
            control.start(name)
            async for _ in control.events():
                pass
        return "all_done"

    [log] = eval(
        Task(
            dataset=[Sample(input="x")],
            solver=[
                prepare(),
                as_solver(
                    swarm(
                        [
                            member(environment_user(seen, True), name="first"),
                            member(environment_user(seen, False), name="second"),
                        ],
                        controller=_controller(run, "controller_test_store"),
                        channels=[],
                    )
                ),
            ],
            scorer=env_scorer(),
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success" and log.samples is not None
    # the store is shared, and one member's change reaches the next and the scorer;
    # memory a setup solver wrote at the default instance is not the members'
    assert seen == {"first": ("prepared", False), "second": ("changed by first", False)}
    assert log.samples[0].scores is not None
    assert log.samples[0].scores["env_scorer"].answer == "changed by first"


@tool
def write() -> Tool:
    async def execute(key: str, value: str) -> str:
        """Write a value to the store.

        Args:
            key: The key.
            value: The value.
        """
        store().set(key, value)
        return "written"

    return execute


@agent
def store_writer(model: Model) -> Agent:
    # runs its write calls until a reply calls none
    async def execute(state: AgentState) -> AgentState:
        tools = [write()]
        while True:
            state.output = await model.generate(state.messages, tools=tools)
            state.messages.append(state.output.message)
            if not state.output.message.tool_calls:
                return state
            result = await execute_tools(state.messages, tools)
            state.messages.extend(result.messages)

    return execute


def _writes(key: str) -> Model:
    return scripted_model(
        Script(
            [ModelOutput.for_tool_call("mockllm", "write", {"key": key, "value": key})]
        )
    )


def _writes_forever(key: str) -> Model:
    # until the sample's message limit ends it
    return scripted_model(
        Script(
            respond=lambda _: ModelOutput.for_tool_call(
                "mockllm", "write", {"key": key, "value": key}
            )
        )
    )


def _swarm_span(sample: EvalSample) -> tuple[str, set[str]]:
    # the span the swarm's ledger is written in, and the spans strictly inside it
    [swarm_span] = [
        e.span_id
        for e in sample.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("type") == "ledger"
    ]
    assert swarm_span is not None
    parents = {
        e.id: e.parent_id for e in sample.events if isinstance(e, SpanBeginEvent)
    }
    inside: set[str] = set()
    for span in parents:
        parent = parents[span]
        while parent is not None:
            if parent == swarm_span:
                inside.add(span)
                break
            parent = parents.get(parent)
    return swarm_span, inside


@pytest.mark.parametrize(
    "writers,message_limit",
    [
        pytest.param({"a": _writes, "b": _writes}, None, id="two"),
        pytest.param({"a": _writes}, None, id="one"),
        pytest.param({"a": _writes, "b": _writes_forever}, 8, id="sample-limit"),
    ],
)
def test_no_span_inside_the_swarm_logs_store_events(
    tmp_path: Path,
    writers: dict[str, Callable[[str], Model]],
    message_limit: int | None,
) -> None:
    members = [member(store_writer(model(n)), name=n) for n, model in writers.items()]
    [log] = eval(
        Task(
            dataset=[Sample(input="x")],
            solver=swarm(
                members,
                # every member writes, and the sample limit is reached
                controller=leaderless(stop_on_submission=False),
                channels=[],
            ),
            message_limit=message_limit,
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.samples is not None
    [sample] = log.samples
    names = list(writers)
    # the store keeps every member's write
    assert {n: sample.store.get(n) for n in names} == {n: n for n in names}
    swarm_span, inside = _swarm_span(sample)
    assert inside
    store_spans = [e.span_id for e in sample.events if isinstance(e, StoreEvent)]
    assert not inside & set(store_spans)
    if message_limit is not None:
        assert sample.limit is not None and sample.limit.type == "message"
        # nothing about the swarm's or enclosing spans' events: a raising span logs
        # none today, which an upstream fix may change
        return
    # the swarm's span logs the net change once, the result record included
    [changes] = [
        e.changes
        for e in sample.events
        if isinstance(e, StoreEvent) and e.span_id == swarm_span
    ]
    assert {c.path.split("/")[1] for c in changes} >= {*names, RECORD_KEY}
    assert store_from_events(sample.events) == sample.store
