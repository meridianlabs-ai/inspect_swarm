import gc
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
import pytest
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, deepagent, general
from inspect_ai.agent._bridge.util import bridge_model_generate
from inspect_ai.dataset import Sample
from inspect_ai.event import (
    InfoEvent,
    ModelEvent,
    SpanBeginEvent,
    SpanEndEvent,
    ToolEvent,
)
from inspect_ai.log import EvalSample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelOutput,
)
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import Tool, ToolCall, ToolChoice, ToolInfo, tool
from inspect_ai.util import (
    Limit,
    LimitExceededError,
    background,
    message_limit,
    sandbox,
    time_limit,
    token_limit,
)

from inspect_swarm import Budget, SwarmControl, member, swarm
from inspect_swarm._controller._control import SwarmRun
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._record import RECORD_KEY, SwarmResultRecord

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Outcome,
    Script,
    controller,
    generator,
    output,
    run_swarm,
    sample,
    scripted_model,
    statuses,
)
from .test_budget import _written_ledger
from .test_member_sandboxes import _member_name, _name, environments

# ---------------------------------------------------------------------------
# Members whose agents start background work directly

# A step of a parent or child loop: a model call on its own conversation, model calls
# until it is stopped, or any other awaited work (a tool call in flight).
Step = Literal["call", "forever"] | Callable[[], Awaitable[Any]]


def _sleep(seconds: float) -> Callable[[], Awaitable[None]]:
    return lambda: anyio.sleep(seconds)


@dataclass
class _Child:
    name: str
    steps: list[Step]
    # its own model, or the parent's
    model: Model | None = None
    # the length of its conversation at its first call
    messages: int = 1


async def _steps(
    model: Model, messages: list[ChatMessage], steps: Sequence[Step]
) -> None:
    for step in steps:
        if step == "call" or step == "forever":
            while True:
                result = await model.generate(messages)
                messages.append(result.message)
                if step == "call":
                    break
        else:
            await step()


# A background child: its steps, logged as `<name> finished`, `<name> stopped` (by a
# limit, which the owner then handles) or `<name> cancelled`.
async def _run_child(model: Model, child: _Child, log: list[str]) -> None:
    messages: list[ChatMessage] = [
        ChatMessageUser(content=child.name) for _ in range(child.messages)
    ]
    try:
        await _steps(child.model or model, messages, child.steps)
    except LimitExceededError:
        log.append(f"{child.name} stopped")
        raise
    except anyio.get_cancelled_exc_class():
        log.append(f"{child.name} cancelled")
        raise
    log.append(f"{child.name} finished")


@agent
def spawner(
    model: Model,
    children: list[_Child],
    log: list[str],
    steps: list[Step] | None = None,
) -> Agent:
    """Start each child with `background()`, run `steps`, then answer."""

    async def execute(state: AgentState) -> AgentState:
        for child in children:
            background(_run_child, model, child, log)
        await _steps(model, state.messages, steps or [])
        state.output = ModelOutput.from_content("mockllm", "done")
        return state

    return execute


def _ledger(outcome: Outcome) -> SwarmLedger:
    return _written_ledger(outcome.transcript)


def _times(outcome: Outcome) -> dict[str, float | None]:
    return {m.name: m.time for m in outcome.record.members}


@pytest.mark.anyio
async def test_each_members_drain_waits_only_for_its_own_background_work() -> None:
    model = scripted_model()
    log: list[str] = []
    outcome = await run_swarm(
        swarm(
            [
                member(
                    spawner(model, [_Child("quick-child", [_sleep(0.05)])], log),
                    name="quick",
                ),
                member(
                    spawner(model, [_Child("stuck-child", [_sleep(3600)])], log),
                    name="stuck",
                ),
            ],
            channels=[],
            budget=Budget(grace=0.5),
        )
    )
    assert statuses(outcome) == {"quick": "submitted", "stuck": "submitted"}
    assert sorted(log) == ["quick-child finished", "stuck-child cancelled"]
    times = _times(outcome)
    assert times["quick"] is not None and times["quick"] < 0.4
    # cancelled at the end of its member's drain, not when the sample ends
    assert times["stuck"] is not None and 0.5 <= times["stuck"] < 3
    assert {
        m.name: (m.background_started, m.background_cancelled)
        for m in _ledger(outcome).members
    } == {"quick": (1, 0), "stuck": (1, 1)}


