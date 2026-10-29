import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from inspect_ai.event import ModelEvent
from inspect_ai.log._transcript import Transcript, _transcript
from inspect_ai.model import GenerateConfig, ModelOutput
from pydantic import JsonValue

from inspect_swarm._bus._record import Record
from inspect_swarm._channel._channel import WaitSpec
from inspect_swarm._critical_path import PathLog, critical_steps_to_answer
from inspect_swarm._evidence import comm_delivered, comm_read, comm_waited, evidence
from inspect_swarm._ledger import EventLog
from inspect_swarm._metrics import _completed  # pyright: ignore[reportPrivateUsage]

BOARD = "shared_memory"
ROSTER = ("a", "b", "c", "m", "p", "q", "w")

# a member's own span, and the spans of a nested swarm inside member a; any other span
# is outside every member, as the controller's is
_PLACES: dict[str | None, str] = {f"span-{m}": m for m in ROSTER}
_PLACES["span-inner"] = "a"


# A transcript reporting evidence to an EventLog, as the swarm's interval does. The
# evidence is written by the runtime's own writers, so it has their shape.
@dataclass
class _Run:
    transcript: Transcript
    events: EventLog
    _records: int = field(default=0)

    def report(self, event: ModelEvent) -> None:
        self.transcript._event(event)  # pyright: ignore[reportPrivateUsage]

    def record(self, member: str, channel: str, loop: str | None) -> Record:
        self._records += 1
        return Record(
            id=f"w-{self._records}",
            channel=channel,
            kind="create",
            sender=member,
            to=(),
            recipients=(),
            routed_out=(),
            payload="",
            op=None,
            wake=True,
            reply_to=None,
            sender_span_id=loop,
            tool_span_id=None,
        )


Op = Callable[[_Run], None]

# a file entry as an operation names it: (id, version), or (id, version, deleted)
File = tuple[str, int] | tuple[str, int, bool]


def _loop(member: str) -> str:
    return f"loop-{member}"


def steps(
    member: str,
    n: int = 1,
    *,
    span: str | None = None,
    error: str | None = None,
    cache: bool = False,
    pending: bool = False,
) -> Op:
    def op(run: _Run) -> None:
        for _ in range(n):
            run.report(
                ModelEvent(
                    model="mockllm/model",
                    input=[],
                    tools=[],
                    tool_choice="auto",
                    config=GenerateConfig(),
                    output=ModelOutput(),
                    span_id=span or f"span-{member}",
                    error=error,
                    cache="read" if cache else None,
                    pending=pending or None,
                )
            )

    return op


def _entry(file: File | dict[str, JsonValue]) -> dict[str, JsonValue]:
    # a write's entry as the board gives it; a rename's is given whole
    if isinstance(file, dict):
        return file
    entry: dict[str, JsonValue] = {
        "file": file[0],
        "path": "/shared_memory/x",
        "base": file[1] - 1 or None,
        "version": file[1],
    }
    if len(file) == 3 and file[2]:
        entry["deleted"] = True
    return entry


def write(
    member: str,
    *files: File | dict[str, JsonValue],
    loop: str | None = None,
    channel: str = BOARD,
    result: dict[str, JsonValue] | None = None,
) -> Op:
    def op(run: _Run) -> None:
        record = run.record(member, channel, loop or _loop(member))
        comm_delivered(
            f"span-{member}",
            record,
            False,
            1,
            [],
            result if result is not None else {"files": [_entry(f) for f in files]},
        )

    return op


def read(
    member: str,
    *files: File,
    listing: bool = False,
    channel: str = BOARD,
) -> Op:
    def op(run: _Run) -> None:
        comm_read(
            f"span-{member}",
            member,
            channel,
            "r-1",
            _loop(member),
            False,
            None,
            [
                {
                    "file": f[0],
                    "version": f[1],
                    "record": "w-1",
                    "lines": None,
                    "deleted": len(f) == 3 and f[2],
                }
                for f in files
            ],
            listing,
        )

    return op


def wait(
    member: str,
    *files: File | dict[str, JsonValue],
    ended: tuple[str, ...] = (),
    loop: str | None = None,
    channels: tuple[str, ...] = (BOARD,),
    outcome: str = "news",
) -> Op:
    def op(run: _Run) -> None:
        comm_waited(
            f"span-{member}",
            WaitSpec(
                id="x-1", member=member, loop=loop or _loop(member), own=True, path=None
            ),
            nested=False,
            channels=list(channels),
            timeout={"requested": None, "effective": 600},
            started=0.0,
            duration=0.0,
            idle=0.0,
            outcome=outcome,
            woken_by=[],
            files=[
                f
                if isinstance(f, dict)
                else {"file": f[0], "version": f[1], "deleted": len(f) == 3 and f[2]}
                for f in files
            ],
            ended=[{"member": m, "status": "submitted"} for m in ended],
        )

    return op


