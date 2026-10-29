import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import (
    Agent,
    AgentState,
    agent,
    deepagent,
    general,
    react,
    run,
)
from inspect_ai.dataset import Sample
from inspect_ai.event import (
    InfoEvent,
    ModelEvent,
    SpanBeginEvent,
    Timeline,
    TimelineEvent,
    TimelineSpan,
)
from inspect_ai.log import transcript
from inspect_ai.log._transcript import Transcript
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageAssistant,
    Model,
    ModelOutput,
)
from inspect_ai.model._call_tools import execute_tools
from inspect_ai.scorer import (
    CORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    includes,
    match,
    scorer,
)
from inspect_ai.solver import TaskState
from inspect_ai.tool import Tool, ToolCall, tool
from inspect_ai.util import background, store
from inspect_ai.util._span import AGENT_SPAN_TYPE
from pydantic import JsonValue

from inspect_swarm import (
    Channel,
    ChannelRun,
    ChannelState,
    Controller,
    Record,
    SwarmControl,
    WaitNews,
    WaitSpec,
    leaderless,
    member,
    shared_memory,
    swarm,
    swarm_channel,
    swarm_controller,
    swarm_tools,
)
from inspect_swarm._budget import current_member
from inspect_swarm._bus._bus import current_bus
from inspect_swarm._bus._record import Draft
from inspect_swarm._evidence import METRICS_KEY, comm_read
from inspect_swarm._ledger import EventLog, ScoreRecord
from inspect_swarm._metrics import GateChecks, SwarmMetrics, gate_checks
from inspect_swarm._record import RECORD_KEY, SwarmResultRecord

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Script,
    after,
    controller,
    evidence,
    generator,
    output,
    run_swarm,
    sample,
    scripted_model,
    sleeper,
)
from .test_wait import _p, nested_loop, resolve  # pyright: ignore[reportPrivateUsage]


def _metrics(transcript: Transcript) -> SwarmMetrics:
    [data] = [
        e.data
        for e in transcript.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("type") == "metrics"
    ]
    assert isinstance(data, dict)
    record = {k: v for k, v in data.items() if k != "type"}
    # the store entry is the same record
    assert store().get(METRICS_KEY) == record
    return SwarmMetrics.model_validate(record)


def _by_name(metrics: SwarmMetrics) -> dict[str, Any]:
    return {m.name: m for m in metrics.members}


@tool
def lookup() -> Tool:
    async def execute(key: str, page: int = 1) -> str:
        """Look up a key.

        Args:
            key: The key.
            page: The page.
        """
        return key

    return execute


@agent
def tool_caller(model: Model) -> Agent:
    """Generate with the lookup tool and run its calls until a reply calls none."""

    async def execute(state: AgentState) -> AgentState:
        tools = [lookup()]
        while True:
            state.output = await model.generate(state.messages, tools=tools)
            state.messages.append(state.output.message)
            if not state.output.message.tool_calls:
                return state
            result = await execute_tools(state.messages, tools)
            state.messages.extend(result.messages)

    return execute


def _lookups(*arguments: dict[str, Any]) -> list[ModelOutput]:
    return [
        *(ModelOutput.for_tool_call("mockllm", "lookup", args) for args in arguments),
        ModelOutput.from_content("mockllm", "done"),
    ]


# ---------------------------------------------------------------------------
# tokens, cost and time


@pytest.mark.anyio
async def test_tokens_and_cost_are_the_ledgers() -> None:
    model = scripted_model(cost=0.01)
    outcome = await run_swarm(
        swarm(
            [
                member(generator(model, calls=2), name="a"),
                member(generator(model), name="b"),
            ],
            controller=leaderless(stop_on_submission=False),
            channels=[],
        )
    )
    metrics = _metrics(outcome.transcript)
    members = _by_name(metrics)
    assert members["a"].tokens == 2 * TOKENS_PER_CALL
    assert members["a"].cost == pytest.approx(0.02)
    assert members["b"].tokens == TOKENS_PER_CALL
    assert members["b"].cost == pytest.approx(0.01)
    assert metrics.tokens == 3 * TOKENS_PER_CALL
    assert metrics.cost == pytest.approx(0.03)


@pytest.mark.anyio
async def test_running_time_is_from_a_members_start_to_its_end() -> None:
    # a ends at once and b after its delay; c is never started
    async def run(control: SwarmControl) -> str:
        control.start("a")
        control.start("b")
        async for _ in control.events():
            pass
        return "all_done"

    outcome = await run_swarm(
        swarm(
            [
                member(generator(scripted_model()), name="a"),
                member(generator(scripted_model(Script(delay=0.5))), name="b"),
                member(sleeper(), name="c"),
            ],
            controller=controller(run, f"partial-{uuid4().hex}"),
            channels=[],
        )
    )
    metrics = _metrics(outcome.transcript)
    a, b, c = metrics.members
    assert metrics.wall_clock >= 0.5
    assert a.running_time is not None and a.running_time < 0.2
    assert b.running_time is not None and 0.5 <= b.running_time <= metrics.wall_clock
    assert c.running_time is None


