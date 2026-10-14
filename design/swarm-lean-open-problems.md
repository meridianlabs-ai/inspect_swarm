# Inspect Swarm: Epoch's LeanOpenProblems swarm, compared

Status: proposed, 2026-10-13. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

Epoch Research's LeanOpenProblems repository evaluates whether models can settle open conjectures stated in Lean. Its `agent-swarm` branch adds a swarm: for each conjecture it starts N identical, leaderless agents (5 in a smoke config, 32 in an Erdős problem 3 config with a \$1,600 budget), each a `react()` or `deepagent()` in its own Docker or Kubernetes sandbox. The agents talk to each other only through inspect_ai's `memory()` tool, which keeps its files in the sample store, so every member sees one shared board. They draw on the sample's cost and working-time limits as one pool, which the prompt tells them is shared. Each member keeps submitting proofs to a trusted Comparator checker until one passes, and the sample's score records every member's verdict separately.

This document compares that swarm with inspect_swarm's design. The shape matches M1 closely: a leaderless roster started together, each member told its name and the roster size, a pooled budget taken from the sample's limits, members ending one by one, and the task's own scorer used inside each member's loop. Three things differ in ways our M1 cannot absorb as designed. Members have their own sandboxes, where we share one by default. The flagship config uses background deepagents, which M1 rejects. The channel is a `memory()` instance with the default name, which our tool-state scope would make private to each member. A fourth gap is smaller: a member that loops is stopped by a custom limit error, which M1 would treat as fatal to the whole sample.

It recommends, in priority order: per-member sandboxes as an opt-in member field; ending only the member on an agent-raised custom limit; background deepagent members; a member-visible budget view; counts of each member's checked submissions; a stricter meaning of "submitted" that excludes a ReAct agent giving up after refusals; a shared-memory channel routed through the bus; and a restatement of artifact scoring for private sandboxes. The implementation plan puts the stricter `submitted`, the rejection of an integer `background` and the documentation of a shared `memory()` in M1; recommendations 1 to 5 and 7, and the stopping example, in M2, whose test is this port; and recommendation 8 after M2 ([swarm-implementation-plan.md](swarm-implementation-plan.md#where-the-deep-dives-milestone-items-land)). It sketches what a port of their swarm would look like, and concludes that Epoch would be a good early user once the first two recommendations land.

## Why

- **It is the first external swarm built on Inspect that we know of that targets capability scaling**, swarm.md's first user group ([Who the users are](swarm.md#who-the-users-are)): FrontierMath-style research mathematics, with budgets in the thousands of dollars per sample, run at scale on Hawk.
- **It was built without inspect_swarm**, in seven commits over 2026-09-30 to 2026-10-06, so it shows what a team reaches for first when inspect_ai is all it has. Each point where it differs from our plan is either a gap in the plan, an assumption the plan should drop, or a choice we should support or deliberately not support.
- **Its experiment is our first eval question.** The Erdős 3 config's own comment, "32 agents x \$50 = \$1600, shared by the sample's 32 agents" (LeanOpenProblems `configs/erdos3-swarm32-sol61-1600usd.yaml:27-28` at 0aff742d6), sets a 32-member pool against 32 single-agent \$50 attempts: swarm.md's "does a swarm beat more attempts at the same cost" ([Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply)).

## Goals and non-goals

Goals:

- Describe their swarm precisely, with references: topology, member construction, sandbox topology, channel, budget, termination, scoring, logging and configuration.
- Compare it with inspect_swarm's merged designs ([swarm-overview.md](swarm-overview.md), [swarm.md](swarm.md), [swarm-api.md](swarm-api.md), [swarm-communication.md](swarm-communication.md), [swarm-limits.md](swarm-limits.md), [swarm-scoring.md](swarm-scoring.md)), classifying each difference.
- Say whether inspect_swarm as designed could express their experiment, what it would take, and what they would gain and lose.
- Recommend changes to our designs, prioritised and tied to a section and a milestone, with open questions where a recommendation would change a standing decision.
- Judge whether they would be a good early user, and sketch a port.

Non-goals:

- Changing the existing designs. Ransom decides each recommendation separately, and [swarm-implementation-plan.md](swarm-implementation-plan.md) records where each lands.
- Reviewing LeanOpenProblems as a codebase. Observations about their code appear only where they bear on our design; [Not this design](#not-this-design) lists the rest.
- Running their code. It is third-party: it was read as exported text and fetched files, never cloned, installed, imported or run.

## Current behaviour

### Sources

- **LeanOpenProblems** (`epoch-research/LeanOpenProblems`, branch `agent-swarm`, head `0aff742d616a6f2fef2906323308723237421970`, commits 85cdb89ad to 0aff742d6, 2026-09-30 to 2026-10-06). References are `path:line` at 0aff742d6. The touched files were read from the exported patch; `apn/limits.py`, `apn/checker.py`, `apn/tools.py` and `apn/layout.py` were fetched as text at the same commit.
- **inspect_ai at 0.3.273** (`9e44f1b77`, 2026-09-29), the version the Erdős 3 config pins (`configs/erdos3-swarm32-sol61-1600usd.yaml:60`). The smoke config pins 0.3.259 (`configs/b68-solved5-swarm.yaml:72`). References prefixed `inspect_ai:`.
- **inspect_boltons** at `35b794e`, the commit LeanOpenProblems pins (`pyproject.toml:9`), for its `resources()` tool and loop limits. References prefixed `inspect_boltons:`.
- **inspect_swarm's designs** on `main` at `b96353d`, cited by section.

### Their swarm

**Topology and start.** One solver, `lean_swarm`, runs `swarm_size` members in an anyio task group and returns when all have returned (`apn/swarm.py:124-173`). There is no leader, no scheduler and no final step: `state.completed = True` and the sample's output is whatever the solver left, which nothing reads (`:170`).

- Members are named `agent-0` to `agent-{N-1}` (`apn/swarm.py:52-64`).
- Before any member starts, the solver writes the conjecture's Lean sketch into every member's sandbox (`:134-137`).
- Every member gets the same task prompt with `shared_resources=True`, followed by a swarm paragraph (`:139-141`, `:158`, `:89-95`): "You are agent-3, one of 32 peer agents working on this same problem in parallel. Each peer works in its own separate environment [...] Coordinate with your peers through the `memory` tool. The tool is shared by all peers and is the only channel between you."

**How members are built.** Each member's agent is built inside its own task, per invocation, by `build_agent()` (`apn/swarm.py:143-157`, `apn/solver.py:120-161`):

- `agent_type="react"` gives `react()`; `"deep"` gives `deepagent()` with `subagents=[general()]` and `background=64` (`apn/solver.py:37`, `:138-145`). Both configs on the branch use `"deep"` (`configs/b68-solved5-swarm.yaml:17`, `configs/erdos3-swarm32-sol61-1600usd.yaml:13`).
- Tools: `text_editor()`, their own `bash(timeout=300)`, inspect_boltons' `resources()` and `memory()` with the default instance (`apn/swarm.py:147`).
- **Gated submission.** `AgentAttempts(attempts=99_999_999)` with an opaque incorrect message: the agent submits, the task's scorer checks it, and the agent keeps working until a submission passes (`apn/swarm.py:148-151`, `apn/solver.py:41-67`, `:192-201`). `react()` scores each submission with the task's scorers and leaves the loop on the first passing score (`inspect_ai: src/inspect_ai/agent/_react.py:316-326`).
- `on_continue` raises inspect_boltons' custom limits when the model loops: 100 turns without a productive tool call, or 30 repeated texts (`apn/limits.py:6-20`). The limit raises `LimitExceededError("custom")` with no `source` and writes a `SampleLimitEvent` (`inspect_boltons: inspect_boltons/limits.py:59-61`).
- Summary compaction at 300,000 tokens (`apn/swarm.py:156`).
- Each member runs under `run(agent, [ChatMessageUser(content)], name=name)`, so in its own agent span named after it (`apn/swarm.py:161`). A custom limit error is caught there and ends only that member; any other error propagates and ends the sample (`:162-164`).

**Sandbox topology: one sandbox per member, plus a shared verifier.**

- The task's compose file (or Kubernetes values file) has two services: `default`, the agent's workspace, and `comparator`, the trusted proof checker (`apn/task.py:95-127`, `:134-190`). Both have no network (`network_mode: none`, `networkIsolated: True`) and 32 GiB memory limits.
- `swarm_sandbox_config()` replaces `default` with N copies of it, named `default` (member 0) and `agent-1` to `agent-{N-1}`, and keeps one `comparator` (`apn/swarm.py:38-39`, `:67-86`). For the 32-member config that is 32 agent containers of 32 GiB each per sample.
- Each member runs inside `sandbox_default(<its sandbox>)` and `_only_sandboxes([<its sandbox>, "comparator"])` (`apn/swarm.py:159`). The first is public inspect_ai and makes `sandbox()` with no name resolve to the member's own sandbox (`inspect_ai: src/inspect_ai/util/_sandbox/context.py:392-401`, `:416-427`). The second resets inspect_ai's private `sandbox_environments_context_var` so the member's context sees only its own sandbox and the comparator (`apn/swarm.py:17`, `:42-49`).
- Their `bash` and inspect_ai's `text_editor` address the default sandbox (`apn/tools.py:8-41`), so no member tool reaches another member's sandbox or the comparator.
- The comparator is a shared service. One `anyio.Lock`, created per sample, serialises every member's checks (`apn/swarm.py:133`, `apn/scorer.py:72-73`). Each check resets the comparator's `.lake` and staging directories, but, as their own comment says, "does not terminate processes a prior check left behind" (`apn/checker.py:280-283`).

**The channel: a shared `memory()` board.**

- `memory()` keeps its files in the sample store under `MemoryStore`, namespaced only by `instance` (`inspect_ai: src/inspect_ai/tool/_tools/_memory.py:15-18`, `:113`). `run()` does not give a member a store of its own (`inspect_ai: src/inspect_ai/agent/_run.py:75-111`), so every member's default-instance `memory()` reads and writes the same files. This is the hazard swarm.md describes in [The sample store](swarm.md#the-sample-store), used here on purpose as the channel.
- Their deepagent members pass their own `memory()`, so deepagent adds none (`inspect_ai: src/inspect_ai/agent/_deepagent/deepagent.py:191-193`). deepagent hands `general()` the parent's tool list, which includes that `memory()` (`deepagent.py:160-166`, `:328-335`), so background subagents read and write the board too: up to 32 members times 64 subagents.
- The board is mutable and unattributed. `create` overwrites an existing file (`_memory.py:316-326`), any member can `delete`, and `str_replace` fails when its old text is absent (`:329-342`), which acts as a weak compare-and-swap. Files carry no author, so a member can tell who wrote what only if the writers follow a convention. The tool's own description, and deepagent's memory prompt, frame it as the agent's private memory "to recover any earlier progress" (`_memory.py:73-82`, `inspect_ai: src/inspect_ai/agent/_deepagent/prompt.py:49-55`), which the swarm paragraph contradicts.
- The commit subject states the design choice: "Give each swarm agent its own sandbox; memory() is the only channel between them" (335f3ef71). Two other things members share are low-bandwidth: the comparator (opaque verdicts and a leftover-process path, above) and the pooled budget, which `resources()` shows shrinking as peers spend.

**Budget.**

- The configs set eval-set `cost_limit` and `working_limit`, which Inspect applies per sample: \$50 and 72 hours for the 5-member smoke run, \$1,600 and 72 hours for 32 members (`configs/b68-solved5-swarm.yaml:39-41`, `configs/erdos3-swarm32-sol61-1600usd.yaml:27-29`). All members run in the sample, so these are a single pool. Nothing splits it, and one member can spend it all.
- Members are told so in the prompt: "Check remaining resources using the `resources` tool. These resources are a shared budget among all swarm members." (`apn/prompts.py:20-34`; commit e477963f3). `resources()` reads `sample_limits()` (`inspect_boltons: inspect_boltons/tools.py:45-73`), so each member sees the pool's total usage.
- Enforcement is Inspect's own: the sample's cost node raises in whichever member makes the call that crosses it, `run_member` re-raises it, and the task group cancels the rest (`apn/swarm.py:162-168`). Calls in flight in other members are cancelled.
- The model config allows 40 connections (`configs/erdos3-swarm32-sol61-1600usd.yaml:25`), shared by 32 members and their background subagents.

**Termination.** A member ends when a submission passes (react leaves its loop), when a custom loop limit fires (caught, that member only), or when a sample limit fires (the whole sample). `react()` also returns normally without any passing check after three consecutive content-filter responses or an unhandled context overflow (`inspect_ai: src/inspect_ai/agent/_react.py:262-277`, returning at `:408`), so a normal return does not prove that the member's last check passed. The solver returns when every member has ended. A member that proves the conjecture does not stop the others, so they keep spending until each also passes, loops, or the pool or working limit ends the sample. Background subagents a deepagent member started are not awaited when it returns ([`deepagent()` and background subagents](swarm.md#deepagent-and-background-subagents)); they run on until the solver ends.

**Scoring.** One scorer, `swarm_scorer`, serves both phases through a ContextVar (`apn/swarm.py:106-121`, `apn/workspace.py:11-27`):

- **In the loop**, called by a member's `react()`, `current_workspace()` is that member's, so it checks that member's sandbox and claim and returns one `Score` (`apn/swarm.py:109-111`). Their `submit` tool stores the claim ("proof" or "disproof") under a per-member store key (`apn/solver.py:90-114`). That per-member key is a hand-rolled version of what our tool-state scope provides.
- **At the end of the sample**, outside any member, it re-checks every member's sandbox and returns `Score(value={"agent-0": "C", "agent-1": "I", ...})` with `metrics={"*": [accuracy(), stderr()]}` (`apn/swarm.py:112-119`). So the log reports one accuracy per member index across samples. There is no single swarm-level value such as "some member proved it".
- The verifier is the score. Open problems have no target, and the Comparator's verdict is both the in-loop gate and the final grade.

**What is logged.**

- One agent span per member, named after it (`apn/swarm.py:161`), with every in-loop check as an intermediate `ScoreEvent` inside it (`inspect_ai: src/inspect_ai/scorer/_score.py:62-76`).
- Each member's submission tree in sample metadata, `submission_contents_agent-3` (`apn/swarm.py:101-102`, `apn/scorer.py:85-94`).
- Every checked submission as a tar sidecar, `artifacts/<sample uuid>/attempt-NNNNN.tar` (`apn/scorer.py:97-115`). The attempt counter is one store key shared by all members (`apn/scorer.py:48-49`) and the file name has no member, so a sidecar cannot be tied to its member from its name. This is swarm.md's "custom tools that keep state in the store with no instance stay shared" ([Members](swarm.md#members)) in practice.
- Usage: Inspect's per-sample `model_usage` and `role_usage` only. There is no per-member attribution, no stop reason and no record of which member solved first.

**How configs express arms.** Hawk eval-set YAML. An arm is a task item with `swarm_size`, `agent_type`, `literature` and the benchmark as task arguments, and the budget as eval-set `cost_limit` and `working_limit` (`configs/erdos3-swarm32-sol61-1600usd.yaml:1-29`). Arms that differ in task arguments are distinct in an eval set. The smoke config's second item names `benchmark: erdos_autoformalized` (`configs/b68-solved5-swarm.yaml:22`), which the head commit removed from `BENCHMARKS` (`apn/task.py:336-343`; 0aff742d6), so that item fails at task construction.

### Our plan, in brief

From the merged designs; each line links to its section.

- `swarm()` is an `Agent`; members are built agents or `member()` records, and copies (`count`) name a registered `@agent` factory and its arguments, from which the runtime builds each member's agent in each sample; a `@swarm_controller` decides who starts and when the swarm is done; `@swarm_channel` objects say how members share information ([swarm-api.md](swarm-api.md#the-api-at-a-glance)).
- M1: a leaderless swarm with the filesystem of a shared sandbox as its channel; members' work must stay inside their invocation, so background deepagents are rejected; each member's built-in tool state is private through a tool-state scope ([swarm.md M1](swarm.md#implementation-plan), [Members](swarm.md#members), [Sandbox topology](swarm.md#sandbox-topology)).
- Budget: caps derived from the sample's limits minus a 5% reserve, a pool by default with optional splits, a soft stop with grace, and a ledger of realized cost per member with stated coverage ([swarm-limits.md](swarm-limits.md#the-budget-parameter)).
- Final answer `first` in M1; a per-member result record in the store; the task's scorers unchanged; per-member scores, `verify`, `vote` and `baseline()` after M2 ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)).
- M2: the bus with a shared-memory channel, fenced tool-output delivery and metadata-only notices ([swarm-communication.md](swarm-communication.md)), and most of the recommendations below ([swarm-implementation-plan.md](swarm-implementation-plan.md)); direct messages, notes, task list and board later.

## Comparison

Each row classifies the difference: **match**; **gap** (their swarm needs something our plan lacks); **assumption** (our plan assumed something their use contradicts); **support** (a choice we should support); **not support** (a choice we should deliberately leave out).

| Aspect | LeanOpenProblems | inspect_swarm | Verdict |
|---|---|---|---|
| Topology | Leaderless; all members start together with the same input | `leaderless`, M1 ([Controller](swarm.md#controller-topology-termination-final-answer)) | Match |
| Member preamble | Name, roster size, "you work in separate environments", the channel (`apn/swarm.py:89-95`) | Name, role, roster size and each channel's instructions ([swarm-api.md Controllers](swarm-api.md#controllers), `start()`) | Match; the "separate environments" line needs the sandbox topology to be known to the swarm |
| Member construction | Agent built per invocation inside the member's task | In the factory form, agent built per member per sample inside the member's task, from the record's factory and arguments; member builders for hooks ([swarm-api.md, the member contract](swarm-api.md#the-member-contract)) | Match: their builder is a registered member builder in our terms |
| Member agents | `react()` or `deepagent(background=64)` | `react()`, synchronous `deepagent()`, bridged; background rejected in M1 ([Members](swarm.md#members)) | **Gap.** Both their configs use background deepagents ([Recommendation 3](#3-m2-background-deepagent-members)) |
| Sandbox | One per member, network-isolated, plus one shared trusted service | One shared sandbox by default; per-member an M2 opt-in ([Sandbox topology](swarm.md#sandbox-topology)) | **Gap; support.** The first concrete external user needs per-member sandboxes, and our reason for sharing (the filesystem is M1's only channel) does not apply when the channel is elsewhere ([Recommendation 1](#1-m2-per-member-sandboxes-as-an-opt-in-member-field)) |
| Channel | Shared default-instance `memory()` board | Filesystem in M1; a shared memory through the bus in M2; direct messages, notes, tasks, board later; a shared tool-state instance is an "observed channel" ([Channels](swarm.md#channels), [swarm-communication.md](swarm-communication.md#tool-state-in-the-sample-store)) | **Assumption; support.** Our design treats a shared store-backed tool as an accident to prevent; they chose it as the channel. The scope keeps it possible with an explicit `instance` ([Recommendation 7](#7-m2-a-shared-memory-channel-through-the-bus)) |
| Budget | Sample cost and working limits as one pool; "shared" stated in the prompt; one member can spend it all | `Budget()` derives a pooled cap from the same sample limits; `split` makes shares an experimental condition ([swarm-limits.md](swarm-limits.md#splits)) | Match for the default; ours adds the soft stop, reserve and splits |
| Budget visibility | `resources()` shows the sample's limits and usage | Nothing for members; the swarm cap and shares are invisible to `sample_limits()` | **Gap.** Members told to pace themselves need to see the cap that will stop them ([Recommendation 4](#4-m2-members-can-see-the-budget-that-will-stop-them)) |
| Stopping on budget | Sample limit: raised in one member, siblings cancelled, in-flight calls lost | Soft stop at the derived cap, grace, then hard stop; ledger marks unknowns ([swarm-limits.md Stopping](swarm-limits.md#stopping)) | Match in kind; ours records the calls theirs loses |
| Working limit | 72 hours, sample-level | Accepted with a warning: sample working time collapses while members queue for connections ([swarm-limits.md](swarm-limits.md#each-limit-kind)) | Match; our warning applies to them: 32 members and their subagents on 40 connections |
| Member-level stop | Custom loop limit ends only that member (`apn/swarm.py:162-164`) | A `LimitExceededError` whose source is not a swarm node is foreign: it errors the member and ends the sample ([swarm-limits.md Exhaustion](swarm-limits.md#exhaustion-and-errors)) | **Gap** ([Recommendation 2](#2-m2-an-agent-raised-custom-limit-ends-only-the-member)) |
| Termination | All members ended, or a sample limit | M1: all members ended, the swarm cap or time; after M2 also a verified answer ([Controller](swarm.md#controller-topology-termination-final-answer)) | Match; from M2 a custom controller can stop on the first proof, provided the member's builder makes a normal return mean a passed check, which for Epoch's members needs M2's member-local custom limit ([Recommendation 6](#6-m1-tighten-submitted-m2-document-stopping-on-the-first-proof)) |
| In-loop scoring | Each member's `react()` calls the task's scorer per submission | Allowed; the scorer is part of the trajectory ([swarm-api.md Scores](swarm-api.md#scores-the-task-scores-the-observer-records)) | Match |
| Final answer | None; the scorer re-checks every member | `first`; after M2 the verify, vote, first chain | Match: unused by them and harmless. M1 gives `react()`'s returns after refusals or context overflow `unsubmitted`, but `first` picks a verified member only if the builder makes a normal return mean a passed check ([Recommendation 6](#6-m1-tighten-submitted-m2-document-stopping-on-the-first-proof)) |
| Per-member scores | A dict value keyed by member, one accuracy per index | After M2: `member_scores` gives `best_member` (team@k) and `mean_member` ([swarm-scoring.md](swarm-scoring.md#answer-scored-tasks-per-member-scores)) | **Not support** per-index metrics: accuracy of "agent-3" across samples is not a swarm measure; `best_member` and `mean_member` are |
| Result kind | Scored on each member's sandbox: an artifact, one per member | "Artifact" tasks assume one shared environment; per-member artifacts need `materialize()` ([swarm-scoring.md](swarm-scoring.md#shared-artifact-tasks-and-per-member-artifacts)) | **Gap.** Per-member sandboxes make per-member artifacts trivial and "the drained environment" undefined ([Recommendation 8](#8-after-m2-per-member-sandboxes-in-the-scoring-contract)) |
| Attempts | Many submissions per member; every check is a `ScoreEvent` and a sidecar | "Each member submits at most once in M1" ([swarm-scoring.md Terms](swarm-scoring.md#terms)); internal attempts not counted | **Gap.** Gate checks and time to the first passing check are the outcome they care about ([Recommendation 5](#5-m2-gate-checks-as-metrics)) |
| Tool state | Shared board on purpose; claims keyed per member by hand; attempt counter shared by accident | Per-member scope by default; explicit instance shared on purpose ([Members](swarm.md#members)) | Match in intent. The scope covers built-in tools only; their claim keys and attempt counter are custom store keys and stay as task code scopes them |
| Logging | Member spans, per-member submission trees, sidecars, sample usage | Member spans and timelines, a result record, evidence, a ledger, stop reasons, harness-validity checks ([Observer](swarm.md#observer-evidence-accounting-and-metrics)) | Match; ours adds what they lack |
| Arms | Hawk YAML; task arguments; eval-set limits | Task parameters; `Budget` derived from the same limits; faithful logging ([swarm-api.md Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)) | Match: a Hawk config needs no new mechanism |
| Scale | 32 members, up to 64 background subagents each, 40 connections | "Tens of members, not thousands" ([Non-goals](swarm.md#non-goals-for-the-first-versions)) | Match by member count; hundreds of agent loops once background subagents are allowed |

## Design

This is an analysis, so its "design" is the answer to the task's questions: whether inspect_swarm could express their experiment, what a port would look like, and whether they would be a good early user. [Recommended changes](#recommended-changes-to-our-designs) lists what follows for our designs.

### Could inspect_swarm express their experiment?

**Not as designed for M1, for four reasons, each fixable.**

1. **Background deepagents are rejected.** Both configs use `deepagent(background=64)`. M1 accepts only members whose work stays inside their invocation, and lifting that needs an inspect_ai change giving `background()` a scoped owner ([Members](swarm.md#members)). A port could run `deepagent()` synchronously, but that is a different arm: subagent calls block the member instead of running beside it.
2. **The swarm does not know about per-member sandboxes.** Nothing in M1 stops a member builder from entering `sandbox_default()` itself, as their `run_member` does, so the topology can be reproduced by hand ([port](#a-port-sketch)). But the swarm's preamble and the `filesystem` channel would describe a shared sandbox, the result record would not say which sandbox each member used, and a scorer that calls `sandbox()` after the swarm would see `default`, which is member 0's.
3. **The board becomes per member.** Under the tool-state scope, `memory()` with the default instance is private to each member ([Members](swarm.md#members)). Ported unchanged, every member would get an empty private memory and believe the prompt that it is shared. A port must name the instance, `memory(instance="board")`; our design then treats it as an observed channel shared on purpose ([swarm-communication.md](swarm-communication.md#tool-state-in-the-sample-store)). The failure mode is silent, which is a documentation point for M1 ([Recommendation 7](#7-m2-a-shared-memory-channel-through-the-bus)).
4. **A looping member would end the sample.** Their custom loop limit has no `source`. Our member wrapper classifies a limit error by whether its source is one of the swarm's or the member's nodes; anything else is foreign, so the member is `errored` and the error propagates, cancelling every member ([swarm-limits.md](swarm-limits.md#stopping), "Member wrapper and statuses"). A builder that caught the error and returned would make the member's status `submitted`, which `first` would select. Neither is right. The custom limit stays foreign until M2 ([Recommendation 2](#2-m2-an-agent-raised-custom-limit-ends-only-the-member)). (A `react()` member that returns after three refusals or an unhandled context overflow is `unsubmitted` in M1, not `submitted`: [Recommendation 6](#6-m1-tighten-submitted-m2-document-stopping-on-the-first-proof).)

**With recommendations 1 and 2 it can, except for background subagents.** Their experiment then needs: 32 member records each naming its sandbox, a deliberately shared `memory(instance="board")`, `Budget()` deriving the pooled cap from the Hawk `cost_limit`, their scorer unchanged, and a synchronous deepagent until recommendation 3 lands.

### What they would gain and lose

Gains, in M1:

- **An orderly stop.** At 95% of the pool members stop at their next call and in-flight calls finish and are recorded, instead of a sample limit cancelling 31 members' calls mid-flight ([swarm-limits.md Stopping](swarm-limits.md#stopping)).
- **A ledger.** Realized cost per member, including its subagents and compaction, with flags that say when totals are lower bounds ([The ledger](swarm-limits.md#the-ledger)). Today they have only the sample total. For a \$1,600 sample, "which members spent the money" is the first question after "did anyone prove it".
- **Stop reasons and statuses.** Why the swarm stopped and how each member ended, rather than inferring it from the transcript.
- **A swarm-of-one baseline with the same accounting**, `swarm(members=agent, channels=[])`, for their \$50 single-agent arm ([swarm-scoring.md Comparisons in M1](swarm-scoring.md#comparisons-in-m1)).
- **Per-member built-in tool state** by default. Their custom attempt counter stays sample-wide unless task code scopes it explicitly (with `tool_state_instance()`); making it member-local also needs member-qualified sidecar names, since per-member counters would otherwise collide on `attempt-NNNNN.tar`.
- **Harness-validity checks** (a member that never acted, members stuck in loops) reported apart from the score ([Observer](swarm.md#observer-evidence-accounting-and-metrics)).
- **Budget splits** as a sweepable condition, if they want to test whether one member starving the others matters ([Splits](swarm-limits.md#splits)).

Gains in M2, beyond what the port needs to run:

- **Early stop.** A ten-line custom controller stops the swarm when the first member ends `submitted`, once the member builder raises a member-local custom limit on any return without a passing check, so that `submitted` means verified ([Recommendation 6](#6-m1-tighten-submitted-m2-document-stopping-on-the-first-proof)). It needs M2: before M2-2 that custom limit has no `source`, so it is foreign and ends the sample ([Recommendation 2](#2-m2-an-agent-raised-custom-limit-ends-only-the-member)).
- A shared-memory channel through the bus, with notices on change and evidence of who wrote what ([Recommendation 7](#7-m2-a-shared-memory-channel-through-the-bus)).
- Monitoring of board writes: sentinel's tool stages see native members' channel tool calls, and the bus hook sees every record.

Gains after M2, if they choose them:

- `member_scores` with `best_member` (team@k) and `mean_member`, `baseline()`, `best_at(k)` and select@k, compared on realized cost ([Comparison quantities](swarm-scoring.md#comparison-quantities)).
- The messages channel with notices.

Losses and costs:

- **Background deepagents** until the scoped owner exists, in M2: the one change they cannot work around.
- **inspect_ai version coupling.** inspect_swarm tracks inspect_ai `main` and needs its registry types and the tool-state scope; their configs pin 0.3.273 "in sync with epoch-research/benchmarks@main" (`configs/erdos3-swarm32-sol61-1600usd.yaml:57-60`). Python is not a problem: they require 3.13 (`pyproject.toml:7`), inside our 3.11+.
- **A young dependency** with M1's API still settling.
- **Their own conveniences.** `_only_sandboxes` uses a private inspect_ai name; a port keeps it, as inspect_swarm may ([Goals](swarm.md#goals)). Their per-sample check lock needs a new home: their builder is called once per member, so a lock it created would be per member, while the lock must be shared by the members of one sample ([swarm-api.md](swarm-api.md#the-member-contract)).

### A port, sketch

A sketch, not an implementation: it assumes recommendations 1 and 2 have landed (both M2) and recommendation 3 has not, and shows where each part of their swarm goes. With recommendation 3, also M2, their builder keeps `background=64`. Names from LeanOpenProblems are theirs; nothing here was run.

```python
# apn/swarm_inspect.py (sketch)
from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.tool import memory, text_editor
from inspect_ai.util import LimitExceededError, store
from inspect_swarm import (
    Budget, Channel, Controller, MemberEnded, StopReason, SwarmControl,
    member, swarm, swarm_channel, swarm_controller,
)


@swarm_channel
def memory_board() -> Channel:
    """Instructions only (M1): the board itself is a shared memory() instance in each member's tools."""

    class _Board(Channel):
        def instructions(self, m) -> str:
            return ("Coordinate with your peers through the `memory` tool. It is shared by "
                    "all peers and is the only channel between you. Prefix files you create "
                    "with your name.")

    return _Board()


@agent
def lean_member(
    name: str, sandbox_name: str, agent_type: AgentType = "react", board: str = "board"
) -> Agent:
    # Called once per member per sample (swarm-api.md, the member contract), so `inner` is this member's own.
    inner = build_agent(
        agent_type,                                 # "deep" must not set background in M1
        tools=[text_editor(), bash(timeout=300), resources(), memory(instance=board)],
        attempts=AgentAttempts(attempts=99_999_999, incorrect_message=gated_incorrect_message),
        submit=AgentSubmit(tool=submit(), name="submit_proof", keep_in_messages=True),
        on_continue=continue_unless_looping("Continue working on the problem."),
        compaction=CompactionSummary(threshold=300_000),
    )

    async def execute(state: AgentState) -> AgentState:
        # The runtime has entered sandbox_default(sandbox_name) (Recommendation 1). Their
        # workspace ContextVar keeps the in-loop scorer pointed at this member.
        set_workspace(Workspace(
            name=name, sandbox=sandbox_name, claim_key=f"{CLAIM_STORE_KEY}:{name}",
            check_lock=comparator_lock(),    # per sample: keyed on the sample's comparator sandbox
        ))
        state = await inner(state)           # a custom loop limit ends only this member (Rec. 2)
        # react() also returns normally after three refusals or an unhandled context
        # overflow; make a normal return mean a passing check (Rec. 6).
        if not store().get(f"verified:{name}", False):   # set by their scorer on a pass
            raise LimitExceededError("custom", value=0, limit=0,
                                     message=f"{name} returned without a verified proof")
        return state

    return execute


@swarm_controller
def until_proved() -> Controller:
    """Stop when the first member ends submitted. Valid only because lean_member raises on an unverified return."""

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async for event in swarm.events():
            if isinstance(event, MemberEnded) and event.status == "submitted":
                return "proved"              # recorded as controller:proved; others soft-stop
        return "all_done"

    return Controller(run)


@task
def apn_swarm(
    benchmark: Benchmark,
    swarm_size: int = 3,
    literature: bool = False,
    agent_type: AgentType = "react",
    sandbox_backend: SandboxBackend = "docker",
    stop: Literal["all_done", "first_proof"] = "all_done",
) -> Task:
    dataset_dir, dataset_fn = BENCHMARKS[benchmark]
    pin = fc_commit(dataset_dir)
    members = [
        member(
            lean_member,
            args={"name": f"agent-{i}", "sandbox_name": _sandbox_name(i), "agent_type": agent_type},
            name=f"agent-{i}",
            sandbox=_sandbox_name(i),        # Recommendation 1: "default", "agent-1", ...
            visible_sandboxes=["comparator"],  # Recommendation 1: shared service the scorer needs
        )
        for i in range(swarm_size)
    ]
    return Task(
        dataset=dataset_fn(...),
        setup=[
            write_sketch_to_member_sandboxes(swarm_size),     # their solver's first step (swarm.py:134-137)
            lean_user_prompt(literature=literature, util_module=fc_profile(pin).util_module),
        ],
        solver=swarm(
            members=members,
            controller=until_proved() if stop == "first_proof" else "leaderless",
            channels=[memory_board()],       # not "filesystem": nothing is shared on disk
            budget=Budget(),                 # pooled cap = 95% of Hawk's cost_limit
        ),
        scorer=swarm_scorer(SandboxComparator(), swarm_size=swarm_size),   # unchanged
        sandbox=swarm_sandbox_config(pin, literature, sandbox_backend, swarm_size),  # unchanged
    )
```

Where each part of their swarm goes:

- **Members.** One record per member, because each names its own sandbox and its builder takes the member's name and sandbox (a counted member would use Recommendation 1's `{member}` template, and its builder could read its copy's name from `tool_state_instance(None)`, which returns the member's tool-state scope). Their builder becomes a registered `@agent` with ordinary parameters, passed uncalled with its arguments in `args`, so the plan logs it faithfully and the runtime builds each member's agent from it ([swarm-api.md Members](swarm-api.md#members)). `name=f"agent-{i}"` keeps their names; count expansion would give `agent-1` to `agent-32`.
- **Prompt.** Their task prompt, with its "shared budget" sentence, moves to a setup solver that sets the sample input, as their single-agent `lean_prover` does (`apn/solver.py:222-227`). Their swarm paragraph becomes the swarm's preamble (name, roster size) plus `memory_board()`'s instructions. Once Recommendation 4 lets the preamble state the split, the budget sentence can come out of their prompt.
- **Channel.** `memory(instance=board)` in every member's tools. Under the tool-state scope an explicit instance is used as given, so all members and their `general()` subagents share it; deepagent adds no memory of its own, because one is already present. `board` is a builder parameter, so the shared instance is in the plan's logged parameters and two arms that name different boards are distinct.
- **Budget.** `Budget()` reads the Hawk config's per-sample `cost_limit` (\$1,600) and stops at \$1,520, keeping \$80 for overshoot. Their 72-hour `working_limit` gets the swarm's start-up warning ([swarm-limits.md](swarm-limits.md#the-budget-parameter)). Cost data comes from their `model_cost_config`.
- **Termination.** `leaderless` reproduces today's behaviour; `until_proved` adds the early stop as a separate arm. Either way the stop reason is recorded.
- **Scoring.** Their scorer gains one line: on a passing in-loop check it sets `verified:<member>` in the store, which the builder reads when the inner agent returns. Otherwise it is unchanged. In the loop it still runs inside the member's context, where the runtime has entered the member's sandbox (Recommendation 1) and the builder has set their workspace ContextVar. At the end it re-checks every member. Because the builder turns every unverified return into `member_limit:custom`, `first` picks the earliest verified member's output, which their scorer ignores; the result record says which member it was. A member that wrote a valid proof but was stopped before `submit_proof` still passes the end-of-sample re-check without being `submitted`, which is why [Recommendation 8](#8-after-m2-per-member-sandboxes-in-the-scoring-contract) keeps the final-artifact grade apart from the selected answer.
- **Check lock.** Their per-sample `anyio.Lock` cannot be created in the builder, which runs once per member, because the members of one sample must share it, nor held on any object every sample shares. `comparator_lock()` returns one lock per sample from a module-level `weakref.WeakKeyDictionary` keyed on the sample's comparator `SandboxEnvironment`, with no new API ([Not this design](#not-this-design)).
- **Arms in Hawk.** Unchanged in shape: `swarm_size`, `agent_type` and a new `stop` are task arguments, so arms stay distinct; `cost_limit` and `working_limit` stay eval-set options.
- **What it cannot do yet.** `agent_type="deep"` with background dispatch, until recommendation 3 (M2). The port must disable background dispatch in its builder. Direct background deepagents are rejected at construction; a custom wrapper such as `lean_member` hides its inner agent's parameters from the registry (`inspect_ai: src/inspect_ai/agent/_agent.py:173-208`), so it must obey the documented restriction itself ([Members](swarm.md#members)).

### Would they be a good early user?

**Yes, once recommendations 1 and 2 land, and better once 3 does.**

- They are swarm.md's first user group exactly, and their headline comparison (a 32-member pool against 32 × \$50 attempts) is our first eval question. The things our plan adds (realized cost per member, an orderly stop, a swarm-of-one baseline, team@k against best@k) are what that comparison needs and what their current code lacks.
- Their task is verifier-gated with no target, which exercises in-loop scoring as a gate, whose metrics are M2's (Recommendation 5), and paths that stay later work: `verify` semantics and per-member artifacts.
- They already run Inspect at scale on Hawk, so the port changes their solver, not their pipeline.
- Their choices test our defaults: per-member sandboxes against our shared default, a shared memory against our scoped tool state. Feedback from them would be concrete.
- Risks: their \$1,600 runs make a library bug expensive, and their pinned inspect_ai version would have to move to one with the registry types, the tool-state scope and the scoped owner for `background()`. A first port on the 5-member smoke config would limit both.

## Alternatives considered

For the recommendations below, the main alternatives:

**Leave per-member sandboxes to task code** (their approach: a member builder enters `sandbox_default()` itself). No change to M1. But the swarm then misdescribes the topology in every member's preamble, records no sandbox per member, and gives scorers no way to find a member's environment except through task-private state. It also needs inspect_ai's private `sandbox_environments_context_var` to hide other members' sandboxes. It is what a port does today; it is not a topology the swarm can record or analyse.

**Make per-member sandboxes the default.** It matches the k-epochs arm's isolation and their choice. But it removes M1's only channel for every other user, multiplies sandbox cost by k, and contradicts the decision that members share the sample's sandbox by default (decision: Ransom, 2026-10-07). Not recommended.

**Treat every `LimitExceededError` with no `source` as the member's own.** Simpler than also matching `type="custom"`. But `source` is optional on every limit error (`inspect_ai: src/inspect_ai/util/_limit.py:40-65`), and a future sample-level error raised without one would be swallowed as a member status. Matching `type="custom"` and `source is None` together keeps every built-in sample limit propagating.

**Treat any custom error whose source is not a swarm or sample node as the member's own.** Wider than `source is None`: it would also capture a custom error sourced from some other limit node, whose owner and scope the swarm cannot know. No such node exists in inspect_ai or inspect_boltons today (the boltons errors have no source), so the narrower predicate covers the real use and anything else keeps the existing propagation rule.

**Let a builder catch the custom limit and return.** Works today with no change, but the member's status becomes `submitted`, `first` can select an unverified looping member, and the stop is not recorded. Rejected.

**Count every intermediate `ScoreEvent` as an attempt.** The simplest rule, and right for Epoch's one-scorer task. But `score()` writes one event per task scorer (`inspect_ai: src/inspect_ai/scorer/_score.py:62-76`) while `react()` decides on the first score alone (`_react.py:321-326`), so a task with two scorers would count one submission twice and could report a secondary scorer's pass as the first pass. Recommendation 5 counts only the gate scorer's events.

**Count attempts outside the swarm, in analysis over `ScoreEvent`s.** Possible from the log alone, since intermediate `ScoreEvent`s sit in member spans. But every user with gated members would rebuild it, and harness-validity checks and the result record would still say "submitted once". Recommendation 5 makes it a metric.

**A notes channel instead of a shared-memory channel.** Notes are append-only typed entries, which avoid the board's overwrite and delete hazards. But the only external swarm we have seen chose a mutable file board, and memory's command set is what models already use. Recommendation 7 keeps notes for later and builds memory's shape first ([Open questions](#open-questions), item 2).

## Compatibility and migration

What the recommendations change:

- **Recommendation 1** adds two optional `Member` fields (`sandbox`, `visible_sandboxes`) and record fields. Additive; logged as plain values, so they enter `task_identifier()` ([swarm-api.md Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)). A shared-sandbox swarm is unchanged.
- **Recommendation 2** changes the member wrapper's classification for one error shape, `type="custom"` with `source is None`. Today that shape ends the sample; afterwards it ends the member with status `member_limit:custom`. No existing swarm exists to break.
- **Recommendation 4** adds a tool and a function; members that do not use them see nothing new.
- **Recommendation 5** adds metric fields under the observer's versioned record ([swarm-api.md Observer](swarm-api.md#observer)).
- **Recommendation 6** splits normal returns into `submitted` and a new status, `unsubmitted`, in the closed status set. A `react()` member that returns after refusals or an unhandled overflow is `unsubmitted`, and `first` never selects it.
- **Recommendation 7** is a new `@swarm_channel` in M2; the existing shared-instance behaviour stays as an observed channel.
- **Porting from a hand-rolled swarm.** A swarm that relied on the default `memory()` instance being shared loses its channel silently under the tool-state scope. The M1 documentation for `member()` should say so with this example (Recommendation 7, M1 part).

## Security

- **Third-party code.** LeanOpenProblems was handled as untrusted text: read from an exported patch and files fetched with `gh api`, never cloned, installed, imported or run, and no agent was started in a checkout of it. Its prompts and comments were read as data.
- **The board is peer model output, unfenced and unattributed.** A member reads peers' text as `memory()` tool output with no sender and no fence, and any member or subagent can overwrite or delete another's files. This is our "observed channel" case ([Security](swarm.md#security)): the bus sees none of it, and `StoreEvent`s misattribute writes under concurrent spans; the `ToolEvent`s in each member's span are the only attribution. Recommendation 7 would bring sender binding, fencing and evidence to it.
- **Per-member sandboxes are a containment layout.** Their network-isolated, separate containers make "memory is the only channel" close to true, which our shared default cannot offer. Two residual paths remain and should be stated by any analysis that relies on isolation: the shared comparator, where a prior check's processes survive the reset (`apn/checker.py:280-283`) and members' verdicts are fixed strings (`apn/solver.py:41-52`); and the pooled budget, whose consumption every member sees through `resources()`. Recommendation 1's restriction keeps other members' sandboxes out of a member's context, but a service listed in `visible_sandboxes` stays reachable by name from any member tool that takes a sandbox name, so listing one is a trust decision for the eval author. Their swarm builds its tools with no sandbox name (`apn/swarm.py:147`, `apn/tools.py:8-13`).
- **What the recommendations add.** Sandbox names come from the eval author, never from members, and are validated against the sample's environments at start. The custom-limit rule reads only the error's type and source. The budget view reports numbers from the swarm's own nodes; it gives a member no handle on them. A shared-memory channel's payloads are model output and follow M2's fencing and storm controls.

## Testing

This document's claims were checked by reading, not running: LeanOpenProblems at 0aff742d6, inspect_ai at tag 0.3.273 and inspect_boltons at `35b794e`. Each claim about our designs cites the section it comes from.

Tests the recommendations would need, in inspect_swarm unless noted (mockllm, asyncio and trio, no network):

- **Recommendation 1:** with a stub sandbox environment registered per name (no Docker), members with `sandbox=` resolve `sandbox()` to their own environment and cannot resolve another member's by default; `visible_sandboxes` adds the named service; an unknown sandbox name raises at swarm start; the result record lists each member's sandbox; `{member}` templates expand under `count`. One Docker test, marked slow, runs two members against a two-service compose file.
- **Recommendation 2:** a member whose `on_continue` raises `LimitExceededError("custom")` with no source ends with `member_limit:custom` while its siblings continue; the same error grouped by a task group inside the agent gives the same status; a group mixing it with a foreign error re-raises the foreign leaf; a custom error with a source that is not a swarm node, and a sample-limit error, still propagate; `first` never selects the stopped member.
- **Recommendation 4:** the budget tool reports the swarm cap, pool usage and the member's share under `pool`, `equal` and weights, and stays consistent with the ledger after a run.
- **Recommendation 5:** a `react(attempts=...)` member with scripted failing then passing submissions records two gate checks, one pass and the pass's time; one submission under two task scorers records one gate check; a failing first scorer with a passing second scorer records no pass; a deepagent member's subagent that calls `score()` does not add to the member's own count; a member with no in-loop scoring records zero.
- **Recommendation 6:** a `react()` member given three content-filter outputs ends `unsubmitted` while a sibling continues, later submits and is selected by `first`; a member ending on an unhandled `model_length` overflow is `unsubmitted`; the example controller ignores the refusing member, stops the swarm at the sibling's submission with reason `controller:proved`, and soft-stops the rest; a builder that raises a member-local custom limit on an unverified return ends that member `member_limit:custom`.
- **Recommendation 3** (inspect_ai): its own tests, with the scoped `background()` owner; in M1, `member()` rejects a `deepagent` member with `args={"background": 64}` as well as `True`.
- **Recommendation 7** is tested in its own design (D-1, for M2), and **Recommendation 8** in its own, after M2.

## Recommended changes to our designs

In priority order. Each names the section it would change and the milestone. The headings and milestone lines give the milestone [swarm-implementation-plan.md](swarm-implementation-plan.md#where-the-deep-dives-milestone-items-land) places each in.

### 1. M2: per-member sandboxes as an opt-in member field

**Change.** Add `member(sandbox: str | None = None, visible_sandboxes: Sequence[str] = ())`. With `sandbox` set, the runtime enters `sandbox_default(sandbox)` for the member and restricts the member's sandbox environments to its own plus the listed services (the same reset of `sandbox_environments_context_var` their `_only_sandboxes` makes), so `sandbox(name)` in the member's context cannot reach another member's sandbox. A `{member}` template expands to each copy's name, so `member(factory, name="agent", count=32, sandbox="{member}")` runs `agent-1` to `agent-32` in services of the same names. The swarm validates at start that every named environment exists, and raises `ValueError` naming the member otherwise. The result record and evidence list each member's sandbox. The preamble says whether members share a sandbox, and `swarm()` raises `ValueError` if `filesystem` is listed while any member has its own sandbox.

**Sections.** [swarm.md Sandbox topology](swarm.md#sandbox-topology) ("a sandbox per member" as an M2 opt-in; the default stays shared); [swarm-api.md Members](swarm-api.md#members) (two fields); [swarm-scoring.md What the swarm records](swarm-scoring.md#what-the-swarm-records) (a `sandbox` field on `MemberResult`).

**Why.** The first external swarm we have seen needed it from its second commit. It is also the isolation the k-epochs arm has, which swarm.md already names as a reason. The routing is small; building the compose file stays the task's job, as theirs already does.

**Milestone.** M2, as an opt-in (M2-1 in [swarm-implementation-plan.md](swarm-implementation-plan.md#m2-pull-requests)). First internal use runs on the shared default ([Open questions](#open-questions), item 1).

### 2. M2: an agent-raised custom limit ends only the member

**Change.** In the member wrapper's classification, a `LimitExceededError` with `type="custom"` and `source is None` is the member's own: the member ends with status `member_limit:custom`, the siblings continue, and the error does not propagate. Bare or grouped, as for the member's own nodes; a group that also holds a foreign leaf re-raises only the foreign leaves, as today. A custom error that carries a source keeps today's rule.

**Sections.** [swarm-limits.md Stopping](swarm-limits.md#stopping) ("Member wrapper and statuses") and [Exhaustion and errors](swarm-limits.md#exhaustion-and-errors).

**Why.** inspect_boltons' loop limits are the stop agents use for "this agent is stuck", and in a swarm a stuck member is one member's problem. Their code catches exactly this case (`apn/swarm.py:162-164`). Under our current rule it would end the sample, and a builder workaround would mislabel the member `submitted`.

**Milestone.** M2 (M2-2). Until M2 such an error ends the sample loudly, as it does for any agent today.

### 3. M2: background deepagent members

**Change.** Build "background deepagents as members" (the inspect_ai scoped owner for `background()`). In M1, make sure the rejection catches an integer `background` (`deepagent(background=64)`), not only `True`.

**Sections.** [swarm.md Members](swarm.md#members) and M2 in [Implementation plan](swarm.md#implementation-plan); [swarm-api.md Members](swarm-api.md#members) (the rejection).

**Why.** Both of their configs use background deepagents. The plan follows user feedback (decision: Ransom, 2026-10-07); this is that feedback. Without it a port changes their arm. It needs no standing decision changed.

**Milestone.** M2: a design (D-2, started during M1), the inspect_ai scoped owner (AI-5) and the swarm side (M2-5). The rejection of an integer `background` is M1 (M1-3).

### 4. M2: members can see the budget that will stop them

**Change.** Provide a `swarm_budget()` function and a `swarm_resources()` tool that report the swarm's cap per kind, the pool's usage, the member's own share and usage when the budget is split, and the time cap. The preamble states the split in a fixed sentence ("the budget is shared by all members" for `pool`).

**Sections.** [swarm-limits.md The budget parameter](swarm-limits.md#the-budget-parameter) and [Splits](swarm-limits.md#splits); [swarm-api.md Controllers](swarm-api.md#controllers) (the preamble).

**Why.** Their members are told to check `resources()` and pace themselves. Under a swarm, `sample_limits()` shows the sample's limit, not the swarm cap 5% below it, and nothing shows a member's share, so a member pacing itself on `resources()` paces against the wrong number. The swarm owns the nodes, so it is the only thing that can report them.

**Milestone.** M2 (M2-3), after the budget nodes and splits; small.

### 5. M2: gate checks as metrics

**Change.** Record, per member, the in-loop checks of its own submissions: `gate_checks` (how many submissions were checked), `gate_passes`, and the swarm time of the first passing check. A gate check is an intermediate `ScoreEvent` from the task's **first** scorer, because `react()` decides on `answer_scores[0]` alone (`inspect_ai: src/inspect_ai/agent/_react.py:321-326`), whose nearest enclosing agent span is the member's own, not a descendant subagent's. A pass is that score's value at 1.0 under Inspect's default `value_to_float`, which is what `AgentAttempts.score_value` defaults to; a member configured with its own `score_value` gets checks counted but passes reported as unknown. Events from the task's other scorers are not checks. Add the three fields to the metrics and to `MemberResult`, and refine swarm-scoring.md's "each member submits at most once in M1" to say that a member's own retries are visible as gate checks and counted.

Two limits are stated with the metric. `react()` does not score the submission that uses up its last permitted attempt (`_react.py:316-319`), so a member that exhausts a finite `attempts` has one more submission than checks. And an agent that checks submissions some other way (a custom tool, a bridged CLI's own loop) records none; its task supplies its own count.

**Sections.** [swarm.md Observer, Metrics](swarm.md#observer-evidence-accounting-and-metrics); [swarm-scoring.md Terms](swarm-scoring.md#terms) and [What the swarm records](swarm-scoring.md#what-the-swarm-records).

**Why.** For a gated task, how many submissions each member had checked and when the first one passed are the outcome; their tar sidecars exist to keep every checked submission. The data is already in the transcript, but counting every `ScoreEvent` would count one submission once per task scorer and could take a secondary scorer's pass for the gate's.

**Milestone.** M2 (M2-4), after M1's metrics work.

### 6. M1: tighten `submitted`; M2: document stopping on the first proof

**Change.** Two parts.

- **`submitted` excludes `react()`'s non-submit returns.** `react()` returns normally, with no submission, after three consecutive content-filter responses or an unhandled context overflow (`inspect_ai: src/inspect_ai/agent/_react.py:262-277`). swarm-scoring.md's Terms say that for `react()` a submission "is a submit", so a normal return is not always a submission. The closed status set has `unsubmitted` ([swarm-limits.md](swarm-limits.md#stopping); M1-9): the wrapper gives it when the returned state's `output.stop_reason` is `content_filter` or `model_length`, which is how such a return ends, and `submitted` otherwise. `first` never selects an `unsubmitted` member. Other agents keep today's rule (a normal return is a submission), stated as such in `member()`.
- **Stopping on the first proof.** Add the `until_proved` controller above to [swarm-api.md Examples](swarm-api.md#examples), with its precondition stated: it is valid only when the member's own code makes a normal return mean a passed check. For Epoch's members, which can also return normally on a failed final state, that means a builder that raises a member-local custom limit when the inner agent returns without a recorded passing check, as `lean_member` does. Without that, the controller can stop every member on a refusal and record `controller:proved` with nothing proved.

**Sections.** [swarm-scoring.md Terms](swarm-scoring.md#terms) and [The final answer: `first`](swarm-scoring.md#the-final-answer-first); [swarm-limits.md Stopping](swarm-limits.md#stopping) (the status set); [swarm-api.md Examples](swarm-api.md#examples).

**Why.** Their swarm keeps 31 members spending after one has proved the conjecture, and from M2 a swarm can stop earlier with no new API: the controller needs only M1's surface, but the builder's member-local custom limit needs M2-2 ([Recommendation 2](#2-m2-an-agent-raised-custom-limit-ends-only-the-member)); in M1 that limit is foreign and ends the sample. But the obvious controller is wrong without the precondition, and `first` has the same hole for any `react()` member: the round-1 review reproduced a `react()` agent that returned successfully with no checks and the output "I cannot help." after three content-filter responses.

**Milestone.** M1 for the status (M1-9); the controller is an example in M2's documentation (M2-11).

### 7. M2: a shared-memory channel through the bus

**Change.** Make the first structured channel a `@swarm_channel` with memory's command set (view, create, str_replace, insert, delete, rename) over per-run channel state. Writes are bus records with the bus-bound sender, so reads fence each file with its last writer and notices tell members when files changed. `str_replace` is the compare-and-swap; `create` over an existing file and `delete` of another member's file are refused or versioned (to be decided in its design). Notes, tasks and the board follow in any order.

In M1, document in `member()` and the sample-store section that a board shared on purpose is `memory(instance="...")`, that the default instance is private, and that a hand-rolled swarm relying on the shared default loses its channel silently.

**Sections.** [swarm.md Channels](swarm.md#channels) (the table's order), [swarm-communication.md Tool state in the sample store](swarm-communication.md#tool-state-in-the-sample-store) and [How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus); swarm.md's [Not this design](swarm.md#not-this-design) item "Routing deliberately shared tool state through the bus" would move into the plan.

**Why.** A shared memory is the channel the only external swarm we know chose, and models already know the tool. Today it is our least-observed channel: no sender, no fence, no evidence, last writer wins. Routing it through the bus gives it the same treatment as messages, and the notices address the reported reluctance of agents to read boards ([swarm-communication.md Why](swarm-communication.md#why)).

**Milestone.** M2, with the bus it needs: its design (D-1, started during M1), then the bus, binding and the channel (M2-6 to M2-8). The documentation part is M1 (M1-3, M1-13) ([Open questions](#open-questions), item 2).

### 8. After M2: per-member sandboxes in the scoring contract

**Change.** With per-member sandboxes (Recommendation 1) there is no single shared environment, so swarm-scoring.md's artifact quantities need restating for that topology. Keep three things apart:

- **The selected artifact (final@k).** The task's scorer on the sandbox of the member the final chain chose, with that sandbox as the scorer's default. With no submission there is no selected artifact: final@k is `empty_value` with `reason="no_submissions"`, as for answer tasks, rather than a score of member 0's sandbox (what `sandbox()` would return).
- **The final-artifact oracle (team@k).** The task's scorer on every started member's final sandbox, `best_member` over them, graded whether or not the member submitted, as swarm-scoring.md already grades every member's artifact. This is an explicit revision of artifact team@k, which today is the one shared environment's score and equal to final@k; under per-member sandboxes it becomes an oracle over k artifacts, compared with pass@k or best@k as answer tasks' team@k is.
- **Run-time verification.** The verdicts the gate gave during the solve (Recommendation 5's checks), recorded and not re-derived. They describe what was verified when, not what the final sandboxes hold.

They coincide only when every member whose final sandbox passes also had a passing, submitted check, and no sandbox changed after its check. Neither holds in general. A member can finish a valid proof and hit the swarm cap before calling `submit_proof`: its final artifact passes (Epoch's end-of-sample re-check would count it, `apn/swarm.py:112-119`) but `first` has nothing to select, so team@k exceeds final@k. And a private sandbox is not a snapshot: background children (once supported) or detached sandbox processes can change it after a passing check, so a final grade can differ from the run-time verdict, which is why swarm-scoring.md takes `final_verdict` at finalisation. The record therefore names the sandbox each grade read.

`member_scores` reaches each member's artifact by scoring with that member's sandbox as default; no `materialize()` is needed. A task scorer that returns a dict over members, as Epoch's does, is not a per-member scorer: `member_scores` needs a scalar scorer of one sandbox, which their per-workspace check already is.

**Sections.** [swarm-scoring.md Shared-artifact tasks and per-member artifacts](swarm-scoring.md#shared-artifact-tasks-and-per-member-artifacts), [The result contract](swarm-scoring.md#the-result-contract), [Comparison quantities](swarm-scoring.md#comparison-quantities).

**Milestone.** After M2, with the scoring work.

## Open questions

None. The two this analysis raised are decided:

1. **Per-member sandboxes are an M2 opt-in.** `member(sandbox=..., visible_sandboxes=...)` routes a member to its own sandbox (Recommendation 1), and the shared sandbox stays the default (decisions: Ransom, 2026-10-07 and 2026-10-13; [swarm-implementation-plan.md](swarm-implementation-plan.md#where-the-deep-dives-milestone-items-land)).
2. **A shared memory routed through the bus is M2's bus channel**, with sender binding, fencing, notices and evidence (Recommendation 7); notes, a task list and a board are later work. The tool-state scope is unchanged: the default `memory()` instance stays private to each member (decision: Ransom, 2026-10-09). (Decision: Ransom, 2026-10-14; [swarm-implementation-plan.md](swarm-implementation-plan.md#decisions), item 2.)

## Not this design

Observed and left out, for Ransom to file or pass on if wanted:

- **Observations for Epoch** about their branch, which are theirs to act on: the smoke config's second item names a benchmark the head commit removed (`configs/b68-solved5-swarm.yaml:22`, `apn/task.py:336-343`); attempt sidecars are numbered by a counter shared by all members and named without the member (`apn/scorer.py:48-49`, `:112`); members keep spending after one has proved the conjecture; a finished deepagent member's background subagents run until the solver ends; per-index accuracy across samples is not a swarm measure; their 72-hour working limit may barely advance while members queue for 40 connections, by the mechanism [swarm-limits.md](swarm-limits.md#each-limit-kind) measured (not measured on their runs). Whether and how to share these is Ransom's call.
- **Per-sample state shared by members** (their comparator lock). Member builders run once per member, channels are shared by every sample, and M1 channels hold no state, so a lock or cache that members of one sample share has no home in the API. A weak-keyed map on a per-sample object works; an API for it can wait for a second user.
- **Shared services in per-member topologies.** Their comparator is a trusted service all members' checks queue on. A queue metric (time waiting for the lock) would explain slow swarms; it belongs to the metrics work if Recommendation 1 lands.
- **Kubernetes cost of per-member sandboxes.** 32 copies of a 32 GiB container per sample. Sizing guidance belongs in the per-member topology's documentation, not in the runtime.
- **The memory tool's private framing.** Its description and deepagent's memory prompt tell agents the memory is their own, which contradicts a shared board. If inspect_ai's `memory()` gains a shared mode, its description should change with it (inspect_ai).