def end(member: str) -> Op:
    def op(run: _Run) -> None:
        evidence(
            "result",
            "member_result",
            member=member,
            span_id=f"span-{member}",
            status="submitted",
            time=0.0,
            sandbox=None,
        )

    return op


def _value(board: str | None, ops: list[Op], answer: str | None) -> int | None:
    transcript = Transcript()
    token = _transcript.set(transcript)
    try:
        events = EventLog(PathLog(board, ROSTER))
        transcript._subscribe(events)  # pyright: ignore[reportPrivateUsage]
        run = _Run(transcript, events)
        for op in ops:
            op(run)
    finally:
        _transcript.reset(token)
    assert events.path is not None
    return critical_steps_to_answer(
        events.path,
        lambda uuid: _completed(events, uuid),
        lambda span: (True, _PLACES.get(span)),
        answer,
    )


def _renamed(file: str) -> dict[str, JsonValue]:
    # a rename's entry for a file moved from version 1 to 2, which has no path or
    # deleted
    return {
        "file": file,
        "old_path": f"/shared_memory/d/{file}",
        "new_path": f"/shared_memory/e/{file}",
        "base": 1,
        "version": 2,
    }


# a ten-step member p that writes f-1
_DEEP_WRITER = [steps("p", 10), write("p", ("f-1", 1))]


