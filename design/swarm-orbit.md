# ORBIT on inspect_swarm

## Overview

ORBIT, the Inspect-based framework for multi-agent security experiments, is designed to run its scheduled experiments on inspect_swarm while ORBIT's own code keeps doing almost everything that makes them ORBIT experiments. inspect_swarm provides the four substrate features this needs, each a general feature that ORBIT cannot build well on top: the [turn seam](swarm-turn-seam.md), which holds a member between the turns of its own `react()` loop and records every change made to its conversation there; a public bus API, with which a channel written outside the package delivers records and reports reads; a controller's choice of a member's preamble at `start()`, which a scheduler that does not run its members at the same time replaces or leaves out; and `Budget(derive=False)`, under which the swarm sets no caps of its own, so the sample's limits end the run as they end any solver. `tests/test_orbit_shape.py` holds these contracts on an ORBIT-shaped harness built from test stand-ins for ORBIT's scheduler, wrappers and channel.

The port itself is ORBIT's work, and this document gives its integration path. ORBIT's agents become ordinary `react()` members, so its fork of `react()` goes. Its scheduler becomes a swarm controller that holds every member at the seam and lets members run one quantum at a time. At the same boundaries, ORBIT's callbacks add attack input and let its defences rewrite a member's conversation. Its reader-and-writer channels become one channel of its own on the bus. Its exposure observer, filters, notices, answer selection, attacks, defences and scorers stay ORBIT code, unchanged where they can be, and ORBIT's model-boundary defences stay `Model` wrappers. ORBIT chooses and records the answer it scores from each member's output on the swarm's result record; that answer path also needs two changes designed elsewhere and not yet built, member outputs on the record and a record that keeps literal member text ([What the record must give](#the-answer-and-a-record-of-the-answer-scored)), so the port's answer path waits on them. A ported run behaves as ORBIT's executor does, apart from a listed set of changes, which ORBIT proves by running its own scheduler, topology and communication tests on both executors, and ORBIT keeps the port's scheduling state and saved member conversations outside the sample's store.

## Background