@pytest.mark.anyio
async def test_a_controller_sees_a_member_end_after_its_drain() -> None:
    log: list[str] = []
    seen: list[list[str]] = []

    async def until_proved(control: SwarmControl) -> str:
        control.start("prover")
        control.start("other")
        async for event in control.events():
            if event.member == "prover" and event.submitted:
                seen.append(list(log))
                break
        return "verified"

    outcome = await run_swarm(
        swarm(
            [
                member(
                    spawner(scripted_model(), [_Child("helper", [_sleep(0.3)])], log),
                    name="prover",
                ),
                member(
                    generator(scripted_model(Script(delay=0.02)), calls=None),
                    name="other",
                ),
            ],
            controller=controller(until_proved, "background_test_until_proved"),
            channels=[],
            budget=Budget(grace=2),
        )
    )
    # the prover's event came once its helper had finished, the drain's length later
    assert seen == [["helper finished"]]
    times = _times(outcome)
    assert times["prover"] is not None and times["prover"] >= 0.3
    assert statuses(outcome) == {"prover": "submitted", "other": "stopped"}
    assert outcome.reason == "verified"


@agent
def replacer(log: list[str]) -> Agent:
    """Start a child that crashes after the agent returns a replacement state."""

    async def crashes() -> None:
        await anyio.sleep(0.05)
        log.append("crashed")
        raise RuntimeError("child crashed")

    async def execute(state: AgentState) -> AgentState:
        background(crashes)
        replacement = AgentState(messages=state.messages)
        replacement.output = ModelOutput.from_content("mockllm", "replaced")
        return replacement

    return execute


@pytest.mark.anyio
async def test_a_foreign_error_in_the_drain_keeps_the_returned_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ended: list[tuple[str, str]] = []
    end = SwarmRun._end  # pyright: ignore[reportPrivateUsage]

    def recording_end(self: SwarmRun, m: Any, status: str) -> None:
        end(self, m, status)
        ended.append((status, m.agent_state.output.completion))

    monkeypatch.setattr(SwarmRun, "_end", recording_end)
    log: list[str] = []
    with pytest.raises(RuntimeError, match="child crashed"):
        await run_swarm(
            swarm(member(replacer(log), name="x"), channels=[], budget=Budget(grace=2))
        )
    assert log == ["crashed"]
    assert ended == [("errored", "replaced")]


@pytest.mark.anyio
async def test_a_member_time_limit_cancels_only_its_agent() -> None:
    log: list[str] = []
    outcome = await run_swarm(
        swarm(
            member(
                spawner(
                    scripted_model(),
                    [
                        # still in its tool call at the limit; it finishes within grace
                        _Child("fast", [_sleep(0.3), "call"]),
                        _Child("slow", [_sleep(3600)]),
                    ],
                    log,
                    steps=[_sleep(3600)],
                ),
                name="x",
                limits=[time_limit(0.1)],
            ),
            channels=[],
            budget=Budget(grace=0.8),
        )
    )
    assert statuses(outcome) == {"x": "member_limit:time"}
    # the fast child finished its tool call and was refused its next model call
    assert log == ["fast stopped", "slow cancelled"]
    [line] = _ledger(outcome).members
    assert (line.background_started, line.background_cancelled) == (2, 1)


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["time_cap", "controller"])
async def test_a_soft_stop_lets_background_work_finish_its_calls(stop: str) -> None:
    async def returns_soon(control: SwarmControl) -> str:
        control.start("x")
        await anyio.sleep(0.1)
        return "all_done"

    log: list[str] = []
    model = scripted_model(Script(delay=0.02), cost=1.0)
    outcome = await run_swarm(
        swarm(
            member(
                spawner(
                    model,
                    [
                        _Child("a", [_sleep(0.3), "call"]),
                        _Child("b", ["call", _sleep(0.3), "call"]),
                    ],
                    log,
                    steps=["forever"],
                ),
                name="x",
            ),
            controller=controller(returns_soon, "background_test_returns_soon")
            if stop == "controller"
            else "leaderless",
            channels=[],
            budget=Budget(time=0.1, grace=2) if stop == "time_cap" else Budget(grace=2),
        )
    )
    assert outcome.reason == ("swarm_time" if stop == "time_cap" else "all_done")
    assert statuses(outcome) == {"x": "stopped"}
    assert sorted(log) == ["a stopped", "b stopped"]
    ledger = _ledger(outcome)
    [line] = ledger.members
    assert line.background_cancelled == 0 and not line.cancelled
    # every call in flight completed and was recorded
    assert ledger.calls.errored == 0