# ---------------------------------------------------------------------------
# harness validity


@pytest.mark.anyio
async def test_a_member_with_no_model_call_never_acted() -> None:
    async def run(control: SwarmControl) -> str:
        control.start("acts")
        control.start("waits")
        async for _ in control.events():
            pass
        return "all_done"

    outcome = await run_swarm(
        swarm(
            [
                member(generator(scripted_model()), name="acts"),
                member(sleeper(0), name="waits"),
                member(generator(scripted_model()), name="unstarted"),
            ],
            controller=controller(run, f"partial-{uuid4().hex}"),
            channels=[],
        )
    )
    assert _metrics(outcome.transcript).never_acted == ["waits", "unstarted"]


@agent
def wired(model: Model) -> Agent:
    """Resolve the swarm tools, then generate once."""

    async def execute(state: AgentState) -> AgentState:
        await resolve()
        state.output = await model.generate(state.messages)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channels,unwired",
    [(["shared_memory"], ["unwired"]), ([], [])],
    ids=["tools", "no-tools"],
)
async def test_members_that_acted_without_resolving_swarm_tools_are_unwired(
    channels: list[str], unwired: list[str]
) -> None:
    outcome = await run_swarm(
        swarm(
            [
                member(wired(scripted_model()), name="wired"),
                member(generator(scripted_model()), name="unwired"),
                member(sleeper(0), name="idle"),
            ],
            channels=channels,
            controller=leaderless(stop_on_submission=False),
        )
    )
    assert _metrics(outcome.transcript).tools_unwired == unwired


@agent
def replaying(model: Model) -> Agent:
    """Generate once, with the cache."""

    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate("same", cache=True)
        return state

    return execute


@pytest.mark.anyio
async def test_a_member_whose_only_call_is_a_cache_replay_acted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INSPECT_CACHE_DIR", str(tmp_path))
    model = scripted_model()
    async with sample() as t:
        # warms the cache, so that the member's call is a replay
        await model.generate("same", cache=True)
        await swarm(member(replaying(model), name="replays"), channels=[])(
            AgentState(messages=[])
        )
        assert _metrics(t).never_acted == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments,longest",
    [
        # one call three times, then changed
        ([{"key": "a"}] * 3 + [{"key": "b"}], 3),
        # the same function with other arguments does not extend a run
        ([{"key": "a"}, {"key": "b"}, {"key": "a"}], 1),
        # arguments compare as canonical JSON
        ([{"key": "a", "page": 2}, {"page": 2, "key": "a"}], 2),
        ([], 0),
    ],
)
async def test_longest_identical_tool_run(
    arguments: Sequence[dict[str, Any]], longest: int
) -> None:
    model = scripted_model(Script(_lookups(*arguments)))
    outcome = await run_swarm(swarm(tool_caller(model), channels=[]))
    [metrics] = _metrics(outcome.transcript).members
    assert metrics.longest_identical_tool_run == longest


@agent
def delegating(model: Model) -> Agent:
    """Run `tool_caller` as a subagent."""

    async def execute(state: AgentState) -> AgentState:
        return await run(tool_caller(model), state.messages)

    return execute


@pytest.mark.anyio
async def test_a_subagents_tool_calls_count_for_its_member() -> None:
    model = scripted_model(Script(_lookups({"key": "a"}, {"key": "a"})))
    outcome = await run_swarm(swarm(delegating(model), channels=[]))
    [metrics] = _metrics(outcome.transcript).members
    assert metrics.longest_identical_tool_run == 2


@pytest.mark.anyio
async def test_a_bridged_members_tool_run_is_unknown() -> None:
    # a bridged member's tool calls leave no tool events, so none are counted
    model = scripted_model(Script(_lookups({"key": "a"}, {"key": "a"})))
    outcome = await run_swarm(
        swarm(member(tool_caller(model), bridged=True), channels=[])
    )
    [metrics] = _metrics(outcome.transcript).members
    assert metrics.longest_identical_tool_run is None


# ---------------------------------------------------------------------------
# critical path


def _stepper(calls: int, delay: float, submits: bool) -> Agent:
    # `calls` calls of `delay` seconds each; the last is filtered unless it submits
    last = (
        output()
        if submits
        else ModelOutput.from_content("mockllm", "no", stop_reason="content_filter")
    )
    script = Script([*(output() for _ in range(calls - 1)), last], delay=delay)
    return generator(scripted_model(script), calls=calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "a,b,submits,answer,steps_to_answer,longest",
    [
        # a submits first, with fewer steps than b
        pytest.param((2, 0.0), (4, 0.05), True, "a", 2, 4, id="first-is-shortest"),
        # b submits first, with more steps than a
        pytest.param((1, 0.3), (3, 0.0), True, "b", 3, 3, id="first-is-longest"),
        pytest.param((2, 0.0), (3, 0.0), False, None, None, 3, id="no-submission"),
    ],
)
async def test_steps_to_answer_and_longest_member_steps(
    a: tuple[int, float],
    b: tuple[int, float],
    submits: bool,
    answer: str | None,
    steps_to_answer: int | None,
    longest: int,
) -> None:
    outcome = await run_swarm(
        swarm(
            [
                member(_stepper(*a, submits), name="a"),
                member(_stepper(*b, submits), name="b"),
            ],
            controller=leaderless(stop_on_submission=False),
            channels=[],
        )
    )
    assert outcome.record.final.member == answer
    metrics = _metrics(outcome.transcript)
    assert metrics.steps_to_answer == steps_to_answer
    assert metrics.longest_member_steps == longest


