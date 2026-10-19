import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._util.working import init_sample_working_time, sample_waiting_time
from inspect_ai.agent import Agent, AgentState, agent
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
from inspect_ai.model import GenerateConfig, Model, ModelOutput, get_model
from inspect_ai.model._call_tools import execute_tools
from inspect_ai.tool import Tool, tool
from inspect_ai.util import store

from inspect_swarm import SwarmControl, member, swarm
from inspect_swarm._evidence import METRICS_KEY
from inspect_swarm._metrics import SwarmMetrics

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Script,
    controller,
    generator,
    run_swarm,
    sample,
    scripted_model,
    sleeper,
)


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
async def test_idle_time_is_the_wall_clock_a_member_was_not_running() -> None:
    # a ends at once and waits for b; c is never started
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
    assert a.running_time is not None and b.running_time is not None
    assert a.idle_time == metrics.wall_clock - a.running_time
    assert a.idle_time > 0.3
    assert b.idle_time < 0.15
    assert c.running_time is None and c.idle_time == metrics.wall_clock


@pytest.mark.anyio
async def test_each_members_connection_wait_is_its_own() -> None:
    # one connection: one member's call holds it while the other two queue behind it,
    # so their waits overlap, and the sample's waiting time counts the overlap once
    model = get_model(
        f"mockllm/m-{uuid4().hex}",
        memoize=False,
        config=GenerateConfig(max_connections=1),
        custom_outputs=Script(delay=0.3),
    )
    init_sample_working_time(time.monotonic())
    outcome = await run_swarm(
        swarm(member(generator, args={"model": model}, count=3), channels=[])
    )
    sample_waiting = sample_waiting_time()
    waits = sorted(m.model_wait_time for m in _metrics(outcome.transcript).members)
    # in the order the members took the connection
    assert waits[0] < 0.1
    assert waits[1] > 0.2
    assert waits[2] > waits[1] + 0.2
    # the sample merges the two overlapping waits; each member keeps its own
    assert sample_waiting == pytest.approx(waits[2], abs=0.1)
    assert sum(waits) > sample_waiting + 0.2


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


@pytest.mark.parametrize("bounded", [False, True])
def test_each_member_has_a_timeline_of_its_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounded: bool
) -> None:
    if bounded:
        monkeypatch.setenv("INSPECT_TRANSCRIPT_BOUNDED", "1")
    model = scripted_model()
    [log] = eval(
        Task(
            dataset=[Sample(input="task")],
            solver=swarm(
                [
                    member(generator(model, calls=2), name="a"),
                    member(generator(model), name="b"),
                ],
                channels=[],
            ),
        ),
        model=model,
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success" and log.samples is not None
    [sample_log] = log.samples
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

    for name, calls in [("a", 2), ("b", 1)]:
        events = _model_events(timelines[name].root)
        assert len(events) == calls
        assert {member_of(e.span_id) for e in events} == {name}


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
