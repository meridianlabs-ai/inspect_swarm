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
    """Which of a member's agent loops get the channel's tools from `swarm_tools()`. `"loop"` (the default): only the member's own loop, the first in which it resolves `swarm_tools()`. `"member"`: also any nested loop the member's agent gives `swarm_tools()`, such as a `deepagent()` member's `general()` subagents, which then act as the member. Which nested loops get `swarm_tools()` is the agent's choice; `reach` filters the channel tools they get and which channels can wake their `swarm_wait`. A channel whose reads consume something for the member, such as an inbox that marks messages read, keeps `"loop"` so that it has one reader."""

    storm: StormControls = StormControls()
    """The storm controls the bus applies to the channel's records. By default there are none."""

    max_wait: int | None = None
    """This channel's contribution to the longest `swarm_wait`, in seconds, or `None` (the default) for a channel whose records wake no waits. A channel that sets it opens a state that implements `WaitingState`. The bus's effective maximum is the largest `max_wait` of the run's open channels that set it."""

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


@dataclass(frozen=True)
class NoticeLine:
    """A line of a member's notice, which `swarm_on_continue()` adds at the member's turn boundary."""

    text: str
    """The line: a fixed template whose only variables are counts, swarm-assigned ids and roster names, never anything a member wrote or chose."""

    ids: Mapping[str, JsonValue]
    """The swarm ids the line names, such as `{"files": [{"file": "f-2", "version": 3, "record": "w-7"}]}`, given as `ids` in the notice's metadata and in its `notified` evidence."""


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

    def notice_lines(self, member: str) -> list[NoticeLine]:
        """The lines of the member's next notice, which they then cover.

        Called at the member's turn boundary, only when the notice will be added, so a line covers what it names: the next call leaves it out until it changes again.

        Args:
            member: The member the notice is for.

        Returns:
            The lines, or `[]` when there is nothing new for the member.
        """
        ...


@dataclass(frozen=True)
class WaitSpec:
    """One call of `swarm_wait`, as the bus asks a `WaitingState` about it."""

    id: str
    """The wait's id (`"x-4"`), numbered per run."""

    member: str
    """The waiting member's name."""

    loop: str | None
    """The agent span of the loop that waits."""

    own: bool
    """Whether that loop is the member's own, not one of its subagents'."""

    path: str | None
    """The shared memory's path filter as the model gave it, or `None`. Later channels add filters of their own."""

    file_id: str | None = None
    """The shared memory's file id filter as the model gave it, or `None`. With neither filter, the wait watches the whole board."""


@dataclass(frozen=True)
class WaitNews:
    """What a channel has for a waiting loop to look at."""

    lines: list[str]
    """The channel's lines for the wait's result. Fixed templates with ids and member names, never content."""

    files: list[dict[str, JsonValue]]
    """What `lines` name, for the `waited` evidence."""


class WaitingState(ChannelState, Protocol):
    """The state of a channel whose records wake waits (one that sets `Channel.max_wait`).

    The bus calls these methods with no `await` around them. None of them marks anything: a loop acknowledges what a wait names by reading it with the channel's own tool.
    """

    def wait_check(self, spec: WaitSpec) -> None:
        """Check a wait's filter when the wait is called.

        Args:
            spec: The wait.

        Raises:
            ToolError: If the filter is malformed.
        """
        ...

    def wakes(self, spec: WaitSpec, record: Record) -> bool:
        """Whether a record the channel has just applied made something reportable to a wait.

        Args:
            spec: The wait.
            record: The applied record, which has `wake` set.

        Returns:
            Whether the wait should be released.
        """
        ...

    def news(self, spec: WaitSpec) -> WaitNews | None:
        """What is reportable to a wait now.

        Args:
            spec: The wait.

        Returns:
            The lines and what they name, or `None` when nothing matching the wait's filter is reportable.
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
