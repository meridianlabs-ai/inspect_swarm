import logging
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from inspect_ai.event import ModelEvent
from inspect_ai.log._transcript import Transcript, _transcript
from inspect_ai.model import GenerateConfig, ModelOutput

from inspect_swarm._bus._record import Record
from inspect_swarm._channel._channel import WaitSpec
from inspect_swarm._critical_path import PathLog, critical_steps_to_answer
from inspect_swarm._evidence import (
    comm_delivered,
    comm_read,
    comm_waited,
    swarm_event,
)
from inspect_swarm._ledger import EventLog
from inspect_swarm._metrics import _completed  # pyright: ignore[reportPrivateUsage]

BOARD = "shared_memory"
MESSAGES = "messages"
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

    def report(self, event: ModelEvent) -> None:
        self.transcript._event(event)  # pyright: ignore[reportPrivateUsage]


Op = Callable[[_Run], None]


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


def write(member: str, record: str, *, channel: str = BOARD) -> Op:
    # the commit of `record`, sent by `member`
    def op(run: _Run) -> None:
        comm_delivered(
            f"span-{member}",
            Record(
                id=record,
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
                sender_span_id=_loop(member),
                tool_span_id=None,
            ),
            False,
            1,
            [],
            None,
        )

    return op


def read(
    member: str,
    *depends_on: str,
    records: tuple[str, ...] | None = None,
    channel: str = BOARD,
) -> Op:
    # a result that concerned `records` (by default `depends_on`) and depends on
    # `depends_on`
    def op(run: _Run) -> None:
        comm_read(
            f"span-{member}",
            member,
            channel,
            "r-1",
            _loop(member),
            False,
            None,
            list(depends_on if records is None else records),
            list(depends_on),
            False,
        )

    return op


def wait(
    member: str,
    *depends_on: str,
    ended: tuple[str, ...] = (),
    outcome: str = "news",
) -> Op:
    def op(run: _Run) -> None:
        comm_waited(
            f"span-{member}",
            WaitSpec(id="x-1", member=member, loop=_loop(member), own=True, path=None),
            nested=False,
            channels=[BOARD],
            timeout={"requested": None, "effective": 600},
            started=0.0,
            duration=0.0,
            idle=0.0,
            outcome=outcome,
            woken_by=[],
            files=[],
            depends_on=list(depends_on),
            ended=[{"member": m, "status": "submitted"} for m in ended],
        )

    return op


def end(member: str) -> Op:
    def op(run: _Run) -> None:
        swarm_event(
            "member_result",
            member=member,
            span_id=f"span-{member}",
            status="submitted",
            time=0.0,
            sandbox=None,
        )

    return op


def _value(ops: list[Op], answer: str | None) -> int | None:
    transcript = Transcript()
    token = _transcript.set(transcript)
    try:
        events = EventLog(PathLog(ROSTER))
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


# a ten-step member p that commits w-1
_DEEP_WRITER = [steps("p", 10), write("p", "w-1")]


