import json
from collections import UserDict
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import anyio
import pytest
from inspect_ai._util.json import to_json_safe
from inspect_ai.agent import Agent, AgentState, agent, run
from inspect_ai.event import InfoEvent, SpanBeginEvent
from inspect_ai.log import EvalSample
from inspect_ai.log._condense import condense_sample
from inspect_ai.log._recorders.eval import _parse_sample_data
from inspect_ai.log._transcript import Transcript
from inspect_ai.tool import Tool, ToolError
from pydantic import JsonValue

from inspect_swarm import (
    Channel,
    ChannelRun,
    ChannelState,
    Controller,
    MemberInfo,
    MemberState,
    NoticeLine,
    Record,
    StopReason,
    SwarmControl,
    WaitNews,
    WaitSpec,
    member,
    swarm,
    swarm_channel,
    swarm_controller,
)
from inspect_swarm._budget import MemberContext, current_member
from inspect_swarm._bus._bus import Bus, Delivery, current_bus
from inspect_swarm._bus._record import Draft, thaw_json
from inspect_swarm._bus._storm import StormControls

from .swarm_runtime import evidence, run_swarm, sample


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

    def notice_lines(self, member: str) -> list[NoticeLine]:
        return []


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
    context: MemberContext = field(init=False)

    def __post_init__(self) -> None:
        self.context = MemberContext(self.name, bridged=False)

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"


MEMBERS = ("w-1", "w-2", "w-3")


class _TestBus(Bus):
    # the stand-in members, for `_send`
    stand_ins: dict[str, _Member]


def _bus(
    *,
    channels: Sequence[Channel] | None = None,
    states: Mapping[str, MemberState] | None = None,
) -> tuple[_TestBus, dict[str, _Member]]:
    members = {
        name: _Member(name, (states or {}).get(name, "running")) for name in MEMBERS
    }
    bus = _TestBus(
        list(members.values()),
        {name: m.context for name, m in members.items()},
        channels or [_Board()],
    )
    bus.stand_ins = members
    return bus, members


@contextmanager
def _as(context: MemberContext | None) -> Generator[None]:
    token = current_member.set(context)
    try:
        yield
    finally:
        current_member.reset(token)


async def _send(
    bus: _TestBus,
    sender: str = "w-1",
    *,
    channel: str = "board",
    to: tuple[str, ...] = (),
    payload: str = "",
    op: dict[str, JsonValue] | None = None,
) -> Delivery:
    with _as(bus.stand_ins[sender].context):
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
    sent, delivered = _comm(transcript)
    assert sent["kind"] == "sent"
    assert sent["member"] == "w-1"
    assert sent["record"]["to"] == list(to)
    assert sent["record"]["recipients"] == recipients
    assert sent["record"]["op"] == {"n": 1}
    assert sent["rejected"] is None
    assert delivered == {
        "version": 1,
        "type": "comm",
        "kind": "delivered",
        "channel": "board",
        "member": "w-1",
        "span_id": None,
        "loop": None,
        "nested": False,
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
async def test_a_refusal_at_step_0_leaves_no_record(
    draft: dict[str, Any],
    states: dict[str, MemberState],
    stopping: bool,
    error: str,
) -> None:
    bus, _ = _bus(states=states)
    if stopping:
        bus.close()
    async with sample() as transcript:
        with pytest.raises(ToolError, match=error):
            await _send(bus, **draft)
    assert (_comm(transcript), _board(bus).applied) == ([], [])
    if not stopping:
        # no id was given
        delivery = await _send(bus)
        assert delivery.record.id == "b-1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload,error",
    [
        pytest.param("a" * 8000, None, id="ascii-at-limit"),
        pytest.param("😀" * 2000, None, id="four-byte-at-limit"),
        pytest.param(
            "😀" * 8000, "note is 32000 bytes; the limit is 8000", id="emoji-over"
        ),
    ],
)
async def test_max_bytes_counts_utf8_bytes_of_the_payload(
    payload: str, error: str | None
) -> None:
    bus, _ = _bus(channels=[_Board(StormControls(max_bytes=8000))])
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


@pytest.mark.parametrize(
    "rate", [(0, 60.0), (5, 0.0), (1, float("nan")), (1, float("inf"))]
)
def test_storm_controls_refuse_an_invalid_rate(
    rate: tuple[int, float],
) -> None:
    with pytest.raises(ValueError, match="rate must be a positive count"):
        StormControls(rate=rate)


