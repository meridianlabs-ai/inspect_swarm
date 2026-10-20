"""Columns for Inspect's `samples_df()` with the swarm's per-sample cost and ledger data.

inspect_swarm supplies only what the swarm records. Reading logs, scores and statistics over the rows (means, fits) are Inspect's and the user's own dataframe work.

For example, the mean score and realized cost of each log (one log per arm), leaving out samples that resumed from a checkpoint:

```python
from inspect_ai.analysis import SampleSummary, samples_df
from inspect_ai.scorer import value_to_float

from inspect_swarm.analysis import SwarmColumns

df = samples_df("logs", columns=SampleSummary + SwarmColumns)
df = df[~df["swarm_resumed"]]
df["score"] = df["score_match"].map(value_to_float(), na_action="ignore")
df.groupby("log")[["score", "swarm_cost"]].mean()
```
"""

from ._columns import SwarmColumns

__all__ = ["SwarmColumns"]
