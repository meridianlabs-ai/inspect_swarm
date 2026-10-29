# ORBIT on inspect_swarm

## Overview

ORBIT, the Inspect-based framework for multi-agent security experiments, could run its scheduled experiments on inspect_swarm while ORBIT's own code keeps doing almost everything that makes them ORBIT experiments. Its agents become ordinary `react()` members of a swarm, so its fork of `react()` goes. Its scheduler becomes a swarm controller that holds every member at the [turn seam](swarm-turn-seam.md) and lets members run one quantum at a time. At the same boundaries, ORBIT's own callbacks add attack input and let its defences rewrite a member's conversation, and the seam records every change they make. Its reader-and-writer channels become one channel of its own on the swarm's bus. Its exposure observer, input and output filters, notices, answer selection, attacks, defences and scorers stay ORBIT code, unchanged where they can be.

inspect_swarm adds only what ORBIT cannot build well on top: the turn seam itself, so that the runtime knows a member is held; a public API for writing a channel on the bus; and each member's output on the swarm's result record, so that ORBIT can choose and record the answer it scores. inspect_ai has two prerequisites besides the seam's own turn gate: sentinel generate stages that can rewrite a request and resample an output, which would let ORBIT's model-boundary defences stop wrapping `Model`, and a log of what such a stage changed. Until those exist, ORBIT keeps its model wrappers as they are.

This document says, for each ORBIT component, where its code lives on the swarm and which hook supports it; works through a two-agent ORBIT experiment with an injection, a compromise, a quarantine defence and a tool gate; gives the exact message order before a member's first model call; and lists the behaviour a port changes.

## Background

