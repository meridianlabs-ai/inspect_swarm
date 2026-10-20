from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Callable,
    ClassVar,
    Literal,
    ParamSpec,
    Protocol,
    overload,
)

from inspect_ai.tool import Tool
from pydantic import JsonValue

from .._bus._record import Record
from .._bus._storm import StormControls
from .._controller._controller import MemberInfo
from .._registry import register_factory

if TYPE_CHECKING:
    from .._bus._bus import Bus

P = ParamSpec("P")


class Channel:
    """A sanctioned way for members to share information.

    Subclass it and override `instructions()`, and return an instance from a factory decorated with `@swarm_channel`. A `Channel` holds configuration only: one object serves every sample.

    A channel with tools and records also sets `name` and `prefix` and overrides `open()`, which gives each run of the swarm its own `ChannelState`. A channel that only adds instructions, such as `filesystem()`, leaves them unset.
    """

    name: ClassVar[str | None] = None
    """The name of the channel's records (`Record.channel`), or `None` for a channel with no records. No two channels of a swarm may share it."""

    prefix: ClassVar[str | None] = None
    """The prefix of the channel's record ids (`"w"` gives `w-1`), set exactly when `name` is. No two channels of a swarm may share it."""

    reach: ClassVar[Literal["loop", "member"]] = "loop"
    """Which of a member's agent loops get the channel's tools from `swarm_tools()`. `"loop"` (the default): only the member's own loop, the first in which it resolves `swarm_tools()`. `"member"`: also the member's nested loops, such as a `deepagent()` member's subagents, which then act as the member."""

    storm: StormControls = StormControls()
    """The storm controls the bus applies to the channel's records. By default there are none."""

    def check(self, roster: Sequence[MemberInfo]) -> None:
        """Check the roster when `swarm()` is called.

        Args:
            roster: The swarm's members.

        Raises:
            ValueError: If the channel cannot serve this roster.
        """
        return None

    def open(self, run: "ChannelRun") -> "ChannelState | None":
        """Open the channel's state for one run of the swarm, before any member starts.

        Args:
            run: The run's bus and roster.

        Returns:
            The run's state, or `None` (the default) for a channel with no tools or records.
        """
        return None

    def instructions(self, member: MemberInfo) -> str | None:
        """Text for the member's preamble.

        Args:
            member: The member the text is for.

        Returns:
            The text, or `None` to add nothing.
        """
        return None


@dataclass(frozen=True)
class ChannelRun:
    """What `Channel.open()` gets for one run of the swarm."""

    bus: "Bus"
    """The run's bus, which the channel's tools deliver records through."""

    roster: Sequence[MemberInfo]
    """The swarm's members."""


class ChannelState(Protocol):
    """A channel's state in one run of the swarm, which only the bus changes.

    The bus calls `validate()`, `admit()` and `apply()` with no `await` between them, so `admit()` sees the state that `apply()` changes.
    """

    def tools(self) -> list[Tool]:
        """The channel's tools, which deliver records through the run's bus."""
        ...

    def validate(self, record: Record) -> None:
        """Check a record's payload and operation data, before the record gets an id.

        Args:
            record: The record.

        Raises:
            ToolError: If the record is malformed. No evidence is written.
        """
        ...

    def admit(self, record: Record) -> None:
        """Check a record against the state as it is now, after the storm controls.

        Args:
            record: The record.

        Raises:
            ToolError: If the state refuses the record. The refusal is in the record's `sent` evidence.
        """
        ...

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        """Apply an admitted record to the state. It cannot fail.

        Args:
            record: The record.

        Returns:
            The members to notify, and a JSON mapping for the record's `delivered` evidence, or `None`.
        """
        ...


@overload
def swarm_channel(func: Callable[P, Channel], /) -> Callable[P, Channel]: ...


@overload
def swarm_channel(
    *, name: str | None = None
) -> Callable[[Callable[P, Channel]], Callable[P, Channel]]: ...


def swarm_channel(
    func: Callable[P, Channel] | None = None, /, *, name: str | None = None
) -> Callable[P, Channel] | Callable[[Callable[P, Channel]], Callable[P, Channel]]:
    """Decorator for registering swarm channels.

    The factory's arguments are recorded with the channel, so they appear in the log and the channel can be rebuilt from it. A factory in a package is registered as `<package>/<name>`.

    Args:
        func: A function returning a `Channel`.
        name: The registered name, in place of the function's name.

    Returns:
        The factory, registered under the `swarm_channel` type.

    Raises:
        TypeError: When the factory is called, if it does not return a `Channel`.
    """
    if func is not None:
        return register_factory("swarm_channel", Channel, func, None)

    def decorate(func: Callable[P, Channel]) -> Callable[P, Channel]:
        return register_factory("swarm_channel", Channel, func, name)

    return decorate
