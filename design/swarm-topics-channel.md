# Inspect Swarm: the topics channel

## Overview

With `channels=[topics([...])]`, the eval author declares named rooms, **topics**, each with the members that read it, the members that write it, whether it is private and whether a post wakes its readers. Members get three tools: `post_topic` posts to a topic, `read_topics` returns the posts the caller has not read yet, oldest first and fenced with the poster's name, and `list_topics` lists the topics the caller can see and who reads and writes each. A post is addressed to the topic (`#team-a`) and reaches every reader by membership, not activity: a reader that has not started yet, or one held idle, finds the post unread when it next looks, as on a blackboard. Every post goes through the swarm's bus like a message, so it gets the bus's sender binding, storm controls and `sent`, `delivered` and `read` evidence, and the swarm's own tools end with a status line counting the caller's unread posts. Posts wake a reader's `swarm_wait` only in topics the author marks `wake=True`. A private topic does not exist for a member outside it: its name never appears in that member's preamble, lists, status lines or errors.

Who may talk to whom in a topic is its `readers` and `writers`; the design adds no member-pair routes, on the bus or for the messages channel, which keeps letting every member message every other. With topics alone, an experiment can run team rooms, a hub and spokes, or a pipeline of stages as configuration, with identical members across arms. `topics()` with no arguments is one room, `#general`, that every member reads and writes.

## Background

