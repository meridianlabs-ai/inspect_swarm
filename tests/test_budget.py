import logging
import math
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._eval.evalset import task_identifier
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.agent import Agent, AgentState, agent, run
from inspect_ai.agent._bridge.types import AgentBridge
from inspect_ai.agent._bridge.util import (
    bridge_generate,
    bridge_model_generate,
    in_bridge_model_generate,
)
from inspect_ai.dataset import Sample
from inspect_ai.event import SampleLimitEvent, ToolEvent
from inspect_ai.log._transcript import Transcript, _transcript, transcript
from inspect_ai.model import (
    ChatMessage,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelCost,
    ModelInfo,
    ModelOutput,
    ModelUsage,
    execute_tools,
    get_model,
    set_model_info,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, ToolChoice, ToolInfo, tool
from inspect_ai.util import (
    Limit,
    LimitExceededError,
    cost_limit,
    message_limit,
    time_limit,
    token_limit,
    turn_limit,
    working_limit,
)
from inspect_ai.util._concurrency import init_concurrency
from inspect_ai.util._limit import (
    check_cost_limit,
    check_message_limit,
    check_token_limit,
    record_model_cost,
    record_model_usage,
    record_turn,
    suspend_token_limit,
)
from pydantic import ValidationError

from inspect_swarm._budget import (
    Budget,
    Caps,
    CostPair,
    Lever,
    MemberContext,
    NodeRole,
    SwarmMessageNode,
    SwarmTokenNode,
    SwarmTurnNode,
    check_cost_data,
    current_member,
    derive_caps,
    is_bridged,
)

TOKENS_PER_CALL = 50


def _output(content: str = "ok") -> ModelOutput:
    output = ModelOutput.from_content("mockllm", content)
    output.usage = ModelUsage(
        input_tokens=0, output_tokens=TOKENS_PER_CALL, total_tokens=TOKENS_PER_CALL
    )
    return output


# Scripted mockllm outputs that count calls and can hold them in flight.
class _Outputs:
    def __init__(
        self,
        outputs: Sequence[ModelOutput] = (),
        gate: anyio.Event | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.outputs = list(outputs)
        self.gate = gate
        self.on_call = on_call
        self.calls = 0
        self.started = 0

    async def __call__(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        self.started += 1
        if self.on_call is not None:
            self.on_call()
        if self.gate is not None:
            await self.gate.wait()
        # a real provider awaits the network, so a cancelled caller stops here
        await anyio.sleep(0)
        self.calls += 1
        return self.outputs.pop(0) if self.outputs else _output()


# A mockllm model whose calls each use 50 tokens and cost `cost_per_call` dollars.
def _model(
    cost_per_call: float | None = None, outputs: _Outputs | None = None
) -> Model:
    name = f"mockllm/m-{uuid4().hex}"
    if cost_per_call is not None:
        per_million = cost_per_call * 1_000_000 / TOKENS_PER_CALL
        set_model_info(
            name,
            ModelInfo(
                cost=ModelCost(
                    input=0, output=per_million, input_cache_write=0, input_cache_read=0
                )
            ),
        )
    return get_model(name, memoize=False, custom_outputs=outputs or _Outputs())


def _usage(tokens: int, cost: float | None = None) -> ModelUsage:
    return ModelUsage(output_tokens=tokens, total_tokens=tokens, total_cost=cost)


@contextmanager
def _entered(nodes: Sequence[Limit]) -> Generator[None, None, None]:
    with ExitStack() as stack:
        for node in nodes:
            stack.enter_context(node)
        yield


@pytest.fixture(autouse=True)
def fresh_concurrency_registry() -> None:
    # the registry's limiters are bound to the async backend that created them, so a
    # trio test must not reuse one an asyncio test or an eval() left behind
    init_concurrency()


@pytest.fixture
def fresh_transcript() -> Iterator[Transcript]:
    token = _transcript.set(Transcript())
    yield transcript()
    _transcript.reset(token)


# ---------------------------------------------------------------------------
# inspect_ai behaviour the swarm relies on. Each test pins an internal name or a
# behaviour the design cites, so that an inspect_ai change fails here.


@agent
def _generate_forever(model: Model) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        while True:
            state.output = await model.generate(state.messages)
            state.messages.append(state.output.message)

    return execute


@agent
def _generate_once(model: Model) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate(state.messages)
        return state

    return execute


@agent
def _sleeper(seconds: float) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        await anyio.sleep(seconds)
        return state

    return execute


@solver
def _nested_working_limit(limit: float, seconds: float) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        _, error = await run(_sleeper(seconds), "go", limits=[working_limit(limit)])
        state.metadata["error"] = None if error is None else error.type
        return state

    return solve


def test_pinned_nested_working_limit_is_not_enforced(tmp_path: Path) -> None:
    # the sample's monitor polls only the root node, so a nested working limit never fires
    [log] = eval(
        Task(dataset=[Sample(input="x")], solver=_nested_working_limit(0.2, 1.5)),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success"
    assert log.samples is not None
    sample = log.samples[0]
    assert sample.limit is None
    assert sample.metadata["error"] is None


@pytest.mark.anyio
async def test_pinned_turn_limit_pays_for_one_more_generation() -> None:
    outputs = _Outputs()
    _, error = await run(
        _generate_forever(_model(outputs=outputs)), "go", limits=[turn_limit(3)]
    )
    assert error is not None and error.type == "turn"
    assert outputs.calls == 4


@pytest.mark.anyio
async def test_pinned_token_node_carries_cost_that_cost_node_misses() -> None:
    model = _model(cost_per_call=10.0)
    with cost_limit(None) as cost_node, token_limit(10) as token_node:
        with pytest.raises(LimitExceededError) as raised:
            await model.generate("x")
    assert raised.value.source is token_node
    assert token_node._usage.total_cost == pytest.approx(10.0)
    assert cost_node.usage == 0.0


@pytest.mark.anyio
async def test_pinned_child_token_exhaustion_bypasses_cost_limit() -> None:
    outputs = _Outputs()
    model = _model(cost_per_call=10.0, outputs=outputs)
    with cost_limit(15) as cost_node:
        for _ in range(3):
            _, error = await run(_generate_once(model), "go", limits=[token_limit(10)])
            assert error is not None and error.type == "token"
    assert outputs.calls == 3
    assert cost_node.usage == 0.0


@agent
def _generate_in_task_group(model: Model) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        async with anyio.create_task_group() as tg:
            tg.start_soon(model.generate, "x")
        return state

    return execute


@pytest.mark.anyio
async def test_pinned_grouped_owned_limit_error_escapes_run() -> None:
    owned = token_limit(0)
    with pytest.raises(ExceptionGroup) as raised:
        await run(_generate_in_task_group(_model()), "go", limits=[owned])
    [leaf] = raised.value.exceptions
    assert isinstance(leaf, LimitExceededError) and leaf.source is owned


def test_pinned_limit_cannot_be_entered_twice() -> None:
    limit = token_limit(10)
    with limit:
        pass
    with pytest.raises(RuntimeError):
        with limit:
            pass


# Bridge compaction that makes a generation, as summary compaction does.
class _GenerateCompaction:
    def __init__(self, model: Model, seen: list[tuple[str, bool]]) -> None:
        self.model = model
        self.seen = seen

    async def compact_input(
        self, messages: list[ChatMessage], force: bool = False
    ) -> tuple[list[ChatMessage], ChatMessageUser | None]:
        self.seen.append(("compaction", in_bridge_model_generate()))
        await self.model.generate("summarise")
        return messages, None

    async def record_output(
        self, input: list[ChatMessage], output: ModelOutput
    ) -> None:
        pass


class _CompactingBridge(AgentBridge):
    def __init__(self, compaction: _GenerateCompaction) -> None:
        super().__init__(AgentState(messages=[]))
        self._test_compaction = compaction

    def compaction(self, tools: Sequence[ToolInfo | Tool], model: Model) -> Any:
        return self._test_compaction


async def _bridge_generate(model: Model, compaction: _GenerateCompaction) -> None:
    await bridge_generate(
        _CompactingBridge(compaction),
        model,
        [ChatMessageUser(content="hello")],
        [],
        None,
        GenerateConfig(),
    )


@pytest.mark.anyio
async def test_pinned_bridge_compaction_runs_before_the_generate_mark() -> None:
    seen: list[tuple[str, bool]] = []
    outputs = _Outputs(
        on_call=lambda: seen.append(("generate", in_bridge_model_generate()))
    )
    model = _model(outputs=outputs)
    await _bridge_generate(model, _GenerateCompaction(model, seen))
    assert seen == [("compaction", False), ("generate", False), ("generate", True)]


@tool
def ask_model(model: Model, calls: int) -> Tool:
    async def execute() -> str:
        """Ask the model a question."""
        for _ in range(calls):
            await model.generate("question")
        return "answer"

    return execute


# the tool loop react() runs, without its sample-only machinery
@agent
def _tool_loop(model: Model, tools: list[Tool]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        while True:
            state.output = await model.generate(state.messages, tools)
            state.messages.append(state.output.message)
            if not state.output.message.tool_calls:
                return state
            result = await execute_tools(state.messages, tools)
            state.messages.extend(result.messages)

    return execute


@pytest.mark.anyio
async def test_pinned_limit_in_tool_becomes_tool_error_then_raises_at_next_generate(
    fresh_transcript: Transcript,
) -> None:
    member_outputs = _Outputs([ModelOutput.for_tool_call("mockllm", "ask_model", {})])
    member_outputs.outputs[0].usage = _usage(TOKENS_PER_CALL)
    tool_outputs = _Outputs()
    member = _tool_loop(
        _model(outputs=member_outputs),
        [ask_model(_model(outputs=tool_outputs), calls=3)],
    )
    with token_limit(120) as node:
        with pytest.raises(LimitExceededError) as raised:
            await member(AgentState(messages=[ChatMessageUser(content="go")]))
    assert raised.value.source is node
    [tool_event] = [e for e in fresh_transcript.events if isinstance(e, ToolEvent)]
    assert tool_event.error is not None and tool_event.error.type == "limit"
    # one member call, two tool calls (the second crosses the limit), then a refusal
    assert (member_outputs.calls, tool_outputs.calls) == (1, 2)
    assert node.usage == 3 * TOKENS_PER_CALL


@pytest.mark.anyio
async def test_pinned_lowered_limit_lets_calls_in_flight_finish_and_record() -> None:
    gate = anyio.Event()
    outputs = _Outputs(gate=gate)
    model = _model(outputs=outputs)
    errors: list[LimitExceededError] = []

    async def caller() -> None:
        try:
            while True:
                await model.generate("x")
        except LimitExceededError as ex:
            errors.append(ex)

    with token_limit(None) as node:
        async with anyio.create_task_group() as tg:
            for _ in range(3):
                tg.start_soon(caller)
            while outputs.started < 3:
                await anyio.sleep(0)
            node.limit = int(node.usage)
            gate.set()
    # nothing was cancelled: each call in flight completed and was recorded, then its
    # generate() raised at the check after recording, and no caller started another
    assert (outputs.started, outputs.calls) == (3, 3)
    assert node.usage == 3 * TOKENS_PER_CALL
    assert [e.source for e in errors] == [node] * 3


# ---------------------------------------------------------------------------
# Budget: validation and logging


@pytest.mark.parametrize(
    "kwargs, field",
    [
        ({"cost": 0}, "cost"),
        ({"cost": -1.0}, "cost"),
        ({"cost": math.nan}, "cost"),
        ({"cost": math.inf}, "cost"),
        ({"time": 0}, "time"),
        ({"time": math.inf}, "time"),
        ({"tokens": 0}, "tokens"),
        ({"tokens": -5}, "tokens"),
        ({"tokens": "0k"}, "tokens"),
        ({"tokens": "lots"}, "tokens"),
        ({"tokens": "bogus:10"}, "tokens"),
        ({"reserve": -0.1}, "reserve"),
        ({"reserve": 1.0}, "reserve"),
        ({"reserve": math.nan}, "reserve"),
        ({"grace": -1.0}, "grace"),
        ({"grace": math.inf}, "grace"),
        ({"split": ()}, "split"),
        ({"split": (1.0, 0.0)}, "split"),
        ({"split": (1.0, -2.0)}, "split"),
        ({"split": (1.0, math.nan)}, "split"),
        ({"split": (math.inf,)}, "split"),
        ({"split": "weighted"}, "split"),
        ({"cap": 10.0}, "cap"),
    ],
)
def test_budget_rejects_invalid_fields(kwargs: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Budget(**kwargs)


def test_budget_rejects_token_limit_objects() -> None:
    # a TokenLimit is logged as its class name, so the cap is an int or a string
    from inspect_ai.util import TokenLimit

    with pytest.raises(ValidationError, match="tokens"):
        Budget(tokens=TokenLimit(tokens=10))  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "budget",
    [
        Budget(),
        Budget(cost=10.0, tokens="output:1m", time=60.0, reserve=0.0, grace=0.0),
        Budget(tokens=500, split="equal"),
        Budget(cost=10.0, split=(1, 2)),
    ],
)
def test_budget_accepts_valid_fields(budget: Budget) -> None:
    assert Budget.model_validate(budget.model_dump()) == budget


@solver
def _arm(budget: Budget) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        return state

    return solve


def test_budget_is_logged_as_its_fields(tmp_path: Path) -> None:
    budgets = [
        Budget(cost=10.0, split=(1, 2)),
        Budget(cost=10.0, split=(2, 1)),
        Budget(cost=10.0, split=(1, 2), reserve=0.1),
    ]
    logs = eval(
        [
            Task(dataset=[Sample(input="x")], solver=_arm(budget=b), name="arm")
            for b in budgets
        ],
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    logged: list[Budget] = []
    for log in logs:
        params = log.plan.steps[0].params_passed["budget"]
        assert isinstance(params, dict)
        logged.append(Budget.model_validate(params))
    assert logged == budgets
    assert len({task_identifier(log, None) for log in logs}) == len(budgets)


# ---------------------------------------------------------------------------
# Deriving the caps at swarm start


# The six root limit nodes a running sample has.
@contextmanager
def _sample(
    *,
    tokens: int | None = None,
    token_type: str = "all",
    cost: float | None = None,
    time: float | None = None,
    working: float | None = None,
    used_tokens: int = 0,
    used_cost: float = 0.0,
) -> Generator[None, None, None]:
    cost_node = cost_limit(cost)
    with (
        token_limit(tokens, type=token_type) as token_node,
        cost_node,
        message_limit(None),
        turn_limit(None),
        working_limit(working),
        time_limit(time),
    ):
        token_node.record(_usage(used_tokens))
        cost_node.record(used_cost)
        yield


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sample, budget, expected",
    [
        ({}, Budget(), Caps(cost=None, tokens=None, token_type="all", time=None)),
        (
            {"cost": 10.0, "used_cost": 2.0},
            Budget(reserve=0.5),
            Caps(cost=3.0, tokens=None, token_type="all", time=None),
        ),
        (
            {"tokens": 1001},
            Budget(),
            Caps(cost=None, tokens=950, token_type="all", time=None),
        ),
        (
            {"tokens": 1000, "token_type": "output", "used_tokens": 100},
            Budget(reserve=0.5),
            Caps(cost=None, tokens=400, token_type="output", time=None),
        ),
        (
            {"cost": 10.0, "tokens": 1000, "time": 600.0},
            Budget(cost=3.0, tokens="output:500", time=10.0),
            Caps(cost=3.0, tokens=500, token_type="output", time=10.0),
        ),
    ],
)
async def test_derive_caps(
    sample: dict[str, Any], budget: Budget, expected: Caps
) -> None:
    with _sample(**sample):
        assert derive_caps(budget, member_count=2) == expected


@pytest.mark.anyio
async def test_derive_time_cap_from_a_short_sample_time_limit() -> None:
    with _sample(time=60.0):
        caps = derive_caps(Budget(), member_count=1)
    assert caps.time is not None and caps.time == pytest.approx(57.0, abs=0.5)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sample, budget, match",
    [
        ({"cost": 10.0, "used_cost": 9.6}, Budget(), "cost cap.*already used 9.6"),
        ({"tokens": 100, "used_tokens": 100}, Budget(), "token cap"),
        ({"tokens": 10}, Budget(reserve=0.95), "token cap"),
    ],
)
async def test_derive_caps_rejects_a_cap_that_is_not_positive(
    sample: dict[str, Any], budget: Budget, match: str
) -> None:
    with _sample(**sample), pytest.raises(ValueError, match=match):
        derive_caps(budget, member_count=1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sample, budget, warning",
    [
        ({"cost": 10.0}, Budget(cost=20.0), "cost cap"),
        ({"time": 60.0}, Budget(time=100.0), "time cap"),
        (
            {"tokens": 1000, "token_type": "output"},
            Budget(tokens="output:2000"),
            "token cap",
        ),
        ({"tokens": 1000}, Budget(tokens="output:2000"), None),
        ({"cost": 10.0}, Budget(cost=5.0), None),
        ({"working": 100.0}, Budget(), "working limit"),
    ],
)
async def test_derive_caps_warnings(
    caplog: pytest.LogCaptureFixture,
    sample: dict[str, Any],
    budget: Budget,
    warning: str | None,
) -> None:
    with caplog.at_level(logging.WARNING, logger="inspect_swarm"), _sample(**sample):
        derive_caps(budget, member_count=1)
    messages = [r.getMessage() for r in caplog.records]
    if warning is None:
        assert messages == []
    else:
        [message] = messages
        assert warning in message


@pytest.mark.anyio
async def test_derive_caps_checks_weight_count_against_roster() -> None:
    with _sample():
        derive_caps(Budget(split=(1, 2)), member_count=2)
        with pytest.raises(ValueError, match="2 weights but the swarm has 3 members"):
            derive_caps(Budget(split=(1, 2)), member_count=3)


def test_check_cost_data() -> None:
    priced = _model(cost_per_call=1.0)
    unpriced = _model()
    check_cost_data([priced, str(priced)])
    with pytest.raises(PrerequisiteError, match=str(unpriced)) as raised:
        check_cost_data([priced, unpriced, unpriced])
    assert str(priced) not in str(raised.value)


# ---------------------------------------------------------------------------
# Swarm nodes


@dataclass
class _NodeCase:
    id: str
    # builds the nodes to enter and the one expected to trip
    nodes: Callable[[], tuple[list[Limit], Limit]]
    trigger: Callable[[], None]
    type: str
    message: str
    stop_cause: str
    stops_swarm: bool


def _pair(
    limit: float, owner: str | None, role: NodeRole, half: str
) -> Callable[[], tuple[list[Limit], Limit]]:
    def build() -> tuple[list[Limit], Limit]:
        pair = CostPair(limit, owner=owner, role=role)
        return pair.nodes, pair.token_node if half == "token" else pair.cost_node

    return build


def _single(node: Callable[[], Limit]) -> Callable[[], tuple[list[Limit], Limit]]:
    def build() -> tuple[list[Limit], Limit]:
        n = node()
        return [n], n

    return build


def _closed_lever(half: str) -> Callable[[], tuple[list[Limit], Limit]]:
    def build() -> tuple[list[Limit], Limit]:
        lever = Lever(owner="a")
        lever.close("verified answer")
        return lever.nodes, lever.token_node if half == "token" else lever.cost_node

    return build


def _record_and_check(tokens: int, cost: float | None = None) -> Callable[[], None]:
    def trigger() -> None:
        record_model_usage(_usage(tokens, cost))
        check_token_limit()

    return trigger


def _suspended_cost(cost: float) -> None:
    with suspend_token_limit():
        record_model_cost(cost)
        check_cost_limit()


def _pre_dispatch() -> None:
    check_token_limit(raise_for_equal=True)


def _suspended_pre_dispatch() -> None:
    with suspend_token_limit():
        check_token_limit(raise_for_equal=True)
        check_cost_limit(raise_for_equal=True)


def _two_turns() -> None:
    record_turn()
    record_turn()


NODE_CASES = [
    _NodeCase(
        "cost cap, token tree",
        _pair(1.0, None, "cap", "token"),
        _record_and_check(10, 1.5),
        "cost",
        "Swarm cost cap reached. value: $1.5000; limit: $1.0000",
        "swarm_cap",
        True,
    ),
    _NodeCase(
        "cost cap, cost tree while suspended",
        _pair(1.0, None, "cap", "cost"),
        lambda: _suspended_cost(1.5),
        "cost",
        "Swarm cost cap reached. value: $1.5000; limit: $1.0000",
        "swarm_cap",
        True,
    ),
    _NodeCase(
        "token cap",
        _single(lambda: SwarmTokenNode(100, owner=None, role="cap")),
        _record_and_check(150),
        "token",
        "Swarm token cap reached. value: 150; limit: 100",
        "swarm_cap",
        True,
    ),
    _NodeCase(
        "output token share",
        _single(lambda: SwarmTokenNode(100, "output", owner="a", role="share")),
        _record_and_check(150),
        "token",
        "Swarm member 'a' token (output) share reached. value: 150; limit: 100",
        "share",
        False,
    ),
    _NodeCase(
        "member cost limit",
        _pair(1.0, "a", "limit", "token"),
        _record_and_check(10, 1.5),
        "cost",
        "Swarm member 'a' cost limit reached. value: $1.5000; limit: $1.0000",
        "member_limit:cost",
        False,
    ),
    _NodeCase(
        "member turn limit",
        _single(lambda: SwarmTurnNode(1, owner="a")),
        _two_turns,
        "turn",
        "Swarm member 'a' turn limit reached. value: 2; limit: 1",
        "member_limit:turn",
        False,
    ),
    _NodeCase(
        "member message limit",
        _single(lambda: SwarmMessageNode(3, owner="a")),
        lambda: check_message_limit(3, raise_for_equal=True),
        "message",
        "Swarm member 'a' message limit reached. count: 3; limit: 3",
        "member_limit:message",
        False,
    ),
    _NodeCase(
        "closed lever",
        _closed_lever("token"),
        _pre_dispatch,
        "custom",
        "Stopped by the swarm: verified answer",
        "stopped",
        False,
    ),
    _NodeCase(
        "closed lever's stop gate while suspended",
        _closed_lever("cost"),
        _suspended_pre_dispatch,
        "custom",
        "Stopped by the swarm: verified answer",
        "stopped",
        False,
    ),
]


@pytest.mark.parametrize("case", NODE_CASES, ids=lambda c: c.id)
def test_node_raises_its_own_error_without_a_sample_limit_event(
    case: _NodeCase, fresh_transcript: Transcript
) -> None:
    nodes, expected = case.nodes()
    with _entered(nodes), pytest.raises(LimitExceededError) as raised:
        case.trigger()
    assert raised.value.source is expected
    assert raised.value.type == case.type
    assert raised.value.message == case.message
    assert not any(isinstance(e, SampleLimitEvent) for e in fresh_transcript.events)


@pytest.mark.anyio
@pytest.mark.parametrize("declared", [True, False], ids=["declared", "marked"])
@pytest.mark.parametrize("case", NODE_CASES, ids=lambda c: c.id)
async def test_node_cancels_a_bridged_member_instead_of_raising(
    case: _NodeCase, declared: bool
) -> None:
    stops: list[str] = []
    member = MemberContext("a", bridged=declared, stop_swarm=stops.append)
    token = current_member.set(member)
    try:
        nodes, _ = case.nodes()
        with member.scope:
            with _entered(nodes):
                if declared:
                    case.trigger()
                else:
                    with bridge_model_generate():
                        case.trigger()
            await anyio.sleep(0)
    finally:
        current_member.reset(token)
    assert member.scope.cancelled_caught
    assert member.stop_cause == case.stop_cause
    # stop() is idempotent, and every later check of a reached cap calls it again
    assert set(stops) == ({"swarm_cap"} if case.stops_swarm else set())
    assert member.bridged
    assert member.bridged_late is not declared


@pytest.mark.anyio
@pytest.mark.parametrize("bridged", [True, False])
async def test_bridge_compaction_reaching_a_cap(bridged: bool) -> None:
    # bridge compaction generates before the bridge's mark, so only a member declared
    # bridged is cancelled there; a native member's error would end a sandboxed sample
    stops: list[str] = []
    member = MemberContext("a", bridged=bridged, stop_swarm=stops.append)
    model = _model()
    cap = SwarmTokenNode(0, owner=None, role="cap")
    token = current_member.set(member)
    try:
        with member.scope, cap:
            if bridged:
                await _bridge_generate(model, _GenerateCompaction(model, []))
            else:
                with pytest.raises(LimitExceededError) as raised:
                    await _bridge_generate(model, _GenerateCompaction(model, []))
                assert raised.value.source is cap
    finally:
        current_member.reset(token)
    assert member.scope.cancelled_caught is bridged
    assert set(stops) == ({"swarm_cap"} if bridged else set())
    assert member.stop_cause == ("swarm_cap" if bridged else None)


@dataclass
class _BridgeCase:
    id: str
    # builds the nodes to enter and the cap, if any, a second request trips
    nodes: Callable[[], tuple[list[Limit], Limit | None]]
    cost_per_call: float | None
    suspended: bool


def _lever_only() -> tuple[list[Limit], Limit | None]:
    return Lever(owner="a").nodes, None


def _token_cap_100() -> tuple[list[Limit], Limit | None]:
    lever, cap = Lever(owner="a"), SwarmTokenNode(100, owner=None, role="cap")
    return [cap, *lever.nodes], cap


def _cost_cap_2() -> tuple[list[Limit], Limit | None]:
    lever, cap = Lever(owner="a"), CostPair(2.0, owner=None, role="cap")
    return [*cap.nodes, *lever.nodes], cap.token_node


BRIDGE_CASES = [
    _BridgeCase("uncapped lever", _lever_only, None, False),
    _BridgeCase("uncapped lever, suspended", _lever_only, 1.0, True),
    _BridgeCase("token cap", _token_cap_100, None, False),
    _BridgeCase("cost cap", _cost_cap_2, 1.0, False),
    _BridgeCase("cost cap, suspended", _cost_cap_2, 1.0, True),
]


@pytest.mark.anyio
@pytest.mark.parametrize("case", BRIDGE_CASES, ids=lambda c: c.id)
async def test_member_found_bridged_by_a_successful_marked_request(
    case: _BridgeCase,
) -> None:
    # the first request (compaction, then the marked generation) stays within the caps
    # and marks the member bridged; the next request's compaction, before the mark,
    # reaches the cap and must cancel the member rather than raise
    stops: list[str] = []
    member = MemberContext("a", bridged=False, stop_swarm=stops.append)
    model = _model(cost_per_call=case.cost_per_call)
    nodes, cap = case.nodes()
    token = current_member.set(member)
    try:
        with member.scope, _entered(nodes):
            with ExitStack() as stack:
                if case.suspended:
                    stack.enter_context(suspend_token_limit())
                await _bridge_generate(model, _GenerateCompaction(model, []))
            assert (member.bridged, member.bridged_late) == (True, True)
            assert not member.scope.cancel_called
            if cap is not None:
                await _bridge_generate(model, _GenerateCompaction(model, []))
    finally:
        current_member.reset(token)
    assert member.scope.cancelled_caught is (cap is not None)
    assert set(stops) == ({"swarm_cap"} if cap is not None else set())
    assert member.stop_cause == ("swarm_cap" if cap is not None else None)


@pytest.mark.anyio
async def test_cost_pair_stops_sequential_child_token_exhaustion() -> None:
    outputs = _Outputs()
    model = _model(cost_per_call=10.0, outputs=outputs)
    cap = CostPair(15.0, owner=None, role="cap")
    with _entered(cap.nodes):
        _, error = await run(_generate_once(model), "go", limits=[token_limit(10)])
        assert error is not None and error.type == "token"
        with pytest.raises(LimitExceededError) as raised:
            await run(_generate_once(model), "go", limits=[token_limit(10)])
    assert raised.value.source is cap.token_node
    assert raised.value.type == "cost"
    assert outputs.calls == 2
    assert cap.cost == pytest.approx(20.0)


@pytest.mark.anyio
async def test_cost_pairs_count_each_call_once() -> None:
    priced = _model(cost_per_call=10.0)
    unpriced = _model()
    meter = CostPair(owner=None, role="meter")
    cap = CostPair(100.0, owner=None, role="cap")
    lever = Lever(owner="a")
    with _entered([*meter.nodes, *cap.nodes, *lever.nodes]):
        await priced.generate("x")
        await unpriced.generate("x")
        # an approval call: suspended from token metering, counted by the cost tree
        with suspend_token_limit():
            await priced.generate("x")
    for pair in (meter, cap, lever):
        assert pair.cost == pytest.approx(20.0)
        assert pair.suspended_cost == pytest.approx(10.0)
        assert pair.token_node.unpriced_calls == 1
        assert pair.token_node.model_usage.total_tokens == 2 * TOKENS_PER_CALL


@agent(name="inspect_swe/claude_code")
def _claude_code() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


@agent
def _native() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


async def _unregistered(state: AgentState) -> AgentState:
    return state


@pytest.mark.parametrize(
    "member, declared, expected",
    [
        (_claude_code(), None, True),
        (_native(), None, False),
        (_unregistered, None, False),
        (_native(), True, True),
        (_claude_code(), False, False),
    ],
)
def test_is_bridged(member: Agent, declared: bool | None, expected: bool) -> None:
    assert is_bridged(member, declared) is expected