# A limit a background child reaches: a swarm cap, one of the member's own limits, or a
# custom limit it raises to stop itself. `scenario` makes the member's model, its
# children and its own steps, fresh for each run.
@dataclass
class _ChildLimit:
    id: str
    scenario: Callable[[list[str]], tuple[Model, list[_Child], list[Step]]]
    log: list[str]
    status: str
    limits: list[Limit] = field(default_factory=lambda: [])
    budget: Budget = field(default_factory=lambda: Budget(grace=2))
    # the sample's cost limit, which a cost cap needs
    sample_cost: float | None = None
    reason: str | None = None
    # a second member, still running when the child stops, which submits
    peer: bool = False


def _cap_running(log: list[str]) -> tuple[Model, list[_Child], list[Step]]:
    # the greedy child reaches the cap while the parent and the slow child work
    return (
        scripted_model(Script(delay=0.05), cost=1.0),
        [
            _Child("greedy", ["forever"], model=scripted_model(cost=1.0)),
            _Child("slow", [_sleep(0.3), "call"]),
        ],
        ["forever"],
    )


def _cap_drain(log: list[str]) -> tuple[Model, list[_Child], list[Step]]:
    # the parent uses the cap and returns while the child's call is in flight; that
    # call passes the cap during the drain
    late = _Child(
        "late", ["call", "call"], model=scripted_model(Script(delay=0.2), cost=1.0)
    )
    return scripted_model(cost=1.0), [late], ["call", "call"]


def _member_limit(log: list[str]) -> tuple[Model, list[_Child], list[Step]]:
    async def parent_continues() -> None:
        log.append("parent continued")

    # the parent goes on after the child has ended, and meets the same limit
    return (
        scripted_model(),
        [_Child("greedy", ["forever"])],
        [_sleep(0.2), parent_continues, "call"],
    )


def _message_limit(log: list[str]) -> tuple[Model, list[_Child], list[Step]]:
    return (
        scripted_model(),
        [
            _Child("long", ["call"], messages=10),
            _Child("short", [_sleep(0.05), "call"]),
        ],
        [_sleep(0.2), "call"],
    )


async def _stop_itself() -> None:
    # how an agent stops itself, such as inspect_boltons' loop limits
    raise LimitExceededError("custom", value=1, limit=1)


def _custom(when: str) -> Callable[[list[str]], tuple[Model, list[_Child], list[Step]]]:
    def scenario(log: list[str]) -> tuple[Model, list[_Child], list[Step]]:
        # in the drain the child stops itself after the agent has returned, and the
        # lever refuses the sibling's calls, so it makes none
        running = when == "running"
        sibling: list[Step] = [_sleep(0.1), "call"] if running else [_sleep(0.1)]
        return (
            scripted_model(),
            [_Child("stuck", [_sleep(0.02), _stop_itself]), _Child("sibling", sibling)],
            [_sleep(0.2), "call"] if running else [],
        )

    return scenario


