# ORBIT on inspect_swarm

## Overview

ORBIT, the Inspect-based framework for multi-agent security experiments, could run its scheduled experiments on inspect_swarm while ORBIT's own code keeps doing almost everything that makes them ORBIT experiments. Its agents become ordinary `react()` members of a swarm, so its fork of `react()` goes. Its scheduler becomes a swarm controller that holds every member at the [turn seam](swarm-turn-seam.md) and lets members run one quantum at a time. At the same boundaries, ORBIT's own callbacks add attack input and let its defences rewrite a member's conversation, and the seam records every change they make. Its reader-and-writer channels become one channel of its own on the swarm's bus. Its exposure observer, input and output filters, notices, answer selection, attacks, defences and scorers stay ORBIT code, unchanged where they can be. The port must behave as ORBIT does today, which ORBIT proves by running its existing scheduler, topology and communication tests on the ported path. ORBIT's state for a run (final conversations, submission order, read cutoffs) stays private to ORBIT, out of the sample's store, which every member's tools share.

inspect_swarm adds only what ORBIT cannot build well on top: the turn seam itself, so that the runtime knows a member is held; a public API for writing a channel on the bus; and a controller's choice of a member's preamble, so that ORBIT's scheduler, which does not run its members at the same time, sends its own wording or none; and a budget that sets no caps of its own, so that the sample's limits end a run as they end ORBIT's executor. ORBIT chooses and records the answer it scores from each member's output on the swarm's result record, which the scoring and result-record designs provide. inspect_ai needs nothing beyond the seam's own turn gate. ORBIT's model-boundary defences stay `Model` wrappers, as they are today; moving them onto sentinel generate-stage protocols is later work for inspect_sentinel's design, and this document lists what that move would need.

This document says, for each ORBIT component, where its code lives on the swarm and which hook supports it; works through a two-agent ORBIT experiment with an injection, a compromise, a quarantine defence and a tool gate; gives the exact message order before a member's first model call; and lists the behaviour a port changes.

## Background

