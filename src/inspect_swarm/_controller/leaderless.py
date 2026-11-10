from contextlib import aclosing

from .._final import FinalSpec, chain_names, check_final, final_chain
from ._control import MemberEnded, MemberIdle, SwarmControl
from ._controller import Controller, StopReason, swarm_controller


@swarm_controller
def leaderless(
    stop_on_submission: bool = True,
    *,
    final: FinalSpec | None = None,
    stop_on_verified: bool = False,
) -> Controller:
    """Start every member with the swarm's input; stop at the first submission, or when none is still working.

    By default the swarm stops when the first member submits. The members still running are stopped: they finish the calls they have in flight, within `Budget.grace`, and the ones still running after it are cancelled. The swarm's answer is chosen from the submissions made by then. The default chain depends on the result contract; with `first` alone, later submissions cannot change the answer.

    A persistent member's answer is a submission: it stops the swarm by default. `leaderless` never wakes a member, so with `stop_on_submission=False` a persistent member answers once and stays idle until the swarm stops.

    Pass `stop_on_submission=False` to let every member run to its end when the other members' work still matters: tasks whose scorer reads a shared artifact (such as the sandbox) after the members end, where members still working change what is scored; arms that measure each member's result (team@k); chains that compare submissions, such as `verify` or `vote`; and agents for which any normal return counts as a submission, so that a member that gives up would end the swarm. Add `final="last"` to make the latest submission the answer, for a team whose members build on each other's work, or `stop_on_verified=True` to stop at the first submission that passes the result contract's verifier.

    Args:
        stop_on_submission: Stop the swarm when the first member ends with a submission.
        final: How the swarm chooses its answer from the submissions: one mode or a chain of modes, tried in order until one gives an answer (see `Controller`). `None` is the default chain for the swarm's result contract: `verify` if it has a verifier, then `vote` if it has an answer key, then `first`. Any other value than `first` or `None` needs `stop_on_submission=False`.
        stop_on_verified: Stop the swarm when a submission passes the result contract's verifier, including any answer of a persistent member. Needs `stop_on_submission=False`. Without a verifier, no submission has a verdict, so it never stops the swarm.

    Returns:
        The controller, which returns the stop reason `verified` when a passed submission stopped the swarm, `submitted` when a member's submission stopped it, `quiescence` when every member ended or is idle and at least one is idle, and `all_done` when every member ended.

    Raises:
        ValueError: If `final` is not a mode, a chain of modes or `None`, has a `reporter` mode, or is other than `first` or `None` while `stop_on_submission` is `True`; or if `stop_on_verified` and `stop_on_submission` are both `True`.
    """
    final = check_final(final)
    names = None if final is None else chain_names(final_chain(final, None))
    if names is not None and "reporter" in names:
        raise ValueError(
            "leaderless(final=...): a leaderless swarm has no reporter; a reporter mode is for coordinator topologies."
        )
    if stop_on_submission and names is not None and names != ["first"]:
        raise ValueError(
            f"leaderless(final={final!r}) needs stop_on_submission=False, so that the members run on after the first submission and the chain has their submissions to choose from."
        )
    if stop_on_submission and stop_on_verified:
        raise ValueError(
            "leaderless(stop_on_verified=True) needs stop_on_submission=False: otherwise the first submission stops the swarm whether or not it passes."
        )

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                # a persistent member's answer comes with its verdict on MemberIdle
                if (
                    stop_on_verified
                    and isinstance(event, MemberEnded | MemberIdle)
                    and event.verdict is not None
                    and event.verdict.passed
                ):
                    return "verified"
                # a persistent member's answer is a submission
                if stop_on_submission and (
                    isinstance(event, MemberIdle)
                    or (isinstance(event, MemberEnded) and event.submitted)
                ):
                    return "submitted"
        if any(m.state == "idle" for m in swarm.members):
            return "quiescence"
        return "all_done"

    return Controller(run, final=final)
