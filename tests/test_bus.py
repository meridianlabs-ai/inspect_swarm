import logging
from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from inspect_ai._util.exception import TerminateSampleError
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.event import InfoEvent
from inspect_ai.log._transcript import Transcript
from inspect_ai.tool import Tool, ToolError
from pydantic import JsonValue

from inspect_swarm import (
    Channel,
    ChannelRun,
    ChannelState,
    CommDecision,
    Controller,
    MemberInfo,
    MemberState,
    Record,
    StopReason,
    SwarmControl,
    SwarmView,
    member,
    swarm,
    swarm_channel,
    swarm_controller,
)
from inspect_swarm._budget import MemberContext, current_member
from inspect_swarm._bus._bus import Bus, Delivery, current_bus
from inspect_swarm._bus._record import Draft
from inspect_swarm._bus._storm import StormControls

from .swarm_runtime import run_swarm, sample


class _BoardState:
    # a record-producing channel's state: refuses an "invalid" payload at step 0 and
    # records past `capacity` at admission, and notifies each record's recipients
    def __init__(self, capacity: int | None) -> None:
        self.capacity = capacity
        self.applied: list[Record] = []

    def tools(self) -> list[Tool]:
        return []

    def validate(self, record: Record) -> None:
        if record.payload == "invalid":
            raise ToolError("invalid payload")

    def admit(self, record: Record) -> None:
        if self.capacity is not None and len(self.applied) >= self.capacity:
            raise ToolError("the board is full")

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        self.applied.append(record)
        return list(record.recipients), {"size": len(self.applied)}


class _Board(Channel):
    name = "board"
    prefix = "b"

    def __init__(
        self, storm: StormControls | None = None, capacity: int | None = None
    ) -> None:
        self.storm = storm or StormControls()
        self.capacity = capacity
        self.opened: list[_BoardState] = []

    def open(self, run: ChannelRun) -> ChannelState | None:
        state = _BoardState(self.capacity)
        self.opened.append(state)
        return state


class _Notes(_Board):
    name = "notes"
    prefix = "n"


class _SameName(_Board):
    prefix = "x"


class _SamePrefix(_Board):
    name = "other"


class _NameOnly(_Board):
    prefix = None


class _Strict(_Board):
    def check(self, roster: Sequence[MemberInfo]) -> None:
        raise ValueError(f"the board refuses {roster[0].name}")


class _Locks(Channel):
    def instructions(self, member: MemberInfo) -> str | None:
        return "Take a lock before editing a file."


@swarm_channel
def bus_test_board() -> Channel:
    return _Board()


@swarm_channel
def bus_test_same_name() -> Channel:
    return _SameName()


@swarm_channel
def bus_test_same_prefix() -> Channel:
    return _SamePrefix()


@swarm_channel
def bus_test_name_only() -> Channel:
    return _NameOnly()


@swarm_channel
def bus_test_strict() -> Channel:
    return _Strict()


@swarm_channel
def bus_test_locks() -> Channel:
    return _Locks()


# Bus tests: a bus over stand-in members, with a record-producing test channel


@dataclass
class _Member:
    name: str
    state: MemberState = "running"
    status: str | None = None
    role: str | None = None

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"


MEMBERS = ("w-1", "w-2", "w-3")


def _bus(
    policy: Any = None,
    *,
    channels: Sequence[Channel] | None = None,
    states: Mapping[str, MemberState] | None = None,
) -> tuple[Bus, dict[str, _Member]]:
    members = {
        name: _Member(name, (states or {}).get(name, "running")) for name in MEMBERS
    }
    return Bus(list(members.values()), channels or [_Board()], policy), members


@contextmanager
def _as(name: str) -> Generator[None]:
    token = current_member.set(MemberContext(name, bridged=False))
    try:
        yield
    finally:
        current_member.reset(token)


async def _send(
    bus: Bus,
    sender: str = "w-1",
    *,
    channel: str = "board",
    to: tuple[str, ...] = (),
    payload: str = "",
    op: Mapping[str, JsonValue] | None = None,
) -> Delivery:
    with _as(sender):
        return await bus.deliver(Draft(channel, "note", to, payload, op))


def _comm(transcript: Transcript) -> list[dict[str, Any]]:
    return [
        e.data
        for e in transcript.events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("type") == "comm"
    ]


