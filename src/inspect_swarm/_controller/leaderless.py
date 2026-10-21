from contextlib import aclosing

from ._control import SwarmControl
from ._controller import Controller, StopReason, swarm_controller


@swarm_controller
def leaderless(stop_on_submission: bool = True) -> Controller:
    """Start every member with the swarm's input; stop at the first submission, or when all have ended.

    By default the swarm stops when the first member submits. The swarm's answer is the first submission, so nothing the other members do after it can change the answer. The members still running are stopped: they finish the calls they have in flight, within `Budget.grace`, and the ones still running after it are cancelled.

    Pass `stop_on_submission=False` to let every member run to its end when the other members' work still matters: tasks whose scorer reads a shared artifact (such as the sandbox) after the members end, where members still working change what is scored; arms that measure each member's result (team@k); and agents for which any normal return counts as a submission, so that a member that gives up would end the swarm.

    Args:
        stop_on_submission: Stop the swarm when the first member ends with a submission.

    Returns:
        The controller, which returns the stop reason `submitted` when a member's submission stopped the swarm, and `all_done` when every member ended.
    """

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if stop_on_submission and event.submitted:
                    return "submitted"
        return "all_done"

    return Controller(run)
