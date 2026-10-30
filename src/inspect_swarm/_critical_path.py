import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import cast

from inspect_ai.event import Event, InfoEvent, ModelEvent
from pydantic import JsonValue

logger = logging.getLogger(__name__)


# A write of the built-in board (`delivered`): each file it touched, with the version
# the file now has and whether the write deleted it.
@dataclass(frozen=True)
class PathWrite:
    position: int
    member: str
    loop: str | None
    files: list[tuple[str, int, bool]]


# A view of the built-in board that showed files (`read` without `listing`): each file
# shown, with the version shown and whether it was a deleted file's tombstone.
@dataclass(frozen=True)
class PathRead:
    position: int
    member: str
    files: list[tuple[str, int, bool]]


# A wait's result (`waited`): the file versions it named, kept only when the built-in
# board was among the channels the wait reached and the built-in messages channel the
# only other, and the roster members whose ends it named.
@dataclass(frozen=True)
class PathWait:
    position: int
    member: str
    loop: str | None
    files: list[tuple[str, int]]
    ended: list[str]


# What the critical path reads from the transcript, by position (the transcript's
# event count when the event was first reported). Filled by EventLog, live only.
# `board` and `messages` are the built-in board's and messages channel's record names,
# or None; `roster` this swarm's members. Evidence naming another member, as a nested
# swarm's does, is dropped before anything else in it is read; the board's payloads are
# read only from the board's events.
class PathLog:
    def __init__(
        self, board: str | None, messages: str | None, roster: Collection[str]
    ) -> None:
        self.board = board
        self.messages = messages
        self.roster = frozenset(roster)
        # model event uuid -> (span id, position)
        self.steps: dict[str, tuple[str | None, int]] = {}
        self.writes: list[PathWrite] = []
        self.reads: list[PathRead] = []
        self.waits: list[PathWait] = []
        # `member_result`: member -> position
        self.ends: dict[str, int] = {}
        # the evidence events taken, by uuid, so that an update is not taken again
        self._taken: set[str] = set()

    def __call__(self, event: Event, position: int) -> None:
        # the transcript gives every event a uuid before it reports it
        assert event.uuid is not None
        if isinstance(event, ModelEvent):
            if event.uuid not in self.steps:
                self.steps[event.uuid] = (event.span_id, position)
            return
        if not (
            isinstance(event, InfoEvent)
            and event.source == "inspect_swarm"
            and isinstance(event.data, dict)
        ):
            return
        data = event.data
        member = data.get("member")
        if not isinstance(member, str) or member not in self.roster:
            return
        if event.uuid in self._taken:
            return
        self._taken.add(event.uuid)
        kind = data.get("kind")
        if data.get("type") == "result" and kind == "member_result":
            self.ends.setdefault(member, position)
        elif data.get("type") != "comm":
            return
        elif kind == "waited":
            files: list[tuple[str, int]] = []
            reached = set(cast(list[str], data["channels"]))
            if self.board in reached and reached <= {self.board, self.messages}:
                files = [
                    (cast(str, e["file"]), cast(int, e["version"]))
                    for e in _entries(data["files"])
                    # the messages channel's entries name the unread messages
                    if "record" not in e
                ]
            ended = [cast(str, e["member"]) for e in _entries(data["ended"])]
            self.waits.append(
                PathWait(
                    position,
                    member,
                    cast(str | None, data["loop"]),
                    files,
                    [m for m in ended if m in self.roster],
                )
            )
        elif self.board is None or data.get("channel") != self.board:
            return
        elif kind == "delivered":
            result = cast(dict[str, JsonValue], data["result"])
            self.writes.append(
                PathWrite(
                    position,
                    member,
                    cast(str | None, data["loop"]),
                    _versions(_entries(result["files"])),
                )
            )
        elif kind == "read" and data.get("listing") is not True:
            self.reads.append(
                PathRead(position, member, _versions(_entries(data["files"])))
            )


