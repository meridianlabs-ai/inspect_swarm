from inspect_ai.agent import AgentChannel
from inspect_ai.agent._channel import current_agent_channel
from inspect_ai.log._samples import sample_active
from inspect_ai.tool import Tool, ToolSource

from .._channel._channel import ChannelState
from .._channel.messages import MessagesState
from ._bus import Bus, current_bus


# Holds no state, so one object serves every member, sample and run: the member, its
# binding and the run's bus are read from ContextVars at each resolution.
class _SwarmTools(ToolSource):
    async def tools(self) -> list[Tool]:
        bus = current_bus.get()
        context = bus.member() if bus is not None else None
        if bus is None or context is None:
            return []
        loop = current_agent_channel()
        # no await from here to the binding, so two loops resolving at once cannot both
        # bind; the member's own loop resolves its tools before it can start a subagent
        if context.loop is None:
            context.loop = loop
            context.acp_bound = _acp_bound(loop)
        return loop_tools(bus, context.loop is loop)


# The tools of a loop of a member: the member's own loop gets every channel's, its other
# loops those of "member"-reach channels; and swarm_wait, when a channel the loop reaches
# wakes waits. Each counts the member's unread messages, as the bus wraps it.
def loop_tools(bus: Bus, own: bool) -> list[Tool]:
    tools = [
        bus.counting(tool, results=not _counts_itself(state, tool))
        for name, state in bus.states.items()
        if own or bus.reach[name] == "member"
        for tool in state.tools()
    ]
    wait = bus.waits.tool(own)
    if wait is not None:
        # a wait's result counts unread messages itself
        tools.append(bus.counting(wait, results=False))
    return tools


def _counts_itself(state: ChannelState, tool: Tool) -> bool:
    return isinstance(state, MessagesState) and tool is state.read_tool


def swarm_tools() -> ToolSource:
    """The tools of the swarm's channels, for a member's agent.

    Pass it in the member's tools, `react(tools=[bash(), swarm_tools()])`. The member's own loop gets the tools of every channel of the swarm that has them; its nested loops, such as a `deepagent()` member's subagents, get only those of channels whose `reach` is `"member"`. A loop also gets `swarm_wait`, which waits for other members' work, when one of the channels it reaches wakes waits, as `shared_memory()` does. The member's own loop is the first in which the tools are resolved, which for `react()` and `deepagent()` members is their top-level loop. Each tool acts as the member, whichever loop calls it.

    The same member definition serves swarms with different channels, and one with none. Outside a swarm it gives no tools.

    Returns:
        A tool source.
    """
    return _SwarmTools()


def _acp_bound(loop: AgentChannel) -> bool:
    # whether the sample's ACP session drives this loop, so that an operator's
    # messages in the log can be attributed to the member
    active = sample_active()
    transport = active.acp_transport if active is not None else None
    ref = transport.ref if transport is not None else None
    return ref is not None and ref._channel is loop  # pyright: ignore[reportPrivateUsage]
