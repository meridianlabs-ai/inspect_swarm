import asyncio
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._eval.task.log import plan_to_eval_plan
from inspect_ai._util.registry import create_registry_object, registry_info
from inspect_ai.agent import Agent, AgentState, agent, as_solver, react
from inspect_ai.agent import run as run_agent
from inspect_ai.dataset import Sample
from inspect_ai.event import SpanBeginEvent, SpanEndEvent, SwarmEvent
from inspect_ai.log import read_eval_log
from inspect_ai.log import transcript as current_transcript
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelOutput,
    get_model,
)
from inspect_ai.solver import Plan
from inspect_ai.util import (
    LimitExceededError,
    message_limit,
    span_id_provider,
    store,
    time_limit,
    token_limit,
)

from inspect_swarm import (
    Budget,
    Controller,
    FinalSpec,
    ResultSpec,
    StopReason,
    SwarmControl,
    Verdict,
    answer_result,
    artifact_result,
    leaderless,
    member,
    swarm,
    swarm_controller,
    swarm_result,
)
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._final import (
    Reporter,
    Synthesize,
    answer,
    check_final,
    final_chain,
    first,
    last,
)
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._record import (
    RECORD_TYPE,
    RESULT_ID_KEY,
    MemberResult,
    SwarmResultRecord,
    _record_data,
    _transcript_result,
)

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Outcome,
    Script,
    after,
    final,
    generator,
    output,
    run_swarm,
    sample,
    scripted_model,
    sleeper,
    statuses,
    swarm_events,
)


def _controller(run: Any, name: str) -> Controller:
    @swarm_controller(name=name)
    def factory() -> Controller:
        return Controller(run)

    return factory()


# the record, read in a sample context with no TaskState, after the swarm has ended
def _record() -> SwarmResultRecord:
    record = _transcript_result(store())
    assert record is not None
    return record


def _logged(record: SwarmResultRecord | None) -> SwarmResultRecord:
    assert record is not None
    return record


# ---------------------------------------------------------------------------
# first


def _result(name: str, status: str | None, time: float | None) -> MemberResult:
    return MemberResult(
        name=name,
        role=None,
        state="pending" if status is None else "ended",
        status=status,
        submitted=status == "submitted",
        time=time,
    )


@pytest.mark.parametrize(
    "members,expected",
    [
        pytest.param(
            [("a", "submitted", 2.0), ("b", "submitted", 1.0)], "b", id="earliest"
        ),
        pytest.param(
            [("a", "submitted", 1.0), ("b", "submitted", 1.0)], "a", id="roster-order"
        ),
        pytest.param(
            [
                ("unsubmitted", "unsubmitted", 0.1),
                ("stopped", "stopped", 0.1),
                ("limited", "member_limit:token", 0.1),
                ("capped", "swarm_cap", 0.1),
                ("cancelled", "cancelled", 0.1),
                ("errored", "errored", 0.1),
                ("pending", None, None),
                ("late", "submitted", 9.0),
            ],
            "late",
            id="only-submissions",
        ),
        pytest.param(
            [("stopped", "stopped", 0.1), ("pending", None, None)],
            None,
            id="no-submissions",
        ),
    ],
)
def test_first_is_the_earliest_submission_then_roster_order(
    members: list[tuple[str, str | None, float | None]], expected: str | None
) -> None:
    chosen = first([_result(*m) for m in members])
    assert (chosen.name if chosen is not None else None) == expected


@pytest.mark.parametrize(
    "members,expected",
    [
        pytest.param(
            [("a", "submitted", 1.0), ("b", "submitted", 2.0)], "b", id="latest"
        ),
        pytest.param(
            [("a", "submitted", 2.0), ("b", "submitted", 2.0)], "a", id="roster-order"
        ),
        pytest.param(
            [
                ("early", "submitted", 0.1),
                ("unsubmitted", "unsubmitted", 9.0),
                ("stopped", "stopped", 9.0),
                ("errored", "errored", 9.0),
                ("pending", None, None),
            ],
            "early",
            id="only-submissions",
        ),
        pytest.param(
            [("stopped", "stopped", 0.1), ("pending", None, None)],
            None,
            id="no-submissions",
        ),
    ],
)
def test_last_is_the_latest_submission_then_roster_order(
    members: list[tuple[str, str | None, float | None]], expected: str | None
) -> None:
    chosen = last([_result(*m) for m in members])
    assert (chosen.name if chosen is not None else None) == expected


def test_first_skips_a_react_member_that_gave_up_after_refusals(
    tmp_path: Path,
) -> None:
    refusals = Script(
        outputs=[
            ModelOutput.from_content(
                "mockllm", "I can't help with that.", stop_reason="content_filter"
            )
            for _ in range(3)
        ]
    )
    # slower, so it submits after the refusing member has ended
    solver = Script(
        outputs=[ModelOutput.for_tool_call("mockllm", "submit", {"answer": "42"})],
        delay=0.1,
    )
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            [
                member(react(model=scripted_model(refusals)), name="refuser"),
                member(react(model=scripted_model(solver)), name="solver"),
            ],
            channels=[],
        ),
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.samples is not None
    [sample] = log.samples
    record = _logged(swarm_result(sample))
    refuser, solved = record.members
    assert (refuser.status, refuser.submitted) == ("unsubmitted", False)
    assert solved.status == "submitted"
    assert refuser.time is not None and solved.time is not None
    assert refuser.time < solved.time
    assert record.final.member == "solver"
    assert sample.output.completion.endswith("42")


@pytest.mark.anyio
async def test_no_submissions_give_an_empty_output_though_members_have_outputs() -> (
    None
):
    scripts = [Script(), Script()]

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        while any(script.calls < 1 for script in scripts):
            await anyio.sleep(0)
        return "enough"

    stop = await run_swarm(
        swarm(
            [
                member(generator(scripted_model(script), calls=None), name=name)
                for name, script in zip(["a", "b"], scripts, strict=True)
            ],
            controller=_controller(run, "final_test_no_submissions"),
            channels=[],
        )
    )
    assert statuses(stop) == {"a": "stopped", "b": "stopped"}
    assert stop.state.output.empty
    assert stop.record.final.model_dump() == {
        "mode": None,
        "member": None,
        "reason": "no_submissions",
        "provisional": False,
        "answer": None,
        "chain": ["first"],
        "tie": False,
    }


@agent
def waiter(gate: anyio.Event, answer: str) -> Agent:
    # answers once the gate is set, without a model call, so a soft stop does not end it
    async def execute(state: AgentState) -> AgentState:
        await gate.wait()
        state.output = ModelOutput.from_content("mockllm", answer)
        return state

    return execute


