"""Read the swarm's per-sample cost and ledger data, to join with Inspect's dataframes.

The helpers here read only what the swarm records. Statistics over the rows (means, fits, scores) are the user's own dataframe work.

For example, the mean score and realized cost of each log (one log per arm), leaving out samples that resumed from a checkpoint:

```python
import pandas as pd
from inspect_ai.analysis import samples_df
from inspect_ai.scorer import value_to_float

from inspect_swarm.analysis import sample_costs

costs = pd.DataFrame([row.model_dump() for row in sample_costs("logs")])
df = samples_df("logs").merge(costs, on=["log", "id", "epoch"])
df = df[~df["resumed"]]
df["score"] = df["score_match"].map(value_to_float())
df.groupby("log")[["score", "cost"]].mean()
```
"""

from ._cost import SampleCost, sample_costs

__all__ = ["SampleCost", "sample_costs"]
