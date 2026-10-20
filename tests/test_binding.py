import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import (
    Agent,
    AgentChannel,
    deepagent,
    general,
    react,
)
from inspect_ai.agent._bridge.util import bridge_model_generate
from inspect_ai.agent._channel import current_agent_channel
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent, SpanBeginEvent
from inspect_ai.log import EvalLog
from inspect_ai.log._samples import sample_active
from inspect_ai.model import (
    ChatMessage,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelOutput,
    get_model,
)
from inspect_ai.tool import Tool, ToolChoice, ToolDef, ToolInfo, tool
from pydantic import JsonValue

from inspect_swarm import (
    Channel,
    ChannelRun,
    ChannelState,
    Record,
    member,
    swarm,
    swarm_channel,
    swarm_tools,
)
from inspect_swarm._budget import SwarmTurnNode, current_member
from inspect_swarm._bus._bus import Bus
from inspect_swarm._bus._record import Draft
from inspect_swarm._bus._storm import StormControls
from inspect_swarm._controller._control import MemberRun


# A test channel whose one tool, `<name>_write`, delivers its text as a record. The
# "loop" channel keeps the default reach, and the "member" channel has member reach.
class _WriteState:
    def __init__(self, channel: str, bus: Bus) -> None:
        self.channel = channel
        self.bus = bus

    def tools(self) -> list[Tool]:
        async def execute(text: str) -> str:
            """Write text to the channel.

            Args:
                text: The text.
            """
            delivery = await self.bus.deliver(
                Draft(self.channel, "write", payload=text)
            )
            return delivery.record.id

        return [ToolDef(execute, name=f"{self.channel}_write").as_tool()]

    def validate(self, record: Record) -> None:
        pass

    def admit(self, record: Record) -> None:
        pass

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        return [], None


class _Loop(Channel):
    name = "loop"
    prefix = "l"

    def open(self, run: ChannelRun) -> ChannelState | None:
        assert self.name is not None
        return _WriteState(self.name, run.bus)


class _Member(_Loop):
    name = "member"
    prefix = "m"
    reach = "member"

    def __init__(self, rate: tuple[int, float] | None) -> None:
        self.storm = StormControls(rate=rate)


@swarm_channel
def binding_test_loop() -> Channel:
    return _Loop()


@swarm_channel
def binding_test_member(rate: tuple[int, float] | None = None) -> Channel:
    return _Member(rate)


# A member's conversation, scripted: the top loop's tool calls in order, then its
# subagent's when the top loop dispatches `general` with "child of <name>". The tools
# each generate was offered are kept, by speaker.
class _Plan:
    def __init__(
        self,
        parent: list[tuple[str, dict[str, Any]]],
        child: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.parent = parent
        self.child = child or []
        self.offered: list[tuple[str, set[str]]] = []

    def __call__(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        users = "\n".join(m.text for m in input if isinstance(m, ChatMessageUser))
        child = re.search(r"child of (\S+)", users)
        if child is not None:
            name, speaker, plan = child.group(1), "child", self.child
        else:
            found = re.search(r"You are (\S+),", users)
            assert found is not None
            name, speaker, plan = found.group(1), "parent", self.parent
        self.offered.append((speaker, {t.name for t in tools}))
        step = len([m for m in input if isinstance(m, ChatMessageTool)])
        if step < len(plan):
            function, arguments = plan[step]
            return ModelOutput.for_tool_call(
                "mockllm",
                function,
                {
                    k: v.format(name=name) if isinstance(v, str) else v
                    for k, v in arguments.items()
                },
            )
        return ModelOutput.for_tool_call("mockllm", "submit", {"answer": "done"})


def _model(plan: _Plan) -> Model:
    return get_model("mockllm/model", memoize=False, custom_outputs=plan)


async def _eval(solver: Agent, tmp_path: Path) -> EvalLog:
    # a running sample, for deepagent's checkpoints and the sample's ACP session
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=solver),
        model="mockllm/model",
        log_dir=str(tmp_path),
    )
    assert log.status == "success", log.error
    return log


def _sent(log: EvalLog) -> list[dict[str, Any]]:
    assert log.samples is not None
    return [
        e.data
        for e in log.samples[0].events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("type") == "comm"
        and e.data.get("kind") == "sent"
    ]


