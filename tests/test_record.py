import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from inspect_ai import Task, eval, score  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._control.scoring import TaskScoring, _score_live_sample
from inspect_ai._util.hash import mm3_hash
from inspect_ai.agent import Agent, AgentState, agent, as_solver, react, run
from inspect_ai.dataset import Sample
from inspect_ai.event import Event, InfoEvent, SpanBeginEvent
from inspect_ai.log import EvalLog, EvalSample, read_eval_log, transcript
from inspect_ai.log._samples import sample_active
from inspect_ai.model import GenerateConfig, ModelOutput, get_model
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import Generate, Solver, TaskState, fork, solver
from inspect_ai.util import SandboxEnvironmentSpec, store, subtask

from inspect_swarm import (
    Budget,
    Controller,
    StopReason,
    SwarmControl,
    SwarmResultRecord,
    leaderless,
    member,
    swarm,
    swarm_budget,
    swarm_controller,
    swarm_result,
)
from inspect_swarm._bus._bus import current_bus
from inspect_swarm._evidence import LEDGER_KEY, METRICS_KEY
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._metrics import SwarmMetrics
from inspect_swarm._record import (
    RECORD_TYPE,
    RESULT_ID_KEY,
    START_TYPE,
    _is_record,
    _transcript_result,
    write_start,
)

from .swarm_runtime import Script, generator, sample, scripted_model
from .test_member_sandboxes import StubConfig

# what one call of swarm_result() gave: the record, None, or the error it raised
Read = SwarmResultRecord | None | BaseException

SAMPLE = "sample 's1' (epoch 1)"
LOST_ID = "its store names no result record"
LONG_REASON = "the most complete answer, " + "with every step checked " * 6


def _controller(run: Any, name: str) -> Controller:
    @swarm_controller(name=name)
    def factory() -> Controller:
        return Controller(run)

    return factory()


def _pair(prefix: str = "", script: Script | None = None) -> Agent:
    return swarm(
        [
            member(generator(scripted_model()), name=f"{prefix}a"),
            member(generator(scripted_model(script)), name=f"{prefix}b"),
        ],
        controller=leaderless(stop_on_submission=False),
        channels=[],
    )


def _submitter() -> Agent:
    # a react() member whose loop scores its first submission with the task's scorers
    submit = ModelOutput.for_tool_call("mockllm", "submit", {"answer": "42"})
    return react(model=scripted_model(Script(respond=lambda _: submit)), attempts=2)


def _react_pair(prefix: str) -> Agent:
    return swarm(
        [member(_submitter(), name=f"{prefix}{n}") for n in ("a", "b")],
        controller=leaderless(stop_on_submission=False),
        channels=[],
    )


@scorer(metrics=[])
def probe(reads: list[tuple[bool, Read]]) -> Scorer:
    """Record what `swarm_result(state)` gives on each call, and pass."""

    async def score(state: TaskState, target: Target) -> Score:
        result: Read
        try:
            result = swarm_result(state)
        except (RuntimeError, ValueError) as ex:
            result = ex
        reads.append((state.completed, result))
        return Score(value=1)

    return score


@scorer(metrics=[])
def passes() -> Scorer:
    """Pass every submission."""

    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=1)

    return score


@scorer(metrics=[])
def reads() -> Scorer:
    """Score the number of members in the record, raising what `swarm_result()` raises."""

    async def score(state: TaskState, target: Target) -> Score:
        record = swarm_result(state)
        return Score(value=-1 if record is None else len(record.members))

    return score


@scorer(metrics=[])
def guarded() -> Scorer:
    """The docs' record-dependent scorer: nothing while the solve can call it."""

    async def score(state: TaskState, target: Target) -> Score | None:
        if not state.completed:
            return None
        record = swarm_result(state)
        assert record is not None
        return Score(value=len(record.members))

    return score


