"""Scorers that grade each member of a swarm.

`member_scores()` grades each member's output with the task's own scorer, for a task scored on its answer, or each member's artifact, for a task scored on what the members leave whose contract has `member_artifacts`. It gives the best and the mean member. The task's own scorer stays the headline score: list the member scorer after it, wrapped in a registered factory of the task's own so that re-scoring can rebuild it:

```python
from inspect_ai.scorer import Scorer, match, scorer

from inspect_swarm import ResultSpec, answer_result
from inspect_swarm.scorer import MEMBER_METRICS, member_scores


def math_result() -> ResultSpec:
    return answer_result(answer_key=boxed_number)


@scorer(metrics=MEMBER_METRICS)
def math_members() -> Scorer:
    return member_scores(match(), math_result())
```

The task then passes `result=math_result()` to `swarm()` or `baseline()`, and `scorer=[match(), math_members()]` to its `Task`.
"""

from ._member_scores import MEMBER_METRICS, member_scores

__all__ = ["MEMBER_METRICS", "member_scores"]
