from collections.abc import Sequence
from contextlib import aclosing
from typing import Literal

from .._final import FinalSpec, chain_names, check_final, final_chain
from ._control import MemberEnded, MemberIdle, SwarmControl
from ._controller import Controller, StopReason, swarm_controller

_STOPS = ("answer", "end")


# Which early stop leaderless arms: the first submission, the first passing submission,
# or none, from the leading mode of the chain the swarm applies.
def _early_stop(
    chain: Sequence[str], stop: Literal["answer", "end"]
) -> Literal["submission", "verified"] | None:
    if stop == "end":
        return None
    if chain[0] == "first":
        return "submission"
    if chain[0] == "verify":
        return "verified"
    return None


@swarm_controller
def leaderless(
    *,
    stop: Literal["answer", "end"] = "answer",
    final: FinalSpec | None = None,
) -> Controller:
    """Start every member with the swarm's input; stop when the answer can no longer change, or when none is still working.

    With `stop="answer"`, the default, the leading mode of the swarm's final-answer chain decides when to stop. Under a chain led by `first`, the swarm stops at the first submission. Under a chain led by `verify`, it stops at the first submission that passes the result contract's verifier; when none passes, every member runs to its end and the rest of the chain chooses from all their submissions. Under any other chain (`vote`, `last`, `synthesize`), every member runs to its end. The chain is `final`, or the default for the swarm's result contract when `final` is `None`, so a verifier or an answer key in the contract changes the default stop.

    When the swarm stops, the members still running finish the calls they have in flight, within `Budget.grace`, and the ones still running after it are cancelled. The swarm's answer is chosen from the submissions made by then.

    A persistent member's answer is a submission, with its verdict. `leaderless` never wakes a member, so when no stop fires a persistent member answers once and stays idle until the swarm stops.

    Pass `stop="end"` to let every member run to its end whatever the chain: for tasks whose scorer reads a shared artifact (such as the sandbox) after the members end, where members still working change what is scored; arms that measure each member's result (team@k, `member_scores()`); verifiers whose passes are graded with `Verdict.value`, where a later pass could rank higher; and agents for which any normal return counts as a submission, so that a member that gives up would end the swarm.

    Args:
        stop: When the swarm stops. `"answer"` stops it as soon as its answer can no longer change, as the leading mode of the chain decides; `"end"` lets every member run to its end.
        final: How the swarm chooses its answer from the submissions: one mode or a chain of modes, tried in order until one gives an answer (see `Controller`). `None` is the default chain for the swarm's result contract: `verify` if it has a verifier, then `vote` if it has an answer key, then `first`.

    Returns:
        The controller, which returns the stop reason `verified` when a passed submission stopped the swarm, `submitted` when a member's submission stopped it, `quiescence` when every member ended or is idle and at least one is idle, and `all_done` when every member ended.

    Raises:
        ValueError: If `stop` is not `"answer"` or `"end"`, or if `final` is not a mode, a chain of modes or `None`, or has a `reporter` mode.
    """
    if stop not in _STOPS:
        raise ValueError(f'stop= must be "answer" or "end", got {stop!r}.')
    final = check_final(final)
    if final is not None and "reporter" in chain_names(final_chain(final, None)):
        raise ValueError(
            "leaderless(final=...): a leaderless swarm has no reporter; a reporter mode is for coordinator topologies."
        )

    async def run(swarm: SwarmControl) -> StopReason:
        early = _early_stop(swarm.final_chain, stop)
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                # a persistent member's answer comes with its verdict on MemberIdle
                if (
                    early == "verified"
                    and isinstance(event, MemberEnded | MemberIdle)
                    and event.verdict is not None
                    and event.verdict.passed
                ):
                    return "verified"
                # a persistent member's answer is a submission
                if early == "submission" and (
                    isinstance(event, MemberIdle)
                    or (isinstance(event, MemberEnded) and event.submitted)
                ):
                    return "submitted"
        if any(m.state == "idle" for m in swarm.members):
            return "quiescence"
        return "all_done"

    return Controller(run, final=final)