# ---------------------------------------------------------------------------
# The provisional answer


@pytest.mark.anyio
async def test_the_provisional_answer_is_set_at_the_first_submission() -> None:
    seen: list[tuple[str, list[str]]] = []
    state = AgentState(messages=[ChatMessageUser(content="task")])
    gate = anyio.Event()

    async def run(control: SwarmControl) -> StopReason:
        control.start("quick")
        control.start("slow")
        async for event in control.events():
            if event.member == "quick":
                states = [m.state for m in control.members]
                seen.append((state.output.completion, states))
                gate.set()
        return "all_done"

    agent_swarm = swarm(
        [
            member(generator(scripted_model(), calls=0, answer="quick"), name="quick"),
            member(waiter(gate, "slow"), name="slow"),
        ],
        controller=_controller(run, "final_test_provisional"),
        channels=[],
    )
    async with sample():
        await agent_swarm(state)
        record = _record()
    [(completion, states)] = seen
    assert completion == "quick"
    assert states == ["ended", "running"]
    assert (record.final.mode, record.final.member) == ("first", "quick")


def _limited_task(*, early_submission: bool) -> Task:
    # the sample's message limit ends the busy member, and so the sample
    members = [
        member(generator(scripted_model(Script(delay=0.01)), calls=None), name="busy")
    ]
    if early_submission:
        members.insert(
            0,
            member(
                generator(scripted_model(), calls=1, answer="early answer"),
                name="early",
            ),
        )
    return Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            members, controller=leaderless(stop_on_submission=False), channels=[]
        ),
        message_limit=6,
    )


