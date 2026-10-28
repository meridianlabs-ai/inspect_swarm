from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessage, ModelOutput

from ._evidence import evidence
from ._record import FinalRecord, MemberResult, SwarmResultRecord, write_record

FinalName = Literal["first", "last"]
"""How the swarm chooses its answer from the submissions: `first`, the earliest, or `last`, the latest. A tie goes to the member listed first in the roster."""


# A controller's choice of the answer, made with `SwarmControl.choose_answer()`.
@dataclass(frozen=True)
class Pick:
    member: str
    reason: str | None


# Refuses a final rule the runtime does not have.
def check_final(final: object) -> None:
    if final is not None and final not in get_args(FinalName):
        names = ", ".join(repr(name) for name in get_args(FinalName))
        raise ValueError(f"final= must be one of {names} or None, got {final!r}.")


# The submission with the earliest swarm time, then roster order; None without one.
def first(members: Sequence[MemberResult]) -> MemberResult | None:
    submissions = [m for m in members if m.submitted]
    # min() keeps the first of equal keys, which is roster order
    return min(submissions, key=_time, default=None)


# The submission with the latest swarm time, then roster order; None without one.
def last(members: Sequence[MemberResult]) -> MemberResult | None:
    submissions = [m for m in members if m.submitted]
    # max() keeps the first of equal keys, which is roster order
    return max(submissions, key=_time, default=None)


# The answer from the members' results so far: the controller's pick when it made one,
# else `final`'s rule over the submissions. `final=None` is the default, `first`.
def answer(
    members: Sequence[MemberResult],
    final: FinalName | None,
    pick: Pick | None,
    *,
    provisional: bool,
) -> FinalRecord:
    if pick is not None:
        return FinalRecord(
            mode="controller",
            member=pick.member,
            reason=pick.reason,
            provisional=provisional,
        )
    mode: FinalName = final or "first"
    chosen = last(members) if mode == "last" else first(members)
    if chosen is None:
        return FinalRecord(
            mode=None, member=None, reason="no_submissions", provisional=provisional
        )
    return FinalRecord(
        mode=mode, member=chosen.name, reason=None, provisional=provisional
    )


# Applies answer() to the members' results so far, sets the chosen member's output
# (from `outputs`, the ended members' outputs by name) as the swarm state's output and
# writes the record: the provisional answer until finalisation. as_solver() copies the
# output to the task state however the swarm ends.
def settle(
    state: AgentState,
    members: Sequence[MemberResult],
    outputs: Mapping[str, ModelOutput],
    final: FinalName | None,
    pick: Pick | None,
    *,
    provisional: bool,
) -> SwarmResultRecord:
    record = SwarmResultRecord(
        members=list(members),
        final=answer(members, final, pick, provisional=provisional),
    )
    chosen = record.final.member
    state.output = outputs[chosen] if chosen is not None else ModelOutput()
    write_record(record)
    return record


# After the stop procedure: the final answer, the final record and the final event,
# written in the swarm's span.
def finalise(
    state: AgentState,
    input: Sequence[ChatMessage],
    members: Sequence[MemberResult],
    outputs: Mapping[str, ModelOutput],
    final: FinalName | None,
    pick: Pick | None,
    *,
    span_id: str | None,
    stop_reason: str | None,
) -> None:
    # the conversation only now, since the sample's message limit applies to it: an
    # answer it does not fit ends the sample on that limit, with the answer as its
    # output and the record still provisional
    state.messages = _conversation(
        input, answer(members, final, pick, provisional=False), outputs
    )
    chosen = settle(state, members, outputs, final, pick, provisional=False).final
    evidence(
        "result",
        "final",
        member=chosen.member,
        span_id=span_id,
        mode=chosen.mode,
        reason=chosen.reason,
        stop_reason=stop_reason,
    )


# The input, then the answer's message marked with the mode and member it came from.
def _conversation(
    input: Sequence[ChatMessage],
    final: FinalRecord,
    outputs: Mapping[str, ModelOutput],
) -> list[ChatMessage]:
    if final.member is None or outputs[final.member].empty:
        return list(input)
    message = outputs[final.member].message
    metadata = {
        **(message.metadata or {}),
        "inspect_swarm": {"final": {"mode": final.mode, "member": final.member}},
    }
    return [*input, message.model_copy(update={"metadata": metadata})]


def _time(member: MemberResult) -> float:
    assert member.time is not None
    return member.time
