from collections.abc import Sequence
from dataclasses import dataclass, field

from inspect_ai.scorer import value_to_float
from inspect_ai.scorer._score import _scorers  # pyright: ignore[reportPrivateUsage]
from inspect_ai.scorer._scorer import unique_scorer_name
from pydantic import BaseModel

from ._ledger import EventLog, MemberLedger, SpanPlaces, SwarmLedger

# react()'s default score_value
_score_value = value_to_float()


# One member's communication over one channel with records, from the bus's evidence as
# it was written. The member's descendants act as the member, so their records and
# reads are its own.
class CommMetrics(BaseModel):
    # records delivered (`delivered` events): committed and applied to the channel
    records: int = 0
    # the UTF-8 bytes of the delivered records' payloads
    record_bytes: int = 0
    # attempts the bus rejected (`sent` events with `rejected`), by the control that
    # rejected them; a control that rejected none is left out
    rejections: dict[str, int] = {}
    # results that showed the channel's content (`read` events), and those that showed
    # a listing of it
    reads: int = 0
    listings: int = 0


# One member's metrics.
class MemberMetrics(BaseModel):
    name: str
    # the member's meter, from its ledger line
    tokens: int
    cost: float
    # seconds from the member's start to its end; None for a member never started
    running_time: float | None
    # harness validity: the longest run of consecutive tool calls in the member's spans
    # with the same function and arguments. A signal, with no threshold. None for a
    # bridged member, whose tool calls leave no tool events.
    longest_identical_tool_run: int | None
    # gate checks (`GateChecks`): the in-loop checks of the member's own submissions,
    # those that passed, and the swarm time of the first that passed. Passes assume
    # react()'s default score_value converter, value_to_float(), so a member with its
    # own converter can record passes that differ from its own gate. The defaults load
    # metrics written before these fields.
    gate_checks: int = 0
    gate_passes: int = 0
    first_pass_time: float | None = None
    # communication (`CommMetrics`), by channel name: an entry for each channel the
    # swarm opened with records, in the order given, zeros included, and none for a
    # swarm without one. The default loads metrics written before this field.
    comm: dict[str, CommMetrics] = {}
    # waits (`waited` events), the member's subagents' included: their number, their
    # number by what ended them (an outcome that ended none is left out), their total
    # duration across the member's loops (concurrent waits each count), and their total
    # idle time, the seconds the member counted as waiting (its own loop in a wait not
    # released, none of its background work running). All None in a swarm that offers
    # no wait, so 0 means offered and unused; the defaults load metrics written before
    # these fields.
    waits: int | None = None
    waits_by_outcome: dict[str, int] | None = None
    wait_time: float | None = None
    idle_time: float | None = None


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
    # harness validity, in roster order: in a swarm with channels that have tools,
    # members with a completed model call whose loops never resolved swarm_tools().
    # The default loads metrics written before this field.
    tools_unwired: list[str] = []
    # critical path, the simple definition: a member's steps are the completed model
    # calls in its spans, cache replays included, from its ledger line, so they cover
    # everything it did through its end. Calls a member makes concurrently (parallel
    # subagent tool calls) count one each, so both numbers bound the sequential depth
    # from above.
    # the selected member's steps; None when there is no answer
    steps_to_answer: int | None
    # the largest step count of any member
    longest_member_steps: int


# When a member ran, in swarm time (seconds from the swarm's start).
@dataclass(frozen=True)
class MemberTimes:
    name: str
    # None for a member never started
    started: float | None
    # None for a member that has not ended, as when its task was cancelled before it ran
    ended: float | None


# How a member was wired to the swarm's channels, as the runtime saw it.
@dataclass(frozen=True)
class MemberWiring:
    # swarm_tools() was resolved in one of its loops
    tools: bool


# A member's gate checks: the in-loop checks of its own submissions, as react() makes
# them with `attempts`. A check is an intermediate score event from the task's first
# scorer, the one react() decides on, whose nearest enclosing agent span is the member's
# own, not a subagent's; the task's other scorers' events for the same submission are
# not checks. A pass is a check whose value is 1.0 under Inspect's default
# value_to_float(), react()'s default score_value; a member that sets its own
# score_value is read by the default too, so its passes can differ from its own gate
# (decision: Ransom, 2026-10-20). react() does not score the submission that
# uses up its last attempt, and an agent that checks submissions without score() makes
# no checks.
@dataclass(frozen=True)
class GateChecks:
    checks: int = 0
    passes: int = 0
    # swarm time of the first passing check; None without one
    first_pass_time: float | None = None


# The name score() gives the task's first scorer's events; None outside a task with a
# scorer.
def first_scorer() -> str | None:
    scorers = _scorers.get(None)
    return unique_scorer_name(scorers[0], []) if scorers else None