_CHILD_LIMITS = [
    _ChildLimit(
        "token-cap-running",
        _cap_running,
        ["greedy stopped", "slow stopped"],
        "swarm_cap",
        budget=Budget(tokens=4 * TOKENS_PER_CALL, grace=2),
        reason="swarm_cap",
    ),
    _ChildLimit(
        "cost-cap-running",
        _cap_running,
        ["greedy stopped", "slow stopped"],
        "swarm_cap",
        budget=Budget(cost=4.0, grace=2),
        sample_cost=100,
        reason="swarm_cap",
    ),
    _ChildLimit(
        "token-cap-drain",
        _cap_drain,
        ["late stopped"],
        "submitted",
        budget=Budget(tokens=2 * TOKENS_PER_CALL, grace=2),
        reason="swarm_cap",
    ),
    _ChildLimit(
        "cost-cap-drain",
        _cap_drain,
        ["late stopped"],
        "submitted",
        budget=Budget(cost=2.0, grace=2),
        sample_cost=100,
        reason="swarm_cap",
    ),
    _ChildLimit(
        "member-token-limit",
        _member_limit,
        ["greedy stopped", "parent continued"],
        "member_limit:token",
        limits=[token_limit(3 * TOKENS_PER_CALL)],
        peer=True,
    ),
    _ChildLimit(
        "member-message-limit",
        _message_limit,
        ["long stopped", "short finished"],
        "submitted",
        limits=[message_limit(5)],
    ),
    _ChildLimit(
        "custom-running",
        _custom("running"),
        ["sibling finished", "stuck stopped"],
        "submitted",
    ),
    _ChildLimit(
        "custom-drain",
        _custom("drain"),
        ["sibling finished", "stuck stopped"],
        "submitted",
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize("case", _CHILD_LIMITS, ids=lambda c: c.id)
async def test_a_limit_reached_in_a_child_ends_only_that_child(
    case: _ChildLimit,
) -> None:
    log: list[str] = []
    model, children, steps = case.scenario(log)
    members = [
        member(spawner(model, children, log, steps=steps), name="x", limits=case.limits)
    ]
    if case.peer:
        peer = generator(scripted_model(Script(delay=0.1)), calls=3)
        members.append(member(peer, name="y"))
    outcome = await run_swarm(
        swarm(members, channels=[], budget=case.budget), cost=case.sample_cost
    )
    if case.reason is not None:
        assert outcome.reason == case.reason
    expected = {"x": case.status} | ({"y": "submitted"} if case.peer else {})
    assert statuses(outcome) == expected
    assert sorted(log) == case.log
    ledger = _ledger(outcome)
    line = ledger.members[0]
    assert (line.background_started, line.background_cancelled) == (len(children), 0)
    # siblings' calls in flight finished
    assert ledger.calls.errored == 0


@pytest.mark.anyio
@pytest.mark.parametrize("when", ["running", "drain"])
async def test_a_sample_limit_reached_in_a_child_ends_the_sample(
    when: str, tmp_path: Path
) -> None:
    log: list[str] = []
    child = _Child("child", ["call"], model=scripted_model(Script(delay=0.2)))
    sample = await _eval(
        swarm(
            [
                member(
                    generator(scripted_model(), calls=0, answer="early answer"),
                    name="early",
                ),
                member(
                    spawner(
                        scripted_model(),
                        [child],
                        log,
                        # in the drain case it returns with the child's call in flight
                        steps=[_sleep(3600 if when == "running" else 0.05)],
                    ),
                    name="busy",
                ),
            ],
            channels=[],
            # a token cap above the sample's limit, which ends the swarm first
            budget=Budget(tokens=10**6, grace=2),
        ),
        tmp_path,
        token_limit=TOKENS_PER_CALL - 10,
    )
    assert log == ["child stopped"]
    assert sample.limit is not None and sample.limit.type == "token"
    assert sample.output.completion == "early answer"
    assert {m.name: m.status for m in _record(sample).members} == {
        "early": "submitted",
        "busy": "errored",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "when,status", [("running", "cancelled"), ("drain", "submitted")]
)
async def test_the_hard_stop_cancels_a_members_background_work(
    when: str, status: str
) -> None:
    async def cleanup_yields_first() -> None:
        try:
            await anyio.sleep_forever()
        finally:
            # the child finishes its cancellation before the owner's exit runs
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0.01)

    log: list[str] = []
    outcome = await run_swarm(
        swarm(
            member(
                spawner(
                    scripted_model(),
                    [_Child("held", [_sleep(3600)])],
                    log,
                    # in the drain case the agent returns during the swarm's grace
                    steps=[cleanup_yields_first if when == "running" else _sleep(0.2)],
                ),
                name="x",
            ),
            channels=[],
            budget=Budget(time=0.05, grace=0.4),
        )
    )
    assert outcome.reason == "swarm_time"
    assert statuses(outcome) == {"x": status}
    assert log == ["held cancelled"]
    [line] = _ledger(outcome).members
    assert (line.background_started, line.background_cancelled) == (1, 1)
    assert line.cancelled


@agent
def eager(log: list[str]) -> Agent:
    """Start background work while the agent is built."""

    async def work() -> None:
        try:
            await anyio.sleep_forever()
        finally:
            log.append("work ended")

    background(work)

    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


@pytest.mark.anyio
async def test_background_work_started_by_a_factory_is_refused() -> None:
    log: list[str] = []
    async with sample() as transcript:
        with pytest.raises(RuntimeError, match="'eager'.*factory started background"):
            await swarm(member(eager, args={"log": log}), channels=[])(
                AgentState(messages=[ChatMessageUser(content="task")])
            )
        # cancelled before the member ended
        assert log == ["work ended"]
        [line] = _written_ledger(transcript).members
    assert line.status == "errored"
    assert (line.background_started, line.background_cancelled) == (1, 1)


async def _bridged_call(model: Model) -> None:
    # a generation the agent bridge serves, as for a bridged subagent
    with bridge_model_generate():
        await model.generate([ChatMessageUser(content="bridged")])


@pytest.mark.anyio
@pytest.mark.parametrize("declared", [True, False], ids=["declared", "detected"])
async def test_bridge_use_and_background_work_are_refused(declared: bool) -> None:
    log: list[str] = []
    model = scripted_model()
    agent = spawner(
        model,
        [_Child("child", [_sleep(3600)])],
        log,
        # bridge use found after the child was started
        steps=[] if declared else [lambda: _bridged_call(model)],
    )
    async with sample() as transcript:
        with pytest.raises(RuntimeError, match="uses the agent bridge and background"):
            await swarm(
                member(agent, name="x", bridged=declared),
                channels=[],
                budget=Budget(grace=0.1),
            )(AgentState(messages=[ChatMessageUser(content="task")]))
        [line] = _written_ledger(transcript).members
    assert line.status == "errored"
    # a declared member starts none; a detected one has its child cancelled, not
    # stopped by a limit
    assert log == ([] if declared else ["child cancelled"])
    assert (line.background_started, line.background_cancelled) == (
        (0, 0) if declared else (1, 1)
    )


@agent
def sandbox_prober(seen: dict[str, dict[str, str]]) -> Agent:
    """Start a background task that records which sandboxes it reaches."""

    async def execute(state: AgentState) -> AgentState:
        name = _member_name(state)

        async def probe() -> None:
            reach = {"sandbox()": _name(sandbox())}
            for other in ("agent-1", "agent-2"):
                try:
                    reach[other] = _name(sandbox(other))
                except ValueError:
                    reach[other] = "refused"
            seen[name] = reach

        background(probe)
        return state

    return execute


@pytest.mark.anyio
async def test_background_work_runs_in_its_members_sandbox() -> None:
    seen: dict[str, dict[str, str]] = {}
    with environments("default", "agent-1", "agent-2"):
        await run_swarm(
            swarm(
                member(
                    sandbox_prober,
                    args={"seen": seen},
                    name="agent",
                    count=2,
                    sandbox="{member}",
                ),
                channels=[],
            )
        )
    assert seen == {
        "agent-1": {"sandbox()": "agent-1", "agent-1": "agent-1", "agent-2": "refused"},
        "agent-2": {"sandbox()": "agent-2", "agent-1": "refused", "agent-2": "agent-2"},
    }


# ---------------------------------------------------------------------------
# deepagent(background=True) members


# A deepagent member's model, scripted by speaker. The parent makes its tool calls in
# order, then submits. A child is dispatched with the prompt "child <name>"; it makes
# the tool calls of each of its turns (by default one `hold` call with its name), then
# answers. A parent turn listed in `wait_before` waits until those children are in
# their `hold` call.
class _Deep:
    def __init__(
        self,
        parent: list[tuple[str, dict[str, Any]]],
        children: dict[str, list[list[tuple[str, dict[str, Any]]]]] | None = None,
        wait_before: dict[int, list[str]] | None = None,
    ) -> None:
        self.parent = parent
        self.children = children or {}
        self.wait_before = wait_before or {}
        self.entered: dict[str, anyio.Event] = {}

    async def __call__(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        first = next(m.text for m in input if isinstance(m, ChatMessageUser))
        turns = len([m for m in input if m.role == "assistant"])
        child = re.search(r"child (\S+)", first)
        if child is not None:
            name = child.group(1)
            plan = self.children.get(name, [[("hold", {"name": name})]])
            if turns < len(plan):
                return _calls(plan[turns])
            return output(f"{name} done")
        for name in self.wait_before.get(turns, []):
            await self.event(name).wait()
        if turns < len(self.parent):
            return _calls([self.parent[turns]])
        return _calls([("submit", {"answer": "done"})])

    def event(self, name: str) -> anyio.Event:
        return self.entered.setdefault(name, anyio.Event())


def _calls(calls: list[tuple[str, dict[str, Any]]]) -> ModelOutput:
    function, arguments = calls[0]
    result = ModelOutput.for_tool_call("mockllm", function, arguments)
    message = result.message
    assert message.tool_calls is not None
    for i, (function, arguments) in enumerate(calls[1:], start=1):
        message.tool_calls.append(
            ToolCall(
                id=f"{message.tool_calls[0].id}-{i}",
                function=function,
                arguments=arguments,
            )
        )
    result.usage = output().usage
    return result


def _dispatch(name: str) -> tuple[str, dict[str, Any]]:
    return ("agent", {"prompt": f"child {name}", "background": True})


# The tool the children call: it signals that the child is in it, waits for the
# child's `until` (by default sleeps for its duration), writes a file named after the
# child in the sandbox, and logs `<name> finished` or `<name> cancelled`.
def _hold(
    plan: _Deep,
    log: list[str],
    durations: dict[str, float] | None = None,
    until: Callable[[str], Awaitable[None]] | None = None,
) -> Tool:
    @tool(name="hold")
    def hold() -> Tool:
        async def execute(name: str) -> str:
            """Hold for a while.

            Args:
                name: The child's name.
            """
            plan.event(name).set()
            try:
                if until is not None:
                    await until(name)
                else:
                    assert durations is not None
                    await anyio.sleep(durations[name])
            except anyio.get_cancelled_exc_class():
                log.append(f"{name} cancelled")
                raise
            if until is None:
                await sandbox().write_file(f"{name}.txt", name)
            log.append(f"{name} finished")
            return "held"

        return execute

    return hold()


def _deep_member(model: Model, tools: list[Tool], **kwargs: Any) -> Any:
    return member(
        deepagent,
        args={
            "model": model,
            "tools": tools,
            "subagents": [general()],
            "todo_write": False,
            "background": True,
        },
        **kwargs,
    )


@scorer(metrics=[])
def written() -> Scorer:
    """The names of the children's files in the sandbox."""

    async def score(state: TaskState, target: Target) -> Score:
        found: list[str] = []
        for name in ("fast", "slow"):
            try:
                await sandbox().read_file(f"{name}.txt")
                found.append(name)
            except FileNotFoundError:
                pass
        return Score(value=",".join(found))

    return score


# deepagent needs a running sample (its loop checkpoints)
async def _eval(solver: Agent, tmp_path: Path, **task: Any) -> EvalSample:
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=solver, **task),
        model="mockllm/model",
        log_dir=str(tmp_path),
    )
    assert log.samples is not None
    return log.samples[0]