def _eval(
    tmp_path: Path,
    solver: Solver | Agent | list[Solver],
    scorers: list[Scorer],
    log_images: bool = True,
    **kwargs: Any,
) -> EvalSample:
    [log] = eval(
        Task(
            dataset=[Sample(id="s1", input="task")],
            solver=solver,
            scorer=scorers,
            **kwargs,
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        log_images=log_images,
        display="none",
    )
    assert log.samples is not None
    [sample] = log.samples
    return sample


def _log(tmp_path: Path, solver: Solver | Agent) -> EvalLog:
    [log] = eval(
        Task(dataset=[Sample(id="s1", input="task")], solver=solver),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success", log.error
    return read_eval_log(log.location)


def _rescore(log: EvalLog, sample: EvalSample) -> Read:
    seen: list[tuple[bool, Read]] = []
    score(log.model_copy(update={"samples": [sample]}), probe(seen), display="none")
    [(completed, result)] = seen
    assert completed
    return result


def _read(sample: EvalSample) -> Read:
    try:
        return swarm_result(sample)
    except ValueError as ex:
        return ex


def _record(read: Read) -> SwarmResultRecord:
    assert isinstance(read, SwarmResultRecord), read
    return read


def _value_error(read: Read, match: str) -> None:
    assert isinstance(read, ValueError), read
    assert SAMPLE in str(read)
    assert re.search(match, str(read)), str(read)


def _names(read: Read) -> list[str]:
    return [m.name for m in _record(read).members]


def _record_events(events: list[Event]) -> list[InfoEvent]:
    return [e for e in events if _is_record(e)]


@agent
def plain() -> Agent:
    """Answer without a swarm."""

    async def execute(state: AgentState) -> AgentState:
        state.output = ModelOutput.from_content("mockllm", "42")
        return state

    return execute


# ---------------------------------------------------------------------------
# Reading in a scorer, on re-score and from a log


@solver
def then_many_events(count: int) -> Solver:
    """Write `count` info events after the swarm, so a bounded transcript evicts its record."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        for i in range(count):
            transcript().info({"step": i})
        store().set("evicted", transcript().history.resident_events_truncated)
        return state

    return solve


@pytest.mark.parametrize("bounded", [False, True])
def test_a_scorer_reads_the_record_and_so_does_re_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounded: bool
) -> None:
    if bounded:
        monkeypatch.setenv("INSPECT_TRANSCRIPT_BOUNDED", "1")
    seen: list[tuple[bool, Read]] = []
    sample = _eval(tmp_path, [as_solver(_pair()), then_many_events(150)], [probe(seen)])
    assert sample.store["evicted"] is bounded
    [(completed, read)] = seen
    assert completed
    assert _names(read) == ["a", "b"]
    assert _record(read) == _record(_read(sample))
    log = read_eval_log(str(next(tmp_path.glob("*.eval"))))
    assert log.samples is not None
    assert _rescore(log, log.samples[0]) == read


def _chooses(reason: str = LONG_REASON) -> Agent:
    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            pass
        control.choose_answer("a", reason=reason)
        return "chosen"

    return swarm(
        [
            member(generator(scripted_model()), name="a"),
            member(generator(scripted_model()), name="b"),
        ],
        controller=_controller(run, f"record_test_reason_{uuid4().hex}"),
        channels=[],
    )


# the text of an attachment the sample logs before its swarm runs
OTHER = "text that another step of the sample logged, " + "long enough to condense " * 5


@solver
def logs_other() -> Solver:
    """Log `OTHER`, which the log keeps as an attachment."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        transcript().info({"text": OTHER})
        return state

    return solve


@solver
def catches_the_end(inner: Agent, match: str | None, seen: list[Read]) -> Solver:
    """Run the swarm, catch the error its end raises when `match` is given, then read the record."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if match is None:
            state = await as_solver(inner)(state, generate)
        else:
            with pytest.raises((OSError, ValueError), match=match):
                await as_solver(inner)(state, generate)
        result: Read
        try:
            result = swarm_result(state)
        except (RuntimeError, ValueError) as ex:
            result = ex
        seen.append(result)
        return state

    return solve


UNWRITABLE = "cannot be written"


def _carrying(field: str, value: str) -> Agent:
    # a swarm whose record holds `value` in `field` of its chosen member, or as the
    # controller's reason
    chosen = value if field == "name" else "a"

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            pass
        control.choose_answer(chosen, reason=value if field == "reason" else None)
        return "chosen"

    return swarm(
        [
            member(
                generator(scripted_model()),
                name=chosen,
                role=value if field == "role" else None,
                sandbox=value if field == "sandbox" else None,
            ),
            # every member has its own sandbox, or none does
            member(
                generator(scripted_model()),
                name="b",
                sandbox="b-box" if field == "sandbox" else None,
            ),
        ],
        controller=_controller(run, f"record_test_carrying_{uuid4().hex}"),
        channels=[],
    )


def _field(record: SwarmResultRecord, field: str) -> str | None:
    if field == "reason":
        return record.final.reason
    value = getattr(record.members[0], field)
    assert value is None or isinstance(value, str)
    return value


@pytest.mark.parametrize("field", ["name", "role", "sandbox", "reason"])
@pytest.mark.parametrize(
    "value,readable",
    [
        # condensed into an attachment in the log
        pytest.param(LONG_REASON, True, id="long"),
        # Inspect's resolution rewrites tc:// as attachment://
        pytest.param("tc://artifact", False, id="tc"),
        pytest.param("tc://" + LONG_REASON, False, id="long-tc"),
        # a log would resolve it to OTHER
        pytest.param(f"attachment://{mm3_hash(OTHER)}", False, id="attachment-hash"),
        # removed from a log that does not log images
        pytest.param("data:;base64,SGVsbG8=", False, id="base64"),
    ],
)
def test_a_record_reads_the_same_live_and_from_the_log(
    tmp_path: Path, field: str, value: str, readable: bool
) -> None:
    if field == "name" and "/" in value:
        # member() refuses such a name before any record holds it
        with pytest.raises(ValueError, match="must not contain '/'"):
            _carrying(field, value)
        return
    # the same outcome whether or not the eval logs images
    for log_images in (True, False):
        after_end: list[Read] = []
        final: list[tuple[bool, Read]] = []
        solver = catches_the_end(
            _carrying(field, value), None if readable else UNWRITABLE, after_end
        )
        names = ("default", value, "b-box") if field == "sandbox" else ("default",)
        log_dir = tmp_path / str(log_images)
        _eval(
            log_dir,
            [logs_other(), solver],
            [probe(final)],
            log_images=log_images,
            sandbox=SandboxEnvironmentSpec("swarm_test_stub", StubConfig(names=names)),
        )
        [(_, live)] = final
        path = str(next(log_dir.glob("*.eval")))
        resolved = read_eval_log(path, resolve_attachments=True)
        unresolved = read_eval_log(path)
        assert resolved.samples is not None and unresolved.samples is not None
        reads = [
            *after_end,
            live,
            _read(resolved.samples[0]),
            _read(unresolved.samples[0]),
            _rescore(unresolved, unresolved.samples[0]),
        ]
        if readable:
            for read in reads:
                assert _field(_record(read), field) == value
        else:
            # the swarm refuses to write it, so no read gives other text
            assert not _record_events(unresolved.samples[0].events)
            for read in reads:
                _value_error(read, LOST_ID)


def test_a_record_whose_attachments_are_missing_is_refused(tmp_path: Path) -> None:
    log = _log(tmp_path, _chooses())
    without = read_eval_log(log.location, exclude_fields={"attachments"})
    assert without.samples is not None
    [no_attachments] = without.samples
    assert no_attachments.attachments == {}
    partial = no_attachments.model_copy(update={"attachments": {"other": "text"}})
    for refused in (no_attachments, partial):
        _value_error(_read(refused), "attachments")
        # re-scoring leaves the reference in the seeded transcript
        _value_error(_rescore(log, refused), "attachments")


def test_a_sample_read_without_its_events_is_refused(tmp_path: Path) -> None:
    path = _log(tmp_path, _pair()).location
    # without the timelines too, which refer to the events and fail to load without them
    log = read_eval_log(path, exclude_fields={"events", "timelines"})
    assert log.samples is not None
    _value_error(_read(log.samples[0]), "without its events")


@solver
def forge(payload: dict[str, Any], id: bool = True, point: bool = True) -> Solver:
    """Write a record event by hand, with an id in its payload unless `id` is false, and the id in the store if `point`."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        data: dict[str, Any] = {"type": RECORD_TYPE, **payload}
        if id:
            data["id"] = "forged"
        transcript().info(data, source="inspect_swarm")
        if point:
            store().set(RESULT_ID_KEY, "forged")
        return state

    return solve


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "version": 2,
                "members": [],
                "final": {
                    "mode": None,
                    "member": None,
                    "reason": "no_submissions",
                    "provisional": False,
                },
            },
            id="version-2",
        ),
        pytest.param({"version": 1, "members": "a, b", "final": {}}, id="malformed"),
    ],
)
def test_a_record_this_inspect_swarm_cannot_read_is_refused(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    sample = _eval(tmp_path, forge(payload), [reads()])
    # final scoring fails with the reader's error
    assert sample.error is not None
    assert SAMPLE in sample.error.message and "can read" in sample.error.message
    _value_error(_read(sample), "can read")
    log = _log(tmp_path / "rescore", forge(payload))
    assert log.samples is not None
    _value_error(_rescore(log, log.samples[0]), "can read")


def test_a_plain_agent_has_no_record(tmp_path: Path) -> None:
    seen: list[tuple[bool, Read]] = []
    sample = _eval(tmp_path, plain(), [probe(seen)])
    assert seen == [(True, None)]
    assert swarm_result(sample) is None


@solver
def after_swarm(inner: Solver, then: Solver) -> Solver:
    """Run the swarm, then `then`."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state = await inner(state, generate)
        return await then(state, generate)

    return solve


def test_a_record_event_without_an_id_is_skipped(tmp_path: Path) -> None:
    # added after the swarm's record, which it does not hide
    seen: list[tuple[bool, Read]] = []
    no_id = forge({"version": 1}, id=False, point=False)
    sample = _eval(tmp_path, after_swarm(as_solver(_pair()), no_id), [probe(seen)])
    [(_, read)] = seen
    assert _names(read) == ["a", "b"]
    assert _names(_read(sample)) == ["a", "b"]
    # the store's id names only such an event: the record's own event lost its id
    seen.clear()
    lost = _eval(tmp_path / "lost", forge({"version": 1}, id=False), [probe(seen)])
    [(_, read)] = seen
    _value_error(read, "no such event")
    _value_error(_read(lost), "no such event")


@solver
def writes_the_old_record() -> Solver:
    """Stand in for the runtime that kept the record in the store."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        member = {
            "role": None,
            "state": "ended",
            "status": "submitted",
            "submitted": True,
            "time": 1.0,
        }
        store().set(
            "inspect_swarm:result",
            {
                "version": 1,
                "members": [{"name": "a", **member}, {"name": "b", **member}],
                "final": {
                    "mode": "first",
                    "member": "a",
                    "reason": None,
                    "provisional": False,
                },
            },
        )
        return state

    return solve


