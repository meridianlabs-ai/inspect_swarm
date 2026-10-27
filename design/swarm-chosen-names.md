# Inspect Swarm: members that choose their own names

## Overview

A new member option, `member(..., choose_name=True)`, leaves the swarm's name for the member out of everything the swarm tells it. Its preamble says how many agents are in the swarm and asks it to choose a name for itself and tell the others what it is, instead of opening with "You are react-1". Its role, if it has one, is still stated. The swarm keeps its own name for the member for everything it tracks (timelines, spans, tool-state scopes, budget shares, the ledger and the result record), so the option changes only what the agent is told.

The `filesystem` channel, the default, keeps working: instead of naming the member's scratch directory after the swarm's name, it tells the member to keep its work in a directory named with the name it chose, and in a roster that mixes the two it tells the named members that their directory may be taken too. The filesystem channel and `channels=[]` are the only configurations supported with the option: `swarm()` refuses any other channel beside a member that chooses its name, the shared-memory channel because its tools show members to each other by the swarm's names, and user channels because their instructions cannot be checked for the name. The result record says which members chose their own names; the name a member chose is in its transcript, not in the swarm's records.

## Why

Every member's preamble opens with the name the swarm assigned, `You are {name}, one of {N} agents in a swarm…` (`src/inspect_swarm/_swarm.py:139-143`). The name comes from `member(name=...)` or the agent's registry name, numbered when several members share it (`react-1` to `react-4`), so a member always starts with an identity the eval author or the framework handed out, and a default name also tells the model which agent framework it runs in. An eval that studies how agents handle identity and coordination when nobody assigns them a name (do they pick names, do two pick the same one, how do they find out who is who) has no way to run that condition today (issue #61).

## Goals and non-goals

Goals:

- A per-member option that keeps the swarm's name for the member out of everything the swarm shows that member: the preamble and the built-in channels' instructions.
- The swarm's internal name, and everything keyed on it, unchanged.
- The filesystem channel and `channels=[]` work with the option; every other channel, built-in or user-defined, is refused with a clear error when any member chooses its name.
- The result record says which members chose their own names, so analysis can tell the conditions apart per member.

Non-goals:

- Registering the name a member chose with the swarm, so that channels could show chosen names and the record could store them. See [Alternatives considered](#alternatives-considered) and [Not this design](#not-this-design).
- Hiding names that the eval author's own code or configuration shows the agent: the sample input, a controller's `start(input=...)`, the member's agent and its prompt, or the sample's sandbox configuration.
- Making the shared-memory channel, `swarm_wait` or the later messages channel work with members that choose their names.

## Current behaviour

Code references are to inspect_swarm `main` at `10ea351`.

### Where the swarm's name reaches the agent

- **The preamble.** `swarm()` builds each member's preamble at run time in `_preamble()`: the name and roster size, then `Your role: {role}.` when the member has a role, the budget split sentence, the sandbox sentence and each channel's `instructions(member)` (`src/inspect_swarm/_swarm.py:104-113`, `:132-161`). `SwarmRun.start()` appends it as the last user message of the member's input (`src/inspect_swarm/_controller/_control.py:331`). Rendered with `.venv/bin/python` for `MemberInfo("react-1", "reviewer")` in a swarm of four with `filesystem` and `shared_memory`, it opens `You are react-1, one of 4 agents in a swarm working on this task at the same time.` and the filesystem paragraph ends `Keep your own work in /scratch/react-1, which no other member uses.` A swarm of one says `You are react-1, the only agent in this swarm.`
- **The budget split sentence** (`split_sentence()`, `src/inspect_swarm/_budget.py:223-239`) and the sandbox sentences (`_swarm.py:148-156`) contain no names.
- **The `filesystem` channel** is instructions only: no tools, no records, no state (`src/inspect_swarm/_channel/filesystem.py`). Its instructions name the member's scratch directory, `scratch.format(member=member.name)` (`:31`), from a template that must contain `{member}` and no other field (`:74-84`). With `shared_volume=True` there is no scratch line (`:21-26`).
- **The shared-memory channel** shows the swarm's names in most results: a viewed file's fence carries `last_writer` (`src/inspect_swarm/_channel/shared_memory.py:877-882`), listing lines name each file's writer (`:903-911`, `_listing_line` at `:1072-1076`), a deleted file reads `deleted by {writer}` (`:869`), a stale write is refused with `last written by {writer}` (`:765`), and wait results say `by {writers}` (`:430-433`, `:1115-1117`). The writer is the record's sender, the member's name (`:372`). Its `check()` requires names in an addressing grammar because they appear in fence attributes and wait results (`:43-45`, `:96-101`).
- **`swarm_wait`**, which comes with a channel that sets `max_wait` (only `shared_memory` today), names ended members (`{member} ended ({status}).`, `src/inspect_swarm/_bus/_wait.py:371`) and its own member (`{name} is ending`, `{name} has ended.`, `{name} is already waiting`, `:381`, `:161`, `:169`). The shared-memory tool's own errors also name the caller (`{member} has ended.`, `shared_memory.py:524`). The bus's address resolution names members in its errors (`src/inspect_swarm/_bus/_bus.py:230-258`), for the later messages channel.
- **Nowhere else.** `swarm_resources()` reports amounts only (`src/inspect_swarm/_tools.py:18-50`). The member's tool-state scope, agent span and timeline are named after it but never sent to a model. A member's own sandbox may be named after it (`sandbox="{member}"`, `src/inspect_swarm/_member.py:119-120`); the swarm never shows the sandbox name, though the sample's sandbox configuration could (a compose `hostname:`, an environment variable).

So with the filesystem channel alone, the preamble's first sentence and the scratch path are the only places the swarm tells a member its name; with any channel that has records, the bus and the channel's tools show every member's name to every member.

### What the swarm records per member

`MemberInfo` (`name`, `role`) is what a controller's `check` and a channel's `check()` and `instructions()` see (`src/inspect_swarm/_controller/_controller.py:14-22`); it is public (`src/inspect_swarm/__init__.py:29`). `swarm()` builds it from each record (`_swarm.py:67-71`), and the bus builds it again for `ChannelRun.roster` (`_bus.py:65-67`). The result record's `MemberResult` holds `name`, `role`, `state`, `status`, `submitted`, `time`, `sandbox` and the gate counts (`src/inspect_swarm/_record.py:12-43`), built by `MemberRun.result()` (`_control.py:221-233`). The ledger's `MemberLedger` holds the name, status and usage (`src/inspect_swarm/_ledger.py:89-115`). `Member` serialises every field into the plan step (`_member.py:174-191`), which `task_identifier()` hashes, so two arms that differ in a member field get different eval-set identifiers (`tests/test_api_log.py:285-295` checks this for agent parameters, factory arguments and limit caps).

### Rules this design keeps

- A name in the swarm's records and tools "is an identifier the eval author chose; members never choose names" ([swarm-api.md Members](swarm-api.md#members), the Names bullet).
- Status lines contain only "names the eval author or controller assigned", never "a name a member chose" ([swarm.md Delivery](swarm.md#delivery-peer-messages-are-model-output)).

### Who uses what

inspect_swarm's channels are the built-in `filesystem` and `shared_memory`, and the user channels in its tests; one of those, `_Locks` in `tests/test_channel.py:37-39`, puts `member.name` in its instructions. Controllers are the built-in `leaderless`, which starts every member with the swarm's input and names no one to them (`src/inspect_swarm/_controller/leaderless.py:33-40`). The design therefore serves the filesystem channel (with and without `shared_volume`) and `channels=[]`, which is all a filesystem-only first study needs, and refuses every other channel when a member chooses its name.

## Design

### At a glance

```python
swarm(
    member(react, args={"tools": [bash()]}, count=4, choose_name=True),
)
```

runs four members, `react-1` to `react-4` in the log, each told: `You are one of 4 agents in a swarm working on this task at the same time. Choose a name for yourself and tell the other members what it is.` followed by the filesystem instructions with `Keep your own work in /scratch/<your name>, a directory named with the name you chose; check that no other member uses the same one.`

### The option

`Member` and `member()` gain one field, after `role`:

```python
choose_name: bool = False
"""Whether the member chooses its own name. `True` leaves the member's name out of its preamble, which asks it to choose a name and tell the other members; the swarm still uses `name` for everything it records."""
```

It is per member, beside `name` and `role`, which it qualifies. A whole swarm without assigned names is `choose_name=True` on each record (one record with `count` for identical members). A roster may mix the two, for example a lead the eval author names and workers that name themselves; the swarm tells a named member its assigned name, and tells no one else. Peers can still discover it through that member's scratch directory in the shared sandbox, or because the member announces it; members that choose never receive their own assigned names in anything the swarm generates.

`member()`'s docstring gets a paragraph, "Chosen names": what the member is and is not told, that the swarm's records still use `name`, that only the filesystem channel or no channel can be used in a swarm where any member has it, and that the eval author's own text (the sample input, a controller's `start(input=...)`, the agent's prompt, the sandbox configuration) is not changed. The `name` argument's description adds: "With `choose_name=True` the member is not told it."

`Member._serialize()` writes `choose_name` after `role`, always, as it writes every other field. A record given as data without the field validates with `False`.

### The preamble

`_preamble()` gains the roster-wide `choosers: bool` parameter described in [The `filesystem` channel](#the-filesystem-channel) and reads the current member's own flag from the `MemberInfo` it already takes. Its first paragraph changes for a member that chooses:

| | `choose_name=False` (unchanged) | `choose_name=True` |
|---|---|---|
| Several members | `You are {name}, one of {N} agents in a swarm working on this task at the same time.` | `You are one of {N} agents in a swarm working on this task at the same time. Choose a name for yourself and tell the other members what it is.` |
| One member | `You are {name}, the only agent in this swarm.` | `You are the only agent in this swarm. Choose a name for yourself.` |

The rest of the preamble is as today, except the filesystem channel's scratch sentence, which changes for every member of a roster in which some member chooses ([The `filesystem` channel](#the-filesystem-channel)). **The role line stays**: `Your role: {role}.` is the eval author's assignment of what the member does, not who it is, and a study of naming wants roles held fixed while names vary. An eval that wants no role leaves `role` unset.

The sentence asks the member to choose and announce a name, rather than saying nothing, because "members have to choose a name for themselves and tell their peers what it is" is the issue's condition, and because the filesystem channel's scratch convention needs a name to refer to. How the member tells the others is up to it: the notes file, a file in the shared directory, or nothing.

### `MemberInfo`

`MemberInfo` gains `choose_name: bool = False` as its last field, so existing positional and keyword constructions keep working:

```python
choose_name: bool = False
"""Whether the member chooses its own name, so that nothing the swarm tells it contains `name`. `swarm()` accepts such a member only with the `filesystem` channel or no channel."""
```

`swarm()` sets it from the record (`_swarm.py:67-71`). The bus builds `ChannelRun.roster` from the runtime's `MemberHandle`s (`_bus.py:42-48`, `:65-67`), so `MemberHandle` gains a read-only `choose_name` property beside `role` (`_control.py:51-62`), implemented by `MemberRun.choose_name`, set from the record beside `role` (`_control.py:183-187`), and the bus copies it. A controller's `check` sees it in `MemberInfo` and its `run` in the handle, so a controller that writes a member's input with `start(input=...)` can leave names out for that member.

### The `filesystem` channel

Only the scratch sentence changes. Which one a member gets depends on the member and on whether anyone in the roster chooses a name:

| Member | No member chooses (unchanged) | Some member chooses |
|---|---|---|
| Named | `Keep your own work in {scratch.format(member=name)}, which no other member uses.` | `Keep your own work in {scratch.format(member=name)}; some other members choose their own names and name their directories after them, so check that none of them uses the same one.` |
| Chooses its name | n/a | `Keep your own work in {scratch.format(member="<your name>")}, a directory named with the name you chose; check that no other member uses the same one.` |

With the default template the chooser's path is `/scratch/<your name>`; with `scratch="/tmp/{member}/work"` it is `/tmp/<your name>/work`. Once any member chooses, the channel cannot promise any member that its directory is unused: two choosers can pick the same name, and a chooser can pick a named member's name (a worker that calls itself `lead` beside a lead named `lead` is told `/scratch/lead` too). Saying so is part of the condition being studied. With `shared_volume=True` there is no scratch sentence and nothing changes.

**How the channel learns about the roster.** `Channel.instructions(member)` sees only the member, so it cannot tell a named member that others choose. `FilesystemChannel` gains a method that takes the roster's flag, and `instructions()` delegates to it:

```python
def member_instructions(self, member: MemberInfo, choosers: bool) -> str:
    # the channel's text for a member of a roster in which some member chooses its own
    # name (`choosers`), which a member that chooses implies
    ...

def instructions(self, member: MemberInfo) -> str | None:
    return self.member_instructions(member, choosers=member.choose_name)
```

`_preamble()` gains a parameter `choosers: bool`, which `swarm()` computes once from the roster (`any(info.choose_name for info in roster)`), and, when `choosers` is true, calls `channel.member_instructions(member, choosers=True)`; otherwise it calls `channel.instructions(member)` as today, so a swarm in which nobody chooses, a user subclass of `FilesystemChannel` included, takes exactly today's path. When `choosers` is true every channel is the built-in `FilesystemChannel`, because it is the only channel accepted beside a member that chooses ([Refusals](#refusals)); so only it needs the flag, and `Channel`'s public interface is unchanged.

`filesystem()`'s docstring for `scratch` adds: "For a member that chooses its own name, `{member}` is shown as `<your name>`, and when any member chooses, no member is told its directory is unused."

### Refusals

One check in `swarm()`, at construction, so an arm that cannot hide names fails when the task is built, not in a sample. A new `_check_chosen_names(channels, roster)`, called after `_check_bus_channels()` (`_swarm.py:77`): when any member of the roster has `choose_name`, every channel must be the built-in filesystem channel (`type(channel) is FilesystemChannel`, so a subclass with its own instructions is refused too). Otherwise it raises for the first member that chooses and the first other channel:

> `swarm(): member 'react-1' chooses its own name (choose_name=True), which the swarm supports only with the filesystem channel or channels=[]; shared_memory is not supported with it. Leave the channel out, or set choose_name=False.`

The rule is an allowlist of the configurations this design has audited, for two reasons:

- **Channels with records** show members to each other by the swarm's names through the bus's own output, not only the channel's: `swarm_wait` results and address errors come from the bus ([Current behaviour](#where-the-swarms-name-reaches-the-agent)), and the later messages channel addresses members by name. The refusal applies to the whole swarm, not just the member that chooses, because a peer's view of the board shows the chooser's assigned name as the writer of its files.
- **User channels** compute their instructions in code (`Channel.instructions()`, `_channel.py:72-81`), which the swarm cannot inspect. No check over their output proves the name is absent: a channel can transform the name, or show it only for some names, and still give the same text when called with another name. A user channel that needs to work beside members that choose is a later change that lets a channel declare it (see [Alternatives considered](#alternatives-considered)).

A swarm with no member that chooses is not checked, so every existing configuration is unchanged. `swarm()`'s `Raises: ValueError` list adds the case.

### The record

`MemberResult` gains `choose_name: bool = False`, after `role`, filled by `MemberRun.result()`. Analysis that reads `inspect_swarm:result` can then split members by condition without decoding the plan step. Nothing else records the option: the ledger is about spend, and the `member_result` evidence keeps its fields.

The name a member chose is **not** recorded. The swarm never learns it: the member states it in free text, in a file, a message or not at all, and any extraction by the swarm would be a guess at model output. It is in the member's transcript, where an analysis (an `inspect_scout` scanner, for example) can find it with the rest of how members referred to themselves and each other. A later design that registers chosen names (below) would add a field for them then.

### Errors and edge cases

- `choose_name=True` with a single member: the one-member sentence above.
- `choose_name=True` with `channels=[]`: no channel instructions are added. Members without `sandbox=` share the sample's sandbox; members with their own sandboxes (`member(sandbox="{member}")`) keep the existing own-sandbox sentence (`_swarm.py:148-156`) and sandbox routing, as today.
- `choose_name=True` with per-member sandboxes and `filesystem(shared_volume=True)`: allowed; no name appears. `sandbox="{member}"` still names the environment after the swarm's name, which the swarm does not show.
- `choose_name=True` with a bridged member: allowed with `filesystem`; bridged members cannot use channels with records anyway.
- A mixed roster: named members keep the unchanged first paragraph, and with the filesystem channel get the mixed-roster scratch sentence above.
- A controller's `start(member, input=...)` is the eval author's text and is used as given.

## Alternatives considered

**A `swarm()` argument.** `swarm(choose_names=True)` states "nobody is assigned a name" in one place, which reads well for the study. But the name and role it qualifies are per member, a mixed roster (a named lead, self-named workers) is a reasonable arm that it cannot express, and the record field is per member anyway. A swarm-wide flag is a loop over records on the per-member one; the reverse is not possible. Not chosen.

**Registering the chosen name.** A `swarm_name(name)` tool that each self-naming member must call before it can use the channels; the swarm checks the name against the addressing grammar and against names already taken, and channels show the registered name instead of the swarm's (fences, listings, wait results, message addresses), with the record storing both. This is what makes shared memory and the messages channel usable with chosen names. It costs a new tool and a gate on every channel tool, a second name per member through the bus, its evidence and every channel, a rule for what peers see before a member registers (a placeholder id, which is an assigned name again), and it breaks the rule that status lines never contain a name a member chose ([swarm.md Delivery](swarm.md#delivery-peer-messages-are-model-output)), which would need its own security argument (a chosen name is model output, constrained by the grammar but still chosen to be read by peers). It also changes the condition being studied: names become a swarm-enforced, unique registry rather than something the members work out. No arm needs it with the filesystem channel; left for a later design if a study needs shared memory or messages with chosen names.

**Anonymous ids in channels with records.** Show other members as opaque ids (`m-3`) and the member itself as `you`. The ids are assigned names by another spelling, so they defeat the condition, and every channel would need a per-viewer renaming of its output. Not chosen.

**Drop the scratch sentence for self-naming members.** Simpler than the `<your name>` form, but members would then have no convention for private work in a shared sandbox, which is a different condition from the named arm in more than the name. Not chosen.

**Accept user channels whose instructions pass a check.** One check calls each channel's `instructions()` with the member's name and with a probe name and accepts the channel when the two texts are equal. Equal texts do not prove the name is absent: `"/scratch/" + (m.name if m.name.isidentifier() else "worker")` gives `/scratch/worker` for a member named `worker` and for the probe. Searching the text for the name fails both ways: a default name such as `solver` can appear in a channel's prose, and a transformed name does not match. Not chosen; no user channel needs to run beside members that choose.

**Let a channel declare that its instructions omit the name.** A `Channel` class attribute, say `chosen_names: ClassVar[bool] = False`, that a channel sets to `True` when its `instructions()` honours `member.choose_name`, with `swarm()` accepting only such channels. It would open the option to user channels on their author's word, as `reach` and `max_wait` do for other properties. Not needed while the filesystem channel is the only one a study uses; it is the natural extension when one is.

## Compatibility and migration

- **Public API.** `member(choose_name=)`, `Member.choose_name`, `MemberInfo.choose_name` and `MemberHandle.choose_name` are additive (`MemberHandle` is a protocol the runtime implements; controllers only read it) with a `False` default; existing calls, positional `MemberInfo(name, role)` included, are unchanged, and so are their preambles.
- **Channels.** Every channel keeps working in a swarm where no member chooses its name. With a member that chooses, only the built-in filesystem channel is accepted. `Channel`'s interface is unchanged; `FilesystemChannel.member_instructions()` is new and internal to the two built-in callers.
- **Logs and task identifiers.** The plan step's member dict gains `"choose_name": false`, written even at the default as every other member field is. Because the field is new, a task's `task_identifier()` changes on upgrade even with unchanged arguments, so continuing an eval set across the upgrade does not match its completed tasks and runs them again; an eval set compares arms run on one inspect_swarm version. That cost is accepted. Replaying an older log, whose member dicts lack the field, gives `False`.
- **Result record.** `MemberResult` gains a defaulted field in record version 1. Older records read with `False`; an older inspect_swarm reading a newer record ignores the field (`MemberResult` keeps pydantic's default `extra="ignore"`). No viewer types are generated from it.
- **Other designs.** This design amends [swarm-api.md](swarm-api.md) (the Names bullet, the `filesystem` Built-ins bullet in Channels, `start()`'s description of the preamble in Controllers, and the preamble line in Security) to say that a member with `choose_name` is not told its name and may choose one for itself, while the swarm's names stay the eval author's.

## Security

- **No new untrusted input reaches the swarm.** The option is the eval author's configuration. The name a member chooses stays in model output (files, messages, the transcript) and never enters text the swarm generates: not the preamble, not status lines, not fences, not records. The rules in [Rules this design keeps](#rules-this-design-keeps) hold unchanged.
- **Refusing every channel but the filesystem channel** is what keeps it so. A design that let chosen names through the bus would have to treat them as model output wherever they appear ([Alternatives considered](#alternatives-considered)).
- **The `<your name>` placeholder** is fixed text in the filesystem channel's template; nothing a member chose is substituted into it.
- **Collisions are not a security boundary.** Two members may choose the same name and use the same scratch directory. The filesystem channel is a convention, not isolation, today as before ([swarm-communication.md The filesystem channel](swarm-communication.md#the-filesystem-channel)).

## Testing

All in inspect_swarm, with mockllm and stub agents, on asyncio and trio, with no network or Docker:

- `tests/test_channel.py`: the preamble of a `choose_name=True` member, for two members and for one, with and without a role, with `filesystem()` (default and a custom `scratch`), `filesystem(shared_volume=True)` (with member sandboxes, as `tests/test_member_sandboxes.py` builds them) and `channels=[]`, both with the shared sample sandbox and with per-member sandboxes (`sandbox="{member}"`, where the own-sandbox sentence stays): it contains the new first sentence, the role line when set, the chooser's scratch sentence with `<your name>`, and none of the members' assigned names (`react-1`, ..., checked against every member's name, not just its own). The existing `preamble_recorder` reads the name from the preamble, so the new cases record by the member's `MemberContext` name or by call order instead.
- `tests/test_channel.py`, mixed roster: a member named `lead` beside a counted record with `choose_name=True`. The lead's first paragraph is unchanged and its scratch sentence is the mixed-roster one (`/scratch/lead`, with "some other members choose their own names" and without "which no other member uses"); the choosers get theirs. The existing all-named cases in `test_each_member_gets_each_channels_instructions` stay as they are, which shows that all-named preambles are unchanged.
- `tests/test_channel.py`, refusals: `swarm()` refuses `channel_test_locks()` (whose instructions use `member.name`) and the constant-instructions channel of `tests/test_bus.py` beside a `choose_name` member, with the message above, and accepts both when no member chooses.
- `tests/test_shared_memory_channel.py`: `swarm()` refuses `shared_memory()` with a `choose_name` member, and with a mixed roster where only one member chooses, also when `filesystem` is listed too; the message names the member and the channel.
- `tests/test_api_log.py`: the logged member dict includes `"choose_name"` (`test_logged_members`), replay rebuilds it (`test_replay_rebuilds_the_swarm`), and a `choose_name` arm in `test_eval_set_arms_are_distinct`'s parameters gives two arms differing only in it distinct identifiers.
- `tests/test_member.py`: a member given as a dict without `choose_name` validates with `False`.
- The result record: a swarm run with one choosing and one named member writes `choose_name` per member in `inspect_swarm:result` (in `tests/test_controller.py`, beside the existing record checks), and the exact set of member keys that `test_the_record_is_written_at_start_and_on_each_start_and_end` in `tests/test_final.py` expects gains `choose_name`, which the record now writes for every member.

## Implementation plan

One pull request, `feat: members that choose their own names`:

1. `src/inspect_swarm/_member.py`: `Member.choose_name`, `member(choose_name=)` with its docstring paragraph and argument, `_serialize()`.
2. `src/inspect_swarm/_controller/_controller.py`: `MemberInfo.choose_name`. `src/inspect_swarm/_controller/_control.py`: `MemberHandle.choose_name`, `MemberRun.choose_name`, `MemberRun.result()`, and `SwarmControl.start()`'s docstring (`:122`), whose preamble becomes "its name unless `choose_name=True`, its role and the roster size, how the budget is split, and each channel's instructions". `src/inspect_swarm/_bus/_bus.py`: the roster's `MemberInfo` carries it. `src/inspect_swarm/_record.py`: `MemberResult.choose_name`.
3. `src/inspect_swarm/_swarm.py`: the roster's `MemberInfo` carries it; `_preamble()`'s first paragraph and its `choosers` parameter; `_check_chosen_names()`; `swarm()`'s `Raises`.
4. `src/inspect_swarm/_channel/filesystem.py`: `member_instructions()` with the scratch sentences, `instructions()` delegating to it, and the `scratch` docstring.
5. User docs: `docs/members.qmd` (the option), `docs/channels.qmd` (the scratch wording for choosers and mixed rosters) and `docs/scoring.qmd` (the per-member `choose_name` field).
6. The tests above. The stand-in members that implement `MemberHandle` in `tests/test_bus.py`, `tests/test_shared_memory.py` and `tests/test_wait.py` gain `choose_name = False`.

## Open questions

None.

## Not this design

- **User channels beside members that choose**, through a channel's declaration that its instructions omit the name ([Alternatives considered](#alternatives-considered)).
- **Registering chosen names** with the swarm (a `swarm_name` tool), so that channels with records show them and the record stores them beside the assigned name. See [Alternatives considered](#alternatives-considered).
- **A silent variant** that leaves the name out without asking the member to choose one, to see whether members name themselves unprompted. It would be a second value of the option (for example `choose_name="silent"`); no arm asks for it yet.
- **Per-member users** ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md)): its user name and home (`swarm-<member>`, `/scratch/<member>`) expose the assigned name to `id` and `pwd`, so it must refuse a roster with any `choose_name=True` member; that refusal, its tests and its implementation step belong to the isolation design's own change.
- **Names in the eval author's own text**: the sample input, a controller's `start(input=...)`, the member agent's prompt, and sandbox configuration that names services after members. The swarm cannot see these and does not try.
