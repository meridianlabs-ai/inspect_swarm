from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias, cast

from pydantic import JsonValue

FrozenJson: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | tuple["FrozenJson", ...]
    | Mapping[str, "FrozenJson"]
)
"""JSON data that cannot be changed: objects are read-only mappings and arrays are tuples."""


@dataclass(frozen=True)
class Record:
    """One communication through the bus: a message, a write, a claim.

    A channel's tool hands the bus a `Draft`; the bus makes the record, binding the sender and resolving the addresses, and gives it to the channel.
    """

    id: str
    """The record's id, the channel's prefix and a per-channel number (`"w-14"`). Every attempt that passes the bus's first checks gets one, refused or not, so the ids of delivered records can skip numbers; `seq` in the `delivered` evidence numbers what the run delivered, across its channels."""

    channel: str
    """The name of the channel the record is for."""

    kind: str
    """What the record does, in the channel's terms (`"message"`, `"create"`)."""

    sender: str
    """The sending member's name, bound by the runtime, never taken from model output."""

    to: tuple[str, ...]
    """The addresses as written. `()` for an unaddressed record, such as a write to shared state, which reaches nobody directly."""

    recipients: tuple[str, ...]
    """The member names the addresses resolved to."""

    routed_out: tuple[str, ...]
    """Resolved names the channel's routes dropped."""

    payload: str
    """The text the record carries."""

    op: Mapping[str, FrozenJson] | None
    """The channel's operation data, if any, as JSON that cannot be changed: objects are read-only mappings and arrays are tuples. A record is shared by the channel and the sender's tool, so neither can change what the other sees."""

    wake: bool
    """Whether the record can end a member's wait (`swarm_wait`)."""

    reply_to: str | None
    """The id of a record this one answers."""

    sender_span_id: str | None
    """The sender's agent span."""

    tool_span_id: str | None
    """The tool span the call ran in."""


# What a channel's tool hands to `Bus.deliver()`, which makes the `Record`: the
# channel's name, the record's kind, member names to deliver to (`()` for an unaddressed
# record), the payload, the channel's operation data, and whether the record can end a
# wait.
@dataclass(frozen=True)
class Draft:
    channel: str
    kind: str
    to: tuple[str, ...] = ()
    payload: str = ""
    op: dict[str, JsonValue] | None = None
    wake: bool = False


# A record's operation data, detached from the draft's and read-only at every depth.
# Only plain dicts and lists are taken: a channel's tool builds the op, so any other
# container is the channel's bug. A container nested more than `max_depth` levels
# deep, counting the outermost as 1, raises `ValueError` before anything inside it is
# visited.
def freeze_json(value: object, max_depth: int) -> FrozenJson:
    def freeze(value: object, depth: int) -> FrozenJson:
        if isinstance(value, dict | list) and depth > max_depth:
            raise ValueError(f"nested more than {max_depth} levels deep")
        if isinstance(value, dict):
            frozen: dict[str, FrozenJson] = {}
            for key, item in cast(dict[object, object], value).items():
                if not isinstance(key, str):
                    raise TypeError(
                        f"A record's op must be JSON data; it has the key {key!r}."
                    )
                frozen[key] = freeze(item, depth + 1)
            return MappingProxyType(frozen)
        if isinstance(value, list):
            return tuple(freeze(item, depth + 1) for item in cast(list[object], value))
        if value is None or isinstance(value, str | int | float | bool):
            return value
        raise TypeError(
            f"A record's op must be JSON data; it holds a {type(value).__name__}."
        )

    return freeze(value, 1)


# Operation data as plain JSON again, for evidence.
def thaw_json(value: FrozenJson) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value
