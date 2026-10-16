"""Helpers for tests that run a swarm: scripted mockllm models, member agents and a sample context."""

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import anyio
from inspect_ai.agent import Agent, AgentState, agent
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
from inspect_ai.util._store import Store, init_subtask_store

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


async def run_swarm(
    swarm: Agent,
    input: str = "task",
    *,
    tokens: int | None = None,
    cost: float | None = None,
    time: float | None = None,
    messages: int | None = None,
) -> dict[str, Any]:
    """Run a swarm in a sample and return its `stopped` evidence: the stop reason and each member's state and status."""
    async with sample(
        tokens=tokens, cost=cost, time=time, messages=messages
    ) as transcript:
        await swarm(AgentState(messages=[ChatMessageUser(content=input)]))
    return stopped(transcript)


def stopped(transcript: Transcript) -> dict[str, Any]:
    """The swarm's `stopped` evidence from a transcript."""
    [data] = [
        e.data
        for e in transcript.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("kind") == "stopped"
    ]
    assert isinstance(data, dict)
    return data


def statuses(stop: dict[str, Any]) -> dict[str, str | None]:
    """Each member's status, by name, from the `stopped` evidence."""
    return {m["member"]: m["status"] for m in stop["members"]}
