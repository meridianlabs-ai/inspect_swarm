"""Evals with multi-agent swarms.

Experimental and under development; the API may change.
"""

from ._budget import Budget
from ._bus._record import Record
from ._channel._channel import Channel, ChannelRun, ChannelState, swarm_channel
from ._channel.filesystem import filesystem
from ._controller._control import (
    MemberEnded,
    MemberHandle,
    MemberState,
    SwarmControl,
    SwarmEvent,
)
from ._controller._controller import (
    Controller,
    MemberInfo,
    StopReason,
    swarm_controller,
)
from ._controller.leaderless import leaderless
from ._member import Member, member
from ._swarm import swarm

try:
    from ._version import __version__
except ImportError:
    __version__ = "unknown"


__all__ = [
    "Budget",
    "Channel",
    "ChannelRun",
    "ChannelState",
    "Controller",
    "Member",
    "MemberEnded",
    "MemberHandle",
    "MemberInfo",
    "MemberState",
    "Record",
    "StopReason",
    "SwarmControl",
    "SwarmEvent",
    "__version__",
    "filesystem",
    "leaderless",
    "member",
    "swarm",
    "swarm_channel",
    "swarm_controller",
]
