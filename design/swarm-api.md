# Inspect Swarm: the `swarm()` API

Status: proposed, 2026-10-08. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

A deeper dive on one topic of [swarm.md](swarm.md), the high-level design: the public shape of `swarm()`. Its arguments mirror the four components (members, channels, controller, observer). Controllers and channels are Inspect registry objects (`@controller`, `@channel`), selectable by name, with their parameters in the log. The [appendix](#appendix-how-this-design-changed-the-earlier-documents) records what this design changed in swarm.md and the overview, and why.

It respects the decisions recorded in swarm.md (Ransom, 2026-10-07): Python 3.11+, inspect_ai internals may be used, inspect_ai limits stay soft, M1 then M2 then the rest in any order, members share the sample sandbox by default, monitoring aligns with inspect_sentinel, red-team features are optional, peer messages are model output delivered as tool output with distinct provenance, and the transcript representation is open. It records two of its own (decision: Ransom, 2026-10-08; [Open questions](#open-questions)): the component is called **Channels**, and a one-line inspect_ai PR adding two registry types lands before M1. Four sibling deep dives run in parallel: [limits](https://github.com/meridianlabs-ai/inspect_swarm/pull/5), [scoring](https://github.com/meridianlabs-ai/inspect_swarm/pull/2), [inter-agent communication](https://github.com/meridianlabs-ai/inspect_swarm/pull/3) and [ORBIT on inspect_swarm](https://github.com/meridianlabs-ai/inspect_swarm/pull/4). This document says where their parameters go in the API ([table](#where-the-sibling-designs-parameters-go)) and does not design their semantics. All four are unmerged proposals; where this document cites one, it cites the PR's head on 2026-10-08.

Code references are to inspect_ai `main` at `fccfb298` (2026-10-07, the version this repository's lockfile installs), inspect_ai `feature/sentinel` at `eff42b6f`, inspect_sentinel at `c8cd71d` and inspect_scout at `d746d2a3` (both 2026-10-07). Paths are relative to each repository's root.

## The API at a glance

Three shapes determine everything else, and are what to review first: `swarm()`'s signature, the controller interface (what a controller can do and see) and the channel interface. Signatures only; the rules are in the sections linked below.

```python
@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],    # member(agent, name=, role=, count=, limits=)
    *,
    controller: str | Controller = "leaderless",            # a name, or a @controller object
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),   # names or @channel objects
    budget: Budget = Budget(),                              # the swarm-wide cap (limits deep dive)
    result: ResultSpec | None = None,                       # the task's result contract (scoring deep dive; after M2)
) -> Agent: ...

# Controllers: how the swarm runs. A @controller factory returns a Controller.
@controller
def leaderless(final: FinalSpec | None = None, stop_on_verified: bool = False) -> Controller: ...   # M1: no parameters

class Controller:
    def __init__(
        self,
        run: Callable[[SwarmControl], Awaitable[StopReason]],   # once per sample; returning requests the stop
        *,
        final: FinalSpec | None = None,                           # the final-answer chain (after M2; M1 answers with `first`)
        check: Callable[[Sequence[MemberInfo]], None] | None = None,   # roster check at construction
    ) -> None: ...

class SwarmControl(Protocol):                    # what `run` works through
    @property
    def members(self) -> Sequence[MemberHandle]: ...   # name, role, state, status, submitted, verdict (after M2)
    def member(self, name: str) -> MemberHandle: ...
    def start(self, member: str | MemberHandle, input: str | list[ChatMessage] | None = None) -> None: ...
    def events(self) -> AsyncIterator[SwarmEvent]: ...  # MemberEnded in M1

# Channels: how members share information. A @channel factory returns a Channel.
@channel
def filesystem(shared: str = "/shared", notes: str = "notes.md", scratch: str = "/scratch/{member}") -> Channel: ...

class Channel:
    def instructions(self, member: MemberInfo) -> str | None: ...   # M1: text for the member's preamble
    # M2: the communication deep dive's operations (tools, validate, apply, notice_lines, quiescent)
```

- **Members** are pydantic records around a registered `Agent`; a bare `Agent` is one member ([Members](#members)).
- **The controller** decides which members start, when and with what input, when the swarm is done, and the final-answer chain. inspect_swarm's fixed runtime owns everything else: member tasks, limits, stopping, verification, finalisation and the record. A controller sees states, statuses and a verdict's pass flag and value, never member text ([Controller and runtime](#controller-and-runtime), [Controllers](#controllers)).
- **Scoring's parts follow M2.** M1 builds this API without the scoring deep dive's post-M2 parts: `result=`, `leaderless()`'s `final` and `stop_on_verified`, `Controller(final=)`, and the `verdict` on handles and `MemberEnded`. M1's final answer is always `first` (decision: Ransom, 2026-10-08; [swarm-scoring.md](swarm-scoring.md#what-m1-leaves-out-of-the-merged-api)). Each is added later as a keyword with a default that keeps M1's behaviour, or as a field that defaults to `None`.
- **Channels** add instructions to each member's preamble in M1, and tools and delivery from M2 ([Channels](#channels)).
- **The observer** has no `observer=` argument: what it records is fixed and versioned. Monitors (inspect_sentinel), scores (the task's scorers) and labels (Scout) are configured where Inspect configures them; the communication deep dive's bus hook, `policy=`, is the one setting on `swarm()` from M2 ([Observer](#observer)).
- **Names, logging and task identity.** A string resolves to a registered controller or channel with its defaults ([Resolving names](#resolving-names)). Every argument this API defines is logged faithfully ([Logging and replay](#logging-and-replay)). An **arm** is one condition of an experiment; in Inspect terms, one task in an eval set: a `Task` instantiated with its arguments and run with a given solver, model and limits, which Inspect tells apart by `task_identifier()`. A swarm is the task's solver, so its arguments, including the nested controller, channel, member and `Budget` parameters, are in the plan step's `params_passed`, which `task_identifier()` hashes: two arms that differ in any of them are distinct in an eval set ([Task identity](#task-identity-what-makes-two-arms-distinct)).
- **One inspect_ai prerequisite**: `"controller"` and `"channel"` added to inspect_ai's `RegistryType`, before M1 (decision: Ransom, 2026-10-08; [The registry](#the-registry)).

## Why

Ransom's direction (2026-10-08) was that the API's shape should match the components, with a registry object (`@controller`) in place of scalar topology and final-answer settings, and the same for the other components where it fits ([quote](#the-request)). The API has to provide:

- **An extension point for how a swarm runs.** Research arms run swarms differently: a cheap member first and the rest only if it fails, members started in waves, ORBIT's scheduled rounds. A closed list of topology names has nowhere to put them; the ORBIT deep dive defines its own `ActivationPolicy` protocol for exactly this (PR #4, "P4. The turn seam and activation policies"). Channels need the same: a way to add one without changing inspect_swarm.
- **The component boundaries in the call.** The final-answer default and the valid modes depend on the topology (`reporter` for coordinators, the verify/vote/first chain for leaderless swarms; swarm.md, [Final answer](swarm.md#controller-topology-termination-final-answer)). Passed as two separate settings, they must be validated against each other; held by one controller object, they cannot disagree.
- **Arguments that reach the log faithfully.** Inspect logs an agent's arguments by its own rules ([How arguments reach the log](#how-arguments-reach-the-log)). Against the installed inspect_ai, an argument `cost_limit(40.0)` is logged as the string `"_CostLimit"`, and inside a list as `null`; a frozen dataclass is logged as its type name. Two `eval_set` arms that differ only in such an argument get the same task identifier, and `eval_set` refuses them: "The task 'arm' is not distinct." (spike below). Sweepability is a goal of swarm.md ("agent count, topology, communication channels and budget split ordinary, sweepable task parameters"), so this matters.
- **One place for the sibling deep dives' parameters.** Each adds parameters to `swarm()`: `budget=Budget(...)`, `result=`, a `final=` chain, `messages(...)` in `channels=`, `policy=`, `routes=`, `member(delivery=, bridged=, input=, exposure=)`, and an activation-policy protocol. Without one shape for the call, those land as a flat list of keyword arguments with no rule for which component each belongs to or how it is logged.

## Goals and non-goals

### Goals

- `swarm()`'s arguments mirror the components: `members=`, `channels=`, `controller=`. The observer has no component argument ([why](#observer)).
- The components users extend are Inspect registry objects, with Inspect's conventions: decorated factories, package-qualified registry names, creation parameters captured into the log, lookup by name, and an `inspect_ai` entry point. Those are controllers (`@controller`) and channels (`@channel`).
- The common case stays one line, and the arguments work as task parameters (`-T`), in a Python `eval_set` sweep and in the log. Names resolve to registry objects. Everything this API defines (member records, controllers, channels, the budget) is logged faithfully and can be rebuilt from the log. What it cannot rebuild alone (a member agent configured with callables, a sample-scoped agent, the task's result contract) has a stated route: a registered builder or the task ([Logging and replay](#logging-and-replay)).
- One place for each sibling design's parameters.
- For everything that configures how a swarm runs or is measured, a stated answer to whether it distinguishes arms in an eval set ([Task identity](#task-identity-what-makes-two-arms-distinct)).

### Non-goals

- The semantics of limits, scoring, communication and ORBIT's scheduled mode. Their deep dives own them; this document gives their parameters a place and states the one rule they must meet (serialisation).
- The coordinator topologies and persistent members (later work in swarm.md's plan). This document shows how a coordinator plugs in, not how idle and wake work.
- An observer registry type or argument. What the observer records is a fixed contract ([Observer](#observer)).
- CLI conveniences beyond what Inspect already parses: grid sweeps, or a dict form for controller parameters on the command line ([Not this design](#not-this-design)).
- Changing how inspect_ai serialises arguments in general, or making arbitrary agents' callbacks serialisable ([Not this design](#not-this-design)).
- Supporting inspect_swe's ACP agents as members in M1 ([Members](#members)).

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
| A plain callable (an `on_continue` hook, a filter) | its `__name__` (`registry.py:233-234`). On replay that string is passed back where the callable was: a submitting `react()` then uses it as continuation text, and `react(submit=False)` rejects it (`src/inspect_ai/agent/_react.py:132`, `:394`). Two hooks with the same `__name__`, such as two closures from one helper, log identically |
| A non-serialisable object inside a list | `null`: `[cost_limit(40.0)]` → `[null]` |

Where that log goes:

- **The plan.** Each solver step's arguments are logged as `params` (with defaults) and `params_passed` (`src/inspect_ai/_eval/task/log.py:1233-1246`). `as_solver()` forwards the agent's registry params to the solver it creates (`src/inspect_ai/agent/_as_solver.py:85-91`), so a `swarm()` used as a solver logs its own arguments.
- **Eval-set identity.** `eval_set()` refuses two tasks with the same `task_identifier()` (`src/inspect_ai/_eval/evalset.py:2046-2052`). The identifier (`:2111-2282`) is `{task_file}@{task_name}#{args_hash}/{model}/{additional_hash}`. `args_hash` hashes the task arguments. `additional_hash` hashes the solver plan (each step's `params_passed`, with `params` stripped, `:2227-2238`), the generate config, model args and model roles, the task version, and the sample limits (message, token, turn, time, working, cost), whether set on the task or as eval-set options. It does not hash scorers, the dataset, epochs, the sandbox or (on `feature/sentinel`) the sentinel configuration; those distinguish two tasks only through the task's name or arguments. Spikes with mockllm:
  - two `Task(name="arm")` with the same solver that differ only in their scorer, or only in `epochs`, are refused as "not distinct"; an `@task` that picks the same two scorers from a task argument gives two distinct arms, and so do two tasks that differ only in `message_limit`;
  - two tasks named `arm` whose solvers were an `@agent` given `Budget(10.0)` and `Budget(20.0)` (a frozen dataclass) are refused; the same agent given pydantic models logs `{"budget": {"cost": 10.0}}` and `{"cost": 20.0}`, and the arms are distinct.
- **Replay.** `create_registry_object()` rebuilds an object from logged arguments, passing them through `registry_kwargs()`, which turns each nested `{"type", "name", "params"}` back into an object (`registry.py:452-467`, `:689-704`). It recognises such a dict only when its `type` is in `RegistryType` (`is_registry_dict()`, `:647-657`). This is the path `--solver <name> -S ...` takes for an `@agent` (`src/inspect_ai/_eval/loader.py:676-684`).
- **The command line.** `-T` and `-S` values are parsed as YAML, and a plain value with commas becomes a list (`src/inspect_ai/_util/config.py:23-48`). So `-T channels=filesystem,messages` is a list, but `-T channels=filesystem` is the string `"filesystem"`.

### Registry objects are shared by concurrent samples

A task's plan is built once, and every sample runs the same objects (`src/inspect_ai/_eval/task/run.py:2831`, `await plan(state, generate)`). So an agent, solver, controller or channel object is shared by all concurrently running samples, and anything per-sample must live in the call, not on the object. Most agents meet this: `react()`, `deepagent()` and inspect_swe's bridged CLI agents keep their state in the invocation. Not all do. inspect_swe's ACP agents (`interactive_claude_code`, `interactive_codex_cli`, `interactive_gemini_cli`) refuse to be constructed outside an active sample and keep the live connection, session and readiness event on the instance (`inspect_swe@a54461e7: src/inspect_swe/acp/agent.py:97-110`, `:190-196`), so they cannot be built at task construction or shared.

### Precedent for member-like records

deepagent's `Subagent` is a plain dataclass built by `subagent()` (`src/inspect_ai/agent/_deepagent/subagent.py:13-53`). It is configuration, not a registry object, and it is logged by its `name` only (table above).

## Design

### The shape of the call

| Component | Argument | Value | Registry type |
|---|---|---|---|
| Members | `members=` | `member(agent, ...)` records; a bare `Agent` is one member | none: the member's `Agent` is already an `@agent` registry object |
| Channels | `channels=` | channel names or `@channel` objects | `channel` (new) |
| Controller | `controller=` | a controller name or a `@controller` object | `controller` (new) |
| Observer | none; from M2, `policy=` sets its bus hook | always on | none |

Two arguments are not components: `budget=` (the swarm-wide cap; the limits deep dive's `Budget`) and, after M2, `result=` (the task's result contract; the scoring deep dive's `ResultSpec`). M2 adds `policy=` (the communication deep dive's bus hook, a setting of the observer's interception point; [Observer](#observer)).

```python
# src/inspect_swarm/_swarm.py
@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],
    *,
    controller: str | Controller = "leaderless",
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),
    budget: Budget = Budget(),          # limits deep dive
    result: ResultSpec | None = None,   # scoring deep dive; after M2
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
- (after M2) `result` is a `ResultSpec` or `None`, and (from M2) `policy` a callable or `None`; the string a raw-solver replay passes for either raises `TypeError` ([Logging and replay](#logging-and-replay));
- the controller's `check` accepts the roster, and (after M2) the controller's `final` is valid for `result` (the scoring deep dive's validation rules, applied to the controller's chain).

### Controller and runtime

swarm.md uses "the controller" both for the policy that decides how the swarm runs and for the machinery that enforces the swarm's invariants. This design separates them:

- **The controller** is a registry object chosen per arm. It decides which members start, when, with what input; when the swarm is done; and which final-answer chain applies.
- **The runtime** is inspect_swarm's fixed code in `swarm()`. It owns member tasks, member spans and limits, the budget nodes, recovery from the swarm's own cap, drain, verification of submissions and the provisional answer, finalisation, the ledger, evidence and metrics.

A controller cannot break an invariant because it never holds what the runtime owns: it starts members through a handle, never runs them itself; it returns a stop reason, and the runtime then runs the limits deep dive's stop procedure; it names a final chain, and the runtime runs it after the drain. Where the sibling deep dives write "the controller" for drain, exhaustion, verification or finalisation, they mean the runtime.

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
- **The member contract.** A member's agent is built once, when the task is built, and the swarm invokes that one object for every member the record stands for and in every sample, concurrently. So the agent must be safe to invoke concurrently, with its state local to each invocation, as `react()`, synchronous `deepagent()` and the bridged CLI agents are ([Current behaviour](#registry-objects-are-shared-by-concurrent-samples)). An agent that must be created inside a sample, such as inspect_swe's ACP agents, is not a valid prebuilt member. It can be wrapped in a registered agent that creates it per invocation:

  ```python
  @agent
  def per_invocation_claude_code(model: str | None = None) -> Agent:
      async def execute(state: AgentState) -> AgentState:
          inner = interactive_claude_code(model=model)   # built inside the sample, per member
          return await inner(state)
      return execute
  ```

  M1 does not test or support ACP members; the wrapper is the route when someone needs one. `swarm()` cannot detect an unsafe agent in general, so the contract is documented on `member()`.
- **Tool state.** The runtime runs each member inside an inspect_ai tool-state scope named after the member, so built-in tools that keep state in the sample store (`memory()`, `bash_session()`, `web_browser()`, the skill tool) give each member its own state when their `instance` is left at the default (swarm.md, [Members](swarm.md#members); how the default is enforced is swarm.md's [open question 2](swarm.md#open-questions)). This is why `member(deepagent(...), count=4)` gives four memories rather than one. `member()`'s docstring states the rest of the contract:
  - an explicit `instance` is used as given, so members, or the copies of one counted member, that name the same instance share that state, which is a channel outside the bus ([Security](#security));
  - the sample store itself is shared, including any environment a task keeps there;
  - state that custom tools or agent code keep in the store with no instance is shared unless they resolve their instance with `tool_state_instance()`.
- **Faithful only as far as the agent's own params.** `registry_value(agent)` writes what Inspect captured when the agent was built. Ordinary values (`react(prompt=..., tools=[bash()])`) round-trip. A callable argument (an `on_continue` hook, a model filter, a `ToolSource` such as the communication deep dive's `swarm_tools()`) is logged as its name and cannot be rebuilt ([table](#how-arguments-reach-the-log)); two members that differ only in such a callback even collide in `eval_set`. The route is a **member builder**: a registered `@agent` factory with ordinary parameters that builds the configured agent inside, so the log records the builder and its parameters and replay calls it again:

  ```python
  @agent
  def worker(prompt: str = WORKER_PROMPT) -> Agent:
      return react(prompt=prompt, tools=[bash(), python(), swarm_tools()], on_continue=swarm_on_continue())
  ```

  `member(worker(), count=3)` logs `{"type": "agent", "name": "worker", "params": {"prompt": "..."}}`; a spike confirmed that the outer factory's identity and parameters replace `react()`'s and that `create_registry_object()` rebuilds it. The examples below use builders wherever a member has hooks.

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

- **`run`** is called once per sample with a `SwarmControl` and returns the stop reason. Returning requests the stop: the runtime stops members still running through the limits deep dive's soft-stop procedure ([Stopping](#controllers), below), then finalises. Its local variables are per sample; the `Controller` object is shared, so it holds no per-sample state ([Current behaviour](#registry-objects-are-shared-by-concurrent-samples)).
- **`final`** (after M2; in M1 the runtime always applies `first`) is the final-answer chain the runtime applies after the drain, and for the provisional answer as members end. `None` means the scoring deep dive's default chain for the task's `result` (verify, then vote, then first, from what the task supplies). It is on the controller, not on `swarm()`, because the default and the valid modes depend on the topology: a coordinator defaults to the lead's report, and `reporter` is invalid for a leaderless swarm.
- **`check`** runs at `swarm()` construction with the roster, for errors a factory cannot see alone (a named member that is not in the roster).
- **The decorator** follows `@agent` and sentinel ([The registry](#the-registry)): registry name `registry_name(func, name or func.__name__)`; the wrapper calls the factory, raises `TypeError` unless it returned a `Controller`, and tags it with `registry_tag(func, instance, RegistryInfo(type="controller", name=...), *args, **kwargs)`; `registry_add()` registers the wrapper. `Controller` is a plain class, not a frozen dataclass, because tagging sets attributes on it. The factory is annotated `-> Controller` so `registry_create("controller", ...)` instantiates it.

The handle a controller works through:

```python
MemberState = Literal["pending", "running", "ended"]

@dataclass(frozen=True)
class VerdictSummary:
    passed: bool
    value: float | None          # scoring's optional graded value; never the explanation (VerdictSummary: after M2)

class MemberHandle(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def role(self) -> str | None: ...
    @property
    def state(self) -> MemberState: ...
    @property
    def status(self) -> str | None: ...              # the limits deep dive's status once ended, else None
    @property
    def submitted(self) -> bool: ...
    @property
    def verdict(self) -> VerdictSummary | None: ...  # after M2; None without a verifier or before it ran

class SwarmControl(Protocol):
    @property
    def members(self) -> Sequence[MemberHandle]: ...           # roster order, after count expansion
    def member(self, name: str) -> MemberHandle: ...            # KeyError for an unknown name
    def start(
        self,
        member: str | MemberHandle,
        input: str | list[ChatMessage] | None = None,           # None: the member's input, else the swarm's
    ) -> None: ...
    def events(self) -> AsyncIterator[SwarmEvent]: ...

@dataclass(frozen=True)
class MemberEnded:
    member: str
    status: str                  # the limits deep dive's member status
    submitted: bool
    verdict: VerdictSummary | None   # after M2

SwarmEvent = MemberEnded         # later work adds kinds (idle and woken members, quiescence)
```

- **`start()`** schedules the member in the runtime's member task group, under `run()` in its own span with its limits, and returns at once. Its input is the given input, else the member record's `input` (the ORBIT deep dive's field), else the swarm's input, followed by the member's preamble: its name, role and roster size, and each channel's instructions ([Channels](#channels)). The preamble's wording is M1's prompt work. In M1 a member runs once: `start()` on a member whose state is not `pending` raises `RuntimeError`. `start()` also raises `RuntimeError` once the swarm is stopping ([Stopping](#controllers), below). Persistent members, later, add a wake operation.
- **`events()`** yields events in order from swarm start, so none is lost between `start()` and iteration. A member counts as running until the runtime has taken its submission's verdict and queued its `MemberEnded`, so an asynchronous verifier still running when the last agent returns cannot be missed. The iterator ends when no member is running and nothing is queued; a member started later is reported by a new `events()` call. One iterator at a time; a second concurrent call raises `RuntimeError`. A controller tests an event's kind before reading its fields (`isinstance(event, MemberEnded)`), as every example below does: `SwarmEvent` becomes a union when later work adds idle and woken members (which a controller must not mistake for an ending) and quiescence (which has no `member`).
- **Statuses.** `state` is the coarse lifecycle a controller schedules on. `status` and `MemberEnded.status` are the limits deep dive's closed set, unchanged: `submitted`, `stopped` (soft stop), `share`, `member_limit:<type>`, `swarm_cap`, `cancelled`, `errored` (PR #5, "Member wrapper and statuses"). `submitted` is the scoring deep dive's notion of a submission, which a member stopped mid-work does not have.
- **No member text.** Handles and events carry states, statuses, names chosen by the eval author and a verdict's pass flag and value, never a member's output, a message body or a verifier's explanation (scoring's `Verdict.explanation` is free text that may quote the answer or a judge model's words; it stays in the observer's result record). A controller therefore cannot pass one member's words into another's input, which would put model output in the user role (swarm.md, [Delivery](swarm.md#delivery-peer-messages-are-model-output)). Members read each other through channels' tools. ORBIT's scheduled mode adds conversation snapshots and trusted interventions to these handles for trusted policies; that is its deep dive's extension, argued there.
- **Stopping.** Returning from `run` is a request to stop, not a cancellation. The runtime passes the returned reason to the limits deep dive's single stop procedure, `stop(reason)`: a soft stop through each running member's stop lever, a grace period of `Budget.grace` for in-flight calls to finish and be recorded, and a hard stop (cancellation) only for members still running after the grace (PR #5, "Stopping"). The swarm's own cap and time cap go through the same procedure (`swarm_cap`, `swarm_time`). The runtime orders every stop so that no member can start once it has begun:
  1. **Close scheduling, synchronously.** The first thing `stop(reason)` does, before any await, is mark the swarm as stopping. From then on `start()` raises `RuntimeError`. This matters because the soft stop closes only the levers of members already running, and the swarm time cap is a timer, not an Inspect time node the members inherit (PR #5, "Stopping", "Triggers"). A member started during grace would have an open lever and could dispatch new calls until the hard stop cancelled them, turning spend into unknown expenditure.
  2. **Cancel the controller, not the members.** For a stop the controller did not request (`swarm_cap`, `swarm_time`), the runtime cancels `run`'s own cancel scope at this point. `run` runs in a scope separate from the member task group, so cancelling it cancels no member. When the controller returned, there is nothing to cancel.
  3. **Soft stop, grace, hard stop**, as the limits deep dive specifies. Members ending during grace still get statuses, evidence and their `MemberEnded`, but no controller acts on them.
  4. **Finalise** with `first` in M1, and after M2 with the controller's `final`.

  Members never started stay `pending`; the evidence records them as not started.
- **Errors and outer stops.** These are not stops the controller requested, and the limits deep dive's exhaustion rules apply unchanged: a foreign error from a member (a sample limit, `TerminateSampleError`, a crash) or an exception from `run` makes the task group cancel the rest at once and propagates; cancellation from outside (the sample's time limit, an operator interrupt) does no awaiting work. A member's own limit is not an error: that member ends with a status and an event, and the swarm continues.
- **Stop reasons.** The limits deep dive's closed set is `all_done`, `verified`, `swarm_cap`, `swarm_time` and later `quiescence`. A controller returns one of the topology reasons (`all_done`, `verified`, `quiescence`), or its own string, which is recorded as `controller:<reason>`; the ORBIT deep dive's `policy:<name>` reasons are this form once its policies are controllers. The built-in `leaderless` returns `all_done` or `verified`.
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
- **The name.** The component is called Channels (decision: Ransom, 2026-10-08: "channels is fine for now", so the name may be revisited). inspect_ai's unrelated `AgentChannel`, the per-execution queue that delivers notices, is always called the *agent channel* in these documents, as swarm.md's [The agent channel](swarm.md#the-agent-channel) does. The names compared are in the [appendix](#why-the-component-is-called-channels).

### Observer

The observer is what the swarm records and where monitors attach: evidence events, the realized-cost ledger, metrics, the per-member result record, and the bus's interception point (swarm.md, [Observer](swarm.md#observer-evidence-accounting-and-metrics)). It has no `observer=` argument and no `@observer` registry type.

**Why what it records is fixed.** The record is a data contract with its readers, not a policy choice. The scoring deep dive's `member_scores()` and `attempt_rows()`, the limits deep dive's ledger and curves, and swarm.md's analysis helpers read its fields by name and version, and every arm writes the same record: a baseline arm runs as a swarm of one (the scoring deep dive's `baseline()`), so single-agent, epochs and swarm arms are read by the same code. A per-arm switch that dropped an evidence kind or changed the ledger's coverage rules would make every field optional for every reader, and an arm without, say, a complete ledger could not be compared on realized cost at all. What users do want to vary in how a run is watched and measured, monitors, scores and labels, already has an Inspect mechanism, configured per task or per eval, so the swarm adds none. The record changes only with an inspect_swarm release, and carries a `version` (swarm.md, [Observer](swarm.md#observer-evidence-accounting-and-metrics)).

**How it is configured.** What is fixed, what can be set, and where:

| What | Set by | Where it lives | Changes how the swarm runs? |
|---|---|---|---|
| Evidence events, the ledger and its coverage rules, metrics, the per-member result record | Fixed | inspect_swarm (versioned) | No |
| What the result record can hold (after M2): verdicts and a comparable answer form exist only when the task's result contract supplies a verifier and an answer key | The task's result contract, `swarm(result=)` | The swarm's arguments, in the task | Yes, where the controller uses them: its `final` chain (`verify`, `vote`) and `stop_on_verified` |
| The bus hook: continue, modify, reject or terminate each sanctioned record (M2) | `swarm(policy=...)`, until the communication deep dive replaces it with sentinel protocols (its open question 2) | The swarm's arguments, in the task | Yes |
| Live monitors and protocols (tool calls, including sends and reads) | inspect_sentinel's `@monitor` and `@protocol`, through `Task(sentinel=...)` or `eval(sentinel=...)` / `eval_set(sentinel=...)` (inspect_ai `feature/sentinel`, `src/inspect_ai/_eval/task/task.py:107`) | The task, or eval options | Yes: a protocol can reject, modify or terminate |
| Scores: the task's headline score and per-member scores | The task's `@scorer`s ([Scores](#scores-the-task-scores-the-observer-records)) | The task | Not through the swarm, which never calls a scorer. But a member may: a retrying agent's in-loop scoring uses the task's scorers ([Scores](#scores-the-task-scores-the-observer-records)) |
| Post-hoc labels (coordination failures, message storms) | Scout `@scanner`s over the logs | A Scout scan job, outside the eval | No |

Whether each of these distinguishes two arms in an eval set is in [Task identity](#task-identity-what-makes-two-arms-distinct).

#### Scores: the task scores, the observer records

Scoring is not an observer extension. The observer records evidence; the task's own scorers score it, as for any Inspect solver. The swarm runtime never calls a scorer (the scoring deep dive rejects computing per-member scores inside the swarm: scorers need the target, and their usage would land in the solve). Two kinds of score read what a swarm leaves behind:

- **The task's score** (final@k, or the team result for shared-artifact tasks). Declared by the task as its `scorer=`, the same scorer in every arm. It reads the final state: `TaskState.output`, which holds the answer the controller's `final` chain selected after the drain, or, for a shared-artifact task, the environment the drained swarm leaves. `final` therefore decides what this score sees; `result=` supplies what the chain needs (the answer key for `vote`, the verifier for `verify`).
- **Per-member scores** (after M2; `best_member`, which is team@k for answer-scored tasks, and `mean_member`). Declared by the task too: the scoring deep dive's `member_scores(scorer, result)` is a scorer builder that the task wraps in a registered, no-argument `@scorer` of its own and lists after its headline scorer. It reads the observer's per-member result record from the sample store (each member's submission, status and verdict) and scores each member's submission with the given scorer on a copy of the state. It does not depend on the `final` chain: it scores every member, whichever answer was chosen. It needs the task's result contract (`kind="answer"`); without one it reports per-member values as unavailable.

Metrics are recorded the same way: the observer writes them to the store and sample metadata, and a scorer or the analysis helpers turn them into reported values (swarm.md's harness-validity checks are scorers of this kind).

**Members may score in the loop.** The runtime does not score, but a member's agent may call the task's scorers during the solve. `score(state)` runs the active task's scorers against the target from inside a solver or agent (`src/inspect_ai/scorer/_score.py:14-80`). `react(attempts=3)` does so after each submission and stops or retries on the first result (`src/inspect_ai/agent/_react.py:324-334`); `deepagent(attempts=)` passes it to its inner `react()` (`_deepagent/deepagent.py:235`); `basic_agent` (`src/inspect_ai/solver/_basic_agent.py:234`), the human agent's `/score` command and the attempt loops of six inspect_swe agents (`claude_code`, `codex_cli`, `mini_swe_agent`, `opencode`, `gemini_cli`, `kimi_code`) do the same. For such members the task's scorer is feedback that shapes the trajectory, not only a measurement afterwards, which is why a scorer change is an arm of its own ([Task identity](#task-identity-what-makes-two-arms-distinct)). Two rules from the scoring deep dive keep the per-member scorer out of that loop:

- `member_scores()` returns `None` at once while `state.completed` is `False`, so in-loop calls see exactly the task scorer's feedback and call count, and it never reads a half-written record (scoring PR #2, "Answer-scored tasks: per-member scores", step 1);
- it must follow the task's headline scorer in the scorer list (same section, "Ordering").

### Resolving names

A string for `controller=` or in `channels=` is resolved by `resolve(type, name)` in `src/inspect_swarm/_registry.py`:

1. A name with a `/` is looked up as given (`registry_lookup()`, which loads that package's entry point).
2. A bare name is looked up as given (an object registered in the user's own task file is unprefixed), then as `inspect_swarm/<name>`. A user's own `leaderless` therefore shadows the built-in, as inspect_ai's bare-name rule does (`registry.py:276-287`).
3. The object is created with no arguments, by `create_registry_object(type, name, {})`. A factory with a required parameter cannot be named this way, and the `TypeError` says so.
4. An unknown name raises `ValueError` listing the registered names of that type (`registry_find()`).

The resolved registry name is recorded in the swarm's evidence for each sample, so a log shows which object a bare name meant. inspect_swarm declares an `inspect_ai` entry point (`inspect_swarm = "inspect_swarm._entrypoint"`) that imports the built-ins, so `inspect_swarm/...` names resolve without importing the package first, as inspect_sentinel's does.

Both registry types need inspect_ai's `RegistryType` to list them ([The registry](#the-registry)): one inspect_ai PR adds `"controller"` and `"channel"`, as inspect_ai#4359 added `validation_predicate`. That is the only inspect_ai change this API needs. It lands before M1, with plain names; namespacing is left to that PR's review (decision: Ransom, 2026-10-08).

### Logging and replay

**The rule for what this API defines.** Every argument of `swarm()`, `member()` and every controller and channel factory is a plain value, a registry object, a list or dict of those, or a pydantic model whose fields follow the same rule (with field serialisers for registry objects, limits and models, as `Member` has). Then the plan step logs the argument faithfully, two arms that differ in it get different eval-set identifiers, and `create_registry_object()` can rebuild it. Replay turns registry objects back into objects but leaves pydantic models as dicts, so every factory that takes a pydantic argument also accepts its dict form and validates it, as `swarm()` does for `Member`.

Under this rule, the swarm-wide cap is the limits deep dive's `Budget`, not a bare `Limit` (logged as `"_CostLimit"`), and `Budget` must be a frozen pydantic model, not a frozen dataclass (logged as `"Budget"`). The scoring deep dive's `FinalMode` values (`synthesize(model=...)`, and `reporter("<member>")`) must be pydantic models too, with a discriminator field so that a chain of mixed strings and modes validates back from JSON, and a serialiser and validator for a `Model` field (`registry_value()` writes a `Model` as a model dict, `registry.py:678-684`, but only at top level, not inside a pydantic model). Only string chains were spiked here; the scoring deep dive's spike verified the mixed form, which is built with the chain after M2.

**What the log records but cannot rebuild.** These fall outside the rule, and each has a route that keeps eval sets and retries correct:

| What | Why | Route |
|---|---|---|
| A member agent configured with callables (hooks, filters, tool sources) | Inspect logs a callable as its name ([table](#how-arguments-reach-the-log)) | A registered member builder with ordinary parameters ([Members](#members)) |
| A member agent that must be created inside a sample (inspect_swe's ACP agents) | It cannot be built at task construction at all | A registered wrapper that creates it per invocation ([Members](#members)) |
| `result=` (after M2), the task's `ResultSpec` | It holds the task's callables (answer key, verifier); the scoring deep dive logs it as its type name, `"ResultSpec"` | Rebuild through the task, or through a registered swarm builder (below) |
| `policy=` (M2), the communication deep dive's `CommPolicy` | A plain callable, logged as its name | The same as `result=` |

**Two kinds of reconstruction.** They differ, and the guarantees differ:

- **Through the task.** `eval-retry` and eval-set retries of a task file or registered task re-run the task's code (`src/inspect_ai/_eval/eval.py:1737-1756`), which builds the swarm, its `result=` and its member builders afresh. Everything is rebuilt; this is the supported path for any swarm. (A run that overrode the solver with `--solver` is retried from its solver spec, `:1758-1762`, which is the second path.)
- **From the solver's logged arguments.** `--solver inspect_swarm/swarm -S ...`, inspect_flow's solver specs, or `create_registry_object()` on a plan step rebuild the raw `swarm()` call from its params. This rebuilds the reconstructible subset: member records, controllers, channels, `Budget`, and member agents whose own params are ordinary. `result` comes back as the string `"ResultSpec"`; `swarm()` raises `TypeError` for a string `result` ("a result contract cannot be rebuilt from a log; rebuild the swarm through its task or a registered builder") rather than treating it as `None`, which would silently change the final-answer chain and per-member claims.
- **A registered swarm builder** makes the second path complete: an `@agent` factory with ordinary parameters that returns the configured `swarm(...)`, result contract included. It logs as `{"type": "agent", "name": "math_swarm", "params": {"k": 4}}` and replays by calling the builder.

  ```python
  @agent
  def math_swarm(k: int = 4, cost: float = 40.0) -> Agent:
      return swarm(
          members=member(deepagent(tools=[bash(), python()]), name="solver", count=k),
          budget=Budget(cost=cost),
          result=answer_result(answer_key=boxed_number, verifier=check_answer),
      )
  ```

For eval-set identity, `result` logged as a type name is harmless when the arms share their result contract, which arms of one task normally do; a task whose arms differ in it takes the difference from a task argument ([Task identity](#task-identity-what-makes-two-arms-distinct)). Member callbacks are not harmless (two arms differing only in a hook collide), which is why the builder route is the documented way to configure members with hooks.

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
- **Python sweeps.** `eval_set([task(controller=leaderless(final=f)) for f in ...])` (after M2) and inspect_flow work on solver arguments too, because they are logged faithfully and so give distinct task identifiers.
- **`--solver inspect_swarm/swarm`.** It works through the `@agent` path, but `members=` needs a dict on the command line (`-S 'members={agent: {type: agent, name: react, params: {}}, count: 4}'`), and it cannot carry a result contract. A registered swarm builder (`--solver math_swarm -S k=8`) has neither problem ([Logging and replay](#logging-and-replay)). Tasks are the intended surface.

### Task identity: what makes two arms distinct

An **arm** is one condition of an experiment. In Inspect terms it is one task in an eval set: a `Task` instantiated with its arguments and run with a given solver, model and limits. Inspect's own measure of "one task" is `task_identifier()`, and `eval_set()` refuses two tasks with the same identifier ([Current behaviour](#how-arguments-reach-the-log)). Arms in swarm.md's experiments differ in task arguments (agent count, budget), in the solver (a single agent, a deepagent, a swarm), in the model, or in epochs; the first three are hashed, epochs is not.

A swarm is an agent used as the task's solver, so it is part of the identifier through the plan: `as_solver()` forwards its registry params, and the plan step's `params_passed` is hashed. Everything this API defines is logged faithfully ([Logging and replay](#logging-and-replay)), so a change to any of its arguments, at any depth, changes the identifier. Everything that configures how a swarm runs or is measured:

| What differs between two arms | Reaches `task_identifier()`? | How, and does it matter |
|---|---|---|
| `controller=`, `channels=`, `budget=`, `members=` (count, names, roles, limits, the member agent's own params) | Yes | The plan's `params_passed`, nested registry dicts and pydantic fields included. Tested ([Testing](#testing)). |
| A member agent's callback (hook, filter, tool source) | Only by name | Logged as `__name__`: two arms differing only in such a callback collide. Configure it in a registered member builder with ordinary parameters, whose params are hashed ([Members](#members)). |
| `result=` | No (logged as `"ResultSpec"`) | Arms of one task normally share it. If they differ (a different verifier), the task builds it from a task argument, which `args_hash` covers, or the arms use different registered swarm builders. |
| `policy=` (M2) | Only by name | A `CommPolicy` is a plain callable, logged as `__name__`. A task that varies the policy takes it from a task argument, or sets it inside a registered swarm builder. |
| The task's scorers, headline or per-member | No | Same as every Inspect task: scorers are part of the task, so they distinguish arms only through the task's name or arguments (spike: two otherwise identical tasks with different scorers are "not distinct"). A scorer can change the run: a member with in-loop scoring (`react(attempts=3)` and the other retrying agents in [Scores](#scores-the-task-scores-the-observer-records)) stops or retries on the task scorer's verdict. So a scorer ablation is an arm of its own, selected by a task argument or name. Re-scoring one run's log with `inspect score` is a substitute only when the scorer that changed was not consulted during solving (no member scores in the loop) and does not need the sandbox (the scoring deep dive's re-scoring rules). |
| Sentinel monitors and protocols | No, on `feature/sentinel` | `Task(sentinel=)` and `eval_set(sentinel=)` are not hashed. An eval-set option applies to every arm alike, so it cannot make two arms differ. A monitoring ablation chooses the sentinel from a task argument. This does matter: a protocol changes the run, and without that argument two arms collide, and a re-run of the eval set with a different sentinel matches the earlier logs as the same tasks (`eval_set()` pairs tasks with existing logs by identifier). Flagged under [Not this design](#not-this-design). |
| The observer's record | Not configurable | Arms in one eval set run one installed inspect_swarm, so they write the same record version. |
| Model, model roles, generate config, sample limits | Yes | Hashed by Inspect directly. |
| Epochs | No | An epochs arm differs from a swarm arm in its solver already; two arms that differ only in epochs need different task arguments or names. |

The rule a task author follows is the one `eval_set()`'s error message gives: distinct task names or task arguments, and registered solvers, whose parameters are then hashed. Everything on `swarm()` is covered by the solver's parameters; anything else a swarm experiment varies goes through the task's name or arguments.

### Where the sibling designs' parameters go

| Deep dive (PR) | Its parameter | In this API |
|---|---|---|
| Limits (#5) | `swarm(budget=Budget(...))` | `swarm(budget=)`, unchanged; `Budget` becomes a frozen pydantic model ([Logging and replay](#logging-and-replay)) |
| Limits (#5) | `member(limits=[...], bridged=)` | `member()` fields; limits serialised by kind and value |
| Limits (#5) | `stop(reason)`, soft stop and grace; stop reasons; member statuses | a controller's return value and the own caps go through `stop(reason)`; its reasons and statuses are the public ones ([Controllers](#controllers)) |
| Scoring (#2) | `swarm(final=...)`, a mode or chain | after M2, the controller's `final=`: `leaderless(final=...)`; the chain's semantics and validation are unchanged |
| Scoring (#2) | `swarm(result=...)` | after M2, `swarm(result=)`, unchanged; rebuilt through the task or a registered swarm builder, never from a raw solver spec ([Logging and replay](#logging-and-replay)) |
| Scoring (#2) | `Verdict(passed, value, explanation)` | after M2, controllers see `VerdictSummary(passed, value)`; the explanation stays in the result record |
| Scoring (#2) | `reporter` mode, reserved | valid only on controllers with a reporter; `coordinator(lead=...)` defaults to it |
| Scoring (#2) | `baseline(agent, result)` | after M2, `swarm(members=member(agent), controller=leaderless(final="first"), channels=[], result=result)` |
| Communication (#3) | `channels=["filesystem", messages(...)]` | the same call; `messages` is a `@channel` factory, `"messages"` resolves to it |
| Communication (#3) | `member(delivery=)` | a `member()` field |
| Communication (#3) | `swarm(policy=)` | `swarm(policy=)` from M2, the setting of the observer's interception point; a callable, so it is logged by name and varied across arms through a task argument or a registered swarm builder ([Task identity](#task-identity-what-makes-two-arms-distinct)) |
| Communication (#3) | the `Channel` protocol | M2's operations on `Channel`, with per-sample state moved off the shared object |
| Communication (#3) | `swarm_tools()`, `swarm_on_continue()`, `swarm_bridged_tools()` | unchanged: member-side helpers, not `swarm()` arguments; a member using them is built by a registered member builder so the log can rebuild it |
| ORBIT (#4) | `member(input=, role=, exposure=)` | `member()` fields; `input` is the default for `SwarmControl.start(input=None)` |
| ORBIT (#4) | `ActivationPolicy`, `SwarmControl`, `MemberHandle` | a controller's `run`, and methods added to these handles |
| ORBIT (#4) | `round_robin(max_rounds=)` | a `@controller` |
| ORBIT (#4) | `final="reporter"` by member name | `final=reporter("<member>")`, the scoring deep dive's mode with a member name |
| ORBIT (#4) | `swarm(routes=...)` | `messages(routes=...)`: routes restrict who may address whom, and the bus applies them to that channel's records before the monitor, as the ORBIT deep dive describes |

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
        ),
        scorer=...,
        sandbox="docker",
    )
```

`inspect eval research_math.py -T k=8 -T cost=80` runs eight members, `solver-1` to `solver-8`, in the sample's sandbox, with the filesystem channel, under a cost cap of 80 (dollars), each started with the sample's input and its preamble; it stops when all have ended, and the final answer is the first submission. After M2, with the scoring deep dive's `result=answer_result(answer_key=boxed_number, verifier=check_answer)`, the default answer is scoring's default chain. The explicit form of the same arm after M2, with a strict verifier-only answer and the swarm stopping at the first verified answer:

```python
swarm(
    members=member(deepagent(tools=[bash(), python()]), name="solver", count=4),
    controller=leaderless(final="verify", stop_on_verified=True),
    channels=[filesystem(notes="notes.md")],
    budget=Budget(cost=40.0),
    result=answer_result(answer_key=boxed_number, verifier=check_answer),   # verify needs a verifier
)
```

The built-in it uses, as it is after M2. In M1 it has no parameters, starts every member and returns `all_done`:

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
        return "all_done"

    return Controller(run, final=final)
```

**A coordinator swarm (later: coordinator topologies, on M2's messages).** A lead and three workers. All start together; workers wait for the lead's instructions with `read_messages(wait_seconds=...)`, and the lead's submission is the answer. Members with hooks and tool sources are registered builders, so the log can rebuild them ([Members](#members)).

```python
from inspect_ai.agent import Agent, agent, react
from inspect_ai.tool import bash, python
from inspect_swarm import coordinator, member, messages, swarm, swarm_on_continue, swarm_tools


@agent
def lead(prompt: str = LEAD_PROMPT) -> Agent:
    return react(prompt=prompt, tools=[bash(), swarm_tools()], on_continue=swarm_on_continue())


@agent
def worker(prompt: str = WORKER_PROMPT) -> Agent:
    return react(prompt=prompt, tools=[bash(), python(), swarm_tools()], on_continue=swarm_on_continue())


swarm(
    members=[member(lead(), name="lead", role="coordinator"), member(worker(), name="worker", count=3)],
    controller=coordinator(lead="lead"),
    channels=["filesystem", messages(delivery="notify")],
)
```

The controller, as it would be built in the coordinator-topologies work:

```python
@controller
def coordinator(lead: str = "lead", final: FinalSpec | None = None) -> Controller:
    """Start everyone; the swarm is done when the lead ends."""

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async for event in swarm.events():
            if isinstance(event, MemberEnded) and event.member == lead:
                return "lead_done"       # soft-stops the workers still running
        return "all_done"

    def check(roster: Sequence[MemberInfo]) -> None:
        if lead not in [m.name for m in roster]:
            raise ValueError(f"coordinator: no member named {lead!r}")

    return Controller(run, final=reporter(lead) if final is None else final, check=check)
```

Workers start with the lead because the communication deep dive delivers only to running recipients (PR #3, "Commit checks": liveness). Starting a member on its first message would need delivery to pending members, which that design rejects; a topology that wants it brings that extension in its own design. A worker that has submitted cannot be woken again until persistent members exist (swarm.md, [Members](swarm.md#members)). `lead_done` is a topology reason the coordinator work adds to the limits deep dive's set; until then it would be recorded as `controller:lead_done`.

**A user-defined controller** (after M2: it needs the scoring deep dive's verifier). Adaptive compute: run one cheap member alone, and start the rest only if its answer does not verify.

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
                    return "verified"
                for m in swarm.members:
                    if m.state == "pending":
                        swarm.start(m)
        return "all_done"

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

## Alternatives considered

**Closed string settings** for the topology and the final answer, and a fixed list of channel names. The least machinery. There is no way to add a topology or a channel without changing inspect_swarm, the final-answer chain has to be validated against a separate `topology=`, and ORBIT's scheduled mode needs a protocol of its own anyway. Rejected.

**Plain config objects and protocols, no registry** (deepagent's `Subagent`; the ORBIT deep dive's `ActivationPolicy` passed as an object). No inspect_ai change. But a user's controller cannot be named from `-T` or a config, parameters are not captured automatically, and an object that is not a pydantic model is logged as its type name, which makes otherwise different arms "not distinct" in `eval_set` (spike). Rejected for controllers and channels; adopted for members, where the object being named (the `Agent`) is already in the registry.

**Register controllers and channels under existing types** (`solver` or `agent`, with metadata saying what they are). No inspect_ai change. But `--solver leaderless` would then resolve to a controller and fail obscurely, and anything that lists solvers or agents would list controllers. Rejected; the literal entry is one line with precedent.

**One `swarm_component` registry type for all components.** One literal entry instead of two. But a name could then resolve to the wrong kind (`channels=["leaderless"]`), so every lookup needs a second check, and the decorator name no longer says the type. Rejected.

**Packages declare their own registry types** (an inspect_ai change that opens `RegistryType` to types registered by installed packages). Satellites would stop needing an inspect_ai PR per type. But the closed set is load-bearing: pydantic validates `RegistryInfo.type` against it, and `is_registry_dict()` uses it during replay to decide whether a logged dict is an object to rebuild or plain data (`registry.py:647-657`). An open set needs a registration API, a rule for when replay may trust a type a package declared (loaded through its entry point before any lookup), and its own review, for a problem that a one-line PR with precedent already solves. Not chosen (decision: Ransom, 2026-10-08, who took the one-line PR); it is an inspect_ai design of its own if more satellites need types.

**Namespaced type names** (`swarm_controller`, `swarm_channel`). Safer if inspect_ai ever wants a `channel` type of its own (its `AgentChannel` is not a registry object today). Scout and sentinel use plain names (`scanner`, `monitor`, `protocol`), and the decorators are `@controller` and `@channel` either way. Plain names are the plan, and the choice is left to the inspect_ai PR's review (decision: Ransom, 2026-10-08).

**Hook-style controllers** (a class with `on_start`, `on_member_ended`, `should_stop` methods, as Inspect's `Hooks` are). Declarative and harder to misuse. But sequencing (escalate, waves, ORBIT's plans) becomes a state machine spread across callbacks, and Inspect's class registration does not capture constructor parameters into the log the way factory decoration does. Rejected in favour of one `run` coroutine over an event stream.

**A factory field on `member()`** (`member(factory=lambda: react(...))`) instead of registered member builders. It would also rebuild hooks per invocation, but the factory is a callable, so the log records only its name and replay cannot call it. A registered `@agent` builder uses Inspect's own registry and logging, and needs nothing new. Rejected.

**Controllers that run members themselves** (`await member.run()` in an anyio task group). Familiar to anyio users. But the controller would then own member tasks, so drain and cap recovery would depend on every controller cancelling correctly, and a controller bug could leave a member running during finalisation. Rejected: the runtime owns member tasks, and the controller only starts them.

**`final=` stays on `swarm()`** (the scoring deep dive's draft). Keeps scoring's API as drafted. But the valid modes and the default depend on the topology, which is the controller, so `swarm()` would have to cross-validate two arguments that belong together. Rejected; scoring's chain and its semantics are unchanged, only where it is passed.

**An observer argument or `@observer`.** Would let an arm change what is recorded: drop an evidence kind, change the ledger's coverage rules, add a record of its own. The record is read as a contract by scorers and analysis, and baseline arms write it through `baseline()`; a configurable record would make every field optional for every reader, and an arm with a reduced ledger could not be compared on realized cost. What users vary in watching and measuring a run (monitors, scores, labels) is already configured through sentinel, the task's scorers and Scout ([Observer](#observer)). Rejected. A new evidence kind, if one is needed, is an additive, versioned change to the record, not a per-arm switch.

**One `SwarmSpec` object** holding all components (`swarm(SwarmSpec(...))`). One serialisable record. Inspect's APIs are keyword-first (`Task(...)`, `react(...)`), and a spec object is one more layer for the common case. Rejected.

## Compatibility and migration

- **inspect_swarm** has no released API ([swarm.md](swarm.md#compatibility-and-migration)). The sibling deep dives' parameters move as [the table](#where-the-sibling-designs-parameters-go) says; each deep dive applies the move when it is next revised or implemented.
- **inspect_ai.** Two entries in `RegistryType`. They are additive: nothing in inspect_ai uses the type set except `RegistryInfo`'s validation and `is_registry_dict()`, which then recognises two more kinds of logged object, and the literal is not in the log schema or the generated TypeScript types (verified above). inspect_swarm needs the inspect_ai version that has them; it tracks inspect_ai `main`, and the release floor pinned by `release-pin-deps.yml` covers released versions. Imported with an older inspect_ai, inspect_swarm's decorators fail at import with pydantic's `ValidationError` on `RegistryInfo`.
- **Registry dict shapes.** With the new types, any dict of exactly the shape `{"type": "controller" | "channel", "name": str, "params": dict}` inside replayed arguments is rebuilt as an object instead of staying data (`is_registry_dict()`, `registry_arg()`, `registry.py:647-701`). No existing caller using those shapes was found in inspect_ai, inspect_sentinel, inspect_scout or inspect_flow; the inspect_ai PR states this reservation in its compatibility note.
- **Eval logs.** The swarm's plan step gains nested `{"type": "controller" | "channel", "name", "params"}` objects and member dicts. These are ordinary JSON inside `params`, which is `dict[str, Any]` (`src/inspect_ai/log/_log.py:705-718`), so every log reader accepts them. An inspect_ai that lacks the types treats them as plain dicts on replay; replaying a swarm log needs inspect_swarm installed anyway.
- **Documents.** swarm.md's section on how members share information is now `swarm.md#channels`; the sibling deep dives' links follow on merge ([appendix](#changes-to-swarmmd-and-the-overview)).

## Security

- **Names from the command line or a log.** `-T`, `-S` and replay hand strings and dicts to name resolution, which only finds registered objects. A `package/name` loads that installed package's declared entry point, which `--solver` already does today (`registry.py:289-297`). Logs replayed with `eval-retry` are trusted input, as they already are for solvers. No new exposure.
- **Controller and channel code is trusted eval-author code**, like solvers. It is never reachable from member tools, and members cannot select a controller or a channel.
- **No model text reaches a controller.** Handles and events carry states, statuses, member names chosen by the eval author, and a verdict's pass flag and numeric value; never a member's output, a message payload or a verifier's explanation, which may quote the answer or a judge model ([Controllers](#controllers)). So a controller cannot relay model output into another member's input. `start(input=...)` takes the eval author's text; its documentation says never to put member output there.
- **The preamble** contains the member's name and role, the roster size, and channel instructions, all from the eval author. Nothing a member wrote enters it.
- **Shared tool state.** Members cannot pick a tool's `instance`; the eval author does, in the member's agent. The default is per member, and a shared instance is a channel the bus does not see, observed like the filesystem (swarm.md, [Security](swarm.md#security)).
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
  - raw-solver replay: `create_registry_object()` on the params of a swarm without `result=` rebuilds an equivalent swarm whose params are equal; with `result=` (after M2) it raises the documented `TypeError`, never substituting `None`;
  - task replay (after M2): retrying a task-file swarm with a verifier result rebuilds the result contract and selects the same final answer (a separate case from raw-solver replay);
  - member builders: two `worker(prompt=...)` builders with different prompts log differently, get different `eval_set` identifiers and rebuild with their `on_continue` hook intact; a registered swarm builder (`math_swarm`) replays through `--solver`-style params (after M2, with its result contract);
  - `eval_set` accepts two arms that differ only in a controller parameter, a channel parameter, a member's `count` or `limits`, or `Budget`, and assigns them different identifiers;
  - (after M2) a final chain mixing strings and pydantic `FinalMode` values (including a `synthesize` with a model) validates back from its logged JSON;
  - `-T channels=filesystem` (a string) and `-T channels=filesystem,messages` (a list) both resolve.
- `tests/test_controller.py`:
  - `leaderless` starts every member and returns `all_done`; after M2, with `stop_on_verified` it returns `verified` at the first passed verdict;
  - (after M2) a delayed verifier: the last member's agent returns before its verdict is taken, and `events()` still yields that member's `MemberEnded` with the verdict before it ends, so `stop_on_verified` and `escalate` see it;
  - a custom controller (`escalate`) starts members later and with custom input; `start()` twice raises; `events()` ends when nothing runs and a second concurrent iterator raises;
  - returning while members run goes through the soft stop: in-flight mockllm calls complete and are recorded, members get status `stopped`, and only a member still running after `Budget.grace` is cancelled; the swarm cap and time cap take the same path with reasons `swarm_cap` and `swarm_time`, and `run` is cancelled;
  - stop ordering, with barriers: a controller that runs a scout first (`escalate` after M2; in M1 one that starts the workers when the scout ends) runs its scout while the time cap (and, separately, the swarm cap) fires; the scout ends unverified during grace. No pending worker starts (a `start()` attempted after the stop began raises `RuntimeError`), no member model call is dispatched after the stop began, cancelling `run` does not cancel the scout's member task, and the never-started workers are recorded as not started;
  - an exception in `run`, or a foreign member error, cancels the rest at once and propagates; a member's own limit ends that member with a `member_limit:<type>` status and the swarm continues;
  - a custom reason is recorded as `controller:<reason>`;
  - no member text: a member that outputs a distinctive string, and after M2 a verifier whose explanation echoes it, and the string appears in no event or handle field;
  - one swarm object serves two concurrent samples with no shared controller state;
  - each member runs in its own tool-state scope: members with `memory()`, and the copies of a counted member, each see only their own memory files, while members given `memory(instance="team")` share them;
  - `check` errors, and after M2 an invalid `final` (including `final="verify"` without a verifier), raise at `swarm()` construction.
- `tests/test_scoring_loop.py`: in M1, only the trajectory case described last; after M2, with the scoring deep dive's `member_scores`, also its in-loop guard test run inside a swarm. Members are `react(attempts=3)` and the task lists `member_scores` after its headline scorer; each member gets the same feedback and the same number of task-scorer calls as with the headline scorer alone, and `member_scores` returns `None` while `completed=False`. A second case shows that a scorer change is a trajectory change: with identical mockllm submissions, a passing task scorer ends a member after one call and a failing one makes it retry, and the two arms are distinct in `eval_set` only when the scorer is chosen by a task argument.
- `tests/test_channel.py`: `filesystem()`'s instructions appear in each member's preamble with that member's scratch directory; `channels=[]` adds none; a duplicate channel raises.
- `tests/test_member.py`: count expansion and names; duplicate names raise; unregistered agents and `deepagent(background=True)` are rejected; `Member` validates from its own logged dict.

In inspect_ai, the registry PR adds a test to `tests/util/test_registry.py` that registers, looks up and round-trips (`is_registry_dict()`) one object of each new type, as inspect_ai#4359 did for `validation_predicate`. The tool-state scope PR adds `tests/util/test_tool_state.py` (an explicit instance is returned as given; `None` resolves to the scope, to `None` outside one, and to `outer/inner` in nested scopes; the scope does not leak out of its block or into a sibling task) and cases in `tests/tools/test_memory.py` (two scopes in one sample keep separate files; no scope keeps today's keys).

## Implementation plan

1. **inspect_ai: two registry types.** Add `"controller"` and `"channel"` to `RegistryType`, a test in `tests/util/test_registry.py`, and a CHANGELOG line. Files: `src/inspect_ai/_util/registry.py`, `tests/util/test_registry.py`, `CHANGELOG.md`. Before M1, with plain names unless that PR's review asks for namespacing (decision: Ransom, 2026-10-08).
   With it, if swarm.md's [open question 2](swarm.md#open-questions) takes the recommendation, a second small PR: **the tool-state scope**. `tool_state_scope()` and `tool_state_instance()` in a new `src/inspect_ai/util/_tool_state.py`, exported from `inspect_ai.util`, and used by `memory()`, `bash_session()`, `web_browser()` and the skill tool. Files: that module, `src/inspect_ai/util/__init__.py`, `src/inspect_ai/tool/_tools/_memory.py`, `_bash_session.py`, `_web_browser/_web_browser.py`, `_skill/tool.py`, `tests/util/test_tool_state.py`, `tests/tools/test_memory.py`, `CHANGELOG.md`.
2. **inspect_swarm: registry plumbing** (M1). The decorators, `Controller`, `Channel`, `MemberInfo`, `resolve()`, and the entry point. Files: `src/inspect_swarm/_controller/_controller.py`, `src/inspect_swarm/_channel/_channel.py`, `src/inspect_swarm/_registry.py`, `src/inspect_swarm/_entrypoint.py`, `pyproject.toml` (`[project.entry-points.inspect_ai]`), `tests/test_registry.py`.
3. **Members and the `swarm()` signature** (M1). `Member` and `member()` with their serialisers; `swarm()`'s arguments, validation and resolution; exports in `src/inspect_swarm/__init__.py`. Files: `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/__init__.py`, `tests/test_member.py`, `tests/test_api_log.py`.
4. **The controller surface in the runtime** (M1). `SwarmControl`, `MemberHandle`, `MemberEnded`, `events()`, `start()` and the preamble, each member's task entered in its tool-state scope, in the M1 runtime the limits and scoring deep dives describe; the built-ins `leaderless` and `filesystem`. Files: `src/inspect_swarm/_controller/_control.py`, `src/inspect_swarm/_controller/leaderless.py`, `src/inspect_swarm/_channel/filesystem.py`, `src/inspect_swarm/_swarm.py`, `tests/test_controller.py`, `tests/test_channel.py`, `tests/test_scoring_loop.py` (its M1 case).
5. **M2.** `messages` as a `@channel`, the communication deep dive's operations on `Channel` with per-run state, and `swarm(policy=)`, which raises `TypeError` for the string a raw-solver replay passes, as `result=` does (tested in `tests/test_api_log.py`).
6. **Later.** The scoring deep dive's `result=`, `final=`, `stop_on_verified` and verdicts, with its post-M2 work and the `member_scores` case of `tests/test_scoring_loop.py` ([swarm-scoring.md](swarm-scoring.md#implementation-plan-after-m2)); `coordinator` with the coordinator topologies (and the `lead_done` reason); new event kinds with persistent members; the ORBIT deep dive's scheduled mode as controllers.

Steps 2 to 4 are the skeleton of M1, not a separate milestone: swarm.md's M1 builds its runtime behind this surface.

## Open questions

None open. Both questions this design raised are decided (Ransom, 2026-10-08), each taking the recommendation:

1. **The component's name** is **Channels**: "channels is fine for now". The name may be revisited later; the names compared are in the [appendix](#why-the-component-is-called-channels).
2. **An inspect_ai PR before M1.** "a is fine": a one-line PR adds `"controller"` and `"channel"` to `RegistryType`, with a test as inspect_ai#4359 had, and lands before M1. Plain names, with namespacing left to that PR's review ([Implementation plan](#implementation-plan), step 1). The alternative of letting packages declare their own registry types is under [Alternatives](#alternatives-considered).

## Not this design

- **Limits and frozen dataclasses log poorly in inspect_ai.** A `Limit` argument is logged as its class name (`"_CostLimit"`), or `null` inside a list; a frozen dataclass as its type name, which makes otherwise different eval-set arms "not distinct". This affects any agent or solver (deepagent's `Subagent.limits`, for example). inspect_ai could serialise dataclasses at top level as it already does inside lists, and limits by kind and value.
- **A dict form for controller parameters on the command line** (`-T 'controller={name: escalate, first: scout}'`).
- **Grid sweeps from the CLI** (`-T k=2,4,8` as three runs) belong to eval-set or inspect_flow, not to inspect_swarm.
- **A generic swarm task** (`inspect_swarm/swarm_task`) that wraps any dataset, so arms can be swept with no task code.
- **Listing controllers and channels** (`inspect list`-style discovery of registered names).
- **Sentinel configuration is not part of `task_identifier()`** (inspect_ai `feature/sentinel`). Two tasks that differ only in `Task(sentinel=...)` are "not distinct", and a re-run of an eval set with a different sentinel matches the earlier logs. Sentinel changes how a run behaves, so it arguably belongs in the identifier's additional hash, as the sample limits are. Until then a monitoring ablation takes the sentinel from a task argument ([Task identity](#task-identity-what-makes-two-arms-distinct)).

## Appendix: how this design changed the earlier documents

History, kept for readers of the earlier drafts; the body above describes the API as it is now.

### The request

Ransom's direction (2026-10-08): "it might be better to have the API shape better match the Components. For example by having a new registry object (`@controller`) that is used instead of just topology and final as scalars. Similar for other components - although substrate is potentially a problematic name - maybe there is something better?"

### The replaced sketch

swarm.md's [Entry point](swarm.md#entry-point) first had an illustrative sketch:

```python
swarm(
    members=member(deepagent(...), count=4),
    topology="leaderless",
    channels=["filesystem"],
    budget=cost_limit(40.0),
    final="verify",
)
```

`topology=` picked from a closed list, `final=` was a separate setting validated against it, and `budget=cost_limit(40.0)` is logged by inspect_ai as `"_CostLimit"`. The [Why](#why) section gives the requirements this design meets instead. The sketch never shipped.

### Why the component is called Channels

swarm.md first called the component *substrate*. The word was obscure, and swarm.md also uses it in its ordinary sense ("aim to be a substrate it could run on", "eval-oriented swarm substrates", "Make ORBIT the substrate"), so the component's name and the generic word collided. Those ordinary-sense uses stay: once the component is called channels, the word no longer means two things. The names compared:

| Name | For | Against |
|---|---|---|
| **Channels** (chosen: decision: Ransom, 2026-10-08, "for now") | Already the argument (`channels=["filesystem"]`) and the column heading of swarm.md's channel table. ORBIT's vocabulary (channels with readers and writers) and Codex's board. The communication deep dive's `Channel` protocol already uses it. swarm.md already calls the filesystem an implicit channel. | inspect_ai has an unrelated `AgentChannel`, the per-execution queue that delivers notices. The documents must say "agent channel" for that one, as swarm.md's [The agent channel](swarm.md#the-agent-channel) already does. |
| Comms / communication | Plain; matches the communication deep dive's title. | The filesystem, a task list and a board are shared state more than messages; informal as an API word. |
| Shared state | Accurate for the filesystem, notes, tasks and board. | Wrong for direct messages, which are not shared. |
| Commons | Names what members hold in common without implying messaging. | Unfamiliar in eval APIs; one more term to learn. |
| Blackboard | An established multi-agent term (Terrarium's blackboards). | A classic architecture with its own control model; implies one shared store, not addressed messages. |
| Workspace | Familiar. | Already means the sandbox's working directory and git worktrees; suggests the filesystem only. |
| Medium, fabric, network, mesh | Neutral. | Vague, and "mesh" already names a topology (Architecture Matters' `mesh_round_robin`). |
| Substrate (keep) | No churn. | Obscure, and it collides with swarm.md's ordinary use of the word. |

### Changes to swarm.md and the overview

Made in this PR, limited to the API and the name:

- swarm.md [The shape](swarm.md#the-shape): component 2 is **Channels**; components 2 and 3 name `@channel` and `@controller` and link here.
- swarm.md [Entry point](swarm.md#entry-point): the sketch above became this API's common case and explicit form, linking here for the rest.
- swarm.md's "Substrate" section: renamed **Channels** (anchor `#channels`), with one sentence on `@channel`. Its two in-document links are updated.
- swarm.md [Controller](swarm.md#controller-topology-termination-final-answer): topologies are controllers; `final=` is the controller's parameter; a note that the runtime, not the controller, owns drain, exhaustion, verification and finalisation.
- swarm.md [Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply): a definition of *arm*.
- swarm.md [Where each part lives](swarm.md#where-each-part-lives) and [Compatibility](swarm.md#compatibility-and-migration): the two registry types in inspect_ai.
- swarm.md M1 in the [Implementation plan](swarm.md#implementation-plan): M1 needs the one-line inspect_ai registry PR first (until this design, swarm.md said M1 needed no inspect_ai change), and builds the API skeleton described here.
- swarm-overview.md: the component table and code sample, the observer, a definition of *arm*, M1's inspect_ai note, and the inspect_ai row of "Where each part lives".

The sibling deep dives link `swarm.md#substrate` (the communication and ORBIT deep dives); whichever of them merges after this PR updates the link to `swarm.md#channels`.
