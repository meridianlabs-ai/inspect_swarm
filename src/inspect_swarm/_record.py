from typing import Literal

from inspect_ai.util import store
from pydantic import BaseModel

MemberState = Literal["pending", "running", "ended"]
"""A member's lifecycle: not started, running, or ended."""

RECORD_KEY = "inspect_swarm:result"


class MemberResult(BaseModel):
    """One member in the swarm's result record."""

    name: str
    """The member's roster name."""

    role: str | None
    """The member's role, if it has one."""

    state: MemberState
    """The member's lifecycle state."""

    status: str | None
    """How the member ended, once it has; `None` before."""

    submitted: bool
    """Whether the member ended with a submission (status `submitted`)."""

    time: float | None
    """Swarm time (seconds from the swarm's start) when the member ended; `None` until then."""


class FinalRecord(BaseModel):
    """The swarm's final answer in its result record."""

    mode: str | None
    """The mode that chose the answer: `first`. `None` when there is no answer."""

    member: str | None
    """The member whose output is the answer. `None` when there is no answer."""

    reason: str | None
    """Why there is no answer: `no_submissions`. `None` when there is one."""

    provisional: bool
    """`True` until the swarm has finalised its answer."""


class SwarmResultRecord(BaseModel):
    """What the swarm did in a sample: each member's result and the final answer, kept in the sample store under `inspect_swarm:result`."""

    version: Literal[1] = 1
    """The record's format version."""

    members: list[MemberResult]
    """The members, in roster order."""

    final: FinalRecord
    """The final answer."""


# Only the runtime writes the record.
def write_record(record: SwarmResultRecord) -> None:
    store().set(RECORD_KEY, record.model_dump(mode="json"))