@agent
def concurrent(model: Model) -> Agent:
    """Replay a cached call, then make two calls at once."""

    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate("same", cache=True)
        async with anyio.create_task_group() as tg:
            tg.start_soon(model.generate, "x")
            tg.start_soon(model.generate, "y")
        return state

    return execute


@pytest.mark.anyio
async def test_cache_replays_and_concurrent_calls_are_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INSPECT_CACHE_DIR", str(tmp_path))
    model = scripted_model()
    async with sample() as t:
        # warms the cache, so that the member's first call is a replay
        await model.generate("same", cache=True)
        await swarm(member(concurrent(model), name="c"), channels=[])(
            AgentState(messages=[])
        )
        metrics = _metrics(t)
        assert (metrics.steps_to_answer, metrics.longest_member_steps) == (3, 3)


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["error", "cancel"])
async def test_an_interrupted_swarm_counts_its_provisional_answer(ending: str) -> None:
    # a submits after two calls; then the controller raises, or cancels an outer
    # scope, while b still waits, so the swarm never finalises
    outer = anyio.CancelScope()

    async def run(control: SwarmControl) -> str:
        control.start("a")
        control.start("b")
        async for event in control.events():
            if event.member == "a":
                break
        if ending == "error":
            raise RuntimeError("controller-failure")
        outer.cancel()
        await anyio.sleep(3600)
        return "unreachable"

    async with sample() as t:
        agent = swarm(
            [
                member(_stepper(2, 0.0, True), name="a"),
                member(sleeper(), name="b"),
            ],
            controller=controller(run, f"interrupted-{uuid4().hex}"),
            channels=[],
        )
        with outer:
            if ending == "error":
                with pytest.raises(RuntimeError, match="controller-failure"):
                    await agent(AgentState(messages=[]))
            else:
                await agent(AgentState(messages=[]))
        assert outer.cancelled_caught is (ending == "cancel")
        record = SwarmResultRecord.model_validate(store().get(RECORD_KEY))
        assert (record.final.member, record.final.provisional) == ("a", True)
        metrics = _metrics(t)
        assert (metrics.steps_to_answer, metrics.longest_member_steps) == (2, 2)


@pytest.mark.anyio
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("rule", ["last", "controller"])
async def test_steps_to_answer_follow_an_answer_other_than_the_first(
    rule: str, interrupted: bool
) -> None:
    # a submits first after two steps, b later after three; `last` and the controller's
    # choice both answer with b. Interrupted, the controller raises while c still waits,
    # so the answer stays provisional.
    async def run(control: SwarmControl) -> str:
        for m in control.members:
            control.start(m)
        ended: set[str] = set()
        async for event in control.events():
            ended.add(event.member)
            if ended >= {"a", "b"}:
                break
        if rule == "controller":
            control.choose_answer("b")
        if interrupted:
            raise RuntimeError("controller-failure")
        return "enough"

    @swarm_controller(name=f"steps-{rule}-{uuid4().hex}")
    def factory() -> Controller:
        return Controller(run, final="last" if rule == "last" else None)

    members = [
        member(_stepper(2, 0.0, True), name="a"),
        member(_stepper(3, 0.05, True), name="b"),
    ]
    if interrupted:
        members.append(member(sleeper(), name="c"))
    async with sample() as t:
        agent = swarm(members, controller=factory(), channels=[])
        if interrupted:
            with pytest.raises(RuntimeError, match="controller-failure"):
                await agent(AgentState(messages=[]))
        else:
            await agent(AgentState(messages=[]))
        record = SwarmResultRecord.model_validate(store().get(RECORD_KEY))
        assert (record.final.mode, record.final.member, record.final.provisional) == (
            rule,
            "b",
            interrupted,
        )
        metrics = _metrics(t)
        assert (metrics.steps_to_answer, metrics.longest_member_steps) == (3, 3)


# ---------------------------------------------------------------------------
# timelines


def _model_events(span: TimelineSpan) -> list[ModelEvent]:
    found: list[ModelEvent] = []
    for item in span.content:
        if isinstance(item, TimelineEvent):
            if isinstance(item.event, ModelEvent):
                found.append(item.event)
        else:
            found.extend(_model_events(item))
    return found


@agent
def recording_eviction(inner: Agent) -> Agent:
    """Run `inner`, then record in the store whether the transcript has evicted events."""

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        store().set("evicted", transcript().history.resident_events_truncated)
        return state

    return execute


