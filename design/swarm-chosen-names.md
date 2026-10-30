# Inspect Swarm: members that choose their own names

## Overview

`member(..., choose_name=True)` leaves the swarm's name for a member out of everything the swarm tells it. Instead of opening with "You are react-1", the member's preamble says how many agents are in the swarm and asks it to choose a name for itself and tell the others what it is. Its role, if it has one, is still stated. The swarm keeps its own name for the member for everything it tracks (timelines, spans, tool-state scopes, budget shares, the ledger and the result record), so the option changes only what the agent is told.

The `filesystem` channel, the default, works with the option: a member that chooses is told to keep its work in a directory named with the name it chose, and when any member chooses, the members the eval author named are told that their directory may be taken too. The filesystem channel and `channels=[]` are the only configurations accepted: `swarm()` refuses every other channel beside a member that chooses its name, because channels with records show members to each other by the swarm's names and a user channel's instructions cannot be checked for the name. The result record says which members chose their own names; the name a member chose is in its transcript, not in the swarm's records.

The code is `_preamble()` and `_check_chosen_names()` in [`_swarm.py`](../src/inspect_swarm/_swarm.py) and `FilesystemChannel.member_instructions()` in [`filesystem.py`](../src/inspect_swarm/_channel/filesystem.py). The user docs are [Members that choose their names](../docs/members.qmd#members-that-choose-their-names), the scratch wording in [the `filesystem` channel](../docs/channels.qmd#the-filesystem-channel), and the record field in [Scoring](../docs/scoring.qmd).

## What a member is told

### The preamble

`_preamble()` builds each member's preamble when the swarm runs. Its first paragraph depends on the member's own flag:

| | `choose_name=False` | `choose_name=True` |
|---|---|---|
| Several members | `You are {name}, one of {N} agents in a swarm working on this task at the same time.` | `You are one of {N} agents in a swarm working on this task at the same time. Choose a name for yourself and tell the other members what it is.` |
| One member | `You are {name}, the only agent in this swarm.` | `You are the only agent in this swarm. Choose a name for yourself.` |

The rest of the preamble is the same for both: `Your role: {role}.` when the member has a role, the budget split sentence, the own-sandbox sentence when members have their own sandboxes, and each channel's instructions. None of these contains a name except the filesystem channel's scratch sentence, below. The role line stays because a role is the eval author's assignment of what the member does, not who it is, and a study of naming holds roles fixed while names vary; an eval that wants no role leaves `role` unset.

The member is asked to choose and announce a name, rather than told nothing, because choosing a name and telling peers is the condition the option exists to study, and because the scratch convention needs a name to refer to. How the member tells the others is up to it: the notes file, a file in the shared directory, or nothing. A swarm-wide setting is `choose_name=True` on every record; a roster may mix the two, for example a lead the eval author names and workers that name themselves.

### The `filesystem` channel

Only the scratch sentence depends on the option. Which one a member gets depends on its own flag and on whether any member of the roster chooses:

| Member | No member chooses | Some member chooses |
|---|---|---|
| Named | `Keep your own work in {scratch.format(member=name)}, which no other member uses.` | `Keep your own work in {scratch.format(member=name)}; some other members choose their own names and name their directories after them, so check that none of them uses the same one.` |
| Chooses its name | n/a | `Keep your own work in {scratch.format(member="<your name>")}, a directory named with the name you chose; check that no other member uses the same one.` |

With the default template a chooser's path is `/scratch/<your name>`; with `scratch="/tmp/{member}/work"` it is `/tmp/<your name>/work`. Once any member chooses, no member can be promised that its directory is unused: two choosers can pick the same name, and a chooser can pick a named member's name (a worker that calls itself `lead` beside a lead named `lead` is told `/scratch/lead` too). Saying so is part of the condition. With `filesystem(shared_volume=True)` there is no scratch sentence, so nothing in the channel's text changes.

`Channel.instructions(member)` sees only the member, so it cannot tell a named member that others choose. `FilesystemChannel.member_instructions(member, choosers)` takes the roster's flag as well, and `instructions(member)` calls it with `choosers=member.choose_name`. `swarm()` computes `choosers` once, as whether any roster member has `choose_name`. When it is true, `_preamble()` calls `member_instructions(member, choosers=True)`, which is safe because `swarm()` has already refused every channel but the built-in filesystem channel ([What is refused](#what-is-refused-and-why)). When it is false, `_preamble()` calls `instructions(member)`, so a swarm in which nobody chooses, with any channel, takes the ordinary path. `Channel`'s public interface has no chosen-name parameter.

The channel creates no directories. A sandbox image that pre-creates a directory per member would tell members the swarm's names; [Sandboxes](../docs/sandboxes.qmd) says to create `/shared` and `/scratch` but not the members' directories when members choose their names.

### Other configurations

- **`channels=[]`**: no channel instructions. Members without `sandbox=` share the sample's sandbox; members with their own sandboxes keep the own-sandbox sentence and routing.
- **Per-member sandboxes with `filesystem(shared_volume=True)`**: accepted, and no name appears. `sandbox="{member}"` still names the environment after the swarm's name, which the swarm never shows the member, though the sample's sandbox configuration could (a compose `hostname:`, an environment variable).
- **Bridged members**: accepted with the filesystem channel or none, as for any member; bridged members cannot use channels with records anyway.
- **A controller's `start(member, input=...)`**: the eval author's text, used as given. A controller sees the flag in `MemberInfo` in its `check` and in `MemberHandle.choose_name` in its `run`, so one that writes a member's input can leave names out for that member.

Text the eval author gives the member is not changed and not checked: the sample input, a controller's `start(input=...)`, the member agent and its prompt, and the sample's sandbox configuration.

## What the swarm keeps

The swarm's name for a member (`member(name=...)`, or the agent's registry name, numbered when several members share it) names everything the swarm tracks, with or without the option: the member's timeline, agent span, tool-state scope, budget share, ledger entry and result record entry. A name in the swarm's records and tools is one the eval author chose ([swarm-api.md Records, logging and names](swarm-api.md#records-logging-and-names), the Names bullet), and status lines contain only names the eval author or controller assigned ([swarm.md Delivery](swarm.md#delivery-peer-messages-are-model-output)); the option keeps both rules.

The flag travels with the member:

- `Member.choose_name` and `member(choose_name=)`, default `False`. `Member` serializes it into the plan step after `role`, always, as it does every other field, so two arms that differ only in it have distinct task identifiers. A record given as data without the field validates with `False`.
- `MemberInfo.choose_name`, the last field, default `False`, so `MemberInfo(name, role)` still constructs. `swarm()` sets it from the record for the roster a controller's `check` and the channels' `check()` see; the bus copies it from `MemberHandle.choose_name` into `ChannelRun.roster`.
- `MemberResult.choose_name`, default `False`, after `role` in the result record ([swarm-result-record.md](swarm-result-record.md)), so analysis can split members by condition without decoding the plan step. The ledger and the `member_result` evidence do not carry it.

The name a member chose is not recorded. The swarm never learns it: the member states it in free text, in a file or not at all, and any extraction by the swarm would be a guess at model output. It is in the member's transcript, where an analysis (an `inspect_scout` scanner, for example) can find it with the rest of how members referred to themselves and each other.

## What is refused, and why

`_check_chosen_names()` runs in `swarm()` at construction, after the bus-channel checks, so an arm that cannot hide names fails when the task is built rather than in a sample. When any roster member has `choose_name`, every channel must be the built-in filesystem channel (`type(channel) is FilesystemChannel`, so a subclass is refused too). Otherwise it raises for the first member that chooses and the first other channel:

> `swarm(): member 'react-1' chooses its own name (choose_name=True), which the swarm supports only with the filesystem channel or channels=[]; inspect_swarm/shared_memory is not supported with it. Leave the channel out, or set choose_name=False.`

A swarm with no member that chooses is not checked. The rule is an allowlist of the configurations audited for the name:

- **Channels with records** show members to each other by the swarm's names, through the channel's tools and through the bus. Shared memory names each file's last writer in views, listings, deletions and stale-write refusals. The messages channel addresses members by name, names senders and recipients in its fences, and lists the roster with `list_members`. `swarm_wait`, which comes with these channels, names ended members. The refusal covers the whole swarm, not only the member that chooses, because a peer's view shows the chooser's assigned name too (as the writer of its files, or the sender of its messages).
- **User channels** compute their instructions in code that the swarm cannot inspect. No check over their output proves the name is absent: a channel can transform the name, or show it only for some names, and still give the same text when called with another name.

## Decisions

- 2026-10-22: the option is per member, beside the `name` and `role` it qualifies, not a `swarm()` argument; a swarm-wide setting is the flag on every record, and a mixed roster is possible (decision: Ransom, 2026-10-22).
- 2026-10-22: the role line is still stated for a member that chooses its name (decision: Ransom, 2026-10-22).
- 2026-10-22: the filesystem channel and `channels=[]` are the only configurations supported; channels with records are refused, and registering chosen names so that they can work is left for a later design (decision: Ransom, 2026-10-22).
- 2026-10-22: `choose_name` is serialized into the plan step even at its default, as every member field is, accepting that adding the field changed the task identifiers of existing swarm tasks rather than omitting the default (decision: Ransom, 2026-10-22).
- 2026-10-28: user channels are refused beside a member that chooses, including subclasses of `FilesystemChannel`, rather than accepted on a check of their instructions or on the channel's declaration (decision: Ransom, 2026-10-28).
- 2026-10-28: the member is asked to choose and announce a name; a chooser gets a `<your name>` scratch path, and once any member chooses, named members are no longer told their directory is unused (decision: Ransom, 2026-10-28).
- 2026-10-28: the result record holds the flag per member, not the name a member chose (decision: Ransom, 2026-10-28).

## Alternatives considered

**A `swarm()` argument.** States "nobody is assigned a name" in one place, but cannot express a mixed roster, and the record field is per member anyway. A swarm-wide flag is a loop over records on the per-member one; the reverse is not possible.

**Registering the chosen name.** A `swarm_name(name)` tool each self-naming member must call before it can use the channels, checked against the addressing grammar and the names already taken, with channels showing the registered name and the record storing both. It is what would make shared memory and messages usable with chosen names. It costs a new tool and a gate on every channel tool, a second name per member through the bus and its evidence, a placeholder for members not yet registered (an assigned name again), and a chosen name in status lines, which would need its own security argument. It also changes the condition: names become a swarm-enforced unique registry rather than something the members work out.

**Anonymous ids in channels with records.** Showing others as `m-3` and the member as `you` is an assigned name by another spelling, and needs a per-viewer renaming of every channel's output.

**No scratch sentence for members that choose.** Simpler, but leaves them without a convention for private work in a shared sandbox, a difference from the named arm beyond the name.

**Accept user channels whose instructions pass a check.** Comparing a channel's instructions for the member's name and a probe name proves nothing (`"/scratch/" + (m.name if m.name.isidentifier() else "worker")` gives the same text for a member named `worker` and for the probe), and searching the text for the name fails both ways.

**Let a channel declare that its instructions omit the name.** A `Channel` class attribute a channel sets when its `instructions()` honours `member.choose_name`, as `reach` and `max_wait` declare other properties. Not needed while the filesystem channel is the only one a study uses; it is the natural extension when one is.

## Compatibility

- **Public API.** `member(choose_name=)`, `Member.choose_name`, `MemberInfo.choose_name`, `MemberHandle.choose_name` and `MemberResult.choose_name` default to `False`; with nobody choosing, preambles and channel instructions are as without the option. `MemberHandle` is a protocol the runtime implements, so a test double that implements it needs the property.
- **Logs.** The plan step's member dict holds `choose_name`. A log whose member dicts lack it replays with `False`.
- **Result record.** `MemberResult.choose_name` is a defaulted field in record version 1: a record without it reads `False`, and an older reader ignores it (`MemberResult` keeps pydantic's default `extra="ignore"`).

## Security

- **No new untrusted input reaches the swarm.** The option is the eval author's configuration. The name a member chooses stays in model output (files, the transcript) and never enters text the swarm generates: not the preamble, not status lines, not fences, not records.
- **Refusing every channel but the filesystem channel** is what keeps it so. A design that let chosen names through the bus would have to treat them as model output wherever they appear ([Alternatives considered](#alternatives-considered)).
- **The `<your name>` placeholder** is fixed text substituted into the channel's own template; nothing a member chose is substituted.
- **Collisions are not a security boundary.** Two members may choose the same name and use the same scratch directory. The filesystem channel is a convention, not isolation ([swarm-communication.md The filesystem channel](swarm-communication.md#the-filesystem-channel)).

## Not this design

- **User channels beside members that choose**, through a channel's declaration that its instructions omit the name ([Alternatives considered](#alternatives-considered)).
- **Registering chosen names** with the swarm (a `swarm_name` tool), so that channels with records show them and the record stores them beside the assigned name.
- **A silent variant** that leaves the name out without asking the member to choose one, to see whether members name themselves unprompted. It would be a second value of the option (for example `choose_name="silent"`); no arm asks for it yet.
- **Per-member users** ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md)): their user name and home (`swarm-<member>`, `/scratch/<member>`) would expose the assigned name to `id` and `pwd`, so that design must refuse a roster with any `choose_name=True` member; the refusal and its tests belong to the isolation design's own change.
- **Names in the eval author's own text**: the sample input, a controller's `start(input=...)`, the member agent's prompt, and sandbox configuration that names services after members. The swarm cannot see these and does not try.