@pytest.mark.parametrize("early_submission", [True, False])
def test_an_outer_limit_leaves_the_provisional_answer(
    tmp_path: Path, early_submission: bool
) -> None:
    [log] = eval(
        _limited_task(early_submission=early_submission),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.samples is not None
    [sample] = log.samples
    assert sample.limit is not None and sample.limit.type == "message"
    record = _logged(swarm_result(sample))
    assert record.final.provisional
    if early_submission:
        assert sample.output.completion == "early answer"
        assert record.final.member == "early"
    else:
        assert sample.output.empty
        assert (record.final.member, record.final.reason) == (None, "no_submissions")


# ---------------------------------------------------------------------------
# Finalisation


def test_an_answer_finalisation_cannot_fit_ends_the_sample_on_its_message_limit(
    tmp_path: Path,
) -> None:
    # the member's own message limit lets it submit; the swarm's conversation, the
    # input and the answer, is over the sample's
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            member(
                generator(scripted_model(), calls=0, answer="42"),
                name="a",
                limits=[message_limit(10)],
            ),
            channels=[],
        ),
        message_limit=1,
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.samples is not None
    [sample] = log.samples
    assert sample.limit is not None and sample.limit.type == "message"
    assert sample.output.completion == "42"
    record = _logged(swarm_result(sample))
    [a] = record.members
    assert (a.state, a.status) == ("ended", "submitted")
    assert (record.final.member, record.final.provisional) == ("a", True)
    # the ledger is written after the final step, which the sample's limit ended
    ledger = SwarmLedger.model_validate(sample.store[LEDGER_KEY])
    assert (ledger.stop_reason, ledger.ended_by, ledger.interrupted) == (
        "submitted",
        "sample_limit:message",
        True,
    )
    assert not [
        e for e in sample.events if isinstance(e, SwarmEvent) and e.kind == "final"
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("earlier", [True, False])
async def test_a_submission_during_grace_is_chosen_only_without_an_earlier_one(
    earlier: bool,
) -> None:
    gate = anyio.Event()

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        if earlier:
            async for event in control.events():
                if event.member == "early":
                    break
        # released as the stop begins, so the late member answers in grace
        gate.set()
        return "enough"

    members = [member(waiter(gate, "late"), name="late")]
    if earlier:
        members.append(
            member(generator(scripted_model(), calls=0, answer="early"), name="early")
        )
    stop = await run_swarm(
        swarm(
            members,
            controller=_controller(run, f"final_test_grace_{earlier}"),
            channels=[],
            budget=Budget(grace=5),
        )
    )
    assert stop.reason == "controller:enough"
    assert set(statuses(stop).values()) == {"submitted"}
    expected = "early" if earlier else "late"
    assert stop.record.final.model_dump() == {
        "mode": "first",
        "member": expected,
        "reason": None,
        "provisional": False,
        "answer": None,
        "chain": ["first"],
        "tie": False,
    }
    assert stop.state.output.completion == expected
    assert final(stop.transcript).member == expected


# ---------------------------------------------------------------------------
# The record


# whether the transcript holds a record event and the store a record id
def _live_record() -> tuple[bool, bool]:
    return (
        any(_record_data(e) is not None for e in current_transcript().events),
        RESULT_ID_KEY in store(),
    )


@agent
def looks_for_the_record(seen: list[tuple[bool, bool]]) -> Agent:
    # looks for the record from the member's context, as a member's tool would
    async def execute(state: AgentState) -> AgentState:
        seen.append(_live_record())
        state.output = ModelOutput.from_content("mockllm", "42")
        return state

    return execute


@pytest.mark.anyio
async def test_the_record_is_written_once_when_the_swarm_ends() -> None:
    seen: list[tuple[bool, bool]] = []
    probed: list[tuple[bool, bool]] = []

    async def run(control: SwarmControl) -> StopReason:
        seen.append(_live_record())
        control.start("started")
        seen.append(_live_record())
        async for _ in control.events():
            seen.append(_live_record())
        control.choose_answer("started", reason="the only one")
        return "all_done"

    agent_swarm = swarm(
        [
            member(looks_for_the_record(probed), name="started", role="lead"),
            member(generator(scripted_model(), calls=1), name="never"),
        ],
        controller=_controller(run, "final_test_record_once"),
        channels=[],
    )
    async with sample() as transcript:
        await run_agent(agent_swarm, "task")
        record_id = store().get(RESULT_ID_KEY)
        record = _record()
    # at the start, after the start and after the end, and from the member's tool
    assert seen == [(False, False)] * 3
    assert probed == [(False, False)]
    swarm_evidence = [e for e in transcript.events if isinstance(e, SwarmEvent)]
    kinds = [e.kind for e in swarm_evidence]
    assert kinds[-4:] == ["final", RECORD_TYPE, "metrics", "ledger"]
    [event] = [e for e in swarm_evidence if e.kind == RECORD_TYPE]
    # in the swarm's span, where the final event is written
    assert event.span_id == final(transcript).data["span_id"]
    assert event.member is None
    assert event.data["id"] == record_id
    members = event.data["members"]
    assert isinstance(members, list) and isinstance(members[0], dict)
    assert set(members[0]) == {
        "name",
        "role",
        "choose_name",
        "state",
        "status",
        "submitted",
        "time",
        "sandbox",
        "gate_checks",
        "gate_passes",
        "first_pass_time",
        "output",
        "answer_key",
        "verdict",
        "verdict_time",
        "left_out",
        "persistent",
        "answer_times",
        "end_origin",
    }
    started, never = record.members
    assert (started.state, started.status, started.submitted, started.role) == (
        "ended",
        "submitted",
        True,
        "lead",
    )
    assert started.time is not None and started.time >= 0
    # a member the controller never started stays pending
    assert (never.state, never.status, never.time) == ("pending", None, None)
    assert record.final.model_dump() == {
        "mode": "controller",
        "member": "started",
        "reason": "the only one",
        "provisional": False,
        "answer": None,
        "chain": ["first"],
        "tie": False,
    }


async def _blocked_provider(name: str, parent_id: str | None, id: str | None) -> str:
    # a span-id provider that never answers for the member's span
    if name == "blocked":
        await anyio.sleep_forever()
    return id or name


@pytest.mark.anyio
async def test_a_member_time_limit_bounds_entering_its_span() -> None:
    with anyio.fail_after(10), span_id_provider(_blocked_provider):
        stop = await run_swarm(
            swarm(
                member(sleeper(), name="blocked", limits=[time_limit(0.05)]),
                channels=[],
            )
        )
    assert stop.reason == "all_done"
    assert statuses(stop) == {"blocked": "member_limit:time"}


@pytest.mark.anyio
async def test_a_member_blocked_entering_its_span_is_hard_stopped() -> None:
    async def run(control: SwarmControl) -> StopReason:
        control.start("blocked")
        return "enough"

    with anyio.fail_after(10), span_id_provider(_blocked_provider):
        stop = await run_swarm(
            swarm(
                member(sleeper(), name="blocked"),
                controller=_controller(run, "final_test_blocked_span"),
                channels=[],
                budget=Budget(grace=0),
            )
        )
    [blocked] = stop.record.members
    assert (blocked.state, blocked.status) == ("ended", "cancelled")
    [result] = swarm_events(stop.transcript, "member_result")
    assert (result.member, result.data["status"], result.data["span_id"]) == (
        "blocked",
        "cancelled",
        None,
    )


@pytest.mark.anyio
async def test_evidence_names_members_and_spans_and_holds_no_member_text() -> None:
    secret = "SECRET-ANSWER-1729"
    model = scripted_model(Script(outputs=[output(secret)]))
    busy = Script()

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        ended: set[str] = set()
        async for event in control.events():
            ended.add(event.member)
            if ended == {"a", "limited"}:
                break
        while busy.calls < 1:
            await anyio.sleep(0)
        return "enough"

    stop = await run_swarm(
        swarm(
            [
                member(generator(model, calls=1), name="a"),
                member(generator(scripted_model(busy), calls=None), name="b"),
                member(
                    generator(scripted_model(), calls=None),
                    name="limited",
                    limits=[token_limit(TOKENS_PER_CALL)],
                ),
            ],
            controller=_controller(run, "final_test_evidence"),
            channels=[],
        )
    )
    events = stop.transcript.events
    spans = {
        e.name: e.id
        for e in events
        if isinstance(e, SpanBeginEvent) and e.type == "agent"
    }
    span_ends = {e.id: i for i, e in enumerate(events) if isinstance(e, SpanEndEvent)}
    results = {
        str(e.member): (i, e)
        for i, e in enumerate(events)
        if isinstance(e, SwarmEvent) and e.kind == "member_result"
    }
    assert statuses(stop) == {
        "a": "submitted",
        "b": "stopped",
        "limited": "member_limit:token",
    }
    for name, (i, e) in results.items():
        r = e.data
        assert r["version"] == 1
        assert r["status"] == statuses(stop)[name]
        # written in the member's span, before it ends
        assert r["span_id"] == e.span_id == spans[name]
        assert i < span_ends[spans[name]]
    event = final(stop.transcript)
    assert (event.member, event.data) == (
        "a",
        {
            "version": 1,
            "span_id": spans["swarm"],
            "mode": "first",
            "reason": None,
            "stop_reason": "controller:enough",
            "answer": None,
        },
    )
    assert event.span_id == spans["swarm"]
    assert secret not in repr([*(e for _, e in results.values()), event])


# ---------------------------------------------------------------------------
# The returned state


@agent
def blank_completion() -> Agent:
    # an output whose completion is empty on purpose, though its message has text
    async def execute(state: AgentState) -> AgentState:
        state.output = ModelOutput.from_content("mockllm", "working text")
        state.output.completion = ""
        return state

    return execute


@pytest.mark.anyio
async def test_the_answer_keeps_its_completion() -> None:
    async with sample():
        state = await swarm(blank_completion(), channels=[])(AgentState(messages=[]))
        [recorded] = _record().members
    assert state.output.completion == ""
    assert recorded.output is not None and recorded.output.completion == ""


def test_the_record_read_from_a_log_keeps_an_empty_completion(tmp_path: Path) -> None:
    task = Task(
        dataset=[Sample(input="task")], solver=swarm(blank_completion(), channels=[])
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    samples = read_eval_log(log.location).samples
    assert samples is not None
    [recorded] = _logged(swarm_result(samples[0])).members
    assert recorded.output is not None and recorded.output.completion == ""


@agent
def answerer(outputs: list[ModelOutput], answer: str) -> Agent:
    # records the output it returns
    async def execute(state: AgentState) -> AgentState:
        state.output = ModelOutput.from_content("mockllm", answer)
        state.output.message.metadata = {"own": True}
        outputs.append(state.output)
        return state

    return execute


@pytest.mark.anyio
async def test_the_returned_state_is_the_input_and_the_chosen_output() -> None:
    outputs: list[ModelOutput] = []
    stop: Outcome = await run_swarm(
        swarm(
            [
                member(answerer(outputs, "chosen"), name="a"),
                member(sleeper(0.01), name="b"),
            ],
            channels=[],
        ),
        input="the task",
    )
    [chosen] = outputs
    assert stop.state.output is chosen
    user, message = stop.state.messages
    assert isinstance(user, ChatMessageUser) and user.content == "the task"
    assert isinstance(message, ChatMessageAssistant) and message.text == "chosen"
    assert message.metadata == {
        "own": True,
        "inspect_swarm": {"final": {"mode": "first", "member": "a"}},
    }
    # the member's own message is unchanged
    assert chosen.message.metadata == {"own": True}


# ---------------------------------------------------------------------------
# final="last"


@pytest.mark.anyio
async def test_a_team_swarm_answers_with_its_last_submission() -> None:
    refusal = ModelOutput.from_content("mockllm", "no", stop_reason="content_filter")
    stop = await run_swarm(
        swarm(
            [
                member(generator(scripted_model(), calls=0, answer="a"), name="a"),
                member(
                    after(["a"], generator(scripted_model(), calls=0, answer="b")),
                    name="b",
                ),
                # ends last, without a submission
                member(
                    after(["b"], generator(scripted_model(Script([refusal])))),
                    name="refuser",
                ),
            ],
            controller=leaderless(stop_on_submission=False, final="last"),
            channels=[],
        )
    )
    assert stop.reason == "all_done"
    assert statuses(stop) == {
        "a": "submitted",
        "b": "submitted",
        "refuser": "unsubmitted",
    }
    assert stop.record.final.model_dump() == {
        "mode": "last",
        "member": "b",
        "reason": None,
        "provisional": False,
        "answer": None,
        "chain": ["last"],
        "tie": False,
    }
    assert (final(stop.transcript).data["mode"], final(stop.transcript).member) == (
        "last",
        "b",
    )
    assert stop.state.output.completion == "b"
    assert stop.state.messages[-1].metadata == {
        "inspect_swarm": {"final": {"mode": "last", "member": "b"}}
    }


def test_an_outer_limit_leaves_the_latest_submission_under_last(
    tmp_path: Path,
) -> None:
    # the sample's message limit ends the busy member, which starts generating once
    # both others have submitted
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            [
                member(generator(scripted_model(), calls=0, answer="a"), name="a"),
                member(
                    after(["a"], generator(scripted_model(), calls=0, answer="b")),
                    name="b",
                ),
                member(
                    after(["b"], generator(scripted_model(), calls=None)), name="busy"
                ),
            ],
            controller=leaderless(stop_on_submission=False, final="last"),
            channels=[],
        ),
        message_limit=6,
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.samples is not None
    [sample] = log.samples
    assert sample.limit is not None and sample.limit.type == "message"
    record = _logged(swarm_result(sample))
    assert (record.final.mode, record.final.member, record.final.provisional) == (
        "last",
        "b",
        True,
    )
    assert sample.output.completion == "b"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: leaderless(final="last"), id="last-stopping-at-first"),
        pytest.param(
            lambda: leaderless(stop_on_submission=False, final="best"),  # pyright: ignore[reportArgumentType]
            id="unknown-mode",
        ),
    ],
)
def test_a_final_rule_the_swarm_cannot_apply_is_refused(
    build: Callable[[], Controller],
) -> None:
    with pytest.raises(ValueError, match="final"):
        build()