@pytest.mark.parametrize("bounded", [False, True])
def test_each_member_has_a_timeline_of_its_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounded: bool
) -> None:
    if bounded:
        monkeypatch.setenv("INSPECT_TRANSCRIPT_BOUNDED", "1")
    model = scripted_model()
    # enough calls that a bounded transcript evicts a's first ones
    a_calls = 120
    [log] = eval(
        Task(
            dataset=[Sample(input="task")],
            solver=recording_eviction(
                swarm(
                    [
                        member(generator(model, calls=a_calls), name="a"),
                        member(generator(model), name="b"),
                    ],
                    controller=leaderless(stop_on_submission=False),
                    channels=[],
                )
            ),
        ),
        model=model,
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success" and log.samples is not None
    [sample_log] = log.samples
    assert sample_log.store["evicted"] is bounded
    assert sample_log.timelines is not None
    timelines = {t.name: t for t in sample_log.timelines}
    assert list(timelines) == ["a", "b"]
    spans = {
        e.name: e.id
        for e in sample_log.events
        if isinstance(e, SpanBeginEvent) and e.name in timelines
    }
    parents = {
        e.id: e.parent_id for e in sample_log.events if isinstance(e, SpanBeginEvent)
    }

    def member_of(span: str | None) -> str | None:
        while span is not None:
            for name, member_span in spans.items():
                if span == member_span:
                    return name
            span = parents.get(span)
        return None

    for name, calls in [("a", a_calls), ("b", 1)]:
        events = _model_events(timelines[name].root)
        assert len(events) == calls
        assert {member_of(e.span_id) for e in events} == {name}
    # a's first model event, evicted in a bounded transcript, is in its timeline
    first = next(
        e
        for e in sample_log.events
        if isinstance(e, ModelEvent) and member_of(e.span_id) == "a"
    )
    assert _model_events(timelines["a"].root)[0].uuid == first.uuid


@pytest.mark.anyio
async def test_timelines_and_metrics_are_written_when_a_cancellation_ends_the_swarm() -> (
    None
):
    async with sample() as t:
        with anyio.move_on_after(0.2):
            await swarm(
                member(
                    generator(scripted_model(Script(delay=0.05)), calls=None),
                    name="spender",
                ),
                channels=[],
            )(AgentState(messages=[]))
        [metrics] = _metrics(t).members
        assert metrics.name == "spender" and metrics.tokens > 0
        assert [timeline.name for timeline in t.timelines] == ["spender"]


@pytest.mark.parametrize("name", ["init", "solvers", "scorers"])
def test_a_member_named_like_a_sample_phase_is_refused(name: str) -> None:
    # Inspect's timelines read spans with these names as the sample's phases
    with pytest.raises(ValueError, match=f"a member is named '{name}'"):
        swarm(member(generator(scripted_model()), name=name), channels=[])


@pytest.mark.anyio
async def test_a_member_name_taken_by_a_timeline_is_refused() -> None:
    async with sample():
        transcript().add_timeline(
            Timeline(name="a", description="", root=TimelineSpan(id="x", name="x"))
        )
        with pytest.raises(ValueError, match="already has a timeline named 'a'"):
            await swarm(member(generator(scripted_model()), name="a"), channels=[])(
                AgentState(messages=[])
            )


@agent
def nesting(model: Model, ending: str) -> Agent:
    """Act, run a swarm whose member has this member's name, then fail or wait as `ending` says."""

    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate(state.messages)
        with pytest.raises(ValueError, match="already has a timeline named 'a'"):
            await swarm(member(generator(model), name="a"), channels=[])(
                AgentState(messages=[])
            )
        if ending == "error":
            raise RuntimeError("original-member-failure")
        await anyio.sleep(3600)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["error", "cancel"])
async def test_a_member_name_taken_while_the_swarm_runs_is_refused_there(
    ending: str,
) -> None:
    # the inner swarm is refused before it runs, and the outer swarm ends as its member
    # makes it end, with its timeline
    async with sample() as t:
        run = swarm(member(nesting(scripted_model(), ending), name="a"), channels=[])
        if ending == "error":
            with pytest.raises(RuntimeError, match="original-member-failure"):
                await run(AgentState(messages=[]))
        else:
            with anyio.move_on_after(0.3) as scope:
                await run(AgentState(messages=[]))
            assert scope.cancelled_caught
        [timeline] = t.timelines
        assert timeline.name == "a"
        assert len(_model_events(timeline.root)) == 1


# ---------------------------------------------------------------------------
# gate checks


@pytest.fixture(params=["asyncio", "trio"])
def eval_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the test's evals on each async backend."""
    monkeypatch.setenv("INSPECT_ASYNC_BACKEND", str(request.param))


def _submitter(*answers: str) -> Model:
    return scripted_model(
        Script(
            [
                ModelOutput.for_tool_call("mockllm", "submit", {"answer": answer})
                for answer in answers
            ]
        )
    )