def _board(bus: Bus) -> _BoardState:
    state = bus.states["board"]
    assert isinstance(state, _BoardState)
    return state


@pytest.mark.anyio
@pytest.mark.parametrize(
    "to,recipients",
    [
        pytest.param((), [], id="unaddressed"),
        pytest.param(("w-2", "w-3", "w-2"), ["w-2", "w-3"], id="addressed"),
    ],
)
async def test_a_record_is_applied_once_with_its_evidence(
    to: tuple[str, ...], recipients: list[str]
) -> None:
    bus, _ = _bus()
    async with sample() as transcript:
        delivery = await _send(bus, to=to, payload="hello", op={"n": 1})
    record = delivery.record
    assert (record.id, record.sender, record.recipients) == (
        "b-1",
        "w-1",
        tuple(recipients),
    )
    assert (delivery.seq, delivery.notify) == (1, recipients)
    assert _board(bus).applied == [record]
    assert bus.records == (record,)
    sent, delivered = _comm(transcript)
    assert sent["kind"] == "sent"
    assert sent["member"] == "w-1"
    assert sent["record"]["to"] == list(to)
    assert sent["record"]["recipients"] == recipients
    assert sent["record"]["op"] == {"n": 1}
    assert (sent["decision"], sent["rejected"]) == (None, None)
    assert "original_payload" not in sent
    assert delivered == {
        "version": 1,
        "type": "comm",
        "kind": "delivered",
        "channel": "board",
        "member": "w-1",
        "span_id": None,
        "record": "b-1",
        "recipients": recipients,
        "seq": 1,
        "notify": recipients,
        "result": {"size": 1},
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "draft,states,stopping,error",
    [
        pytest.param(
            {"to": ("w-9",)},
            {},
            False,
            "'w-9' is not a member of the swarm; the members are w-1, w-2, w-3.",
            id="unknown",
        ),
        pytest.param({"to": ("w-1",)}, {}, False, "is you", id="self"),
        pytest.param(
            {"to": ("w-2",)}, {"w-2": "pending"}, False, "has not started", id="pending"
        ),
        pytest.param(
            {"to": ("w-2",)}, {"w-2": "ended"}, False, "has ended", id="ended"
        ),
        pytest.param({}, {}, True, "the swarm is stopping", id="stopping"),
        pytest.param(
            {"payload": "invalid"}, {}, False, "invalid payload", id="validate"
        ),
    ],
)
async def test_a_refusal_before_the_policy_leaves_no_record(
    draft: dict[str, Any],
    states: dict[str, MemberState],
    stopping: bool,
    error: str,
) -> None:
    seen: list[Record] = []

    async def policy(record: Record, view: SwarmView) -> None:
        seen.append(record)

    bus, _ = _bus(policy, states=states)
    if stopping:
        bus.close()
    async with sample() as transcript:
        with pytest.raises(ToolError, match=error):
            await _send(bus, **draft)
    assert (seen, _comm(transcript), _board(bus).applied) == ([], [], [])
    if not stopping:
        # no id was given
        delivery = await _send(bus)
        assert delivery.record.id == "b-1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "decision,applied,error",
    [
        pytest.param(None, "hello", None, id="none"),
        pytest.param(CommDecision("continue"), "hello", None, id="continue"),
        pytest.param(
            CommDecision("modify", payload="safe", explanation="redacted"),
            "safe",
            None,
            id="modify",
        ),
        pytest.param(CommDecision("escalate"), "hello", None, id="escalate"),
        pytest.param(
            CommDecision("reject", message="no thanks"),
            None,
            (ToolError, "no thanks"),
            id="reject",
        ),
        pytest.param(
            CommDecision("reject"),
            None,
            (ToolError, "the swarm's policy rejected this note"),
            id="reject-default",
        ),
        pytest.param(
            CommDecision("terminate", message="off topic"),
            None,
            (TerminateSampleError, "off topic"),
            id="terminate",
        ),
    ],
)
async def test_the_policy_decides_with_the_resolved_record(
    decision: CommDecision | None,
    applied: str | None,
    error: tuple[type[Exception], str] | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[tuple[Record, MemberState, int]] = []

    async def policy(record: Record, view: SwarmView) -> CommDecision | None:
        seen.append((record, view.member("w-2").state, len(view.records)))
        return decision

    bus, _ = _bus(policy)
    async with sample() as transcript:
        if error is None:
            await _send(bus, to=("w-2",), payload="hello")
        else:
            with pytest.raises(error[0], match=error[1]):
                await _send(bus, to=("w-2",), payload="hello")
    [(record, state, records)] = seen
    assert (record.id, record.recipients, record.payload) == ("b-1", ("w-2",), "hello")
    assert (state, records) == ("running", 0)
    assert [r.payload for r in _board(bus).applied] == (
        [] if applied is None else [applied]
    )
    events = _comm(transcript)
    sent = events[0]
    assert sent["record"]["payload"] == (applied or "hello")
    assert sent["decision"] == (
        None
        if decision is None
        else {"action": decision.action, "explanation": decision.explanation}
    )
    if error is None:
        assert sent["rejected"] is None
        assert [e["kind"] for e in events] == ["sent", "delivered"]
    else:
        assert sent["rejected"] == {"control": "policy", "reason": error[1]}
        assert [e["kind"] for e in events] == ["sent"]
    if applied == "safe":
        assert sent["original_payload"] == "hello"
    escalated = [r for r in caplog.records if "escalated b-1" in r.getMessage()]
    assert len(escalated) == (
        1 if decision is not None and decision.action == "escalate" else 0
    )
    assert all(r.levelno == logging.WARNING for r in escalated)


def test_a_modify_decision_needs_a_payload() -> None:
    with pytest.raises(ValueError, match="a modify decision needs a payload"):
        CommDecision("modify")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload,policy_payload,error",
    [
        pytest.param("a" * 8000, None, None, id="ascii-at-limit"),
        pytest.param("😀" * 2000, None, None, id="four-byte-at-limit"),
        pytest.param(
            "😀" * 8000, None, "note is 32000 bytes; the limit is 8000", id="emoji-over"
        ),
        pytest.param(
            "short",
            "a" * 8001,
            "note is 8001 bytes; the limit is 8000",
            id="modified-over",
        ),
    ],
)
async def test_max_bytes_counts_utf8_bytes_of_the_payload_as_delivered(
    payload: str, policy_payload: str | None, error: str | None
) -> None:
    async def policy(record: Record, view: SwarmView) -> CommDecision | None:
        if policy_payload is None:
            return None
        return CommDecision("modify", payload=policy_payload)

    bus, _ = _bus(policy, channels=[_Board(StormControls(max_bytes=8000))])
    async with sample() as transcript:
        if error is None:
            await _send(bus, payload=payload)
        else:
            with pytest.raises(ToolError, match=error):
                await _send(bus, payload=payload)
    [sent, *_] = _comm(transcript)
    if error is None:
        assert len(_board(bus).applied) == 1
    else:
        assert _board(bus).applied == []
        assert sent["rejected"] == {"control": "max_bytes", "reason": error}


