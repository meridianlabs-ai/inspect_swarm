# Restrictions – Inspect Swarm

## What the swarm does not support

- No background work in bridged members. A member cannot combine background work with the agent bridge. [background()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#background) raises in a bridged member, and a member that uses the bridge after starting background work ends `errored`.
- No channels with tools for bridged members. A bridged member cannot use the [shared-memory channel](./shared-memory.html.md) yet, and [swarm()](./reference/inspect_swarm.html.md#swarm) refuses one beside it.
- No checkpointing. Inspect’s sample checkpointing does not restore the swarm’s shared state or its caps, so a swarm raises an error before any member starts in a sample with checkpointing on. Turn checkpointing off for tasks that run a swarm: set `checkpoint=False` on `Task()` or [eval()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval), or remove every checkpoint setting, including `--checkpoint` and `INSPECT_EVAL_CHECKPOINT`.
- No ACP agents. ACP agents wait for their caller to drive them, so they are refused.
- No scorers that read a member’s tool state. Each member runs in its own tool-state scope, so built-in tools that keep state in the store ([memory()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#memory), [bash_session()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#bash_session), [web_browser()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#web_browser), the skill tool) keep each member’s at keys inside its scope, which differ from the default ones. Tool state that a setup solver prepared at the default instance is not carried into the members either; they start with fresh state ([Members](./members.html.md#tool-state)).

## Bridged members

A bridged member runs a CLI agent in the sandbox through Inspect’s agent bridge. `inspect_swe` agents count as bridged by default; pass `member(..., bridged=True)` for other agents that use the bridge. The swarm stops a bridged member by cancelling it.

The CLI’s own tool calls do not emit Inspect [ToolEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#toolevent)s, so the tool-event evidence described here does not cover them, Inspect Sentinel does not see them, and `longest_identical_tool_run` is `null` for bridged members.

## What the log records

- Members’ [ToolEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#toolevent)s are the record of what they did, except for bridged members.
- The shared-memory channel’s writes, views and waits are `comm` events ([Shared Memory](./shared-memory.html.md#the-record-of-the-board)).
- Files members share in a sandbox, tool instances they share, and a tool that reads the transcript are channels the swarm observes but does not record ([Channels](./channels.html.md#shared-files-are-observed-not-recorded)).

Spans inside a swarm log no [StoreEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#storeevent)s. The swarm’s span logs the net change to the store, which says what changed but not which member changed it. If an error, limit or cancellation escapes the swarm and ends the sample, the log may have no [StoreEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#storeevent) for the swarm’s writes. `sample.store` is always the record of the final store.

## The store keys

The swarm owns three keys in the sample store, written when the swarm ends: `inspect_swarm:result_id`, `inspect_swarm:ledger` and `inspect_swarm:metrics`. Task code must not write them, or an event with source `inspect_swarm`. The result record itself is an event in the transcript ([Scoring](./scoring.html.md#the-result-record)). A tool that reads the transcript sees every member’s model and tool calls, a channel between members that the swarm records no evidence of ([Scoring](./scoring.html.md#the-record-belongs-to-the-swarm)).
