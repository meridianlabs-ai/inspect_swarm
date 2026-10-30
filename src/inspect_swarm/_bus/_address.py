import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from inspect_ai.tool import ToolError

if TYPE_CHECKING:
    from .._controller._control import MemberHandle

# the addressing grammar of member names, which appear in fence attributes, status
# lines and wait results as they are
MEMBER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}")

# every other running member
ALL = "all"

# addresses a member name may not take: `all`, and `swarm`, the sender of the
# swarm's own records (later); `role:<role>` is outside the grammar
RESERVED = (ALL, "swarm")


# The member names a record's addresses resolve to at send time, in the order first
# addressed, with duplicates collapsed: a member name, or `all` for every running
# member but the sender. An unaddressed record resolves to nobody.
def resolve(
    sender: str, to: Sequence[str], members: Mapping[str, "MemberHandle"]
) -> tuple[str, ...]:
    recipients: list[str] = []
    for address in to:
        if address == ALL and ALL not in members:
            others = [
                name
                for name, m in members.items()
                if name != sender and m.state == "running"
            ]
            if not others:
                raise ToolError("all: no other member is running.")
            names = others
        else:
            m = members.get(address)
            if m is None:
                # `all` is the broadcast only when no member takes the name
                broadcast = (
                    ""
                    if ALL in members
                    else f", and {ALL} addresses every other running member"
                )
                raise ToolError(
                    f"{address!r} is not a member of the swarm; the members are {', '.join(members)}{broadcast}."
                )
            if address == sender:
                raise ToolError(f"{address} is you; you cannot send to yourself.")
            if m.state == "pending":
                raise ToolError(f"{address} has not started.")
            if m.state == "ended":
                raise ToolError(f"{address} has ended.")
            names = [address]
        recipients.extend(name for name in names if name not in recipients)
    return tuple(recipients)
