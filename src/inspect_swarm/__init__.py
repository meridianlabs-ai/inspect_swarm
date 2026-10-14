"""Evals with multi-agent swarms.

Experimental and under development; the API may change.
"""

from ._budget import Budget
from ._channel._channel import Channel, swarm_channel
from ._controller._control import SwarmControl
from ._controller._controller import (
    Controller,
    MemberInfo,
    StopReason,
    swarm_controller,
)
from ._member import Member, member
from ._swarm import swarm

try:
    from ._version import __version__
except ImportError:
    __version__ = "unknown"


__all__ = [
    "Budget",
    "Channel",
    "Controller",
    "Member",
    "MemberInfo",
    "StopReason",
    "SwarmControl",
    "__version__",
    "member",
    "swarm",
    "swarm_channel",
    "swarm_controller",
]
