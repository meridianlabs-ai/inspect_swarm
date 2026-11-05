# Inspect Swarm: the topics channel

## Overview

With `channels=[topics([...])]`, the eval author declares named rooms, **topics**, each with the members that read it, the members that write it, whether it is private and whether a post wakes its readers. Members get three tools: `post_topic` posts to a topic, `read_topics` returns the posts the caller has not read yet, oldest first and fenced with the poster's name, and `list_topics` lists the topics the caller can see and who reads and writes each. A post is addressed to the topic (`#team-a`) and reaches every reader by membership, not activity: a reader that has not started yet, or one held idle, finds the post unread when it next looks, as on a blackboard. Every post goes through the swarm's bus like a message, so it gets the bus's sender binding, storm controls and `sent`, `delivered` and `read` evidence, and the swarm's own tools end with a status line counting the caller's unread posts. Posts wake a reader's `swarm_wait` only in topics the author marks `wake=True`. A private topic does not exist for a member outside it: its name never appears in that member's preamble, lists, status lines or errors.

The same design adds **routes** to the bus, `swarm(routes=[(sender, recipient), ...])`: when set, a record reaches only the recipients its sender has a route to, in the messages channel and the topics channel alike, and a send that reaches nobody is refused with evidence. With topics and routes, an experiment can run team rooms, a hub and spokes, or a pipeline of stages as configuration, with identical members across arms. `topics()` with no arguments is one room, `#general`, that every member reads and writes.

## Background