@scorer(metrics=[accuracy()])
def always_correct() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=CORRECT)

    return score


@agent
def checking_subagent(model: Model) -> Agent:
    """Run a `react()` agent with attempts as a subagent."""

    async def execute(state: AgentState) -> AgentState:
        return await run(react(model=model, attempts=3), state.messages)

    return execute


@pytest.mark.usefixtures("eval_backend")
@pytest.mark.parametrize(
    "agent,scorers,checks,passes",
    [
        # failing then passing submissions
        (lambda: react(model=_submitter("41", "42"), attempts=3), [match()], 2, 1),
        # one submission under two task scorers is one check
        (
            lambda: react(model=_submitter("42"), attempts=3),
            [match(), includes()],
            1,
            1,
        ),
        # only the first scorer's pass counts; the last attempt is not scored
        (
            lambda: react(model=_submitter("41", "41"), attempts=2),
            [match(), always_correct()],
            1,
            0,
        ),
        # a subagent's checks are not its member's
        (lambda: checking_subagent(_submitter("41", "42")), [match()], 0, 0),
        # no in-loop scoring
        (lambda: generator(scripted_model()), [match()], 0, 0),
    ],
)
def test_gate_checks(
    tmp_path: Path,
    agent: Callable[[], Agent],
    scorers: list[Scorer],
    checks: int,
    passes: int,
) -> None:
    [log] = eval(
        Task(
            dataset=[Sample(input="What is the answer?", target="42")],
            solver=swarm(member(agent(), name="a"), channels=[]),
            scorer=scorers,
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success" and log.samples is not None, log.error
    [sample_log] = log.samples
    [metrics] = SwarmMetrics.model_validate(sample_log.store[METRICS_KEY]).members
    [result] = SwarmResultRecord.model_validate(sample_log.store[RECORD_KEY]).members
    assert (metrics.gate_checks, metrics.gate_passes) == (checks, passes)
    assert (result.gate_checks, result.gate_passes) == (checks, passes)
    assert result.first_pass_time == metrics.first_pass_time
    if passes:
        # the pass ended the member's loop
        assert result.time is not None and result.first_pass_time is not None
        assert 0 < result.first_pass_time <= result.time
    else:
        assert result.first_pass_time is None


def test_first_pass_time_is_the_passing_checks() -> None:
    events = EventLog()
    events(SpanBeginEvent(id="member", name="a", type=AGENT_SPAN_TYPE))
    events(SpanBeginEvent(id="scoring", name="score", parent_id="member"))
    events.scores += [
        ScoreRecord(span_id="scoring", scorer="match", value="I", time=11.0),
        ScoreRecord(span_id="scoring", scorer="match", value="C", time=13.0),
        ScoreRecord(span_id="scoring", scorer="match", value="C", time=17.0),
    ]
    assert gate_checks(events, "member", "match", start=10.0) == GateChecks(3, 2, 3.0)


def test_records_written_before_gate_checks_load() -> None:
    # version-1 records as written before the gate fields
    metrics = SwarmMetrics.model_validate(
        {
            "version": 1,
            "tokens": 10,
            "cost": 0.0,
            "wall_clock": 1.0,
            "members": [
                {
                    "name": "a",
                    "tokens": 10,
                    "cost": 0.0,
                    "running_time": 1.0,
                    "longest_identical_tool_run": 1,
                }
            ],
            "never_acted": [],
            "steps_to_answer": 1,
            "longest_member_steps": 1,
        }
    )
    record = SwarmResultRecord.model_validate(
        {
            "version": 1,
            "members": [
                {
                    "name": "a",
                    "role": None,
                    "state": "ended",
                    "status": "submitted",
                    "submitted": True,
                    "time": 1.0,
                }
            ],
            "final": {
                "mode": "first",
                "member": "a",
                "reason": None,
                "provisional": False,
            },
        }
    )
    for gates in [metrics.members[0], record.members[0]]:
        assert (gates.gate_checks, gates.gate_passes, gates.first_pass_time) == (
            0,
            0,
            None,
        )


# ---------------------------------------------------------------------------
# waits


@agent
def waiting_peer(ready: anyio.Event, finish: anyio.Event) -> Agent:
    """Wait for a peer's file, for that peer's end, for the last member's end, and alone."""

    async def execute(state: AgentState) -> AgentState:
        wait = (await resolve())["swarm_wait"]
        ready.set()
        # released by working's write
        await wait(path=_p("r"))
        # by working's end, which held hears of first
        await wait(path=_p("none"))
        finish.set()
        # by held's end
        await wait(path=_p("none"))
        # no other member is running
        await wait(path=_p("none"))
        return state

    return execute


@agent
def working(ready: anyio.Event) -> Agent:
    """Work while a subagent waits, then write the file that ends both waits."""

    async def execute(state: AgentState) -> AgentState:
        memory = (await resolve())["shared_memory"]
        await ready.wait()

        async def subagent() -> None:
            async with nested_loop("child") as tools:
                await tools["swarm_wait"]()

        async with anyio.create_task_group() as tg:
            tg.start_soon(subagent)
            await anyio.sleep(0.2)
            await memory(command="create", path=_p("r/a.md"), file_text="x")
        return state

    return execute


@agent
def held(finish: anyio.Event) -> Agent:
    """Run until told to finish."""

    async def execute(state: AgentState) -> AgentState:
        await finish.wait()
        return state

    return execute


@pytest.mark.anyio
async def test_waits_wait_time_and_idle_time() -> None:
    ready, finish = anyio.Event(), anyio.Event()
    outcome = await run_swarm(
        swarm(
            [
                member(waiting_peer(ready, finish), name="p"),
                member(working(ready), name="q"),
                member(held(finish), name="r"),
            ],
            channels=[shared_memory()],
            controller=leaderless(stop_on_submission=False),
        )
    )
    p, q, r = _metrics(outcome.transcript).members
    assert (p.waits, p.waits_by_outcome) == (4, {"news": 3, "all_waiting": 1})
    assert p.wait_time is not None and p.idle_time is not None
    # p's own loop waited through q's work, with nothing else of p running
    assert 0.15 <= p.idle_time <= p.wait_time
    assert p.running_time is not None and p.idle_time <= p.running_time
    # q's subagent waited while q's own loop worked: wait time, but no idle time
    assert (q.waits, q.waits_by_outcome, q.idle_time) == (1, {"news": 1}, 0)
    assert q.wait_time is not None and q.wait_time >= 0.15
    assert (r.waits, r.waits_by_outcome, r.wait_time, r.idle_time) == (0, {}, 0, 0)


@pytest.mark.anyio
@pytest.mark.parametrize("channels", [["filesystem"], []])
async def test_a_swarm_that_offers_no_wait_has_no_wait_metrics(
    channels: list[str],
) -> None:
    outcome = await run_swarm(
        swarm(member(generator(scripted_model()), name="a"), channels=channels)
    )
    [a] = _metrics(outcome.transcript).members
    assert (a.waits, a.waits_by_outcome, a.wait_time, a.idle_time) == (
        None,
        None,
        None,
        None,
    )


# ---------------------------------------------------------------------------
# the dependency-linked critical path


def _answered_by(name: str) -> Controller:
    # starts every member, lets them all end, then chooses `name`'s submission
    async def run(control: SwarmControl) -> str:
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            pass
        control.choose_answer(name)
        return "all_done"

    return controller(run, f"answered-by-{name}-{uuid4().hex}")


def _all_metrics(transcript: Transcript) -> list[SwarmMetrics]:
    # every swarm's metrics in a sample, in the order the swarms ended
    return [
        SwarmMetrics.model_validate({k: v for k, v in e.data.items() if k != "type"})
        for e in transcript.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("type") == "metrics"
    ]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # Inspect's trace records are captured too once an eval in the same process has set
    # up its logging
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _calls(*calls: tuple[str, dict[str, Any]]) -> Model:
    return scripted_model(
        Script([ModelOutput.for_tool_call("mockllm", f, a) for f, a in calls])
    )


_SUBMIT = ("submit", {"answer": "done"})


def _eval_metrics(solver: Agent, tmp_path: Path) -> SwarmMetrics:
    # the swarm's metrics in an eval of one sample, for react() members, which need an
    # active sample
    [log] = eval(
        Task(dataset=[Sample(input="task")], solver=solver),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success" and log.samples is not None, log.error
    return SwarmMetrics.model_validate(log.samples[0].store[METRICS_KEY])


@pytest.mark.usefixtures("eval_backend")
def test_a_wait_and_a_view_link_the_waiter_to_the_writer(tmp_path: Path) -> None:
    # a makes three steps that write nothing, then creates the file on its fourth; b
    # waits on its first step, views the file on its second, and submits on its third
    a = _calls(
        *[("shared_memory", {"command": "changes"})] * 3,
        ("shared_memory", {"command": "create", "path": _p("a.md"), "file_text": "x"}),
        _SUBMIT,
    )
    b = _calls(
        ("swarm_wait", {}),
        ("shared_memory", {"command": "view", "path": _p("a.md")}),
        _SUBMIT,
    )
    metrics = _eval_metrics(
        swarm(
            [
                member(react(model=a, tools=[swarm_tools()]), name="a"),
                member(react(model=b, tools=[swarm_tools()]), name="b"),
            ],
            channels=[shared_memory()],
            controller=_answered_by("b"),
        ),
        tmp_path,
    )
    assert (metrics.steps_to_answer, metrics.critical_steps_to_answer) == (3, 6)


def _parallel_children() -> Model:
    # the parent dispatches two general() children in one turn, each child submits at
    # once, and then the parent submits
    def respond(input: list[ChatMessage]) -> ModelOutput:
        if any(m.role == "user" and "child task" in m.text for m in input):
            return ModelOutput.for_tool_call("mockllm", "submit", {"answer": "x"})
        if any(m.role == "tool" for m in input):
            return ModelOutput.for_tool_call("mockllm", "submit", {"answer": "done"})
        message = ChatMessageAssistant(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"c{i}", function="agent", arguments={"prompt": "child task"}
                )
                for i in range(2)
            ],
            model="mockllm",
        )
        return ModelOutput(
            model="mockllm",
            choices=[ChatCompletionChoice(message=message, stop_reason="tool_calls")],
        )

    return scripted_model(Script(respond=respond))


@pytest.mark.usefixtures("eval_backend")
def test_parallel_children_count_one_after_another(tmp_path: Path) -> None:
    metrics = _eval_metrics(
        swarm(
            member(
                deepagent,
                args={
                    "model": _parallel_children(),
                    "subagents": [general()],
                    "todo_write": False,
                },
                name="c",
            ),
            channels=[],
        ),
        tmp_path,
    )
    # the parent's two steps and one of each child, where the slower branch alone
    # would give 3
    assert (metrics.steps_to_answer, metrics.critical_steps_to_answer) == (4, 4)


@agent
def draining(model: Model, late: Model) -> Agent:
    """Make three steps, then return with a fourth call in flight in background work."""

    async def execute(state: AgentState) -> AgentState:
        for _ in range(3):
            state.output = await model.generate(state.messages)
        background(late.generate, "late")
        # the background call is dispatched before the agent returns
        await anyio.sleep(0.05)
        return state

    return execute


@agent
def waits_for_an_end(model: Model) -> Agent:
    """Make a step, wait for news, then make another step."""

    async def execute(state: AgentState) -> AgentState:
        wait = (await resolve())["swarm_wait"]
        await model.generate(state.messages)
        await wait()
        state.output = await model.generate(state.messages)
        return state

    return execute


@pytest.mark.anyio
async def test_a_wait_for_an_end_follows_the_members_drained_work() -> None:
    outcome = await run_swarm(
        swarm(
            [
                member(
                    draining(scripted_model(), scripted_model(Script(delay=0.2))),
                    name="q",
                ),
                member(waits_for_an_end(scripted_model()), name="w"),
            ],
            channels=[shared_memory()],
            controller=_answered_by("w"),
        )
    )
    [wait] = evidence(outcome.transcript, "waited")
    assert wait["ended"] == [{"member": "q", "status": "submitted"}]
    metrics = _metrics(outcome.transcript)
    assert (metrics.steps_to_answer, metrics.critical_steps_to_answer) == (2, 5)


@agent
def deep_writer(model: Model, written: anyio.Event) -> Agent:
    """Make ten steps, then write the file a peer reads."""

    async def execute(state: AgentState) -> AgentState:
        memory = (await resolve())["shared_memory"]
        for _ in range(10):
            state.output = await model.generate(state.messages)
        await memory(command="create", path=_p("peer.md"), file_text="peer content")
        written.set()
        return state

    return execute


@agent
def reads_in_its_drain(written: anyio.Event, replace: bool) -> Agent:
    """Return a placeholder with no step; background work views the peer's file in the drain and, if `replace`, answers with it."""

    async def execute(state: AgentState) -> AgentState:
        memory = (await resolve())["shared_memory"]

        async def fetch() -> None:
            await written.wait()
            text = await memory(command="view", path=_p("peer.md"))
            if replace:
                state.output = ModelOutput.from_content("mockllm", str(text))

        background(fetch)
        state.output = ModelOutput.from_content("mockllm", "placeholder")
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("replace", [True, False])
async def test_a_read_in_the_answering_members_drain_counts(replace: bool) -> None:
    written = anyio.Event()
    outcome = await run_swarm(
        swarm(
            [
                member(deep_writer(scripted_model(), written), name="p"),
                member(reads_in_its_drain(written, replace), name="m"),
            ],
            channels=[shared_memory()],
            controller=_answered_by("m"),
        )
    )
    answer = outcome.state.output.completion
    assert ("peer content" in answer) is replace
    assert (answer == "placeholder") is not replace
    metrics = _metrics(outcome.transcript)
    assert (metrics.steps_to_answer, metrics.critical_steps_to_answer) == (0, 10)


@agent
def writes_twice_and_views(model: Model) -> Agent:
    """Make a step, write f-1 of its board twice, and view it."""

    async def execute(state: AgentState) -> AgentState:
        memory = (await resolve())["shared_memory"]
        state.output = await model.generate(state.messages)
        await memory(command="create", path=_p("inner.md"), file_text="x")
        await memory(command="append", path=_p("inner.md"), append_text="y")
        await memory(command="view", path=_p("inner.md"))
        return state

    return execute


# A custom waiting channel that takes the built-in board's name, with records and wait
# entries of its own shape: every record is news to every other member's waits.
class _ImpostorState:
    def __init__(self) -> None:
        self.applied: list[Record] = []

    def tools(self) -> list[Tool]:
        return []

    def validate(self, record: Record) -> None:
        return None

    def admit(self, record: Record) -> None:
        return None

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        self.applied.append(record)
        return [], {"size": len(self.applied)}

    def wait_check(self, spec: WaitSpec) -> None:
        return None

    def wakes(self, spec: WaitSpec, record: Record) -> bool:
        return record.sender != spec.member

    def news(self, spec: WaitSpec) -> WaitNews | None:
        if not any(r.sender != spec.member for r in self.applied):
            return None
        return WaitNews(
            ["a note"], [{"foreign_record": "x-1"}, {"file": "f-99", "version": 7}]
        )


class _Impostor(Channel):
    name = "shared_memory"
    prefix = "s"
    max_wait = 5

    def open(self, run: ChannelRun) -> ChannelState | None:
        return _ImpostorState()


@swarm_channel
def metrics_test_impostor() -> Channel:
    return _Impostor()


@agent
def notes(model: Model, steps: int) -> Agent:
    """Make `steps` steps, then post a note and record a read, each of the custom channel's own shape."""

    async def execute(state: AgentState) -> AgentState:
        for _ in range(steps):
            state.output = await model.generate(state.messages)
        bus = current_bus.get()
        assert bus is not None
        await bus.deliver(Draft("shared_memory", "note", wake=True))
        context = current_member.get()
        assert context is not None
        comm_read(
            context.span_id,
            context.name,
            "shared_memory",
            "r-1",
            None,
            False,
            None,
            [{"file": "f-99", "version": 7, "lines": None, "deleted": False}],
            False,
        )
        return state

    return execute


@pytest.mark.anyio
async def test_a_custom_channel_named_like_the_board_makes_no_file_edge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # w waits once p has ended: the wait names the custom channel's entries, f-99
    # version 7 among them, and p's end, which alone links
    outcome = await run_swarm(
        swarm(
            [
                member(notes(scripted_model(), 10), name="p"),
                member(after(["p"], waits_for_an_end(scripted_model())), name="w"),
            ],
            channels=["metrics_test_impostor"],
            controller=_answered_by("w"),
        )
    )
    [wait] = evidence(outcome.transcript, "waited")
    assert wait["files"] == [
        {"foreign_record": "x-1"},
        {"file": "f-99", "version": 7},
    ]
    assert wait["ended"] == [{"member": "p", "status": "submitted"}]
    metrics = _metrics(outcome.transcript)
    assert (metrics.steps_to_answer, metrics.critical_steps_to_answer) == (2, 11)
    assert not _warnings(caplog)


@agent
def nests(model: Model, inner: Agent) -> Agent:
    """Make a step and create f-1 on the outer board, run a nested swarm, then view f-1."""

    async def execute(state: AgentState) -> AgentState:
        memory = (await resolve())["shared_memory"]
        state.output = await model.generate(state.messages)
        await memory(command="create", path=_p("outer.md"), file_text="x")
        await run(inner, "inner task")
        await memory(command="view", file_id="f-1")
        return state

    return execute


def _impostor_swarm() -> Agent:
    return swarm(
        [
            member(notes(scripted_model(), 1), name="ip"),
            member(after(["ip"], waits_for_an_end(scripted_model())), name="iw"),
        ],
        channels=["metrics_test_impostor"],
        # iw, which waits for ip's end, answers
        controller=leaderless(stop_on_submission=False, final="last"),
    )


def _board_swarm() -> Agent:
    return swarm(
        member(writes_twice_and_views(scripted_model()), name="ib"),
        channels=[shared_memory()],
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "inner,inner_value,steps",
    [
        # its f-1 reaches version 2 while the outer board's f-1 is at version 1
        pytest.param(_board_swarm, 1, 2, id="built-in-board"),
        # its results and wait entries have shapes of their own
        pytest.param(_impostor_swarm, 2, 4, id="custom-channel-named-shared_memory"),
    ],
)
async def test_a_nested_swarms_channels_make_no_edge_for_the_outer_swarm(
    inner: Callable[[], Agent],
    inner_value: int,
    steps: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # the outer member views the outer board's f-1 after the inner swarm ends, so inner
    # evidence taken as the outer board's would make that view inconsistent
    outer = swarm(
        member(nests(scripted_model(), inner()), name="m"), channels=[shared_memory()]
    )
    # two swarms in one sample, so not run_swarm(), which reads one final event
    async with sample() as transcript:
        await run(outer, "task")
    inner_metrics, metrics = _all_metrics(transcript)
    assert inner_metrics.critical_steps_to_answer == inner_value
    # the inner members' steps are the outer member's
    assert (metrics.steps_to_answer, metrics.critical_steps_to_answer) == (
        steps,
        steps,
    )
    assert not _warnings(caplog)


@pytest.mark.anyio
async def test_a_resumed_sample_has_no_critical_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("inspect_swarm._ledger.sample_resumed", lambda: True)
    outcome = await run_swarm(
        swarm(member(generator(scripted_model()), name="a"), channels=[])
    )
    metrics = _metrics(outcome.transcript)
    assert (metrics.steps_to_answer, metrics.critical_steps_to_answer) == (1, None)