# ---------------------------------------------------------------------------
# The controller's choice


@pytest.mark.anyio
async def test_a_controller_chooses_the_answer_in_place_of_its_final_rule() -> None:
    before: list[str] = []
    state = AgentState(messages=[ChatMessageUser(content="task")])

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for _ in control.events():
            pass
        before.append(state.output.completion)
        control.choose_answer(control.member("b"), reason="most complete")
        return "chosen"

    @swarm_controller(name="final_test_choose")
    def chooser() -> Controller:
        return Controller(run, final="last")

    agent_swarm = swarm(
        [
            member(generator(scripted_model(), calls=0, answer="a"), name="a"),
            member(
                after(["a"], generator(scripted_model(), calls=0, answer="b")),
                name="b",
            ),
            member(
                after(["b"], generator(scripted_model(), calls=0, answer="c")),
                name="c",
            ),
        ],
        controller=chooser(),
        channels=[],
    )
    async with sample() as transcript:
        await agent_swarm(state)
        record = _record()
    # until the choice, the controller's final rule
    assert before == ["c"]
    assert record.final.model_dump() == {
        "mode": "controller",
        "member": "b",
        "reason": "most complete",
        "provisional": False,
        "answer": None,
        "chain": ["last"],
        "tie": False,
    }
    event = final(transcript)
    assert (event.data["mode"], event.member, event.data["reason"]) == (
        "controller",
        "b",
        "most complete",
    )
    assert state.output.completion == "b"
    assert state.messages[-1].metadata == {
        "inspect_swarm": {"final": {"mode": "controller", "member": "b"}}
    }


@agent
def after_choice(chosen: anyio.Event, inner: Agent) -> Agent:
    # waits, making no model call, until the controller has chosen, then runs inner
    async def execute(state: AgentState) -> AgentState:
        await chosen.wait()
        return await inner(state)

    return execute


@pytest.mark.anyio
async def test_a_controllers_choice_is_the_provisional_answer_at_once() -> None:
    seen: dict[str, str] = {}
    state = AgentState(messages=[ChatMessageUser(content="task")])
    chosen = anyio.Event()

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for event in control.events():
            if event.member == "b":
                # `last` would answer with b
                control.choose_answer("a", reason="earlier")
                seen["chosen"] = state.output.completion
                chosen.set()
            elif event.member == "c":
                seen["later"] = state.output.completion
        return "all_done"

    @swarm_controller(name="final_test_choose_provisional")
    def chooser() -> Controller:
        return Controller(run, final="last")

    agent_swarm = swarm(
        [
            member(generator(scripted_model(), calls=0, answer="a"), name="a"),
            member(
                after(["a"], generator(scripted_model(), calls=0, answer="b")),
                name="b",
            ),
            # submits after the choice, so `last` alone would answer with it
            member(
                after_choice(chosen, generator(scripted_model(), calls=0, answer="c")),
                name="c",
            ),
            # still running when the sample's message limit ends the swarm
            member(after(["c"], generator(scripted_model(), calls=None)), name="busy"),
        ],
        controller=chooser(),
        channels=[],
    )
    # c waits on the choice, so it submits only after it
    with anyio.fail_after(10):
        async with sample(messages=6):
            with pytest.raises(LimitExceededError):
                await agent_swarm(state)
            record = _record()
    expected = {
        "mode": "controller",
        "member": "a",
        "reason": "earlier",
        "provisional": True,
        "answer": None,
        "chain": ["last"],
        "tie": False,
    }
    assert seen == {"chosen": "a", "later": "a"}
    # the sample's limit ended the swarm before finalisation
    assert state.output.completion == "a"
    assert record.final.model_dump() == expected


