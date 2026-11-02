# inspect_swarm – Inspect Swarm

## Swarms and members

### swarm

A swarm of agents working on one sample.

Each member gets a timeline in the sample named after it, so member names must be unique across the swarm runs and other timelines in a sample. To run swarms more than once in one sample, build them with distinct member names. A member may not be named `init`, `solvers` or `scorers`: Inspect’s timelines read spans with those names as the sample’s phases.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_swarm.py#L39)

``` python
@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],
    *,
    controller: str | Controller = "leaderless",
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),
    budget: Budget = Budget(),  # noqa: B008
) -> Agent
```

`members` [Member](../reference/inspect_swarm.configuration.html.md#member) \| [Agent](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#agent) \| Sequence\[[Member](../reference/inspect_swarm.configuration.html.md#member) \| [Agent](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#agent)\]  
The roster: built agents or [member()](../reference/inspect_swarm.html.md#member) records. A bare agent is `member(agent)`. A factory-form [member()](../reference/inspect_swarm.html.md#member) may stand for several members (`count`).

`controller` str \| [Controller](../reference/inspect_swarm.html.md#controller)  
How the swarm runs: who starts, when and with what input, and when the swarm is done. A name resolves to a registered controller with its default parameters.

`channels` str \| [Channel](../reference/inspect_swarm.html.md#channel) \| Sequence\[str \| [Channel](../reference/inspect_swarm.html.md#channel)\]  
How members share information. A name resolves to a registered channel with its default parameters. `[]` means none: members get no channel instructions, though a shared sandbox is still shared.

`budget` [Budget](../reference/inspect_swarm.configuration.html.md#budget)  
The swarm-wide cap.

### member

A swarm member, with a name, role, limits or count.

A member is a built agent, `member(react(prompt=...))`, or a registered `@agent` factory and its arguments, `member(deepagent, args={"tools": [bash()]}, count=4)`. Both must be registered with `@agent`, so that the log can name them and replay can find them. A built agent can also be passed to `swarm(members=...)` without [member()](../reference/inspect_swarm.html.md#member).

No agent object runs twice at the same time in one sample. A built agent is one member: it is built once, when the task is built, and shared by concurrent samples, as any Inspect solver is. The same agent object cannot be in two members. Several copies of one configuration (`count` above 1) need the factory form: the swarm calls `factory(**args)` once for each member in each sample, so each copy gets its own agent and whatever the factory builds. [member()](../reference/inspect_swarm.html.md#member) checks the arguments against the factory’s signature but does not call it, so the factory’s own checks run when the member is built.

Names. Members left on their default name are numbered across the whole roster, not per record, so one member with different arguments takes two records: `[member(react, args={"prompt": a}, count=6), member(react, args={"prompt": b, "model": m})]` is `react-1` to `react-7`. When every copy differs, give each its own record, as in `[member(react, args={"prompt": p}) for p in prompts]`. Names given with `name` are not numbered across records, so give each record its own.

Objects given to an agent are shared. Every member built from one record receives the same objects from `args`, and two built agents given the same tool object share it, across members and samples. Inspect’s built-in tools keep their state in the sample store or the sandbox, so this matters only for a custom tool or helper that keeps state in its Python object (a counter, a cache, a client). To give each member its own, construct it inside a member builder, an `@agent` factory that builds the configured agent, and use the factory form.

Tool state. Each member runs in its own tool-state scope, named after the member. Built-in tools that keep state in the sample store ([memory()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#memory), [bash_session()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#bash_session), [web_browser()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#web_browser), the skill tool) give each member its own state when their `instance` is left at the default, so the default [memory()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#memory) is private to each member. An explicit `instance` is used as given: members, or the copies of one counted member, that name the same instance share its state, so a board shared on purpose is `memory(instance="board")`. The sample store itself is shared. State that custom tools or agent code keep in the store with no instance is shared unless they resolve their instance with [tool_state_instance()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#tool_state_instance). Built-in tool state that a setup solver prepared at the default instance before the swarm runs is not visible to the members.

Submissions. A member that returns normally has submitted, and its output is its answer, unless the output’s stop reason is `content_filter` or `model_length`. That is how [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react) returns after three refusals or a context overflow it could not recover from, so the member ends `unsubmitted` and the swarm never chooses its output as the answer. Other agents keep the rule that a normal return is a submission: an agent that gives up in some other way and returns is treated as having answered.

Stopping itself. An agent that raises `LimitExceededError("custom", ...)` with no `source` to stop itself ends only its member, with status `member_limit:custom`; the other members continue and the swarm never chooses its output as the answer. This holds when the error is raised in a task group inside the agent, too.

Sandboxes. Members share the sample’s sandbox by default. With `sandbox`, a member works in its own: the swarm makes that environment the member’s default, so [sandbox()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#sandbox) in its tools returns it, and hides the sample’s other environments from the member except those in `visible_sandboxes`, such as a shared service. Tools that choose a sandbox without a name, such as [text_editor()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#text_editor), [bash_session()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#bash_session) and the agent bridge, use the member’s own; a visible service is reached only by name. `sandbox="{member}"` gives each copy of a counted member the environment named after it: `member(factory, name="agent", count=2, sandbox="{member}")` runs `agent-1` and `agent-2` in the environments of those names. The sample’s sandbox configuration provides the environments (for Docker, a compose service for each), and the swarm checks that they exist when it starts. When one member has its own sandbox, every member must have one, no two the same, and the `filesystem` channel, which describes a shared sandbox, can be used only as `filesystem(shared_volume=True)`, for sandboxes that each mount one shared volume. A service in `visible_sandboxes` is reachable from any of the member’s tools that take a sandbox name, so list only services every member may use.

Chosen names. With `choose_name=True` the member is not told its name: its preamble says how many agents are in the swarm and asks it to choose a name for itself and tell the other members, and the `filesystem` channel tells it to keep its work in a directory named with the name it chose. Its role, if it has one, is still stated. The swarm’s records, spans, tool-state scopes and budget shares still use `name`. A swarm in which any member chooses its name can use only the `filesystem` channel or no channel (`channels=[]`), since other channels show members to each other by name. Text you give the member yourself is not changed: the sample input, a controller’s `start(input=...)`, the agent’s prompt and the sandbox configuration.

Scoring with member sandboxes. The task’s scorers run after the swarm, outside every member, where [sandbox()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#sandbox) is `default`. A scorer that grades the members’ work names each member’s sandbox itself, as in `sandbox("agent-1")`, and the `sandbox` field of each member in the swarm’s result record says which member used which.

Background work belongs to the member. Work its agent starts with [background()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#background), such as `deepagent(background=...)` subagents, spends through the member’s limits and share, and is the member’s in the ledger. When the member’s agent returns, the swarm stops the background work from starting model calls, gives it the swarm’s grace period to finish calls and tool calls in flight, and cancels what is left. The member ends after that, with the status its agent ended with; `Budget.grace=0` cancels background work at once. A member time limit bounds its agent only: after the agent returns, its background work gets the grace period too, and the cancelled work’s cleanup is awaited. Background work must be started by the agent while it runs; a factory that starts it while building the agent makes the member `errored`. A sourceless custom limit error raised in background work ends only the task that raised it; the member’s agent and its other background work continue. Work started outside [background()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#background) (an `asyncio` task, a process left running in the sandbox) is not stopped. Bridged members cannot have background work: [background()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#background) raises in a member declared or found bridged, and a member that uses the agent bridge after starting background work (through a bridged subagent, for example) is cancelled and ends `errored`.

ACP agents, which wait for their caller to drive them rather than returning, are refused: a built one when [member()](../reference/inspect_swarm.html.md#member) is called, and a factory’s when the member is built.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_member.py#L198)

``` python
def member(
    agent: Agent | AgentFactory,
    args: Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    role: str | None = None,
    choose_name: bool = False,
    count: int = 1,
    limits: Sequence[Limit] | None = None,
    bridged: bool | None = None,
    sandbox: str | None = None,
    visible_sandboxes: Sequence[str] = (),
) -> Member
```

`agent` [Agent](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#agent) \| AgentFactory  
A built, registered agent, or a registered `@agent` factory.

`args` Mapping\[str, Any\] \| None  
The factory’s keyword arguments. Only for a factory.

`name` str \| None  
The member’s name. Defaults to the agent’s or factory’s registry name, without its package; a default name that more than one member has is numbered across the roster. With `count` above 1 the members are `name-1` to `name-k`. It names the member’s tool-state scope, so it cannot be empty or contain “/” or “#”. With `choose_name=True` the member is not told it.

`role` str \| None  
The member’s role, given to the controller and in the member’s preamble.

`choose_name` bool  
Whether the member chooses its own name instead of being told `name`.

`count` int  
How many members this record stands for. Above 1 needs a factory.

`limits` Sequence\[[Limit](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#limit)\] \| None  
The member’s own token, cost, turn, message or time limits.

`bridged` bool \| None  
Whether the agent is bridged (runs a CLI in the sandbox through Inspect’s agent bridge). `None` counts inspect_swe’s agents as bridged and others as native. Pass `True` for any other bridged agent: the swarm stops a bridged member by cancelling it, because a limit error raised inside the bridge would end the sample, and it must know before the member’s first model request. Bridged members cannot use channels with tools and records yet, and [swarm()](../reference/inspect_swarm.html.md#swarm) refuses one beside such a channel. A bridged agent that is not declared and not in the `inspect_swe/` namespace is not refused: it cannot reach those channels’ tools, so it runs without them. Pass `True` to have it refused instead.

`sandbox` str \| None  
The name of the member’s own sandbox environment. `{member}` stands for the member’s name. `None` shares the sample’s sandbox.

`visible_sandboxes` Sequence\[str\]  
Other sandbox environments the member can reach by name, such as a shared service. Only with `sandbox`. It cannot include `default`, which inside the member is the member’s own sandbox.

## Controllers

### leaderless

Start every member with the swarm’s input; stop at the first submission, or when all have ended.

By default the swarm stops when the first member submits. The swarm’s answer is the first submission, so nothing the other members do after it can change the answer. The members still running are stopped: they finish the calls they have in flight, within `Budget.grace`, and the ones still running after it are cancelled.

Pass `stop_on_submission=False` to let every member run to its end when the other members’ work still matters: tasks whose scorer reads a shared artifact (such as the sandbox) after the members end, where members still working change what is scored; arms that measure each member’s result (<team@k>); and agents for which any normal return counts as a submission, so that a member that gives up would end the swarm. Add `final="last"` to make the latest submission the answer, for a team whose members build on each other’s work.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/leaderless.py#L8)

``` python
@swarm_controller
def leaderless(
    stop_on_submission: bool = True, *, final: FinalName | None = None
) -> Controller
```

`stop_on_submission` bool  
Stop the swarm when the first member ends with a submission.

`final` [FinalName](../reference/inspect_swarm.html.md#finalname) \| None  
How the swarm chooses its answer from the submissions: `first`, the earliest, or `last`, the latest. A tie goes to the member listed first. `None` is the default, `first`. `last` needs `stop_on_submission=False`.

### swarm_controller

Decorator for registering swarm controllers.

The factory’s arguments are recorded with the controller, so they appear in the log and the controller can be rebuilt from it. A factory in a package is registered as `<package>/<name>`.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_controller.py#L67)

``` python
def swarm_controller(
    func: Callable[P, Controller] | None = None, /, *, name: str | None = None
) -> (
    Callable[P, Controller]
    | Callable[[Callable[P, Controller]], Callable[P, Controller]]
)
```

`func` Callable\[P, [Controller](../reference/inspect_swarm.html.md#controller)\] \| None  
A function returning a [Controller](../reference/inspect_swarm.html.md#controller).

`name` str \| None  
The registered name, in place of the function’s name.

### Controller

How a swarm runs: which members start, when and with what input, and when the swarm is done.

A [Controller](../reference/inspect_swarm.html.md#controller) holds configuration only: one object serves every sample, so per-sample state belongs in `run`’s local variables. Return one from a factory decorated with `@swarm_controller`.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_controller.py#L28)

``` python
class Controller
```

#### Methods

\_\_init\_\_  
Create a controller.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_controller.py#L34)

``` python
def __init__(
    self,
    run: Callable[[SwarmControl], Awaitable[StopReason]],
    *,
    final: FinalName | None = None,
    check: Callable[[Sequence[MemberInfo]], None] | None = None,
) -> None
```

`run` Callable\[\[[SwarmControl](../reference/inspect_swarm.html.md#swarmcontrol)\], Awaitable\[[StopReason](../reference/inspect_swarm.html.md#stopreason)\]\]  
Called once per sample. Starts members and waits for their events through the [SwarmControl](../reference/inspect_swarm.html.md#swarmcontrol), and returns the stop reason. Returning requests the stop.

`final` [FinalName](../reference/inspect_swarm.html.md#finalname) \| None  
How the swarm chooses its answer from the submissions, for the provisional answer and when the swarm ends: `first`, the earliest submission, or `last`, the latest. `None` is the default, `first`. When `run` names the answer with `SwarmControl.choose_answer()`, that choice is the answer instead.

`check` Callable\[\[Sequence\[[MemberInfo](../reference/inspect_swarm.html.md#memberinfo)\]\], None\] \| None  
Called with the roster when the swarm is built. Raise `ValueError` for a roster the controller cannot run.

### SwarmControl

What a controller’s `run` works through: the swarm’s members and their events, for one sample.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L117)

``` python
class SwarmControl(Protocol)
```

#### Attributes

`members` Sequence\[[MemberHandle](../reference/inspect_swarm.html.md#memberhandle)\]  
The members, in roster order.

#### Methods

member  
The member with this name.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L125)

``` python
def member(self, name: str) -> MemberHandle
```

`name` str  
The member’s name.

start  
Start a member, and return at once.

The member runs in its own task, span and tool-state scope, with its limits. Its input is `input`, or the swarm’s input when `input` is `None`, followed by the member’s preamble: its name unless `choose_name=True`, its role and the roster size, how the budget is split, and each channel’s instructions.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L136)

``` python
def start(
    self, member: str | MemberHandle, input: str | list[ChatMessage] | None = None
) -> None
```

`member` str \| [MemberHandle](../reference/inspect_swarm.html.md#memberhandle)  
The member, or its name.

`input` str \| list\[[ChatMessage](https://inspect.aisi.org.uk/reference/inspect_ai.model.html#chatmessage)\] \| None  
The member’s input. `None` gives it the swarm’s input.

events  
The swarm’s events, in order from the swarm’s start.

Each event is yielded once. The iterator ends when no member is running and no event is waiting; a member started later is reported by a new `events()` call. Use one iterator at a time: an iterator holds the events from its first step until it ends or is closed (`aclose()`, or leaving a `contextlib.aclosing()` block).

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L153)

``` python
def events(self) -> AsyncGenerator[SwarmEvent, None]
```

choose_answer  
Make this member’s submission the swarm’s answer, in place of the controller’s `final` rule.

The choice is the provisional answer from now on and the final answer when the swarm ends. The result record and the `final` event give the mode `controller`, the member and `reason`. Choose once, from a member that has ended with a submission.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L163)

``` python
def choose_answer(
    self, member: str | MemberHandle, reason: str | None = None
) -> None
```

`member` str \| [MemberHandle](../reference/inspect_swarm.html.md#memberhandle)  
The member, or its name.

`reason` str \| None  
Why the controller chose it, for the record.

### MemberHandle

A member as a controller sees it: its name, role, state and status, never its text.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L65)

``` python
class MemberHandle(Protocol)
```

#### Attributes

`name` str  
The member’s name.

`role` str \| None  
The member’s role, if it has one.

`choose_name` bool  
Whether the member chooses its own name and is not told `name`.

`state` [MemberState](../reference/inspect_swarm.html.md#memberstate)  
The member’s lifecycle state.

`status` str \| None  
How the member ended, once it has: `submitted`, `unsubmitted`, `stopped`, `share`, `member_limit:<type>`, `swarm_cap`, `cancelled` or `errored`.

`submitted` bool  
Whether the member ended with a submission (status `submitted`).

### MemberEnded

A member ended.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L99)

``` python
@dataclass(frozen=True)
class MemberEnded
```

#### Attributes

`member` str  
The member’s name.

`status` str  
How it ended, as `MemberHandle.status`.

`submitted` bool  
Whether it ended with a submission.

### SwarmEvent

An event a controller receives from `SwarmControl.events()`. Later versions add kinds, so test an event’s type before reading its fields.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_control.py#L113)

``` python
SwarmEvent = MemberEnded
```

### MemberInfo

A member as the controller’s `check` and a channel’s `instructions()` see it.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_controller.py#L14)

``` python
@dataclass(frozen=True)
class MemberInfo
```

#### Attributes

`name` str  
The member’s name.

`role` str \| None  
The member’s role, if it has one.

`choose_name` bool  
Whether the member chooses its own name, so that nothing the swarm tells it contains `name`. [swarm()](../reference/inspect_swarm.html.md#swarm) accepts such a member only with the `filesystem` channel or no channel.

### MemberState

A member’s lifecycle: not started, running, or ended.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_record.py#L18)

``` python
MemberState = Literal["pending", "running", "ended"]
```

### StopReason

Why a controller’s `run` returned: a topology reason such as `all_done`, or the controller’s own, recorded as `controller:<reason>`.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_controller/_controller.py#L10)

``` python
StopReason = str
```

### FinalName

How the swarm chooses its answer from the submissions: `first`, the earliest, or `last`, the latest. A tie goes to the member listed first in the roster.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_final.py#L11)

``` python
FinalName = Literal["first", "last"]
```

## Channels

### filesystem

A shared directory, with a convention for using it, stated in each member’s preamble.

By default the channel describes the sample’s sandbox, which the members share: a directory for shared files, a notes file members append to, and a scratch directory for each member.

With `shared_volume=True` it describes members with their own sandboxes (`member(sandbox=...)`) that each mount one shared volume at `shared`: `shared` and the notes file in it are the same directory in every member’s sandbox. There is no scratch directory, since each member’s sandbox is already its own. The swarm cannot check that the volume is mounted in every member’s sandbox; it takes your word for it.

The channel adds no tools and does not create the directories.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/filesystem.py#L49)

``` python
@swarm_channel
def filesystem(
    shared: str = "/shared",
    notes: str = "notes.md",
    scratch: str = _SCRATCH,
    shared_volume: bool = False,
) -> Channel
```

`shared` str  
The directory for files members share. With `shared_volume=True`, an absolute path where the volume is mounted.

`notes` str  
The notes file members read and append to, relative to `shared` (or an absolute path). With `shared_volume=True` it must be inside `shared`.

`scratch` str  
Each member’s own directory. `{member}` is replaced by the member’s name. For a member that chooses its own name, `{member}` is shown as `<your name>`, and when any member chooses, no member is told its directory is unused. Not used with `shared_volume=True`.

`shared_volume` bool  
Whether members have their own sandboxes that each mount one shared volume at `shared`. [swarm()](../reference/inspect_swarm.html.md#swarm) accepts the channel beside per-member sandboxes only with `True`, and refuses `True` when members share the sample’s sandbox.

### shared_memory

A board of files that every member of the swarm, and their subagents, read and write.

Each member gets a `shared_memory` tool with the commands and parameters of inspect_ai’s [memory()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#memory) over files under `/shared_memory`. Every write goes through the swarm’s bus as a record from the member that made it. A view shows a file inside a fence that names its last writer and version, and lists of changed files name them by ids such as `f-3`, which `view` takes as its `file_id`. A loop must view a file in its current version before it overwrites it with `create`, inserts into it, renames it or deletes it. `append` needs no view: it adds text on a new line at the end of a file, creating the file if it does not exist. `changes` returns only the list of files other agents changed since the loop last viewed them, which ends every result. That list names at most 10 files at the end of a result, and every file in `changes`.

A fence is a label for the reader, not a protection: a member can write text that looks like a fence, another member’s file or a status line.

The board lives for one run of the swarm. A result longer than the tool output limit is truncated in the middle, as Inspect truncates any tool’s result.

Each member, and each of its subagents, also gets `swarm_wait`, which blocks until a file it watches has a change it has not viewed, another member ends, every member is waiting, the swarm stops, or a timeout passes. A wait is wall-clock time inside a tool call: it counts towards the member’s time limit, the swarm’s time cap, and the sample’s time and working limits. It makes no model call, so it adds no tokens, cost or steps.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/shared_memory.py#L107)

``` python
@swarm_channel
def shared_memory(
    max_files: int = 10_000,
    max_file_bytes: int = 1_000_000,
    max_file_lines: int = 100_000,
    max_board_bytes: int = 100_000_000,
    rate: tuple[int, float] | list[int | float] | None = (60, 60.0),
    max_wait: int = 600,
) -> Channel
```

`max_files` int  
The most files on the board, at least 1.

`max_file_bytes` int  
The most UTF-8 bytes in one file, at least 1.

`max_file_lines` int  
The most lines in one file, at least 1.

`max_board_bytes` int  
The most UTF-8 bytes in all files together, at least `max_file_bytes`.

`rate` tuple\[int, float\] \| list\[int \| float\] \| None  
The most writes one member may make, all its loops together, in a sliding window of seconds, as `(count, seconds)`; `None` for no limit.

`max_wait` int  
The longest `swarm_wait` in seconds, at least 1, and its default timeout.

### messages

Direct messages between the members of the swarm.

Each member’s own loop gets three tools. `send_message` sends a message to a member, to several, or to `"all"`, every other running member, and returns its id, such as `m-3`. `read_messages` returns the member’s unread messages, oldest first, each inside a `<peer_message>` fence that names its id, sender and recipients, and marks them read. `list_members` lists the roster with each member’s role, state and, once it has ended, its status. While a member has unread messages, the results of `send_message`, `list_members`, `shared_memory` and `swarm_wait` in its own loop end with a line that counts them; `list_members` also says when there are none. The tools of other channels carry no count. Nested loops, such as a [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent) member’s subagents, get none of these tools, so a member’s inbox has one reader.

Every send goes through the swarm’s bus as a record from the member that made it. A send is refused when an address is not a running member other than the sender, when the message is empty or longer than `max_bytes`, when a recipient already has `max_unread` unread messages, when the sender is over its `rate`, or, with `dedupe`, when a recipient still has the same text from the same sender unread. A refused send reaches no recipient.

One read always returns at least the oldest unread message whole: the read is bounded by the largest fence the roster allows plus `max_bytes`, and Inspect’s tool output limit does not apply to it.

A fence is a label for the reader, not a protection: a member can write text that looks like a fence, another member’s message or a status line.

The member’s own loop also gets `swarm_wait`, which returns while it has unread messages sent with `wake=True` (the default), and otherwise blocks as [shared_memory()](../reference/inspect_swarm.html.md#shared_memory)’s does, up to `max_wait` seconds. A wait is wall-clock time inside a tool call: it counts towards the member’s time limit, the swarm’s time cap, and the sample’s time and working limits.

The channel needs member names that start with a lowercase letter, followed by at most 47 lowercase letters, digits, `_` or `-`, other than `all` and `swarm`. Bridged members cannot use it yet.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/messages.py#L108)

``` python
@swarm_channel
def messages(
    max_bytes: int = 8_000,
    max_unread: int = 50,
    rate: tuple[int, float] | list[int | float] | None = (20, 60.0),
    dedupe: bool = True,
    max_wait: int = 120,
) -> Channel
```

`max_bytes` int  
The most UTF-8 bytes in one message, at least 1.

`max_unread` int  
The most unread messages one member may have, at least 1. A send that would pass it is refused.

`rate` tuple\[int, float\] \| list\[int \| float\] \| None  
The most messages one member may send in a sliding window of seconds, as `(count, seconds)`; `None` for no limit. A message to several members counts once.

`dedupe` bool  
Whether to refuse a message whose text a recipient still has unread from the same sender.

`max_wait` int  
The channel’s longest `swarm_wait` in seconds, at least 1. With other channels that wake waits, the longest of theirs applies.

### swarm_tools

The tools of the swarm’s channels, for a member’s agent.

Pass it in the member’s tools, `react(tools=[bash(), swarm_tools()])`. The member’s own loop gets the tools of every channel of the swarm that has them; its nested loops, such as a [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent) member’s subagents, get only those of channels whose `reach` is `"member"`. A loop also gets `swarm_wait`, which waits for other members’ work, when one of the channels it reaches wakes waits, as [shared_memory()](../reference/inspect_swarm.html.md#shared_memory) does. The member’s own loop is the first in which the tools are resolved, which for [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react) and [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent) members is their top-level loop. Each tool acts as the member, whichever loop calls it.

The same member definition serves swarms with different channels, and one with none. Outside a swarm it gives no tools.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_bus/_tools.py#L42)

``` python
def swarm_tools() -> ToolSource
```

### swarm_channel

Decorator for registering swarm channels.

The factory’s arguments are recorded with the channel, so they appear in the log and the channel can be rebuilt from it. A factory in a package is registered as `<package>/<name>`.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L224)

``` python
def swarm_channel(
    func: Callable[P, Channel] | None = None, /, *, name: str | None = None
) -> Callable[P, Channel] | Callable[[Callable[P, Channel]], Callable[P, Channel]]
```

`func` Callable\[P, [Channel](../reference/inspect_swarm.html.md#channel)\] \| None  
A function returning a [Channel](../reference/inspect_swarm.html.md#channel).

`name` str \| None  
The registered name, in place of the function’s name.

### Channel

A sanctioned way for members to share information.

Subclass it and override `instructions()`, and return an instance from a factory decorated with `@swarm_channel`. A [Channel](../reference/inspect_swarm.html.md#channel) holds configuration only: one object serves every sample.

A channel with tools and records also sets `name` and `prefix` and overrides `open()`, which gives each run of the swarm its own [ChannelState](../reference/inspect_swarm.html.md#channelstate). A channel that only adds instructions, such as [filesystem()](../reference/inspect_swarm.html.md#filesystem), leaves them unset.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L27)

``` python
class Channel
```

#### Attributes

`name` str \| None  
The name of the channel’s records (`Record.channel`), or `None` for a channel with no records. No two channels of a swarm may share it.

`prefix` str \| None  
The prefix of the channel’s record ids (`"w"` gives `w-1`), set exactly when `name` is. No two channels of a swarm may share it.

`reach` Literal\['loop', 'member'\]  
Which of a member’s agent loops get the channel’s tools from [swarm_tools()](../reference/inspect_swarm.html.md#swarm_tools). `"loop"` (the default): only the member’s own loop, the first in which it resolves [swarm_tools()](../reference/inspect_swarm.html.md#swarm_tools). `"member"`: also any nested loop the member’s agent gives [swarm_tools()](../reference/inspect_swarm.html.md#swarm_tools), such as a [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent) member’s [general()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#general) subagents, which then act as the member. Which nested loops get [swarm_tools()](../reference/inspect_swarm.html.md#swarm_tools) is the agent’s choice; `reach` filters the channel tools they get and which channels can wake their `swarm_wait`. A channel whose reads consume something for the member, such as an inbox that marks messages read, keeps `"loop"` so that it has one reader.

`storm` StormControls  
The storm controls the bus applies to the channel’s records. By default there are none.

`max_wait` int \| None  
This channel’s contribution to the longest `swarm_wait`, in seconds, or `None` (the default) for a channel whose records wake no waits. A channel that sets it opens a state that implements [WaitingState](../reference/inspect_swarm.html.md#waitingstate). The bus’s effective maximum is the largest `max_wait` of the run’s open channels that set it.

#### Methods

check  
Check the roster when [swarm()](../reference/inspect_swarm.html.md#swarm) is called.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L50)

``` python
def check(self, roster: Sequence[MemberInfo]) -> None
```

`roster` Sequence\[[MemberInfo](../reference/inspect_swarm.html.md#memberinfo)\]  
The swarm’s members.

open  
Open the channel’s state for one run of the swarm, before any member starts.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L61)

``` python
def open(self, run: "ChannelRun") -> "ChannelState | None"
```

`run` [ChannelRun](../reference/inspect_swarm.html.md#channelrun)  
The run’s bus and roster.

instructions  
Text for the member’s preamble.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L72)

``` python
def instructions(self, member: MemberInfo) -> str | None
```

`member` [MemberInfo](../reference/inspect_swarm.html.md#memberinfo)  
The member the text is for.

### ChannelRun

What `Channel.open()` gets for one run of the swarm.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L84)

``` python
@dataclass(frozen=True)
class ChannelRun
```

#### Attributes

`bus` Bus  
The run’s bus, which the channel’s tools deliver records through.

`roster` Sequence\[[MemberInfo](../reference/inspect_swarm.html.md#memberinfo)\]  
The swarm’s members.

### ChannelState

A channel’s state in one run of the swarm, which only the bus changes.

The bus calls `validate()`, `admit()` and `apply()` with no `await` between them, so `admit()` sees the state that `apply()` changes.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L95)

``` python
class ChannelState(Protocol)
```

#### Methods

tools  
The channel’s tools, which deliver records through the run’s bus.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L101)

``` python
def tools(self) -> list[Tool]
```

validate  
Check a record’s payload and operation data, before the record gets an id.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L105)

``` python
def validate(self, record: Record) -> None
```

`record` [Record](../reference/inspect_swarm.html.md#record)  
The record.

admit  
Check a record against the state as it is now, after the storm controls.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L116)

``` python
def admit(self, record: Record) -> None
```

`record` [Record](../reference/inspect_swarm.html.md#record)  
The record.

apply  
Apply an admitted record to the state. It cannot fail.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L127)

``` python
def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]
```

`record` [Record](../reference/inspect_swarm.html.md#record)  
The record.

### WaitingState

The state of a channel whose records wake waits (one that sets `Channel.max_wait`).

The bus calls these methods with no `await` around them. None of them marks anything: a loop acknowledges what a wait names by reading it with the channel’s own tool.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L173)

``` python
class WaitingState(ChannelState, Protocol)
```

#### Methods

tools  
The channel’s tools, which deliver records through the run’s bus.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L101)

``` python
def tools(self) -> list[Tool]
```

validate  
Check a record’s payload and operation data, before the record gets an id.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L105)

``` python
def validate(self, record: Record) -> None
```

`record` [Record](../reference/inspect_swarm.html.md#record)  
The record.

admit  
Check a record against the state as it is now, after the storm controls.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L116)

``` python
def admit(self, record: Record) -> None
```

`record` [Record](../reference/inspect_swarm.html.md#record)  
The record.

apply  
Apply an admitted record to the state. It cannot fail.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L127)

``` python
def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]
```

`record` [Record](../reference/inspect_swarm.html.md#record)  
The record.

wait_check  
Check a wait’s filter when the wait is called.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L179)

``` python
def wait_check(self, spec: WaitSpec) -> None
```

`spec` [WaitSpec](../reference/inspect_swarm.html.md#waitspec)  
The wait.

wakes  
Whether a record the channel has just applied made something reportable to a wait.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L190)

``` python
def wakes(self, spec: WaitSpec, record: Record) -> bool
```

`spec` [WaitSpec](../reference/inspect_swarm.html.md#waitspec)  
The wait.

`record` [Record](../reference/inspect_swarm.html.md#record)  
The applied record, which has `wake` set.

news  
What is reportable to a wait now.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L202)

``` python
def news(self, spec: WaitSpec) -> WaitNews | None
```

`spec` [WaitSpec](../reference/inspect_swarm.html.md#waitspec)  
The wait.

### WaitSpec

One call of `swarm_wait`, as the bus asks a [WaitingState](../reference/inspect_swarm.html.md#waitingstate) about it.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L139)

``` python
@dataclass(frozen=True)
class WaitSpec
```

#### Attributes

`id` str  
The wait’s id (`"x-4"`), numbered per run.

`member` str  
The waiting member’s name.

`loop` str \| None  
The agent span of the loop that waits.

`own` bool  
Whether that loop is the member’s own, not one of its subagents’.

`path` str \| None  
The shared memory’s path filter as the model gave it, or `None`. Later channels add filters of their own.

`file_id` str \| None  
The shared memory’s file id filter as the model gave it, or `None`. With neither filter, the wait watches the whole board.

### WaitNews

What a channel has for a waiting loop to look at.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_channel/_channel.py#L162)

``` python
@dataclass(frozen=True)
class WaitNews
```

#### Attributes

`lines` list\[str\]  
The channel’s lines for the wait’s result. Fixed templates with ids and member names, never content.

`files` list\[dict\[str, JsonValue\]\]  
What `lines` name, for the `waited` evidence.

### Record

One communication through the bus: a message, a write, a claim.

A channel’s tool hands the bus a `Draft`; the bus makes the record, binding the sender and resolving the addresses, and gives it to the channel.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_bus/_record.py#L20)

``` python
@dataclass(frozen=True)
class Record
```

#### Attributes

`id` str  
The record’s id, the channel’s prefix and a per-channel number (`"w-14"`). Every attempt that passes the bus’s first checks gets one, refused or not, so the ids of delivered records can skip numbers; `seq` in the `delivered` evidence numbers what the run delivered, across its channels.

`channel` str  
The name of the channel the record is for.

`kind` str  
What the record does, in the channel’s terms (`"message"`, `"create"`).

`sender` str  
The sending member’s name, bound by the runtime, never taken from model output.

`to` tuple\[str, ...\]  
The addresses as written. `()` for an unaddressed record, such as a write to shared state, which reaches nobody directly.

`recipients` tuple\[str, ...\]  
The member names the addresses resolved to.

`routed_out` tuple\[str, ...\]  
Resolved names the channel’s routes dropped.

`payload` str  
The text the record carries.

`op` Mapping\[str, FrozenJson\] \| None  
The channel’s operation data, if any, as JSON that cannot be changed: objects are read-only mappings and arrays are tuples. A record is shared by the channel and the sender’s tool, so neither can change what the other sees.

`wake` bool  
Whether the record can end a member’s wait (`swarm_wait`).

`reply_to` str \| None  
The id of a record this one answers.

`sender_span_id` str \| None  
The sender’s agent span.

`tool_span_id` str \| None  
The tool span the call ran in.

## Results

### swarm_result

Read the swarm’s result record.

The swarm writes its record to the transcript once, when it ends, however it ends, and its id to the store it ran with. This returns the record the store of `source` names: the last swarm to end with that store, which is the outermost of nested swarms. A forked branch or a subtask has its own store, and so its own record.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_record.py#L187)

``` python
def swarm_result(source: TaskState | EvalSample) -> SwarmResultRecord | None
```

`source` [TaskState](https://inspect.aisi.org.uk/reference/inspect_ai.solver.html#taskstate) \| [EvalSample](https://inspect.aisi.org.uk/reference/inspect_ai.log.html#evalsample)  
The [TaskState](https://inspect.aisi.org.uk/reference/inspect_ai.solver.html#taskstate) of the sample being run or scored, as a scorer has it in final scoring and when a log is re-scored, or as a solver has it after the swarm has returned; or a sample read from a log, with its events and attachments.

### SwarmResultRecord

What the swarm did in a sample: each member’s result and the final answer, written to the transcript when the swarm ends and read with [swarm_result()](../reference/inspect_swarm.html.md#swarm_result).

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_record.py#L82)

``` python
class SwarmResultRecord(BaseModel)
```

#### Attributes

`version` Literal\[1\]  
The record’s format version.

`members` list\[[MemberResult](../reference/inspect_swarm.html.md#memberresult)\]  
The members, in roster order.

`final` [FinalRecord](../reference/inspect_swarm.html.md#finalrecord)  
The final answer.

### MemberResult

One member in the swarm’s result record.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_record.py#L29)

``` python
class MemberResult(BaseModel)
```

#### Attributes

`name` str  
The member’s roster name.

`role` str \| None  
The member’s role, if it has one.

`choose_name` bool  
Whether the member chose its own name, and was not told `name`. The name it chose is in its transcript, not here.

`state` [MemberState](../reference/inspect_swarm.html.md#memberstate)  
The member’s lifecycle state.

`status` str \| None  
How the member ended, once it has; `None` before.

`submitted` bool  
Whether the member ended with a submission (status `submitted`).

`time` float \| None  
Swarm time (seconds from the swarm’s start) when the member ended; `None` until then.

`sandbox` str \| None  
The member’s own sandbox environment, by name; `None` when it shares the sample’s.

`gate_checks` int  
How many of the member’s own submissions were checked in its loop, as [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react) checks them with `attempts`: intermediate scores from the task’s first scorer, made in the member’s own agent span and not a subagent’s. Set when the member ends.

`gate_passes` int  
How many of those checks passed: a score of 1.0 under Inspect’s default `value_to_float()`, which is [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react)’s default `score_value`. Passes assume that default converter, so a member with its own `score_value` can record passes that differ from its own gate. Set when the member ends.

`first_pass_time` float \| None  
Swarm time of the member’s first passing check; `None` without one. A pass assumes the default `value_to_float()` converter, as for `gate_passes`.

### FinalRecord

The swarm’s final answer in its result record.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_record.py#L66)

``` python
class FinalRecord(BaseModel)
```

#### Attributes

`mode` str \| None  
The mode that chose the answer: `first` or `last` (the controller’s `final` rule), or `controller` (the controller’s `SwarmControl.choose_answer()`). `None` when there is no answer.

`member` str \| None  
The member whose output is the answer. `None` when there is no answer.

`reason` str \| None  
Why there is no answer: `no_submissions`. Under the mode `controller`, the reason the controller gave for its choice. Otherwise `None`.

`provisional` bool  
`True` when the swarm ended without finalising its answer: a limit, error or cancellation ended it first, or the answer did not fit the sample’s message limit.

## Budget view

### swarm_resources

A tool that reports the swarm’s budget to the member that calls it.

It shows the caps that will stop the swarm, how much all members have used, the member’s own share when the budget is split, and the time cap. Use it in place of a tool that reads [sample_limits()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#sample_limits), which shows the sample’s limits rather than the swarm’s caps.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_tools.py#L8)

``` python
@tool
def swarm_resources() -> Tool
```

### swarm_budget

The budget of the swarm the caller runs in.

Reports the swarm’s cap per kind and how much of it the whole swarm has used, and, when the budget is split and the caller is a member, the member’s share and its usage. The caps are the ones that stop the swarm, which [sample_limits()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#sample_limits) does not show: the swarm holds back part of each sample limit.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_budget.py#L649)

``` python
def swarm_budget() -> SwarmBudget
```

### SwarmBudget

The swarm’s budget as a member sees it, from [swarm_budget()](../reference/inspect_swarm.html.md#swarm_budget).

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_budget.py#L612)

``` python
@dataclass(frozen=True)
class SwarmBudget
```

#### Attributes

`split` Literal\['pool', 'equal'\] \| tuple\[float, ...\]  
How the cost and token caps are divided between members, as `Budget.split`.

`cost` [BudgetUsage](../reference/inspect_swarm.html.md#budgetusage) \| None  
The cost cap in dollars, or `None` when the swarm has none.

`tokens` [BudgetUsage](../reference/inspect_swarm.html.md#budgetusage) \| None  
The token cap, or `None` when the swarm has none.

`token_type` str  
The tokens the token cap meters: `all`, or a metering type such as `output`.

`time` [BudgetUsage](../reference/inspect_swarm.html.md#budgetusage) \| None  
The time cap, and the seconds since the swarm started, or `None` when the swarm has none.

### BudgetUsage

One capped kind of the swarm’s budget, and what has been used of it.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/83b552b7d0692d944b19bbce6d5e0514fe41eb69/src/inspect_swarm/_budget.py#L595)

``` python
@dataclass(frozen=True)
class BudgetUsage
```

#### Attributes

`cap` float  
The swarm’s cap: dollars, tokens or seconds.

`used` float  
What the whole swarm has used of the cap so far.

`share` float \| None  
The member’s own share of the cap when the budget is split, else `None`. Time is never split.

`share_used` float \| None  
What the member has used of its share, when it has one.
