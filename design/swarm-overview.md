# Inspect Swarm: overview

Status: proposed, 2026-10-07. The short form of [swarm.md](swarm.md), the detailed design. It assumes the reader knows inspect_ai, and skips the motivation and research survey. Each section links to the detailed argument.

## What it is

`inspect_swarm` lets an Inspect eval run a **swarm**: several agents working on one sample at the same time and coordinating. It makes "how many agents, how they are arranged, how they communicate, and how the budget is split" ordinary task parameters that an eval set can sweep. It also records what is needed to compare a swarm fairly with more epochs or a bigger single-agent budget, and to monitor what the agents say to each other.

It is an eval library, not a production orchestration framework. The runtime lives in this repository. inspect_ai gets changes only where behaviour needs them, and inspect_swarm may use inspect_ai internals where appropriate ([where each part lives](swarm.md#where-each-part-lives)).

## The shape

`swarm()` returns an Inspect `Agent`, so it runs anywhere an agent does. A swarm is four things kept separate ([the shape](swarm.md#the-shape)):

| Component | What it is |
|---|---|
| **Members** | The agents: each a name, role, `Agent`, model, tools, limits and conversation. Any `Agent` that keeps its work inside its invocation: `react()`, synchronous `deepagent()`, or a bridged Claude Code or Codex. ([Members](swarm.md#members)) |
| **Channels** | How members share information: the shared sandbox filesystem from M1; direct messages from M2; later, optionally, a notes/fact log, a task list with claims, and a board. Each is a `@channel` registry object. ([Channels](swarm.md#channels)) |
| **Controller** | How the swarm runs: topology (leaderless first; coordinator tree and lead-with-teammates later), start and wake, termination, and the final-answer chain. Each topology is a `@controller` registry object; the swarm's fixed runtime does the drain and finalisation. ([Controller](swarm.md#controller-topology-termination-final-answer)) |
| **Observer** | What is recorded and where monitors attach: one bus for every sanctioned message, evidence events, a realized-cost ledger, and swarm metrics. ([Observer](swarm.md#observer-evidence-accounting-and-metrics)) |

`swarm()`'s arguments mirror the components ([API](swarm-api.md)). The common case is `swarm(members=member(deepagent(...), count=4))`; written out:

```python
swarm(
    members=member(deepagent(...), count=4),
    controller="leaderless",   # a name or a @controller object, including a user's own
    channels=["filesystem"],   # names or @channel objects
    budget=Budget(),           # caps derived from the sample's limits
)
```

A strict, verifier-only answer is a controller parameter and needs the task's verifier: `controller=leaderless(final="verify")` with `result=answer_result(verifier=...)`.

The observer has no argument: what it records is a fixed, versioned contract that scorers and analysis read, the same in a swarm and in its baselines. Monitors are configured through inspect_sentinel, scores through the task's own scorers, and labels through Scout ([API](swarm-api.md#observer)). The API's own arguments are logged faithfully, so names, task parameters and Python sweeps all work; members configured with hooks and the task's result contract are rebuilt through registered builders or the task ([API](swarm-api.md#logging-and-replay)). A swarm is the task's solver, so its arguments are part of Inspect's task identity, and two *arms* (two tasks in an eval set, each a `Task` with its arguments, solver, model and limits) that differ in any of them are distinct ([API](swarm-api.md#task-identity-what-makes-two-arms-distinct)).

A swarm of one member with no channels is a single agent with the same accounting, so it is the natural baseline.

## Main decisions

**The swarm owns its members' work, including descendants.** On termination it cancels and awaits everything a member started before it finalises, so nothing edits shared files during the final answer and all usage is counted.
- `background()` attaches work to the sample, not the caller, so M1 rejects background deepagent members.
- Supporting them needs a scoped owner for `background()` in inspect_ai: later, if wanted.
- ([Members](swarm.md#members))

**Limits are soft stopping rules; arms are compared on realized cost.** Inspect checks limits before and after calls and reserves nothing for calls in flight, so any cap is overshot by the calls in flight. The design does not plan on making limits hard. Instead:
- a ledger records what was actually spent, including descendants, finalisation and overshoot;
- calls it cannot see (cancelled in flight, unpriced, native compaction) are marked unknown or unattributed, not zero;
- the final-answer reserve is best-effort.
([Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply), [Accounting](swarm.md#observer-evidence-accounting-and-metrics))

**The task decides what a member's result is.**
- *Answer-scored tasks* (a final answer string) get per-member scores and team@k, comparable with best@k from epochs. Voting needs a task-defined comparable answer form.
- *Shared-artifact tasks* (scored on the sandbox, like SWE-bench) get one team score. Individual correctness is unavailable unless each member leaves its own artifact.
([Results and scoring](swarm.md#results-and-scoring-a-task-owned-contract))

**Default final answer for leaderless swarms: `verify`, else `vote`, else `first`.** Use the task's in-loop verifier when it has one, otherwise vote when answers are comparable, otherwise take the first submission. Every member's submission is recorded regardless. For shared-artifact tasks the result is the drained environment. ([Final answer](swarm.md#controller-topology-termination-final-answer))

**Members share the sample's sandbox by default.** The filesystem is M1's only channel, and bridged members already run there.
- A sandbox per member, or partial isolation, is optional later work. Possible reasons: isolation matching the epochs arm, or containment baselines.
- Concurrent edits, git index locks and resource contention in a shared sandbox are handled by the task's layout: per-member worktrees or scratch directories, and a sandbox sized for the members.
([Sandbox topology](swarm.md#sandbox-topology))

**One bus for all sanctioned communication.** Every message, note, claim or post goes through `deliver()`: monitor, then storm controls, then evidence, then delivery. Nothing else writes to a member's inbox. The filesystem stays an *observed* channel, seen only through tool calls, with the limits that implies. ([The bus](swarm.md#the-bus-one-interception-point), [Security](swarm.md#security))

**Monitoring aligns with inspect_sentinel.** There is no separate swarm monitor type.
- A send is a tool call, so sentinel's tool stages see it with the sender's identity, and sentinel's per-sample state gives monitors a joint view of the swarm.
- M2 uses sentinel directly if its dispatcher has reached inspect_ai `main`, otherwise a minimal hook in sentinel's action vocabulary.
([The bus](swarm.md#the-bus-one-interception-point))

**Peer messages are model output, delivered as tool output with distinct provenance.** A peer's text reaches a member only as the result of a swarm tool (`read_messages()` and the like), never as a user-role message.
- Inside the tool result each message is fenced as data under a sender line the bus stamps, and carries only what the sender wrote, never its tool calls or transcript. Tool output alone is not a trust boundary.
- At a turn boundary the swarm injects at most a metadata-only notice, such as "3 unread from `worker-2`", as deepagent does for background completions.
- Logs and evidence mark peer content and notices distinctly, preferring metadata to a new `source` value.
- How the bus obtains each member's channel ref (the binder) and how the notice is rendered are settled in M2's design, possibly on inspect_ai internals.
- Vendor swarms such as Codex deliver peer text in the user role; native members never do.
([Delivery](swarm.md#delivery-peer-messages-are-model-output))

**Red-team features are optional and unscheduled.** Forged senders, secret channels and targeted injection may never be built. The single interception point keeps them possible. ([The bus](swarm.md#the-bus-one-interception-point))

**Align with ORBIT; do not adopt it as the substrate.**
- Reuse its vocabulary: rosters and roles, channels with readers and writers, delivery modes, evidence kinds.
- Its scheduled-activation model and its forked `react()` do not fit continuously active members that wake each other.
- The only other Inspect peer swarm found, the code for the paper *Architecture Matters for Multi-Agent Security*, is a scheduled study harness, not a reusable runtime.
- Within Inspect, follow inspect_petri's conventions for concurrent agents in one sample: model roles, a named timeline per member, and harness-validity scores. Accept ControlArena-shaped monitors through inspect_sentinel.
- Outside Inspect, SCHEME is the closest published design: a shared sandbox, peer messages as tool output, and monitors that see actions.
([Relationship to ORBIT](swarm.md#relationship-to-orbit), [Related projects](swarm.md#related-projects-and-what-they-teach), [Alternatives](swarm.md#alternatives-considered))

**Python 3.11+.** Above inspect_ai's 3.10 floor. The scaffold moves to 3.11 in a separate PR before M1. ([Compatibility](swarm.md#compatibility-and-migration))

## Where each part lives

| Repository | What |
|---|---|
| **inspect_swarm** | `swarm()`, controller, members, budget and ledger, bus and channels, evidence, metrics, scorers, prompts, Scout scanners |
| **inspect_ai** | Only where needed: the `controller` and `channel` registry types (before M1); possibly a binder hook (M2), a scoped owner for `background()`, clean `react()` re-entry, per-span usage |
| **inspect_swe** | Enabling and mapping Codex multi-agent v2 and Claude Code agent teams |
| **inspect_sentinel** | Monitors and protocols |
| **ORBIT-like packages** | Scenarios, attacks, defenses |

Detail: [where each part lives](swarm.md#where-each-part-lives).

## Plan

M1, then M2; after that, a menu in any order or in part, driven by user feedback with no internal evidence gate. ([Implementation plan](swarm.md#implementation-plan))

- **M1: leaderless filesystem swarm with full accounting.** The `swarm()` API with `@controller` and `@channel`, shared sandbox, cap and ledger, drain, final-answer modes, the result contract, `InfoEvent` evidence and metrics. Its only inspect_ai change is a one-line PR adding two registry types, landed first (decision: Ransom, 2026-10-08).
- **M2: the bus and direct messages.** `deliver()`, `send_message`, delivery modes, monitoring through sentinel, and the binder.
- **Later, any order:**
  - structured channels (each needs M2; quiescence needs persistent members);
  - coordinator topologies and persistent members;
  - background deepagents as members;
  - bridged swarms;
  - safety hooks;
  - optionally, red-team features and other sandbox topologies.

## Open

- **Transcript representation for swarm evidence.** M1 uses versioned `InfoEvent`s as a working default. Whether to propose a first-class inter-agent message event in inspect_ai, and when, is undecided. ([Open questions](swarm.md#open-questions))