A deep dive under [swarm-communication.md](swarm-communication.md), which designs the bus and anticipates this channel: "A board post is `kind="post"` addressed to a channel id whose subscribers are the recipients; the channel id is resolved by the board's resolver" ([How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus)), with `#`-prefixed channel ids reserved in the address grammar ([Addressing](swarm-communication.md#addressing)). It builds on the messages channel (the same design), the shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md)), `swarm_wait` ([swarm-wait.md](swarm-wait.md)), persistent members ([swarm-persistent-members.md](swarm-persistent-members.md)) and the public bus API, S2, of [swarm-orbit.md](swarm-orbit.md#s2-a-public-bus-api-for-channels). ORBIT keeps its own channel on that API, and resolves its posts' recipients with the public address hook this design adds, so they appear in the `sent` and `delivered` evidence ([Channel-resolved addresses](#channel-resolved-addresses); [ORBIT's channel](#orbits-channel-what-is-general-and-what-is-orbits)).

It keeps the standing decisions of those designs. The ones it leans on most: peer text reaches a member only as tool output, fenced, with the sender the bus bound (decision: Ransom, 2026-10-07); the swarm adds no message to a member's conversation, and members learn of changes from tools (2026-10-27); the status line ends the results of the swarm's own tools only (2026-10-30); a design serves the shapes real use has and refuses the rest loudly (2026-10-08).

Paths prefixed `ORBIT:` are in ORBIT's repository ([github.com/wlanderson0/orbit](https://github.com/wlanderson0/orbit)); the others are in this one.

## Why

The swarm has two channels with records, and neither gives an experiment a room:

- **Messages are addressed per send and gated by activity.** Every `send_message` names its recipients, and the bus refuses a recipient that has not started or has ended ([Current behaviour](#addressing-and-liveness)). A group that should share one conversation (a team, a stage's output) has no name, so every member must be told who is in it, and a member a controller starts later can never receive what was said before it started.
- **The shared memory is one board for everyone.** Every member and every subagent reads and writes every file. It has no notion of readers and writers, no private area, and it wakes every waiter of a path on every write. The lead-and-workers example says it in its worker prompt: "Every worker is woken when the lead writes, whichever worker the task is for" (`examples/lead_and_workers.py:9`).
- **Nothing says who may talk to whom.** The messages channel lets every member address every other ([swarm-communication.md Configuration](swarm-communication.md#configuration), "No routes"), and the shared memory lets every member read every file, so no channel keeps a hub's spokes or two teams apart.

The research questions swarm.md lists ask for exactly these as configuration: "Which topology and channels matter? Ablations: leaderless against coordinator, filesystem only against messages, task list or board, one mechanism removed at a time", with "topology and channels as configuration, not code; identical members across arms" ([swarm.md](swarm.md#eval-questions-and-the-experimental-design-they-imply)). ORBIT, the security framework built on Inspect, runs its topology experiments on channels with declared readers and writers, private channels and communication edges ([ORBIT's channel](#orbits-channel-what-is-general-and-what-is-orbits)); the swarm has the bus for them but no channel of that shape.

## Goals and non-goals

Goals:

- A built-in `topics` channel: rooms declared by the eval author with readers, writers, a private flag and opt-in wake; `post_topic`, `read_topics` and `list_topics`; per-reader unread tracking; delivery by membership, to readers whatever their state.
- The bus's machinery for it unchanged in kind: sender binding, fencing, storm controls, evidence, the status line, `swarm_wait`, news for idle persistent members.
- Who may talk to whom in a topic is its readers and writers, declared per topic. Each channel decides its own communication restrictions, and some have none (decision: Ransom, 2026-11-05).
- The experiment topologies it serves, each as configuration: a common room, team rooms, a hub and spokes, and a pipeline ([Topologies](#topologies)). The scope is those.
- The topics channel uses only the bus operations S2 makes public, a public protocol for channel-resolved addresses that this design adds to S2, and three in-package integrations named below ([How this sits on S2](#how-this-sits-on-s2)). ORBIT's channel resolves its recipients with the same protocol (decision: Ransom, 2026-11-06, at the ORBIT team's request).

Non-goals:

- ORBIT's channel and its port ([swarm-orbit.md](swarm-orbit.md)). Its NDJSON read format, store state and counter, `step_start` read cutoffs, attack-created channels, push delivery (`notify`, `auto`, dialogue projections) and exposure stay ORBIT's.
- Topics created by members at run time, and subscriptions. Every topic is declared by the eval author before the swarm runs ([Not this design](#not-this-design)).
- Rereading history: a read returns unread posts only.
- Bridged members (Claude Code, Codex) on topics. `swarm()` refuses a bridged member beside a channel `swarm_bridged_tools()` does not serve, as it does today ([Current behaviour](#bridged-members-and-custom-channels)).
- Member-pair routes: on the bus for every channel (`swarm(routes=...)`), inside a topic (ORBIT's communication edges intersected with readers) or for the messages channel (`messages(routes=...)`). The messages channel stays unrestricted; an arm whose topology must hold leaves it out ([Alternatives considered](#alternatives-considered)).
- Push into a member's conversation, which [swarm-communication.md open question 2](swarm-communication.md#open-questions) governs for every channel.

## Current behaviour

Only what this design depends on. Paths are relative to `src/inspect_swarm/` unless they name another directory.

### The bus

- **`deliver()`** (`_bus/_bus.py:171-231`) binds the sender from the calling member, refuses everything once the swarm is stopping, resolves `draft.to` with the member resolver (`:181`), builds the `Record` with `routed_out=()` (`:183-199`), runs the channel's `validate()` (`:200`), then, with no `await`, the sender's liveness, an unencodable payload, the channel's storm controls and `admit()` (`:206-218`), writes `sent` (`:221`), increments the commit sequence, runs `apply()`, writes `delivered`, releases waits and reports news for idle members (`:223-231`). A refusal at those checks writes `sent` with the control and reason before raising (`:213-218`).
- **`Record.routed_out`** exists, "Resolved names the channel's routes dropped" (`_bus/_record.py:49-50`), and is written into `sent`'s data (`_evidence.py:177`), always empty: nothing sets it (`_bus/_bus.py:190`). This design leaves it so.
- **Evidence types.** `sent` carries the record's `to` and `recipients` in inspect_ai's typed `CommunicationRecord`, and `delivered` carries `recipients` in `CommunicationDelivery`, documented as "the agents it reached" (`_evidence.py:167`, `:204`). A rejection's `control` is a free string (inspect_ai `CommunicationRejection.control`).
- **Reads.** A channel's tool reports a result's records with `Bus.read(channel, records, depends_on=...)` (`_bus/_bus.py:233-268`), which writes the `read` event and the critical path's links.
- **The status line.** `Bus.status()` returns the messages channel's unread line, found by the state's type, in the member's own loop only (`_bus/_bus.py:328-338`). `send_message`, `shared_memory` and `swarm_wait` end with it; `list_members` ends with the messages line or "You have no unread messages." (`_channel/messages.py:332-335`).
- **Idle members.** After a commit, a `wake=True` record that would end a whole-board wait of an idle member's own loop is reported to the runtime as news for that member, which the controller sees as `MemberNews` (`_bus/_bus.py:309-320`). Metrics and the ledger read every channel's `sent`, `delivered` and `read` events generically, by channel name, and count rejections by control (`_metrics.py:311-337`, `_ledger.py:350-370`).

### Addressing and liveness

`resolve()` (`_bus/_address.py:25-61`) takes member names and `all`. It refuses a name that is not a member (`'#team-a' is not a member of the swarm; ...`), the sender itself, and a member that is `pending`, `ending` or `ended`; an `idle` persistent member is accepted. `all` is every other running member. Member names follow `[a-z][a-z0-9_-]{0,47}`, with `all` and `swarm` reserved (`_address.py:12`, `:19`), which the messages channel's `check()` enforces (`_channel/messages.py:102-107`). The messages channel's `validate()` requires at least one recipient (`_channel/messages.py:210-211`).

### `swarm_wait` and channels it does not know

`Waits._make_tool()` describes the wait from the channel states it waits on: the messages description when every one is the messages state, otherwise the shared-memory description with its `path` and `file_id` parameters (`_bus/_wait.py:356-363`, `wait_tool()` at `:442`). A wait result appends the status line unless the result is only the messages channel's news (`_bus/_wait.py:296-325`).

### Bridged members and custom channels

`swarm()` refuses a bridged member in a swarm with any channel that has records other than the built-in messages and shared memory, because `swarm_bridged_tools()` serves only those two (`_swarm.py:346-360`). A member that chooses its own name is accepted only beside the filesystem channel or none (`_swarm.py:366-377`).

### Channel arguments in the log

A list of pydantic models given to a `@swarm_channel` factory is logged as a list of their fields, and replay passes those dicts back to the factory: a factory given `[Topic(name="team-a", readers=["a", "b"], private=True)]` and `rate=(10, 30.0)` logs `{"topics": [{"name": "team-a", "readers": ["a", "b"], "private": True}], "rate": [10, 30.0]}` in the plan step, and `create_registry_object()` calls it again with the dict and the list. So a factory takes models or mappings, as `swarm()` takes `Member` records or mappings (`_swarm.py:250-252`).

### Who uses rooms today

Nothing in this repository: its channels with records are messages and the shared memory, and the lead-and-workers example runs a hub and spokes over the shared memory, waking every worker on every lead write (`examples/lead_and_workers.py:1-10`, `:89-94`). Outside it:

- **ORBIT** has rooms, readers, writers, private channels and edges, in its own channel ([below](#orbits-channel-what-is-general-and-what-is-orbits)). Its topology presets are a single agent, a star (`star_orchestrator_workers`) and meshes (`ORBIT: orbit/configs/topology_presets.py:1-13`).
- **Codex** has an optional message board with channels, threads, posts and subscriptions ([swarm.md](swarm.md#related-work-and-what-it-tells-us)), a vendor swarm's internal traffic that the bridged-swarms work maps, not this channel.

So the shapes this design serves are the room configurations ORBIT's experiments use and the topologies swarm.md's ablations name, declared statically by the author. Anything else is refused at construction ([Configuration](#configuration)).

### ORBIT's channel: what is general and what is ORBIT's

ORBIT's `ChannelConfig` is an id, an optional display name, `readers`, `writers` and `private` (`ORBIT: orbit/configs/communication.py:12-28`). A post goes to the channel's readers, minus the sender, intersected with the configured communication edges; with a policy configured, a post with no recipient left fails (`ORBIT: orbit/communication/runtime.py:230-252`). Edges are `CommunicationEdge(from_agent, to_agent)` pairs: `None` derives routes from membership and `[]` denies every cross-agent route (`ORBIT: orbit/configs/setup.py:223-227`, `:273-278`). `private` is a label: "Membership and directed routes enforce access; the label alone does not" (`ORBIT: docs/runtime-architecture.md:49-50`); the collusion scorer reads it to pick a coalition's private-channel messages (`ORBIT: orbit/scorers/collusion_scorer.py:89-94`). `channel_list` shows only the channels the caller reads or writes (`ORBIT: orbit/communication/tools.py:164-180`). Read cursors are per agent, session and channel; a read returns a bounded page after the cursor, and the caller's own posts too (`ORBIT: orbit/communication/delivery.py:81-145`, `runtime.py:281-297`). A post must fit one read and one inbox at its size limits (`delivery.py:179-201`).

| ORBIT feature | Here |
|---|---|
| Rooms with declared readers and writers | Topics ([Configuration](#configuration)) |
| Delivery by membership: readers that have not run yet get the post | Yes ([Delivery by membership](#delivery-by-membership)) |
| Communication edges, intersected with readers | Left out: a topic's readers and writers are its restriction, and a pair that must not talk is in no room together ([Alternatives considered](#alternatives-considered)) |
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
 │   their state; validate()                                                    │
 │ 2 liveness (sender), payload, storm controls (max_bytes, rate)               │
 │ 3 sent    4 seq, apply (each reader's unread), delivered, wake, idle news    │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Everything is inspect_swarm code; inspect_ai needs no change. The bus changes in three places, each small: a channel may resolve its own addresses ([Channel-resolved addresses](#channel-resolved-addresses)), a status line from more than one channel ([The status line](#the-status-line)) and a `swarm_wait` description that names topics ([Waiting](#waiting)).

### Configuration

```python
# src/inspect_swarm/_channel/topics.py
class Topic(BaseModel, frozen=True):
    """A topic: a room that its readers read and its writers post to."""

    name: str
    """The topic's name, addressed as `#<name>`: a lowercase letter, then at most 47 lowercase letters, digits, `_` or `-`, and not `c-` followed only by digits, which is reserved for swarm-assigned ids."""

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
- **Validation when `topics()` is called** (`ValueError`, naming the topic): at least one topic, each a `Topic` or a mapping validated into one; each name in the grammar and not in the reserved form `c-<digits>` (`c-3`; `c-red` and `orders` are valid), `topic name c-3 is reserved for swarm-assigned ids`; no two topics with one name; `readers` and `writers` either `"all"` or a non-empty list without repeats; `max_bytes`, `max_wait` at least 1 and `rate` as the messages channel checks them (`_channel/_params.py`).
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

A red member never learns that `#blue` exists. Topics do not restrict the messages channel: an arm whose teams must talk only in their rooms leaves direct messages out.

**A hub and spokes.** A lead gives each worker its own orders in a private topic and reads every report:

```python
workers = [f"worker-{i}" for i in range(1, k + 1)]
topics([
    *(Topic(name=f"orders-{w}", readers=[w], writers=["lead"], private=True, wake=True) for w in workers),
    Topic(name="reports", readers=["lead"], writers=workers, wake=True),
])
```

A post in `#orders-worker-2` wakes worker-2 alone, or, for a persistent worker held idle, gives the controller `MemberNews` for worker-2 alone, which the shared-memory version of the lead-and-workers example cannot do. Workers do not read each other's reports. The arm has no messages channel, since direct messages would let workers talk to each other.

**A pipeline.** Each stage hands its output to the next:

```python
stages = ["extract", "analyse", "write"]
topics([Topic(name=f"{a}-to-{b}", readers=[b], writers=[a], wake=True) for a, b in zip(stages, stages[1:])])
```

With a controller that starts each stage when the one before it ends, a stage finds its input unread when it starts, because the post reached it by membership while it was pending ([Delivery by membership](#delivery-by-membership)). With every stage started at once, a downstream stage waits for its input with `swarm_wait`.

### Records and addressing

A post is a `Record` with `channel="topics"`, `kind="post"`, `to=("#orders",)`, `recipients` the topic's readers other than the poster (in roster order), `routed_out=()` as for every record, `payload` the text, `op=None`, `wake` the topic's `wake`, and `reply_to` the id of a post the poster has posted or read, or `None`. The record is addressed, so `sent` names its readers in its typed `recipients` and `delivered` names whom it reached.

The `#` prefix keeps topic addresses apart from member names, as the address grammar reserved (`swarm-communication.md` [Addressing](swarm-communication.md#addressing)). A topic's address is its author-given name, not a swarm-assigned id such as `#c-3`: those ids were reserved for names a member chooses, which are payload and may not appear in status lines ([How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus), "Names a member chooses"); every topic here is named by the eval author and grammar-checked, as member names are, so its name is safe in fences, status lines and preambles, and a model reads `#orders` more reliably than `#c-3` ([Alternatives considered](#alternatives-considered)). Swarm-assigned ids stay reserved for topics members might create later (decision: Ransom, 2026-11-05).

#### Channel-resolved addresses

A channel state may resolve its own records' addresses. If the state of `draft.channel` has a method `resolve(sender: str, to: tuple[str, ...]) -> tuple[str, ...]`, step 0 of `deliver()` calls it in place of the member resolver; otherwise nothing changes. It returns the recipients or raises `ToolError`, before a record exists, as an unresolvable member address does today. It is synchronous and must not await, since steps 0 to 4 of `deliver()` run with no `await`, so the recipients agree with what `apply()` sees.

The protocol is public, `ResolvingState`, exported from `inspect_swarm` beside `Bus`, `Draft`, `Delivery`, `Caller` and `calling_member` (decision: Ransom, 2026-11-06, at the ORBIT team's request: their channel computes its own recipients and wants them in the standard `sent` and `delivered` records rather than only in `apply()`'s result). The topics state and ORBIT's channel use it ([swarm-orbit.md S2](swarm-orbit.md#s2-a-public-bus-api-for-channels)). The bus checks what `resolve()` returns: every name a member of the swarm, none twice, and not the sender. A violation is a bug in the channel's code and raises `ValueError` naming the channel, as a read naming a record the channel did not commit does. The recipients' state is not checked: pending, idle, ending and ended members are accepted. The sender's liveness check and the stopping rule are unchanged. S2's `Bus` protocol, `Draft` and `Delivery` are unchanged, and a channel without `resolve()` keeps the member resolver or posts unaddressed records.

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

The messages channel keeps its rule: a direct message to a pending, ending or ended member is refused, because an inbox is a member's and a message to someone who will never read it is a failed send; an explicitly addressed idle persistent member accepts it, and the `all` address selects only running members ([Addressing and liveness](#addressing-and-liveness)). A topic's history is the room's, and a reader that arrives later reads what was said before it came.

#### Private topics

A private topic exists only for its readers and writers. For every other member its name never appears in the member's preamble, `list_topics`, status lines, `swarm_wait` results or tool errors, and naming it gets the same error as a name that does not exist. A public topic is listed to every member, with its readers and writers, so members know the rooms they are not in and who is in them. The flag is in the logged configuration, so analysis can tell private traffic apart (ORBIT's collusion scorer uses its label for that).

Private here is a visibility rule, not an access rule: access is `readers` and `writers` in both cases, and the evidence holds every post, private or not, for whoever reads the log.

### The tools

All three are offered to the member's own loop only and refuse a call from another loop with a `ToolError`, as the messages channel's do (`_channel/messages.py:345-357`). Each ends with the status line ([The status line](#the-status-line)).

**`post_topic(topic: str, message: str, reply_to: str | None = None) -> str`**

- `topic`: a topic you post to, such as `orders` or `#orders`. `message`: the text. `reply_to`: the id of a post this one answers, such as `p-3`.
- Delivers `Draft("topics", "post", to=("#orders",), payload=message, wake=<the topic's wake>, reply_to=reply_to)`. `validate()` requires `kind == "post"`, no `op`, a non-empty payload, a recipient (`#<name> has no reader but you.` for a writer that is the topic's only reader), and a `reply_to` the poster has posted or read; `admit()` has nothing to check.
- Returns `Posted p-7 to #orders, read by worker-1, worker-2.`

**`read_topics(topic: str | None = None) -> str`**

- Without `topic`, the caller's unread posts in every topic it reads, in commit order; with it, those in that topic, which the caller must read (a topic it does not read is `you do not read #reports.`; a private one it is not in, the unknown-topic error).
- Packs whole posts up to the read budget, marks exactly those read, in the synchronous step that builds the result, and reports them with the bus's `Bus.read("topics", ids)`, as `read_messages` does, which writes the `read` event and the critical path's links ([How this sits on S2](#how-this-sits-on-s2)). Built with `max_output=0`, as `read_messages` is, so no output limit cuts a fence.
- Never blocks; a reader with nothing unread waits with `swarm_wait`.

**`list_topics() -> str`**

One line per topic the caller can see, in declaration order: `#orders-worker-2 (private): readers worker-2; writers lead; you read. 1 unread.`, `#reports: readers lead; writers worker-1, worker-2; you post.`, and, for a public topic the caller is not in, `#red: readers red-1, red-2, red-3; writers red-1, red-2, red-3; you are not in it.` `"all"` is shown as `all members`. It shows no peer text and writes no evidence, as `list_members` does.

#### Reading

Every accepted post fits one read, as every message does ([swarm-communication.md Direct messages](swarm-communication.md#direct-messages)):

- When the state opens, it computes the largest fence the run allows (the tag, the longest id `p-` plus 14 digits, the longest topic name, the longest member name as `from`, and a `reply_to` id), the largest header and remainder lines (counts of 14 digits), the largest other-topics line (every topic the run declares, each with a count of 14 digits), and the read budget, `header_max + envelope_max + max_bytes + trailer_max`, where `trailer_max` is the largest remainder line plus the largest other-topics line.
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

- One post is `1 post.`; nothing unread is `You have no unread posts.` (or `... in #orders.`), with no `read` event. With `topic`, the remainder line names the topic and the call, `2 more unread posts in #orders; call read_topics(topic="orders") again.`, and the result then ends with a line counting the caller's unread posts in its other topics, `You have 4 unread posts in other topics (#general 3, #red 1).`, whenever there are any, including after an empty read or one that drained the topic. An unfiltered read's remainder line counts everything left, so it needs no other line.
- Attribute values are record ids, author-given topic names and roster names, all grammar-checked, so they need no escaping (`_fence.py`).
- The fence is a label, not a boundary: a post is delivered as typed, and a member can write text that looks like another member's post or a status line (decision: Ransom, 2026-10-27; [swarm-communication.md Fencing](swarm-communication.md#fencing)). Evidence comes from bus events and ids, never from result text.

### Storm controls

| Control | Default | On violation |
|---|---|---|
| `max_bytes` per post, UTF-8 | 8,000 | rejected: `post is N bytes; the limit is 8000` |
| `rate` per member, across its topics | 20 per 60 s, sliding window | rejected: `rate limit: retry in N s` |

Both are the bus's `StormControls`, so the bus applies them and records their refusals as it does for messages. The channel has no `max_unread` and no `dedupe`: those are back-pressure on an inbox the recipient must be running to drain, and under membership delivery a pending or ended reader never drains its unread posts, so a cap on them would close a topic to every writer for good. `rate` bounds what any one member can add, and a reader's unread posts are read a budget at a time.

### The status line

`Bus.status()` returns the calling loop's unread counts, and nothing in a nested loop, which reads neither messages nor posts. Its output always **ends with one short summary line** holding every total, so that Inspect's tool output truncation, which keeps the start and the end of a result, keeps the totals whatever comes before them:

- **Without the topics channel, or with no unread posts**, it is today's messages line, `You have 2 unread messages.`, or nothing: every output of a swarm without topics is unchanged.
- **With unread posts**, it is two lines: first the per-topic detail, `Unread posts: #orders-worker-2 3, #general 1.`, then the summary, `You have 2 unread messages and 4 unread posts.`, or `You have 4 unread posts.` when no message is unread. The detail comes first because it grows with topic names (up to 48 bytes each); the summary does not, and is at most 50 bytes while each count is below 1,000, so a `max_tool_output` of 100, which keeps the last 50 bytes, keeps both totals.

The tools that end with the status line today (`send_message`, `shared_memory`, `swarm_wait`) and the topics tools end with it; `list_members` ends with it, or with its zero-count line `You have no unread messages.` when it is empty, as today; `read_topics` ends with its remainder line and, after a read of one topic, the other-topics line instead ([Fencing](#fencing)), both inside its read budget and never truncated (`max_output=0`), and `read_messages` with its own remainder, as today. Tools of other channels, a user channel's or Inspect's, carry no count (decision: Ransom, 2026-10-30).

The detail line holds counts and author-given topic names the caller reads, never a payload. Topics are listed in declaration order, so the line is at most one entry per topic the author declared.

### Waiting

The topics state is a `WaitingState`:

- **`wakes(spec, record)`**: the wait is the member's own loop and the member is one of the record's recipients. The bus calls it only for records with `wake` set, so only posts in `wake=True` topics end a wait.
- **`news(spec)`**: in the member's own loop, when any unread post is from a `wake=True` topic, one line counting every unread post and naming topics and posters: `4 unread posts in #orders-worker-2, #general (from lead, worker-1). Call read_topics() to read them.`; its `depends_on` is every unread post it counts, those of `wake=False` topics included, as the messages channel's news counts quiet messages ([swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance)). Without a waking unread post it has no news, and a wait that returns for another reason ends with the status line's count, which links nothing.
- **Idle persistent members.** The bus's existing check reports a `wake=True` post to an idle reader as news, so the controller gets `MemberNews(member, sender, ...)`, and `MemberIdle.news` is set while such a post stays unread. A post in a `wake=False` topic waits for the reader's next activation.

Two changes in `_bus/_wait.py`:

- **The description.** `wait_tool()` takes a `topics` flag beside `messages` and `board`, and `_make_tool()` sets it when a topics state is among the channels waited on. The description's first sentence names each source the wait watches (an unread message sent with `wake=True`, an unread post in a topic that wakes its readers, a shared-memory change, a member's end), and its acknowledgment sentence names each read tool. `board` stays true for any channel state other than messages and topics, as today, so a custom waiting channel's wait is described as it is now.
- **The trailing count.** Today a wait result ends with the messages status line unless the result is only the messages channel's news, so truncating the middle of a longer result keeps the count (`_bus/_wait.py:321-325`, [docs/messages.qmd](../docs/messages.qmd)). The rule is kept, with the status output above in place of the messages line: after the news, member-end and release lines are built, the status output is appended unless every one of those lines is one counting channel's own news and no other counting channel has an unread record. So a result that is only message news ends with the status output when posts are unread, and a result with message and topic news ends with it, its summary last; a result that is only one channel's news, with nothing unread elsewhere, ends with that news, whose line leads with its count. With messages alone this is exactly today's rule, and the existing truncation test (`test_a_wait_with_other_news_ends_with_the_unread_count`) passes unchanged.

`swarm_wait` is offered with the topics channel even when no topic wakes: it still returns on a member's end, when every member is waiting, at the stop and at its timeout, and a member may wait for those.

### Instructions

The channel's `instructions(member)` is a fixed template with only author-given names: what the three tools do; that posts are shown between `<topic_post>` tags whose `from` attribute names the poster; the member's own topics with its access, `You read and post in #general; you read #orders-worker-2; you post in #reports.`, or `You are in no topic.`; for topics it reads that wake, `A post in #orders-worker-2 ends swarm_wait.`; and that the results of the swarm's tools count unread posts. Public topics the member is not in are left to `list_topics`, and private ones it is not in are never named. It names no other member.

### Evidence

No new event or field. Posts write `sent`, `delivered` and `read` events with `channel="topics"`, as messages do ([swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance)): `sent` with the record, its `to` (`["#orders"]`) and its `recipients`, with `routed_out` empty in its data as for every record; `delivered` with the recipients and the commit sequence; `read` with the posts a read showed. A wait stays one bus-wide `waited` event with `channel=None`, as today (`_evidence.py:270-280`), with `"topics"` in its `wait.channels` when it watches the topics channel, and its `depends_on` naming the posts its news showed; the ledger's wait path reads it unchanged (`_ledger.py:330-342`). The communication metrics, the ledger and the critical path read them by channel name, so they count topics with no change ([Current behaviour](#the-bus)). Analysis reads a topic's privacy, readers and writers from the logged channel configuration.

### How this sits on S2

The topics channel uses the public `deliver(Draft)`, `Bus.read()` and `ResolvingState` operations. As a built-in channel, it also uses three in-package integrations: the member/loop and lifecycle guards shared with messages, the status lines, and the `swarm_wait` description. The status integration includes the unread counts used by the wait's trailing-count rule. ORBIT's channel resolves its own recipients and reports reads with the public `Bus.read()`; it does not use the built-in status lines.

## Alternatives considered

**Unaddressed posts, with recipients the channel computes in `apply()`.** S2's pattern for ORBIT, needing no bus change. But the `sent` event would name no recipients, `delivered`'s typed `recipients`, "the agents it reached", would be empty for every post, and a reader's unread posts would be the channel's own bookkeeping beside the bus's records. Rejected; ORBIT's channel resolves its recipients with `resolve()` too, so that its `sent` and `delivered` records name them.

**A `recipients="roster"` class attribute**, lifting the liveness check for a channel's addresses. swarm-orbit.md rejected it for ORBIT. It is not enough here either: `#orders` still has to resolve to the topic's readers, which only the channel knows. The resolver hook does both.

**Posting to topics through `send_message(to="#orders")`.** One fewer tool. But one inbox would mix two delivery rules (liveness for members, membership for topics), the inbox's `max_unread` back-pressure would close a topic as soon as a pending reader had 50 unread posts, and an arm with topics and without direct messages would need a messages channel that refuses member addresses. Rejected.

**Swarm-assigned ids (`#c-3`).** What swarm-communication.md reserved, for names a member chooses. Every topic here is declared and named by the author, so the name is as safe as a member name, and a model would have to map numbers to rooms. Rejected; ids remain the form for topics created at run time, if those are ever built.

**Wake set per post by the model** (`post_topic(wake=True)`), as `send_message` does. But whether a room interrupts its readers' waits is the experiment's design, not the poster's choice, and a per-post flag lets a chatty member end every reader's wait. Rejected for a per-topic setting the author logs.

**Member-pair routes on the bus**, `swarm(routes=[(sender, recipient), ...])`, splitting every addressed record's recipients into those the sender may reach and `routed_out`, and refusing a send that reaches nobody, so one rule would govern messages and topics. Rejected (decision: Ransom, 2026-11-05): channels may support different topologies, so how a restriction is specified is the channel's; a swarm with several channels may be rare, and where there are several an experiment may want different communication on each; and some channels need no restriction at all. Topics' readers and writers express every topology above.

**Routes for the messages channel** (`messages(routes=...)`). Would let an arm keep direct messages under a topology. Not needed for ORBIT, whose channel computes its own recipients; left out, and a later design can add it if an experiment needs it ([Not this design](#not-this-design)) (decision: Ransom, 2026-11-05).

**Communication edges inside a topic**, ORBIT's edges intersected with a topic's readers. A room's readers are its audience; a pair that must not talk is put in no room together, which the hub-and-spokes example does with a topic per worker. Rejected as a second restriction on one room.

**ORBIT's `private` as a label only.** Simpler. But a public and a private topic would then behave identically in the swarm, and a team experiment could not keep one team's room unknown to the other. Rejected for a visibility rule that the label also records.

**Rooms as shared-memory files** (`/shared_memory/rooms/red.md`). No new channel. But the board has no readers or writers, no privacy, no per-reader unread posts, and wakes every waiter of a path on every write. Rejected.

**A `max_unread` or a total post cap per topic.** Bounds unread growth. But a reader that never runs would close the topic for every writer; `rate` already bounds what a member adds. Rejected.

## Compatibility and migration

- **Stored formats.** No new event type or field: posts write today's communication events, with a new channel name, kind (`post`) and id prefix (`p`); `routed_out` stays empty. The viewer's generated types and the swarm's evidence version are unchanged.
- **Public API.** New: `topics`, `Topic` and `ResolvingState`. Without topics, every tool's schema, description and output is unchanged: the status line, the wait description and its trailing count behave as today.
- **Existing swarms with the messages channel** gain nothing they did not ask for.
- **Bridged members** are refused beside `topics` by today's rule, with today's message naming the channel.
- **ORBIT's port** posts addressed records resolved by its channel's `resolve()` in place of unaddressed ones ([swarm-orbit.md S2](swarm-orbit.md#s2-a-public-bus-api-for-channels)).
- **inspect_ai**: no change.

## Security

- **Untrusted input reaching the new code:** the post text, the `topic` argument and `reply_to`, all model output, and through the bridge none (bridged members are refused).
  - The text is stored and delivered as typed, inside a fence that is a label, never a boundary; it is bounded by `max_bytes`, and a lone surrogate is refused by the bus's existing check. It never reaches a status line, a wait result, a preamble or an error.
  - `topic` is checked against the topics the caller can see; any other value gets the same error, which lists only those topics, so the error reveals no private topic. `reply_to` must be an id the caller has posted or read.
- **Sender identity** is bound by the bus from the member context, as for every record; no argument sets it.
- **Private topics** are hidden from non-members in every text the swarm writes for them: preamble, list, status line, wait result, errors, and a post's result, which only a writer, a member of the topic, receives. The log holds everything, for its readers.
- **Topics are the author's**, validated at construction; no model output changes them.
- **What topics do not stop**: communication through the messages channel, shared sandboxes, shared state or shared tool instances. Members that share a sandbox, the shared memory or a shared tool instance can still talk through it, as swarm-communication.md says of messages ([The filesystem channel](swarm-communication.md#the-filesystem-channel)); an arm whose topology must be the only path gives members their own sandboxes and leaves the messages channel and shared state out.

## Testing

All async tests are `@pytest.mark.anyio` and run on asyncio and trio through the repository's fixture; members are mockllm `react()` agents with scripted tool calls. Nothing needs network, Docker or a model provider.

- **`tests/test_topics_channel.py`** (construction): the default is one public `#general` for all; each `ValueError` of `topics()` and `check()` with its message (bad and repeated names, a reserved `c-3` given as a `Topic` and as a mapping beside an accepted `c-red`, empty or repeated readers, an unknown member, a topic no post could leave, bounds); a list of `Topic`s and a list of mappings give the same channel; logging and replay round-trip, and two arms that differ in one topic's `private` or `wake` have different task identifiers (in `tests/test_api_log.py`); instructions name the member's own topics and access and no private topic it is not in; `swarm()` refuses a bridged member and a member with `choose_name=True` beside `topics`.
- **`tests/test_topics.py`** (runtime):
  - a post reaches readers and not the poster; a reader's `read_topics()` returns it fenced, marks it read, and writes a `read` event with its id; `sent` and `delivered` name `to=["#orders"]` and the readers;
  - membership delivery: a post to a pending reader is unread when a controller starts it later; to an ended reader, delivered and never read; a post by a member that has ended is refused with liveness evidence;
  - permissions: a non-writer's post, a read of a topic the caller does not read, and an unknown topic each give their error with no record; a private topic is absent from a non-member's preamble, `list_topics`, status lines and errors, and naming it gives the unknown-topic text exactly;
  - reading: across topics in commit order; with `topic`, only that topic's, and the result ends with the other-topics line after a partial, an empty and a draining read; the read budget with the longest names, every topic in the other-topics line and a payload at `max_bytes` fits one read; packing stops at the budget and the remainder line counts the rest; `max_output=0` with a small `max_tool_output`;
  - storm controls: `max_bytes` and `rate` refusals with their controls in `sent`;
  - the status line on `post_topic`, `list_topics`, `send_message`, `list_members`, `shared_memory` and `swarm_wait`, with messages and topics both open in both orders of `channels=`, and none in a deepagent's `general()` subagent, which gets none of the topics tools; the summary line is last; with 48-character topic names and `max_tool_output=100`, the truncated result of each of those tools still holds both totals, in both channel orders; with topics open and no unread post, and with no topics channel, each output is today's;
  - waiting: a post in a `wake=True` topic ends a reader's wait and names topics and posters; a `wake=False` post does not, and is counted when the wait ends for another reason; `depends_on` of a wait with news includes the quiet posts; a persistent reader held idle gets `MemberNews` for a waking post and not for a quiet one; the wait description with topics alone, with messages, and with the shared memory; the trailing counts (`tests/test_wait.py`, table-driven): only message news with unread posts ends with the topics count, message and topic news ends with both, only topic news ends with the messages count, and the existing messages-only truncation test is unchanged.
- **`tests/test_bus.py`**: a channel written only with public names whose state resolves its own addresses gets those names as the typed `recipients` of `sent` and `delivered`, whatever the recipients' state; a `ToolError` from `resolve()` leaves no record; a returned non-member, duplicate or the sender raises `ValueError`; a state without `resolve()` keeps the member resolver.
- **`tests/test_metrics.py`**: topics counted per member like messages.

## Implementation plan

1. **The topics channel.** `src/inspect_swarm/_channel/topics.py` (`Topic`, `topics()`, its validation, `check()`, `instructions()`, `TopicsState` with `resolve()`, `validate()`, `admit()`, `apply()`, the three tools, the read budget, the status line and the `WaitingState` methods); `_channel/_channel.py` (`ResolvingState`, public); `_bus/_bus.py` (step 0 calls a state's `resolve()` and checks what it returns; `status()` composes the topics detail line and the summary line); `_bus/_wait.py` (`topics` in `wait_tool()` and `_make_tool()`, the per-channel trailing count; the status output in place of the messages line); `_channel/messages.py` (`list_members` adds the topics line); `src/inspect_swarm/__init__.py` (`Topic`, `topics`, `ResolvingState`) and the entry point's imports; `docs/topics.qmd` (the topologies above as examples), `docs/channels.qmd` (the list of built-in channels, and a channel resolving its own addresses), `docs/_quarto.yml`. Tests: `test_topics_channel.py`, `test_topics.py`, the resolver cases of `test_bus.py`, `test_wait.py`, `test_api_log.py` and `test_metrics.py`.

## Open questions

None.

## Not this design

- **Topics created by members at run time** (ORBIT's attack-created `secret:` channels, Codex board subscriptions). They would need swarm-assigned ids, since a member's name for a topic is payload, and a rule for who may join.
- **Rereading a topic's history** with a cursor, as ORBIT's `after_sequence` does, and showing a poster its own posts.
- **Bridged members on topics**: declarations for `swarm_bridged_tools(["topics"])` and its `swarm_wait`.
- **`swarm_wait` describes a custom waiting channel as the shared memory**, with its `path` and `file_id` parameters (`_bus/_wait.py:356-363`). A user channel that wakes waits needs its own wait description and filter; this design leaves that rule as it is.
- **A hub-and-spoke version of the lead-and-workers example** on topics, waking only the worker a task is for.
- **Restrictions for the messages channel**, such as `messages(routes=...)`. Each channel decides its own; the messages channel can add one if an experiment needs direct messages under a topology (decision: Ransom, 2026-11-05).
