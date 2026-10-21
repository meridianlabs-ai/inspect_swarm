from pathlib import Path
from typing import Any

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent, SpanBeginEvent, SpanEndEvent
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageUser,
    ModelOutput,
)
from inspect_ai.util import (
    message_limit,
    span_id_provider,
    store,
    time_limit,
    token_limit,
)

from inspect_swarm import (
    Budget,
    Controller,
    StopReason,
    SwarmControl,
    leaderless,
    member,
    swarm,
    swarm_controller,
)
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._final import first
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._record import RECORD_KEY, MemberResult, SwarmResultRecord

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Outcome,
    Script,
    evidence,
    final,
    generator,
    output,
    run_swarm,
    sample,
    scripted_model,
    sleeper,
    statuses,
)


def _controller(run: Any, name: str) -> Controller:
    @swarm_controller(name=name)
    def factory() -> Controller:
        return Controller(run)

    return factory()


def final_event(outcome: Outcome) -> InfoEvent:
    [event] = [
        e
        for e in outcome.transcript.events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("kind") == "final"
    ]
    return event


def _record() -> SwarmResultRecord:
    return SwarmResultRecord.model_validate(store().get(RECORD_KEY))


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
    record = SwarmResultRecord.model_validate(sample.store[RECORD_KEY])
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
    seen: list[tuple[str, SwarmResultRecord]] = []
    state = AgentState(messages=[ChatMessageUser(content="task")])
    gate = anyio.Event()

    async def run(control: SwarmControl) -> StopReason:
        control.start("quick")
        control.start("slow")
        async for event in control.events():
            if event.member == "quick":
                seen.append((state.output.completion, _record()))
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
    [(completion, record)] = seen
    assert completion == "quick"
    assert (record.final.member, record.final.provisional) == ("quick", True)
    assert [m.state for m in record.members] == ["ended", "running"]


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
    record = SwarmResultRecord.model_validate(sample.store[RECORD_KEY])
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
    record = SwarmResultRecord.model_validate(sample.store[RECORD_KEY])
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
        e
        for e in sample.events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("kind") == "final"
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
    }
    assert stop.state.output.completion == expected
    assert final(stop.transcript)["member"] == expected


# ---------------------------------------------------------------------------
# The record


@pytest.mark.anyio
async def test_the_record_is_written_at_start_and_on_each_start_and_end() -> None:
    seen: list[SwarmResultRecord] = []
    stored: list[dict[str, Any]] = []

    async def run(control: SwarmControl) -> StopReason:
        seen.append(_record())
        control.start("started")
        seen.append(_record())
        async for _ in control.events():
            seen.append(_record())
            stored.append(store().get(RECORD_KEY))
        return "all_done"

    model = scripted_model()
    stop = await run_swarm(
        swarm(
            [
                member(generator(model, calls=1), name="started", role="lead"),
                member(generator(model, calls=1), name="never"),
            ],
            controller=_controller(run, "final_test_record_updates"),
            channels=[],
        )
    )
    at_start, after_start, after_end = seen
    assert [m.state for m in at_start.members] == ["pending", "pending"]
    assert at_start.final.model_dump() == {
        "mode": None,
        "member": None,
        "reason": "no_submissions",
        "provisional": True,
    }
    assert [m.state for m in after_start.members] == ["running", "pending"]
    started = after_end.members[0]
    assert (started.state, started.status, started.submitted) == (
        "ended",
        "submitted",
        True,
    )
    assert started.role == "lead"
    # no member's output: those stay in the members' states
    [after_end_stored] = stored
    assert set(after_end_stored["members"][0]) == {
        "name",
        "role",
        "state",
        "status",
        "submitted",
        "time",
        "sandbox",
        "gate_checks",
        "gate_passes",
        "first_pass_time",
    }
    assert started.time is not None and started.time >= 0
    assert after_end.final.member == "started" and after_end.final.provisional
    # a member the controller never started stays pending
    never = stop.record.members[1]
    assert (never.state, never.status, never.time) == ("pending", None, None)
    assert not stop.record.final.provisional


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
    [result] = evidence(stop.transcript, "member_result")
    assert (result["status"], result["span_id"]) == ("cancelled", None)


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
        str(e.data["member"]): (i, e)
        for i, e in enumerate(events)
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("kind") == "member_result"
    }
    common = {"version": 1, "type": "result"}
    assert statuses(stop) == {
        "a": "submitted",
        "b": "stopped",
        "limited": "member_limit:token",
    }
    for name, (i, e) in results.items():
        r = e.data
        assert isinstance(r, dict)
        assert r | common == r
        assert (r["kind"], r["status"]) == ("member_result", statuses(stop)[name])
        # written in the member's span, before it ends
        assert r["span_id"] == e.span_id == spans[name]
        assert i < span_ends[spans[name]]
    payload = final(stop.transcript)
    assert payload == {
        **common,
        "kind": "final",
        "member": "a",
        "span_id": spans["swarm"],
        "mode": "first",
        "reason": None,
        "stop_reason": "controller:enough",
    }
    assert final_event(stop).span_id == spans["swarm"]
    assert secret not in repr([*(e.data for _, e in results.values()), payload])


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
    assert state.output.completion == ""


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
