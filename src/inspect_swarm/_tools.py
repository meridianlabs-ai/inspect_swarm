from collections.abc import Callable

from inspect_ai.tool import Tool, tool

from ._budget import BudgetUsage, swarm_budget


@tool
def swarm_resources() -> Tool:
    """A tool that reports the swarm's budget to the member that calls it.

    It shows the caps that will stop the swarm, how much all members have used, the member's own share when the budget is split, and the time cap. Use it in place of a tool that reads `sample_limits()`, which shows the sample's limits rather than the swarm's caps.

    Returns:
        The tool.
    """

    async def execute() -> str:
        """Check the swarm's budget: the caps that stop the swarm, how much of each all members have used, and your own share when the budget is split.

        Returns:
            The budget, one line per cap.
        """
        budget = swarm_budget()
        tokens = (
            "Tokens" if budget.token_type == "all" else f"Tokens ({budget.token_type})"
        )
        lines = [
            *_lines("Cost", budget.cost, lambda v: f"${v:,.4f}"),
            *_lines(tokens, budget.tokens, lambda v: f"{v:,.0f}"),
            *_lines("Time", budget.time, lambda v: f"{v:,.0f} seconds"),
        ]
        return "\n".join(lines) or "The swarm has no cost, token or time cap."

    return execute


def _lines(
    kind: str, usage: BudgetUsage | None, show: Callable[[float], str]
) -> list[str]:
    if usage is None:
        return []
    lines = [
        f"{kind}: the swarm has used {show(usage.used)} of its cap of {show(usage.cap)}; {show(max(0.0, usage.cap - usage.used))} left."
    ]
    if usage.share is not None and usage.share_used is not None:
        lines.append(
            f"{kind}, your share: you have used {show(usage.share_used)} of {show(usage.share)}; {show(max(0.0, usage.share - usage.share_used))} left."
        )
    return lines