@pytest.mark.anyio
async def test_the_rate_counts_each_senders_commits_in_a_sliding_window() -> None:
    bus, _ = _bus(channels=[_Board(StormControls(rate=(2, 0.5)), capacity=3)])
    async with sample() as transcript:
        await _send(bus)
        await _send(bus)
        with pytest.raises(ToolError, match=r"rate limit: retry in 1 s"):
            await _send(bus)
        # another sender has its own window
        await _send(bus, "w-2")
        # a send refused at admission does not count against w-2's two
        with pytest.raises(ToolError, match="the board is full"):
            await _send(bus, "w-2")
        _board(bus).capacity = None
        await _send(bus, "w-2")
        # the window slides
        await anyio.sleep(0.6)
        await _send(bus)
    rejected = [e["rejected"] for e in _comm(transcript) if e["kind"] == "sent"]
    assert [r["control"] if r else None for r in rejected] == [
        None,
        None,
        "rate",
        None,
        "admit",
        None,
        None,
    ]


@pytest.mark.parametrize("rate", [(0, 60.0), (5, 0.0)])
def test_storm_controls_refuse_a_rate_that_admits_nothing(
    rate: tuple[int, float],
) -> None:
    with pytest.raises(ValueError, match="rate must be a positive count"):
        StormControls(rate=rate)


