from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ._record import Record

if TYPE_CHECKING:
    from .._controller._control import MemberHandle


@dataclass(frozen=True)
class CommDecision:
    """What a policy decides about a record, in inspect_sentinel's action names.

    `continue` delivers the record. `modify` delivers it with `payload` in place of its own, and keeps the original in the evidence. `reject` refuses it: the sender gets a `ToolError` with `message`. `terminate` ends the sample. `escalate` has nobody to escalate to, so the record is delivered with a warning.

    Raises:
        ValueError: If `action` is `modify` and `payload` is `None`.
    """

    action: Literal["continue", "modify", "reject", "escalate", "terminate"]
    """The decision."""

    payload: str | None = None
    """The replacement payload, for `modify`."""

    message: str | None = None
    """What the sender reads, for `reject`, and the reason for `terminate`."""

    explanation: str | None = None
    """Why, for the log only."""

    def __post_init__(self) -> None:
        if self.action == "modify" and self.payload is None:
            raise ValueError("CommDecision: a modify decision needs a payload.")


class SwarmView(Protocol):
    """What a policy sees of the swarm: its members and the records delivered so far."""

    @property
    def members(self) -> Sequence["MemberHandle"]:
        """The members, in roster order, with their states and statuses."""
        ...

    def member(self, name: str) -> "MemberHandle":
        """The member with this name.

        Args:
            name: The member's name.

        Raises:
            KeyError: If no member has this name.
        """
        ...

    @property
    def records(self) -> Sequence[Record]:
        """The records delivered so far, in the order they changed channel state."""
        ...


CommPolicy = Callable[[Record, SwarmView], Awaitable[CommDecision | None]]
"""A policy the bus runs on each record before delivering it: an async function of the record and a view of the swarm, returning a `CommDecision`, or `None` to continue."""
