# Channels – Inspect Swarm

## Overview

`channels=` decides how members share information. There are three built-in channels:

- `filesystem`, the default: a convention for sharing files in a sandbox, stated in each member’s preamble. It adds no tools.
- `shared_memory`: a board of files that members read and write with a tool, with a record of every write and read. See [Shared Memory](./shared-memory.html.md).
- `messages`: direct messages between members, with a record of every message and read. See [Messages](./messages.html.md).

`channels=[]` gives members no channel instructions. Channels change what members are told and what tools they get, not what they can reach: members share the sample’s sandbox whatever the channels, unless they have their own ([Sandboxes](./sandboxes.html.md)).

A channel is given as an object, `filesystem(...)`, or by its registered name with its default arguments, `"filesystem"`. Each channel can be given once.

## The filesystem channel

`filesystem` tells each member, in its preamble, to share files in `/shared`, to read and append to `/shared/notes.md`, and to keep its own work in `/scratch/<member>`. It does not create these directories. Change the paths with `filesystem(...)`:

``` python
from inspect_ai.agent import react

from inspect_swarm import filesystem, member, swarm

solver = swarm(
    members=member(react, count=4),
    channels=[filesystem(shared="/work/shared", scratch="/work/{member}")],
)
```

A member that chooses its own name ([Members](./members.html.md#members-that-choose-their-names)) is told to keep its work in `/scratch/<your name>`, a directory named with the name it chose, and to check that no other member uses the same one. The members you named are still told `/scratch/<member>`, and, when any member chooses, that a member that chooses could pick the same directory, so they check that no other member uses it.

When members have their own sandboxes and each sandbox mounts one shared volume, use `filesystem(shared_volume=True)`. Set `shared=` to the absolute path where the volume is mounted; `notes=` must be inside it. The preamble then says that `shared` and the notes file in it are the same in every member’s sandbox, and gives no scratch directory, since each sandbox is already private. The swarm cannot check that the volume is mounted, and it refuses `shared_volume=True` when members share the sample’s sandbox. See [Sandboxes](./sandboxes.html.md#a-shared-volume).

[`examples/shared_sandbox.py`](https://github.com/meridianlabs-ai/inspect_swarm/blob/main/examples/shared_sandbox.py) runs [bash()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#bash) members in one container with this channel.

## Shared files are observed, not recorded

The filesystem channel opens no state and offers no tools, so no tool of the swarm’s carries file traffic. What the log has is each member’s own tool calls ([bash()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#bash), [python()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#python), [text_editor()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#text_editor)), as [ToolEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#toolevent)s.

Those tool calls are evidence that members may have communicated through files, not a complete record. Processes a member starts in the sandbox can read and write files between logged tool calls, and after the member has ended. A bridged member’s file operations are the CLI’s own tools, which leave no [ToolEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#toolevent) at all. An experiment that attributes communication to the filesystem must state this limit, or add its own instrumentation, such as filesystem snapshots or audit logs in the sandbox.

The same holds for every other path between members that the swarm does not carry:

- A built-in tool given the same explicit `instance` in several members, such as `memory(instance="board")`, shares its state through the sample store. Members’ [ToolEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#toolevent)s record its use, and nothing else does. See [Members](./members.html.md#tool-state).
- A tool that reads the transcript sees the other members’ model and tool calls ([Scoring](./scoring.html.md#the-record-belongs-to-the-swarm)).

Channels limit what members are told, not what they can do. An arm meant to have one channel only, or none, needs members that cannot reach each other otherwise: their own sandboxes, and no shared tool instances.

## Writing a channel

A channel is a factory decorated with `@swarm_channel` that returns a [Channel](./reference/inspect_swarm.html.md#channel). A channel that only adds instructions, as `filesystem` does, subclasses [Channel](./reference/inspect_swarm.html.md#channel) and overrides `instructions()`:

``` python
from inspect_swarm import Channel, MemberInfo, swarm_channel


class LeadChannel(Channel):
    def __init__(self, lead: str) -> None:
        self.lead = lead

    def instructions(self, member: MemberInfo) -> str | None:
        if member.name == self.lead:
            return "Write the plan to /shared/plan.md before you start."
        return f"Read /shared/plan.md, which {self.lead} writes, before you start."


@swarm_channel
def lead(name: str) -> Channel:
    """Tell members that one of them writes a plan for the others."""
    return LeadChannel(name)
```

`instructions()` returns text for the member’s preamble, or `None` to add nothing. `check()`, if overridden, is called with the roster when [swarm()](./reference/inspect_swarm.html.md#swarm) is called and raises `ValueError` for a roster the channel cannot serve. A [Channel](./reference/inspect_swarm.html.md#channel) holds configuration only: one object serves every sample. The factory’s arguments are logged with the channel.

A channel with tools and records, as `shared_memory` is, also sets `name` and `prefix` and returns a [ChannelState](./reference/inspect_swarm.html.md#channelstate) from `open()` for each run of the swarm; its records go through the swarm’s bus. Its `reach` decides which of a member’s loops get its tools from [swarm_tools()](./reference/inspect_swarm.html.md#swarm_tools): `"loop"` (the default), only the member’s own loop; `"member"`, also the nested loops the member’s agent gives [swarm_tools()](./reference/inspect_swarm.html.md#swarm_tools). The agent decides which nested loops get [swarm_tools()](./reference/inspect_swarm.html.md#swarm_tools); `reach` filters the channel tools they get and which channels can wake their `swarm_wait`. A channel whose reads consume something for the member, such as an inbox that marks messages read, keeps `"loop"`, so that a subagent cannot read what the member’s own loop then never sees. `shared_memory` sets `"member"`: a view consumes nothing, and each loop keeps its own seen state. A channel’s tools are given to the member as they are: with the `messages` channel, their results do not count the member’s unread messages, as the swarm’s own tools do. A member learns of messages from those, or by calling `list_members`. [swarm_bridged_tools()](./reference/inspect_swarm.html.md#swarm_bridged_tools) serves only the built-in [messages()](./reference/inspect_swarm.html.md#messages) and [shared_memory()](./reference/inspect_swarm.html.md#shared_memory) channels, so [swarm()](./reference/inspect_swarm.html.md#swarm) refuses a bridged member beside a custom channel with records ([Restrictions](./restrictions.html.md#channels-for-bridged-members)).

This channel keeps a notice board. Any member posts a notice, and every member reads them all:

``` python
from collections.abc import Mapping

from inspect_ai.tool import Tool, ToolDef, ToolError
from pydantic import JsonValue

from inspect_swarm import (
    Bus,
    Channel,
    ChannelRun,
    ChannelState,
    Draft,
    MemberInfo,
    Record,
    calling_member,
    swarm_channel,
)


class NoticesState:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.notices: list[Record] = []

    def tools(self) -> list[Tool]:
        async def post_notice(text: str) -> str:
            """Post a notice that every member can read.

            Args:
                text: The notice.
            """
            delivery = await self.bus.deliver(Draft("notices", "notice", payload=text))
            return f"Posted {delivery.record.id}."

        async def read_notices() -> str:
            """Read the other members' notices."""
            caller = calling_member()
            assert caller is not None
            shown = [r for r in self.notices if r.sender != caller.member]
            self.bus.read("notices", [r.id for r in shown])
            lines = [f"{r.id} from {r.sender}: {r.payload}" for r in shown]
            return "\n".join(lines) or "No notices."

        return [ToolDef(post_notice).as_tool(), ToolDef(read_notices).as_tool()]

    def validate(self, record: Record) -> None:
        if not record.payload.strip():
            raise ToolError("A notice needs text.")

    def admit(self, record: Record) -> None:
        pass

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        self.notices.append(record)
        return [], None


class Notices(Channel):
    name = "notices"
    prefix = "n"

    def open(self, run: ChannelRun) -> ChannelState:
        return NoticesState(run.bus)

    def instructions(self, member: MemberInfo) -> str | None:
        return "Post what others should know with post_notice, and read theirs with read_notices."


@swarm_channel
def notices() -> Channel:
    return Notices()
```

Members get the tools from [swarm_tools()](./reference/inspect_swarm.html.md#swarm_tools), as with the built-in channels. A tool hands the bus a [Draft](./reference/inspect_swarm.html.md#draft), and `run.bus.deliver()` makes the [Record](./reference/inspect_swarm.html.md#record), binds the sender from the calling member, checks it, and calls the state’s `validate()`, then `admit()` and `apply()` with no `await` between them, so what `admit()` checks still holds when `apply()` runs. After address resolution and validation, the log records accepted sends and refusals by liveness, payload and storm checks or `admit()` as `sent` events. Each delivered record also has a `delivered` event. Earlier refusals, including a stopping swarm, produce no communication event. A refusal raises [ToolError](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#toolerror), which the member sees as the tool’s result.

A draft with no `to` is unaddressed: it reaches nobody directly, and the channel decides whom it concerns. `apply()` returns the members the record is news to and a JSON mapping, both written in the `delivered` event, and may name members that have not started or have ended. A draft addressed with `to=` is refused if a member it names has not started, is ending or has ended.

[calling_member()](./reference/inspect_swarm.html.md#calling_member) tells a tool which member it acts for: a [Caller](./reference/inspect_swarm.html.md#caller) with the member’s name, whether the calling loop is a nested one, and that loop’s agent span. It comes from the runtime, never from model output, and is `None` outside a member of a swarm.

A tool whose result shows the channel’s records reports them with `run.bus.read(name, records)`, using the [ChannelRun](./reference/inspect_swarm.html.md#channelrun) that `open()` was given: `name` is the channel’s `name`, and `records` are the ids of the records the result shows. A waiting channel’s `news()` reports them in `WaitNews(lines, files, depends_on=[...])`. The log records them as the result’s `depends_on`, the records the member’s later work depends on, which [the critical path](./analysis.html.md#the-critical-path) links. Two rules decide what a result depends on:

- A result that tells the loop a record was committed, without showing its content, as a wait does, depends on it.
- A result built from several records, such as a file made by several writes, depends on them all. Pass them as `depends_on=` when they differ from the records the result shows.

A channel that reports nothing adds no dependency. Every id must be a record the channel committed in this run, or the call raises `ValueError`.

`read()` returns the read’s id (`r-5`), which the log’s `read` event carries. Pass `listing=True` for a result that shows what exists rather than its content, which the metrics count as a listing, not a read.

See the docstrings of [Channel](./reference/inspect_swarm.html.md#channel), [ChannelRun](./reference/inspect_swarm.html.md#channelrun), [ChannelState](./reference/inspect_swarm.html.md#channelstate), [Bus](./reference/inspect_swarm.html.md#bus), [Draft](./reference/inspect_swarm.html.md#draft), [Delivery](./reference/inspect_swarm.html.md#delivery), `calling_member`, [WaitingState](./reference/inspect_swarm.html.md#waitingstate) and [WaitNews](./reference/inspect_swarm.html.md#waitnews) in the [reference](./reference/index.html.md).