@asynccontextmanager
async def _held(
    bus: Bus, entered: anyio.Event, **draft: Any
) -> AsyncGenerator[list[Any]]:
    # a send whose policy is running while the body runs
    outcome: list[Any] = []

    async def send() -> None:
        try:
            outcome.append(await _send(bus, **draft))
        except ToolError as ex:
            outcome.append(ex)

    async with anyio.create_task_group() as tg:
        tg.start_soon(send)
        await entered.wait()
        yield outcome


@pytest.mark.anyio
@pytest.mark.parametrize(
    "during,reason",
    [
        ("recipient-ends", "w-2 ended before the note was delivered."),
        ("sender-ends", "w-1 ended before the note was delivered."),
        ("stop-begins", "the swarm is stopping; nothing more is delivered."),
    ],
)
async def test_liveness_is_rechecked_after_the_policy_for_every_party(
    during: str, reason: str
) -> None:
    entered, release = anyio.Event(), anyio.Event()

    async def policy(record: Record, view: SwarmView) -> None:
        entered.set()
        await release.wait()

    bus, members = _bus(policy)
    async with sample() as transcript:
        async with _held(bus, entered, to=("w-2", "w-3")) as outcome:
            if during == "stop-begins":
                bus.close()
            else:
                members["w-2" if during == "recipient-ends" else "w-1"].state = "ended"
            release.set()
    [error] = outcome
    assert isinstance(error, ToolError) and error.message == reason
    # all or nothing: w-3, still running, gets nothing either
    assert _board(bus).applied == []
    [sent] = _comm(transcript)
    assert sent["rejected"] == {"control": "liveness", "reason": reason}


@pytest.mark.anyio
async def test_concurrent_records_cannot_both_take_the_last_slot() -> None:
    async def policy(record: Record, view: SwarmView) -> None:
        await anyio.sleep(0)

    bus, _ = _bus(policy, channels=[_Board(capacity=1)])
    outcomes: list[Any] = []

    async def send(sender: str) -> None:
        try:
            outcomes.append(await _send(bus, sender))
        except ToolError as ex:
            outcomes.append(ex)

    async with sample() as transcript:
        async with anyio.create_task_group() as tg:
            tg.start_soon(send, "w-1")
            tg.start_soon(send, "w-2")
    assert sorted(type(o).__name__ for o in outcomes) == ["Delivery", "ToolError"]
    # the refused record left the state as it was
    assert len(_board(bus).applied) == 1
    sent = [e for e in _comm(transcript) if e["kind"] == "sent"]
    assert sorted(str(e["rejected"]) for e in sent) == sorted(
        ["None", str({"control": "admit", "reason": "the board is full"})]
    )


@pytest.mark.anyio
async def test_seq_is_the_commit_order_when_policies_finish_out_of_id_order() -> None:
    first_held, second_done = anyio.Event(), anyio.Event()

    async def policy(record: Record, view: SwarmView) -> None:
        if record.id == "b-1":
            first_held.set()
            await second_done.wait()

    bus, _ = _bus(policy)
    async with sample() as transcript:
        async with _held(bus, first_held, payload="first") as outcome:
            await _send(bus, "w-2", payload="second")
            second_done.set()
    [first] = outcome
    assert (first.record.id, first.seq) == ("b-1", 2)
    assert [r.id for r in bus.records] == ["b-2", "b-1"]
    assert [r.payload for r in _board(bus).applied] == ["second", "first"]
    delivered = [e for e in _comm(transcript) if e["kind"] == "delivered"]
    assert [(e["record"], e["seq"]) for e in delivered] == [("b-2", 1), ("b-1", 2)]


@pytest.mark.anyio
async def test_record_ids_count_per_channel_from_one() -> None:
    bus, _ = _bus(channels=[_Board(), _Notes()])
    ids = [
        (await _send(bus, channel=channel)).record.id
        for channel in ("board", "notes", "board")
    ]
    assert ids == ["b-1", "n-1", "b-2"]
    assert [r.id for r in bus.records] == ids


# Swarm tests: the runtime opens the channels and closes the bus


