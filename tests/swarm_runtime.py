"""Helpers for tests that run a swarm: scripted mockllm models, member agents and a sample context."""

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import anyio
from inspect_ai.agent import Agent, AgentState, agent, run
from inspect_ai.event import InfoEvent
from inspect_ai.log._transcript import Transcript, _transcript
from inspect_ai.model import (
    ChatMessage,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelCost,
    ModelInfo,
    ModelOutput,
    ModelUsage,
    get_model,
    set_model_info,
)
from inspect_ai.model._model import init_sample_model_usage
from inspect_ai.tool import ToolChoice, ToolInfo
from inspect_ai.util import (
    cost_limit,
    message_limit,
    time_limit,
    token_limit,
    turn_limit,
    working_limit,
)
from inspect_ai.util._concurrency import init_concurrency
from inspect_ai.util._store import Store, init_subtask_store, store

from inspect_swarm import Controller, swarm_controller
from inspect_swarm._record import RECORD_KEY, SwarmResultRecord

TOKENS_PER_CALL = 50


def output(content: str = "ok") -> ModelOutput:
    """A mockllm output that used `TOKENS_PER_CALL` output tokens."""
    result = ModelOutput.from_content("mockllm", content)
    result.usage = ModelUsage(
        input_tokens=0, output_tokens=TOKENS_PER_CALL, total_tokens=TOKENS_PER_CALL
    )
    return result


class Script:
    """Scripted mockllm outputs that count calls, take `delay` seconds, and can be held in flight until `gate` is set."""

    def __init__(
        self,
        outputs: Sequence[ModelOutput] = (),
        gate: anyio.Event | None = None,
        respond: Callable[[list[ChatMessage]], ModelOutput] | None = None,
        delay: float = 0,
    ) -> None:
        self.outputs = list(outputs)
        self.delay = delay
        self.gate = gate
        self.respond = respond
        self.started = 0
        self.calls = 0

    async def __call__(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        self.started += 1
        if self.gate is not None:
            await self.gate.wait()
        # a real provider awaits the network, so a cancelled caller stops here
        await anyio.sleep(self.delay)
        self.calls += 1
        if self.respond is not None:
            return self.respond(input)
        return self.outputs.pop(0) if self.outputs else output()


def scripted_model(script: Script | None = None, cost: float | None = None) -> Model:
    """A mockllm model whose calls each use `TOKENS_PER_CALL` tokens and cost `cost` dollars."""
    name = f"mockllm/m-{uuid4().hex}"
    if cost is not None:
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
    return get_model(name, memoize=False, custom_outputs=script or Script())


@agent
def generator(model: Model, calls: int | None = 1, answer: str = "") -> Agent:
    """Generate `calls` times (`None`: until stopped), appending each reply, then answer `answer` if given."""

    async def execute(state: AgentState) -> AgentState:
        n = 0
        while calls is None or n < calls:
            state.output = await model.generate(state.messages)
            state.messages.append(state.output.message)
            n += 1
        if answer:
            state.output = ModelOutput.from_content("mockllm", answer)
        return state

    return execute


@agent
def sleeper(seconds: float = 3600.0) -> Agent:
    """Wait, as a member inside a long tool call does."""

    async def execute(state: AgentState) -> AgentState:
        await anyio.sleep(seconds)
        return state

    return execute


def controller(run: Any, name: str) -> Controller:
    """A controller registered under `name` whose run is `run`."""

    @swarm_controller(name=name)
    def factory() -> Controller:
        return Controller(run)

    return factory()


@asynccontextmanager
async def sample(
    *,
    tokens: int | None = None,
    cost: float | None = None,
    time: float | None = None,
    messages: int | None = None,
) -> AsyncGenerator[Transcript]:
    """The limit roots, store and transcript of a running sample."""
    # the registry's limiters are bound to the async backend that created them
    init_concurrency()
    init_subtask_store(Store())
    init_sample_model_usage()
    transcript = Transcript()
    token = _transcript.set(transcript)
    try:
        with (
            token_limit(tokens),
            cost_limit(cost),
            message_limit(messages),
            turn_limit(None),
            working_limit(None),
            time_limit(time),
        ):
            yield transcript
    finally:
        _transcript.reset(token)


@dataclass
class Outcome:
    """What a swarm run left: its stop reason, its result record, the state it returned and its transcript."""

    reason: str
    record: SwarmResultRecord
    state: AgentState
    transcript: Transcript


async def run_swarm(
    swarm: Agent,
    input: str = "task",
    *,
    tokens: int | None = None,
    cost: float | None = None,
    time: float | None = None,
    messages: int | None = None,
) -> Outcome:
    """Run a swarm in a sample and return what it left."""
    async with sample(
        tokens=tokens, cost=cost, time=time, messages=messages
    ) as transcript:
        # in its own agent span, as as_solver() and run() give it
        state = await run(swarm, [ChatMessageUser(content=input)])
        record = SwarmResultRecord.model_validate(store().get(RECORD_KEY))
    return Outcome(final(transcript)["stop_reason"], record, state, transcript)


def evidence(transcript: Transcript, kind: str) -> list[dict[str, Any]]:
    """The swarm's evidence payloads of one kind, in order."""
    return [
        e.data
        for e in transcript.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("kind") == kind
    ]


def final(transcript: Transcript) -> dict[str, Any]:
    """The swarm's `final` evidence from a transcript."""
    [data] = evidence(transcript, "final")
    return data


def statuses(outcome: Outcome) -> dict[str, str | None]:
    """Each member's status, by name, from the result record."""
    return {m.name: m.status for m in outcome.record.members}