def test_a_log_from_before_the_record_moved_is_refused(tmp_path: Path) -> None:
    seen: list[tuple[bool, Read]] = []
    sample = _eval(tmp_path, writes_the_old_record(), [probe(seen)])
    [(_, read)] = seen
    _value_error(read, "inspect_swarm:result")
    _value_error(_read(sample), "inspect_swarm:result")
    log = read_eval_log(str(next(tmp_path.glob("*.eval"))))
    assert log.samples is not None
    _value_error(_rescore(log, log.samples[0]), "inspect_swarm:result")


def test_a_sample_whose_store_lost_the_id_is_refused(tmp_path: Path) -> None:
    log = _log(tmp_path, _pair())
    assert log.samples is not None
    [logged] = log.samples
    recovered = logged.model_copy(update={"store": {}})
    # and a process that died after a member ended, before the record
    record_event = _record_events(logged.events)[0]
    died = recovered.model_copy(
        update={"events": [e for e in logged.events if e is not record_event]}
    )
    for refused in (recovered, died):
        _value_error(_read(refused), LOST_ID)
        _value_error(_rescore(log, refused), LOST_ID)


@solver
def forks_with_a_plain_branch() -> Solver:
    """Run a plain agent and a swarm in forked branches; return the plain agent's."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        plain_state, _ = await fork(state, [as_solver(plain()), as_solver(_pair())])
        return plain_state

    return solve


def test_a_plain_branch_beside_a_forked_swarm_is_refused(tmp_path: Path) -> None:
    seen: list[tuple[bool, Read]] = []
    _eval(tmp_path, forks_with_a_plain_branch(), [probe(seen)])
    [(_, read)] = seen
    _value_error(read, LOST_ID)


@agent
def waits(started: anyio.Event, release: anyio.Event) -> Agent:
    """Say that it has started, then wait for `release`."""

    async def execute(state: AgentState) -> AgentState:
        started.set()
        await release.wait()
        return state

    return execute


async def _interrupted_events() -> list[Event]:
    # a swarm with no channels, cancelled once both members have started and before
    # either has ended
    started = [anyio.Event(), anyio.Event()]
    never = anyio.Event()
    agent_swarm = swarm(
        [member(waits(e, never), name=n) for e, n in zip(started, "ab", strict=True)],
        channels=[],
    )
    events: list[Event] = []
    async with sample() as t:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run, agent_swarm, "task")
            for event in started:
                await event.wait()
            # what recovery keeps of a process that died now: events, and no store
            events = list(t.events)
            tg.cancel_scope.cancel()
    return events


def test_a_swarm_interrupted_before_any_member_ended_is_refused(
    tmp_path: Path,
) -> None:
    events = anyio.run(_interrupted_events)
    assert not _record_events(events)
    snapshot = EvalSample(id="s1", epoch=1, input="task", target="", events=events)
    without_start = snapshot.model_copy(
        update={
            "events": [
                e
                for e in events
                if not (isinstance(e, InfoEvent) and e.source == "inspect_swarm")
            ]
        }
    )
    assert len(without_start.events) == len(events) - 1
    log = _log(tmp_path, plain())
    _value_error(_read(snapshot), LOST_ID)
    _value_error(_rescore(log, snapshot), LOST_ID)
    # what the start event prevents
    assert _read(without_start) is None
    assert _rescore(log, without_start) is None


# ---------------------------------------------------------------------------
# The start event


def _starts_later() -> Agent:
    async def run(control: SwarmControl) -> StopReason:
        await anyio.sleep(0.01)
        control.start("a")
        async for _ in control.events():
            pass
        control.start("b")
        async for _ in control.events():
            pass
        return "all_done"

    return swarm(
        [
            member(generator(scripted_model()), name="a"),
            member(generator(scripted_model()), name="b"),
        ],
        controller=_controller(run, "record_test_starts_later"),
        channels=[],
    )


@pytest.mark.anyio
@pytest.mark.parametrize("later", [False, True])
async def test_the_start_event_precedes_every_member(later: bool) -> None:
    async with sample() as t:
        await run(_starts_later() if later else _pair(), "task")
    events = list(t.events)
    [start] = [
        e
        for e in events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("type") == START_TYPE
    ]
    assert start.source == "inspect_swarm"
    assert start.data == {"type": START_TYPE, "version": 1}
    # in the swarm's span, where the record is written
    [record] = _record_events(events)
    assert start.span_id == record.span_id
    members = [
        events.index(e)
        for e in events
        if isinstance(e, SpanBeginEvent) and e.name in ("a", "b")
    ]
    assert len(members) == 2
    assert events.index(start) < min(members)


# ---------------------------------------------------------------------------
# Reads during a run


@agent
def runs_a_nested_swarm(inner: Agent, then: Agent) -> Agent:
    """Run a nested swarm, then `then`."""

    async def execute(state: AgentState) -> AgentState:
        await run(inner, "inner task")
        return await then(state)

    return execute


@pytest.mark.parametrize(
    "build,calls,names",
    [
        pytest.param(lambda: _react_pair(""), 2, ["a", "b"], id="first-swarm"),
        pytest.param(
            lambda: [as_solver(_react_pair("1")), as_solver(_react_pair("2"))],
            4,
            ["2a", "2b"],
            id="two-in-sequence",
        ),
        pytest.param(
            lambda: swarm(
                member(
                    runs_a_nested_swarm(_react_pair("inner-"), _submitter()),
                    name="outer",
                ),
                channels=[],
            ),
            3,
            ["outer"],
            id="nested",
        ),
    ],
)
def test_a_read_while_a_swarm_in_the_sample_runs_raises(
    tmp_path: Path, build: Any, calls: int, names: list[str]
) -> None:
    seen: list[tuple[bool, Read]] = []
    sample = _eval(tmp_path, build(), [passes(), probe(seen)])
    assert sample.error is None, sample.error
    *in_loop, (completed, final) = seen
    assert len(in_loop) == calls
    for in_solve, read in in_loop:
        assert not in_solve
        assert isinstance(read, RuntimeError)
        assert "while a swarm in this sample is running" in str(read)
    # final scoring reads the last swarm to end: the second, or the outer one
    assert completed
    assert _names(final) == names


@pytest.mark.parametrize("order", ["after", "first", "alone"])
def test_a_guarded_scorer_skips_the_record_during_the_solve(
    tmp_path: Path, order: str
) -> None:
    script = Script(
        respond=lambda _: ModelOutput.for_tool_call(
            "mockllm", "submit", {"answer": "42"}
        )
    )
    agent_swarm = swarm(
        member(react(model=scripted_model(script), attempts=2), name="a"),
        channels=[],
    )
    scorers = {
        "after": [passes(), guarded()],
        "first": [guarded(), passes()],
        "alone": [guarded()],
    }[order]
    sample = _eval(tmp_path, agent_swarm, scorers)
    if order == "alone":
        # the attempt loop indexes the first score, and gets none
        assert sample.error is not None and "IndexError" in sample.error.traceback
        return
    assert sample.error is None, sample.error
    # the attempt loop saw the passing score, and stopped at the first submission
    assert script.calls == 1
    assert sample.scores is not None and sample.scores["guarded"].value == 1
    [metrics] = SwarmMetrics.model_validate(sample.store[METRICS_KEY]).members
    # gate checks count the first configured scorer's score events
    assert metrics.gate_checks == (1 if order == "after" else 0)


def _scores_live(seen: list[tuple[bool, Read]]) -> Agent:
    release = anyio.Event()
    started = anyio.Event()

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        await started.wait()
        active = sample_active()
        assert active is not None
        handle = TaskScoring(
            scorers=[probe(seen)],
            scorer_names=["probe"],
            model=get_model("mockllm/model", memoize=False),
            model_roles=None,
            generate_config=GenerateConfig(),
            epochs_reducer=None,
            metrics=None,
            score_on_error=False,
        )
        scores: dict[str, Any] = {}
        errors: dict[str, str] = {}
        # in a task of its own, since it binds the scoring context
        async with anyio.create_task_group() as tg:
            tg.start_soon(_score_live_sample, handle, active, scores, errors)
        release.set()
        async for _ in control.events():
            pass
        return "all_done"

    return swarm(
        member(waits(started, release), name="a"),
        controller=_controller(run, "record_test_live_scoring"),
        channels=[],
    )


def test_live_control_scoring_while_a_swarm_runs_raises(tmp_path: Path) -> None:
    # eval() runs on asyncio, as inspect_ai's live scoring tests do
    seen: list[tuple[bool, Read]] = []
    _eval(tmp_path, _scores_live(seen), [passes()])
    [(in_solve, read)] = seen
    assert not in_solve
    assert isinstance(read, RuntimeError)


@solver
def reads_after(inner: Agent, ending: str, seen: list[tuple[bool, Read]]) -> Solver:
    """Run the swarm as a solver, then read its record however it ended."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        swarm_solver = as_solver(inner)
        try:
            if ending == "cancel":
                started = False
                async with anyio.create_task_group() as tg:

                    async def go() -> None:
                        await swarm_solver(state, generate)

                    tg.start_soon(go)
                    # once the early member has ended, while the busy one runs
                    while not started:
                        await anyio.sleep(0.01)
                        started = any(_early_ended(e) for e in transcript().events)
                    tg.cancel_scope.cancel()
            else:
                state = await swarm_solver(state, generate)
        finally:
            seen.append((state.completed, swarm_result(state)))
        return state

    return solve


