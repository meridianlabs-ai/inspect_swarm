# Inspect Swarm: the `swarm()` API

Status: proposed, 2026-10-08. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

A deeper dive on one topic of [swarm.md](swarm.md), the high-level design: the public shape of `swarm()`. It replaces swarm.md's illustrative [Entry point](swarm.md#entry-point) sketch, where `topology=` and `final=` are strings and `budget=` is a limit object, with an API whose arguments mirror the four components (members, channels, controller, observer). Controllers and channels become Inspect registry objects (`@controller`, `@channel`), selectable by name, with their parameters in the log. It also renames the component swarm.md calls the *substrate* to **channels**, and applies that name in swarm.md and the overview.

It respects the decisions recorded in swarm.md (Ransom, 2026-10-07): Python 3.11+, inspect_ai internals may be used, inspect_ai limits stay soft, M1 then M2 then the rest in any order, members share the sample sandbox by default, monitoring aligns with inspect_sentinel, red-team features are optional, peer messages are model output delivered as tool output with distinct provenance, and the transcript representation is open. Four sibling deep dives run in parallel: [limits](https://github.com/meridianlabs-ai/inspect_swarm/pull/5), [scoring](https://github.com/meridianlabs-ai/inspect_swarm/pull/2), [inter-agent communication](https://github.com/meridianlabs-ai/inspect_swarm/pull/3) and [ORBIT on inspect_swarm](https://github.com/meridianlabs-ai/inspect_swarm/pull/4). This document says where their parameters go in the API ([table](#where-the-sibling-designs-parameters-go)) and does not design their semantics. All four are unmerged proposals; where this document cites one, it cites the PR's head on 2026-10-08.

Code references are to inspect_ai `main` at `fccfb298` (2026-10-07, the version this repository's lockfile installs), inspect_ai `feature/sentinel` at `eff42b6f`, inspect_sentinel at `c8cd71d` and inspect_scout at `d746d2a3` (both 2026-10-07). Paths are relative to each repository's root.

## Why

Ransom's direction (2026-10-08): "it might be better to have the API shape better match the Components. For example by having a new registry object (`@controller`) that is used instead of just topology and final as scalars. Similar for other components - although substrate is potentially a problematic name - maybe there is something better?"

swarm.md's sketch is:

```python
swarm(
    members=member(deepagent(...), count=4),
    topology="leaderless",
    channels=["filesystem"],
    budget=cost_limit(40.0),
    final="verify",
)
```

Four problems follow from it.

- **No extension point for how a swarm runs.** `topology=` picks from a closed list. A research arm that runs differently (a cheap member first and the rest only if it fails, members started in waves, ORBIT's scheduled rounds) has nowhere to go. The ORBIT deep dive already has to invent a separate `ActivationPolicy` protocol for this (PR #4, "P4. The turn seam and activation policies"). The same holds for channels: `channels=` is a list of strings with no way to add one.
- **The component boundaries are not in the call.** The final-answer default depends on the topology (`reporter` for coordinators, the verify/vote/first chain for leaderless swarms; swarm.md, [Final answer](swarm.md#controller-topology-termination-final-answer)), yet `final=` is a separate scalar that has to be validated against `topology=`.
- **Arguments do not reach the log faithfully.** Inspect logs an agent's arguments by its own rules ([How arguments reach the log](#how-arguments-reach-the-log)). Run against the installed inspect_ai, the sketch's `budget=cost_limit(40.0)` is logged as the string `"_CostLimit"`, and inside a list as `null`. A frozen dataclass such as the limits deep dive's `Budget` is logged as its type name, `"Budget"`. Two `eval_set` arms that differ only in such an argument then get the same task identifier, and `eval_set` refuses them: "The task 'arm' is not distinct." (spike below). Sweepability is a goal of swarm.md ("agent count, topology, communication channels and budget split ordinary, sweepable task parameters"), so this matters.
- **"Substrate" is obscure, and swarm.md also uses it in its ordinary sense** ("aim to be a substrate it could run on", "eval-oriented swarm substrates"), so the component's name and the generic word collide.

The sibling deep dives are each adding parameters to `swarm()`: `budget=Budget(...)`, `result=`, a `final=` chain, `messages(...)` in `channels=`, `policy=`, `routes=`, `member(delivery=, bridged=, input=, exposure=)`, and an activation-policy protocol. Without one shape for the call, those land as a flat list of keyword arguments with no rule for which component each belongs to or how it is logged.

## Goals and non-goals

### Goals

- `swarm()`'s arguments mirror the components: `members=`, `channels=`, `controller=`. The observer is not an argument ([why](#observer-no-argument)).
- The components users extend are Inspect registry objects, with Inspect's conventions: decorated factories, package-qualified registry names, creation parameters captured into the log, lookup by name, and an `inspect_ai` entry point. Those are controllers (`@controller`) and channels (`@channel`).
- The common case stays one line, and every argument works as a task parameter (`-T`), in a Python `eval_set` sweep and in the log: names resolve to registry objects, and every argument serialises faithfully and can be rebuilt from the log.
- One place for each sibling design's parameters.
- A better name than "substrate", applied in swarm.md and the overview.

### Non-goals

- The semantics of limits, scoring, communication and ORBIT's scheduled mode. Their deep dives own them; this document gives their parameters a place and states the one rule they must meet (serialisation).
- The coordinator topologies and persistent members (later work in swarm.md's plan). This document shows how a coordinator plugs in, not how idle and wake work.
- An observer registry type. The observer is fixed so that arms stay comparable ([Observer](#observer-no-argument)).
- CLI conveniences beyond what Inspect already parses: grid sweeps, or a dict form for controller parameters on the command line ([Not this design](#not-this-design)).
- Changing how inspect_ai serialises arguments in general ([Not this design](#not-this-design)).

## Current behaviour

### The registry

- **Registry types are a closed `Literal`.** `RegistryType` lists every type (`src/inspect_ai/_util/registry.py:41-60`), and `RegistryInfo.type` is validated against it by pydantic (`:72-80`). Constructing `RegistryInfo(type="controller", name="x")` against the installed inspect_ai raises `ValidationError`.
- **Satellite packages add their types to inspect_ai.** Scout's `loader`, `scanner` and `scanjob` came in inspect_ai#2556 (2025-10-04). `validation_predicate` came in inspect_ai#4359 (2026-07-21), which changed one line of `registry.py` and added a test and a CHANGELOG line. inspect_sentinel's `monitor` and `protocol` are on inspect_ai's `feature/sentinel` branch, not yet on `main`.
- **The literal is not in the log schema.** inspect_ai's generated OpenAPI schema (`src/inspect_ai/_view/inspect-openapi.json`) does not contain the registry type names; `scanjob` occurs nowhere in it. So adding a type changes no log format and no generated TypeScript type.
- **The decorator pattern.** `@agent` (`src/inspect_ai/agent/_agent.py:162-212`) and sentinel's decorators (`inspect_sentinel: src/inspect_sentinel/_decorators.py:230-304`) do the same thing:
  - compute the registry name with `registry_name()`, which prefixes the installed package's name, so a factory in inspect_swarm is `inspect_swarm/<name>` (`registry.py:252-259`);
  - wrap the factory; the wrapper calls it, checks what it returned, and tags the returned object with `registry_tag(factory, instance, info, *args, **kwargs)`, which binds the call's arguments to the factory's signature and stores them as the object's registry params (`:151-183`);
  - register the wrapper with `registry_add()` (`:127-148`).
- **Lookup by name.** `registry_lookup(type, name)` tries the name as given, then `inspect_ai/<name>` for a bare name (`registry.py:276-287`). For a `package/name` it loads that package's `inspect_ai` entry point first (`:289-297`). inspect_sentinel declares one (`inspect_sentinel: pyproject.toml`, `[project.entry-points.inspect_ai]`); inspect_swarm does not yet.
- **`registry_create()`** instantiates a registered callable only when its return annotation's name, lower-cased, equals the registry type (`registry.py:436-449`). So a `controller` factory must be annotated `-> Controller`.

### How arguments reach the log

`registry_tag()` records an object's arguments with `extract_named_params()` (`registry.py:186-249`). Its rules, checked by spikes against the installed inspect_ai:

| Argument | Logged as |
|---|---|
| `str`, `int`, `float`, `bool`, `None` | itself |
| A registry object with params, at top level or inside lists and dicts | `{"type", "name", "params"}`, recursively (`registry_value()`, `:660-686`): `react(prompt="hi")` became `{"type": "agent", "name": "react", "params": {"prompt": "hi"}}` |
| A pydantic model | its fields (`to_jsonable_python`): `{"cost": 10.0}` |
| An object with `_repr_params_()`, at top level only | what that method returns. Inspect's compaction strategies use this (`src/inspect_ai/model/_compaction/types.py:37`) |
| Any other object at top level | its `name` attribute, else its type's name: `cost_limit(40.0)` → `"_CostLimit"`; a frozen dataclass `Budget(10.0)` → `"Budget"`; deepagent's `Subagent` → its `name` |
| A dataclass inside a list | a dict, but its callables become their `__name__`: a member record holding a `react()` agent → `{"agent": "execute", ...}` |
| A non-serialisable object inside a list | `null`: `[cost_limit(40.0)]` → `[null]` |

Where that log goes:

- **The plan.** Each solver step's arguments are logged as `params` (with defaults) and `params_passed` (`src/inspect_ai/_eval/task/log.py:1233-1246`). `as_solver()` forwards the agent's registry params to the solver it creates (`src/inspect_ai/agent/_as_solver.py:85-91`), so a `swarm()` used as a solver logs its own arguments.
- **Eval-set identity.** `task_identifier()` hashes the plan's `params_passed` together with the task arguments (`src/inspect_ai/_eval/evalset.py:2227-2241`), and `eval_set()` refuses two tasks with the same identifier (`:2046-2052`). A spike ran `eval_set` with mockllm over two tasks named `arm` whose solvers were an `@agent` given `Budget(10.0)` and `Budget(20.0)` (a frozen dataclass). It failed with "The task 'arm' is not distinct." The same agent given pydantic models logs `{"budget": {"cost": 10.0}}` and `{"cost": 20.0}`, which differ.
- **Replay.** `create_registry_object()` rebuilds an object from logged arguments, passing them through `registry_kwargs()`, which turns each nested `{"type", "name", "params"}` back into an object (`registry.py:452-467`, `:689-704`). It recognises such a dict only when its `type` is in `RegistryType` (`is_registry_dict()`, `:647-657`). This is the path `--solver <name> -S ...` takes for an `@agent` (`src/inspect_ai/_eval/loader.py:676-684`).
- **The command line.** `-T` and `-S` values are parsed as YAML, and a plain value with commas becomes a list (`src/inspect_ai/_util/config.py:23-48`). So `-T channels=filesystem,messages` is a list, but `-T channels=filesystem` is the string `"filesystem"`.

### Registry objects are shared by concurrent samples

A task's plan is built once, and every sample runs the same objects (`src/inspect_ai/_eval/task/run.py:2831`, `await plan(state, generate)`). So an agent, solver, controller or channel object is shared by all concurrently running samples, and anything per-sample must live in the call, not on the object. Inspect's agents already meet this, which is also why `member(agent, count=4)` may run one agent object four times at once.

### Precedent for member-like records

deepagent's `Subagent` is a plain dataclass built by `subagent()` (`src/inspect_ai/agent/_deepagent/subagent.py:13-53`). It is configuration, not a registry object, and it is logged by its `name` only (table above).

## Design

### The shape of the call

| Component | Argument | Value | Registry type |
|---|---|---|---|
| Members | `members=` | `member(agent, ...)` records; a bare `Agent` is one member | none: the member's `Agent` is already an `@agent` registry object |
| Channels (was Substrate) | `channels=` | channel names or `@channel` objects | `channel` (new) |
| Controller | `controller=` | a controller name or a `@controller` object | `controller` (new) |
| Observer | none | always on | none |

Two arguments are not components: `budget=` (the swarm-wide cap; the limits deep dive's `Budget`) and `result=` (the task's result contract; the scoring deep dive's `ResultSpec`). M2 adds `policy=` (the communication deep dive's bus hook, which belongs to the observer; [Observer](#observer-no-argument)).

```python
# src/inspect_swarm/_swarm.py
@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],
    *,
    controller: str | Controller = "leaderless",
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),
    budget: Budget = Budget(),          # limits deep dive
    result: ResultSpec | None = None,   # scoring deep dive
) -> Agent:
    """A swarm of agents working on one sample.

    Args:
        members: The roster. Each `Member` may stand for several members (`count`). A bare `Agent` is `member(agent)`.
        controller: How the swarm runs: topology, start and wake, termination and the final answer. A name resolves to a registered controller with its default parameters.
        channels: How members share information. `[]` means none: members get no channel instructions or tools, though a shared sandbox is still shared.
        budget: The swarm-wide cap.
        result: The task's result contract.
    """
```

The common case is one line, `swarm(members=member(deepagent(...), count=4))`, and means:

```python
swarm(
    members=member(deepagent(...), count=4),
    controller="leaderless",      # = leaderless(), whose final chain is scoring's default
    channels=["filesystem"],      # = [filesystem()]
    budget=Budget(),              # caps derived from the sample's limits
)
```

`swarm()` validates at construction and raises `ValueError` (or `TypeError` for wrong types), naming the argument:

- every member's agent is a registered agent (`is_registry_object(agent, "agent")`), as `as_solver()` already requires (`_as_solver.py:45-48`), so the log can name it;
- member names, after `count` expansion, are unique;
- each channel name appears at most once;
- names resolve ([Resolution](#resolving-names));
- the controller's `check` accepts the roster, and the controller's `final` is valid for `result` (the scoring deep dive's validation rules, applied to the controller's chain).

### Controller and runtime

swarm.md uses "the controller" both for the policy that decides how the swarm runs and for the machinery that enforces the swarm's invariants. This design separates them:

- **The controller** is a registry object chosen per arm. It decides which members start, when, with what input; when the swarm is done; and which final-answer chain applies.
- **The runtime** is inspect_swarm's fixed code in `swarm()`. It owns member tasks, member spans and limits, the budget nodes, recovery from the swarm's own cap, drain, verification of submissions and the provisional answer, finalisation, the ledger, evidence and metrics.

A controller cannot break an invariant because it never holds what the runtime owns: it starts members through a handle, never runs them itself; it returns a stop reason, and the runtime then drains; it names a final chain, and the runtime runs it after the drain. Where the sibling deep dives write "the controller" for drain, exhaustion, verification or finalisation, they mean the runtime.

### Members

Members are data around an `Agent`, which is already a registry object, so they get a record, not a decorator.

```python
# src/inspect_swarm/_member.py
class Member(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    agent: Agent                  # validated as a registered agent; serialised with registry_value()
    name: str | None = None       # default: the agent's unqualified registry name
    role: str | None = None       # data; given to the controller and in the member's preamble
    count: int = 1                # members built from this record: name-1 ... name-k when count > 1
    limits: list[Limit] = []      # the member's own limits (limits deep dive); serialised by kind and value
    # fields the sibling deep dives add: delivery (communication), bridged (limits),
    # input and exposure (ORBIT)

def member(
    agent: Agent,
    *,
    name: str | None = None,
    role: str | None = None,
    count: int = 1,
    limits: Sequence[Limit] | None = None,
) -> Member: ...
```

- **Why pydantic.** A pydantic model is logged as its fields, at top level and inside lists ([table](#how-arguments-reach-the-log)). Two field serialisers make it faithful: `agent` is written with `registry_value()`, so it appears as `{"type": "agent", "name": "react", "params": {...}}`; each limit is written as `{"limit": "cost", "value": 2.0}` (with the metering type for tokens, which the limits deep dive reads from the object). A spike with this model logged a member as `{"agent": {"type": "agent", "name": "react", "params": {"prompt": "solve"}}, "name": "w", "count": 4}`.
- **Replay.** Rebuilding the swarm from the log hands `swarm()` a dict whose `agent` is already an `Agent` again (`registry_kwargs()` rebuilt it; the same spike confirmed this). `swarm()` therefore accepts a mapping wherever it accepts a `Member` and validates it with `Member.model_validate()`. A limit dict is rebuilt by a validator that calls the matching Inspect limit factory.
- **Names.** With `count=1` the member is called `name`. With `count=k>1` the members are `name-1` to `name-k`. A name is an identifier the eval author chose; members never choose names (swarm.md, [Delivery](swarm.md#delivery-peer-messages-are-model-output)).
- **Background deepagents** are rejected by `member()` while background ownership is unsupported, as swarm.md's [Members](swarm.md#members) says.

### Controllers

A controller is a `Controller` object returned by a factory decorated with `@controller`, the way a `Task` is returned by an `@task` factory.

```python
# src/inspect_swarm/_controller/_controller.py
StopReason = str
FinalSpec = str | FinalMode | Sequence[str | FinalMode]   # the scoring deep dive's chain

class Controller:
    """How a swarm runs. Holds configuration only: one object serves every sample."""

    def __init__(
        self,
        run: Callable[[SwarmControl], Awaitable[StopReason]],
        *,
        final: FinalSpec | None = None,
        check: Callable[[Sequence[MemberInfo]], None] | None = None,
    ) -> None: ...

@dataclass(frozen=True)
class MemberInfo:
    name: str
    role: str | None

def controller(
    func: Callable[P, Controller] | None = None, *, name: str | None = None
) -> ...: ...   # same overloads as @agent: @controller and @controller(name=...)
```

- **`run`** is called once per sample with a `SwarmControl` and returns the stop reason. Returning is the stop: the runtime cancels members still running, drains, and finalises. Its local variables are per sample; the `Controller` object is shared, so it holds no per-sample state ([Current behaviour](#registry-objects-are-shared-by-concurrent-samples)).
- **`final`** is the final-answer chain the runtime applies after the drain, and for the provisional answer as members end. `None` means the scoring deep dive's default chain for the task's `result` (verify, then vote, then first, from what the task supplies). It is on the controller, not on `swarm()`, because the default and the valid modes depend on the topology: a coordinator defaults to the lead's report, and `reporter` is invalid for a leaderless swarm.
- **`check`** runs at `swarm()` construction with the roster, for errors a factory cannot see alone (a named member that is not in the roster).
- **The decorator** follows `@agent` and sentinel ([The registry](#the-registry)): registry name `registry_name(func, name or func.__name__)`; the wrapper calls the factory, raises `TypeError` unless it returned a `Controller`, and tags it with `registry_tag(func, instance, RegistryInfo(type="controller", name=...), *args, **kwargs)`; `registry_add()` registers the wrapper. `Controller` is a plain class, not a frozen dataclass, because tagging sets attributes on it. The factory is annotated `-> Controller` so `registry_create("controller", ...)` instantiates it.

The handle a controller works through:

```python
MemberStatus = Literal["pending", "running", "done", "limit", "errored", "cancelled"]

class MemberHandle(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def role(self) -> str | None: ...
    @property
    def status(self) -> MemberStatus: ...
    @property
    def submitted(self) -> bool: ...
    @property
    def verdict(self) -> Verdict | None: ...    # scoring's Verdict; None without a verifier or before it ran

class SwarmControl(Protocol):
    @property
    def members(self) -> Sequence[MemberHandle]: ...           # roster order, after count expansion
    def member(self, name: str) -> MemberHandle: ...            # KeyError for an unknown name
    def start(
        self,
        member: str | MemberHandle,
        input: str | list[ChatMessage] | None = None,           # None: the swarm's own input
    ) -> None: ...
    def events(self) -> AsyncIterator[SwarmEvent]: ...

@dataclass(frozen=True)
class MemberEnded:
    member: str
    status: MemberStatus
    submitted: bool
    verdict: Verdict | None

@dataclass(frozen=True)
class MessageDelivered:          # M2: metadata only, never the payload
    record: str                  # the bus record id
    channel: str
    sender: str
    recipients: tuple[str, ...]

SwarmEvent = MemberEnded | MessageDelivered
```

- **`start()`** schedules the member in the runtime's member task group, under `run()` in its own span with its limits, and returns at once. Its input is the given input, or the swarm's input, followed by the member's preamble: its name, role and roster size, and each channel's instructions ([Channels](#channels)). The preamble's wording is M1's prompt work. In M1 a member runs once: `start()` on a member that is not `pending` raises `RuntimeError`. Persistent members, later, add a wake operation.
- **`events()`** yields events in order from swarm start, so none is lost between `start()` and iteration. It ends when no member is running and nothing is queued; a member started later is reported by a new `events()` call. One iterator at a time; a second concurrent call raises `RuntimeError`. M1 has `MemberEnded` only; M2 adds `MessageDelivered` after the bus's delivery step. In M1 a member's submission is what it returns when its agent ends (the scoring deep dive defines what counts as one), so `MemberEnded` carries whether it submitted and the verdict, which the runtime takes as the member ends (scoring deep dive, "When verdicts are taken").
- **No member text.** Handles and events carry statuses, verdicts and names, never a member's output or a message body. A controller therefore cannot pass one member's words into another's input, which would put model output in the user role (swarm.md, [Delivery](swarm.md#delivery-peer-messages-are-model-output)). Members read each other through channels' tools. ORBIT's scheduled mode adds conversation snapshots and trusted interventions to these handles for trusted policies; that is its deep dive's extension, argued there.
- **Errors and stops the runtime owns.** The controller's `run` runs inside the swarm's task group. When the swarm's own cap or time runs out, the runtime cancels `run` and the members, records the runtime's reason (for example `swarm_cap` or `time`), drains and finalises; the limits deep dive defines which reasons exist. An exception from `run`, or a member error that is not the member's own limit, drains and propagates under swarm.md's [exhaustion rules](swarm.md#observer-evidence-accounting-and-metrics). A member's own limit ends that member with status `limit` and an event; the swarm continues.
- **Stop reasons.** The reason `run` returns is recorded as the swarm's stop reason. The built-ins return `all_ended`, `verified` and `lead_ended`, swarm.md's "all submitted" (made exact: a member stopped by its own limit has ended without submitting), "verified answer" and "the root submits". A custom controller's string is recorded as given.
- **Usage.** A controller normally makes no model calls. If one does, its usage is the swarm's, not a member's; which node meters it is the limits deep dive's call, as for verifier calls.

The built-in controllers are `inspect_swarm/leaderless` (M1) and, later, `inspect_swarm/coordinator`. If the ORBIT deep dive's scheduled mode is adopted, its `ActivationPolicy` becomes a controller's `run`, its `SwarmControl` and `MemberHandle` additions (`activate()`, `snapshot()`, `set_output()`, `append()`, `edit()`) are methods on these same handles, and its `round_robin` is a `@controller`. Its own text already says "M1's leaderless controller is the policy that activates every member once".

### Channels

A channel is a `Channel` object returned by a factory decorated with `@channel`. The communication deep dive owns what a channel does; this design gives it a registry identity and the M1 surface.

```python
# src/inspect_swarm/_channel/_channel.py
class Channel:
    """A sanctioned way for members to share information. Holds configuration only: one object serves every sample."""

    def instructions(self, member: MemberInfo) -> str | None:
        """Text added to the member's preamble, or None. M1."""
        return None

    # M2 adds the communication deep dive's operations (tools, validate, apply,
    # notice_lines, quiescent), whose per-sample state the runtime holds per run.

def channel(func=None, *, name: str | None = None): ...   # as @controller, with type "channel"
```

- **Built-ins.** `inspect_swarm/filesystem` (M1) carries the shared-sandbox convention as parameters, for example `filesystem(shared="/shared", notes="notes.md", scratch="/scratch/{member}")`; its `instructions()` states them for each member. `inspect_swarm/messages` (M2) is the communication deep dive's `messages(delivery=, max_bytes=, ...)`, now a `@channel` factory. Later channels (notes, task list, board) are more `@channel` factories.
- **User channels.** In M1 a user channel can only add instructions, which already makes named, sweepable conventions possible: `lockfiles(dir="/shared/locks")`, after the C compiler's lock files, against `filesystem()` alone. From M2 a user channel implements the communication deep dive's operations; until that deep dive declares them stable they are experimental.
- **Per-sample state.** `Channel` objects are shared by concurrent samples, so their state cannot live on them. The communication deep dive's `Channel` protocol keeps inboxes and versions on the channel object (`apply()`, `quiescent()`); it must move that state to an object the runtime creates per run (or the sample store, as swarm.md's "store-backed state" has it). That is the one change this design asks of it.
- **No channel, no instructions.** `channels=[]` gives members no channel instructions or swarm tools. The shared sandbox is still shared: what is shared is the sandbox topology's business (swarm.md, [Sandbox topology](swarm.md#sandbox-topology)), not the channel list's.

### Observer: no argument

The observer (evidence, the ledger, metrics, the interception point) is the part of a swarm that must be the same in every arm, or arms cannot be compared. So it has no argument and no decorator. It is extended through Inspect's existing registries instead: monitors and protocols are inspect_sentinel's `@monitor` and `@protocol`; swarm-level scores are `@scorer`s (the scoring deep dive's `member_scores()`); post-hoc labels are Scout `@scanner`s. The communication deep dive's `policy=` hook is the observer's one knob, on `swarm()` from M2, until sentinel replaces it (that deep dive's open question 2).

### Resolving names

A string for `controller=` or in `channels=` is resolved by `resolve(type, name)` in `src/inspect_swarm/_registry.py`:

1. A name with a `/` is looked up as given (`registry_lookup()`, which loads that package's entry point).
2. A bare name is looked up as given (an object registered in the user's own task file is unprefixed), then as `inspect_swarm/<name>`. A user's own `leaderless` therefore shadows the built-in, as inspect_ai's bare-name rule does (`registry.py:276-287`).
3. The object is created with no arguments, by `create_registry_object(type, name, {})`. A factory with a required parameter cannot be named this way, and the `TypeError` says so.
4. An unknown name raises `ValueError` listing the registered names of that type (`registry_find()`).

The resolved registry name is recorded in the swarm's evidence for each sample, so a log shows which object a bare name meant. inspect_swarm declares an `inspect_ai` entry point (`inspect_swarm = "inspect_swarm._entrypoint"`) that imports the built-ins, so `inspect_swarm/...` names resolve without importing the package first, as inspect_sentinel's does.

Both registry types need inspect_ai's `RegistryType` to list them ([The registry](#the-registry)): one inspect_ai PR adds `"controller"` and `"channel"`, as inspect_ai#4359 added `validation_predicate`. That is the only inspect_ai change this API needs, and it is needed before M1 ([open question 2](#open-questions)).

### Every argument serialises

The rule for every argument of `swarm()`, `member()`, and every controller and channel factory: it is a plain value, a registry object, a list or dict of those, or a pydantic model whose fields follow the same rule (with field serialisers for registry objects and limits, as `Member` has). Then the plan step logs the argument faithfully, two arms that differ in it get different eval-set identifiers, and `create_registry_object()` can rebuild it. Replay turns registry objects back into objects but leaves pydantic models as dicts, so every factory that takes a pydantic argument also accepts its dict form and validates it, as `swarm()` does for `Member`.

Under this rule, swarm.md's sketch argument `budget=cost_limit(40.0)` (logged as `"_CostLimit"`) is replaced by the limits deep dive's `Budget`, which must be a frozen pydantic model, not a frozen dataclass (logged as `"Budget"`). The scoring deep dive's `FinalMode` values (for example `synthesize(model=...)`) must be pydantic models too. Its `ResultSpec` holds the task's callables and is logged as its type name, which that deep dive accepts; it is the one exception, and it is safe for eval-set identity because arms of one task share their result contract.

A spike confirmed the whole path with a simulated `controller` registry type (`RegistryInfo.model_construct()` stands in for the literal entry): `swarm(members=[member(react(prompt="solve"), name="w", count=4)], controller=leaderless(final=["verify", "first"]))` logged the following (the names are unprefixed because the spike ran outside a package; in inspect_swarm they are `inspect_swarm/leaderless`, since `registry_log_name()` strips only `inspect_ai/`, `registry.py:521-535`)

```json
{
  "members": [{"agent": {"type": "agent", "name": "react", "params": {"prompt": "solve"}}, "name": "w", "count": 4}],
  "controller": {"type": "controller", "name": "leaderless", "params": {"final": ["verify", "first"]}}
}
```

and `create_registry_object("agent", ...)` on those params called `swarm()` again with a `Controller` object for `controller` and, for the member, a dict whose `agent` was an `Agent`.

### Sweeping

- **Task parameters.** A task exposes the axes it wants, and names resolve: `-T controller=escalate`, `-T channels=filesystem,messages`. A single `-T channels=filesystem` arrives as a string, which `channels=` accepts.
- **Python sweeps.** `eval_set([task(controller=leaderless(final=f)) for f in ...])` and inspect_flow work on solver arguments too, because they are logged faithfully and so give distinct task identifiers.
- **`--solver inspect_swarm/swarm`.** It works through the `@agent` path, but `members=` needs a dict on the command line (`-S 'members={agent: {type: agent, name: react, params: {}}, count: 4}'`). Tasks are the intended surface.

### Where the sibling designs' parameters go

| Deep dive (PR) | Its parameter | In this API |
|---|---|---|
| Limits (#5) | `swarm(budget=Budget(...))` | `swarm(budget=)`, unchanged; `Budget` becomes a frozen pydantic model ([Every argument serialises](#every-argument-serialises)) |
| Limits (#5) | `member(limits=[...], bridged=)` | `member()` fields; limits serialised by kind and value |
| Scoring (#2) | `swarm(final=...)`, a mode or chain | the controller's `final=`: `leaderless(final=...)`; the chain's semantics and validation are unchanged |
| Scoring (#2) | `swarm(result=...)` | `swarm(result=)`, unchanged |
| Scoring (#2) | `reporter` mode, reserved | valid only on controllers with a reporter; `coordinator(lead=...)` defaults to it |
| Scoring (#2) | `baseline(agent, result)` | `swarm(members=member(agent), controller=leaderless(final="first"), channels=[], result=result)` |
| Communication (#3) | `channels=["filesystem", messages(...)]` | the same call; `messages` is a `@channel` factory, `"messages"` resolves to it |
| Communication (#3) | `member(delivery=)` | a `member()` field |
| Communication (#3) | `swarm(policy=)` | `swarm(policy=)` from M2, the observer's knob |
| Communication (#3) | the `Channel` protocol | M2's operations on `Channel`, with per-sample state moved off the shared object |
| Communication (#3) | `swarm_tools()`, `swarm_on_continue()`, `swarm_bridged_tools()` | unchanged: member-side helpers, not `swarm()` arguments |
| ORBIT (#4) | `member(input=, role=, exposure=)` | `member()` fields; `input` is the default for `SwarmControl.start(input=None)` |
| ORBIT (#4) | `ActivationPolicy`, `SwarmControl`, `MemberHandle` | a controller's `run`, and methods added to these handles |
| ORBIT (#4) | `round_robin(max_rounds=)` | a `@controller` |
| ORBIT (#4) | `final="reporter"` by member name | `final=reporter("<member>")`, the scoring deep dive's mode with a member name |
| ORBIT (#4) | `swarm(routes=...)` | `messages(routes=...)`: routes restrict who may address whom, and the bus applies them to that channel's records before the monitor, as the ORBIT deep dive describes |

### Naming the component: Channels

"Substrate" becomes **Channels**: members, channels, controller, observer.

| Name | For | Against |
|---|---|---|
| **Channels** (recommended) | Already the argument (`channels=["filesystem"]`) and the column heading of swarm.md's channel table. ORBIT's vocabulary (channels with readers and writers) and Codex's board. The communication deep dive's `Channel` protocol already uses it. swarm.md already calls the filesystem an implicit channel. | inspect_ai has an unrelated `AgentChannel`, the per-execution queue that delivers notices. The documents must say "agent channel" for that one, as swarm.md's [The agent channel](swarm.md#the-agent-channel) already does. |
| Comms / communication | Plain; matches the communication deep dive's title. | The filesystem, a task list and a board are shared state more than messages; informal as an API word. |
| Shared state | Accurate for the filesystem, notes, tasks and board. | Wrong for direct messages, which are not shared. |
| Commons | Names what members hold in common without implying messaging. | Unfamiliar in eval APIs; one more term to learn. |
| Blackboard | An established multi-agent term (Terrarium's blackboards). | A classic architecture with its own control model; implies one shared store, not addressed messages. |
| Workspace | Familiar. | Already means the sandbox's working directory and git worktrees; suggests the filesystem only. |
| Medium, fabric, network, mesh | Neutral. | Vague, and "mesh" already names a topology (Architecture Matters' `mesh_round_robin`). |
| Substrate (keep) | No churn. | Obscure, and it collides with swarm.md's ordinary use of the word. |

The ordinary-sense uses in swarm.md ("aim to be a substrate it could run on", "eval-oriented swarm substrates", "Make ORBIT the substrate") stay: once the component is called channels, the word no longer means two things.

### Examples

**A leaderless swarm (M1).** The FrontierMath-style arm from swarm.md, with the axes exposed as task parameters:

```python
from inspect_ai import Task, task
from inspect_ai.agent import deepagent
from inspect_ai.tool import bash, python
from inspect_swarm import Budget, member, swarm


@task
def research_math(k: int = 4, cost: float = 40.0, controller: str = "leaderless") -> Task:
    return Task(
        dataset=...,
        solver=swarm(
            members=member(deepagent(tools=[bash(), python()]), name="solver", count=k),
            controller=controller,
            budget=Budget(cost=cost),
            result=research_math_result(),   # the scoring deep dive's answer_result(...)
        ),
        scorer=...,
        sandbox="docker",
    )
```

`inspect eval research_math.py -T k=8 -T cost=80` runs eight members, `solver-1` to `solver-8`, in the sample's sandbox, with the filesystem channel, under a cost cap of 80 (dollars), each started with the sample's input and its preamble; it stops when all have ended, and the final answer is scoring's default chain. The explicit form of the same arm, with a strict verifier-only answer and the swarm stopping at the first verified answer:

```python
swarm(
    members=member(deepagent(tools=[bash(), python()]), name="solver", count=4),
    controller=leaderless(final="verify", stop_on_verified=True),
    channels=[filesystem(notes="notes.md")],
    budget=Budget(cost=40.0),
    result=research_math_result(),
)
```

The built-in it uses:

```python
@controller
def leaderless(final: FinalSpec | None = None, stop_on_verified: bool = False) -> Controller:
    """Start every member with the swarm's input; stop when all have ended."""
    if final is not None and has_reporter(final):   # any `reporter` mode in the chain
        raise ValueError("leaderless: a leaderless swarm has no reporter")

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async for event in swarm.events():
            if stop_on_verified and isinstance(event, MemberEnded) and event.verdict and event.verdict.passed:
                return "verified"
        return "all_ended"

    return Controller(run, final=final)
```

**A coordinator swarm (later: coordinator topologies, on M2's messages).** A lead and three workers; workers start when the lead first messages them, and the lead's submission is the answer.

```python
from inspect_ai.agent import react
from inspect_swarm import coordinator, member, messages, swarm, swarm_on_continue, swarm_tools

lead = react(prompt=LEAD_PROMPT, tools=[bash(), swarm_tools()], on_continue=swarm_on_continue())
worker = react(prompt=WORKER_PROMPT, tools=[bash(), python(), swarm_tools()], on_continue=swarm_on_continue())

swarm(
    members=[member(lead, name="lead", role="coordinator"), member(worker, name="worker", count=3)],
    controller=coordinator(lead="lead"),
    channels=["filesystem", messages(delivery="notify")],
)
```

The controller, as it would be built in the coordinator-topologies work:

```python
@controller
def coordinator(lead: str = "lead", final: FinalSpec | None = None) -> Controller:
    """Start the lead; start each other member when it is first sent a message."""

    async def run(swarm: SwarmControl) -> StopReason:
        swarm.start(lead)
        async for event in swarm.events():
            if isinstance(event, MessageDelivered):
                for name in event.recipients:
                    if swarm.member(name).status == "pending":
                        swarm.start(name)
            elif isinstance(event, MemberEnded) and event.member == lead:
                return "lead_ended"
        return "all_ended"

    def check(roster: Sequence[MemberInfo]) -> None:
        if lead not in [m.name for m in roster]:
            raise ValueError(f"coordinator: no member named {lead!r}")

    return Controller(run, final=reporter(lead) if final is None else final, check=check)
```

This assumes the bus accepts a record for a member that has not started and holds it in that member's inbox; the communication deep dive decides whether it does, and the coordinator work confirms it. A worker that has submitted cannot be woken again until persistent members exist (swarm.md, [Members](swarm.md#members)).

**A user-defined controller.** Adaptive compute: run one cheap member alone, and start the rest only if its answer does not verify.

```python
from collections.abc import Sequence

from inspect_swarm import Controller, MemberEnded, MemberInfo, StopReason, SwarmControl, controller


@controller
def escalate(first: str, final: FinalSpec | None = None) -> Controller:
    """Run `first` alone; if its submission does not verify, start everyone else."""

    async def run(swarm: SwarmControl) -> StopReason:
        swarm.start(first)
        async for event in swarm.events():
            if isinstance(event, MemberEnded) and event.member == first:
                if event.verdict is not None and event.verdict.passed:
                    return "first_verified"
                for m in swarm.members:
                    if m.status == "pending":
                        swarm.start(m)
        return "all_ended"

    def check(roster: Sequence[MemberInfo]) -> None:
        if first not in [m.name for m in roster]:
            raise ValueError(f"escalate: no member named {first!r}")

    return Controller(run, final=final, check=check)


swarm(
    members=[member(react(model="openai/gpt-5-mini"), name="scout"), member(deepagent(), name="worker", count=4)],
    controller=escalate(first="scout"),
    result=answer_result(verifier=check_answer),
)
```

Defined in a task file, its registry name is `escalate`; in an installed package `mypkg`, it is `mypkg/escalate`. The plan step logs `"controller": {"type": "controller", "name": "escalate", "params": {"first": "scout", "final": null}}`. `-T controller=escalate` cannot work, because `first` has no default; a task that sweeps it takes `first` as its own parameter.

### Changes to swarm.md and the overview

Made in this PR, limited to the API and the name:

- swarm.md [The shape](swarm.md#the-shape): component 2 is **Channels**; components 2 and 3 name `@channel` and `@controller` and link here.
- swarm.md [Entry point](swarm.md#entry-point): the sketch becomes this API's common case and explicit form, and links here for the rest.
- swarm.md "Substrate" section: renamed **Channels** (anchor `#channels`), with one sentence on `@channel`. Its two in-document links are updated; the ordinary-sense uses of "substrate" stay.
- swarm.md [Controller](swarm.md#controller-topology-termination-final-answer): topologies are controllers; `final=` is the controller's parameter; a note that the runtime, not the controller, owns drain, exhaustion, verification and finalisation.
- swarm.md [Where each part lives](swarm.md#where-each-part-lives): a row for the two registry types in inspect_ai.
- swarm.md M1 in the [Implementation plan](swarm.md#implementation-plan): M1 needs the one-line inspect_ai registry PR first, and builds the API skeleton described here.
- swarm-overview.md: the component table and code sample, M1's inspect_ai note, and the inspect_ai row of "Where each part lives".

The sibling deep dives link `swarm.md#substrate` (the communication and ORBIT deep dives); whichever of them merges after this PR updates the link to `swarm.md#channels`.

## Alternatives considered

**Keep strings and scalars** (swarm.md's sketch). The least machinery. There is no way to add a topology or a channel without changing inspect_swarm, the final-answer chain has to be validated against a separate `topology=`, and ORBIT's scheduled mode needs a protocol of its own anyway. Rejected.

**Plain config objects and protocols, no registry** (deepagent's `Subagent`; the ORBIT deep dive's `ActivationPolicy` passed as an object). No inspect_ai change. But a user's controller cannot be named from `-T` or a config, parameters are not captured automatically, and an object that is not a pydantic model is logged as its type name, which makes otherwise different arms "not distinct" in `eval_set` (spike). Rejected for controllers and channels; adopted for members, where the object being named (the `Agent`) is already in the registry.

**Register controllers and channels under existing types** (`solver` or `agent`, with metadata saying what they are). No inspect_ai change. But `--solver leaderless` would then resolve to a controller and fail obscurely, and anything that lists solvers or agents would list controllers. Rejected; the literal entry is one line with precedent.

**One `swarm_component` registry type for all components.** One literal entry instead of two. But a name could then resolve to the wrong kind (`channels=["leaderless"]`), so every lookup needs a second check, and the decorator name no longer says the type. Rejected.

**Namespaced type names** (`swarm_controller`, `swarm_channel`). Safer if inspect_ai ever wants a `channel` type of its own (its `AgentChannel` is not a registry object today). Scout and sentinel use plain names (`scanner`, `monitor`, `protocol`), and the decorators are `@controller` and `@channel` either way. Plain names recommended; [open question 2](#open-questions) leaves the choice to the inspect_ai PR's review.

**Hook-style controllers** (a class with `on_start`, `on_member_ended`, `should_stop` methods, as Inspect's `Hooks` are). Declarative and harder to misuse. But sequencing (escalate, waves, ORBIT's plans) becomes a state machine spread across callbacks, and Inspect's class registration does not capture constructor parameters into the log the way factory decoration does. Rejected in favour of one `run` coroutine over an event stream.

**Controllers that run members themselves** (`await member.run()` in an anyio task group). Familiar to anyio users. But the controller would then own member tasks, so drain and cap recovery would depend on every controller cancelling correctly, and a controller bug could leave a member running during finalisation. Rejected: the runtime owns member tasks, and the controller only starts them.

**`final=` stays on `swarm()`** (the scoring deep dive's draft). Keeps scoring's API as drafted. But the valid modes and the default depend on the topology, which is the controller, so `swarm()` would have to cross-validate two arguments that belong together. Rejected; scoring's chain and its semantics are unchanged, only where it is passed.

**An observer argument or `@observer`.** Would let a user change evidence or accounting per arm, which is exactly what must not vary between arms. Rejected; extensions go through sentinel, scorers and Scout.

**One `SwarmSpec` object** holding all components (`swarm(SwarmSpec(...))`). One serialisable record. Inspect's APIs are keyword-first (`Task(...)`, `react(...)`), and a spec object is one more layer for the common case. Rejected.

## Compatibility and migration

- **inspect_swarm** has no released API ([swarm.md](swarm.md#compatibility-and-migration)). The sketch's `topology=` and `final=` never shipped. The sibling deep dives' parameters move as [the table](#where-the-sibling-designs-parameters-go) says; each deep dive applies the move when it is next revised or implemented.
- **inspect_ai.** Two entries in `RegistryType`. They are additive: nothing in inspect_ai uses the type set except `RegistryInfo`'s validation and `is_registry_dict()`, which then recognises two more kinds of logged object, and the literal is not in the log schema or the generated TypeScript types (verified above). inspect_swarm needs the inspect_ai version that has them; it tracks inspect_ai `main`, and the release floor pinned by `release-pin-deps.yml` covers released versions. Imported with an older inspect_ai, inspect_swarm's decorators fail at import with pydantic's `ValidationError` on `RegistryInfo`.
- **Eval logs.** The swarm's plan step gains nested `{"type": "controller" | "channel", "name", "params"}` objects and member dicts. These are ordinary JSON inside `params`, which is `dict[str, Any]` (`src/inspect_ai/log/_log.py:705-718`), so every log reader accepts them. An inspect_ai that lacks the types treats them as plain dicts on replay; replaying a swarm log needs inspect_swarm installed anyway.
- **Documents.** `swarm.md#substrate` becomes `swarm.md#channels`; the sibling deep dives' links follow on merge ([above](#changes-to-swarmmd-and-the-overview)).

## Security

- **Names from the command line or a log.** `-T`, `-S` and replay hand strings and dicts to name resolution, which only finds registered objects. A `package/name` loads that installed package's declared entry point, which `--solver` already does today (`registry.py:289-297`). Logs replayed with `eval-retry` are trusted input, as they already are for solvers. No new exposure.
- **Controller and channel code is trusted eval-author code**, like solvers. It is never reachable from member tools, and members cannot select a controller or a channel.
- **No model text reaches a controller.** Handles and events carry statuses, verdicts, member names chosen by the eval author, record ids and channel names, never a member's output or a message payload ([Controllers](#controllers)). So a controller cannot relay model output into another member's input. `start(input=...)` takes the eval author's text; its documentation says never to put member output there.
- **The preamble** contains the member's name and role, the roster size, and channel instructions, all from the eval author. Nothing a member wrote enters it.
- **Logged parameters** are the eval author's arguments, written as JSON data in the plan, as Inspect already writes solver parameters.

## Testing

All in inspect_swarm, with mockllm, on asyncio and trio, no network or Docker:

- `tests/test_registry.py`:
  - `@controller` and `@channel` register under `inspect_swarm/<name>` for built-ins and the bare name for a task file's own factory, with and without `name=`;
  - a factory returning the wrong type raises `TypeError`;
  - resolution: a `/` name, a bare built-in name, a user factory shadowing a built-in, an unknown name (`ValueError` listing names), a factory with a required parameter (`TypeError`);
  - `registry_lookup("controller", "inspect_swarm/leaderless")` succeeds in a fresh interpreter that has not imported inspect_swarm (the entry point).
- `tests/test_api_log.py`:
  - plan-step `params` and `params_passed` for the common case, the explicit form and a heterogeneous roster match expected JSON, including nested agents and limits;
  - `create_registry_object()` on those params rebuilds an equivalent swarm whose params are equal;
  - `eval_set` accepts two arms that differ only in a controller parameter, a channel parameter, a member's `count` or `limits`, or `Budget`, and assigns them different identifiers;
  - `-T channels=filesystem` (a string) and `-T channels=filesystem,messages` (a list) both resolve.
- `tests/test_controller.py`:
  - `leaderless` starts every member and stops `all_ended`; with `stop_on_verified` it stops at the first passed verdict and the rest are drained;
  - a custom controller (`escalate`) starts members later and with custom input; `start()` twice raises; `events()` ends when nothing runs and a second concurrent iterator raises;
  - returning while members run cancels and drains them before finalisation; an exception in `run` drains and propagates; a member's own limit yields `MemberEnded(status="limit")` and the swarm continues; the swarm cap cancels `run`, records the runtime's reason and finalises;
  - events and handles expose no member output (the dataclasses' fields are the contract; one test asserts a member's distinctive output string never appears in any event);
  - one swarm object serves two concurrent samples with no shared controller state;
  - `check` errors and invalid `final` raise at `swarm()` construction.
- `tests/test_channel.py`: `filesystem()`'s instructions appear in each member's preamble with that member's scratch directory; `channels=[]` adds none; a duplicate channel raises.
- `tests/test_member.py`: count expansion and names; duplicate names raise; unregistered agents and `deepagent(background=True)` are rejected; `Member` validates from its own logged dict.

In inspect_ai, the registry PR adds a test to `tests/util/test_registry.py` that registers, looks up and round-trips (`is_registry_dict()`) one object of each new type, as inspect_ai#4359 did for `validation_predicate`.

## Implementation plan

1. **inspect_ai: two registry types.** Add `"controller"` and `"channel"` to `RegistryType`, a test in `tests/util/test_registry.py`, and a CHANGELOG line. Files: `src/inspect_ai/_util/registry.py`, `tests/util/test_registry.py`, `CHANGELOG.md`. Before M1.
2. **inspect_swarm: registry plumbing** (M1). The decorators, `Controller`, `Channel`, `MemberInfo`, `resolve()`, and the entry point. Files: `src/inspect_swarm/_controller/_controller.py`, `src/inspect_swarm/_channel/_channel.py`, `src/inspect_swarm/_registry.py`, `src/inspect_swarm/_entrypoint.py`, `pyproject.toml` (`[project.entry-points.inspect_ai]`), `tests/test_registry.py`.
3. **Members and the `swarm()` signature** (M1). `Member` and `member()` with their serialisers; `swarm()`'s arguments, validation and resolution; exports in `src/inspect_swarm/__init__.py`. Files: `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/__init__.py`, `tests/test_member.py`, `tests/test_api_log.py`.
4. **The controller surface in the runtime** (M1). `SwarmControl`, `MemberHandle`, `MemberEnded`, `events()`, `start()` and the preamble, in the M1 runtime the limits and scoring deep dives describe; the built-ins `leaderless` and `filesystem`. Files: `src/inspect_swarm/_controller/_control.py`, `src/inspect_swarm/_controller/leaderless.py`, `src/inspect_swarm/_channel/filesystem.py`, `src/inspect_swarm/_swarm.py`, `tests/test_controller.py`, `tests/test_channel.py`.
5. **M2.** `messages` as a `@channel`, the communication deep dive's operations on `Channel` with per-run state, `MessageDelivered`, and `swarm(policy=)`.
6. **Later.** `coordinator` with the coordinator topologies; the ORBIT deep dive's scheduled mode as controllers.

Steps 2 to 4 are the skeleton of M1, not a separate milestone: swarm.md's M1 builds its runtime behind this surface.

## Open questions

1. **The component's new name.** This document recommends **Channels** and has applied it in swarm.md and the overview. The alternatives are compared [above](#naming-the-component-channels); "comms" is the runner-up. If you prefer another, the rename is mechanical.
2. **An inspect_ai PR before M1.** swarm.md said M1 needs no inspect_ai change. `@controller` and `@channel` need two entries in `RegistryType`: a one-line PR with a test, as for `validation_predicate` (inspect_ai#4359). Options:
   - (a) land that PR before M1 (recommended); plain names `controller` and `channel`, or `swarm_controller` and `swarm_channel` if inspect_ai's reviewers prefer namespacing;
   - (b) start M1 with string names resolved from inspect_swarm's own table and add the decorators when the PR lands, which means a second API change soon after the first.

## Not this design

- **Limits and frozen dataclasses log poorly in inspect_ai.** A `Limit` argument is logged as its class name (`"_CostLimit"`), or `null` inside a list; a frozen dataclass as its type name, which makes otherwise different eval-set arms "not distinct". This affects any agent or solver (deepagent's `Subagent.limits`, for example). inspect_ai could serialise dataclasses at top level as it already does inside lists, and limits by kind and value.
- **A dict form for controller parameters on the command line** (`-T 'controller={name: escalate, first: scout}'`).
- **Grid sweeps from the CLI** (`-T k=2,4,8` as three runs) belong to eval-set or inspect_flow, not to inspect_swarm.
- **A generic swarm task** (`inspect_swarm/swarm_task`) that wraps any dataset, so arms can be swept with no task code.
- **Listing controllers and channels** (`inspect list`-style discovery of registered names).
