"""Helpers for tests that run a swarm: scripted mockllm models, member agents and a sample context."""

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import anyio
from inspect_ai.agent import Agent, AgentState, agent, run
from inspect_ai.agent._bridge._errors import PROVIDER_ERROR_KEY
from inspect_ai.agent._bridge.sandbox.bridge import _error_after_unwind
from inspect_ai.agent._bridge.sandbox.service import _forward_provider_errors
from inspect_ai.agent._bridge.sandbox.types import SandboxAgentBridge
from inspect_ai.agent._bridge.types import AgentBridge
from inspect_ai.agent._bridge.util import bridge_generate
from inspect_ai.event import (
    CommunicationDelivery,
    CommunicationEvent,
    CommunicationKind,
    CommunicationSend,
    CommunicationWait,
    SwarmEvent,
)
from inspect_ai.log import transcript as current_transcript
from inspect_ai.log._transcript import Transcript, _transcript
from inspect_ai.model import (
    ChatMessage,
    ChatMessageUser,
    CompactionStrategy,
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
from pydantic import JsonValue

from inspect_swarm import Controller, swarm_controller
from inspect_swarm._record import SwarmResultRecord, _transcript_result

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


async def bridged_generate(model: Model, messages: list[ChatMessage]) -> ModelOutput:
    """A generation the agent bridge serves, through inspect_ai's `bridge_generate()`."""
    output, _ = await bridge_generate(
        AgentBridge(AgentState(messages=[])),
        model,
        messages,
        [],
        None,
        GenerateConfig(),
        requested_model=None,
    )
    return output


@agent
def cli(
    model: Model,
    calls: int | None = 1,
    requests: int = 1,
    compaction: CompactionStrategy | None = None,
    retries: int = 0,
) -> Agent:
    """A CLI agent run as inspect_ai's sandbox bridge runs one: `calls` turns (`None`: until stopped) of `requests` overlapping generations, each served through the bridge service's error forwarding, compacted with `compaction` and tracked into the agent's state as the bridge's dialects track it. A limit a generation reaches is answered with an error reply, after which the CLI makes `retries` more requests, one at a time, and exits; the bridge then raises the limit it kept."""

    async def execute(state: AgentState) -> AgentState:
        bridge = SandboxAgentBridge(
            state=state,
            filter=None,
            retry_refusals=None,
            compaction=compaction,
            port=13131,
            model=None,
        )

        # the CLI's own conversation, which it sends with each request
        conversation = list(state.messages)

        async def generate(
            json_data: dict[str, JsonValue], headers: dict[str, str] | None = None
        ) -> dict[str, JsonValue]:
            output, _ = await bridge_generate(
                bridge,
                model,
                conversation,
                [],
                None,
                GenerateConfig(),
                requested_model=None,
            )
            # the bridge's dialects track the agent's state from each generation
            await bridge._track_state(conversation, output)  # pyright: ignore[reportPrivateUsage]
            return {}

        forward = _forward_provider_errors(generate, bridge)
        replies: list[dict[str, JsonValue]] = []

        async def request() -> None:
            replies.append(await forward({}))

        n = 0
        while calls is None or n < calls:
            async with anyio.create_task_group() as tg:
                for _ in range(requests):
                    tg.start_soon(request)
            if any(PROVIDER_ERROR_KEY in reply for reply in replies):
                for _ in range(retries):
                    await request()
                break
            conversation.append(state.output.message)
            n += 1
        error = await _error_after_unwind(bridge, None)
        if error is not None:
            raise error
        return state

    return execute


@agent
def sleeper(seconds: float = 3600.0) -> Agent:
    """Wait, as a member inside a long tool call does."""

    async def execute(state: AgentState) -> AgentState:
        await anyio.sleep(seconds)
        return state

    return execute


@agent
def after(names: list[str], inner: Agent) -> Agent:
    """Wait, making no model call, until the named members' `member_result` evidence is in the transcript, then run `inner`."""

    async def execute(state: AgentState) -> AgentState:
        while True:
            ended = {
                e.member for e in swarm_events(current_transcript(), "member_result")
            }
            if ended >= set(names):
                return await inner(state)
            await anyio.sleep(0.01)

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
        record = _transcript_result(store())
        assert record is not None
    return Outcome(
        cast(str, final(transcript).data["stop_reason"]),
        record,
        state,
        transcript,
    )


def comm_events(
    transcript: Transcript, kind: CommunicationKind
) -> list[CommunicationEvent]:
    """The swarm's communication events of one kind, in order."""
    return [
        e
        for e in transcript.events
        if isinstance(e, CommunicationEvent)
        and e.source == "inspect_swarm"
        and e.kind == kind
    ]


def swarm_events(transcript: Transcript, kind: str) -> list[SwarmEvent]:
    """The swarm's `SwarmEvent`s of one kind, in order."""
    return [
        e for e in transcript.events if isinstance(e, SwarmEvent) and e.kind == kind
    ]


def send(event: CommunicationEvent) -> CommunicationSend:
    """A `sent` event's `send`."""
    assert event.send is not None
    return event.send


def delivery(event: CommunicationEvent) -> CommunicationDelivery:
    """A `delivered` event's `delivery`."""
    assert event.delivery is not None
    return event.delivery


def wait(event: CommunicationEvent) -> CommunicationWait:
    """A `waited` event's `wait`."""
    assert event.wait is not None
    return event.wait


def data(event: CommunicationEvent | SwarmEvent) -> dict[str, Any]:
    """An event's `data`, untyped, for reading nested values."""
    return event.data


def final(transcript: Transcript) -> SwarmEvent:
    """The swarm's `final` event from a transcript."""
    [event] = swarm_events(transcript, "final")
    return event


def statuses(outcome: Outcome) -> dict[str, str | None]:
    """Each member's status, by name, from the result record."""
    return {m.name: m.status for m in outcome.record.members}
