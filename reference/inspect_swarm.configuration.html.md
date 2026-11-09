# Configuration – Inspect Swarm

## Records

[member()](../reference/inspect_swarm.html.md#member) returns a [Member](../reference/inspect_swarm.configuration.html.md#member), and `swarm(budget=...)` takes a [Budget](../reference/inspect_swarm.configuration.html.md#budget). They are on their own page because [Member](../reference/inspect_swarm.configuration.html.md#member) and [member()](../reference/inspect_swarm.html.md#member) would share a heading anchor on one page.

### Member

A swarm member: a built agent, or a registered `@agent` factory and its arguments.

Create one with [member()](../reference/inspect_swarm.html.md#member), which documents the fields and the member contract.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/_member.py#L94)

``` python
class Member(BaseModel)
```

#### Attributes

`agent` [Agent](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#agent) \| None  
A built, registered agent. Exactly one of `agent` and `factory` is set.

`factory` AgentFactory \| None  
A registered `@agent` factory, called with `args` to build the member’s agent.

`args` dict\[str, Any\]  
The factory’s keyword arguments.

`name` str \| None  
The member’s name. `None` uses the agent’s or factory’s unqualified registry name.

`role` str \| None  
The member’s role, given to the controller and in the member’s preamble.

`choose_name` bool  
Whether the member chooses its own name. `True` leaves the member’s name out of the swarm’s preamble, which asks it to choose a name and tell the other members; the swarm still uses `name` for everything it records. A controller’s `start(preamble=...)` replaces that preamble with its own text, used as given, or `preamble=False` leaves it out; neither adds a request to choose a name.

`count` int  
How many members this record stands for. Above 1 needs the factory form.

`limits` list\[[Limit](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#limit)\]  
The member’s own limits.

`bridged` bool \| None  
Whether the member’s agent is bridged. `None` counts agents in the `inspect_swe/` namespace as bridged.

`sandbox` str \| None  
The member’s own sandbox environment, where `{member}` stands for the member’s name. `None` shares the sample’s sandbox.

`visible_sandboxes` list\[str\]  
Other sandbox environments the member can reach by name, such as a shared service. Only with `sandbox`.

`persistent` bool  
Whether the member stays available after it answers, held idle until the controller wakes it with `SwarmControl.wake()`.

### Budget

The swarm-wide budget.

By default the swarm derives its caps from the sample’s cost, token and time limits when it starts, holding back `reserve` of each. The caps are stopping rules: when one is reached the swarm stops its members, gives them up to `grace` seconds to finish the calls they have in flight, and then cancels the members still running. If the sample has a time limit, the grace period is capped at half its remaining time.

Every field is a plain value, so each can be a task parameter, and two budgets that differ in any field are logged as different arguments.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/41c5a3e21c8abea5fb39fc4a653538f6ad0a4e2d/src/inspect_swarm/_budget.py#L34)

``` python
class Budget(BaseModel)
```

#### Attributes

`cost` float \| None  
Swarm cost cap in dollars. `None` derives it from the sample’s cost limit when `derive` is `True`, and sets no cap when it is `False`.

`tokens` int \| str \| None  
Swarm token cap. A string such as `"output:1m"` is parsed by `parse_token_limit()` and keeps its metering type. `None` derives it from the sample’s token limit, with that limit’s metering type, when `derive` is `True`, and sets no cap when it is `False`.

`time` float \| None  
Seconds after which the swarm stops its members. `None` derives it from the sample’s time limit when `derive` is `True`, and sets no cap when it is `False`.

`reserve` float  
Fraction of each sample limit held back when a cap is derived.

`split` Literal\['pool', 'equal'\] \| tuple\[float, ...\]  
How the cost and token caps are divided between members: one shared pool, equal shares, or one weight per member in roster order. A member that uses up its share stops, and its unused share is not given to the others.

`grace` float  
Seconds members have, at most, to finish calls already in flight after a stop. If the sample has a time limit, this period is capped at half its remaining time.

`derive` bool  
Whether a cap left `None` is derived from the sample’s limit. `False` sets no such cap: the sample’s own limits then end the swarm as they end any solver.
