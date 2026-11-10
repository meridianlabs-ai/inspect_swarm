import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args

from inspect_ai._util.registry import (
    is_model_dict,
    model_create_from_dict,
    registry_value,
)
from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessage, Model, ModelOutput
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
)

from ._evidence import swarm_event
from ._record import FinalRecord, MemberResult
from ._result import ResultSpec

FinalName = Literal["verify", "vote", "first", "last", "synthesize"]
"""A mode of the final-answer chain, by name: `verify`, the passed submission with the best verdict; `vote`, the earliest submission in the largest group of equal answer keys; `first`, the earliest submission; `last`, the latest; `synthesize`, reserved for a later release."""


class Synthesize(BaseModel):
    """Reserved: a mode that combines the submissions with one model call. The swarm does not run it yet."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    mode: Literal["synthesize"] = "synthesize"
    model: str | Model | None = None
    prompt: str | None = None

    # A `Model` is logged as Inspect's model dict, which `registry_value()` writes only
    # at top level or inside lists and dicts, and read back from it.
    @field_validator("model", mode="before")
    @classmethod
    def _model_from_dict(cls, value: object) -> object:
        return model_create_from_dict(value) if is_model_dict(value) else value

    @field_serializer("model")
    def _model_to_dict(self, model: str | Model | None) -> Any:
        return registry_value(model)


class Reporter(BaseModel):
    """Reserved: a mode that answers with one member's output, for coordinator topologies. No controller accepts it yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["reporter"] = "reporter"
    member: str


FinalMode = Annotated[Synthesize | Reporter, Field(discriminator="mode")]

FinalSpec = FinalName | FinalMode | Sequence[FinalName | FinalMode]
"""The final-answer chain: one mode, or a sequence of modes tried in order on the submissions, where the first that produces an answer wins."""

_FINAL_SPEC: TypeAdapter[FinalSpec] = TypeAdapter(FinalSpec)


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


# A controller's `final`, validated as a log gives it back (the dict form of a mode
# included); refuses a value that is not a chain, and an empty chain.
def check_final(final: object) -> FinalSpec | None:
    if final is None:
        return None
    try:
        spec = _FINAL_SPEC.validate_python(final)
    except ValidationError:
        names = ", ".join(repr(name) for name in get_args(FinalName))
        raise ValueError(
            f"final= must be one of {names}, a mode, a sequence of them, or None, got {final!r}."
        ) from None
    if not isinstance(spec, str | BaseModel) and len(spec) == 0:
        raise ValueError("final= is an empty chain; give at least one mode, or None.")
    return spec


# The chain the runtime applies: the controller's `final` as a list, or, for `None`,
# the default for the task's result contract: `verify` with a verifier, then `vote`
# with an answer key, then `first`.
def final_chain(
    final: FinalSpec | None, result: ResultSpec | None
) -> list[FinalName | FinalMode]:
    if final is None:
        chain: list[FinalName | FinalMode] = []
        if result is not None and result.verifier is not None:
            chain.append("verify")
        if result is not None and result.answer_key is not None:
            chain.append("vote")
        return [*chain, "first"]
    if isinstance(final, str | BaseModel):
        return [final]
    return list(final)


# The chain's name for each of its modes, as the record gives them.
def chain_names(chain: Sequence[FinalName | FinalMode]) -> list[str]:
    return [mode if isinstance(mode, str) else mode.mode for mode in chain]


# Refuses, when the swarm is built, a chain the task's result contract cannot support or
# the runtime cannot run.
def check_chain(
    chain: Sequence[FinalName | FinalMode], result: ResultSpec | None
) -> None:
    for name in chain_names(chain):
        if name == "verify" and (result is None or result.verifier is None):
            raise ValueError(
                "final=: the chain has verify, which needs a verifier in the result contract: pass swarm(result=answer_result(verifier=...)), or leave verify out."
            )
        if name == "vote" and (result is None or result.answer_key is None):
            raise ValueError(
                "final=: the chain has vote, which needs an answer_key in the result contract: pass swarm(result=answer_result(answer_key=...)), or leave vote out."
            )
        if name == "synthesize":
            raise ValueError(
                "final=: the chain has synthesize, which the swarm does not run yet. Use verify, vote, first or last."
            )
        if name == "reporter":
            raise ValueError(
                "final=: the chain has a reporter, which the swarm does not run yet; it is reserved for coordinator topologies."
            )


