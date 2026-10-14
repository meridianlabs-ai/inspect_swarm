from typing import Protocol


class SwarmControl(Protocol):
    """What a controller's `run` works through: the swarm's members and their events, for one sample."""
