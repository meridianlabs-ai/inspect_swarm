# ORBIT on inspect_swarm

Status: proposed, 2026-10-08. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

A deep dive on one topic of [swarm.md](swarm.md): how ORBIT, the Inspect-based multi-agent security framework compared in [Relationship to ORBIT](swarm.md#relationship-to-orbit), could run on inspect_swarm. It maps ORBIT's concepts onto inspect_swarm's members, substrate, controller, bus and observer; says what inspect_swarm must provide so that ORBIT runs without forking `react()` or wrapping `Model`; says what a port of ORBIT changes; and gives a migration path and what stays ORBIT-specific.

Three sibling deep dives run in parallel and are referenced, not designed, here: limits, scoring, and inter-agent communication. Ransom's decisions of 2026-10-07 in swarm.md stand: Python 3.11+; inspect_ai internals may be used; limits stay soft; M1, then M2, then the rest in any order; a shared sandbox by default; monitoring through inspect_sentinel; red-team features optional; peer messages are model output, delivered as tool output with distinct provenance and never as user messages; the transcript representation is open.

Code references are to ORBIT at `588b3035` (the revision swarm.md cites), inspect_ai at `aa20052a`, and inspect_sentinel at `0f9b5c71`. Paths are relative to each repository's root; ORBIT paths are prefixed `ORBIT:`.

## Why

swarm.md recommends that inspect_swarm "align with ORBIT and aim to be a substrate it could run on, not to absorb it", and lists in one line what it should provide: turn boundaries and a scheduler, per-member tool and model wrapping, and a communication hook that does not wrap `Model`. That line is a claim, not a design. This document checks it against ORBIT's code and makes it concrete.

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
- Offering ORBIT's `auto` delivery mode, or any path by which the bus puts peer text in the user role.
- Deciding whether or when ORBIT adopts inspect_swarm.

## Current behaviour

### ORBIT

**Roster and roles.** `AgentSpec` (`ORBIT: orbit/configs/setup.py:79-164`) has a name, an informational `role` label, an optional model and temperature, a system prompt, tool names, `is_compromised`, a per-agent `max_messages` (agents at the limit are skipped, `agent_scheduler.py:539-551`), and `submit` (whether the agent has a terminal submit tool). `AgentGroup` gives several agents a shared goal that replaces the sample input as their first user message (`ORBIT: orbit/configs/execution.py:22-40`, `agent_scheduler.py:243-254`).

**Invocation and routes are separate.** `TopologyEdge` says how one agent invokes another (`handoff`, `tool` via `as_tool`, or `direct_run`; `setup.py:230-256`). `CommunicationEdge` says who may message whom, intersected with channel membership (`setup.py:223-228`, `:273-278`). Invocation edges never grant message permission.

**Channels.** `ChannelConfig` has an id, `readers`, `writers` and a `private` label (`ORBIT: orbit/configs/communication.py:12-29`). The tools are `channel_post`, `channel_read` (cursor and bounded pages) and `channel_list` (`ORBIT: orbit/communication/tools.py:88-202`). The runtime binds the sender from a ContextVar identity, so a model cannot choose it (`ORBIT: orbit/communication/runtime.py:51-124`). A send with no permitted recipient fails (`runtime.py:246-252`); an oversized send becomes a `ToolError` for the sender (`tools.py:128-129`). State lives in Inspect store models (`ORBIT: orbit/communication/state.py:120-155`). Attacks register further channels at run time, such as secret coalition channels (`orchestrator.py:240-268`).

**Delivery modes.** `CommunicationConfig.delivery` is `poll`, `notify` (the default) or `auto`, overridable per agent (`communication.py:54-75`). For `notify` and `auto` the outermost `Model` wrapper derives one `ChatMessageSystem` per request, appended after the conversation and never stored in it (`delivery.py:203-224`, `:234-283`, `:330-343`). A `notify` notice lists channel ids and unread counts and "contains no message text"; an `auto` message carries bodies under an "untrusted messages" header. Dialogue projections, a separate adapter, insert a peer's completed text into the recipient's conversation as a user turn (`ORBIT: orbit/communication/dialogue.py:1-8`, `:193`). The legacy `peer_messages` and `summary` observation modes also inject user messages (`agent_scheduler.py:685-803`).

**Scheduled activation.** `ExecutionConfig` (`execution.py:109-207`) selects round-robin, superstep or interleaved modes, or an explicit plan (`ORBIT: orbit/configs/scheduling.py`, `ORBIT: orbit/execution/plans.py`). A plan step lists agents, runs them in order or concurrently with a barrier, and may set its own quantum and visibility. Quanta are `legacy`, `model_step` (one generation and its tool batch), `tool_step` (one tool call; a batch of several is rejected before any runs) and `completion` (`scheduling.py:13`, `turn_react.py:462-467`). The public task helper defaults to scheduled `model_step` execution (`ORBIT: docs/architecture.md`, "Executors and conversations"). A trusted `after_activation` callback may amend pending steps with an audited revision (`plans.py:222-267`). Messages never activate an agent: "Sending never changes the schedule or wakes a submitted agent" (`ORBIT: docs/runtime-architecture.md`, "Channels and visibility"). `visibility="step_start"` freezes channel reads at the step's start sequence (`runtime.py:36-48`, `:292-297`).

**The outer loop.** `ExperimentScheduler.run_loop` repeats turns until a halt condition: `max_turns`, wall-clock `max_time_seconds`, convergence of outputs, attack success, or custom conditions (`ORBIT: orbit/scheduler/scheduler.py:164-245`, `ORBIT: orbit/configs/scheduler.py`). Each turn activates runtime attacks, applies pending injections as user messages tagged `untrusted_observation` (`orchestrator.py:834-849`), runs pre-turn defense hooks over every agent's `AgentState` (`:851-861`; `ORBIT: orbit/defenses/dual_llm.py:295-330` rewrites messages there), runs the executor, and evaluates attack outcomes (`:884-893`).

**Compromised members.** `CompromisedAgentAttack` either appends the attacker's payload as a system message (`inject_prompt`) or replaces the agent with a `react()` built from the payload and the original tools (`replace_agent`) (`ORBIT: orbit/attacks/compromised/compromised.py:35-75`).

**Defenses.** Tool gates wrap each tool and run `on_tool_call` before the callable, turning a denial into a `ToolError` (`ORBIT: orbit/defenses/boundaries.py:191-294`). Input filters (safety prompts, memory injection, input monitoring with redaction) run in a `ModelAPI` wrapper, and output checks with resampling in a `Model` wrapper (`model_filter.py:127-212`, `boundaries.py:141-185`, `:296-343`).

**Evidence.** Kinds are `sent`, `read`, `notified`, `delivered` (content inserted into a conversation) and `model_exposure` (original content survived into a successful generation's input) (`state.py:57-83`, `ORBIT: docs/communication.md`). Each is an `InfoEvent` with source `orbit.communication` (`runtime.py:190-227`) and carries agent, session, invocation, channel, sequence and Inspect span, tool-call and event ids. Exposure is matched by the read's tool-call id and rendered text (`runtime.py:330-399`).

**Output member.** `ExecutionConfig.output_agent` picks whose output becomes `TaskState.output`; `history_projection` picks which conversations are copied into `TaskState.messages` for scoring (`agent_scheduler.py:813-869`).

### inspect_ai seams this design uses

- **`react()`'s `on_continue`.** It is awaited after every completed turn (`src/inspect_ai/agent/_react.py:366-392`), except a turn that submitted (`:324-335`), a turn whose context overflow was recovered (`:271-276`), and a turn interrupted through the agent channel (`:359-364`). It may return `True`, a string (appended as a user message), `False` (ends the loop), or an `AgentState`, whose messages replace the conversation with no message added (`:388-390`). `deepagent()` already composes its own notices into the caller's `on_continue` this way (`src/inspect_ai/agent/_deepagent/deepagent.py:222-237`, `lifecycle_tools.py:533-578`).
- **The agent channel cannot hold a turn.** `subscribe_turn_state` callbacks are synchronous observers (`src/inspect_ai/agent/_channel/channel.py:293-324`). `before_turn` blocks only when the conversation has no user message yet (`:432-453`).
- **`on_before_model_generate` runs while holding a connection slot.** The slot is taken at `src/inspect_ai/model/_model.py:1058`; the hook is awaited inside the attempt at `:1505`, before the cache lookup (`:1528-1537`). A hook that waited there would pin a connection slot for as long as it waited. Hooks are also process-wide, not per sample.
- **Transcript subscriptions.** A sample's transcript notifies subscribers when an event is added and again when it is updated, so a completed `ModelEvent` is seen with its final `input`, `error` and `cache` fields (`src/inspect_ai/log/_transcript.py:689`, `:692-737`, `:977-988`; `src/inspect_ai/event/_model.py:102`, `:126`, `:135`).

### Spike

I ran a throwaway solver on the worktree's inspect_ai (`fccfb298e`, whose `react()`, channel, `run()` and `ModelEvent` are unchanged from `aa20052a`), with three `react()` members scripted by mockllm to call a tool twice and then submit:

- Each member's `on_continue` signalled "yielded" and then waited on an anyio event; a round-robin loop opened one member at a time. Tool calls ran in the order `a1 b1 c1 a2 b2 c2`. Each member's final conversation had one system message, one user message and no continue prompts.
- Opening all three gates and waiting for all three yields (a concurrent step with a barrier) also worked.
- Cancelling the task group while members were parked ended the sample cleanly with status `success`.
- A transcript subscription saw each tool result in the member's next completed `ModelEvent` input, which is ORBIT's exposure evidence, with no `Model` wrapper.

So ORBIT's `model_step` scheduling works on unmodified `react()` members that stay inside one `react()` invocation for the whole sample.

## Design

### Concept map

| ORBIT | inspect_swarm | Notes |
|---|---|---|
| `AgentSpec` (name, role, model, prompt, tools) | `member()` record (name, role, `Agent`, model, tools, limits) | ORBIT builds each `Agent` itself and passes it in. |
| `AgentSpec.max_messages` | the member's `message_limit` under `run()` | ORBIT skips the agent; inspect_swarm ends the member with a limit status. Same effect on later turns. |
| `AgentGroup.goal` | per-member initial input (new, [P1](#p1-member-records-m1)) | |
| `AgentSpec.submit` | how the member's `Agent` is built (`submit=False`) | Unchanged. |
| `is_compromised`, `CompromisedAgentAttack` | roster data built by ORBIT before `swarm()`; timed compromise through [trusted turn hooks](#p5-trusted-turn-hooks-later-with-p4) | No compromise feature in inspect_swarm. |
| Invocation edges, persistent delegation sessions | inside a member's own `Agent` (`as_tool`, `handoff`) | ORBIT-specific; not swarm members. |
| `communication_edges` | the bus's route policy ([P2](#p2-bus-extension-points-m2)) | |
| `ChannelConfig` readers and writers | a channel kind on the bus's public extension point ([P2](#p2-bus-extension-points-m2)) | Later, swarm.md's board channels take `readers` and `writers` with these names. |
| `poll`, `notify` | swarm.md's `poll`, `notify` | ORBIT's notice is derived per request; inspect_swarm's is added at a turn boundary (see [Differences](#behaviour-a-port-changes)). |
| `auto`, dialogue projections, `peer_messages` | none | Stay ORBIT-specific. |
| Scheduler modes, plans, quanta | an activation policy over gated members ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | `model_step` and `completion` only. |
| `visibility="step_start"` | a read cutoff carried by the activation ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | |
| `max_turns`, halt conditions, hooks | the policy's own loop; extensible stop reasons ([P4](#p4-the-turn-seam-and-activation-policies-scheduled-mode-item)) | |
| `sent`, `read`, `notified`, `model_exposure` | `sent`, `read`, `notified` (new), `exposed` ([P3](#p3-evidence-and-exposure-without-a-model-wrapper-m2)) | |
| ORBIT `delivered` (inserted into a conversation) | `intervened`, only from trusted turn hooks ([P5](#p5-trusted-turn-hooks-later-with-p4)) | inspect_swarm's `delivered` means "enqueued to an inbox" ([Evidence mapping](#p3-evidence-and-exposure-without-a-model-wrapper-m2)). |
| `output_agent` | `final="reporter"` naming a member | `history_projection` goes to the scoring deep dive ([P1](#p1-member-records-m1)). |
| Tool gates (`on_tool_call`) | Inspect approval policies today; sentinel `BeforeToolCall` protocols when sentinel's dispatcher is on inspect_ai `main` | swarm.md's monitoring decision. |
| Input filters, output checks, resampling | sentinel's `BeforeGenerate`, `AfterGenerate` and `resample()`, designed but not built (`inspect_sentinel: design/sentinel-overview.md`, "Stages", "Protocols") | Until they exist, see [Migration](#migration-path). |
| Notice and exposure `Model` wrappers | notices at turn boundaries; exposure from a transcript subscription | No `Model` wrapper. |
| Shared sandbox ("Neither channels nor agent groups isolate shared files or sandboxes") | swarm.md's shared-sandbox default | Same. |

### What inspect_swarm provides

Five items. P1 to P3 fall inside M1 and M2 as small additions; P4 and P5 are the "optional scheduled (turn-based) execution mode" in swarm.md's later work, which needs M2's bus only for its read cutoff.

#### P1. Member records (M1)

- `member(agent, *, name, role=None, input=None, limits=...)`. `input` (a string or message list) replaces the sample input as this member's first messages, for ORBIT's group goals. `role` is a free label recorded in evidence and member timelines, as ORBIT's role is informational.
- `final="reporter"` takes the member's name, which is ORBIT's `output_agent`.
- The scoring deep dive decides how each member's final `AgentState` is exposed to the task's scorer. ORBIT needs it to rebuild `history_projection`.

#### P2. Bus extension points (M2)

swarm.md already says that every explicit channel is a set of tools plus state that hands records to `deliver()`. For ORBIT that has to be a public extension point, because ORBIT's reader and writer channels, its secret coalition channels and its blackboards are its own channel kinds. The inter-agent communication deep dive owns the shapes; ORBIT needs these properties of them:

- **A public record and `deliver()`.** A record carries `channel` (an id the channel kind chooses), `recipients`, `kind` and `payload`. The bus assigns a `message_id` and a sample-wide, increasing `sequence`; ORBIT's cursors, causation links and step-start snapshots need both.
- **The sender is stamped, never passed.** `deliver()` takes the sender from the calling member's context, the way ORBIT's `actor_identity()` checks a tool's bound agent against the active one (`runtime.py:117-124`). Called outside a member, it refuses, unless the caller is trusted code that names an origin (the path swarm.md reserves for optional red-team records).
- **Routes.** `swarm(routes=...)` takes directed `(sender, recipient)` pairs, or `None` for all pairs, and the bus drops recipients the routes forbid before the monitor runs. A record left with no recipient is refused to the sender as a tool error, as in ORBIT. Channel membership is the channel kind's check: it computes recipients from its readers and refuses senders that are not writers.
- **Read helpers for external kinds.** `record_read(message_ids, rendered)`, called from inside a read tool, writes the `read` record and registers what the tool returned so that [P3](#p3-evidence-and-exposure-without-a-model-wrapper-m2) can detect exposure. A `max_sequence` argument implements step-start visibility.
- **Notice contributions.** A channel kind reports each member's unread count per channel id so that the M2 notice can list them. The notice rule in swarm.md already allows channel ids the task or controller assigned and excludes names a member chose; ORBIT's configured channel ids satisfy it.

Size caps and back-pressure are swarm.md's storm controls already, and match ORBIT's `ChannelCapacityError`-to-`ToolError` behaviour.

#### P3. Evidence and exposure without a model wrapper (M2)

**Kinds.** inspect_swarm's evidence kinds become `sent`, `delivered`, `read`, `notified` and `exposed`, and [P5](#p5-trusted-turn-hooks-later-with-p4) adds `intervened`. Against ORBIT's:

| ORBIT | inspect_swarm | Meaning in inspect_swarm |
|---|---|---|
| `sent` | `sent` | The bus accepted the record (after policy). |
| (none) | `delivered` | The record was enqueued for a recipient. ORBIT stores one message that recipients read. |
| `read` | `read` | A read tool returned the message. |
| `notified` | `notified` | A notice naming the channel was added to the member's conversation. |
| `delivered` | `intervened` | Trusted code inserted content into a conversation. The bus never does. |
| `model_exposure` | `exposed` | The read result was in the input of a successful generation. |

Records carry the fields ORBIT's evidence carries, renamed: member (ORBIT's agent), the member's conversation id (ORBIT's session; sentinel's `conversation`), the activation id under a policy (ORBIT's invocation), channel, `message_id`, `sequence`, and the Inspect span, tool-call, message and model-event ids. ORBIT's `parent_event_id` from an exposure to its read is the record's link to its cause.

**The exposure observer.** At sample start the swarm subscribes to the transcript. When a `ModelEvent` completes with no error, inside a member's span, the observer looks in its `input` for `ChatMessageTool` messages whose tool-call ids have read receipts, and confirms that the receipt's rendered text is in the message, as ORBIT does (`runtime.py:363-379`). It then writes one `exposed` record per message, member and conversation, the first time only. An event with `cache == "read"` is recorded as exposure of a replayed generation, as ORBIT marks `source="inspect_cache"`. The observer stores a hash of the rendered text, not the text, so payloads are not copied into the store a second time.

This replaces ORBIT's two non-defense wrappers:

- **No `Model` wrapper.** The observer reads Inspect's own `ModelEvent`, which records the input after Inspect's own transformations and hooks (`_model.py:1505-1580`). It sees cache hits without a separate path, and it does not need `_resolve_config`.
- **Not a model-boundary audit.** It sees what Inspect recorded as the input, so a provider-side rewrite is invisible to it, as to ORBIT. A read with no later successful generation has no exposure; coverage is known because the swarm installs the observer for every member.
- **The subscription is a private API.** `Transcript._subscribe` is private; inspect_swarm may use internals (decision: Ransom, 2026-10-07).

#### P4. The turn seam and activation policies (scheduled-mode item)

**The seam.** `swarm_continue(on_continue=None) -> AgentContinue` returns an `on_continue` that wraps the caller's own:

1. Call the wrapped `on_continue` (or apply `react()`'s default). If it returns `False`, return `False`: the member ends as it would alone.
2. If an activation policy governs this member and the current activation's quantum is `model_step`, signal "yielded" to the policy and wait until the policy opens the member's next activation. Cancellation (drain) ends the wait. With no policy, or quantum `completion`, there is no wait.
3. Append whatever is pending for the member (an M2 notice, a [P5](#p5-trusted-turn-hooks-later-with-p4) injection) by returning an `AgentState`, composed with the wrapped result as `deepagent()`'s `background_on_continue` composes its notices. With nothing pending, return the wrapped result unchanged.

The member is found from a ContextVar the swarm sets when it starts the member's task. A nested agent that also uses `swarm_continue()` passes through, because the hook acts only in the loop whose agent span is the member's own.

Because the member stays inside one `react()` invocation, its conversation, attempt count, compaction state, MCP connections and agent channel persist across activations, and nothing re-enters `react()`. The member's first activation is the start of its `run()`; it ends at submission, at `on_continue` returning `False`, or at a limit. `deepagent()` accepts `on_continue` and passes it to its `react()`, so deepagent members take the seam the same way. Bridged members and any other agent without the seam get one activation of quantum `completion`.

ORBIT's quanta map as follows:

- `model_step`: one generation and its tool batch; the seam is `react()`'s turn boundary.
- `completion`: the whole run.
- `tool_step` and `legacy` have no equivalent and stay on ORBIT's own executor ([What stays ORBIT-specific](#what-stays-orbit-specific)).

**Policies.** An activation policy is trusted Python that decides which members run and when:

```python
class ActivationPolicy(Protocol):
    async def __call__(self, swarm: SwarmControl) -> StopReason | None: ...


class SwarmControl(Protocol):
    members: Mapping[str, MemberHandle]
    def sequence(self) -> int: ...                       # current bus sequence, for step-start cutoffs
    def stop(self, reason: StopReason) -> None: ...


class MemberHandle(Protocol):
    name: str
    role: str | None
    status: Literal["pending", "parked", "running", "done", "limit", "errored", "cancelled"]
    state: AgentState                                    # readable and writable only while pending or parked
    async def activate(
        self,
        *,
        quantum: Literal["model_step", "completion"] = "model_step",
        read_cutoff: int | None = None,                  # step-start visibility
        inject: Sequence[ChatMessage] = (),              # P5
        origin: str | None = None,                       # required with inject
    ) -> ActivationResult: ...                           # returns at the next yield or when the member ends
```

`ActivationResult` carries the activation id, the member's status afterwards, the message counts before and after, whether the member submitted, and the duration. Every activation start and finish is an `InfoEvent` in the member's span, as ORBIT's schedule events are (`plans.py:121-132`).

- **Concurrent steps.** A policy starts several `activate()` calls in an anyio task group and awaits them all; that is a barrier.
- **Errors.** A member's own limit ends that member, and `activate()` returns with status `limit`. Any other member error propagates through the swarm's member task group, which cancels the other members and the policy and re-raises the original, the same contract as ORBIT's "Concurrent failure cancels and joins siblings, then propagates the original failure". The swarm's own cap is recovered as swarm.md's [Accounting](swarm.md#observer-evidence-accounting-and-metrics) specifies.
- **Wake requests.** Under a policy, a `notify` delivery adds the notice at the recipient's next activation but never starts one, which is ORBIT's rule that messages do not activate agents.
- **Stop reasons.** swarm.md records a stop reason from a fixed set. A policy may return its own reason, recorded as `policy:<name>` (for example `policy:max_turns`, `policy:schedule_exhausted`, `policy:convergence`), alongside the swarm's own reasons.
- **The default.** M1's leaderless controller is the policy that activates every member once with quantum `completion`, so adding the interface changes no M1 behaviour.

inspect_swarm ships one reference policy, `round_robin(max_rounds=...)`: members in roster order, one `model_step` each, skipping members that ended. It serves as the test vehicle and as a policy for other scheduled harnesses. ORBIT's plans, superstep snapshots, `after_activation` amendments and outer-loop hooks are ORBIT's policy, written against `SwarmControl`.

#### P5. Trusted turn hooks (later, with P4)

ORBIT changes conversations between turns: pending injections, `inject_prompt` compromise, and defenses that rewrite messages before a turn. inspect_swarm gives trusted code two ways to do this, both at a turn boundary:

- **Append.** `activate(inject=[...], origin="...")` appends the messages when the member resumes. For a member's first activation, they go after its initial input.
- **Edit in place.** While a member is `pending` or `parked`, trusted code may change `MemberHandle.state.messages`. Reading or writing it while the member runs raises.

Each change writes an `intervened` record with the origin, member, activation id, and the ids of messages appended or replaced.

Hooks are configured by the eval author in Python. They are never reachable from member tools. inspect_swarm ships no hook that uses them; ORBIT's attacks, defenses and dialogue adapter would be its own. The bus still never delivers peer text this way. Whether inspect_swarm should allow user-role injections through this path at all is [open question 1](#open-questions).

### What an ORBIT port changes

These are the changes inside ORBIT's `common/v1` profile, in the order of the [migration path](#migration-path).

- **Agents.** `build_turn_react_agents` builds `react()` (or `deepagent()`) with `on_continue=swarm_continue(spec_on_continue)`, in place of `turn_react`.
- **Executor.** `ScheduledExecutor` and `AgentScheduler` become an `ActivationPolicy`. `ScheduleRuntime` keeps deciding which agents each step runs; `_execute_agent` becomes `MemberHandle.activate()`; asyncio tasks and semaphores become anyio task groups and limiters, or disappear because inspect_swarm owns member tasks.
- **Outer loop.** `ExperimentScheduler.run_loop` runs inside the policy: halt conditions and hooks between steps, attacks through `activate(inject=...)`, pre-turn defenses on parked members' state.
- **Channels.** ORBIT's channel tools build records and call `deliver()`, and call `record_read()` before returning. Channel membership and per-session cursors stay in ORBIT's channel kind. `communication_edges` become `routes`.
- **Delivery and observation.** `CommunicationDeliveryModel` and `CommunicationModel` are removed: `notify` uses inspect_swarm's notice, and exposure comes from P3. ORBIT's `CommunicationState` becomes a projection of inspect_swarm's evidence at finalisation, so its collusion and exposure scorers read the same index.
- **Defenses.** Tool gates become Inspect approval policies, then sentinel protocols. Safety-prompt filters become part of the member's system prompt. Input and output filters wait for sentinel's generate stages.
- **Output.** `output_agent` becomes `final="reporter"`.
- **Dependencies.** ORBIT depends on inspect_swarm, which tracks inspect_ai `main`.

### What stays ORBIT-specific

- Scenarios, datasets, attack and defense registries, scorers, judges, the DCOP phase controller, the YAML and CLI wrapper, and the viewer overview.
- The `legacy/v1` profile: `legacy` and `tool_step` quanta, the `summary` and `peer_messages` observation modes, cross-agent memory injection, and legacy channel formats. It keeps `turn_react` and ORBIT's executors.
- `auto` delivery and dialogue projections, which put peer text in a conversation outside a tool result.
- `TopologyExecutor` and persistent delegation sessions. A root agent with delegates can be one swarm member if ORBIT wants the ledger.
- Plan amendment with audited revisions, superstep snapshots of activity summaries, and convergence halting: all inside ORBIT's policy.
- Model-boundary defenses until sentinel's generate stages exist.

### Behaviour a port changes

A port does not reproduce every ORBIT behaviour exactly. Results from the two runtimes should not be pooled unless these are accepted or controlled:

- **Notices persist.** ORBIT recomputes the notice for every request and never stores it (`delivery.py:297-305`, `:336-343`). inspect_swarm adds it to the conversation at a turn boundary (the M2 design). Prompts and cache keys differ.
- **Notice role.** ORBIT's notice is a system message (`delivery.py:218`). inspect_swarm's role is the M2 design's choice, and never `operator`.
- **No continue prompt on resume.** `turn_react` adds "Please continue with your next action." on every resume after a tool result; a gated `react()` member adds nothing, because it never left its loop.
- **Overflow recovery is not a boundary.** Under `model_step`, `turn_react` yields after context overflow (`turn_react.py:255-258`). `react()` does not call `on_continue` after a recovered overflow, so that activation runs one more generation.
- **One span per member.** ORBIT records one agent span per activation. inspect_swarm records one per member with activation events inside, and a named timeline per member (swarm.md, Member views).
- **Member time limits include time parked at the seam.** Use token, cost or message limits per member under a policy. The limits deep dive owns the details. A sequential policy has at most one member's calls in flight, which bounds overshoot by one member's fan-out.
- **Read output format.** inspect_swarm's own read tools fence each message under a bus-stamped sender line (swarm.md, Delivery). ORBIT's channel kind keeps its JSON lines, where JSON string escaping is the fence.

### Migration path

Each step is useful alone.

1. **After M1: concurrent `completion` runs.** ORBIT experiments whose plan is one concurrent `completion` step with no channels map onto a leaderless filesystem swarm. They gain drain, the realized-cost ledger and per-member submissions.
2. **After M2: channels on the bus.** ORBIT's channel kind on P2 and evidence through P3 remove ORBIT's notice and exposure wrappers. Scheduled ORBIT runs still use ORBIT's executor, but their channels, routes and evidence are inspect_swarm's.
3. **After the scheduled-mode item (P4, P5): ORBIT's scheduler as a policy.** `turn_react` is no longer used by `common/v1`, and tool gates move to approval policies. Defense experiments that need model-boundary filters still wrap the member's model with ORBIT's filter. inspect_swarm neither needs nor prevents that.
4. **When sentinel's generate stages land: defenses as protocols.** Input and output filters become sentinel protocols, and resampling becomes `resample()`. The defense wrapper and its cache refusal go away.

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

- Works today and handles caching correctly when done with ORBIT's care.
- Each wrapper must preserve model identity and effective configuration through a private API, the wrappers' order matters, and inspect_swarm would carry this for every member.
- Rejected: turn-boundary notices and a transcript subscription need neither.

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
  - the member record gains `input`;
  - the bus gains a public record type, `deliver()`, `routes`, and `message_id` and `sequence`;
  - evidence gains the `notified` and `intervened` kinds, under the versioned `InfoEvent` payload swarm.md specifies;
  - the scheduled-mode item gains `swarm_continue()`, `ActivationPolicy` and `round_robin`;
  - stop reasons gain `policy:<name>`.
- **inspect_ai**: none required. The seam uses `on_continue`, and the observer uses the transcript subscription. Both are private or semi-private surfaces inspect_swarm may use. The binder and the notice item are M2's.
- **ORBIT logs.** Logs ORBIT already wrote are unaffected. A ported run writes inspect_swarm's evidence (`source="inspect_swarm"`) and no longer writes `orbit.communication` records; ORBIT's scorers read a projection built at finalisation. Runs should record which runtime produced them, because of the [behaviour changes](#behaviour-a-port-changes).
- **Dependency ranges.** ORBIT accepts `inspect-ai>=0.3.263,<0.4` and tests one locked version (`ORBIT: pyproject.toml`). inspect_swarm tracks inspect_ai `main` and uses internals, so a port inherits inspect_swarm's floor, which is narrower.
- **Python and concurrency.** Both require Python 3.11+. inspect_swarm runs on asyncio and trio, so policy code must use anyio.

## Security

- **Peer content** reaches a member only as tool output, from inspect_swarm's read tools or from a channel kind's read tools. The bus never inserts it elsewhere. ORBIT's `auto` and dialogue paths are not offered.
- **Sender identity** is stamped by `deliver()` from the calling member's context. An external channel kind cannot set it. Trusted calls outside a member must name an origin, which the evidence keeps.
- **Trusted hooks and policies** are eval-author Python running in the runner process, as ORBIT's extensions are ("Registration and channel permissions are not a plugin sandbox", `ORBIT: docs/security-extensions.md`). They are never exposed as tools, and every change they make to a conversation writes an `intervened` record.
- **Notices** list only channel ids configured by the task or registered by trusted code, never ids or names a member created, which keeps swarm.md's notice rule.
- **The exposure observer** reads model inputs in memory and stores ids and hashes only.
- **Policy decisions are never read from model output.** ORBIT installs no model-facing schedule editor (`plans.py:1-6`); a port must keep it that way.

## Testing

All in inspect_swarm, with mockllm, on asyncio and trio, with no network and no Docker:

- **The seam:**
  - a `round_robin` run reproduces the spike's order, with one system message and no added user messages;
  - a concurrent step waits at its barrier;
  - a `completion` activation runs to submission;
  - parked members are cancelled cleanly by drain;
  - a wrapped `on_continue` keeps its `True`, string, `AgentState` and `False` semantics;
  - a submission ends the member;
  - a nested agent built with `swarm_continue()` does not park;
  - a member without the seam under a `model_step` policy runs one activation, and the harness-validity checks report it.
- **Policies:**
  - stop reasons `policy:<name>` are recorded;
  - a member limit returns status `limit`;
  - a member error cancels siblings and propagates the original;
  - the swarm's own cap ends the policy and drains;
  - notices do not start activations;
  - `MemberHandle.state` refuses access while running.
- **Bus extension:**
  - an external channel kind delivers through `deliver()` with the sender stamped;
  - a call outside a member without an origin is refused;
  - routes drop recipients, and a record left with none becomes a tool error;
  - `sequence` increases monotonically;
  - `max_sequence` hides later messages.
- **Exposure:**
  - a read is exposed at the member's next successful generation;
  - a cache replay is marked;
  - a failed generation does not count;
  - a read with no later generation has no exposure;
  - concurrent members are attributed by span.
- **Interventions:** an `intervened` record is written for appends and in-place edits.
- **An ORBIT-shaped conformance test** with no ORBIT dependency. A policy with sequential and concurrent steps over members using a reader and writer channel kind checks ORBIT's documented rules:
  - messages do not activate members;
  - submitted members are not activated again;
  - step-start snapshots hide same-step sends;
  - concurrent failure cancels and joins siblings.

## Implementation plan

Steps for inspect_swarm, each a PR inside the milestone named:

1. **M1: member records.** `input=` and `role=` on `member()`, `final="reporter"` by member name. Files: `src/inspect_swarm/_member.py`, `_final.py`, tests. Access to final member states is the scoring deep dive's.
2. **M2: bus extension points.** The public record type and `deliver()` with the sender stamped, `routes`, `message_id` and `sequence`, `record_read()` with `max_sequence`, and notice contributions. Files: `_bus.py`, `_channels.py`, `_evidence.py`. Shapes coordinated with the inter-agent communication deep dive.
3. **M2: exposure observer and `notified`.** The transcript subscription, read receipts and hashes, and cache-replay marking. Files: `_observer.py`, `_evidence.py`.
4. **Scheduled mode: the seam and policies.** `swarm_continue()`, `ActivationPolicy`, `SwarmControl`, `MemberHandle`, `round_robin`, and `policy:<name>` stop reasons. M1's controller is re-expressed as the default policy. Files: `_turn.py`, `_policy.py`, `policies/_round_robin.py`, `_swarm.py`.
5. **Scheduled mode: trusted turn hooks.** `activate(inject=..., origin=...)`, parked-state access, `intervened` evidence. Files: `_turn.py`, `_evidence.py`.
6. **The ORBIT-shaped conformance test.** `tests/test_orbit_shape.py`.

A port of ORBIT follows the [migration path](#migration-path); it is ORBIT work and not planned here.

## Open questions

1. **Trusted injections in the user role.** P5 lets eval-author code append any message at a member's turn boundary. ORBIT's pending injections and dialogue projections are user messages.
   - (a) Allow any role through trusted hooks, always written as `intervened` with an origin. The bus rule (peer messages never in the user role) is unaffected, because the bus never uses this path.
   - (b) Allow trusted hooks to append only non-user roles, so ORBIT's dialogue adapter and user-role injections stay on ORBIT's executor.

   Recommendation: (a). The decision of 2026-10-07 governs what the swarm delivers between members; an eval author's own injected input is the eval's design, as a sample's input is, and the evidence keeps it distinguishable.
2. **Where the turn seam lives.** This design uses `swarm_continue()` (no inspect_ai change; members must be built with it). The alternative is an awaited gate in inspect_ai's `AgentChannel.before_turn` (works for any channel-facade agent; an inspect_ai change). M2's notices need the same seam, so the inter-agent communication deep dive should use the one chosen here. Recommendation: `swarm_continue()`, and propose the channel gate only if a user needs to schedule agents that cannot take it.

## Not this design

- **`tool_step` on `react()`.** Rejecting a multi-call batch before any tool runs needs a `react()` change, or a `parallel_tool_calls=False` approximation that providers honour unevenly.
- **More built-in policies** (superstep, explicit plans, Concordia-style engines, AgentsNet's synchronous rounds), beyond `round_robin`.
- **Board channels with readers and writers.** The board is a later item in swarm.md. When built, it should take ORBIT's `readers`/`writers` names, so ORBIT's channel kind could become configuration.
- **Sentinel's generate stages and `resample()`.** They are on sentinel's roadmap. ORBIT's model-boundary defenses wait for them.
- **Per-member time and working limits while parked.** For the limits deep dive.
- **Transcript eviction.** With `INSPECT_TRANSCRIPT_BOUNDED` set, old events leave memory (`src/inspect_ai/log/_transcript.py:69-76`). The observer is live, so it is unaffected, but any post-hoc evidence rebuild would need the log, not the in-memory transcript.