@agent
def bus_user(
    buses: list[Bus],
    send: bool = False,
    outcomes: list[str] | None = None,
) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        bus = current_bus.get()
        assert bus is not None
        buses.append(bus)
        if send:
            for _ in range(2):
                try:
                    await bus.deliver(Draft(channel="board", kind="note"))
                    message = "delivered"
                except ToolError as ex:
                    message = ex.message
                assert outcomes is not None
                outcomes.append(message)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channels,opened",
    [
        pytest.param([], [], id="none"),
        pytest.param(["filesystem"], [], id="filesystem"),
        pytest.param(["filesystem", "bus_test_board"], ["board"], id="board"),
    ],
)
async def test_each_run_opens_its_own_channel_states(
    channels: list[str], opened: list[str]
) -> None:
    buses: list[Bus] = []
    agent = swarm(
        member(bus_user, args={"buses": buses}, name="w", count=2), channels=channels
    )
    await run_swarm(agent)
    await run_swarm(agent)
    first, _, second, _ = buses
    # one bus for every member of a run, and a new one for each run
    assert buses == [first, first, second, second] and first is not second
    for bus in (first, second):
        assert list(bus.states) == opened
        # instructions-only channels give no swarm tools
        assert [t for s in bus.states.values() for t in s.tools()] == []
    if opened:
        assert first.states["board"] is not second.states["board"]


@pytest.mark.anyio
async def test_a_stop_refuses_records_whose_policy_was_running_and_later_ones() -> None:
    entered = anyio.Event()

    async def policy(record: Record, view: SwarmView) -> None:
        entered.set()
        bus = current_bus.get()
        assert bus is not None
        while not bus.stopping:
            await anyio.sleep(0.01)

    @swarm_controller(name="bus_test_stop_while_held")
    def stop_while_held() -> Controller:
        async def run(control: SwarmControl) -> StopReason:
            control.start("w")
            await entered.wait()
            return "all_done"

        return Controller(run)

    buses: list[Bus] = []
    outcomes: list[str] = []
    stop = await run_swarm(
        swarm(
            member(
                bus_user,
                args={"buses": buses, "send": True, "outcomes": outcomes},
                name="w",
            ),
            controller=stop_while_held(),
            channels=["bus_test_board"],
            policy=policy,
        )
    )
    assert stop["reason"] == "all_done"
    assert outcomes == ["the swarm is stopping; nothing more is delivered."] * 2
    [bus] = buses
    assert bus.records == ()


@agent(name="inspect_swe/bus_test_cli")
def _cli() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


@agent
def bus_test_native() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


@pytest.mark.parametrize(
    "bridged",
    [
        pytest.param(lambda: member(_cli, name="cli"), id="inspect_swe"),
        pytest.param(
            lambda: member(bus_test_native(), name="cli", bridged=True), id="declared"
        ),
    ],
)
def test_a_bridged_member_is_refused_beside_a_channel_with_records(
    bridged: Callable[[], Any],
) -> None:
    with pytest.raises(
        ValueError,
        match=r"member 'cli' is bridged, and bridged members cannot use bus channels such as bus_test_board yet",
    ):
        swarm([bridged(), bus_test_native()], channels=["filesystem", bus_test_board()])
    for channels in (["filesystem"], []):
        swarm([bridged(), bus_test_native()], channels=channels)


@pytest.mark.parametrize(
    "channels,error",
    [
        pytest.param(
            ["bus_test_board", "bus_test_same_name"],
            "bus_test_board and bus_test_same_name both have the record name 'board'",
            id="name",
        ),
        pytest.param(
            ["bus_test_board", "bus_test_same_prefix"],
            "bus_test_board and bus_test_same_prefix both have the record prefix 'b'",
            id="prefix",
        ),
        pytest.param(
            ["bus_test_name_only"],
            "bus_test_name_only must set both a record name and a record prefix",
            id="name-without-prefix",
        ),
        pytest.param(["bus_test_strict"], "the board refuses w-1", id="check"),
    ],
)
def test_swarm_refuses_channels_that_collide_or_refuse_the_roster(
    channels: list[str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        swarm(member(bus_test_native, name="w", count=2), channels=channels)


@agent
def preamble_reader(preambles: list[str]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        preambles.append(state.messages[-1].text)
        return state

    return execute


@pytest.mark.anyio
async def test_two_instructions_only_channels_are_accepted_and_both_instruct() -> None:
    preambles: list[str] = []
    await run_swarm(
        swarm(
            member(preamble_reader, args={"preambles": preambles}, name="w"),
            channels=["filesystem", bus_test_locks()],
        )
    )
    [preamble] = preambles
    assert "Share files with them in /shared." in preamble
    assert "Take a lock before editing a file." in preamble
