import os
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent, ModelEvent
from inspect_ai.log import EvalSample
from inspect_ai.model import ChatMessageTool, ChatMessageUser
from inspect_ai.tool import ToolDef

from inspect_swarm import (
    leaderless,
    member,
    messages,
    swarm,
    swarm_bridged_tools,
    swarm_tools,
)

inspect_swe = pytest.importorskip("inspect_swe")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("OPENAI_API_KEY")),
        reason="needs ANTHROPIC_API_KEY and OPENAI_API_KEY",
    ),
]

WORDS = {"cc": "AMBER-7", "codex": "COBALT-3"}

TASK = "Work with the other members of the swarm."

# the CLI's swarm_wait lasts longer than the 60 s Codex documents as its default MCP
# tool timeout; both CLIs actually allow 300 s
DELAY = 75

PROMPT = """You are in a swarm with a member named w-1. Use the swarm's MCP tools.
1. Call send_message with to="w-1" and the message "hello from <your member name>".
2. Call swarm_wait once with no arguments, and wait for it to return.
3. Call read_messages. It gives you a code word.
4. {reply}
Then stop."""

CC_REPLY = 'Start a subagent with your Task (or Agent) tool and have the subagent call send_message with to="w-1" and the code word as the message. Do not send it yourself.'

CODEX_REPLY = 'Call send_message with to="w-1" and the code word as the message.'


@agent
def collector(cli: str, delay: float = DELAY) -> Agent:
    """Wait for the CLI's hello, give it its code word after `delay`, and wait for the word back."""

    async def execute(state: AgentState) -> AgentState:
        tools = {ToolDef(t).name: t for t in await swarm_tools().tools()}
        heard = ""

        async def until(done: Callable[[str], bool]) -> None:
            nonlocal heard
            for _ in range(20):
                if done(heard):
                    return
                await tools["swarm_wait"]()
                heard += str(await tools["read_messages"]())

        await until(lambda text: f'from="{cli}"' in text)
        await anyio.sleep(delay)
        await tools["send_message"](to=cli, message=f"The code word is {WORDS[cli]}.")
        await until(lambda text: WORDS[cli] in text)
        return state

    return execute


def _cli(name: str) -> Agent:
    tools = [swarm_bridged_tools(["messages"])]
    if name == "cc":
        return inspect_swe.claude_code(
            model="anthropic/claude-sonnet-4-5",
            system_prompt=PROMPT.format(reply=CC_REPLY),
            bridged_tools=tools,
        )
    return inspect_swe.codex_cli(
        model="openai/gpt-5",
        system_prompt=PROMPT.format(reply=CODEX_REPLY),
        bridged_tools=tools,
        config_overrides={"mcp_servers.swarm.tool_timeout_sec": "700"},
    )


# one CLI per swarm: two bridged agents in one sandbox both take the bridge's ports
@pytest.mark.parametrize("cli", ["cc", "codex"])
def test_a_cli_member_messages_a_native_member(cli: str) -> None:
    task = Task(
        dataset=[Sample(input=TASK)],
        solver=swarm(
            [member(collector(cli), name="w-1"), member(_cli(cli), name=cli)],
            channels=[messages()],
            controller=leaderless(stop_on_submission=False),
        ),
        sandbox="docker",
    )
    [log] = eval(task, model="mockllm/model", limit=1, time_limit=900)
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples

    # the bus bound the sender, through the bridge
    records = [s["record"] for s in _evidence(sample, "sent") if s["member"] == cli]
    assert {(r["via"], r["sender"]) for r in records} == {("bridge", cli)}
    assert any(f"hello from {cli}" in r["payload"].lower() for r in records)
    assert any(WORDS[cli] in r["payload"] for r in records)
    reads = [r for r in _evidence(sample, "read") if r["member"] == cli]
    assert reads and {r["via"] for r in reads} == {"bridge"}
    # its wait outlasted the delay and returned the message, not an MCP timeout
    [wait] = [w for w in _evidence(sample, "waited") if w["member"] == cli]
    assert (wait["outcome"], wait["via"]) == ("news", "bridge")
    assert wait["duration"] > 60
    assert any(
        "Call read_messages() to read them." in m.text
        for e in sample.events
        if isinstance(e, ModelEvent)
        for m in e.input
        if isinstance(m, ChatMessageTool)
    )
    if cli == "cc":
        # the reply came from a subagent: the conversation that proposed it is not the
        # one the task started
        proposals = [
            e
            for e in sample.events
            if isinstance(e, ModelEvent)
            and any(
                c.function.endswith("send_message") and WORDS[cli] in str(c.arguments)
                for c in e.output.message.tool_calls or []
            )
        ]
        assert proposals
        for e in proposals:
            first = next(m for m in e.input if isinstance(m, ChatMessageUser))
            assert TASK not in first.text


def _evidence(sample: EvalSample, kind: str) -> list[dict[str, Any]]:
    return [
        e.data
        for e in sample.events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("kind") == kind
    ]