@pytest.mark.anyio
async def test_a_controller_chooses_once_from_a_submission() -> None:
    errors: list[BaseException] = []

    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        control.start("refuser")
        async for _ in control.events():
            pass
        # no such member, ended without a submission, never started, chosen twice
        for name in ["nobody", "refuser", "idle", "a", "a"]:
            try:
                control.choose_answer(name)
            except (KeyError, RuntimeError) as ex:
                errors.append(ex)
        return "chosen"

    refusal = ModelOutput.from_content("mockllm", "no", stop_reason="content_filter")
    stop = await run_swarm(
        swarm(
            [
                member(generator(scripted_model(), calls=0, answer="a"), name="a"),
                member(generator(scripted_model(Script([refusal]))), name="refuser"),
                member(sleeper(), name="idle"),
            ],
            controller=_controller(run, "final_test_choose_once"),
            channels=[],
        )
    )
    assert [type(e) for e in errors] == [
        KeyError,
        RuntimeError,
        RuntimeError,
        RuntimeError,
    ]
    assert "already chosen" in str(errors[-1])
    assert (stop.record.final.mode, stop.record.final.member) == ("controller", "a")


# ---------------------------------------------------------------------------
# The final-answer chain


def _submission(
    name: str,
    time: float,
    *,
    passed: bool | None = None,
    value: float | None = None,
    key: str | None = None,
) -> MemberResult:
    return MemberResult(
        name=name,
        role=None,
        state="ended",
        status="submitted",
        submitted=True,
        time=time,
        verdict=None if passed is None else Verdict(passed, value),
        answer_key=key,
    )


_PENDING = MemberResult(
    name="pending", role=None, state="pending", status=None, submitted=False, time=None
)


@pytest.mark.parametrize(
    "chain,members,expected",
    [
        pytest.param(
            ["verify"],
            [
                _submission("a", 1.0, passed=True, value=0.5),
                _submission("b", 2.0, passed=True, value=0.9),
                _submission("c", 0.5, passed=False, value=1.0),
            ],
            ("verify", "b", None, False),
            id="verify-best-value",
        ),
        pytest.param(
            ["verify"],
            [
                _submission("a", 1.0, passed=True),
                _submission("b", 2.0, passed=True, value=-5.0),
            ],
            ("verify", "b", None, False),
            id="verify-no-value-ranks-below-any-number",
        ),
        pytest.param(
            ["verify"],
            [
                _submission("a", 2.0, passed=True, value=math.nan),
                _submission("b", 1.0, passed=True),
            ],
            ("verify", "b", None, False),
            id="verify-nan-ranks-as-no-value",
        ),
        pytest.param(
            ["verify"],
            [
                _submission("a", 2.0, passed=True, value=1.0),
                _submission("b", 1.0, passed=True, value=1.0),
                _submission("c", 1.0, passed=True, value=1.0),
            ],
            ("verify", "b", None, False),
            id="verify-ties-by-time-then-roster",
        ),
        pytest.param(
            ["verify"],
            [
                _submission("a", 1.0, passed=True),
                _submission("b", 2.0, passed=True, value=math.nan),
                _submission("c", 3.0, passed=True, value=-math.inf),
            ],
            ("verify", "c", None, False),
            id="verify-no-value-ranks-below-minus-infinity",
        ),
        pytest.param(
            ["verify"],
            [_submission("a", 1.0, passed=False), _submission("b", 2.0)],
            (None, None, "none_verified", False),
            id="verify-strict-none-passed",
        ),
        pytest.param(
            ["vote"],
            [
                _submission("a", 1.0, key="x"),
                _submission("b", 2.0, key="y"),
                _submission("c", 3.0, key="y"),
            ],
            ("vote", "b", None, False),
            id="vote-plurality",
        ),
        pytest.param(
            ["vote"],
            [_submission("a", 1.0), _submission("b", 2.0, key="y")],
            ("vote", "b", None, False),
            id="vote-no-key-abstains",
        ),
        pytest.param(
            ["vote"],
            [
                _submission("a", 3.0, key="x"),
                _submission("b", 2.0, key="y"),
                _submission("c", 4.0, key="x"),
                _submission("d", 5.0, key="y"),
            ],
            ("vote", "b", None, True),
            id="vote-tie-to-the-earliest-group",
        ),
        pytest.param(
            ["vote"],
            [_submission("a", 1.0), _submission("b", 2.0)],
            (None, None, "no_answer_keys", False),
            id="vote-strict-no-keys",
        ),
        pytest.param(
            ["verify", "vote", "first"],
            [
                _submission("a", 1.0, passed=False, key="x"),
                _submission("b", 2.0, passed=False, key="y"),
                _submission("c", 3.0, passed=False, key="y"),
            ],
            ("vote", "b", None, False),
            id="default-falls-through-to-vote",
        ),
        pytest.param(
            ["verify", "vote", "first"],
            [
                _submission("a", 2.0, passed=False),
                _submission("b", 1.0, passed=False),
            ],
            ("first", "b", None, False),
            id="default-falls-through-to-first",
        ),
        pytest.param(
            ["verify", "last"],
            [_submission("a", 1.0, passed=False), _submission("b", 2.0)],
            ("last", "b", None, False),
            id="verify-then-last",
        ),
        pytest.param(
            ["verify", "vote", "first"],
            [_PENDING],
            (None, None, "no_submissions", False),
            id="never-started-members-are-never-chosen",
        ),
    ],
)
def test_the_chain_takes_the_first_mode_that_gives_an_answer(
    chain: Any,
    members: list[MemberResult],
    expected: tuple[str | None, str | None, str | None, bool],
) -> None:
    chosen = answer(members, chain, None, provisional=False)
    assert (
        chosen.mode,
        chosen.member,
        chosen.reason,
        chosen.tie,
    ) == expected
    assert chosen.chain == chain


def _verifier() -> Any:
    async def check(answer: str, metadata: dict[str, Any]) -> Verdict:
        return Verdict(passed=answer == "42")

    return check


def _key(text: str) -> str | None:
    return text.strip() or None


@pytest.mark.parametrize(
    "final,result,expected",
    [
        pytest.param(None, None, ["first"], id="no-contract"),
        pytest.param(None, answer_result(), ["first"], id="nothing-to-check"),
        pytest.param(
            None,
            answer_result(answer_key=_key, verifier=_verifier()),
            ["verify", "vote", "first"],
            id="verifier-and-key",
        ),
        pytest.param(
            None,
            answer_result(verifier=_verifier()),
            ["verify", "first"],
            id="verifier",
        ),
        pytest.param(None, answer_result(answer_key=_key), ["vote", "first"], id="key"),
        pytest.param("verify", None, ["verify"], id="one-mode"),
        pytest.param(("vote", "last"), None, ["vote", "last"], id="sequence"),
    ],
)
def test_final_chain_builds_the_default_from_the_result(
    final: FinalSpec | None, result: ResultSpec | None, expected: list[str]
) -> None:
    assert final_chain(final, result) == expected


