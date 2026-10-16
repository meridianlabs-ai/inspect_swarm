# Inspect Swarm: implementation plan

Status: proposed, 2026-10-13. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

This is the build plan for inspect_swarm across the merged designs. It is a plan, not a design: each pull request below names the design sections it implements. The few things a pull request needs that no merged design decides (M1's critical-path definition and stuck-in-loops signal, refusing bridged members in a swarm with a bus channel) are Ransom's decisions, recorded under [Decisions](#decisions); the rest waits for the design pull requests in the graph.

There are two milestones (decision: Ransom, 2026-10-13):

- **M1, ready for first internal usage.** A leaderless swarm over a shared sandbox, with full accounting: the `swarm()` API with registered controllers and channels, per-member tool state, a budget cap that stops members in an orderly way, a ledger of what was actually spent, the final answer `first` with every member's submission recorded, and evidence and metrics in the log. Existing task scorers score it unchanged. Two small inspect_ai pull requests land first: the registry types and the tool-state scope.
- **M2, ready for external usage.** What an external team needs to port its own agent-swarm experiment and gain from it, measured against Epoch's LeanOpenProblems swarm: a sandbox per member, background deepagent members, a shared-memory channel routed through a monitored bus, budget visibility for members, custom limits that end only the member and gate-check metrics, plus user docs, examples, an API stability statement and a first PyPI release. The port keeps its own scorer, so M2 adds no scoring beyond M1's.

Direct messages between members and scoring beyond `first` (the result contract, per-member scores, `baseline()` and the comparisons with epochs) are not needed for that and are future work ([Decisions](#decisions), Ransom, 2026-10-14).

Each milestone is a set of small pull requests. Most are in this repository; three are in inspect_ai, each a separate pull request with its own place in the dependency graph. Neither milestone needs a change in inspect_swe or inspect_sentinel. Everything else in the designs is future work, in any order.

## Why

The designs are separate deep dives, each with its own implementation plan. The milestones are defined by two audiences: internal users first, external users next, with Epoch's swarm as the test of the second (decision: Ransom, 2026-10-13). Building needs one ordered list of pull requests that says which design text each implements, which can be built at the same time, and what is deliberately left for later.

## Goals and non-goals

Goals:

- Define M1 and M2, deciding where each item the deep dives assigned to a milestone lands, with the reason.
- Break each milestone into pull requests that are as independent as possible, each with its scope, files, tests, dependencies and the design sections it implements, and show which can be built in parallel.
- Identify the inspect_ai, inspect_swe and inspect_sentinel pull requests separately.
- List the future work, one line per item.

Non-goals:

- New design. Where a pull request needs a decision no merged design makes, it is an [open question](#open-questions), a decision Ransom made for this plan ([Decisions](#decisions)), or a design pull request in the graph, not something this document settles on its own.
- ORBIT (PR #4, an open draft). Future work only.

## Current state

What the plan starts from, checked on 2026-10-13:

- **No runtime code.** `src/inspect_swarm/` holds only `__init__.py` (exporting `__version__` and nothing else), `_version.py` and `py.typed`; `tests/` holds the scaffold's package test.
- **The merged designs:** [swarm-overview.md](swarm-overview.md), [swarm.md](swarm.md), [swarm-api.md](swarm-api.md), [swarm-communication.md](swarm-communication.md) (with its optional push delivery), [swarm-limits.md](swarm-limits.md), [swarm-scoring.md](swarm-scoring.md), [swarm-lean-open-problems.md](swarm-lean-open-problems.md) and [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md).
- **The inspect_ai prerequisites have not landed.** inspect_ai `main`'s `RegistryType` has no `swarm_controller` or `swarm_channel` (`src/inspect_ai/_util/registry.py`), and `inspect_ai.util` has no `tool_state_scope`. This repository's lockfile installs inspect_ai `0.3.278.dev1+gfccfb298e`; the latest inspect_ai release is 0.3.279 (2026-10-12).
- **inspect_sentinel's dispatcher has reached inspect_ai `main`.** `src/inspect_ai/_sentinel/` (with `_dispatch.py`) is on `main`, with commits through 2026-10-12 (#5811, "sentinel monitors can watch every agent's model calls"). Its tool stages still do not run for bridged agents: `warn_sentinel_bridged()` (`src/inspect_ai/_sentinel/_context.py:57-64`, at `81c1668d3`) warns that bridged agents' tool calls are not monitored, only their model calls (inspect_ai#5759). So the bus's `policy=` hook is still needed for bridged sends ([Decisions](#decisions), item 5).
- **The scaffold is still on Python 3.10.** `pyproject.toml` (`requires-python = ">=3.10"`, pyright `pythonVersion = "3.10"`), `AGENTS.md`, `CONTRIBUTING.md` and the CI matrix in `.github/workflows/build.yaml` (`["3.10", "3.14"]`) predate the 3.11 decision (decision: Ransom, 2026-10-07; [swarm.md Compatibility](swarm.md#compatibility-and-migration)).
- **Releases are off.** `.github/workflows/release.yaml` has only `workflow_dispatch`; its comment says to restore the `push` trigger to cut 0.1.0. The `inspect-ai` dependency is a PyPI version floor (`>=0.3.281` since 2026-10-16), so every inspect_ai change inspect_swarm uses must be in a PyPI release of inspect_ai before it can merge.
- **Sibling packages' layout for docs and examples.** inspect_sentinel and inspect_flow keep a Quarto site in `docs/` (`_quarto.yml`); inspect_sentinel keeps runnable `examples/`. This repository's `pyproject.toml` already excludes `docs/` from the wheel and from ruff.

## Milestones

### M1: ready for first internal usage

M1 is a leaderless filesystem swarm with full accounting ([swarm.md Implementation plan](swarm.md#implementation-plan)). It contains:

- the `swarm()` API skeleton with `@swarm_controller`, `@swarm_channel`, members as built agents or `member()` records, copies built from an `@agent` factory and its arguments for each member in each sample, and no agent object running twice in a sample ([swarm-api.md, the member contract](swarm-api.md#the-member-contract)), name resolution and faithful logging, and the built-ins `leaderless` and `filesystem` ([swarm-api.md](swarm-api.md#implementation-plan), steps 2 to 4);
- the shared-sandbox default with its filesystem prompt convention, and per-member tool state through the tool-state scope ([swarm.md Members](swarm.md#members), [Sandbox topology](swarm.md#sandbox-topology));
- members whose work stays inside their invocation (`react()`, synchronous `deepagent()`, bridged agents); background members rejected, and ACP agents refused when built;
- the budget: swarm nodes, `Budget` with derived caps, member limits, the soft stop with grace, recovery from the swarm's own cap only, the final step and the provisional answer, the ledger, and splits ([swarm-limits.md](swarm-limits.md#implementation-plan), all five steps);
- the final answer `first`, the provisional answer and the per-member result record ([swarm-scoring.md Part 1](swarm-scoring.md#implementation-plan-for-m1)); this is how the brief's "final-answer modes, the result contract" is read (decision: Ransom, 2026-10-14);
- `InfoEvent` evidence, a named timeline per member, a recorded stop reason, metrics, harness-validity checks, and a simple critical path: the steps to the answer and the longest member's step count (decision: Ransom, 2026-10-14; [swarm.md Observer](swarm.md#observer-evidence-accounting-and-metrics));
- thin analysis helpers for realized-cost comparisons and λ fits;
- three small items from [swarm-lean-open-problems.md](swarm-lean-open-problems.md#recommended-changes-to-our-designs): the `unsubmitted` status, the rejection of an integer `background`, and documentation that the default `memory()` is private to each member ([table](#where-the-deep-dives-milestone-items-land));
- the two inspect_ai pull requests that land first: the registry types (decision: Ransom, 2026-10-08) and the tool-state scope (decision: Ransom, 2026-10-09).

Done means: an internal user can run a leaderless swarm of `react()`, synchronous `deepagent()` or bridged members on an existing task, sweep member count and budget as task parameters, and compare arms on the ledger's realized cost, with the task's own scorers unchanged.

### M2: ready for external usage

M2's test is Epoch's LeanOpenProblems swarm: an external team could port that experiment to inspect_swarm and gain from doing so ([swarm-lean-open-problems.md](swarm-lean-open-problems.md)). Its analysis says what the port needs and what the port gains ([Could inspect_swarm express their experiment?](swarm-lean-open-problems.md#could-inspect_swarm-express-their-experiment), [What they would gain and lose](swarm-lean-open-problems.md#what-they-would-gain-and-lose)). M2 contains:

- **What the port needs:** per-member sandboxes (Recommendation 1), an agent-raised custom limit that ends only the member (Recommendation 2), and background deepagent members, which need inspect_ai's scoped owner for `background()` (Recommendation 3). Without the first two the port cannot express the experiment; without the third it runs a different arm. With them it runs with its scorer unchanged ([With recommendations 1 and 2 it can](swarm-lean-open-problems.md#could-inspect_swarm-express-their-experiment)).
- **What the port gains:** members that can see the budget that will stop them (Recommendation 4), gate checks as metrics (Recommendation 5), and a shared-memory channel through the bus (Recommendation 7).
- **The bus**, because the shared-memory channel runs through it: `deliver()`, storm controls, the `policy=` hook, evidence, member binding, notices and exposure ([swarm-communication.md](swarm-communication.md#implementation-plan), steps 1, 2, 4 and 5), with a construction-time refusal of bridged members in a swarm that has a bus channel (M2-6).
- **What any external release needs:** user docs, examples (among them the `until_proved` controller of Recommendation 6), a statement of which API is supported and which is experimental, packaging, the Python floor, and a first PyPI release that depends on released inspect_ai.

Not in M2 (decisions: Ransom, 2026-10-14; [Decisions](#decisions)):

- The `messages` channel (direct messages) and bus channel tools for bridged members. Epoch's swarm uses neither, and the port gains nothing from them that the shared-memory channel does not give it.
- Scoring beyond `first`: swarm-scoring.md Part 2's result contract and record additions, `member_scores`, `best_at(k)`, `baseline()` and the analysis helper, and per-member sandboxes in the scoring contract (Recommendation 8). The port runs with its own scorer, and the Lean analysis lists these as optional gains after M2 ([What they would gain and lose](swarm-lean-open-problems.md#what-they-would-gain-and-lose)). Under per-member sandboxes in M2, the task's scorers run after the swarm, where `sandbox()` is `default`; a scorer that grades members' artifacts names each member's sandbox itself, as Epoch's end-of-sample check does today, and the record's `sandbox` field says which member used which (M2-1).

Done means: Epoch's port as sketched in [A port, sketch](swarm-lean-open-problems.md#a-port-sketch) runs on inspect_swarm with background deepagent members, per-member sandboxes and a bus-routed board, from a PyPI release, with docs that describe it.

### Where the deep dives' milestone items land

Each item a deep dive assigns to a milestone, with the milestone it is in and why:

| Item | Source | Milestone | Why |
|---|---|---|---|
| Swarm nodes, `Budget`, member wrapper, stopping, exhaustion, ledger | [swarm-limits.md](swarm-limits.md#implementation-plan) steps 1–4 | M1 | The cap and the ledger are what M1's comparisons rest on |
| Budget splits and the sweep example | swarm-limits.md step 5 | M1 | Budget split is one of the library's sweepable axes ([swarm.md Goals](swarm.md#goals)); small, and a leaf of the graph |
| Optional inspect_ai observability PRs (a `Model.compact()` event; a status code on errored model events) | swarm-limits.md [open question 3](swarm-limits.md#open-questions) | Optional, alongside M1, not blocking | They relax the ledger's completeness rules; the ledger works without them |
| Record and `first` | [swarm-scoring.md](swarm-scoring.md#implementation-plan-for-m1) | M1 | M1's answer and the record scorers read |
| Per-member sandboxes, `member(sandbox=, visible_sandboxes=)` | [Lean Rec. 1](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field) | M2 | Needed by the external port; internal first use runs on the shared default (decision: Ransom, 2026-10-07) |
| Agent-raised custom limit ends only the member | [Lean Rec. 2](swarm-lean-open-problems.md#2-m2-an-agent-raised-custom-limit-ends-only-the-member) | M2 | Needed by the external port; until then such an error ends the sample loudly, as it does for any agent today, rather than producing a wrong result |
| Reject integer `background` as well as `True` | [Lean Rec. 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members), M1 part | M1 | Without it `deepagent(background=64)` slips past the rejection and breaks the drain guarantee silently |
| Background deepagent members (inspect_ai scoped owner) | Lean Rec. 3; [swarm.md Members](swarm.md#members) | M2 | Both of Epoch's configs use them; without them the port is a different arm |
| Budget visible to members | [Lean Rec. 4](swarm-lean-open-problems.md#4-m2-members-can-see-the-budget-that-will-stop-them) | M2 | Members pacing themselves is the external port's need; first internal use needs the cap, not the view |
| Gate checks as metrics | [Lean Rec. 5](swarm-lean-open-problems.md#5-m2-gate-checks-as-metrics) | M2 | The outcome gated tasks like Epoch's care about; not needed to run or compare a first internal swarm |
| Critical-path steps for leaderless swarms | [swarm.md Observer](swarm.md#observer-evidence-accounting-and-metrics), Metrics | M1, the simple definition; the dependency-linked one Future | Decision: Ransom, 2026-10-14. M1's only channel, the filesystem, is observed but not attributed, so a chain linked by reads of another member's writes has no cross-member edges in M1 |
| `unsubmitted` status for `react()`'s non-submit returns | [Lean Rec. 6](swarm-lean-open-problems.md#6-m1-tighten-submitted-m2-document-stopping-on-the-first-proof), first part | M1 | Without it `first` can select a member that returned after three refusals or a context overflow: a wrong answer for any internal `react()` swarm |
| The `until_proved` example controller | Lean Rec. 6, second part | M2 docs | An example for verifier-gated external tasks |
| Documentation that the default `memory()` is per member | [Lean Rec. 7](swarm-lean-open-problems.md#7-m2-a-shared-memory-channel-through-the-bus), M1 part | M1 | The scope makes a hand-rolled shared board fail silently; internal users porting hand-rolled swarms hit it first |
| Shared-memory channel through the bus | Lean Rec. 7 | M2 | The port's channel, with sender binding, fencing, notices and evidence |
| Per-member sandboxes in the scoring contract | [Lean Rec. 8](swarm-lean-open-problems.md#8-after-m2-per-member-sandboxes-in-the-scoring-contract) | Future | Decision: Ransom, 2026-10-14. The port runs with its own scorer; Rec. 8 builds on the result contract, which is future work |
| Result contract, `member_scores`, `best_at(k)`, `baseline()`, analysis helper | [swarm-scoring.md Part 2](swarm-scoring.md#implementation-plan-after-m2) steps 1, 4, 5 | Future | Decision: Ransom, 2026-10-14. Optional gains for the port, not needs; M2's "Done means" asks for none of them |
| Bus core, binding, notices, exposure | [swarm-communication.md](swarm-communication.md#implementation-plan) steps 1, 2, 4, 5 | M2 | The shared-memory channel needs them |
| The `messages` channel; bus tools for bridged members | swarm-communication.md steps 3, 6 | Future | Decision: Ransom, 2026-10-14. Not needed by the external port; bridged members are refused in a swarm with a bus channel (M2-6) |

Everything else the deep dives place after M2 or later is future work.

## M1 pull requests

IDs: `AI-n` are inspect_ai pull requests (opened on the Meridian fork and promoted to `UKGovernmentBEIS/inspect_ai` as usual); `M1-n` are inspect_swarm. An inspect_swarm pull request that depends on an inspect_ai one waits for a PyPI release containing it, raises the minimum in `pyproject.toml` when `src/` needs that release, and updates `uv.lock` with `uv lock --upgrade-package inspect-ai` in its own diff; when only tests need it, it updates only the lock. inspect_ai's changelog is written by Release Please from the pull request title, so no inspect_ai pull request edits `CHANGELOG.md`.

### Graph

```mermaid
flowchart LR
  AI1[AI-1 registry types] --> M1_1
  AI2[AI-2 tool-state scope] --> M1_4
  M1_0[M1-0 Python 3.11] --> M1_1[M1-1 registry plumbing]
  M1_0 --> M1_2[M1-2 budget nodes]
  M1_1 --> M1_3[M1-3 members and signature]
  M1_2 --> M1_3
  M1_3 --> M1_4[M1-4 runtime core]
  M1_4 --> M1_5[M1-5 record and first]
  M1_4 --> M1_6[M1-6 ledger]
  M1_4 --> M1_8[M1-8 splits]
  M1_5 --> M1_7[M1-7 exhaustion and final step]
  M1_6 --> M1_7
  M1_5 --> M1_9[M1-9 unsubmitted status]
  M1_5 --> M1_10[M1-10 evidence and metrics]
  M1_6 --> M1_10
  M1_6 --> M1_12[M1-12 analysis helpers]
  M1_5 --> M1_11[M1-11 critical path]
  M1_10 --> M1_11
  M1_7 --> M1_13[M1-13 README for internal users]
  M1_8 --> M1_13
  M1_9 --> M1_13
  AI3[AI-3 compact event, optional]
  AI4[AI-4 errored-event status, optional]
```

What can be built in parallel, in waves (each wave starts when its dependencies merge):

1. AI-1, AI-2 and M1-0, all independent. The optional AI-3 and AI-4 can go at any point.
2. M1-1 (after AI-1 and M1-0) and M1-2 (after M1-0).
3. M1-3.
4. M1-4 (needs AI-2).
5. M1-5, M1-6 and M1-8, in parallel.
6. M1-7, M1-9, M1-10 and M1-12, in parallel.
7. M1-11 and M1-13.

The critical path is AI-1 → M1-1 → M1-3 → M1-4 → M1-5 → M1-7 → M1-13, with AI-2 needed by M1-4. Starting AI-2 in wave 1 keeps it off the critical path.

### inspect_ai

**AI-1. Registry types `swarm_controller` and `swarm_channel`.** Adds the two values to `RegistryType`, with a test that registers, looks up and round-trips one object of each (`is_registry_dict()`), as inspect_ai#4359 did for `validation_predicate`.
- Files: `src/inspect_ai/_util/registry.py`, `tests/util/test_registry.py`.
- Depends on: nothing.
- Implements: [swarm-api.md The registry](swarm-api.md#the-registry) and its implementation plan step 1 (decision: Ransom, 2026-10-08; prefix 2026-10-09).

**AI-2. The tool-state scope.** `tool_state_scope()`, `tool_state_instance()` and `private_tool_instance()`, exported from `inspect_ai.util`; `memory()`, `bash_session()`, `web_browser()` (refusing its legacy fallback inside a scope) and the skill tool resolve their default instance through it; deepagent's child skill instance becomes `private_tool_instance(sa.name)`.
- Files: `src/inspect_ai/util/_tool_state.py`, `src/inspect_ai/util/__init__.py`, `src/inspect_ai/tool/_tools/_memory.py`, `_bash_session.py`, `_web_browser/_web_browser.py`, `_skill/tool.py`, `src/inspect_ai/agent/_deepagent/agent_tool.py`.
- Tests: `tests/util/test_tool_state.py`, `tests/tools/test_memory.py`, `tests/tools/test_skill.py`, stubbed-RPC cases for `bash_session()` and `web_browser()`, as [swarm-api.md Testing](swarm-api.md#testing) lists. No Docker.
- Depends on: nothing.
- Implements: [swarm.md Members](swarm.md#members) ("Per-member tool state") and [open question 2](swarm.md#open-questions) (decision: Ransom, 2026-10-09); swarm-api.md implementation plan step 1's second pull request.

**AI-3 (optional). An event from `Model.compact()` at invocation.** Lets the ledger judge native compaction per call instead of treating every run in a process that has loaded a native-compaction provider as a lower bound.
- Files and tests: inspect_ai's model and event modules; to be scoped when proposed.
- Depends on: nothing. Nothing in M1 depends on it.
- Implements: [swarm-limits.md open question 3](swarm-limits.md#open-questions) and its "Native compaction" item under Not this design. The question is undecided; this plan carries it as optional.

**AI-4 (optional). A status code on errored model events.** Lets the ledger count a verified pre-inference rejection (an HTTP 429) as zero spend instead of unknown.
- Depends on: nothing. Nothing in M1 depends on it.
- Implements: the same open question; undecided.

### inspect_swarm

**M1-0. Python 3.11 floor.** A `chore:` pull request that moves the scaffold from 3.10 to 3.11, so later pull requests may use `ExceptionGroup` and `except*` natively.
- Files: `pyproject.toml` (`requires-python`, pyright `pythonVersion`, the "Matches inspect-ai's floor" comment), `uv.lock`, `AGENTS.md`, `CONTRIBUTING.md`, `.github/workflows/build.yaml` (matrix `["3.11", "3.14"]`).
- Tests: CI on the new matrix.
- Depends on: nothing. Every other inspect_swarm pull request is based on it, because pyright's `pythonVersion` and the CI matrix decide which syntax passes.
- Implements: [swarm.md Compatibility](swarm.md#compatibility-and-migration) ("These move to 3.11 in a separate scaffold PR before M1") (decision: Ransom, 2026-10-07).

**M1-1. Registry plumbing.** The `@swarm_controller` and `@swarm_channel` decorators, `Controller`, `Channel` (M1's `instructions()` only), `MemberInfo`, `resolve()` and the `inspect_ai` entry point.
- Files: `src/inspect_swarm/_controller/_controller.py`, `src/inspect_swarm/_channel/_channel.py`, `src/inspect_swarm/_registry.py`, `src/inspect_swarm/_entrypoint.py`, `pyproject.toml` (`[project.entry-points.inspect_ai]`), `uv.lock`.
- Tests: `tests/test_registry.py`.
- Depends on: M1-0, AI-1.
- Implements: [swarm-api.md](swarm-api.md#implementation-plan) step 2; [Resolving names](swarm-api.md#resolving-names).

**M1-2. Swarm nodes and the budget parameter.** `Budget` as a frozen pydantic model with validation and cap derivation, the swarm node classes and cost pairs (ownership, messages, per-call pricing, the bridged branch), bridged detection, and the tests that pin the inspect_ai behaviour the nodes rely on.
- Files: `src/inspect_swarm/_budget.py`.
- Tests: `tests/test_budget.py` (pinned behaviour, validation, derivation, nodes).
- Depends on: M1-0. Independent of M1-1.
- Implements: [swarm-limits.md](swarm-limits.md#implementation-plan) step 1; [The node tree](swarm-limits.md#the-node-tree), [The budget parameter](swarm-limits.md#the-budget-parameter), [Swarm nodes](swarm-limits.md#swarm-nodes).

**M1-3. Members and the `swarm()` signature.** `Member` and `member()` with the built-agent form (`agent`) and the factory form (`factory`, `args`), their serialisers (a built agent as Inspect's agent registry dict; a factory as its registry name, `args` as `@agent` logs an agent's params), the private `build_member()` the runtime calls, count expansion and names, with repeated default names numbered in roster order; the construction checks, which refuse a built agent with `count > 1` or `args` (the message showing the factory form), an unregistered callable and `args` that do not bind to the factory's signature, all without calling the factory; `swarm()`'s arguments, validation and resolution, including bare built agents in `members=`, the refusal of bare factories, the identity check that no agent object is in two members, and logged member dicts in both forms; the exports. The background rejection reads a built deepagent's params or a factory's `args`, and catches an integer `background` as well as `True`. `member()`'s docstring states the member contract: no agent object runs twice in a sample, copies need the factory form, which builds each member's agent in each sample, objects given to agents (a factory's `args`, a tool object two built agents hold) are shared by every member and sample they reach, and a stateful custom tool belongs inside a member builder; and the tool-state contract, including that the default `memory()` instance is private to each member and a board shared on purpose is `memory(instance="...")`.
- Files: `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/__init__.py`.
- Tests: `tests/test_member.py` (including the refusals, bare factories, the identity check, default-name numbering, `background=64` in both forms and validating the logged dict in both forms), `tests/test_api_log.py` (the logged forms for a bare agent, a list of agents, a single record and a mixed list, with package namespaces kept; replay rebuilding built agents and resolving factories without building them; and `eval_set` identity for two arms that differ in a built agent's params or a factory's `args`).
- Depends on: M1-1, M1-2 (the signature takes a `Budget`).
- Implements: [swarm-api.md](swarm-api.md#implementation-plan) step 3, [Members](swarm-api.md#members), [Logging and replay](swarm-api.md#logging-and-replay); [Lean Rec. 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members)'s M1 part; [Lean Rec. 7](swarm-lean-open-problems.md#7-m2-a-shared-memory-channel-through-the-bus)'s M1 documentation part.

**M1-4. Runtime core: the controller surface, members and stopping.** The largest M1 pull request, because the two deep dives place these steps together: `SwarmControl`, `MemberHandle`, `MemberEnded`, `events()`, `start()` and the preamble; the member wrapper with its lever, re-created member limits and leaf-by-leaf error classification, each member's task entered in its tool-state scope and a factory-form member's agent built there by `build_member()` (a factory error makes the member `errored`; an ACP agent is refused with `TypeError`); the runtime lever; `stop()` with step 0, soft stop, grace and hard stop; the time-cap timer; member statuses and stop reasons; the built-ins `leaderless` and `filesystem`.
- Files: `src/inspect_swarm/_controller/_control.py`, `src/inspect_swarm/_controller/leaderless.py`, `src/inspect_swarm/_channel/filesystem.py`, `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `uv.lock`.
- Tests: `tests/test_controller.py` (including one agent per factory-form member per sample: six builds for `count=3` over two concurrent samples, the build inside the member's scope, the factory error and the ACP refusal), `tests/test_channel.py`, `tests/test_swarm_limits.py` (members and stopping), `tests/test_scoring_loop.py` (its M1 case), the per-member tool-state cases of swarm.md's runtime tests. Filesystem-channel tests use the local sandbox, or Docker marked slow and skipped in CI without it.
- Depends on: M1-3, AI-2.
- Implements: [swarm-api.md](swarm-api.md#implementation-plan) step 4, [Controller and runtime](swarm-api.md#controller-and-runtime), [Controllers](swarm-api.md#controllers), [Channels](swarm-api.md#channels); [swarm-limits.md](swarm-limits.md#implementation-plan) step 2, [Stopping](swarm-limits.md#stopping); [swarm.md Members](swarm.md#members) and [Sandbox topology](swarm.md#sandbox-topology).

**M1-5. The record and `first`.** `MemberResult`, `FinalRecord` and `SwarmResultRecord` in the store; `first`, the provisional answer, finalisation and the returned `AgentState`; the `member_result` and `final` evidence events; the wrapper keeps its `AgentState` reference.
- Files: `src/inspect_swarm/_record.py`, `src/inspect_swarm/_final.py`, `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/_controller/_control.py`.
- Tests: `tests/test_final.py`, `tests/test_task_scorers.py` (with the local sandbox for the files case).
- Depends on: M1-4.
- Implements: [swarm-scoring.md Part 1](swarm-scoring.md#implementation-plan-for-m1): [The final answer: `first`](swarm-scoring.md#the-final-answer-first), [What the swarm records](swarm-scoring.md#what-the-swarm-records).

**M1-6. The ledger.** `MemberLedger` and `SwarmLedger`, event counting, reconciliation and claims, written as an `InfoEvent` and a store entry.
- Files: `src/inspect_swarm/_budget.py` (or `_ledger.py` if it grows), `src/inspect_swarm/_evidence.py`.
- Tests: the ledger cases of `tests/test_budget.py`.
- Depends on: M1-4. Whichever of M1-5 and M1-6 merges first creates `_evidence.py`.
- Implements: [swarm-limits.md](swarm-limits.md#implementation-plan) step 4, [The ledger](swarm-limits.md#the-ledger); [swarm.md Accounting](swarm.md#observer-evidence-accounting-and-metrics).

**M1-7. Exhaustion and the final step.** Recovery from the swarm's own caps, propagation with the swarm's own errors removed from mixed groups, the synchronous `finally`, and the final meter around finalisation and the provisional answer.
- Files: `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/_final.py`.
- Tests: the exhaustion cases of `tests/test_swarm_limits.py`, including that a sample time limit leaves the ledger and the provisional answer written.
- Depends on: M1-5 (the finalisation it wraps) and M1-6 (the ledger its tests check). This runs swarm-limits.md's steps 3 and 4 in the other order, so that step 3's test of the ledger surviving a sample limit lands with it.
- Implements: [swarm-limits.md](swarm-limits.md#implementation-plan) step 3, [Exhaustion and errors](swarm-limits.md#exhaustion-and-errors), [The final step and the reserve](swarm-limits.md#the-final-step-and-the-reserve).

**M1-8. Splits and the sweep example.** Share nodes and validation for `split` (`"pool"`, `"equal"`, weights), and a task example in `README.md` showing the sweeps.
- Files: `src/inspect_swarm/_budget.py`, `README.md`.
- Tests: the split cases of `tests/test_budget.py` and `tests/test_swarm_limits.py`.
- Depends on: M1-4.
- Implements: [swarm-limits.md](swarm-limits.md#implementation-plan) step 5, [Splits](swarm-limits.md#splits), [Budget splits as a sweep axis](swarm-limits.md#budget-splits-as-a-sweep-axis).

**M1-9. The `unsubmitted` status.** The member wrapper gives `unsubmitted` when a returned state's `output.stop_reason` is `content_filter` or `model_length`, and `submitted` otherwise; `first` never selects an `unsubmitted` member; `member()` documents that other agents keep the rule that a normal return is a submission.
- Files: `src/inspect_swarm/_member.py`, `src/inspect_swarm/_final.py`.
- Tests: the Rec. 6 cases of [Lean Testing](swarm-lean-open-problems.md#testing) that need no custom controller, in `tests/test_final.py` and `tests/test_swarm_limits.py`.
- Depends on: M1-5.
- Implements: [Lean Rec. 6](swarm-lean-open-problems.md#6-m1-tighten-submitted-m2-document-stopping-on-the-first-proof), first part.

**M1-10. Evidence, timelines, metrics and harness-validity checks.** A named timeline per member (`transcript().add_timeline()`), the metrics swarm.md lists (per-member and total tokens and cost from the ledger, wall clock, idle time, per-member time waiting for a model connection), and harness-validity checks reported apart from the task score: a member that never acted, and members stuck in loops, reported as a signal rather than a verdict (decision: Ransom, 2026-10-14; [Decisions](#decisions), item 9). For the second, each member's metrics carry `longest_identical_tool_run`, the longest run of consecutive tool calls in the member's spans with the same function and the same arguments (compared as canonical JSON). No threshold is fixed until there is data; analysis applies one. A bridged member's tool calls have no `ToolEvent`s ([swarm-communication.md The filesystem channel](swarm-communication.md#the-filesystem-channel)), so its value is `None`, not 0.
- Files: `src/inspect_swarm/_metrics.py`, `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_swarm.py`.
- Tests: `tests/test_metrics.py`, on mockllm runs, including a deterministic contention test (two members queued on one model connection) showing each member's connection wait apart from the sample's aggregate waiting time, which Inspect merges across overlapping waits (`src/inspect_ai/_util/working.py:90-113`, at `81c1668d3`); a scripted member repeating one call three times, then changing it, reports a run of 3, and the same function with different arguments does not extend a run.
- Depends on: M1-5, M1-6.
- Implements: [swarm.md Observer](swarm.md#observer-evidence-accounting-and-metrics) ("Metrics", "Member views", the harness-validity bullet of "Analysis").

**M1-11. Critical path for leaderless swarms, the simple definition** (decision: Ransom, 2026-10-14). Two per-sample metrics: `steps_to_answer`, the selected member's steps up to its submission (absent when there is no answer), and `longest_member_steps`, the largest step count of any member. A step is one completed model call in the member's spans, cache replays included, from the member's ledger line (`calls.calls + calls.cache_replays`, M1-6). Under `first` the selected member ends at its submission, so its steps to the answer are all its steps. Calls a member makes concurrently (parallel subagent tool calls) count one each, so both numbers bound the sequential depth from above; the metric's docstring says so.
- Why this definition: the dependency-linked one swarm.md suggests (the longest chain of member steps linked by reads of another member's writes) needs attributed cross-member reads and writes. M1's only channel, the filesystem, is observed but not attributed, so in M1 that graph has no cross-member edges and reduces to these per-member chains. The linked definition is later work over the bus's evidence ([Future work](#future-work)), and swarm.md's caution stands: latency curves are not interpreted until it exists.
- Files: `src/inspect_swarm/_metrics.py`. Tests: `tests/test_metrics.py`, on mockllm runs with scripted members of known step counts, including a run with no submission.
- Depends on: M1-5 (the selected member), M1-10 (the metrics module and the ledger lines it reads). No separate design.
- Implements: swarm.md Observer, "critical-path steps" ("Settling it is part of M1's metrics work").

**M1-12. Analysis helpers.** Thin helpers that read a set of logs and compare arms on the ledger's realized cost, and fit λ (Ord's method) over agent count × per-agent budget.
- Files: `src/inspect_swarm/analysis/__init__.py`, `src/inspect_swarm/analysis/_cost.py`.
- Tests: `tests/analysis/`, on synthetic logs with known answers.
- Depends on: M1-6 (the ledger's format).
- Implements: [swarm.md Observer](swarm.md#observer-evidence-accounting-and-metrics) ("Analysis", first bullet) and its Testing ("Analysis helpers").

**M1-13. README for internal users.** How to run a swarm on an existing task, the common configurations, and M1's documented restrictions: background members are rejected; tool state a setup solver prepared at the default instance is not carried into members, and tasks whose scorers read a member's tool state are unsupported ([swarm.md Compatibility](swarm.md#compatibility-and-migration)); the default `memory()` is private to each member and a shared board needs an explicit instance; the filesystem observation limit ([swarm.md Security](swarm.md#security)); the result record's store key `inspect_swarm.result` is owned by the runtime, task code must not write it, and a tool that reads it gives members a view of each other's results, a channel outside the bus ([swarm-scoring.md What the swarm records](swarm-scoring.md#what-the-swarm-records); decision: Ransom, 2026-10-14).
- Files: `README.md`.
- Depends on: M1-7, M1-8, M1-9 (so the text describes merged behaviour).
- Implements: the documentation parts of swarm.md's M1 and Lean Rec. 7's M1 part.

## M2 pull requests

`D-n` are design pull requests in this repository's `design/`. A design pull request precedes code where no merged design decides what the code must do.

### Graph

```mermaid
flowchart LR
  M1((M1)) --> M2_1[M2-1 per-member sandboxes]
  M1 --> M2_2[M2-2 member-ending custom limits]
  M1 --> M2_3[M2-3 budget view]
  M1 --> M2_4[M2-4 gate checks]
  D2[D-2 background owner design] --> AI5[AI-5 scoped owner for background]
  AI5 --> M2_5[M2-5 background deepagent members]
  M1 --> M2_5
  M1 --> M2_6[M2-6 bus core]
  M2_6 --> M2_7[M2-7 member binding]
  D1[D-1 memory channel design] --> M2_7
  D1 --> M2_8
  M2_7 --> M2_8[M2-8 memory channel]
  M2_8 --> M2_9[M2-9 notices]
  M2_8 --> M2_10[M2-10 exposure and comm metrics]
  M2_1 & M2_2 & M2_3 & M2_4 & M2_5 & M2_9 & M2_10 --> M2_11[M2-11 docs and examples]
  M2_11 --> M2_12[M2-12 API statement, packaging, first release]
```

"M1" in the graph means the M1 pull requests the item names under "Depends on"; most M2 work needs only part of M1. Four strands are independent of each other and can be built in parallel:

- **Isolation and limits:** M2-1, M2-2, M2-3 and M2-4, each alone.
- **Background members:** D-2 → AI-5 → M2-5. D-2 starts during M1 (decision: Ransom, 2026-10-14).
- **Bus and memory channel:** M2-6 → M2-7 → M2-8 → {M2-9, M2-10}, with D-1 before M2-7 (it decides how the binding treats a member's descendants) and M2-8. D-1 starts during M1 (decision: Ransom, 2026-10-14).
- **Release:** M2-11 and M2-12, after the three strands above.

### Designs

**D-1. Design: the shared-memory channel.** A `@swarm_channel` with memory's command set over per-run channel state, built on the bus: what a write record is, how reads fence each file with its last writer, what notices say when files change, and the semantics Recommendation 7 leaves to its design (refuse or version `create` over an existing file and `delete` of another member's file; `str_replace` as compare-and-swap). It must also decide whether a member's descendants (deepagent's `general()` subagents, background ones included) can read and write the board, as Epoch's children do today, and if so how the binding rule of [Members and identity](swarm-communication.md#members-and-identity) changes for this channel, how a write is attributed to the member and to the loop that made it, and what `read` and `exposed` evidence mean with several readers per member (decision: Ransom, 2026-10-14; [Decisions](#decisions), item 7). It also says what write and read evidence the dependency-linked critical path needs (each write attributed to its member, and reads of another member's writes recorded as `exposed`), so that the later metric can be built over the bus's evidence without changing it (decision: Ransom, 2026-10-14).
- Files: `design/swarm-memory-channel.md`.
- Depends on: nothing; [swarm-communication.md How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus) is the interface it implements.
- Implements: nothing yet; prepares [Lean Rec. 7](swarm-lean-open-problems.md#7-m2-a-shared-memory-channel-through-the-bus).

**D-2. Design: background deepagent members.** The inspect_ai scoped owner that `background()` attaches to when one encloses the caller (the sample's task group otherwise, as now), and how the swarm uses it: entering it per member, draining it on member end and at the swarm's stop, and how the ledger and statuses count background children. Written here, as [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md) designed its inspect_ai scope here.
- Files: `design/swarm-background-members.md`.
- Depends on: nothing.
- Implements: nothing yet; prepares [swarm.md Members](swarm.md#members) ("Lifting the restriction") and [Lean Rec. 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members).

### inspect_ai

**AI-5. A scoped owner for `background()`.** As D-2 specifies. The only inspect_ai change in M2.
- Files and tests: as D-2 specifies (inspect_ai's `background()` utility and deepagent's background dispatch, with tests in its own suite).
- Depends on: D-2.

### inspect_swe and inspect_sentinel

None in M2, and none in M1.

- **inspect_swe.** Bridged members are accepted from M1 unchanged; swarm-limits.md detects them by their inspect_swe registry names. The two pieces of work that need inspect_swe changes, users isolation ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md#implementation-plan) step 3) and bridged swarms, are future work.
- **inspect_sentinel.** A native member's call to a channel tool is an ordinary tool call, which sentinel's tool stages see with no change; the bus's `policy=` hook covers what sentinel does not ([swarm-communication.md Monitoring](swarm-communication.md#monitoring-sentinel-first-the-bus-hook-for-the-rest)). The dispatcher on inspect_ai `main` still skips bridged agents' tool stages ([Current state](#current-state)), so that hook stays as designed ([Decisions](#decisions), item 5).

### inspect_swarm

**M2-1. Per-member sandboxes.** `member(sandbox=..., visible_sandboxes=...)` with the `{member}` template under `count`; the runtime enters `sandbox_default(sandbox)` for the member and restricts its sandbox environments to its own plus the listed services; names validated at swarm start (`ValueError` naming the member); a `sandbox` field on `MemberResult` and in evidence; the preamble says whether members share a sandbox; `swarm()` raises `ValueError` when `filesystem` is listed while any member has its own sandbox. `member()`'s docstring says that the task's scorers run after the swarm, outside every member, where `sandbox()` is `default`: a scorer that grades members' artifacts names each member's sandbox itself, and the record's `sandbox` field says which member used which. Routing the headline scorer to the selected member's sandbox is Lean Recommendation 8, future work.
- Files: `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `src/inspect_swarm/_controller/_control.py` (preamble), `src/inspect_swarm/_record.py`, `src/inspect_swarm/_evidence.py`.
- Tests: the Rec. 1 cases of [Lean Testing](swarm-lean-open-problems.md#testing) with stub sandbox environments (no Docker), one case showing that a scorer run after the swarm sees `default` from `sandbox()` and a member's environment from `sandbox(name)`, and one Docker test, marked slow, with a two-service compose file.
- Depends on: M1-4, M1-5.
- Implements: [Lean Rec. 1](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field).

**M2-2. An agent-raised custom limit ends only the member.** A `LimitExceededError` with `type="custom"` and `source is None` ends the member with status `member_limit:custom`, bare or grouped; a group that also holds a foreign leaf re-raises only the foreign leaves.
- Files: `src/inspect_swarm/_member.py`.
- Tests: the Rec. 2 cases of Lean Testing, in `tests/test_swarm_limits.py`.
- Depends on: M1-4 (and M1-5 for the test that `first` never selects the stopped member).
- Implements: [Lean Rec. 2](swarm-lean-open-problems.md#2-m2-an-agent-raised-custom-limit-ends-only-the-member).

**M2-3. Budget view for members.** `swarm_budget()` and a `swarm_resources()` tool reporting the cap per kind, the pool's usage, the member's share and usage under a split, and the time cap; a fixed preamble sentence stating the split.
- Files: `src/inspect_swarm/_budget.py`, a new `src/inspect_swarm/_tools.py`, `src/inspect_swarm/_controller/_control.py` (preamble), `src/inspect_swarm/__init__.py`.
- Tests: the Rec. 4 cases of Lean Testing.
- Depends on: M1-8 (shares), M1-6 (consistency with the ledger).
- Implements: [Lean Rec. 4](swarm-lean-open-problems.md#4-m2-members-can-see-the-budget-that-will-stop-them).

**M2-4. Gate checks as metrics.** Per member, `gate_checks`, `gate_passes` and the swarm time of the first passing check, counted from intermediate `ScoreEvent`s of the task's first scorer in the member's own span; added to the metrics and to `MemberResult`.
- Files: `src/inspect_swarm/_metrics.py`, `src/inspect_swarm/_record.py`.
- Tests: the Rec. 5 cases of Lean Testing.
- Depends on: M1-10.
- Implements: [Lean Rec. 5](swarm-lean-open-problems.md#5-m2-gate-checks-as-metrics).

**M2-5. Background deepagent members.** The runtime enters AI-5's owner for each member and drains it; the rejection of `deepagent(background=...)` members is lifted; the ledger and statuses cover background children as D-2 specifies.
- Files: `src/inspect_swarm/_member.py`, `src/inspect_swarm/_swarm.py`, `uv.lock`; others as D-2 specifies.
- Tests: as D-2 specifies, at least a `deepagent(background=True)` member whose background child is cancelled and awaited by the drain and whose spend is in the member's ledger line.
- Depends on: AI-5 (merged on inspect_ai `main`), M1-4, M1-6.
- Implements: [Lean Rec. 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members); swarm.md Members ("Lifting the restriction").

**M2-6. Bus core.** `Record`, `Bus` and `deliver()` steps 0 to 4 with the liveness recheck, the stopping flag and `close()`; storm controls; `CommDecision`, `CommPolicy` and `SwarmView`; `sent` and `delivered` evidence; `Channel` gains `name`, `prefix`, `check()` and `open()`, with `ChannelRun` and `ChannelState`; `swarm(policy=)` with its `TypeError` for a string; `bus.close()` as the first synchronous act of `stop(reason)`.
- **Refusing bridged members on a bus channel** (decision: Ransom, 2026-10-14, which leaves bridged members' bus tools to future work). `swarm()` raises `ValueError` at construction when `channels=` holds a channel with a non-`None` `name` (one that opens state and gives tools; instructions-only channels such as `filesystem` have `name=None`) and any member is bridged, by swarm-limits.md's rule: `member(bridged=True)`, or `bridged=None` with a factory registered in the `inspect_swe/` namespace ([Knowing a member is bridged](swarm-limits.md#swarm-nodes)). The message names the member and the channel and says that bridged members cannot use bus channels yet. The check runs with swarm-communication.md's other construction checks (duplicate `name` or `prefix`). A bridged agent the rule misses is not refused; it has no way to reach the channel's tools, so it runs as a member without that channel, which `member()` documents next to its `bridged=True` remedy. No design specified this refusal before this plan; swarm-communication.md's [Bridged members](swarm-communication.md#bridged-members) assumed `swarm_bridged_tools()` would exist.
- Files: `src/inspect_swarm/_bus/_record.py`, `_bus/_bus.py`, `_bus/_storm.py`, `_bus/_policy.py`, `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_channel/_channel.py`, `src/inspect_swarm/_swarm.py`.
- Tests: `tests/test_bus.py`, the `policy` case of `tests/test_api_log.py`, and construction cases in `tests/test_bus.py`: an `inspect_swe/` agent member and a `bridged=True` member are each refused alongside a record-producing test channel; both are accepted with only `filesystem` or `channels=[]`. The construction cases swarm-communication.md put in `test_messages_channel.py` that need no messages channel (two instructions-only channels accepted, two record-producing test channels sharing a `name` or a `prefix` rejected, `channels=[]` and `channels=["filesystem"]` offering no swarm tools) move here with test channels, since that file is future work.
- Depends on: M1-4.
- Implements: [swarm-communication.md](swarm-communication.md#implementation-plan) step 1; [Records and the bus](swarm-communication.md#records-and-the-bus), [Storm controls](swarm-communication.md#storm-controls), [How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus).

**M2-7. Member binding.** The member handle gains M2's fields (effective delivery, inbox, wait event, bound loop) and the member ContextVar; `Member` gains `delivery`; `swarm_tools()` with first-resolution binding and the ACP-bound flag.
- Files: `src/inspect_swarm/_controller/_control.py`, `src/inspect_swarm/_member.py`, `src/inspect_swarm/_bus/_tools.py`.
- Tests: `tests/test_binding.py`, including the descendant cases D-1 decides.
- Depends on: M2-6, D-1 (the binding rule for a member's descendants).
- Implements: swarm-communication.md step 2; [Members and identity](swarm-communication.md#members-and-identity), [Tools reach members through a tool source](swarm-communication.md#tools-reach-members-through-a-tool-source).

**M2-8. The shared-memory channel.** As D-1 specifies: the `@swarm_channel` factory, its `ChannelState`, its tools, fenced reads and its evidence; exported and imported by the entry point. In place of the `messages` channel, it is the first channel with records, so the bus's fencing (`src/inspect_swarm/_fence.py`, shared later with `synthesize`) lands here.
- Files: `src/inspect_swarm/_channel/memory.py` (name as D-1 decides), `src/inspect_swarm/_fence.py`, `src/inspect_swarm/__init__.py`, `src/inspect_swarm/_entrypoint.py`.
- Tests: as D-1 specifies; `tests/test_fence.py`. If D-1 gives descendants the board, a synchronous `general()` child of a `deepagent()` member reads and writes it here, and a background `general()` child does so in a test that lands with whichever of M2-5 and M2-8 merges second; if D-1 restricts the board to the member's top-level loop, a test pins that restriction instead.
- Depends on: D-1, M2-7.
- Implements: [Lean Rec. 7](swarm-lean-open-problems.md#7-m2-a-shared-memory-channel-through-the-bus); [swarm-communication.md Fencing](swarm-communication.md#fencing).

**M2-9. Notices.** `swarm_on_continue()` with its continuation rules, the fixed template, notice evidence, the wiring checks and `delivery_effective` in the sample metadata, exercised through the memory channel's `notice_lines`.
- Files: `src/inspect_swarm/_bus/_notice.py`, `src/inspect_swarm/_swarm.py`.
- Tests: `tests/test_notices.py`.
- Depends on: M2-8.
- Implements: swarm-communication.md step 4; [Notices](swarm-communication.md#notices).

**M2-10. Exposure and communication metrics.** Post-hoc `exposed` events in the finalisation walk, exact for native reads by tool call id; record counts and sizes per member, rejections per control.
- Files: `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_metrics.py`.
- Tests: `tests/test_comm_evidence.py`.
- Depends on: M2-8, M1-10.
- Implements: swarm-communication.md step 5; [Evidence and provenance](swarm-communication.md#evidence-and-provenance).

**M2-11. User docs and examples.** A Quarto site in `docs/`, as inspect_sentinel and inspect_flow keep, and runnable `examples/`: getting started; members, controllers and channels; budgets, splits and the ledger; sandbox topologies (shared, per member) with sizing guidance; the shared-memory channel, the `policy=` hook and the filesystem observation limit (swarm-communication.md step 7's content for what M2 builds), and that bridged members cannot use it yet; scoring: the task's own scorers, the result record, and with per-member sandboxes a scorer that names each member's sandbox; the result record's store key `inspect_swarm.result` is owned by the runtime, task code must not write it, and a tool reading it gives members a view of each other's results, a channel outside the bus (decision: Ransom, 2026-10-14); comparisons with epochs through Inspect's own reducers ([swarm-scoring.md Comparisons in M1](swarm-scoring.md#comparisons-in-m1)); writing a controller, including `until_proved` with its precondition ([Lean Rec. 6](swarm-lean-open-problems.md#6-m1-tighten-submitted-m2-document-stopping-on-the-first-proof), second part); and an example shaped like Epoch's port.
- Files: `docs/` (new), `examples/` (new), `README.md`.
- Tests: examples run on mockllm in CI where they need no sandbox; the rest are marked slow.
- Depends on: the functional M2 pull requests it documents (M2-1 to M2-10).
- Implements: the documentation parts of every M2 item; swarm-communication.md step 7 for the memory channel.

**M2-12. API statement, packaging and the first release.** The README and the package docstring state which API is supported and which is experimental, as decided ([Decisions](#decisions), item 6); classifiers move from "Development Status :: 1 - Planning"; `requires-python` stays at M1-0's 3.11; the `push` trigger is restored in `release.yaml`, so Release Please opens the 0.1.0 release pull request.
- Files: `README.md`, `src/inspect_swarm/__init__.py`, `pyproject.toml`, `.github/workflows/release.yaml`.
- Depends on: M2-11, and an inspect_ai PyPI release containing AI-1, AI-2 and AI-5 (the floor in `pyproject.toml` names a release, so the work that uses them cannot merge before it ships). Epoch's configs pin inspect_ai 0.3.273 ([Lean, What they would gain and lose](swarm-lean-open-problems.md#what-they-would-gain-and-lose)); the port moves them to that release.
- Implements: what an external release needs.

## Future work

Unordered; each line is an item that may be done in any order or in part (decision: Ransom, 2026-10-07).

- Direct messages: the `messages` channel with `send_message`, `read_messages`, `list_members` and delivery modes (swarm-communication.md step 3) (decision: Ransom, 2026-10-14).
- Bus channel tools for bridged members, `swarm_bridged_tools()` (swarm-communication.md step 6), which lifts M2-6's refusal (decision: Ransom, 2026-10-14).
- Structured channels beyond the shared memory: notes or fact log, task list with claims and leases, board.
- Coordinator topologies, persistent members (the `submit=False` protocol or an inspect_ai PR for clean `react()` re-entry), wake from idle and quiescence-based termination.
- Push delivery, each opt-in: reminders, content injection, urgent messages (with the inspect_ai `Steer` pull request), nested notices, push metrics; push for bridged members in inspect_swe.
- The result contract and the record's additions: `ResultSpec`, verifiers, `verdict` events and `VerdictSummary` (swarm-scoring.md Part 2 step 1). The scoring items below except `best_at(k)` build on it (decision: Ransom, 2026-10-14: out of M2).
- `member_scores` with team@k and mean member (step 4).
- `baseline()`. Without the contract it would only name M1's swarm of one, `swarm(members=agent, channels=[])`, which swarm-scoring.md says M1 needs no helper for. What it adds is passing the task's result spec, so every arm gets the same run-time verdicts select@k needs; it lands with the contract.
- The `best_at(k)` reducer. It needs no contract and can land alone at any time; for binary scores such as Epoch's verdicts it equals Inspect's `pass_at(k)`, so the port gains nothing from it in M2.
- The analysis helper: `attempt_rows`, `compare_arms`, `best_at_k`, `select_at_k` (step 5).
- Per-member sandboxes in the scoring contract (Lean Recommendation 8), after a design that assigns how the headline scorer grades the selected member's sandbox (Inspect scores in the parent sample's context, where `sandbox()` is `default`: `src/inspect_ai/agent/_as_solver.py:71-82`, `src/inspect_ai/_eval/task/run.py:3093-3095`, at `81c1668d3`), the headline value when no member submitted while a sandbox holds a passing artifact, the verifier's sandbox for each submission, and the drained `final_verdict`.
- The final-answer chain (`verify`, `vote`, `first`) with `stop_on_verified`, after deciding swarm-scoring.md's open question 1.
- `synthesize` as a final-answer mode.
- Per-member artifacts in a shared sandbox through `materialize()`.
- The solve/scoring cost boundary.
- The dependency-linked critical path (the longest chain of member steps linked by reads of another member's writes), over the bus's attributed writes and `exposed` reads, with the evidence D-1 names; latency curves are interpreted only from then (decision: Ransom, 2026-10-14).
- Users isolation in one sandbox ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md)), with its inspect_ai, inspect_swe and inspect_swarm pull requests.
- Further isolation rungs: hidden processes, bubblewrap, per-member cgroups, per-member network rules, Landlock.
- Bridged swarms: Codex multi-agent v2 and Claude Code agent teams in inspect_swe, with their traffic mapped to swarm evidence.
- Safety hooks: roles as data, per-member prompt, model and tool overrides, joint-monitor helpers on inspect_sentinel, a scheduled execution mode.
- Replacing `policy=` with a sentinel adapter (swarm-communication.md's open question 1).
- A first-class inter-agent event type in inspect_ai (swarm.md's open question 1).
- ORBIT on inspect_swarm (PR #4).
- Red-team features: forged senders, secret channels, targeted injection.
- Inspect Scout scanners for coordination failures.
- The inspect_ai fix for limits reached inside bridged agents (swarm-limits.md's open question 2).
- Checkpoint and resume for swarms.
- A swarm view in the log viewer.

## Alternatives considered

**Per-member sandboxes in M1.** They serve the external port, not first internal use, which runs on the shared default. Putting them in M2 keeps M1 small, and M2-1 is independent of the rest of M2.

**Users isolation in M2 instead of per-member sandboxes.** [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md) gives members of one sandbox their own Linux users. Epoch's swarm runs members in separate, network-isolated containers, which users isolation does not reproduce (shared kernel, `/tmp`, process table and network), and it costs two inspect_ai pull requests and one in inspect_swe ([its plan](swarm-sandbox-isolation.md#implementation-plan), steps 1 to 3). Per-member sandboxes are one inspect_swarm pull request on private inspect_ai names the designs already allow. Users isolation stays future work, as its design says ("No milestone").

**Direct messages in M2 as well as the shared-memory channel.** It costs the `messages` channel and its bridged tools (about two more pull requests, swarm-communication.md steps 3 and 6), which no known external user needs. Ransom chose to leave them out ([Decisions](#decisions), item 2).

**Leave the bus out of M2** and keep the shared board an observed `memory(instance=...)` channel, which already works in M1. The port needs nothing more to run. But the bus-routed board is the gain the Lean analysis names for attribution, fencing and notices, and Ransom's M2 brief lists a shared-memory channel. Rejected with item 2.

**The result contract and per-member scoring in M2**, because the port's headline comparison (a 32-member pool against 32 single-agent attempts) is team@k against best@k on realized cost. But the port runs with its own scorer, its end-of-sample check already grades every member's sandbox, and the Lean analysis lists these as optional gains; artifact scoring under per-member sandboxes also needs a further design before it can be built. Ransom left them out ([Decisions](#decisions), item 8).

**`best_at(k)` or `baseline()` in M2 without the contract.** Both could stand alone: `best_at(k)` is a reducer over epoch scores, and `baseline()` without a result spec is M1's swarm of one under a name. Neither is worth an M2 pull request: Epoch's verdicts are binary, where `best_at(k)` equals `pass_at(k)`, and the swarm of one needs no helper.

**One pull request per deep-dive step, in each deep dive's own order.** Simpler to trace, but the deep dives already pair swarm-api.md step 4 with swarm-limits.md step 2 (members are started through the wrapper, and the stopping tests run through `swarm()`), and swarm-limits.md step 3's tests check step 4's ledger. M1-4 and M1-7 follow those dependencies.

## Compatibility and migration

- **inspect_swarm** has no released API until M2-12; nothing in M1 or M2 before it is a compatibility concern. From M2-12, the API statement says what later releases keep.
- **Eval logs.** M1 and M2 write only existing event types, store entries and message metadata, each payload carrying `version: 1`, as the deep dives specify. No log schema, generated TypeScript type or ts-mono change is in either milestone.
- **inspect_ai.** AI-1 adds two literal values outside the log schema; AI-2 changes nothing outside a scope; AI-5 changes nothing outside an owner scope, as D-2 must confirm. The optional AI-3 and AI-4 add observability without changing behaviour.
- **External users' inspect_ai versions.** A release of inspect_swarm requires an inspect_ai release with its prerequisites, so users pinned to older inspect_ai (Epoch: 0.3.273) must move.

## Security

This plan adds no behaviour of its own. Each pull request carries its design's Security section: [swarm.md](swarm.md#security), [swarm-api.md](swarm-api.md#security), [swarm-communication.md](swarm-communication.md#security) for the bus and the memory channel (whose payloads are peer model output, fenced and stamped with the bus-bound sender), [swarm-limits.md](swarm-limits.md#security), [swarm-scoring.md](swarm-scoring.md#security), and [swarm-lean-open-problems.md](swarm-lean-open-problems.md#security) for per-member sandboxes (sandbox names come from the eval author and are validated at start; a service in `visible_sandboxes` is reachable by every member, a trust decision for the eval author). D-1 and D-2 each need their own Security section: the memory channel handles model-written file names and contents, and the background owner changes who can outlive a member. The result record in the store is readable by every member's code, though not by models; a tool that reads it is a channel between members outside the bus, with none of its fencing or evidence, which the docs say (M1-13, M2-11).

## Testing

Each pull request carries the tests listed with it, from its design's Testing section. Across both milestones:

- Runtime tests use mockllm with scripted outputs, need no network or Docker, and run on asyncio and trio (`tests/conftest.py`'s `anyio_backend`).
- Filesystem and per-member-sandbox tests use the local sandbox or stub sandbox environments where possible; Docker tests are marked slow and skipped in CI without Docker.
- Bridged tests (Claude Code, Codex members) need Docker, inspect_swe and provider keys; they run by hand or on a schedule, never in PR CI.
- inspect_ai pull requests carry their tests in inspect_ai's own suite.
- Pinned-behaviour tests (M1-2) fail here when an inspect_ai change breaks an internal name the swarm uses, which is how tracking inspect_ai `main` stays safe.

## Decisions

Ransom's answers to this plan's questions, on 2026-10-14 (his words quoted), and one point settled from the code.

1. **M1's final answer is `first` plus the per-member record.** The brief's "final-answer modes, the result contract" is read as M1 ([swarm-scoring.md Part 1](swarm-scoring.md#part-1-m1)). Ransom: "OQ1 - yes". With item 8, the result contract is future work rather than M2.
2. **M2 has the bus without direct messages.** M2 has the bus core, binding, notices, exposure and the shared-memory channel; the `messages` channel and bridged members' bus tools are future work, and a swarm that gives a bridged member a bus channel is refused at construction (M2-6). Ransom: "OQ2 agree with the recommendation".
3. **Designs D-1 and D-2 start during M1, and M1's critical path is the simple definition.** Ransom: "Agree on starting D1 and D2 in M1", and on the critical path, "OQ3 agree with the recommendation": M1-11 defines it as the steps to the answer and the longest member's step count, needs no separate design and lands inside M1; the dependency-linked definition is later work over the bus's evidence, whose needs D-1 states; swarm.md's caution on latency curves stands.
4. **`baseline()` is future work**, with the result contract (item 8).
5. **Monitoring keeps the `policy=` hook.** Settled from the code, not by Ransom: inspect_sentinel's dispatcher is on inspect_ai `main`, but its tool stages still skip bridged agents (`src/inspect_ai/_sentinel/_context.py:57-64`, at `81c1668d3`; inspect_ai#5759), and internal bus records are not tool calls. So M2 builds the `policy=` hook as swarm-communication.md designs it, and native members' channel tool calls are also seen by sentinel's tool stages with no change. Replacing `policy=` with a sentinel adapter stays future work.
6. **What the first release calls supported.** Supported: `swarm()`, `member()`, `Budget`, the built-in controller and channel names, and the version-1 record and evidence formats, with breaking changes called out in release notes. Experimental: the `Controller`/`SwarmControl` and `Channel`/`ChannelState` interfaces for user-written controllers and channels, as swarm-communication.md already says of the channel operations. Docs are a Quarto site in `docs/`, like inspect_sentinel's and inspect_flow's. Ransom: "OQ6 agree with the recommendation".
7. **D-1 decides whether a member's descendants reach the board.** The bus's binding rule ([Members and identity](swarm-communication.md#members-and-identity)) gives swarm tools only to a member's first agent loop, but Epoch's members pass their `memory()` to deepagent's `general()` children (`src/inspect_ai/agent/_deepagent/deepagent.py:328-335`, at `81c1668d3`), so up to 64 background children per member read and write the board today ([Lean, Their swarm](swarm-lean-open-problems.md#their-swarm)). D-1 decides descendant access for the memory channel, with how writes are attributed to the member and its loop and what read and exposure evidence mean with several readers, and M2-7's binding follows D-1 for this channel. Ransom: "a".
8. **The result contract and the scoring built on it are not in M2.** The result contract and record additions; `member_scores`, `best_at(k)` and `baseline()`; per-member sandboxes in the scoring contract, with the design artifact scoring under them needs; and the analysis helper are [future work](#future-work). Epoch's port needs only Recommendations 1 to 3 to run with its scorer unchanged, the Lean analysis lists per-member scores, `baseline()` and `best_at(k)` as optional gains after M2, and M2's "Done means" does not mention scoring. Ransom: "Agree that this should be moved out of M2." M2-1 documents what a scorer sees meanwhile.

9. **M1-10 reports "members stuck in loops" as a signal, not a verdict.** swarm.md lists the check among the harness-validity checks ([Observer, Analysis](swarm.md#observer-evidence-accounting-and-metrics)). Per member, the longest run of consecutive identical tool calls (same function and arguments) is reported as a metric, with no threshold fixed until there is data. Ransom: "OQ1 agree with recommendation".

One more decision shapes this plan: the result record's store key is documented as owned by the runtime and readable by member code (Ransom: "yes, add the docs line"; M1-13, M2-11 and [swarm-scoring.md What the swarm records](swarm-scoring.md#what-the-swarm-records)).

## Open questions

None. Every question this plan raised is decided above.

## Not this design

Noticed while planning and left out, for Ransom to file if wanted:

- **The comment in `pyproject.toml`** says the Python floor "Matches inspect-ai's floor"; M1-0 changes both, and the comment should say why the floor differs.
- **A docs-publishing workflow.** Neither inspect_sentinel nor inspect_flow has one in `.github/workflows`; how the Quarto site is published is decided with M2-11.