@pytest.mark.parametrize(
    "board,ops,answer,expected",
    [
        pytest.param(
            BOARD,
            [steps("a", 3), end("a")],
            "a",
            3,
            id="single-member",
        ),
        pytest.param(
            BOARD,
            [
                steps("a", 5),
                write("a", ("f-1", 1)),
                steps("b"),
                wait("b", ("f-1", 1)),
                steps("b"),
                steps("a"),
                write("a", ("f-1", 2)),
                read("b", ("f-1", 2)),
                steps("b"),
                end("a"),
                end("b"),
            ],
            "b",
            7,
            id="wait-view-submit",
        ),
        pytest.param(
            BOARD,
            [
                *_DEEP_WRITER,
                read("m", ("f-1", 1)),
                steps("m"),
                write("m", ("f-2", 1)),
                end("m"),
            ],
            "m",
            11,
            id="tool-views-calls-and-writes",
        ),
        pytest.param(
            BOARD,
            [*_DEEP_WRITER, read("m", ("f-1", 1)), end("m")],
            "m",
            10,
            id="view-returned-with-no-step-after",
        ),
        pytest.param(
            BOARD,
            [
                *_DEEP_WRITER,
                steps("q", 2),
                write("q", ("f-1", 2)),
                read("m", ("f-1", 2)),
                steps("m"),
                end("m"),
            ],
            "m",
            11,
            id="view-follows-every-write-in-history",
        ),
        pytest.param(
            BOARD,
            [
                *_DEEP_WRITER,
                steps("m"),
                read("m", ("f-1", 1)),
                write("m", ("f-2", 1)),
                read("c", ("f-2", 1)),
                steps("c"),
                end("c"),
            ],
            "c",
            11,
            id="independent-write-after-view-carries-it",
        ),
        pytest.param(
            BOARD,
            [
                steps("q", 4),
                steps("w"),
                end("q"),
                wait("w", ended=("q",)),
                steps("w"),
                end("w"),
            ],
            "w",
            5,
            id="wait-for-an-end",
        ),
        pytest.param(
            BOARD,
            [
                steps("p", 5),
                write("p", ("f-1", 1)),
                steps("w"),
                write("w", ("f-1", 2)),
                wait("w", ("f-1", 2)),
                end("w"),
            ],
            "w",
            5,
            id="wait-after-own-blind-append",
        ),
        pytest.param(
            BOARD,
            [
                *_DEEP_WRITER,
                steps("w"),
                write("w", ("f-1", 2), loop="loop-w-child"),
                write("w", ("f-1", 3)),
                wait("w", ("f-1", 3)),
                end("w"),
            ],
            "w",
            1,
            id="wait-after-own-append-follows-the-other-loop",
        ),
        pytest.param(
            BOARD,
            [
                *_DEEP_WRITER,
                write("w", ("f-1", 2)),
                wait("w", ("f-1", 2)),
                write("w", ("f-1", 3)),
                write("w", ("f-1", 4)),
                wait("w", ("f-1", 4)),
                wait("w", ("f-1", 4)),
                steps("w"),
                end("w"),
            ],
            "w",
            11,
            id="repeated-waits-after-blind-appends",
        ),
        pytest.param(
            BOARD,
            [
                steps("p"),
                *(write("p", (f"f-{i}", 1)) for i in range(1, 11)),
                steps("q", 10),
                write("q", ("f-11", 1)),
                steps("w"),
                wait("w", *((f"f-{i}", 1) for i in range(1, 11))),
                steps("w"),
                end("w"),
            ],
            "w",
            2,
            id="wait-names-ten-of-eleven-files",
        ),
        pytest.param(
            BOARD,
            [
                *_DEEP_WRITER,
                wait("m"),
                wait("m", outcome="cancelled"),
                steps("m"),
                end("m"),
            ],
            "m",
            1,
            id="wait-that-named-nothing",
        ),
        pytest.param(
            BOARD,
            [*_DEEP_WRITER, read("m", ("f-1", 1), listing=True), steps("m"), end("m")],
            "m",
            1,
            id="listing",
        ),
        pytest.param(
            BOARD,
            [
                *_DEEP_WRITER,
                steps("q", 2),
                write("q", ("f-1", 2, True)),
                read("m", ("f-1", 2, True)),
                steps("m"),
                end("m"),
            ],
            "m",
            3,
            id="tombstone-view-follows-the-delete",
        ),
        pytest.param(
            BOARD,
            [
                steps("p", 10),
                write("p", ("f-1", 1), channel="notes"),
                read("m", ("f-1", 1), channel="notes"),
                steps("m"),
                end("m"),
            ],
            "m",
            1,
            id="another-channel",
        ),
        pytest.param(
            BOARD,
            [
                steps("q"),
                write("q", ("f-1", 1), ("f-2", 1)),
                steps("p", 10),
                write(
                    "p",
                    _renamed("f-1"),
                    _renamed("f-2"),
                ),
                read("m", ("f-2", 2)),
                steps("m"),
                end("m"),
            ],
            "m",
            11,
            id="directory-rename-writes-every-file",
        ),
        pytest.param(
            BOARD,
            [
                steps("a", 2),
                steps("inner", 3, span="span-inner"),
                write("inner", result={"size": 1}),
                read("inner", ("f-1", 1)),
                wait("inner", {"foreign_record": "x-1"}),
                end("inner"),
                write("i", ("f-1", 1)),
                read("i", ("f-1", 1)),
                end("a"),
            ],
            "a",
            5,
            id="nested-swarm-evidence-dropped",
        ),
        pytest.param(
            None,
            [
                steps("p", 10),
                write("p", ("f-1", 1)),
                end("p"),
                read("m", ("f-1", 1)),
                wait("m", ("f-99", 7), ended=("p",)),
                steps("m"),
                end("m"),
            ],
            "m",
            11,
            id="no-builtin-board",
        ),
        pytest.param(
            BOARD,
            [
                steps("q", 20),
                write("q", ("f-1", 1)),
                steps("p", 2),
                end("p"),
                wait("m", ("f-1", 1), ended=("p",), channels=("notes",)),
                wait("m", ("f-1", 1), channels=(BOARD, "notes")),
                wait("m", ("f-99", 1), channels=(BOARD, "notes")),
                wait("m", {"note": "n-1"}, channels=(BOARD, "notes")),
                steps("m"),
                end("m"),
            ],
            "m",
            3,
            id="waits-reaching-a-custom-waiting-channel",
        ),
        pytest.param(
            BOARD,
            [
                steps("x", 10),
                write("x", ("f-1", 1)),
                steps("p", 2),
                write("p", ("f-1", 1)),
                steps("m", span="span-controller"),
                read("m", ("f-1", 1)),
                steps("m"),
                end("m"),
            ],
            "m",
            3,
            id="outside-roster-and-outside-members-ignored",
        ),
        pytest.param(
            BOARD,
            [
                steps("m", 2, error="boom"),
                steps("m", 1, pending=True),
                steps("m", 2, cache=True),
                steps("m"),
                end("m"),
            ],
            "m",
            3,
            id="errored-pending-and-cache-replayed-calls",
        ),
        pytest.param(BOARD, [steps("m"), end("m")], None, None, id="no-answer"),
        pytest.param(BOARD, [steps("m")], "m", None, id="answer-with-no-end"),
    ],
)
def test_critical_steps_to_answer(
    board: str | None,
    ops: list[Op],
    answer: str | None,
    expected: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _value(board, ops, answer) == expected
    assert not caplog.records


@pytest.mark.parametrize(
    "ops,problem",
    [
        pytest.param(
            [
                *_DEEP_WRITER,
                write("q", ("f-1", 2)),
                read("m", ("f-1", 1)),
            ],
            "m was shown f-1 version 1, but the board's current version of it was 2",
            id="read-of-an-old-version",
        ),
        pytest.param(
            [read("m", ("f-1", 1))],
            "m was shown f-1 version 1, but the board's current version of it was None",
            id="read-of-an-unknown-file",
        ),
        pytest.param(
            [*_DEEP_WRITER, wait("m", ("f-1", 2))],
            "a wait of m named f-1 version 2, which no write of this swarm's board produced",
            id="wait-naming-an-unwritten-version",
        ),
    ],
)
def test_inconsistent_evidence_has_no_value(
    ops: list[Op], problem: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _value(BOARD, [*ops, steps("m"), end("m")], "m") is None
    [warning] = caplog.records
    assert problem in warning.getMessage()
