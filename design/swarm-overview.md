# Inspect Swarm: overview

This is the short form of [swarm.md](swarm.md), the detailed design. It assumes the reader knows inspect_ai, and skips the motivation and research survey. Each section links to the detailed argument.

## What it is

`inspect_swarm` lets an Inspect eval run a **swarm**: several agents working on one sample at the same time and coordinating. It makes "how many agents, how they are arranged, how they communicate, and how the budget is split" ordinary task parameters that an eval set can sweep. It also records what is needed to compare a swarm fairly with more epochs or a bigger single-agent budget, and to monitor what the agents say to each other.

It is an eval library, not a production orchestration framework. The runtime lives in this repository. inspect_ai gets changes only where behaviour needs them, and inspect_swarm may use inspect_ai internals where appropriate ([where each part lives](swarm.md#where-each-part-lives)).

## The shape

`swarm()` returns an Inspect `Agent`, so it runs anywhere an agent does. A swarm is four things kept separate ([the shape](swarm.md#the-shape)):

| Component | What it is |
|---|---|
| **Members** | The agents: each a name, role, built agent or `@agent` factory and arguments, limits and conversation. No agent object runs twice at once in a sample: copies (`count`) are built from a factory per member per sample. Any agent that keeps its work inside its invocation: `react()`, synchronous `deepagent()`, or a bridged Claude Code or Codex. ([Members](swarm.md#members)) |
| **Channels** | How members share information: the shared sandbox filesystem from M1; a shared memory routed through the bus from M2; later, optionally, direct messages, a notes/fact log, a task list with claims, and a board. Each is a `@swarm_channel` registry object. ([Channels](swarm.md#channels)) |
| **Controller** | How the swarm runs: topology (leaderless first; coordinator tree and lead-with-teammates later), start and wake, termination, and the final-answer chain. Each topology is a `@swarm_controller` registry object; the swarm's fixed runtime does the drain and finalisation. ([Controller](swarm.md#controller-topology-termination-final-answer)) |
| **Observer** | What is recorded and where monitors attach: one bus for every sanctioned message, evidence events, a realized-cost ledger, and swarm metrics. ([Observer](swarm.md#observer-evidence-accounting-and-metrics)) |

`swarm()`'s arguments mirror the components ([API](swarm-api.md)). Members are built agents, `swarm(members=[react(...), deepagent(...)])`, or `member()` records; several copies of one configuration take a factory and its arguments, so that each copy gets its own agent: the common case is `swarm(members=member(deepagent, args={...}, count=4))` ([API](swarm-api.md#the-member-contract)). Written out:

```python
swarm(
    members=member(deepagent, args={...}, count=4),
    controller="leaderless",   # a name or a @swarm_controller object, including a user's own
    channels=["filesystem"],   # names or @swarm_channel objects
    budget=Budget(),           # caps derived from the sample's limits
)
```

After M2, a strict, verifier-only answer is a controller parameter and needs the task's verifier: `controller=leaderless(final="verify")` with `result=answer_result(verifier=...)`.

The observer has no argument: what it records is a fixed, versioned contract that scorers and analysis read, the same in a swarm and in its baselines. Monitors are configured through inspect_sentinel, scores through the task's own scorers, and labels through Scout ([API](swarm-api.md#observer)). The API's own arguments are logged faithfully, so names, task parameters and Python sweeps all work; members configured with hooks and the task's result contract are rebuilt through registered builders or the task ([API](swarm-api.md#logging-and-replay)). A swarm is the task's solver, so its arguments are part of Inspect's task identity, and two *arms* (two tasks in an eval set, each a `Task` with its arguments, solver, model and limits) that differ in any of them are distinct ([API](swarm-api.md#task-identity-what-makes-two-arms-distinct)).

A swarm of one member with no channels is a single agent with the same accounting, so it is the natural baseline.

## Main decisions

**The swarm owns its members' work, including descendants.** On termination it cancels and awaits everything a member started before it finalises, so nothing edits shared files during the final answer and all usage is counted.
- `background()` attaches work to the sample, not the caller, so M1 rejects background deepagent members.
- Supporting them needs a private inspect_ai seam for where `background()` starts work, and an owner per member in inspect_swarm that drains the member's background work before the member ends, which M2 adds ([swarm-background-members.md](swarm-background-members.md), [plan](swarm-implementation-plan.md#m2-ready-for-external-usage)).
- ([Members](swarm.md#members))

**Limits are soft stopping rules; arms are compared on realized cost.** Inspect checks limits before and after calls and reserves nothing for calls in flight, so any cap is overshot by the calls in flight. The design does not plan on making limits hard. Instead:
- a ledger records what was actually spent, including descendants, finalisation and overshoot;
- calls it cannot see (cancelled in flight, unpriced, native compaction) are marked unknown or unattributed, not zero;
- the final-answer reserve is best-effort.
([Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply), [Accounting](swarm.md#observer-evidence-accounting-and-metrics))

**The task decides what a member's result is.** M1 adds no scorer: the task's own scorers score the swarm's answer or the environment it leaves, as after any agent. Per-member claims come after M2 (decision: Ransom, 2026-10-08; [scoring](swarm-scoring.md)).
- *Answer-scored tasks* (a final answer string) get per-member scores and team@k, comparable with best@k from epochs. Voting needs a task-defined comparable answer form.
- *Shared-artifact tasks* (scored on the sandbox, like SWE-bench) get one team score. Individual correctness is unavailable unless each member leaves its own artifact.
([Results and scoring](swarm.md#results-and-scoring-a-task-owned-contract))

**Default final answer for leaderless swarms: `verify`, else `vote`, else `first`.** Use the task's in-loop verifier when it has one, otherwise vote when answers are comparable, otherwise take the first submission. Every member's submission is recorded regardless. For shared-artifact tasks the result is the drained environment. M1 builds `first` only; the chain comes after M2 (decision: Ransom, 2026-10-08). ([Final answer](swarm.md#controller-topology-termination-final-answer))

**Members share the sample's sandbox by default.** The filesystem is M1's only channel, and bridged members already run there.
- A sandbox per member is an opt-in in M2; partial isolation is optional later work. Possible reasons: isolation matching the epochs arm, or containment baselines.
- Concurrent edits, git index locks and resource contention in a shared sandbox are handled by the task's layout: per-member worktrees or scratch directories, and a sandbox sized for the members.
([Sandbox topology](swarm.md#sandbox-topology))

**No agent object runs twice at once in a sample.** A member is a built agent, shared across samples as any Inspect solver is, or an `@agent` factory and its arguments, which the runtime calls in the member's task in each sample; `count > 1` needs the factory, and one object cannot be two members (decision: Ransom, 2026-10-14). The factory form keeps state the factory creates (hooks, caches, a CLI session id) private to each copy. Objects passed as arguments are still shared, and built-in tools keep their state in the store, which is why the tool-state scope below is still needed. ([API](swarm-api.md#the-member-contract))

**Each member's tool state is its own; the sample store is still shared.** Built-in tools such as `memory()` and `bash_session()` keep state in the sample store, and agents in one sample share it unless each tool has its own `instance`; `deepagent()` adds a shared `memory()` by default. Each member runs in a tool-state scope (a small inspect_ai change before M1) so that the default instance is per member (decision: Ransom, 2026-10-09). A shared instance named on purpose is a channel, observed like the filesystem. The alternatives, refusing such members at construction or only warning, were rejected. ([Members](swarm.md#members), [Security](swarm.md#security))

**One bus for all sanctioned communication.** Every message, shared-memory write, note, claim or post goes through `deliver()`: monitor, then storm controls and the channel's admission check, then evidence, then applying it to the channel's state (delivery, for a message). Nothing else changes channel state or writes to a member's inbox. The filesystem stays an *observed* channel, seen only through tool calls, with the limits that implies. ([The bus](swarm.md#the-bus-one-interception-point), [Security](swarm.md#security))

**Monitoring aligns with inspect_sentinel.** There is no separate swarm monitor type.
- A send is a tool call, so sentinel's tool stages see it with the sender's identity, and sentinel's per-sample state gives monitors a joint view of the swarm.
- Sentinel's dispatcher is part of inspect_ai, and every M2 record is a native member's tool call that its tool stages see, so M2 monitors through sentinel only. Its tool stages still skip bridged agents, so a minimal bus hook in sentinel's action vocabulary is future work, for bridged members' bus tools and internal records ([decisions](swarm-implementation-plan.md#decisions), item 5).
([The bus](swarm.md#the-bus-one-interception-point))

**Peer messages are model output, delivered as tool output with distinct provenance.** By default a peer's text reaches a member only as the result of a swarm tool (`read_messages()` and the like), never as a user-role message. That is the default, not an absolute (decision: Ransom, 2026-10-09): after M2 an arm may opt into injection, below.
- Inside the tool result each message is fenced as data under a sender line the bus stamps, and carries only what the sender wrote, never its tool calls or transcript. Tool output alone is not a trust boundary.
- **No protection is given to peer text** (decision: Ransom, 2026-10-27). The fence is a label for the reader, not a security boundary: a member can write text that looks like a fence, another member's text, or a status line the swarm writes. By default, peer text reaches members only as tool output; the messages channel's opt-in `inject` mode delivers it in a user message. Evidence comes from bus events and ids, never from result text. A channel that needs stronger guarantees, for example for multi-agent security experiments, must provide them itself. ([Security](swarm.md#security))
- The swarm adds no message to a member's conversation (decision: Ransom, 2026-10-27). Members learn of changes only through tools: the status line that ends a channel tool's result, such as "3 unread from `worker-2`" or the shared memory's changed file ids, a command that returns that line alone (`changes`), and `swarm_wait`. Notices at each turn boundary were dropped: in a large swarm someone has almost always written something during any turn, so they arrive every turn, and any automatic message needs thresholds that are hard to pick; a tool lets the member ask when it cares.
- Logs and evidence mark peer content distinctly, preferring metadata to a new `source` value. Every read has a `read` event and, after M2, injections carry metadata ids, so analysis of model input can correlate it with swarm records, though not always exactly: repeated tool call ids, message edits and merged user messages can make it ambiguous, and an exact join is not an M2 guarantee; the swarm itself writes no exposure event, and how read results name their records for ORBIT's exposure observer is decided by the ORBIT port, after M2.
- The swarm never posts into members' agent channels, so communication's tools need no inspect_ai change beyond the registry types ([swarm-communication.md](swarm-communication.md)). Background members need one inspect_ai change, a private seam for where `background()` starts work ([swarm-background-members.md](swarm-background-members.md), [swarm-implementation-plan.md](swarm-implementation-plan.md#m2-pull-requests)).
- Vendor swarms such as Codex deliver peer text in the user role; native members do only in an opt-in arm.
- **Push after M2, optional and off by default**, each a logged ablation axis: reminders while messages stay unread; injection of the fenced bodies as a marked user-role message, a deliberately weaker boundary offered for experiments; and urgent messages that interrupt a recipient blocked in a long call, which need a small inspect_ai change (a `Steer` agent-channel item) ([push delivery](swarm-communication.md#push-delivery-after-m2-optional)). Reminders and injection depend on whether ordinary messages are pushed at all, which is open ([swarm-communication.md](swarm-communication.md#open-questions), open question 2).
([Delivery](swarm.md#delivery-peer-messages-are-model-output))

**Red-team features are optional and unscheduled.** Forged senders, secret channels and targeted injection may never be built. The single interception point keeps them possible. ([The bus](swarm.md#the-bus-one-interception-point))

**Align with ORBIT; do not adopt it as the substrate.**
- Reuse its vocabulary: rosters and roles, channels with readers and writers, evidence kinds.
- Its scheduled-activation model and its forked `react()` do not fit continuously active members that wake each other.
- The only other Inspect peer swarm found, the code for the paper *Architecture Matters for Multi-Agent Security*, is a scheduled study harness, not a reusable runtime.
- Within Inspect, follow inspect_petri's conventions for concurrent agents in one sample: model roles, a named timeline per member, and harness-validity scores. Accept ControlArena-shaped monitors through inspect_sentinel.
- Outside Inspect, SCHEME is the closest published design: a shared sandbox, peer messages as tool output, and monitors that see actions.
([Relationship to ORBIT](swarm.md#relationship-to-orbit), [Related projects](swarm.md#related-projects-and-what-they-teach), [Alternatives](swarm.md#alternatives-considered))

**Python 3.11+.** Above inspect_ai's 3.10 floor. ([Compatibility](swarm.md#compatibility-and-migration))

## Where each part lives

| Repository | What |
|---|---|
| **inspect_swarm** | `swarm()`, controller, members, budget and ledger, bus and channels, evidence, metrics, scorers, prompts, Scout scanners |
| **inspect_ai** | Only where needed: the `swarm_controller` and `swarm_channel` registry types (before M1), a tool-state scope for members (before M1), a switch that turns span `StoreEvent`s off inside a swarm (M1; decision: Ransom, 2026-10-19), a private seam for where `background()` starts work (M2), clean `react()` re-entry, per-span usage, a `Steer` agent-channel item (only for optional urgent messages after M2) |
| **inspect_swe** | Enabling and mapping Codex multi-agent v2 and Claude Code agent teams |
| **inspect_sentinel** | Monitors and protocols |
| **ORBIT-like packages** | Scenarios, attacks, defenses |

Detail: [where each part lives](swarm.md#where-each-part-lives).

## Plan

M1, then M2; after that, a menu in any order or in part, driven by user feedback with no internal evidence gate. Milestones defined by their audiences (decision: Ransom, 2026-10-13); [swarm-implementation-plan.md](swarm-implementation-plan.md) breaks each into pull requests, with their dependencies and the design sections each implements.

- **M1: ready for first internal usage.** The leaderless filesystem swarm with full accounting: the `swarm()` API with `@swarm_controller` and `@swarm_channel`, shared sandbox, per-member tool state, cap and ledger, drain, the final answer `first`, with each member's submission status and time recorded, evidence events and metrics, with a simple critical path (steps to the answer, longest member). Existing task scorers score it unchanged. Its inspect_ai changes land first: a one-line PR adding two registry types (decision: Ransom, 2026-10-08) and the tool-state scope (decision: Ransom, 2026-10-09).
- **M2: ready for external usage**, measured by whether Epoch's LeanOpenProblems swarm could port to it and gain ([swarm-lean-open-problems.md](swarm-lean-open-problems.md)): per-member sandboxes, background deepagent members (a private inspect_ai seam for `background()` and an owner per member in inspect_swarm), custom limits that end only the member, budget visibility for members, gate-check metrics, a shared-memory channel through the bus, the answer rule `last` and a controller's choice of the answer, and user docs, examples, an API statement and a first release. The port keeps its own scorer. Direct messages and scoring beyond `first`, `last` and a controller's choice (the result contract, per-member scores, `baseline()` and the comparisons with epochs) are later work (decision: Ransom, 2026-10-14; [decisions](swarm-implementation-plan.md#decisions)).
- **Later, any order:** [future work](swarm-implementation-plan.md#future-work).