def _early_ended(event: Event) -> bool:
    return (
        isinstance(event, InfoEvent)
        and isinstance(event.data, dict)
        and event.data.get("kind") == "member_result"
        and event.data.get("member") == "early"
    )


@pytest.mark.parametrize("ending", ["normal", "limit", "cancel"])
def test_a_solver_reads_the_record_once_the_swarm_has_returned(
    tmp_path: Path, ending: str
) -> None:
    seen: list[tuple[bool, Read]] = []
    agent_swarm = swarm(
        [
            member(generator(scripted_model(), answer="early"), name="early"),
            # slower than early, so early's is the first submission
            member(
                generator(
                    scripted_model(Script(delay=0.01)),
                    calls=1 if ending == "normal" else None,
                ),
                name="busy",
            ),
        ],
        controller=leaderless(stop_on_submission=False),
        channels=[],
    )
    _eval(
        tmp_path,
        reads_after(agent_swarm, ending, seen),
        [],
        message_limit=6 if ending == "limit" else None,
    )
    [(completed, read)] = seen
    # the shape a solver wrapper around the swarm relies on
    assert not completed
    record = _record(read)
    assert record.final.member == "early"
    assert record.final.provisional is (ending != "normal")


@pytest.mark.parametrize("writer", ["write_record", "write_ledger", "write_metrics"])
def test_a_failed_write_at_the_end_does_not_leave_the_swarm_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    def fail(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(f"inspect_swarm._controller._control.{writer}", fail)
    seen: list[Read] = []
    _eval(tmp_path, catches_the_end(_pair(), "disk full", seen), [])
    [read] = seen
    if writer == "write_record":
        # no record, but the swarm's events
        _value_error(read, LOST_ID)
    else:
        assert _names(read) == ["a", "b"]


def _fail_second_start(monkeypatch: pytest.MonkeyPatch) -> None:
    # the first swarm's start is written, the second one's (or a nested one's) fails
    calls: list[None] = []

    def fail_second() -> None:
        calls.append(None)
        if len(calls) == 2:
            raise OSError("disk full")
        write_start()

    monkeypatch.setattr("inspect_swarm._controller._control.write_start", fail_second)


@pytest.mark.parametrize("failure", ["start", "record"])
def test_a_swarm_that_leaves_no_record_leaves_no_earlier_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # the second swarm's output is the sample's, so the first's record is not its record
    seen: list[Read] = []
    if failure == "start":
        _fail_second_start(monkeypatch)
        second = catches_the_end(_pair("second-"), "disk full", seen)
    else:
        second = catches_the_end(_chooses("tc://artifact"), UNWRITABLE, seen)
    _eval(tmp_path, [as_solver(_pair("first-")), second], [])
    [read] = seen
    _value_error(read, LOST_ID)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["start", "record", "record-after-an-error"])
