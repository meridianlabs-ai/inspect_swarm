import copy
from typing import Literal

from inspect_ai.agent import AgentContinue, AgentState
from inspect_ai.model import ChatMessageUser
from inspect_ai.util._span import current_agent_span_id

from .._budget import MemberContext
from .._evidence import comm_notified
from ._bus import Bus, current_bus


def swarm_on_continue(
    inner: str | AgentContinue | None = None,
    *,
    no_tool_calls: Literal["continue", "stop"] = "continue",
) -> AgentContinue:
    """Notices for a swarm member, as its agent's `on_continue` hook.

    Pass it as the member's hook, `react(on_continue=swarm_on_continue())`, or around the hook the member would otherwise have, `swarm_on_continue(inner)`. The member then continues or stops exactly as it would without it. At each of its turn boundaries, unless it is stopping, a member in `notify` mode gets a notice of what changed in the swarm's channels since its last notice, such as shared memory files other agents wrote. A notice is a user message with fixed text: counts, ids such as `f-3` and member names, never what a member wrote.

    Only the member's own loop gets notices, not its subagents. Outside a swarm, or for a member in `poll` mode, it adds nothing.

    Args:
        inner: The hook the member would otherwise have. A string continues the member with that message after a turn without tool calls, as it does as `on_continue` of `react()` with a submit tool. A function is called and its result kept.
        no_tool_calls: Without `inner`, what the member does after a turn without tool calls, which must be what its agent does with no hook: `"continue"` for `react()` with a submit tool and for `deepagent()`, `"stop"` for `react(submit=False)`. Ignored when `inner` is given.

    Returns:
        The hook.
    """

    async def continuation(state: AgentState) -> bool | str | AgentState:
        # what the member would do without the wrapper
        if inner is None:
            return bool(state.output.message.tool_calls) or no_tool_calls == "continue"
        if isinstance(inner, str):
            return True if state.output.message.tool_calls else inner
        return await inner(state)

    async def execute(state: AgentState) -> bool | str | AgentState:
        bus = current_bus.get()
        context = bus.member() if bus is not None else None
        if bus is None or context is None or bus.nested(context):
            return await continuation(state)
        # the member's own loop has the hook, whether it goes on, stops or the inner
        # hook raises
        context.hooked = True
        result = await continuation(state)
        # a pending notice does not keep a member going that would have stopped
        if result is not False:
            _notice(bus, context, result if isinstance(result, AgentState) else state)
        return result

    return execute


# Appends the member's notice, if it has one, to the conversation its loop goes on with.
def _notice(bus: Bus, context: MemberContext, state: AgentState) -> None:
    # a loop that has not resolved swarm_tools() cannot act on a notice
    if context.delivery_effective != "notify" or context.loop is None:
        return
    # the ids as they are now, which the channel may change later
    lines = [
        (channel, line.text, copy.deepcopy(dict(line.ids)))
        for channel, channel_state in bus.states.items()
        for line in channel_state.notice_lines(context.name)
    ]
    if not lines:
        return
    notice = bus.notice_id()
    message = ChatMessageUser(
        content="\n".join(text for _, text, _ in lines),
        metadata={
            "inspect_swarm": {
                "version": 1,
                "type": "notice",
                "id": notice,
                "lines": [
                    {"channel": channel, "ids": ids} for channel, _, ids in lines
                ],
            }
        },
    )
    state.messages.append(message)
    loop = current_agent_span_id()
    for channel, _, ids in lines:
        comm_notified(
            context.span_id, context.name, channel, notice, message.id, loop, ids
        )