A deep dive on one topic of [swarm.md](swarm.md): how ORBIT, the framework compared in [Relationship to ORBIT](swarm.md#relationship-to-orbit), could run on inspect_swarm. It builds on:

- [swarm-turn-seam.md](swarm-turn-seam.md) for boundary callbacks, held members, `end()` and the `boundary` and `changed` evidence;
- [swarm-api.md](swarm-api.md) for controllers, member handles and stopping;
- [swarm-communication.md](swarm-communication.md) for the bus and channels;
- [swarm-scoring.md](swarm-scoring.md) for the final answer and the result record;
- [swarm-event-types.md](swarm-event-types.md) for the swarm's evidence events;
- [swarm-limits.md](swarm-limits.md) for statuses, member limits and the stop procedure.

It keeps the standing decisions of those designs. The ones it leans on most:

- Peer messages reach members as tool output with distinct provenance (decision: Ransom, 2026-10-07), and peer text enters the user role only in an explicit, opt-in arm (2026-10-09). These govern the swarm's own channels.
- The swarm adds no message to a member's conversation: no notices and no turn hook of its own (2026-10-27).
- Code at the seam is the caller's: what it reads and what it adds, in which role, is the caller's decision, and the seam records it ([swarm-turn-seam.md Non-goals](swarm-turn-seam.md#goals-and-non-goals)). A member ended at a boundary is `stopped`, never a submission (2026-10-28).
- Only submissions are the swarm's answers ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)).
- The swarm writes no exposure event: exposure is ORBIT's observer's (2026-10-15).

Code references are to ORBIT 1.0.4 (tag `v1.0.4`, `076104ae`, [github.com/wlanderson0/orbit](https://github.com/wlanderson0/orbit)), prefixed `ORBIT:`; inspect_ai `main` at `6f5f9c20a`; inspect_sentinel `main` at `e9bbcf2`; and inspect_swarm `main` at `33bf17b`. Paths are relative to each repository's root.

## Why

ORBIT pays for running outside a substrate:

- **A forked `react()`.** `turn_react` (`ORBIT: orbit/agents/turn_react.py:1-26`) exists so that a scheduler can stop an agent after one quantum and call it again later. Its signature (`:90-104`) has no `compaction`, `retry_refusals`, `approval` or `review`, and its loop opens no agent channel and no checkpointer; inspect_ai's `react()` has all of them (`src/inspect_ai/agent/_react.py:57-72`). It reimplements submission, attempts, overflow handling and continue prompts (`turn_react.py:148-173`, `:283-381`, `:529-555`), and imports private inspect_ai modules (`:31`, `:50`, `:66-67`).
- **Re-entry on a stored conversation.** Each activation is a fresh `run()` on the agent's stored `AgentState` (`ORBIT: orbit/execution/agent_scheduler.py:583-588`), so the fork guards against inserting the system prompt twice (`turn_react.py:469-490`) and appends "Please continue with your next action." when it resumes after a tool result (`:221-232`).
- **Up to four `Model` layers per agent** (`ORBIT: orbit/solvers/orchestrator.py:213-237`, `orbit/agents/topologies.py:85-123`): a notice and inbox wrapper, an output check that resamples, an input filter wrapper that refuses Inspect's response cache because the cache lookup sits above the filters (`ORBIT: orbit/defenses/model_filter.py:214-291`), and an exposure observer below the filters. Building them needs the private `Model._resolve_config` (`ORBIT: orbit/_model_config.py:8-17`), which ORBIT's maintenance notes call "a private compatibility boundary" (`ORBIT: docs/core-maintenance.md:53-59`). Any compromise attack or filtering defence puts every agent behind the filter wrapper, so the whole run loses response caching (`orchestrator.py:172-176`, `:214-224`).
- **Its own concurrency.** Concurrent plan steps use raw asyncio (`agent_scheduler.py:352-377`), where Inspect code is expected to run on anyio.

ORBIT's users are swarm.md's user group 2, multi-agent safety and security evals. On inspect_swarm, security experiments and capability-scaling experiments would share one runtime, one realized-cost ledger and one evidence model, and ORBIT would stop maintaining a `react()` fork.

Most of a scheduled mode can be ORBIT's own code. What ORBIT cannot build on top is narrower: the runtime's knowledge that a member is held (without it the swarm reads a held member's status, time, waits and stop wrongly, [swarm-turn-seam.md Why](swarm-turn-seam.md#why)); a supported way to write a channel, since the bus's types and the calling member are private; the members' outputs after the swarm, from the object the swarm's own answer comes from; and, in Inspect, model-boundary hooks that rewrite and log what they rewrote.

## Goals and non-goals

### Goals

- For each ORBIT component, say whether it stays ORBIT code and which inspect_ai or inspect_swarm hook supports it.
- Specify what inspect_swarm adds, each with why ORBIT cannot do it well on top.
- Specify inspect_ai's prerequisites, what each is and why ORBIT needs it, for each to be designed in inspect_ai.
- Show ORBIT's scheduler as its own controller over the seam.
- Work one ORBIT experiment through the port: members, channel, schedule, an injection, a compromise, a quarantine defence and a tool gate.
- Say what ORBIT logs, what the swarm logs, and what inspect_ai must add.
- Give an explicit record of the answer actually scored.
- Give the exact message order before a member's first model call, and an integration test that holds it.

### Non-goals

- **Writing ORBIT's port**, or deciding whether ORBIT adopts inspect_swarm.
- **Designing the inspect_ai prerequisites.** This document says what they are and why; each gets its own design in inspect_ai or inspect_sentinel.
- **Changing the turn seam's contract.** ORBIT is one caller of it.
- **Red-team features in inspect_swarm.** Attacks, defences and their registries stay ORBIT's (decision: Ransom, 2026-10-07).
- **ORBIT configurations that do not fit a held `react()` loop**: the `legacy` and `tool_step` quanta, the legacy observation modes and the `legacy/v1` profile ([What stays on ORBIT's executor](#what-stays-on-orbits-executor)).

## Current behaviour

### ORBIT 1.0.4

**Agents.** `AgentSpec` (`ORBIT: orbit/configs/setup.py:79-164`) holds a name, an informational `role`, a model and temperature, a system prompt, tool names, `is_compromised`, a per-agent `max_messages` (default 50, `:137`) and `submit`. The scheduled executor builds one `turn_react` per agent (`ORBIT: orbit/agents/topologies.py:443-531`), whose system message is the agent's prompt followed by ORBIT's own assistant prompt, "Do not output more than one tool call...", and the submit prompt (`turn_react.py:72-81`, `:493-526`). A `submit=False` agent gets `react_no_submit` (`:390-455`) and stays in the rotation after a tool-free reply. `AgentGroup` gives agents a shared goal that replaces the sample input as their first user message (`ORBIT: orbit/configs/execution.py:22-38`, `agent_scheduler.py:243-254`).

**Channels.** `ChannelConfig` has an id, `readers`, `writers` and `private` (`ORBIT: orbit/configs/communication.py:12-29`).

- The tools are `channel_post`, `channel_read` and `channel_list` (`ORBIT: orbit/communication/tools.py:88-202`). `channel_read` returns NDJSON, a header then one `{"id","sender","sequence","content"}` line per record, a format kept for exact exposure matching (`:152-160`).
- The sender is the agent name the tools were built with (`orchestrator.py:336-342`, `tools.py:127`); a ContextVar identity only cross-checks it (`ORBIT: orbit/communication/runtime.py:117-124`).
- A send goes to the channel's readers, minus the sender, intersected with the configured communication edges (`runtime.py:246-249`); with a communication policy configured, a send with no recipient fails (`:250-252`). Capacity is checked before anything changes (`:265-269`). The send path has no `await`.
- One store counter numbers messages and events (`runtime.py:175-177`, `:259`). All channel state is in store models (`ORBIT: orbit/communication/state.py:120-155`).
- `visibility="step_start"` freezes reads at the counter's value when the step started, through a ContextVar (`runtime.py:36-48`, `:292`).
- Attacks register channels at run time: the collusion `secret:<name>` channel is registered on its tool's first use (`ORBIT: orbit/attacks/collusion/collusion_channel.py:63-67`), and its tool is pre-wired because scheduled agent closures cannot gain tools later (`orchestrator.py:245-272`).

**Delivery.** `delivery` is `poll`, `notify` (the default) or `auto`, per agent overridable (`communication.py:54-92`). For `notify` and `auto` the outermost `Model` wrapper derives one `ChatMessageSystem` per request and appends it last, never storing it (`ORBIT: orbit/communication/delivery.py:203-224`, `:234-282`, `:330-343`). A notice lists channel ids and unread counts and "contains no message text" (`:250-264`); an `auto` inbox carries message bodies under an "untrusted messages" header (`:231`, `:266-282`). Dialogue projections insert a peer's completed text as a user turn at the start of an activation (`ORBIT: orbit/communication/dialogue.py:151-200`).

**Scheduling.** `ExecutionConfig` (`execution.py:109-207`) selects round-robin, superstep or interleaved modes, or an explicit plan (`ORBIT: orbit/configs/scheduling.py:16-116`, `ORBIT: orbit/execution/plans.py:76-267`).

- A plan step lists agents and runs them in order, or concurrently with a barrier (`agent_scheduler.py:298-400`, the asyncio batch at `:352-377`); one plan step is one outer turn (`:282-283`).
- The quanta (`scheduling.py:13`): `legacy`; `model_step`, one generation and its tool batch; `tool_step`, one tool call, refusing a batch of several before any runs (`turn_react.py:462-467`); `completion`, until submission for a submitting agent and until a tool-free reply for a `submit=False` one. The public task helper defaults to scheduled `model_step` (`ORBIT: orbit/tasks/multi_agent.py:100-102`).
- `max_messages` is an eligibility check: an agent at its limit is skipped before an activation starts (`agent_scheduler.py:290-296`, `:539-551`); one that starts runs its whole quantum.
- A trusted `after_activation` callback may amend pending steps, with an audited revision (`plans.py:222-267`). Models are given no schedule editor (`plans.py:1-6`).
- Messages never activate an agent (`ORBIT: docs/runtime-architecture.md:80`).

**The outer loop.** `ExperimentScheduler.run_loop` repeats turns until a halt condition (`ORBIT: orbit/scheduler/scheduler.py:164-244`, `ORBIT: orbit/scheduler/halt_conditions.py:17-100`): the executor complete (all submitted, plan exhausted or failed), `max_turns`, `max_time_seconds`, attack success, or a custom condition. Convergence halting is off for the scheduled executor (`orchestrator.py:888-892`). Each turn (`orchestrator.py:907-1026`):

1. activates runtime attacks and routes their new messages to their targets (`:919-935`, `:657-677`);
2. applies pending injections (`:937-952`);
3. runs pre-turn defence hooks over every agent's `AgentState` (`:954-964`), then captures each compromise prompt as the defences left it (`:966-973`);
4. runs the executor (`:976`);
5. evaluates attack outcomes over the turn's new messages (`:996-1008`).

**Attacks that change a conversation.** The examples this design uses:

- **Pending injections.** `indirect_injection` with a `target_tool` queues `{target_tool, target_agent, payload, turn_injected}` in `AttackLog.pending_injections` (`ORBIT: orbit/attacks/injection/indirect.py:35-45`). Step 2 turns each unapplied entry into `ChatMessageUser("[Output from <tool>]: <payload>", metadata={"untrusted_observation": True})`, appended to the target's conversation, or to every unsubmitted agent's when there is no target, and marks it applied (`orchestrator.py:937-952`).
- **`inject_prompt` compromise.** `CompromisedAgentAttack` adds a `ChatMessageSystem` with the payload, tagged `untrusted_observation` and with its provenance (`ORBIT: orbit/attacks/compromised/compromised.py:106-120`), before deployment or at a `specific_turn` (`:151-162`). The orchestrator inserts a copy into the target's conversation after its leading system messages, once (`orchestrator.py:619-654`). A `CompromisedPromptFilter` re-adds the captured prompt at the model boundary where a conversation lacks it, as a fresh delegated child's does (`compromised.py:34-71`, installed at `orchestrator.py:172-176`). `replace_agent` is refused by both native executors (`orchestrator.py:324-331`).

**Defences.** Eleven are registered (`ORBIT: orbit/defenses/registry.py:31-43`).

- **Tool gates.** `DefenseBoundary.wrap_tool` runs each defence's `on_tool_call` in an `orbit.defense` span before the tool, records a `defense_intervention`, and turns a denial into `ToolError("Blocked by defense '<name>': <reason>")` (`ORBIT: orbit/defenses/boundaries.py:191-293`, the raise at `:238-239`). It wraps every tool, channel tools included, outermost (`orchestrator.py:182-211`). The built-in `tool_wrapper` defence denies configured tools and argument patterns such as `rm -rf` (`ORBIT: orbit/defenses/tool_wrapper.py:26-37`, `:81-111`).
- **Input filters** run in a `ModelAPI` wrapper in priority order (`model_filter.py:127-211`): the compromise prompt filter (-100), memory injection (1, appending a `[Memory Context]` system message), safety and vaccination prompts (10, prepending a system message, `model_filter.py:328-372`), and boundary input monitoring last, which replaces a blocked message in the provider-bound copy with "[Content withheld by a configured defense.]" (`boundaries.py:141-186`).
- **Output checks.** `_BoundaryModel` checks every choice, resamples up to `max_resamples` (each a new generation), and otherwise returns a refusal copy with every tool call removed, leaving the raw `ModelEvent` output as it was (`boundaries.py:296-343`).
- **`dual_llm`** has three paths (`ORBIT: orbit/defenses/dual_llm.py`): a tool wrapper that returns a tool-less quarantined model's summary in place of each tool result (`:102-106`, `:189-199`); a pre-deployment rewrite of tagged setup messages (`:98-100`, `:282-293`); and the pre-turn hook, which, on the scheduled path, rewrites in place every message of each target agent's conversation tagged `untrusted_observation` and not yet `dual_llm_rewritten`, in any role (`:295-338`, `:355-386`). Its quarantined calls are ordinary `ModelEvent`s outside any agent.

**The message order before the first model call.** For a scheduled `model_step` agent with everything present, the provider receives (`ORBIT: orbit/solvers/orchestrator.py:779-1036`, `agent_scheduler.py:520-588`, `turn_react.py:198`, `:469-490`, `delivery.py:330-343`, `model_filter.py:161-211`):

| # | Role | Message | Stored |
|---|---|---|---|
| 1 | system | safety and vaccination prompts, prepended by input filters | no, per request, below the `ModelEvent` |
| 2 | system | the agent's prompt, inserted at index 0 on the first activation | yes |
| 3 | system | collusion prompts, then the `inject_prompt` compromise, inserted after the leading system messages (`orchestrator.py:602-614`, `:650-653`) | yes |
| 4 | user | the seed: the group goal, or a copy of the sample input (`agent_scheduler.py:222-254`) | yes |
| 5 | any | untagged runtime-attack messages, appended (`ORBIT: orbit/execution/scheduled_executor.py:141-151`) | yes |
| 6 | user | pending injections, `[Output from <tool>]: ...`, appended, possibly rewritten by `dual_llm` | yes |
| 7 | user | legacy observations and dialogue projections | yes |
| 8 | system | the `notify` notice or `auto` inbox, appended last by the outermost wrapper | no, but in the `ModelEvent` |
| 9 | system | the memory context, appended by its input filter | no, per request, below the `ModelEvent` |

Boundary input monitoring then redacts messages in the provider-bound copy. "Please continue with your next action." is never on a first call.

**Logging.** ORBIT writes `InfoEvent`s (`transcript().info()`):

- communication evidence with source `orbit.communication`: `sent`, `read`, `notified`, `delivered` and `model_exposure`, each with its message ids, agent, session and invocation, and Inspect span, tool-call, event and model-event ids (`runtime.py:190-227`; kinds at `state.py:57-83`);
- `orbit_agent_started` per agent invocation (`ORBIT: orbit/execution/communication.py:38-56`);
- `orbit_schedule` events: step started and finished, activation started and finished with status and reason, and plan amendments (`plans.py:121-132`, `:157-267`);
- `defense_intervention` (each decision, with the redaction text for a blocked input), `defense_resample`, `defense_resolved`, `defense_activity` and `defense_instrumentation_exempt` (`boundaries.py:63-92`, `:204-208`, `:330-334`; `ORBIT: orbit/defenses/evidence.py:110-166`);
- `orbit.attack_injection` and `orbit.attack_outcome` (`orchestrator.py:414-418`, `:485-489`), and runtime errors (`ORBIT: orbit/execution/errors.py:189-215`).

What reaches the model is only partly logged. The `ModelEvent` records the request before the input filters, with the notice already appended (`ORBIT: docs/runtime-errors-and-interventions.md:9-11`, `:167-169`); the filtered request is in no ORBIT event and only in Inspect's raw provider record when that is kept, by default for the first few calls per model plus errors (`src/inspect_ai/_eval/eval.py:319`). A refusal the output check substitutes is in the agent's state and its `defense_intervention`, not in any `ModelEvent`.

**Exposure.** ORBIT's observer, a `ModelAPI` wrapper below the filters, sees the filtered request (`model_filter.py:186`, `ORBIT: orbit/communication/model_observer.py:24-95`). After a successful generation it matches each read's tool result by tool call id, function, agent and session, captured at read time (`ORBIT: orbit/execution/provenance.py:17-52`), and each rendered record as a newline-delimited block of the result, and matches notices by message id and exact text (`runtime.py:330-399`, `delivery.py:360-406`). The collusion scorer reads the store's `CommunicationState` and reports incomplete observation as unknown, not zero (`ORBIT: orbit/scorers/collusion_scorer.py:78-141`).

**The answer.** At the end, `sync_to_task_state` takes `output_agent`, else the last agent to submit, else the agent of the last step with results; projects that agent's conversation (`selected`) or every agent's (`all`) into `TaskState.messages`; and sets `TaskState.output` to that agent's output if its completion is not empty, without asserting that it submitted (`agent_scheduler.py:813-869`). It runs in the solver's `finally`, after failures and cancellation alike (`orchestrator.py:1038-1099`). Nothing records which agent's output was scored or whether it submitted: the submit registry is in memory only, and the halt reason is a transcript string (`ORBIT: orbit/execution/submit_registry.py:23-112`, `orchestrator.py:731-735`).

### inspect_swarm

- **A member's first conversation**, verified by running two `react(prompt=...)` members on mockllm under a controller that started each with `start(member, input=[system, user])`: the first `ModelEvent` input was react's system message, the start input in order, then the swarm's preamble as a user message. react inserts its system message at index 0 (`src/inspect_ai/agent/_react.py:213-216`), and `start()` builds the input followed by the preamble (`src/inspect_swarm/_controller/_control.py:335-343`). The preamble names the member, the roster size and its role, says the members work "at the same time", and adds each channel's instructions (`src/inspect_swarm/_swarm.py:136-175`).
- **The bus is private.** A channel's `open()` gets `ChannelRun.bus`, typed as the private `Bus` (`src/inspect_swarm/_channel/_channel.py:84-92`). Its tools call `Bus.deliver(Draft(...))` (`src/inspect_swarm/_bus/_bus.py:138-195`), where `Draft` and `Delivery` are private (`_bus/_record.py:67-78`, `_bus/_bus.py:28-35`). The calling member is `Bus.member()`, returning the private `MemberContext` (`_bus.py:211-216`), and a read's evidence is written by the private `comm_read()` with `Bus.read_id()` (`src/inspect_swarm/_evidence.py:102-127`, `_bus.py:206-209`). `shared_memory` uses all of these from inside the package (`src/inspect_swarm/_channel/shared_memory.py:11-14`, `:517-526`, `:610-620`, `:913-927`). The docs say a channel with records goes through the bus, and document no way to do it (`docs/channels.qmd:77`).
- **Addresses and order.** Step 0 of `deliver()` refuses an address to a member that is pending or has ended (`_bus.py:234-251`); an unaddressed record resolves to nobody. `validate()`, `admit()` and `apply()` run with no `await` between them, so `apply()` sees records in commit order; what `apply()` returns (`notify`, `result`) goes into the `delivered` evidence (`_bus.py:166-195`).
- **Tools.** `swarm_tools()` gives each loop the tools of every channel state, resolved in the member's context (`src/inspect_swarm/_bus/_tools.py:11-34`).
- **The result record** has each member's state, status, submission and time, not its output (`src/inspect_swarm/_record.py:12-46`); the runtime holds ended members' outputs in memory (`_control.py:644-647`). [swarm-scoring.md What the record adds](swarm-scoring.md#what-the-record-adds) plans `MemberResult.output` after M2.
- **The answer** is a submission only: `first`, `last` or the controller's `choose_answer()`, which refuses a member that did not submit (`src/inspect_swarm/_final.py:29-67`, `_control.py:373-387`). The returned messages are the input and the answer's message, marked with the mode and member (`_final.py:124-137`).
- **Controllers** see names, roles, states and statuses, never text (`_control.py:56-87`).

### The turn seam

[swarm-turn-seam.md](swarm-turn-seam.md) is designed and not yet built. It gives each member's handle `on_boundary(callback, origin=)`, `end(origin=)` and `held`. A callback receives a `Boundary` with a detached copy of the conversation and output at every boundary of the member's own loop, boundary 0 before the first generation included; it may wait, then continue, return a new conversation, or end the member. While callbacks run the member is held: its time limit pauses, `swarm_wait` does not count it as active, and a stop ends it at once, `stopped`. Callbacks run in registration order, each in the member's task, and their model calls count against the member's limits. Every change is a `changed` event with the callback's origin and the ids, positions and hashes of the messages added, removed and replaced. A rewrite that is not a pure append resets react's compaction state, and drops summaries derived from changed history. Background-dispatching `deepagent()` members and bridged members are refused, and a member with callbacks that never reaches a boundary ends `errored`. The seam ships no `activate()`, snapshot or intervention API: those are the caller's.

### inspect_ai

- **Sentinel's generate stages only observe or terminate.** `BeforeGenerate` runs once per agent model call, after the input is prepared and before the connection slot and the cache lookup; `AfterGenerate` runs after the output is complete, on cache hits too (`src/inspect_ai/model/_model.py:1050-1063`, `:1109-1113`; inspect_sentinel `design/generate-stages.md`, decisions 4 and 5). Only `terminate` takes effect (`src/inspect_ai/_sentinel/_dispatch.py:275-280`). `modify` at either stage, a `reject` that replays, and a `resample` protocol are proposed and deferred to a second part (`design/generate-stages.md`, decisions 8 to 13 and sections c and d).
- **What a `ModelEvent` records.** Its `input` is the prepared input that `Model.generate` passes down (`_model.py:2071-2085`). A `ModelAPI` wrapper's rewrite happens below it (`:1664`), so the event shows the request before ORBIT's filters; the cache lookup is keyed on that same pre-filter input (`:1542-1558`).
- **Input preparation** collapses consecutive messages of one role for providers that need it (`_model.py:1437`, `:2823-2838`).

## Design

### Where each ORBIT component goes

| ORBIT component | On the port | Supported by |
|---|---|---|
| `AgentSpec`, the member builder | ORBIT's registered `@agent`, which builds `react()` from the spec's fields, once per member per sample | `member(orbit_member, args=..., name=, role=)` ([swarm-api.md](swarm-api.md#members)) |
| `turn_react` | Removed for ported runs | `react()` with the turn seam |
| Group goals, sample input | ORBIT's controller | `start(member, input=[...])` |
| Scheduler: modes, plans, quanta, barriers, `after_activation`, `max_messages`, halt conditions | ORBIT's `@swarm_controller` ([below](#scheduling-orbits-controller-over-the-seam)) | `on_boundary()`, `start()`, `events()`, stop reasons ([S1](#s1-the-turn-seam)) |
| Snapshots for eligibility and attack outcomes | ORBIT's controller | `Boundary.messages` and `Boundary.output` |
| Pending injections, `inject_prompt` compromise, runtime attack messages | ORBIT's `orbit:attack` callback | The seam; each change a `changed` event ([S1](#s1-the-turn-seam)) |
| Pre-turn defences (`dual_llm`) | ORBIT's `orbit:defense` callback | The seam |
| Channels, communication edges, capacity, cursors, secret channels, `step_start` visibility | ORBIT's `@swarm_channel` | The public bus API ([S2](#s2-a-public-bus-api-for-channels)) |
| `notify` and `auto` delivery | ORBIT's delivery `Model` wrapper, unchanged | None needed |
| Exposure observer | ORBIT's observer, unchanged, writing ORBIT's own events | Read evidence from `record_read()` ([S2](#s2-a-public-bus-api-for-channels)); later an `AfterGenerate` stage ([I2](#i2-sentinel-generate-stages-that-rewrite-and-resample)) |
| Tool gates, `dual_llm`'s tool wrapper | ORBIT's tool wrappers, applied by the member builder, unchanged | None needed |
| Input filters, output checks and resampling | ORBIT's filter wrappers, unchanged, until I2 and I3 exist; then sentinel protocols | [I2](#i2-sentinel-generate-stages-that-rewrite-and-resample), [I3](#i3-a-log-of-what-a-generate-stage-changed) |
| Answer selection (`output_agent`) and history projection | ORBIT's solver wrapper around the swarm | Each member's output on the record ([S3](#s3-each-members-output-on-the-result-record)) |
| A record of the answer scored | ORBIT's solver wrapper | [S3](#s3-each-members-output-on-the-result-record) |
| `CommunicationState`, the collusion scorer, the viewer overview | ORBIT, unchanged: its channel keeps writing its own state and events | None needed |
| Scenarios, datasets, attack and defence registries, scorers, judges, YAML and CLI | ORBIT, unchanged | None needed |
| `legacy` and `tool_step` quanta, legacy observation modes, `legacy/v1` | ORBIT's executor | None ([What stays on ORBIT's executor](#what-stays-on-orbits-executor)) |

So a ported run is ORBIT code throughout, on three swarm additions and three inspect_ai prerequisites, one of which the seam already needs.

### What inspect_swarm adds

#### S1. The turn seam

The seam is [swarm-turn-seam.md](swarm-turn-seam.md), used as designed; nothing is added to it for ORBIT. It is the one piece ORBIT cannot build on top: a hook ORBIT wires into its own `react()` (`on_continue`) misses boundary 0 and every recovered turn, and leaves the swarm reading a held member's status, time limit, waits and stop wrongly ([swarm-turn-seam.md Why](swarm-turn-seam.md#why)).

The seam is also the standard record of changes at a boundary that ORBIT needs: each of ORBIT's callbacks is registered with its own origin, so a boundary where attack input was added and a defence then rewrote it has one `changed` event per origin, in order, with the ids and hashes of what each added, removed or replaced. ORBIT keeps writing its own content records (`defense_activity`, `orbit.attack_injection`) beside them; the seam records no text ([swarm-turn-seam.md Evidence](swarm-turn-seam.md#evidence)).

Whether code at a boundary may read member text or add user- or system-role input is ORBIT's decision as the caller, not the swarm's ([swarm-turn-seam.md Non-goals](swarm-turn-seam.md#goals-and-non-goals)).

#### S2. A public bus API for channels

A channel with records needs the bus, and every name it needs is private. ORBIT could import them from `inspect_swarm._bus`, as `shared_memory` does from inside the package, but private names change without notice, and the sender binding and the evidence must be the bus's, not a copy. So the bus becomes a public, experimental surface, as the channel interface already is:

```python
# inspect_swarm (public, experimental)

@dataclass(frozen=True)
class Draft: ...            # moved from _bus/_record.py, unchanged: channel, kind, to, payload, op, wake

@dataclass(frozen=True)
class Delivery: ...         # moved from _bus/_bus.py, unchanged: record, seq, notify, result


@dataclass(frozen=True)
class Caller:
    """The member a channel's tool acts for."""

    member: str             # the member's name
    nested: bool            # whether the calling loop is not the member's own
    loop_span_id: str | None  # the agent span of the calling loop


def calling_member() -> Caller | None:
    """The member of the current swarm run that the caller runs in or descends from; None outside one."""


class Bus(Protocol):
    """A swarm run's bus, as `ChannelRun.bus` gives it to a channel."""

    @property
    def stopping(self) -> bool: ...                       # once true, every deliver() is refused
    def running(self, member: str) -> bool: ...           # started and not ended
    async def deliver(self, draft: Draft) -> Delivery: ...
    def record_read(
        self,
        channel: str,
        *,
        record_ids: Sequence[str],
        data: Mapping[str, JsonValue] | None = None,
    ) -> str: ...                                         # writes the `read` event; returns its read id
```

- **`deliver()`** is today's, unchanged: it binds the sender from the calling member, resolves addresses, runs the channel's `validate()`, the storm controls, `admit()`, the `sent` evidence, `apply()` and the `delivered` evidence, and returns the `Delivery`. Called outside a member of the run, it raises `RuntimeError` as today.
- **`record_read()`** writes a `read` communication event for the calling member, in the current tool span, with the loop and `nested` taken from the caller, the given record ids, and `data` for the channel's own fields; it returns the read id (`r-5`). Outside a member of this run it raises `RuntimeError`, and for a channel name the run has not opened, `ValueError`. Under [swarm-event-types.md](swarm-event-types.md#communicationevent) the ids go in `record_ids` and `data` in `data`; shared memory passes its `files` and `listing` there.
- **`calling_member()`** is the public form of `Bus.member()`, for channel tools and for any code in a member's context that needs to know which member it acts for. It returns a frozen `Caller`, never the runtime's mutable context.
- **`ChannelRun.bus`** is typed as the `Bus` protocol. `shared_memory` moves onto the public names, with no change in behaviour.

Two things ORBIT's channel needs are deliberately not added, because the channel interface already gives them:

- **Readers that have not started or have ended.** ORBIT's channels are blackboards: membership, not activity, decides who reads, and under a schedule the first post goes to readers still parked at boundary 0, or pending. An addressed record would be refused at step 0, so ORBIT's channel posts **unaddressed** records (`to=()`), computes its recipients itself from readers and edges, as it does now, and returns them in `apply()`'s `result`, which the `delivered` evidence carries. `notify` names the recipients whose loop the record is news to.
- **A commit sequence for `step_start` visibility.** `apply()` runs in commit order with no `await` around it, so a counter the channel increments in `apply()` is a commit sequence; ORBIT's own store counter, incremented there, serves. Record ids, given before the storm controls, are not in commit order and are not used for it.

#### S3. Each member's output on the result record

`MemberResult` gains `output: ModelOutput | None`, the member's output once it has ended, `None` before: the field [swarm-scoring.md](swarm-scoring.md#what-the-record-adds) plans after M2, brought forward alone, without the answer keys, verdicts or chain fields. The runtime writes it in `_end()` from the output it already holds for the answer (`_control.py:603-647`), so it is there after a member's own limit, an end at a boundary, a stop and a hard stop, with the output the seam took from the loop's last state. The format version stays 1: the field is optional and a record without it reads as before.

ORBIT could save outputs itself, from its member wrapper. It needs the record's instead because its answer and the swarm's must come from the same object: the record's `final.member` and ORBIT's choice are then compared like with like, the output is the one the runtime kept however the member ended, and a re-scored log, which rebuilds only the store, still has it. The record is readable by member code while the swarm runs, so a tool written to read it gives members each other's outputs; the user docs already say the record belongs to the swarm ([swarm-scoring.md What the swarm records](swarm-scoring.md#what-the-swarm-records)), and say so for outputs too.

### inspect_ai prerequisites

Each is specified here by what it must do and why ORBIT needs it, and designed in inspect_ai or inspect_sentinel.

#### I1. The turn gate and the compaction reset

The private turn gate in the agent channel and `reset()` on react's compaction handler, as [swarm-turn-seam.md](swarm-turn-seam.md#the-inspect_ai-change-a-turn-gate-and-a-compaction-reset) designs them. S1 needs them; ORBIT needs nothing more of them.

#### I2. Sentinel generate stages that rewrite and resample

What: `modify` at `BeforeGenerate`, replacing the request for that call only; `modify` at `AfterGenerate`, replacing the output's message; and a `resample` protocol that draws again and substitutes the best candidate, as inspect_sentinel's generate-stages design proposes for its second part (decisions 9, 11 and 12, section d). ORBIT needs, of that proposal:

- the replaced request is what is sent, what the `ModelEvent` records, and what the cache is keyed on, so a filtered member can use the response cache;
- the replacement is not written back to the agent's conversation, as ORBIT's filters are per request today;
- `AfterGenerate` sees the request as sent, after any `BeforeGenerate` modify, and runs on cache hits, so an observer at that stage sees exactly what reached the model;
- a protocol can tell which member a call belongs to, which `calling_member()` (S2) answers.

Why: ORBIT's input filters (memory context, safety prompts, input monitoring with redaction, the compromise prompt filter) and its output checks with resampling are per-call rewrites. Today they need a `ModelAPI` and a `Model` wrapper built through the private `_resolve_config`, and refuse caching for the whole run. With I2 they become sentinel protocols, and ORBIT's observer an `AfterGenerate` stage that sees cache hits too.

#### I3. A log of what a generate stage changed

What: when a generate stage modifies, the log shows the request before and after the change and the provider's response beside what the agent received:

- a `BeforeGenerate` modify records, in its `SentinelEvent`, the change from the step's input to the request sent, by message id: the ids, positions and hashes of messages added, removed and replaced, as the seam's `changed` event records them, plus the original content of each removed or replaced message. The proposal records no copy, since the `ModelEvent` holds the request sent (decision 13); but the `ModelEvent` then no longer holds the request before the filters, and nothing else in the log does, since the member's conversation is not logged and its later requests are filtered too;
- an `AfterGenerate` modify records the replacement message in `SentinelEvent.modified`, joined to the provider's output in the `ModelEvent` by the generate's step id, as decision 13 proposes; each resampled draw is its own `ModelEvent` in the stage's span, and the event says which draw the agent received.

Why: ORBIT's results depend on what its filters removed and what its output checks replaced, and today that is reconstructed from ORBIT's own `defense_intervention` and `defense_activity` events, while the `ModelEvent` shows a request the model never received. With I3 the Inspect log itself answers "what did the model see, and what did the agent get".

Until I2 and I3 exist, ORBIT keeps its wrappers and its events, and logs what they change itself.

### ORBIT's code on the swarm

#### Members

ORBIT's member builder is a registered `@agent` taking the spec's fields as arguments, so the plan logs them. It builds:

```python
react(
    name=spec.name,
    prompt=AgentPrompt(instructions=spec.system_prompt, assistant_prompt=ORBIT_ASSISTANT_PROMPT),
    tools=[*wrapped_tools, swarm_tools()],      # tool gates and dual_llm's quarantine applied as today
    model=wrapped_model,                        # ORBIT's delivery, output-check, filter and observer layers
    submit=submit_tool if spec.submit else False,
    on_continue=keep_going if not spec.submit else None,
)
```

- `ORBIT_ASSISTANT_PROMPT` is `turn_react`'s assistant prompt, so the system message stays as it is.
- A `submit=False` member needs `on_continue` to return its own state: `react(submit=False)` otherwise ends at its first tool-free reply, with no boundary after it ([swarm-turn-seam.md Supported members](swarm-turn-seam.md#supported-members)). With it, a tool-free reply reaches a boundary with `after == "reply"`, where ORBIT's controller ends the activation and the member stays available.
- The builder wraps the agent so that a synchronous `finally` saves the final conversation under an ORBIT store key for history projection, and enters ORBIT's communication identity for its observer, as `observe_agent` does today.

#### Scheduling: ORBIT's controller over the seam

ORBIT's scheduler is a `@swarm_controller` of its own. It registers three callbacks on every member, in this order, then starts every member; each parks at boundary 0 until its first activation:

```python
# ORBIT code, not inspect_swarm

class Slot:                                   # one per member
    def __init__(self) -> None:
        self.boundary: Boundary | None = None # the latest snapshot
        self.parked = anyio.Event()
        self.go = anyio.Event()
        self.ended = anyio.Event()            # set from MemberEnded
        self.quantum: Literal["model_step", "completion"] = "model_step"
        self.active = False
        self.starting = False                 # this boundary begins an activation

    async def at_boundary(self, b: Boundary) -> None:      # origin "orbit:schedule"
        self.starting = False
        if self.active and self.quantum == "completion" and b.after != "reply":
            return None                       # completion runs on to a tool-free reply
        self.active = False
        self.boundary = b
        self.parked.set()
        with suspend_working_limit():         # held time is waiting time
            await self.go.wait()
        self.go = anyio.Event()
        self.active = self.starting = True
        return None

    async def activate(self, quantum: Literal["model_step", "completion"]) -> None:
        await first_of(self.parked, self.ended)
        if self.ended.is_set():
            return
        self.parked = anyio.Event()
        self.quantum = quantum
        self.go.set()
        await first_of(self.parked, self.ended)   # its next boundary, or its end


@swarm_controller
def orbit_scheduler(experiment: dict[str, JsonValue]) -> Controller:
    async def run(control: SwarmControl) -> str:
        config = ExperimentConfig.model_validate(experiment)
        slots = {m.name: Slot() for m in control.members}
        attacks, defenses = OrbitAttacks(config, slots), OrbitDefenses(config, slots)
        for m in control.members:
            m.on_boundary(slots[m.name].at_boundary, origin="orbit:schedule")
            m.on_boundary(attacks.callback(m.name), origin="orbit:attack")
            m.on_boundary(defenses.callback(m.name), origin="orbit:defense")
        async with anyio.create_task_group() as tg:
            tg.start_soon(mark_ended, control, slots)     # MemberEnded -> slot.ended
            for m in control.members:
                control.start(m, input=seed_for(config, m.name))
            reason = await run_turns(config, slots, attacks)
            tg.cancel_scope.cancel()
        return reason                                     # recorded as controller:<reason>

    return Controller(run)
```

- **Turns.** `run_turns` is ORBIT's outer loop and `ScheduleRuntime`, unchanged in substance: for each turn it checks the halt conditions, begins the turn's attacks, and runs the plan step. A sequential step awaits `slots[name].activate(quantum)` in order; a concurrent step starts them in an anyio task group, whose exit is the barrier, under an anyio capacity limiter for `max_concurrency`. `after_activation` amendments run between activations, as today. ORBIT's `orbit_schedule` events are written by the controller, as `ScheduleRuntime` writes them now.
- **Quanta.** `model_step` is one boundary. `completion` keeps a submitting member going until it submits and ends, and a `submit=False` member until a boundary with `after == "reply"`. A recovered context overflow reaches a boundary like any turn, so under `model_step` the activation ends there, as `turn_react`'s does (`turn_react.py:254-262`).
- **Eligibility.** Before activating a member the controller reads `slots[name].boundary.messages`, so `max_messages` is checked exactly as now. Pending injections for that member that have not been applied yet are counted with them, since today they are appended before the check.
- **Attack outcomes and halts.** The controller reads the members' snapshots after each step for `_evaluate_attack_outcomes`, and returns ORBIT's halt reason: `controller:max_turns`, `controller:schedule_exhausted`, `controller:all_agents_submitted`, `controller:attack_success` and so on.
- **Ending.** Returning requests the swarm's stop, which ends every held member at once, `stopped` ([swarm-turn-seam.md Stopping](swarm-turn-seam.md#stopping)). A submitting member that submits ends `submitted` on its own, and is never activated again.
- **Concurrency.** Everything is anyio. A member's error propagates as the swarm's foreign error and cancels the siblings and the controller, as ORBIT's "concurrent failure cancels and joins siblings, then propagates the original failure".
- **Read cutoffs.** Before activating a member under `step_start` visibility, the controller writes the channel's counter at the start of the step to an ORBIT store key, per member; the channel's read tool reads it for `calling_member()`. Controller and channel share the sample's store, as ORBIT's state already does.
- **Messages never activate a member.** Only the controller sets `go`.

#### Interventions at a boundary

The `orbit:attack` and `orbit:defense` callbacks act only at a boundary that begins an activation (`slots[name].starting`), and return `None` at every other one, including those inside a `completion` activation. Each receives the conversation as the callbacks before it left it, and returns a new list; the seam applies it, and records one `changed` event per callback.

- **Pending injections** (`orbit:attack`). The callback takes this member's unapplied `AttackLog.pending_injections` entries, and those with no target, and appends the same message ORBIT builds today, then marks them applied and writes ORBIT's info line:

  ```python
  injected = ChatMessageUser(
      content=f"[Output from {source}]: {entry['payload']}",
      metadata={"untrusted_observation": True},
  )
  return [*b.messages, injected]
  ```

  The seam records an append: one `added` id at the end. A broadcast entry is appended to each unsubmitted member when that member is next activated, rather than to all at the turn's start; it lands at the same place in each conversation, since a parked member's conversation does not change in between.
- **`inject_prompt` compromise** (`orbit:attack`). At the activation where the attack is due (before deployment, that is the first; at `specific_turn`, the first activation of that turn), the callback inserts ORBIT's tagged `ChatMessageSystem` after the leading system messages, unless one with the same provenance is there, exactly as `_inject_compromised_instructions` does. The seam records an insertion, so it is a rewrite: react's compaction state is reset, which costs nothing at boundary 0. ORBIT's `CompromisedPromptFilter` stays on the member's model for delegated conversations.
- **`dual_llm`'s pre-turn rewrite** (`orbit:defense`). The callback runs `_rewrite_messages_in_list()` over `b.messages`, the detached copy, and returns it. The rewritten messages keep their ids, so the seam records each as `replaced`, with its hashes before and after. It then runs `capture_injected_prompt` on the result, as ORBIT does after its pre-turn hooks. The quarantined model's calls run in the member's task, so they count against the member's token and cost limits and its ledger line ([Behaviour a port changes](#behaviour-a-port-changes)).

Because attack input comes before defences in registration order, a defence sees and can rewrite the injection before the member's next generation, as in ORBIT today.

#### The channel

ORBIT's `channel_post`, `channel_read` and `channel_list` become the tools of one ORBIT `@swarm_channel`, say `orbit`, with `reach="loop"`, whose state keeps ORBIT's store-backed registry, messages, receipts and cursors, attack-registered secret channels included.

- **Post.** The tool finds the caller with `calling_member()`, checks that it is a writer, and computes the recipients from readers and edges; it refuses a non-writer or an empty recipient set with a `ToolError`, as now. It then delivers `Draft("orbit", "post", payload=text, op={"channel": id, "recipients": [...]})`, unaddressed. `validate()` checks the post alone; `admit()` checks capacity, against state that `apply()` changes with no `await` in between, so two concurrent posts cannot both take the last place; `apply()` increments ORBIT's counter, stores the message with it and the bus's record id, and returns the recipients as `notify` and in its `result`.
- **Read.** The tool checks read permission, pages from the caller's cursor, hides messages above the caller's read cutoff, renders ORBIT's NDJSON, and calls `record_read("orbit", record_ids=...)` before it returns, keeping the rendered fragments under the read id and the tool call id ORBIT's provenance captures, as `record_read` does today (`ORBIT: orbit/communication/runtime.py:300-327`).
- **Evidence.** The bus writes the swarm's `sent`, `delivered` and `read` events for ORBIT's records, which the swarm's ledger and communication metrics count; ORBIT keeps writing its own `orbit.communication` events and `CommunicationState`, each carrying the swarm's record id, so the two join. The collusion scorer and the viewer overview read ORBIT's state as now.
- **Waking.** `wake=False`: no ORBIT record wakes a wait or activates a member.

#### Delivery, exposure and filters

ORBIT's model layers stay as the member builder builds them, in today's order: delivery outermost, the output check, the filter wrapper (which records the `ModelEvent`), the observer below the filters, then the provider. Nothing in the swarm changes them:

- `notify` and `auto` are computed per request from ORBIT's channel state, for the member the builder was built for.
- The observer matches reads as today, by the tool call id captured at read time; the swarm puts no metadata on tool results and ORBIT needs none.
- Exposure and coverage stay ORBIT's `model_exposure`, `notified` and `delivered` events and `observation_enabled_sessions`.

When I2 and I3 exist, the input filters and output checks become sentinel protocols keyed by `calling_member()`, the observer an `AfterGenerate` stage, and the filter, output-check and observer layers, with `_resolve_config`, go. The delivery wrapper is the last; it can become a `BeforeGenerate` modify that appends the notice, also per request.

#### The answer, and a record of the answer scored

ORBIT's task wraps the swarm in a solver of its own, which awaits `as_solver(swarm(...))` inside `try`. Its `finally` is synchronous, so it runs under cancellation, and runs after `as_solver()`'s own `finally` has copied the swarm's answer into the `TaskState` (`src/inspect_ai/agent/_as_solver.py:65-80`). It:

1. reads the `inspect_swarm:result` record and ORBIT's own schedule state;
2. chooses the answer member by ORBIT's rule: `output_agent`, else the member that submitted last (by the record's `time`), else the member of the last step with results;
3. sets `TaskState.output` to that member's `output` from the record (S3), if its completion is not empty;
4. sets `TaskState.messages` from the conversations the member wrappers saved, by `history_projection`;
5. writes ORBIT's answer record, below;
6. lets the original exception, if any, propagate unchanged; a failure in steps 1 to 5 is recorded as ORBIT records cleanup errors, never replacing it.

**The answer record.** ORBIT writes it to the store under `orbit:answer`, so a re-scored log keeps it, and as an ORBIT `InfoEvent` (`{"orbit_answer": ...}`) in the solver's span:

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `member` | the member whose output is `TaskState.output`; `None` when none was set |
| `rule` | `output_agent`, `last_submitted` or `last_result` |
| `status`, `submitted` | that member's status and submission, from the swarm's record |
| `message_id` | the id of the scored output's message |
| `swarm_final` | the swarm's own `final`: its mode and member |
| `agrees` | whether ORBIT's member is the swarm's answer |
| `provisional` | whether the swarm's record was still provisional (an outer limit or cancellation ended it) |
| `halt_reason` | ORBIT's halt reason, the controller's stop reason without its `controller:` prefix |

So the answer actually scored is named in one place, with how it was chosen and whether it was a submission. When the output member submitted, ORBIT's controller also calls `choose_answer(member, reason="output_agent")` once it has ended, so the swarm's own `final` names the same member and `agrees` is true; when it did not, the swarm's `final` is `no_submissions` and only ORBIT's record names the answer. The swarm's rule and record are unchanged.

### Logging

| What | Written by | As |
|---|---|---|
| Communication on ORBIT's channel: sends, deliveries, reads | the swarm's bus, through S2 | `sent`, `delivered`, `read` communication events with the swarm's record and read ids ([swarm-event-types.md](swarm-event-types.md)) |
| ORBIT's communication evidence, exposure and coverage | ORBIT's channel and observer, as today | `orbit.communication` `InfoEvent`s and `CommunicationState` |
| Holds and changes at boundaries | the seam | `boundary` and `changed` swarm events, one `changed` per ORBIT origin |
| Attack and defence content and decisions | ORBIT, as today | `orbit.attack_injection`, `orbit.attack_outcome`, `defense_intervention`, `defense_resample`, `defense_activity`, `defense_resolved` |
| The schedule | ORBIT's controller | `orbit_schedule` events, as `ScheduleRuntime` writes them; the swarm's `boundary` events give each hold's duration |
| Member results, the swarm's answer, ledger, metrics | the swarm | `member_result`, `final`, `ledger`, `metrics` swarm events and the result record |
| The answer scored | ORBIT's solver wrapper | `orbit:answer` and its `orbit_answer` event |
| Model calls | inspect_ai | `ModelEvent`s: today the request before ORBIT's filters; with I2, the request sent |
| What a filter changed, and what the agent received | ORBIT's events today; inspect_ai with I3 | `SentinelEvent`s with the change and the replacement |

ORBIT stops writing `orbit_agent_started` once per activation: a member is one `react()` invocation in one span, and its activations are ORBIT's schedule events inside it.

### The message order before the first model call

For a member of the port with everything present, the first generation is assembled in this order:

1. **react's system message**, ORBIT's agent prompt with its assistant prompt, inserted at index 0 when the loop starts (`src/inspect_ai/agent/_react.py:213-216`).
2. **The start input** ORBIT's controller passes: the group goal as a user message, or a copy of the sample input.
3. **The swarm's preamble**, a user message (`_control.py:343`), unless ORBIT removes it ([open question 1](#open-questions)).
4. **Boundary 0**, the callbacks in order:
   1. `orbit:schedule` holds the member until its first activation and changes nothing;
   2. `orbit:attack` inserts collusion and `inject_prompt` system messages after message 1, and appends untagged runtime-attack messages and pending injections at the end;
   3. `orbit:defense` rewrites tagged messages in place.

   The seam applies the result, a rewrite when a system message was inserted, and writes its events.
5. **`before_turn()`** adds no operator message; compaction, below its threshold, sends the conversation as it is.
6. **ORBIT's delivery wrapper** appends the `notify` notice or `auto` inbox last, if the member has unread records.
7. **Inspect prepares the input**, collapsing consecutive messages of a role for providers that need it, and records the `ModelEvent`.
8. **ORBIT's input filters**, below the event: the memory context appended, safety and vaccination prompts prepended, and boundary monitoring's redactions.

The provider therefore receives `[safety prompts] [agent prompt] [collusion] [compromise] [seed] [preamble] [runtime attack messages] [pending injections] [notice] [memory context]`. That is ORBIT's order today ([Current behaviour](#orbit-104)) with the preamble after the seed, and without legacy observations and dialogue projections, which ported runs do not use. With the preamble removed at boundary 0, the order is ORBIT's.

The integration test in [Testing](#testing) holds this order on an ORBIT-shaped member, at both points where it is visible: the `ModelEvent`'s input and the request a mock provider receives.

### A worked example

ORBIT's `common_channel` example (`ORBIT: examples/common_channel.py:18-38`) has two agents, `writer` and `reader`, both `submit=False`, a channel `team` that both read and only `writer` writes, `notify` delivery, the plan `[[writer, reader], [reader]]` with `model_step`, and `output_agent="reader"`. Its offline test expects one writer call and two reader calls (`ORBIT: tests/test_common_runtime.py:88-133`). ORBIT has no channel-and-schedule example with three agents or with attacks, so this one adds, through `ExperimentConfig.attacks` and `defenses` (`ORBIT: orbit/configs/experiment.py:31-69`), four entries from ORBIT 1.0.4's registries:

- `indirect_injection` with `target_tool="channel_read"` and `target_agent="reader"`: a pending injection for the reader;
- `compromised_agent` with `compromise_mode="inject_prompt"` and `target_agent="writer"`, `timing.phase="pre_deployment"`;
- `dual_llm` targeting `reader`;
- `tool_wrapper` targeting `writer`, with `config={"blocked_tools": ["channel_list"]}`.

These are ORBIT's own components, combined here for illustration, not one of ORBIT's examples.

**The swarm.** ORBIT's task builds:

```python
swarm(
    members=[
        member(orbit_member, args=writer_spec_fields, name="writer", role="writer"),
        member(orbit_member, args=reader_spec_fields, name="reader", role="reader"),
    ],
    controller=orbit_scheduler(experiment=config_json),
    channels=[orbit_channel(communication=communication_json)],
)
```

wrapped in ORBIT's solver. Each member's model has the delivery layer, the filter layer (the compromise prompt filter, because a compromise attack exists) and the observer; the writer also has the tool gate on its tools, and the reader `dual_llm`'s quarantine wrapper on its tools and the output check.

**Run.**

1. The controller registers the three callbacks on both members and starts both with the sample input. Each parks at boundary 0, held.
2. The pre-deployment attacks run: the compromise is due for `writer`; the injection is queued for `reader`.
3. Turn 0, step `[writer, reader]`, sequential:
   - `activate("writer")`. At boundary 0, `orbit:attack` inserts the compromise after the system message; `orbit:defense` does nothing (no `dual_llm` on the writer). The seam writes one `changed` event, origin `orbit:attack`, with an insertion at index 1. The writer's first request: `[agent prompt, compromise, input, preamble]`; no notice, since nothing is unread. It calls `channel_post`; the post is delivered unaddressed with recipients `[reader]`. Its next boundary (`after == "tools"`) ends the activation.
   - `activate("reader")`. `orbit:attack` appends `[Output from channel_read]: <payload>`; `orbit:defense` has `dual_llm` rewrite it, under the same id, from a quarantined model call charged to the reader. Two `changed` events: an append by `orbit:attack`, a replacement by `orbit:defense`. The reader's first request: `[agent prompt, input, preamble, quarantined summary, notice "team: 1 unread"]`. It calls `channel_read`, whose result `dual_llm`'s tool wrapper replaces with a summary, so, as ORBIT's code reads, the observer finds no exposure for that read. Its next boundary ends the activation.
4. Turn 1, step `[reader]`: the reader answers with a tool-free reply, reaching a boundary with `after == "reply"`.
5. The plan is exhausted; the controller returns `schedule_exhausted`. The stop ends both held members, `stopped`. Neither submitted, so the swarm's `final` is `no_submissions`.
6. ORBIT's solver wrapper sets `TaskState.output` to the reader's output from the record, the messages to the reader's saved conversation, and writes `orbit:answer` with `member="reader"`, `rule="output_agent"`, `submitted=false`, `agrees=false`. The `match` scorer scores the reader's reply.

Had the writer called `channel_list`, the tool gate would have refused it with `ToolError("Blocked by defense '<name>': Tool 'channel_list' is blocked")`, recorded as a `defense_intervention`, and the writer's turn would have gone on to its boundary as any tool error does.

### Behaviour a port changes

Runs on the two runtimes should not be pooled unless these are accepted or controlled for; ORBIT records the runtime in each sample's metadata.

- **The preamble** is a user message after the seed, saying the members work "at the same time", unless ORBIT removes it at boundary 0 ([open question 1](#open-questions)).
- **No resume prompt.** A held member never left its loop, so "Please continue with your next action." is never added.
- **Injections and pre-turn defences apply at a member's next activation**, not at the start of every turn for every member. A member's conversation does not change while it is parked, so they land in the same place; ORBIT's eligibility check counts pending injections for the member, as today they are counted after being appended.
- **Defence work is the member's.** `dual_llm`'s pre-turn quarantine calls run at the member's boundary, so they count against its token and cost limits and its ledger line, and as working time unless ORBIT marks them otherwise; today they run in the orchestrator, outside any agent.
- **Conversational members end `stopped`** at the swarm's stop, and the swarm's own answer is a submission only. ORBIT's answer record names what was scored.
- **Member time limits exclude held time** ([swarm-turn-seam.md Time](swarm-turn-seam.md#time)).
- **One span per member**, with `react()`'s own features (compaction, refusal retries, the agent channel) available; ORBIT's activations are its schedule events inside the span.
- **A rewrite at a boundary resets compaction**, and drops summaries made from changed history, where compaction is on.
- **Unsupported members** are refused: background-dispatching `deepagent()` and bridged members.

Unchanged: notices and inboxes (per request, never stored), tool gates and their judges (inside the tool call, charged as now), filters and caching (refused while any filter wraps a member, until I2), the channel's read format, and exposure.

### What stays on ORBIT's executor

- The `legacy/v1` profile and `turn_react` for it, with the `legacy` quantum and legacy channel formats.
- The `tool_step` quantum: refusing a batch of several tool calls before any runs needs a change to `react()`.
- The `summary` and `peer_messages` observation modes.
- Dialogue and activity projections. They could move as ORBIT code (an `orbit:dialogue` callback that inserts the peer's text at activation start, and publishing from the member's own boundary through ORBIT's channel), but a port does not need them first.
- The topology executor and persistent delegation sessions; a root agent with its delegates can be one member if ORBIT wants the ledger.

## Alternatives considered

**ORBIT's own `on_continue` hook, with no seam.** ORBIT's member builder can give `react()` an `on_continue` that waits for the scheduler and returns a replaced state. It needs no swarm change. But it misses boundary 0 and every recovered turn, and the swarm reads a member held there as running: its status after release, its time limit, `swarm_wait` and the stop are all wrong ([swarm-turn-seam.md Why](swarm-turn-seam.md#why)). Rejected for the seam.

**A swarm-owned scheduled mode**: `activate()`, `parked`, `snapshot()`, staged `append()` and `edit()`, an `intervened` event and a `round_robin` controller in inspect_swarm. One implementation for every scheduled harness. But it puts ORBIT's policy into swarm API before a second user asks, and holders compose badly, where callbacks compose by order ([swarm-turn-seam.md Alternatives considered](swarm-turn-seam.md#alternatives-considered)). Rejected: each of these is a few lines of ORBIT's controller over the seam, and `changed` is the record.

**Bus extensions for ORBIT's channel**: a `recipients="roster"` channel class attribute so that addressed records reach pending and ended readers, and a public commit sequence. Rejected: an unaddressed record with recipients the channel computes reaches the same readers, and `apply()` already runs in commit order.

**Exposure reporting through the swarm** (`record_exposure()`, coverage declarations and kept notice texts on the bus). It would check that reported ids belong to the run and member and write the events in the swarm's format. But the swarm would only relay ORBIT's claims, which ORBIT's own events already carry, with ORBIT's store index beside them. Rejected: exposure stays ORBIT's events.

**Naming a read in model input**: stamping read results with the read id in metadata, from a callback or an inspect_ai `tool_result_metadata()` helper. Not needed: ORBIT's observer joins by the tool call id it captures at read time, as today.

**Choosing a member that did not submit as the swarm's answer** (`choose_answer(member, allow_unsubmitted=True)`). The swarm's `final` would then always name ORBIT's answer. But it contradicts the rule that only submissions are answers, which the record's readers rely on. Rejected for ORBIT's own record beside the swarm's.

**Outputs saved by ORBIT's member wrapper**, with no record change. Works on top. But the swarm's answer and ORBIT's would come from two objects, which can differ for a member ended at a boundary or by a hard stop, and Part 2 adds the field anyway. Rejected for S3, which brings that field forward.

**ORBIT's channel off the bus**, keeping its own runtime and tools. Needs no S2. But the swarm's ledger and metrics would not see ORBIT's communication, and a send after the swarm began to stop would not be refused. Rejected as the recommended path; it remains possible.

**Keeping one `run()` per activation on a stored `AgentState`.** Closest to ORBIT today. But it needs clean re-entry of `react()`, loses per-run state (attempts, compaction, MCP connections, the agent channel), and rebinds the member on every activation. Rejected.

**Model wrapping for ever.** It works today. But it needs a private inspect_ai API, refuses caching for the whole run, and logs a request the model never received. Rejected as the end state: I2 and I3.

## Compatibility and migration

- **inspect_swarm** is experimental throughout. S2 adds public names (`Draft`, `Delivery`, `Caller`, `calling_member`, `Bus`) and types `ChannelRun.bus` as the protocol; `shared_memory` moves onto them with no behaviour change. S3 adds an optional record field; the record's version stays 1 and records written before it load. The seam's own compatibility is its design's.
- **inspect_ai**: I1 as the seam designs it; I2 and I3 are additions to sentinel's generate stages and its event, designed there.
- **ORBIT logs.** Logs already written keep their readers. A ported run writes ORBIT's events as today, plus the swarm's communication and swarm events and ORBIT's answer record; ORBIT's store models and its viewer overview are unchanged.
- **Dependencies.** ORBIT accepts `inspect-ai>=0.3.263,<0.4` and locks 0.3.263 (`ORBIT: pyproject.toml:19`). A port depends on inspect_swarm, and so on the inspect_ai it requires.
- **Python and concurrency.** Both require Python 3.11+. inspect_swarm runs on asyncio and trio, so ORBIT's controller and callbacks use anyio.

**Migration path.** Each step is usable on its own; everything not moved keeps running on ORBIT's executor.

1. **After S2 and S3: unscheduled runs.** Configurations that are one concurrent `completion` step move: ORBIT's controller starts every member and waits for them, with ORBIT's channel on the bus and its solver wrapper choosing and recording the answer.
2. **After the seam (S1, I1): scheduled runs.** `model_step` and `completion` plans, round-robin and superstep move, with injections, compromise and pre-turn defences as callbacks. ORBIT's model layers are unchanged.
3. **After I2 and I3: defences as protocols.** Filters and output checks become sentinel protocols, the observer an `AfterGenerate` stage, and the wrappers and `_resolve_config` go; caching works for filtered members.

## Security

- **Callbacks, the controller and the channel are trusted ORBIT code** in the runner process, as ORBIT's extensions are ("Registration and channel permissions are not a plugin sandbox", `ORBIT: docs/security-extensions.md:159-160`). None is reachable from a member's tools, and no model output decides a schedule (`plans.py:1-6`).
- **Peer text in a conversation.** The swarm's rules for peer text govern its own channels. ORBIT's experiments put attacker text in user and system roles on purpose (injections, compromise prompts, `auto` inboxes); that is ORBIT's experiment design. Every change at a boundary is a `changed` event with its origin and hashes; per-request text (notices, inboxes) is in the `ModelEvent` input.
- **Sender identity.** `deliver()` binds the sender from the calling member; ORBIT's channel cannot set it. `calling_member()` reads the same context, never model output.
- **Read evidence.** `record_read()` refuses a caller outside the run and writes only ids and the channel's data; it carries no model text unless the channel puts some in `data`, which ORBIT's does not.
- **The record's outputs** (S3) are readable by any member code that reads the store; a tool written to read them would be a channel between members outside the bus. The docs say the record belongs to the swarm.
- **Untrusted input in ORBIT's code** (tool arguments to gates, channel payloads, model output to filters) is handled by ORBIT as today; the port moves none of it into the swarm.

## Testing

All in inspect_swarm, with mockllm, on asyncio and trio, no network or Docker, with no ORBIT dependency.

- **The public bus API** (`tests/test_bus.py`):
  - a user channel written only with public names delivers a record, and its `sent` and `delivered` events match a built-in channel's;
  - `calling_member()` names the member from a tool in its own loop and from a nested loop's tool (`nested` true), and is `None` outside a swarm;
  - `record_read()` writes the `read` event in the tool span with the caller, the record ids and `data`, returns increasing read ids, and raises outside a member and for an unopened channel;
  - an unaddressed record whose `apply()` names pending and ended members in `notify` and `result` is delivered while they are pending, held at boundary 0, and ended, and the `delivered` event carries them;
  - a counter incremented in `apply()` follows commit order when an earlier record is held by the storm controls;
  - `shared_memory`'s tests pass unchanged on the public names.
- **Member outputs on the record** (`tests/test_record.py`): the output is set after a submission, after a member's own limit, after an end at a boundary, after a stop and after a hard stop, matching the swarm's answer where that member is the answer; a record without the field loads.
- **ORBIT-shaped conformance** (`tests/test_orbit_shape.py`), on the seam:
  - **message order**: a `react(prompt=...)` member whose model is a test `Model` wrapper that appends a system notice, over a `ModelAPI` wrapper that prepends a safety system message and records what it passes on, started with a goal, with three callbacks at boundary 0 that insert a tagged system message after the system prompt, append a tagged user injection, and replace the injection under its id. The first `ModelEvent` input is exactly `[system prompt, compromise, goal, preamble, rewritten injection, notice]`, and the mock provider receives `[safety prompt, ...those]`; with a fourth callback that removes the preamble, both lose it and nothing else changes. Each callback writes its own `changed` event;
  - a controller of `Slot`s alternates two members by `model_step`, one generation each, with no continue prompt added; a concurrent step waits at its barrier; a `completion` activation of a `submit=False` member with `on_continue` returning its state runs to a tool-free reply and stops there, and one of a submitting member runs to submission;
  - an eligibility check on the latest boundary skips a member at its message cap;
  - a reader-and-writer test channel on the public API: messages do not activate members, a post reaches a reader still held at boundary 0, a step-start cutoff hides a post committed during the step, and capacity checked in `admit()` lets exactly one of two concurrent posts take the last place;
  - a member error cancels its sibling and the controller and propagates;
  - a solver wrapper shaped like ORBIT's sets a never-submitted member's output from the record and writes an answer record naming it, after a normal stop, after an outer message limit, and after a sample time limit, with the original error still ending the sample in the last two.

ORBIT's own port tests the same order with its real wrappers and attacks, in ORBIT.

## Implementation plan

inspect_swarm, after M2, in the plan's "later, any order" work:

1. **The public bus API (S2).** `src/inspect_swarm/_bus/_record.py` and `_bus/_bus.py` (`Draft`, `Delivery`, `Caller`, `calling_member()`, `record_read()`, the `Bus` protocol), `src/inspect_swarm/_channel/_channel.py` (`ChannelRun.bus`), `src/inspect_swarm/_evidence.py` (the read writer behind `record_read()`), `src/inspect_swarm/_channel/shared_memory.py` (the public names), `src/inspect_swarm/__init__.py`; `docs/channels.qmd` (writing a channel with records); `tests/test_bus.py`.
2. **Member outputs on the record (S3).** `src/inspect_swarm/_record.py`, `src/inspect_swarm/_controller/_control.py`; `docs/scoring.qmd`; `tests/test_record.py`. Independent of step 1.
3. **ORBIT-shaped conformance tests.** `tests/test_orbit_shape.py`. Needs the seam built ([swarm-turn-seam.md Implementation plan](swarm-turn-seam.md#implementation-plan)), and steps 1 and 2.

inspect_ai and inspect_sentinel: I1 is step 1 of the seam's plan; I2 and I3 are designed and built there. The port itself is ORBIT's work, by the migration path.

## Open questions

1. **The preamble on ORBIT's members.** `start()` always appends the swarm's preamble after the input, saying the members work "at the same time", which a scheduled run makes untrue and ORBIT does not send today.
   - (a) ORBIT removes or replaces it at boundary 0 with its own callback. No swarm change; the removal is a `changed` event and a compaction reset at boundary 0.
   - (b) A swarm option to start a member without a preamble.

   Recommendation: (a). ORBIT can do it on top, and the record shows it.
2. **Member outputs on the record now (S3)**, ahead of the rest of scoring's Part 2, or ORBIT saving outputs from its member wrapper. Recommendation: S3, so the answer ORBIT scores and the swarm's answer come from the same object.
3. **What a generate-stage modify records (I3).** inspect_sentinel's proposal records no copy of a modified request (decision 13); ORBIT needs the request before its filters. Recommendation: record the change by message id and hash, with the original content of removed and replaced messages only, decided in inspect_sentinel's design of the second part.

## Not this design

- **Other designs' references to the earlier version of this one**: swarm.md's ORBIT revision and version and its turn-hook item; swarm-api.md's ORBIT rows (`ActivationPolicy`, `round_robin`, `member(input=)`, routes); swarm-implementation-plan.md's ORBIT future work and its note on `tool_result_metadata()`; swarm-event-types.md's list of ORBIT event kinds. To be aligned in a follow-up.
- **`tool_step` on `react()`**, and replacing a member's agent mid-sample.
- **Scheduling a member's background descendants**, which the seam refuses.
- **Conversation identity for descendants**, which ORBIT's persistent delegation tracks.
- **A board channel with readers and writers in inspect_swarm**, which could take ORBIT's names when it is built.
- **ORBIT's other Inspect gaps** that a port does not need: a public effective-config accessor, `active_model`, a handoff predicate, `as_tool()` keeping `model_input`, and the interrupted `ToolEvent`'s null fields.
- **A possible ORBIT bug**, for the reply to the ORBIT team: `memory_poisoning` queues a pending-injection entry without `applied` (`ORBIT: orbit/attacks/injection/memory_poisoning.py:104-112`), and the pending-injection step does not skip it (`orchestrator.py:939-952`), so as read it is also broadcast as `[Output from unknown]: ...`. Not run.
