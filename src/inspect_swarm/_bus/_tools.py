from inspect_ai.agent import AgentChannel
from inspect_ai.agent._channel import current_agent_channel
from inspect_ai.log._samples import sample_active
from inspect_ai.tool import Tool, ToolSource

from ._bus import current_bus


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
        own = context.loop is loop
        tools = [
            tool
            for name, state in bus.states.items()
            if own or bus.reach[name] == "member"
            for tool in state.tools()
        ]
        # swarm_wait, when a channel the loop reaches wakes waits
        wait = bus.waits.tool()
        if wait is not None and bus.waits.reaches(own):
            tools.append(wait)
        return tools


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
