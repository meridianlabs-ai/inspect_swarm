from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessage, ModelOutput

from ._evidence import swarm_event
from ._operator import OPERATOR
from ._record import FinalRecord, MemberResult

FinalName = Literal["first", "last"]
"""How the swarm chooses its answer from the submissions: `first`, the earliest, or `last`, the latest. A tie goes to the member listed first in the roster, then to its earlier answer."""


# A controller's choice of the answer, made with `SwarmControl.choose_answer()`: for a
# persistent member, which of its answers.
@dataclass(frozen=True)
class Pick:
    member: str
    reason: str | None
    answer: int | None = None


# One answer that can be the swarm's: a non-persistent member's submission (`answer`
# None), at its end, or one of a persistent member's answers, by index.
@dataclass(frozen=True)
class Candidate:
    name: str
    answer: int | None
    time: float


# Refuses a final rule the runtime does not have.
def check_final(final: object) -> None:
    if final is not None and final not in get_args(FinalName):
        names = ", ".join(repr(name) for name in get_args(FinalName))
        raise ValueError(f"final= must be one of {names} or None, got {final!r}.")


# A member the operator stopped: none of its answers can be the swarm's, as a stopped
# member's output never is.
def withdrawn(member: MemberResult) -> bool:
    return member.end_origin == OPERATOR


# Every candidate, in roster order and, within a persistent member, answer order: each
# answer of a persistent member, whatever its status, unless the operator stopped it,
# and the submission of any other member that ended with one.
def candidates(members: Sequence[MemberResult]) -> list[Candidate]:
    found: list[Candidate] = []
    for m in members:
        if m.persistent:
            if withdrawn(m):
                continue
            found.extend(
                Candidate(m.name, i, time) for i, time in enumerate(m.answer_times)
            )
        elif m.submitted:
            assert m.time is not None
            found.append(Candidate(m.name, None, m.time))
    return found


# The candidate with the earliest swarm time, then roster and answer order; None
# without one.
def first(members: Sequence[MemberResult]) -> Candidate | None:
    # min() keeps the first of equal keys, which is roster and answer order
    return min(candidates(members), key=_time, default=None)


# The candidate with the latest swarm time, then roster and answer order; None without
# one.
def last(members: Sequence[MemberResult]) -> Candidate | None:
    # max() keeps the first of equal keys, which is roster and answer order
    return max(candidates(members), key=_time, default=None)


# The answer from the members' results so far: the controller's pick when it made one,
# unless the operator has since stopped that member, else `final`'s rule over the
# candidates. `final=None` is the default, `first`.
def answer(
    members: Sequence[MemberResult],
    final: FinalName | None,
    pick: Pick | None,
    *,
    provisional: bool,
) -> FinalRecord:
    if pick is not None and not any(
        withdrawn(m) for m in members if m.name == pick.member
    ):
        return FinalRecord(
            mode="controller",
            member=pick.member,
            reason=pick.reason,
            provisional=provisional,
            answer=pick.answer,
        )
    mode: FinalName = final or "first"
    chosen = last(members) if mode == "last" else first(members)
    if chosen is None:
        return FinalRecord(
            mode=None, member=None, reason="no_submissions", provisional=provisional
        )
    return FinalRecord(
        mode=mode,
        member=chosen.name,
        reason=None,
        provisional=provisional,
        answer=chosen.answer,
    )


# Applies answer() to the members' results so far and sets the chosen output as the
# swarm state's output: the provisional answer until finalisation. `outputs` are the
# ended non-persistent members' outputs by name, and `answers` the persistent members'
# answers by name, in order. as_solver() copies the output to the task state however
# the swarm ends. The record is written only when the swarm ends.
def settle(
    state: AgentState,
    members: Sequence[MemberResult],
    outputs: Mapping[str, ModelOutput],
    final: FinalName | None,
    pick: Pick | None,
    *,
    provisional: bool,
    answers: Mapping[str, Sequence[ModelOutput]] | None = None,
) -> FinalRecord:
    chosen = answer(members, final, pick, provisional=provisional)
    state.output = _output(chosen, outputs, answers or {})
    return chosen


# After the stop procedure: the final answer and the final event, written in the
# swarm's span.
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
    answers: Mapping[str, Sequence[ModelOutput]] | None = None,
) -> None:
    answers = answers or {}
    # the conversation only now, since the sample's message limit applies to it: an
    # answer it does not fit ends the sample on that limit, with the answer as its
    # output and the record provisional
    state.messages = _conversation(
        input, answer(members, final, pick, provisional=False), outputs, answers
    )
    chosen = settle(
        state, members, outputs, final, pick, provisional=False, answers=answers
    )
    swarm_event(
        "final",
        member=chosen.member,
        span_id=span_id,
        mode=chosen.mode,
        reason=chosen.reason,
        stop_reason=stop_reason,
        answer=chosen.answer,
    )


# The chosen answer's output; an empty output when there is no answer.
def _output(
    chosen: FinalRecord,
    outputs: Mapping[str, ModelOutput],
    answers: Mapping[str, Sequence[ModelOutput]],
) -> ModelOutput:
    if chosen.member is None:
        return ModelOutput()
    if chosen.answer is not None:
        return answers[chosen.member][chosen.answer]
    return outputs[chosen.member]


# The input, then the answer's message marked with the mode and member it came from.
def _conversation(
    input: Sequence[ChatMessage],
    final: FinalRecord,
    outputs: Mapping[str, ModelOutput],
    answers: Mapping[str, Sequence[ModelOutput]],
) -> list[ChatMessage]:
    output = _output(final, outputs, answers)
    if final.member is None or output.empty:
        return list(input)
    message = output.message
    metadata = {
        **(message.metadata or {}),
        "inspect_swarm": {"final": {"mode": final.mode, "member": final.member}},
    }
    return [*input, message.model_copy(update={"metadata": metadata})]


def _time(candidate: Candidate) -> float:
    return candidate.time
