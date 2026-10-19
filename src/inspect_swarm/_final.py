from collections.abc import Mapping, Sequence

from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessage, ModelOutput

from ._evidence import evidence
from ._record import FinalRecord, MemberResult, SwarmResultRecord, write_record


# The submission with the earliest swarm time, then roster order; None without one.
def first(members: Sequence[MemberResult]) -> MemberResult | None:
    submissions = [m for m in members if m.submitted]
    # min() keeps the first of equal keys, which is roster order
    return min(submissions, key=_time, default=None)


# Applies first to the members' results so far, sets the chosen member's output (from
# `outputs`, the ended members' outputs by name) as the swarm state's output and writes
# the record: the provisional answer until finalisation. as_solver() copies the output
# to the task state however the swarm ends.
def settle(
    state: AgentState,
    members: Sequence[MemberResult],
    outputs: Mapping[str, ModelOutput],
    *,
    provisional: bool,
) -> SwarmResultRecord:
    chosen = first(members)
    state.output = outputs[chosen.name] if chosen is not None else ModelOutput()
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
    outputs: Mapping[str, ModelOutput],
    *,
    span_id: str | None,
    stop_reason: str | None,
) -> None:
    # the conversation only now, since the sample's message limit applies to it: an
    # answer it does not fit ends the sample on that limit, with the answer as its
    # output and the record still provisional
    chosen = first(members)
    state.messages = _conversation(
        input, chosen.name if chosen is not None else None, outputs
    )
    final = settle(state, members, outputs, provisional=False).final
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
    input: Sequence[ChatMessage], chosen: str | None, outputs: Mapping[str, ModelOutput]
) -> list[ChatMessage]:
    if chosen is None or outputs[chosen].empty:
        return list(input)
    message = outputs[chosen].message
    metadata = {
        **(message.metadata or {}),
        "inspect_swarm": {"final": {"mode": "first", "member": chosen}},
    }
    return [*input, message.model_copy(update={"metadata": metadata})]


def _time(member: MemberResult) -> float:
    assert member.time is not None
    return member.time
