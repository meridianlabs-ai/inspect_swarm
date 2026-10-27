import gc
import logging
import math
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import uuid4

import anyio
import httpx
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._eval.evalset import task_identifier
from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.registry import RegistryInfo, set_registry_info
from inspect_ai.agent import Agent, AgentState, agent, run
from inspect_ai.agent._bridge.types import AgentBridge
from inspect_ai.agent._bridge.util import (
    bridge_generate,
    bridge_model_generate,
    in_bridge_model_generate,
)
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent, ModelEvent, SampleLimitEvent, ToolEvent
from inspect_ai.log._samples import (
    _sample_active,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.log._transcript import Transcript, _transcript, transcript
from inspect_ai.model import (
    ChatMessage,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelCall,
    ModelCost,
    ModelInfo,
    ModelOutput,
    ModelUsage,
    ServedModelUsage,
    execute_tools,
    get_model,
    set_model_info,
)
from inspect_ai.model._providers.mockllm import (  # pyright: ignore[reportMissingTypeStubs]
    MockLLM,
)
from inspect_ai.model._providers.util.hooks import (  # pyright: ignore[reportMissingTypeStubs]
    HttpxHooks,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, ToolChoice, ToolInfo, tool
from inspect_ai.util import (
    Limit,
    LimitExceededError,
    cost_limit,
    message_limit,
    span_id_provider,
    store,
    time_limit,
    token_limit,
    turn_limit,
    working_limit,
)
from inspect_ai.util._checkpoint.checkpointer import ResumeCheckpoint
from inspect_ai.util._checkpoint.checkpointer_impl import (
    _CheckpointerSetup,  # pyright: ignore[reportPrivateUsage]
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
from tenacity import wait_none
from tenacity.wait import WaitBaseT

from inspect_swarm import (
    Controller,
    StopReason,
    SwarmControl,
    member,
    swarm,
)
from inspect_swarm._budget import (
    Budget,
    Caps,
    CostPair,
    Lever,
    MemberContext,
    NodeRole,
    SwarmCostNode,
    SwarmMessageNode,
    SwarmNode,
    SwarmTokenNode,
    SwarmTurnNode,
    check_cost_data,
    current_member,
    derive_caps,
    is_bridged,
    share_nodes,
)
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._ledger import Calls, SwarmLedger, sample_resumed

from .swarm_runtime import Script, controller, generator, sample, scripted_model

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
        requested_model=None,
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
# Splits


def _shares(
    budget: Budget, caps: Caps, members: int
) -> list[tuple[float | None, int | None]]:
    # each member's (cost share, token share), in roster order
    shares = share_nodes(budget, caps, [f"m{i}" for i in range(members)])
    result: list[tuple[float | None, int | None]] = []
    for nodes in shares.values():
        cost = [n.limit for n in nodes if isinstance(n, SwarmCostNode)]
        # a cost pair's token-tree node has no token limit
        tokens = [n.limit for n in nodes if isinstance(n, SwarmTokenNode) and n.limit]
        result.append((cost[0] if cost else None, tokens[0] if tokens else None))
    return result


@pytest.mark.parametrize(
    "split, cost, tokens, expected",
    [
        ("equal", 9.0, None, [(3.0, None)] * 3),
        ((1, 2, 3), 12.0, None, [(2.0, None), (4.0, None), (6.0, None)]),
        ("equal", None, 300, [(None, 100)] * 3),
        # the remainder goes to the largest fractional parts: 42.86 and 28.57
        ((1, 2, 3, 1), None, 100, [(None, 14), (None, 29), (None, 43), (None, 14)]),
        # equal fractions: by roster order
        ("equal", None, 10, [(None, 4), (None, 3), (None, 3)]),
        ((0.1, 0.2), 3.0, 7, [(1.0, 2), (2.0, 5)]),
        # large weights: the sum and cap * weight would overflow a float
        ((1e308, 1e308), 10.0, 10, [(5.0, 5), (5.0, 5)]),
        ((1e308, 1e307), 11.0, None, [(10.0, None), (1.0, None)]),
        # a share far below the cap is kept, not rounded to zero on the way
        ((1e308, 1e-16), 1e308, None, [(1e308, None), (1e-16, None)]),
    ],
)
def test_shares_divide_the_caps_by_weight(
    split: Literal["equal"] | tuple[float, ...],
    cost: float | None,
    tokens: int | None,
    expected: list[tuple[float | None, int | None]],
) -> None:
    caps = Caps(cost=cost, tokens=tokens, token_type="all", time=None)
    shares = _shares(Budget(split=split), caps, len(expected))
    assert shares == [
        (pytest.approx(c) if c is not None else None, t) for c, t in expected
    ]
    if tokens is not None:
        assert sum(t for _, t in shares if t is not None) == tokens


def test_shares_keep_the_token_caps_metering_type() -> None:
    caps = Caps(cost=None, tokens=100, token_type="output", time=None)
    shares = share_nodes(Budget(split="equal"), caps, ["a", "b"])
    for name, [node] in shares.items():
        assert isinstance(node, SwarmTokenNode)
        assert (node.owner, node.role, node.type) == (name, "share", "output")


def test_a_pool_has_no_shares() -> None:
    caps = Caps(cost=10.0, tokens=100, token_type="all", time=None)
    assert share_nodes(Budget(), caps, ["a", "b"]) == {}


@pytest.mark.parametrize(
    "split, caps, match",
    [
        ("equal", Caps(None, None, "all", 60.0), "neither"),
        ("equal", Caps(None, 2, "all", None), "token cap of 2 .* 3 members"),
        # weights can give a member a zero share of a cap larger than the roster
        (
            (1.0, 1.0, 100.0),
            Caps(None, 50, "all", None),
            "token cap of 50 .* 3 members",
        ),
        ((1e308, 1e308, 5e-324), Caps(10.0, None, "all", None), "cost share of zero"),
    ],
)
def test_share_errors(
    split: Literal["equal"] | tuple[float, ...], caps: Caps, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        share_nodes(Budget(split=split), caps, ["a", "b", "c"])


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


def _stop_with(nodes: list[Limit], stops: list[str]) -> None:
    # as the runtime does: the swarm's caps stop it when they are reached
    for node in nodes:
        if isinstance(node, SwarmNode) and node.role == "cap":
            node.stop_swarm = stops.append


@pytest.mark.anyio
@pytest.mark.parametrize("declared", [True, False], ids=["declared", "marked"])
@pytest.mark.parametrize("case", NODE_CASES, ids=lambda c: c.id)
async def test_node_cancels_a_bridged_member_instead_of_raising(
    case: _NodeCase, declared: bool
) -> None:
    stops: list[str] = []
    member = MemberContext("a", bridged=declared)
    token = current_member.set(member)
    try:
        nodes, _ = case.nodes()
        _stop_with(nodes, stops)
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


@pytest.mark.anyio
@pytest.mark.parametrize("bridged", [True, False])
async def test_bridge_compaction_reaching_a_cap(bridged: bool) -> None:
    # bridge compaction generates before the bridge's mark, so only a member declared
    # bridged is cancelled there; a native member's error would end a sandboxed sample
    stops: list[str] = []
    member = MemberContext("a", bridged=bridged)
    model = _model()
    cap = SwarmTokenNode(0, owner=None, role="cap")
    _stop_with([cap], stops)
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
    # the cap begins the swarm's stop whether it cancels the member or raises
    assert set(stops) == {"swarm_cap"}
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
    member = MemberContext("a", bridged=False)
    model = _model(cost_per_call=case.cost_per_call)
    nodes, cap = case.nodes()
    _stop_with(nodes, stops)
    token = current_member.set(member)
    try:
        with member.scope, _entered(nodes):
            with ExitStack() as stack:
                if case.suspended:
                    stack.enter_context(suspend_token_limit())
                await _bridge_generate(model, _GenerateCompaction(model, []))
            assert member.bridged
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


# ---------------------------------------------------------------------------
# The ledger


def _price(name: str, cost: float) -> None:
    # each call uses TOKENS_PER_CALL output tokens and costs `cost` dollars
    set_model_info(
        name,
        ModelInfo(
            cost=ModelCost(
                input=0,
                output=cost * 1_000_000 / TOKENS_PER_CALL,
                input_cache_write=0,
                input_cache_read=0,
            )
        ),
    )


def _api_model(api: type[MockLLM], script: Script, cost: float | None) -> Model:
    # a model on a test provider class, which lives only as long as the model
    set_registry_info(api, RegistryInfo(type="modelapi", name="mockllm"))
    name = f"m-{uuid4().hex}"
    if cost is not None:
        _price(f"mockllm/{name}", cost)
    return Model(
        api(name, custom_outputs=cast(Any, script)), GenerateConfig(max_retries=2)
    )


def _native_compaction_model(cost: float) -> Model:
    class NativeCompaction(MockLLM):
        async def compact(
            self,
            input: list[ChatMessage],
            tools: list[ToolInfo],
            config: GenerateConfig,
            instructions: str | None = None,
        ) -> tuple[list[ChatMessage], ModelUsage | None]:
            usage = ModelUsage(
                output_tokens=TOKENS_PER_CALL, total_tokens=TOKENS_PER_CALL
            )
            return input, usage

    return _api_model(NativeCompaction, Script(), cost)


def _no_usage_model(cost: float) -> Model:
    class NoUsage(MockLLM):
        async def generate(
            self,
            input: list[ChatMessage],
            tools: list[ToolInfo],
            tool_choice: ToolChoice,
            config: GenerateConfig,
        ) -> ModelOutput:
            return ModelOutput.from_content("mockllm", "ok")

    return _api_model(NoUsage, Script(), cost)


def _retrying_model(script: Script, cost: float) -> Model:
    class Retrying(MockLLM):
        def should_retry(self, ex: Exception) -> bool:
            return True

        def retry_wait(self) -> WaitBaseT | None:
            return wait_none()

    return _api_model(Retrying, script, cost)


def _client_retrying_model(calls: Sequence[list[int]], cost: float) -> Model:
    # each call sends the next list's HTTP statuses through Inspect's HTTP hooks, as a
    # provider's client retries inside one call, and fails unless the last is 200;
    # Inspect retries a failed call
    pending = list(calls)

    class ClientRetrying(MockLLM):
        async def generate(
            self,
            input: list[ChatMessage],
            tools: list[ToolInfo],
            tool_choice: ToolChoice,
            config: GenerateConfig,
        ) -> ModelOutput | tuple[ModelOutput, ModelCall]:
            statuses = pending.pop(0)
            responses = iter(statuses)
            transport = httpx.MockTransport(lambda _: httpx.Response(next(responses)))
            async with httpx.AsyncClient(transport=transport) as client:
                hooks = HttpxHooks(client, api=self)
                status = 0
                with hooks.request() as request_id:
                    for _ in statuses:
                        response = await client.post(
                            "https://provider.test/generate",
                            headers={HttpxHooks.REQUEST_ID_HEADER: request_id},
                        )
                        status = response.status_code
            if status != 200:
                raise RuntimeError("provider: retries exhausted")
            return await super().generate(input, tools, tool_choice, config)

        def should_retry(self, ex: Exception) -> bool:
            return True

        def retry_wait(self) -> WaitBaseT | None:
            return wait_none()

    return _api_model(ClientRetrying, Script(), cost)


def _rated_model(
    outputs: Sequence[ModelOutput], *, input_call: float, output_call: float
) -> Model:
    # priced per TOKENS_PER_CALL input tokens and per TOKENS_PER_CALL output tokens
    name = f"mockllm/m-{uuid4().hex}"
    set_model_info(
        name,
        ModelInfo(
            cost=ModelCost(
                input=input_call * 1_000_000 / TOKENS_PER_CALL,
                output=output_call * 1_000_000 / TOKENS_PER_CALL,
                input_cache_write=0,
                input_cache_read=0,
            )
        ),
    )
    return get_model(name, memoize=False, custom_outputs=Script(outputs))


def _usage_output(
    *, input: int = 0, output: int = 0, model: str = "mockllm"
) -> ModelOutput:
    result = ModelOutput.from_content(model, "ok")
    result.usage = ModelUsage(
        input_tokens=input, output_tokens=output, total_tokens=input + output
    )
    return result


def _router_model(outputs: Sequence[ModelOutput]) -> Model:
    # an unpriced model whose provider reports which model served each call
    class Router(MockLLM):
        def served_model_usage(
            self, output: ModelOutput
        ) -> list[ServedModelUsage] | None:
            if output.usage is None:
                return None
            return [ServedModelUsage(output.model, output.usage)]

    return _api_model(Router, Script(outputs), None)


@agent
def _approving(
    model: Model, approver: Model | None = None, approvals: int = 1
) -> Agent:
    # generates, then makes approval calls, which run with token metering suspended
    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate(state.messages)
        with suspend_token_limit():
            for _ in range(approvals):
                await (approver or model).generate("approve?")
        return state

    return execute


@agent
def _cached(model: Model) -> Agent:
    # the second call is a cache replay of the first
    async def execute(state: AgentState) -> AgentState:
        for _ in range(2):
            state.output = await model.generate("same", cache=True)
        return state

    return execute


@agent
def _compacting(model: Model) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate(state.messages)
        await model.compact(state.messages, [])
        return state

    return execute


@agent
def _recording(model: Model, cost: float) -> Agent:
    # records usage in the limit tree that the sample's usage never sees
    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate(state.messages)
        record_model_usage(
            ModelUsage(
                output_tokens=TOKENS_PER_CALL,
                total_tokens=TOKENS_PER_CALL,
                total_cost=cost,
            )
        )
        return state

    return execute


@agent
def _failing() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        raise RuntimeError("member crashed")

    return execute


def _written_ledger(transcript: Transcript) -> SwarmLedger:
    [data] = [
        e.data
        for e in transcript.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("type") == "ledger"
    ]
    assert isinstance(data, dict)
    record = {k: v for k, v in data.items() if k != "type"}
    # the store entry is the same record
    assert store().get(LEDGER_KEY) == record
    return SwarmLedger.model_validate(record)


async def _ledger(
    agent_swarm: Agent, before: Sequence[Model] = (), **limits: Any
) -> SwarmLedger:
    # `before`: models the sample calls once each before the swarm starts
    async with sample(**limits) as t:
        for model in before:
            await model.generate("before")
        await agent_swarm(AgentState(messages=[ChatMessageUser(content="task")]))
        return _written_ledger(t)


def _starting_all(*runtime_calls: Model) -> Controller:
    # makes each runtime call, then starts every member and waits for them
    async def run(control: SwarmControl) -> StopReason:
        for model in runtime_calls:
            await model.generate("plan")
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            pass
        return "all_done"

    return controller(run, f"budget_test_{uuid4().hex}")


@pytest.mark.anyio
async def test_ledger_meters_sum_to_the_swarm_meter_and_reconcile() -> None:
    # no native-capable class from another test may still be loaded
    gc.collect()
    planner = scripted_model(cost=3.0)
    agent_swarm = swarm(
        [
            member(generator(scripted_model(cost=1.0)), name="a"),
            member(generator(scripted_model(cost=10.0), calls=2), name="b"),
        ],
        controller=_starting_all(planner),
        channels=[],
    )
    ledger = await _ledger(agent_swarm, cost=100.0)

    a, b = ledger.members
    assert (a.name, a.status, a.meter.cost, a.calls.calls) == (
        "a",
        "submitted",
        pytest.approx(1.0),
        1,
    )
    assert (b.name, b.status, b.meter.cost, b.calls.calls) == (
        "b",
        "submitted",
        pytest.approx(20.0),
        2,
    )
    assert ledger.runtime.cost == pytest.approx(3.0)
    assert ledger.final.cost == 0
    meters = [a.meter, b.meter, ledger.runtime, ledger.final]
    assert ledger.swarm.cost == pytest.approx(sum(m.cost for m in meters))
    assert ledger.swarm.usage.total_tokens == sum(m.usage.total_tokens for m in meters)
    assert ledger.swarm.usage.total_tokens == 4 * TOKENS_PER_CALL
    assert ledger.calls == Calls(calls=4)
    # two priced models and the runtime's: the sample's delta is the swarm meter
    assert ledger.sample_delta.total_tokens == 4 * TOKENS_PER_CALL
    assert ledger.sample_delta.total_cost == pytest.approx(24.0)
    assert ledger.unattributed.total_tokens == 0
    assert ledger.unattributed.total_cost == pytest.approx(0.0, abs=1e-9)
    assert ledger.caps == {"cost": pytest.approx(95.0), "tokens": None, "time": None}
    assert ledger.sample_limits["cost"] == 100.0
    assert ledger.overshoot == {}
    assert ledger.native_compaction_classes == []
    assert (ledger.stop_reason, ledger.interrupted, ledger.resumed) == (
        "all_done",
        False,
        False,
    )
    assert ledger.total_complete and ledger.attribution_complete


@pytest.mark.anyio
async def test_ledger_counts_only_the_swarm_span_tree() -> None:
    # the swarm runs in its own agent span, as under as_solver(), while the sample
    # makes a call outside it: the sample's delta holds that call, the swarm's counts
    # and meters do not
    gc.collect()
    outside = scripted_model(cost=1.0)
    started = anyio.Event()
    outside_done = anyio.Event()

    async def run_members(control: SwarmControl) -> StopReason:
        started.set()
        await outside_done.wait()
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            pass
        return "all_done"

    agent_swarm = swarm(
        generator(scripted_model(cost=1.0)),
        controller=controller(run_members, "budget_test_span_tree"),
        channels=[],
    )

    async def call_outside() -> None:
        await started.wait()
        await outside.generate("x")
        outside_done.set()

    async with sample() as t:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run, agent_swarm, "task")
            tg.start_soon(call_outside)
        ledger = _written_ledger(t)
    assert ledger.calls == Calls(calls=1)
    assert ledger.members[0].calls == Calls(calls=1)
    assert ledger.swarm.usage.total_tokens == TOKENS_PER_CALL
    assert ledger.sample_delta.total_tokens == 2 * TOKENS_PER_CALL
    assert ledger.unattributed.total_tokens == TOKENS_PER_CALL
    assert ledger.total_complete
    assert not ledger.attribution_complete


@pytest.mark.anyio
async def test_ledger_keeps_usage_reported_without_a_total() -> None:
    # some providers report component counts with total_tokens of zero
    gc.collect()
    output = _output()
    output.usage = ModelUsage(output_tokens=TOKENS_PER_CALL, total_tokens=0)
    ledger = await _ledger(
        swarm(generator(scripted_model(Script([output]), cost=2.0)), channels=[])
    )
    assert ledger.sample_delta.output_tokens == TOKENS_PER_CALL
    assert ledger.sample_delta.total_cost == pytest.approx(2.0)
    assert ledger.unattributed.output_tokens == 0
    assert ledger.unattributed.total_cost == pytest.approx(0.0, abs=1e-9)
    assert ledger.total_complete and ledger.attribution_complete


@agent
def _delegating(model: Model) -> Agent:
    # generates, then runs a subagent that generates in a span of its own
    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate(state.messages)
        await run(_generate_once(model), "x")
        return state

    return execute


@pytest.mark.anyio
async def test_ledger_finds_member_spans_named_by_a_span_id_provider() -> None:
    # a span-id provider may ignore the id run() asks for
    gc.collect()

    async def provider(name: str, parent: str | None, requested: str | None) -> str:
        return f"provided-{uuid4().hex}"

    agent_swarm = swarm(_delegating(scripted_model(cost=1.0)), channels=[])
    async with sample() as t:
        with span_id_provider(provider):
            await run(agent_swarm, "task")
        ledger = _written_ledger(t)
    assert ledger.calls == Calls(calls=2)
    assert ledger.members[0].calls == Calls(calls=2)


@solver
def _swarm_solver(agent_swarm: Agent) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        result = await run(agent_swarm, state.messages)
        state.output = result.output
        return state

    return solve


@pytest.mark.parametrize(
    "budget,limits,status",
    [
        pytest.param(Budget(cost=201.0), [], "swarm_cap", id="swarm-cost-cap"),
        pytest.param(
            Budget(),
            [token_limit(101 * TOKENS_PER_CALL - 25)],
            "member_limit:token",
            id="member-token-limit",
        ),
    ],
)
def test_ledger_reads_the_price_of_a_limit_reaching_call_from_a_bounded_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: Budget,
    limits: list[Limit],
    status: str,
) -> None:
    # Inspect prices a call after its event's last update, so the event a bounded
    # transcript has evicted and serialized for the call that reaches a limit has no
    # price. Each call costs $2; the 101st reaches the limit, after the resident window.
    gc.collect()
    monkeypatch.setenv("INSPECT_TRANSCRIPT_BOUNDED", "1")
    model = scripted_model(cost=2.0)
    agent_swarm = swarm(
        member(generator(model, calls=None), name="spender", limits=limits),
        channels=[],
        budget=budget,
    )
    [log] = eval(
        Task(dataset=[Sample(input="task")], solver=_swarm_solver(agent_swarm)),
        model=model,
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success" and log.samples is not None
    ledger = SwarmLedger.model_validate(log.samples[0].store[LEDGER_KEY])
    assert ledger.members[0].status == status
    assert ledger.calls == Calls(calls=101)
    assert ledger.swarm.cost == pytest.approx(202.0)
    assert ledger.total_complete


@pytest.mark.anyio
async def test_ledger_charges_an_approval_call_to_its_member_and_leaves_its_tokens_unattributed() -> (
    None
):
    gc.collect()
    ledger = await _ledger(swarm(_approving(scripted_model(cost=1.0)), channels=[]))
    [m] = ledger.members
    assert m.meter.cost == pytest.approx(2.0)
    assert m.meter.suspended_cost == pytest.approx(1.0)
    assert m.meter.usage.total_tokens == TOKENS_PER_CALL
    assert ledger.unattributed.total_tokens == TOKENS_PER_CALL
    assert ledger.unattributed.total_cost == pytest.approx(0.0, abs=1e-9)
    assert ledger.total_complete
    assert not ledger.attribution_complete


@pytest.mark.anyio
async def test_ledger_counts_cache_replays_and_charges_them_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gc.collect()
    monkeypatch.setenv("INSPECT_CACHE_DIR", str(tmp_path))
    ledger = await _ledger(swarm(_cached(scripted_model(cost=1.0)), channels=[]))
    [m] = ledger.members
    assert m.calls == Calls(calls=1, cache_replays=1)
    assert m.meter.cost == pytest.approx(1.0)
    assert m.meter.usage.total_tokens == TOKENS_PER_CALL
    assert ledger.total_complete and ledger.attribution_complete


@pytest.mark.anyio
async def test_ledger_attributes_native_compaction_to_the_member() -> None:
    model = _native_compaction_model(cost=1.0)
    ledger = await _ledger(swarm(_compacting(model), channels=[]))
    [m] = ledger.members
    # the compaction has no model event, but the member's lever records it
    assert m.calls == Calls(calls=1)
    assert m.meter.usage.total_tokens == 2 * TOKENS_PER_CALL
    assert m.meter.cost == pytest.approx(2.0)
    assert ledger.unattributed.total_tokens == 0
    # a loaded class with native compaction leaves the total a lower bound
    assert any(c.endswith("NativeCompaction") for c in ledger.native_compaction_classes)
    assert not ledger.total_complete


@pytest.mark.anyio
async def test_ledger_a_loaded_native_compaction_class_clears_total_complete() -> None:
    gc.collect()
    loaded = _native_compaction_model(cost=1.0)
    ledger = await _ledger(swarm(generator(scripted_model(cost=1.0)), channels=[]))
    assert ledger.calls == Calls(calls=1)
    assert ledger.native_compaction_classes != []
    assert not ledger.total_complete
    del loaded


@pytest.mark.anyio
async def test_ledger_an_unpriced_call_clears_total_complete() -> None:
    gc.collect()
    unpriced = scripted_model()
    ledger = await _ledger(
        swarm(
            [
                member(generator(scripted_model(cost=1.0)), name="priced"),
                member(generator(unpriced), name="unpriced"),
            ],
            channels=[],
        )
    )
    assert ledger.calls == Calls(calls=2, unpriced=1)
    assert ledger.members[1].calls == Calls(calls=1, unpriced=1)
    assert ledger.unpriced_calls_recorded == 1
    assert ledger.unpriced_models == [str(unpriced)]
    assert not ledger.total_complete


@pytest.mark.anyio
@pytest.mark.parametrize("caller", ["member", "controller"])
async def test_ledger_a_response_without_usage_clears_total_complete(
    caller: str,
) -> None:
    gc.collect()
    model = _no_usage_model(cost=1.0)
    priced = scripted_model(cost=1.0)
    if caller == "member":
        agent_swarm = swarm(generator(model), channels=[])
    else:
        agent_swarm = swarm(
            generator(priced), controller=_starting_all(model), channels=[]
        )
    ledger = await _ledger(agent_swarm)
    assert ledger.calls.no_usage == 1
    [m] = ledger.members
    assert m.calls.no_usage == (1 if caller == "member" else 0)
    assert not ledger.total_complete


@pytest.mark.anyio
async def test_ledger_a_recovered_error_clears_total_complete() -> None:
    gc.collect()
    failures = [RuntimeError("transport timeout")]

    def respond(input: list[ChatMessage]) -> ModelOutput:
        if failures:
            raise failures.pop()
        return _output()

    script = Script(respond=respond)
    ledger = await _ledger(
        swarm(generator(_retrying_model(script, cost=1.0)), channels=[])
    )
    [m] = ledger.members
    # the errored attempt, then the retry that succeeded
    assert m.status == "submitted"
    assert m.calls == Calls(calls=1, errored=1)
    assert not ledger.total_complete


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        # a failed request, then a successful one, in one call
        pytest.param([[500, 200]], Calls(calls=1, errored=1), id="then-success"),
        # a call whose client gave up after two failed requests, then Inspect's retry
        pytest.param([[500, 500], [200]], Calls(calls=1, errored=2), id="then-failure"),
    ],
)
@pytest.mark.anyio
async def test_ledger_counts_requests_a_client_retried_inside_a_call(
    calls: list[list[int]], expected: Calls
) -> None:
    gc.collect()
    model = _client_retrying_model(calls, cost=1.0)
    ledger = await _ledger(swarm(generator(model), channels=[]))
    [m] = ledger.members
    assert m.status == "submitted"
    assert m.calls == expected
    assert ledger.calls == expected
    assert not ledger.total_complete
    assert not ledger.attribution_complete


@pytest.mark.anyio
async def test_ledger_an_unpriced_approval_call_clears_total_complete() -> None:
    # the suspended call bypasses the token node's count; the sample delta and the
    # call's event both show it
    gc.collect()
    approver = scripted_model()
    ledger = await _ledger(
        swarm(_approving(scripted_model(cost=1.0), approver), channels=[])
    )
    assert ledger.calls == Calls(calls=2, unpriced=1)
    assert ledger.unpriced_calls_recorded == 0
    assert ledger.unpriced_models == [str(approver)]
    assert not ledger.total_complete


@pytest.mark.anyio
async def test_ledger_an_unpriced_routed_approval_call_clears_total_complete() -> None:
    # two approval calls to one model, served by a priced and an unpriced model: the
    # model's aggregate keeps the known cost, so only the per-call check sees it
    gc.collect()
    served = f"mockllm/served-{uuid4().hex}"
    _price(served, 1.0)
    router = _router_model(
        [
            _usage_output(output=TOKENS_PER_CALL, model=served),
            _usage_output(
                output=TOKENS_PER_CALL, model=f"mockllm/unpriced-{uuid4().hex}"
            ),
        ]
    )
    ledger = await _ledger(
        swarm(_approving(scripted_model(cost=1.0), router, approvals=2), channels=[])
    )
    assert ledger.calls == Calls(calls=3, unpriced=1)
    assert ledger.unpriced_calls_recorded == 0
    assert ledger.unpriced_models == []
    assert not ledger.total_complete


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case", ["one-model", "small-model-first", "large-model-first"]
)
async def test_ledger_reconciles_after_prior_sample_spend(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    # the sample spent $100 before the swarm; the swarm's calls cost $0.000001 each.
    # The sample's interval cost is a difference of cumulative sums, rounded at $100.
    gc.collect()
    large = _rated_model(
        [
            _usage_output(output=TOKENS_PER_CALL),  # $100, before the swarm
            _usage_output(input=TOKENS_PER_CALL),  # $0.000001
        ],
        input_call=1e-6,
        output_call=100.0,
    )
    small = _rated_model(
        [_usage_output(output=TOKENS_PER_CALL)] * 2, input_call=0, output_call=1e-6
    )
    if case == "one-model":
        members: list[Agent] = [generator(large)]
        before = [large]
    else:
        # the sample's usage lists models in the order of their first call
        members = [generator(small), generator(large)]
        before = [small, large] if case == "small-model-first" else [large, small]
    with caplog.at_level(logging.ERROR, logger="inspect_swarm"):
        ledger = await _ledger(swarm(members, channels=[]), before=before)
    assert "does not reconcile" not in caplog.text
    assert ledger.unattributed.total_tokens == 0
    assert ledger.unattributed.total_cost == pytest.approx(0.0, abs=1e-12)
    assert ledger.total_complete and ledger.attribution_complete


@pytest.mark.anyio
async def test_ledger_counts_a_hard_stopped_call_as_errored() -> None:
    gc.collect()
    script = Script(gate=anyio.Event())

    async def run(control: SwarmControl) -> StopReason:
        control.start(control.members[0])
        # stop once the member's call is in flight; with no grace it is cancelled
        while script.started < 1:
            await anyio.sleep(0)
        return "all_done"

    ledger = await _ledger(
        swarm(
            generator(scripted_model(script, cost=1.0)),
            controller=controller(run, "budget_test_hard_stop"),
            channels=[],
            budget=Budget(grace=0),
        )
    )
    [m] = ledger.members
    assert (m.status, m.cancelled) == ("cancelled", True)
    assert m.calls == Calls(errored=1)
    assert ledger.stop_reason == "all_done"
    assert not ledger.total_complete


@pytest.mark.anyio
async def test_ledger_records_the_cap_overshoot() -> None:
    gc.collect()
    script = Script(gate=anyio.Event())
    model = scripted_model(script, cost=10.0)

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        # both calls are in flight before either records
        while script.started < 2:
            await anyio.sleep(0)
        assert script.gate is not None
        script.gate.set()
        async for _ in control.events():
            pass
        return "all_done"

    ledger = await _ledger(
        swarm(
            member(generator, args={"model": model}, count=2),
            controller=controller(run, "budget_test_overshoot"),
            channels=[],
            budget=Budget(cost=15.0),
        )
    )
    assert ledger.stop_reason == "swarm_cap"
    assert ledger.caps == {"cost": 15.0, "tokens": None, "time": None}
    assert ledger.overshoot == {"cost": pytest.approx(5.0)}
    assert ledger.swarm.cost == pytest.approx(20.0)


@pytest.mark.anyio
async def test_ledger_is_written_when_a_member_crashes() -> None:
    gc.collect()
    async with sample() as t:
        with pytest.raises(RuntimeError, match="member crashed"):
            await swarm(_failing(), channels=[])(
                AgentState(messages=[ChatMessageUser(content="task")])
            )
        ledger = _written_ledger(t)
    [m] = ledger.members
    assert m.status == "errored"
    assert ledger.interrupted
    assert not ledger.total_complete


@pytest.mark.anyio
async def test_ledger_logs_an_error_when_the_meters_exceed_the_sample(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gc.collect()
    # the sample has spent $100 before the swarm, and the swarm meter records $1 more
    # than the sample did
    prior = scripted_model(cost=100.0)
    with caplog.at_level(logging.ERROR, logger="inspect_swarm"):
        ledger = await _ledger(
            swarm(_recording(scripted_model(cost=1.0), cost=1.0), channels=[]),
            before=[prior],
        )
    assert ledger.unattributed.total_tokens == -TOKENS_PER_CALL
    assert ledger.unattributed.total_cost == pytest.approx(-1.0)
    assert "does not reconcile" in caplog.text


@pytest.mark.anyio
async def test_pinned_a_priced_model_event_carries_its_cost(
    fresh_transcript: Transcript,
) -> None:
    # the ledger's per-call pricing check reads the cost Inspect sets on the event's usage
    await scripted_model(cost=2.0).generate("x")
    [event] = [e for e in fresh_transcript.events if isinstance(e, ModelEvent)]
    assert event.output.usage is not None
    assert event.output.usage.total_cost == pytest.approx(2.0)


@pytest.mark.parametrize(
    "attempt,expected",
    [(None, False), ("initial", False), ("resume", True)],
)
def test_pinned_sample_resumed_reads_the_checkpointer_setup(
    attempt: Literal["initial", "resume"] | None, expected: bool
) -> None:
    setup = _CheckpointerSetup(
        config=cast(Any, None),
        log_location="",
        sample_id=1,
        epoch=1,
        resume_checkpoint=None if attempt is None else ResumeCheckpoint(attempt),
    )
    token = _sample_active.set(cast(Any, SimpleNamespace(checkpointer=setup)))
    try:
        assert sample_resumed() is expected
    finally:
        _sample_active.reset(token)
    assert sample_resumed() is False