@pytest.mark.anyio
async def test_a_payload_that_is_not_valid_text_is_refused_with_evidence() -> None:
    # a lone surrogate, which a model's JSON tool arguments can carry
    bus, _ = _bus(channels=[_Board(StormControls(max_bytes=100))])
    reason = "the note is not valid text: it has an unpaired surrogate at character 1"
    async with sample() as transcript:
        with pytest.raises(ToolError, match=reason):
            await _send(bus, payload="a\ud800")
    assert _board(bus).applied == []
    [sent] = _comm(transcript)
    assert sent["rejected"] == {"control": "payload", "reason": reason}


@pytest.mark.anyio
async def test_a_sender_that_has_ended_is_refused_with_evidence() -> None:
    # a task a member left behind still runs in its context after it ends
    bus, members = _bus()
    members["w-1"].state = "ended"
    reason = "w-1 has ended, so its note was not delivered."
    async with sample() as transcript:
        with pytest.raises(ToolError, match=reason):
            await _send(bus, to=("w-2", "w-3"))
    assert _board(bus).applied == []
    [sent] = _comm(transcript)
    assert sent["rejected"] == {"control": "liveness", "reason": reason}


@pytest.mark.anyio
@pytest.mark.parametrize("same_name", [False, True])
async def test_only_the_runs_own_members_can_deliver(same_name: bool) -> None:
    # outside any member, or in another run's member of the same name
    context = MemberContext("w-1", bridged=False) if same_name else None
    bus, _ = _bus()
    async with sample() as transcript:
        with _as(context), pytest.raises(RuntimeError, match="outside a member"):
            await bus.deliver(Draft("board", "note"))
    assert (_comm(transcript), _board(bus).applied) == ([], [])


def _set_path(op: Any, path: str) -> None:
    op["files"][0]["path"] = path


@pytest.mark.anyio
async def test_a_record_keeps_the_operation_data_it_was_sent_with() -> None:
    op: dict[str, JsonValue] = {"files": [{"file": "f-1", "path": "/a"}]}
    original = {"files": [{"file": "f-1", "path": "/a"}]}
    bus, _ = _bus()
    async with sample() as transcript:
        delivery = await _send(bus, op=op)
    # the tool's own dict changes after the send, and the delivered record cannot
    _set_path(op, "/b")
    with pytest.raises(TypeError):
        _set_path(delivery.record.op, "/c")
    for record in (delivery.record, _board(bus).applied[0]):
        assert record.op is not None and thaw_json(record.op) == original
    sent, _ = _comm(transcript)
    assert sent["record"]["op"] == original


@pytest.mark.anyio
@pytest.mark.parametrize(
    "op,held",
    [
        ({"files": {"f-1", "f-2"}}, "set"),
        ({"files": ("f-1", "f-2")}, "tuple"),
        ({"files": MappingProxyType({"f-1": "/a"})}, "mappingproxy"),
        ({"files": UserDict({"f-1": "/a"})}, "UserDict"),
        (MappingProxyType({"files": []}), "mappingproxy"),
    ],
    ids=["set", "tuple", "proxy", "userdict", "root-proxy"],
)
async def test_operation_data_other_than_dicts_and_lists_is_refused(
    op: Any, held: str
) -> None:
    # a channel's tool builds the op from plain JSON, so anything else is its bug
    bus, _ = _bus()
    async with sample() as transcript:
        with pytest.raises(TypeError, match=f"op must be JSON data; it holds a {held}"):
            await _send(bus, op=op)
    assert (_board(bus).applied, _comm(transcript)) == ([], [])
    assert (await _send(bus)).record.id == "b-1"


def _nested(shape: str, depth: int) -> JsonValue:
    # `depth` levels of containers around a leaf
    value: JsonValue = "leaf"
    for level in range(depth):
        if shape == "array" or (shape == "mixed" and level % 2):
            value = [value]
        else:
            value = {"k": value}
    return value