@pytest.mark.anyio
async def test_a_subagent_gets_only_member_reach_tools_and_writes_as_the_member(
    tmp_path: Path,
) -> None:
    plan = _Plan(
        parent=[
            ("member_write", {"text": "parent"}),
            ("agent", {"prompt": "child of {name}"}),
            ("loop_write", {"text": "parent after the child"}),
        ],
        child=[
            ("member_write", {"text": "child"}),
            ("member_write", {"text": "child again"}),
        ],
    )
    log = await _eval(
        swarm(
            member(
                deepagent,
                args={
                    "model": _model(plan),
                    "tools": [swarm_tools()],
                    "subagents": [general()],
                    "todo_write": False,
                },
                name="w",
            ),
            # the member's writes share one rate, whichever loop makes them
            channels=[binding_test_loop(), binding_test_member(rate=(2, 3600.0))],
        ),
        tmp_path,
    )

    # the binding stays on the top loop, before and after its subagent runs
    offered = {
        speaker: [
            {"loop_write", "member_write"} & tools
            for s, tools in plan.offered
            if s == speaker
        ]
        for speaker in ("parent", "child")
    }
    assert offered == {
        "parent": [{"loop_write", "member_write"}] * 4,
        "child": [{"member_write"}] * 3,
    }

    sent = _sent(log)
    assert [
        (
            s["channel"],
            s["record"]["payload"],
            s["member"],
            s["nested"],
            s["rejected"] and s["rejected"]["control"],
        )
        for s in sent
    ] == [
        ("member", "parent", "w", False, None),
        ("member", "child", "w", True, None),
        ("member", "child again", "w", True, "rate"),
        ("loop", "parent after the child", "w", False, None),
    ]
    # the remaining window depends on how long the run took to get there
    assert re.fullmatch(r"rate limit: retry in \d+ s", sent[2]["rejected"]["reason"])
    # each write names the loop that made it
    assert log.samples is not None
    spans = {
        e.id: e.name for e in log.samples[0].events if isinstance(e, SpanBeginEvent)
    }
    loops = [s["loop"] for s in sent]
    assert all(s["loop"] == s["record"]["sender_span_id"] for s in sent)
    assert loops[0] == loops[3] != loops[1] == loops[2]
    assert spans[loops[1]] == "general"


@tool
def binding_test_probe(probes: list[tuple[str, bool | None, bool]]) -> Tool:
    async def execute() -> str:
        """Report the member's ACP binding."""
        context = current_member.get()
        active = sample_active()
        assert context is not None and active is not None
        transport = active.acp_transport
        assert transport is not None
        ref = transport.ref
        bound = ref is not None and ref._channel is current_agent_channel()  # pyright: ignore[reportPrivateUsage]
        probes.append((context.name, context.acp_bound, bound))
        return "ok"

    return execute


@pytest.mark.anyio
async def test_the_acp_flag_says_whether_acp_drives_the_members_loop_and_nothing_is_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[object] = []

    def record(self: AgentChannel, item: object) -> None:
        posted.append(item)

    monkeypatch.setattr(AgentChannel, "_post", record)
    monkeypatch.setattr(AgentChannel, "_interrupt", record)
    probes: list[tuple[str, bool | None, bool]] = []
    plan = _Plan(parent=[("binding_test_probe", {})])
    await _eval(
        swarm(
            member(
                react,
                args={
                    "model": _model(plan),
                    "tools": [binding_test_probe(probes), swarm_tools()],
                },
                name="w",
                count=2,
            ),
            channels=[binding_test_loop()],
        ),
        tmp_path,
    )
    assert sorted(name for name, _, _ in probes) == ["w-1", "w-2"]
    assert all(flag == bound for _, flag, bound in probes)
    # the first member to open its loop is bound; a sibling only once it has ended
    assert any(bound for _, _, bound in probes)
    assert posted == []


@pytest.mark.anyio
async def test_outside_a_swarm_there_are_no_swarm_tools() -> None:
    assert await swarm_tools().tools() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "delivery,bridged,descendant,effective",
    [
        pytest.param(None, False, False, "notify", id="default"),
        pytest.param("poll", False, False, "poll", id="poll"),
        pytest.param("notify", True, False, "poll", id="bridged"),
        pytest.param("notify", False, True, "notify", id="bridged-descendant"),
    ],
)
async def test_effective_delivery(
    delivery: Any, bridged: bool, descendant: bool, effective: str
) -> None:
    run = MemberRun(member(react(), delivery=delivery, bridged=bridged), "w", ())
    if descendant:
        # a bridged descendant's generation, checked by one of the member's nodes,
        # finds the member bridged for its budget's sake
        token = current_member.set(run.context)
        try:
            with bridge_model_generate():
                SwarmTurnNode(None, owner="w").detect_bridge()
        finally:
            current_member.reset(token)
        assert run.context.bridged
    assert run.context.delivery_effective == effective