@pytest.mark.parametrize(
    "final",
    [
        pytest.param("best", id="unknown-name"),
        pytest.param(["first", "best"], id="unknown-name-in-chain"),
        pytest.param([], id="empty-chain"),
        pytest.param({"mode": "unknown"}, id="unknown-mode"),
        pytest.param(3, id="not-a-chain"),
    ],
)
def test_a_final_that_is_not_a_chain_is_refused(final: Any) -> None:
    with pytest.raises(ValueError, match="final="):
        Controller(_returns, final=final)


async def _returns(control: SwarmControl) -> StopReason:
    return "all_done"


@swarm_controller(name="final_test_any_chain")
def any_chain(final: FinalSpec | None = None) -> Controller:
    return Controller(_returns, final=final)


@agent
def holds(controller: Controller) -> Agent:
    # stands in for a solver that logs a controller among its parameters
    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


def test_a_mixed_chain_validates_back_from_its_log() -> None:
    model = get_model("mockllm/model", config=GenerateConfig(temperature=0.5))
    chain: FinalSpec = [
        "verify",
        Synthesize(model=model, prompt="x"),
        Reporter(member="lead"),
        "first",
    ]
    controller = any_chain(final=chain)
    step = plan_to_eval_plan(
        Plan([as_solver(holds(controller))]), GenerateConfig()
    ).steps[0]
    logged = json.loads(step.model_dump_json())["params"]["controller"]
    assert logged["params"]["final"] == [
        "verify",
        {
            "mode": "synthesize",
            "model": {
                "model": "mockllm/model",
                "config": {"temperature": 0.5},
                "base_url": None,
                "model_args": {},
            },
            "prompt": "x",
        },
        {"mode": "reporter", "member": "lead"},
        "first",
    ]

    # from the logged JSON, and from the dicts replay passes, with the Model rebuilt
    replayed = create_registry_object(
        "swarm_controller", registry_info(controller).name, logged["params"]
    )
    assert isinstance(replayed, Controller)
    for rebuilt in (check_final(logged["params"]["final"]), replayed.final):
        assert isinstance(rebuilt, list)
        verify, synthesize, reporter, first_mode = rebuilt
        assert (verify, reporter, first_mode) == (
            "verify",
            Reporter(member="lead"),
            "first",
        )
        assert isinstance(synthesize, Synthesize) and synthesize.prompt == "x"
        assert isinstance(synthesize.model, Model)
        assert str(synthesize.model) == "mockllm/model"
        assert synthesize.model.config.temperature == 0.5


