from ._control import SwarmControl
from ._controller import Controller, StopReason, swarm_controller


@swarm_controller
def leaderless() -> Controller:
    """Start every member with the swarm's input; stop when all have ended.

    Returns:
        The controller, which returns the stop reason `all_done`.
    """

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async for _ in swarm.events():
            pass
        return "all_done"

    return Controller(run)