A deep dive on one topic of [swarm.md](swarm.md): how ORBIT, the framework compared in [Relationship to ORBIT](swarm.md#relationship-to-orbit), could run on inspect_swarm. It builds on:

- [swarm-turn-seam.md](swarm-turn-seam.md) for boundary callbacks, held members, `end()` and the `boundary` and `changed` evidence;
- [swarm-api.md](swarm-api.md) for controllers, member handles and stopping;
- [swarm-communication.md](swarm-communication.md) for the bus and channels;
- [swarm-scoring.md](swarm-scoring.md) for the final answer and the result record, whose Part 2 plan adds each member's output to the record in its first step;
- [swarm-result-record.md](swarm-result-record.md) for where the record is kept and its reader, `swarm_result()`;
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

Most of a scheduled mode can be ORBIT's own code. What ORBIT cannot build on top is narrower: the runtime's knowledge that a member is held (without it the swarm reads a held member's status, time, waits and stop wrongly, [swarm-turn-seam.md Why](swarm-turn-seam.md#why)); a supported way to write a channel, since the bus's types and the calling member are private; and a preamble that says only what the controller makes true, since `start()` always appends one saying the members work at the same time; and a run ended by the sample's own limits, since the swarm always derives caps from them, and a cap ends a run as a swarm stop.

## Goals and non-goals

### Goals

- For each ORBIT component, say whether it stays ORBIT code and which inspect_ai or inspect_swarm hook supports it.
- Specify what inspect_swarm adds, each with why ORBIT cannot do it well on top.
- Name inspect_ai's prerequisite, and list what moving ORBIT's model defences onto sentinel protocols would need, as input to that later design.
- Show ORBIT's scheduler as its own controller over the seam.
- Require that a port keep ORBIT's behaviour, proved by ORBIT's own tests on the ported path, with ORBIT's run state private to ORBIT.
- Work one ORBIT experiment through the port: members, channel, schedule, an injection, a compromise, a quarantine defence and a tool gate.
- Say what ORBIT logs and what the swarm logs.
- Give an explicit record of the answer actually scored.
- Give the exact message order before a member's first model call, and an integration test that holds it.

### Non-goals

- **Writing ORBIT's port**, or deciding whether ORBIT adopts inspect_swarm.
- **Designing inspect_ai changes**, including the move of ORBIT's model defences onto sentinel generate-stage protocols, which inspect_sentinel's design decides ([ORBIT's model defences stay wrappers](#orbits-model-defences-stay-wrappers)).
- **Changing the turn seam's contract.** ORBIT is one caller of it.
- **Re-specifying ORBIT's executor.** Eligibility, submission, read cutoffs, attack evaluation and answer selection are ORBIT's code, kept as they are and pinned by ORBIT's tests, not restated here (decision: Ransom, 2026-10-30).
- **Red-team features in inspect_swarm.** Attacks, defences and their registries stay ORBIT's (decision: Ransom, 2026-10-07).
- **ORBIT configurations that do not fit a held `react()` loop**: the `legacy` and `tool_step` quanta, the legacy observation modes, the `legacy/v1` profile, scheduled agents with `tool` or `handoff` delegation edges, and seeds with no user message ([What stays on ORBIT's executor](#what-stays-on-orbits-executor)).

## Current behaviour

### ORBIT 1.0.4

**Agents.** `AgentSpec` (`ORBIT: orbit/configs/setup.py:79-164`) holds a name, an informational `role`, a model and temperature, a system prompt, tool names, `is_compromised`, a per-agent `max_messages` (default 50, `:137`) and `submit`. The scheduled executor builds one `turn_react` per agent (`ORBIT: orbit/agents/topologies.py:443-531`), whose system message is the agent's prompt followed by ORBIT's own assistant prompt, "Do not output more than one tool call...", and the submit prompt (`turn_react.py:72-81`, `:493-526`). A `submit=False` agent gets `react_no_submit` (`:390-455`) and stays in the rotation after a tool-free reply. A scheduled agent may also have `tool` or `handoff` edges to other agents, each a fresh delegate of the target built from the target's own agent (`ORBIT: orbit/agents/topologies.py:173-197`, `:443-460`), so the delegate carries the target's channel tools, sender included (`orchestrator.py:336-342`). `AgentGroup` gives agents a shared goal that replaces the sample input as their first user message (`ORBIT: orbit/configs/execution.py:22-38`, `agent_scheduler.py:243-254`).

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
| 3 | system | the system messages that open the sample input, when the seed is a copy of it (`agent_scheduler.py:222-254`; kept by `ORBIT: tests/test_schedule_plans.py:475-501`) | yes |
| 4 | system | collusion prompts, then the `inject_prompt` compromise, each inserted before the turn after all the seed's leading system messages; row 2 goes in front of them on the first activation (`orchestrator.py:602-614`, `:650-653`) | yes |
| 5 | user first | the rest of the seed: the group goal, or the sample input after its leading system messages | yes |
| 6 | any | untagged runtime-attack messages, appended (`ORBIT: orbit/execution/scheduled_executor.py:141-151`) | yes |
| 7 | user | pending injections, `[Output from <tool>]: ...`, appended, possibly rewritten by `dual_llm` | yes |
| 8 | user | legacy observations and dialogue projections | yes |
| 9 | system | the `notify` notice or `auto` inbox, appended last by the outermost wrapper | no, but in the `ModelEvent` |
| 10 | system | the memory context, appended by its input filter | no, per request, below the `ModelEvent` |

Boundary input monitoring then redacts messages in the provider-bound copy. "Please continue with your next action." is never on a first call.

**Logging.** ORBIT writes `InfoEvent`s (`transcript().info()`):

- communication evidence with source `orbit.communication`: `sent`, `read`, `notified`, `delivered` and `model_exposure`, each with its message ids, agent, session and invocation, and Inspect span, tool-call, event and model-event ids (`runtime.py:190-227`; kinds at `state.py:57-83`);
- `orbit_agent_started` per agent invocation (`ORBIT: orbit/execution/communication.py:38-56`);
- `orbit_schedule` events: step started and finished, activation started and finished with status and reason, and plan amendments (`plans.py:121-132`, `:157-267`);
- `defense_intervention` (each decision, with the redaction text for a blocked input), `defense_resample`, `defense_resolved`, `defense_activity` and `defense_instrumentation_exempt` (`boundaries.py:63-92`, `:204-208`, `:330-334`; `ORBIT: orbit/defenses/evidence.py:110-166`);
- `orbit.attack_injection` and `orbit.attack_outcome` (`orchestrator.py:414-418`, `:485-489`), and runtime errors (`ORBIT: orbit/execution/errors.py:189-215`).

What reaches the model is only partly logged. The `ModelEvent` records the request before the input filters, with the notice already appended (`ORBIT: docs/runtime-errors-and-interventions.md:9-11`, `:167-169`); the filtered request is in no ORBIT event and only in Inspect's raw provider record when that is kept, by default for the first few calls per model plus errors (`src/inspect_ai/_eval/eval.py:319`). A refusal the output check substitutes is only in the agent's state: its `defense_intervention` names the blocked output with no replacement (`boundaries.py:109-110`, `:336-343`), and no event holds the refusal's text, message id or `content_filter` stop reason.

**Exposure.** ORBIT's observer, a `ModelAPI` wrapper below the filters, sees the filtered request (`model_filter.py:186`, `ORBIT: orbit/communication/model_observer.py:24-95`). After a successful generation it matches each read's tool result by tool call id, function, agent and session, captured at read time (`ORBIT: orbit/execution/provenance.py:17-52`), and each rendered record as a newline-delimited block of the result, and matches notices by message id and exact text (`runtime.py:330-399`, `delivery.py:360-406`). The collusion scorer reads the store's `CommunicationState` and reports incomplete observation as unknown, not zero (`ORBIT: orbit/scorers/collusion_scorer.py:78-141`).

**The answer.** At the end, `sync_to_task_state` takes `output_agent`, else the last agent to submit, else the agent of the last step with results; projects that agent's conversation (`selected`) or every agent's (`all`) into `TaskState.messages`; and sets `TaskState.output` to that agent's output if its completion is not empty, without asserting that it submitted (`agent_scheduler.py:813-869`). It runs in the solver's `finally`, after failures and cancellation alike, under `message_limit(None)` (`orchestrator.py:1038-1099`, `:1057-1060`). Nothing records which agent's output was scored or whether it submitted: the submit registry is in memory only, and the halt reason is a transcript string (`ORBIT: orbit/execution/submit_registry.py:23-112`, `orchestrator.py:731-735`).

### inspect_swarm

- **A member's first conversation**, verified by running two `react(prompt=...)` members on mockllm under a controller that started each with `start(member, input=[system, user])`: the first `ModelEvent` input was react's system message, the start input in order, then the swarm's preamble as a user message. react inserts its system message at index 0 (`src/inspect_ai/agent/_react.py:213-216`), and `start()` builds the input followed by the preamble (`src/inspect_swarm/_controller/_control.py:335-343`). The preamble names the member, the roster size and its role, says the members work "at the same time", and adds each channel's instructions (`src/inspect_swarm/_swarm.py:136-175`). `start(member, input=None)` takes no argument that changes it or leaves it out (`_control.py:323-343`).
- **The bus is private.** A channel's `open()` gets `ChannelRun.bus`, typed as the private `Bus` (`src/inspect_swarm/_channel/_channel.py:84-92`). Its tools call `Bus.deliver(Draft(...))` (`src/inspect_swarm/_bus/_bus.py:138-195`), where `Draft` and `Delivery` are private (`_bus/_record.py:67-78`, `_bus/_bus.py:28-35`). The calling member is `Bus.member()`, returning the private `MemberContext` (`_bus.py:211-216`), and a read's evidence is written by the private `comm_read()` with `Bus.read_id()` (`src/inspect_swarm/_evidence.py:102-127`, `_bus.py:206-209`): an `InfoEvent` whose top-level fields are the member, spans, loop, `nested`, channel, read id, `files` and `listing`. The ledger reads `listing` from the top level (`src/inspect_swarm/_ledger.py:339-368`), and the metrics count a read with `listing` true as a listing and any other as a read (`src/inspect_swarm/_metrics.py:271-276`). `shared_memory` uses all of these from inside the package (`src/inspect_swarm/_channel/shared_memory.py:11-14`, `:517-526`, `:610-620`, `:913-927`). The docs say a channel with records goes through the bus, and document no way to do it (`docs/channels.qmd:77`).
- **Addresses and order.** Step 0 of `deliver()` refuses an address to a member that is pending or has ended (`_bus.py:234-251`); an unaddressed record resolves to nobody. `validate()`, `admit()` and `apply()` run with no `await` between them, so `apply()` sees records in commit order; what `apply()` returns (`notify`, `result`) goes into the `delivered` evidence (`_bus.py:166-195`).
- **Tools.** `swarm_tools()` is a `ToolSource`: it holds no tools, and gives each loop the tools of every channel state when the loop resolves its tools, in the member's context, binding the member's own loop on its first resolution (`src/inspect_swarm/_bus/_tools.py:11-34`). Inspect turns a source's tools into `ToolDef`s only then (`src/inspect_ai/tool/_tool_def.py:198-211`), so a wrapper applied to the list a member builder passes to `react()` never sees them.
- **The result record** has each member's state, status, submission and time, not its output (`src/inspect_swarm/_record.py:12-46`); the runtime holds ended members' outputs in memory (`_control.py:644-647`). [swarm-scoring.md What the record adds](swarm-scoring.md#what-the-record-adds) adds `MemberResult.output`, written when a member ends, with or without a result contract, in step 1 of its Part 2 plan; [swarm-result-record.md](swarm-result-record.md) keeps the record in the transcript, written when the swarm ends however it ends, and read with `swarm_result()`.
- **The answer** is a submission only: `first`, `last` or the controller's `choose_answer()`, which refuses a member that did not submit (`src/inspect_swarm/_final.py:29-67`, `_control.py:373-387`). The returned messages are the input and the answer's message, marked with the mode and member (`_final.py:124-137`).
- **Controllers** see names, roles, states and statuses, never text (`_control.py:56-87`).
- **The budget.** `swarm()` derives cost, token and time caps from the sample's limits, holding back `reserve`, by default 5% (`src/inspect_swarm/_budget.py:53`, `:160-166`); an unset cap is always derived, so a sample with limits always has swarm caps. A call that reaches a swarm cap ends its member `swarm_cap`, and the swarm returns normally after its stop procedure ([budget.qmd](../docs/budget.qmd)); the cost cap is checked with the token check (`_budget.py:438-455`), before Inspect checks the sample's cost limit. Verified with a priced mockllm member making a $1.20 call under a $1 sample cost limit, with a solver after the swarm: under `Budget(reserve=0)` the sample recorded no limit and the later solver ran; under an explicit cost cap above the sample's limit, which the swarm allows with a warning (`_budget.py:175-182`), the sample's cost limit ended the sample and the later solver did not run.
- **The final step.** After the stop, the swarm sets its state's messages to its input followed by the answer's message, under the sample's message limit (`src/inspect_swarm/_final.py:95-111`, `:124-137`; `_control.py:448-462`). It uses its input only there and as `start()`'s default input (`_control.py:335-343`). Verified on asyncio and trio: under a sample message limit of ten, a swarm run on a ten-message input, whose one member started on a short goal and submitted, raised `Message limit exceeded. count: 11; limit: 10` from that step; run on an empty input, it returned the answer's message alone.

### The turn seam

This design uses the seam's contract as [swarm-turn-seam.md](swarm-turn-seam.md) specifies it: boundary callbacks registered with an origin on a member's handle ([Registering](swarm-turn-seam.md#the-callback-contract)), boundary 0 before the first generation, a held member's time, waits and stop ([Time](swarm-turn-seam.md#time), [Stopping](swarm-turn-seam.md#stopping)), one `changed` event per callback that changes the conversation ([Evidence](swarm-turn-seam.md#evidence)), and the members it refuses ([Supported members](swarm-turn-seam.md#supported-members)). The seam ships no `activate()`, snapshot or intervention API: those are the caller's.

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
| The preamble (ORBIT sends none) | ORBIT's controller | `start(member, preamble=False)` ([S3](#s3-a-controllers-preamble)) |
| Scheduler: modes, plans, quanta, barriers, `after_activation`, `max_messages`, halt conditions | ORBIT's `@swarm_controller` ([below](#scheduling-orbits-controller-over-the-seam)) | `on_boundary()`, `start()`, `events()`, stop reasons ([S1](#s1-the-turn-seam)) |
| Snapshots for eligibility and attack outcomes | ORBIT's controller, with final conversations in ORBIT's run state ([What the port must keep](#what-the-port-must-keep)) | `Boundary.messages` |
| Pending injections, `inject_prompt` compromise, runtime attack messages | ORBIT's `orbit:attack` callback | The seam; each change a `changed` event ([S1](#s1-the-turn-seam)) |
| Pre-turn defences (`dual_llm`) | ORBIT's `orbit:defense` callback | The seam |
| Channels, communication edges, capacity, cursors, secret channels, `step_start` visibility | ORBIT's `@swarm_channel`, with read cutoffs in ORBIT's run state | The public bus API ([S2](#s2-a-public-bus-api-for-channels)) |
| `notify` and `auto` delivery | ORBIT's delivery `Model` wrapper, unchanged | None needed |
| Exposure observer | ORBIT's observer, unchanged, writing ORBIT's own events | Read evidence from `record_read()` ([S2](#s2-a-public-bus-api-for-channels)) |
| Tool gates, `dual_llm`'s tool wrapper | ORBIT's tool wrappers, unchanged, applied by the member builder to its own tools and, through an ORBIT tool source, to the channel tools `swarm_tools()` resolves ([Members](#members)) | None needed |
| Input filters, output checks and resampling | ORBIT's filter and output-check wrappers, unchanged | None needed ([ORBIT's model defences stay wrappers](#orbits-model-defences-stay-wrappers)) |
| Submission order (`last_submitted`) | ORBIT's submit tool behind its defence chain, given to `react()`; the registry in ORBIT's run state | `AgentSubmit(tool=)` |
| The sample's token, cost and time limits | ORBIT's task, which leaves them to end the run | `Budget(derive=False)` ([S4](#s4-a-budget-that-sets-no-caps-of-its-own)) |
| Answer selection (`output_agent`) and history projection | ORBIT's solver wrapper around the swarm | Each member's output on the result record ([swarm-scoring.md step 1](swarm-scoring.md#what-the-record-adds)), read with `swarm_result()` ([swarm-result-record.md](swarm-result-record.md)) |
| A record of the answer scored | ORBIT's solver wrapper | The same |
| `CommunicationState`, the collusion scorer, the viewer overview | ORBIT, unchanged: its channel keeps writing its own state and events | None needed |
| Scenarios, datasets, attack and defence registries, scorers, judges, YAML and CLI | ORBIT, unchanged | None needed |
| `legacy` and `tool_step` quanta, legacy observation modes, `legacy/v1` | ORBIT's executor | None ([What stays on ORBIT's executor](#what-stays-on-orbits-executor)) |

So a ported run is ORBIT code throughout, on four swarm additions, two parts of other swarm designs (member outputs on the record and `swarm_result()`), and the seam's own inspect_ai prerequisite.

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
        listing: bool = False,
        records: Sequence[str] = (),
        fields: Mapping[str, JsonValue] | None = None,
    ) -> str: ...                                         # writes the `read` event; returns its read id
```

- **`deliver()`** is today's, unchanged: it binds the sender from the calling member, resolves addresses, runs the channel's `validate()`, the storm controls, `admit()`, the `sent` evidence, `apply()` and the `delivered` evidence, and returns the `Delivery`. Called outside a member of the run, it raises `RuntimeError` as today.
- **`record_read()`** writes the `read` evidence `comm_read()` writes today, in today's shape, for the calling member: an `InfoEvent` with the member, its span, the loop and `nested` taken from the caller, the channel, a new read id, the current tool span and `listing` as top-level fields, `records` beside them when it is not empty, and each of `fields` beside them too. It returns the read id (`r-5`). `listing` says the read showed what exists rather than its content, which the ledger and the metrics count apart, as today; `records` names the bus records whose content the read showed. A key of `fields` that is one of the event's own (`version`, `type`, `kind`, `member`, `span_id`, `loop`, `nested`, `channel`, `read`, `tool_span_id`, `listing`, `records`) raises `ValueError`, as does a channel name the run has not opened; outside a member of this run it raises `RuntimeError`. `shared_memory` calls it with `listing=` and `fields={"files": files}`, which writes exactly the event it writes now, so the ledger's `_comm_record()` and the listing and read metrics read it unchanged. The typed-event migration maps the event later, as it maps shared memory's reads ([swarm-event-types.md](swarm-event-types.md#how-inspect_swarm-writes-them)); S2 does not depend on it.
- **`calling_member()`** is the public form of `Bus.member()`, for channel tools and for any code in a member's context that needs to know which member it acts for. It returns a frozen `Caller`, never the runtime's mutable context.
- **`ChannelRun.bus`** is typed as the `Bus` protocol. `shared_memory` moves onto the public names, with no change in behaviour.

Two things ORBIT's channel needs are deliberately not added, because the channel interface already gives them:

- **Readers that have not started or have ended.** ORBIT's channels are blackboards: membership, not activity, decides who reads, and under a schedule the first post goes to readers still parked at boundary 0, or pending. An addressed record would be refused at step 0, so ORBIT's channel posts **unaddressed** records (`to=()`), computes its recipients itself from readers and edges, as it does now, and returns them in `apply()`'s `result`, which the `delivered` evidence carries. `notify` names the recipients whose loop the record is news to.
- **A commit sequence for `step_start` visibility.** `apply()` runs in commit order with no `await` around it, so a counter the channel increments in `apply()` is a commit sequence; ORBIT's own store counter, incremented there, serves. Record ids are not used for it: one is given before the storm controls and `admit()`, so a refused record consumes an id and leaves a gap, where ORBIT's counter, like its store counter today, counts only applied records.

#### S3. A controller's preamble

`start()` always appends the swarm's preamble, which tells a member it is one of N agents "working on this task at the same time". A scheduled controller makes that untrue, and ORBIT sends its agents no such message today. A controller is the one place that knows how its members run, so it chooses a member's preamble when it starts the member (decision: Ransom, 2026-10-29: changing the preamble is more broadly useful, and simpler, than changing the conversation at boundary 0):

```python
# inspect_swarm, SwarmControl
def start(
    self,
    member: str | MemberHandle,
    input: str | list[ChatMessage] | None = None,
    *,
    preamble: str | Literal[False] | None = None,
) -> None:
    """...

    Args:
        preamble: The member's preamble, a user message after its input. `None` gives it the swarm's preamble; a string replaces the swarm's preamble with that text; `False` leaves it out.
    """
```

- **`None`** is today's behaviour, unchanged for every existing controller.
- **A string** replaces the whole preamble, channel instructions, budget split and sandbox statement included, so a controller that replaces it states what its members need of them. The text is the controller's, as `input` is: the swarm does not change it, also for a member with `choose_name=True` ([swarm-chosen-names.md](swarm-chosen-names.md)). An empty or whitespace-only string raises `ValueError` saying to pass `False`.
- **`False`** adds no message. Then the input must hold at least one `ChatMessageUser`, since a conversation without one would leave `react()` waiting for operator input ([swarm-turn-seam.md The callback contract](swarm-turn-seam.md#the-callback-contract) holds callbacks to the same rule); otherwise `start()` raises `ValueError` naming the member, before the member starts. A string `input` is always a user message.
- **Any other value** (a list, a message, `True`) raises `TypeError`.
- The checks run before `start()` changes the member's state, so a refused call leaves the member pending.

ORBIT's controller passes `preamble=False`, so its members start with ORBIT's seed alone, as today; it could pass its own wording instead, such as a statement of the schedule. Its channel's `instructions()` returns `None`, since ORBIT's tools describe themselves.

#### S4. A budget that sets no caps of its own

ORBIT's runs end at the sample's limits: the limit's error propagates out of ORBIT's solver, after its finalization, and no later solver runs. Under a swarm, a derived cap ends the run first, as a swarm stop that returns normally: every derived cap does under a reserve, and the cost cap does even at `reserve=0`, since it is checked before the sample's cost limit ([Current behaviour](#inspect_swarm)). An explicit cap above each sample limit leaves the sample's limit to act, but a task cannot know the limits an eval will set, and every sample would log the swarm's warning. So `Budget` gets one field:

```python
class Budget(BaseModel):
    ...
    derive: bool = True
    """Whether a cap left `None` is derived from the sample's limit. `False` sets no such cap: the sample's own limits then end the swarm as they end any solver."""
```

- **`True`** is today's behaviour, unchanged.
- **`False`**: `derive_caps()` derives nothing, so each cap left `None` is no cap, as it is today under a sample without that limit; a cap given explicitly is checked and applied as today. `reserve` then has no effect. A split budget with neither a cost nor a token cap is refused with a `ValueError`, as today under a sample without those limits (`_budget.py:192-195`); ORBIT's is a pool.

ORBIT's task passes `Budget(derive=False)`.

### I1. The inspect_ai prerequisite: the turn gate and the compaction reset

The private turn gate in the agent channel and `reset()` on react's compaction handler, as [swarm-turn-seam.md](swarm-turn-seam.md#the-inspect_ai-change-a-turn-gate-and-a-compaction-reset) designs them. The seam needs them; ORBIT needs nothing more of inspect_ai.

### ORBIT's model defences stay wrappers

ORBIT keeps its model wrappers on the port: the delivery wrapper, the output check with its resampling, the input filters and the exposure observer, built as today ([Delivery, exposure and filters](#delivery-exposure-and-filters)). That is this design's end state (decision: Ransom, 2026-10-30). Moving them onto sentinel generate-stage protocols, which would drop the private `_resolve_config` and let filtered members use the response cache, is later work, decided in inspect_sentinel's design of its generate stages' second part (`design/generate-stages.md`, sections c and d). As input to that design, not a contract specified here, the move would need:

- **A refusal outcome.** A stage can return an output with `stop_reason="content_filter"` and no tool calls, which ORBIT's output check returns when it exhausts its resamples (`ORBIT: orbit/defenses/boundaries.py:336-343`), so react's refusal retries and its exit after three refusals in a row (`src/inspect_ai/agent/_react.py:290-297`, `:792-796`) keep working. The proposal recomputes a replaced output's stop reason as `tool_calls` or `stop` (section d).
- **Charged draws.** Every draw a protocol makes in place of the agent's output is charged to the member's and the sample's token limits, as ORBIT's wrapper charges each resample today (`boundaries.py:312-335`). Protocols run under `suspend_token_limit()` (`src/inspect_ai/_sentinel/_dispatch.py:298-300`), which stops metering at every enclosing token scope (`src/inspect_ai/util/_limit.py:649-674`), and the proposal does not charge `resample`'s draws to the agent (section d).
- **Helper calls marked.** ORBIT's auxiliary model calls, such as `dual_llm`'s quarantine model at a boundary (`ORBIT: orbit/defenses/dual_llm.py:370-375`), run in the member's task and span, so Inspect would take them for the member's own turns and run its generate stages on them (`src/inspect_ai/_sentinel/_context.py:68-69`). ORBIT marks them with `model_origin("helper")` (`src/inspect_ai/model/_origin.py:9`), so that member-keyed filters, such as the compromise prompt filter, and the exposure observer skip them.
- **An opt-in detailed record of a modify.** Sentinel's proposed default stays as it is (decision 13: a `BeforeGenerate` modify keeps no copy of the request), and a protocol that rewrites a request can opt in to recording its change by message id: the ids, positions and hashes of the messages added, removed and replaced, and the original content of each message removed or replaced. ORBIT's filters would opt in, so the log keeps what it holds today, the request before ORBIT's filters (decision: Ransom, 2026-10-29: the default is not changed without the people who own it).

### ORBIT's code on the swarm

#### What the port must keep

This design does not re-specify ORBIT's executor, and it does not prove, edge by edge, that the port matches it. A ported run behaves as it does on ORBIT's executor, except for the changes in [Behaviour a port changes](#behaviour-a-port-changes), and ORBIT's own tests prove it by running on both executors ([Testing](#testing)): its scheduler, topology and communication suites, plus a test for each edge below that they do not pin yet, written on ORBIT's executor first. The list below is the one place this design claims parity. A probe that finds another edge adds a line to it, and review blocks only on the contracts the seam, the bus, the preamble and the budget expose (decision: Ransom, 2026-10-30).

**Differences the dual-executor tests must check:**

- **Seeds.** A seed whose first message is a system message with the agent's rendered prompt gets no second copy, as `_ensure_system_message()` gives it none (`turn_react.py:469-490`): the first request and its message-limit check are the same. ORBIT's controller can leave that message out of the start input, since react inserts the same prompt.
- **Eligibility.** `max_messages` at the first activation, for a group goal and for a sample input that opens with a system message.
- **Quanta, barriers and halting.** Plan steps in order and concurrently, each halt condition, and the submit setting with the flag omitted under each scheduling mode and set either way.
- **Continue prompts.** None after a tool-free reply that ends a `model_step` activation, submitting or not; within a submitting `completion` activation, the prompt `turn_react` adds today (`turn_react.py:350-372`).
- **Attack outcomes** of every activation, a member's final turn included, ended by submission and by its message limit.
- **Tools.** The defence chain on every tool, the submit tool and the channel tools included; a denied submit records no submission and leaves the submission order unchanged; `last_submitted` after two concurrent submits where the first to submit ends last.
- **Broadcasts.** A pending injection with no target reaches every member that is unsubmitted when it falls due, once each, at the same place in each conversation.
- **Due interventions.** An injection or compromise due at a turn whose step skips its target is applied, in the form the pre-turn defences give it, at the target's first activation at or after that turn.
- **Defences before use.** With `dual_llm` on a compromised target, no model call carries the raw compromise where ORBIT's executor sends the defended one.
- **Committed changes only.** After a pre-turn defence that raises, and an injection the message limit refuses, an intervention is recorded applied, with its evidence, exactly when it is in the target's final conversation.
- **Read cutoffs.** `step_start` on every path that selects records (explicit reads, notices and `auto` inboxes) under `poll`, `notify` and `auto`, with cursors and exposure.
- **Output checks.** An exhausted output check, with `retry_refusals` set and not; a resampling check under a member and a sample token limit, every draw charged to both.
- **Sample limits.** A priced call over the sample's cost limit, a token limit crossed at a known call, and the time limit each end the sample on that limit after the same call, with ORBIT's answer and record set, and no later solver runs.
- **Finalization.** An `all` projection longer than the sample's message limit is kept whole; a sample input as long as that limit, with a member started on a short group goal that submits, is scored on the submission with no limit error; a finalization failure after a sample limit leaves that limit's error propagating.
- **The answer.** Answer selection and history projection under each rule; an empty submission (a tool-only `submit(answer="")`) keeps the output the task had before the swarm; a member output that begins with `attachment://` or `tc://`, in the selected member and in another, is scored as written.

- **ORBIT's run state stays private to ORBIT.** The state the port adds for a run lives in one ORBIT object per sample, shared by ORBIT's controller, member wrappers, channel and solver wrapper, and never under a sample-store key. That state is each member's final conversation, the submission order, each member's read cutoff, the schedule state and the halt reason. The solver wrapper creates the object before the swarm, and the others reach it through a context variable it sets, which every task the swarm starts inherits. The sample's store is shared by every member (`src/inspect_swarm/_member.py:221`), and `tool_state_scope()` changes only the default namespace of tools that use it. In the store, a peer's store tool could read a member's conversation, instructions and answer while the swarm runs, or rewrite the submission order and so change the answer scored. ORBIT's channel state, in store models today (`state.py:120-155`), stays where it is: the port moves no existing ORBIT state.

#### Members

ORBIT's member builder is a registered `@agent` that takes the spec's fields as arguments, so the plan logs them. It builds `react()` from what ORBIT's builder gives `turn_react` today (`ORBIT: orbit/agents/topologies.py:443-531`). That is the agent's prompt with ORBIT's assistant prompt, so the system message is unchanged; its tools and its submit tool, all behind the member's defence chain; its model layers; and its submit setting, the scheduling mode's default included. The swarm changes four things:

- **Channel tools come through a tool source.** `swarm_tools()` gives a loop its channel tools only when the loop resolves its tools ([Current behaviour](#inspect_swarm)), so a wrapper applied to the list the builder passes to `react()` never sees them. ORBIT passes its own `ToolSource`. It resolves `swarm_tools()` in the same call, and so in the member's context and loop, then returns each tool through the member's chain. The channel tools get the gate and quarantine they get today, and the swarm's binding of the member's loop and its `reach` rules are unchanged.
- **The submit tool** is ORBIT's registry tool (`ORBIT: orbit/execution/submit_registry.py:115-167`), wrapped by the chain and then given to `react()` as `AgentSubmit(tool=...)`. A denied submit therefore neither records a submission nor ends the member. The registry is in ORBIT's run state. `react()` ends the member on the tool as on its own submit tool, and the swarm records a submission as usual.
- **Continuation.** Every member's `on_continue` is ORBIT's continuation adapter, which keeps each quantum's continue-prompt behaviour as it is today ([What the port must keep](#what-the-port-must-keep)). Without an adapter, a submitting `react()` appends its continue prompt after every tool-free reply (`src/inspect_ai/agent/_react.py:404-417`), where `turn_react` ends a `model_step` activation with none (`turn_react.py:350-352`); and `react(submit=False)` ends at its first tool-free reply, with no boundary after it ([swarm-turn-seam.md Supported members](swarm-turn-seam.md#supported-members)). With it, a tool-free reply reaches a boundary with `after == "reply"`, carrying only the messages `turn_react` would have added.
- **The final conversation** is saved in ORBIT's run state by a synchronous `finally` in the member wrapper, inside the agent the swarm awaits. It is therefore saved before the swarm queues `MemberEnded`, however the member ends. The wrapper also enters ORBIT's communication identity for its observer, as `observe_agent` does today.

#### Scheduling: ORBIT's controller over the seam

ORBIT's scheduler becomes a `@swarm_controller` of its own: ORBIT's outer loop and `ScheduleRuntime`, over the seam. It registers three callbacks on every member, in this order: `orbit:schedule`, `orbit:attack` and `orbit:defense`. It then starts each member with its seed and `preamble=False` ([S3](#s3-a-controllers-preamble)), and each member parks at boundary 0.

- **Holding.** `orbit:schedule` parks a member at each boundary that ends an activation and waits for the scheduler's go inside `suspend_working_limit()`, as [swarm-turn-seam.md](swarm-turn-seam.md#the-callback-contract) sketches ORBIT's activation. Inside a `completion` activation it lets the member run on: a submitting member to its submission, a `submit=False` member to a boundary with `after == "reply"`. Activating a member sets its go and waits for its next park or its end (`MemberEnded` from `events()`).
- **Snapshots.** Eligibility checks and attack evaluation read `Boundary.messages` at a park, and the saved final conversation for a member that ended during an activation. They are ORBIT's code, as are `after_activation` amendments and the `orbit_schedule` events. Where a snapshot differs from what ORBIT's executor reads, ORBIT's code absorbs the difference. One example: at boundary 0 a snapshot already holds react's system message, which `turn_react` inserts only after the eligibility check (`turn_react.py:469-490`). ORBIT's tests on the ported path catch any difference it misses.
- **Plan steps** use anyio. A sequential step activates its members in order. A concurrent step activates them in a task group whose exit is the barrier. A member's error propagates as the swarm's foreign error and cancels its siblings and the controller.
- **Halting.** The controller returns ORBIT's halt reason (`controller:schedule_exhausted`, `controller:all_agents_submitted` and so on). Returning requests the swarm's stop, which ends every held member as `stopped` ([swarm-turn-seam.md Stopping](swarm-turn-seam.md#stopping)).
- **Messages never activate a member.** Only the controller's go does.
- **Limits.** ORBIT's task passes `budget=Budget(derive=False)` ([S4](#s4-a-budget-that-sets-no-caps-of-its-own)), so the swarm sets no caps of its own and the sample's token, cost and time limits end a ported run. `max_turns` and `max_time_seconds` stay the controller's halt conditions, and `max_messages` its eligibility check.

#### Interventions at a boundary

At a boundary that begins an activation, ORBIT's `orbit:attack` callback adds the attack input that is due for the member (pending injections, collusion prompts, the `inject_prompt` compromise, untagged runtime-attack messages), and then `orbit:defense` runs the pre-turn defences (`dual_llm`'s rewrite), so a defence sees attack input before the member's next generation, as today. What they add, where, and in what form is ORBIT's code as today (`orchestrator.py:602-654`, `:937-973`). The seam applies their result and records one `changed` event per callback. Broadcasts, interventions due while their target is not activated, defences before use and changes that do not commit are on the [list of differences](#what-the-port-must-keep).

#### The channel

ORBIT's `channel_post`, `channel_read` and `channel_list` become the tools of one ORBIT `@swarm_channel`, say `orbit`, with `reach="loop"`, whose state keeps ORBIT's store-backed registry, messages, receipts and cursors, attack-registered secret channels included. Its semantics are ORBIT's, as its communication suites pin them. From the swarm it takes:

- **Delivery.** A post is an unaddressed `deliver()` whose recipients ORBIT computes and `apply()` returns ([S2](#s2-a-public-bus-api-for-channels)). Capacity is checked in `admit()`, and ORBIT's counter is incremented in `apply()`, with no `await` between them, so two concurrent posts cannot both take the last place.
- **The caller.** The tools find the posting or reading member with `calling_member()`. No ported member has a nested loop that uses the channel, since configurations with delegation edges stay on ORBIT's executor.
- **Evidence.** A read calls `record_read("orbit", records=[...])` with the bus record ids of the messages it rendered, beside ORBIT's own read evidence under the tool call id its provenance captures (`ORBIT: orbit/communication/runtime.py:300-327`). The bus writes the swarm's `sent`, `delivered` and `read` events for ORBIT's records, which the swarm's ledger and communication metrics count; ORBIT keeps writing its own `orbit.communication` events and `CommunicationState`, each carrying the swarm's record id, so the two join. The collusion scorer and the viewer overview read ORBIT's state as now.
- **Waking.** `wake=False`: no ORBIT record wakes a wait or activates a member.

#### Delivery, exposure and filters

ORBIT's model layers stay as the member builder builds them, in today's order: delivery outermost, the output check, the filter wrapper (which records the `ModelEvent`), the observer below the filters, then the provider. Nothing in the swarm changes them:

- `notify` and `auto` are computed per request from ORBIT's channel state, for the member the builder was built for, under that member's `step_start` cutoff as an explicit read is. Today the cutoff is a context variable set around each step's execution (`runtime.py:36-48`). A member's task now lives across steps, and a value the controller sets does not reach it, so the selection reads the cutoff from ORBIT's run state instead.
- The observer matches reads as today, by the tool call id captured at read time; the swarm puts no metadata on tool results and ORBIT needs none.
- Exposure and coverage stay ORBIT's `model_exposure`, `notified` and `delivered` events and `observation_enabled_sessions`.

These layers stay wrappers ([ORBIT's model defences stay wrappers](#orbits-model-defences-stay-wrappers)), built through the private `_resolve_config` as today, and a filtered member still refuses the response cache.

#### The answer, and a record of the answer scored

ORBIT's task wraps the swarm in a solver of its own, which runs the swarm agent with `run()` on an empty input: ORBIT's controller gives every member its seed through `start(member, input=...)`, so the swarm needs no input of its own. The swarm's final step therefore holds only the answer's message, which no sample message limit of one or more refuses, while every member's conversation stays under that limit ([Current behaviour](#inspect_swarm)); and `run()` leaves the `TaskState` to ORBIT's wrapper. After the swarm, in a synchronous `finally`, it selects the answer by ORBIT's rule and projects history as `sync_to_task_state` does today. It reads each member's output from the swarm's result record with `swarm_result()` ([swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result)), and each member's conversation and the submission order from ORBIT's run state. The wrapper differs from today in three ways:

- When ORBIT's rule selects no nonempty completion, it keeps the output the task had before the swarm, as ORBIT does today.
- Selection, projection and the answer record run under `message_limit(None)`, as ORBIT's finalization does today (`ORBIT: orbit/solvers/orchestrator.py:1057-1060`), so the sample's message limit never cuts a projection, an `all` projection included. With the empty swarm input, no step of the wrapper's finalization raises that limit, as none of ORBIT's does today.
- An exception that ended the swarm propagates unchanged, after the output, the messages and the answer record are set. A failure in the wrapper's own finalization, the record reader's included, never replaces it: ORBIT records the failure and adds it as a note to that exception, as its cleanup does today (`ORBIT: orbit/solvers/orchestrator.py:1069-1089`). With no such exception, the failure propagates.

**What ORBIT needs of the record's reader.** `swarm_result()` must return the record whatever literal text the members' outputs hold. [swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result) refuses, with `ValueError`, any string in a record that reads as an unresolved attachment reference, a member's literal output included, and its implementation refuses every string that begins with `attachment://` or `tc://`. Once outputs are on the record, one member whose output begins that way, selected or not, would make every member's output unreadable, and ORBIT's experiments put attacker-controlled text in member outputs on purpose. So the port depends on the record telling a member's literal text from a reference left unresolved; how it does is that design's to decide ([Not this design](#not-this-design)), and the port does not move until it does ([Compatibility and migration](#compatibility-and-migration)).

**The answer record.** ORBIT writes it to the store under `orbit:answer`, so a re-scored log keeps it, once every member has ended, so no member's tool reads or changes it, and as an ORBIT `InfoEvent` (`{"orbit_answer": ...}`) in the solver's span:

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `selected` | the member ORBIT's rule selected; `None` when none was |
| `rule` | the rule that selected it: `output_agent`, `last_submitted` or `last_result`; `None` when none did |
| `member` | the member whose output is `TaskState.output`: `selected` when its output was used, `None` when the output the task had before the swarm was kept |
| `status`, `submitted` | `member`'s status and submission, from the swarm's record; `None` without a member |
| `message_id` | the id of `TaskState.output`'s message when `member` is set; `None` otherwise |
| `swarm_final` | the swarm's own `final`: its mode, member and reason |
| `agrees` | whether `member` is the swarm's `final.member` |
| `provisional` | whether the swarm's record was provisional (a limit, error or cancellation ended the swarm before it finalised) |
| `halt_reason` | ORBIT's halt reason, from ORBIT's run state; `None` when the swarm ended before the controller returned |

So the answer actually scored is named in one place, with how it was chosen and whether it was a submission, and every field describes the output that was scored. Before it returns, ORBIT's controller computes the same selection; when the selected member ended `submitted`, it calls `choose_answer(member, reason=rule)`, so the swarm's own `final` names that member, and `agrees` is true when that member's output is the one used. A submission with an empty completion (a tool-only `submit(answer="")`) leaves the output the task had before the swarm, so `member` is `None` and `agrees` is false although the swarm's `final` names the submission. When the selected member did not submit, the swarm's `final` follows the controller's `final` rule over the other members' submissions: another member's submission when one submitted, `no_submissions` when none did; `agrees` is false and only ORBIT's record names what was scored. The swarm's rule and record are unchanged.

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
| Model calls | inspect_ai | `ModelEvent`s, recording the request before ORBIT's filters, as today |
| What a filter changed, and which output the check blocked | ORBIT's filters and output check, as today | `defense_intervention`, `defense_resample` and `defense_activity`; a substituted refusal is only in the member's conversation, as today |

ORBIT stops writing `orbit_agent_started` once per activation: a member is one `react()` invocation in one span, and its activations are ORBIT's schedule events inside it.

### The message order before the first model call

For a member of the port with everything present, the first generation is assembled in this order:

1. **react's system message**, ORBIT's agent prompt with its assistant prompt, inserted at index 0 when the loop starts (`src/inspect_ai/agent/_react.py:213-216`).
2. **The start input** ORBIT's controller passes, its seed: the group goal as a user message, or a copy of the sample input, whose leading system messages, if it has any, come first; a first system message with the agent's rendered prompt is not repeated ([What the port must keep](#what-the-port-must-keep)).
3. **No preamble**: ORBIT's controller starts its members with `preamble=False` ([S3](#s3-a-controllers-preamble)).
4. **Boundary 0**, the callbacks in order:
   1. `orbit:schedule` holds the member until its first activation and changes nothing;
   2. `orbit:attack` inserts collusion prompts, then the `inject_prompt` compromise, after all the leading system messages, which are message 1 and any leading system messages of the seed; then appends untagged runtime-attack messages and pending injections at the end;
   3. `orbit:defense` rewrites tagged messages in place.

   The seam applies the result, a rewrite when a system message was inserted, and writes its events.
5. **`before_turn()`** adds no operator message; compaction, below its threshold, sends the conversation as it is.
6. **ORBIT's delivery wrapper** appends the `notify` notice or `auto` inbox last, if the member has unread records.
7. **Inspect prepares the input**, collapsing consecutive messages of a role for providers that need it, and records the `ModelEvent`.
8. **ORBIT's input filters**, below the event: the memory context appended, safety and vaccination prompts prepended, and boundary monitoring's redactions.

The provider therefore receives `[safety prompts] [agent prompt] [the seed's leading system messages] [collusion] [compromise] [the rest of the seed] [runtime attack messages] [pending injections] [notice] [memory context]`. That is ORBIT's order today ([Current behaviour](#orbit-104)), without legacy observations and dialogue projections, which ported runs do not use. A controller that passes its own preamble text instead would add it, as a user message, after the seed and before the runtime-attack messages.

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
    budget=Budget(derive=False),
)
```

wrapped in ORBIT's solver. Each member's model has the delivery layer, the filter layer (the compromise prompt filter, because a compromise attack exists) and the observer; the writer also has the tool gate on its tools, channel tools included through ORBIT's tool source, and the reader `dual_llm`'s quarantine wrapper on its tools, channel tools included, and the output check.

**Run.**

1. The controller registers the three callbacks on both members and starts both with the sample input. Each parks at boundary 0, held.
2. The pre-deployment attacks run: the compromise is due for `writer`; the injection is queued for `reader`.
3. Turn 0, step `[writer, reader]`, sequential:
   - `activate("writer")`. At boundary 0, `orbit:attack` inserts the compromise after the system message; `orbit:defense` does nothing (no `dual_llm` on the writer). The seam writes one `changed` event, origin `orbit:attack`, with an insertion at index 1. The writer's first request: `[agent prompt, compromise, input]`; no notice, since nothing is unread. It calls `channel_post`; the post is delivered unaddressed with recipients `[reader]`. Its next boundary (`after == "tools"`) ends the activation.
   - `activate("reader")`. `orbit:attack` appends `[Output from channel_read]: <payload>`; `orbit:defense` has `dual_llm` rewrite it, under the same id, from a quarantined model call charged to the reader. Two `changed` events: an append by `orbit:attack`, a replacement by `orbit:defense`. The reader's first request: `[agent prompt, input, quarantined summary, notice "team: 1 unread"]`. It calls `channel_read`, whose result `dual_llm`'s tool wrapper replaces with a summary, so, as ORBIT's code reads, the observer finds no exposure for that read. Its next boundary ends the activation.
4. Turn 1, step `[reader]`: the reader answers with a tool-free reply, reaching a boundary with `after == "reply"`.
5. The plan is exhausted; the controller returns `schedule_exhausted`. The stop ends both held members, `stopped`. Neither submitted, so the swarm's `final` is `no_submissions`.
6. ORBIT's solver wrapper sets `TaskState.output` to the reader's output from the record, the messages to the reader's saved conversation, and writes `orbit:answer` with `selected` and `member` both `"reader"`, `rule="output_agent"`, `submitted=false`, `swarm_final` with reason `no_submissions`, and `agrees=false`. The controller made no `choose_answer()` call, since the reader did not submit. The `match` scorer scores the reader's reply.

Had the writer called `channel_list`, the tool gate ORBIT's tool source put on it would have refused it with `ToolError("Blocked by defense '<name>': Tool 'channel_list' is blocked")`, recorded as a `defense_intervention`, and the writer's turn would have gone on to its boundary as any tool error does.

### Behaviour a port changes

Runs on the two runtimes should not be pooled unless these are accepted or controlled for; ORBIT records the runtime in each sample's metadata.

- **No resume prompt.** A held member never left its loop, so "Please continue with your next action." is never added.
- **Interventions and pre-turn defences apply at the target's first activation at or after they fall due**, not at the start of the turn for every member. A member's conversation does not change while it is parked, so they land in the same place; ORBIT's eligibility check counts pending injections for the member, as today they are counted after being appended.
- **Defence work is the member's.** `dual_llm`'s pre-turn quarantine calls run at the member's boundary, so they count against its token and cost limits and its ledger line, and as working time unless ORBIT marks them otherwise; today they run in the orchestrator, outside any agent.
- **Conversational members end `stopped`** at the swarm's stop, and the swarm's own answer is a submission only. ORBIT's answer record names what was scored.
- **Member time limits exclude held time** ([swarm-turn-seam.md Time](swarm-turn-seam.md#time)).
- **One span per member**, with `react()`'s own features (compaction, refusal retries, the agent channel) available; ORBIT's activations are its schedule events inside the span.
- **Unrecovered context overflow ends a member.** When `react()` cannot recover from `model_length` it returns, with no boundary after it (`src/inspect_ai/agent/_react.py:276-286`), and the swarm records the member `unsubmitted` (`src/inspect_swarm/_member.py:612-620`); `turn_react` ends only the activation, and ORBIT can activate the agent again (`ORBIT: orbit/agents/turn_react.py:254-262`). The port keeps react's ending (decision: Ransom, 2026-10-30).
- **Three refusals in a row end a member's loop.** `react()` stops after three consecutive outputs with `stop_reason="content_filter"` (`src/inspect_ai/agent/_react.py:290-297`), which ORBIT's output check returns each time it exhausts its resamples; `turn_react` has no such exit.
- **A rewrite at a boundary resets compaction**, and drops summaries made from changed history, where compaction is on.
- **Unsupported members** are refused: background-dispatching `deepagent()` and bridged members.

Everything else is unchanged; the [list of differences](#what-the-port-must-keep) names what ORBIT's tests check.

### What stays on ORBIT's executor

ORBIT's port refuses each of these with a `ValueError` naming the setting, and they keep running on ORBIT's executor:

- The `legacy/v1` profile and `turn_react` for it, with the `legacy` quantum and legacy channel formats.
- The `tool_step` quantum: refusing a batch of several tool calls before any runs needs a change to `react()`.
- The `summary` and `peer_messages` observation modes.
- Dialogue and activity projections. They could move as ORBIT code (an `orbit:dialogue` callback that inserts the peer's text at activation start, and publishing from the member's own boundary through ORBIT's channel), but a port does not need them first.
- The topology executor and persistent delegation sessions; a root agent with its delegates can be one member if ORBIT wants the ledger.
- Samples whose seed has no user message: an empty or system-only sample input with no group goal, even where a pending injection at boundary 0 would supply one. `start(preamble=False)` refuses such an input before the member starts ([S3](#s3-a-controllers-preamble)), which keeps the guard against releasing a member that has no user input; ORBIT's controller checks the seed first and raises its `ValueError` naming the sample and the agent.
- Scheduled agents with `tool` or `handoff` edges. A fresh delegate runs in its caller's member, so `calling_member()` names the caller, not the delegate's agent, and the delegate's channel tools, read cursors, sender and defended compromise would all need a delegate identity the swarm does not have ([Not this design](#not-this-design)).

## Alternatives considered

**ORBIT's own `on_continue` hook, with no seam.** ORBIT's member builder can give `react()` an `on_continue` that waits for the scheduler and returns a replaced state. It needs no swarm change. But it misses boundary 0 and every recovered turn, and the swarm reads a member held there as running: its status after release, its time limit, `swarm_wait` and the stop are all wrong ([swarm-turn-seam.md Why](swarm-turn-seam.md#why)). Rejected for the seam.

**A swarm-owned scheduled mode**: `activate()`, `parked`, `snapshot()`, staged `append()` and `edit()`, an `intervened` event and a `round_robin` controller in inspect_swarm. One implementation for every scheduled harness. But it puts ORBIT's policy into swarm API before a second user asks, and holders compose badly, where callbacks compose by order ([swarm-turn-seam.md Alternatives considered](swarm-turn-seam.md#alternatives-considered)). Rejected: each of these is a few lines of ORBIT's controller over the seam, and `changed` is the record.

**Bus extensions for ORBIT's channel**: a `recipients="roster"` channel class attribute so that addressed records reach pending and ended readers, and a public commit sequence. Rejected: an unaddressed record with recipients the channel computes reaches the same readers, and `apply()` already runs in commit order.

**Exposure reporting through the swarm** (`record_exposure()`, coverage declarations and kept notice texts on the bus). It would check that reported ids belong to the run and member and write the events in the swarm's format. But the swarm would only relay ORBIT's claims, which ORBIT's own events already carry, with ORBIT's store index beside them. Rejected: exposure stays ORBIT's events.

**Naming a read in model input**: stamping read results with the read id in metadata, from a callback or an inspect_ai `tool_result_metadata()` helper. Not needed: ORBIT's observer joins by the tool call id it captures at read time, as today.

**Choosing a member that did not submit as the swarm's answer** (`choose_answer(member, allow_unsubmitted=True)`). The swarm's `final` would then always name ORBIT's answer. But it contradicts the rule that only submissions are answers, which the record's readers rely on. Rejected for ORBIT's own record beside the swarm's.

**ORBIT removing or replacing the preamble at boundary 0**, from its own callback. Needs no swarm change, and the seam records the removal as a `changed` event. But it edits the conversation to undo what the swarm put there, costs a compaction reset, and serves only ORBIT; a controller choosing the preamble when it starts a member is simpler and serves any controller that does not run its members at the same time. Rejected (decision: Ransom, 2026-10-29).

**A preamble option on `member()` or `swarm()`** instead of `start()`. The eval author would set it. But whether members work at the same time is the controller's fact, not the member's or the roster's, and `start()` already takes the member's input. Rejected.

**Outputs saved by ORBIT's member wrapper**, or a member-output field brought forward for ORBIT alone. Works on top. But the swarm's answer and ORBIT's would come from two objects, which can differ for a member ended at a boundary or by a hard stop, and step 1 of the scoring design's Part 2 plan writes each member's output to the record anyway. Rejected: ORBIT reads the record's outputs with `swarm_result()`.

**Selecting `last_submitted` by the record's end times.** No ORBIT submit tool. But a member's end time is not its submit time: a member whose submit ran first can end after another while a second tool of its batch still runs, and the swarm would then score a different member from ORBIT's executor. Rejected for ORBIT's own submission order.

**ORBIT's channel off the bus**, keeping its own runtime and tools. Needs no S2. But the swarm's ledger and metrics would not see ORBIT's communication, and a send after the swarm began to stop would not be refused. Rejected as the recommended path; it remains possible.

**`Budget(reserve=0)` for ORBIT's limits.** No swarm change. But the swarm's cost cap is checked before the sample's, so a run that reaches the sample's cost limit ends as a swarm stop that returns normally, and later solvers run where ORBIT's executor ends the sample. Rejected for S4.

**Keeping one `run()` per activation on a stored `AgentState`.** Closest to ORBIT today. But it needs clean re-entry of `react()`, loses per-run state (attempts, compaction, MCP connections, the agent channel), and rebinds the member on every activation. Rejected.

**ORBIT's model defences as sentinel protocols, specified here.** It would drop the private `_resolve_config`, let filtered members use the response cache, and log the request the model received. But it depends on generate-stage features inspect_sentinel's design has deferred to its second part, and their shape is that design's to decide. Deferred: this design keeps the wrappers and lists what the move would need ([ORBIT's model defences stay wrappers](#orbits-model-defences-stay-wrappers); decision: Ransom, 2026-10-30).

## Compatibility and migration

- **inspect_swarm** is experimental throughout. S2 adds public names (`Draft`, `Delivery`, `Caller`, `calling_member`, `Bus`) and types `ChannelRun.bus` as the protocol; `shared_memory` moves onto them, writing the same events, so stored logs, the ledger and the metrics are unchanged. S3 adds a keyword argument to `start()`, and S4 a `Budget` field, each defaulting to today's behaviour. The seam's, the result record's and the member outputs' compatibility is their designs'.
- **inspect_ai**: I1 as the seam designs it, nothing else.
- **ORBIT logs.** Logs already written keep their readers. A ported run writes ORBIT's events as today, plus the swarm's communication and swarm events and ORBIT's answer record; ORBIT's store models and its viewer overview are unchanged.
- **Dependencies.** ORBIT declares its inspect_ai requirement in its packaging configuration (`ORBIT: pyproject.toml:16-19`). A port adds inspect_swarm, and so the inspect_ai inspect_swarm requires.
- **Python and concurrency.** Both require Python 3.11+. inspect_swarm runs on asyncio and trio, so ORBIT's controller and callbacks use anyio.

**Migration path.** Configurations move in one step; everything not moved keeps running on ORBIT's executor.

- **After the seam (S1, I1), S2, S3, S4, member outputs on the record and a `swarm_result()` that reads any literal output ([What ORBIT needs of the record's reader](#the-answer-and-a-record-of-the-answer-scored)): scheduled runs.** `model_step` and `completion` plans and the round-robin, superstep and interleaved modes move, for agents without delegation edges, with injections, compromise and pre-turn defences as callbacks, ORBIT's channel on the bus, and its solver wrapper choosing and recording the answer. ORBIT's model layers are unchanged. A configuration that is one concurrent `completion` step moves in the same step, not earlier: the member builder's continuation adapter keeps a `submit=False` member going after a tool-free reply ([Members](#members)), so without the seam's boundary after a tool-free reply such a member would continue past it (`src/inspect_ai/agent/_react.py:556-574`) until a limit ends it, where ORBIT's executor ends its `completion` there. One member builder serves every ported configuration.

## Security

- **Callbacks, the controller and the channel are trusted ORBIT code** in the runner process, as ORBIT's extensions are ("Registration and channel permissions are not a plugin sandbox", `ORBIT: docs/security-extensions.md:159-160`). None is reachable from a member's tools, and no model output decides a schedule (`plans.py:1-6`).
- **Peer text in a conversation.** The swarm's rules for peer text govern its own channels. ORBIT's experiments put attacker text in user and system roles on purpose (injections, compromise prompts, `auto` inboxes); that is ORBIT's experiment design. Every change at a boundary is a `changed` event with its origin and hashes; per-request text (notices, inboxes) is in the `ModelEvent` input.
- **Sender identity.** `deliver()` binds the sender from the calling member; ORBIT's channel cannot set it. `calling_member()` reads the same context, never model output.
- **Read evidence.** `record_read()` refuses a caller outside the run and a key that would overwrite one of the event's own fields, and writes only ids and the channel's fields; it carries no model text unless the channel puts some in `fields`, which ORBIT's does not.
- **The preamble** a controller passes is controller code's text, as `input` is; nothing a member wrote reaches it through the swarm. `start()` checks only its type and that it is not blank.
- **Members' outputs on the result record** are read by ORBIT's solver wrapper after the swarm; who can read the record, and when, is [swarm-result-record.md Security](swarm-result-record.md#security)'s.
- **ORBIT's run state** (final conversations, the submission order, read cutoffs) is never in the sample's store, so no member's tool, such as a store tool a model drives, can read a peer's private conversation or reorder submissions to change the answer scored. The submit tool records the member it was built for, never a name from its arguments, and runs behind the member's defence chain.
- **Untrusted input in ORBIT's code** (tool arguments to gates, channel payloads, model output to filters) is handled by ORBIT as today; the port moves none of it into the swarm.

## Testing

In inspect_swarm, with mockllm, on asyncio and trio, no network or Docker, with no ORBIT dependency:

- **The public bus API** (`tests/test_bus.py`):
  - a user channel written only with public names delivers a record, and its `sent` and `delivered` events match a built-in channel's;
  - `calling_member()` names the member from a tool in its own loop and from a nested loop's tool (`nested` true), and is `None` outside a swarm;
  - `record_read()` writes the `read` event in the tool span with the caller, `listing`, `records` and the channel's fields at the top level, returns increasing read ids, and raises `RuntimeError` outside a member and `ValueError` for an unopened channel and for a `fields` key that is one of the event's own;
  - an unaddressed record whose `apply()` names pending and ended members in `notify` and `result` is delivered while they are pending, held at boundary 0, and ended, and the `delivered` event carries them;
  - a counter incremented in `apply()` follows the order of the accepted `apply()` calls across concurrent deliveries, and has no gap where a delivery between them was refused by the rate control or by `admit()`, though that refusal consumed a record id;
  - `shared_memory`'s tests pass unchanged on the public names, and a run that views a file and lists the board has the same `read` events and the same `listings` and `reads` metrics as before.
- **The preamble** (`tests/test_controller.py`): `start()` with no `preamble` gives today's input and preamble; a string replaces the preamble with that text, for a member with `choose_name=True` too; `False` adds none; an empty or blank string and `False` with an input that has no user message raise `ValueError`, and `True` or a list `TypeError`, each leaving the member pending.
- **A budget that sets no caps** (`tests/test_budget.py`): under `Budget(derive=False)` and a sample with token, cost and time limits, `derive_caps()` gives no caps; a priced call over the sample's cost limit, and calls over its token limit, raise the sample's limit error out of the swarm, with no member `swarm_cap`, and a solver after the swarm does not run; an explicit cost cap under `derive=False` still stops the swarm at that cap.
- **ORBIT-shaped conformance** (`tests/test_orbit_shape.py`), on the seam:
  - **message order**: a `react(prompt=...)` member whose model is a test `Model` wrapper that appends a system notice, over a `ModelAPI` wrapper that prepends a safety system message and records what it passes on, started with `preamble=False`, with three callbacks at boundary 0 that insert a tagged system message after all the leading system messages, append a tagged user injection, and replace the injection under its id. Started with a goal, the first `ModelEvent` input is exactly `[system prompt, compromise, goal, rewritten injection, notice]`; started with a sample input `[system, user, assistant, user]`, it is exactly `[system prompt, the input's system message, compromise, user, assistant, user, rewritten injection, notice]`; in both the mock provider receives `[safety prompt, ...those]`. Started with a preamble string instead, the string is a user message after the input and nothing else changes. Each callback writes its own `changed` event;
  - a test controller that holds members in a boundary callback, as ORBIT's will, alternates two members by `model_step`, one generation each, with no continue prompt added; a concurrent step waits at its barrier; a `completion` activation of a `submit=False` member with `on_continue` returning its state runs to a tool-free reply and stops there, and one of a submitting member runs to submission;
  - **tools behind ORBIT's wrappers**: a member whose tools are a test `ToolSource` that wraps what `swarm_tools()` resolves, and whose submit tool, given as `AgentSubmit(tool=)`, is wrapped too, with a gate that denies the channel's list tool and the submit tool and a result wrapper on the channel's read tool: the list call returns the gate's `ToolError` and the channel's tool does not run, the denied submit leaves the member running and records no submission, and the read call returns the wrapper's result in place of the channel's;
  - a reader-and-writer test channel on the public API: messages do not activate members, a post reaches a reader still held at boundary 0, a step-start cutoff hides a post committed during the step, and capacity checked in `admit()` lets exactly one of two concurrent posts take the last place;
  - a member error cancels its sibling and the controller and propagates;

In ORBIT, by its port, with its own test setup (mockllm, no network):

- **Behaviour kept.** ORBIT's scheduler, topology and communication suites run on both executors: `tests/test_agent_scheduler.py`, `test_schedule_plans.py`, `test_scheduler.py`, `test_attack_outcomes.py`, `test_topologies.py`, `test_common_runtime.py`, `test_configured_communication.py`, `test_communication_boundaries.py`, `test_communication_contract.py` and `test_message_ordering.py`, and the scheduled attack and defence runs in `test_dual_llm_scheduled_e2e.py`, `test_compromised_e2e.py` and `test_collusion_mockllm_e2e.py`. The round-robin, superstep and interleaved modes run under `model_step` and `completion`. Cases of `turn_react` itself, of the `legacy` and `tool_step` quanta, and of scheduled delegation edges stay on ORBIT's executor.
- **The differences not yet pinned.** Each line of [What the port must keep](#what-the-port-must-keep) that the suites above do not cover gets a test on ORBIT's executor first, then runs on both.
- **Port only.** A peer's store tool finds no member's final conversation and cannot change the submission order or the answer scored; the message order of [The message order before the first model call](#the-message-order-before-the-first-model-call) with ORBIT's real wrappers and attacks; the answer record names the output scored after a normal stop and after a sample time, token or message limit, and is not provisional after a long-input, short-goal submission; after an empty submission it has `member` `None` and `agrees` false; a failure in the wrapper's finalization after a sample limit ended the swarm leaves that limit's error propagating, with the failure as its note; a member whose loop ends on unrecovered `model_length` ends `unsubmitted` and is not activated again; and a configuration with a `tool` or `handoff` edge, a `legacy` or `tool_step` quantum, or a legacy observation mode is refused with a `ValueError` naming it, and so is a sample whose seed has no user message.

## Implementation plan

inspect_swarm:

1. **The public bus API (S2).** `src/inspect_swarm/_bus/_record.py` and `_bus/_bus.py` (`Draft`, `Delivery`, `Caller`, `calling_member()`, `record_read()`, the `Bus` protocol), `src/inspect_swarm/_channel/_channel.py` (`ChannelRun.bus`), `src/inspect_swarm/_evidence.py` (`comm_read()` taking the channel's fields and `records`, behind `record_read()`), `src/inspect_swarm/_channel/shared_memory.py` (the public names), `src/inspect_swarm/__init__.py`; `docs/channels.qmd` (writing a channel with records); `tests/test_bus.py`.
2. **A controller's preamble (S3).** `src/inspect_swarm/_controller/_control.py` (`SwarmControl.start()` and its docstring, `SwarmRun.start()`); `docs/controllers.qmd` (`start()`'s arguments); `tests/test_controller.py`. Independent of step 1.
3. **A budget that sets no caps (S4).** `src/inspect_swarm/_budget.py` (`Budget.derive`, `derive_caps()`); `docs/budget.qmd` (the field); `tests/test_budget.py`. Independent of steps 1 and 2.
4. **ORBIT-shaped conformance tests.** `tests/test_orbit_shape.py`. Needs the seam ([swarm-turn-seam.md Implementation plan](swarm-turn-seam.md#implementation-plan)) and steps 1 and 2.

inspect_ai: I1 is step 1 of the seam's plan. The port itself is ORBIT's work, by the migration path, with ORBIT's tests in [Testing](#testing) on both executors.

## Open questions

None.

## Not this design

- **Literal output that reads as a reference, in the result record.** [swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result) refuses it; the port needs it read ([The answer](#the-answer-and-a-record-of-the-answer-scored)). The change is that design's: for example, the writer could mark which strings are a member's literal text, so the reader refuses only an unmarked reference it cannot resolve.
- **Other designs' references to the earlier version of this one**: swarm-result-record.md's ORBIT wrapper around `as_solver(swarm(...))` and its member wrapper saving conversations to an ORBIT store key; swarm.md's ORBIT revision and version and its turn-hook item; swarm-api.md's ORBIT rows (`ActivationPolicy`, `round_robin`, `member(input=)`, routes); swarm-implementation-plan.md's ORBIT future work and its note on `tool_result_metadata()`; swarm-event-types.md's list of ORBIT event kinds; and the links into this design's earlier sections and approach from swarm-turn-seam.md (its Why and Non-goals, and "What this means for swarm-orbit.md"), swarm-communication.md (tool results, exposure as swarm evidence, metadata on read results) and swarm-memory-channel.md (`read` evidence). To be aligned in a follow-up.
- **`tool_step` on `react()`**, and replacing a member's agent mid-sample.
- **Scheduling a member's background descendants**, which the seam refuses.
- **Conversation identity for descendants**, which ORBIT's persistent delegation tracks.
- **A board channel with readers and writers in inspect_swarm**, which could take ORBIT's names when it is built.
- **ORBIT's other Inspect gaps** that a port does not need: a public effective-config accessor, `active_model`, a handoff predicate, `as_tool()` keeping `model_input`, and the interrupted `ToolEvent`'s null fields.
- **A controller reading the swarm's preamble** to extend it rather than replace it (a `preamble` property on the member handle). No user needs it: ORBIT sends none.
- **A possible ORBIT bug**, for the reply to the ORBIT team: `memory_poisoning` queues a pending-injection entry without `applied` (`ORBIT: orbit/attacks/injection/memory_poisoning.py:104-112`), and the pending-injection step does not skip it (`orchestrator.py:939-952`), so as read it is also broadcast as `[Output from unknown]: ...`. Not run.