@pytest.mark.parametrize(
    "build,error",
    [
        pytest.param(
            lambda: swarm(
                react(),
                controller=leaderless(stop_on_submission=False, final="verify"),
                result=answer_result(answer_key=_key),
            ),
            "verify, which needs a verifier",
            id="verify-without-a-verifier",
        ),
        pytest.param(
            lambda: swarm(
                react(),
                controller=leaderless(
                    stop_on_submission=False, final=["vote", "first"]
                ),
                result=answer_result(verifier=_verifier()),
            ),
            "vote, which needs an answer_key",
            id="vote-without-a-key",
        ),
        pytest.param(
            lambda: swarm(
                react(),
                controller=leaderless(stop_on_submission=False, final="vote"),
            ),
            "vote, which needs an answer_key",
            id="vote-without-a-contract",
        ),
        pytest.param(
            lambda: swarm(
                react(),
                controller=leaderless(stop_on_submission=False, final="synthesize"),
            ),
            "synthesize, which the swarm does not run yet",
            id="synthesize",
        ),
        pytest.param(
            lambda: swarm(react(), controller=any_chain(final=Reporter(member="x"))),
            "reporter, which the swarm does not run yet",
            id="reporter-in-a-controller-that-accepts-it",
        ),
        pytest.param(
            lambda: leaderless(stop_on_submission=False, final=Reporter(member="x")),
            "a leaderless swarm has no reporter",
            id="reporter-in-leaderless",
        ),
        pytest.param(
            lambda: leaderless(final=["verify", "first"]),
            "needs stop_on_submission=False",
            id="chain-stopping-at-first",
        ),
        pytest.param(
            lambda: leaderless(stop_on_verified=True),
            "stop_on_verified=True\\) needs stop_on_submission=False",
            id="stop-on-verified-stopping-at-first",
        ),
    ],
)
def test_a_chain_the_swarm_cannot_apply_is_refused(
    build: Callable[[], object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        build()


def _team(*members: tuple[str, str, list[str]]) -> list[Any]:
    # members that answer `text` without a model call, each after the named ones ended
    return [
        member(
            after(waits, generator(scripted_model(), calls=0, answer=text)),
            name=name,
        )
        for name, text, waits in members
    ]


@pytest.mark.anyio
async def test_the_default_chain_verifies_then_votes_then_takes_the_first() -> None:
    contract = answer_result(answer_key=_key, verifier=_verifier())
    team = leaderless(stop_on_submission=False)
    # b's answer passes, after a's that does not
    verified = await run_swarm(
        swarm(
            _team(("a", "7", []), ("b", "42", ["a"]), ("c", "8", ["b"])),
            controller=team,
            channels=[],
            result=contract,
        )
    )
    # nothing passes: the earliest of the most common keys
    voted = await run_swarm(
        swarm(
            _team(("a", "7", []), ("b", "8", ["a"]), ("c", "8", ["b"])),
            controller=team,
            channels=[],
            result=contract,
        )
    )
    # nothing passes, and no answer has a key
    firsts = await run_swarm(
        swarm(
            _team(("a", " ", []), ("b", "\n", ["a"])),
            controller=team,
            channels=[],
            result=contract,
        )
    )
    chain = ["verify", "vote", "first"]
    for outcome, mode, chosen in [
        (verified, "verify", "b"),
        (voted, "vote", "b"),
        (firsts, "first", "a"),
    ]:
        record = outcome.record.final
        assert (record.chain, record.mode, record.member) == (chain, mode, chosen)
        assert final(outcome.transcript).data["mode"] == mode
        assert outcome.state.messages[-1].metadata == {
            "inspect_swarm": {"final": {"mode": mode, "member": chosen}}
        }
    assert verified.state.output.completion == "42"
    assert voted.state.output.completion == "8"


@pytest.mark.anyio
async def test_a_strict_verify_leaves_no_answer_when_nothing_passes() -> None:
    stop = await run_swarm(
        swarm(
            _team(("a", "7", []), ("b", "8", ["a"])),
            controller=leaderless(stop_on_submission=False, final="verify"),
            channels=[],
            result=answer_result(verifier=_verifier()),
        )
    )
    assert statuses(stop) == {"a": "submitted", "b": "submitted"}
    assert (stop.record.final.mode, stop.record.final.member) == (None, None)
    assert stop.record.final.reason == "none_verified"
    assert stop.state.output.empty


@pytest.mark.anyio
async def test_the_provisional_answer_follows_the_chain_after_each_verdict() -> None:
    seen: list[tuple[str, str]] = []
    state = AgentState(messages=[ChatMessageUser(content="task")])

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for event in control.events():
            # the event waits for the verdict, which the answer already reflects
            seen.append((event.member, state.output.completion))
        return "all_done"

    agent_swarm = swarm(
        _team(("a", "7", []), ("b", "42", ["a"]), ("c", "8", ["b"])),
        controller=_controller(run, "final_test_provisional_chain"),
        channels=[],
        result=answer_result(verifier=_verifier()),
    )
    async with sample():
        await agent_swarm(state)
        record = _record()
    # first while nothing passes, then the passed submission
    assert seen == [("a", "7"), ("b", "42"), ("c", "42")]
    assert (record.final.chain, record.final.mode) == (["verify", "first"], "verify")


@pytest.mark.anyio
async def test_the_provisional_answer_follows_the_vote_after_each_key() -> None:
    seen: list[tuple[str, str]] = []
    state = AgentState(messages=[ChatMessageUser(content="task")])
    interrupt = anyio.CancelScope()

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for event in control.events():
            # the event follows the key, which the answer already reflects
            seen.append((event.member, state.output.completion))
            if event.member == "c":
                # interrupted once the majority has formed, before the final step
                interrupt.cancel()
                await anyio.sleep_forever()
        return "all_done"

    agent_swarm = swarm(
        _team(("a", "x", []), ("b", "y", ["a"]), ("c", "y", ["b"])),
        controller=_controller(run, "final_test_provisional_vote"),
        channels=[],
        result=answer_result(answer_key=_key),
    )
    async with sample():
        with interrupt:
            await agent_swarm(state)
        record = _record()
    assert interrupt.cancelled_caught
    # a's key while it is alone or tied with b's, then the majority's earliest
    assert seen == [("a", "x"), ("b", "x"), ("c", "y")]
    assert (record.final.mode, record.final.member, record.final.provisional) == (
        "vote",
        "b",
        True,
    )
    assert state.output.completion == "y"


@pytest.mark.anyio
async def test_the_vote_counts_keys_the_record_leaves_out() -> None:
    # two members answer with a key the record cannot hold
    stop = await run_swarm(
        swarm(
            _team(
                ("a", "attachment://x", []),
                ("b", "attachment://x", ["a"]),
                ("c", "y", ["b"]),
            ),
            controller=leaderless(stop_on_submission=False, final="vote"),
            channels=[],
            result=answer_result(answer_key=_key),
        )
    )
    record = stop.record
    # chosen by the vote the runtime took, with the keys as the members gave them
    assert (record.final.mode, record.final.member, record.final.tie) == (
        "vote",
        "a",
        False,
    )
    assert [(m.answer_key, m.left_out) for m in record.members] == [
        (None, ["output", "answer_key"]),
        (None, ["output", "answer_key"]),
        ("y", []),
    ]


def test_a_vote_keeps_no_image_data_in_a_log_without_images(tmp_path: Path) -> None:
    # two members answer with different image data URIs, which a log without images
    # removes from the record: the vote keeps no copy of them
    images = ["data:image/png;base64,Zmlyc3Q=", "data:image/png;base64,c2Vjb25k"]
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            _team(("a", images[0], []), ("b", images[1], ["a"]), ("c", "y", ["b"])),
            controller=leaderless(stop_on_submission=False, final="vote"),
            channels=[],
            result=answer_result(answer_key=_key),
        ),
    )
    [log] = eval(
        task,
        model="mockllm/model",
        log_dir=str(tmp_path),
        log_images=False,
        display="none",
    )
    assert log.status == "success", log.error
    [sample] = read_eval_log(log.location).samples or []
    [event] = [
        e for e in sample.events if isinstance(e, SwarmEvent) and e.kind == RECORD_TYPE
    ]
    logged = json.dumps(event.data)
    assert not any(image.split(",")[1] in logged for image in images)
    final = event.data["final"]
    assert isinstance(final, dict)
    assert (final["mode"], final["member"], final["tie"]) == ("vote", "a", True)


class _Environment:
    """A verifier of an environment that members change: it passes while `state` is `good`, and records the answers it is called with."""

    def __init__(self, explanation: str = "checked") -> None:
        self.state = "good"
        self.calls: list[str] = []
        self.explanation = explanation

    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict:
        self.calls.append(answer)
        return Verdict(passed=self.state == "good", explanation=self.explanation)


@agent
def breaks(environment: _Environment) -> Agent:
    # changes the environment and ends without an answer, as react() does after
    # refusals
    async def execute(state: AgentState) -> AgentState:
        environment.state = "broken"
        state.output = ModelOutput.from_content(
            "mockllm", "no", stop_reason="content_filter"
        )
        return state

    return execute


@pytest.mark.anyio
async def test_the_final_verdict_checks_the_environment_after_the_stop() -> None:
    environment = _Environment()
    stop = await run_swarm(
        swarm(
            [
                *_team(("a", "done", [])),
                member(after(["a"], breaks(environment)), name="b"),
            ],
            controller=leaderless(stop_on_submission=False),
            channels=[],
            result=artifact_result(verifier=environment),
        )
    )
    record = stop.record
    assert statuses(stop) == {"a": "submitted", "b": "unsubmitted"}
    # a's submission passed when it was made; the environment then changed
    assert record.members[0].verdict == Verdict(True, None, "checked")
    assert record.final_verdict == Verdict(False, None, "checked")
    assert (record.final.mode, record.final.member) == ("verify", "a")
    assert environment.calls == ["done", "done"]
    # no verdict event: it judges no member's submission
    assert len(swarm_events(stop.transcript, "verdict")) == 1