# The gate checks of the member whose agent span is `member_span`, with `start` the
# swarm's start on the runtime's clock. A task with no scorer (`scorer` None) has none.
def gate_checks(
    events: EventLog, member_span: str, scorer: str | None, start: float
) -> GateChecks:
    if scorer is None:
        return GateChecks()
    checks = passes = 0
    first_pass: float | None = None
    for score in events.scores:
        if score.scorer != scorer or _agent_span(events, score.span_id) != member_span:
            continue
        checks += 1
        if _score_value(score.value) == 1.0:
            passes += 1
            if first_pass is None:
                first_pass = score.time - start
    return GateChecks(checks, passes, first_pass)


def swarm_metrics(
    ledger: SwarmLedger,
    events: EventLog,
    places: SpanPlaces,
    times: Sequence[MemberTimes],
    gates: Sequence[GateChecks],
    wiring: Sequence[MemberWiring],
    channels: Sequence[str],
    waits_offered: bool,
    wall_clock: float,
    answer: str | None,
) -> SwarmMetrics:
    tool_calls: dict[str, list[tuple[str, str]]] = {m.name: [] for m in ledger.members}
    for tool in events.tools.values():
        _, member = places(tool.span_id)
        if member is not None:
            tool_calls[member].append((tool.function, tool.arguments))
    comm = _comm_metrics(events, [m.name for m in ledger.members], channels)
    waits = _waits(events, [m.name for m in ledger.members]) if waits_offered else None

    members: list[MemberMetrics] = []
    for line, member_times, gate in zip(ledger.members, times, gates, strict=True):
        wait = None if waits is None else waits[line.name]
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
                longest_identical_tool_run=None
                if line.bridged
                else _longest_run(tool_calls[line.name]),
                gate_checks=gate.checks,
                gate_passes=gate.passes,
                first_pass_time=gate.first_pass_time,
                comm=comm[line.name],
                waits=None if wait is None else wait.waits,
                waits_by_outcome=None if wait is None else wait.by_outcome,
                wait_time=None if wait is None else wait.time,
                idle_time=None if wait is None else wait.idle,
            )
        )
    return SwarmMetrics(
        tokens=ledger.swarm.usage.total_tokens,
        cost=ledger.swarm.cost,
        wall_clock=wall_clock,
        members=members,
        never_acted=[m.name for m in ledger.members if _steps(m) == 0],
        # react() resolves its tools before its first model call
        tools_unwired=[
            m.name
            for m, w in zip(ledger.members, wiring, strict=True)
            if channels and not w.tools and _steps(m) >= 1
        ],
        steps_to_answer=None
        if answer is None
        else next(_steps(m) for m in ledger.members if m.name == answer),
        longest_member_steps=max(_steps(m) for m in ledger.members),
    )


# Each member's communication over each of the channels, from the evidence events that
# name one of the members. Member names are unique in a sample, since each names a
# timeline, so another swarm's events never name these members.
def _comm_metrics(
    events: EventLog, members: Sequence[str], channels: Sequence[str]
) -> dict[str, dict[str, CommMetrics]]:
    comm = {name: {c: CommMetrics() for c in channels} for name in members}
    # the payload bytes of each record the bus accepted, by member and record id
    accepted: dict[tuple[str, str | None], int] = {}
    for event in events.comms:
        metrics = comm.get(event.member, {}).get(event.channel)
        if metrics is None:
            continue
        match event.kind:
            case "sent" if event.rejected is not None:
                metrics.rejections[event.rejected] = (
                    metrics.rejections.get(event.rejected, 0) + 1
                )
            case "sent":
                accepted[(event.member, event.record)] = event.payload_bytes
            case "delivered":
                metrics.records += 1
                metrics.record_bytes += accepted[(event.member, event.record)]
            case "read" if event.listing:
                metrics.listings += 1
            case "read":
                metrics.reads += 1
            case _:
                pass
    return comm


# One member's waits, totalled.
@dataclass
class _Waits:
    waits: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict[str, int])
    time: float = 0.0
    idle: float = 0.0


# Each member's waits, from the `waited` events that name one of the members, as
# `_comm_metrics` counts their other communication.
def _waits(events: EventLog, members: Sequence[str]) -> dict[str, _Waits]:
    waits = {name: _Waits() for name in members}
    for event in events.waits:
        wait = waits.get(event.member)
        if wait is None:
            continue
        wait.waits += 1
        wait.by_outcome[event.outcome] = wait.by_outcome.get(event.outcome, 0) + 1
        wait.time += event.duration
        wait.idle += event.idle
    return waits


# Completed model calls in the member's spans, cache replays included.
def _steps(line: MemberLedger) -> int:
    return line.calls.calls + line.calls.cache_replays


# The nearest agent span enclosing `span`, itself included.
def _agent_span(events: EventLog, span: str | None) -> str | None:
    while span is not None and span not in events.agent_spans:
        span = events.parents.get(span)
    return span


def _longest_run(calls: Sequence[tuple[str, str]]) -> int:
    longest = run = 0
    previous: tuple[str, str] | None = None
    for call in calls:
        run = run + 1 if call == previous else 1
        longest = max(longest, run)
        previous = call
    return longest
