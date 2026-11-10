"""Evals with multi-agent swarms.

Everything in this package is experimental and may change in any release without notice, including `swarm()`, `member()`, `Budget`, the built-in controller and channel names, and the record and evidence formats. No support is guaranteed.
"""

from ._budget import Budget, BudgetUsage, SwarmBudget, swarm_budget
from ._bus._bridged import swarm_bridged_tools
from ._bus._bus import Bus, Caller, Delivery, calling_member
from ._bus._record import Draft, Record
from ._bus._tools import swarm_tools
from ._channel._channel import (
    Channel,
    ChannelRun,
    ChannelState,
    WaitingState,
    WaitNews,
    WaitSpec,
    swarm_channel,
)
from ._channel.filesystem import filesystem
from ._channel.messages import messages
from ._channel.shared_memory import shared_memory
from ._controller._control import (
    ControlEvent,
    MemberEnded,
    MemberHandle,
    MemberIdle,
    MemberNews,
    MemberState,
    SwarmControl,
    VerdictSummary,
)
from ._controller._controller import (
    Controller,
    MemberInfo,
    StopReason,
    swarm_controller,
)
from ._controller.leaderless import leaderless
from ._final import FinalName
from ._isolation import Share, UserIsolation, users
from ._member import Member, member
from ._record import (
    FinalRecord,
    LeftOut,
    MemberResult,
    SwarmResultRecord,
    swarm_result,
)
from ._result import (
    MemberArtifacts,
    ResultKind,
    ResultSpec,
    Verdict,
    Verifier,
    answer_result,
    artifact_result,
)
from ._seam import Boundary, BoundaryCallback
from ._swarm import baseline, swarm
from ._tools import swarm_resources

try:
    from ._version import __version__
except ImportError:
    __version__ = "unknown"


__all__ = [
    "Boundary",
    "BoundaryCallback",
    "Budget",
    "BudgetUsage",
    "Bus",
    "Caller",
    "Channel",
    "ChannelRun",
    "ChannelState",
    "ControlEvent",
    "Controller",
    "Delivery",
    "Draft",
    "FinalName",
    "FinalRecord",
    "LeftOut",
    "Member",
    "MemberArtifacts",
    "MemberEnded",
    "MemberHandle",
    "MemberIdle",
    "MemberInfo",
    "MemberNews",
    "MemberResult",
    "MemberState",
    "Record",
    "ResultKind",
    "ResultSpec",
    "Share",
    "StopReason",
    "SwarmBudget",
    "SwarmControl",
    "SwarmResultRecord",
    "UserIsolation",
    "Verdict",
    "VerdictSummary",
    "Verifier",
    "WaitNews",
    "WaitSpec",
    "WaitingState",
    "__version__",
    "answer_result",
    "artifact_result",
    "baseline",
    "calling_member",
    "filesystem",
    "leaderless",
    "member",
    "messages",
    "shared_memory",
    "swarm",
    "swarm_bridged_tools",
    "swarm_budget",
    "swarm_channel",
    "swarm_controller",
    "swarm_resources",
    "swarm_result",
    "swarm_tools",
    "users",
]
