import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import cast

from inspect_ai.event import CommunicationEvent, Event, ModelEvent, SwarmEvent
from pydantic import JsonValue

logger = logging.getLogger(__name__)


# A commit (`delivered`): its record id and sender.
@dataclass(frozen=True)
class PathCommit:
    position: int
    member: str
    record: str


# A result that showed commits (`read` or `waited`): its `depends_on`, and for a wait
# the roster members whose ends it named.
@dataclass(frozen=True)
class PathResult:
    position: int
    member: str
    depends_on: list[str]
    ended: list[str]


# What the critical path reads from the transcript, by position (the transcript's
# event count when the event was first reported). Filled by EventLog, live only.
# `roster` is this swarm's members. Evidence naming another member, as a nested swarm's
# does, is dropped before anything else in it is read. Every channel is read the same
# way: no channel name, payload or file is read.
class PathLog:
    def __init__(self, roster: Collection[str]) -> None:
        self.roster = frozenset(roster)
        # model event uuid -> (span id, position)
        self.steps: dict[str, tuple[str | None, int]] = {}
        self.commits: list[PathCommit] = []
        self.results: list[PathResult] = []
        # `member_result`: member -> position
        self.ends: dict[str, int] = {}
        # a persistent member's `answer`: (member, answer index) -> position
        self.answers: dict[tuple[str, int], int] = {}
        # the evidence events taken, by uuid, so that an update is not taken again
        self._taken: set[str] = set()

    def __call__(self, event: Event, position: int) -> None:
        # the transcript gives every event a uuid before it reports it
        assert event.uuid is not None
        if isinstance(event, ModelEvent):
            if event.uuid not in self.steps:
                self.steps[event.uuid] = (event.span_id, position)
            return
        if isinstance(event, SwarmEvent):
            if event.kind == "member_result" and self._take(event, event.member):
                assert event.member is not None
                self.ends.setdefault(event.member, position)
            elif event.kind == "answer" and self._take(event, event.member):
                assert event.member is not None
                index = cast(int, event.data["answer"])
                self.answers.setdefault((event.member, index), position)
            return
        if not (
            isinstance(event, CommunicationEvent)
            and event.source == "inspect_swarm"
            and event.kind in ("delivered", "read", "waited")
            and self._take(event, event.agent)
        ):
            return
        member = event.agent
        data = event.data
        if event.kind == "delivered":
            [record] = event.record_ids
            self.commits.append(PathCommit(position, member, record))
            return
        ended: list[str] = []
        if event.kind == "waited":
            named = [cast(str, e["member"]) for e in _entries(data["ended"])]
            ended = [m for m in named if m in self.roster]
        self.results.append(
            PathResult(position, member, cast(list[str], data["depends_on"]), ended)
        )

    # Whether to take an evidence event: one naming a roster member, not taken before.
    def _take(self, event: Event, member: str | None) -> bool:
        assert event.uuid is not None
        if member not in self.roster or event.uuid in self._taken:
            return False
        self._taken.add(event.uuid)
        return True


@dataclass(frozen=True)
class _Step:
    member: str


@dataclass(frozen=True)
class _End:
    member: str


# the chosen answer of a persistent member
@dataclass(frozen=True)
class _Answered:
    member: str


# The number of steps on the longest chain into the answering member's end, or for a
# persistent member's answer (`answer_index`) into that answer's `answer` event: each
# member's nodes form one chain in position order, a commit is its sender's chain
# there, and a result follows the commits it depends on and the member ends it named.
# None without an answer, without the answering member's end or answer event, or when
# a result depends on a record no recorded commit made (with a warning).
def critical_steps_to_answer(
    log: PathLog,
    completed: Callable[[str], bool],
    places: Callable[[str | None], tuple[bool, str | None]],
    answer: str | None,
    answer_index: int | None = None,
) -> int | None:
    if answer is None:
        return None
    target = (
        log.ends.get(answer)
        if answer_index is None
        else log.answers.get((answer, answer_index))
    )
    if target is None:
        return None
    nodes: list[tuple[int, _Step | _End | _Answered | PathCommit | PathResult]] = []
    for uuid, (span, position) in log.steps.items():
        member = places(span)[1]
        # steps outside every member (the controller's, the final step's) are ignored
        if member is not None and completed(uuid):
            nodes.append((position, _Step(member)))
    nodes.extend((member_end, _End(m)) for m, member_end in log.ends.items())
    if answer_index is not None:
        nodes.append((target, _Answered(answer)))
    nodes.extend((n.position, n) for n in [*log.commits, *log.results])
    nodes.sort(key=lambda node: node[0])

    # each member's running value, the value at each member's end, and each record's
    # value at its commit
    value: dict[str, int] = {}
    ends: dict[str, int] = {}
    committed: dict[str, int] = {}
    for _, node in nodes:
        reached = value.get(node.member, 0)
        match node:
            case _Step():
                reached += 1
            case _Answered():
                return reached
            case _End():
                if answer_index is None and node.member == answer:
                    return reached
                ends.setdefault(node.member, reached)
            case PathCommit():
                committed[node.record] = reached
            case PathResult():
                for record in node.depends_on:
                    if record not in committed:
                        logger.warning(
                            f"The swarm's critical path is not recorded: a result of {node.member} depends on {record}, which no recorded commit made."
                        )
                        return None
                    reached = max(reached, committed[record])
                for ended in node.ended:
                    reached = max(reached, ends.get(ended, 0))
        value[node.member] = reached
    return None


# A persistent member's steps before one of its answers: its completed model calls
# recorded before that answer's `answer` event. None when the event is not recorded.
def steps_before_answer(
    log: PathLog,
    completed: Callable[[str], bool],
    places: Callable[[str | None], tuple[bool, str | None]],
    answer: str,
    answer_index: int,
) -> int | None:
    target = log.answers.get((answer, answer_index))
    if target is None:
        return None
    return sum(
        1
        for uuid, (span, position) in log.steps.items()
        if position < target and places(span)[1] == answer and completed(uuid)
    )


def _entries(value: JsonValue) -> list[dict[str, JsonValue]]:
    return cast(list[dict[str, JsonValue]], value)
