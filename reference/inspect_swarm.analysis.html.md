# inspect_swarm.analysis – Inspect Swarm

## Columns

### SwarmColumns

Columns for `inspect_ai.analysis.samples_df()` with each sample’s realized cost and what the swarm’s ledger says about it, as in `samples_df(logs, columns=SampleSummary + SwarmColumns)`. The `inspect_swarm.analysis` module docstring lists every column.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/analysis/_columns.py#L111)

``` python
SwarmColumns: list[Column] = [
    SampleColumn("swarm_cost", path=_cost, type=float, full=True),
    SampleColumn("swarm_complete", path=_complete, type=bool, full=True),
    SampleColumn("swarm_members", path=_members, type=int, full=True),
    SampleColumn("swarm_resumed", path=_resumed, type=bool, full=True),
    SampleColumn("swarm_cost_cap", path=_cost_cap, type=float, full=True),
    SampleColumn("swarm_token_cap", path=_token_cap, type=float, full=True),
    SampleColumn("swarm_time_cap", path=_time_cap, type=float, full=True),
    SampleColumn("swarm_errored", path=_errored, type=int, full=True),
    SampleColumn("swarm_no_usage", path=_no_usage, type=int, full=True),
    SampleColumn("swarm_unpriced", path=_unpriced, type=int, full=True),
    SampleColumn("swarm_unpriced_models", path=_unpriced_models, full=True),
]
```

## The ledger

### swarm_ledger

Read the swarm’s ledger from a sample.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/analysis/_columns.py#L14)

``` python
def swarm_ledger(sample: EvalSample) -> SwarmLedger | None
```