def _sample_ledger(sample: EvalSample) -> SwarmLedger:
    return SwarmLedger.model_validate(sample.store[LEDGER_KEY])


def _record(sample: EvalSample) -> SwarmResultRecord:
    return SwarmResultRecord.model_validate(sample.store[RECORD_KEY])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "grace,finished,cancelled",
    [
        pytest.param(0.4, ["fast"], 1, id="one-past-grace"),
        pytest.param(1.5, ["fast", "slow"], 0, id="both-within-grace"),
        pytest.param(0.0, [], 2, id="no-grace"),
    ],
)
async def test_a_deepagent_members_background_children_are_drained_before_it_ends(
    grace: float, finished: list[str], cancelled: int, tmp_path: Path
) -> None:
    # no native-capable class from another test may still be loaded
    gc.collect()
    # the parent dispatches three children, cancels the third with agent_cancel, and
    # submits while the other two are in their tool calls
    plan = _Deep(
        parent=[
            _dispatch("fast"),
            _dispatch("slow"),
            _dispatch("dropped"),
            ("agent_cancel", {"agent_id": "AGENT-3"}),
        ],
        wait_before={3: ["dropped"], 4: ["fast", "slow"]},
    )
    log: list[str] = []
    hold = _hold(plan, log, {"fast": 0.1, "slow": 1.0, "dropped": 3600})
    sample = await _eval(
        swarm(
            _deep_member(scripted_model(plan, cost=1.0), [hold], name="deep"),  # pyright: ignore[reportArgumentType]
            channels=[],
            budget=Budget(grace=grace),
        ),
        tmp_path,
        sandbox="local",
        scorer=written(),
    )
    [result] = _record(sample).members
    assert result.status == "submitted"
    assert sorted(log) == sorted(
        ["dropped cancelled"]
        + [
            f"{name} finished" if name in finished else f"{name} cancelled"
            for name in ("fast", "slow")
        ]
    )
    # a scorer run after the swarm sees what the drain let finish
    assert sample.scores is not None
    assert sample.scores["written"].value == ",".join(finished)
    # the member ended after its children's spans had
    events = sample.events
    children = {
        e.id for e in events if isinstance(e, SpanBeginEvent) and e.name == "general"
    }
    assert len(children) == 3
    child_ends = [
        i
        for i, e in enumerate(events)
        if isinstance(e, SpanEndEvent) and e.id in children
    ]
    [ended] = [
        i
        for i, e in enumerate(events)
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("kind") == "member_result"
    ]
    assert len(child_ends) == 3 and max(child_ends) < ended
    ledger = _sample_ledger(sample)
    [line] = ledger.members
    # the child stopped with agent_cancel is not counted as cancelled
    assert (line.background_started, line.background_cancelled) == (3, cancelled)
    # the line holds every completed call: the parent's and the children's
    completed = sum(1 for e in events if isinstance(e, ModelEvent) and e.error is None)
    assert line.calls.calls == completed
    assert line.meter.usage.total_tokens == completed * TOKENS_PER_CALL
    assert ledger.total_complete is (cancelled == 0)


