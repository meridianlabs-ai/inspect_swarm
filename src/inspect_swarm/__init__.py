"""Evals with multi-agent swarms.

Everything in this package is experimental and may change in any release without notice, including `swarm()`, `member()`, `Budget`, the built-in controller and channel names, and the record and evidence formats. No support is guaranteed.
"""

from ._budget import Budget, BudgetUsage, SwarmBudget, swarm_budget
from ._bus._notice import swarm_on_continue
from ._bus._record import Record
from ._bus._tools import swarm_tools
from ._channel._channel import (
    Channel,
    ChannelRun,
    ChannelState,
    NoticeLine,
    WaitingState,
    WaitNews,
    WaitSpec,
    swarm_channel,
)
from ._channel.filesystem import filesystem
from ._channel.shared_memory import shared_memory
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
from ._final import FinalName
from ._member import Member, member
from ._swarm import swarm
from ._tools import swarm_resources

try:
    from ._version import __version__
except ImportError:
    __version__ = "unknown"


__all__ = [
    "Budget",
    "BudgetUsage",
    "Channel",
    "ChannelRun",
    "ChannelState",
    "Controller",
    "FinalName",
    "Member",
    "MemberEnded",
    "MemberHandle",
    "MemberInfo",
    "MemberState",
    "NoticeLine",
    "Record",
    "StopReason",
    "SwarmBudget",
    "SwarmControl",
    "SwarmEvent",
    "WaitNews",
    "WaitSpec",
    "WaitingState",
    "__version__",
    "filesystem",
    "leaderless",
    "member",
    "shared_memory",
    "swarm",
    "swarm_budget",
    "swarm_channel",
    "swarm_controller",
    "swarm_on_continue",
    "swarm_resources",
    "swarm_tools",
]
