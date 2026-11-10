# Inspect Swarm: the topics channel

## Overview

With `channels=[topics([...])]`, the eval author declares named rooms, **topics**, each with the members that read it, the members that write it and whether a post wakes its readers. Members get three tools in their own loop: `post_topic` posts to a topic, `read_topics` returns the posts the caller has not read yet, oldest first and fenced with the poster's name, and `list_topics` lists the caller's topics and who reads and writes each. A post is addressed to the topic (`#team-a`) and reaches every reader by membership, not activity: a reader that has not started yet, or one held idle, finds the post unread when it next looks, as on a blackboard. Every post goes through the swarm's bus like a message, so it gets the bus's sender binding, storm controls and `sent`, `delivered` and `read` evidence, and the swarm's own tools end with lines counting the caller's unread posts. Posts end a reader's `swarm_wait` only in topics the author marks `wake=True`. A topic exists only for its readers and writers: for any other member its name never appears in the member's preamble, lists, count lines or errors.

Who may talk to whom in a topic is its `readers` and `writers`; there are no member-pair routes, on the bus or for the messages channel, which lets every member message every other. With topics alone, an experiment runs team rooms, a hub and spokes, or a pipeline of stages as configuration, with identical members across arms. `topics()` with no arguments is one room, `#general`, that every member reads and writes. The bus finds a post's recipients by asking the channel: a channel state with a `resolve()` method (the public `ResolvingState` protocol) resolves its own addresses in place of the member resolver, which ORBIT's channel uses too.

