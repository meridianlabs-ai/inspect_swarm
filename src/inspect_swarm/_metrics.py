from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from ._ledger import CallRecord, EventLog, SpanPlaces, SwarmLedger


# One member's metrics.
class MemberMetrics(BaseModel):
    name: str
    # the member's meter, from its ledger line
    tokens: int
    cost: float
    # seconds from the member's start to its end; None for a member never started
    running_time: float | None
    # seconds of the swarm's wall clock the member was not running: before its start
    # and after its end
    idle_time: float
    # seconds the member's completed model calls waited before their last attempt began:
    # for a connection, and for earlier failed attempts and their backoff, which Inspect
    # also counts as waiting. Summed over calls, so concurrent calls each count, and
    # never merged with other members' waits as the sample's waiting time is.
    model_wait_time: float
    # harness validity: the longest run of consecutive tool calls in the member's spans
    # with the same function and arguments. A signal, with no threshold. None for a
    # bridged member, whose tool calls leave no tool events.
    longest_identical_tool_run: int | None


# The swarm's metrics for one run, written once, when the swarm ends, as an InfoEvent
# and a store entry. Tokens and cost are the ledger's; its claims say whether they
# are complete.
class SwarmMetrics(BaseModel):
    version: int = 1
    # the swarm meter: members, runtime and final step
    tokens: int
    cost: float
    # seconds from the swarm's start to its end
    wall_clock: float
    # in roster order
    members: list[MemberMetrics]
    # harness validity: members with no completed model call in their spans, in roster
    # order
    never_acted: list[str]


# When a member ran, in swarm time (seconds from the swarm's start).
@dataclass(frozen=True)
class MemberTimes:
    name: str
    # None for a member never started
    started: float | None
    # None for a member that has not ended, as when its task was cancelled before it ran
    ended: float | None


def swarm_metrics(
    ledger: SwarmLedger,
    events: EventLog,
    places: SpanPlaces,
    times: Sequence[MemberTimes],
    wall_clock: float,
) -> SwarmMetrics:
    waits = {m.name: 0.0 for m in ledger.members}
    for call in events.calls.values():
        _, member = places(call.span_id)
        wait = _wait(call)
        if member is not None and wait is not None:
            waits[member] += wait
    tool_calls: dict[str, list[tuple[str, str]]] = {m.name: [] for m in ledger.members}
    for tool in events.tools.values():
        _, member = places(tool.span_id)
        if member is not None:
            tool_calls[member].append((tool.function, tool.arguments))

    members: list[MemberMetrics] = []
    for line, member_times in zip(ledger.members, times, strict=True):
        running = (
            None
            if member_times.started is None
            else (member_times.ended if member_times.ended is not None else wall_clock)
            - member_times.started
        )
        members.append(
            MemberMetrics(
                name=line.name,
                tokens=line.meter.usage.total_tokens,
                cost=line.meter.cost,
                running_time=running,
                idle_time=wall_clock - (running or 0.0),
                model_wait_time=waits[line.name],
                longest_identical_tool_run=None
                if line.bridged
                else _longest_run(tool_calls[line.name]),
            )
        )
    return SwarmMetrics(
        tokens=ledger.swarm.usage.total_tokens,
        cost=ledger.swarm.cost,
        wall_clock=wall_clock,
        members=members,
        never_acted=[
            m.name for m in ledger.members if m.calls.calls + m.calls.cache_replays == 0
        ],
    )


# How long a completed call waited before its last attempt began. None for a call that
# did not complete, whose event keeps no start before its wait.
def _wait(call: CallRecord) -> float | None:
    if call.completed is None:
        return None
    return max(0.0, (call.reported - call.timestamp).total_seconds())


def _longest_run(calls: Sequence[tuple[str, str]]) -> int:
    longest = run = 0
    previous: tuple[str, str] | None = None
    for call in calls:
        run = run + 1 if call == previous else 1
        longest = max(longest, run)
        previous = call
    return longest
