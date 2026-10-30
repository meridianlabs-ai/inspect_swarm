"""Evals with multi-agent swarms.

Everything in this package is experimental and may change in any release without notice, including `swarm()`, `member()`, `Budget`, the built-in controller and channel names, and the record and evidence formats. No support is guaranteed.
"""

from ._budget import Budget, BudgetUsage, SwarmBudget, swarm_budget
from ._bus._record import Record
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
    MemberEnded,
    MemberHandle,
    MemberState,
    SwarmControl,
    SwarmEvent,
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
    "FinalRecord",
    "LeftOut",
    "Member",
    "MemberArtifacts",
    "MemberEnded",
    "MemberHandle",
    "MemberInfo",
    "MemberResult",
    "MemberState",
    "Record",
    "ResultKind",
    "ResultSpec",
    "StopReason",
    "SwarmBudget",
    "SwarmControl",
    "SwarmEvent",
    "SwarmResultRecord",
    "Verdict",
    "VerdictSummary",
    "Verifier",
    "WaitNews",
    "WaitSpec",
    "WaitingState",
    "__version__",
    "answer_result",
    "artifact_result",
    "filesystem",
    "leaderless",
    "member",
    "messages",
    "shared_memory",
    "swarm",
    "swarm_budget",
    "swarm_channel",
    "swarm_controller",
    "swarm_resources",
    "swarm_result",
    "swarm_tools",
]
