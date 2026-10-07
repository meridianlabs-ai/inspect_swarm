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
| **Substrate** | How members share information: the shared sandbox filesystem from M1; direct messages from M2; later, optionally, a notes/fact log, a task list with claims, and a board. ([Substrate](swarm.md#substrate)) |
| **Controller** | How the swarm runs: topology (leaderless first; coordinator tree and lead-with-teammates later), start and wake, termination, drain, and the final answer. ([Controller](swarm.md#controller-topology-termination-final-answer)) |
| **Observer** | What is recorded and where monitors attach: one bus for every sanctioned message, evidence events, a realized-cost ledger, and swarm metrics. ([Observer](swarm.md#observer-evidence-accounting-and-metrics)) |

```python
swarm(
    members=member(deepagent(...), count=4),
    topology="leaderless",
    channels=["filesystem"],
    budget=cost_limit(40.0),
    final="verify",
)
```

A swarm of one member with no channels is a single agent with the same accounting, so it is the natural baseline.

## Main decisions

Decisions marked *Ransom* were taken by Ransom on 2026-10-07; the others are the design's recommendation.

**The swarm owns its members' work, including descendants.** On termination it cancels and awaits everything a member started before it finalises, so nothing edits shared files during the final answer and all usage is counted.
- `background()` attaches work to the sample, not the caller, so M1 rejects background deepagent members.
- Supporting them needs a scoped owner for `background()` in inspect_ai: later, if wanted.
- ([Members](swarm.md#members))

**Limits are soft stopping rules; arms are compared on realized cost.** *Ransom.* Inspect checks limits before and after calls and reserves nothing for calls in flight, so any cap is overshot by the calls in flight. The design does not plan on making limits hard. Instead:
- a ledger records what was actually spent, including descendants, finalisation and overshoot;
- calls it cannot see (cancelled in flight, unpriced, native compaction) are marked unknown or unattributed, not zero;
- the final-answer reserve is best-effort.
([Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply), [Accounting](swarm.md#observer-evidence-accounting-and-metrics))

**The task decides what a member's result is.**
- *Answer-scored tasks* (a final answer string) get per-member scores and team@k, comparable with best@k from epochs. Voting needs a task-defined comparable answer form.
- *Shared-artifact tasks* (scored on the sandbox, like SWE-bench) get one team score. Individual correctness is unavailable unless each member leaves its own artifact.
([Results and scoring](swarm.md#results-and-scoring-a-task-owned-contract))

**Default final answer for leaderless swarms: `verify`, else `vote`, else `first`.** *Ransom.* Use the task's in-loop verifier when it has one, otherwise vote when answers are comparable, otherwise take the first submission. Every member's submission is recorded regardless. For shared-artifact tasks the result is the drained environment. ([Final answer](swarm.md#controller-topology-termination-final-answer))

**Members share the sample's sandbox by default.** *Ransom.* The filesystem is M1's only channel, and bridged members already run there.
- A sandbox per member, or partial isolation, is optional later work. Possible reasons: isolation matching the epochs arm, or containment baselines.
- Concurrent edits, git index locks and resource contention in a shared sandbox are handled by the task's layout: per-member worktrees or scratch directories, and a sandbox sized for the members.
([Sandbox topology](swarm.md#sandbox-topology))

**One bus for all sanctioned communication.** Every message, note, claim or post goes through `deliver()`: monitor, then storm controls, then evidence, then delivery. Nothing else writes to a member's inbox. The filesystem stays an *observed* channel, seen only through tool calls, with the limits that implies. ([The bus](swarm.md#the-bus-one-interception-point), [Security](swarm.md#security))

**Monitoring aligns with inspect_sentinel.** *Ransom.* There is no separate swarm monitor type.
- A send is a tool call, so sentinel's tool stages see it with the sender's identity, and sentinel's per-sample state gives monitors a joint view of the swarm.
- M2 uses sentinel directly if its dispatcher has reached inspect_ai `main`, otherwise a minimal hook in sentinel's action vocabulary.
([The bus](swarm.md#the-bus-one-interception-point))

**Messages reach a member through its agent channel, using inspect_ai internals.** Peer messages are delivered as `UserMessage` items with provenance, which `react()` already handles. The open part is the binder: how the bus obtains each member's channel ref, given ACP's first-binder-wins rule. M2's design settles it, and a public inspect_ai API is a later clean-up. ([The agent channel](swarm.md#the-agent-channel))

**Red-team features are optional and unscheduled.** *Ransom.* Forged senders, secret channels and targeted injection may never be built. The single interception point keeps them possible. ([The bus](swarm.md#the-bus-one-interception-point))

**Align with ORBIT; do not adopt it as the substrate.**
- Reuse its vocabulary: rosters and roles, channels with readers and writers, delivery modes, evidence kinds.
- Its scheduled-activation model and its forked `react()` do not fit continuously active members that wake each other.
([Relationship to ORBIT](swarm.md#relationship-to-orbit), [Alternatives](swarm.md#alternatives-considered))

**Python 3.11+.** *Ransom.* Above inspect_ai's 3.10 floor. The scaffold moves to 3.11 in a separate PR before M1. ([Compatibility](swarm.md#compatibility-and-migration))

## Where each part lives

| Repository | What |
|---|---|
| **inspect_swarm** | `swarm()`, controller, members, budget and ledger, bus and channels, evidence, metrics, scorers, prompts, Scout scanners |
| **inspect_ai** | Only where behaviour needs it: possibly a binder hook (M2), a scoped owner for `background()`, clean `react()` re-entry, per-span usage |
| **inspect_swe** | Enabling and mapping Codex multi-agent v2 and Claude Code agent teams |
| **inspect_sentinel** | Monitors and protocols |
| **ORBIT-like packages** | Scenarios, attacks, defenses |

Detail: [where each part lives](swarm.md#where-each-part-lives).

## Plan

M1, then M2; after that, a menu in any order or in part, driven by user feedback with no internal evidence gate. *Ransom.* ([Implementation plan](swarm.md#implementation-plan))

- **M1: leaderless filesystem swarm with full accounting.** Shared sandbox, cap and ledger, drain, final-answer modes, the result contract, `InfoEvent` evidence and metrics. No inspect_ai changes.
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