A deep dive under [swarm-communication.md](swarm-communication.md), which designs the bus and anticipates this channel: "A board post is `kind="post"` addressed to a channel id whose subscribers are the recipients; the channel id is resolved by the board's resolver" ([How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus)), with `#`-prefixed channel ids reserved in the address grammar ([Addressing](swarm-communication.md#addressing)). It builds on the messages channel (the same design), the shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md)), `swarm_wait` ([swarm-wait.md](swarm-wait.md)), persistent members ([swarm-persistent-members.md](swarm-persistent-members.md)) and the public bus API, S2, of [swarm-orbit.md](swarm-orbit.md#s2-a-public-bus-api-for-channels). ORBIT keeps its own channel on that API; this design changes nothing ORBIT's port relies on ([ORBIT's channel](#orbits-channel-what-is-general-and-what-is-orbits)).

It keeps the standing decisions of those designs. The ones it leans on most: peer text reaches a member only as tool output, fenced, with the sender the bus bound (decision: Ransom, 2026-10-07); the swarm adds no message to a member's conversation, and members learn of changes from tools (2026-10-27); the status line ends the results of the swarm's own tools only (2026-10-30); a design serves the shapes real use has and refuses the rest loudly (2026-10-08).

Code references are to inspect_swarm `main` at `6ccfd74` and ORBIT 1.0.4 (tag `v1.0.4`, [github.com/wlanderson0/orbit](https://github.com/wlanderson0/orbit)), prefixed `ORBIT:`. Claims marked *(spike)* were checked by running code against those commits.

## Why

The swarm has two channels with records, and neither gives an experiment a room:

- **Messages are addressed per send and gated by activity.** Every `send_message` names its recipients, and the bus refuses a recipient that has not started or has ended ([Current behaviour](#addressing-and-liveness)). A group that should share one conversation (a team, a stage's output) has no name, so every member must be told who is in it, and a member a controller starts later can never receive what was said before it started.
- **The shared memory is one board for everyone.** Every member and every subagent reads and writes every file. It has no notion of readers and writers, no private area, and it wakes every waiter of a path on every write. The lead-and-workers example says it in its worker prompt: "Every worker is woken when the lead writes, whichever worker the task is for" (`examples/lead_and_workers.py:9`).
- **Nothing says who may talk to whom.** The messages channel lets every member address every other ([swarm-communication.md Configuration](swarm-communication.md#configuration), "No routes"), so a hub-and-spoke arm that also has direct messages is not a hub and spokes.

The research questions swarm.md lists ask for exactly these as configuration: "Which topology and channels matter? Ablations: leaderless against coordinator, filesystem only against messages, task list or board, one mechanism removed at a time", with "topology and channels as configuration, not code; identical members across arms" ([swarm.md](swarm.md#eval-questions-and-the-experimental-design-they-imply)). ORBIT, the security framework built on Inspect, runs its topology experiments on channels with declared readers and writers, private channels and communication edges ([ORBIT's channel](#orbits-channel-what-is-general-and-what-is-orbits)); the swarm has the bus for them but no channel of that shape.

## Goals and non-goals

Goals:

- A built-in `topics` channel: rooms declared by the eval author with readers, writers, a private flag and opt-in wake; `post_topic`, `read_topics` and `list_topics`; per-reader unread tracking; delivery by membership, to readers whatever their state.
- The bus's machinery for it unchanged in kind: sender binding, fencing, storm controls, evidence, the status line, `swarm_wait`, news for idle persistent members.
- Routes on the bus, so that one rule says who may talk to whom for both addressed channels.
- The experiment topologies it serves, each as configuration: a common room, team rooms, a hub and spokes, and a pipeline ([Topologies](#topologies)). The scope is those.
- The topics channel uses only the bus operations S2 makes public, plus three in-package integrations named below, so it does not change ORBIT's port.

Non-goals:

- ORBIT's channel and its port ([swarm-orbit.md](swarm-orbit.md)). Its NDJSON read format, store state and counter, `step_start` read cutoffs, attack-created channels, push delivery (`notify`, `auto`, dialogue projections) and exposure stay ORBIT's.
- Topics created by members at run time, and subscriptions. Every topic is declared by the eval author before the swarm runs ([Not this design](#not-this-design)).
- Rereading history: a read returns unread posts only.
- Bridged members (Claude Code, Codex) on topics. `swarm()` refuses a bridged member beside a channel `swarm_bridged_tools()` does not serve, as it does today ([Current behaviour](#bridged-members-and-custom-channels)).
- Routes for shared state. The shared memory and the filesystem have no recipients; routes cannot govern them ([Routes](#routes)).
- Push into a member's conversation, which [swarm-communication.md open question 2](swarm-communication.md#open-questions) governs for every channel.

## Current behaviour

Only what this design depends on. Paths are relative to `src/inspect_swarm/` unless they name another directory.

### The bus

- **`deliver()`** (`_bus/_bus.py:171-231`) binds the sender from the calling member, refuses everything once the swarm is stopping, resolves `draft.to` with the member resolver (`:181`), builds the `Record` with `routed_out=()` (`:183-199`), runs the channel's `validate()` (`:200`), then, with no `await`, the sender's liveness, an unencodable payload, the channel's storm controls and `admit()` (`:206-218`), writes `sent` (`:221`), increments the commit sequence, runs `apply()`, writes `delivered`, releases waits and reports news for idle members (`:223-231`). A refusal at those checks writes `sent` with the control and reason before raising (`:213-218`).
- **`Record.routed_out`** exists, "Resolved names the channel's routes dropped" (`_bus/_record.py:49-50`), and is written into `sent`'s data (`_evidence.py:177`), always empty: nothing sets it (`_bus/_bus.py:190`).
- **Evidence types.** `sent` carries the record's `to` and `recipients` in inspect_ai's typed `CommunicationRecord`, and `delivered` carries `recipients` in `CommunicationDelivery`, documented as "the agents it reached" (`_evidence.py:167`, `:204`). A rejection's `control` is a free string (inspect_ai `CommunicationRejection.control`).
- **Reads.** A channel's tool reports a result's records with `Bus.read(channel, records, depends_on=...)` (`_bus/_bus.py:233-268`), which writes the `read` event and the critical path's links.
- **The status line.** `Bus.status()` returns the messages channel's unread line, found by the state's type, in the member's own loop only (`_bus/_bus.py:328-338`). `send_message`, `shared_memory` and `swarm_wait` end with it; `list_members` ends with the messages line or "You have no unread messages." (`_channel/messages.py:332-335`).
- **Idle members.** After a commit, a `wake=True` record that would end a whole-board wait of an idle member's own loop is reported to the runtime as news for that member, which the controller sees as `MemberNews` (`_bus/_bus.py:309-320`). Metrics and the ledger read every channel's `sent`, `delivered` and `read` events generically, by channel name, and count rejections by control (`_metrics.py:311-337`, `_ledger.py:350-370`).

### Addressing and liveness

`resolve()` (`_bus/_address.py:25-61`) takes member names and `all`. It refuses a name that is not a member, the sender itself, and a member that is `pending`, `ending` or `ended`; an `idle` persistent member is accepted. `all` is every other running member. *(spike: `resolve("a", ("#team-a",), ...)` raises `'#team-a' is not a member of the swarm; ...`, and a member in state `idle` resolves.)* Member names follow `[a-z][a-z0-9_-]{0,47}`, with `all` and `swarm` reserved (`_address.py:12`, `:19`), which the messages channel's `check()` enforces (`_channel/messages.py:102-107`). The messages channel's `validate()` requires at least one recipient (`_channel/messages.py:210-211`).

### `swarm_wait` and channels it does not know

`Waits._make_tool()` describes the wait from the channel states it waits on: the messages description when every one is the messages state, otherwise the shared-memory description with its `path` and `file_id` parameters (`_bus/_wait.py:356-363`, `wait_tool()` at `:442`). A wait result appends the status line unless the result is only the messages channel's news (`_bus/_wait.py:296-325`).

### Bridged members and custom channels

`swarm()` refuses a bridged member in a swarm with any channel that has records other than the built-in messages and shared memory, because `swarm_bridged_tools()` serves only those two (`_swarm.py:346-360`). A member that chooses its own name is accepted only beside the filesystem channel or none (`_swarm.py:366-377`).

### Channel arguments in the log

A list of pydantic models given to a `@swarm_channel` factory is logged as a list of their fields, and replay passes those dicts back to the factory; a list of tuples given to an `@agent` is logged as a list of lists *(spike: a factory given `[Topic(name="team-a", readers=["a", "b"], private=True)]` and `rate=(10, 30.0)` logged `{"topics": [{"name": "team-a", "readers": ["a", "b"], "private": True}], "rate": [10, 30.0]}` in the plan step, and `create_registry_object()` called it again with the dict and the list; `routes=[("lead", "w-1"), ("w-1", "lead")]` logged `[["lead", "w-1"], ["w-1", "lead"]]`)*. So a factory takes models or mappings, as `swarm()` takes `Member` records or mappings (`_swarm.py:250-252`).

### Who uses rooms today

Nothing in this repository: its channels with records are messages and the shared memory, and the lead-and-workers example runs a hub and spokes over the shared memory, waking every worker on every lead write (`examples/lead_and_workers.py:1-10`, `:89-94`). Outside it:

- **ORBIT** has rooms, readers, writers, private channels and edges, in its own channel ([below](#orbits-channel-what-is-general-and-what-is-orbits)). Its topology presets are a single agent, a star (`star_orchestrator_workers`) and meshes (`ORBIT: orbit/configs/topology_presets.py:1-13`).
- **Codex** has an optional message board with channels, threads, posts and subscriptions ([swarm.md](swarm.md#related-work-and-what-it-tells-us)), a vendor swarm's internal traffic that the bridged-swarms work maps, not this channel.

So the shapes this design serves are the room configurations ORBIT's experiments use and the topologies swarm.md's ablations name, declared statically by the author. Anything else is refused at construction ([Configuration](#configuration)).

### ORBIT's channel: what is general and what is ORBIT's

ORBIT 1.0.4's `ChannelConfig` is an id, an optional display name, `readers`, `writers` and `private` (`ORBIT: orbit/configs/communication.py:12-28`). A post goes to the channel's readers, minus the sender, intersected with the configured communication edges; with a policy configured, a post with no recipient left fails (`ORBIT: orbit/communication/runtime.py:230-252`). Edges are `CommunicationEdge(from_agent, to_agent)` pairs: `None` derives routes from membership and `[]` denies every cross-agent route (`ORBIT: orbit/configs/setup.py:223-227`, `:273-278`). `private` is a label: "Membership and directed routes enforce access; the label alone does not" (`ORBIT: docs/runtime-architecture.md:49-50`); the collusion scorer reads it to pick a coalition's private-channel messages (`ORBIT: orbit/scorers/collusion_scorer.py:89-94`). `channel_list` shows only the channels the caller reads or writes (`ORBIT: orbit/communication/tools.py:164-180`). Read cursors are per agent, session and channel; a read returns a bounded page after the cursor, and the caller's own posts too (`ORBIT: orbit/communication/delivery.py:81-145`, `runtime.py:281-297`). A post must fit one read and one inbox at its size limits (`delivery.py:179-201`).

| ORBIT feature | Here |
|---|---|
| Rooms with declared readers and writers | Topics ([Configuration](#configuration)) |
| Delivery by membership: readers that have not run yet get the post | Yes ([Delivery by membership](#delivery-by-membership)) |
| Communication edges, intersected with readers | Routes on the bus, for messages and topics ([Routes](#routes)) |
| Capacity: every accepted post fits one read | Yes, by bytes, as the messages channel does ([Reading](#reading)) |
| `private` | Hides the topic from non-members, and is in the logged configuration for analysis ([Private topics](#private-topics)) |
| Per-reader read cursors | Per-reader unread tracking, per member, not per session: a member's own loop is the one reader ([Reading](#reading)) |
| NDJSON read format, store state and counter, `step_start` cutoffs, attack-created `secret:` channels, `notify`/`auto` delivery and dialogue projections, exposure | ORBIT's; left out |
| Rereads from an explicit cursor (`after_sequence`), the caller's own posts in its reads, paged `channel_list` | Left out: no use here needs them ([Not this design](#not-this-design)) |

## Design

### At a glance

```
 lead (react)                                        worker-2 (react, pending)
 post_topic(topic="orders", message="...")           ... starts later ...
        │ Draft(channel="topics", kind="post",       read_topics() ──▶ <topic_post id="p-1" topic="#orders" from="lead">
        │       to=("#orders",), wake=True)                           ...
        ▼                                                             </topic_post>
 ┌──────────────────────────── Bus.deliver() ───────────────────────────────────┐
 │ 0 bind sender; the topics state resolves #orders to its readers, whatever    │
 │   their state; routes split recipients / routed_out; validate()              │
 │ 2 liveness (sender), payload, routes, storm controls (max_bytes, rate)       │
 │ 3 sent    4 seq, apply (each reader's unread), delivered, wake, idle news    │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Everything is inspect_swarm code; inspect_ai needs no change. The bus changes in four places, each small: a channel may resolve its own addresses ([Channel-resolved addresses](#channel-resolved-addresses)), routes ([Routes](#routes)), a status line from more than one channel ([The status line](#the-status-line)) and a `swarm_wait` description that names topics ([Waiting](#waiting)).

### Configuration

```python
# src/inspect_swarm/_channel/topics.py
class Topic(BaseModel, frozen=True):
    """A topic: a room that its readers read and its writers post to."""

    name: str
    """The topic's name, addressed as `#<name>`: a lowercase letter, then at most 47 lowercase letters, digits, `_` or `-`."""

    readers: Literal["all"] | list[str] = "all"
    """The members that read it: `"all"`, every member of the swarm, or a list of member names."""

    writers: Literal["all"] | list[str] = "all"
    """The members that post to it, in the same form."""

    private: bool = False
    """Whether only its readers and writers know it exists."""

    wake: bool = False
    """Whether a post ends its readers' `swarm_wait` and is news for an idle reader."""


@swarm_channel
def topics(
    topics: Sequence[Topic | Mapping[str, object]] = (Topic(name="general"),),
    max_bytes: int = 8_000,                                   # UTF-8 bytes per post
    rate: tuple[int, float] | list[int | float] | None = (20, 60.0),  # posts per member per sliding window (s), across topics
    max_wait: int = 120,                                      # this channel's contribution to swarm_wait's maximum
) -> Channel: ...
```

- **The string `"topics"`** in `channels=` gives the default: one public topic, `#general`, that every member reads and writes, with no wake. `-T channels=filesystem,topics` selects it from the command line.
- **Logging.** `Topic` is a pydantic model, so a list of them is logged as their fields and two arms that differ in any topic are distinct; replay passes the logged dicts back, which `topics()` validates into `Topic`s, as `swarm()` does for `Member` records ([Channel arguments in the log](#channel-arguments-in-the-log)). `Topic` is exported from `inspect_swarm`.
- **Validation when `topics()` is called** (`ValueError`, naming the topic): at least one topic; each name in the grammar; no two topics with one name; `readers` and `writers` either `"all"` or a non-empty list without repeats; `max_bytes`, `max_wait` at least 1 and `rate` as the messages channel checks them (`_channel/_params.py`).
- **`check(roster)`** at `swarm()` construction (`ValueError`): every member name in the address grammar and not reserved, as the messages channel requires, since names appear in fences; every name a topic lists is a member; and each topic has a writer with at least one reader other than itself, since otherwise no post could reach anyone (a topic whose only reader is its only writer).
- **Channel attributes.** `name = "topics"`, `prefix = "p"` (record ids `p-1`, `p-2`), `reach = "loop"`: a read marks posts read for the member, so a member's topics have one reader, its own loop, as its inbox does ([swarm-communication.md Members and identity](swarm-communication.md#members-and-identity)). `storm = StormControls(max_bytes=max_bytes, rate=rate)`; `max_wait` as given, so the channel always wakes waits and `swarm_wait` comes with it.
- **Refused combinations**, by today's rules and no new code: a bridged member beside `topics` ([Current behaviour](#bridged-members-and-custom-channels)); a member with `choose_name=True` beside `topics`. Each names the channel in its error.

### Topologies

Each is configuration only; the members are the same in every arm.

**A common room**, the blackboard baseline beside "filesystem only" and "messages": `channels=["filesystem", "topics"]`.

**Team rooms.** Two teams with a private room each, and an announcement room the leads write and everyone reads:

```python
red = ["red-1", "red-2", "red-3"]
blue = ["blue-1", "blue-2", "blue-3"]
swarm(
    members=[member(worker, name="red", count=3), member(worker, name="blue", count=3)],
    channels=[topics([
        Topic(name="red", readers=red, writers=red, private=True),
        Topic(name="blue", readers=blue, writers=blue, private=True),
        Topic(name="announcements", writers=["red-1", "blue-1"], wake=True),
    ])],
)
```

A red member never learns that `#blue` exists. With the messages channel added, `routes` keeps direct messages inside each team, plus between the two leads.

**A hub and spokes.** A lead gives each worker its own orders in a private topic and reads every report:

```python
workers = [f"worker-{i}" for i in range(1, k + 1)]
topics([
    *(Topic(name=f"orders-{w}", readers=[w], writers=["lead"], private=True, wake=True) for w in workers),
    Topic(name="reports", readers=["lead"], writers=workers, wake=True),
])
```

A post in `#orders-worker-2` wakes worker-2 alone, or, for a persistent worker held idle, gives the controller `MemberNews` for worker-2 alone, which the shared-memory version of the lead-and-workers example cannot do. Workers do not read each other's reports. With the messages channel too, `routes=[("lead", w) for w in workers] + [(w, "lead") for w in workers]` keeps direct messages on the spokes.

**A pipeline.** Each stage hands its output to the next:

```python
stages = ["extract", "analyse", "write"]
topics([Topic(name=f"{a}-to-{b}", readers=[b], writers=[a], wake=True) for a, b in zip(stages, stages[1:])])
```

With a controller that starts each stage when the one before it ends, a stage finds its input unread when it starts, because the post reached it by membership while it was pending ([Delivery by membership](#delivery-by-membership)). With every stage started at once, a downstream stage waits for its input with `swarm_wait`.

### Records and addressing

A post is a `Record` with `channel="topics"`, `kind="post"`, `to=("#orders",)`, `recipients` the topic's readers other than the poster (in roster order, less any that routes drop), `routed_out` the readers routes dropped, `payload` the text, `op=None`, `wake` the topic's `wake`, and `reply_to` the id of a post the poster has posted or read, or `None`. The record is addressed, so `sent` names its readers in its typed `recipients`, `delivered` names whom it reached, and routes can govern it.

The `#` prefix keeps topic addresses apart from member names, as the address grammar reserved (`swarm-communication.md` [Addressing](swarm-communication.md#addressing)). A topic's address is its author-given name, not a swarm-assigned id such as `#c-3`: those ids were reserved for names a member chooses, which are payload and may not appear in status lines ([How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus), "Names a member chooses"); every topic here is named by the eval author and grammar-checked, as member names are, so its name is safe in fences, status lines and preambles, and a model reads `#orders` more reliably than `#c-3` ([Alternatives considered](#alternatives-considered)).

#### Channel-resolved addresses

A channel state may resolve its own records' addresses. If the state of `draft.channel` has a method `resolve(sender: str, to: tuple[str, ...]) -> tuple[str, ...]`, step 0 of `deliver()` calls it in place of the member resolver; otherwise nothing changes. It returns the recipients or raises `ToolError`, before a record exists, as an unresolvable member address does today. It is an internal protocol of the package (`ResolvingState` in `_channel/_channel.py`, not exported), used by the topics state only; S2's `Bus` protocol, `Draft` and `Delivery` are unchanged, and a channel written on S2 keeps the member resolver or posts unaddressed records, as ORBIT's does.

The topics state's `resolve()`:

1. `to` holds exactly one address, `#<name>`; otherwise "post to one topic at a time, such as #orders.".
2. The topic exists and the sender can see it (any public topic, and a private one it reads or writes); otherwise `no topic #<name>; your topics are #a, #b.`, the same text for a private topic the sender is not in as for a name that does not exist ([Private topics](#private-topics)).
3. The sender is a writer; otherwise `you cannot post to #<name>; its writers are lead.`.
4. The recipients are its readers other than the sender, **whatever their state**: pending, running, idle, ending or ended ([Delivery by membership](#delivery-by-membership)).

The post tool accepts `topic="orders"` or `topic="#orders"` and always addresses `#orders`.

#### Delivery by membership

A post reaches every reader the topic lists, not only those running when it is posted. The bus's liveness rules stay as they are: the poster must be running (step 2's liveness check), and nothing is delivered once the swarm is stopping. Only the recipients' state is not checked, because a topic is a room and its readers are its members, not the members active at the moment. So:

- a **pending** reader (not started by its controller) finds the post unread when it starts;
- an **idle** persistent reader finds it unread when woken, and a post in a `wake=True` topic is news the controller sees as `MemberNews` ([Waiting](#waiting));
- an **ended** reader has it unread forever. It is still a recipient in the evidence, which says the record was delivered to its unread posts, not that it was read; a `read` event says that.

The messages channel keeps its rule: a direct message to a member that is not running is refused, because an inbox is a member's and a message to someone who will never read it is a failed send. A topic's history is the room's, and a reader that arrives later reads what was said before it came.

#### Private topics

A private topic exists only for its readers and writers. For every other member its name never appears in the member's preamble, `list_topics`, status lines, `swarm_wait` results or tool errors, and naming it gets the same error as a name that does not exist. A public topic is listed to every member, with its readers and writers, so members know the rooms they are not in and who is in them. The flag is in the logged configuration, so analysis can tell private traffic apart (ORBIT's collusion scorer uses its label for that).

Private here is a visibility rule, not an access rule: access is `readers` and `writers` in both cases, and the evidence holds every post, private or not, for whoever reads the log.

### Routes

```python
def swarm(
    members=..., *, controller=..., channels=..., budget=...,
    routes: Sequence[tuple[str, str] | list[str]] | None = None,
) -> Agent:
    """...
        routes: Who may send to whom, as (sender, recipient) member names. `None` (the default): every member may reach every other. A list: a record reaches only recipients its sender has a route to, in the messages and topics channels; `[]` lets nobody reach anyone. Routes do not govern the shared memory or the filesystem, which have no recipients.
    """
```

- **On the bus, for every addressed channel.** The bus holds the run's routes. At step 0, after the addresses resolve (by the member resolver or a channel's), it splits the recipients in order into those the sender has a route to, which stay `recipients`, and the rest, which become `routed_out`. Unaddressed records (shared-memory writes) have no recipients and pass unchanged. Routes are pairs of members, not a channel's setting, so the messages channel and the topics channel obey one rule, and an arm cannot give them two that disagree.
- **A send that reaches nobody is refused, with evidence.** A new commit check at step 2, after liveness and the payload check and before the storm controls: a record whose `recipients` is empty and whose `routed_out` is not is refused with control `routes` and `worker-2 cannot reach worker-3: the swarm's routes do not allow it.` (or `... cannot reach any reader of #reports ...`), and its `sent` event records `routed_out`. Metrics count it with the other rejections by control. The messages channel's `validate()` then requires a recipient or a routed-out member, rather than a recipient, so a send whose every recipient is routed out reaches this check instead of failing validation without evidence.
- **A send that reaches some is delivered, and says whom it did not reach.** `Sent m-4 to worker-2. Not sent to worker-3, worker-4: your routes do not reach them.`; `Posted p-7 to #all-hands, read by lead. Not posted to worker-2: your routes do not reach them.` A model is never left believing a member got a message it did not.
- **`all`** resolves to the running members other than the sender, as today, and routes then drop those the sender cannot reach.
- **`list_members`** marks members the caller has no route to, `worker-3: running (no route from you)`, when routes are set.
- **Validation** at `swarm()` construction (`ValueError`): each route is two member names of the roster, not the same name twice; repeats collapse. Routes given to a swarm with neither the messages nor the topics channel are refused, since they would govern nothing. Logged as a list of lists; replay's lists are accepted.
- **Reads** need no route check: recipients are fixed when a record commits, and a read shows only what was delivered to the reader.
- **What routes do not bound.** Members that share a sandbox, the shared memory or a shared tool instance can still talk through it, as swarm-communication.md says of messages ([The filesystem channel](swarm-communication.md#the-filesystem-channel)). An arm whose topology must be the only path gives members their own sandboxes and leaves shared state out.

ORBIT's channel computes its own recipients from its own edges and posts unaddressed records ([swarm-orbit.md The channel](swarm-orbit.md#the-channel)), so swarm routes never apply to it, and a ported run sets none.

### The tools

All three are offered to the member's own loop only and refuse a call from another loop with a `ToolError`, as the messages channel's do (`_channel/messages.py:345-357`). Each ends with the status line ([The status line](#the-status-line)).

**`post_topic(topic: str, message: str, reply_to: str | None = None) -> str`**

- `topic`: a topic you post to, such as `orders` or `#orders`. `message`: the text. `reply_to`: the id of a post this one answers, such as `p-3`.
- Delivers `Draft("topics", "post", to=("#orders",), payload=message, wake=<the topic's wake>, reply_to=reply_to)`. `validate()` requires `kind == "post"`, no `op`, a non-empty payload, a recipient or a routed-out reader, and a `reply_to` the poster has posted or read; `admit()` has nothing to check.
- Returns `Posted p-7 to #orders, read by worker-1, worker-2.`, with the routes sentence when routes dropped a reader.

**`read_topics(topic: str | None = None) -> str`**

- Without `topic`, the caller's unread posts in every topic it reads, in commit order; with it, those in that topic, which the caller must read (a topic it does not read is `you do not read #reports.`; a private one it is not in, the unknown-topic error).
- Packs whole posts up to the read budget, marks exactly those read, in the synchronous step that builds the result, and reports them with `Bus.read("topics", ids)`, which writes the `read` event and the critical path's links. Built with `max_output=0`, as `read_messages` is, so no output limit cuts a fence.
- Never blocks; a reader with nothing unread waits with `swarm_wait`.

**`list_topics() -> str`**

One line per topic the caller can see, in declaration order: `#orders-worker-2 (private): readers worker-2; writers lead; you read. 1 unread.`, `#reports: readers lead; writers worker-1, worker-2; you post.`, and, for a public topic the caller is not in, `#red: readers red-1, red-2, red-3; writers red-1, red-2, red-3; you are not in it.` `"all"` is shown as `all members`. It shows no peer text and writes no evidence, as `list_members` does.

#### Reading

Every accepted post fits one read, as every message does ([swarm-communication.md Direct messages](swarm-communication.md#direct-messages)):

- When the state opens, it computes the largest fence the run allows (the tag, the longest id `p-` plus 14 digits, the longest topic name, the longest member name as `from`, and a `reply_to` id), the largest header and remainder lines (counts of 14 digits), and the read budget, `header_max + envelope_max + max_bytes + trailer_max`.
- A read always includes the oldest unread post (which fits, by the storm control's `max_bytes` at post time) and adds whole posts while the total stays within the budget.

Per-reader unread tracking is one list per member of the records delivered to it, in commit order, filtered by topic for a read with `topic`. A poster's own posts are never unread for it: it is not a recipient of them.

#### Fencing

```
3 posts, oldest first.

<topic_post id="p-4" topic="#orders-worker-2" from="lead">
...payload...
</topic_post>
<topic_post id="p-9" topic="#orders-worker-2" from="lead" reply_to="p-6">
...payload...
</topic_post>
<topic_post id="p-11" topic="#general" from="worker-1">
...payload...
</topic_post>
2 more unread posts; call read_topics() again.
```

- One post is `1 post.`; nothing unread is `You have no unread posts.` (or `... in #orders.`), with no `read` event. With `topic`, the remainder line names the topic and the call: `2 more unread posts in #orders; call read_topics(topic="orders") again.`
- Attribute values are record ids, author-given topic names and roster names, all grammar-checked, so they need no escaping (`_fence.py`).
- The fence is a label, not a boundary: a post is delivered as typed, and a member can write text that looks like another member's post or a status line (decision: Ransom, 2026-10-27; [swarm-communication.md Fencing](swarm-communication.md#fencing)). Evidence comes from bus events and ids, never from result text.

### Storm controls

| Control | Default | On violation |
|---|---|---|
| `max_bytes` per post, UTF-8 | 8,000 | rejected: `post is N bytes; the limit is 8000` |
| `rate` per member, across its topics | 20 per 60 s, sliding window | rejected: `rate limit: retry in N s` |
| `routes` | none | rejected when no reader is reachable ([Routes](#routes)) |

Both of the first two are the bus's `StormControls`, so the bus applies them and records their refusals as it does for messages. The channel has no `max_unread` and no `dedupe`: those are back-pressure on an inbox the recipient must be running to drain, and under membership delivery a pending or ended reader never drains its unread posts, so a cap on them would close a topic to every writer for good. `rate` bounds what any one member can add, and a reader's unread posts are read a budget at a time.

### The status line

`Bus.status()` returns the lines of every built-in channel that counts unread records for the calling loop, in the order of `channels=`: the messages line, `You have 2 unread messages.`, and the topics line, `You have 4 unread posts (#orders-worker-2 3, #general 1).`, each only when its count is above zero, and nothing in a nested loop, which reads neither. The tools that end with the status line today (`send_message`, `shared_memory`, `swarm_wait`) and the topics tools end with these lines; `list_members` keeps its zero-count message line and adds the topics line; `read_topics` ends with its remainder line instead of the topics line, and `read_messages` with its own, as today. Tools of other channels, a user channel's or Inspect's, carry no count (decision: Ransom, 2026-10-30).

The topics line holds counts and author-given topic names the caller reads, never a payload. Topics are listed in declaration order, so the line is at most one entry per topic the author declared.

### Waiting

The topics state is a `WaitingState`:

- **`wakes(spec, record)`**: the wait is the member's own loop and the member is one of the record's recipients. The bus calls it only for records with `wake` set, so only posts in `wake=True` topics end a wait.
- **`news(spec)`**: in the member's own loop, when any unread post is from a `wake=True` topic, one line counting every unread post and naming topics and posters: `4 unread posts in #orders-worker-2, #general (from lead, worker-1). Call read_topics() to read them.`; its `depends_on` is every unread post it counts, those of `wake=False` topics included, as the messages channel's news counts quiet messages ([swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance)). Without a waking unread post it has no news, and a wait that returns for another reason ends with the status line's count, which links nothing.
- **Idle persistent members.** The bus's existing check reports a `wake=True` post to an idle reader as news, so the controller gets `MemberNews(member, sender, ...)`, and `MemberIdle.news` is set while such a post stays unread. A post in a `wake=False` topic waits for the reader's next activation.

Two changes in `_bus/_wait.py`:

- **The description.** `wait_tool()` takes a `topics` flag beside `messages` and `board`, and `_make_tool()` sets it when a topics state is among the channels waited on. The description's first sentence names each source the wait watches (an unread message sent with `wake=True`, an unread post in a topic that wakes its readers, a shared-memory change, a member's end), and its acknowledgment sentence names each read tool. `board` stays true for any channel state other than messages and topics, as today, so a custom waiting channel's wait is described as it is now.
- **The trailing count.** A wait result ends with each counting channel's status line unless that channel's news is already in the result, so a result with message news and quiet unread posts still counts the posts. With messages alone this is today's rule.

`swarm_wait` is offered with the topics channel even when no topic wakes: it still returns on a member's end, when every member is waiting, at the stop and at its timeout, and a member may wait for those.

### Instructions

The channel's `instructions(member)` is a fixed template with only author-given names: what the three tools do; that posts are shown between `<topic_post>` tags whose `from` attribute names the poster; the member's own topics with its access, `You read and post in #general; you read #orders-worker-2; you post in #reports.`, or `You are in no topic.`; for topics it reads that wake, `A post in #orders-worker-2 ends swarm_wait.`; and that the results of the swarm's tools count unread posts. Public topics the member is not in are left to `list_topics`, and private ones it is not in are never named. It names no other member.

### Evidence

No new event or field. Posts write `sent`, `delivered`, `read` and `waited` events with `channel="topics"`, as messages do ([swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance)): `sent` with the record, its `to` (`["#orders"]`), its `recipients` and, in its data, `routed_out`; `delivered` with the recipients and the commit sequence; `read` with the posts a read showed. A routes refusal is a `sent` with control `routes`. The communication metrics, the ledger and the critical path read them by channel name, so they count topics with no change ([Current behaviour](#the-bus)). Analysis reads a topic's privacy, readers and writers from the logged channel configuration.

### How this sits on S2

The topics channel's tools use only the operations S2 makes public ([swarm-orbit.md S2](swarm-orbit.md#s2-a-public-bus-api-for-channels)): `deliver(Draft)`, the read evidence call, and the calling member. It lives in the package and uses three integrations S2 does not publish, each a built-in channel's, as the messages channel's are today: channel-resolved addresses, the status line and the `swarm_wait` description. Routes are the bus's own. ORBIT's port is unchanged: its channel posts unaddressed records with recipients it computes, takes no part in the status line, and its runs set no routes.

## Alternatives considered

**Unaddressed posts, with recipients the channel computes in `apply()`.** S2's pattern for ORBIT, needing no bus change. But the `sent` event would name no recipients, `delivered`'s typed `recipients`, "the agents it reached", would be empty for every post, and routes on the bus could not govern posts, so the messages channel and the topics channel would need two rules. Rejected for a built-in channel; ORBIT keeps the pattern.

**A `recipients="roster"` class attribute**, lifting the liveness check for a channel's addresses. swarm-orbit.md rejected it for ORBIT. It is not enough here either: `#orders` still has to resolve to the topic's readers, which only the channel knows. The resolver hook does both.

**Posting to topics through `send_message(to="#orders")`.** One fewer tool. But one inbox would mix two delivery rules (liveness for members, membership for topics), the inbox's `max_unread` back-pressure would close a topic as soon as a pending reader had 50 unread posts, and an arm with topics and without direct messages would need a messages channel that refuses member addresses. Rejected.

**Swarm-assigned ids (`#c-3`).** What swarm-communication.md reserved, for names a member chooses. Every topic here is declared and named by the author, so the name is as safe as a member name, and a model would have to map numbers to rooms. Rejected; ids remain the form for topics created at run time, if those are ever built.

**Wake set per post by the model** (`post_topic(wake=True)`), as `send_message` does. But whether a room interrupts its readers' waits is the experiment's design, not the poster's choice, and a per-post flag lets a chatty member end every reader's wait. Rejected for a per-topic setting the author logs.

**Routes per channel** (`messages(routes=...)`, `topics(routes=...)`), where swarm-api.md first placed them. Two settings that can disagree, for one fact about members. Rejected for one setting on `swarm()` the bus applies.

**No routes.** Topics alone express every topology above, so routes would matter only beside the messages channel. But an arm that keeps direct messages, which a hub and spokes with a coordinator naturally has, would then not have the topology it claims, and `Record.routed_out` was reserved for exactly this. Kept, as its own implementation step ([Open questions](#open-questions)).

**ORBIT's `private` as a label only.** Simpler. But a public and a private topic would then behave identically in the swarm, and a team experiment could not keep one team's room unknown to the other. Rejected for a visibility rule that the label also records.

**Rooms as shared-memory files** (`/shared_memory/rooms/red.md`). No new channel. But the board has no readers or writers, no privacy, no per-reader unread posts, and wakes every waiter of a path on every write. Rejected.

**A `max_unread` or a total post cap per topic.** Bounds unread growth. But a reader that never runs would close the topic for every writer; `rate` already bounds what a member adds. Rejected.

## Compatibility and migration

- **Stored formats.** No new event type or field: posts write today's communication events, with a new channel name, kind (`post`), id prefix (`p`) and rejection control (`routes`). `routed_out` gets its first non-empty values. The viewer's generated types and the swarm's evidence version are unchanged.
- **Public API.** New: `topics`, `Topic`, and `swarm(routes=None)`. `routes=None` is today's behaviour. Without topics and routes, every tool's schema, description and output is unchanged: the status line, the wait description and the messages channel's validation behave as today.
- **Existing swarms with the messages channel** gain nothing they did not ask for; with routes, sends that routes drop are refused or report the members they missed.
- **Bridged members** are refused beside `topics` by today's rule, with today's message naming the channel.
- **ORBIT's port** is unchanged ([How this sits on S2](#how-this-sits-on-s2)).
- **inspect_ai**: no change.

## Security

- **Untrusted input reaching the new code:** the post text, the `topic` argument and `reply_to`, all model output, and through the bridge none (bridged members are refused).
  - The text is stored and delivered as typed, inside a fence that is a label, never a boundary; it is bounded by `max_bytes`, and a lone surrogate is refused by the bus's existing check. It never reaches a status line, a wait result, a preamble or an error.
  - `topic` is checked against the topics the caller can see; any other value gets the same error, which lists only those topics, so the error reveals no private topic. `reply_to` must be an id the caller has posted or read.
- **Sender identity** is bound by the bus from the member context, as for every record; no argument sets it.
- **Private topics** are hidden from non-members in every text the swarm writes for them: preamble, list, status line, wait result, errors, and a post's result, which only a writer, a member of the topic, receives. The log holds everything, for its readers.
- **Routes and topics are the author's**, validated at construction; no model output changes them.
- **What routes and topics do not stop**: communication through shared sandboxes, shared state or shared tool instances ([Routes](#routes)).

## Testing

All async tests are `@pytest.mark.anyio` and run on asyncio and trio through the repository's fixture; members are mockllm `react()` agents with scripted tool calls. Nothing needs network, Docker or a model provider.

- **`tests/test_topics_channel.py`** (construction): the default is one public `#general` for all; each `ValueError` of `topics()` and `check()` with its message (bad and repeated names, empty or repeated readers, an unknown member, a topic no post could leave, bounds); a list of `Topic`s and a list of mappings give the same channel; logging and replay round-trip, and two arms that differ in one topic's `private` or `wake` have different task identifiers (in `tests/test_api_log.py`); instructions name the member's own topics and access and no private topic it is not in; `swarm()` refuses a bridged member and a member with `choose_name=True` beside `topics`.
- **`tests/test_topics.py`** (runtime):
  - a post reaches readers and not the poster; a reader's `read_topics()` returns it fenced, marks it read, and writes a `read` event with its id; `sent` and `delivered` name `to=["#orders"]` and the readers;
  - membership delivery: a post to a pending reader is unread when a controller starts it later; to an ended reader, delivered and never read; a post by a member that has ended is refused with liveness evidence;
  - permissions: a non-writer's post, a read of a topic the caller does not read, and an unknown topic each give their error with no record; a private topic is absent from a non-member's preamble, `list_topics`, status lines and errors, and naming it gives the unknown-topic text exactly;
  - reading: across topics in commit order; with `topic`, only that topic's, the rest still counted; the read budget with the longest names and a payload at `max_bytes` fits one read; packing stops at the budget and the remainder line counts the rest; `max_output=0` with a small `max_tool_output`;
  - storm controls: `max_bytes` and `rate` refusals with their controls in `sent`;
  - the status line on `post_topic`, `list_topics`, `send_message`, `shared_memory` and `swarm_wait`, with messages and topics both open, and none in a deepagent's `general()` subagent, which gets none of the topics tools;
  - waiting: a post in a `wake=True` topic ends a reader's wait and names topics and posters; a `wake=False` post does not, and is counted when the wait ends for another reason; `depends_on` of a wait with news includes the quiet posts; a persistent reader held idle gets `MemberNews` for a waking post and not for a quiet one; the wait description with topics alone, with messages, and with the shared memory.
- **`tests/test_bus.py`**: a channel state with `resolve()` gets its addresses resolved by it, recipients whatever their state; routes split recipients and `routed_out` in order for the member resolver and a channel's; a record whose recipients are all routed out is refused with control `routes` after validation and before the storm controls, with `routed_out` in `sent`; unaddressed records pass routes unchanged; `routes=[]` refuses every addressed send.
- **`tests/test_messages.py`**: with routes, a direct message off the routes is refused with `routes` evidence; a list of addresses partly off the routes is delivered to the rest and its result names the others; `all` drops unreachable members; `list_members` marks them.
- **`tests/test_channel.py`**, with the other `swarm()` construction checks: `routes` validation (unknown name, self-route, no addressed channel), repeats collapsing, logging as lists and replay.
- **`tests/test_metrics.py`**: topics counted per member like messages; `routes` rejections counted by control.

## Implementation plan

1. **The topics channel.** `src/inspect_swarm/_channel/topics.py` (`Topic`, `topics()`, its validation, `check()`, `instructions()`, `TopicsState` with `resolve()`, `validate()`, `admit()`, `apply()`, the three tools, the read budget, the status line and the `WaitingState` methods); `_channel/_channel.py` (`ResolvingState`); `_bus/_bus.py` (step 0 calls a state's `resolve()`; `status()` joins the messages and topics lines); `_bus/_wait.py` (`topics` in `wait_tool()` and `_make_tool()`, the per-channel trailing count); `_channel/messages.py` (`list_members` adds the topics line); `src/inspect_swarm/__init__.py` (`Topic`, `topics`) and the entry point's imports; `docs/topics.qmd` (the topologies above as examples), `docs/channels.qmd` (the list of built-in channels), `docs/_quarto.yml`. Tests: `test_topics_channel.py`, `test_topics.py`, the topics cases of `test_bus.py`, `test_wait.py`, `test_api_log.py` and `test_metrics.py`.
2. **Routes.** `src/inspect_swarm/_swarm.py` (`routes=`, its validation and the docstring, passed to the run); `_controller/_control.py` (`SwarmRun` gives the bus its routes); `_bus/_bus.py` (the split at step 0 and the `routes` check at step 2); `_channel/messages.py` (`validate()`'s recipient rule, the send result, `list_members`' mark); `_channel/topics.py` (the post result); `docs/messages.qmd` and `docs/topics.qmd` (routes), `docs/channels.qmd`. Tests: the routes cases of `test_bus.py`, `test_messages.py`, `test_topics.py`, the construction tests and `test_metrics.py`. Independent of step 1 except for the topics cases, which follow it.

## Open questions

1. **Routes on the bus, as `swarm(routes=...)`, governing the messages and topics channels.** The alternatives are routes per channel, or no routes, leaving topologies to topics and an arm that needs one to leave the messages channel out. Recommendation: on the bus, as designed, because a topology is a fact about members that every addressed channel should obey, and an arm with a coordinator usually keeps direct messages. It is step 2 of the plan, so the topics channel does not wait on it. Confidence: high.
2. **A topic's address is its author-given name (`#orders`), not a swarm-assigned id (`#c-3`)** as swarm-communication.md anticipated. Recommendation: the name, since every topic is the author's and grammar-checked; ids stay reserved for topics members might create later. Confidence: high.

## Not this design

- **Topics created by members at run time** (ORBIT's attack-created `secret:` channels, Codex board subscriptions). They would need swarm-assigned ids, since a member's name for a topic is payload, and a rule for who may join.
- **Rereading a topic's history** with a cursor, as ORBIT's `after_sequence` does, and showing a poster its own posts.
- **Bridged members on topics**: declarations for `swarm_bridged_tools(["topics"])` and its `swarm_wait`.
- **`swarm_wait` describes a custom waiting channel as the shared memory**, with its `path` and `file_id` parameters (`_bus/_wait.py:356-363`). A user channel that wakes waits needs its own wait description and filter; this design leaves that rule as it is.
- **Channel-resolved addresses for user channels**: making `ResolvingState` public, with S2, once a channel outside the package needs it.
- **A hub-and-spoke version of the lead-and-workers example** on topics, waking only the worker a task is for.
- **Route-aware construction checks**: refusing a topic whose writers can reach none of its readers under the swarm's routes, which today fails on the first post.
