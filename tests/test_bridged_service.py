from typing import Any
from uuid import uuid4

import anyio
import pytest
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.agent._bridge.sandbox.service import call_tool
from inspect_ai.agent._bridge.sandbox.types import SandboxAgentBridge
from inspect_ai.tool import ToolCall, ToolDef, ToolError
from inspect_ai.util._span import current_span_id, span

from inspect_swarm import (
    leaderless,
    member,
    messages,
    shared_memory,
    swarm,
    swarm_bridged_tools,
    swarm_tools,
)

from .swarm_runtime import evidence, run_swarm

PLAN = "/shared_memory/plan.md"


# A bridged member's CLI as the bridge's service sees it: each call is proposed by a
# model call in one of the CLI's conversations, an agent span of its own, and runs in
# the member's context, as the service task does, outside that span.
class _Cli:
    def __init__(self, channels: list[str], max_wait: int = 280) -> None:
        spec = swarm_bridged_tools(channels, max_wait)
        self.bridge = SandboxAgentBridge(
            state=AgentState(messages=[]),
            filter=None,
            retry_refusals=None,
            compaction=None,
            port=13131,
            model=None,
            bridged_tools={spec.name: {ToolDef(t).name: t for t in spec.tools}},
        )
        self._spans: dict[str, str | None] = {}

    async def call(self, conversation: str, tool: str, **arguments: Any) -> str:
        if conversation not in self._spans:
            async with span(conversation, type="agent"):
                self._spans[conversation] = current_span_id()
        [info] = [
            info
            for key, info in self.bridge.served_tools.items()
            if (key.server, key.tool) == ("swarm", tool)
        ]
        name = f"mcp__swarm__{tool}"
        self.bridge.register_tool_execution_grants(
            [ToolCall(id=uuid4().hex, function=name, arguments=arguments)],
            [info.model_copy(update={"name": name})],
            span_id=self._spans[conversation],
        )
        return str(await call_tool(self.bridge)("swarm", tool, arguments))


@agent
def native(sent: anyio.Event, done: anyio.Event, results: dict[str, str]) -> Agent:
    """Write the plan, message the CLI, then read its reply once it is done."""

    async def execute(state: AgentState) -> AgentState:
        tools = {ToolDef(t).name: t for t in await swarm_tools().tools()}
        await tools["shared_memory"](command="create", path=PLAN, file_text="a\nb\n")
        await tools["send_message"](to="cli", message="first", wake=False)
        await tools["send_message"](to="cli", message="second")
        sent.set()
        await done.wait()
        results["native"] = str(await tools["read_messages"]())
        return state

    return execute


@agent
def cli(script: Any) -> Agent:
    """Run `script` with a bridged CLI."""

    async def execute(state: AgentState) -> AgentState:
        await script()
        return state

    return execute


@pytest.mark.anyio
async def test_a_bridged_cli_is_one_loop_of_its_member() -> None:
    sent, done = anyio.Event(), anyio.Event()
    results: dict[str, str] = {}

    async def script() -> None:
        c = _Cli(["messages", "shared_memory"])
        # a wait from one conversation ends on the waking message
        results["wait"] = await c.call("parent", "swarm_wait")
        # the inbox is the member's: a read by one conversation leaves the other none
        results["parent"] = await c.call("parent", "read_messages")
        results["child"] = await c.call("child", "read_messages")
        # a view by one conversation counts for the other
        await c.call("child", "shared_memory", command="view", path=PLAN)
        results["insert"] = await c.call(
            "parent",
            "shared_memory",
            command="insert",
            path=PLAN,
            insert_line=1,
            insert_text="x",
        )
        results["send"] = await c.call("child", "send_message", to="w-1", message="hi")
        done.set()

    with anyio.fail_after(20):
        outcome = await run_swarm(
            swarm(
                [
                    member(native(sent, done, results), name="w-1"),
                    member(cli(script), name="cli", bridged=True),
                ],
                channels=[messages(rate=None), shared_memory(rate=None)],
                controller=leaderless(stop_on_submission=False),
            )
        )

    # the board's news too, since the CLI has the shared memory
    assert results["wait"].split("\n") == [
        "2 unread messages (from w-1). Call read_messages() to read them.",
        '1 shared memory file changed where you waited (f-1 new; by w-1). View one with shared_memory(command="view", file_id="f-1").',
        "You have 2 unread messages.",
    ]
    assert "first" in results["parent"] and "second" in results["parent"]
    assert results["child"] == "You have no unread messages."
    assert results["insert"].startswith("Inserted text after line 1")
    assert results["send"] == "Sent m-3 to w-1."
    assert 'from="cli"' in results["native"] and "hi" in results["native"]

    reads = [r for r in evidence(outcome.transcript, "read") if r["member"] == "cli"]
    # the read that found nothing is no read
    assert [(r["via"], r["nested"]) for r in reads] == [("bridge", False)] * 2
    assert len({r["loop"] for r in reads}) == 1
    assert None not in {r["tool_span_id"] for r in reads}
    sends = [s for s in evidence(outcome.transcript, "sent") if s["member"] == "cli"]
    assert [(s["record"]["via"], s["nested"]) for s in sends] == [("bridge", False)] * 2
    assert [s["record"]["kind"] for s in sends] == ["insert", "message"]
    [wait] = [w for w in evidence(outcome.transcript, "waited")]
    assert (wait["member"], wait["via"], wait["outcome"]) == ("cli", "bridge", "news")
    native_sends = [
        s for s in evidence(outcome.transcript, "sent") if s["member"] == "w-1"
    ]
    assert {s["record"]["via"] for s in native_sends} == {"tool"}