async def test_a_swarm_that_fails_at_either_end_keeps_its_accounting(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def run_controller(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            pass
        control.choose_answer("a", reason="tc://artifact")
        if failure == "record-after-an-error":
            raise RuntimeError("controller-failure")
        return "chosen"

    def fail() -> None:
        raise OSError("disk full")

    if failure == "start":
        monkeypatch.setattr("inspect_swarm._controller._control.write_start", fail)
    agent_swarm = swarm(
        [
            member(generator(scripted_model()), name="a"),
            member(generator(scripted_model()), name="b"),
        ],
        controller=_controller(run_controller, f"record_test_end_{uuid4().hex}"),
        channels=[],
    )
    error = {
        "start": "disk full",
        "record": UNWRITABLE,
        "record-after-an-error": "controller-failure",
    }[failure]
    async with sample() as t:
        observers = len(t._event_loggers)
        with pytest.raises((OSError, ValueError, RuntimeError), match=error) as raised:
            await run(agent_swarm, "task")
        # the swarm stops observing the transcript however it ends
        assert len(t._event_loggers) == observers
        ledger, metrics = store().get(LEDGER_KEY), store().get(METRICS_KEY)
    if failure == "start":
        # no member ran, so there is nothing to account for
        assert (ledger, metrics) == (None, None)
        return
    assert [m.name for m in SwarmLedger.model_validate(ledger).members] == ["a", "b"]
    assert metrics is not None
    if failure == "record-after-an-error":
        # the error that ended the swarm, with the record's
        assert any(UNWRITABLE in note for note in raised.value.__notes__)


@agent
def nests_a_failing_swarm(inner: Agent, seen: list[bool]) -> Agent:
    """Run a nested swarm whose end fails, then say whether the outer swarm's contexts are back."""

    async def execute(state: AgentState) -> AgentState:
        before = (_time_cap(), current_bus.get())
        with pytest.raises((OSError, ValueError), match=f"disk full|{UNWRITABLE}"):
            await run(inner, "inner task")
        budget, bus = before
        seen.append(_time_cap() == budget and current_bus.get() is bus)
        return state

    return execute


def _time_cap() -> float | None:
    time = swarm_budget().time
    return None if time is None else time.cap


@pytest.mark.anyio
@pytest.mark.parametrize("writer", ["write_start", "write_record"])
async def test_a_failed_write_of_a_nested_swarm_restores_the_outer_contexts(
    monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    if writer == "write_start":
        # the outer swarm's start is written, the inner one's fails
        _fail_second_start(monkeypatch)
        inner = swarm(member(plain(), name="i"), channels=[], budget=Budget(time=50))
    else:
        inner = swarm(
            member(plain(), name="i"),
            controller=_chooses_alone("tc://artifact"),
            channels=["shared_memory"],
            budget=Budget(time=50),
        )
    seen: list[bool] = []
    outer = swarm(
        member(nests_a_failing_swarm(inner, seen), name="o"),
        channels=["shared_memory"],
        budget=Budget(time=100),
    )
    async with sample():
        await run(outer, "task")
    assert seen == [True]


def _chooses_alone(reason: str) -> Controller:
    async def run(control: SwarmControl) -> StopReason:
        control.start("i")
        async for _ in control.events():
            pass
        control.choose_answer("i", reason=reason)
        return "chosen"

    return _controller(run, f"record_test_alone_{uuid4().hex}")


# ---------------------------------------------------------------------------
# Forks and subtasks


@solver
def forks_two_swarms(seen: list[Read]) -> Solver:
    """Run two swarms in forked branches, B ending last; return A's state."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        a_state, b_state = await fork(
            state,
            [as_solver(_pair("A")), as_solver(_pair("B", Script(delay=0.2)))],
        )
        seen.append(swarm_result(b_state))
        return a_state

    return solve


def test_each_forked_branch_reads_its_own_record(tmp_path: Path) -> None:
    branch: list[Read] = []
    seen: list[tuple[bool, Read]] = []
    sample = _eval(tmp_path, forks_two_swarms(branch), [probe(seen)])
    # B ended last
    _, last = _record_events(sample.events)
    assert [m.name for m in _payload_record(last).members] == ["Ba", "Bb"]
    [(_, read)] = seen
    assert _names(read) == ["Aa", "Ab"]
    assert _names(_read(sample)) == ["Aa", "Ab"]
    [b_read] = branch
    assert _names(b_read) == ["Ba", "Bb"]


def _payload_record(event: InfoEvent) -> SwarmResultRecord:
    assert isinstance(event.data, dict)
    data = {k: v for k, v in event.data.items() if k not in ("type", "id")}
    return SwarmResultRecord.model_validate(data)


@solver
def runs_a_swarm_in_a_subtask(seen: list[Read]) -> Solver:
    """Run a swarm in a subtask, reading the child's record there."""

    @subtask
    async def child() -> str:
        await run(_pair("child-"), "task")
        seen.append(_transcript_result(store()))
        return "done"

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await child()
        return state

    return solve


def test_a_parent_does_not_read_a_subtasks_record(tmp_path: Path) -> None:
    child: list[Read] = []
    seen: list[tuple[bool, Read]] = []
    _eval(tmp_path, runs_a_swarm_in_a_subtask(child), [probe(seen)])
    [child_read] = child
    assert _names(child_read) == ["child-a", "child-b"]
    [(_, read)] = seen
    _value_error(read, LOST_ID)