# A version of a board file as a write left it: the writing loop, the writer's value
# there, whether it deleted the file, the largest value among the writes in the file's
# history up to it, and the latest earlier version written by another loop.
@dataclass(frozen=True)
class _Version:
    loop: str | None
    value: int
    deleted: bool
    history: int
    other: int | None


@dataclass(frozen=True)
class _Step:
    member: str


@dataclass(frozen=True)
class _End:
    member: str


# The number of steps on the longest chain into the answering member's end: each
# member's nodes form one chain in position order, a read follows the writes of the
# version it showed, and a wait result follows the peer writes and the member ends it
# named. None without an answer, without the answering member's end, or when the
# evidence is inconsistent (with a warning).
def critical_steps_to_answer(
    log: PathLog,
    completed: Callable[[str], bool],
    places: Callable[[str | None], tuple[bool, str | None]],
    answer: str | None,
) -> int | None:
    if answer is None or answer not in log.ends:
        return None
    nodes: list[tuple[int, _Step | _End | PathWrite | PathRead | PathWait]] = []
    for uuid, (span, position) in log.steps.items():
        member = places(span)[1]
        # steps outside every member (the controller's, the final step's) are ignored
        if member is not None and completed(uuid):
            nodes.append((position, _Step(member)))
    nodes.extend((member_end, _End(m)) for m, member_end in log.ends.items())
    nodes.extend((n.position, n) for n in [*log.writes, *log.reads, *log.waits])
    nodes.sort(key=lambda node: node[0])

    # each member's running value, and the value at each member's end
    value: dict[str, int] = {}
    ends: dict[str, int] = {}
    # file -> version -> the write that made it, and file -> its current version
    versions: dict[str, dict[int, _Version]] = {}
    current: dict[str, int] = {}
    for _, node in nodes:
        reached = value.get(node.member, 0)
        match node:
            case _Step():
                reached += 1
            case _End():
                if node.member == answer:
                    return reached
                ends.setdefault(node.member, reached)
            case PathWrite():
                for file, version, deleted in node.files:
                    history = versions.setdefault(file, {})
                    last = current.get(file)
                    before = None if last is None else history[last]
                    history[version] = _Version(
                        loop=node.loop,
                        value=reached,
                        deleted=deleted,
                        history=reached
                        if before is None
                        else max(before.history, reached),
                        other=None
                        if before is None
                        else last
                        if before.loop != node.loop
                        else before.other,
                    )
                    current[file] = version
            case PathRead():
                for file, version, deleted in node.files:
                    shown = versions.get(file, {}).get(version)
                    if (
                        shown is None
                        or current[file] != version
                        or shown.deleted != deleted
                    ):
                        return _inconsistent(
                            f"{node.member} was shown {file} version {version}, but the board's current version of it was {current.get(file)}"
                        )
                    # a tombstone shows who deleted the file, not its content
                    reached = max(reached, shown.value if deleted else shown.history)
            case PathWait():
                for file, version in node.files:
                    named = versions.get(file, {}).get(version)
                    if named is None:
                        return _inconsistent(
                            f"a wait of {node.member} named {file} version {version}, which no write of this swarm's board produced"
                        )
                    # the peer write the result reported: before the waiter's own blind
                    # append, when that made the current version
                    if named.loop == node.loop:
                        named = (
                            None if named.other is None else versions[file][named.other]
                        )
                    if named is not None:
                        reached = max(reached, named.value)
                for ended in node.ended:
                    reached = max(reached, ends.get(ended, 0))
        value[node.member] = reached
    return None


def _entries(value: JsonValue) -> list[dict[str, JsonValue]]:
    return cast(list[dict[str, JsonValue]], value)


def _versions(entries: list[dict[str, JsonValue]]) -> list[tuple[str, int, bool]]:
    return [
        (cast(str, e["file"]), cast(int, e["version"]), e.get("deleted") is True)
        for e in entries
    ]


def _inconsistent(problem: str) -> None:
    logger.warning(
        f"The swarm's critical path is not recorded: its board evidence is inconsistent ({problem})."
    )
    return None