A deep dive on one topic of [swarm.md](swarm.md): how ORBIT, the framework compared in [Relationship to ORBIT](swarm.md#relationship-to-orbit), runs on inspect_swarm. It builds on:

- [swarm-turn-seam.md](swarm-turn-seam.md) and [Boundary callbacks](../docs/controllers.qmd#boundary-callbacks) for held members, `end()` and the `boundary` and `changed` evidence;
- [swarm-api.md](swarm-api.md) and [Controllers](../docs/controllers.qmd) for controllers, member handles and stopping;
- [swarm-communication.md](swarm-communication.md) and [Writing a channel](../docs/channels.qmd#writing-a-channel) for the bus and channels;
- [swarm-scoring.md](swarm-scoring.md) and [swarm-result-record.md](swarm-result-record.md) for the final answer, the result record and its reader, `swarm_result()`;
- [swarm-event-types.md](swarm-event-types.md) for the swarm's evidence events;
- [swarm-limits.md](swarm-limits.md) and [Budget](../docs/budget.qmd) for statuses, caps and the stop procedure.

It keeps the standing decisions of those designs. The ones it leans on most:

- Peer messages reach members as tool output with distinct provenance (decision: Ransom, 2026-10-07), and peer text enters the user role only in an explicit, opt-in arm (2026-10-09). These govern the swarm's own channels.
- The swarm adds no message to a member's conversation: no notices and no turn hook of its own (2026-10-27).
- Code at the seam is the caller's: what it reads and what it adds, in which role, is the caller's decision, and the seam records it ([swarm-turn-seam.md Non-goals](swarm-turn-seam.md#goals-and-non-goals)). A member ended at a boundary is `stopped`, never a submission (2026-10-28).
- Only submissions are the swarm's answers ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)).
- The swarm writes no exposure event: exposure is ORBIT's observer's (2026-10-15).

References into ORBIT, prefixed `ORBIT:`, are to its 1.0.4 tag (`076104ae`, [github.com/wlanderson0/orbit](https://github.com/wlanderson0/orbit)), relative to its root. inspect_swarm and inspect_ai code is named by file and function.

## Why

ORBIT pays for running outside a substrate:

- **A forked `react()`.** `turn_react` (`ORBIT: orbit/agents/turn_react.py:1-26`) exists so that a scheduler can stop an agent after one quantum and call it again later. It has no `compaction`, `retry_refusals`, `approval` or `review`, opens no agent channel and no checkpointer, reimplements submission, attempts, overflow handling and continue prompts, and imports private inspect_ai modules (`:31`, `:50`, `:66-67`).
- **Re-entry on a stored conversation.** Each activation is a fresh `run()` on the agent's stored `AgentState` (`ORBIT: orbit/execution/agent_scheduler.py:583-588`), so the fork guards against inserting the system prompt twice (`turn_react.py:469-490`) and appends "Please continue with your next action." when it resumes after a tool result (`:221-232`).
- **Up to four `Model` layers per agent** (`ORBIT: orbit/solvers/orchestrator.py:213-237`), built through the private `Model._resolve_config` (`ORBIT: orbit/_model_config.py:8-17`). Any compromise attack or filtering defence puts every agent behind a filter wrapper that refuses Inspect's response cache (`ORBIT: orbit/defenses/model_filter.py:214-291`).
- **Its own concurrency.** Concurrent plan steps use raw asyncio (`agent_scheduler.py:352-377`), where Inspect code is expected to run on anyio.

ORBIT's users are swarm.md's user group 2, multi-agent safety and security evals. On inspect_swarm, security experiments and capability-scaling experiments share one runtime, one realized-cost ledger and one evidence model, and ORBIT stops maintaining a `react()` fork.

Most of a scheduled mode is ORBIT's own code. What ORBIT cannot build on top is narrower, and is what inspect_swarm provides: the runtime's knowledge that a member is held (without it the swarm reads a held member's status, time, waits and stop wrongly, [swarm-turn-seam.md Why](swarm-turn-seam.md#why)); a supported way to write a channel, since the bus's types and the calling member would otherwise be private; a preamble that says only what the controller makes true, since the swarm's own preamble says the members work at the same time; and a run ended by the sample's own limits, since by default the swarm derives caps from them, and a cap ends a run as a swarm stop that returns normally.

## What inspect_swarm provides

### S1. The turn seam

[Boundary callbacks](../docs/controllers.qmd#boundary-callbacks), as [swarm-turn-seam.md](swarm-turn-seam.md) designs them. `MemberHandle.on_boundary(callback, origin=...)` registers a callback that runs at every turn boundary of the member's own loop: boundary 0 before its first generation, and before each later one, recovered turns included. The callback gets a `Boundary` (`member`, `index`, `activation`, `after`, and copies of `messages` and the last `output`) and returns `None`, a replacement conversation, or `"end"`. While callbacks run the member is held: the swarm knows it, its time limit does not run, `swarm_wait` does not count it as active, and a stop or `end()` ends it at once, `stopped`. Callbacks run in registration order, each receiving the conversation as the ones before it left it, and the runtime writes one `changed` event per callback that changes the conversation, with its origin and the ids and hashes of what it added, removed or replaced, never the text. The seam rests on a private turn gate in inspect_ai's agent channel and a compaction reset in `react()`, which the swarm installs for every member.

Nothing in the seam is ORBIT's. ORBIT is one caller: its `activate()`, snapshots, attack input and defences are its own code over `on_boundary()`, and a hook ORBIT wires into its own `react()` (`on_continue`) would miss boundary 0 and every recovered turn and leave the swarm reading a held member wrongly ([Alternatives considered](#alternatives-considered)). Whether code at a boundary may read member text or add user- or system-role input is the caller's decision, not the swarm's.

### S2. A public bus API for channels

A channel with records goes through the bus, and a channel written outside the package does so with public, experimental names, exported from `inspect_swarm` ([Writing a channel](../docs/channels.qmd#writing-a-channel)). Besides those sketched below, `Record` is public: the record the bus makes from a draft, with the bound sender and resolved recipients, which `Delivery.record` holds and the channel state's `validate()`, `admit()` and `apply()` receive.

```python
# inspect_swarm/_bus/_record.py and _bus/_bus.py, exported from inspect_swarm

@dataclass(frozen=True)
class Draft:                # what a channel's tool hands to Bus.deliver()
    channel: str
    kind: str
    to: tuple[str, ...] = ()            # () is unaddressed: apply() decides whom it concerns
    payload: str = ""
    op: dict[str, JsonValue] | None = None
    wake: bool = False
    reply_to: str | None = None


@dataclass(frozen=True)
class Delivery:             # what Bus.deliver() returns for a delivered record
    record: Record
    seq: int                            # the commit sequence number across the run's channels
    notify: list[str]                   # as the channel's apply() returned them
    result: Mapping[str, JsonValue] | None


@dataclass(frozen=True)
class Caller:               # the member a channel's tool acts for
    member: str
    nested: bool                        # whether the calling loop is not the member's own
    loop_span_id: str | None


def calling_member() -> Caller | None: ...   # None outside a member of a swarm run


class Bus(Protocol):        # ChannelRun.bus, as a channel's open() receives it
    @property
    def stopping(self) -> bool: ...
    async def deliver(self, draft: Draft) -> Delivery: ...
    def read(
        self,
        channel: str,
        records: Sequence[str],
        *,
        depends_on: Sequence[str] | None = None,
        listing: bool = False,
    ) -> str: ...                            # writes the `read` event; returns its read id
```

- **`deliver()`** binds the sender from the calling member, resolves the addresses, and calls the channel's `validate()`; it then checks that the sender is still running, the payload and the channel's storm controls, calls `admit()`, writes the `sent` evidence, calls `apply()` and writes the `delivered` evidence, with no `await` between the checks and `apply()`. A refusal is a `ToolError`, which the calling tool's result shows; a channel name no channel of the swarm has raises `ValueError`, and a call outside a member of this run raises `RuntimeError`. Once the swarm is stopping, every `deliver()` is refused.
- **`read()`** writes the `read` `CommunicationEvent` for a tool result that shows the channel's records: the calling member, its span, the loop and `nested`, the channel and `records` as `record_ids`, and in `data` a new read id, the tool span, the records the calling loop newly depends on (`depends_on`, by default `records`, each written once per loop, which the [critical path](swarm-critical-path.md#what-channels-report) links), `listing` and `via`. It returns the read id (`r-5`). `listing` says the result showed what exists rather than its content, which the metrics count apart. An unknown channel or an id the channel did not commit in this run raises `ValueError`; a call outside a member of this run raises `RuntimeError`. The protocol takes no channel data for the event. The shared memory channel passes its `files` through a keyword of the run's bus class, `SwarmBus`, that the protocol leaves out.
- **`calling_member()`** is the runtime's answer to which member a tool acts for, never model output. `nested` is false in the member's own loop: the first loop in which the member resolves `swarm_tools()`, matched by both its agent channel and its agent span, or, until a loop binds, the loop its agent runs directly, in the member's agent span. So an agent run with `run()` from a member is nested even when it opens no agent channel. A bridged member's CLI is one loop, its own. The bus's `sent`, `delivered` and `read` events take `nested` from the same rule.
- **`ChannelRun.bus`** is the run's `SwarmBus`, typed as the `Bus` protocol, and a channel outside the package relies on the protocol alone. The built-in channels also use members of `SwarmBus` the protocol leaves out (`member()`, `nested()`, `status()`, `running()`, `roster`, `idle()`, `idle_spec()`, and `read()`'s `files=`). The protocol has no lifecycle query: a member's state is the runtime's, not the bus's, and `deliver()` already refuses a sender that is not running.

Two things ORBIT's channel needs are not bus features, because the channel interface already gives them:

- **Readers that have not started or have ended.** ORBIT's channels are blackboards: membership, not activity, decides who reads, and under a schedule the first post goes to readers still held at boundary 0. A draft addressed with `to=` is refused at resolution if a member it names has not started, is ending or has ended. ORBIT's channel therefore posts **unaddressed** records, computes its recipients itself from readers and edges, and returns them from `apply()`, in `notify` and in the `result` that the `delivered` evidence carries.
- **A commit sequence for `step_start` visibility.** `apply()` runs in commit order with no `await` around it, so a counter the channel increments in `apply()` is a commit sequence, known when the record is stored. Record ids are not one: an id is given once `validate()` accepts the record, before the liveness, payload and storm checks and `admit()`, so a record refused by those consumes an id. `Delivery.seq` numbers commits across the run's channels, but reaches the tool only after `apply()` has stored the record. ORBIT's own store counter, incremented in `apply()`, serves; it also counts ORBIT's own communication events (`ORBIT: orbit/communication/runtime.py:175-177`), so it orders messages and cutoffs, as today, without being a count of them.

### S3. A controller's preamble

`SwarmControl.start()` appends the member's preamble after its input as a user message, and the swarm's preamble tells a member that it is one of N agents "working on this task at the same time". A controller is the one place that knows how its members run, so it chooses the preamble when it starts a member (decision: Ransom, 2026-10-29: changing the preamble is more broadly useful, and simpler, than changing the conversation at boundary 0):

```python
# inspect_swarm, SwarmControl
def start(
    self,
    member: str | MemberHandle,
    input: str | list[ChatMessage] | None = None,
    *,
    preamble: str | Literal[False] | None = None,
) -> None: ...
```

- **`None`** gives the swarm's preamble: the member's name unless `choose_name=True`, its role and the roster size, the budget split, the sandbox statement and each channel's instructions.
- **A string** replaces all of it and is used as given, also for a member with `choose_name=True` ([swarm-chosen-names.md](swarm-chosen-names.md)). An empty or blank string raises `ValueError` saying to pass `False`.
- **`False`** adds no message. The input must then hold a `ChatMessageUser`, since a conversation without one would leave `react()` waiting for operator input; otherwise `start()` raises `ValueError` naming the member. A string `input` is always a user message.
- **Any other value** raises `TypeError`.
- The checks run before `start()` changes anything, so a refused call leaves the member pending.

[Controllers](../docs/controllers.qmd) documents the argument.

### S4. A budget that sets no caps of its own

By default the swarm derives cost, token and time caps from the sample's limits, holding back `reserve`, and a call that reaches a cap ends its member `swarm_cap` and the swarm returns normally after its stop procedure, so later solvers run. ORBIT's runs end at the sample's limits: the limit's error propagates out of ORBIT's solver, after its finalization, and no later solver runs. An explicit cap above each sample limit would leave the sample's limit to act, but a task cannot know the limits an eval will set, and every sample would log the swarm's warning for a cap above the sample's limit. So `Budget` has one field more:

```python
class Budget(BaseModel):
    ...
    derive: bool = True
    """Whether a cap left `None` is derived from the sample's limit. `False` sets no such cap: the sample's own limits then end the swarm as they end any solver."""
```

Under `derive=False`, `derive_caps()` derives nothing, so a cap left `None` is no cap; a cap given explicitly applies as before, and `reserve` has no effect. A split budget with neither a cost nor a token cap is refused with `ValueError`. A sample cost limit with no swarm cost cap still needs cost data for every member's model, which the swarm checks when it starts. [No derived caps](../docs/budget.qmd#no-derived-caps) documents the field.

### The conformance tests

`tests/test_orbit_shape.py` builds a scheduled harness the way ORBIT's port builds one, on the public API alone, with mockllm, on asyncio and trio, and no ORBIT dependency. Its stand-ins are a scheduler that holds each member in an `orbit:schedule` boundary callback (inside `suspend_working_limit()`) and lets it run one `model_step` or `completion` quantum on its go; a member builder that gives `react()` a continuation adapter as `on_continue`; a reader-and-writer channel on `Draft`, `deliver()`, `calling_member()` and `read()`; a `Model` wrapper that appends a system notice and a `ModelAPI` wrapper below the `ModelEvent` that prepends a safety prompt; and a `ToolSource` that wraps the channel tools `swarm_tools()` resolves. Its tests hold:

- **the message order** before the first model call, for a goal seed, a sample-input seed that opens with a system message, and a preamble string ([below](#the-message-order-before-the-first-model-call)), with one `changed` event per callback;
- **scheduling**: `model_step` alternating two members one generation each, with no continue prompt; a concurrent step waiting at its barrier while a held member stays held; a `completion` activation running a `submit=False` member to its tool-free reply and a submitting member to its submission;
- **tools behind ORBIT's wrappers**: a gate that refuses the channel's list tool before the channel's tool runs, a quarantine wrapper whose result replaces the channel's read result, and a gated submit tool given as `AgentSubmit(tool=)` whose denial records no submission and leaves the member running;
- **the channel**: a post reaching a reader held at boundary 0 without activating it, a step-start cutoff hiding a post committed during the step, and capacity checked in `admit()` giving the last place to exactly one of two concurrent posts;
- **errors**: a member's error cancelling its sibling and the controller, and propagating.

## ORBIT's executor

What the port maps from, in ORBIT 1.0.4.

**Agents.** `AgentSpec` (`ORBIT: orbit/configs/setup.py:79-164`) holds a name, an informational `role`, a model and temperature, a system prompt, tool names, `is_compromised`, a per-agent `max_messages` (default 50) and `submit`. The scheduled executor builds one `turn_react` per agent (`ORBIT: orbit/agents/topologies.py:443-531`), whose system message is the agent's prompt followed by ORBIT's assistant prompt and the submit prompt (`turn_react.py:72-81`, `:493-526`). A `submit=False` agent gets `react_no_submit` (`:390-455`) and stays in the rotation after a tool-free reply. A scheduled agent may have `tool` or `handoff` edges to other agents, each a fresh delegate of the target carrying the target's channel tools (`topologies.py:173-197`, `orchestrator.py:336-342`). `AgentGroup` gives agents a shared goal that replaces the sample input as their first user message (`ORBIT: orbit/configs/execution.py:22-38`).

**Channels.** `ChannelConfig` has an id, `readers`, `writers` and `private` (`ORBIT: orbit/configs/communication.py:12-29`). The tools are `channel_post`, `channel_read` and `channel_list` (`ORBIT: orbit/communication/tools.py:88-202`); `channel_read` returns NDJSON, one `{"id","sender","sequence","content"}` line per record. The sender is the agent name the tools were built with. A send goes to the channel's readers, minus the sender, intersected with the configured communication edges, and capacity is checked before anything changes (`runtime.py:246-269`). One store counter numbers messages and events, and all channel state is in store models (`ORBIT: orbit/communication/state.py:120-155`). `visibility="step_start"` freezes reads at the counter's value when the step started, through a ContextVar (`runtime.py:36-48`). Attacks register channels at run time, such as the collusion `secret:<name>` channel (`ORBIT: orbit/attacks/collusion/collusion_channel.py:63-67`).

**Delivery.** `delivery` is `poll`, `notify` (the default) or `auto`. For `notify` and `auto` the outermost `Model` wrapper derives one `ChatMessageSystem` per request and appends it last, never storing it (`ORBIT: orbit/communication/delivery.py:203-282`, `:330-343`): a notice of channel ids and unread counts, or an `auto` inbox of message bodies under an "untrusted messages" header.

**Scheduling.** `ExecutionConfig` (`execution.py:109-207`) selects round-robin, superstep or interleaved modes, or an explicit plan (`ORBIT: orbit/execution/plans.py:76-267`). A plan step runs agents in order, or concurrently with a barrier; one step is one outer turn. The quanta are `legacy`; `model_step`, one generation and its tool batch; `tool_step`, one tool call; and `completion`, until submission for a submitting agent and until a tool-free reply for a `submit=False` one. `max_messages` is an eligibility check before an activation starts. A trusted `after_activation` callback may amend pending steps; models are given no schedule editor. Messages never activate an agent.

**The outer loop.** `ExperimentScheduler.run_loop` repeats turns until a halt condition (`ORBIT: orbit/scheduler/scheduler.py:164-244`): the executor complete, `max_turns`, `max_time_seconds`, attack success, or a custom condition. Each turn (`orchestrator.py:907-1026`) activates runtime attacks and routes their messages; applies pending injections; runs pre-turn defence hooks over every agent's `AgentState` and captures each compromise prompt as the defences left it; runs the executor; and evaluates attack outcomes over the turn's new messages.

**Attacks that change a conversation.** A pending injection from `indirect_injection` with a `target_tool` becomes `ChatMessageUser("[Output from <tool>]: <payload>", metadata={"untrusted_observation": True})`, appended to the target's conversation, or to every unsubmitted agent's when there is no target (`orchestrator.py:937-952`). An `inject_prompt` compromise is a tagged `ChatMessageSystem` inserted after the target's leading system messages, once (`ORBIT: orbit/attacks/compromised/compromised.py:106-162`, `orchestrator.py:619-654`); a `CompromisedPromptFilter` re-adds it at the model boundary where a conversation lacks it.

**Defences.** Tool gates (`DefenseBoundary.wrap_tool`) wrap every tool, channel tools included, outermost, and turn a denial into `ToolError("Blocked by defense '<name>': <reason>")` (`ORBIT: orbit/defenses/boundaries.py:191-293`). Input filters run in a `ModelAPI` wrapper in priority order (`model_filter.py:127-211`). Output checks resample up to `max_resamples` and otherwise return a refusal with no tool calls (`boundaries.py:296-343`). `dual_llm` (`ORBIT: orbit/defenses/dual_llm.py`) replaces each tool result with a quarantined model's summary, and its pre-turn hook rewrites in place every message tagged `untrusted_observation` and not yet rewritten.

**Logging and exposure.** ORBIT writes `InfoEvent`s: `orbit.communication` evidence (`sent`, `read`, `notified`, `delivered`, `model_exposure`), `orbit_agent_started`, `orbit_schedule` events, `defense_*` events, and attack injections and outcomes. The `ModelEvent` records the request before ORBIT's input filters. ORBIT's observer, a `ModelAPI` wrapper below the filters, matches each read's tool result by the tool call id, function, agent and session captured at read time (`ORBIT: orbit/execution/provenance.py:17-52`), and notices by message id and text.

**The answer.** At the end, `sync_to_task_state` takes `output_agent`, else the last agent to submit, else the agent of the last step with results; projects that agent's conversation (`selected`) or every agent's (`all`) into `TaskState.messages`; and sets `TaskState.output` to that agent's output if its completion is not empty (`agent_scheduler.py:813-869`). It runs in the solver's `finally`, under `message_limit(None)` (`orchestrator.py:1038-1099`). Nothing records which agent's output was scored or whether it submitted.

## The integration path

### Where each ORBIT component goes

| ORBIT component | On the port | Supported by |
|---|---|---|
| `AgentSpec`, the member builder | ORBIT's registered `@agent`, which builds `react()` from the spec's fields | `member(orbit_member, args=..., name=, role=)` |
| `turn_react` | Removed for ported runs | `react()` with the turn seam |
| Group goals, sample input | ORBIT's controller | `start(member, input=[...])` |
| The preamble (ORBIT sends none) | ORBIT's controller | `start(member, preamble=False)` ([S3](#s3-a-controllers-preamble)) |
| Scheduler: modes, plans, quanta, barriers, `after_activation`, `max_messages`, halt conditions | ORBIT's `@swarm_controller` ([Scheduling](#scheduling-orbits-controller-over-the-seam)) | `on_boundary()`, `start()`, `events()`, stop reasons ([S1](#s1-the-turn-seam)) |
| Snapshots for eligibility and attack outcomes | ORBIT's controller, with final conversations in ORBIT's run state | `Boundary.messages` |
| Pending injections, `inject_prompt` compromise, runtime attack messages | ORBIT's `orbit:attack` callback | The seam; each change a `changed` event |
| Pre-turn defences (`dual_llm`) | ORBIT's `orbit:defense` callback | The seam |
| Channels, edges, capacity, cursors, secret channels, `step_start` visibility | ORBIT's `@swarm_channel`, with read cutoffs in ORBIT's run state | The public bus API ([S2](#s2-a-public-bus-api-for-channels)) |
| `notify` and `auto` delivery, input filters, output checks, exposure observer | ORBIT's `Model` and `ModelAPI` wrappers, unchanged | None needed ([ORBIT's model defences stay wrappers](#orbits-model-defences-stay-wrappers)) |
| Tool gates, `dual_llm`'s tool wrapper | ORBIT's tool wrappers, applied to its own tools and, through an ORBIT tool source, to the channel tools | `swarm_tools()` as a `ToolSource` |
| Submission order (`last_submitted`) | ORBIT's submit tool behind its defence chain; the registry in ORBIT's run state | `AgentSubmit(tool=)` |
| The sample's token, cost and time limits | ORBIT's task, which leaves them to end the run | `Budget(derive=False)` ([S4](#s4-a-budget-that-sets-no-caps-of-its-own)) |
| Answer selection, history projection, a record of the answer scored | ORBIT's solver wrapper around the swarm | Each member's output on the result record, read with `swarm_result()` ([The answer](#the-answer-and-a-record-of-the-answer-scored)) |
| `CommunicationState`, the collusion scorer, the viewer overview, scenarios, registries, scorers, YAML and CLI | ORBIT, unchanged | None needed |
| `legacy` and `tool_step` quanta, legacy observation modes, `legacy/v1`, delegation edges | ORBIT's executor | None ([What stays on ORBIT's executor](#what-stays-on-orbits-executor)) |

### What the port must keep

This design does not re-specify ORBIT's executor (decision: Ransom, 2026-10-30). A ported run behaves as it does on ORBIT's executor, except for [Behaviour a port changes](#behaviour-a-port-changes), and ORBIT's own tests prove it by running on both executors: its scheduler, topology and communication suites, plus a test for each edge below that they do not pin yet, written on ORBIT's executor first. The list is the one place this design claims parity; a probe that finds another edge adds a line to it.

- **Seeds.** A seed whose first message is a system message with the agent's rendered prompt gets no second copy, and the message the member keeps is the seed's own, with its id, source and metadata, since ORBIT's input filter checks system messages by their tags (`ORBIT: orbit/defenses/boundaries.py:158-175`).
- **Eligibility.** `max_messages` at the first activation, for a group goal and for a sample input that opens with a system message.
- **Quanta, barriers and halting.** Plan steps in order and concurrently, each halt condition, and the submit setting under each scheduling mode.
- **Continue prompts.** None after a tool-free reply that ends a `model_step` activation; within a submitting `completion` activation, the prompt `turn_react` adds today (`turn_react.py:350-372`).
- **Attack outcomes** of every activation, a member's final turn included.
- **Tools.** The defence chain on every tool, the submit tool and channel tools included; a denied submit records no submission; `last_submitted` after two concurrent submits where the first to submit ends last.
- **Broadcasts.** A pending injection with no target reaches every member unsubmitted when it falls due, once each, at the same place in each conversation.
- **Due interventions.** An injection or compromise due at a turn whose step skips its target is applied, in the form the pre-turn defences give it, at the target's first activation at or after that turn; one due for a target never activated again is in its final conversation and an `all` projection, with its evidence.
- **Defences before use.** With `dual_llm` on a compromised target, no model call carries the raw compromise where ORBIT's executor sends the defended one.
- **Committed changes only.** An intervention is recorded applied, with its evidence, exactly when it is in the target's final conversation, also after a pre-turn defence that raises and an injection the message limit refuses.
- **Read cutoffs.** `step_start` on every path that selects records (explicit reads, notices and `auto` inboxes), under each delivery mode.
- **Output checks.** An exhausted output check, with `retry_refusals` set and not; every resampled draw charged to the member's and the sample's token limits.
- **Sample limits.** A priced call over the sample's cost limit, a token limit and the time limit each end the sample on that limit after the same call, with ORBIT's answer and record set, and no later solver runs.
- **Finalization.** An `all` projection longer than the sample's message limit is kept whole; a finalization failure after a sample limit leaves that limit's error propagating.
- **The answer.** Answer selection and history projection under each rule; an empty submission keeps the output the task had before the swarm; a member output that begins with `attachment://` or `tc://` is scored as written.

**ORBIT's run state stays private to ORBIT.** The state the port adds for a run (each member's final conversation, the submission order, each member's read cutoff, the schedule state and the halt reason) lives in one ORBIT object per sample, shared by ORBIT's controller, member wrappers, channel and solver wrapper through a context variable the solver wrapper sets, which every task the swarm starts inherits; never under a sample-store key. The sample's store is shared by every member, and `tool_state_scope()` changes only the default namespace of tools that use it ([Members](../docs/members.qmd)), so in the store a peer's store tool could read a member's conversation or reorder submissions and so change the answer scored. ORBIT's existing channel state stays in its store models: the port moves no existing ORBIT state.

### Members

ORBIT's member builder is a registered `@agent` that takes the spec's fields as arguments, so the plan logs them, and builds `react()` from what ORBIT gives `turn_react` today: the agent's prompt with ORBIT's assistant prompt, its tools and submit tool behind the member's defence chain, its model layers, and its submit setting. Four things differ from `turn_react`:

- **Channel tools come through a tool source.** `swarm_tools()` is a `ToolSource` that gives a loop its channel tools only when the loop resolves its tools, in the member's context, binding the member's own loop, so a wrapper applied to the list passed to `react()` never sees them. ORBIT passes its own `ToolSource`, which resolves `swarm_tools()` in the same call and returns each tool through the member's chain. The conformance tests hold this shape.
- **The submit tool** is ORBIT's registry tool (`ORBIT: orbit/execution/submit_registry.py:115-167`), wrapped by the chain and given to `react()` as `AgentSubmit(tool=...)`. A denied submit neither records a submission nor ends the member.
- **Continuation.** Every member's `on_continue` is ORBIT's continuation adapter. Without one, a submitting `react()` appends its continue prompt after every tool-free reply, and `react(submit=False)` ends at its first tool-free reply; with one, a tool-free reply reaches a boundary with `after == "reply"`, carrying only the messages `turn_react` would have added.
- **The final conversation** is saved in ORBIT's run state by a synchronous `finally` in the member wrapper, inside the agent the swarm awaits, so it is saved before the swarm queues `MemberEnded`, however the member ends. The wrapper also enters ORBIT's communication identity for its observer.

### Scheduling: ORBIT's controller over the seam

ORBIT's scheduler is a `@swarm_controller` of its own: ORBIT's outer loop and `ScheduleRuntime` over the seam. It registers three callbacks on every member, in this order: `orbit:schedule`, `orbit:attack` and `orbit:defense`. It then starts each member with its seed and `preamble=False`, and each member parks at boundary 0.

- **Holding.** `orbit:schedule` parks a member at each boundary that ends an activation and waits for the scheduler's go inside `suspend_working_limit()`. Inside a `completion` activation it lets the member run on: a submitting member to its submission, a `submit=False` member to a boundary with `after == "reply"`. Activating a member sets its go and waits for its next park or its `MemberEnded`.
- **Snapshots.** Eligibility checks and attack evaluation read `Boundary.messages` at a park, and the saved final conversation for a member that ended during an activation. They, `after_activation` amendments and the `orbit_schedule` events are ORBIT's code, which absorbs any difference from what its executor reads; one is that at boundary 0 a snapshot already holds react's system message, which `turn_react` inserts only after the eligibility check.
- **Plan steps** use anyio: a sequential step activates its members in order, a concurrent step in a task group whose exit is the barrier. A member's error propagates as the swarm's foreign error and cancels its siblings and the controller.
- **Halting.** The controller returns ORBIT's halt reason, recorded as `controller:<reason>`. Returning requests the swarm's stop, which ends every held member `stopped`.
- **Messages never activate a member.** Only the controller's go does.
- **Limits.** ORBIT's task passes `budget=Budget(derive=False)`, so the sample's token, cost and time limits end a ported run. `max_turns` and `max_time_seconds` stay the controller's halt conditions, and `max_messages` its eligibility check.

### Interventions at a boundary

At a boundary that begins an activation, `orbit:attack` adds the attack input due for the member (pending injections, collusion prompts, the `inject_prompt` compromise, untagged runtime-attack messages), and then `orbit:defense` runs the pre-turn defences (`dual_llm`'s rewrite), so a defence sees attack input before the member's next generation. What they add, where and in what form is ORBIT's code as today (`orchestrator.py:602-654`, `:937-973`). The seam applies their result and records one `changed` event per origin; ORBIT keeps writing its own content records (`defense_activity`, `orbit.attack_injection`) beside them.

### The channel

ORBIT's `channel_post`, `channel_read` and `channel_list` become the tools of one ORBIT `@swarm_channel` with `reach="loop"`, whose state keeps ORBIT's store-backed registry, messages, receipts and cursors, attack-registered secret channels included. Its semantics are ORBIT's. From the swarm it takes:

- **Delivery.** A post is an unaddressed `deliver()` whose recipients ORBIT computes and `apply()` returns. Capacity is checked in `admit()` and ORBIT's counter incremented in `apply()`, with no `await` between them, so two concurrent posts cannot both take the last place.
- **The caller.** The tools find the posting or reading member with `calling_member()`. No ported member has a nested loop that uses the channel, since configurations with delegation edges stay on ORBIT's executor.
- **Evidence.** A read calls `read(<channel>, [...])` with the bus record ids of the messages it rendered, beside ORBIT's own read evidence. The bus writes the swarm's `sent`, `delivered` and `read` events for ORBIT's records, which the swarm's ledger, metrics and critical path count; ORBIT keeps writing its own `orbit.communication` events and `CommunicationState`, each carrying the swarm's record id, so the two join.
- **Waking.** `wake=False`: no ORBIT record wakes a wait or activates a member.
- **Instructions.** `instructions()` returns `None`, since ORBIT's tools describe themselves and its members get no preamble.

### Delivery, exposure and filters

ORBIT's model layers stay as its builder builds them, in today's order: delivery outermost, the output check, the filter wrapper (which records the `ModelEvent`), the observer below the filters, then the provider. Nothing in the swarm changes them. `notify` and `auto` are computed per request from ORBIT's channel state for the member the builder was built for, under that member's `step_start` cutoff, which a member's long-lived task reads from ORBIT's run state rather than a context variable the controller sets. The observer matches reads by the tool call id captured at read time; the swarm puts no metadata on tool results. Exposure and coverage stay ORBIT's events.

### ORBIT's model defences stay wrappers

ORBIT keeps its model wrappers on the port, built through the private `_resolve_config`, and a filtered member still refuses the response cache (decision: Ransom, 2026-10-30). Moving them onto sentinel generate-stage protocols, which would drop `_resolve_config` and let filtered members use the cache, is later work for inspect_sentinel's design of its generate stages' second part (`design/generate-stages.md`, sections c and d), where `modify`, a replaying `reject` and `resample` are deferred. As input to that design, not a contract specified here, the move would need:

- **A refusal outcome.** A stage can return an output with `stop_reason="content_filter"` and no tool calls, as ORBIT's output check does when it exhausts its resamples, so `react()`'s refusal retries and its exit after three refusals keep working.
- **Charged draws.** Every draw a protocol makes in place of the agent's output is charged to the member's and the sample's token limits, as ORBIT's wrapper charges each resample; protocols run under `suspend_token_limit()`.
- **Helper calls marked.** ORBIT's auxiliary model calls, such as `dual_llm`'s quarantine model at a boundary, run in the member's task and span, so ORBIT marks them with `model_origin("helper")`, and member-keyed filters and the observer skip them.
- **An opt-in detailed record of a modify.** Sentinel's default stays (a `BeforeGenerate` modify keeps no copy of the request), and a protocol that rewrites a request can opt in to recording its change by message id, with the original content of each message removed or replaced, so the log keeps what it holds today (decision: Ransom, 2026-10-29: the default is not changed without the people who own it).

### The answer, and a record of the answer scored

ORBIT's task wraps the swarm in a solver of its own, which runs the swarm agent with `run()` on an empty input: ORBIT's controller gives every member its seed through `start(member, input=...)`. The swarm's final step, which sets its state's messages to its input followed by the answer's message under the sample's message limit, therefore holds only the answer's message, while every member's conversation stays under that limit; and `run()` leaves the `TaskState` to ORBIT's wrapper. After the swarm, in a synchronous `finally` and under `message_limit(None)`, the wrapper selects the answer by ORBIT's rule and projects history as `sync_to_task_state` does, reading each member's output from the result record with `swarm_result()` ([swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result)), and each member's conversation and the submission order from ORBIT's run state. When ORBIT's rule selects no nonempty completion, it keeps the output the task had before the swarm. An exception that ended the swarm propagates unchanged, after the output, the messages and the answer record are set; a failure in the wrapper's own finalization never replaces it, and is added to it as a note.

**What the record must give.** The swarm runtime holds ended members' outputs in memory and puts none on the record: `MemberResult` has state, status, submission and times. [swarm-scoring.md What the record adds](swarm-scoring.md#what-the-record-adds) adds each member's output, captured when the member ends and published with the record when the swarm exits. The record must also be written, and read back, whatever literal text the outputs hold: the record's writer refuses any string that would read back from the log as other text, and its reader refuses any string that reads as an unresolved attachment reference, so one member output that begins with `attachment://` or `tc://`, which ORBIT's experiments put there on purpose, would make the record unwritable or every member's output unreadable. The reader must tell a member's literal text from a reference left unresolved and refuse only a reference it cannot resolve (decision: Ransom, 2026-10-30, which covers those prefixes and a writer's marker of literal text; a base64 `data:` URI and invalid UTF-8 in an output are for the result-record work to decide). Both are the result-record designs' changes ([Not this design](#not-this-design)), and the port's answer path waits on them.

**The answer record.** ORBIT writes it to the store under `orbit:answer`, so a re-scored log keeps it, once every member has ended, so no member's tool reads or changes it, and as an ORBIT `InfoEvent` (`{"orbit_answer": ...}`) in the solver's span:

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `selected` | the member ORBIT's rule selected; `None` when none was |
| `rule` | `output_agent`, `last_submitted` or `last_result`; `None` when none selected |
| `member` | the member whose output is `TaskState.output`; `None` when the earlier output was kept |
| `status`, `submitted` | `member`'s status and submission, from the swarm's record; `None` without a member |
| `message_id` | the id of `TaskState.output`'s message when `member` is set |
| `swarm_final` | the swarm's own `final`: its mode, member and reason |
| `agrees` | whether `member` is set and is the swarm's `final.member` |
| `provisional` | whether the swarm's record was provisional |
| `halt_reason` | ORBIT's halt reason; `None` when the swarm ended before the controller returned |

Before it returns, ORBIT's controller computes the same selection; when the selected member ended `submitted`, it calls `choose_answer(member, reason=rule)`, so the swarm's own `final` names that member. `choose_answer()` refuses a member that did not submit, so when the selected member did not, the swarm's `final` follows the controller's `final` rule over the others' submissions, `agrees` is false, and only ORBIT's record names what was scored. The swarm's rule and record are unchanged.

### Logging

| What | Written by | As |
|---|---|---|
| Communication on ORBIT's channel | the swarm's bus | `sent`, `delivered`, `read` communication events ([swarm-event-types.md](swarm-event-types.md)) |
| ORBIT's communication evidence, exposure and coverage | ORBIT's channel and observer | `orbit.communication` `InfoEvent`s and `CommunicationState` |
| Holds and changes at boundaries | the seam | `boundary` and `changed` swarm events, one `changed` per ORBIT origin |
| Attack and defence content and decisions | ORBIT | `orbit.attack_injection`, `orbit.attack_outcome`, `defense_*` |
| The schedule | ORBIT's controller | `orbit_schedule` events; the swarm's `boundary` events give each hold's duration |
| Member results, the swarm's answer, ledger, metrics | the swarm | `member_result`, `final`, `ledger`, `metrics` swarm events and the result record |
| The answer scored | ORBIT's solver wrapper | `orbit:answer` and its `orbit_answer` event |
| Model calls | inspect_ai | `ModelEvent`s, recording the request before ORBIT's filters |

ORBIT stops writing `orbit_agent_started` once per activation: a member is one `react()` invocation in one span, and its activations are ORBIT's schedule events inside it.

### The message order before the first model call

For a member of the port with everything present, the first generation is assembled in this order:

1. **react's system message**, ORBIT's agent prompt with its assistant prompt, inserted at index 0 when the loop starts. `react()` inserts its prompt without looking at the seed, so when the seed already opens with the agent's rendered prompt the conversation holds two copies; there ORBIT's port suppresses react's copy and keeps the seed's own message, with its id, source and metadata, as [What the port must keep](#what-the-port-must-keep) requires; the member builder chooses how. The order below is for a seed without that prompt.
2. **The start input**, ORBIT's seed: the group goal as a user message, or a copy of the sample input, whose leading system messages come first.
3. **No preamble** (`preamble=False`). A controller that passes text instead gets it here, as a user message after the seed.
4. **Boundary 0**, the callbacks in order: `orbit:schedule` holds the member until its first activation and changes nothing; `orbit:attack` inserts collusion prompts, then the `inject_prompt` compromise, after all the leading system messages, then appends untagged runtime-attack messages and pending injections; `orbit:defense` rewrites tagged messages in place. The seam applies the result and writes its events.
5. **`before_turn()`** adds no operator message; compaction, below its threshold, sends the conversation as it is.
6. **ORBIT's delivery wrapper** appends the `notify` notice or `auto` inbox last.
7. **Inspect prepares the input**, collapsing consecutive messages of a role for providers that need it, and records the `ModelEvent`.
8. **ORBIT's input filters**, below the event: the memory context appended, safety and vaccination prompts prepended, and boundary monitoring's redactions.

The provider therefore receives `[safety prompts] [agent prompt] [the seed's leading system messages] [collusion] [compromise] [the rest of the seed] [runtime attack messages] [pending injections] [notice] [memory context]`, ORBIT's order today without the legacy observations and dialogue projections that ported runs do not use. The conformance test holds the swarm's part of this order at both points where it is visible, the `ModelEvent`'s input and the request the provider receives, with stand-ins for the wrappers and callbacks.

### A worked example

ORBIT's `common_channel` example (`ORBIT: examples/common_channel.py:18-38`) has two agents, `writer` and `reader`, both `submit=False`, a channel `team` that both read and only `writer` writes, `notify` delivery, the plan `[[writer, reader], [reader]]` with `model_step`, and `output_agent="reader"`. This example adds four entries from ORBIT's registries: `indirect_injection` with `target_tool="channel_read"` and `target_agent="reader"`; `compromised_agent` in `inject_prompt` mode on `writer`, before deployment; `dual_llm` on `reader`; and `tool_wrapper` on `writer` with `blocked_tools=["channel_list"]`. ORBIT's task builds:

```python
swarm(
    members=[
        member(orbit_member, args=writer_spec_fields, name="writer", role="writer"),
        member(orbit_member, args=reader_spec_fields, name="reader", role="reader"),
    ],
    controller=orbit_scheduler(experiment=config_json),
    channels=[orbit_channel(communication=communication_json)],
    budget=Budget(derive=False),
)
```

wrapped in ORBIT's solver.

1. The controller registers the three callbacks on both members and starts both with the sample input and no preamble. Each parks at boundary 0.
2. The pre-deployment attacks run: the compromise is due for `writer`; the injection is queued for `reader`.
3. Turn 0, step `[writer, reader]`, sequential:
   - `writer`: at boundary 0, `orbit:attack` inserts the compromise after the system message, one `changed` event. The first request is `[agent prompt, compromise, input]`. It calls `channel_post`; the post is delivered unaddressed with recipients `[reader]`. Its next boundary (`after == "tools"`) ends the activation.
   - `reader`: `orbit:attack` appends `[Output from channel_read]: <payload>`, and `orbit:defense` has `dual_llm` rewrite it under the same id, from a quarantined call charged to the reader: two `changed` events. The first request is `[agent prompt, input, quarantined summary, notice "team: 1 unread"]`. Its `channel_read` result is replaced by `dual_llm`'s tool wrapper. Its next boundary ends the activation.
4. Turn 1, step `[reader]`: the reader gives a tool-free reply, reaching a boundary with `after == "reply"`.
5. The plan is exhausted and the controller returns `schedule_exhausted`. The stop ends both held members `stopped`; neither submitted, so the swarm's `final` is `no_submissions`.
6. Once the record carries member outputs ([What the record must give](#the-answer-and-a-record-of-the-answer-scored)), ORBIT's solver wrapper sets `TaskState.output` to the reader's output from the record and the messages to the reader's saved conversation, and writes `orbit:answer` with `selected` and `member` both `"reader"`, `rule="output_agent"`, `submitted=false` and `agrees=false`.

Had the writer called `channel_list`, the tool gate ORBIT's tool source put on it would have refused it with `ToolError("Blocked by defense '<name>': Tool 'channel_list' is blocked")`, and the turn would have gone on to its boundary as any tool error does.

### Behaviour a port changes

Runs on the two runtimes should not be pooled unless these are accepted or controlled for; ORBIT records the runtime in each sample's metadata.

- **No resume prompt.** A held member never left its loop, so "Please continue with your next action." is never added.
- **Interventions and pre-turn defences apply at the target's first activation at or after they fall due**, not at the start of the turn for every member. A member's conversation does not change while it is parked, so they land in the same place.
- **Defence work is the member's.** `dual_llm`'s pre-turn quarantine calls run at the member's boundary, so they count against its limits and its ledger line, and as working time unless ORBIT marks them otherwise.
- **Conversational members end `stopped`** at the swarm's stop, and the swarm's own answer is a submission only; ORBIT's answer record names what was scored.
- **Member time limits exclude held time.**
- **One span per member**, with `react()`'s own features (compaction, refusal retries, the agent channel) available.
- **Unrecovered context overflow ends a member.** `react()` returns, and the swarm records the member `unsubmitted`, where `turn_react` ends only the activation (`ORBIT: orbit/agents/turn_react.py:254-262`). The port keeps react's ending (decision: Ransom, 2026-10-30).
- **Three refusals in a row end a member's loop**, as `react()` stops after three consecutive `content_filter` outputs, which ORBIT's output check returns each time it exhausts its resamples.
- **A rewrite at a boundary resets compaction**, where compaction is on.
- **Unsupported members** are refused: background-dispatching `deepagent()` and bridged members.

### What stays on ORBIT's executor

ORBIT's port refuses each of these with a `ValueError` naming the setting, and they keep running on ORBIT's executor:

- The `legacy/v1` profile, the `legacy` quantum and legacy channel formats.
- The `tool_step` quantum: refusing a batch of several tool calls before any runs needs a change to `react()`.
- The `summary` and `peer_messages` observation modes, and dialogue and activity projections.
- The topology executor and persistent delegation sessions.
- Samples whose seed has no user message. `start(preamble=False)` refuses such an input; ORBIT's controller checks the seed first and raises its own error naming the sample and the agent.
- Scheduled agents with `tool` or `handoff` edges. A fresh delegate runs in its caller's member, so `calling_member()` names the caller, and the delegate's channel tools, cursors, sender and defended compromise would need a delegate identity the swarm does not have.

## Decisions

- **Red-team features stay ORBIT's** (decision: Ransom, 2026-10-07): attacks, defences and their registries are ORBIT code. The swarm provides general features (the seam, the public bus API, the preamble, `derive=False`), and scheduling, snapshots, interventions, the channel's semantics and answer selection are ORBIT's code over them.
- **A controller chooses the preamble at `start()`** (decision: Ransom, 2026-10-29).
- **The swarm writes no exposure event** (decision: Ransom, 2026-10-15); exposure stays ORBIT's observer's events, joined to the swarm's by record id.
- **ORBIT's executor is not re-specified here**; parity is proved by ORBIT's tests on both executors, and review of this design blocks only on the contracts the seam, the bus, the preamble and the budget expose (decision: Ransom, 2026-10-30).
- **ORBIT's model defences stay wrappers** (decision: Ransom, 2026-10-30); the sentinel move is inspect_sentinel's design, and its default for a modify is not changed without its owners (decision: Ransom, 2026-10-29).
- **The port keeps `react()`'s ending on unrecovered context overflow** (decision: Ransom, 2026-10-30).
- **The result record's reader reads literal member text**, those prefixes included (decision: Ransom, 2026-10-30).

## Alternatives considered

**ORBIT's own `on_continue` hook, with no seam.** Needs no swarm change. But it misses boundary 0 and every recovered turn, and the swarm reads a member held there as running: its status after release, its time limit, `swarm_wait` and the stop are all wrong ([swarm-turn-seam.md Why](swarm-turn-seam.md#why)). Rejected for the seam.

**A swarm-owned scheduled mode**: `activate()`, `snapshot()`, staged `append()` and `edit()`, an `intervened` event and a `round_robin` controller in inspect_swarm. One implementation for every scheduled harness. But it puts ORBIT's policy into swarm API before a second user asks, and holders compose badly where callbacks compose by order ([swarm-turn-seam.md Alternatives considered](swarm-turn-seam.md#alternatives-considered)). Rejected: each is a few lines of ORBIT's controller over the seam, and `changed` is the record.

**Importing the bus's private names.** ORBIT could import from `inspect_swarm._bus`, as the built-in channels do. But private names change without notice, and the sender binding and the evidence must be the bus's, not a copy. Rejected for the public protocol.

**Bus extensions for ORBIT's channel**: a `recipients="roster"` channel attribute so that addressed records reach pending and ended readers, and a commit sequence handed to `apply()`. Rejected: an unaddressed record with recipients the channel computes reaches the same readers, and `apply()` already runs in commit order.

**Exposure reporting through the swarm** (`record_exposure()`, coverage declarations and kept notice texts on the bus). The swarm would only relay ORBIT's claims, which ORBIT's own events already carry. Rejected.

**Naming a read in model input**, by stamping read results with the read id in metadata. Not needed: ORBIT's observer joins by the tool call id it captures at read time.

**Choosing a member that did not submit as the swarm's answer** (`choose_answer(member, allow_unsubmitted=True)`). The swarm's `final` would always name ORBIT's answer, but it contradicts the rule that only submissions are answers, which the record's readers rely on. Rejected for ORBIT's own record beside the swarm's.

**ORBIT removing or replacing the preamble at boundary 0**, from its own callback. Needs no swarm change. But it edits the conversation to undo what the swarm put there, costs a compaction reset, and serves only ORBIT. Rejected (decision: Ransom, 2026-10-29).

**A preamble option on `member()` or `swarm()`.** Whether members work at the same time is the controller's fact, not the member's or the roster's, and `start()` already takes the member's input. Rejected.

**`Budget(reserve=0)` for ORBIT's limits.** No swarm change. But a derived cap still ends the run as a swarm stop that returns normally, so later solvers run where ORBIT's executor ends the sample. Rejected for `derive=False`.

**Outputs saved by ORBIT's member wrapper** instead of read from the record. Works on top, but the swarm's answer and ORBIT's would come from two objects, which can differ for a member ended at a boundary or by a hard stop. Rejected: ORBIT reads the record's outputs with `swarm_result()`.

**Selecting `last_submitted` by the record's end times.** A member's end time is not its submit time: a member whose submit ran first can end after another while a second tool of its batch still runs. Rejected for ORBIT's own submission order.

**ORBIT's channel off the bus**, keeping its own runtime and tools. Needs no public bus API, but the swarm's ledger and metrics would not see ORBIT's communication, and a send after the swarm began to stop would not be refused. Rejected as the recommended path; it remains possible.

**One `run()` per activation on a stored `AgentState`.** Closest to ORBIT today. But it needs clean re-entry of `react()`, loses per-run state (attempts, compaction, MCP connections, the agent channel), and rebinds the member on every activation. Rejected.

## Compatibility

- **inspect_swarm** is experimental throughout. The public bus names are `Draft`, `Record`, `Delivery`, `Caller`, `calling_member` and `Bus`; the built-in channels write their events with the same schemas. A custom agent's `run()` descendants count as nested loops, which affects message restrictions, shared memory's own-loop tracking and status lines, and wait ownership for them. `start(preamble=)` and `Budget.derive` default to the behaviour without them.
- **ORBIT logs.** Logs already written keep their readers. A ported run writes ORBIT's events as today, plus the swarm's communication and swarm events and ORBIT's answer record; ORBIT's store models and viewer overview are unchanged.
- **Python and concurrency.** inspect_swarm requires Python 3.11+ and runs on asyncio and trio, so ORBIT's controller and callbacks use anyio.

## Security

- **Callbacks, the controller and the channel are trusted ORBIT code** in the runner process, as ORBIT's extensions are (`ORBIT: docs/security-extensions.md:159-160`). None is reachable from a member's tools, and no model output decides a schedule.
- **Peer text in a conversation.** The swarm's rules for peer text govern its own channels. ORBIT's experiments put attacker text in user and system roles on purpose; that is ORBIT's experiment design. Every change at a boundary is a `changed` event with its origin and hashes; per-request text (notices, inboxes) is in the `ModelEvent` input.
- **Sender identity.** `deliver()` binds the sender from the calling member, and `calling_member()` reads the same runtime context, never model output; a channel cannot set either.
- **Read evidence.** `read()` refuses a caller outside the run and ids the channel did not commit, and writes only ids and flags, no model text.
- **The preamble** a controller passes is controller code's text, as `input` is; nothing a member wrote reaches it through the swarm. `start()` checks only its type and that it is not blank.
- **ORBIT's run state** is never in the sample's store, so no member's tool can read a peer's private conversation or reorder submissions to change the answer scored. The submit tool records the member it was built for, never a name from its arguments, and runs behind the member's defence chain.
- **Members' outputs on the result record** are read by ORBIT's solver wrapper after the swarm; who can read the record, and when, is [swarm-result-record.md Security](swarm-result-record.md#security)'s.
- **Untrusted input in ORBIT's code** (tool arguments to gates, channel payloads, model output to filters) is handled by ORBIT as today; the port moves none of it into the swarm.

## Not this design

- **Member outputs on the result record, and literal output that reads as a reference.** The port's answer path reads each member's output from the record ([The answer](#the-answer-and-a-record-of-the-answer-scored)). Adding `MemberResult.output` is [swarm-scoring.md What the record adds](swarm-scoring.md#what-the-record-adds)'s; making the writer and [reader](swarm-result-record.md#reading-it-swarm_result) keep a member's literal text that begins with `attachment://` or `tc://` is approved (decision: Ransom, 2026-10-30) and is the result-record design's and its implementation's to make, for example with a writer's marker of literal text.
- **Other references to the earlier, proposal form of this one**: anchors such as P1 to P5, Spike, ORBIT's solver wrapper and Open questions, linked from swarm.md, swarm-api.md, swarm-communication.md, swarm-memory-channel.md, swarm-implementation-plan.md, swarm-result-record.md, swarm-topics-channel.md and swarm-turn-seam.md; their ORBIT rows and future-work items (`ActivationPolicy`, `round_robin`, `member(input=)`, routes, `tool_result_metadata()`, ORBIT event kinds, swarm.md's turn-hook item); swarm-result-record.md's ORBIT wrapper and its member wrapper saving conversations to an ORBIT store key, where this design keeps them in ORBIT's run state; and the header of `tests/test_orbit_shape.py`, which cites this design's former Testing section, now [The conformance tests](#the-conformance-tests). To be aligned in a follow-up.
- **`tool_step` on `react()`**, and replacing a member's agent mid-sample.
- **Scheduling a member's background descendants**, which the seam refuses.
- **Conversation identity for descendants**, which ORBIT's persistent delegation tracks.
- **A board channel with readers and writers in inspect_swarm**, which could take ORBIT's names when it is built.
- **ORBIT's other Inspect gaps** that a port does not need: a public effective-config accessor, `active_model`, a handoff predicate, `as_tool()` keeping `model_input`, and the interrupted `ToolEvent`'s null fields.
- **A controller reading the swarm's preamble** to extend it rather than replace it. No user needs it: ORBIT sends none.
- **A possible ORBIT bug**, for the reply to the ORBIT team: `memory_poisoning` queues a pending-injection entry without `applied` (`ORBIT: orbit/attacks/injection/memory_poisoning.py:104-112`), and the pending-injection step does not skip it (`orchestrator.py:939-952`), so as read it is also broadcast as `[Output from unknown]: ...`. Not run.
