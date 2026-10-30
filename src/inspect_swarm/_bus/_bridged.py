from collections.abc import Callable, Sequence
from typing import Any

from inspect_ai.agent import BridgedToolsSpec
from inspect_ai.tool import Tool, ToolDef, ToolError

from .._channel._channel import ToolDeclaration
from .._channel.messages import MessagesState
from .._channel.messages import declarations as messages_declarations
from .._channel.shared_memory import builtin_board
from .._channel.shared_memory import declarations as board_declarations
from ._bus import Bus, current_bus
from ._record import current_via
from ._wait import wait_tool

SERVER = "swarm"

_CHANNELS: dict[str, Callable[[], list[ToolDeclaration]]] = {
    "messages": messages_declarations,
    "shared_memory": board_declarations,
}


def swarm_bridged_tools(channels: Sequence[str]) -> BridgedToolsSpec:
    """The tools of the swarm's channels, for a bridged member's agent.

    Pass it in the agent's bridged tools, `claude_code(bridged_tools=[swarm_bridged_tools(["messages"])])`. A bridged agent's tools are fixed when it is built, so name the channels it uses: `"messages"` gives `send_message`, `read_messages` and `list_members`, and `"shared_memory"` gives `shared_memory`. Both also give `swarm_wait`. The tools are served as the MCP server `swarm`.

    Each tool acts as the member whose agent calls it, and finds the swarm's channel when it is called; a tool whose channel the swarm does not have returns an error saying so. The CLI is one loop of the member, whichever of its conversations calls: its subagents read the member's messages, and a message any of them reads is read for all; they share what the member has viewed in the shared memory; and their sends and writes are the member's. The member's evidence records these calls with `via` set to `"bridge"`.

    A bridged member never counts as waiting, since one call in `swarm_wait` does not show that its CLI is idle: its waits end on news, a member's end, the swarm's stop or their timeout, never because every member is waiting. The CLI's MCP tool timeout must be longer than the longest wait of the swarm's channels (`max_wait`). Codex's default is 60 seconds, so give a Codex member `codex_cli(config_overrides={"mcp_servers.swarm.tool_timeout_sec": "700"})` or another value above it.

    Args:
        channels: The channels whose tools to give: `"messages"`, `"shared_memory"` or both.

    Returns:
        The tools, as an MCP server named `swarm`.

    Raises:
        ValueError: If `channels` is empty, names a channel twice, or names one other than `"messages"` and `"shared_memory"`.
    """
    names = list(channels)
    unknown = [name for name in names if name not in _CHANNELS]
    if not names or unknown or len(set(names)) < len(names):
        raise ValueError(
            f"swarm_bridged_tools(): channels must name {' or '.join(repr(c) for c in _CHANNELS)}, or both, each once; got {names!r}."
        )
    tools = [
        _relay(name, declaration) for name in names for declaration in _CHANNELS[name]()
    ]
    tools.append(_wait(names))
    return BridgedToolsSpec(name=SERVER, tools=tools)


# A channel's tool for a bridged member: the run's tool of that name, called through the
# bridge as the member.
def _relay(channel: str, declaration: ToolDeclaration) -> Tool:
    async def execute(**arguments: Any) -> str:
        bus = _bus(declaration.name)
        state = _state(bus, channel)
        if state is None:
            raise ToolError(
                f"The swarm has no {channel} channel, so {declaration.name} cannot be used."
            )
        tool = next(
            t for t in bus.states[state].tools() if ToolDef(t).name == declaration.name
        )
        token = current_via.set("bridge")
        try:
            return str(await tool(**arguments))
        finally:
            current_via.reset(token)

    return ToolDef(
        execute,
        name=declaration.name,
        description=declaration.description,
        parameters=declaration.parameters,
        max_output=declaration.max_output,
    ).as_tool()


# swarm_wait for a bridged member, waiting on the given channels the swarm has
def _wait(channels: Sequence[str]) -> Tool:
    async def wait(path: str | None, file_id: str | None, timeout: int | None) -> str:
        bus = _bus("swarm_wait")
        states = [s for s in (_state(bus, c) for c in channels) if s is not None]
        if not states:
            raise ToolError(
                f"The swarm has no {' or '.join(channels)} channel, so swarm_wait cannot be used."
            )
        token = current_via.set("bridge")
        try:
            return await bus.wait(path, file_id, timeout, states)
        finally:
            current_via.reset(token)

    return wait_tool(
        wait,
        messages="messages" in channels,
        board="shared_memory" in channels,
        timeout="Seconds to wait at most (default and maximum: the longest wait of the swarm's channels).",
    )


def _bus(tool: str) -> Bus:
    bus = current_bus.get()
    context = bus.member() if bus is not None else None
    if bus is None or context is None:
        raise ToolError(f"{tool} can only be used by a member of a swarm.")
    # a native member's bridged subagent would be a second reader of its inbox
    if context.loop is not None:
        raise ToolError(
            f"{tool} is for a bridged member; {context.name} uses the swarm's tools from swarm_tools()."
        )
    return bus


# the name of the run's state of a built-in channel, by its state's type, which a
# custom channel cannot take; None when the swarm does not have it
def _state(bus: Bus, channel: str) -> str | None:
    if channel == "shared_memory":
        return builtin_board(bus.states)
    return next(
        (name for name, s in bus.states.items() if isinstance(s, MessagesState)), None
    )