@agent
def waiter(started: anyio.Event, done: anyio.Event, results: dict[str, str]) -> Agent:
    """Wait in the member's own loop, then stay running until `done`."""

    async def execute(state: AgentState) -> AgentState:
        tools = {ToolDef(t).name: t for t in await swarm_tools().tools()}
        started.set()
        results["native"] = str(await tools["swarm_wait"]())
        await done.wait()
        return state

    return execute


@pytest.mark.anyio
async def test_a_bridged_member_never_counts_as_waiting() -> None:
    started, done = anyio.Event(), anyio.Event()
    results: dict[str, str] = {}

    async def script() -> None:
        c = _Cli(["messages"])
        await started.wait()

        async def wait(conversation: str) -> None:
            results[conversation] = await c.call(conversation, "swarm_wait")

        # its waits are per call, so two conversations wait at once
        async with anyio.create_task_group() as tg:
            tg.start_soon(wait, "parent")
            tg.start_soon(wait, "child")
        done.set()

    with anyio.fail_after(20):
        outcome = await run_swarm(
            swarm(
                [
                    member(waiter(started, done, results), name="w-1"),
                    member(cli(script), name="cli", bridged=True),
                ],
                channels=[messages(max_wait=1)],
                controller=leaderless(stop_on_submission=False),
            )
        )

    # with every member in a wait, none was released for all waiting
    timeout = "Nothing you waited for happened in 1 s."
    assert results == {"native": timeout, "parent": timeout, "child": timeout}
    assert {w["outcome"] for w in evidence(outcome.transcript, "waited")} == {"timeout"}


@pytest.mark.anyio
async def test_a_bridged_wait_is_capped_by_the_tools_max_wait() -> None:
    # below the CLI's own tool timeout, which the channel's longer wait would pass
    started, done = anyio.Event(), anyio.Event()
    results: dict[str, str] = {}

    async def script() -> None:
        await started.wait()
        results["cli"] = await _Cli(["messages"], max_wait=1).call(
            "parent", "swarm_wait"
        )
        # running until the native wait returns, so that its end does not release it
        while "native" not in results:
            await anyio.sleep(0.05)
        done.set()

    with anyio.fail_after(20):
        outcome = await run_swarm(
            swarm(
                [
                    member(waiter(started, done, results), name="w-1"),
                    member(cli(script), name="cli", bridged=True),
                ],
                channels=[messages(max_wait=2)],
                controller=leaderless(stop_on_submission=False),
            )
        )

    # the native member's wait keeps the channel's maximum
    assert results == {
        "native": "Nothing you waited for happened in 2 s.",
        "cli": "Nothing you waited for happened in 1 s.",
    }
    assert sorted(
        (w["member"], w["timeout"]["effective"])
        for w in evidence(outcome.transcript, "waited")
    ) == [("cli", 1), ("w-1", 2)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,arguments,channel",
    [
        ("shared_memory", {"command": "changes"}, "shared_memory"),
        ("swarm_wait", {}, "shared_memory"),
    ],
)
async def test_a_tool_of_a_channel_the_swarm_lacks_says_so(
    tool: str, arguments: dict[str, Any], channel: str
) -> None:
    errors: list[str] = []

    async def script() -> None:
        try:
            await _Cli([channel]).call("parent", tool, **arguments)
        except ToolError as ex:
            errors.append(str(ex))

    await run_swarm(
        swarm(member(cli(script), name="cli", bridged=True), channels=["messages"])
    )
    assert errors == [
        f"The swarm has no shared_memory channel, so {tool} cannot be used."
    ]


@agent
def mixed(script: Any) -> Agent:
    """A native member that resolves swarm_tools(), then runs `script` with a bridged CLI, as a bridged subagent would."""

    async def execute(state: AgentState) -> AgentState:
        await swarm_tools().tools()
        await script()
        return state

    return execute


@pytest.mark.anyio
async def test_a_native_members_bridged_subagent_is_refused() -> None:
    # it would be a second reader of the member's inbox
    errors: list[str] = []

    async def script() -> None:
        try:
            await _Cli(["messages"]).call("parent", "read_messages")
        except ToolError as ex:
            errors.append(str(ex))

    await run_swarm(swarm(member(mixed(script), name="w-1"), channels=["messages"]))
    assert errors == [
        "read_messages is for a bridged member; w-1 uses the swarm's tools from swarm_tools()."
    ]


@pytest.mark.anyio
async def test_the_bridged_tools_are_the_native_ones() -> None:
    found: dict[str, ToolDef] = {}

    @agent
    def lister() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            found.update(
                {ToolDef(t).name: ToolDef(t) for t in await swarm_tools().tools()}
            )
            return state

        return execute

    await run_swarm(swarm(lister(), channels=["messages", "shared_memory"]))
    bridged = {
        ToolDef(t).name: ToolDef(t)
        for t in swarm_bridged_tools(["messages", "shared_memory"]).tools
    }
    assert sorted(bridged) == sorted(found)
    for name, native in found.items():
        assert (native.description, native.max_output) == (
            bridged[name].description,
            bridged[name].max_output,
        )
        if name == "swarm_wait":
            # its timeout's description names the run's longest wait
            assert list(native.parameters.properties) == list(
                bridged[name].parameters.properties
            )
        else:
            assert native.parameters == bridged[name].parameters


@pytest.mark.parametrize(
    "channels", [[], ["filesystem"], ["messages", "messages"], "messages"]
)
def test_channels_must_be_the_built_in_ones_each_once(channels: Any) -> None:
    with pytest.raises(ValueError, match="channels must name 'messages' or"):
        swarm_bridged_tools(channels)


def test_max_wait_must_be_at_least_one() -> None:
    with pytest.raises(
        ValueError, match="max_wait must be a whole number of at least 1"
    ):
        swarm_bridged_tools(["messages"], max_wait=0)
