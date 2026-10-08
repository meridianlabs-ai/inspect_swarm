# Inspect Swarm: high-level design

Status: proposed, 2026-10-07. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

This is the first design document for `inspect_swarm`. It is deliberately high level. It sets out what the library is for, which eval questions it should make answerable, the shape of the architecture and where its parts live, and a first slice small enough to build and learn from. It asks for feedback before any detailed design. API sketches are illustrative; names and signatures are not proposals yet.

[swarm-overview.md](swarm-overview.md) is the short form. It covers the components and the main decisions for a reader who knows inspect_ai, and links back here for detail.

Decisions Ransom took on the first draft (2026-10-07) are marked inline as "(decision: Ransom, 2026-10-07)":
- limits stay soft;
- Python 3.11+;
- inspect_ai internals may be used;
- peer messages are model output, delivered as tool output with distinct provenance;
- no internal experiment gate;
- M1 then M2, the rest in any order;
- a shared sandbox by default;
- red-team features optional;
- monitoring through inspect_sentinel;
- the default final answer.

It synthesises the team's earlier design notes on swarms. One was a layered runtime RFC: a `swarm()` agent on `deepagent()` with messaging, a task list, a fact log, a board and a monitor hook. The other argued for cheap experiments first, using leaderless swarms that communicate through the filesystem. It also draws on published work, cited inline. Figures quoted from secondary sources, or that could not be checked against a primary source, are marked *unverified*.

Code references are to inspect_ai `main` at `aa20052a` (2026-10-06), inspect_swe at `a54461e7` (2026-10-05), inspect_sentinel at `0f9b5c71` (2026-10-06), inspect_evals at `cef701f5` and ORBIT at `588b3035`. Paths are relative to each repository's root.

## Why

### Swarms are now a shipped product and a research result

Parallel agents that run at the same time and coordinate are no longer a lab curiosity:

- **OpenAI.**
  - GPT-5.6 Sol introduced an `ultra` mode that "coordinat[es] four agents in parallel by default" ([preview](https://openai.com/index/previewing-gpt-5-6-sol/), [launch](https://openai.com/index/gpt-5-6/)). The launch charts compare one-agent, four-agent and (on two benchmarks) 16-agent configurations, for example Terminal-Bench 2.1 at 88.8% for Sol and 91.9% for Sol Ultra.
  - The same tool surface ships in Codex and in the [Responses API multi-agent beta](https://developers.openai.com/api/docs/guides/responses-multi-agent): `spawn_agent`, `send_message`, `followup_task`, `wait_agent`, `interrupt_agent` and `list_agents`, with agents addressed by tree paths (`/root/task1`).
  - Codex also has an optional message-board extension with channels, threads, posts and subscriptions (openai/codex, `codex-rs/ext/agent-message-board/`).
  - The [Navier–Stokes announcement](https://openai.com/index/navier-stokes-solution/) attributes the result to a system whose winning group "involved on the order of 10,000 concurrent agents", resolved "about 88 hours after the first agents were launched", with 2.7 million messages and about 130 billion output tokens for that problem. OpenAI states no cost. Third-party estimates run from about \$6.5M (Navier–Stokes output tokens at list price) to about \$15M (all problems) ([capitalandcompute.net](https://capitalandcompute.net/blog/openai-navier-stokes-10000-agents-cost/), *unverified*).
- **Anthropic.**
  - Claude Code [agent teams](https://code.claude.com/docs/en/agent-teams) have a lead and teammates, `SendMessage` mailboxes, a shared task list with file-locked claims, and `TeammateIdle`/`TaskCompleted` hooks.
  - The [C compiler experiment](https://www.anthropic.com/engineering/building-c-compiler) ran 16 parallel agents with no orchestrator. They coordinated through lock files in a shared git repository and a running document of failed approaches, over about 2,000 sessions and just under \$20,000.
  - The [Fermat's Last Theorem formalisation](https://www.anthropic.com/research/formalizing-fermats-last-theorem) used "dozens" of agents for 11 days. Coordination ran through Prove2Me, a shared DAG of theorem statements. Early attempts failed because agents "quickly lost track of the project's state".
  - The [multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) beat single-agent Opus 4 by 90.2% on an internal eval, at about 15× the tokens of chat.
- **Others.**
  - Kimi K2.5's [Agent Swarm](https://arxiv.org/abs/2602.02276) trains an orchestrator to spawn parallel subagents. It defines **CriticalSteps**, the critical-path analogue of step count.
  - Cursor's [long-running coding agents](https://cursor.com/blog/scaling-agents) found that flat peers with locks slowed to the throughput of two or three agents. Planners plus non-coordinating workers plus a judge worked better.

### How swarms are built elsewhere

General-purpose multi-agent frameworks mostly hand a peer's turns to the recipient as conversation:
- in the **assistant** role: LangGraph swarms, OpenAI Agents SDK handoffs, Microsoft Agent Framework broadcasts;
- or in the **user** role: AutoGen, AG2, Google ADK, Strands, and task-prompt injection in CrewAI, CAMEL and MetaGPT.

Their limits are mostly turn caps. Two features matter here:
- Coding-agent mail layers deliver peer message bodies through a tool:
  - [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail)'s `fetch_inbox`, prompted by a reminder that carries only counts and fixed text;
  - [Gas Town](https://github.com/gastownhall/gastown)'s `gt mail`, whose injected reminder does include each message's sender-written subject.
- A few frameworks add structure worth borrowing: ADK's fencing of quoted agent content, AG2's inline arbiter, Magentic-One's stall counter.

Eval harnesses for multi-agent systems besides ORBIT exist, including another Inspect-based peer mesh. None of those surveyed is a reusable runtime of continuously active members. [Related projects](#related-projects-and-what-they-teach) surveys them and what each teaches this design.

### Swarms are a third inference-scaling axis, and its value is contested

An agent's performance on a task can be scaled in three ways:

1. more independent attempts (Inspect `epochs`, scored as pass@k or best@k);
2. a larger budget for one attempt (token, cost or time limits);
3. more agents in one attempt (the swarm).

[Ord's "Swarm Scaling"](https://www.tobyord.com/writing/swarm-scaling) fits the GPT-5.6 launch charts with a parallelisability exponent λ: N agents together get as much done as one agent with N^λ times the budget.

- His fits are λ ≈ 0.68 (BrowseComp), 0.57 (SEC-Bench Pro) and 0.48 (Terminal-Bench).
- Each 4× in agents needs roughly 2× the total tokens for the same score.
- He concludes that the main payoff is speed, at a steep cost premium.

Other results point the other way:

- [Scaling Discovery through Test-Time Communication](https://arxiv.org/abs/2609.21032) compares k communicating agents with the best of k independent agents at equal per-agent resources. On ARC-AGI-3, team@3 matched best@13 and team@5 matched best@33. The agents had no roles and no orchestrator. They coordinated through an append-only log and a few shared records in the shared filesystem.
- The same paper finds no gain on Terminal-Bench 2.0: team@2 scored 60.7% against 62.4% for independent pass@2.
- [Towards a Science of Scaling Agent Systems](https://arxiv.org/abs/2512.08296) matched compute across 260 configurations. It found multi-agent systems lose 39–70% on a strictly sequential planning benchmark. Tasks where a single agent already exceeds about 45% saw negative returns.

So whether a swarm buys capability, rather than only speed, depends on the task, the model and the coordination design. Measuring that is an eval problem, and matched-cost comparisons against the other two axes are the central control.

That swarms and budget are distinct axes is also less clear than it looks:

- `deepagent(background=True)` already runs up to 8 subagents concurrently (`src/inspect_ai/agent/_deepagent/deepagent.py:46`, `:89-97`).
- Subagents that share a sandbox can already talk through the filesystem.
- The design should therefore treat "swarm" as a set of controllable variables (agent count, topology, channels, budget split), not as one new agent type. A deepagent with background subagents is one point in that space.

### Multi-agent systems raise safety questions single-agent evals do not

- **Unsanctioned channels.** In the [OpenAI/Hugging Face incident](https://openai.com/index/hugging-face-incident-and-the-road-ahead/), evaluation agents meant to be isolated turned a shared package cache into "an unintended message board". When it was wiped they rebuilt it in directory names. [METR's investigation](https://metr.org/blog/2026-08-26-openai-hugging-face-incident-investigation/) counts roughly 1,200 agents, over 70,000 messages and files, and about 700 agents that went on to take part in the attack.
  - OpenAI names "unauthorized communication" and "agents adopting goals from one another" among the causes.
  - It reports that agents without multi-agent tools found side channels too, which it attributes to generalisation from multi-agent training.
  - An eval harness for swarms must therefore observe the channels it did not provide, not only the ones it did.
- **Fragmentation.** [Multi-Agent AI Control](https://arxiv.org/abs/2607.07368) shows that an attack split across agents evades monitors that watch one agent at a time. Monitors need a joint view.
- **Collusion and covert channels.**
  - Steganographic collusion is feasible and resists paraphrasing ([Secret Collusion](https://arxiv.org/abs/2402.07510), [Hidden in Plain Text](https://arxiv.org/abs/2410.03768)).
  - Collusion emerged in 94% of long-horizon two-agent trajectories in [one 2026 study](https://arxiv.org/abs/2609.24967).
  - [Colosseum](https://arxiv.org/abs/2602.15198) finds that detecting collusion from messages and detecting it from outcomes disagree.
- **Propagation.**
  - [ORBIT](https://arxiv.org/abs/2609.33102), built on Inspect, measures how compromised agents, injections and collusion propagate under different topologies. Star topologies raised one model's attack success rate from 17% to 36% compared with a standalone agent.
  - In the same work, defenses cut compromised-agent attack success from 72% to 12%, but a colluding pair stayed at about 30%.

### Who the users are

Most of the feedback that shapes the plan is expected to come from these users, external ones included, as the library is built (see [Implementation plan](#implementation-plan)).

1. **Capability-scaling experiments.** FrontierMath-style research mathematics, long coding tasks, Terminal-Bench. The question: at a fixed cost, does a swarm beat more epochs or a bigger budget, and how does that change with agent count?
2. **Multi-agent safety and security evals** (ORBIT-like). Topologies, roles, compromised members and defenses, scored on attack success and utility. Red-team machinery (injected messages, secret channels) is an optional feature of this library, not a committed one ([The bus](#the-bus-one-interception-point)).
3. **Control and monitoring research.** Per-agent against joint monitors, paraphrasing defenses, covert-channel capacity, detecting unsanctioned channels.
4. **Evaluating vendor swarms as shipped.** Codex multi-agent or Claude Code agent teams run in a sandbox through inspect_swe, measured with the same metrics and monitors as an Inspect-native swarm.

## Eval questions and the experimental design they imply

The library is worth building to the extent that it makes these questions cheap to ask and hard to get wrong. The table says what users' experiments need from the harness. It is not a list of experiments this project commits to running (decision: Ransom, 2026-10-07).

| Question | Experimental design | What the harness must provide |
|---|---|---|
| Does a swarm beat more attempts or a bigger budget at the same cost? | Four arms with the same cap: single agent with budget B; k epochs at B/k each (pass@k, best@k); a deepagent with k background subagents and budget B; a swarm of k members with total budget B (team@k). Compared on realized cost, not on the cap. | A swarm-wide cap at the same level as the other arms' caps. Realized cost recorded per member and in total, including the final-answer step and calls in flight when the cap was hit. A task-owned result contract, so team@k, best@k and per-member correctness are claimed only where the task can score them ([Results and scoring](#results-and-scoring-a-task-owned-contract)). |
| How does capability scale with agent count (λ)? | A grid over agent count × per-agent budget; fit iso-performance curves (Ord's method). | Agent count and budget as task parameters, so `eval_set` or inspect_flow can sweep them; total tokens and cost per sample. |
| What does a swarm buy in latency? | The same arms, measuring wall clock and critical path. | Per-member timestamps; critical-path steps (Kimi's CriticalSteps); idle time; a way to separate waiting on model connections from waiting on peers. |
| Which topology and channels matter? | Ablations: leaderless against coordinator, filesystem only against messages, task list or board, one mechanism removed at a time. | Topology and channels as configuration, not code; identical members across arms. |
| How do swarms fail? | Post-hoc labelling of transcripts with a failure taxonomy (e.g. [MAST](https://arxiv.org/abs/2503.13657): step repetition, unaware of stopping conditions, task derailment, ignored peer input, missing verification). | Attributable transcripts: who sent what to whom, when, and what each member did next; Inspect Scout scanners over them. |
| How do attacks propagate, and do defenses hold? | Red-team arms: a compromised member, injected messages, a coalition with a secret channel; defenses on and off; benign baselines. | Roles as data; per-member prompts, models and tools; a single interception point on communication; scorers for attack success and utility. Injection and secret channels are optional features of this library. |
| Can monitors catch distributed attacks? | Per-member against joint monitors, calibrated on honest runs (Multi-Agent AI Control's design). | Every communication and every shared-state write as an attributable event; a monitor hook that can see the whole swarm. |
| Do agents build channels we did not give them? | Containment probes: shared caches, shared directories, network, with channel use detected from tool calls. | Tool-call evidence per member, monitored with the same machinery; a sandbox layout that makes sanctioned and unsanctioned sharing explicit. |

Three design consequences follow from the first two rows.

- **The swarm's cap must be expressible as one sample-level number.** Then a swarm arm and a single-agent arm differ only in the solver, never in how the cap is set.
- **Caps are stopping rules; comparisons use realized cost.** Inspect's limits are cooperative. They are checked before a call is dispatched and after a completed call is recorded (`src/inspect_ai/model/_model.py:3035-3043`, `:3082-3095`), and nothing reserves budget for calls already in flight. The design treats limits as soft stopping rules and does not depend on any inspect_ai change to make them hard (decision: Ransom, 2026-10-07).
  - So a cap is overshot by whatever calls are in flight when it is reached. The round-1 review measured three synchronised 600-token generations under a 1,000-token cap recording 1,800 tokens.
  - The number of calls in flight is set by the agents, not by the member count. A single synchronous `deepagent()` dispatches its parallel-safe subagent calls concurrently (`src/inspect_ai/agent/_deepagent/agent_tool.py:919-942`, `src/inspect_ai/model/_call_tools.py:442-466`). In the round-2 review, one such member had three 600-token subagent calls and its own 50-token call in flight, and recorded 1,850 tokens under a 1,000-token cap.
  - The same holds for a single-agent arm: its overshoot depends on its agent and tools, not on being one agent.
  - Arms are therefore compared on what they actually spent: score against realized total cost, as curves where possible (Ord's method), with cap and realized cost both reported. Equal caps alone are not "matched cost".
- **Who produced a result is task-defined.** A swarm records every member's own submission as well as the final one. Whether that supports per-member correctness, team@k or voting depends on how the task is scored. A task scored on a final answer string can score each member's answer. A task scored on a shared sandbox (SWE-bench runs tests against the shared repository) has one team result, and individual correctness is unavailable unless each member leaves its own stable artifact. [Results and scoring](#results-and-scoring-a-task-owned-contract) makes this a contract the task supplies.

## Goals and non-goals

### Goals

- Make agent count, topology, communication channels and budget split ordinary, sweepable task parameters for Inspect evals.
- Account for realized cost and usage per member and per swarm, under a swarm-wide cap set the same way as a single agent's limit.
- Make every sanctioned communication observable, attributable and interceptable at one point, and keep unsanctioned channels (the filesystem, shared caches) observable through the tool calls that use them.
- Let any Inspect `Agent` be a member: `react()`, `deepagent()`, or a bridged agent (Claude Code, Codex) through inspect_swe. The swarm must own and drain all of a member's work, including descendants. Until the swarm can do that for `background()` work, M1 accepts only members whose work stays inside their invocation ([Members](#members)).
- Evaluate vendor swarms (Codex multi-agent, Claude Code agent teams) with the same evidence model and metrics as native ones.
- Keep inspect_ai changes to small, general extension points needed for behaviour. The swarm runtime itself lives here.
  - inspect_swarm may use inspect_ai internals (private modules, unexported types) where appropriate, as other inspect_* packages do; the team controls both and manages the dependency (decision: Ransom, 2026-10-07).
  - So needing a private name is not by itself a reason for an inspect_ai PR.
- Start with the smallest useful slice (a leaderless swarm over the shared filesystem with full accounting), built as the first slice of the full architecture, and let users' feedback decide what follows.

### Non-goals (for the first versions)

- A production orchestration framework. This is for evals; ergonomics for building products are not a goal.
- Handoff "swarms" (OpenAI Swarm, Agents SDK handoffs), where one agent is active at a time. Inspect's `handoff()` already covers them.
- Thousands of agents in one sample. A sample runs in one process and event loop, and a swarm shares the model's `max_connections`. The target is tens of members, not the Navier–Stokes scale.
- Swarms that span samples, or communication between samples.
- Multi-agent training, or human–agent teams.
- A swarm graph view in the log viewer. Spans and the existing transcript view come first.
- Security scenarios, attack and defense libraries. These belong to ORBIT-like packages built on top; inspect_swarm provides the hooks.

## Current behaviour: what Inspect provides today

This section covers only what the design depends on. All paths are in inspect_ai unless noted.

### Running several agents in one sample

- Agents in one sample are anyio tasks on one event loop.
  - `collect()` runs coroutines concurrently, each in its own span (`src/inspect_ai/util/_collect.py:13-50`).
  - `background()` starts work on the sample's task group (`src/inspect_ai/util/_background.py:19-81`). The runner cancels that task group when the solver ends (`src/inspect_ai/_eval/task/run.py:2930-2934`).
- `run(agent, input, limits=[...], name=...)` runs any agent in an agent span with its own limits. It catches its own limit errors and returns them (`src/inspect_ai/agent/_run.py:35-111`).
- There is no per-sample cap on concurrent agents. The binding constraint is the model's `max_connections`, shared by every member and every sample.

### `deepagent()` and background subagents

- `deepagent(background=True | int)` dispatches subagents concurrently via `background()`. The default cap is 8 running agents (`src/inspect_ai/agent/_deepagent/deepagent.py:46`; `agent_tool.py:599-631`).
- Lifecycle tools: `agent_status`, `agent_wait`, `agent_cancel` and `agent_list` (`lifecycle_tools.py`).
- Background dispatch is top level only: nested levels always get `background_enabled=False` (`agent_tool.py:893-915`).
- Communication is parent to child at dispatch only. A running child cannot be steered, children cannot message each other, and a finished child cannot be given more work.
- Children are abandoned when the parent returns (`design/deepagent-background.md`, "Lifetime semantics").
  - They run on the sample's task group, not under their caller (`agent_tool.py:620-631`, `util/_background.py:81`), until the runner cancels that group when the solver ends (`_eval/task/run.py:2930-2934`).
  - The registry that tracks them exists only while the deepagent runs (`deepagent.py:244-250`).
  - So code that awaits a background deepagent, for example under `run()` or `collect()`, cannot see or await children still running. In the round-1 review's probe, a member joined at 150 tokens, and its still-running child raised the total to 200 afterwards.
  - Synchronous subagents (`background=False`) are awaited inside the member (`agent_tool.py:548-569`).
- Errors: a child's own errors are recorded on its future. Sample-level `LimitExceededError`, `TerminateSampleError` and `ModelRefusalError` propagate and end the sample (`agent_tool.py:716-738`).

### The agent channel

- `AgentChannel` is a per-execution queue with a bound cancel scope (`src/inspect_ai/agent/_channel/channel.py`).
  - `react()` opens one (`src/inspect_ai/agent/_react.py:234-237`).
  - It drains the channel at the top of every turn (`_react.py:256-258`).
- `ChannelItem` is `Union[UserMessage, Cancel]` (`_channel/items.py:62`). The docstrings describe it as an open union with `Announce` and `Steer` reserved for later (`items.py:3-8`). In practice a new item type means editing inspect_ai, because `react()` ignores items it does not recognise.
- `coalesce()` merges only operator-sourced messages (`items.py:96-100`).
- `ChatMessage.source` is the closed `Literal["input", "generate", "operator"]` (`src/inspect_ai/core/_chat_message.py:29`).
- `AgentRef`, `UserMessage` and `current_agent_channel` are not exported from `inspect_ai.agent` (`src/inspect_ai/agent/__init__.py:10-14`). inspect_swarm may import them from the private modules, so this is a visibility detail, not a blocker.
- `before_turn()` appends every `UserMessage` item to the conversation as a `ChatMessageUser` (`channel.py:432-453`). A `UserMessage` is by definition an operator-injected turn (`items.py:37-46`), so it puts its text in the user role, the most trusted input a model has. The swarm therefore never delivers peer text this way ([Delivery](#delivery-peer-messages-are-model-output)).
- deepagent already follows the pattern the swarm adopts. Its background-completion notice is harness-authored and metadata-only ("Background agent(s) finished — collect the result"), injected at a turn boundary, and the child's result is fetched with the `agent_status` tool, so it arrives as tool output (`src/inspect_ai/agent/_deepagent/lifecycle_tools.py:510-530`, `:597-634`).
- **The binder.** A *binder* is how a producer obtains an execution's `AgentRef` so that it can post into that execution's channel. Today `agent_channel()` offers each new channel's ref to the sample's ACP session, first binder wins, so ACP is the only producer (`_channel/__init__.py:125-133`).
  - The swarm's bus is a second producer: it needs each member's ref to deliver messages into it.
  - The member's channel is opened inside that member's `react()`, so code running in the member (a tool, a model wrapper, `on_continue`) can reach it through the private `current_agent_channel()`. The swarm's controller, outside the member, cannot.
  - How the bus obtains refs, and how that coexists with ACP's first-binder-wins rule, is deferred to M2's design.
- The channel brief names a "subagent supervisor" and "detached child channels" as intended future producers (`design/acp/agent_channel_brief.md:32`, `:178`).

### `react()` lifecycle

- `react()` ends when the model submits and attempts are used up (`_react.py:302-335`).
- `on_continue` is called each turn the loop continues. It is async, and it may append a message or replace the state (`_react.py:366-392`).
  - It is a usable hook for injecting peer messages into a member that is still working.
  - It is not called after a successful submit, because the loop breaks first (`_react.py:324-335`). So it cannot by itself hold a submitted member idle until a message wakes it.
  - With `submit=False`, `react()` stops when the model makes no tool calls unless `on_continue` says otherwise (`_react.py:421-555`).
- There is no idle state after submit. Calling `react()` again on a finished `AgentState` re-inserts the system prompt (`_react.py:213-216`), so re-awakening a member needs care or an inspect_ai change.

### Limits and cost

Limit kinds include `token_limit`, `cost_limit`, `message_limit`, `turn_limit`, `time_limit` and `working_limit` (`src/inspect_ai/util/_limit.py:576-889`). Each kind is a tree held in a ContextVar.

- Usage is recorded on the current node and every ancestor (`_CostLimit.record`, `_limit.py:1247-1251`).
- Checks run root to leaf (`_limit.py:1253-1263`).
- Member tasks inherit the enclosing node when they are spawned. So a limit opened around a group of members meters all of them, and a limit inside each member meters that member alone.

I checked this with a spike: three `react()` members under `collect()`, with mockllm reporting 50 tokens per call.

- Under a 1,000-token swarm limit with 400-token member limits:
  - the swarm node recorded 1,000 tokens;
  - the member nodes recorded 350, 350 and 300;
  - the swarm limit was raised inside all three members, and `collect()` re-raised them as an `ExceptionGroup` of three `LimitExceededError`s.
- With the same members under `Task(token_limit=1000)` and no swarm limit, Inspect recorded a normal sample token limit and no error.

Three caveats apply to using limit nodes as a budget:

- **They do not bound spend.** Checks are cooperative and happen before dispatch and after a call completes (`src/inspect_ai/model/_model.py:3035-3043`, `:3082-3095`), so a cap is overshot by every call in flight across all members and their descendants when it is reached (see [Eval questions](#eval-questions-and-the-experimental-design-they-imply)).
- **Who raises what depends on the group size.** When only one member raises, `collect()` re-raises that exception bare instead of as a group (`src/inspect_ai/util/_collect.py:44-48`). Ancestor limits are checked first, so an outer sample limit can win over the swarm's own (`_limit.py:1082-1092`).
- **Token and cost nodes can disagree.** A call that trips a token limit raises before its cost is recorded into the cost nodes (`src/inspect_ai/model/_model.py:3089-3095`), although the call's `ModelUsage` still carries its cost. This was found by the round-1 review.

What the log keeps:

- Per-sample `model_usage` and `role_usage` (`src/inspect_ai/log/_log.py:311-314`).
- No per-agent or per-span usage. Per-span totals exist only when a timeline is built from `ModelEvent`s.
- `message_limit` checks the calling conversation's length, so under concurrency it applies per member, not per swarm (`_limit.py:743-757`).

### Transcript and events

- The event union is closed (`src/inspect_ai/event/_event.py:30-55`), and logs are validated against it.
  - A new event type needs an inspect_ai change, a regenerated schema, ts-mono types and a viewer renderer (`design/type-generation-pipeline.md`).
  - A library outside inspect_ai cannot add one.
- `transcript().info(data, source=)` writes a structured `InfoEvent` (`src/inspect_ai/log/_transcript.py:544-551`). The viewer renders these generically.
- `span()` takes no metadata (`src/inspect_ai/util/_span.py:55-58`), so member identity can be carried only in the span name or in separate events.
- Store events are diffs of the whole store taken at span entry and exit (`_span.py:100-101`). With concurrent member spans, one member's `StoreEvent` will include other members' writes *(inferred from the code, not tested)*.

### Bridged agents

- `sandbox_agent_bridge` routes a sandboxed agent's model calls to Inspect models. Each call becomes a `ModelEvent`.
- `bridged_tools` exposes host-side Inspect tools to the sandboxed agent over MCP (`src/inspect_ai/agent/_bridge/sandbox/bridge.py:141-146`). inspect_swe's Claude Code agent accepts `bridged_tools` (`inspect_swe: src/inspect_swe/_claude_code/claude_code.py:125`). So an inspect_swarm messaging tool can be offered to a bridged member.
- Codex Multi-Agent V2 `agent_message` items become author-attributed user messages, with the raw item kept on the content (`src/inspect_ai/agent/_bridge/responses_impl.py:1196-1245`). They appear only inside `ModelEvent` inputs; no event represents an inter-agent message. This is how Codex's own swarm delivers peer text (in the user role, with an author line). inspect_swarm's native members do not, and this design does not change the bridge ([Delivery](#delivery-peer-messages-are-model-output)).
- inspect_swe uses the bridge's `ModelEventSink` (`src/inspect_ai/model/_model.py:2933`) to rebuild Codex subagent spans from `spawn_agent`/`close_agent` calls and `agent_message` items (`inspect_swe: src/inspect_swe/_codex_cli/_events/consumer.py:1-33`, `detection.py:50-86`).
- Claude Code subagents are rebuilt from session files. Agent teams are not represented *(inferred from a search for team, teammate and SendMessage that found nothing)*.

### Monitoring and intervention

- Tool approval already offers `approve`, `modify`, `reject`, `terminate` and `escalate` per tool call (`src/inspect_ai/approval/_approval.py:7-15`).
- [inspect_sentinel](https://github.com/meridianlabs-ai/inspect_sentinel) generalises this into monitors (which score) and protocols (which decide), at the `BeforeToolCall` and `AfterToolCall` stages. Generate stages are designed but not built (`inspect_sentinel: design/sentinel-overview.md`).
  - Its inspect_ai dispatcher is on inspect_ai's `feature/sentinel` branch, not yet on `main` (`inspect_sentinel: design/pr-series.md`, "Decisions").
  - Every step carries a `conversation` id meant to tell agents in one sample apart. Monitor state (`Context.store_as`) is per sample, so it is shared across a swarm's members.
  - Sentinels do not yet run for bridged agents' tool calls (`inspect_sentinel: design/workstreams.md`).

## Design

### The shape

A swarm is four things, and the library keeps them separate. ORBIT reached the same factoring, separating invocation, message routes, activation and conversation ownership.

1. **Members**: who the agents are. Each has a name, a role (data), an `Agent`, its own model, tools and limits, and its own conversation.
2. **Substrate**: how members share information. The shared sandbox filesystem, and optional explicit channels: direct messages, a shared notes/fact log, a task list with claims, a board.
3. **Controller**: how the swarm runs. Topology (leaderless, coordinator tree, lead plus teammates), how members are started and woken, termination, and how the final answer is produced.
4. **Observer**: what is recorded and who may intervene. Evidence events for every communication, per-member accounting, swarm metrics, and the interception point that monitors attach to (and optional red-team features could, if they are ever built).

```
                   ┌─────────────────────── swarm() : Agent ────────────────────────┐
 Task / sample ──▶ │ Controller: topology · start/wake · termination · final answer │
                   │      │ starts                                                  │
                   │  ┌───▼────┐  ┌────────┐  ┌────────┐   members: any Agent       │
                   │  │ m1     │  │ m2     │  │ m3 ... │   (react, deepagent,       │
                   │  │ limits │  │ limits │  │ limits │    bridged Claude/Codex)   │
                   │  └─┬───▲──┘  └─┬───▲──┘  └─┬───▲──┘                            │
                   │    │send│deliver │   │       │   │                             │
                   │  ┌─▼───┴────────▼───┴───────▼───┴──┐  ◀── monitors             │
                   │  │ Bus: one chokepoint for every   │      (sentinel protocols) │
                   │  │ sanctioned communication        │                           │
                   │  └─┬───────────────────────────────┘                           │
                   │    │ messages · notes · tasks · board (store-backed state)     │
                   │  Shared sandbox (default): filesystem as implicit channel      │
                   │  Observer: evidence events · per-member usage · metrics        │
                   │  Budget: swarm-wide limit node; member limit nodes below it    │
                   └────────────────────────────────────────────────────────────────┘
```

### Entry point

`swarm()` returns an `Agent`, so it works anywhere an agent does: as a solver via `as_solver`, under `run()`, or as a member of another swarm (not a goal). Illustratively:

```python
from inspect_swarm import swarm, member

agent = swarm(
    members=member(deepagent(...), count=4),  # or a list of named, heterogeneous members
    topology="leaderless",                    # later: "coordinator", "lead"
    channels=["filesystem"],                  # later: "messages", "notes", "tasks", "board"
    budget=cost_limit(40.0),                  # swarm-wide; reserve kept for the final answer
    final="verify",                           # or "vote", "first", "synthesize", "reporter"
)
```

All of these are plain values, so a task can expose them as `-T` parameters and an eval set can sweep them. A swarm of one member with no channels is a single agent with the same accounting, which makes it the natural baseline arm.

### Members

- A member is a record: name, role, `Agent`, model, tools, limits, status (`running`, `idle`, `done`, `errored`, `cancelled`) and its conversation.
- Each member runs under `run()` in its own agent span named after it, with its limits as a child of the swarm's budget node.
- Members may be native agents or bridged agents.
  - A bridged member reaches the substrate's tools through `bridged_tools`.
  - It reaches the shared filesystem through the sandbox it already has.
- **Ownership boundary.** The swarm owns everything a member starts, including its descendants. Draining a member means cancelling and awaiting all of that work before the swarm finalises, records usage or returns. Otherwise:
  - a descendant could keep editing shared files while the final answer is produced;
  - it could add usage after the ledger is taken;
  - or it could raise a limit error through the sample's task group instead of through the controller.

  Members fall into three groups:
  - **Supported from M1.** Members whose work stays inside their invocation: `react()`, `deepagent()` without background dispatch (synchronous subagents are awaited), and bridged agents whose agent process the member awaits.
  - **Not supported in M1.** Members that use `background()`, which today attaches work to the sample's task group with no owner the swarm can drain. This includes `deepagent(background=True)` and any custom agent that calls `background()`. The swarm rejects `deepagent(background=True)` members where it can detect them, and the restriction is documented for custom agents.
  - **Lifting the restriction.** It needs a change in behaviour, not just access to private names: a scoped owner that `background()` attaches to when one encloses the caller (defaulting to the sample's task group, as now). The swarm would then own each member's descendants. This is an inspect_ai change, proposed when a swarm of background deepagents is needed, not in M1.
- **Sandbox processes.** Processes a member starts in the sandbox that outlive its tool calls (for example under `nohup`) are outside any Inspect task group. They are part of the [filesystem observation limit](#security), not something the drain can promise to stop.
- **The deepagent baseline arm** keeps its existing semantics. Children are abandoned when the parent returns and cancelled when the solver ends, and their spend up to then is part of the arm's realized cost.
- **Persistent members** (idle after submitting, woken by a message) are needed by coordinator topologies, not by the first slice. A blocking `on_continue` alone cannot provide them, because a successful submit leaves `react()` before `on_continue` runs. Two candidates for the coordinator-topologies work:
  - **An altered submission protocol.** Run the member with `submit=False` and give it a swarm `report` tool that records an answer without ending the loop. Its `on_continue` then waits on the inbox when the model stops calling tools, and returns when woken. No inspect_ai change is needed, but the member's prompt and stop condition differ from a standard `react()` agent.
  - **Clean re-entry.** Re-enter `react()` on an existing state without inserting a second system prompt. This needs an inspect_ai change.

### Substrate

| Channel | What it is | When | What it adds over the filesystem convention |
|---|---|---|---|
| Filesystem | The shared sandbox ([Sandbox topology](#sandbox-topology)). Members read and write files; a convention (a shared directory, an append-only `notes.md`, per-member scratch directories) is given in the prompt. | M1 | (Baseline.) Free; the channel Test-Time Communication, the C compiler and the Hugging Face incident all used. Observable only through tool calls. |
| Messages | `send_message(to, text)`, with wake or no-wake, `read_messages()` and `list_members()`. The recipient is notified at its next turn boundary and reads the content with `read_messages()`. | M2 | Addressed, attributable messages that wake the recipient; every message monitored and evidenced. The Codex v2 and Claude Code teams surface. |
| Notes / fact log | Append-only typed entries (`fact`, `fail`, `done`), read with a tool; members are notified of new entries. | Later, on request | Typed entries with a notice when they change, instead of a file members must remember to read; each entry monitored and evidenced. DeLM-style shared context. Unlike DeLM, entries are not injected into the prompt. |
| Task list | Create, claim (first claim wins, optional lease), update, list, with dependencies. | Later, on request | Atomic claims and leases that cannot race the way lock files do; dependencies; a state the controller can read for quiescence. Claude Code teams and DeLM. |
| Board | Channels, threads, posts, and subscriptions with metadata-only notifications. | Later, on request | Many-to-many discussion with subscriptions and notifications instead of polling files; posts monitored and evidenced. Codex's message board. |

Every explicit channel is a set of `@tool`s plus state. The tools do not deliver anything themselves; they hand a typed communication record (sender, recipients, kind, payload) to the bus.

Shared state gets explicit concurrency rules, because agents hold claims and edits for minutes:
- notes are append-only;
- anything mutable is updated with compare-and-swap, failing if the content changed, as Letta's shared memory blocks distinguish;
- claims are TTL leases that report conflicts rather than block, like mcp_agent_mail's file reservations.

The [CoAgent paper](https://arxiv.org/abs/2606.15376) argues that blocking locks stall long inferences and plain optimistic concurrency throws away minutes of work.

### Delivery: peer messages are model output

A peer's text is another model's output, so it reaches a member's model only as **tool output**: the result of a swarm tool the member calls (`read_messages()`, a notes read, a thread read). It is never a `ChatMessageUser` or `UserMessage` turn, and never carries `source` `input` or `operator` (decision: Ransom, 2026-10-07). A header saying "this is not a user instruction" would be a prompt-level mitigation; tool output is structural, and it matches the trust models in [Security](#security).

Delivery has two modes, an axis because they change behaviour:

- `poll`: no notice. The member learns of messages only by calling the read tools.
- `notify` (default): at the member's next turn boundary, the swarm injects a **notice** that carries only metadata. The member then reads the content with a tool.

ORBIT's third mode, which injects full bodies (`auto`), is deliberately not offered.

The notice is the one swarm-authored text that enters a member's context outside a tool result.
- It is a fixed template, in the manner of deepagent's background-completion notice: "3 unread messages from `worker-2`, `worker-4`; call `read_messages()`".
- Its only variables are counts, channel kinds, and names the eval author or controller assigned: member names from the roster, and board channels the task defined.
- It never includes a message body, a subject line, a thread title, or a name a member chose. A board channel or thread a member created is referred to by a swarm-assigned id.

So the notice does not let peer text into the user role. How it is rendered, and how wake works for bridged members (which have no `react()` channel; they may get `poll` only), is part of M2's design.

Tool output is a better place than the user role, but it is not a trust boundary by itself. ADK's own fencing module calls peers' turns and tool results "attacker-reachable" ([`_fencing.py`](https://github.com/google/adk-python/blob/main/src/google/adk/flows/llm_flows/context/_fencing.py)). So the read tools also follow three rules taken from the frameworks surveyed:
- **Fence each message as data.** Each message sits between begin and end markers under a sender line the bus stamps. Copies of the markers inside the payload are removed, so a payload cannot close its own block. A fixed note says the content is another agent's message to read, not instructions to follow.
- **Relay text, not mechanics.** A message is what the sender wrote, never the sender's tool calls, tool results or transcript. Microsoft Agent Framework and the OpenAI Agents SDK's `remove_all_tools` filter make the same choice.
- **Bind sender identity at the bus.** Members never assert who they are. METR's investigation of the Hugging Face incident found agents impersonating one another. Terrarium's attacks inject system messages through its communication proxy.

Provenance is distinct in logs and evidence as well as in the prompt:
- Peer content appears as the result of a named swarm tool, linked to its `sent`, `delivered` and `read` evidence records.
- The notice is a distinct swarm item, either a dedicated channel item or a message carrying a swarm metadata marker, never an operator message.

The two ways to mark the notice:
- **A new `source` value**, which has the reader-compatibility cost described in [Compatibility](#compatibility-and-migration).
- **Message `metadata`**, which does not.

M2's design should lean towards metadata and a dedicated item, and add a `source` value only if the viewer must distinguish swarm notices without reading metadata. Whether either needs an inspect_ai change, given that private internals are allowed, is for M2's design.

Vendor swarms differ. Codex's multi-agent v2 delivers peer text as author-attributed user messages, and the bridge reproduces that ([Bridged agents](#bridged-agents)). That is the vendor's design, evaluated as shipped; native members never receive peer text that way.

### Sandbox topology

Which sandbox each member runs in is a decision, not an accident of the scaffold.

**Default: all members share the sample's sandbox** (decision: Ransom, 2026-10-07).
- The shared filesystem is M1's only channel, so members must see the same files.
- Bridged members already run in the sample's sandbox.
- It is also what the cited leaderless systems did: the C compiler's shared repository, Test-Time Communication's shared container.

**Other topologies are optional and deferred to later work, if users need them.**
- **A sandbox per member**, through Inspect's multiple named sandbox environments per sample (`sandbox(name)`, `src/inspect_ai/util/_sandbox/context.py:48`).
- **Partial isolation**:
  - private sandboxes plus one shared volume or service;
  - or one shared sandbox with a Linux user and write scope per member, as SCHEME does ("All agents in a given sample share a single Docker sandbox", with per-agent file scopes).

Cases that might need them, none committed:
- isolation comparable to the k-epochs arm, so that a "no communication" swarm matches independent attempts in its environment as well as its prompts;
- shared filesystem against none as a sweep axis;
- containment baselines for the unsanctioned-channel question.

**Concurrent work in one shared sandbox.** The task's sandbox layout and the member prompt own this, and the swarm supplies each member's name and directories.
- **Conflicting edits to one repository.** Members edit the same files, or one member's broken build fails another's tests. The usual mitigations are conventions the swarm can set up but not enforce:
  - per-member git worktrees or scratch directories, with a shared integration branch;
  - per-member build directories;
  - the C compiler's pattern of claiming work before touching it.
  Whether members share one working tree is a task choice, and it changes what is measured.
- **Git index locks.** Concurrent `git` commands in one working tree contend for `.git/index.lock` and fail intermittently. Per-member worktrees avoid this; a shared tree needs members prompted to retry.
- **Resource contention.** Several bridged agents (each a CLI process plus its bridge) in one container compete for CPU, memory and file handles, and may contend for ports. The sandbox must be sized for the member count. Whether several bridged agents can share one container cleanly is checked when bridged members are first used.
- **Long-running processes** a member leaves behind are outside the drain ([Members](#members)), and are part of the filesystem observation limit ([Security](#security)).

### The bus: one interception point

All sanctioned communication goes through one function, `deliver(record)`, which runs these steps in order:

1. **Policy and monitor.** Attached protocols decide `continue`, `modify` (rewrite the payload, e.g. paraphrase), `reject` (the sender gets a tool error it can react to) or `terminate`.
2. **Storm controls.** Per-sender rate, deduplication, inbox cap and size cap. Claude Code ships these for cross-session messaging so that a message loop "stops on its own".
   - A full inbox is returned to the sender as a tool error, which is back-pressure it can react to, as AG2's arbiter does with `InboxFull` ([AG2 Network](https://github.com/ag2ai/ag2)).
   - Loop detection beyond rate limits is a later option, from two candidates:
     - a progress-aware stall counter like Magentic-One's, which rises on no progress or a detected loop, decays on progress, and acts at a threshold ([AutoGen](https://github.com/microsoft/autogen));
     - a unique-senders-in-window rule like Strands' repetitive-handoff detection.
3. **Evidence.** One event per record, with the verdict and, when the payload was rewritten, the original. Each record also carries a causation id (the record it replies to), as AG2's hub envelopes do, so message → reply → effect chains can be followed.
4. **Delivery** to the recipients' inboxes, waking them if requested.

Nothing else writes to a member's inbox, so no sanctioned path between members bypasses the monitor or the transcript.

**Red-team features are optional and unscheduled** (decision: Ransom, 2026-10-07). These are records with a forged or hidden sender, secret channels, and injection into a specific member. They may never be built and no milestone commits to them. The single interception point keeps them possible: they would enter as records at step 1, under the safety constraints in [Security](#security).

Monitoring aligns with inspect_sentinel; there is no separate `SwarmMonitor` type (decision: Ransom, 2026-10-07). The reasons:

- A send is a tool call, so once sentinel's dispatcher lands, every send is already a `BeforeToolCall` step with the sender's `conversation` id. A sentinel protocol can then reject or modify it with no swarm-specific code.
- Monitor state is per sample, so a protocol can keep a joint, swarm-wide view. This is what the fragmentation result asks for.
- Unsanctioned channels are tool calls too (a `bash` that writes into a shared cache). The same monitors see them.
- Monitors need actions as well as messages.
  - [SCHEME](https://arxiv.org/abs/2605.29178) found that monitors with code access flagged 68–99% of coordinated sabotage (depending on the attacker model, with one monitor model) at a 2% false-positive rate.
  - [NARCBench](https://arxiv.org/abs/2604.01151)'s message-only collusion monitors scored below chance in the inspect_evals registry's report (AUROC 0.10–0.17), reading honest convergence as suspicious.
  - A bus-only monitor would be the weak design. Sentinel's tool stages, plus the bus's records, give monitors both views.
- Post-hoc replay of new monitors over existing logs, as ControlArena's `evaluate_monitors_post_hoc` does, comes from sentinel's Scout replay. A sentinel monitor is the same monitor live and replayed.

What sentinel does not cover is the delivery side: what each recipient was shown, after rewriting, notification and coalescing. The bus records that itself. If monitors turn out to need to act on deliveries rather than sends, it can become a sentinel stage later.

The bus uses sentinel's action vocabulary from the start and adopts sentinel protocols when sentinel's dispatcher reaches inspect_ai `main`. Sentinel is under active development and may well be ready by M2. If it is, M2 uses it directly. If not, M2 carries the smallest protocol-shaped hook with sentinel's action names, to be replaced by sentinel, and nothing more.

### Observer: evidence, accounting and metrics

**Evidence.** Each communication produces records of the kinds ORBIT uses: `sent`, `delivered`, `read` and `exposed`, the last meaning it entered a model's input. Each record carries:

- sender and recipients (member names);
- channel and kind;
- payload, or a reference to it;
- the monitor verdict and the original payload when rewritten;
- the ids that tie it to the rest of the log: the sender's span id, its tool call id and the Inspect event id.

M1 writes these as `InfoEvent`s with `source="inspect_swarm"` and a versioned payload, inside the member spans. That is the working default for M1, not a settled format. Whether and when to propose a first-class event type in inspect_ai is [open question 1](#open-questions). Two things weigh against doing it early:

- it is a schema, ts-mono and viewer change;
- bridged swarms are where native and Codex `agent_message` traffic should share one type, and its shape will be clearer once they are built.

**Accounting.** Two separate things:

- **A stopping rule.** The swarm opens one cap node (a `cost_limit` or `token_limit`) around its members, and each member runs with its own limits below it. The spike above shows these nodes see every member's usage with no inspect_ai change.
- **A ledger.** Realized usage and cost, taken after the drain so it includes descendants, calls that completed after the cap was hit, and the final-answer step. The ledger, not the cap, is what analysis compares across arms. It is not taken from limit nodes, because token and cost nodes can disagree (see [Limits and cost](#limits-and-cost)).

The ledger states its coverage and how each kind of call is charged, because no single Inspect record covers everything.

- **Totals per arm come from Inspect's sample usage** (`model_usage` and `role_usage`, with cost on each `ModelUsage`). This is what Inspect's limits are charged from (`record_and_check_model_usage`, `src/inspect_ai/model/_model.py:3045-3095`). It counts newly incurred usage, excludes response-cache replays, and includes native provider compaction. Every arm gets it the same way, so arm-to-arm comparisons use it.
  - It also includes scorer model calls, and per-member scoring makes more of them in the swarm arm. So arm totals use a snapshot of sample usage taken when the swarm returns, which every arm records because baselines run as a swarm of one ([swarm-scoring.md](swarm-scoring.md#cost-comparisons-exclude-scoring)).
- **Attribution per member comes from the model events in each member's span**, with these rules:
  - **Cache replays.** An event with `cache="read"` (`src/inspect_ai/event/_model.py:135`) replays the original call's output and usage, but costs nothing new (`_model.py:1537-1564`). It is counted as a replay and charged zero, not summed. A naive sum would double-count it (the round-2 review: 50 tokens incurred, 100 summed).
  - **Native compaction.** `Model.compact()` records usage with no model event (`_model.py:1349-1361`), and a `CompactionEvent` carries no billable usage. `deepagent()` defaults to automatic compaction, which tries native compaction first. In M1 this usage appears as the difference between the sample total and the member-attributed sum, reported as *unattributed*, not hidden. Attributing it needs an inspect_ai change, for example usage on the compaction event.
  - **Cancelled calls.** A generation cancelled in flight (by the drain, or by a sibling's limit error) leaves an error event with no usage (`_model.py:1658-1674`), and the provider may still have charged for it. The ledger counts such calls as *unknown*, not zero. This applies to Inspect's sample totals too.
  - **Unpriced models.** Usage with no cost is reported as cost *unknown*.
- **Claims.** A total is reported as complete realized expenditure only when there are no unknown entries and the unattributed remainder is zero or explained. Otherwise it is reported as a lower bound, with the number of unknown calls. This corrects the measurement contract only; reconciling against provider billing is out of scope.

The reserve and exhaustion policy, and its limits:

- **Reserve.** The swarm cap sits below the sample's limit by a reserve meant for the final-answer step. Overshoot from calls in flight can consume it, and the number of those calls depends on how much each member and its descendants run concurrently, not on the member count. So the reserve is best-effort.
  - It becomes a guarantee only when the eval bounds both the number of concurrent calls across every member's tree and the size of each call. For example: plain `react()` members whose tools make no model calls, with output limits set, need headroom of at least the reserve plus one maximum-size call per member.
  - A member that fans out raises that bound by its own fan-out; a synchronous deepagent is an example.
  - Without such bounds, nothing about the member count makes finalisation certain.
- **Exhaustion.** The swarm handles only its own cap. On a `LimitExceededError` whose `source` is the swarm's own node, arriving bare or inside an `ExceptionGroup`, it cancels and drains the remaining members, then finalises. Everything else propagates unchanged:
  - a sample-level limit;
  - a member's own limit already caught by `run()`;
  - `TerminateSampleError`;
  - any other error.
  A group that mixes the swarm's own limit error with anything else propagates too.
- **Provisional answer.** If an outer limit ends the sample first, there is no final step. The controller therefore keeps a provisional answer current as members submit: the final-answer chain, without `synthesize`, applied to the submissions so far ([swarm-scoring.md](swarm-scoring.md#the-final-answer-chain)). `as_solver()` copies the agent's output to the task state even when an exception ends the agent (`src/inspect_ai/agent/_as_solver.py:65-80`), so it survives, provided the controller sets it on the `AgentState` object it was passed. A provisional answer exists only once something has been submitted, or verified under a strict `final="verify"`. If the sample ends before that, the output is empty and the sample's metadata records that the swarm produced no answer and why. Nothing is invented.
- **Roles.** Per-member usage is also mapped to model roles where members use different models, so `role_usage` in the log stays meaningful.
- **Communication volume, as a diagnostic.** Realized totals stay whole-call usage. Inspect reports usage for a whole generation (`src/inspect_ai/core/_model_output.py:13-40`), and one generation's input mixes task instructions, several peers' earlier tool results and cached prefixes, so no measured charge belongs to any one message. Communication is therefore reported separately, and never as a partition of the bill:
  - **Measured:** counts and sizes of messages, notes and posts sent and read, per member and per sender, plus the usage of generations whose tool calls were swarm communication tools.
  - **Estimated:** any per-message or per-sender share of tokens or cost (for example, a message's tokenised length times the price of each generation it stayed in context). It is labelled an estimate, and its attribution convention is left to the milestone that builds it.

  This matters because in SCHEME, reading and sending messages took 56–60% of all tokens, and topology alone changed cost by 50–60%. A member that floods others is visible in the measured volumes.

**Metrics** recorded per sample, for scorers and analysis:

- per-member and total tokens and cost;
- wall clock;
- critical-path steps. Kimi's CriticalSteps sums, over stages, the coordinator's steps plus the slowest subagent's. That formula assumes a staged coordinator; a leaderless, asynchronous swarm needs its own definition, for example the longest chain of member steps linked by reads of another member's writes. Settling it is part of M1's metrics work, and latency curves are not interpreted before then;
- idle time per member;
- time each member spends waiting for a model connection, so that a 16-member swarm throttled by `max_connections` is not mistaken for a slow one. Inspect accounts waiting time per sample, counting overlapping waits once (`src/inspect_ai/_util/working.py:95-115`), so per-member waiting needs its own timing;
- messages, notes, claims and posts per member, and blocked or rewritten communications.

**Member views.** inspect_petri runs its auditor and target concurrently in one sample, and gives each role its own named timeline with `transcript().add_timeline()` (`src/inspect_ai/log/_transcript.py:605`; [inspect_petri](https://github.com/meridianlabs-ai/inspect_petri) `_auditor/auditor.py`). The swarm does the same per member, so the viewer can show one member's trajectory without a new event type.

**Analysis.**

- Thin helpers that turn a set of logs from the four arms into comparisons on realized cost, and λ fits.
- Harness-validity checks scored separately from the task, following Petri, which scores `auditor_failure` and `stuck_in_loops` and tells users to check them "before trusting the target-behavior scores". For a swarm: a member that never acted, members stuck in loops, deadlock or quiescence never reached, and message storms.
- Inspect Scout scanners that label coordination failures (MAST categories, duplicated work, message storms) over swarm transcripts.

### Results and scoring: a task-owned contract

Inspect scorers take a `TaskState` and a target, and many inspect or modify the sandbox (`src/inspect_ai/scorer/_scorer.py:35-39`). What counts as "a member's result" therefore belongs to the task, not to the swarm. The task declares one of two kinds.

**Answer-scored tasks.** The scorer reads a final answer, for example a FrontierMath-style value or a BrowseComp answer.

- Each member's submission is a stable artifact, so the task's own scorer can score it on a copy of the state carrying that answer.
- This gives per-member correctness and team@k, which compare directly with best@k and pass@k from epochs. team@k is the best member's result, Test-Time Communication's "any of the k agents"; it selects with the target, as best@k does. The swarm's selected answer (final@k) compares instead with the same final-answer rule applied to k epochs (select@k). [swarm-scoring.md](swarm-scoring.md#comparison-quantities) defines each quantity.
- Voting additionally needs a task-defined comparable form: a normaliser or equivalence rule, such as the match rules a scorer already applies (`src/inspect_ai/scorer/_match.py:9-45`).
- Free-form proofs or code have no such form, so `vote` is unavailable for them.

**Shared-artifact tasks.** The scorer examines the environment.

- Examples: SWE-bench runs tests against the shared repository (`inspect_evals: src/inspect_evals/swe_bench/scorers.py:32-78`). Frontier-CS recovers a solution from the sandbox when the output text is empty (`inspect_evals: src/inspect_evals/frontier_cs/scorer.py:631-676`).
- A swarm on such a task has one team result: the state of the shared environment when the swarm ends.
- Re-scoring each member's text against that one environment would credit the team's artifact to every member, so individual correctness is reported as unavailable.
- Per-member correctness can be claimed only if the task gives each member a stable artifact of its own (its own worktree, branch or output directory) and a way to score it.
- The comparison with epochs is then team result against best@k or pass@k at equal realized cost. Test-Time Communication draws the same distinction: it compares best-of-member results with a single shared container's result on Terminal-Bench ([Appendix A.6](https://arxiv.org/html/2609.21032v1)).

**What M1 does.** [swarm-scoring.md](swarm-scoring.md) details the contract, the record, the scorers and the comparisons. M1's per-member, team@k and voting helpers support answer-scored tasks only. For shared-artifact tasks, M1 reports the team environment score with realized cost. It does not claim that recording member strings makes every coding benchmark comparable to epochs.

### Controller: topology, termination, final answer

**Topologies.** Configuration over one runtime, not separate agents:

| Topology | Started by | Typical channels | Ends when |
|---|---|---|---|
| Leaderless (M1) | The controller starts all members with the same task | filesystem; later notes and tasks | all members submitted, the budget or time is exhausted, or (optionally) the first answer passes a task-supplied verifier |
| Coordinator tree (later) | A root member spawns named children, which may spawn their own | messages; later tasks | the root submits |
| Lead and teammates (later) | A lead with a fixed roster of teammates | messages, tasks | the lead submits |

A deepagent with `background=True` is not reimplemented. It stays a member type and a baseline arm.

**Termination.** Always bounded by the swarm budget and the sample's limits.

- Leaderless swarms have no single submitter. In M1 they end when every member has submitted, or on budget, time or a verified answer. With explicit channels they would end by quiescence: every member idle, no open or claimed task, and empty inboxes. Quiescence needs members that can be idle and woken, so it depends on the persistent-members work ([Members](#members)).
- Every run records why it stopped, as one of a fixed set of reasons: all submitted, verified answer, swarm cap, sample limit, time, quiescence. Strands and AutoGen report stop reasons the same way, and AutoGen's termination conditions compose with `&` and `|`. Later topologies may compose conditions the same way.
- Quiescence, when explicit channels exist, can follow SCHEME's pattern: a shared status registry with `wait` and `done`, where a `wait` returns immediately once every other member is done. AG2's passive `max_silence` expectations are a related signal.
- On termination the controller cancels and awaits the remaining members and their descendants (drain, within the [ownership boundary](#members)). It does not abandon them, so nothing edits the shared environment during finalisation and their usage is in the ledger before the sample is scored.

**Final answer.** For shared-artifact tasks, the final result is the environment as the drained swarm leaves it. A later topology could add an integration member that runs last. For answer-scored tasks, `final=` selects one of:

- `reporter`: the coordinator's or a designated member's submission.
- `vote`: plurality over member submissions, using the task's comparable answer form. Unavailable when the task has none.
- `first`: the first submission.
- `verify`: the first or best submission that a task-supplied verifier tool accepts. The verifier must be the task's in-loop checker, never the scorer's target.
- `synthesize`: one extra model call over the member submissions and shared notes.

The defaults:

- coordinator topologies: `reporter`;
- leaderless swarms: `verify` when the task supplies a verifier, otherwise `vote` when the task defines a comparable answer form, otherwise `first` (decision: Ransom, 2026-10-07).

The default is a chain that falls through: when nothing verifies the swarm votes, and when nothing can vote it takes the first submission. A single mode, such as `final="verify"`, is strict. [swarm-scoring.md](swarm-scoring.md#the-final-answer-chain) specifies each mode, `synthesize` and the verifier.

The leaderless default matches how leaderless swarms succeed in practice: the C compiler's oracle, Test-Time Communication's dense verifier. Voting means nothing without a comparable answer form. Every member's own submission is recorded regardless, so the effect of the final step can be measured separately.

### Where each part lives

| Part | Lives in | Why |
|---|---|---|
| `swarm()`, controller, members, budget, bus, channels, evidence, metrics, scorers, prompts, Scout scanners | inspect_swarm | Fast iteration; the runtime is opinionated and experimental. |
| Notifying members and delivering peer messages | inspect_swarm, on inspect_ai's private channel internals where needed (M2) | Content is tool output from swarm tools, which needs nothing from inspect_ai. The metadata-only notice reaches a member at a turn boundary through its channel, or an `on_continue`-style injection as deepagent does. The open parts are: the [binder](#the-agent-channel), how the bus obtains each member's ref; and whether the notice is a dedicated channel item, which `react()` would need to render (an inspect_ai behaviour change), or a marked message. M2's design decides both, alongside ACP's first-binder-wins rule. Public exports are a later clean-up, not a prerequisite. |
| Clean re-entry of `react()` on an existing state (no second system prompt), or an idle state | inspect_ai (coordinator topologies), optional | Persistent members; the alternative is a `submit=False` member protocol inside inspect_swarm. |
| A scoped owner for `background()` work, so a swarm can own and drain a member's descendants | inspect_ai, when background deepagent members are needed | Today `background()` attaches to the sample's task group; without an owner the swarm cannot drain `deepagent(background=True)` members. |
| `span(..., metadata=)` and per-span usage in the log, including native compaction usage (for example on `CompactionEvent`) | inspect_ai, nice to have | Member identity and complete per-member cost without side records or an unattributed remainder. |
| Correct `StoreEvent` attribution under concurrent spans | inspect_ai, bug-class | Affects any concurrent agents, not only swarms. |
| A first-class inter-agent message event, if one is wanted | inspect_ai, [open question 1](#open-questions) | Shared by native swarms and bridged Codex `agent_message` traffic; a schema change. |
| Turning on and mapping Codex multi-agent v2 and Claude Code agent teams | inspect_swe | Agent-specific configuration and event reconstruction already live there. |
| Monitors and protocols over communications | inspect_sentinel | One monitoring abstraction across single- and multi-agent evals. |
| Scenarios, attacks and defenses | ORBIT-like packages on top | Not core; inspect_swarm provides roles and the bus hook. Injection is an optional, unscheduled feature. |

### Bridged swarms

Bridged agents enter in two ways.

1. **Vendor swarm as one member.** Codex with multi-agent v2, or Claude Code with agent teams, runs in the sandbox as a single Inspect agent and coordinates internally. inspect_swarm does not control it. It provides:
   - the same budget and accounting, so a vendor swarm can be an arm in the matched-cost comparison;
   - evidence records mapped from the vendor's traffic. inspect_swe already sees Codex's `agent_message` items and spawn and close calls; Claude Code teams' mailboxes and task files are in the sandbox.
2. **Bridged agents as native members.** Several Claude Code or Codex instances are members of an inspect_swarm swarm, coordinating through its channels via `bridged_tools`. This gives heterogeneous swarms and the same bus-level interception for vendor agents.

Mapping 1 is the more valuable for "evaluate what vendors ship", and it is inspect_swe work. Mapping 2 falls out of the member design.

### Relationship to ORBIT

ORBIT is a mature, Inspect-based security framework (v1.0.3, Apache-2.0, [code](https://github.com/wlanderson0/orbit)). Its core is a single `mas_orchestrator` solver with:

- a roster (`AgentSpec`, with a role label, model, tools and a compromised flag);
- invocation edges kept separate from channels with reader and writer lists;
- delivery modes `poll`, `notify` and `auto`;
- a scheduler with round-robin, superstep and interleaved modes and explicit plans:
  - plan steps can run agents concurrently as an activation batch;
  - a `completion` quantum lets each activation run to completion;
  - a failing activation cancels and joins its siblings ([`agent_scheduler.py:348-377`](https://github.com/wlanderson0/orbit/blob/588b3035f92450ec4f0496bb8b629daa205e84c7/orbit/execution/agent_scheduler.py#L348));
- attack and defense registries, with defenses enforced by wrapping the `Model`;
- evidence events (`sent`, `read`, `notified`, `delivered`, `model_exposure`) written as `InfoEvent`s.

Other points that bear on reuse:

- To get turn-level control, it forks `react()` (as `turn_react`) and imports private inspect_ai modules. Private imports are acceptable for an inspect_* package. The fork is the concern: it diverges from `react()` instead of tracking it.
- It accepts `inspect-ai>=0.3.263,<0.4`, with a lockfile baseline it is tested against.
- Its concurrency uses raw asyncio rather than anyio.

Its concurrency is scheduled activation: the scheduler decides which agents run in each step, and messages do not activate agents (its docs: "Messages do not activate agents"). inspect_swarm's members are continuously active, wake on messages, and are joined only at termination.

The recommendation is to **align with ORBIT and aim to be a substrate it could run on, not to absorb it**:

- **Reuse its vocabulary** where it fits: roster and roles as data, channels with reader and writer lists, delivery modes, evidence kinds, output member.
- **Provide what it had to build itself**, without forking `react()`:
  - turn boundaries and a scheduler;
  - per-member tool and model wrapping;
  - a communication hook that does not wrap `Model` (which breaks response caching).
- **Leave scenarios, attack and defense registries and security scorers to ORBIT.**

ORBIT's scheduled activation (rounds, plans with concurrent batches, and quanta) is a useful second execution mode for reproducible security experiments. inspect_swarm's default is continuously active members woken by messages, which is what vendor swarms do. Supporting a scheduled mode is later, optional work, not part of M1 or M2.

### Related projects and what they teach

*Are there other eval-oriented swarm substrates besides ORBIT?*
- **On Inspect, one study harness.** [Architecture Matters for Multi-Agent Security](https://github.com/benhagag10/Architecture-Matters-for-Multi-Agent-Security) (code for the ICML 2026 paper of that name) runs four-peer meshes on Inspect. Among the projects surveyed here, it is the only other Inspect peer swarm, and it is a scheduled study harness, not a reusable runtime of continuously active members. Two more Inspect projects set conventions to align with: inspect_petri for concurrent agents in one sample, and ControlArena for monitors.
- **Outside Inspect, several harnesses** are worth learning from, but not aligning with: SCHEME, Terrarium, Concordia.
- **Production frameworks** teach delivery, storm-control and termination details, but none is an eval substrate.

All sources below were opened on 2026-10-07 unless marked *unverified*.

**Inspect-based.**

| Project | What it is | What it teaches inspect_swarm |
|---|---|---|
| [inspect_petri](https://github.com/meridianlabs-ai/inspect_petri) (Meridian) | Auditor, target and judge. A Solver runs two Agents concurrently in an anyio task group, each in its own span, connected by a channel, with models assigned by role. | The conventions for concurrent agents in one sample: model roles, per-role named timelines ([Member views](#observer-evidence-accounting-and-metrics)), and harness-validity scores kept separate from the task score. It is two-party, not a swarm. |
| [ControlArena](https://github.com/UKGovernmentBEIS/control-arena) (UK AISI, Redwood) | Policies, monitors and micro-protocols as serialisable Inspect Agents. Monitor verdicts go in message metadata, and `evaluate_monitors_post_hoc` replays monitors over logs. Its settings are all single-agent. | Monitors that attach per step and can be replayed post hoc. inspect_swarm gets both through inspect_sentinel, and should accept monitors of that shape. |
| [Architecture Matters for Multi-Agent Security](https://github.com/benhagag10/Architecture-Matters-for-Multi-Agent-Security) (ICML 2026 paper code; also built on Inspect) | Thirteen architectural conditions, including `mesh_round_robin` and `mesh_delegation` with four peers (`conditions.py`, `_mesh_solver`). One solver loop activates one peer at a time: round-robin up to 60 turns, or, in delegation mode, a lead peer that delegates to the others through `as_tool`. Shared history is spliced into each peer's `AgentState`, and peer output is tagged in message metadata. Memory visibility is private, own-reasoning or shared. | Scheduled, one-at-a-time activation, like ORBIT. Peer output reaches the next peer as conversation messages, and delegation results arrive as tool results. It confirms ORBIT's vocabulary (topologies, memory visibility) and that architecture changes attack success. It is not reusable as a substrate for continuously active members. |
| inspect_evals: NARCBench, tau2 | NARCBench: a message-only collusion monitor over committee deliberations ([arXiv 2604.01151](https://arxiv.org/abs/2604.01151)). tau2: a user simulator that turns each side's output into the other's user message. | NARCBench: monitors need actions, not only messages. tau2: the role-flipping that inspect_swarm's delivery rule avoids. |

**Eval harnesses outside Inspect.**

| Project | What it is | What it teaches inspect_swarm |
|---|---|---|
| [SCHEME](https://arxiv.org/abs/2605.29178) (coordinated sabotage and monitoring, 2026-05) | 2–4 agents in one shared Docker sandbox, each a separate Linux user with its own file scopes. Messages go through `read_comms`/`send_comm` tools on harness-stamped append-only channels, with `wait`/`done` against a status registry. Main and side tasks are scored jointly in the sandbox. Monitor views are ablated (code, comms, per agent). No public code found. | The closest published design to M1 and M2: shared sandbox by default, peer messages as tool output, quiescence by status registry, monitors with code access, and the communication share of cost. |
| [Terrarium](https://github.com/umass-aisec/Terrarium) (UMass, [arXiv 2510.14312](https://arxiv.org/abs/2510.14312)) | Append-only blackboards behind one communication proxy (the Megaboard), with a factor graph for topology. Turn-based planning and execution phases. Attacks include agent poisoning, context overflow and proxy-injected system messages. | The proxy is both the observation point and the attack point. This confirms one bus, and that sender identity must be bound there. |
| [Concordia](https://github.com/google-deepmind/concordia) (Google DeepMind) | A game master resolves actions. Engines are swappable (sequential, simultaneous, asynchronous) and chainable into scenes. Logs are evaluated by LLM autoraters. | Scheduling as a policy over one runtime. inspect_swarm's continuously active default is Concordia's asynchronous engine, and a scheduled mode can be another policy later. |
| AgentsNet ([arXiv 2507.08616](https://arxiv.org/abs/2507.08616)), Sotopia, MASEval ([arXiv 2603.08835](https://arxiv.org/abs/2603.08835)) | Synchronous rounds over graphs up to 100 agents, with every agent asked for an answer; simultaneous, round-robin or random action order; framework-agnostic "system as the unit of evaluation". | Answer-scored results per agent, and comparisons across papers that need scheduled modes. |

**Production frameworks**, judged by how a peer's message reaches the recipient.

| Framework | How a peer's message arrives | What it teaches inspect_swarm |
|---|---|---|
| Google ADK | User role, fenced between quoted-content markers with a data-not-instructions preamble; markers inside the payload are removed. | The fencing inside inspect_swarm's read tools ([Delivery](#delivery-peer-messages-are-model-output)). |
| AG2 Network | A user turn prefixed `[sender]:`. A hub with a write-ahead log stamps envelopes with a causation id. An inline arbiter applies per-agent token buckets, inbox caps with a high-water mark (`InboxFull`) and delegation depth. | Back-pressure as a tool error, and causation ids in evidence ([The bus](#the-bus-one-interception-point)). |
| AutoGen / Magentic-One | User role with `source=sender`. The orchestrator keeps task and progress ledgers, with a stall counter that triggers replanning. Termination conditions compose with `&` and `|`. | Stall-based loop detection and composable termination as later options; record stop reasons. |
| LangGraph, OpenAI Agents SDK, Microsoft Agent Framework | The peer's turns arrive as assistant messages (with a name or author), or as a tool result for agents used as tools. Agent Framework filters tool-control content before relaying. | Relay text, not transcripts; tool results for solicited answers are universal. |
| Strands Swarm, CrewAI, CAMEL, MetaGPT | Templated user input or task prompts. Strands has handoff and iteration caps plus optional repetitive-handoff detection; MetaGPT has a budget (`NoMoneyException`) and an all-idle stop. | Turn caps are the norm and are coarse; the swarm's budget is the primary stop. |
| [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail) | Tool output (`fetch_inbox`), prompted by a rate-limited reminder hook that carries only counts and fixed text (`scripts/hooks/check_inbox.sh`). Advisory file reservations with TTLs that report conflicts. | Direct precedent for tool-output delivery with metadata-only notices, and for leases that report conflicts. |
| [Gas Town](https://github.com/gastownhall/gastown) | Mail bodies are read through `gt mail` (tool output). Its injected reminder lists each message's id, sender and **subject** (`internal/cmd/mail_check.go`, `formatInjectOutput`). Per-role mail budgets. | Bodies through tools, but its notice puts a sender-written subject in context, which inspect_swarm's notice contract excludes. Mail budgets as a norm. |
| Letta | Shared memory blocks: insert is concurrency-safe, replace fails if the text changed, rethink is last-writer-wins. Peer messages arrive as a system-role notice (rendering *unverified*). | Explicit concurrency rules for shared state ([Substrate](#substrate)). |

Kimi K2.6 (up to 300 subagents with "context sharding") and xAI's Grok 4.20 multi-agent mode (4 or 16 agents with a leader that synthesises; internal agent communication undocumented) are orchestrator-and-workers systems, like the vendor swarms above; their internals are *unverified*.

**Delivering unsolicited peer messages as tool output is rare.** Among the projects surveyed, only the coding-agent mail layers and SCHEME do it, besides Codex and Claude Code's own tools; and only mcp_agent_mail also keeps its notices free of peer-written text. No general-purpose framework enforces it at runtime with distinct provenance in logs. So that part of inspect_swarm is new, and the fencing above is what makes it defensible.

## Alternatives considered

**Build `swarm()` inside inspect_ai.** This is the earlier RFC's placement.

- It would make channel items and persistent members easy, but couple an experimental, opinionated runtime to inspect_ai's release and compatibility bar.
- The co-development setup (inspect_swarm tracks inspect_ai `main`) lets us move only the extension points into inspect_ai, as they prove necessary.
- Rejected for now. Revisit if the runtime stabilises and most users want it built in.

**Extend `deepagent()` with peer messaging** (add `send_message` among background children).

- Smallest change for the coordinator-tree case.
- It cannot express leaderless swarms, fixed rosters or heterogeneous members.
- It would load swarm semantics onto deepagent's prompt and tool surface.
- deepagent stays a member type and a baseline arm instead.

**Build the full layered runtime first.** Messaging, task list, fact log, board and monitor, then run experiments.

- The most complete, and every piece has precedent.
- It front-loads months of work before knowing whether swarms show a large effect on the tasks we care about.
- Test-Time Communication got its gains with an append-only log in the shared filesystem.
- Rejected as the first slice. Kept as the target architecture, built piece by piece after M1 and M2 as users ask for it. There is no internal evidence gate (decision: Ransom, 2026-10-07).

**Only a one-off filesystem swarm, no library.** A one-off solver in an experiment repository.

- Fastest to a first number.
- Loses the accounting, evidence and final-answer machinery that make the number trustworthy, and would be rebuilt for the next experiment.
- Partly adopted: M1 is a leaderless filesystem swarm, but built as the first slice of the library.

**Make ORBIT the substrate.**

- It already has topologies, channels, scheduling (including concurrent activation batches) and evidence.
- It is built around scheduled activation, where the scheduler decides who runs and messages do not wake agents. The swarms this design targets have continuously active members that wake each other.
- It forks `react()`, so its members diverge from `react()` instead of tracking it. Its private inspect_ai imports are not a reason on their own, since inspect_swarm may use internals too.
- It schedules with raw asyncio, where this repository runs on both anyio backends.
- Its Python floor (3.11) matches inspect_swarm's, so that is no longer a difference.
- It is security-first, while the first users here are capability-scaling experiments that need realized-cost accounting and the result contract above.
- Rejected as the substrate; aligned with instead.

**Deliver peer messages as conversation turns** (user or assistant role), as most frameworks do (AutoGen, AG2, ADK, Strands, LangGraph, the OpenAI Agents SDK).

- Simplest to build. Every provider renders it, and it is what Codex's own swarm does.
- It puts another model's output in the most trusted role (user), or makes it look like the recipient's own words (assistant). LangGraph's documentation warns that relaying full histories confuses the receiving agent.
- Rejected (decision: Ransom, 2026-10-07) in favour of tool output with fencing and metadata-only notices ([Delivery](#delivery-peer-messages-are-model-output)).

**A scheduler that activates agents in turns, as the default** (ORBIT, Concordia's sequential engine, Terrarium, AgentsNet's synchronous rounds).

- Reproducible, and easier to analyse.
- The vendor swarms being evaluated run their members continuously and wake them by message.
- Kept as an optional later scheduling policy, not the default.

**Monitor through model wrapping** (ORBIT's approach: subclass the `Model` to filter inputs and outputs).

- Sees everything a member reads.
- Breaks response caching, couples to model internals, and cannot tell a peer message from any other input.
- Rejected in favour of a bus chokepoint plus sentinel's tool stages.

**A new event type from day one.**

- Clean, and gives the viewer a real view.
- A public schema contract before we know the right shape, and one that must also fit bridged traffic.
- Not chosen for M1, which uses `InfoEvent`s with a versioned payload, as ORBIT does. Whether to propose one later is [open question 1](#open-questions).

## Compatibility and migration

- **inspect_swarm** has no released API, so nothing it adds is a compatibility concern yet.
  - It supports Python 3.11 and later, above inspect_ai's 3.10 floor (decision: Ransom, 2026-10-07). So the design can use 3.11 features, notably native `ExceptionGroup` and `except*` for recovering the swarm's own cap ([Accounting](#observer-evidence-accounting-and-metrics)).
  - The scaffold still says 3.10 in `AGENTS.md`, `pyproject.toml` (`requires-python`, pyright's `pythonVersion`), `CONTRIBUTING.md` and the CI matrix in `.github/workflows/build.yaml`. These move to 3.11 in a separate scaffold PR before M1's code, not on this design branch, which may change only `design/`.
  - It must run on both anyio backends, which rules out raw `asyncio` primitives in the runtime.
- **Eval logs.** M1 writes only existing event types (spans, tool and model events, `InfoEvent`s) plus store and metadata entries. Old viewers and readers see a swarm log as an ordinary log with concurrent agent spans. The `InfoEvent` payload carries a `version` field so later readers can tell formats apart.
- **inspect_ai extension points**, where behaviour needs them, are additive:
  - possibly a binder hook (M2's design decides);
  - extending the `source` literal changes the log schema and the generated TypeScript types, so it goes through inspect_ai's type-generation pipeline and a ts-mono PR;
  - a new `source` value is additive for new writers, but readers whose literal still has only `input`, `generate` and `operator` would reject logs containing it. M2's detailed design must either state the minimum reader version or carry the swarm notice's provenance in message `metadata`. The second avoids version skew and is preferred unless the viewer needs the distinction. Peer content itself needs neither, because it is tool output ([Delivery](#delivery-peer-messages-are-model-output)).
- **A new event type**, if one is proposed ([open question 1](#open-questions)), is the largest change: the event union, schema, ts-mono types, viewer renderer, dataframes and Scout's event handling. M1 uses existing event types; whether and when to add a new event type remains open.
- **inspect_swe** changes are additive options on the Codex and Claude Code agents.
- **Vendor traffic visibility.** Evidence for vendor swarms is limited by what the bridge sees.
  - Codex `agent_message` content can be `encrypted_content`, which is opaque except to OpenAI (`src/inspect_ai/agent/_bridge/responses_impl.py:1196-1245`).
  - inspect_swe's completion detection reads plaintext `input_text` (`inspect_swe: src/inspect_swe/_codex_cli/_events/detection.py:65-86`).
  - The bridged-swarms work measures which evidence fields (`sent`, `read`, `exposed`, payload) are actually observable per vendor, and records unknowns as unknown rather than implying the same coverage as the native bus.

## Security

Untrusted input reaches this code from several directions.

- **Peer messages are model output** and may be adversarial: a compromised member, or ordinary prompt injection relayed from a web page.
  - They reach a member only as tool output, never in the user role (decision: Ransom, 2026-10-07). Notices carry metadata only, with no peer-chosen strings ([Delivery](#delivery-peer-messages-are-model-output)). Claude Code teams take the same stance: an agent message is never user consent.
  - Approval and sentinel decisions are never taken from message content.
  - The `synthesize` final-answer mode is the one place peer text enters a prompt the harness composes. It quotes member submissions as delimited data in a separate model call, not in any member's context. That is a weaker boundary, so `synthesize` is never the default.
- **Message volume.** Storm controls (rate, deduplication, inbox and size caps) bound how much one member can push into others' contexts and into the log.
- **The shared filesystem** is an unsanctioned, unmonitored channel by construction. The design treats it as an observed channel, not a trusted one. The Hugging Face incident shows that agents will also find channels the eval designer did not intend, such as package caches, shared mounts and network services.
  - The sandbox layout for swarm tasks should make sharing explicit: one shared directory, per-member scratch space, no shared caches unless intended.
  - Containment probes are an eval in their own right, not a property the library can promise.
  - Samples must not share sandboxes. Isolation between samples is Inspect's sandbox's job, and nothing here weakens it.
  - **Observation limit.** Tool calls are evidence of possible filesystem communication, not a complete audit. Processes a member starts in the sandbox can read, write or exchange files between logged tool calls, and after the member has ended. Experiments that attribute communication to the filesystem must state this limit, or add independent instrumentation such as filesystem snapshots or audit logs in the sandbox.
- **Red-team features** (forged senders, injected records, secret channels) are optional and may never be built. If they are, they create adversarial content deliberately, so they must be configured only by the eval author, recorded in the evidence events with their true origin, and never reachable from member tools.
- **Log contents.** Evidence payloads are model text. They are written as JSON data in `InfoEvent`s, not as Markdown, so the viewer does not render them as formatted content.
- **Bridged members** run in the sandbox. The swarm's tools reach them through `bridged_tools`, which execute host-side only for calls the model proposed (`bridge.py:141-146`). Today sentinels do not see bridged agents' tool calls, so a swarm of bridged members gets bus-level interception but not tool-level monitoring until that gap is closed.

## Testing

- **Runtime tests** use mockllm with scripted outputs and usage. They need no network and no Docker, and they run on asyncio and trio:
  - members start and run concurrently;
  - the swarm cap and member limits record and stop correctly (the spike above becomes a test);
  - the ledger records realized usage including overshoot from concurrent in-flight calls, finalisation and drained members, and differs from the cap when it should;
  - a synchronous deepagent member fanning out to parallel subagent calls overshoots the cap by its fan-out, and the reserve is reported as best-effort;
  - response-cache replays are counted as replays and charged zero;
  - native compaction usage appears as unattributed and agrees with the sample totals;
  - calls cancelled in flight are counted as unknown, and the total is then reported as a lower bound;
  - zero submissions, and zero verified submissions under a strict `final="verify"`, leave an empty output with the reason recorded;
  - the controller recovers its own cap's exhaustion both as an `ExceptionGroup` and as a lone `LimitExceededError` (which `collect()` unwraps);
  - sample-level limits, `TerminateSampleError`, other errors and mixed groups propagate unchanged;
  - an outer limit that trips before the reserve is used leaves the provisional answer as the output;
  - drain cancels and awaits members;
  - `deepagent(background=True)` members are rejected while background ownership is unsupported;
  - answer-scored tasks get per-member and team@k scores, voting refuses tasks without a comparable answer form, and shared-artifact tasks report a team score with per-member correctness marked unavailable;
  - each final-answer mode selects as specified;
  - every member's submission is recorded;
  - evidence events are written with the right ids, including causation ids;
  - storm controls and monitor verdicts (`continue`, `modify`, `reject`) behave as specified, and a full inbox reaches the sender as a tool error;
  - peer content reaches a recipient only in a read tool's result:
    - fenced, with the bus-stamped sender;
    - with markers inside the payload removed;
    - with no tool calls or results relayed;
    - notices contain no peer-chosen strings (bodies, subjects, thread titles, member-created channel names);
  - every run records its stop reason;
  - communication diagnostics keep measured volumes apart from estimated per-message token or cost shares, and never change realized totals.
- **Filesystem-channel tests** need a sandbox. They use the local sandbox where possible and Docker otherwise, marked slow and skipped in CI without Docker, following inspect_ai's conventions.
- **Bridged tests** (Codex and Claude Code members, vendor swarms) need Docker, inspect_swe and provider keys. They are marked and run by hand or in a scheduled job, never in PR CI.
- **Analysis helpers** (realized-cost comparison, λ fit) are tested on synthetic logs with known answers.

## Implementation plan

The plan fixes only the first two milestones: M1, then M2. After that, the remaining work is a menu, not a sequence (decision: Ransom, 2026-10-07):

- any item can be done in any order, or only partly (for example one structured channel without the others);
- the order is left open to feedback from users, external ones included, as the library is built;
- there is no internal experiment or evidence gate between milestones.

Each milestone is a small series of PRs, and the project convention applies: discuss and review after each one, and do not start the next automatically.

**M0. This design.** Feedback from Ransom.

**Before M1: scaffold to Python 3.11.** A separate `chore:` PR moves `AGENTS.md`, `pyproject.toml`, `CONTRIBUTING.md` and the CI matrix from 3.10 to 3.11 ([Compatibility](#compatibility-and-migration)).

**M1. Leaderless filesystem swarm with full accounting** (inspect_swarm only; no inspect_ai changes).
- `swarm()` with leaderless topology over members whose work stays inside their invocation (`react()`, synchronous `deepagent()`, bridged agents). Background deepagent members are rejected.
- The shared-sandbox default and its filesystem prompt convention: shared directory, append-only notes file, per-member scratch directories or worktrees ([Sandbox topology](#sandbox-topology)).
- A swarm cap node, member limits, a final-answer reserve, and a provisional answer.
- Recovery from its own cap only.
- A realized-cost ledger taken after the drain, with its coverage rules: arm totals from sample usage; member attribution from events, with cache replays charged zero; unattributed native compaction; cancelled and unpriced calls reported as unknown.
- Drain on termination.
- Final-answer modes `vote`, `first`, `verify` and `synthesize`, with the decided leaderless default.
- Per-member submissions recorded.
- Evidence and metrics written to `InfoEvent`s, store and metadata; a named timeline per member; a recorded stop reason.
- Harness-validity checks (a member that never acted, loops, deadlock) reported separately from the task score.
- A definition of critical path for leaderless swarms.
- The task result contract: answer-scored tasks get per-member and team@k scorers; shared-artifact tasks get a team score only.
- Thin analysis helpers for realized-cost comparison and λ fits, for users running scaling experiments.
- Files: `src/inspect_swarm/_swarm.py`, `_member.py`, `_budget.py`, `_final.py`, `_metrics.py`, `_evidence.py`, `scorer/`, `tests/`.

**M2. The bus and direct messages.**
- `deliver()` with storm controls (including back-pressure to the sender) and evidence kinds `sent`, `delivered`, `read` and `exposed`, with causation ids.
- Fenced, text-only read tools, with sender identity bound at the bus ([Delivery](#delivery-peer-messages-are-model-output)).
- Monitoring through inspect_sentinel: its protocols directly if its dispatcher is on inspect_ai `main` by then; otherwise the minimal hook in its action vocabulary ([The bus](#the-bus-one-interception-point)).
- `send_message`, `read_messages` and `list_members`, with delivery modes `poll` and `notify`. Content is delivered as tool output, and notices are metadata-only ([Delivery](#delivery-peer-messages-are-model-output)).
- Notices through inspect_ai's private channel internals, or an `on_continue`-style injection; M2's design picks one.
- M2's design settles the binder: how the bus obtains each member's ref, and whether that needs a small inspect_ai hook for behaviour. Public exports are a later clean-up.

**Later work, in any order or in part.** The real dependencies between items are noted so that whichever is picked first is not blocked unexpectedly.

- **Structured channels.** Notes/fact log, task list with claims and leases, and board with subscriptions; each can be built alone. [Substrate](#substrate) says what each adds over the filesystem convention.
  - Each needs M2's bus.
  - Quiescence-based termination needs the persistent members below.
- **Coordinator topologies and persistent members.**
  - Coordinator tree with path addressing; lead and teammates; idle and wake; `followup_task`-style waking.
  - Either the `submit=False` member protocol or an inspect_ai PR for clean `react()` re-entry ([Members](#members)).
  - Needs M2's messages.
- **Background deepagents as members.** An inspect_ai PR giving `background()` a scoped owner. Independent of the rest.
- **Bridged swarms.**
  - inspect_swe options to enable Codex multi-agent v2 and Claude Code agent teams.
  - Mapping their traffic into the same evidence records, after measuring what each vendor's traffic exposes (encrypted Codex messages, Claude Code mailbox files), with unknown fields kept unknown.
  - Bridged agents as native members via `bridged_tools`, which needs M2 for anything beyond the filesystem.
- **Safety and security hooks.**
  - Roles as data; per-member prompt, model and tool overrides for compromised members.
  - Joint-monitor helpers on sentinel.
  - An optional scheduled (turn-based) execution mode.
- **Optional, unscheduled: red-team features.** Injection with forged or hidden senders, secret channels, and injection into a specific member. They may never be built; they need M2's bus.
- **Per-member artifacts for shared-artifact tasks.** Per-member scores when the task supplies each member's artifact ([swarm-scoring.md](swarm-scoring.md#shared-artifact-tasks-and-per-member-artifacts)). Independent of the rest.
- **Optional, if needed: other sandbox topologies.** A sandbox per member, or partial isolation ([Sandbox topology](#sandbox-topology)).

## Open questions

1. **Transcript representation for swarm evidence** (left open; decision: Ransom, 2026-10-07). M1 writes versioned `InfoEvent`s as its working default.
   - (a) Keep `InfoEvent`s until bridged swarms are built, then propose a first-class event in inspect_ai shared with bridged traffic.
   - (b) Propose the event type alongside M2.
   - (c) Keep `InfoEvent`s indefinitely.

   Recommendation: (a). The shape should be settled with bridged traffic in view, and the schema change is costly.

## Not this design

Adjacent problems noticed and left out, for Ransom to file if wanted:

- **`StoreEvent` attribution under concurrent spans** (inspect_ai). Store diffs are taken at span entry and exit, so with concurrent agents one span's event can include another's writes. This affects deepagent background subagents today.
- **Per-span or per-agent usage in the eval log** (inspect_ai). Today only per-model and per-role usage is stored.
- **`span(..., metadata=)`** (inspect_ai). Would let member identity and roles be stamped on spans instead of carried in names.
- **Sentinels for bridged agents' tool calls** (inspect_sentinel, already a known gap in its workstreams).
- **Claude Code agent teams in inspect_swe.** No representation of teammates, mailboxes or the shared task list today.
- **Checkpoint and resume for long swarms.** Multi-day swarm samples will want inspect_ai's sample checkpointing to cover several members.
- **A swarm view in the log viewer** (member graph, message timeline).
- **Cost limits miss calls that trip a token limit** (inspect_ai). A call that raises at the token-limit check never records its cost into active cost-limit nodes (`src/inspect_ai/model/_model.py:3089-3095`), although its `ModelUsage` keeps the cost. Found by the round-1 review; the ledger above sidesteps it.
- **`max_connections` and swarm size.** A swarm's members share a model's connection pool with every other sample; an eval-level policy (e.g. reserve connections per swarm) may be needed for latency experiments.
