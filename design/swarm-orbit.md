# ORBIT on inspect_swarm

Status: proposed, 2026-10-08; revised 2026-10-14 to follow the designs merged since, and 2026-10-15 for the bus's commit sequence and admission check, and for exposure left to ORBIT's own observer; 2026-10-16 for how read results are named for that observer, now chosen by the ORBIT work, and with the ORBIT design's remaining issues kept as open questions. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

ORBIT, the Inspect-based framework for multi-agent security experiments, could run its scheduled experiments on inspect_swarm without its fork of `react()` and without wrapping `Model`. In such a port:

- ORBIT's agents become ordinary `react()` or `deepagent()` members.
- Its scheduler becomes a `@swarm_controller` that runs members one quantum at a time.
- Its reader-and-writer channels become a channel of its own on the swarm's bus.
- The evidence its collusion scorer reads comes from the swarm's evidence.

ORBIT keeps its scenarios, attacks, defenses and scorers, and the configurations listed under [What stays ORBIT-specific](#what-stays-orbit-specific).

What inspect_swarm would add is all future work in [the implementation plan](swarm-implementation-plan.md#future-work), which lists "a scheduled execution mode" and "ORBIT on inspect_swarm". None of it is in M1 or M2:

- **A scheduled mode** ([P4](#p4-scheduled-mode-the-turn-seam-and-activation)).
  - A controller runs a member for one quantum with `activate()`. The member then parks at its next turn boundary inside `swarm_on_continue()`, the hook M2 already adds for notices.
  - The member stays inside one `react()` call for the whole sample, so nothing re-enters `react()`.
- **Trusted interventions** ([P5](#p5-trusted-interventions)). Between activations, eval-author code may append scenario or attack input to a staged copy of a member's conversation, and defenses may rewrite that copy. Every change is recorded.
- **Three small bus extensions** ([P2](#p2-bus-extensions)):
  - a channel whose records also reach members that are not running;
  - the bus's commit sequence, exposed to channels and controllers;
  - a public `record_read()` that writes a read's `read` event and returns its read id.
- **Exposure stays ORBIT's** ([P3](#p3-exposure)). The swarm computes no exposure. ORBIT's own post-filter observer joins its input to swarm reads and notices and reports what it finds through `record_exposure()`.

A port changes some behaviour, listed under [Behaviour a port changes](#behaviour-a-port-changes):

- notices stay in the conversation;
- ORBIT's resume prompt goes;
- the swarm's own answer is always a submission.

Two decisions are Ransom's ([Open questions](#open-questions)):

1. May a scheduled controller read and change member conversations, including adding user-role or system-role input?
2. Are the port's behaviour changes acceptable?

## Background

This is a deep dive on one topic of [swarm.md](swarm.md): how ORBIT, the multi-agent security framework compared in [Relationship to ORBIT](swarm.md#relationship-to-orbit), could run on inspect_swarm. It covers:

- how ORBIT's concepts map onto inspect_swarm's members, channels, controller, bus and observer;
- what inspect_swarm must add so that ORBIT runs without forking `react()` or wrapping `Model`;
- what a port of ORBIT changes;
- a migration path, and what stays ORBIT-specific.

Its first revision (2026-10-08) was written alongside the sibling deep dives. This revision follows the designs merged since, which decide much of what that revision proposed:

- [swarm-api.md](swarm-api.md) for controllers, member handles and stopping;
- [swarm-communication.md](swarm-communication.md) for the bus, channels, `swarm_on_continue()`, notices, evidence and the notices' metadata ids;
- [swarm-limits.md](swarm-limits.md) for statuses, member limits and the stop procedure;
- [swarm-scoring.md](swarm-scoring.md) for the final answer and the result record;
- [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md) for sandbox topology;
- [swarm-implementation-plan.md](swarm-implementation-plan.md) for the milestones.

[Against the merged designs](#against-the-merged-designs) lists, design by design, what this one uses as is, what it extends and what it gives up.

It keeps the standing decisions recorded in swarm.md and the deep dives:

- **2026-10-07.** Python 3.11+. inspect_ai internals may be used. Limits stay soft. M1, then M2, then the rest in any order. Members share the sample sandbox by default. Monitoring goes through inspect_sentinel. Red-team features are optional. Peer messages are model output, delivered as tool output with distinct provenance. The transcript representation stays open.
- **2026-10-09.** Peer text may enter the user role only in an explicit, opt-in experimental arm.
- **2026-10-13.** Ordinary notices stay on `swarm_on_continue()` even once inspect_ai has a `Steer` item. The milestones are defined by audience.
- **2026-10-14.** M2 is ready for external use, measured against Epoch's LeanOpenProblems swarm. Direct messages, and scoring beyond `first`, are later work.

Code references:

- ORBIT at `588b3035`, the revision swarm.md cites. ORBIT paths are prefixed `ORBIT:`.
- inspect_ai at `aa20052a`, unless a reference names another revision. Sentinel's generate stages are cited at `be4fd1fba`, inspect_ai `main` on 2026-10-14.
- inspect_sentinel at `0f9b5c71`, or `main` at `59d0fb6` where named.

Paths are relative to each repository's root.

## Why

swarm.md recommends that inspect_swarm "align with ORBIT and aim to be a substrate it could run on, not to absorb it". It lists what inspect_swarm should provide for that:

- turn boundaries and an activation-policy interface;
- notices without `Model` wrappers, and swarm evidence that ORBIT's own exposure observer joins model input to;
- an extension point on the bus for channel kinds.

That list is a claim, not a design. This document checks it against ORBIT's code and the merged designs, and makes it concrete.

ORBIT pays for running outside such a substrate:

- **A forked `react()`.** `turn_react` (`ORBIT: orbit/agents/turn_react.py`) exists so that a scheduler can stop an agent after one quantum and call it again later.
  - Its signature (`:90-104`) has no `compaction`, `retry_refusals`, `approval` or `review`.
  - Its loop opens no agent channel and no checkpointer.
  - inspect_ai's `react()` has all of these (`src/inspect_ai/agent/_react.py:56-72`, `:213-237`). Every `react()` improvement has to be ported to the fork by hand, or is lost.
  - It also imports private inspect_ai modules (`turn_react.py:31`, `:50`, `:66-67`).
- **Re-entry on a stored conversation.** Each activation is a fresh `run(agent, state)` on the agent's stored `AgentState` (`ORBIT: orbit/execution/agent_scheduler.py:588`). So the fork has to:
  - guard against re-inserting the system prompt (`turn_react.py:469-490`);
  - append a user message, "Please continue with your next action.", when resuming after a tool result (`:225-232`).
- **Three `Model` wrappers per agent** (`ORBIT: orbit/solvers/orchestrator.py:209-232`):
  - one adds unread notices to each request (`ORBIT: orbit/communication/delivery.py:308-352`);
  - one observes which messages reached a successful model input (`ORBIT: orbit/communication/model_observer.py:24-157`);
  - one applies defense filters, and refuses Inspect's response cache because the cache lookup sits above the filters (`ORBIT: orbit/defenses/model_filter.py:214-290`).

  The wrappers must reproduce the model's identity and effective configuration, which needs the private `Model._resolve_config` (`ORBIT: orbit/_model_config.py:8-17`).
- **Its own concurrency.** Concurrent plan steps use raw asyncio (`agent_scheduler.py:352-377`), whereas Inspect code is expected to run on anyio.

ORBIT's users are user group 2 in swarm.md: multi-agent safety and security evals. If ORBIT can run on inspect_swarm, security experiments and capability-scaling experiments share one runtime, one realized-cost ledger and one evidence model, and ORBIT stops maintaining a `react()` fork.

## Goals and non-goals

### Goals

- Map each ORBIT concept named in the task to an inspect_swarm component as the merged designs define it, or say it has none. The concepts are: roster and roles, channels with reader and writer lists, scheduled activation, delivery modes, evidence, the output member, compromised members, and model wrapping.
- Say what inspect_swarm must add for ORBIT to run with neither `turn_react` nor `Model` wrappers. Each addition is stated as an extension of a merged design, with its place in the plan's future work and its dependencies on M1 and M2 pull requests.
- Say what a port of ORBIT changes, and what stays ORBIT-specific.
- Give a migration path in which each step is usable on its own.

### Non-goals

- **Changing what the merged designs decided.** Where ORBIT needs more, this design extends them and says so ([Against the merged designs](#against-the-merged-designs)).
- **Porting ORBIT's own code.** Its scenarios, attacks, defenses and scorers, and its `legacy/v1` runtime profile, stay as they are.
- **Building red-team features in inspect_swarm.** They stay optional and unscheduled (decision: Ransom, 2026-10-07). This design keeps only the extension points they would use.
- **ORBIT's `auto` delivery for ORBIT's channels, and its dialogue and activity projections.** The later messages channel's opt-in `inject` mode is inspect_swarm's form of `auto` (decision: Ransom, 2026-10-09), but it applies to that channel only ([What stays ORBIT-specific](#what-stays-orbit-specific)).
- **Deciding whether or when ORBIT adopts inspect_swarm.**

## Current behaviour

### ORBIT

**Roster and roles.** `AgentSpec` (`ORBIT: orbit/configs/setup.py:79-164`) holds:

- a name and an informational `role` label;
- an optional model and temperature;
- a system prompt and tool names;
- `is_compromised`;
- a per-agent `max_messages`;
- `submit`, which says whether the agent has a terminal submit tool.

`max_messages` is an eligibility check. An agent whose conversation has reached it is skipped before an activation starts. An activation that does start runs to the end of its quantum (`agent_scheduler.py:539-551`).

A `submit=False` agent stays in the rotation after a tool-free reply (`ORBIT: orbit/agents/turn_react.py:422-453`).

`AgentGroup` gives several agents a shared goal, which replaces the sample input as their first user message (`ORBIT: orbit/configs/execution.py:22-40`, `agent_scheduler.py:243-254`).

**Invocation and routes are separate.**

- `TopologyEdge` says how one agent invokes another: `handoff`, `tool` (via `as_tool`) or `direct_run` (`setup.py:230-256`).
- `CommunicationEdge` says who may message whom, intersected with channel membership (`setup.py:223-228`, `:273-278`).
- Invocation edges never grant permission to send messages.

**Channels.**

- `ChannelConfig` has an id, `readers`, `writers` and a `private` label (`ORBIT: orbit/configs/communication.py:12-29`).
- The tools are `channel_post`, `channel_read` (with a cursor and bounded pages) and `channel_list` (`ORBIT: orbit/communication/tools.py:88-202`).
- The runtime binds the sender from a ContextVar identity, so a model cannot choose it (`ORBIT: orbit/communication/runtime.py:51-124`).
- A send with no permitted recipient fails (`runtime.py:246-252`). An oversized send becomes a `ToolError` for the sender (`tools.py:128-129`).
- Channel state lives in Inspect store models (`ORBIT: orbit/communication/state.py:120-155`).
- Attacks register further channels at run time, such as secret coalition channels (`orchestrator.py:240-268`).

**Delivery modes.** `CommunicationConfig.delivery` is `poll`, `notify` (the default) or `auto`, and can be overridden per agent (`communication.py:54-75`).

- For `notify` and `auto`, the outermost `Model` wrapper derives one `ChatMessageSystem` per request. It is appended after the conversation and never stored in it (`delivery.py:203-224`, `:234-283`, `:330-343`).
- A `notify` notice lists channel ids and unread counts and "contains no message text".
- An `auto` message carries message bodies under an "untrusted messages" header.
- Dialogue projections, a separate adapter, insert a peer's completed text into the recipient's conversation as a user turn (`ORBIT: orbit/communication/dialogue.py:1-8`, `:193`).
- The legacy `peer_messages` and `summary` observation modes also inject user messages (`agent_scheduler.py:685-803`).

**Scheduled activation.**

- `ExecutionConfig` (`execution.py:109-207`) selects round-robin, superstep or interleaved modes, or an explicit plan (`ORBIT: orbit/configs/scheduling.py`, `ORBIT: orbit/execution/plans.py`).
- A plan step lists agents and runs them in order, or concurrently with a barrier. It may set its own quantum and visibility.
- The quanta (`scheduling.py:13`, `turn_react.py:462-467`) are:
  - `legacy`;
  - `model_step`: one generation and its tool batch;
  - `tool_step`: one tool call; a batch of several calls is rejected before any of them runs;
  - `completion`: for a `submit=False` agent, it ends at a tool-free reply and the agent can be activated again; for a submitting agent, it ends at submission.
- The public task helper defaults to scheduled `model_step` execution (`ORBIT: docs/architecture.md`, "Executors and conversations"). The `common/v1` profile uses `completion` for interleaved conversations (`ORBIT: orbit/scenarios/runtime_profiles.py:80-85`).
- A trusted `after_activation` callback may amend pending steps, with an audited revision (`plans.py:222-267`).
- Messages never activate an agent: "Sending never changes the schedule or wakes a submitted agent" (`ORBIT: docs/runtime-architecture.md`, "Channels and visibility").
- `visibility="step_start"` freezes channel reads at the sequence number where the step started (`runtime.py:36-48`, `:292-297`).

**The outer loop.** `ExperimentScheduler.run_loop` repeats turns until a halt condition: `max_turns`, wall-clock `max_time_seconds`, converged outputs, attack success, or custom conditions (`ORBIT: orbit/scheduler/scheduler.py:164-245`, `ORBIT: orbit/configs/scheduler.py`). Each turn:

1. activates runtime attacks;
2. applies pending injections as user messages tagged `untrusted_observation` (`orchestrator.py:834-849`);
3. runs pre-turn defense hooks over every agent's `AgentState` (`:851-861`; `ORBIT: orbit/defenses/dual_llm.py:295-330` rewrites messages there);
4. runs the executor;
5. evaluates attack outcomes (`:884-893`).

**Compromised members.** `CompromisedAgentAttack` has two modes (`ORBIT: orbit/attacks/compromised/compromised.py:35-75`):

- `inject_prompt` appends the attacker's payload as a system message;
- `replace_agent` replaces the agent callable with a `react()` built from the payload and the original tools.

Either can happen before deployment or at a `specific_turn` (`:110-123`). A replacement takes effect at the agent's next activation, which calls the new callable (`agent_scheduler.py:536`).

**Defenses.**

- Tool gates wrap each tool and run `on_tool_call` before the callable, turning a denial into a `ToolError` (`ORBIT: orbit/defenses/boundaries.py:191-294`).
- Input filters (safety prompts, memory injection, input monitoring with redaction) run in a `ModelAPI` wrapper (`model_filter.py:127-212`, `boundaries.py:141-185`).
- Output checks with resampling run in a `Model` wrapper (`boundaries.py:296-343`).

**Evidence.** The kinds are (`state.py:57-83`, `ORBIT: docs/communication.md`):

- `sent`;
- `read`;
- `notified`: a notice reached a successful model input;
- `delivered`: content was inserted into a conversation;
- `model_exposure`: original content survived into a successful generation's input.

Each is an `InfoEvent` with source `orbit.communication` (`runtime.py:190-227`). Each carries the agent, session, invocation, channel and sequence, and the Inspect span, tool-call and event ids. Exposure is matched by the read's tool-call id and the rendered text (`runtime.py:330-399`).

**Output member.**

- `ExecutionConfig.output_agent` picks whose latest output becomes `TaskState.output`, whether or not that agent submitted ("Answer selection does not assert that this agent submitted/succeeded", `agent_scheduler.py:861-869`).
- `history_projection` picks which conversations are copied into `TaskState.messages` for scoring (`:813-860`).

**Consumers of the evidence.**

- The collusion scorer reads these from `CommunicationState`, and reports missing coverage as unknown rather than zero (`ORBIT: orbit/scorers/collusion_scorer.py:78-130`):
  - private channel policies;
  - canonical messages;
  - read receipts, with each message's rendered fragment;
  - the sessions for which observation was enabled;
  - `model_exposure` events.
- The viewer overview reads the same store keys, and the `InfoEvent`s whose source is `orbit.communication` (`ORBIT: orbit/viewer/_render.py:135-150`, `:293-300`).

### inspect_ai seams this design uses

- **`react()`'s `on_continue`.** It is awaited after every completed turn (`src/inspect_ai/agent/_react.py:366-392`). The exceptions are a turn that submitted (`:324-335`), a turn whose context overflow was recovered (`:271-276`), and a turn interrupted through the agent channel (`:359-364`).
  - It may return `True`; a string, which is appended as a user message; `False`, which ends the loop; or an `AgentState`, whose messages and output replace the state's (`:388-390`).
  - Without an `on_continue`, a `submit=False` `react()` ends at its first tool-free reply (`:553-554`).
  - Returning `False` ends the loop with whatever the last turn left. After a tool-call turn, the returned output still has `stop_reason == "tool_calls"` ([Spike](#spike)).
- **The agent channel cannot hold a turn.** `subscribe_turn_state` callbacks are synchronous observers (`src/inspect_ai/agent/_channel/channel.py:293-324`). `before_turn` blocks only when the conversation has no user message yet (`:432-453`).
- **`on_before_model_generate` runs while holding a connection slot.** The slot is taken at `src/inspect_ai/model/_model.py:1058`, and the hook is awaited inside the attempt at `:1505`, before the cache lookup (`:1528-1537`). A hook that waited there would pin a connection slot for as long as it waited. Hooks are also process-wide, not per sample.
- **What a `ModelEvent` records.** The event's `input` is recorded before `ModelAPI.generate` is called (`src/inspect_ai/model/_model.py:1577`, `:1636`). A `ModelAPI` wrapper that rewrites the request, as ORBIT's defense filters do, changes what the provider receives but not what the event records.
- **Transcript history.** A sample's transcript keeps every event in memory unless `INSPECT_TRANSCRIPT_BOUNDED` is set. If it is set, older events are evicted, and `transcript().history.full_history_available` says whether they can still be read through a history provider (`src/inspect_ai/log/_transcript.py:69-73`, `:302-310`, at `fccfb298e`).
- **Message limits** are checked before every generation and on every append to a limited conversation (`src/inspect_ai/model/_model.py:1025-1028`, `src/inspect_ai/util/_limited_conversation.py:19-30`).
- **Approvers run with token and turn limits suspended** (`src/inspect_ai/approval/_apply.py:49-57`).
- **Sentinel's generate stages only observe.** At `be4fd1fba`:
  - `sentinel_before_generate` runs before the request is sent and before the cache lookup (`src/inspect_ai/model/_model.py:1017-1030`). `sentinel_after_generate` runs before the output reaches the agent.
  - Only a `terminate` decision takes effect at either stage (`src/inspect_ai/_sentinel/_dispatch.py:168-227`).
  - Monitors must not mutate the step's input or output (`inspect_sentinel: src/inspect_sentinel/_step.py:13-60`, at `59d0fb6`).
  - So a protocol cannot yet redact a request or resample an output.

### What the merged designs already provide

- **Controllers and handles** ([swarm-api.md](swarm-api.md#controllers)).
  - A controller is a `@swarm_controller` object. Its `run(SwarmControl)` starts members with `start(member, input=...)` and returns a stop reason. A reason the controller adds is recorded as `controller:<reason>`.
  - The runtime owns member tasks, limits, stopping, finalisation and evidence.
  - `MemberState` is `pending`, `running` or `ended`.
  - Handles and events carry no member text.
  - swarm-api.md expected this deep dive's scheduled mode to be a controller, with `activate()`, `snapshot()`, `append()` and `edit()` as methods on the same handles. It left to this design the case for giving such controllers member text.
- **The bus and channels** ([swarm-communication.md](swarm-communication.md#records-and-the-bus)).
  - Every sanctioned record goes through `Bus.deliver()`. The bus binds the sender from the member ContextVar and resolves addresses at step 0, where a recipient that is not `running` makes the send fail; an unaddressed record, such as a shared-memory write, has none. Then come the policy, commit checks (the sender's and recipients' liveness, storm controls and the channel's `admit()`), `sent` evidence, and finally apply with `delivered` evidence carrying the commit sequence number ([swarm-memory-channel.md Changes to the bus](swarm-memory-channel.md#changes-to-the-bus)).
  - A channel is a `@swarm_channel` whose per-run `ChannelState` supplies `tools()`, `validate()`, `admit()`, `apply()`, `notice_lines()` and `quiescent()`. User channels may implement it from M2, and the interface is experimental in the first release.
  - Routes are a parameter of the later messages channel, `messages(routes=)`.
- **The turn hook and notices** ([Notices](swarm-communication.md#notices)).
  - `swarm_on_continue(inner=None, *, no_tool_calls="continue")` computes the continuation exactly as the member would without it.
  - Unless that result is `False`, it appends a metadata-only notice: a `ChatMessageUser` with `metadata.inspect_swarm.type == "notice"`.
  - It acts only in the member's bound loop.
  - The swarm never posts to agent channels. Ordinary notices stay on this hook (decision: Ransom, 2026-10-13).
- **Evidence** ([Evidence and provenance](swarm-communication.md#evidence-and-provenance)).
  - The kinds are `sent`, `delivered`, `notified` and `read`, as `InfoEvent`s with `source="inspect_swarm"`, `version: 1` and `type: "comm"`. M2 computes no exposure.
  - Notices and injections carry the swarm's ids in their metadata. A read is identified by its `read` event and the fenced ids in its result text; its result message carries no swarm metadata in M2 ([Records in model input](swarm-communication.md#records-and-the-bus); decision: Ransom, 2026-10-16).
- **Limits and stopping** ([swarm-limits.md](swarm-limits.md#stopping)).
  - Every stop goes through `stop(reason)`: close synchronously, soft stop, grace, hard stop, finalise.
  - Member statuses are a closed set. A normal return is `submitted`, unless the output's `stop_reason` is `content_filter` or `model_length`.
  - A member time limit is an Inspect `time_limit` around the member.
  - Approval calls are kept out of token and turn nodes, but they count against cost nodes ([Interactions](swarm-limits.md#interactions)).
- **Answers** ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)).
  - Only submissions are answers ("nothing is invented"). M1's answer is `first`, and `reporter(member)` comes later with the final-answer chain.
  - The record `inspect_swarm:result` keeps every member's status, and from Part 2 its output.
  - The returned state carries the sample's input and the answer, not the members' conversations.
- **Sandboxes.** Members share the sample's sandbox by default. Per-member sandboxes are an M2 opt-in (`member(sandbox=, visible_sandboxes=)`). Users isolation in one sandbox is later work ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md)).
- **Plan** ([swarm-implementation-plan.md](swarm-implementation-plan.md#future-work)). "A scheduled execution mode" and "ORBIT on inspect_swarm (PR #4)" are future work.

### Spike

Throwaway solvers were run on the worktree's inspect_ai (`fccfb298e`, whose `react()`, channel, `run()` and `ModelEvent` code is unchanged from `aa20052a`). Three `react()` members were scripted by mockllm to call a tool twice and then submit. Each member's `on_continue` played the part this design gives `swarm_on_continue()` in scheduled mode: it signalled "yielded", then waited on an anyio event.

- A round-robin loop opened one member at a time. Tool calls ran in the order `a1 b1 c1 a2 b2 c2`. Each member's final conversation had one system message, one user message and no continue prompts.
- Opening all three gates and waiting for all three to yield (a concurrent step with a barrier) also worked.
- Cancelling the task group while members were parked ended the sample cleanly, with status `success`.
- A transcript subscriber saw each tool result in the member's next completed `ModelEvent` input, with no `Model` wrapper. (The port no longer uses this: ORBIT's exposure is observed below its filters, [P3](#p3-exposure).)
- A `submit=False` `react()` member whose `on_continue` parked at a tool-free reply, and resumed by returning its own `AgentState`, gave two activations with two replies. The second generation's input was the system message, the user input and the first reply, with no continue prompt added.
- Releasing a parked member by returning `False` (2026-10-14):
  - A submitting member parked after a tool-call turn returned at once, with its conversation ending in the tool result and `output.stop_reason == "tool_calls"`.
  - A `submit=False` member parked after a tool-free reply returned its reply as its output, with `stop_reason == "stop"`.
  - Both samples ended with status `success`.

So ORBIT's `model_step` scheduling, and its repeatable conversational `completion` activations, work on unmodified `react()` members that stay inside one `react()` invocation for the whole sample. M1-9's status rule would read the first released member as `submitted`, which is why [P4](#p4-scheduled-mode-the-turn-seam-and-activation) gives a member released mid-work its own stop cause.

## Design

### Concept map

| ORBIT | inspect_swarm | Notes |
|---|---|---|
| `AgentSpec` (name, role, model, prompt, tools) | `member(factory, args=, name=, role=, limits=)` | ORBIT's member builder, a registered `@agent`, builds each agent from the spec's fields, once per member per sample ([swarm-api.md](swarm-api.md#the-member-contract)). Members with hooks are rebuilt through ORBIT's task ([swarm-api.md](swarm-api.md#logging-and-replay)). |
| `AgentSpec.max_messages` | An eligibility check in ORBIT's controller, against the member's [snapshot](#member-handles-and-snapshots) | Not Inspect's `message_limit`, which stops before a generation, not before an activation. |
| `AgentGroup.goal` | `input=` on `start()` or `activate()`, from ORBIT's controller | No member field is needed ([P1](#p1-members-input-and-output-no-new-api)). |
| `AgentSpec.submit=False` | `swarm_on_continue(no_tool_calls="stop")` under a scheduled activation ([P4](#p4-scheduled-mode-the-turn-seam-and-activation)) | A tool-free reply ends the activation, not the member. |
| `is_compromised`, pre-deployment compromise | Roster data that ORBIT builds before `swarm()` | inspect_swarm has no compromise feature. |
| Timed `inject_prompt` compromise | `append()` at a boundary ([P5](#p5-trusted-interventions)) | Eval-authored input. Its role is [open question 1](#open-questions). |
| Timed `replace_agent` compromise | None | Stays on ORBIT's executor. |
| Invocation edges, persistent delegation sessions | Inside a member's own `Agent` (`as_tool`, `handoff`) | ORBIT-specific; not swarm members. |
| `communication_edges` | Applied by ORBIT's channel when it computes recipients ([P2](#p2-bus-extensions)) | Not `messages(routes=)`, which belongs to the messages channel. |
| `ChannelConfig` readers and writers, secret coalition channels, DCOP blackboards | A user `@swarm_channel` with `recipients="roster"` ([P2](#p2-bus-extensions)) | The channel interface is experimental in the first release. |
| `poll`, `notify` | swarm-communication.md's `poll` and `notify` | The notice is computed when a parked member resumes, and stays in the conversation ([Behaviour a port changes](#behaviour-a-port-changes)). |
| `auto` | None for ORBIT's channel | The later `messages(delivery="inject")` is the messages channel's own mode. |
| Dialogue and activity projections, `peer_messages`, `summary` | None | They put peer text in a conversation outside a tool result, and stay on ORBIT's executor. |
| Scheduler modes, plans, quanta | ORBIT's `@swarm_controller` driving `activate()` ([P4](#p4-scheduled-mode-the-turn-seam-and-activation)) | `model_step` and `completion` only. |
| `visibility="step_start"` | `activate(read_cutoff=)` checked against the bus's commit sequence (`delivered.seq`) ([P2](#p2-bus-extensions), [P4](#p4-scheduled-mode-the-turn-seam-and-activation)) | |
| `max_turns`, halt conditions, hooks | The controller's `run`; its reasons are recorded as `controller:<reason>` | |
| `sent`, `read`, `model_exposure` | `sent`, `read`, and ORBIT's own `model_exposure` written through `record_exposure()` ([P3](#p3-exposure)) | |
| ORBIT `notified` (a notice reached a successful input) | `model_exposure` with `notice` set, through `record_exposure()` ([P3](#p3-exposure)) | The swarm's own `notified` means a notice was added to the conversation. |
| ORBIT `delivered` (content inserted into a conversation) | None on the swarm runtime | Only `auto` and dialogue projections produce it. The swarm's `delivered` means a record reached channel state. |
| `output_agent` (latest output, submitted or not) | ORBIT's solver wrapper around the swarm, reading the result record in a synchronous `finally`; later, `reporter(member)` | `reporter` selects only a submission ([P1](#p1-members-input-and-output-no-new-api)). |
| `history_projection` | ORBIT's member wrapper saves each final conversation, and ORBIT's solver wrapper projects them in its `finally` | The swarm returns no member conversations ([P1](#p1-members-input-and-output-no-new-api)). |
| Tool gates (`on_tool_call`) | Per-member `react(approval=...)` policies, or sentinel `BeforeToolCall` protocols (the dispatcher is on inspect_ai `main`) | Judge usage moves out of member token and turn limits ([Behaviour a port changes](#behaviour-a-port-changes)). |
| Input filters with redaction, output checks, resampling | None yet. Sentinel's generate stages only observe or terminate | ORBIT keeps its filter wrapper and reports exposure after its filters ([P3](#p3-exposure)). |
| Notice `Model` wrapper | `swarm_on_continue()` notices | Removed for ported runs. |
| Exposure `Model`/`ModelAPI` wrapper | ORBIT's own observer, reporting through `record_exposure()` and joining input to swarm reads and notices ([P3](#p3-exposure)) | Kept on every member whose exposure ORBIT measures. |
| Shared sandbox ("Neither channels nor agent groups isolate shared files or sandboxes") | The shared-sandbox default. Per-member sandboxes are an M2 opt-in | Same default. |

### What inspect_swarm provides

Four items, P2 to P5, all future work. The plan already lists the scheduled mode and ORBIT as future work, and M1 and M2 do not depend on any of them. P1 records what ORBIT needs from members, input and output, all of which the merged designs already provide. Each later item names the M1 and M2 pull requests it builds on ([Implementation plan](#implementation-plan)).

#### P1. Members, input and output (no new API)

- **Roles.** `role` is a field of M1's `Member` record ([swarm-api.md](swarm-api.md#members)). Like ORBIT's, it is informational: it appears in the preamble, the record and the evidence.
- **Per-member input.** ORBIT's group goals are input that ORBIT's controller passes to `start(member, input=...)`, or to `activate(..., input=...)` for a member's first activation. This design's first revision proposed a `member(input=)` field, which swarm-api.md lists as the default for `start()`. It is no longer needed, because ORBIT always runs its own controller.
- **The answer.** swarm-scoring.md makes only a submission an answer: `first` in M1, and `reporter(member)` once the final-answer chain is built. ORBIT's `output_agent` takes the output member's latest output whether or not it submitted. ORBIT keeps that rule on its own side, in an ORBIT solver that wraps the swarm, as its `mas_orchestrator` wraps its executor today ([below](#orbits-solver-wrapper)).

  The swarm's own answer, and the record's `final`, are unaffected. A conversational member released at the stop after a tool-free reply ends `submitted` ([P4](#stopping-a-scheduled-swarm)), so under `reporter` such a member's last reply is an answer too.
- **Member conversations.** The swarm returns the sample's input and its answer, not the members' conversations ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)). For `history_projection`, ORBIT's member builder, a registered `@agent` of its own, builds each member's agent and wraps it. In a synchronous `finally` block, the wrapper writes the final `AgentState.messages` to an ORBIT store key under the member's name. Because `react()` updates the state it was given in place, the wrapper sees the final conversation after a submission, a limit, a stop release, a hard stop or a cancellation.

##### ORBIT's solver wrapper

ORBIT's selection and projection must survive an exceptional exit, as they do today: `mas_orchestrator` calls `executor.finalize(state)` in a `finally`, after deployment failures, a failed first turn and cancellation alike, and never replaces the original exception (`ORBIT: orbit/solvers/orchestrator.py:930-959`). A separate solver step after the swarm would lose them exactly then. An outer sample limit or a cancellation ends the swarm by raising, and Inspect's chain runs no later step once one raises (`src/inspect_ai/solver/_chain.py:84-91`); the sample would keep the swarm's provisional answer, not ORBIT's choice.

So ORBIT's task uses one solver of its own that awaits the swarm's solver (`as_solver(swarm(...))`) inside `try`, with a `finally` that:

1. runs after `as_solver()`'s own `finally` has copied the swarm's messages and provisional or final answer into the `TaskState` (`src/inspect_ai/agent/_as_solver.py:65-80`), and after every member wrapper's `finally`, which run as member tasks unwind inside the swarm;
2. reads the `inspect_swarm:result` record, which the runtime keeps accurate as members end, with `provisional: true` after an outer limit ([swarm-scoring.md](swarm-scoring.md#what-the-swarm-records)), and sets `TaskState.output` to the output member's `output` (a field Part 2 adds to the record), if the member has one;
3. sets `TaskState.messages` from the conversations the member wrappers saved, by ORBIT's `history_projection`;
4. is synchronous, with no `await`, because an `await` in a `finally` that runs under cancellation is cancelled itself. Store reads and `TaskState` writes are synchronous;
5. lets the original exception, if there is one, propagate unchanged. A failure in the projection itself is recorded, as ORBIT records cleanup errors today, and never replaces the original exception.

The swarm's own answer rule, and its record's `final`, are unaffected.

#### P2. Bus extensions

ORBIT's reader and writer channels, the secret coalition channels its attacks register, and its DCOP blackboards are one user channel: an ORBIT `@swarm_channel` whose `ChannelState` implements swarm-communication.md's operations ([How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus)). Its tools call `Bus.deliver()`, so the bus binds the sender from the member ContextVar, as ORBIT's `actor_identity()` does today (`ORBIT: orbit/communication/runtime.py:117-124`).

ORBIT's channel computes each record's recipients itself, from the channel's readers intersected with its communication edges. Its tool refuses a sender who is not a writer, and a send left with no recipient, with a `ToolError`, as ORBIT does now. So the messages channel's `routes` are not needed. ORBIT's capacity checks, which ORBIT raises as `ChannelCapacityError` now, go in its `admit()` at step 2, not in `validate()` at step 0: a capacity depends on channel state, and a check before the policy's `await` would let two concurrent posts both pass against the same free capacity, where `admit()` sees each post's predecessor ([Records and the bus](swarm-communication.md#records-and-the-bus); decision: Ransom, 2026-10-15). `validate()` keeps the checks on the post alone.

Three extensions are needed, and one choice is left to the ORBIT work:

- **Recipients that are not running.**
  - Step 0 of `deliver()` rejects a send to any recipient that is not `running`. A pending member is one that has not started; under scheduled mode, that is one not yet activated.
  - ORBIT's channels are blackboards. Membership, not activity, decides who may read a message, and a reader that has not started yet, or has submitted, keeps its place.
  - Under a round-robin, the first member's first post goes to readers that are all still pending, and the liveness rule would reject it.
  - A channel therefore declares `recipients: ClassVar[Literal["running", "roster"]] = "running"`. With `"roster"`, step 0 resolves addresses against the roster alone and step 2 skips the recipients' liveness check (the sender's still applies), so pending and ended readers get the record in the channel's state.
  - The `delivered` event records each recipient's state. A pending reader sees the notice at its first boundary after it starts.
  - The bus still refuses every send once the swarm is stopping. The messages channel keeps `"running"`.
- **The commit sequence.** ORBIT's sequence is the bus's commit sequence, the run-wide number step 4 gives each applied record and writes as `delivered.seq` ([swarm-memory-channel.md Changes to the bus](swarm-memory-channel.md#changes-to-the-bus)), not the number in a record id: ids are given before the policy's `await`, so a record held by the policy can commit after a step-start cutoff with a lower id number, and comparing ids would show it to a reader that started before it existed (decision: Ransom, 2026-10-15).
  - `Delivery.seq` returns the number to the sender's tool, and `SwarmControl.sequence()` returns the latest one committed, 0 before any ([P4](#p4-scheduled-mode-the-turn-seam-and-activation)).
  - `Bus.sequence()` returns the same number to a channel. Because the bus gives the number before it calls `apply()`, and steps 2 to 4 have no `await`, inside `apply()` it is the applied record's own, so ORBIT's channel stores it with the record in its canonical messages there, before any reader can see the record.
  - Step-start visibility compares a record's stored commit number with the activation's cutoff. `Record` gains no sequence field.
- **Read receipts.**
  - The bus call that a channel's read tool makes to write its `read` event becomes public as `record_read(records) -> str`. Inside the read tool it writes the `read` event in the tool span and returns the read id, under which ORBIT's channel keeps the fragments it rendered for its observer ([P3](#p3-exposure)).
  - It runs before the tool returns, at the point where ORBIT's own `record_read()` registers its rendered messages (`ORBIT: orbit/communication/tools.py:154-162`).
- **Naming a read in model input.** M2 puts no swarm metadata on a read tool's result message ([swarm-communication.md Records in model input](swarm-communication.md#records-and-the-bus)), because ORBIT's observer is the only consumer of such a name (decision: Ransom, 2026-10-16: "This is only needed by ORBIT? In that case it should be after M2."). The ORBIT work chooses how its observer finds a read's result message, among:
  - (c) **Stamping in `swarm_on_continue()`**, the leading option (Ransom, 2026-10-16: "I agree that c sounds most promising here, we can decide for sure once we start work."). The hook runs after `execute_tools` appends a turn's results and before the next generate (`src/inspect_ai/agent/_react.py:301-307`, `:376`), so it can set `metadata["inspect_swarm"]` (read id, channel, record ids) on the results of that turn's reads, with no inspect_ai change. It covers only loops that run the hook: a member's descendants and hookless members get no stamp.
  - **An inspect_ai `tool_result_metadata()` helper** (AI-6 in this PR's earlier revisions), which a running tool calls to set its own result message's metadata, through a per-call cell that `execute_tools` copies into the `ChatMessageTool` (today it sets content, tool call id, function and error only, `src/inspect_ai/model/_call_tools.py:412-418`). It covers every loop, at the cost of a new public inspect_ai API.
  - **Joining through the `read` event.** The observer finds the result by the event's tool span and the call's `ToolEvent`, whose id is the result's `tool_call_id`. No metadata, but tool call ids can repeat within a loop ([swarm-memory-channel.md Descendants](swarm-memory-channel.md#descendants)).
  - **Keeping ORBIT's current join.** ORBIT's `record_read()` captures the read's tool call id at read time (`ORBIT: orbit/execution/provenance.py:17-49`), and its observer matches a `ChatMessageTool` by tool call id, function, agent and session, then checks each rendered line verbatim (`ORBIT: orbit/communication/runtime.py:330-395`). It needs no swarm or inspect_ai change.

Notices need nothing new. ORBIT's channel returns `notice_lines(member)` with its configured channel ids and unread counts. These ids are chosen by the eval author, or by trusted attack code that registers a channel, never by a member, so swarm.md's notice rule allows them.

#### P3. Exposure

Exposure in ORBIT's sense, whether a record's or a notice's original content reached a successful generation's input after ORBIT's defenses, is ORBIT's own observer's job. The swarm computes no exposure and writes no exposure event of its own ([swarm-communication.md Alternatives considered](swarm-communication.md#alternatives-considered); decision: Ransom, 2026-10-15). ORBIT already observes it at the only place a redaction is visible: its observer sits inside its defense input filters and sees the filtered request (`ORBIT: orbit/defenses/model_filter.py:161-212`, `model_observer.py:42-95`), while a `ModelEvent` records the request before them (`src/inspect_ai/model/_model.py:1577`, `:1636`). So the port keeps that observer on every member whose exposure ORBIT measures, filtered or not.

- **What the swarm gives the observer.** A read has its `read` event and the read id `record_read()` returns, and the observer finds its result message as the ORBIT work chooses ([P2](#p2-bus-extensions), "Naming a read in model input"); a notice carries its notice id in metadata ([swarm-communication.md Records in model input](swarm-communication.md#records-and-the-bus)). The observer joins input messages to reads and notices that way, not by text alone, and then applies ORBIT's own rules to the content: each record's rendered fragment as a newline-delimited block of the filtered result (`ORBIT: orbit/communication/runtime.py:373-377`), and a notice's exact text (`delivery.py:370-380`). ORBIT's channel keeps its rendered fragments per read id in its own state, as ORBIT's `record_read()` registers them today at the same point (`tools.py:154-162`).
- **A notice's original text.** ORBIT compares a notice in the filtered input with the text it had when created (`ORBIT: orbit/communication/delivery.py:203-217`, `:370-379`), and the observer sees only the message as it now is, after any trusted `edit()` or defense. So the bus keeps each notice's text as `swarm_on_continue()` created it, in memory for the rest of the sample, keyed by notice id and by the notice message's id; `notice_text(id) -> str | None` returns it to ORBIT's observer, and the `notified` event stores its SHA-256 and length. The comparison, and the claim that a notice reached the model, stay ORBIT's.
- **Combined user messages.** For some providers Inspect combines adjacent user messages before the request, merging their metadata with the later message's winning (`src/inspect_ai/model/_model.py:1387`, `:2736-2756`, `:2812-2856`), and a notice is often followed by a continuation. `metadata["combined_from"]` names only the two ids of the last pairwise merge (`:2829-2837`), so when three or more messages combine, earlier notices' ids are lost from that input. ORBIT does not meet this today: its notices and delivered context are a tagged message its `CommunicationDeliveryModel` adds inside `generate()` (`ORBIT: orbit/communication/delivery.py:330-353`), matched by message id and exact text. On the swarm, notices are user messages that `swarm_on_continue()` appends. So the port may join a combined message to its notices through the loop's `notified` events, which record each notice message's own id, in order, and look for each notice's `notice_text()` within it. Full `combined_from` lineage would be an optional standalone inspect_ai fix, not a swarm prerequisite; the ORBIT work decides ([Open questions](#open-questions)). Tool results are never combined.
- **Reporting.** After each successful generation the observer calls `record_exposure(model_event_id, *, read=None, records=(), notice=None)` for what it found intact. The bus checks that the ids belong to this run and to the calling member, and writes ORBIT's own event, `model_exposure` with `type: "orbit"`, carrying the ids, the `ModelEvent` id, the activation id under a scheduled controller, and `cached: true` when the generation was a cache read. It records the observer's finding; it does not check it against the input.
- **Coverage.** `record_exposure_coverage(complete=True)` declares the observer installed for the member's whole run. A member without that declaration has unknown exposure, never zero, and the projection treats it as ORBIT treats a member without observation.

#### P4. Scheduled mode: the turn seam and activation

Scheduled mode is a way for a controller to run members, not a new kind of controller. A controller enters it per member, by activating that member with `activate()` instead of starting it with `start()`. The member's agent needs `swarm_on_continue()`, the hook swarm-communication.md already requires of any member that gets notices. No second hook is added.

##### The seam

`swarm_on_continue(inner=None, *, no_tool_calls="continue")` keeps its signature. Its scheduled behaviour applies only to a call made in the bound loop of a member whose current run began with `activate()`. In every other case (outside a swarm, in a nested loop, or for a member started with `start()`), it is exactly swarm-communication.md's hook.

The member's bound loop is the one swarm-communication.md's tool source binds ([Members and identity](swarm-communication.md#members-and-identity)). For a member with no `swarm_tools()`, the first call of the hook binds it. For `react()` and `deepagent()` that is always the top-level loop, because `deepagent()` passes `on_continue` only to its top-level agent (`src/inspect_ai/agent/_deepagent/deepagent.py:102-103`, `:222-237`).

At a scheduled member's turn boundary, the hook runs these steps:

1. **Continuation result.** Compute it as swarm-communication.md's hook does:
   - with `inner`, the inner hook's result;
   - with no inner hook, `True` after tool calls, and the result `no_tool_calls` gives after a tool-free reply (`True` for `"continue"`, `False` for `"stop"`).
2. **Stopping.** If the swarm is stopping, return `False` without parking ([Stopping a scheduled swarm](#stopping-a-scheduled-swarm)).
3. **Is this an activation boundary?**
   - **The conversational boundary.** The result is `False` only because `no_tool_calls="stop"` was applied with no inner hook, after a tool-free reply. This is ORBIT's `submit=False` agent: alone it would end here, and under a schedule the activation ends instead and the member stays available. It is a boundary under every quantum.
   - **A quantum boundary.** The result is not `False`, and the activation's quantum is `model_step`.
   - **Otherwise.** If the result is `False` from an inner hook, return `False`: the member ends, as it would alone. In any other case, behave exactly as the unscheduled hook does: append any notice and return the result.
4. **Continued state.** Build the state that `react()` would continue with, applying the result as `react()` does (`_react.py:366-407`, `:536-554`):
   - a string is appended as a user message (with `{submit}` replaced, for a submitting member);
   - a returned `AgentState` replaces both the messages and the output;
   - `True` after a tool-free reply appends `react()`'s own continue prompt (`DEFAULT_CONTINUE_PROMPT` with the submit tool's name);
   - `True` after tool calls adds nothing;
   - the conversational boundary adds nothing.

   These are the caller's and `react()`'s own continuations, and they are kept. What a port removes is only `turn_react`'s extra resume prompt, which the hook never adds.
5. **Park.** Take a [snapshot](#member-handles-and-snapshots) of the continued state. It is also the staged copy that [P5](#p5-trusted-interventions) interventions change. Mark the member `parked`, finish the activation (its `ActivationResult` is returned to the controller), and wait.
   - **Resumed by `activate()`.** Commit the staged conversation, set the new activation's read cutoff, and append any notice. The notice is computed now, so it covers records delivered while the member was parked. Then return the result as an `AgentState`.

     So the model next sees, in order: the caller's continuation (step 4), the interventions in the order the controller applied them, then the notice.
   - **Released by a stop.** Return `False` ([Stopping a scheduled swarm](#stopping-a-scheduled-swarm)).

A member that never reaches a boundary is unaffected, because nothing above runs:

- quantum `completion` with a submitting member, which runs to submission;
- a bridged member;
- an agent without the hook.

Its activation lasts until it ends. When a member activated with `model_step` makes two or more model calls and the hook never runs, the runtime records `scheduled_unwired` in the harness-validity checks, as swarm-communication.md records `notify_unwired`.

Because the member stays inside one `react()` invocation, these persist across activations, and nothing re-enters `react()`:

- its conversation;
- its attempt count;
- its compaction state;
- its MCP connections;
- its agent channel.

ORBIT's quanta map as follows:

- `model_step`: one generation and its tool batch.
- `completion`: until submission for a submitting member, and until a tool-free reply for one with `no_tool_calls="stop"`.
- `tool_step` and `legacy` have no equivalent, and stay on ORBIT's own executor.

##### Supported members

A scheduled member must keep all of its work inside its own turn, so that parking the turn suspends everything the member is doing:

- `react()` members, and `deepagent()` members without background dispatch, whose synchronous subagents run inside the turn's tool calls and finish before `on_continue` runs ([Interactions](swarm-limits.md#interactions));
- bridged members and agents without the hook, which never reach a boundary and so run one activation to their end.

Background descendants are excluded. A `deepagent(background=True)` member, or one with an integer `background`, dispatches children that run in the sample's task group, or after M2-5 in the member's background owner ([swarm-background-members.md](swarm-background-members.md)), and returns at once (`agent_tool.py:620-631`). Its `on_continue` belongs to its top-level loop only (`deepagent.py:102-103`). Parking that loop would leave the children generating between activations. That breaks the quantum, the barrier of a concurrent step, and the bound of one member's calls in flight under a sequential controller. M2-5's per-member owner gives the swarm ownership and drain, not a turn gate.

So:

- `activate()` raises `ValueError` naming the member when its agent is a `deepagent()` with `background` set, to `True` or an integer. It detects this the same way M1-3's rejection does, before M2-5 lifts that rejection for `start()`.
- The refusal is in `activate()`, where the runtime holds the member's configuration. A controller's `check` cannot make it at construction, because it receives only `MemberInfo` (name and role, [swarm-api.md](swarm-api.md#controllers)), and this design does not widen that interface.
- `start()` keeps M2-5's support for background members unchanged.
- A custom agent that calls `background()` itself cannot be detected. `activate()` documents it as unsupported.
- Scheduling a member's descendants as well is not this design.

##### Member handles and snapshots

Scheduled mode adds these to swarm-api.md's handles, as swarm-api.md anticipated:

```python
class MemberHandle(Protocol):                    # swarm-api.md's, plus:
    @property
    def parked(self) -> bool: ...                # running, and waiting at a boundary for activate()
    def snapshot(self) -> MemberSnapshot: ...    # detached deep copy; any state
    async def activate(
        self,
        *,
        quantum: Literal["model_step", "completion"] = "model_step",
        input: str | list[ChatMessage] | None = None,   # first activation only, as start()'s
        read_cutoff: int | None = None,                 # step-start visibility
    ) -> ActivationResult: ...                   # returns at the next boundary, or when the member ends
    async def append(self, messages: Sequence[ChatMessage], *, origin: str) -> None: ...   # P5
    async def edit(self, transform: MessageTransform, *, origin: str) -> None: ...         # P5


class SwarmControl(Protocol):                    # swarm-api.md's, plus:
    def sequence(self) -> int: ...               # the latest commit sequence (delivered.seq); 0 before any


@dataclass(frozen=True)
class ActivationResult:
    activation: str                              # "a-7": per-swarm, deterministic
    outcome: Literal["boundary", "ended"]
    state: MemberState                           # swarm-api.md's: "running" (parked) or "ended"
    status: str | None                           # the limits deep dive's status once ended
    submitted: bool
    messages_before: int
    messages_after: int
    duration: float
    snapshot: MemberSnapshot
```

- **`activate()`.**
  - On a `pending` member, it starts the member as `start()` would: with the given input, else the swarm's input, followed by the member's preamble. It then returns at the member's first boundary, or when the member ends.
  - On a parked member, it resumes the member as in step 5.
  - It raises `RuntimeError` in each of these cases:
    - the member is running and not parked (one activation at a time);
    - the member has ended;
    - the member was started with `start()`;
    - the swarm is stopping, as `start()` does.
  - It raises `ValueError` for a background-enabled `deepagent()` member ([Supported members](#supported-members)).
  - Each activation's start and end is an `InfoEvent` in the member's span, with `type: "schedule"` and kind `activation_started` or `activation_ended`. The events carry the activation id, the quantum, the read cutoff, the message counts, the duration and the outcome, as ORBIT's schedule events do (`ORBIT: orbit/execution/plans.py:121-132`).
- **States.** A parked member's state is `running`. So swarm-communication.md's liveness rule, `list_members()` and `events()` treat it as running, which is what it is: it holds an open `react()` call. `MemberEnded` events are queued for scheduled members as for any other.
- **Read cutoff.** The activation's `read_cutoff` stays on the member's runtime handle until its next activation. A channel's read tool reads it with `activation_read_cutoff() -> int | None`, a member-side helper that reads the member ContextVar. ORBIT's channel hides records whose commit sequence is above it, so a record committed after the step started is hidden whenever its policy began. The built-in channels ignore it.
- **Snapshots.**
  - `MemberSnapshot` holds a deep copy of the member's messages and `output`, its state and status, whether it is parked, and an `interrupted` flag.
  - The runtime keeps a reference to the `AgentState` the member's agent actually runs on, because `run()` works on a copy. swarm-scoring.md's member wrapper already keeps that reference for the member's output.
  - A snapshot taken after a cancellation mid-activation is marked `interrupted`, and may end with an unanswered tool call.
  - No API hands out the live `AgentState` or its message list. A controller reads snapshots, and changes a conversation only through `append()` and `edit()`.
- **Member text.** swarm-api.md gives controllers no member text, so that a controller cannot pass one member's words into another member's input. Scheduled controllers need text:
  - ORBIT's convergence halting and attack-outcome checks read outputs;
  - its pre-turn defenses rewrite conversations.

  `snapshot()` and `edit()` therefore carry member text, and only to controllers that activate members. Moving text between members stays forbidden as a contract on eval-author code, which the `intervened` record makes visible ([P5](#p5-trusted-interventions), [Security](#security)). Whether this exception is acceptable is part of [open question 1](#open-questions).

##### Controller rules

- **Concurrent steps.** A controller starts several `activate()` calls in an anyio task group and awaits them all. That is a barrier.
- **Eligibility.** A controller reads `snapshot()` before it activates a member, so ORBIT's `max_messages` check stays exactly as it is: skip a member whose conversation has reached the cap, and let an activation that starts run to the end of its quantum.
- **Errors.**
  - A member's own limit ends that member, and `activate()` returns with outcome `ended` and status `member_limit:<type>`.
  - A foreign error propagates as swarm-api.md specifies ([Controllers](swarm-api.md#controllers)): the member task group cancels the rest, including the controller's `run`. For a single failure, this is ORBIT's contract, "Concurrent failure cancels and joins siblings, then propagates the original failure". Simultaneous independent failures surface as the limits deep dive's group.
- **Wake.** Notices never start an activation, which is ORBIT's rule that messages do not activate agents.
- **Stop reasons.** ORBIT's halt reasons are the controller's own, recorded as `controller:<reason>`: for example `controller:max_turns`, `controller:schedule_exhausted` and `controller:convergence`.
- **Unscheduled members.** A controller that only calls `start()` is unaffected. `leaderless` stays as it is.

inspect_swarm ships one reference controller, `round_robin(max_rounds: int, quantum: Literal["model_step", "completion"] = "model_step")`, registered as `inspect_swarm/round_robin`. It activates members in roster order, one quantum each, skips members that have ended, and returns `controller:max_rounds` or `all_done`. It is the test vehicle, and a controller for other scheduled harnesses. ORBIT's plans, superstep snapshots, `after_activation` amendments and outer-loop hooks are ORBIT's controller, written against `SwarmControl`.

##### Stopping a scheduled swarm

The limits deep dive's `stop(reason)` applies unchanged ([Stopping](swarm-limits.md#stopping)), with two additions:

- **Step 0 releases parked members.** In the same synchronous step that closes scheduling and the bus, the runtime releases every parked member. Its hook returns `False`, and a scheduled member that reaches a boundary after that does not park (step 2 of the seam).
  - Parked members therefore end at once. They neither wait out the grace period nor need the hard stop, so they leave no cancelled loop to make the ledger incomplete.
  - Interventions staged but not committed are recorded with `committed: false` ([P5](#p5-trusted-interventions)).
- **The status of a released member.**
  - Returning `False` makes `react()` return normally, and the spike shows that a member released after a tool-call turn keeps `stop_reason == "tool_calls"`. M1-9's rule would read that as `submitted`.
  - So the hook sets the member's stop cause to `stopped` whenever it returns `False` because of a stop, and the member wrapper gives a normal return with a stop cause that status (`m.stop_cause or _status_for(err)`, where today only the cancelled branch reads it).
  - The one exception is the conversational boundary. A member released after a tool-free reply under `no_tool_calls="stop"` returns as `react(submit=False)` alone would, so it ends `submitted`, with its last reply as its output.

A stop the controller did not request (`swarm_cap`, `swarm_time`) cancels the controller's `run`, and with it any `activate()` call in flight. The member stays running until its next boundary or its next model call, where the soft stop ends it.

#### P5. Trusted interventions

ORBIT changes conversations between turns: pending injections, timed `inject_prompt` compromise, and defenses that rewrite messages before a turn (`dual_llm.py:295-330`). Scheduled mode gives trusted controller code two operations on a member handle, valid only while the member is `pending` or parked:

- **`append(messages, origin=...)`** adds messages to the end of the conversation.
- **`edit(transform, origin=...)`** calls `transform` (sync or async) with a detached deep copy of the conversation, and takes back a new message list.

While a member is pending or parked, its handle holds a staged copy of the conversation. Both operations change the staged copy under the handle's lock, so each sees the effect of the ones before it.

`activate()` waits for any operation in progress, then commits the staged copy. For a pending member, the staged copy is `run()`'s input. For a parked member, the hook returns it to `react()` on resume. Either operation raises `RuntimeError` if the member is running and not parked, or has ended.

**Evidence.** Each operation writes an `intervened` event (`type: "schedule"`) when it commits. The event holds:

- the origin, the member and the activation id;
- found by diffing on message id: the ids of messages appended, removed, and replaced (same id, different content), with content hashes.

An operation cancelled before it completes changes nothing and writes nothing. If the swarm stops while operations are staged, they are recorded with `committed: false`.

**Order at a boundary.** A controller applies its interventions for a member in this order, then activates it:

1. attack and scenario input, through `append`;
2. defenses, through `edit`, which therefore see, and can rewrite, step 1's messages, as ORBIT's pre-turn defenses see its pending injections today (`orchestrator.py:834-861`);
3. `activate()`, which commits the result and then appends any notice.

**What may go through this path.**

- Interventions are configured by the eval author in Python, and are never reachable from member tools.
- They carry eval-authored input (scenario events, attack payloads, compromise prompts) and transformations of the existing conversation (sanitising, redaction).
- They must not carry another member's output. A peer's text keeps its provenance whoever moves it. Peer messages reach members as tool output (decision: Ransom, 2026-10-07), or in the user role only through the messages channel's opt-in, fenced `inject` arm (decision: Ransom, 2026-10-09), which is not this path.
- So ORBIT's dialogue and activity projections, which turn a peer's reply into a user turn (`dialogue.py:160-200`), are not adapted to interventions. They stay on ORBIT's executor.
- Whether eval-authored input may use the user or system role is [open question 1](#open-questions).
- inspect_swarm ships no intervention of its own.

### Against the merged designs

| Design | What this design does with it |
|---|---|
| [swarm-api.md](swarm-api.md#controllers) controllers and handles | **Uses.** Scheduled mode is a `@swarm_controller` driving `activate()`. It **extends** `MemberHandle` with `parked`, `snapshot()`, `activate()`, `append()` and `edit()`, and `SwarmControl` with `sequence()`, as swarm-api.md anticipated. `MemberState` is unchanged: parked members are `running`. The first revision's `ActivationPolicy`, `set_output()` and `policy:<name>` reasons are dropped, in favour of the controller's `run`, swarm-scoring.md's answer rule and `controller:<reason>`. |
| swarm-api.md, "No member text" | **Extends**, for controllers that activate members: `snapshot()` and `edit()` carry text ([P4](#member-handles-and-snapshots); [open question 1](#open-questions)). |
| swarm-api.md, the sibling-parameters table | Of `member(input=, role=, exposure=)`, none remains: `role` is M1's, input goes through `start()` or `activate()`, and exposure is ORBIT's observer's, with no member parameter. The ORBIT row for `routes` is not needed, because ORBIT's channel computes its own recipients. `round_robin` is a `@swarm_controller`, as that table says. |
| [swarm-communication.md](swarm-communication.md#notices) `swarm_on_continue()` and notices | **Uses** as the turn seam, which settles the first revision's question of where the seam lives. **Extends** it with the scheduled path. Notices are unchanged (user role, metadata, kept in the conversation), except that a parked member's notice is computed when it resumes. |
| swarm-communication.md bus and channels | **Uses** for ORBIT's channel, as a user `@swarm_channel`. **Extends** with `Channel.recipients`, the commit sequence in `Delivery` and `Bus.sequence()`, and a public `record_read()`; ORBIT's capacity checks use its `admit()` ([P2](#p2-bus-extensions)). Does not use `messages(routes=)`. |
| swarm-communication.md evidence | **Uses** the kinds and the notices' metadata ids. Adds `record_exposure()`, which writes ORBIT's `model_exposure` events from its observer, and the `schedule` evidence type ([P3](#p3-exposure)). ORBIT's `delivered` and `notified` mean something else, and the [projection](#compatibility-and-migration) maps them. |
| swarm-communication.md `inject` (later) | Not used for ORBIT's channel. ORBIT's `auto` stays ORBIT-specific. |
| [swarm-limits.md](swarm-limits.md#stopping) stopping and statuses | **Uses** the stop procedure and the closed status set. **Extends** step 0 to release parked members, and the member wrapper to give `stopped` to a member the hook released mid-work ([Stopping a scheduled swarm](#stopping-a-scheduled-swarm)). |
| swarm-limits.md member limits and approvals | **Uses** as decided. Parked time inside member time limits, and judges outside token and turn limits, are [behaviour changes](#behaviour-a-port-changes). |
| [swarm-scoring.md](swarm-scoring.md#the-final-answer-first) answers and record | **Follows.** Only submissions are answers. ORBIT's latest-output rule and history projection live in ORBIT's solver wrapper, reading the record in a synchronous `finally` ([P1](#orbits-solver-wrapper)). |
| [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md), per-member sandboxes | **Uses.** The shared default, as ORBIT's. Scenarios that want isolation can use M2's per-member sandboxes, and later users isolation. |
| [swarm-implementation-plan.md](swarm-implementation-plan.md#future-work) | **Follows.** Every pull request here is future work, after the M1 and M2 pull requests it names ([Implementation plan](#implementation-plan)). |

No conflict breaks an ORBIT requirement. The closest is swarm-scoring.md's rule that only submissions are answers, which ORBIT meets on its own side.

### What an ORBIT port changes

These are the changes for the `common/v1` configurations that can move, in the order of the [migration path](#migration-path). A port also needs a supported-configuration check that keeps every combination in [What stays ORBIT-specific](#what-stays-orbit-specific) on ORBIT's executor.

- **Agents.** `build_turn_react_agents` becomes ORBIT's member builder, which the runtime calls per member per sample: it builds `react()` (or `deepagent()`) with `on_continue=swarm_on_continue(no_tool_calls="stop" if not submit else "continue")` and `swarm_tools()` in place of `turn_react`, inside ORBIT's member wrapper ([P1](#p1-members-input-and-output-no-new-api)). Each `AgentSpec` becomes a `member(orbit_member, args={...}, name=..., role=...)` record whose `args` are the spec's fields, so the plan logs them.
- **Executor.**
  - `ScheduledExecutor` and `AgentScheduler` become ORBIT's `@swarm_controller`. `ScheduleRuntime` keeps deciding which agents each step runs.
  - `_execute_agent` becomes an eligibility check on `snapshot()`, followed by `activate()`.
  - asyncio tasks and semaphores become anyio task groups and limiters, or disappear, because inspect_swarm owns member tasks.
- **Outer loop.** `ExperimentScheduler.run_loop` runs inside the controller's `run`. Halt conditions and hooks run between steps. At each boundary, runtime attacks and pending injections go in through `append`, then pre-turn defenses through `edit`, then `activate()`.
- **Channels.** ORBIT's channel tools become an ORBIT `@swarm_channel` with `recipients="roster"`.
  - It sends with `deliver()` and records reads with `record_read()`, keeping its rendered fragments under the read id it returns.
  - It honours `activation_read_cutoff()`.
  - It keeps its channel registry, canonical messages and receipts, with the record ids and commit sequences the bus assigns.
- **Delivery and observation.**
  - `CommunicationDeliveryModel` is removed: `notify` uses the swarm's notice.
  - `CommunicationModel` stays as the exposure observer, joining input to reads and notices as [P2](#p2-bus-extensions) and [P3](#p3-exposure) leave to the port, and reporting through `record_exposure()`.
  - `CommunicationState` is completed at finalisation from the swarm's evidence ([Compatibility](#compatibility-and-migration)).
- **Defenses.**
  - Tool gates become per-member approval policies or sentinel `BeforeToolCall` protocols.
  - Safety-prompt filters become part of the member's system prompt.
  - Input and output filters stay as ORBIT's wrapper until sentinel's generate stages can rewrite and resample.
- **Output.** `output_agent` and `history_projection` move into ORBIT's solver wrapper around the swarm, whose synchronous `finally` reads the result record and the member-wrapper keys ([P1](#orbits-solver-wrapper)).
- **Dependencies.** ORBIT depends on inspect_swarm, which needs inspect_ai 0.3.283 or later (it tracked inspect_ai `main` until 2026-10-22).

### What stays ORBIT-specific

- Scenarios, datasets, attack and defense registries, scorers, judges, the DCOP phase controller, the YAML and CLI wrapper, and the viewer overview.
- The `legacy/v1` profile, which keeps `turn_react` and ORBIT's executors:
  - the `legacy` and `tool_step` quanta;
  - the `summary` and `peer_messages` observation modes;
  - cross-agent memory injection;
  - legacy channel formats.
- **`auto` delivery, and dialogue and activity projections.**
  - They put peer text in a conversation outside a tool result.
  - The later `messages(delivery="inject")` is a mode of inspect_swarm's messages channel, a fenced user message stored at a boundary. It is neither a mode of a user channel nor ORBIT's per-request system message.
  - So the `common/v1` interleaved conversations that use dialogue projections (`runtime_profiles.py:57-72`) stay on ORBIT's executor.
- **Timed `replace_agent` compromise.** A parked `react()` invocation keeps running its original closure, and this design gives no way to swap a member's agent mid-sample. Pre-deployment replacement is roster data, and moves.
- **`TopologyExecutor` and persistent delegation sessions.** A root agent with delegates can be one swarm member, if ORBIT wants the ledger.
- **ORBIT's own scheduling logic**, all inside ORBIT's controller: plan amendment with audited revisions, superstep snapshots of activity summaries, and convergence halting.
- **Model-boundary defenses, and the post-filter observer they require**, until sentinel's generate stages can rewrite requests and resample outputs.

### Behaviour a port changes

A port does not reproduce every ORBIT behaviour exactly. Runs from the two runtimes should not be pooled unless these changes are accepted or controlled for. Whether Ransom accepts them for an opted-in port is [open question 2](#open-questions).

- **Notices persist, in the user role.**
  - ORBIT recomputes the notice for every request as a system message, and never stores it (`delivery.py:218`, `:297-305`, `:336-343`).
  - inspect_swarm appends a `ChatMessageUser` with notice metadata at a turn boundary, and keeps it ([Notices](swarm-communication.md#notices)).
  - Prompts and cache keys differ.
- **No continue prompt on resume.** `turn_react` adds "Please continue with your next action." on every resume after a tool result. A parked `react()` member adds nothing, because it never left its loop.
- **Overflow recovery is not a boundary.** Under `model_step`, `turn_react` yields after a context overflow (`turn_react.py:255-258`). `react()` does not call `on_continue` after a recovered overflow, so that activation runs one more generation.
- **One span per member.** ORBIT records one agent span per activation. inspect_swarm records one per member, with activation events inside it, and a named timeline per member (swarm.md, Member views).
- **Member time limits include time parked at the seam**, because a member time limit is an Inspect `time_limit` around the whole member ([What each kind does](swarm-limits.md#what-each-kind-does-in-a-swarm)). Under a scheduled controller, use token, cost or message limits per member. A sequential controller has at most one member's calls in flight, because background descendants are refused ([Supported members](#supported-members)), which bounds overshoot by one member's fan-out.
- **Judge usage in tool gates.** ORBIT's tool-gate judges run inside the tool call, and are charged to all of the member's limits. Approval policies run with token and turn metering suspended, so the same judge no longer counts against member token or turn limits. Its cost still counts against cost caps, shares and member cost limits, and is attributed to the member in the ledger; its tokens appear as unattributed ([Interactions](swarm-limits.md#interactions)).
- **The swarm's answer is a submission.** If a schedule ends before a submitting output member has submitted, the swarm's own output is empty, with `reason="no_submissions"`. ORBIT's solver wrapper restores ORBIT's choice from the record, including after an outer limit or a cancellation ([P1](#orbits-solver-wrapper)). Analysis that reads the swarm's `final` record sees the swarm's rule, not ORBIT's.
- **Read output format.** inspect_swarm's own read tools fence each message under a bus-stamped sender line. ORBIT's channel keeps its JSON lines, where JSON string escaping is the fence.

### Migration path

Each step names the configurations it moves. Everything else keeps running on ORBIT's executor, with its existing machinery (`turn_react`, the notice and observer wrappers), until a later step.

1. **After M1: concurrent `completion` runs without channels.**
   - These are configurations that are one concurrent `completion` step, with no channels. ORBIT's controller starts every member with `start()`, passing group goals as input, and returns `all_done`.
   - ORBIT's solver wrapper takes the output member's output and projects the saved conversations.
   - They gain the drain, the realized-cost ledger and per-member records.
   - User controllers are experimental in the first release, but M1 runs them.
2. **After M2 and the bus extensions (F-1, F-2): the same runs, with channels.**
   - ORBIT's channels become its `@swarm_channel`, with M2's notices, and ORBIT's observer reports exposure through `record_exposure()`. This works because these runs execute inside a swarm, where the member ContextVar exists.
   - Scheduled runs do not move in this step. Their executor calls `run()` per activation outside any swarm, so the bus could not bind a sender.
3. **After scheduled mode (F-3, F-4): ORBIT's scheduler as a controller.**
   - Scheduled `common/v1` runs move, except the combinations in [What stays ORBIT-specific](#what-stays-orbit-specific).
   - `turn_react` and the notice wrapper are no longer used for them, and tool gates move to approval policies or sentinel protocols.
   - Members with defense filters keep ORBIT's filter wrapper, with its observer below it.
4. **When sentinel's generate stages can rewrite and resample: defenses as protocols.**
   - Input and output filters become sentinel protocols.
   - Once it is confirmed that the transcript then records the rewritten request, the filter wrapper goes away. `BeforeGenerate` already runs before the cache lookup, so caching would no longer need to be refused.
   - ORBIT's observer does not: it stays the only source of `record_exposure()` reports and coverage ([P3](#p3-exposure)). Without the wrapper, response caching is no longer refused, so the observer must see cache hits as well as misses: a `ModelAPI` observer alone never sees a hit, because Inspect returns a cached result before calling the API. It stays a `Model`-level observer that also records cache reads, as `CommunicationModel` does today (`ORBIT: orbit/communication/model_observer.py:117-150`), or becomes an ORBIT-owned observing sentinel stage that runs on both paths. Either way it reports the same records and notices, successful generations only, and declares coverage the same way; the ORBIT work chooses the mechanism. The port requires a genuine cache-hit test through `Model.generate()`: hits reported with `cached: true`, successful generations only, and no duplicate report on a miss. The wrapper is retired only when its replacement reports.
   - Input monitoring that only detects or terminates could use the observing stages already on inspect_ai `main`.

Through all four steps, `legacy/v1` stays on ORBIT's own executors.

## Alternatives considered

**A turn gate in inspect_ai's agent channel.** Add an async gate that producers register and `before_turn` awaits (`channel.py:432-453`).

- It would cover the first turn, recovered-overflow turns and every agent that uses the channel facade, without building the agent with `swarm_on_continue()`.
- But it is an inspect_ai behaviour change. And swarm-communication.md decided that the swarm never posts to agent channels, and that ordinary notices stay on `swarm_on_continue()` even once a `Steer` item exists (decision: Ransom, 2026-10-13). A scheduled seam elsewhere would split the turn boundary in two.
- Kept as the path if users need to schedule agents that cannot take the hook, as swarm-communication.md keeps the same question for notices.

**A separate scheduling hook** (`swarm_continue()`, this design's first revision). It is equivalent, but a member would then need two composed hooks, and their order would matter. Folding the scheduled path into `swarm_on_continue()` keeps one hook and one wiring check. Rejected.

**Gate in `on_before_model_generate`.** A process-wide hook that already sees every generation.

- It runs inside the connection slot (`_model.py:1058`, `:1505`), so a member waiting there would hold a slot. With `max_connections` at or below the number of parked members, nothing else could run.
- It also fires for every model call in the process, including scorers and judges, and once per retry.
- Rejected.

**Keep ORBIT's model: one `run()` per activation on a stored `AgentState`.**

- Closest to ORBIT today, and every activation is its own span.
- But it needs clean re-entry of `react()`, an inspect_ai change that swarm.md lists for persistent members. It also loses per-run state (attempt count, compaction, MCP connections, the agent channel), and the bus's member binding is per activation, so every re-entry would rebind.
- Rejected in favour of the seam, which needs no inspect_ai change.

**Notices and exposure by wrapping `Model`, as ORBIT does.**

- Works today. It handles caching correctly when done with ORBIT's care, and a `ModelAPI`-level observer sees the request after any filter below it.
- But each wrapper must preserve the model's identity and effective configuration through a private API, the wrappers' order matters, and inspect_swarm would carry this for every member.
- Rejected for inspect_swarm's own machinery: turn-boundary notices need none, and the swarm computes no exposure. ORBIT keeps its observer, which reports through `record_exposure()` ([P3](#p3-exposure)).

**Exposure computed by the swarm**, live through a transcript subscription (the first revision) or after the drain by a walk over member spans (later revisions). Either duplicates what ORBIT's observer already sees, and only the observer sees the request after ORBIT's filters. Rejected with M2's own exposure event ([swarm-communication.md Alternatives considered](swarm-communication.md#alternatives-considered); decision: Ransom, 2026-10-15).

**Bus routes for ORBIT's channel.** Pass ORBIT's communication edges as routes, and let the bus drop recipients. But routes are a messages-channel parameter, ORBIT already intersects edges with membership when it computes recipients, and a generic routes interface for user channels would be one more surface for one user. Rejected.

**A `set_output()` on the controller** (the first revision). It would reproduce ORBIT's output selection inside the swarm. But it contradicts swarm-scoring.md's rule that only submissions are answers, and ORBIT can apply its own rule after the swarm. Rejected.

**A live, mutable state handle for interventions.** Give trusted code `MemberHandle.state`, and let it change `messages` while the member is parked.

- Simplest for ORBIT's pre-turn defenses, which already mutate `AgentState.messages` in place.
- But `AgentState.messages` is a mutable list of mutable messages (`src/inspect_ai/agent/_agent.py:44`). A reference taken while parked stays writable after the member resumes, and in-place edits pass through no code that could record them or check the member's state.
- Rejected in favour of snapshots, plus `append` and `edit` on a staged copy ([P5](#p5-trusted-interventions)).

**Absorb ORBIT's scheduler into inspect_swarm.** Ship plans, amendments, superstep and quanta as inspect_swarm features.

- One implementation for every scheduled harness.
- But it is the largest option, and it turns ORBIT-specific policy into inspect_swarm API before a second user asks for it.
- Rejected. inspect_swarm ships the handles and `round_robin`.

**Share vocabulary only.** inspect_swarm reuses ORBIT's names and evidence kinds, and ORBIT keeps its runtime.

- Cheapest.
- But ORBIT keeps the fork and the wrappers, and the two never share a ledger or evidence.
- Not enough for the task, though it remains what happens if ORBIT stays on its own runtime.

## Compatibility and migration

- **inspect_swarm** has no released API, and everything here comes after its first release. The additions are:
  - on the bus: `Channel.recipients`, `Delivery.seq`, `Bus.sequence()`, `record_read()`, `notice_text()`, `record_exposure()` and `record_exposure_coverage()`;
  - in evidence: ORBIT's `model_exposure` events written through `record_exposure()`, per-member coverage declarations, and the `schedule` type with its kinds `activation_started`, `activation_ended` and `intervened`, all inside the versioned `InfoEvent` payloads;
  - in scheduled mode: the scheduled path of `swarm_on_continue()`, the handle methods, `SwarmControl.sequence()`, `activation_read_cutoff()`, `ActivationResult`, `MemberSnapshot` and `round_robin`;
  - the limits deep dive's member wrapper reading the stop cause on a normal return.

  Every default keeps the behaviour of a swarm that uses none of them.
- **inspect_ai**: nothing beyond M2's own prerequisites. The seam uses `on_continue`. If the ORBIT work names read results through an inspect_ai `tool_result_metadata()` helper ([P2](#p2-bus-extensions)), that helper is a new public inspect_ai API; the other options need no inspect_ai change, and full `combined_from` lineage is optional.
- **ORBIT logs.** Logs ORBIT has already written are unaffected, and keep their own readers. A ported run writes inspect_swarm's evidence (`source="inspect_swarm"`). It should also record which runtime produced it, because of the [behaviour changes](#behaviour-a-port-changes).
- **ORBIT's `CommunicationState`.** Its consumers, the collusion scorer and the viewer overview, read it from the store. A port keeps writing it at finalisation, with each field from one source:

  | Field | Source on the swarm runtime |
  |---|---|
  | `version` | `1` when every member's coverage is known; `None` (evidence unavailable) when the swarm's evidence is missing |
  | `channels` (ids, readers, writers, `private`) | ORBIT's channel, which registers channels as today, including secret channels registered by attacks |
  | `messages` | ORBIT's channel at send time, with the record id, the commit sequence (`seq`) and the recipients that `deliver()` returns. `session_id` is the member's agent span (sentinel's `conversation`), `invocation_id` the activation id, and `turn` the controller's outer turn |
  | `receipts` (per-message `rendered_messages`) | ORBIT's channel, which keeps the fragments it rendered under the read id `record_read()` returns ([P2](#p2-bus-extensions)). The read id and tool span come from the swarm's `read` event; the tool-call and event ids come from ORBIT's read-time capture (`ORBIT: orbit/execution/provenance.py:17-49`) where it finds one unambiguously, and are `None` otherwise |
  | `events`: `sent`, `read` | The swarm's `sent` and `read` events, with the same record ids; native ids as for receipts |
  | `events`: `model_exposure` | The `model_exposure` events ORBIT's observer wrote through `record_exposure()` for records |
  | `events`: `notified` | The `model_exposure` events with `notice` set. These match ORBIT's meaning: the notice's original text reached a successful input (`delivery.py:370-380`). The swarm's own `notified` events are not projected |
  | `events`: `delivered` | None: `auto` and dialogue projections do not run on the swarm runtime. The swarm's `delivered` events are not projected |
  | `observation_enabled_sessions` | Members whose observer declared complete coverage through `record_exposure_coverage()`. Any other member is left out, so the scorer reports its reads as uncovered (`collusion_scorer.py:108-111`) instead of counting zero exposures |
  | `successful_input_sessions`, `observed_sessions`, `observed_invocations` | Members whose observer reported at least one successful generation; member spans; activation ids |

  Native links come from where they are recorded: span ids from the swarm's events, model-event ids from the observer's `record_exposure()` reports, and tool-call ids from ORBIT's read-time capture; a link no source records unambiguously is `None`. On the ORBIT side, the viewer's event list is changed to accept `source="inspect_swarm"` evidence as well as `orbit.communication` (`_render.py:143`), rather than duplicating events.
- **Dependency ranges.** ORBIT accepts `inspect-ai>=0.3.263,<0.4` and tests one locked version (`ORBIT: pyproject.toml`). inspect_swarm requires the inspect_ai release that carries its prerequisites, so a port inherits inspect_swarm's floor, which is narrower.
- **Python and concurrency.** Both require Python 3.11+. inspect_swarm runs on asyncio and trio, so controller code must use anyio.

## Security

- **Peer content** reaches a member only as tool output, from a channel's read tools. In the user role, it arrives only through the messages channel's opt-in `inject` arm, which ORBIT's channel does not offer. Neither the bus nor an intervention inserts it anywhere else, and ORBIT's `auto` delivery and its dialogue and activity projections are not offered on the swarm runtime.
- **Sender identity** is stamped by `deliver()` from the member ContextVar. ORBIT's channel cannot set it. Records with no member behind them come only from channel code, through `deliver_internal()`, under the reserved sender `swarm`.
- **Recipients that are not running** (`recipients="roster"`) receive records into channel state only. Nothing runs or wakes because of them, and the bus still refuses every send once the swarm is stopping.
- **Controllers and interventions** are eval-author Python running in the runner process, as ORBIT's extensions are ("Registration and channel permissions are not a plugin sandbox", `ORBIT: docs/security-extensions.md`).
  - They are never exposed as tools.
  - They act only on a staged copy of a pending or parked member's conversation, and every committed change writes an `intervened` event with its origin and the ids of the messages appended, removed or replaced.
  - That a transformation carries no peer output is a contract on eval code, which inspect_swarm cannot check. The event makes a breach visible.
- **Member text to controllers.** Only controllers that activate members get it, through `snapshot()` and `edit()`. That widens what swarm-api.md lets a controller see ([open question 1](#open-questions)). A controller cannot write to a member except through `append()` and `edit()`, both recorded.
- **Defenses before the next generation.** At a boundary, defenses run after attack input is appended, so a sanitising defense sees an injection before the member's next generation, as in ORBIT today.
- **Exposure is ORBIT's observer's claim.** The bus checks that reported ids belong to the run and the calling member, and records the report; it joins nothing by text, and a member without a complete observer has unknown exposure, never zero.
- **Notices** list only channel ids configured by the task or registered by trusted code, never ids or names a member created. This keeps swarm.md's notice rule.
- **Metadata ids** on swarm messages come from bus state, never from a payload, so a reproduced fence cannot pass for a read in the observer's join.
- **Controller decisions are never read from model output.** ORBIT installs no model-facing schedule editor (`plans.py:1-6`), and a port must keep it that way.

## Testing

All in inspect_swarm, with mockllm, on asyncio and trio, with no network and no Docker:

- **The seam** (`tests/test_scheduled.py`):
  - a `round_robin` run reproduces the spike's order, with one system message and no added user messages;
  - a concurrent step waits at its barrier;
  - a `completion` activation of a submitting member runs to submission;
  - a `no_tool_calls="stop"` member gives two tool-free replies in two activations, with no continue prompt added, under both quanta. Started with `start()`, the same member ends at its first tool-free reply;
  - an inner hook keeps its `True`, string, `AgentState` and `False` semantics, unscheduled and at a scheduled boundary. A returned string, and a returned state's extra message and output, are in the next model input after the boundary, ahead of an appended intervention and a notice. `True` after a tool-free reply of a submitting member adds `react()`'s continue prompt;
  - a record delivered while a member is parked is in the notice it gets at resume;
  - a submission ends the member, and a nested agent built with `swarm_on_continue()` does not park;
  - a member without the hook, under a `model_step` controller, runs one activation, and `scheduled_unwired` is recorded;
  - outside a scheduled activation, the hook passes swarm-communication.md's notice and continuation cases unchanged.
- **Handles and stopping** (`tests/test_scheduled.py`):
  - `activate()` raises on a running, ended or `start()`-started member, and once stopping;
  - `activate()` raises `ValueError` for `deepagent(background=True)` and `deepagent(background=4)` members, including under `round_robin`, and after M2-5 the same members still run under `start()`;
  - a parked member is `running` to `list_members()` and to the bus's liveness check;
  - a member limit returns outcome `ended` with `member_limit:<type>`;
  - one member error cancels its siblings and the controller, and propagates;
  - the controller returning `controller:max_rounds` releases parked members at step 0:
    - a mid-work member ends `stopped`, with no hard stop and a complete ledger;
    - a member parked at a conversational boundary ends `submitted`, with its last reply;
  - a swarm cap reached while members are parked releases them the same way, and cancels the controller's pending `activate()`;
  - notices do not start activations;
  - an eligibility check on `snapshot()` reproduces ORBIT's `max_messages` boundary: with a one-message input and a cap of two, an eligible activation runs to its answer;
  - `snapshot()` works in every state, and after a hard stop is marked `interrupted`.
- **Bus extensions** (`tests/test_bus.py`):
  - a `recipients="roster"` test channel delivers to pending and ended members, and records their states in `delivered`, while the default channel still rejects them;
  - every send is refused once stopping;
  - the commit sequence increases monotonically across channels in commit order, and `Delivery.seq`, `delivered.seq` and `Bus.sequence()` read inside `apply()` agree; `SwarmControl.sequence()` returns the latest committed;
  - a record held by a test policy while a later record commits gets the higher commit number, and a read cutoff taken from `SwarmControl.sequence()` between the two commits hides it, although its record id is the lower;
  - a read tool honouring `activation_read_cutoff()` hides later records;
  - `record_read()` called inside a test channel's read tool writes the `read` event in the tool span, returns the read id; the way the observer finds the result message is tested with the option the ORBIT work chooses ([P2](#p2-bus-extensions)).
- **Exposure reporting** (`tests/test_comm_evidence.py`):
  - `record_exposure()` from a test observer writes `model_exposure` with the read, record and `ModelEvent` ids and the activation id, and `cached: true` for a cache replay; it refuses a read id, record id or notice id from another run or another member;
  - a member whose observer declared complete coverage is in `observation_enabled_sessions`, and one that did not is left out;
  - `notice_text()` returns a notice's text as created, by notice id and by message id, after a trusted `edit()` changed the message under the same id; a test observer comparing with it reports an intact notice and does not report the edited one; the `notified` event's hash and length match the text;
  - on a mock `ModelAPI` that combines user messages, a notice followed by a continuation string, and `[notice A, continuation, notice B]`, each reach the observer as one message, and a test observer that joins it to the loop's `notified` events finds each notice's `notice_text()` within it and reports both notices.
- **Interventions** (`tests/test_scheduled.py`):
  - `append` and `edit` raise while the member runs, and after it ends;
  - an `edit` that deletes a message, replaces one with the same id and new content, and appends one writes one `intervened` event listing each;
  - an operation cancelled midway changes nothing, and operations staged at a stop are recorded with `committed: false`;
  - an injection appended with `append` and tagged untrusted is rewritten by a following `edit` before the member's next generation.
- **ORBIT-shaped conformance tests** (`tests/test_orbit_shape.py`), with no ORBIT dependency:
  - a controller with sequential and concurrent steps, over members using a reader-and-writer test channel, checks ORBIT's documented rules: messages do not activate members, submitted members are not activated again, step-start cutoffs hide same-step sends, including one whose policy began before the step and committed during it, a channel capacity checked in `admit()` lets exactly one of two concurrent posts held by the policy take the last free place, with the other's refusal in its `sent` event, and a concurrent failure cancels and joins siblings;
  - a test helper builds a `CommunicationState`-shaped index from the swarm's evidence by the projection table above. It checks private-channel messages, per-session exposure pairs, notices with ORBIT's meaning (no `notified` for a same-id redacted notice), and uncovered receipts when coverage is incomplete;
  - a test solver wrapper shaped like ORBIT's sets a never-submitted output member's output and a projected history from the record and saved conversations, after a normal controller stop, after an outer sample message limit, and after cancellation by a sample time limit; in the last two the original error still ends the sample, and the projection is in the logged `TaskState`, where a separate solver step after the swarm would leave the swarm's provisional answer.

## Implementation plan

Future work, after M1 and M2, in the plan's "Later, any order" menu. Each step is one pull request, named `F-n` here, with the M1 and M2 pull requests it needs ([swarm-implementation-plan.md](swarm-implementation-plan.md)):

1. **F-1. Bus extensions.**
   - Scope: `Channel.recipients` with the step-0 and commit-check rules; the commit sequence as `Delivery.seq` and `Bus.sequence()`; public `record_read(records) -> str`; and the way a read result is named for ORBIT's observer, as [P2](#p2-bus-extensions) leaves to the ORBIT work (for option (c), the stamping in `swarm_on_continue()`, `_bus/_notice.py`).
   - Files: `src/inspect_swarm/_bus/_record.py`, `_bus/_bus.py`, `src/inspect_swarm/_channel/_channel.py`, `src/inspect_swarm/_evidence.py`.
   - Tests: the bus-extension cases in `tests/test_bus.py`.
   - Depends on: M2-6 (bus core), and M2-8 (the first channel's read events).
2. **F-2. Exposure reporting.**
   - Scope: `record_exposure()` and `record_exposure_coverage()`, writing ORBIT's `model_exposure` events with the activation id and `cached` fields after checking the ids against the run and the member; the notice message id in `notified`; each notice's original text kept in memory, `notice_text()`, and the text's SHA-256 and length in `notified`.
   - Files: `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_bus/_bus.py`, `src/inspect_swarm/_bus/_notice.py`.
   - Tests: the exposure-reporting cases in `tests/test_comm_evidence.py`.
   - Depends on: M2-9 (notices), F-1.
3. **F-3. Scheduled mode.**
   - Scope:
     - the scheduled path of `swarm_on_continue()`;
     - `activate()`, `parked`, `snapshot()`, `ActivationResult` and `MemberSnapshot`;
     - `SwarmControl.sequence()` and `activation_read_cutoff()`;
     - the `schedule` evidence type and `scheduled_unwired`;
     - stop step 0 releasing parked members, and the member wrapper's stop cause on a normal return;
     - the refusal of background-enabled `deepagent()` members in `activate()`;
     - `round_robin`.
   - Files: `src/inspect_swarm/_bus/_notice.py`, `src/inspect_swarm/_controller/_control.py`, `src/inspect_swarm/_controller/_schedule.py` (new), `src/inspect_swarm/_controller/round_robin.py` (new), `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_entrypoint.py`.
   - Tests: the seam and handle cases of `tests/test_scheduled.py`.
   - Depends on: M1-4 (handles and `stop()`), M1-5 (the member's `AgentState` reference), M1-9 (the status rule it amends), M2-9 (`swarm_on_continue()`), and F-1 for the commit sequence.
4. **F-4. Trusted interventions.**
   - Scope: staged conversations, `append` and `edit`, commit at `activate()`, and `intervened` events.
   - Files: `src/inspect_swarm/_controller/_schedule.py`, `src/inspect_swarm/_evidence.py`.
   - Tests: the intervention cases of `tests/test_scheduled.py`.
   - Depends on: F-3.
5. **F-5. ORBIT-shaped conformance tests.**
   - Files: `tests/test_orbit_shape.py`.
   - Depends on: F-1 to F-4.

A port of ORBIT follows the [migration path](#migration-path). It is ORBIT work, and is not planned here.

## Open questions

1. **Scheduled controllers and member conversations.** Under [P4](#member-handles-and-snapshots) and [P5](#p5-trusted-interventions), a controller that activates members can do two things:
   - read member text through `snapshot()`, which swarm-api.md otherwise withholds from controllers;
   - append eval-authored input at a boundary.

   ORBIT needs both. Its convergence halting and attack-outcome checks read outputs, and its pre-turn defenses rewrite conversations. Its pending injections are user messages, and its timed `inject_prompt` compromise is a system message. Peer output is excluded either way ([P5](#p5-trusted-interventions)).
   - (a) Allow both, for controllers that activate members only. Input may be in any role, and every change is recorded as `intervened` with an origin.
   - (b) Snapshots carry counts and statuses only, and appended input may not use the user or system role. ORBIT's convergence halting, attack-outcome checks, injections, `inject_prompt` compromise and `dual_llm` defense then stay on ORBIT's executor.

   Recommendation: (a). The 2026-10-07 and 2026-10-09 decisions govern what members receive from each other. An eval author's scenario and attack input is part of the eval's design, as a sample's input is. The controller is trusted code, and the record keeps every change distinguishable.
2. **The trajectory changes of a port.** These apply to ORBIT runs that opt into the swarm runtime ([Behaviour a port changes](#behaviour-a-port-changes)):
   - notices that persist, as user messages, with different cache keys;
   - no resume prompt;
   - an extra generation after a recovered overflow;
   - one span per member instead of one per activation;
   - member time limits that include parked time;
   - tool-gate judges no longer counted against member token and turn limits (their cost still counts);
   - the swarm's own answer empty when the output member never submitted, unless ORBIT's solver wrapper restores it.

   Recommendation: accept them, with each run recording its runtime so that results from the two runtimes are never pooled silently.

3. **How ORBIT's observer finds a read's result message** ([P2](#p2-bus-extensions), "Naming a read in model input"). Options: (c) stamping in `swarm_on_continue()`, which covers only loops that run the hook; an inspect_ai `tool_result_metadata()` helper in inspect_ai; joining through the `read` event; keeping ORBIT's current tool-call-id join. Leading: (c) (Ransom, 2026-10-16), to be decided when the ORBIT work starts.
4. **Notices inside combined user messages** ([P3](#p3-exposure), "Combined user messages"). Options: join a combined message to its notices through the loop's `notified` events, in order; or rely on full `combined_from` lineage, an optional standalone inspect_ai fix. To be decided when the ORBIT work starts.

Issues found in the ORBIT design from 2026-10-16 on are recorded here with their evidence and options, to be resolved when the ORBIT work starts, and do not block the swarm's own design (decision: Ransom, 2026-10-16: "Stop iterating further on the ORBIT design - leave any issues as open questions in the orbit doc to be resolved later.").

Settled since the first revision: where the turn seam lives. It is `swarm_on_continue()`. swarm-communication.md places notices there, and never in the agent channel, and ordinary notices stay there even once a `Steer` item exists (decision: Ransom, 2026-10-13). So the seam is the same hook, and the agent-channel gate is the alternative for agents that cannot take it.

## Not this design

- **`tool_step` on `react()`.** Rejecting a multi-call batch before any tool runs needs a `react()` change, or a `parallel_tool_calls=False` approximation that providers honour unevenly.
- **Replacing a member's agent mid-sample**, which timed `replace_agent` compromise would need.
- **Scheduling a member's background descendants.** A scheduled member parks only its top-level loop, so background children would keep running between activations. This design refuses such members ([Supported members](#supported-members)); a turn gate over a member's descendants would be its own design, on top of the per-member background owner D-2 designs ([swarm-background-members.md](swarm-background-members.md)).
- **Conversation identity for descendants.** ORBIT's observer sees each generation with the member ContextVar of the member it belongs to, so its reports are attributed to that member, nested agents included. Per-descendant conversation identity, which ORBIT's persistent delegation tracks, is not designed here. D-1 decided that descendants use the shared-memory channel as their member, each call attributed to its own loop ([swarm-memory-channel.md Descendants](swarm-memory-channel.md#descendants)).
- **More built-in controllers** beyond `round_robin`: superstep, explicit plans, Concordia-style engines, AgentsNet's synchronous rounds.
- **Board channels with readers and writers.** The board is later work in swarm.md. When it is built, it should take ORBIT's `readers` and `writers` names and `recipients="roster"`, so that ORBIT's channel could become configuration.
- **Sentinel generate stages that rewrite and resample.** The stages on inspect_ai `main` only observe or terminate. ORBIT's model-boundary defenses wait for protocols that can rewrite a request or resample an output.
- **The ledger under a bounded transcript.** The ledger's call counts read member spans after the drain. With `INSPECT_TRANSCRIPT_BOUNDED` set and no history provider, it sees a truncated history, and its claims should say so.
- **The read cutoff for the built-in channels.** Only ORBIT's channel honours `activation_read_cutoff()`. The shared-memory and messages channels could adopt it if another scheduled harness asks.