_NOT_LOGGED = "operation data can't be logged as sent"
_TOO_DEEP = "nested more than 100 levels deep"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "op,refusal",
    [
        ({"text": "café 🙂", "files": [{"päth": "日本"}]}, None),
        ({"n": 10**4299, "m": -(10**4299)}, None),
        ({"n": float("nan"), "m": float("inf")}, None),
        ({"k": _nested("mixed", 99)}, None),
        ({"text": "a\ud800"}, _NOT_LOGGED),
        ({"files": [{"path": "f\udc00"}]}, _NOT_LOGGED),
        ({"files": [{"\ud800": "f"}]}, _NOT_LOGGED),
        ({"n": 10**4300}, _NOT_LOGGED),
        ({"n": -(10**4300)}, _NOT_LOGGED),
        ({"files": [{"size": 10**4300}]}, _NOT_LOGGED),
        ({"k": _nested("array", 100)}, _TOO_DEEP),
        ({"k": _nested("object", 100)}, _TOO_DEEP),
        ({"k": _nested("mixed", 1099)}, _TOO_DEEP),
    ],
    ids=[
        "unicode",
        "int-4300-digits",
        "nan",
        "depth-100",
        "surrogate-value",
        "surrogate-nested",
        "surrogate-key",
        "int-4301-digits",
        "negative-int-4301-digits",
        "nested-int-4301-digits",
        "depth-101-array",
        "depth-101-object",
        "depth-1100",
    ],
)
async def test_operation_data_commits_only_if_the_log_reads_it_back_as_sent(
    op: dict[str, JsonValue], refusal: str | None
) -> None:
    bus, _ = _bus()
    async with sample() as transcript:
        if refusal is not None:
            with pytest.raises(ToolError, match=refusal):
                await _send(bus, op=op)
            assert (_board(bus).applied, _comm(transcript)) == ([], [])
        else:
            await _send(bus, op=op)
    # written and read as the eval log writes and reads a sample
    read = _parse_sample_data(
        to_json_safe(
            condense_sample(
                EvalSample(
                    id=1, epoch=1, input="", target="", events=list(transcript.events)
                )
            )
        )
    )
    sent = [
        e.data["record"]
        for e in read.events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("kind") == "sent"
        and isinstance(e.data["record"], dict)
    ]
    if refusal is not None:
        assert sent == []
        assert (await _send(bus)).record.id == "b-1"
    else:
        # as JSON text, so that NaN matches itself
        assert [json.dumps(r["op"]) for r in sent] == [json.dumps(op)]


@pytest.mark.anyio
async def test_record_ids_count_per_channel_and_seq_per_run() -> None:
    bus, _ = _bus(channels=[_Board(), _Notes()])
    deliveries = [
        await _send(bus, channel=channel) for channel in ("board", "notes", "board")
    ]
    assert [(d.record.id, d.seq) for d in deliveries] == [
        ("b-1", 1),
        ("n-1", 2),
        ("b-2", 3),
    ]


# Swarm tests: the runtime opens the channels and closes the bus


