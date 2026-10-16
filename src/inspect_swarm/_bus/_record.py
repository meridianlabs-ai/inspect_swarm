from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue


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

    op: Mapping[str, JsonValue] | None
    """The channel's operation data, if any."""

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
