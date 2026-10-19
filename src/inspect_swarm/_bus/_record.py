from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

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

    A channel's tool hands the bus a `Draft`; the bus makes the record, binding the sender and resolving the addresses, and gives it to the policy and the channel.
    """

    id: str
    """The record's id, the channel's prefix and a per-run number (`"w-14"`). Ids are given before the policy runs, so they are attempt ids: the order records changed channel state in is their commit sequence number, `seq` in the `delivered` evidence."""

    channel: str
    """The name of the channel the record is for."""

    kind: str
    """What the record does, in the channel's terms (`"message"`, `"create"`)."""

    origin: Literal["member", "swarm"]
    """Who made it: a member, or the swarm itself."""

    sender: str
    """The sending member's name, bound by the runtime, never taken from model output; `"swarm"` for the swarm's own records."""

    to: tuple[str, ...]
    """The addresses as written. `()` for an unaddressed record, such as a write to shared state, which reaches nobody directly."""

    recipients: tuple[str, ...]
    """The member names the addresses resolved to."""

    routed_out: tuple[str, ...]
    """Resolved names the channel's routes dropped."""

    payload: str
    """The text the record carries. A policy's `modify` replaces it."""

    op: Mapping[str, FrozenJson] | None
    """The channel's operation data, if any, as JSON that cannot be changed: objects are read-only mappings and arrays are tuples. A record is shared by the policy, the channel and the bus's history, so none of them can change what the others see."""

    wake: bool
    """Whether the record ends a recipient's wait."""

    reply_to: str | None
    """The id of a record this one answers."""

    via: Literal["tool", "bridge", "internal"]
    """How the record reached the bus."""

    sender_span_id: str | None
    """The sender's agent span."""

    tool_span_id: str | None
    """The tool span the call ran in."""


# What a channel's tool hands to `Bus.deliver()`, which makes the `Record`: the
# channel's name, the record's kind, member names to deliver to (`()` for an unaddressed
# record), the payload and the channel's operation data.
@dataclass(frozen=True)
class Draft:
    channel: str
    kind: str
    to: tuple[str, ...] = ()
    payload: str = ""
    op: Mapping[str, JsonValue] | None = None


# A record's operation data, detached from the draft's and read-only at every depth. A
# string or key UTF-8 cannot encode raises `UnicodeEncodeError`: the log would not
# keep it as sent. A container nested more than `max_depth` levels deep, counting the
# outermost as 1, raises `ValueError` before anything inside it is visited.
def freeze_json(value: object, max_depth: int) -> FrozenJson:
    def freeze(value: object, depth: int) -> FrozenJson:
        if isinstance(value, Mapping | list | tuple) and depth > max_depth:
            raise ValueError(f"nested more than {max_depth} levels deep")
        if isinstance(value, Mapping):
            items = cast(Mapping[object, object], value).items()
            frozen: dict[str, FrozenJson] = {}
            for key, item in items:
                if not isinstance(key, str):
                    raise TypeError(
                        f"A record's op must be JSON data; it has the key {key!r}."
                    )
                key.encode("utf-8")
                frozen[key] = freeze(item, depth + 1)
            return MappingProxyType(frozen)
        if isinstance(value, list | tuple):
            return tuple(
                freeze(item, depth + 1) for item in cast(Sequence[object], value)
            )
        if isinstance(value, str):
            value.encode("utf-8")
            return value
        if value is None or isinstance(value, int | float | bool):
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
