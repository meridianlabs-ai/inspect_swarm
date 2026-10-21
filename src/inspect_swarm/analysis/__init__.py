"""Columns for Inspect's `samples_df()` with the swarm's per-sample cost and ledger data, and the ledger's models.

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

`SwarmColumns` are columns for `inspect_ai.analysis.samples_df()` with each sample's realized cost and what the swarm's ledger says about it. Use them beside Inspect's own columns, as in `samples_df(logs, columns=SampleSummary + SwarmColumns)`. They read the full sample (its store and `model_usage`):

- `swarm_cost`: realized cost in dollars, the priced cost in the sample's `model_usage`, scorer calls included. Tokens are in `SampleSummary`'s `total_tokens`.
- `swarm_complete`: no call is known to be missing from `swarm_cost`. The swarm's ledger marks its interval complete, and every model in the sample's `model_usage` is priced. The ledger judges only the swarm's interval; calls outside it, scorer calls included, are not checked for missing usage. `False` for a sample with no ledger, since nothing judged its spend.
- `swarm_members`: members in the swarm's roster.
- `swarm_resumed`: the sample resumed from a checkpoint, so its swarm restarted its caps from zero. `False` for a sample with no ledger.
- `swarm_cost_cap`, `swarm_token_cap`, `swarm_time_cap`: the swarm's caps, derived or explicit; the time cap in seconds.
- `swarm_errored`, `swarm_no_usage`, `swarm_unpriced`: model calls in the swarm that ended in an error or cancellation (retries included), that completed with no usage, and whose usage has no cost. Each makes the cost a lower bound. The ledger keeps the other reasons.
- `swarm_unpriced_models`: models with tokens and no cost, the ledger's and any in the sample's `model_usage`, as a JSON list.

The columns read from the ledger are null for a sample with no ledger. A sample that runs two swarms keeps only the last one's ledger, while its `model_usage` covers both. A ledger this inspect_swarm cannot read (a version other than 1) raises a `ValueError` naming the sample.

The columns read the sample's `store` and `model_usage`, so do not exclude either with `samples_df(exclude_fields=...)`.

The columns hold no per-member data. For that, read the whole ledger with `swarm_ledger()`. It returns a `SwarmLedger`, whose `members` are `MemberLedger`s with a `Meter` and `Calls` each. `SwarmLedger.model_json_schema()` describes every field.
"""

from .._ledger import Calls, MemberLedger, Meter, SwarmLedger
from ._columns import SwarmColumns, swarm_ledger

__all__ = [
    "Calls",
    "MemberLedger",
    "Meter",
    "SwarmColumns",
    "SwarmLedger",
    "swarm_ledger",
]