The code is `Topic`, `topics()` and `TopicsState` in [`_channel/topics.py`](../src/inspect_swarm/_channel/topics.py); the `ResolvingState` protocol in [`_channel/_channel.py`](../src/inspect_swarm/_channel/_channel.py); the address hook in `SwarmBus.deliver()`, with `_check_recipients()`, and the composed count lines in `SwarmBus.status()` and `SwarmBus.unread()`, in [`_bus/_bus.py`](../src/inspect_swarm/_bus/_bus.py); the wait's topics description and trailing-count rule in [`_bus/_wait.py`](../src/inspect_swarm/_bus/_wait.py); the shared own-loop guard `own_caller()` and name check `check_member_names()` in [`_channel/messages.py`](../src/inspect_swarm/_channel/messages.py); and read packing, `pack()`, in [`_fence.py`](../src/inspect_swarm/_fence.py), shared by both channels. The user docs are [Topics](../docs/topics.qmd), which owns the tools' text, the topologies, the limits and the record of posts, and [Writing a channel](../docs/channels.qmd#writing-a-channel) for `ResolvingState`. The tests are `tests/test_topics.py`, `tests/test_topics_channel.py`, and the topics and resolver cases in `tests/test_bus.py`, `tests/test_wait.py` and `tests/test_persistent.py`.

## Background

A deep dive under [swarm-communication.md](swarm-communication.md), which designs the bus and anticipates this channel: "A board post is `kind="post"` addressed to a channel id whose subscribers are the recipients; the channel id is resolved by the board's resolver" ([How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus)), with `#`-prefixed channel ids reserved in the address grammar ([Addressing](swarm-communication.md#addressing)). It sits beside the messages channel (the same design) and the shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md)), and uses `swarm_wait` ([swarm-wait.md](swarm-wait.md)), persistent members' news ([swarm-persistent-members.md](swarm-persistent-members.md#news-for-idle-members)) and the public bus API ([swarm-orbit.md S2](swarm-orbit.md#s2-a-public-bus-api-for-channels)).

It keeps the standing decisions of those designs. The ones it leans on most: peer text reaches a member only as tool output, fenced, with the sender the bus bound (decision: Ransom, 2026-10-07); the swarm adds no message to a member's conversation, and members learn of changes from tools (2026-10-27); the count lines end the results of the swarm's own tools only (2026-10-30); a design serves the shapes real use has and refuses the rest loudly (2026-10-08).

Paths prefixed `ORBIT:` are in ORBIT's repository ([github.com/wlanderson0/orbit](https://github.com/wlanderson0/orbit)); the others are in this one.

## Why

The swarm's other channels with records do not give an experiment a room:

- **Messages are addressed per send and gated by activity.** Every `send_message` names its recipients, and the member resolver refuses a recipient that has not started, is ending or has ended. A group that should share one conversation (a team, a stage's output) has no name, so every member must be told who is in it, and a member a controller starts later never receives what was said before it started.
- **The shared memory is one board for everyone.** Every member and subagent reads and writes every file; it has no readers and writers, no private area, and it wakes every waiter of a path on every write. The lead-and-workers example says so in its worker prompt: "Every worker is woken when the lead writes, whichever worker the task is for" ([`examples/lead_and_workers.py`](../examples/lead_and_workers.py)).
- **Neither says who may talk to whom.** The messages channel lets every member address every other ([swarm-communication.md Configuration](swarm-communication.md#configuration)), and the shared memory lets every member read every file.

The research questions swarm.md lists ask for exactly these as configuration: "Which topology and channels matter? Ablations: leaderless against coordinator, filesystem only against messages, task list or board, one mechanism removed at a time", with "topology and channels as configuration, not code; identical members across arms" ([swarm.md](swarm.md#eval-questions-and-the-experimental-design-they-imply)). ORBIT, the security framework built on Inspect, runs its topology experiments on channels with declared readers and writers.

## The shapes it serves

The channel serves the room configurations ORBIT's experiments use and the topologies swarm.md's ablations name, all declared statically by the eval author: a common room, team rooms, a hub and spokes, and a pipeline. [Topics, Topologies](../docs/topics.qmd#topologies) gives each as configuration. These are examples, not a classification: the channel does not inspect the graph the topics form, so a cycle of stages or any other arrangement of rooms is accepted. What construction refuses is malformed declarations, names outside the grammar or reserved, members that are not in the roster, and a topic no post could leave ([Configuration](#configuration)), each with a `ValueError`.

ORBIT's own channel is the closest prior art. Its `ChannelConfig` is an id, an optional display name, `readers`, `writers` and `private` (`ORBIT: orbit/configs/communication.py`). A post goes to the channel's readers, minus the sender, intersected with the configured communication edges (`ORBIT: orbit/communication/runtime.py`); edges are `CommunicationEdge` pairs, where `None` derives routes from membership and `[]` denies every cross-agent route (`ORBIT: orbit/configs/setup.py`). `private` is a label, "Membership and directed routes enforce access; the label alone does not" (`ORBIT: docs/runtime-architecture.md`), which the collusion scorer reads to pick a coalition's private-channel messages (`ORBIT: orbit/scorers/collusion_scorer.py`). `channel_list` shows only the channels the caller reads or writes, and reads take an optional cursor for rereads (`ORBIT: orbit/communication/tools.py`). Its topology presets are a single agent, a star and meshes (`ORBIT: orbit/configs/topology_presets.py`).

| ORBIT feature | Here |
|---|---|
| Rooms with declared readers and writers | Topics ([Configuration](#configuration)) |
| Delivery by membership: readers that have not run yet get the post | Yes ([Delivery by membership](#delivery-by-membership)) |
| Communication edges, intersected with readers | Left out: a topic's readers and writers are its restriction, and a pair that must not talk is in no room together ([Alternatives considered](#alternatives-considered)) |
| Capacity: every accepted post fits one read | Yes, by bytes, as the messages channel does ([Reading](#reading)) |
| `private`, a label the collusion scorer reads | No counterpart: every topic is visible only to its members ([Who sees a topic](#who-sees-a-topic)) |
| Per-reader read cursors | Per-reader unread posts, per member: a member's own loop is the one reader |
| NDJSON read format, store state and counter, `step_start` cutoffs, attack-created `secret:` channels, `notify`/`auto` delivery and dialogue projections, exposure | ORBIT's own channel; not here |
| Rereads from an explicit cursor, the caller's own posts in its reads, paged `channel_list` | Left out: no use here needs them ([Not this design](#not-this-design)) |

ORBIT keeps its own channel on the public bus API and resolves its posts' recipients with `ResolvingState` ([swarm-orbit.md The channel](swarm-orbit.md#the-channel)).

## At a glance

```
 lead (react)                                        worker-2 (react, pending)
 post_topic(topic="orders", message="...")           ... starts later ...
        │ Draft(channel="topics", kind="post",       read_topics() ──▶ <topic_post id="p-1" topic="#orders" from="lead">
        │       to=("#orders",), wake=True)                           ...
        ▼                                                             </topic_post>
 ┌──────────────────────────── Bus.deliver() ───────────────────────────────────┐
 │ 0 bind sender; TopicsState.resolve() maps #orders to its readers, whatever   │
 │   their state; the bus checks them; validate()                               │
 │ 2 liveness (sender), payload, storm controls (max_bytes, rate), admit()      │
 │ 3 sent    4 seq, apply (each reader's unread), delivered, waits, idle news   │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Everything is inspect_swarm code; inspect_ai has no part of its own in the channel.

## Configuration

```python
# src/inspect_swarm/_channel/topics.py, exported from inspect_swarm
class Topic(BaseModel):  # model_config: frozen=True, extra="forbid"
    name: str
    readers: Literal["all"] | list[str] = "all"
    writers: Literal["all"] | list[str] = "all"
    wake: bool = False


@swarm_channel
def topics(
    topics: Sequence[Topic | Mapping[str, object]] = (Topic(name="general"),),
    max_bytes: int = 8_000,                                         # UTF-8 bytes per post
    rate: tuple[int, float] | list[int | float] | None = (20, 60.0),  # posts per member per sliding window (s), across topics
    max_wait: int = 120,                                            # the channel's longest swarm_wait
) -> Channel: ...
```

- **The string `"topics"`** in `channels=` gives the default, one `#general` that every member reads and writes, with no wake; `-T channels=filesystem,topics` selects it from the command line. `topics` is registered through the entry point beside the other built-in channels.
- **Logging and replay.** A list of pydantic models given to a `@swarm_channel` factory is logged as a list of their fields, so two arms that differ in any topic are distinct, and replay passes the logged dicts back. `topics()` therefore takes `Topic`s or mappings, as `swarm()` takes `Member` records or mappings, and validates each into a `Topic`; `extra="forbid"` refuses an unknown field, such as ORBIT's `private`. A `Topic` instance is validated again from its fields, since a model can be built without its checks.
- **Validation when `topics()` is called** (`ValueError`, naming the topic): `topics` a list or tuple with at least one item, each a `Topic` or a mapping; each name in the member-name grammar (a lowercase letter, then at most 47 lowercase letters, digits, `_` or `-`) and not of the form `c-<digits>`, which is reserved for swarm-assigned ids (`c-3` is refused; `c-red` and `orders` are valid); no name twice; `readers` and `writers` either `"all"` or a non-empty list without repeats; `max_bytes` and `max_wait` at least 1 and `rate` checked as the messages channel checks it (`_channel/_params.py`).
- **`check(roster)`** at `swarm()` (`ValueError`): every member name in the grammar and not `all` or `swarm` (`check_member_names()`, shared with the messages channel, since names appear in fences); every name a topic lists is a member; and each topic has a writer with a reader other than itself, since otherwise no post to it could reach anyone.
- **Channel attributes.** `name = "topics"`, `prefix = "p"` (record ids `p-1`, `p-2`), `reach = "loop"`: a read marks posts read for the member, so a member's topics have one reader, its own loop, as its inbox does ([Writing a channel](../docs/channels.qmd#writing-a-channel)). `storm = StormControls(max_bytes=max_bytes, rate=rate)`; `max_wait` as given, so the channel always comes with `swarm_wait`.
- **Refused combinations**, by `swarm()`'s general rules: a bridged member beside `topics`, since `swarm_bridged_tools()` serves only the messages and shared-memory channels, and a member with `choose_name=True`, which the swarm supports only beside the filesystem channel or none. Each error names the channel.

## Records and addressing

A post is a `Record` with `channel="topics"`, `kind="post"`, `to=("#orders",)`, `recipients` the topic's readers other than the poster, in roster order, `routed_out=()`, `payload` the text, `op=None`, `wake` the topic's `wake`, and `reply_to` the id of a post the poster has posted or read, or `None`. The record is addressed, so `sent` names its readers in its typed `recipients` and `delivered` names whom it reached.

The `#` prefix keeps topic addresses apart from member names, as the address grammar reserved ([swarm-communication.md Addressing](swarm-communication.md#addressing)). A topic's address is its author-given name, not a swarm-assigned id such as `#c-3`. Those ids were reserved for names a member chooses, which are payload and may not appear in count lines ([How channels plug into the bus](swarm-communication.md#how-channels-plug-into-the-bus)); every topic here is named by the eval author and grammar-checked, as member names are, so its name is safe in fences, count lines and preambles, and a model reads `#orders` more reliably than `#c-3`. Swarm-assigned ids stay reserved for topics members might create later (decision: Ransom, 2026-11-05).

### Channel-resolved addresses

A channel state may resolve its own records' addresses. If the state of `draft.channel` has a callable `resolve(sender: str, to: tuple[str, ...]) -> tuple[str, ...]`, step 0 of `deliver()` calls it in place of the member resolver, for every draft of the channel, `to=()` included, before a record exists; otherwise the member resolver runs. `resolve()` returns the recipients or raises `ToolError`, which refuses the draft with no record and no evidence, as an unresolvable member address does. It is synchronous and must not await, since `deliver()` runs from it to `apply()` with no `await`, so the recipients agree with what `apply()` sees.

The protocol is public, `ResolvingState(ChannelState, Protocol)`, exported from `inspect_swarm` beside `Bus`, `Draft`, `Delivery`, `Caller` and `calling_member` (decision: Ransom, 2026-11-06, at the ORBIT team's request: their channel computes its own recipients and wants them in the standard `sent` and `delivered` records rather than only in `apply()`'s result). The bus checks what `resolve()` returns in `_check_recipients()`: every name a member of the swarm, none twice, and not the sender. A violation is a bug in the channel's code and raises `ValueError` naming the channel, as a read naming a record the channel did not commit does. The recipients' state is not checked: pending, idle, ending and ended members are accepted. The sender's liveness check and the stopping rule apply as to every record. [Writing a channel](../docs/channels.qmd#writing-a-channel) has an example of a channel of rooms written with the public names.

`TopicsState.resolve()`:

1. `to` must be exactly one address beginning with `#`; otherwise `post to one topic at a time, such as #orders.`
2. The topic must exist and the sender read or write it; otherwise the unknown-topic error ([Who sees a topic](#who-sees-a-topic)).
3. The sender must be a writer; otherwise `you cannot post to #<name>; its writers are lead.`
4. The recipients are its readers other than the sender, **whatever their state** ([Delivery by membership](#delivery-by-membership)).

`post_topic` accepts `topic="orders"` or `topic="#orders"` and always addresses `#<name>`. The state's `validate()` then requires `kind == "post"` with no `op`, a non-empty payload (`post is empty.`), at least one recipient (`#<name> has no reader but you.`, for a writer that is the topic's only reader) and a `reply_to`, if given, that the poster has posted or read. These refusals come before the record has an id, so they leave no evidence. `admit()` has nothing to check. `apply()` appends the record to each recipient's unread posts, adds its id to the poster's known ids, and returns the recipients as `notify`, with no result mapping.

### Delivery by membership

A post reaches every reader the topic lists, not only those running when it is posted. The poster must be running (the bus's liveness check, with evidence), and nothing is delivered once the swarm is stopping; only the recipients' state is not checked, because a topic is a room and its readers are its members, not the members active at the moment. So:

- a **pending** reader, not yet started by its controller, finds the post unread when it starts;
- an **idle** persistent reader finds it unread when woken, and a post in a `wake=True` topic is news the controller sees as `MemberNews` ([Waiting](#waiting));
- an **ended** reader has it unread for good. It is still a recipient in the evidence, which says the record was delivered to its unread posts, not that it was read; a `read` event says that.

The messages channel keeps its own rule: a direct message to a pending, ending or ended member is refused, because an inbox is a member's and a message to someone who will never read it is a failed send. A topic's history is the room's, and a reader that arrives later reads what was said before it came.

### Who sees a topic

Visibility follows membership: a topic exists for a member only if the member reads or writes it. For every other member its name never appears in the member's preamble, `list_topics`, count lines, `swarm_wait` results, post results or tool errors, and naming it gets the same error as a name that does not exist: `no topic #<name>; your topics are #a, #b.` (or `you are in no topic.`), which lists only the caller's own topics. There is no public or private flag (decision: Ransom, 2026-11-09: "make it all private and drop the flag"), so members never learn of rooms they are not in or who is in them.

This is a visibility rule, not an access rule: access is `readers` and `writers`, and the evidence holds every post for whoever reads the log. The logged configuration has every topic with its readers and writers, so analysis can tell one room's traffic from another's.

## The state and the tools

`TopicsState` keeps, per run: the topics in declaration order, with their readers and writers resolved against the roster (`"all"` is every member) in roster order; each member's unread posts, one list of records in commit order; and each member's known ids, the posts it has posted or read, which its replies may name. A poster is never a recipient of its own post, so its own posts are never unread for it.

The three tools are offered to the member's own loop only (`reach = "loop"`) and, through `own_caller()`, refuse a call from a nested loop with a `ToolError`, as the messages channel's tools do. `read_topics` and `list_topics` also refuse a member that is no longer running; `post_topic` does not, so an ended member's post reaches the bus and is refused there with liveness evidence. All three are `parallel=True`. Their arguments, results and messages are in [Topics, The tools](../docs/topics.qmd#the-tools); what the code adds:

- **`post_topic(topic, message, reply_to=None)`** delivers `Draft("topics", "post", to=("#<name>",), payload=message, wake=<the topic's wake>, reply_to=reply_to)` and returns the post's id and its readers, followed by the count lines.
- **`read_topics(topic=None)`** reads every topic the caller reads, or one: a named topic must pass the visibility check and then be one the caller reads (`you do not read #reports.` for a topic it only writes). It never blocks.
- **`list_topics()`** lists the caller's topics in declaration order with their configured readers and writers (`"all"` shown as `all members`), the caller's access and its unread count there, followed by the count lines. It shows no peer text and writes no evidence, as `list_members` does.

### Reading

Every accepted post fits one read, as every message does ([swarm-communication.md Direct messages](swarm-communication.md#direct-messages)). When the state opens it computes the read budget: the largest header (`N posts, oldest first.` at a 14-digit count), the largest fence the run allows (with the id `p-` plus 14 digits, the longest topic name, the longest roster name as `from`, and a `reply_to` of the longest id) with an empty payload, `max_bytes`, and the largest trailer, which is the larger remainder line (unfiltered, or for the longest topic) plus the other-topics line for every declared topic at a 14-digit count.

A read is synchronous from the unread list to the mark, so what is shown is exactly what is marked. It selects the caller's unread posts, filtered by topic for a read with `topic`, and `pack()` takes the oldest whole and then each next fenced post while the total stays within the budget; the first always fits, because the storm control bounded its payload by `max_bytes` when it was posted. `pack()` draws fenced posts lazily, so a read of a long backlog renders at most one post past what it shows. The read removes those posts from the unread list, adds them to the caller's known ids, and reports them with `Bus.read("topics", ids)`, which writes the `read` event and the critical path's links. A read with nothing to show writes no event. The result is the header, the fences, a remainder line when posts of the selection remain, and, for a read of one topic, a line counting the caller's unread posts in its other topics whenever there are any, including after an empty or a draining read. The tool is built with `max_output=0`, as `read_messages` is, so no output limit cuts a fence, and it carries no count lines: its own lines count what remains.

### Fencing

Each post is a `<topic_post>` fence with the attributes `id`, `topic` (`#<name>`), `from` and, for a reply, `reply_to` ([Topics, The tools](../docs/topics.qmd#the-tools) shows one). Attribute values are record ids, author-given topic names and roster names, all grammar-checked, so they need no escaping (`_fence.py`). The fence is a label, not a boundary: a post is delivered as typed, and a member can write text that looks like another member's post or a count line (decision: Ransom, 2026-10-27; [swarm-communication.md Fencing](swarm-communication.md#fencing)). Evidence comes from bus events and ids, never from result text.

## Storm controls

`max_bytes` per post and `rate` per member across its topics are the bus's `StormControls`, so the bus applies them and records their refusals in `sent` as it does for messages ([Topics, Limits](../docs/topics.qmd#limits)). The channel has no `max_unread` and no `dedupe`. Those are back-pressure on an inbox the recipient must be running to drain, and under delivery by membership a pending or ended reader never drains its unread posts, so a cap on them would close a topic to every writer for good. `rate` bounds what any one member adds, and a reader's unread posts are read a budget at a time.

## The count lines

`SwarmBus.status(context)` builds the lines that end the results of the swarm's own tools in the member's own loop, and returns nothing in a nested loop, which reads neither messages nor posts. It finds the built-in channels' states by type (`MessagesState`, and `TopicsState` through `builtin_topics()`), not by name, which a custom channel may take.

- **Without the topics channel, or with no unread posts**, it is the messages channel's line, `You have 2 unread messages.`, or nothing.
- **With unread posts**, it is `TopicsState.status()`: two lines, the per-topic detail, `Unread posts: #orders-worker-2 3, #general 1.`, then a summary holding every total, `You have 2 unread messages and 4 unread posts.`, or `You have 4 unread posts.` when no message is unread or there is no messages channel. The summary always comes **last**, because Inspect's tool output truncation keeps the start and the end of a result: the detail grows with topic names (up to 48 bytes each), while the summary is at most 50 bytes while each count is below 1,000, so a `max_tool_output` of 100, which keeps the last 50 bytes, keeps both totals.

`post_topic`, `list_topics`, `send_message`, `list_members`, `shared_memory` and `swarm_wait` end with these lines; `list_members` ends with `You have no unread messages.` when there are none. `read_topics` and `read_messages` end with their own remainder lines instead. Tools of other channels, a user channel's or Inspect's, carry no count (decision: Ransom, 2026-10-30). The detail line holds counts and author-given names of topics the caller reads, in declaration order, never a payload. [Topics, Hearing about posts](../docs/topics.qmd#hearing-about-posts) shows them as members see them.

## Waiting

`TopicsState` is a `WaitingState`:

- **`wakes(spec, record)`**: the wait is the member's own loop and the member is one of the record's recipients. The bus calls it only for records with `wake` set, so only posts in `wake=True` topics end a wait.
- **`news(spec)`**: in the member's own loop, when any unread post is from a `wake=True` topic, one line counting every unread post and naming its topics (in declaration order) and posters (sorted), `4 unread posts in #orders-worker-2, #general (from lead, worker-1). Call read_topics() to read them.`; its `WaitNews.depends_on` is every unread post it counts, those of `wake=False` topics included, as the messages channel's news counts quiet messages, and the bus trims it per loop before writing the `waited` event ([Evidence](#evidence)). Without a waking unread post it has no news, and a wait that returns for another reason ends with the count lines, which link nothing. `news()` marks nothing read.
- **`wait_check(spec)`** accepts every wait: the wait's `path` and `file_id` filters belong to the shared memory.
- **Idle persistent members.** After a commit, the bus reports a waking record that would end a whole-board wait of an idle member's own loop as news for that member, so a post in a `wake=True` topic gives the controller `MemberNews` for each idle reader, and `MemberIdle.news` is set while such a post stays unread ([swarm-persistent-members.md News for idle members](swarm-persistent-members.md#news-for-idle-members)). A post in a `wake=False` topic waits for the reader's next activation.

`swarm_wait` comes with the topics channel even when no topic wakes: it still returns on a member's end, when every member is waiting, at the stop and at its timeout, and a member may wait for those. Because the channel's reach is `"loop"`, only the member's own loop's wait watches it.

**The description.** `Waits._make_tool()` passes `topics=True` to `wait_tool()` when a topics state is among the channels the loop's wait watches, and the description then comes from `_topics_description(messages, board)`: its first sentence names each source the wait watches (an unread message sent with `wake=True`, an unread post in a topic that wakes its readers, a shared-memory change, a member's end), its acknowledgment sentence names each read tool, and it says that quiet messages and posts are counted when the wait returns for another reason. `board` is set when any watched channel is neither the messages nor the topics channel, so the shared memory or a custom waiting channel brings the `path` and `file_id` parameters, as without topics.

**The trailing count.** A wait result ends with the count lines, so that truncating the middle of a longer result keeps the totals, unless every line of the result is one counting channel's own news (whose line leads with its count) and no other counting channel has an unread record for the member (`SwarmBus.unread()`, which counts for the messages and topics channels and is 0 for others). So a result that is only message news ends with the count lines when posts are unread; a result with message and topic news ends with them, their summary last; and a result that is only one channel's news, with nothing unread elsewhere, ends with that news. With messages alone this is the messages channel's rule ([Messages, Hearing about messages](../docs/messages.qmd#hearing-about-messages)).

## Instructions

The channel's `instructions(member)` is a fixed template with only author-given names: what the three tools do; that posts are shown between `<topic_post>` tags whose `from` attribute names the poster; the member's own topics with its access, `You read and post in #general; you read #orders-worker-2; you post in #reports.`, or `You are in no topic.`; for waking topics it reads, `A post in #a or #b ends swarm_wait, so call it to wait for one rather than polling.`; and that while it has unread posts the results of the swarm's built-in tools end with a line that counts them. Topics the member is not in are never named, and no other member is named.

## Evidence

No new event or field. Posts write `sent`, `delivered` and `read` `CommunicationEvent`s with `channel="topics"`, kind `post` and the `p` prefix, as messages do ([swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance); [Topics, The record of posts](../docs/topics.qmd#the-record-of-posts)). `sent` carries the record with `to` (`["#orders"]`), its `recipients` and an empty `routed_out`; a refusal at the liveness, payload, storm or `admit()` checks writes `sent` with the control and reason. `delivered` carries the recipients and the commit sequence; `read` carries the posts a read showed. A wait stays one bus-wide `waited` event with `channel=None`, with `"topics"` in its `wait.channels` when it watches the channel, its news's posts in `data.files`, and in `data.depends_on` the posts its news counted less those an earlier result in the same loop already depended on: the bus trims `WaitNews.depends_on` per loop ([swarm-critical-path.md The bus checks and trims](swarm-critical-path.md#the-bus-checks-and-trims)), so a second wait before the read repeats the news and `files` but depends on nothing new. The communication metrics, the ledger and the critical path read these events by channel name, so they count topics as they count any channel ([swarm-critical-path.md](swarm-critical-path.md#each-channels-rule)). Analysis reads a topic's readers and writers from the logged channel configuration.

## How this sits on S2

The channel uses the public operations of [swarm-orbit.md S2](swarm-orbit.md#s2-a-public-bus-api-for-channels): `deliver(Draft)`, `Bus.read()` and `ResolvingState`. As a built-in channel it also uses three in-package integrations, through the run's `SwarmBus`: the member/loop and lifecycle guards shared with the messages channel (`own_caller()`, `member()`, `nested()`, `running()`); the count lines (`SwarmBus.status()` and `SwarmBus.unread()`, which find the topics state by type and serve the wait's trailing-count rule); and the `swarm_wait` description. ORBIT's channel resolves its own recipients with `resolve()` and reports reads with `Bus.read()`; it does not use the built-in count lines.

## Decisions

- 2026-11-05: each channel decides its own communication restrictions, and some have none; there are no member-pair routes on the bus or for the messages channel, and a topic's restriction is its readers and writers (decision: Ransom, 2026-11-05).
- 2026-11-05: a topic is addressed by its author-given name; swarm-assigned ids (`#c-3`) stay reserved for topics members might create later (decision: Ransom, 2026-11-05).
- 2026-11-06: the address hook is public, `ResolvingState`, and ORBIT's channel resolves its recipients with it, so that its recipients are in the standard `sent` and `delivered` records (decision: Ransom, 2026-11-06, at the ORBIT team's request).
- 2026-11-09: every topic is visible only to its members, with no public/private flag (decision: Ransom, 2026-11-09).

## Alternatives considered

**Unaddressed posts, with recipients the channel computes in `apply()`.** What a channel without `resolve()` does, needing no address hook. But the `sent` event would name no recipients, `delivered`'s typed `recipients`, "the agents it reached", would be empty for every post, and a reader's unread posts would be the channel's own bookkeeping beside the bus's records. Rejected for `resolve()`, which ORBIT's channel uses too.

**A `recipients="roster"` class attribute**, lifting the liveness check for a channel's addresses. swarm-orbit.md rejected it for ORBIT, and it is not enough here either: `#orders` still has to resolve to the topic's readers, which only the channel knows. The resolver hook does both.

**Posting to topics through `send_message(to="#orders")`.** One fewer tool. But one inbox would mix two delivery rules (liveness for members, membership for topics), the inbox's `max_unread` back-pressure would close a topic as soon as a pending reader had 50 unread posts, and an arm with topics and without direct messages would need a messages channel that refuses member addresses. Rejected.

**Swarm-assigned ids (`#c-3`).** What swarm-communication.md reserved, for names a member chooses. Every topic here is declared and named by the author, so its name is as safe as a member name, and a model would have to map numbers to rooms. Rejected; ids remain the form for topics created at run time, if those are ever built.

**Wake set per post by the model** (`post_topic(wake=True)`), as `send_message` does. But whether a room interrupts its readers' waits is the experiment's design, not the poster's choice, and a per-post flag lets a chatty member end every reader's wait. Rejected for a per-topic setting the author logs.

**Member-pair routes on the bus**, `swarm(routes=[(sender, recipient), ...])`, splitting every addressed record's recipients into those the sender may reach and `routed_out`, so one rule would govern messages and topics. Rejected (decision: Ransom, 2026-11-05): channels may support different topologies, so how a restriction is specified is the channel's; a swarm with several channels may be rare, and where there are several an experiment may want different communication on each; and some channels need no restriction at all. Topics' readers and writers express every topology above. `Record.routed_out` stays empty for every record.

**Routes for the messages channel** (`messages(routes=...)`). Would let an arm keep direct messages under a topology. Not needed for ORBIT, whose channel computes its own recipients; left out, and a later design can add it if an experiment needs it (decision: Ransom, 2026-11-05).

**Communication edges inside a topic**, ORBIT's edges intersected with a topic's readers. A room's readers are its audience; a pair that must not talk is put in no room together, as the hub and spokes does with a topic per worker. Rejected as a second restriction on one room.

**A public/private flag.** Public topics would let members see rooms they are not in and who is in them, and a team experiment would have to remember to mark each team's room private to keep it unknown to the other team. Rejected for one behaviour, visibility by membership (decision: Ransom, 2026-11-09).

**Rooms as shared-memory files** (`/shared_memory/rooms/red.md`). No new channel. But the board has no readers or writers, no privacy, no per-reader unread posts, and wakes every waiter of a path on every write. Rejected.

**A `max_unread` or a total post cap per topic.** Bounds unread growth. But a reader that never runs would close the topic for every writer; `rate` already bounds what a member adds. Rejected.

## Compatibility

- **Stored formats.** No event type or field of its own: posts write the swarm's communication events with a channel name, kind (`post`) and id prefix (`p`) of their own, and `routed_out` empty. The viewer's generated types and the swarm's evidence version are those of every other channel.
- **Public API.** `topics`, `Topic` and `ResolvingState`. In a swarm without the topics channel, no tool schema, description or output mentions topics: the count lines, the wait description and its trailing count are the messages channel's.
- **Custom channels.** A channel state without `resolve()` keeps the member resolver, and unaddressed drafts reach nobody directly. A state with `resolve()` receives every draft of its channel, `to=()` included, and defines its own address grammar (the `Draft.to` and `Record.to` docstrings say so).
- **Bridged members** and members that choose their own names are refused beside `topics` by `swarm()`'s general rules, with messages naming the channel.
- **ORBIT's channel** posts addressed records resolved by its state's `resolve()` ([swarm-orbit.md The channel](swarm-orbit.md#the-channel)).

## Security

- **Untrusted input reaching the channel:** the post text, the `topic` argument and `reply_to`, all model output; nothing through the bridge, since bridged members are refused.
  - The text is stored and delivered as typed, inside a fence that is a label, never a boundary; it is bounded by `max_bytes`, and a payload that cannot be written to the log (a lone surrogate) is refused by the bus's payload check, with `sent` evidence. It never reaches a count line, a wait result, a preamble or an error.
  - `topic` is checked against the topics the caller reads or writes; any other value gets the same error, which lists only the caller's own topics, so the error reveals no topic the caller is not in. A value outside the name grammar is shown escaped (`repr`) in that error, since it may not be valid text. `reply_to` must be an id the caller has posted or read, and its error does not echo the value.
- **Sender identity** is bound by the bus from the member context, as for every record; no argument sets it.
- **Topics are hidden from non-members** in every text the swarm writes for them: preamble, list, count lines, wait results, errors, and a post's result, which only a writer, a member of the topic, receives. The log holds everything, for its readers.
- **Topics are the author's**, validated at construction; no model output changes them.
- **What topics do not stop**: communication through the messages channel, shared sandboxes, the shared memory or shared tool instances, as swarm-communication.md says of messages ([The filesystem channel](swarm-communication.md#the-filesystem-channel)). An arm whose topology must be the only path gives members their own sandboxes and leaves the messages channel and shared state out ([Topics, Topologies](../docs/topics.qmd#topologies)).
- **A channel's `resolve()`** is the channel author's code, not model input; the bus still refuses a non-member, a duplicate or the sender among its recipients, so a buggy resolver cannot address a name outside the roster or the sender.

## Not this design

- **Topics created by members at run time** (ORBIT's attack-created `secret:` channels, Codex board subscriptions). They would need swarm-assigned ids, since a member's name for a topic is payload, and a rule for who may join.
- **Rereading a topic's history** with a cursor, as ORBIT's `after_sequence` does, and showing a poster its own posts.
- **Bridged members on topics**: declarations for `swarm_bridged_tools(["topics"])` and its `swarm_wait`.
- **Push into a member's conversation**, which [swarm-communication.md open question 2](swarm-communication.md#open-questions) governs for every channel.
- **`swarm_wait` describes a custom waiting channel as the shared memory**, with its `path` and `file_id` parameters. A user channel that wakes waits needs its own wait description and filter.
- **A hub-and-spoke version of the lead-and-workers example** on topics, waking only the worker a task is for.
- **Restrictions for the messages channel**, such as `messages(routes=...)`. Each channel decides its own; the messages channel can add one if an experiment needs direct messages under a topology (decision: Ransom, 2026-11-05).