@pytest.mark.parametrize(
    "ops,answer,expected",
    [
        pytest.param([steps("a", 3), end("a")], "a", 3, id="single-member"),
        pytest.param(
            [
                steps("a", 5),
                write("a", "w-1"),
                steps("b"),
                wait("b", "w-1"),
                steps("b"),
                steps("a"),
                write("a", "w-2"),
                read("b", "w-2", records=("w-2",)),
                steps("b"),
                end("a"),
                end("b"),
            ],
            "b",
            7,
            id="wait-view-submit",
        ),
        pytest.param(
            [
                *_DEEP_WRITER,
                read("m", "w-1"),
                steps("m"),
                write("m", "w-2"),
                end("m"),
            ],
            "m",
            11,
            id="tool-views-calls-and-writes",
        ),
        pytest.param(
            [*_DEEP_WRITER, read("m", "w-1"), end("m")],
            "m",
            10,
            id="view-returned-with-no-step-after",
        ),
        pytest.param(
            [
                steps("a", 2),
                write("a", "w-1"),
                steps("a"),
                write("a", "w-2"),
                steps("b", 2),
                read("b", "w-1", "w-2", records=("w-2",)),
                steps("b"),
                end("b"),
            ],
            "b",
            4,
            id="view-follows-every-write-in-history",
        ),
        pytest.param(
            [
                *_DEEP_WRITER,
                steps("m"),
                read("m", "w-1"),
                write("m", "w-2"),
                read("c", "w-2"),
                steps("c"),
                end("c"),
            ],
            "c",
            11,
            id="independent-write-after-view-carries-it",
        ),
        pytest.param(
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
            [
                *_DEEP_WRITER,
                write("w", "w-2"),
                wait("w", "w-1"),
                write("w", "w-3"),
                wait("w"),
                steps("w"),
                end("w"),
            ],
            "w",
            11,
            id="wait-on-a-peer-record-then-trimmed",
        ),
        pytest.param(
            [
                steps("p"),
                *(write("p", f"w-{i}") for i in range(1, 11)),
                steps("q", 10),
                write("q", "w-11"),
                steps("w"),
                wait("w", *(f"w-{i}" for i in range(1, 12))),
                steps("w"),
                end("w"),
            ],
            "w",
            11,
            id="wait-depends-on-eleven-records",
        ),
        pytest.param(
            [
                *_DEEP_WRITER,
                wait("m"),
                wait("m", outcome="cancelled"),
                steps("m"),
                end("m"),
            ],
            "m",
            1,
            id="wait-that-depends-on-nothing",
        ),
        pytest.param(
            [*_DEEP_WRITER, read("m", records=("w-1",)), steps("m"), end("m")],
            "m",
            1,
            id="records-without-depends-on-make-no-edge",
        ),
        pytest.param(
            [
                *_DEEP_WRITER,
                steps("q", 2),
                write("q", "w-2"),
                read("m", "w-2"),
                steps("m"),
                end("m"),
            ],
            "m",
            3,
            id="tombstone-view-follows-the-delete",
        ),
        pytest.param(
            [
                steps("p", 10),
                write("p", "m-1", channel=MESSAGES),
                steps("p", 5),
                steps("q", 2),
                write("q", "s-3", channel="notes"),
                read("m", "m-1", channel=MESSAGES),
                steps("m"),
                read("c", "s-3", channel="notes"),
                steps("c"),
                end("m"),
            ],
            "m",
            11,
            id="any-channel-links-at-the-commit",
        ),
        pytest.param(
            [steps("m", 3), write("m", "w-1"), read("m", "w-1"), steps("m"), end("m")],
            "m",
            4,
            id="own-commit",
        ),
        pytest.param(
            [
                steps("a", 2),
                steps("inner", 3, span="span-inner"),
                write("inner", "w-1"),
                read("inner", "w-1", "w-9"),
                wait("inner", "w-9", ended=("inner",)),
                end("inner"),
                end("a"),
            ],
            "a",
            5,
            id="nested-swarm-evidence-dropped",
        ),
        pytest.param(
            [
                steps("x", 10),
                write("x", "w-1"),
                steps("p", 2),
                write("p", "w-2"),
                steps("m", span="span-controller"),
                read("m", "w-2"),
                steps("m"),
                end("m"),
            ],
            "m",
            3,
            id="outside-roster-and-outside-members-ignored",
        ),
        pytest.param(
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
        pytest.param([steps("m"), end("m")], None, None, id="no-answer"),
        pytest.param([steps("m")], "m", None, id="answer-with-no-end"),
    ],
)
def test_critical_steps_to_answer(
    ops: list[Op],
    answer: str | None,
    expected: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _value(ops, answer) == expected
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "ops,problem",
    [
        pytest.param(
            [*_DEEP_WRITER, read("m", "w-1", "w-2")],
            "a result of m depends on w-2, which no recorded commit made",
            id="read-of-an-uncommitted-record",
        ),
        pytest.param(
            [*_DEEP_WRITER, wait("m", "w-2")],
            "a result of m depends on w-2, which no recorded commit made",
            id="wait-on-an-uncommitted-record",
        ),
        pytest.param(
            [read("m", "w-1"), *_DEEP_WRITER],
            "a result of m depends on w-1, which no recorded commit made",
            id="read-before-the-commit",
        ),
        pytest.param(
            [
                steps("a"),
                steps("inner", span="span-inner"),
                write("inner", "w-1"),
                read("m", "w-1"),
            ],
            "a result of m depends on w-1, which no recorded commit made",
            id="nested-swarm-commit-is-not-the-outer-one",
        ),
    ],
)
def test_a_result_depending_on_no_recorded_commit_has_no_value(
    ops: list[Op], problem: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _value([*ops, steps("m"), end("m")], "m") is None
    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert problem in warning.getMessage()