@pytest.mark.anyio
@pytest.mark.parametrize("released", ["within_grace", "past_grace"])
async def test_a_cap_reached_in_a_childs_tool_call_stops_the_swarm_while_the_parent_waits(
    released: str, tmp_path: Path
) -> None:
    # the parent waits on two children with agent_wait. One child runs two tool calls
    # in parallel: `nested` reaches the cap through a nested model call (a `limit` tool
    # result), while its `hold` call is held until the swarm's stop has begun, which
    # the controller's cancellation shows. A stop begun only when an error left a
    # child would never come: the held call would hold the child and the parent.
    plan = _Deep(
        parent=[
            _dispatch("capper"),
            _dispatch("other"),
            ("agent_wait", {"agent_ids": ["AGENT-1", "AGENT-2"], "mode": "all"}),
        ],
        children={"capper": [[("nested", {}), ("hold", {"name": "capper"})]]},
    )
    stopping = anyio.Event()
    waiting = anyio.Event()
    log: list[str] = []
    nested_model = scripted_model()

    async def held(name: str) -> None:
        await stopping.wait()
        if released == "past_grace":
            await anyio.sleep_forever()

    @tool(name="nested")
    def nested() -> Tool:
        async def execute() -> str:
            """Ask another model."""
            # once every other call has been made, so this one reaches the cap
            await plan.event("other").wait()
            await waiting.wait()
            # the parent's call that set `waiting` records its usage first
            await anyio.sleep(0.05)
            await nested_model.generate("nested")
            return "answered"

        return execute

    async def parent_model(
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        result = await plan(input, tools, tool_choice, config)
        if (
            result.message.tool_calls
            and result.message.tool_calls[0].function == "agent_wait"
        ):
            waiting.set()
        return result

    async def run_until_stopped(control: SwarmControl) -> str:
        control.start("deep")
        try:
            await anyio.sleep_forever()
        finally:
            stopping.set()
        return "unreachable"

    sample = await _eval(
        swarm(
            _deep_member(
                scripted_model(parent_model),  # pyright: ignore[reportArgumentType]
                [_hold(plan, log, until=held), nested()],
                name="deep",
            ),
            controller=controller(run_until_stopped, f"background_test_cap_{released}"),
            channels=[],
            # the parent's three calls and each child's first call
            budget=Budget(tokens=5 * TOKENS_PER_CALL, grace=0.5),
        ),
        tmp_path,
    )
    ledger = _sample_ledger(sample)
    assert ledger.stop_reason == "swarm_cap"
    [nested_event] = [
        e for e in sample.events if isinstance(e, ToolEvent) and e.function == "nested"
    ]
    assert nested_event.error is not None and nested_event.error.type == "limit"
    [line] = ledger.members
    if released == "within_grace":
        # each child was refused its next call and ended alone; the parent then met
        # the cap
        assert sorted(log) == ["capper finished", "other finished"]
        assert line.status == "swarm_cap"
        assert line.background_cancelled == 0
        assert ledger.calls.errored == 0
    else:
        assert sorted(log) == ["capper cancelled", "other cancelled"]
        assert line.status == "cancelled"
        assert line.background_cancelled == 2
