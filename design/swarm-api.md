# Inspect Swarm: the `swarm()` API

Status: proposed, 2026-10-08; amended 2026-10-14 so that no agent object runs twice at once in one sample ([Members](#members)). Issue: none. Author: agent (Claude), reviewed by Codex; see the PRs.

## Overview

`swarm()` is an Inspect agent whose arguments mirror a swarm's components. `members=` lists the agents: built agents, or `member()` records that add a name, role, limits or `count`; `controller=` chooses how the swarm runs (who starts, when it is done, how the answer is chosen); `channels=` chooses how members share information; and `budget=` caps the whole swarm's spend. What the swarm records is fixed, so it has no argument. Controllers and channels are Inspect registry objects, so a task can name them as `-T` parameters, an eval set can sweep them, and every argument lands in the log faithfully enough to tell two experiment arms apart and to rebuild the swarm.

The swarm guarantees that no agent object runs twice at the same time in one sample. A built agent (`swarm(members=[react(...), deepagent(...)])`) is one member, and is shared across concurrent samples as any Inspect solver is; the same object cannot appear in two members. Several copies of one configuration (`count`) take the factory form, `member(deepagent, args={"tools": [bash()]}, count=4)`: the swarm calls the factory once for each member in each sample, inside that member's own task, so each copy gets its own agent and factory-created state. Objects supplied through `args`, or given to a built agent, remain shared; construct stateful tools and helpers inside a factory to give each member its own. These are Ransom's decisions (2026-10-14).

A deeper dive on one topic of [swarm.md](swarm.md), the high-level design: the public shape of `swarm()`. Its arguments mirror the four components (members, channels, controller, observer). Controllers and channels are Inspect registry objects (`@swarm_controller`, `@swarm_channel`), selectable by name, with their parameters in the log.

It respects the decisions recorded in swarm.md (Ransom, 2026-10-07): Python 3.11+, inspect_ai internals may be used, inspect_ai limits stay soft, M1 then M2 then the rest in any order, members share the sample sandbox by default, monitoring aligns with inspect_sentinel, red-team features are optional, peer messages are model output delivered as tool output with distinct provenance, and the transcript representation is open. It records two of its own (decision: Ransom, 2026-10-08; [Open questions](#open-questions)): the component is called **Channels**, and a one-line inspect_ai PR adding two registry types lands before M1. Others, of 2026-10-14, are that no agent object runs twice at once in one sample: a member is a built agent or an `@agent` factory with an `args` dict, and copies (`count`) need the factory ([The member contract](#the-member-contract)). Its sibling deep dives are [swarm-limits.md](swarm-limits.md), [swarm-scoring.md](swarm-scoring.md), [swarm-communication.md](swarm-communication.md) and [ORBIT on inspect_swarm](https://github.com/meridianlabs-ai/inspect_swarm/pull/4), an open draft cited at its head on 2026-10-08. This document says where their parameters go in the API ([table](#where-the-sibling-designs-parameters-go)) and does not design their semantics.

Code references are to inspect_ai `main` at `fccfb298` (2026-10-07, the version this repository's lockfile installs), inspect_ai `feature/sentinel` at `eff42b6f`, inspect_sentinel at `c8cd71d` and inspect_scout at `d746d2a3` (both 2026-10-07). Paths are relative to each repository's root.

## The API at a glance

Three shapes determine everything else, and are what to review first: `swarm()`'s signature, the controller interface (what a controller can do and see) and the channel interface. Signatures only; the rules are in the sections linked below.

```python
@agent
def swarm(
    members: Member | Agent | Sequence[Member | Agent],    # member(agent, name=, ...) or member(factory, args=, count=, ...)
    *,
    controller: str | Controller = "leaderless",            # a name, or a @swarm_controller object
    channels: str | Channel | Sequence[str | Channel] = ("filesystem",),   # names or @swarm_channel objects
    budget: Budget = Budget(),                              # the swarm-wide cap (limits deep dive)
    result: ResultSpec | None = None,                       # the task's result contract (scoring deep dive; after M2)
) -> Agent: ...

# Controllers: how the swarm runs. A @swarm_controller factory returns a Controller.
@swarm_controller
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
    def events(self) -> AsyncGenerator[SwarmEvent, None]: ...  # MemberEnded in M1

# Channels: how members share information. A @swarm_channel factory returns a Channel.
@swarm_channel
def filesystem(shared: str = "/shared", notes: str = "notes.md", scratch: str = "/scratch/{member}") -> Channel: ...

class Channel:
    def instructions(self, member: MemberInfo) -> str | None: ...   # M1: text for the member's preamble
    # M2: name, prefix, reach, check() and open(), which returns a per-run ChannelState with the
    # communication deep dive's operations (tools, validate, admit, apply, notice_lines, quiescent)
```

- **Members** are built agents or pydantic `member()` records. A record holds a built agent, or a registered `@agent` factory and its arguments: `member(deepagent, args={"tools": [bash()]}, count=4)`, which the runtime builds once for each member in each sample. `count > 1` needs the factory form, and one agent object cannot be in two members, so no agent object runs twice at once in one sample ([Members](#members)).
- **The controller** decides which members start, when and with what input, when the swarm is done, and the final-answer chain. inspect_swarm's fixed runtime owns everything else: member tasks, limits, stopping, verification, finalisation and the record. A controller sees states, statuses and a verdict's pass flag and value, never member text ([Controller and runtime](#controller-and-runtime), [Controllers](#controllers)).
- **Scoring's parts follow M2.** M1 builds this API without the scoring deep dive's post-M2 parts: `result=`, `leaderless()`'s `final` and `stop_on_verified`, `Controller(final=)`, and the `verdict` on handles and `MemberEnded`. M1's final answer is always `first` (decision: Ransom, 2026-10-08; [swarm-scoring.md](swarm-scoring.md#what-m1-leaves-out-of-the-merged-api)). Each is added later as a keyword with a default that keeps M1's behaviour, or as a field that defaults to `None`.
- **Channels** add instructions to each member's preamble in M1, and tools and delivery from M2 ([Channels](#channels)).
- **The observer** has no `observer=` argument: what it records is fixed and versioned. Monitors (inspect_sentinel), scores (the task's scorers) and labels (Scout) are configured where Inspect configures them; in M2 sentinel is the only monitor, and the communication deep dive's bus hook is later work: a sentinel adapter, or a `policy=` setting on `swarm()` if sentinel still cannot run on a synthesized step ([Observer](#observer)).
- **Names, logging and task identity.** A string resolves to a registered controller or channel with its defaults ([Resolving names](#resolving-names)). Every argument this API defines is logged faithfully ([Logging and replay](#logging-and-replay)). An **arm** is one condition of an experiment; in Inspect terms, one task in an eval set: a `Task` instantiated with its arguments and run with a given solver, model and limits, which Inspect tells apart by `task_identifier()`. A swarm is the task's solver, so its arguments, including the nested controller, channel and `Budget` parameters and each member's factory and arguments, are in the plan step's `params_passed`, which `task_identifier()` hashes: two arms that differ in any of them are distinct in an eval set ([Task identity](#task-identity-what-makes-two-arms-distinct)).
- **Two inspect_ai prerequisites before M1**: `"swarm_controller"` and `"swarm_channel"` added to inspect_ai's `RegistryType` (decision: Ransom, 2026-10-08; [The registry](#the-registry); the prefix: [The shape of the call](#the-shape-of-the-call)), and the tool-state scope (decision: Ransom, 2026-10-09; [Members](#members)).

## Why

The API's shape matches the components, with a controller registry object in place of scalar topology and final-answer settings, and the same for the other components where it fits (decision: Ransom, 2026-10-08). The API has to provide:

- **An extension point for how a swarm runs.** Research arms run swarms differently: a cheap member first and the rest only if it fails, members started in waves, ORBIT's scheduled rounds. A closed list of topology names has nowhere to put them; the ORBIT deep dive defines its own `ActivationPolicy` protocol for exactly this (PR #4, "P4. The turn seam and activation policies"). Channels need the same: a way to add one without changing inspect_swarm.
- **The component boundaries in the call.** The final-answer default and the valid modes depend on the topology (`reporter` for coordinators, the verify/vote/first chain for leaderless swarms; swarm.md, [Final answer](swarm.md#controller-topology-termination-final-answer)). Passed as two separate settings, they must be validated against each other; held by one controller object, they cannot disagree.
- **Arguments that reach the log faithfully.** Inspect logs an agent's arguments by its own rules ([How arguments reach the log](#how-arguments-reach-the-log)). Against the installed inspect_ai, an argument `cost_limit(40.0)` is logged as the string `"_CostLimit"`, and inside a list as `null`; a frozen dataclass is logged as its type name. Two `eval_set` arms that differ only in such an argument get the same task identifier, and `eval_set` refuses them: "The task 'arm' is not distinct." (spike below). Sweepability is a goal of swarm.md ("agent count, topology, communication channels and budget split ordinary, sweepable task parameters"), so this matters.
- **Members that do not share an agent.** Inspect builds a task's agent once and runs it for every sample, so an agent must keep per-sample state in its invocation ([Current behaviour](#registry-objects-are-shared-by-concurrent-samples)). A swarm asks more: `count=4` runs one record four times at once in the same sample, sandbox and store. State an agent's factory keeps in its closure (hooks, caches, tool objects, a session id) is then shared by the copies, silently. inspect_swe's `claude_code()` is a real case: it allocates its Claude Code session id once per agent object, so four copies of one object would run `claude --session-id` with the same id in one sandbox. Sharing a built agent across samples is how Inspect runs every solver, since samples have their own sandboxes and stores; running one object twice at once in a sample is the new hazard. This amendment closes it in the API instead of documenting it: copies come from a factory, and the same object cannot be two members (raised by a Meridian colleague; decision: Ransom, 2026-10-14).
- **One place for the sibling deep dives' parameters.** Each adds parameters to `swarm()`: `budget=Budget(...)`, `result=`, a `final=` chain, `messages(...)` in `channels=`, a later `policy=`, `routes=`, `member(delivery=, bridged=, input=)`, and an activation-policy protocol. Without one shape for the call, those land as a flat list of keyword arguments with no rule for which component each belongs to or how it is logged.

## Goals and non-goals

### Goals

- `swarm()`'s arguments mirror the components: `members=`, `channels=`, `controller=`. The observer has no component argument ([why](#observer)).
- The components users extend are Inspect registry objects, with Inspect's conventions: decorated factories, package-qualified registry names, creation parameters captured into the log, lookup by name, and an `inspect_ai` entry point. Those are controllers (`@swarm_controller`) and channels (`@swarm_channel`).
- The common case stays one line, and the arguments work as task parameters (`-T`), in a Python `eval_set` sweep and in the log. Names resolve to registry objects. Everything this API defines (member records, controllers, channels, the budget) is logged faithfully and can be rebuilt from the log. What it cannot rebuild alone (a member factory given callables, the task's result contract) has a stated route: a registered builder or the task ([Logging and replay](#logging-and-replay)).
- One place for each sibling design's parameters.
- No agent object runs twice at the same time in one sample: a built agent is one member, and copies are built from a factory per member per sample. Callers can still pass agents they built themselves, without `member()` ([The member contract](#the-member-contract); decision: Ransom, 2026-10-14).
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

- **Registry types are a closed `Literal`.** `RegistryType` lists every type (`src/inspect_ai/_util/registry.py:41-60`), and `RegistryInfo.type` is validated against it by pydantic (`:72-80`). Constructing `RegistryInfo(type="swarm_controller", name="x")` against the installed inspect_ai raises `ValidationError`.
- **Satellite packages add their types to inspect_ai.** Scout's `loader`, `scanner` and `scanjob` came in inspect_ai#2556 (2025-10-04). `validation_predicate` came in inspect_ai#4359 (2026-07-21), which changed one line of `registry.py` and added a test and a CHANGELOG line. inspect_sentinel's `monitor` and `protocol` came with sentinel's dispatcher (`RegistryType` lists both at inspect_ai `be4fd1fba`, 2026-10-14).
- **The literal is not in the log schema.** inspect_ai's generated OpenAPI schema (`src/inspect_ai/_view/inspect-openapi.json`) does not contain the registry type names; `scanjob` occurs nowhere in it. So adding a type changes no log format and no generated TypeScript type.
- **The decorator pattern.** `@agent` (`src/inspect_ai/agent/_agent.py:162-212`) and sentinel's decorators (`inspect_sentinel: src/inspect_sentinel/_decorators.py:230-304`) do the same thing:
  - compute the registry name with `registry_name()`, which prefixes the installed package's name, so a factory in inspect_swarm is `inspect_swarm/<name>` (`registry.py:252-259`);
  - wrap the factory; the wrapper calls it, checks what it returned, and tags the returned object with `registry_tag(factory, instance, info, *args, **kwargs)`, which binds the call's arguments to the factory's signature and stores them as the object's registry params (`:151-183`);
  - register the wrapper with `registry_add()` (`:127-148`).
- **Lookup by name.** `registry_lookup(type, name)` tries the name as given, then `inspect_ai/<name>` for a bare name (`registry.py:276-287`). For a `package/name` it loads that package's `inspect_ai` entry point first (`:289-297`). inspect_sentinel declares one (`inspect_sentinel: pyproject.toml`, `[project.entry-points.inspect_ai]`); inspect_swarm does not yet.
- **`registry_create()`** instantiates a registered callable only when its return annotation's name, lower-cased, equals the registry type (`registry.py:436-449`); otherwise it returns the factory itself. `create_registry_object()`, which replay uses, instantiates any registered callable (`:452-467`). inspect_ai's own types that no class name lower-cases to are created that way: `score_reducer` (`src/inspect_ai/scorer/_reducer/registry.py:151`) and `task_source` (`src/inspect_ai/_eval/registry.py:275`). A spike with a factory annotated `-> Controller` and registered under a simulated `swarm_controller` type confirmed both: `registry_create()` returned the function, `create_registry_object()` the `Controller`.

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

A task's plan is built once, and every sample runs the same objects (`src/inspect_ai/_eval/task/run.py:2831`, `await plan(state, generate)`). So an agent, solver, controller or channel object is shared by all concurrently running samples, and anything per-sample must live in the call, not on the object. `handoff()` and `as_tool()` likewise take a built agent and run that one object on every call (`src/inspect_ai/agent/_handoff.py:19`, `_as_tool.py:23`). Inspect's own agents meet this contract by building in the call: `deepagent()` builds its subagents, tools and inner `react()` inside `execute()`, with the comment "All setup runs per-sample inside execute() so there is no shared mutable state across concurrent samples" (`src/inspect_ai/agent/_deepagent/deepagent.py:122-124`), and its background reminder hook keeps a counter in a closure that is built there too (`lifecycle_tools.py:567-570`).

The contract is across samples, which run in separate sandboxes. It does not promise that one object can run several times at once in the same sample, and some agents rely on that:

- **inspect_swe's `claude_code()`** allocates its session id in the factory, "once per agent instance so that all calls to execute() for the same sample share the same session", runs `claude --session-id <id>` on a first call and `claude --resume <id>` on later ones, and rebinds the id from a checkpoint with `nonlocal` (`inspect_swe@678e6a0: src/inspect_swe/_claude_code/claude_code.py:295-308`, `:362-366`, `:479-517`). Its system-prompt pinning filter is built once per instance for the same reason (`:301-308`). Two concurrent calls of one object in one sandbox therefore drive one Claude Code session from two processes.
- **inspect_swe's ACP agents** (`interactive_claude_code`, `interactive_codex_cli`, `interactive_gemini_cli`) refuse to be constructed outside an active sample and keep the live connection, session and readiness event on the instance (`inspect_swe@678e6a0: src/inspect_swe/acp/agent.py:97-104`, `:195-253`), so they cannot be built at task construction or shared. Their `__call__` opens the session, sets `ready` and then blocks "until either the caller cancels the task or the ACP adapter process exits"; the caller drives the conversation through `conn.prompt()` (`:85-94`, `:251-253`).
- **Tools built at construction** hold Python state too: `web_search()` creates its search provider lazily in a closure shared by every caller of that tool object (`src/inspect_ai/tool/_tools/_web_search/_web_search.py:203-214`). Stateful tools mostly avoid this by keeping state elsewhere: the built-in tools in the sample store under an `instance` name ([Members](#members), Tool state), and local MCP servers in a session per async task, "keyed by the running task OBJECT, rather than per sample" (`src/inspect_ai/tool/_mcp/_local.py:148-171`).

### How Inspect separates a factory from its arguments

- **Tasks.** `eval()` and `eval_set()` take a `@task` function and `task_args`, and call the function with them (`src/inspect_ai/_eval/loader.py:174-185`); a `Task` object passed directly gets no `task_args` (`:147-160`). `eval-retry` rebuilds a task the same way, from its registry name and logged arguments.
- **Solvers.** `SolverSpec(solver, args)` names a solver and its arguments for `--solver name -S ...` and retries (`src/inspect_ai/solver/_solver.py:65-75`).
- **inspect_flow.** `FlowSolver` and `FlowAgent` are "a registry `name` (or `factory`) plus its `args`" (`inspect_flow@4028c0f0: src/inspect_flow/_types/flow_types.py:309-360`).
- **Registry objects do not keep their arguments.** The `@agent` wrapper calls the factory and tags the returned object with `registry_tag()`, which stores only the serialised form of the arguments, `extract_named_params()` (`src/inspect_ai/agent/_agent.py:172-213`; `registry.py:151-183`, `:186-249`). So a built agent cannot be rebuilt from itself: its callables are names, its non-registry objects type names ([How arguments reach the log](#how-arguments-reach-the-log)). A factory is told apart from an agent it built by the registry: the factory is the object registered under its name, `registry_lookup("agent", registry_info(f).name) is f`, and an agent it built is not (spike: `True` for `deepagent`, `False` for `deepagent(tools=[bash()])`).

### Precedent for member-like records

deepagent's `Subagent` is a plain dataclass built by `subagent()` (`src/inspect_ai/agent/_deepagent/subagent.py:13-53`). It is configuration, not a registry object, and it is logged by its `name` only (table above). deepagent copies its subagent list in each call but keeps the tool, model and limit objects in it, "Preserves Tool/Model/Limit object identity" (`deepagent.py:401-414`): Inspect shares objects passed as arguments and builds fresh what the factory makes.

## Design

### The shape of the call

| Component | Argument | Value | Registry type |
|---|---|---|---|
| Members | `members=` | built agents, or `member(agent, ...)` and `member(factory, args=..., ...)` records; one or a list | none: a member's agent or factory is already a registered `@agent` |
| Channels | `channels=` | channel names or `@swarm_channel` objects | `swarm_channel` (new) |
| Controller | `controller=` | a controller name or a `@swarm_controller` object | `swarm_controller` (new) |
| Observer | none; after M2, `policy=` may set its bus hook | always on | none |

Two arguments are not components: `budget=` (the swarm-wide cap; the limits deep dive's `Budget`) and, after M2, `result=` (the task's result contract; the scoring deep dive's `ResultSpec`). M2 adds neither; the communication deep dive's bus hook may later add `policy=` as a setting of the observer's interception point ([Observer](#observer)).

**The `swarm_` prefix** (decision: Ransom, 2026-10-09). The two new registry types are `swarm_controller` and `swarm_channel`, and their decorators are named the same: `@swarm_controller` and `@swarm_channel`. The prefix keeps a swarm channel apart from inspect_ai's per-execution queue, which these documents always call the *agent channel*, and leaves the plain `controller` and `channel` free in inspect_ai's closed `RegistryType` should it ever want types of those names. Only those four identifiers carry it. The component names (Channels, Controller), the `swarm()` arguments `channels=` and `controller=`, registered names such as `inspect_swarm/leaderless` and `inspect_swarm/messages`, and the classes the decorators return (`Controller`, `Channel`, `SwarmControl`, and the communication deep dive's `ChannelState` and `ChannelRun`) keep their names. Renaming the classes would buy nothing: they are already qualified by the package, and the one place inspect_ai relates a class name to a type, `registry_create()`'s return-annotation rule, cannot be met for `swarm_controller` by any reasonable class name (it would take a class called `Swarm_Controller`), so inspect_swarm creates its objects with `create_registry_object()` whatever the classes are called ([The registry](#the-registry), [Resolving names](#resolving-names)).

```python
# src/inspect_swarm/_swarm.py
AgentFactory = Callable[..., Agent]     # a registered @agent factory, such as `deepagent`

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
        members: The roster: built agents or `member()` records. A bare `Agent` is `member(agent)`. A factory-form `Member` may stand for several members (`count`).
        controller: How the swarm runs: topology, start and wake, termination and the final answer. A name resolves to a registered controller with its default parameters.
        channels: How members share information. `[]` means none: members get no channel instructions or tools, though a shared sandbox is still shared.
        budget: The swarm-wide cap.
        result: The task's result contract.
    """
```

The common case is one line, `swarm(members=member(deepagent, args={...}, count=4))`, and means:

```python
swarm(
    members=member(deepagent, args={...}, count=4),   # four deepagents, each built for its member
    controller="leaderless",      # = leaderless(), whose final chain is scoring's default
    channels=["filesystem"],      # = [filesystem()]
    budget=Budget(),              # caps derived from the sample's limits
)
```

`swarm()` validates at construction and raises `ValueError` (or `TypeError` for wrong types), naming the argument:

- `members=` holds built agents and `Member` records (or, on replay, their logged dicts); a bare factory raises `TypeError` saying to use `member(factory, args=...)` ([Records, logging and names](#records-logging-and-names));
- every member's agent is a registered agent and every member's factory a registered `@agent` factory whose arguments bind to its signature ([The member contract](#the-member-contract)), so the log can name it;
- no agent object appears in more than one member (checked by identity), so none runs twice at once in a sample;
- member names, after `count` expansion and the numbering of repeated default names, are unique;
- each channel name appears at most once;
- names resolve ([Resolution](#resolving-names));
- (after M2) `result` is a `ResultSpec` or `None`, and (after M2, if the bus hook is a setting) `policy` a callable or `None`; the string a raw-solver replay passes for either raises `TypeError` ([Logging and replay](#logging-and-replay));
- the controller's `check` accepts the roster, and (after M2) the controller's `final` is valid for `result` (the scoring deep dive's validation rules, applied to the controller's chain).

### Controller and runtime

swarm.md uses "the controller" both for the policy that decides how the swarm runs and for the machinery that enforces the swarm's invariants. This design separates them:

- **The controller** is a registry object chosen per arm. It decides which members start, when, with what input; when the swarm is done; and which final-answer chain applies.
- **The runtime** is inspect_swarm's fixed code in `swarm()`. It owns member tasks, member spans and limits, the budget nodes, recovery from the swarm's own cap, drain, verification of submissions and the provisional answer, finalisation, the ledger, evidence and metrics.

A controller cannot break an invariant because it never holds what the runtime owns: it starts members through a handle, never runs them itself; it returns a stop reason, and the runtime then runs the limits deep dive's stop procedure; it names a final chain, and the runtime runs it after the drain. Where the sibling deep dives write "the controller" for drain, exhaustion, verification or finalisation, they mean the runtime.

### Members

Members are data around a registered agent or a registered `@agent` factory, both already registry objects, so they get a record, not a decorator. A built agent can also be passed to `members=` directly; it is `member(agent)` ([The member contract](#the-member-contract)).

```python
# src/inspect_swarm/_member.py
class Member(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    agent: Agent | None = None            # a built, registered agent; serialised with registry_value()
    factory: AgentFactory | None = None   # or a registered @agent factory; serialised as its registry name
    args: dict[str, Any] = {}             # the factory's keyword arguments; serialised as @agent logs an agent's params
    name: str | None = None               # default: the agent's or factory's unqualified registry name; if given, not empty, no "/" or "#"
    role: str | None = None               # data; given to the controller and in the member's preamble
    count: int = 1                        # members from this record: name-1 ... name-k when count > 1 (factory form only)
    limits: list[Limit] = []              # the member's own limits (limits deep dive); serialised by kind and value
    # fields the sibling deep dives add: delivery and, after M2, remind (communication), bridged (limits),
    # input (ORBIT)

def build_member(record: Member) -> Agent:  # private: the agent, or record.factory(**record.args); only the runtime calls it
    ...

def member(
    agent: Agent | AgentFactory,
    args: Mapping[str, Any] | None = None,
    *,
    name: str | None = None,
    role: str | None = None,
    count: int = 1,
    limits: Sequence[Limit] | None = None,
) -> Member: ...
```

Exactly one of `agent` and `factory` is set. `member()` tells them apart through the registry: a factory is the object registered under its own name, and an agent it built is not ([How Inspect separates a factory from its arguments](#how-inspect-separates-a-factory-from-its-arguments)).

The two forms side by side: `swarm(members=[react(prompt=...), deepagent(tools=[bash()])])` is two members, `react` and `deepagent`, each a built agent; `member(deepagent(tools=[bash()]), name="lead", role="coordinator")` is one built agent with a name and role; and `member(deepagent, args={"tools": [bash(), python()]}, count=4)` is four deepagents, each built for its own member in each sample.

#### The member contract

**No agent object runs twice at the same time in one sample** (decisions: Ransom, 2026-10-14: fix this in the API rather than by documenting it; accept built agents, with copies taking the factory form and its `args` dict, [open question 3](#open-questions)). A built agent is shared by every concurrent sample, as every Inspect solver is ([Current behaviour](#registry-objects-are-shared-by-concurrent-samples)); that is not the hazard. The hazard is the same object running more than once in one sample, sandbox and store, and the API closes the two ways it could happen:

- **`count > 1` needs the factory form.** `member(deepagent(...), count=4)` raises `TypeError`: "count > 1 runs several copies at once, so each needs its own agent: pass the factory and its arguments, member(deepagent, args={...}, count=4)".
- **One agent object, one member.** `swarm()` raises `ValueError` when the same agent object (by identity) appears in more than one member, bare or in a record, naming both members.

The two forms:

- **A built agent** runs once per sample, as the member it is. It is built once, at task construction, as a solver is, and shared across samples. Its own closure state therefore follows Inspect's usual contract, per invocation; an agent that keeps state on the object (`claude_code()`'s session id) shares it between samples, which run in separate sandboxes, exactly as it does as an ordinary solver.
- **The factory form** builds an agent per member per run. The runtime calls `build_member()`, that is `factory(**args)`, once for each member the record stands for, each time the swarm runs: once per member in each sample, epoch and retry. It calls it in the member's task, inside every scope the runtime enters for the member (its tool-state scope; from M2 its sandbox), before the member wrapper enters its limit nodes, builds the conversation on the state the runtime keeps for the member, opens the member's span and calls the agent ([swarm-limits.md](swarm-limits.md#stopping), "Member wrapper and statuses"). The member keeps that agent for its whole life in the run; a persistent member, later, is woken on the same agent. Nothing the factory builds is shared: its closures, hooks, caches and the tools it constructs, and `claude_code()`'s session id. A spike with a stub `Member` of this shape and a factory whose agent counts its calls in a closure ran `count=3` over two samples on mockllm: six builds, each agent called once. One agent object passed to three concurrent calls in the same two samples counted up to six.
- **When to choose the factory form** with `count=1` too. It is required only for copies, but it also gives: state the agent keeps on its object that is private to the member and the sample, not shared with concurrent samples; a `get_model()` in the factory that resolves against the sample's active model and roles; and agents that can only be built inside a sample. Building costs one factory call per member per sample, 50 to 200 microseconds for `deepagent(tools=[bash(), python()])`, `react(tools=[bash(), python()])` and a builder that constructs its own tools (timeit, 2026-10-14), against model calls that take seconds.
- **Construction checks.** `member()` raises `TypeError`, naming the agent or factory:
  - when it is neither a registered agent nor a registered `@agent` factory: a lambda, a plain function, or a factory whose registry name now resolves to another object. The message says to decorate it with `@agent`, so that the log can name it and replay can find it;
  - when a built agent is given `args` ("args are the factory's arguments: pass the factory, member(deepagent, args={...})") or `count > 1` (above);
  - when a factory's `args` do not bind to its signature (an unknown name, or a required parameter missing), checked with the same `inspect.signature(...).bind()` that `registry_tag()` uses.

  `member()` does not call a factory, so a factory's own checks run when the member is built. A probe build at construction would fail for a factory that refuses to run outside a sample, and would run any factory's side effects at task construction. Inspect already validates some agent arguments in the call (deepagent's `max_depth`, `src/inspect_ai/agent/_deepagent/deepagent.py:133-134`). A factory that raises is a foreign error to the member wrapper, since it has no limit source: the member is `errored` and the error propagates and fails the sample, as an agent that raises in its own call does today.
- **What stays shared: objects an agent is given.** Objects passed in a factory's `args`, or to the factory of a built agent, are built once by the task: every call of the factory receives the same object, and two built agents given the same tool object share it, as deepagent's per-call copies of its subagents keep their tools ([Precedent](#precedent-for-member-like-records)). This case is left to documentation: the swarm cannot tell a stateful object from a stateless one, and copying does not help, because `copy.deepcopy` returns a function unchanged and a copied model or MCP server would lose its connection state. In practice:
  - Inspect's built-in tools keep their state in the sample store or the sandbox, not in the tool object, so the tool-state scope separates members however the tool reached them;
  - a local MCP server keeps one session per async task, and each member runs in its own task, so each member gets its own session;
  - a custom tool or helper that keeps state in its Python object (a counter in a closure, a cache, `web_search()`'s lazily created provider) is shared by every member and sample it reaches. To make it per member, construct it in a member builder used in the factory form (below). `member()`'s docstring says so. Such an object used on purpose to pass information between members is a channel outside the bus ([Security](#security)).
- **Bridged agents** take either form: `claude_code()` as one member, or `member(claude_code, args={...}, count=4)` for four agents in each sample, each with its own session id. The limits deep dive's bridged detection reads the agent's or factory's registry name ([swarm-limits.md](swarm-limits.md#swarm-nodes)).
- **ACP agents are refused.** An ACP agent's call opens a session and then waits until its caller cancels it, while the caller drives the conversation through `conn.prompt()` ([Current behaviour](#registry-objects-are-shared-by-concurrent-samples)). A member must return when its work is done, so an ACP member would run until a cap stopped it. The runtime checks each agent's class, a built agent at `swarm()` construction and a factory's agent after building: if one of its bases is a class named `ACPAgent` defined in an `inspect_swe` module (checked by name, so inspect_swarm does not import inspect_swe), it raises `TypeError`, "ACP agents are interactive sessions driven by their caller and cannot be swarm members". A built ACP agent normally cannot reach `swarm()`, since ACP agents refuse construction outside a sample. A builder that drives an ACP agent and returns is later work; M1 does not support ACP members.

#### Records, logging and names

- **Why pydantic.** A pydantic model is logged as its fields, at top level and inside lists ([table](#how-arguments-reach-the-log)). Serialisers make it faithful, writing exactly one of the two forms. A built agent is written with `registry_value()`, Inspect's normal agent registry dict: `{"agent": {"type": "agent", "name": "react", "params": {"prompt": "b"}}, "name": "x", ...}`. A factory is written as its registry name (`registry_log_name()`, which drops `inspect_ai/`) and `args` with `extract_named_params(factory, False, **args)`, the function `@agent` uses for a built agent's params, so a member's arguments log exactly as the agent's own params would have (spike: `{"tools": [{"type": "tool", "name": "bash", "params": {}}, {"type": "tool", "name": "python", "params": {}}]}`, equal to `registry_params(deepagent(tools=[bash(), python()]))`): `{"factory": "react", "args": {"prompt": "solve"}, "name": "w", "count": 4, ...}`. Each limit is written as `{"limit": "cost", "value": 2.0}` (with the metering type for tokens, which the limits deep dive reads from the object).
- **Bare built agents.** An agent passed to `members=` without `member()` is logged by Inspect's own capture, as its registry dict at top level and inside a list (spike: `swarm(members=react(prompt="a"))` logged `{"members": {"type": "agent", "name": "react", "params": {"prompt": "a"}}}`, and a list mixing a bare agent and both record forms logged each in its form).
- **The factory form is not a registry dict.** Its logged form has no `{"type": "agent", "name", "params"}` dict, on purpose. Replay turns every such dict into an object at task construction (`registry_kwargs()`, `registry.py:689-704`), which is right for a built agent but would build a factory-form member's agent outside any sample, once, and share it between its copies.
- **No bare factories in `members=`.** `swarm()` refuses a bare factory (`swarm(members=deepagent)`), alone or in a list, with `TypeError`: "members= takes agents or member() records: write member(deepagent, args={...})". Normalising it inside `swarm()` would be too late, because the `@agent` wrapper captures `swarm()`'s arguments as the caller passed them, once `swarm()`'s body has returned (`src/inspect_ai/agent/_agent.py:176`, `:196`); a refusal raised in the body stops the call before anything is captured. A bare factory would otherwise log as a string, its registry name with namespace at top level (`"inspect_ai/deepagent"`) and without it inside a list (`["deepagent", "react"]`, `registry.py:231-235`), with no record for replay to rebuild.
- **Replay.** `registry_kwargs()` rebuilds a built agent's registry dict into an agent at task construction, as it does for any solver, whether bare or inside a member dict. A factory-form member dict comes back with `factory` as a string and its `args`' nested registry dicts rebuilt into objects (spike: `bash` came back as a tool). `swarm()` accepts an agent, a `Member` or a mapping in each position and validates a mapping with `Member.model_validate()`: an `agent` field holds the rebuilt agent; a `factory` string is resolved with `registry_lookup("agent", name)`, which loads a `package/name`'s entry point first, and an unknown name raises `ValueError`. A limit dict is rebuilt by a validator that calls the matching Inspect limit factory. A spike replayed a bare agent and a list mixing a bare agent, a factory record and an agent record: each came back in its own form.
- **Names.** A member's name defaults to its agent's or factory's unqualified registry name. A name also names the member's tool-state scope, which refuses names that are empty or contain `/` or `#`, so `member()` raises `ValueError` for such an explicit `name=` at construction rather than leaving the runtime to fail when the member starts. A default derived from a registry name is not checked for these restrictions at construction (it has no package prefix, so no `/`, but a registered name could contain `#`) and can fail when the member starts; such an agent needs a valid explicit name. With `count=k>1` a record's members are `name-1` to `name-k`. Default names that repeat across the roster, from distinct built agents of the same registry name (`[react(...), react(...)]`) or alongside a counted record, are numbered the same way in roster order (`react-1`, `react-2`). An explicit name is never renumbered, and a collision with one raises `ValueError`. A name is an identifier the eval author chose; members never choose names (swarm.md, [Delivery](swarm.md#delivery-peer-messages-are-model-output)).
- **Background deepagents** are rejected while background ownership is unsupported, as swarm.md's [Members](swarm.md#members) says: a built `deepagent` whose registry params set `background` to `True` or to an integer (`deepagent(background=64)`), or a `deepagent` factory whose `args` do. M2-5 removes the rejection: a member's `background()` work then runs in an owner the swarm keeps for that member and is drained before the member ends ([swarm-background-members.md Lifting the refusal](swarm-background-members.md#lifting-the-refusal); [swarm-implementation-plan.md](swarm-implementation-plan.md#m2-ready-for-external-usage)).

#### Tool state and member builders

- **Tool state.** The runtime runs each member inside an inspect_ai tool-state scope named after the member, so built-in tools that keep state in the sample store (`memory()`, `bash_session()`, `web_browser()`, the skill tool) give each member its own state when their `instance` is left at the default (swarm.md, [Members](swarm.md#members); decision: Ransom, 2026-10-09, on swarm.md's [open question 2](swarm.md#open-questions)). This is why `member(deepagent, count=4)` gives four memories rather than one. Separate agents do not make the scope unnecessary: built-in tools keep their state in the sample store under their instance name, and a new tool object with the default instance reaches the same key as any other in the same scope (`store_as(MemoryStore, instance=tool_state_instance(instance))`, `src/inspect_ai/tool/_tools/_memory.py:115`). Separate agents separate Python objects; the scope separates store keys, including those of tools shared between members. `member()`'s docstring states the rest of the contract:
  - an explicit `instance` is used as given, so members, or the copies of one counted member (built from the same `args`), that name the same instance share that state, which is a channel outside the bus ([Security](#security));
  - the sample store itself is shared, including any environment a task keeps there;
  - state that custom tools or agent code keep in the store with no instance is shared unless they resolve their instance with `tool_state_instance()`;
  - built-in tool state a setup solver prepared at the default instance before the swarm runs is not the members': each member starts with its own, so tasks that rely on it are unsupported in M1.
- **Faithful only as far as the agent's or factory's arguments.** A built agent's params and a factory's `args` are logged by Inspect's rules. Ordinary values (`react(prompt=..., tools=[bash()])`, `args={"prompt": ..., "tools": [bash()]}`) round-trip. A callable argument (an `on_continue` hook, a model filter, a `ToolSource` such as the communication deep dive's `swarm_tools()`) is logged as its name and cannot be rebuilt ([table](#how-arguments-reach-the-log)); two members that differ only in such a callback even collide in `eval_set`. The run itself is unaffected, because the runtime uses the built agent, or calls the factory with the objects in `args`, not their logged form; what is lost is raw-solver replay and eval-set identity. The route is a **member builder**: a registered `@agent` factory with ordinary parameters that builds the configured agent inside, so the log records the builder and its parameters and replay calls it again. A builder works in both forms:

  ```python
  @agent
  def worker(prompt: str = WORKER_PROMPT) -> Agent:
      return react(prompt=prompt, tools=[bash(), python(), swarm_tools()], on_continue=swarm_on_continue())
  ```

  `member(worker, args={"prompt": "..."}, count=3)` logs `{"factory": "worker", "args": {"prompt": "..."}, "count": 3, ...}`, and replay looks `worker` up and calls it with that prompt. `member(worker(prompt="..."), name="w")` is the built form, logged as `{"agent": {"type": "agent", "name": "worker", "params": {"prompt": "..."}}, ...}`. In the factory form a builder is a factory like any other, so its body runs once per member per sample, and the hooks and tools it builds are that member's own. The examples below use builders wherever a member has hooks.

### Controllers

A controller is a `Controller` object returned by a factory decorated with `@swarm_controller`, the way a `Task` is returned by an `@task` factory.

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

def swarm_controller(
    func: Callable[P, Controller] | None = None, *, name: str | None = None
) -> ...: ...   # same overloads as @agent: @swarm_controller and @swarm_controller(name=...)
```

- **`run`** is called once per sample with a `SwarmControl` and returns the stop reason. Returning requests the stop: the runtime stops members still running through the limits deep dive's soft-stop procedure ([Stopping](#controllers), below), then finalises. Its local variables are per sample; the `Controller` object is shared, so it holds no per-sample state ([Current behaviour](#registry-objects-are-shared-by-concurrent-samples)).
- **`final`** (after M2; in M1 the runtime always applies `first`) is the final-answer chain the runtime applies after the drain, and for the provisional answer as members end. `None` means the scoring deep dive's default chain for the task's `result` (verify, then vote, then first, from what the task supplies). It is on the controller, not on `swarm()`, because the default and the valid modes depend on the topology: a coordinator defaults to the lead's report, and `reporter` is invalid for a leaderless swarm.
- **`check`** runs at `swarm()` construction with the roster, for errors a factory cannot see alone (a named member that is not in the roster).
- **The decorator** follows `@agent` and sentinel ([The registry](#the-registry)): registry name `registry_name(func, name or func.__name__)`; the wrapper calls the factory, raises `TypeError` unless it returned a `Controller`, and tags it with `registry_tag(func, instance, RegistryInfo(type="swarm_controller", name=...), *args, **kwargs)`; `registry_add()` registers the wrapper. `Controller` is a plain class, not a frozen dataclass, because tagging sets attributes on it. The factory is annotated `-> Controller` for type checkers. No class name matches `swarm_controller` under `registry_create()`'s rule, so `registry_create("swarm_controller", ...)` returns the factory uninstantiated; inspect_swarm never calls it, and creates controllers by name with `create_registry_object()` ([Resolving names](#resolving-names)), as inspect_ai does for `score_reducer`.

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
    def events(self) -> AsyncGenerator[SwarmEvent, None]: ...   # one open at a time; aclose() releases it

@dataclass(frozen=True)
class MemberEnded:
    member: str
    status: str                  # the limits deep dive's member status
    submitted: bool
    verdict: VerdictSummary | None   # after M2

SwarmEvent = MemberEnded         # later work adds kinds (idle and woken members, quiescence)
```

- **`start()`** schedules the member in the runtime's member task group, in its own span with its limits, which the member wrapper enters before it builds the member's conversation, so a member message limit applies from the first message, and calls the agent on a state the runtime keeps, so its output survives an escaping error ([swarm-limits.md](swarm-limits.md#stopping), "Member wrapper and statuses"), and returns at once. Its input is the given input, else the member record's `input` (the ORBIT deep dive's field), else the swarm's input, followed by the member's preamble: its name, role and roster size, and each channel's instructions ([Channels](#channels)). The preamble's wording is M1's prompt work. In M1 a member runs once: `start()` on a member whose state is not `pending` raises `RuntimeError`. `start()` also raises `RuntimeError` once the swarm is stopping ([Stopping](#controllers), below). A controller can meet that refusal only by resuming before the stop's cancellation reaches it (an event already queued); if the refusal then leaves `run`, the runtime treats it as the stop, not as the controller's error, and the controller ends as the cancellation would have ended it. The rest of `run`'s errors are classified as **Errors and outer stops** below says. Persistent members, later, add a wake operation.
- **`events()`** yields events in order from swarm start, so none is lost between `start()` and iteration. A member counts as running until the runtime has taken its submission's verdict and queued its `MemberEnded`, so an asynchronous verifier still running when the last agent returns cannot be missed. The iterator ends when no member is running and nothing is queued; a member started later is reported by a new `events()` call. One iterator at a time: an iterator holds the events from its first step until it ends or is closed, and a second iterator's first step raises `RuntimeError` while one is open. `events()` therefore returns an `AsyncGenerator[SwarmEvent, None]`, not an `AsyncIterator`, so a controller that stops iterating early releases it with `aclose()` or by iterating inside `contextlib.aclosing()`; one abandoned without either stays open until Python finalises it, and until then another iterator's first step raises. A controller tests an event's kind before reading its fields (`isinstance(event, MemberEnded)`), as every example below does: `SwarmEvent` becomes a union when later work adds idle and woken members (which a controller must not mistake for an ending) and quiescence (which has no `member`).
- **Statuses.** `state` is the coarse lifecycle a controller schedules on. `status` and `MemberEnded.status` are the limits deep dive's closed set, unchanged: `submitted`, `unsubmitted`, `stopped` (soft stop), `share`, `member_limit:<type>`, `swarm_cap`, `cancelled`, `errored` ([swarm-limits.md](swarm-limits.md#stopping), "Member wrapper and statuses"). `submitted` is the scoring deep dive's notion of a submission, which a member stopped mid-work does not have.
- **Members with background work** (from M2-5) end after their drain ([swarm-background-members.md Statuses, ended, and the record](swarm-background-members.md#statuses-ended-and-the-record)). Such a member stays `running` until its background children have finished or been cancelled, so the drain adds to the time between its agent's return and its `MemberEnded`: up to the effective grace plus the cancelled children's cleanup, which has no deadline. After M2, the verdict on its submission is still taken before that `MemberEnded` is queued, as `events()` above requires, so a configured verifier's queueing and running add to it as they do for any member. Its status is still the one its agent ended with, unless a foreign error from a child during the drain makes it `errored`. The controllers below (`leaderless`'s `stop_on_verified`, `coordinator` and `escalate`) act on that `MemberEnded` unchanged, that much later.
- **No member text.** Handles and events carry states, statuses, names chosen by the eval author and a verdict's pass flag and value, never a member's output, a message body or a verifier's explanation (scoring's `Verdict.explanation` is free text that may quote the answer or a judge model's words; it stays in the observer's result record). A controller therefore cannot pass one member's words into another's input, which would put model output in the user role (swarm.md, [Delivery](swarm.md#delivery-peer-messages-are-model-output)). Members read each other through channels' tools. ORBIT's scheduled mode adds conversation snapshots and trusted interventions to these handles for trusted policies; that is its deep dive's extension, argued there.
- **Stopping.** Returning from `run` is a request to stop, not a cancellation. The runtime passes the returned reason to the limits deep dive's single stop procedure, `stop(reason)`: a soft stop through each running member's stop lever, a grace period of `Budget.grace` for in-flight calls to finish and be recorded, and a hard stop (cancellation) only for members still running after the grace ([swarm-limits.md](swarm-limits.md#stopping)). The swarm's own cap and time cap go through the same procedure (`swarm_cap`, `swarm_time`). The runtime orders every stop so that no member can start once it has begun:
  1. **Close scheduling, synchronously.** The first thing `stop(reason)` does, before any await, is mark the swarm as stopping. From then on `start()` raises `RuntimeError`; a controller that resumes and meets that refusal ends with the stop, not with an error (`start()` above). This matters because the soft stop closes only the levers of members already running, and the swarm time cap is a timer, not an Inspect time node the members inherit ([swarm-limits.md](swarm-limits.md#stopping), "Triggers"). A member started during grace would have an open lever and could dispatch new calls until the hard stop cancelled them, turning spend into unknown expenditure.
  2. **Cancel the controller, not the members.** For a stop the controller did not request (`swarm_cap`, `swarm_time`), the runtime cancels `run`'s own cancel scope at this point. `run` runs in a scope separate from the member task group, so cancelling it cancels no member. When the controller returned, there is nothing to cancel.
  3. **Soft stop, grace, hard stop**, as the limits deep dive specifies. Members ending during grace still get statuses, evidence and their `MemberEnded`, but no controller acts on them.
  4. **Finalise** with `first` in M1, and after M2 with the controller's `final`.

  Members never started stay `pending`; the evidence records them as not started.
- **Errors and outer stops.** These are not stops the controller requested, and the limits deep dive's exhaustion rules apply unchanged: a foreign error from a member (a sample limit, `TerminateSampleError`, a crash) or from `run` makes the task group cancel the rest at once and propagates. The runtime classifies an exception from `run` leaf by leaf, bare or grouped, as the limits deep dive's [Exhaustion and errors](swarm-limits.md#exhaustion-and-errors) step 2 says: it removes `start()` refusals raised because a stop had already begun; it removes swarm-cap leaves and runtime-lever leaves as the swarm's own (the cap node has already begun the stop, and the lever refuses only once a stop has begun), so a controller whose own model call reaches a cap ends through the orderly `swarm_cap` stop; and it propagates only the remaining foreign leaves, an ordinary `RuntimeError` included, a single leaf bare and several as a group; cancellation from outside (the sample's time limit, an operator interrupt) does no awaiting work. A member's own limit is not an error: that member ends with a status and an event, and the swarm continues.
- **Stop reasons.** The limits deep dive's closed set is `all_done`, `verified`, `swarm_cap`, `swarm_time` and later `quiescence`. A controller returns one of the topology reasons (`all_done`, `verified`, `quiescence`), or its own string, which is recorded as `controller:<reason>`; the ORBIT deep dive's `policy:<name>` reasons are this form once its policies are controllers. The built-in `leaderless` returns `all_done` or `verified`.
- **Usage.** A controller normally makes no model calls. If one does, its usage is the swarm's, not a member's; the limits deep dive meters it, as it does verifier calls, under the runtime lever ([swarm-limits.md](swarm-limits.md#swarm-nodes)).

The built-in controllers are `inspect_swarm/leaderless` (M1) and, later, `inspect_swarm/coordinator`. If the ORBIT deep dive's scheduled mode is adopted, its `ActivationPolicy` becomes a controller's `run`, its `SwarmControl` and `MemberHandle` additions (`activate()`, `snapshot()`, `set_output()`, `append()`, `edit()`) are methods on these same handles, and its `round_robin` is a `@swarm_controller`. Its own text already says "M1's leaderless controller is the policy that activates every member once".

### Channels

A channel is a `Channel` object returned by a factory decorated with `@swarm_channel`. The communication deep dive owns what a channel does; this design gives it a registry identity and the M1 surface.

```python
# src/inspect_swarm/_channel/_channel.py
class Channel:
    """A sanctioned way for members to share information. Holds configuration only: one object serves every sample."""

    def instructions(self, member: MemberInfo) -> str | None:
        """Text added to the member's preamble, or None. M1."""
        return None

    # M2 adds the communication deep dive's name, prefix, reach, check() and open(); open() gives a
    # per-run ChannelState with tools, validate, admit, apply, notice_lines and quiescent.

def swarm_channel(func=None, *, name: str | None = None): ...   # as @swarm_controller, with type "swarm_channel"
```

- **Built-ins.** `inspect_swarm/filesystem` (M1) carries the shared-sandbox convention as parameters, for example `filesystem(shared="/shared", notes="notes.md", scratch="/scratch/{member}")`; its `instructions()` states them for each member. It adds instructions only and creates no directories. `scratch` must contain a bare `{member}` field and no other field (`ValueError` at construction otherwise), so each member is given its own directory. M2's first channel with tools is a shared memory routed through the bus, `inspect_swarm/shared_memory` ([swarm-memory-channel.md](swarm-memory-channel.md), D-1 in [swarm-implementation-plan.md](swarm-implementation-plan.md#designs)). `inspect_swarm/messages`, later work, is the communication deep dive's `messages(delivery=, max_bytes=, ...)` as a `@swarm_channel` factory. Later channels (notes, task list, board) are more `@swarm_channel` factories.
- **User channels.** In M1 a user channel can only add instructions, which already makes named, sweepable conventions possible: `lockfiles(dir="/shared/locks")`, after the C compiler's lock files, against `filesystem()` alone. From M2 a user channel implements the communication deep dive's operations; the first release calls them experimental ([swarm-implementation-plan.md](swarm-implementation-plan.md#decisions), item 6).
- **Per-sample state.** `Channel` objects are shared by concurrent samples, so their state cannot live on them. Inboxes, versions and other run state live on a `ChannelState` the runtime opens per run ([swarm-communication.md](swarm-communication.md#how-channels-plug-into-the-bus)).
- **No channel, no instructions.** `channels=[]` gives members no channel instructions or swarm tools. The shared sandbox is still shared: what is shared is the sandbox topology's business (swarm.md, [Sandbox topology](swarm.md#sandbox-topology)), not the channel list's.
- **The name.** The component is called Channels (decision: Ransom, 2026-10-08: "channels is fine for now", so the name may be revisited). inspect_ai's unrelated `AgentChannel`, the per-execution queue that delivers notices, is always called the *agent channel* in these documents, as swarm.md's [The agent channel](swarm.md#the-agent-channel) does. The names compared are in the [appendix](#appendix-why-the-component-is-called-channels).

### Observer

The observer is what the swarm records and where monitors attach: evidence events, the realized-cost ledger, metrics, the per-member result record, and the bus's interception point (swarm.md, [Observer](swarm.md#observer-evidence-accounting-and-metrics)). It has no `observer=` argument and no `@observer` registry type.

**Why what it records is fixed.** The record is a data contract with its readers, not a policy choice. The scoring deep dive's `member_scores()` and `attempt_rows()`, the limits deep dive's ledger and curves, and swarm.md's analysis columns read its fields by name and version, and every arm writes the same record: a baseline arm runs as a swarm of one (the scoring deep dive's `baseline()`), so single-agent, epochs and swarm arms are read by the same code. A per-arm switch that dropped an evidence kind or changed the ledger's coverage rules would make every field optional for every reader, and an arm without, say, a complete ledger could not be compared on realized cost at all. What users do want to vary in how a run is watched and measured, monitors, scores and labels, already has an Inspect mechanism, configured per task or per eval, so the swarm adds none. The record changes only with an inspect_swarm release, and carries a `version` (swarm.md, [Observer](swarm.md#observer-evidence-accounting-and-metrics)).

**How it is configured.** What is fixed, what can be set, and where:

| What | Set by | Where it lives | Changes how the swarm runs? |
|---|---|---|---|
| Evidence events, the ledger and its coverage rules, metrics, the per-member result record | Fixed | inspect_swarm (versioned) | No |
| What the result record can hold (after M2): verdicts and a comparable answer form exist only when the task's result contract supplies a verifier and an answer key | The task's result contract, `swarm(result=)` | The swarm's arguments, in the task | Yes, where the controller uses them: its `final` chain (`verify`, `vote`) and `stop_on_verified` |
| The bus hook: continue, modify, reject or terminate each sanctioned record (after M2; in M2 sentinel monitors the channel tools) | `swarm(policy=...)` or a sentinel adapter (the communication deep dive's open question 1) | The swarm's arguments, in the task | Yes |
| Live monitors and protocols (tool calls, including sends and reads) | inspect_sentinel's `@monitor` and `@protocol`, through `Task(sentinel=...)` or `eval(sentinel=...)` / `eval_set(sentinel=...)` (inspect_ai `feature/sentinel`, `src/inspect_ai/_eval/task/task.py:107`) | The task, or eval options | Yes: a protocol can reject, modify or terminate |
| Scores: the task's headline score and per-member scores | The task's `@scorer`s ([Scores](#scores-the-task-scores-the-observer-records)) | The task | Not through the swarm, which never calls a scorer. But a member may: a retrying agent's in-loop scoring uses the task's scorers ([Scores](#scores-the-task-scores-the-observer-records)) |
| Post-hoc labels (coordination failures, message storms) | Scout `@scanner`s over the logs | A Scout scan job, outside the eval | No |

Whether each of these distinguishes two arms in an eval set is in [Task identity](#task-identity-what-makes-two-arms-distinct).

#### Scores: the task scores, the observer records

Scoring is not an observer extension. The observer records evidence; the task's own scorers score it, as for any Inspect solver. The swarm runtime never calls a scorer (the scoring deep dive rejects computing per-member scores inside the swarm: scorers need the target, and their usage would land in the solve). Two kinds of score read what a swarm leaves behind:

- **The task's score** (final@k, or the team result for shared-artifact tasks). Declared by the task as its `scorer=`, the same scorer in every arm. It reads the final state: `TaskState.output`, which holds the answer the controller's `final` chain selected after the drain, or, for a shared-artifact task, the environment the drained swarm leaves. `final` therefore decides what this score sees; `result=` supplies what the chain needs (the answer key for `vote`, the verifier for `verify`).
- **Per-member scores** (after M2; `best_member`, which is team@k for answer-scored tasks, and `mean_member`). Declared by the task too: the scoring deep dive's `member_scores(scorer, result)` is a scorer builder that the task wraps in a registered, no-argument `@scorer` of its own and lists after its headline scorer. It reads the observer's per-member result record from the sample store (each member's submission, status and verdict) and scores each member's submission with the given scorer on a copy of the state. It does not depend on the `final` chain: it scores every member, whichever answer was chosen. It needs the task's result contract (`kind="answer"`); without one it reports per-member values as unavailable.

Metrics are recorded the same way: the observer writes them to the store and an `InfoEvent`, as it writes the ledger, and a scorer or the analysis helpers turn them into reported values (swarm.md's harness-validity checks are scorers of this kind).

**Members may score in the loop.** The runtime does not score, but a member's agent may call the task's scorers during the solve. `score(state)` runs the active task's scorers against the target from inside a solver or agent (`src/inspect_ai/scorer/_score.py:14-80`). `react(attempts=3)` does so after each submission and stops or retries on the first result (`src/inspect_ai/agent/_react.py:324-334`); `deepagent(attempts=)` passes it to its inner `react()` (`_deepagent/deepagent.py:235`); `basic_agent` (`src/inspect_ai/solver/_basic_agent.py:234`), the human agent's `/score` command and the attempt loops of six inspect_swe agents (`claude_code`, `codex_cli`, `mini_swe_agent`, `opencode`, `gemini_cli`, `kimi_code`) do the same. For such members the task's scorer is feedback that shapes the trajectory, not only a measurement afterwards, which is why a scorer change is an arm of its own ([Task identity](#task-identity-what-makes-two-arms-distinct)). Two rules from the scoring deep dive keep the per-member scorer out of that loop:

- `member_scores()` returns `None` at once while `state.completed` is `False`, so in-loop calls see exactly the task scorer's feedback and call count, and it never reads a half-written record ([swarm-scoring.md](swarm-scoring.md#answer-scored-tasks-per-member-scores), step 1);
- it must follow the task's headline scorer in the scorer list (same section, "Ordering").

### Resolving names

A string for `controller=` or in `channels=` is resolved by `resolve(type, name)` in `src/inspect_swarm/_registry.py`:

1. A name with a `/` is looked up as given (`registry_lookup()`, which loads that package's entry point).
2. A bare name is looked up as given (an object registered in the user's own task file is unprefixed), then as `inspect_swarm/<name>`. A user's own `leaderless` therefore shadows the built-in, as inspect_ai's bare-name rule does (`registry.py:276-287`).
3. The object is created with no arguments, by `create_registry_object(type, name, {})`, not `registry_create()`, which would return the factory itself ([The registry](#the-registry)). A factory with a required parameter cannot be named this way, and the `TypeError` says so.
4. An unknown name raises `ValueError` listing the registered names of that type (`registry_find()`).

The resolved registry name is recorded in the swarm's evidence for each sample, so a log shows which object a bare name meant. inspect_swarm declares an `inspect_ai` entry point (`inspect_swarm = "inspect_swarm._entrypoint"`) that imports the built-ins, so `inspect_swarm/...` names resolve without importing the package first, as inspect_sentinel's does.

Both registry types need inspect_ai's `RegistryType` to list them ([The registry](#the-registry)): one inspect_ai PR adds `"swarm_controller"` and `"swarm_channel"`, as inspect_ai#4359 added `validation_predicate`. It lands before M1 (decision: Ransom, 2026-10-08); the types carry the `swarm_` prefix described under [The shape of the call](#the-shape-of-the-call). The tool-state scope PR is also required before M1 (decision: Ransom, 2026-10-09; [Implementation plan](#implementation-plan)).

### Logging and replay

**The rule for what this API defines.** Every argument of `swarm()`, `member()` and every controller and channel factory is a plain value, a registry object, a list or dict of those, or a pydantic model whose fields follow the same rule (with field serialisers for registry objects, factories, limits and models, as `Member` has). Then the plan step logs the argument faithfully, two arms that differ in it get different eval-set identifiers, and `create_registry_object()` can rebuild it. Replay turns registry objects back into objects but leaves pydantic models as dicts, so every factory that takes a pydantic argument also accepts its dict form and validates it, as `swarm()` does for `Member`.

Under this rule, the swarm-wide cap is the limits deep dive's `Budget`, not a bare `Limit` (logged as `"_CostLimit"`), and `Budget` must be a frozen pydantic model, not a frozen dataclass (logged as `"Budget"`). The scoring deep dive's `FinalMode` values (`synthesize(model=...)`, and `reporter("<member>")`) must be pydantic models too, with a discriminator field so that a chain of mixed strings and modes validates back from JSON, and a serialiser and validator for a `Model` field (`registry_value()` writes a `Model` as a model dict, `registry.py:678-684`, but only at top level, not inside a pydantic model). Only string chains were spiked here; the scoring deep dive's spike verified the mixed form, which is built with the chain after M2.

**What the log records but cannot rebuild.** These fall outside the rule, and each has a route that keeps eval sets and retries correct:

| What | Why | Route |
|---|---|---|
| A member agent built with callables, or a member factory given them in `args` (hooks, filters, tool sources) | Inspect logs a callable as its name ([table](#how-arguments-reach-the-log)) | A registered member builder with ordinary parameters ([Members](#tool-state-and-member-builders)) |
| `result=` (after M2), the task's `ResultSpec` | It holds the task's callables (answer key, verifier); the scoring deep dive logs it as its type name, `"ResultSpec"` | Rebuild through the task, or through a registered swarm builder (below) |
| `policy=` (after M2, if the bus hook is a setting rather than the sentinel adapter) | A plain callable, logged as its name | The same as `result=` |

**Two kinds of reconstruction.** They differ, and the guarantees differ:

- **Through the task.** `eval-retry` and eval-set retries of a task file or registered task re-run the task's code (`src/inspect_ai/_eval/eval.py:1737-1756`), which builds the swarm, its `result=` and its member builders afresh. Everything is rebuilt; this is the supported path for any swarm. (A run that overrode the solver with `--solver` is retried from its solver spec, `:1758-1762`, which is the second path.)
- **From the solver's logged arguments.** `--solver inspect_swarm/swarm -S ...`, inspect_flow's solver specs, or `create_registry_object()` on a plan step rebuild the raw `swarm()` call from its params. This rebuilds the reconstructible subset: member records, controllers, channels, `Budget`, built member agents whose own params are ordinary (rebuilt here, at task construction, as any solver is), and member factories whose `args` are ordinary (not built here: the rebuilt records name their factories, which the runtime calls in the sample, as in the original run). `result` comes back as the string `"ResultSpec"`; `swarm()` raises `TypeError` for a string `result` ("a result contract cannot be rebuilt from a log; rebuild the swarm through its task or a registered builder") rather than treating it as `None`, which would silently change the final-answer chain and per-member claims.
- **A registered swarm builder** makes the second path complete: an `@agent` factory with ordinary parameters that returns the configured `swarm(...)`, result contract included. It logs as `{"type": "agent", "name": "math_swarm", "params": {"k": 4}}` and replays by calling the builder.

  ```python
  @agent
  def math_swarm(k: int = 4, cost: float = 40.0) -> Agent:
      return swarm(
          members=member(deepagent, args={"tools": [bash(), python()]}, name="solver", count=k),
          budget=Budget(cost=cost),
          result=answer_result(answer_key=boxed_number, verifier=check_answer),
      )
  ```

For eval-set identity, `result` logged as a type name is harmless when the arms share their result contract, which arms of one task normally do; a task whose arms differ in it takes the difference from a task argument ([Task identity](#task-identity-what-makes-two-arms-distinct)). Member callbacks are not harmless (two arms differing only in a hook collide), which is why the builder route is the documented way to configure members with hooks.

Spikes confirmed the whole path. The controller part used a simulated registry type (`RegistryInfo.model_construct()` stands in for the literal entry; the spike used `"controller"`, and a re-run of its registry part under `"swarm_controller"` on 2026-10-09 logged and rebuilt the controller the same way, with only the type string changed). The member part was re-run on 2026-10-14 with a stub `Member` holding `factory`, `args`, `name` and `count`. `swarm(members=[member(react, args={"prompt": "solve"}, name="w", count=4)], controller=leaderless(final=["verify", "first"]))` logs the following (the names are unprefixed because the spikes ran outside a package; in inspect_swarm the controller is `inspect_swarm/leaderless`, since `registry_log_name()` strips only `inspect_ai/`, `registry.py:521-535`)

```json
{
  "members": [{"factory": "react", "args": {"prompt": "solve"}, "name": "w", "count": 4}],
  "controller": {"type": "swarm_controller", "name": "leaderless", "params": {"final": ["verify", "first"]}}
}
```

and `create_registry_object("agent", ...)` on those params called `swarm()` again with a `Controller` object for `controller` and, for the member, a dict whose `factory` is the string `"react"`, which `Member`'s validator resolves to the registered factory. The member spike also ran the stub runtime on two samples, and an `eval_set` of two arms that differ only in a member's `args` accepted them as distinct.

### Sweeping

- **Task parameters.** A task exposes the axes it wants, and names resolve: `-T controller=escalate`, `-T channels=filesystem,messages`. A single `-T channels=filesystem` arrives as a string, which `channels=` accepts.
- **Python sweeps.** `eval_set([task(controller=leaderless(final=f)) for f in ...])` (after M2) and inspect_flow work on solver arguments too, because they are logged faithfully and so give distinct task identifiers.
- **`--solver inspect_swarm/swarm`.** It works through the `@agent` path, but `members=` needs a dict on the command line (`-S 'members={factory: react, count: 4}'`), and it cannot carry a result contract. A registered swarm builder (`--solver math_swarm -S k=8`) has neither problem ([Logging and replay](#logging-and-replay)). Tasks are the intended surface.

### Task identity: what makes two arms distinct

An **arm** is one condition of an experiment. In Inspect terms it is one task in an eval set: a `Task` instantiated with its arguments and run with a given solver, model and limits. Inspect's own measure of "one task" is `task_identifier()`, and `eval_set()` refuses two tasks with the same identifier ([Current behaviour](#how-arguments-reach-the-log)). Arms in swarm.md's experiments differ in task arguments (agent count, budget), in the solver (a single agent, a deepagent, a swarm), in the model, or in epochs; the first three are hashed, epochs is not.

A swarm is an agent used as the task's solver, so it is part of the identifier through the plan: `as_solver()` forwards its registry params, and the plan step's `params_passed` is hashed. Everything this API defines is logged faithfully ([Logging and replay](#logging-and-replay)), so a change to any of its arguments, at any depth, changes the identifier. Everything that configures how a swarm runs or is measured:

| What differs between two arms | Reaches `task_identifier()`? | How, and does it matter |
|---|---|---|
| `controller=`, `channels=`, `budget=`, `members=` (a built agent's own params, or a factory and its `args`; count, names, roles, limits) | Yes | The plan's `params_passed`, nested registry dicts and pydantic fields included. Tested ([Testing](#testing)). |
| A callable given to a member's agent or in its `args` (hook, filter, tool source) | Only by name | Logged as `__name__`: two arms differing only in such a callback collide. Configure it in a registered member builder with ordinary parameters, whose `args` are hashed ([Members](#tool-state-and-member-builders)). |
| `result=` | No (logged as `"ResultSpec"`) | Arms of one task normally share it. If they differ (a different verifier), the task builds it from a task argument, which `args_hash` covers, or the arms use different registered swarm builders. |
| `policy=` (after M2, if built as a setting) | Only by name | A policy is a plain callable, logged as `__name__`. A task that varies the policy takes it from a task argument, or sets it inside a registered swarm builder. |
| The task's scorers, headline or per-member | No | Same as every Inspect task: scorers are part of the task, so they distinguish arms only through the task's name or arguments (spike: two otherwise identical tasks with different scorers are "not distinct"). A scorer can change the run: a member with in-loop scoring (`react(attempts=3)` and the other retrying agents in [Scores](#scores-the-task-scores-the-observer-records)) stops or retries on the task scorer's verdict. So a scorer ablation is an arm of its own, selected by a task argument or name. Re-scoring one run's log with `inspect score` is a substitute only when the scorer that changed was not consulted during solving (no member scores in the loop) and does not need the sandbox (the scoring deep dive's re-scoring rules). |
| Sentinel monitors and protocols | No, on `feature/sentinel` | `Task(sentinel=)` and `eval_set(sentinel=)` are not hashed. An eval-set option applies to every arm alike, so it cannot make two arms differ. A monitoring ablation chooses the sentinel from a task argument. This does matter: a protocol changes the run, and without that argument two arms collide, and a re-run of the eval set with a different sentinel matches the earlier logs as the same tasks (`eval_set()` pairs tasks with existing logs by identifier). Flagged under [Not this design](#not-this-design). |
| The observer's record | Not configurable | Arms in one eval set run one installed inspect_swarm, so they write the same record version. |
| Model, model roles, generate config, sample limits | Yes | Hashed by Inspect directly. |
| Epochs | No | An epochs arm differs from a swarm arm in its solver already; two arms that differ only in epochs need different task arguments or names. |

The rule a task author follows is the one `eval_set()`'s error message gives: distinct task names or task arguments, and registered solvers, whose parameters are then hashed. Everything on `swarm()` is covered by the solver's parameters; anything else a swarm experiment varies goes through the task's name or arguments.

### Where the sibling designs' parameters go

| Deep dive | Its parameter | In this API |
|---|---|---|
| Limits | `swarm(budget=Budget(...))` | `swarm(budget=)`; `Budget` is a frozen pydantic model ([Logging and replay](#logging-and-replay)) |
| Limits | `member(limits=[...], bridged=)` | `member()` fields; limits serialised by kind and value |
| Limits | `stop(reason)`, soft stop and grace; stop reasons; member statuses | a controller's return value and the own caps go through `stop(reason)`; its reasons and statuses are the public ones ([Controllers](#controllers)) |
| Scoring | the final-answer chain, a mode or chain | after M2, the controller's `final=`: `leaderless(final=...)` |
| Scoring | `swarm(result=...)` | after M2, `swarm(result=)`; rebuilt through the task or a registered swarm builder, never from a raw solver spec ([Logging and replay](#logging-and-replay)) |
| Scoring | `Verdict(passed, value, explanation)` | after M2, controllers see `VerdictSummary(passed, value)`; the explanation stays in the result record |
| Scoring | `reporter` mode, reserved | valid only on controllers with a reporter; `coordinator(lead=...)` defaults to it |
| Scoring | `baseline(member, result)` | after M2, `swarm(members=member, controller=leaderless(final="first"), channels=[], result=result)`, where `member` is a built agent or a `Member` record, as `members=` takes |
| Communication | `channels=["filesystem", messages(...)]` | the same call; `messages` is a `@swarm_channel` factory, `"messages"` resolves to it |
| Communication | `member(delivery=)`; after M2, `member(remind=)` | `member()` fields |
| Communication | `swarm(policy=)` | Not in M2, where sentinel is the only monitor. If the bus hook is later a setting rather than the sentinel adapter, `swarm(policy=)` is the setting of the observer's interception point; a callable, so it is logged by name and varied across arms through a task argument or a registered swarm builder ([Task identity](#task-identity-what-makes-two-arms-distinct)) |
| Communication | the `Channel` protocol | M2's operations on `Channel`, with per-sample state on a per-run `ChannelState` |
| Communication | `swarm_tools()`, `swarm_on_continue()`, `swarm_bridged_tools()` | member-side helpers, not `swarm()` arguments; a member using them is a registered member builder, which builds them per member and which the log can name |
| ORBIT (#4) | `member(input=, role=)` | `member()` fields; `input` is the default for `SwarmControl.start(input=None)`. ORBIT's `exposure=` is gone: exposure is ORBIT's own observer's ([swarm-orbit.md P3](swarm-orbit.md#p3-exposure)) |
| ORBIT (#4) | `ActivationPolicy`, `SwarmControl`, `MemberHandle` | a controller's `run`, and methods added to these handles |
| ORBIT (#4) | `round_robin(max_rounds=)` | a `@swarm_controller` |
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
            members=member(deepagent, args={"tools": [bash(), python()]}, name="solver", count=k),
            controller=controller,
            budget=Budget(cost=cost),
        ),
        scorer=...,
        sandbox="docker",
    )
```

`inspect eval research_math.py -T k=8 -T cost=80` runs eight members, `solver-1` to `solver-8`, each with its own deepagent, in the sample's sandbox, with the filesystem channel, under a cost cap of 80 (dollars), each started with the sample's input and its preamble; it stops when all have ended, and the final answer is the first submission. After M2, with the scoring deep dive's `result=answer_result(answer_key=boxed_number, verifier=check_answer)`, the default answer is scoring's default chain. The explicit form of the same arm after M2, with a strict verifier-only answer and the swarm stopping at the first verified answer:

```python
swarm(
    members=member(deepagent, args={"tools": [bash(), python()]}, name="solver", count=4),
    controller=leaderless(final="verify", stop_on_verified=True),
    channels=[filesystem(notes="notes.md")],
    budget=Budget(cost=40.0),
    result=answer_result(answer_key=boxed_number, verifier=check_answer),   # verify needs a verifier
)
```

The built-in it uses, as it is after M2. In M1 it has no parameters, starts every member and returns `all_done`:

```python
import contextlib

@swarm_controller
def leaderless(final: FinalSpec | None = None, stop_on_verified: bool = False) -> Controller:
    """Start every member with the swarm's input; stop when all have ended."""
    if final is not None and has_reporter(final):   # any `reporter` mode in the chain
        raise ValueError("leaderless: a leaderless swarm has no reporter")

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with contextlib.aclosing(swarm.events()) as events:   # released on the early return
            async for event in events:
                if stop_on_verified and isinstance(event, MemberEnded) and event.verdict and event.verdict.passed:
                    return "verified"
        return "all_done"

    return Controller(run, final=final)
```

**A coordinator swarm (later: coordinator topologies, on the later messages channel).** A lead and three workers. All start together; workers wait for the lead's instructions with `swarm_wait` ([swarm-wait.md](swarm-wait.md)), and the lead's submission is the answer. Members with hooks and tool sources are registered builders, passed uncalled, so the log can name them and each member gets its own hooks ([Members](#tool-state-and-member-builders)).

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
    members=[member(lead, name="lead", role="coordinator"), member(worker, name="worker", count=3)],
    controller=coordinator(lead="lead"),
    channels=["filesystem", messages(delivery="notify")],
)
```

The controller, as it would be built in the coordinator-topologies work:

```python
import contextlib

@swarm_controller
def coordinator(lead: str = "lead", final: FinalSpec | None = None) -> Controller:
    """Start everyone; the swarm is done when the lead ends."""

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with contextlib.aclosing(swarm.events()) as events:   # released on the early return
            async for event in events:
                if isinstance(event, MemberEnded) and event.member == lead:
                    return "lead_done"       # soft-stops the workers still running
        return "all_done"

    def check(roster: Sequence[MemberInfo]) -> None:
        if lead not in [m.name for m in roster]:
            raise ValueError(f"coordinator: no member named {lead!r}")

    return Controller(run, final=reporter(lead) if final is None else final, check=check)
```

Workers start with the lead because the communication deep dive delivers only to running recipients ([swarm-communication.md](swarm-communication.md#records-and-the-bus), commit checks: liveness). Starting a member on its first message would need delivery to pending members, which that design rejects; a topology that wants it brings that extension in its own design. A worker that has submitted cannot be woken again until persistent members exist (swarm.md, [Members](swarm.md#members)). `lead_done` is a topology reason the coordinator work adds to the limits deep dive's set; until then it would be recorded as `controller:lead_done`.

**A user-defined controller** (after M2: it needs the scoring deep dive's verifier). Adaptive compute: run one cheap member alone, and start the rest only if its answer does not verify.

```python
import contextlib
from collections.abc import Sequence

from inspect_swarm import Controller, MemberEnded, MemberInfo, StopReason, SwarmControl, swarm_controller


@swarm_controller
def escalate(first: str, final: FinalSpec | None = None) -> Controller:
    """Run `first` alone; if its submission does not verify, start everyone else."""

    async def run(swarm: SwarmControl) -> StopReason:
        swarm.start(first)
        async with contextlib.aclosing(swarm.events()) as events:   # released on the early return
            async for event in events:
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
    members=[member(react(model="openai/gpt-5-mini"), name="scout"), member(deepagent, name="worker", count=4)],
    controller=escalate(first="scout"),
    result=answer_result(verifier=check_answer),
)
```

Defined in a task file, its registry name is `escalate`; in an installed package `mypkg`, it is `mypkg/escalate`. The plan step logs `"controller": {"type": "swarm_controller", "name": "escalate", "params": {"first": "scout", "final": null}}`. `-T controller=escalate` cannot work, because `first` has no default; a task that sweeps it takes `first` as its own parameter.

## Alternatives considered

**Closed string settings** for the topology and the final answer, and a fixed list of channel names. The least machinery. There is no way to add a topology or a channel without changing inspect_swarm, the final-answer chain has to be validated against a separate `topology=`, and ORBIT's scheduled mode needs a protocol of its own anyway. Rejected.

**Plain config objects and protocols, no registry** (deepagent's `Subagent`; the ORBIT deep dive's `ActivationPolicy` passed as an object). No inspect_ai change. But a user's controller cannot be named from `-T` or a config, parameters are not captured automatically, and an object that is not a pydantic model is logged as its type name, which makes otherwise different arms "not distinct" in `eval_set` (spike). Rejected for controllers and channels; adopted for members, where the object being named (the agent's `@agent` factory) is already in the registry.

**Register controllers and channels under existing types** (`solver` or `agent`, with metadata saying what they are). No inspect_ai change. But `--solver leaderless` would then resolve to a controller and fail obscurely, and anything that lists solvers or agents would list controllers. Rejected; the literal entry is one line with precedent.

**One `swarm_component` registry type for all components.** One literal entry instead of two. But a name could then resolve to the wrong kind (`channels=["leaderless"]`), so every lookup needs a second check, and the decorator name no longer says the type. Rejected.

**Packages declare their own registry types** (an inspect_ai change that opens `RegistryType` to types registered by installed packages). Satellites would stop needing an inspect_ai PR per type. But the closed set is load-bearing: pydantic validates `RegistryInfo.type` against it, and `is_registry_dict()` uses it during replay to decide whether a logged dict is an object to rebuild or plain data (`registry.py:647-657`). An open set needs a registration API, a rule for when replay may trust a type a package declared (loaded through its entry point before any lookup), and its own review, for a problem that a one-line PR with precedent already solves. Not chosen (decision: Ransom, 2026-10-08, who took the one-line PR); it is an inspect_ai design of its own if more satellites need types.

**Plain type names** (`controller`, `channel`, with decorators `@controller` and `@channel`). Scout and sentinel use plain names (`scanner`, `monitor`, `protocol`). But a bare `channel` reads as inspect_ai's own agent channel, and both words would collide if inspect_ai ever wanted types of those names (its `AgentChannel` is not a registry object today). Rejected in favour of the `swarm_` prefix on the types and the decorators alike (decision: Ransom, 2026-10-09; [The shape of the call](#the-shape-of-the-call)).

**Hook-style controllers** (a class with `on_start`, `on_member_ended`, `should_stop` methods, as Inspect's `Hooks` are). Declarative and harder to misuse. But sequencing (escalate, waves, ORBIT's plans) becomes a state machine spread across callbacks, and Inspect's class registration does not capture constructor parameters into the log the way factory decoration does. Rejected in favour of one `run` coroutine over an event stream.

**Built agents with `count`** (`member(deepagent(...), count=4)`, this design's form until 2026-10-14), with the contract that a member's agent is safe to invoke concurrently. The shortest call, and the one `handoff()` and `as_tool()` use. But the swarm then runs one object `count` times at once in one sample, sandbox and store, a stronger demand than Inspect's across-samples contract, and nothing checks it: `claude_code()` fails it through its per-instance session id, and any closure state an agent keeps fails it silently. Rejected for `count > 1`, which needs the factory form; a built agent with `count=1` stays (decision: Ransom, 2026-10-14).

**Factories only** (`members=` takes only `member(factory, args=...)` records; this amendment's first revision). One form, and every member's agent built per member per sample. But it makes the caller go through a factory and `member()` for every member, when building agents directly is the ordinary way to configure an Inspect agent, and the hazard it guards against is narrower than that: sharing a built agent across samples is how Inspect runs every solver, and only `count` and the same object in two members run one object twice in a sample. Rejected (decision: Ransom, 2026-10-14: "I think it should be easy for a caller to construct their own agents directly without the member() indirection").

**Copies of a built agent, rebuilt from its registry info** (`member(deepagent(...), count=4)`, with `create_registry_object("agent", name, registry_params(agent))` per copy). The call stays the familiar one. But a built agent keeps only the logged form of its arguments ([How Inspect separates a factory from its arguments](#how-inspect-separates-a-factory-from-its-arguments)), so the rebuild is only as faithful as the log, and the loss cannot be detected: an `on_continue` hook comes back as its name, a string that a submitting `react()` accepts as continuation text, so the rebuilt agent's params equal the original's while its behaviour differs. deepagent's `Subagent` objects come back as their names. Refusing the agents whose params do not round-trip would need the raw arguments, which are gone. Rejected.

**Copies of a built agent, from raw creation arguments that inspect_ai keeps** (an inspect_ai PR in which `registry_tag()` also stores the bound arguments, unlogged, and a `registry_recreate(agent)` calls the factory again with them). The call, its static typing and the log shape stay as they were, and the rebuild is exact. But it needs an inspect_ai change for every registry object, which then holds references to all its arguments; the agent passed to `member()` is built at task construction only to be discarded, which an agent that exists only inside a sample cannot be; and `member(deepagent(...))` reads as handing the swarm an object that it then never runs. The factory-and-arguments form gets the same result with no inspect_ai change and in Inspect's own idiom for factories ([Current behaviour](#how-inspect-separates-a-factory-from-its-arguments)). It was the strongest alternative; not chosen ([open question 3](#open-questions), decision: Ransom, 2026-10-14).

**Factory arguments as keywords of `member()`** (`member(deepagent, count=4, tools=[bash()])`). Shorter. But factory parameters collide with member fields: `react()` takes `name` and `description`, inspect_swe's `claude_code()` takes `name` and `sandbox`, and M2 adds `member(sandbox=)`. A collision would silently send an argument to the wrong place, or need a rule for which wins. Rejected for `args=`, as `eval(task_args=)`, `SolverSpec.args` and inspect_flow's `FlowAgent(args=)` keep arguments apart.

**`functools.partial`** (`member(partial(deepagent, tools=[bash()]), count=4)`). Type checkers check the bound arguments, which an `args` dict does not get. But it allows positional arguments, which the log must then rebind, it is not an idiom Inspect uses anywhere, and `member()` would have to unwrap a `partial` to find and validate the registered factory. Not chosen ([open question 3](#open-questions), decision: Ransom, 2026-10-14).

**One agent per copy, built at construction.** `member()` calls the factory `count` times and holds the agents, as if the caller had built them. It would serve the copies as a built agent serves one member, but it runs a factory at task construction, with its side effects there and failing for an agent that can only be built in a sample, while building per member per sample costs one factory call of 50 to 200 microseconds and keeps the factory form's other benefits ([The member contract](#the-member-contract)). Rejected.

**Copy a built agent per copy** (`member(deepagent(...), count=4)` with `copy.deepcopy(agent)`). The familiar call. But `copy.deepcopy` returns a function unchanged, so a closure-based agent, which is every `@agent`, is not copied at all, and copying the objects it closes over would break models and MCP servers. Rejected.

**Build a probe agent in `member()`** to raise a factory's own errors at task construction. Earlier errors for misconfigured arguments. But an agent that exists only inside a sample cannot be probed, and the probe runs the factory's side effects at task construction. Argument binding is checked without building; the factory's own checks run when the member is built, as deepagent's already do in its call. Rejected.

**A plain callable factory** (`member(lambda: react(...))`). Any code can build the agent. But the log records only the callable's name, so replay cannot call it and two arms that differ inside it collide. A registered `@agent` factory, a member builder where needed, uses Inspect's registry and logging. Rejected.

**Controllers that run members themselves** (`await member.run()` in an anyio task group). Familiar to anyio users. But the controller would then own member tasks, so drain and cap recovery would depend on every controller cancelling correctly, and a controller bug could leave a member running during finalisation. Rejected: the runtime owns member tasks, and the controller only starts them.

**`final=` on `swarm()`.** One fewer object. But the valid modes and the default depend on the topology, which is the controller, so `swarm()` would have to cross-validate two arguments that belong together. Rejected.

**An observer argument or `@observer`.** Would let an arm change what is recorded: drop an evidence kind, change the ledger's coverage rules, add a record of its own. The record is read as a contract by scorers and analysis, and baseline arms write it through `baseline()`; a configurable record would make every field optional for every reader, and an arm with a reduced ledger could not be compared on realized cost. What users vary in watching and measuring a run (monitors, scores, labels) is already configured through sentinel, the task's scorers and Scout ([Observer](#observer)). Rejected. A new evidence kind, if one is needed, is an additive, versioned change to the record, not a per-arm switch.

**One `SwarmSpec` object** holding all components (`swarm(SwarmSpec(...))`). One serialisable record. Inspect's APIs are keyword-first (`Task(...)`, `react(...)`), and a spec object is one more layer for the common case. Rejected.

## Compatibility and migration

- **inspect_swarm** has no released API ([swarm.md](swarm.md#compatibility-and-migration)). Its first release, in M2, calls `swarm()`, `member()`, `Budget`, the built-in controller and channel names and the version-1 record and evidence formats supported, and the `Controller`/`SwarmControl` and `Channel`/`ChannelState` interfaces for user-written controllers and channels experimental ([swarm-implementation-plan.md](swarm-implementation-plan.md#decisions), item 6).
- **inspect_ai.** Two entries in `RegistryType`. They are additive: nothing in inspect_ai uses the type set except `RegistryInfo`'s validation and `is_registry_dict()`, which then recognises two more kinds of logged object, and the literal is not in the log schema or the generated TypeScript types (verified above). inspect_swarm needs the inspect_ai version that has them; 0.3.281 is the first release with them. From M1-4 inspect_swarm also needs the tool-state scope, which is on inspect_ai `main` (#5861) but in no release yet, so it depends on inspect_ai's git `main` as a temporary workaround, and returns to a released `inspect-ai>=X` floor before inspect_swarm is published, since PyPI rejects git dependencies (decision: Ransom, 2026-10-16, 2026-10-21). Imported with an older inspect_ai, inspect_swarm's decorators fail at import with pydantic's `ValidationError` on `RegistryInfo`.
- **Registry dict shapes.** With the new types, any dict of exactly the shape `{"type": "swarm_controller" | "swarm_channel", "name": str, "params": dict}` inside replayed arguments is rebuilt as an object instead of staying data (`is_registry_dict()`, `registry_arg()`, `registry.py:647-701`). Neither string occurs anywhere in inspect_ai, inspect_sentinel, inspect_scout or inspect_flow (checked 2026-10-09), so no existing caller uses those shapes; the inspect_ai PR states this reservation in its compatibility note.
- **Member records.** A built agent logs as Inspect's agent registry dict, as before; the factory form's `{"factory", "args"}` record is new. No released code or log holds either, and `member()` refuses a built agent with `count > 1` with a message that shows the factory call ([The member contract](#the-member-contract)).
- **Eval logs.** The swarm's plan step gains nested `{"type": "swarm_controller" | "swarm_channel", "name", "params"}` objects and member dicts. These are ordinary JSON inside `params`, which is `dict[str, Any]` (`src/inspect_ai/log/_log.py:705-718`), so every log reader accepts them. An inspect_ai that lacks the types treats them as plain dicts on replay; replaying a swarm log needs inspect_swarm installed anyway.

## Security

- **Names from the command line or a log.** `-T`, `-S` and replay hand strings and dicts to name resolution, which only finds registered objects. A `package/name` loads that installed package's declared entry point, which `--solver` already does today (`registry.py:289-297`). Logs replayed with `eval-retry` are trusted input, as they already are for solvers. No new exposure.
- **Controller and channel code is trusted eval-author code**, like solvers. It is never reachable from member tools, and members cannot select a controller or a channel.
- **No model text reaches a controller.** Handles and events carry states, statuses, member names chosen by the eval author, and a verdict's pass flag and numeric value; never a member's output, a message payload or a verifier's explanation, which may quote the answer or a judge model ([Controllers](#controllers)). So a controller cannot relay model output into another member's input. `start(input=...)` takes the eval author's text; its documentation says never to put member output there.
- **The preamble** contains the member's name and role, the roster size, and channel instructions, all from the eval author. Nothing a member wrote enters it.
- **Shared tool state.** Members cannot pick a tool's `instance`; the eval author does, in the member's agent. Explicit scoped instances (starting with `@`) may name the current scope or an enclosing scope, including their private instances; unrelated scopes are refused, so no instance can name a sibling member's scoped state. The default is per member, and a shared instance is a channel the bus does not see, observed like the filesystem (swarm.md, [Security](swarm.md#security)).
- **Shared objects.** No agent object runs twice in one sample, and in the factory form nothing the factory builds is shared. An object the eval author passes in `args`, or gives to more than one built agent, is one object for every member it reaches and every sample; if it keeps Python state that members' tool calls can write and read, it is a channel outside the bus, like a shared tool instance. Only the eval author can create one, and `member()`'s docstring says so ([The member contract](#the-member-contract)).
- **Factories from a log.** Replay resolves a member's `factory` by registry name, which only finds registered `@agent` factories, the same lookup `--solver` and `registry_kwargs()` already make; the factory then runs inside the sample, with the arguments the log recorded.
- **Logged parameters** are the eval author's arguments, written as JSON data in the plan, as Inspect already writes solver parameters.

## Testing

All in inspect_swarm, with mockllm, on asyncio and trio, no network or Docker:

- `tests/test_registry.py`:
  - `@swarm_controller` and `@swarm_channel` register under `inspect_swarm/<name>` for built-ins and the bare name for a task file's own factory, with and without `name=`;
  - a factory returning the wrong type raises `TypeError`;
  - the registry types are `swarm_controller` and `swarm_channel` (`registry_info()` of a built-in), and an object built by a `@swarm_controller` factory logs as `{"type": "swarm_controller", ...}`;
  - resolution: a `/` name, a bare built-in name, a user factory shadowing a built-in, an unknown name (`ValueError` listing names), a factory with a required parameter (`TypeError`);
  - `registry_lookup("swarm_controller", "inspect_swarm/leaderless")` succeeds in a fresh interpreter that has not imported inspect_swarm (the entry point).
- `tests/test_api_log.py`:
  - plan-step `params` and `params_passed` for the common case, the explicit form and a heterogeneous roster match expected JSON, including nested agents and limits;
  - raw-solver replay: `create_registry_object()` on the params of a swarm without `result=` rebuilds an equivalent swarm whose params are equal; with `result=` (after M2) it raises the documented `TypeError`, never substituting `None`;
  - task replay (after M2): retrying a task-file swarm with a verifier result rebuilds the result contract and selects the same final answer (a separate case from raw-solver replay);
  - a bare built agent, a list of built agents, a single record and a list mixing bare agents with both record forms each log as specified (agents as registry dicts, factory records as full member dicts, a package factory keeping its namespace, `inspect_swarm/...` or a test package's), and all replay through `create_registry_object()`, built agents rebuilt at task construction and factory records resolved without building any agent;
  - `eval_set` gives different identifiers to two arms whose built member agents differ only in a parameter, and to two arms whose factory records differ only in `args`;
  - member records log as `{"factory": ..., "args": ...}` with `args` equal to what `@agent` would log for the built agent (`deepagent` with `tools=[bash(), python()]`), and raw-solver replay resolves the factory by name and rebuilds the tools in `args` without building any agent;
  - member builders: two `member(worker, args={"prompt": ...})` records with different prompts log differently, get different `eval_set` identifiers and, replayed, run with their `on_continue` hook intact; a registered swarm builder (`math_swarm`) replays through `--solver`-style params (after M2, with its result contract);
  - `eval_set` accepts two arms that differ only in a controller parameter, a channel parameter, a member's `count` or `limits`, or `Budget`, and assigns them different identifiers;
  - (after M2) a final chain mixing strings and pydantic `FinalMode` values (including a `synthesize` with a model) validates back from its logged JSON;
  - `-T channels=filesystem` (a string) and `-T channels=filesystem,messages` (a list) both resolve.
- `tests/test_controller.py`:
  - `leaderless` starts every member and returns `all_done`; after M2, with `stop_on_verified` it returns `verified` at the first passed verdict;
  - (after M2) a delayed verifier: the last member's agent returns before its verdict is taken, and `events()` still yields that member's `MemberEnded` with the verdict before it ends, so `stop_on_verified` and `escalate` see it;
  - a custom controller (`escalate`) starts members later and with custom input; `start()` twice raises; `events()` ends when nothing runs; a second iterator raises while one is open, including one that competes in the interval after a waiting iterator is woken (the woken one keeps its event); an open iterator holds its events until it is closed;
  - returning while members run goes through the soft stop: in-flight mockllm calls complete and are recorded, members get status `stopped`, and only a member still running after `Budget.grace` is cancelled; the swarm cap and time cap take the same path with reasons `swarm_cap` and `swarm_time`, and `run` is cancelled;
  - stop ordering, with barriers: a controller that runs a scout first (`escalate` after M2; in M1 one that starts the workers when the scout ends) runs its scout while the time cap (and, separately, the swarm cap) fires; the scout ends unverified during grace. No pending worker starts (a `start()` attempted after the stop began raises `RuntimeError`), no member model call is dispatched after the stop began, cancelling `run` does not cancel the scout's member task, and the never-started workers are recorded as not started;
  - a foreign exception in `run` (an ordinary `RuntimeError` included), or a foreign member error, cancels the rest at once and propagates, while a cap or runtime-lever error from `run`, bare or grouped, ends it through the stop; a member's own limit ends that member with a `member_limit:<type>` status and the swarm continues;
  - a custom reason is recorded as `controller:<reason>`;
  - no member text: a member that outputs a distinctive string, and after M2 a verifier whose explanation echoes it, and the string appears in no event or handle field;
  - one swarm object serves two concurrent samples with no shared controller state;
  - no agent object runs twice in a sample: two built members run their own objects, and in the factory form a factory whose agent counts its calls in a closure, with `count=3` over two concurrent samples, is built six times and each agent is called once; the build happens inside the member's tool-state scope (the factory reads `tool_state_instance(None)` and gets the member's scope instance, `"@w-1"` for member `w-1` when the swarm runs inside no other scope); an object passed in `args` is the same object in every member (the documented sharing); a factory that raises makes the member `errored` and fails the sample; an agent whose class derives from a class named `ACPAgent` in a module under `inspect_swe` is refused with `TypeError` when built;
  - each member runs in its own tool-state scope: members with `memory()`, and the copies of a counted member, each see only their own memory files, while members given `memory(instance="team")` share them; default-instance tool state a setup solver prepared is not visible to members (the rest of the four-tool cases are swarm.md's runtime tests, in `tests/test_tool_state.py`: two deepagent members' `general` subagents keep their skill lists under their own private instances, `@<member>#general`, and each member gets its own `bash_session()` and `web_browser()` session);
  - an explicit member name that is empty or contains `/` or `#` is refused by `member()`;
  - `check` errors, and after M2 an invalid `final` (including `final="verify"` without a verifier), raise at `swarm()` construction.
- `tests/test_scoring_loop.py`: in M1, only the trajectory case described last; after M2, with the scoring deep dive's `member_scores`, also its in-loop guard test run inside a swarm. Members are `react(attempts=3)` and the task lists `member_scores` after its headline scorer; each member gets the same feedback and the same number of task-scorer calls as with the headline scorer alone, and `member_scores` returns `None` while `completed=False`. A second case shows that a scorer change is a trajectory change: with identical mockllm submissions, a passing task scorer ends a member after one call and a failing one makes it retry, and the two arms are distinct in `eval_set` only when the scorer is chosen by a task argument.
- `tests/test_channel.py`: `filesystem()`'s instructions appear in each member's preamble with that member's scratch directory; `channels=[]` adds none; a duplicate channel raises; a `scratch` with no `{member}` field, or with any other field, is refused.
- `tests/test_member.py`: count expansion and names, with default names taken from the agent or factory; repeated default names from distinct built agents (`[react(), react()]`) and alongside a counted record are numbered in roster order; a collision with an explicit name raises; `swarm(members=deepagent)`, `swarm(members=[deepagent, react])` and a list mixing a record and a bare factory raise `TypeError` saying to use `member(factory, args=...)`; a built agent is accepted bare, in a list and in `member()`; `member(deepagent(), count=4)` and `member(deepagent(), args={...})` raise `TypeError`, the first showing the factory form; the same agent object in two members (bare twice, or bare and in a record) raises `ValueError`; a lambda and an unregistered function are rejected with `TypeError`; `args` with an unknown name or a missing required parameter raise `TypeError`; `deepagent(background=True)`, `deepagent(background=64)`, `member(deepagent, args={"background": True})` and `args={"background": 64}` are rejected (in M1; M2-5 turns these into acceptance cases, [swarm-background-members.md Testing](swarm-background-members.md#inspect_swarm-m2-5)); `Member` validates from its own logged dict, and an unknown factory name raises `ValueError`; `member()` never calls the factory (a factory that raises outside a sample is accepted).

In inspect_ai, the registry PR adds a test to `tests/util/test_registry.py` that registers, looks up and round-trips (`is_registry_dict()`) one object of each new type, as inspect_ai#4359 did for `validation_predicate`. The tool-state scope PR (inspect_ai#5861, merged 2026-10-16) adds `tests/util/test_tool_state.py` (an explicit instance is returned as given; `None` resolves to the scope's instance `"@<name>"`, to `None` outside one, and to `"@outer/inner"` in nested scopes; `private_tool_instance()` gives `"<scope>#<name>"`; an explicit instance starting with `@` outside the current scope chain raises `ValueError`, as does an empty scope name or one containing `/` or `#`; the scope does not leak out of its block or into a sibling task) and tool cases for the whole family: `tests/tools/test_memory.py` (two scopes in one sample keep separate files, writable and readonly; no scope keeps today's keys); `tests/tools/test_skill.py` (direct default skill tools and deepagent child skill tools are per scope; `private_tool_instance()` names keep today's keys outside a scope); `bash_session()` and `web_browser()` cases with the sandbox RPC stubbed, so no Docker (separate session ids per scope, one reused session within a scope); and the legacy browser fallback raising `PrerequisiteError` inside a scope.

## Implementation plan

1. **inspect_ai: two registry types.** Add `"swarm_controller"` and `"swarm_channel"` to `RegistryType`, with a test in `tests/util/test_registry.py`. Files: `src/inspect_ai/_util/registry.py`, `tests/util/test_registry.py`. inspect_ai's changelog is written from the pull request title, so neither inspect_ai PR here edits `CHANGELOG.md` (decision: Ransom, 2026-10-13). Before M1 (decision: Ransom, 2026-10-08), with the `swarm_` prefix ([The shape of the call](#the-shape-of-the-call)).
   With it, also before M1, a second small PR: **the tool-state scope** (decision: Ransom, 2026-10-09; swarm.md's [open question 2](swarm.md#open-questions)). `tool_state_scope()`, `tool_state_instance()` and `private_tool_instance()` in a new `src/inspect_ai/util/_tool_state.py`, exported from `inspect_ai.util`; used by `memory()`, `bash_session()`, `web_browser()` (which also refuses its legacy fallback inside a scope) and the skill tool; and deepagent's child skill instance changed to `private_tool_instance(sa.name)`. Files: that module, `src/inspect_ai/util/__init__.py`, `src/inspect_ai/tool/_tools/_memory.py`, `_bash_session.py`, `_web_browser/_web_browser.py`, `_skill/tool.py`, `_skill/install.py` (the shared install, as merged), `src/inspect_ai/agent/_deepagent/agent_tool.py`, `tests/util/test_tool_state.py`, `tests/tools/test_memory.py`, `tests/tools/test_skill.py`, a stubbed-RPC test for `bash_session()` and `web_browser()`.
2. **inspect_swarm: registry plumbing** (M1). The decorators, `Controller`, `Channel`, `MemberInfo`, `resolve()`, and the entry point. Files: `src/inspect_swarm/_controller/_controller.py`, `src/inspect_swarm/_channel/_channel.py`, `src/inspect_swarm/_registry.py`, `src/inspect_swarm/_entrypoint.py`, `pyproject.toml` (`[project.entry-points.inspect_ai]`), `tests/test_registry.py`.
3. **Members and the `swarm()` signature** (M1). `Member` and `member()` with the built-agent and factory forms, their serialisers, the construction checks and the runtime's private `build_member()`; bare agents in `members=`, the identity check and default-name numbering; `swarm()`'s arguments, validation and resolution; exports in `src/inspect_swarm/__init__.py`. Files: `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/__init__.py`, `tests/test_member.py`, `tests/test_api_log.py`.
4. **The controller surface in the runtime** (M1). `SwarmControl`, `MemberHandle`, `MemberEnded`, `events()`, `start()` and the preamble, each member's task entered in its tool-state scope and a factory-form member's agent built there (with the ACP refusal), in the M1 runtime the limits and scoring deep dives describe; the built-ins `leaderless` and `filesystem`. Files: `src/inspect_swarm/_controller/_control.py`, `src/inspect_swarm/_controller/leaderless.py`, `src/inspect_swarm/_channel/filesystem.py`, `src/inspect_swarm/_swarm.py`, `tests/test_controller.py`, `tests/test_channel.py`, `tests/test_scoring_loop.py` (its M1 case).
5. **M2.** The communication deep dive's operations on `Channel` with per-run state; M2 monitors through sentinel only, and the bus hook (a sentinel adapter, or `swarm(policy=)`, which would raise `TypeError` for the string a raw-solver replay passes, as `result=` does) is later work ([swarm-communication.md Monitoring](swarm-communication.md#monitoring-sentinel-first-the-bus-hook-for-the-rest)); the shared-memory channel is M2's first `@swarm_channel` with tools ([swarm-implementation-plan.md](swarm-implementation-plan.md#m2-pull-requests)). `messages` as a `@swarm_channel` is later work.
6. **Later.** The scoring deep dive's `result=`, `final=`, `stop_on_verified` and verdicts, with its post-M2 work and the `member_scores` case of `tests/test_scoring_loop.py` ([swarm-scoring.md](swarm-scoring.md#implementation-plan-after-m2)); `coordinator` with the coordinator topologies (and the `lead_done` reason); new event kinds with persistent members; the ORBIT deep dive's scheduled mode as controllers.

Steps 2 to 4 are the skeleton of M1, not a separate milestone: swarm.md's M1 builds its runtime behind this surface.

## Open questions

None open. The first two questions this design raised are decided (Ransom, 2026-10-08), and the third, from the 2026-10-14 amendment, the same day; each took the recommendation.

1. **The component's name** is **Channels**: "channels is fine for now". The name may be revisited later; the names compared are in the [appendix](#appendix-why-the-component-is-called-channels).
2. **An inspect_ai PR before M1.** "a is fine": a one-line PR adds the two registry types to `RegistryType`, with a test as inspect_ai#4359 had, and lands before M1 ([Implementation plan](#implementation-plan), step 1). The types are `swarm_controller` and `swarm_channel` (decision: Ransom, 2026-10-09; [The shape of the call](#the-shape-of-the-call)). The alternative of letting packages declare their own registry types is under [Alternatives](#alternatives-considered).
3. **How a member names its factory's arguments** is (a), `member(factory, args={...})` (decision: Ransom, 2026-10-14: "go with a"). It is the form for copies (`count > 1`) and for any member built per sample; a single member can also be a built agent (decision: Ransom, 2026-10-14, [The member contract](#the-member-contract)). The options compared, all building the agent per member per sample:
   - (a) `member(deepagent, args={"tools": [bash()]}, count=4)`, a factory and an `args` dict. Inspect's own idiom (`task_args`, `SolverSpec.args`, inspect_flow's `args`), no inspect_ai change, no collision with member fields. The arguments are checked against the factory's signature when `member()` runs, not by a type checker.
   - (b) `member(partial(deepagent, tools=[bash()]), count=4)`. Statically typed, but not an Inspect idiom, and positional arguments must be rebound for the log.
   - (c) `member(deepagent(tools=[bash()]), count=4)` as before, with an inspect_ai PR that keeps each registry object's raw creation arguments so the swarm can call the factory again. The familiar call, typed, and the same log shape; it costs an inspect_ai change to every registry object and builds an agent at task construction only to discard it.

   (a) was chosen because it is Inspect's own way of separating a factory from its arguments, needs no inspect_ai change and keeps factory parameters apart from member fields. (b) was not, because `partial` is not an Inspect idiom and its positional arguments complicate logging and validation; (c) was not, because it needs an inspect_ai change to every registry object and builds an agent at task construction only to discard it ([Alternatives](#alternatives-considered)).

## Not this design

- **Limits and frozen dataclasses log poorly in inspect_ai.** A `Limit` argument is logged as its class name (`"_CostLimit"`), or `null` inside a list; a frozen dataclass as its type name, which makes otherwise different eval-set arms "not distinct". This affects any agent or solver (deepagent's `Subagent.limits`, for example). inspect_ai could serialise dataclasses at top level as it already does inside lists, and limits by kind and value.
- **A dict form for controller parameters on the command line** (`-T 'controller={name: escalate, first: scout}'`).
- **Grid sweeps from the CLI** (`-T k=2,4,8` as three runs) belong to eval-set or inspect_flow, not to inspect_swarm.
- **A generic swarm task** (`inspect_swarm/swarm_task`) that wraps any dataset, so arms can be swept with no task code.
- **Listing controllers and channels** (`inspect list`-style discovery of registered names).
- **Sentinel configuration is not part of `task_identifier()`** (inspect_ai `feature/sentinel`). Two tasks that differ only in `Task(sentinel=...)` are "not distinct", and a re-run of an eval set with a different sentinel matches the earlier logs. Sentinel changes how a run behaves, so it arguably belongs in the identifier's additional hash, as the sample limits are. Until then a monitoring ablation takes the sentinel from a task argument ([Task identity](#task-identity-what-makes-two-arms-distinct)).
- **Refusing member `args` that do not round-trip.** `member()` holds a factory's raw arguments, so it could refuse callables and other values Inspect logs only by name, turning the member-builder rule into a check. Today they run correctly and are logged by name, with builders as the documented route ([Tool state and member builders](#tool-state-and-member-builders)).
- **`claude_code()` shares its session id across concurrent calls of one object.** Inside a swarm no agent object runs twice in one sample, but other code that runs one `claude_code()` object twice at once in one sandbox (two parallel `as_tool()` calls, for example) still drives one Claude Code session from two processes. An inspect_swe matter.
- **Per-copy factory arguments.** The `count` copies of a record are built from the same `args`. A factory that needs its own member's name reads `tool_state_instance(None)`, which returns the member's scope instance (`"@w-1"`), or `"@outer/w-1"` when the swarm runs inside another tool-state scope, so the name is the last `/`-separated part with the `@` removed (`scope.removeprefix("@").rsplit("/", 1)[-1]`); a `{member}` template in `args`, like M2's sandbox template, can wait for a user.

## Appendix: why the component is called Channels

The names compared for the component that says how members share information:

| Name | For | Against |
|---|---|---|
| **Channels** (chosen: decision: Ransom, 2026-10-08, "for now") | The argument (`channels=["filesystem"]`) and the column heading of swarm.md's channel table. ORBIT's vocabulary (channels with readers and writers) and Codex's board. The communication deep dive's `Channel` uses it. swarm.md calls the filesystem an implicit channel. | inspect_ai has an unrelated `AgentChannel`, the per-execution queue that delivers notices. The documents say "agent channel" for that one, as swarm.md's [The agent channel](swarm.md#the-agent-channel) does. |
| Comms / communication | Plain; matches the communication deep dive's title. | The filesystem, a task list and a board are shared state more than messages; informal as an API word. |
| Shared state | Accurate for the filesystem, notes, tasks and board. | Wrong for direct messages, which are not shared. |
| Commons | Names what members hold in common without implying messaging. | Unfamiliar in eval APIs; one more term to learn. |
| Blackboard | An established multi-agent term (Terrarium's blackboards). | A classic architecture with its own control model; implies one shared store, not addressed messages. |
| Workspace | Familiar. | Already means the sandbox's working directory and git worktrees; suggests the filesystem only. |
| Medium, fabric, network, mesh | Neutral. | Vague, and "mesh" already names a topology (Architecture Matters' `mesh_round_robin`). |
| Substrate | Neutral. | Obscure, and it collides with swarm.md's ordinary use of the word ("aim to be a substrate it could run on"). |
