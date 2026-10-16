from collections.abc import Sequence

from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessage, ModelOutput

from ._evidence import evidence
from ._record import FinalRecord, MemberResult, SwarmResultRecord, write_record


# The submission with the earliest swarm time, then roster order; None without one.
def first(members: Sequence[MemberResult]) -> MemberResult | None:
    submissions = [m for m in members if m.submitted]
    # min() keeps the first of equal keys, which is roster order
    return min(submissions, key=_time, default=None)


# Applies first to the members' results so far, sets the answer as the swarm state's
# output and writes the record: the provisional answer until finalisation.
# as_solver() copies the output to the task state however the swarm ends.
def settle(
    state: AgentState, members: Sequence[MemberResult], *, provisional: bool
) -> SwarmResultRecord:
    chosen = first(members)
    state.output = (
        chosen.output
        if chosen is not None and chosen.output is not None
        else ModelOutput()
    )
    final = (
        FinalRecord(
            mode="first", member=chosen.name, reason=None, provisional=provisional
        )
        if chosen is not None
        else FinalRecord(
            mode=None, member=None, reason="no_submissions", provisional=provisional
        )
    )
    record = SwarmResultRecord(members=list(members), final=final)
    write_record(record)
    return record


# After the stop procedure: the final answer, the final record and the final event,
# written in the swarm's span.
def finalise(
    state: AgentState,
    input: Sequence[ChatMessage],
    members: Sequence[MemberResult],
    *,
    span_id: str | None,
    stop_reason: str | None,
) -> None:
    # the conversation only now, since the sample's message limit applies to it: an
    # answer it does not fit ends the sample on that limit, with the answer as its
    # output and the record still provisional
    state.messages = _conversation(input, first(members))
    final = settle(state, members, provisional=False).final
    evidence(
        "result",
        "final",
        member=final.member,
        span_id=span_id,
        mode=final.mode,
        reason=final.reason,
        stop_reason=stop_reason,
    )


# The input, then the answer's message marked with the member it came from.
def _conversation(
    input: Sequence[ChatMessage], chosen: MemberResult | None
) -> list[ChatMessage]:
    if chosen is None or chosen.output is None or chosen.output.empty:
        return list(input)
    message = chosen.output.message
    metadata = {
        **(message.metadata or {}),
        "inspect_swarm": {"final": {"mode": "first", "member": chosen.name}},
    }
    return [*input, message.model_copy(update={"metadata": metadata})]


def _time(member: MemberResult) -> float:
    assert member.time is not None
    return member.time