@pytest.mark.anyio
async def test_the_final_verdict_without_an_answer_and_with_a_lost_explanation() -> (
    None
):
    environment = _Environment(explanation="attachment://elsewhere")
    stop = await run_swarm(
        swarm(
            [member(breaks(environment), name="b")],
            controller=leaderless(stop_on_submission=False),
            channels=[],
            result=artifact_result(verifier=environment),
        )
    )
    assert environment.calls == [""]
    assert stop.record.final_verdict == Verdict(False)
    assert stop.record.left_out == ["explanation"]


class _Holds:
    """A verifier that passes at once until its call `hold_from` (from 1), and from it waits until cancelled; it then lets the cancellation propagate, or catches it and returns a passing verdict all the same."""

    def __init__(self, hold_from: int, catches: bool) -> None:
        self.hold_from = hold_from
        self.catches = catches
        self.calls: list[str] = []

    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict:
        self.calls.append(answer)
        if len(self.calls) >= self.hold_from:
            try:
                await anyio.sleep_forever()
            except anyio.get_cancelled_exc_class():
                if not self.catches:
                    raise
        return Verdict(passed=True)


# A swarm whose final step calls `verifier`: for `artifact`, its call on the final
# environment, after the run verified the submission; for `answer`, its call on the
# submission the run left unverified, whose check in the run holds until the swarm's
# time cap ends it.
def _final_step_swarm(kind: str, verifier: Any) -> Agent:
    if kind == "artifact":
        result = artifact_result(verifier=verifier)
        budget = Budget()
    else:
        result = answer_result(verifier=verifier)
        budget = Budget(time=0.2, grace=0.05)
    return swarm(
        _team(("a", "done", [])),
        controller=leaderless(stop_on_submission=False),
        channels=[],
        budget=budget,
        result=result,
    )


# The swarm's record and ledger after its final step was ended, by a cancellation or a
# failure (`cancelled`): the answer stays provisional, and the final step's call left no
# verdict.
def _assert_final_step_ended(kind: str, cancelled: bool) -> None:
    record = _record()
    ledger = SwarmLedger.model_validate(store().get(LEDGER_KEY))
    assert (record.final.member, record.final.provisional) == ("a", True)
    if kind == "artifact":
        assert record.final_verdict is None
        assert record.members[0].verdict == Verdict(passed=True)
    else:
        assert record.members[0].verdict is None
    assert (ledger.final_cancelled, ledger.interrupted) == (cancelled, True)


@pytest.mark.anyio
@pytest.mark.parametrize("catches", [False, True], ids=["propagates", "catches"])
@pytest.mark.parametrize("kind", ["artifact", "answer"])
async def test_a_cancellation_during_the_final_step_ends_it(
    kind: str, catches: bool
) -> None:
    verifier = _Holds(hold_from=2 if kind == "artifact" else 1, catches=catches)
    async with sample():
        with anyio.move_on_after(1.0) as scope:
            await run_agent(_final_step_swarm(kind, verifier), "task")
        # the cancellation propagates, and the answer stays provisional, even when the
        # verifier caught it and returned a verdict
        assert scope.cancelled_caught
        assert verifier.calls == ["done", "done"]
        _assert_final_step_ended(kind, cancelled=True)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("timeout", ["timeout", "wait_for"])
@pytest.mark.parametrize("kind", ["artifact", "answer"])
async def test_an_asyncio_timeout_during_the_final_step_ends_it(
    kind: str, timeout: str
) -> None:
    # asyncio's own Task.cancel(), which no AnyIO scope knows of, caught by the verifier
    verifier = _Holds(hold_from=2 if kind == "artifact" else 1, catches=True)
    async with sample():
        running = run_agent(_final_step_swarm(kind, verifier), "task")
        with pytest.raises(TimeoutError):
            if timeout == "timeout":
                async with asyncio.timeout(1.0):
                    await running
            else:
                await asyncio.wait_for(running, 1.0)
        assert verifier.calls == ["done", "done"]
        _assert_final_step_ended(kind, cancelled=True)


class _FailsInTheFinalStep:
    """A verifier whose first call passes, or for `holds` waits until cancelled, and whose second, in the final step, raises or returns something other than a `Verdict`."""

    def __init__(self, holds: bool, fails: str) -> None:
        self.holds = holds
        self.fails = fails
        self.calls: list[str] = []

    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict:
        self.calls.append(answer)
        if len(self.calls) == 1:
            if self.holds:
                await anyio.sleep_forever()
            return Verdict(passed=True)
        if self.fails == "raises":
            raise RuntimeError("the verifier failed")
        return cast(Verdict, True)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fails,error,match",
    [
        ("raises", RuntimeError, "the verifier failed"),
        ("invalid", TypeError, "must return a Verdict, got a bool"),
    ],
)
@pytest.mark.parametrize("kind", ["artifact", "answer"])
async def test_a_verifier_failing_in_the_final_step_fails_the_sample(
    kind: str, fails: str, error: type[Exception], match: str
) -> None:
    verifier = _FailsInTheFinalStep(holds=kind == "answer", fails=fails)
    async with sample():
        with pytest.raises(error, match=match):
            await run_agent(_final_step_swarm(kind, verifier), "task")
        assert verifier.calls == ["done", "done"]
        _assert_final_step_ended(kind, cancelled=False)


@pytest.mark.anyio
async def test_leaderless_stops_at_the_first_verified_submission() -> None:
    stop = await run_swarm(
        swarm(
            [
                *_team(("a", "7", []), ("b", "42", ["a"])),
                member(sleeper(), name="slow"),
            ],
            controller=leaderless(stop_on_submission=False, stop_on_verified=True),
            channels=[],
            budget=Budget(grace=0.05),
            result=answer_result(verifier=_verifier()),
        )
    )
    assert stop.reason == "verified"
    assert statuses(stop) == {"a": "submitted", "b": "submitted", "slow": "cancelled"}
    assert (stop.record.final.mode, stop.record.final.member) == ("verify", "b")


@pytest.mark.anyio
async def test_leaderless_stopping_at_the_first_submission_applies_the_default_chain() -> (
    None
):
    stop = await run_swarm(
        swarm(
            [*_team(("a", "7", [])), member(sleeper(), name="slow")],
            channels=[],
            budget=Budget(grace=0.05),
            result=answer_result(answer_key=_key, verifier=_verifier()),
        )
    )
    assert stop.reason == "submitted"
    assert statuses(stop) == {"a": "submitted", "slow": "cancelled"}
    final_record = stop.record.final
    assert (final_record.chain, final_record.mode, final_record.member) == (
        ["verify", "vote", "first"],
        "vote",
        "a",
    )
