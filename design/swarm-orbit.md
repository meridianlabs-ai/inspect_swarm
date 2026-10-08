# ORBIT on inspect_swarm

Status: proposed, 2026-10-08. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

A deep dive on one topic of [swarm.md](swarm.md): how ORBIT, the Inspect-based multi-agent security framework compared in [Relationship to ORBIT](swarm.md#relationship-to-orbit), could run on inspect_swarm. It maps ORBIT's concepts onto inspect_swarm's members, substrate, controller, bus and observer; says what inspect_swarm must provide so that ORBIT runs without forking `react()` or wrapping `Model`; says what a port of ORBIT changes; and gives a migration path and what stays ORBIT-specific.

Three sibling deep dives run in parallel and are referenced, not designed, here: limits, scoring, and inter-agent communication. Ransom's decisions of 2026-10-07 in swarm.md stand: Python 3.11+; inspect_ai internals may be used; limits stay soft; M1, then M2, then the rest in any order; a shared sandbox by default; monitoring through inspect_sentinel; red-team features optional; peer messages are model output, delivered as tool output with distinct provenance and never as user messages; the transcript representation is open.

Code references are to ORBIT at `588b3035` (the revision swarm.md cites), inspect_ai at `aa20052a`, and inspect_sentinel at `0f9b5c71`. Paths are relative to each repository's root; ORBIT paths are prefixed `ORBIT:`.

## Why

swarm.md recommends that inspect_swarm "align with ORBIT and aim to be a substrate it could run on, not to absorb it", and, as merged in #1, listed in one line what it should provide: turn boundaries and a scheduler, per-member tool and model wrapping, and a communication hook that does not wrap `Model`. That line is a claim, not a design. This document checks it against ORBIT's code and makes it concrete.

ORBIT pays for running outside such a substrate:

- **A forked `react()`.** `turn_react` (`ORBIT: orbit/agents/turn_react.py`) exists so that a scheduler can stop an agent after one quantum and call it again later. Its signature (`:90-104`) has no `compaction`, `retry_refusals`, `approval` or `review`, and its loop opens no agent channel and no checkpointer, all of which inspect_ai's `react()` has (`src/inspect_ai/agent/_react.py:56-72`, `:213-237`). Every `react()` improvement has to be ported by hand or is lost. It also imports private inspect_ai modules (`turn_react.py:31`, `:50`, `:66-67`).
- **Re-entry on a stored conversation.** Each activation is a fresh `run(agent, state)` on the agent's stored `AgentState` (`ORBIT: orbit/execution/agent_scheduler.py:588`). The fork therefore guards against re-inserting the system prompt (`turn_react.py:469-490`) and appends a user message "Please continue with your next action." when resuming after a tool result (`:225-232`).
- **Three `Model` wrappers per agent** (`ORBIT: orbit/solvers/orchestrator.py:209-232`): one that adds unread notices to each request (`ORBIT: orbit/communication/delivery.py:308-352`), one that observes which messages reached a successful model input (`ORBIT: orbit/communication/model_observer.py:24-157`), and one for defense filters, which refuses Inspect's response cache because the cache lookup sits above the filters (`ORBIT: orbit/defenses/model_filter.py:214-290`). The wrappers must reproduce model identity and effective configuration, which needs the private `Model._resolve_config` (`ORBIT: orbit/_model_config.py:8-17`).
- **Its own concurrency.** Concurrent plan steps use raw asyncio (`agent_scheduler.py:352-377`), where Inspect code is expected to run on anyio.

For inspect_swarm, ORBIT's users are user group 2 in swarm.md (multi-agent safety and security evals). If ORBIT can run on inspect_swarm, security experiments and capability-scaling experiments share one runtime, one realized-cost ledger and one evidence model, and ORBIT stops maintaining a `react()` fork.

## Goals and non-goals

### Goals

- Map each ORBIT concept named in the task (roster and roles, channels with reader and writer lists, scheduled activation, delivery modes, evidence, output member, compromised members, model wrapping) to an inspect_swarm component, or say that it has none.
- List what inspect_swarm must provide for ORBIT to run with neither `turn_react` nor `Model` wrappers, with each item placed in swarm.md's milestones.
- List what a port of ORBIT would change, and what stays ORBIT-specific.
- Give a migration path in which each step is usable on its own.

### Non-goals

- Designing the sibling topics: limits and the ledger, scoring and the result contract, and the inter-agent communication details (binder, notice rendering, read tools). Where this document needs something from them it states the requirement and links.
- Porting ORBIT's scenarios, attacks, defenses or scorers, or its `legacy/v1` runtime profile.
- Building red-team features in inspect_swarm. They stay optional and unscheduled (decision: Ransom, 2026-10-07); this design only keeps the extension points they would use.
- Offering ORBIT's `auto` delivery mode or its dialogue and activity projections, or any path by which peer text reaches a member outside a tool result.
- Deciding whether or when ORBIT adopts inspect_swarm.

## Current behaviour

### ORBIT

**Roster and roles.** `AgentSpec` (`ORBIT: orbit/configs/setup.py:79-164`) has a name, an informational `role` label, an optional model and temperature, a system prompt, tool names, `is_compromised`, a per-agent `max_messages`, and `submit` (whether the agent has a terminal submit tool). `max_messages` is an eligibility check: an agent whose conversation has reached it is skipped before an activation starts, and an activation that starts runs to its quantum's end (`agent_scheduler.py:539-551`). A `submit=False` agent stays in the rotation after a tool-free reply (`ORBIT: orbit/agents/turn_react.py:422-453`). `AgentGroup` gives several agents a shared goal that replaces the sample input as their first user message (`ORBIT: orbit/configs/execution.py:22-40`, `agent_scheduler.py:243-254`).

**Invocation and routes are separate.** `TopologyEdge` says how one agent invokes another (`handoff`, `tool` via `as_tool`, or `direct_run`; `setup.py:230-256`). `CommunicationEdge` says who may message whom, intersected with channel membership (`setup.py:223-228`, `:273-278`). Invocation edges never grant message permission.

**Channels.** `ChannelConfig` has an id, `readers`, `writers` and a `private` label (`ORBIT: orbit/configs/communication.py:12-29`). The tools are `channel_post`, `channel_read` (cursor and bounded pages) and `channel_list` (`ORBIT: orbit/communication/tools.py:88-202`). The runtime binds the sender from a ContextVar identity, so a model cannot choose it (`ORBIT: orbit/communication/runtime.py:51-124`). A send with no permitted recipient fails (`runtime.py:246-252`); an oversized send becomes a `ToolError` for the sender (`tools.py:128-129`). State lives in Inspect store models (`ORBIT: orbit/communication/state.py:120-155`). Attacks register further channels at run time, such as secret coalition channels (`orchestrator.py:240-268`).

**Delivery modes.** `CommunicationConfig.delivery` is `poll`, `notify` (the default) or `auto`, overridable per agent (`communication.py:54-75`). For `notify` and `auto` the outermost `Model` wrapper derives one `ChatMessageSystem` per request, appended after the conversation and never stored in it (`delivery.py:203-224`, `:234-283`, `:330-343`). A `notify` notice lists channel ids and unread counts and "contains no message text"; an `auto` message carries bodies under an "untrusted messages" header. Dialogue projections, a separate adapter, insert a peer's completed text into the recipient's conversation as a user turn (`ORBIT: orbit/communication/dialogue.py:1-8`, `:193`). The legacy `peer_messages` and `summary` observation modes also inject user messages (`agent_scheduler.py:685-803`).

**Scheduled activation.** `ExecutionConfig` (`execution.py:109-207`) selects round-robin, superstep or interleaved modes, or an explicit plan (`ORBIT: orbit/configs/scheduling.py`, `ORBIT: orbit/execution/plans.py`). A plan step lists agents, runs them in order or concurrently with a barrier, and may set its own quantum and visibility. Quanta are `legacy`, `model_step` (one generation and its tool batch), `tool_step` (one tool call; a batch of several is rejected before any runs) and `completion` (`scheduling.py:13`, `turn_react.py:462-467`). For a `submit=False` agent, `completion` ends at a tool-free reply and the agent can be activated again; for a submitting agent it ends at submission. The public task helper defaults to scheduled `model_step` execution (`ORBIT: docs/architecture.md`, "Executors and conversations"), and the `common/v1` profile uses `completion` for interleaved conversations (`ORBIT: orbit/scenarios/runtime_profiles.py:80-85`). A trusted `after_activation` callback may amend pending steps with an audited revision (`plans.py:222-267`). Messages never activate an agent: "Sending never changes the schedule or wakes a submitted agent" (`ORBIT: docs/runtime-architecture.md`, "Channels and visibility"). `visibility="step_start"` freezes channel reads at the step's start sequence (`runtime.py:36-48`, `:292-297`).

**The outer loop.** `ExperimentScheduler.run_loop` repeats turns until a halt condition: `max_turns`, wall-clock `max_time_seconds`, convergence of outputs, attack success, or custom conditions (`ORBIT: orbit/scheduler/scheduler.py:164-245`, `ORBIT: orbit/configs/scheduler.py`). Each turn activates runtime attacks, applies pending injections as user messages tagged `untrusted_observation` (`orchestrator.py:834-849`), runs pre-turn defense hooks over every agent's `AgentState` (`:851-861`; `ORBIT: orbit/defenses/dual_llm.py:295-330` rewrites messages there), runs the executor, and evaluates attack outcomes (`:884-893`).

**Compromised members.** `CompromisedAgentAttack` either appends the attacker's payload as a system message (`inject_prompt`) or replaces the agent callable with a `react()` built from the payload and the original tools (`replace_agent`) (`ORBIT: orbit/attacks/compromised/compromised.py:35-75`). Either can happen before deployment or at a `specific_turn` (`:110-123`); a replacement takes effect at the agent's next activation, which calls the new callable (`agent_scheduler.py:536`).

**Defenses.** Tool gates wrap each tool and run `on_tool_call` before the callable, turning a denial into a `ToolError` (`ORBIT: orbit/defenses/boundaries.py:191-294`). Input filters (safety prompts, memory injection, input monitoring with redaction) run in a `ModelAPI` wrapper, and output checks with resampling in a `Model` wrapper (`model_filter.py:127-212`, `boundaries.py:141-185`, `:296-343`).

**Evidence.** Kinds are `sent`, `read`, `notified`, `delivered` (content inserted into a conversation) and `model_exposure` (original content survived into a successful generation's input) (`state.py:57-83`, `ORBIT: docs/communication.md`). Each is an `InfoEvent` with source `orbit.communication` (`runtime.py:190-227`) and carries agent, session, invocation, channel, sequence and Inspect span, tool-call and event ids. Exposure is matched by the read's tool-call id and rendered text (`runtime.py:330-399`).

**Output member.** `ExecutionConfig.output_agent` picks whose latest output becomes `TaskState.output`, whether or not that agent submitted ("Answer selection does not assert that this agent submitted/succeeded", `agent_scheduler.py:861-869`); `history_projection` picks which conversations are copied into `TaskState.messages` for scoring (`:813-860`).

**Consumers of the evidence.** The collusion scorer reads private channel policies, canonical messages, read receipts with their per-message rendered fragments, observation-enabled sessions and `model_exposure` events from `CommunicationState`, and reports missing coverage as unknown rather than zero (`ORBIT: orbit/scorers/collusion_scorer.py:78-130`). The viewer overview reads the same store keys and the `InfoEvent`s whose source is `orbit.communication` (`ORBIT: orbit/viewer/_render.py:135-150`, `:293-300`).

### inspect_ai seams this design uses

- **`react()`'s `on_continue`.** It is awaited after every completed turn (`src/inspect_ai/agent/_react.py:366-392`), except a turn that submitted (`:324-335`), a turn whose context overflow was recovered (`:271-276`), and a turn interrupted through the agent channel (`:359-364`). It may return `True`, a string (appended as a user message), `False` (ends the loop), or an `AgentState`, whose messages replace the conversation with no message added (`:388-390`). Without an `on_continue`, a `submit=False` `react()` ends at its first tool-free reply (`:553-554`). `deepagent()` already composes its own notices into the caller's `on_continue` this way (`src/inspect_ai/agent/_deepagent/deepagent.py:222-237`, `lifecycle_tools.py:533-578`).
- **The agent channel cannot hold a turn.** `subscribe_turn_state` callbacks are synchronous observers (`src/inspect_ai/agent/_channel/channel.py:293-324`). `before_turn` blocks only when the conversation has no user message yet (`:432-453`).
- **`on_before_model_generate` runs while holding a connection slot.** The slot is taken at `src/inspect_ai/model/_model.py:1058`; the hook is awaited inside the attempt at `:1505`, before the cache lookup (`:1528-1537`). A hook that waited there would pin a connection slot for as long as it waited. Hooks are also process-wide, not per sample.
- **Transcript subscriptions.** A sample's transcript notifies subscribers when an event is added and again when it is updated, so a completed `ModelEvent` is seen with its final `input`, `error` and `cache` fields (`src/inspect_ai/log/_transcript.py:689`, `:692-737`, `:977-988`; `src/inspect_ai/event/_model.py:102`, `:126`, `:135`). An exception in a subscriber is logged and swallowed (`_transcript.py:751-766`).
- **What a `ModelEvent` records.** The event's `input` is recorded before `ModelAPI.generate` is called (`src/inspect_ai/model/_model.py:1577`, `:1636`). A `ModelAPI` wrapper that rewrites the request, as ORBIT's defense filters do, changes what the provider receives but not the event.
- **Message limits** are checked before every generation and on every append to a limited conversation (`src/inspect_ai/model/_model.py:1025-1028`, `src/inspect_ai/util/_limited_conversation.py:19-30`).
- **Approvers run with token and turn limits suspended**, so a judge's inference in an approval policy is not charged to those limits (`src/inspect_ai/approval/_apply.py:49-57`).

### Spike

I ran a throwaway solver on the worktree's inspect_ai (`fccfb298e`, whose `react()`, channel, `run()` and `ModelEvent` are unchanged from `aa20052a`), with three `react()` members scripted by mockllm to call a tool twice and then submit:

- Each member's `on_continue` signalled "yielded" and then waited on an anyio event; a round-robin loop opened one member at a time. Tool calls ran in the order `a1 b1 c1 a2 b2 c2`. Each member's final conversation had one system message, one user message and no continue prompts.
- Opening all three gates and waiting for all three yields (a concurrent step with a barrier) also worked.
- Cancelling the task group while members were parked ended the sample cleanly with status `success`.
- A transcript subscription saw each tool result in the member's next completed `ModelEvent` input, which is ORBIT's exposure evidence, with no `Model` wrapper.
- A `submit=False` `react()` member whose `on_continue` parked at a tool-free reply and resumed by returning its own `AgentState` gave two activations with two replies; the second generation's input was the system message, the user input and the first reply, with no continue prompt added.

So ORBIT's `model_step` scheduling, and its repeatable conversational `completion` activations, work on unmodified `react()` members that stay inside one `react()` invocation for the whole sample.

## Design

### Concept map

| ORBIT | inspect_swarm | Notes |
|---|---|---|
| `AgentSpec` (name, role, model, prompt, tools) | `member()` record (name, role, `Agent`, model, tools, limits) | ORBIT builds each `Agent` itself and passes it in. |
| `AgentSpec.max_messages` | an eligibility check in ORBIT's policy, against the member's [snapshot](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item) | Not Inspect's `message_limit`, which stops before a generation, not before an activation. |
| `AgentGroup.goal` | per-member initial input (new, [P1](#p1-member-records-m1)) | |
| `AgentSpec.submit=False` | `swarm_continue(conversational=True)` ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | A tool-free reply ends the activation, not the member. |
| `is_compromised`, pre-deployment compromise | roster data built by ORBIT before `swarm()` | No compromise feature in inspect_swarm. |
| Timed `inject_prompt` compromise | an `append` intervention at a boundary ([P5](#p5-trusted-interventions-later-with-p4)) | Eval-authored input; its role is [open question 1](#open-questions). |
| Timed `replace_agent` compromise | none | Stays on ORBIT's executor. |
| Invocation edges, persistent delegation sessions | inside a member's own `Agent` (`as_tool`, `handoff`) | ORBIT-specific; not swarm members. |
| `communication_edges` | the bus's route policy ([P2](#p2-bus-extension-points-m2)) | |
| `ChannelConfig` readers and writers | a channel kind on the bus's public extension point ([P2](#p2-bus-extension-points-m2)) | Later, swarm.md's board channels take `readers` and `writers` with these names. |
| `poll`, `notify` | swarm.md's `poll`, `notify` | ORBIT's notice is derived per request; inspect_swarm's is added at a turn boundary ([Behaviour a port changes](#behaviour-a-port-changes)). |
| `auto`, dialogue and activity projections, `peer_messages`, `summary` | none | They put peer text in a conversation outside a tool result; they stay on ORBIT's executor. |
| Scheduler modes, plans, quanta | an activation policy over gated members ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | `model_step` and `completion` only. |
| `visibility="step_start"` | a read cutoff carried by the activation ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | |
| `max_turns`, halt conditions, hooks | the policy's own loop; extensible stop reasons ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | |
| `sent`, `read`, `model_exposure` | `sent`, `read`, `exposed` ([P3](#p3-evidence-and-exposure-m2)) | |
| `notified` (a notice reached a successful input) | `exposed` with subject `notice` ([P3](#p3-evidence-and-exposure-m2)) | inspect_swarm's `notified` means the notice was added to the conversation. |
| ORBIT `delivered` (inserted into a conversation) | none on the swarm runtime | Only `auto` and dialogue projections produce it, and both stay on ORBIT's executor. |
| `output_agent` (latest output, submitted or not) | the policy's `set_output()` from the member's snapshot ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | swarm.md's `reporter` selects a submission, which is narrower. |
| Tool gates (`on_tool_call`) | per-member `react(approval=...)` policies today; sentinel `BeforeToolCall` protocols when sentinel's dispatcher is on inspect_ai `main` | swarm.md's monitoring decision. Judge usage is no longer charged to member token and turn limits ([Behaviour a port changes](#behaviour-a-port-changes)). |
| Input filters, output checks, resampling | sentinel's `BeforeGenerate`, `AfterGenerate` and `resample()`, designed but not built (`inspect_sentinel: design/sentinel-overview.md`, "Stages", "Protocols") | Until they exist ORBIT keeps its filter wrapper and its post-filter observer ([P3](#p3-evidence-and-exposure-m2)). |
| Notice `Model` wrapper | notices at turn boundaries | Removed for ported runs. |
| Exposure `Model`/`ModelAPI` wrapper | a transcript observer, or ORBIT's own observer reporting through `record_exposure()` where defense filters remain ([P3](#p3-evidence-and-exposure-m2)) | |
| Shared sandbox ("Neither channels nor agent groups isolate shared files or sandboxes") | swarm.md's shared-sandbox default | Same. |

### What inspect_swarm provides

Five items. P1 to P3 fall inside M1 and M2 as small additions; P4 and P5 are the "optional scheduled (turn-based) execution mode" in swarm.md's later work, which needs M2's bus only for its read cutoff.

#### P1. Member records (M1)

- `member(agent, *, name, role=None, input=None, limits=...)`. `input` (a string or message list) replaces the sample input as this member's first messages, for ORBIT's group goals. `role` is a free label recorded in evidence and member timelines, as ORBIT's role is informational.
- `final="reporter"` takes a member's name and selects that member's submission, as swarm.md defines it. ORBIT's `output_agent` is broader (the latest output, submitted or not), so it maps to P4's `set_output()`, not to `reporter`.
- The scoring deep dive decides how the swarm's output and each member's final conversation reach the task's scorer. ORBIT needs both an output taken from a member that never submitted and every member's final conversation for `history_projection`; P4's snapshots are the surface this design offers for them.

#### P2. Bus extension points (M2)

swarm.md already says that every explicit channel is a set of tools plus state that hands records to `deliver()`. For ORBIT that has to be a public extension point, because ORBIT's reader and writer channels, its secret coalition channels and its blackboards are its own channel kinds. The inter-agent communication deep dive owns the shapes; ORBIT needs these properties of them:

- **A public record and `deliver()`.** A record carries `channel` (an id the channel kind chooses), `recipients`, `kind` and `payload`. The bus assigns a `message_id` and a sample-wide, increasing `sequence`, and returns them with the recipients that survived the route check; ORBIT's canonical messages, cursors and step-start snapshots need all three.
- **The sender is stamped, never passed.** `deliver()` takes the sender from the calling member's context, the way ORBIT's `actor_identity()` checks a tool's bound agent against the active one (`runtime.py:117-124`). Called outside a member, it refuses, unless the caller is trusted code that names an origin (the path swarm.md reserves for optional red-team records).
- **Routes.** `swarm(routes=...)` takes directed `(sender, recipient)` pairs, or `None` for all pairs, and the bus drops recipients the routes forbid before the monitor runs. A record left with no recipient is refused to the sender as a tool error, as in ORBIT. Channel membership is the channel kind's check: it computes recipients from its readers and refuses senders that are not writers.
- **Per-message read receipts.** `record_read(fragments: Mapping[str, str])`, called inside a read tool before it returns, takes each returned message's id and the exact text the tool renders for it. The rendering contract, which every channel kind implements, is ORBIT's: each fragment appears in the tool's result text as its own newline-delimited block, so that `"\n" + fragment + "\n"` is a substring of `"\n" + result + "\n"` (`ORBIT: orbit/communication/runtime.py:373-377`). The helper resolves the active tool call (its id, function name and Inspect event id) from the member's pending `ToolEvent`, as ORBIT's `capture_tool_reference()` does (`ORBIT: orbit/execution/provenance.py`), and writes one `read` record. The record stores each fragment's SHA-256 and length; the fragments themselves are kept in the observer's memory for the rest of the sample and are never written to the store. A `max_sequence` argument implements step-start visibility.
- **Notice contributions.** A channel kind reports each member's unread count per channel id so that the M2 notice can list them. The notice rule in swarm.md already allows channel ids the task or controller assigned and excludes names a member chose; ORBIT's configured channel ids satisfy it.

Size caps and back-pressure are swarm.md's storm controls already, and match ORBIT's `ChannelCapacityError`-to-`ToolError` behaviour.

#### P3. Evidence and exposure (M2)

**Kinds.** inspect_swarm's evidence kinds become `sent`, `delivered`, `read`, `notified` and `exposed`, and [P5](#p5-trusted-interventions-later-with-p4) adds `intervened`. Against ORBIT's:

| ORBIT | inspect_swarm | Meaning in inspect_swarm |
|---|---|---|
| `sent` | `sent` | The bus accepted the record (after policy). |
| (none) | `delivered` | The record was enqueued for a recipient. ORBIT stores one message that recipients read. |
| `read` | `read` | A read tool returned the message, with its fragment hash. |
| (none) | `notified` | A notice was added to the member's conversation. |
| `notified` | `exposed`, subject `notice` | The notice was in the input of a successful generation. |
| `model_exposure` | `exposed`, subject `message` | The message's fragment was in the input of a successful generation. |
| `delivered` | (none) | ORBIT's `auto` and dialogue insertion; not on the swarm runtime. |

Records carry the fields ORBIT's evidence carries, renamed: member (ORBIT's agent), the member's conversation id (ORBIT's session; sentinel's `conversation`), the activation id under a policy (ORBIT's invocation), channel, `message_id`, `sequence`, and the Inspect span, tool-call, message and model-event ids. An `exposed` record links to the `read` or `notified` record it confirms, as ORBIT's `parent_event_id` does.

**The transcript observer.** At sample start, before any member starts, the swarm subscribes to the transcript. When a `ModelEvent` inside a member's span completes with no error, the observer:

- for each `read` receipt of that member and conversation, finds the `ChatMessageTool` in the event's `input` with the receipt's tool-call id; skips it unless its function matches the receipt's and its `error` is `None`; and then checks each fragment separately with the newline-delimited substring rule. Each surviving fragment gets one `exposed` record the first time it is seen for that message, member and conversation. A read whose result was truncated or rewritten so that only some fragments survive exposes only those;
- for each notice of that member, writes `exposed` with subject `notice` the first time the notice's message id is in the input;
- marks an event with `cache == "read"` as a replayed generation, as ORBIT marks `source="inspect_cache"`.

**Coverage.** A consumer can only treat a missing `exposed` record as "not exposed" when coverage is complete, so every member carries a coverage value in the swarm's evidence:

- `complete`: the observer was subscribed before the member started and has not failed.
- `failed`: the observer raised. Inspect logs and swallows subscriber exceptions (`_transcript.py:751-766`), so the observer catches its own exceptions, records an error `InfoEvent`, and marks every member it covers `failed` for the rest of the sample.
- `external`: the member's exposure is reported by the eval's own observer (below); complete only if that observer says it was installed for the whole run.
- `none`: no observation (for example a bridged member).

Anything but `complete`, or an external observer that declared itself complete, means exposure is unknown, never zero.

**Where the transcript cannot see what the provider received.** A `ModelEvent` records its input before `ModelAPI.generate` is called (`_model.py:1577`, `:1636`). ORBIT's defense input filters rewrite the request inside a `ModelAPI` wrapper (`ORBIT: orbit/defenses/model_filter.py:161-212`), and ORBIT's own exposure observer sits inside them and snapshots the filtered request (`model_observer.py:42-95`). So for a member with such filters, the transcript would show a redacted peer message as if it had reached the model. Such a member is declared with `member(..., exposure="external")`: the transcript observer skips it, and the eval's own post-filter observer calls `record_exposure(...)`, which applies the same receipts, fragment rule and first-seen rule and writes the same records. ORBIT keeps its `CommunicationModelAPI` for exactly as long as it keeps its defense filters. When those filters become sentinel `BeforeGenerate` protocols, which are designed to run before the cache lookup, the rewritten request should be what Inspect records (the event is recorded after the cache lookup, `_model.py:1528-1577`); that has to be confirmed when sentinel's generate stages are built, before the external observer is dropped.

The subscription is a private API: `Transcript._subscribe`. inspect_swarm may use internals (decision: Ransom, 2026-10-07).

#### P4. The turn seam and activation policies (scheduled-mode item)

**The seam.** `swarm_continue(on_continue=None, *, conversational=False) -> AgentContinue` returns an `on_continue` that wraps the caller's own:

1. Call the wrapped `on_continue` if there is one; if it returns `False`, return `False`: the member ends as it would alone. With none, apply `react()`'s default, except that under a policy a conversational member's tool-free reply is a boundary (step 2), not the end that the native default makes it.
2. Decide whether this is an activation boundary. Under an activation policy it is one after every turn when the activation's quantum is `model_step`; and, for a `conversational=True` member, after a tool-free reply whatever the quantum. With no policy, or quantum `completion` for a member that is not conversational, it is not.
3. At a boundary, take a [snapshot](#member-handles-snapshots-and-output), signal "yielded" to the policy, and wait until the policy opens the member's next activation. Cancellation (drain) ends the wait. On resume, return the member's committed conversation (with any [P5](#p5-trusted-interventions-later-with-p4) interventions and any M2 notice appended) as an `AgentState`, so `react()` adds no continue prompt.
4. Not at a boundary, append any pending M2 notice by returning an `AgentState` composed with the wrapped result, as `deepagent()`'s `background_on_continue` composes its notices; with nothing pending, return the wrapped result unchanged.

`conversational=True` is for ORBIT's `submit=False` agents. Without a policy it changes nothing: the wrapped default still ends the member at a tool-free reply (`_react.py:553-554`). Under a policy, a tool-free reply ends the activation and the member stays available, which is what ORBIT's repeatable `completion` activations do; the spike's second case is this path.

The member is found from a ContextVar the swarm sets when it starts the member's task. A nested agent that also uses `swarm_continue()` passes through, because the hook acts only in the loop whose agent span is the member's own.

Because the member stays inside one `react()` invocation, its conversation, attempt count, compaction state, MCP connections and agent channel persist across activations, and nothing re-enters `react()`. The member's first activation is the start of its `run()`; it ends at submission, at the wrapped `on_continue` returning `False`, at a limit, or at drain. `deepagent()` accepts `on_continue` and passes it to its `react()`, so deepagent members take the seam the same way. Bridged members and any other agent without the seam get one activation of quantum `completion`.

ORBIT's quanta map as follows:

- `model_step`: one generation and its tool batch.
- `completion`: until submission for a submitting member; until a tool-free reply for a conversational one.
- `tool_step` and `legacy` have no equivalent and stay on ORBIT's own executor ([What stays ORBIT-specific](#what-stays-orbit-specific)).

**Policies.** An activation policy is trusted Python that decides which members run and when:

```python
class ActivationPolicy(Protocol):
    async def __call__(self, swarm: SwarmControl) -> StopReason | None: ...


class SwarmControl(Protocol):
    members: Mapping[str, MemberHandle]
    def sequence(self) -> int: ...                       # current bus sequence, for step-start cutoffs
    def set_output(self, member: str) -> None: ...       # the member's latest snapshot output becomes the swarm's output
    def stop(self, reason: StopReason) -> None: ...


class MemberHandle(Protocol):
    name: str
    role: str | None
    status: Literal["pending", "parked", "running", "done", "limit", "errored", "cancelled"]
    def snapshot(self) -> MemberSnapshot: ...            # detached deep copy; any status
    async def append(self, messages: Sequence[ChatMessage], *, origin: str) -> None: ...      # P5
    async def edit(self, transform: MessageTransform, *, origin: str) -> None: ...            # P5
    async def activate(
        self,
        *,
        quantum: Literal["model_step", "completion"] = "model_step",
        read_cutoff: int | None = None,                  # step-start visibility
    ) -> ActivationResult: ...                           # returns at the next boundary or when the member ends
```

`ActivationResult` carries the activation id, the member's status afterwards, the message counts before and after, whether the member submitted, the duration, and the snapshot taken at the boundary. Every activation start and finish is an `InfoEvent` in the member's span, as ORBIT's schedule events are (`plans.py:121-132`).

##### Member handles, snapshots and output

- **Snapshots.** `MemberSnapshot` is a deep copy of the member's messages and `output`, its status, and an `interrupted` flag. The swarm keeps a reference to the `AgentState` the member's agent actually runs on, as ORBIT's `retain_running_state` does (`agent_scheduler.py:562-567`), because `run()` works on a copy. It snapshots at every boundary and when the member ends, including after a limit and after drain; a snapshot taken after cancellation mid-activation is marked `interrupted` and may end with an unanswered tool call.
- **No live state.** No API hands out the live `AgentState` or its message list. A policy reads snapshots; it changes a conversation only through `append` and `edit`.
- **Output.** `set_output(member)` makes that member's latest snapshot `output` the swarm's output, whether or not the member submitted, and records the choice and the member's status in the swarm's evidence. It is how ORBIT's `output_agent` is kept, including for a schedule that ends at `max_turns` before the output member submits. How the swarm's output and final conversations reach the scorer is the scoring deep dive's.

##### Policy rules

- **Concurrent steps.** A policy starts several `activate()` calls in an anyio task group and awaits them all; that is a barrier.
- **Eligibility.** A policy reads `snapshot()` before activating, so ORBIT's `max_messages` check stays exactly as it is: skip a member whose conversation has reached the cap, and let an activation that starts run to its quantum's end.
- **Errors.** A member's own limit ends that member, and `activate()` returns with status `limit`. Any other member error propagates through the swarm's member task group, which cancels the other members and the policy and re-raises, following swarm.md's rules for its own cap and for groups that mix errors (swarm.md, [Accounting](swarm.md#observer-evidence-accounting-and-metrics)). This is ORBIT's contract, "Concurrent failure cancels and joins siblings, then propagates the original failure", for a single failure; simultaneous independent failures surface as swarm.md's exception group.
- **Wake requests.** Under a policy, a `notify` delivery adds the notice at the recipient's next activation but never starts one, which is ORBIT's rule that messages do not activate agents.
- **Stop reasons.** swarm.md records a stop reason from a fixed set. A policy may return its own reason, recorded as `policy:<name>` (for example `policy:max_turns`, `policy:schedule_exhausted`, `policy:convergence`), alongside the swarm's own reasons.
- **The default.** M1's leaderless controller is the policy that activates every member once with quantum `completion`, so adding the interface changes no M1 behaviour.

inspect_swarm ships one reference policy, `round_robin(max_rounds=...)`: members in roster order, one `model_step` each, skipping members that ended. It serves as the test vehicle and as a policy for other scheduled harnesses. ORBIT's plans, superstep snapshots, `after_activation` amendments and outer-loop hooks are ORBIT's policy, written against `SwarmControl`.

#### P5. Trusted interventions (later, with P4)

ORBIT changes conversations between turns: pending injections, timed `inject_prompt` compromise, and defenses that rewrite messages before a turn (`dual_llm.py:295-330`). inspect_swarm gives trusted code two operations, valid only while the member is `pending` or `parked`:

- **`append(messages, origin=...)`** adds messages to the end of the conversation.
- **`edit(transform, origin=...)`** calls `transform` (sync or async) with a detached deep copy of the conversation and takes back a new message list.

While a member is pending or parked, its handle holds a staged copy of the conversation. Both operations change the staged copy under the handle's lock, so each sees the effect of the ones before it. `activate()` waits for any operation in progress, then commits the staged copy: for a pending member it is `run()`'s input; for a parked one the seam returns it to `react()` on resume. Either operation raises if the member is running or has ended.

Each operation writes an `intervened` record when it commits: the origin, member, activation id, and, by diffing on message id, the ids of messages appended, removed, and replaced (same id, different content), with content hashes. An operation cancelled before it completes changes nothing and writes nothing. If the member is drained while operations are staged, they are recorded as `intervened` with `committed: false`.

**Order at a boundary.** A policy applies its interventions for a member in this order, then activates it:

1. attack and scenario input, through `append`;
2. defenses, through `edit`, which therefore see and can rewrite step 1's messages, as ORBIT's pre-turn defenses see its pending injections today (`orchestrator.py:834-861`);
3. `activate()`, which commits the result and then appends any M2 notice.

**What may go through this path.** Interventions are configured by the eval author in Python and are never reachable from member tools. They carry eval-authored input (scenario events, attack payloads, compromise prompts) and transformations of the existing conversation (sanitising, redaction). They must not carry another member's output: a peer's text keeps its provenance whoever moves it, and peer messages reach members only as tool output (decision: Ransom, 2026-10-07). So ORBIT's dialogue and activity projections, which turn a peer's reply into a user turn (`dialogue.py:160-200`), are not adapted to interventions; they stay on ORBIT's executor. Whether eval-authored input may use the user or system role is [open question 1](#open-questions). inspect_swarm ships no intervention of its own.

### What an ORBIT port changes

These are the changes for the `common/v1` configurations that can move, in the order of the [migration path](#migration-path). A port also needs a supported-configuration check that keeps every combination in [What stays ORBIT-specific](#what-stays-orbit-specific) on ORBIT's executor.

- **Agents.** `build_turn_react_agents` builds `react()` (or `deepagent()`) with `on_continue=swarm_continue(spec_on_continue, conversational=not submit)`, in place of `turn_react`.
- **Executor.** `ScheduledExecutor` and `AgentScheduler` become an `ActivationPolicy`. `ScheduleRuntime` keeps deciding which agents each step runs; `_execute_agent` becomes an eligibility check on `snapshot()` followed by `MemberHandle.activate()`; asyncio tasks and semaphores become anyio task groups and limiters, or disappear because inspect_swarm owns member tasks.
- **Outer loop.** `ExperimentScheduler.run_loop` runs inside the policy: halt conditions and hooks between steps; at each boundary, runtime attacks and pending injections through `append`, then pre-turn defenses through `edit`, then `activate()`.
- **Channels.** ORBIT's channel tools call `deliver()` to send and `record_read()` before returning, and keep writing their own channel registry, canonical messages and receipts with the bus-assigned ids. `communication_edges` become `routes`.
- **Delivery and observation.** `CommunicationDeliveryModel` is removed: `notify` uses inspect_swarm's notice. `CommunicationModel` stays, reporting through `record_exposure()`, on members with defense filters, and is removed elsewhere. `CommunicationState` is completed at finalisation from the swarm's evidence ([Compatibility](#compatibility-and-migration)).
- **Defenses.** Tool gates become per-member approval policies, then sentinel protocols. Safety-prompt filters become part of the member's system prompt. Input and output filters stay as ORBIT's wrapper until sentinel's generate stages exist.
- **Output.** `output_agent` becomes `set_output()` at the end of the policy.
- **Dependencies.** ORBIT depends on inspect_swarm, which tracks inspect_ai `main`.

### What stays ORBIT-specific

- Scenarios, datasets, attack and defense registries, scorers, judges, the DCOP phase controller, the YAML and CLI wrapper, and the viewer overview.
- The `legacy/v1` profile: `legacy` and `tool_step` quanta, the `summary` and `peer_messages` observation modes, cross-agent memory injection, and legacy channel formats. It keeps `turn_react` and ORBIT's executors.
- `auto` delivery, and dialogue and activity projections, which put peer text in a conversation outside a tool result. The `common/v1` interleaved conversations that use dialogue projections (`runtime_profiles.py:57-72`) therefore stay on ORBIT's executor.
- Timed `replace_agent` compromise. A parked `react()` invocation keeps running its original closure, and this design gives no way to swap a member's agent mid-sample. Pre-deployment replacement is roster data and moves.
- `TopologyExecutor` and persistent delegation sessions. A root agent with delegates can be one swarm member if ORBIT wants the ledger.
- Plan amendment with audited revisions, superstep snapshots of activity summaries, and convergence halting: all inside ORBIT's policy.
- Model-boundary defenses, and the post-filter observer they require, until sentinel's generate stages exist.

### Behaviour a port changes

A port does not reproduce every ORBIT behaviour exactly. Runs from the two runtimes should not be pooled unless these are accepted or controlled; whether Ransom accepts them for an opted-in port is [open question 3](#open-questions):

- **Notices persist.** ORBIT recomputes the notice for every request and never stores it (`delivery.py:297-305`, `:336-343`). inspect_swarm adds it to the conversation at a turn boundary (the M2 design). Prompts and cache keys differ.
- **Notice role.** ORBIT's notice is a system message (`delivery.py:218`). inspect_swarm's role is the M2 design's choice, and never `operator`.
- **No continue prompt on resume.** `turn_react` adds "Please continue with your next action." on every resume after a tool result; a gated `react()` member adds nothing, because it never left its loop.
- **Overflow recovery is not a boundary.** Under `model_step`, `turn_react` yields after context overflow (`turn_react.py:255-258`). `react()` does not call `on_continue` after a recovered overflow, so that activation runs one more generation.
- **One span per member.** ORBIT records one agent span per activation. inspect_swarm records one per member with activation events inside, and a named timeline per member (swarm.md, Member views).
- **Member time limits include time parked at the seam.** Use token, cost or message limits per member under a policy. The limits deep dive owns the details. A sequential policy has at most one member's calls in flight, which bounds overshoot by one member's fan-out.
- **Judge usage in tool gates.** ORBIT's tool-gate judges run inside the tool call and are charged to the member's limits. Approval policies run with token and turn limits suspended (`approval/_apply.py:49-57`), so the same judge is not charged to those limits; it is still in the sample's usage and the ledger. The limits deep dive should confirm the intended accounting for monitors.
- **Read output format.** inspect_swarm's own read tools fence each message under a bus-stamped sender line (swarm.md, Delivery). ORBIT's channel kind keeps its JSON lines, where JSON string escaping is the fence.

### Migration path

Each step names the configurations it moves; everything else keeps running on ORBIT's executor with its existing machinery (`turn_react`, the notice and observer wrappers) until a later step.

1. **After M1: concurrent `completion` runs without channels.** Configurations that are one concurrent `completion` step of submitting agents, with no channels and an answer taken from a submission, map onto a leaderless filesystem swarm. They gain drain, the realized-cost ledger and per-member submissions.
2. **After M2: the same runs with channels.** Those configurations can add ORBIT's channels as a channel kind on the bus, with M2's notices and the transcript observer, because they run inside a swarm, where P2's member context and P3's subscription exist. Scheduled runs do not move in this step: their executor calls `run()` per activation outside any swarm, so the bus would refuse their sends and the observer would never be installed.
3. **After the scheduled-mode item (P4, P5): ORBIT's scheduler as a policy.** Scheduled `common/v1` runs move, except the combinations in [What stays ORBIT-specific](#what-stays-orbit-specific). `turn_react` and the notice wrapper are no longer used for them; tool gates move to approval policies. Members with defense filters keep ORBIT's filter wrapper and its observer, reporting through `record_exposure()`.
4. **When sentinel's generate stages land: defenses as protocols.** Input and output filters become sentinel protocols, resampling becomes `resample()`, and, once it is confirmed that the transcript then records the filtered request, the filter wrapper and the observer wrapper go away.

Through all four steps `legacy/v1` stays on ORBIT's own executors.

## Alternatives considered

**A turn gate in inspect_ai's agent channel.** Add an async gate that producers register and `before_turn` awaits (`channel.py:432-453`).

- Covers the first turn, recovered-overflow turns and every agent that uses the channel facade, without rebuilding the agent with `swarm_continue()`.
- It is an inspect_ai behaviour change, it depends on the binder M2 has not settled, and `react()` and `deepagent()` members, the cases ORBIT needs, already work through `on_continue`.
- Kept as the path if members that cannot take `swarm_continue()` need scheduling; [open question 2](#open-questions).

**Gate in `on_before_model_generate`.** A process-wide hook that already sees every generation.

- It runs inside the connection slot (`_model.py:1058`, `:1505`), so a member waiting there would hold a slot. With `max_connections` at or below the number of parked members, nothing else could run.
- It also fires for every model call in the process, including scorers and judges, and once per retry.
- Rejected.

**Keep ORBIT's model: one `run()` per activation on a stored `AgentState`.**

- Closest to ORBIT today, and every activation is its own span.
- It needs clean re-entry of `react()` (an inspect_ai change swarm.md lists for persistent members), loses per-run state (attempt count, compaction, MCP connections, the channel), and adds a span per activation.
- Rejected in favour of the seam, which needs no inspect_ai change.

**Notices and exposure by wrapping `Model`, as ORBIT does.**

- Works today; it handles caching correctly when done with ORBIT's care, and a `ModelAPI`-level observer sees the request after any filter below it.
- Each wrapper must preserve model identity and effective configuration through a private API, the wrappers' order matters, and inspect_swarm would carry this for every member.
- Rejected for inspect_swarm's own machinery: turn-boundary notices and a transcript subscription need neither. An eval whose own filters rewrite requests below the transcript keeps its post-filter observer and reports through `record_exposure()` ([P3](#p3-evidence-and-exposure-m2)).

**A live, mutable state handle for interventions.** Give trusted code `MemberHandle.state` and let it change `messages` while the member is parked.

- Simplest for ORBIT's pre-turn defenses, which already mutate `AgentState.messages` in place.
- `AgentState.messages` is a mutable list of mutable messages (`src/inspect_ai/agent/_agent.py:44`); a reference taken while parked stays writable after the member resumes, and in-place edits pass through no code that could record them or check the member's status.
- Rejected in favour of snapshots plus `append` and `edit` on a staged copy ([P5](#p5-trusted-interventions-later-with-p4)).

**Absorb ORBIT's scheduler into inspect_swarm.** Ship plans, amendments, superstep and quanta as inspect_swarm features.

- One implementation for every scheduled harness.
- It is the largest option and turns ORBIT-specific policy into inspect_swarm API before a second user asks for it.
- Rejected. inspect_swarm ships the interface and `round_robin`.

**Share vocabulary only.** inspect_swarm reuses ORBIT's names and evidence kinds; ORBIT keeps its runtime.

- Cheapest, and what swarm.md implies today.
- ORBIT keeps the fork and wrappers, and the two never share a ledger or evidence.
- Not enough for the task, though it remains what happens if ORBIT stays on its own runtime.

## Compatibility and migration

- **inspect_swarm** has no released API. P1 to P5 are additions to swarm.md's plan:
  - the member record gains `input` and `exposure`;
  - the bus gains a public record type, `deliver()`, `routes`, `message_id` and `sequence`, `record_read()` and `record_exposure()`;
  - evidence gains the `notified` and `intervened` kinds, an `exposed` subject, and per-member coverage, under the versioned `InfoEvent` payload swarm.md specifies;
  - the scheduled-mode item gains `swarm_continue()`, `ActivationPolicy`, `SwarmControl`, `MemberHandle` and `round_robin`;
  - stop reasons gain `policy:<name>`.
- **inspect_ai**: none required. The seam uses `on_continue`, and the observer uses the transcript subscription. Both are surfaces inspect_swarm may use. The binder and the notice item are M2's.
- **ORBIT logs.** Logs ORBIT already wrote are unaffected and keep their own readers. A ported run writes inspect_swarm's evidence (`source="inspect_swarm"`) for sends, reads, notices and exposure, and should record which runtime produced it, because of the [behaviour changes](#behaviour-a-port-changes).
- **ORBIT's `CommunicationState`.** Its consumers, the collusion scorer and the viewer overview, read it from the store, so a port keeps writing it, with each field from one source:

  | Field | Source on the swarm runtime |
  |---|---|
  | `version` | `1` when every member's coverage is known; `None` (evidence unavailable) when the swarm's evidence is missing |
  | `channels` (ids, readers, writers, `private`) | ORBIT's channel kind, which registers channels as today, including secret channels registered by attacks |
  | `messages` | ORBIT's channel kind at send time, with the `message_id`, `sequence` and surviving recipients that `deliver()` returns; `session_id` is the member's conversation id, `invocation_id` the activation id, `turn` the policy's outer turn |
  | `receipts` (per-message `rendered_messages`) | ORBIT's channel kind, which already holds the fragments it passes to `record_read()`; tool-call and event ids from the swarm's `read` record |
  | `events`: `sent`, `read` | the swarm's `sent` and `read` records, with the same message ids and native ids |
  | `events`: `model_exposure` | the swarm's `exposed` records with subject `message` |
  | `events`: `notified` | the swarm's `exposed` records with subject `notice`, which match ORBIT's meaning (the notice reached a successful input, `delivery.py:373-379`); the swarm's own `notified` records are not projected |
  | `events`: `delivered` | none: `auto` and dialogue projections do not run on the swarm runtime |
  | `observation_enabled_sessions` | members whose coverage is `complete`, or `external` and declared complete; a `failed` or `none` member is left out, so the scorer reports its reads as uncovered (`collusion_scorer.py:108-111`) instead of counting zero exposures |
  | `successful_input_sessions`, `observed_sessions`, `observed_invocations` | members with at least one observed successful generation; member conversation ids; activation ids |

  Native links (span, tool-call, message and model-event ids) are copied from the swarm's records. The viewer's event list is changed on the ORBIT side to accept `source="inspect_swarm"` evidence as well as `orbit.communication` (`_render.py:143`), rather than duplicating events.
- **Dependency ranges.** ORBIT accepts `inspect-ai>=0.3.263,<0.4` and tests one locked version (`ORBIT: pyproject.toml`). inspect_swarm tracks inspect_ai `main` and uses internals, so a port inherits inspect_swarm's floor, which is narrower.
- **Python and concurrency.** Both require Python 3.11+. inspect_swarm runs on asyncio and trio, so policy code must use anyio.

## Security

- **Peer content** reaches a member only as tool output, from inspect_swarm's read tools or from a channel kind's read tools. Neither the bus nor an intervention inserts it elsewhere. ORBIT's `auto` delivery and its dialogue and activity projections are not offered on the swarm runtime.
- **Sender identity** is stamped by `deliver()` from the calling member's context. An external channel kind cannot set it. Trusted calls outside a member must name an origin, which the evidence keeps.
- **Interventions and policies** are eval-author Python running in the runner process, as ORBIT's extensions are ("Registration and channel permissions are not a plugin sandbox", `ORBIT: docs/security-extensions.md`). They are never exposed as tools. They act only on a staged copy of a pending or parked member's conversation, and every committed change writes an `intervened` record with its origin and the ids of messages appended, removed or replaced. That a transformation carries no peer output is a contract on eval code, not something inspect_swarm can check; the record makes a breach visible.
- **Defenses before the next generation.** At a boundary, defenses run after attack input is appended, so a sanitising defense sees an injection before the member's next generation, as in ORBIT today.
- **Exposure is never overstated.** Members whose requests are rewritten below the transcript use their own post-filter observer, and coverage that is incomplete or failed is reported as unknown.
- **Notices** list only channel ids configured by the task or registered by trusted code, never ids or names a member created, which keeps swarm.md's notice rule.
- **The exposure observer** keeps read fragments in memory and stores ids, hashes and lengths only.
- **Policy decisions are never read from model output.** ORBIT installs no model-facing schedule editor (`plans.py:1-6`); a port must keep it that way.

## Testing

All in inspect_swarm, with mockllm, on asyncio and trio, with no network and no Docker:

- **The seam:**
  - a `round_robin` run reproduces the spike's order, with one system message and no added user messages;
  - a concurrent step waits at its barrier;
  - a `completion` activation of a submitting member runs to submission;
  - a conversational (`submit=False`) member gives two tool-free replies in two activations, with no continue prompt added, under both quanta; without a policy it ends at its first tool-free reply, as alone;
  - parked members are cancelled cleanly by drain;
  - a wrapped `on_continue` keeps its `True`, string, `AgentState` and `False` semantics;
  - a submission ends the member;
  - a nested agent built with `swarm_continue()` does not park;
  - a member without the seam under a `model_step` policy runs one activation, and the harness-validity checks report it.
- **Policies, snapshots and output:**
  - stop reasons `policy:<name>` are recorded;
  - a member limit returns status `limit`;
  - one member error cancels siblings and propagates the original; two simultaneous errors follow swarm.md's group rules;
  - the swarm's own cap ends the policy and drains;
  - notices do not start activations;
  - an eligibility check on `snapshot()` reproduces ORBIT's `max_messages` boundary: with a one-message input and a cap of two, an eligible activation runs to its answer;
  - `snapshot()` works in every status, and after drain is marked `interrupted`;
  - `set_output()` on a named member that never submitted, after a policy stops at `policy:max_turns`, makes its latest output the swarm's output.
- **Bus extension:**
  - an external channel kind delivers through `deliver()` with the sender stamped;
  - a call outside a member without an origin is refused;
  - routes drop recipients, and a record left with none becomes a tool error;
  - `sequence` increases monotonically;
  - `max_sequence` hides later messages.
- **Exposure:**
  - a read is exposed at the member's next successful generation;
  - a two-message read whose result reaches the model with only one fragment intact exposes only that message;
  - a tool message with a different call id, function or conversation, or with an error, exposes nothing;
  - a cache replay is marked;
  - a failed generation does not count;
  - a read with no later generation has no exposure;
  - concurrent members are attributed by span;
  - a notice's `exposed` record (subject `notice`) appears only after a successful generation that included it;
  - an observer that raises marks coverage `failed`, and no member is reported as complete;
  - a member declared `exposure="external"` is skipped by the transcript observer; with a test `ModelAPI` wrapper that redacts a peer message and reports through `record_exposure()`, the redacted message is not exposed even though the `ModelEvent` input contains it.
- **Interventions:**
  - `append` and `edit` raise while the member runs or after it ends;
  - an `edit` that deletes a message, replaces one with the same id and new content, and appends one writes one `intervened` record listing each;
  - an operation cancelled midway changes nothing; staged operations at drain are recorded with `committed: false`;
  - an injection appended with `append` and tagged untrusted is rewritten by a following `edit` before the member's next generation.
- **ORBIT-shaped conformance tests** with no ORBIT dependency:
  - a policy with sequential and concurrent steps over members using a reader and writer channel kind checks ORBIT's documented rules: messages do not activate members, submitted members are not activated again, step-start snapshots hide same-step sends, and concurrent failure cancels and joins siblings;
  - a test helper builds a `CommunicationState`-shaped index from the swarm's evidence by the projection table above, and checks private-channel messages, per-session exposure pairs, notices with ORBIT's meaning, and uncovered receipts when coverage failed.

## Implementation plan

Steps for inspect_swarm, each a PR inside the milestone named:

1. **M1: member records.** `input=` and `role=` on `member()`, `final="reporter"` by member name. Files: `src/inspect_swarm/_member.py`, `_final.py`, tests. How outputs and final conversations reach scorers is the scoring deep dive's.
2. **M2: bus extension points.** The public record type and `deliver()` with the sender stamped, `routes`, `message_id` and `sequence`, `record_read()` with fragments and `max_sequence`, and notice contributions. Files: `_bus.py`, `_channels.py`, `_evidence.py`. Shapes coordinated with the inter-agent communication deep dive.
3. **M2: exposure.** The transcript observer with per-fragment matching, notice exposure, cache-replay marking, coverage and failure handling, `member(exposure="external")` and `record_exposure()`. Files: `_observer.py`, `_evidence.py`, `_member.py`.
4. **Scheduled mode: the seam and policies.** `swarm_continue()` including `conversational`, `ActivationPolicy`, `SwarmControl` with `set_output()`, `MemberHandle` with snapshots, `round_robin`, and `policy:<name>` stop reasons. M1's controller is re-expressed as the default policy. Files: `_turn.py`, `_policy.py`, `policies/_round_robin.py`, `_swarm.py`.
5. **Scheduled mode: trusted interventions.** Staged conversations, `append` and `edit`, commit at `activate()`, `intervened` evidence. Files: `_turn.py`, `_policy.py`, `_evidence.py`.
6. **The ORBIT-shaped conformance tests.** `tests/test_orbit_shape.py`.

A port of ORBIT follows the [migration path](#migration-path); it is ORBIT work and not planned here.

## Open questions

1. **Roles for eval-authored input.** P5 lets eval-author code append messages at a member's boundary. ORBIT's pending injections are user messages and its timed `inject_prompt` compromise is a system message. Peer output is excluded either way ([P5](#p5-trusted-interventions-later-with-p4)).
   - (a) Allow any role for eval-authored input, always written as `intervened` with an origin.
   - (b) Allow only non-user, non-system roles, so those ORBIT attacks stay on ORBIT's executor.

   Recommendation: (a). The 2026-10-07 decision governs what members receive from each other; an eval author's scenario and attack input is the eval's design, as a sample's input is, and the record keeps it distinguishable.
2. **Where the turn seam lives.** This design uses `swarm_continue()` (no inspect_ai change; members must be built with it). The alternative is an awaited gate in inspect_ai's `AgentChannel.before_turn`, which also covers the first turn and recovered-overflow turns and works for any channel-facade agent, but is an inspect_ai change. M2's notices need the same seam, so the inter-agent communication deep dive should use the one chosen here. Recommendation: `swarm_continue()`, and propose the channel gate only if a user needs to schedule agents that cannot take it.
3. **The trajectory changes of a port.** For ORBIT runs that opt into the swarm runtime: persistent notices and different cache keys, no resume prompt, an extra generation after a recovered overflow, one span per member instead of one per activation, member time limits that include parked time, and tool-gate judges no longer charged to member token and turn limits ([Behaviour a port changes](#behaviour-a-port-changes)). Recommendation: accept them, with each run recording its runtime so results from the two runtimes are never pooled silently.

## Not this design

- **`tool_step` on `react()`.** Rejecting a multi-call batch before any tool runs needs a `react()` change, or a `parallel_tool_calls=False` approximation that providers honour unevenly.
- **Replacing a member's agent mid-sample**, which timed `replace_agent` compromise would need.
- **Conversation identity for descendants.** Exposure inside a member's nested agents is attributed to the member whose span contains the generation; per-descendant conversation identity, which ORBIT's persistent delegation tracks, is not designed here.
- **More built-in policies** (superstep, explicit plans, Concordia-style engines, AgentsNet's synchronous rounds), beyond `round_robin`.
- **Board channels with readers and writers.** The board is a later item in swarm.md. When built, it should take ORBIT's `readers`/`writers` names, so ORBIT's channel kind could become configuration.
- **Sentinel's generate stages and `resample()`.** They are on sentinel's roadmap. ORBIT's model-boundary defenses wait for them.
- **Per-member time and working limits while parked, and monitor accounting under approval.** For the limits deep dive.
- **Transcript eviction.** With `INSPECT_TRANSCRIPT_BOUNDED` set, old events leave memory (`src/inspect_ai/log/_transcript.py:69-76`). The observer is live, so it is unaffected, but any post-hoc evidence rebuild would need the log, not the in-memory transcript.