# Every candidate, in roster order and, within a persistent member, answer order: each
# answer of a persistent member, whatever its status, and the submission of any other
# member that ended with one.
def candidates(members: Sequence[MemberResult]) -> list[Candidate]:
    found: list[Candidate] = []
    for m in members:
        if m.persistent:
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


# The passed submission with the highest verdict value (no value, or NaN, ranks below
# any number), then the earliest swarm time, then roster order; None without one.
def verify(members: Sequence[MemberResult]) -> Candidate | None:
    verdicts = {m.name: m.verdict for m in members if not m.persistent}
    passed = [
        c
        for c in candidates(members)
        if (verdict := verdicts.get(c.name)) is not None and verdict.passed
    ]

    # a number ranks above no number (None or NaN), whatever the number, -inf included
    def rank(c: Candidate) -> tuple[bool, float, float]:
        verdict = verdicts[c.name]
        assert verdict is not None
        value = verdict.value
        if value is None or math.isnan(value):
            return (True, 0.0, c.time)
        return (False, -value, c.time)

    # min() keeps the first of equal keys, which is roster order
    return min(passed, key=rank, default=None)


# The vote over the submissions' answer keys, one per member, where a submission
# without a key does not vote: the earliest submission in the plurality group, the
# votes by key, and whether the plurality was tied, in which case the group whose first
# submission is earliest wins. The chosen candidate is None when no submission has a
# key.
def vote(
    members: Sequence[MemberResult],
) -> tuple[Candidate | None, dict[str, int], bool]:
    keys = {m.name: m.answer_key for m in members if not m.persistent}
    groups: dict[str, list[Candidate]] = {}
    for c in candidates(members):
        key = keys.get(c.name)
        if key is not None:
            groups.setdefault(key, []).append(c)
    votes = {key: len(group) for key, group in groups.items()}
    if not groups:
        return None, votes, False
    most = max(votes.values())
    # each group's earliest submission, by swarm time then roster order
    leaders = [min(group, key=_time) for group in groups.values() if len(group) == most]
    return min(leaders, key=_time), votes, len(leaders) > 1


# The answer from the members' results so far: the controller's pick when it made one,
# else the first mode of `chain` that produces an answer.
def answer(
    members: Sequence[MemberResult],
    chain: Sequence[FinalName | FinalMode],
    pick: Pick | None,
    *,
    provisional: bool,
) -> FinalRecord:
    names = chain_names(chain)
    if pick is not None:
        return FinalRecord(
            mode="controller",
            member=pick.member,
            reason=pick.reason,
            provisional=provisional,
            answer=pick.answer,
            chain=names,
        )
    votes: dict[str, int] | None = None
    reason = "no_submissions"
    for mode in names:
        tie = False
        if mode == "verify":
            chosen = verify(members)
            reason = "none_verified"
        elif mode == "vote":
            chosen, votes, tie = vote(members)
            reason = "no_answer_keys"
        elif mode == "first":
            chosen = first(members)
        elif mode == "last":
            chosen = last(members)
        else:
            # check_chain() refuses the modes the runtime does not run
            raise AssertionError(f"the swarm cannot apply the final mode {mode!r}")
        if chosen is not None:
            return FinalRecord(
                mode=mode,
                member=chosen.name,
                reason=None,
                provisional=provisional,
                answer=chosen.answer,
                chain=names,
                votes=votes,
                tie=tie,
            )
    if not candidates(members):
        reason = "no_submissions"
    return FinalRecord(
        mode=None,
        member=None,
        reason=reason,
        provisional=provisional,
        chain=names,
        votes=votes,
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
    chain: Sequence[FinalName | FinalMode],
    pick: Pick | None,
    *,
    provisional: bool,
    answers: Mapping[str, Sequence[ModelOutput]] | None = None,
) -> FinalRecord:
    chosen = answer(members, chain, pick, provisional=provisional)
    state.output = chosen_output(chosen, outputs, answers or {})
    return chosen


# After the stop procedure: the final answer and the final event, written in the
# swarm's span.
def finalise(
    state: AgentState,
    input: Sequence[ChatMessage],
    members: Sequence[MemberResult],
    outputs: Mapping[str, ModelOutput],
    chain: Sequence[FinalName | FinalMode],
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
        input, answer(members, chain, pick, provisional=False), outputs, answers
    )
    chosen = settle(
        state, members, outputs, chain, pick, provisional=False, answers=answers
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
def chosen_output(
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
    output = chosen_output(final, outputs, answers)
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