@agent
def bus_user(buses: list[Bus]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        bus = current_bus.get()
        assert bus is not None
        buses.append(bus)
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


@agent
def bus_test_neighbour(
    side: str, buses: dict[str, Bus], ready: anyio.Event, done: anyio.Event
) -> Agent:
    # "a" delivers from itself and a child task once "b", a member of the same name in
    # another run, has tried to deliver through a's bus
    async def execute(state: AgentState) -> AgentState:
        bus = current_bus.get()
        assert bus is not None
        buses[side] = bus
        if side == "a":
            ready.set()
            await done.wait()
            await bus.deliver(Draft("board", "note", payload="a"))
            async with anyio.create_task_group() as tg:
                tg.start_soon(bus.deliver, Draft("board", "note", payload="a's child"))
        else:
            await ready.wait()
            try:
                with pytest.raises(RuntimeError, match="outside a member"):
                    await buses["a"].deliver(Draft("board", "note", payload="b"))
            finally:
                done.set()
        return state

    return execute


@pytest.mark.anyio
async def test_a_member_of_another_run_with_the_same_name_cannot_deliver() -> None:
    buses: dict[str, Bus] = {}
    ready, done = anyio.Event(), anyio.Event()

    def run(side: str) -> Agent:
        return swarm(
            member(
                bus_test_neighbour,
                args={"side": side, "buses": buses, "ready": ready, "done": done},
                name="w",
            ),
            channels=["bus_test_board"],
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_swarm, run("a"))
        tg.start_soon(run_swarm, run("b"))
    applied = {
        side: [r.payload for r in _board(bus).applied] for side, bus in buses.items()
    }
    assert applied == {"a": ["a", "a's child"], "b": []}


@agent
def bus_test_child() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        bus = current_bus.get()
        assert bus is not None
        await bus.deliver(Draft("board", "note", payload="from the child"))
        return state

    return execute


@agent
def bus_test_parent(nested: bool) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        if nested:
            await run(bus_test_child(), "go")
        else:
            await bus_test_child()(state)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize("nested", [False, True], ids=["member", "descendant"])
async def test_comm_evidence_names_the_members_span_and_the_record_the_loops(
    nested: bool,
) -> None:
    outcome = await run_swarm(
        swarm(
            member(bus_test_parent, args={"nested": nested}, name="w"),
            channels=["bus_test_board"],
        )
    )
    spans = {
        e.id: e.name for e in outcome.transcript.events if isinstance(e, SpanBeginEvent)
    }
    [result] = evidence(outcome.transcript, "member_result")
    member_span = result["span_id"]
    assert spans[member_span] == "w"
    sent, delivered = _comm(outcome.transcript)
    assert (sent["span_id"], delivered["span_id"]) == (member_span, member_span)
    assert (sent["member"], delivered["member"]) == ("w", "w")
    loop_span = sent["record"]["sender_span_id"]
    assert spans[loop_span] == ("bus_test_child" if nested else "w")


@agent
def bus_test_late_sender(buses: list[Bus], outcomes: list[str]) -> Agent:
    # sends once the swarm has begun to stop, while it still runs in the grace period
    async def execute(state: AgentState) -> AgentState:
        bus = current_bus.get()
        assert bus is not None
        buses.append(bus)
        with anyio.fail_after(5):
            while not bus.stopping:
                await anyio.sleep(0.01)
        for _ in range(2):
            try:
                await bus.deliver(Draft(channel="board", kind="note"))
                outcomes.append("delivered")
            except ToolError as ex:
                outcomes.append(ex.message)
        return state

    return execute


@pytest.mark.anyio
async def test_a_stop_refuses_records_from_members_still_running() -> None:
    @swarm_controller(name="bus_test_start_and_return")
    def start_and_return() -> Controller:
        async def run(control: SwarmControl) -> StopReason:
            control.start("w")
            return "all_done"

        return Controller(run)

    buses: list[Bus] = []
    outcomes: list[str] = []
    outcome = await run_swarm(
        swarm(
            member(
                bus_test_late_sender,
                args={"buses": buses, "outcomes": outcomes},
                name="w",
            ),
            controller=start_and_return(),
            channels=["bus_test_board"],
        )
    )
    assert outcome.reason == "all_done"
    assert outcomes == ["the swarm is stopping; nothing more is delivered."] * 2
    [bus] = buses
    assert _board(bus).applied == []
    assert _comm(outcome.transcript) == []


# Waits: the bus releases a wait for a channel whose records wake waits


class _WakingState(_BoardState):
    # reports to a wait every applied record whose payload names the waiting member
    def wait_check(self, spec: WaitSpec) -> None:
        return None

    def wakes(self, spec: WaitSpec, record: Record) -> bool:
        return record.payload == spec.member

    def news(self, spec: WaitSpec) -> WaitNews | None:
        if any(r.payload == spec.member for r in self.applied):
            return WaitNews([f"a note for {spec.member}"], [])
        return None


class _Waking(_Board):
    name = "waking"
    prefix = "k"
    max_wait = 5

    def open(self, run: ChannelRun) -> ChannelState | None:
        return _WakingState(None)


class _LongerWaking(_Waking):
    name = "longer"
    prefix = "l"
    max_wait = 30


class _WakingWithoutWaits(_Board):
    max_wait = 10


class _WakingWithoutState(Channel):
    # opens no state, as an instructions-only channel does
    max_wait = 3600


@pytest.mark.anyio
async def test_only_a_record_with_wake_releases_a_wait() -> None:
    bus, members = _bus(channels=[_Waking(), _LongerWaking()])
    # the largest of the waking channels' max_wait
    assert bus.waits.max_wait == 30
    results: list[str] = []

    async def wait() -> None:
        with _as(members["w-2"].context):
            results.append(await bus.wait(None, None))

    async def send(wake: bool) -> None:
        with _as(members["w-1"].context):
            await bus.deliver(Draft("waking", "note", payload="w-2", wake=wake))

    async with sample() as transcript:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(wait)
                await anyio.wait_all_tasks_blocked()
                await send(False)
                await anyio.wait_all_tasks_blocked()
                assert results == []
                await send(True)
    assert results == ["a note for w-2"]
    sent = [c["record"]["wake"] for c in _comm(transcript) if c["kind"] == "sent"]
    assert sent == [False, True]
    [waited] = [c for c in _comm(transcript) if c["kind"] == "waited"]
    assert (waited["channels"], waited["woken_by"]) == (["waking", "longer"], [2])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channels",
    [
        pytest.param(lambda: [_WakingWithoutWaits()], id="state-without-waits"),
        pytest.param(lambda: [_WakingWithoutState()], id="no-state"),
        pytest.param(lambda: [_Waking(), _WakingWithoutState()], id="no-state-beside"),
    ],
)
async def test_a_channel_with_max_wait_needs_a_waiting_state(
    channels: Callable[[], list[Channel]],
) -> None:
    with pytest.raises(TypeError, match="does not implement WaitingState"):
        _bus(channels=channels())


@pytest.mark.anyio
async def test_an_instructions_only_channel_needs_no_waiting_state() -> None:
    bus, _ = _bus(channels=[_Locks(), _Waking()])
    assert bus.waits.max_wait == 5


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
