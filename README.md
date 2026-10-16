# Inspect Swarm

> [!WARNING]
> **Experimental.** Inspect Swarm is under active development. It has no stable API, no releases, and may change or be withdrawn without notice.

Inspect Swarm will support evals that use multi-agent swarms with [Inspect](https://inspect.aisi.org.uk).

More to come.

## Budgets and splits

A swarm takes its budget from the sample's limits. With the default `Budget()`, it reads the sample's cost, token and time limits when it starts and stops its members at 95% of each. The other 5% (`reserve`) covers calls already in flight when the swarm stops.

`Budget.split` divides the cost and token caps between members:

- `"pool"` (default): members share one budget, and any member may spend all of it.
- `"equal"`: each member gets an equal share.
- A tuple of weights, one per member in roster order: `(1, 1, 2)` gives the third member half.

A member that uses up its share stops. Its unused share is not given to the others, so the split stays a fixed condition of the experiment.

Agent count, budget and split are task parameters, so they can be swept:

```python
from typing import Literal

from inspect_ai import Task, task
from inspect_ai.agent import deepagent
from inspect_ai.tool import bash

from inspect_swarm import Budget, member, swarm


@task
def research(
    k: int = 4, budget: float = 40.0, split: Literal["pool", "equal"] = "pool"
) -> Task:
    return Task(
        dataset=...,
        solver=swarm(
            members=member(deepagent, args={"tools": [bash()]}, name="solver", count=k),
            budget=Budget(split=split),
        ),
        scorer=...,
        cost_limit=budget,
    )
```

The task sets the sample's cost limit once, and the swarm derives its cap from it, so a single-agent arm reads the same number. Three sweeps:

- Matched budget: vary `k` at a fixed `budget` with `split="pool"`. `k=1` is the single-agent arm.
- Agent count × per-agent budget `b`: vary `k` and `b`, with `budget = k * b` and `split="equal"`. Each member's share is `b * 0.95`.
- Split as an ablation: hold `k` and `budget` fixed and vary `split`.

For example, `inspect eval research.py -T k=8 -T budget=80 -T split=equal`. Compare arms on the cost the log records, not on the cap.