`sample` [EvalSample](https://inspect.aisi.org.uk/reference/inspect_ai.log.html#evalsample)  
A sample read from a log with its store, for example from [read_eval_log()](https://inspect.aisi.org.uk/reference/inspect_ai.log.html#read_eval_log). A sample that runs two swarms keeps only the last one’s ledger.

### SwarmLedger

Realized usage and cost of one run of the swarm, and how complete they are.

The swarm writes its ledger once, when it ends, to the sample store as `inspect_swarm:ledger` and as a [SwarmEvent](https://inspect.aisi.org.uk/reference/inspect_ai.event.html#swarmevent) of kind `ledger` whose `data` is the store entry. Read it from a sample with [swarm_ledger()](../reference/inspect_swarm.analysis.html.md#swarm_ledger).

The store entry’s shape follows `version`. This inspect_swarm reads version 1 only.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/_ledger.py#L121)

``` python
class SwarmLedger(BaseModel)
```

#### Attributes

`version` Literal\[1\]  
Format version of the ledger.

`budget` [Budget](../reference/inspect_swarm.configuration.html.md#budget)  
The swarm’s budget as configured.

`caps` dict\[str, float \| None\]  
The swarm’s caps, derived or explicit, keyed by `cost` (dollars), `tokens` and `time` (seconds). `None` for a kind with no cap.

`sample_limits` dict\[str, float \| None\]  
The sample’s limits when the swarm started, keyed by `token`, `cost`, `message`, `turn`, `working` and `time`. `None` for a limit not set.

`members` list\[[MemberLedger](../reference/inspect_swarm.analysis.html.md#memberledger)\]  
Each member, in roster order.

`runtime` [Meter](../reference/inspect_swarm.analysis.html.md#meter)  
Calls made by the swarm’s runtime, such as the controller’s.

`final` [Meter](../reference/inspect_swarm.analysis.html.md#meter)  
Calls made by the final step, which chooses the answer.

`swarm` [Meter](../reference/inspect_swarm.analysis.html.md#meter)  
Every call in the swarm’s context: the members, the runtime, the final step and anything else.

`calls` [Calls](../reference/inspect_swarm.analysis.html.md#calls)  
Model events in the swarm’s span tree: the members, the runtime and the final step.

`unpriced_calls_recorded` int  
Calls the swarm meter recorded with usage but no cost.

`sample_delta` [ModelUsage](https://inspect.aisi.org.uk/reference/inspect_ai.model.html#modelusage)  
The sample’s model usage over the swarm’s lifetime, summed over its models.

`unattributed` [ModelUsage](https://inspect.aisi.org.uk/reference/inspect_ai.model.html#modelusage)  
What the sample used over the swarm’s lifetime that the swarm meter did not record: tokens are `sample_delta` minus `swarm.usage`, and `total_cost` is `sample_delta`’s cost minus `swarm.cost`.

`unpriced_models` list\[str\]  
Models in `sample_delta` with tokens but no cost.

`native_compaction_classes` list\[str\]  
Loaded [ModelAPI](https://inspect.aisi.org.uk/reference/inspect_ai.model.html#modelapi) classes that override `compact()`, when the swarm started or ended. Native compaction emits no event, so the ledger cannot count its calls.

`overshoot` dict\[str, float\]  
How far usage at the end went past each cap, keyed by `cost`, `tokens` and `time`. Only kinds over their cap appear.

`stop_reason` str \| None  
The reason the swarm stopped its members, from the first stop begun. `None` when no stop began.

`ended_by` str \| None  
The sample limit (`sample_limit:<type>`) or operator interrupt (`operator`) that ended the swarm, even during its own stop. `unknown` when an outside cause may have ended it but cannot be named. `None` when nothing from outside ended it.

`interrupted` bool  
The swarm did not finish: an error, a limit or a cancellation ended it before or during its final step.

`final_cancelled` bool  
The final step was cancelled.

`resumed` bool  
The sample resumed from a checkpoint, so the swarm restarted its caps from zero. Swarms now refuse checkpointing, so only logs from earlier versions can have it true.

`total_complete` bool  
No call is known to be missing from the swarm’s usage and cost: no member, member background task or final step was cancelled, the swarm was not interrupted, every call completed with priced usage, every model in `sample_delta` is priced, and no loaded model class compacts natively.

`attribution_complete` bool  
`total_complete`, and the swarm meter recorded all the sample used over the swarm’s lifetime: `unattributed` is zero, to within rounding.

### MemberLedger

One member’s entry in the swarm’s ledger.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/_ledger.py#L91)

``` python
class MemberLedger(BaseModel)
```

#### Attributes

`name` str  
The member’s name.

`status` str \| None  
How the member ended (`submitted`, `swarm_cap`, …), or `None` for a member never started.

`bridged` bool  
The member runs a CLI agent through Inspect’s agent bridge.

`meter` [Meter](../reference/inspect_swarm.analysis.html.md#meter)  
Usage and cost of the calls made in the member’s context.

`calls` [Calls](../reference/inspect_swarm.analysis.html.md#calls)  
Model events in the member’s spans.

`cancelled` bool  
The member’s agent was cancelled, or the swarm’s hard stop cancelled its background drain after the agent had returned. A hard stop during the drain keeps the agent’s status, so `cancelled` can be `True` with `status="submitted"`. The agent is cancelled by the hard stop after a grace period, a member time limit or a cancellation from outside the swarm. A bridged member is also cancelled at once when it reaches a cap, its share or one of its own limits, or makes a call after the swarm stopped it.

`background_started` int  
Tasks the member’s agent started with [background()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#background), such as [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent) background subagents.

`background_cancelled` int  
Of `background_started`, the tasks that were cancelled: still running when the member’s drain ended, or cancelled by an error from the member’s work or by the hard stop.

### Meter

Usage and cost recorded by one of the swarm’s meters.

A meter counts the model calls made in its context: one member’s, the runtime’s, the final step’s, or the whole swarm’s.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/_ledger.py#L49)

``` python
class Meter(BaseModel)
```

#### Attributes

`usage` [ModelUsage](https://inspect.aisi.org.uk/reference/inspect_ai.model.html#modelusage)  
Token usage of the calls recorded, except those made while token metering was suspended (approval and review calls).

`cost` float  
Cost in dollars of the priced calls recorded, suspended calls included.

`suspended_cost` float  
The part of `cost` recorded while token metering was suspended (approval and review calls).

### Calls

Counts of the model events in the swarm, or in one member’s spans.

`errored`, `no_usage` and `unpriced` count calls whose cost is missing, so any of them above zero makes the cost a lower bound.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/_ledger.py#L67)

``` python
class Calls(BaseModel)
```

#### Attributes

`calls` int  
Model calls that completed, cache replays excluded.

`cache_replays` int  
Model events replayed from the cache (`cache="read"`). They cost nothing.

`errored` int  
Model calls that ended in an error or were cancelled, plus the requests a provider’s client retried inside one call. Their usage is unknown.

`no_usage` int  
Model calls that completed with output but no usage.

`unpriced` int  
Model calls that completed with usage but no cost (`total_cost` is `None`).
