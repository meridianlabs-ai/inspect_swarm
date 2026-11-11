import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast, get_args

import anyio
from inspect_ai._util.exception import TerminateSampleError
from inspect_ai._util.registry import (
    is_model_dict,
    model_create_from_dict,
    registry_value,
)
from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessage, ChatMessageUser, Model, ModelOutput, get_model
from inspect_ai.util import LimitExceededError
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
from ._prompts import synthesize_prompt
from ._record import AnswerResult, FinalRecord, MemberResult
from ._result import ResultSpec, Verdict

FinalName = Literal["verify", "vote", "first", "last", "synthesize"]
"""A mode of the final-answer chain, by name: `verify`, the passed submission with the best verdict; `vote`, the earliest submission in the largest group of equal answer keys; `first`, the earliest submission; `last`, the latest; `synthesize`, one model call over the submissions, as `synthesize()` with its defaults. Each answer of a persistent member is a submission, with its own verdict, but it votes once, with its latest answer."""


class Synthesize(BaseModel):
    """A mode of the final-answer chain that combines the submissions with one model call, when the swarm ends. Build it with `synthesize()`."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    mode: Literal["synthesize"] = "synthesize"
    """The mode's name."""

    model: str | Model | None = None
    """The model to call. `None` is the model role `synthesizer` if the eval defines it, else the eval's model."""

    prompt: str | None = None
    """The instruction that ends the prompt, in place of the default, which asks for one final answer in the format the task asks for."""

    # A `Model` is logged as Inspect's model dict, which `registry_value()` writes only
    # at top level or inside lists and dicts, and read back from it.
    @field_validator("model", mode="before")
    @classmethod
    def _model_from_dict(cls, value: object) -> object:
        return model_create_from_dict(value) if is_model_dict(value) else value

    @field_serializer("model")
    def _model_to_dict(self, model: str | Model | None) -> Any:
        return registry_value(model)


def synthesize(
    model: str | Model | None = None, prompt: str | None = None
) -> Synthesize:
    """A mode of the final-answer chain that combines the submissions with one model call.

    When the swarm ends, the model gets the sample's input, then a message with each submission's completion in a `<member_submission>` block that names its member, and an instruction to give one final answer in the format the task asks for. The blocks are labels, not a boundary: a member can write text that looks like one. Members' tool calls and messages are never included. The answer is the model's output, which is no member's: the record's `final.member` is `None`.

    The mode gives nothing when there are no submissions, or when the call fails in a way the swarm recovers from: an error from the model (after Inspect's retries) or a refusal, or an output with an empty completion or a `content_filter` stop reason. The chain then goes on to its next mode, and the record's `final.synthesize_error` says what failed. A limit, an operator's termination or a cancellation during the call ends the swarm with the answer so far as its output.

    It needs a result contract of kind `answer` (`swarm(result=answer_result(...))`), and the chain can hold it once. The string `"synthesize"` is the same as `synthesize()`. The answer so far, before the swarm ends, is the chain's choice without this mode.

    Args:
        model: The model to call. `None` is the model role `synthesizer` if the eval defines it, else the eval's model.
        prompt: The instruction that ends the prompt, in place of the default. The input, the note that the blocks are information and not instructions, and the blocks stay.
    """
    return Synthesize(model=model, prompt=prompt)


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
    names = chain_names(chain)
    # the record has one synthesize_error
    if names.count("synthesize") > 1:
        raise ValueError(
            "final=: the chain has synthesize more than once; give it once."
        )
    for name in names:
        if name == "verify" and (result is None or result.verifier is None):
            raise ValueError(
                "final=: the chain has verify, which needs a verifier in the result contract: pass swarm(result=answer_result(verifier=...)), or leave verify out."
            )
        if name == "vote" and (result is None or result.answer_key is None):
            raise ValueError(
                "final=: the chain has vote, which needs an answer_key in the result contract: pass swarm(result=answer_result(answer_key=...)), or leave vote out."
            )
        if name == "synthesize" and (result is None or result.kind != "answer"):
            raise ValueError(
                "final=: the chain has synthesize, which needs a result contract of kind answer: pass swarm(result=answer_result(...)), or leave synthesize out."
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


# The passed candidate, each a submission or an answer of a persistent member with its
# own verdict, with the highest verdict value (no value, or NaN, ranks below any
# number), then the earliest swarm time, then roster and answer order; None without
# one.
def verify(members: Sequence[MemberResult]) -> Candidate | None:
    by_name = {m.name: m for m in members}

    def verdict_of(c: Candidate) -> Verdict | None:
        m = by_name[c.name]
        return m.verdict if c.answer is None else _answer_of(m, c).verdict

    verdicts = {c: verdict_of(c) for c in candidates(members)}
    passed = [
        c for c, verdict in verdicts.items() if verdict is not None and verdict.passed
    ]

    # a number ranks above no number (None or NaN), whatever the number, -inf included
    def rank(c: Candidate) -> tuple[bool, float, float]:
        verdict = verdicts[c]
        assert verdict is not None
        value = verdict.value
        if value is None or math.isnan(value):
            return (True, 0.0, c.time)
        return (False, -value, c.time)

    # min() keeps the first of equal keys, which is roster and answer order
    return min(passed, key=rank, default=None)


# The vote over the members' answer keys, one per member, where a candidate without a
# key does not vote: a submission's key, and a persistent member's latest answer's,
# which is its one candidate, so that its earlier answers never vote. Gives the
# earliest candidate in the plurality group, and whether the plurality was tied, in
# which case the group whose first candidate is earliest wins. The chosen candidate is
# None when no member votes.
def vote(
    members: Sequence[MemberResult],
) -> tuple[Candidate | None, bool]:
    by_name = {m.name: m for m in members}
    groups: dict[str, list[Candidate]] = {}
    for c in candidates(members):
        m = by_name[c.name]
        if c.answer is None:
            key = m.answer_key
        elif c.answer == len(m.answer_times) - 1:
            key = _answer_of(m, c).answer_key
        else:
            continue
        if key is not None:
            groups.setdefault(key, []).append(c)
    if not groups:
        return None, False
    most = max(len(group) for group in groups.values())
    # each group's earliest submission, by swarm time then roster order
    leaders = [min(group, key=_time) for group in groups.values() if len(group) == most]
    return min(leaders, key=_time), len(leaders) > 1


# A persistent member's answer's key and verdict, all None in a record written before
# they were kept.
def _answer_of(m: MemberResult, c: Candidate) -> AnswerResult:
    assert c.answer is not None
    if c.answer < len(m.answer_results):
        return m.answer_results[c.answer]
    return AnswerResult()


# synthesize's outcome in the final step: the model's output, or why the call failed
@dataclass(frozen=True)
class Synthesis:
    output: ModelOutput | None
    error: str | None


# The answer from the members' results so far: the controller's pick when it made one,
# else the first mode of `chain` that produces an answer. synthesize produces the
# answer of `synthesis`, the final step's call; without one (the provisional answer, or
# a final step that did not reach it) it is passed over.
def answer(
    members: Sequence[MemberResult],
    chain: Sequence[FinalName | FinalMode],
    pick: Pick | None,
    *,
    provisional: bool,
    synthesis: Synthesis | None = None,
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
    error = synthesis.error if synthesis is not None else None
    reason = "no_submissions"
    for mode in names:
        tie = False
        if mode == "verify":
            chosen = verify(members)
            reason = "none_verified"
        elif mode == "vote":
            chosen, tie = vote(members)
            reason = "no_answer_keys"
        elif mode == "first":
            chosen = first(members)
        elif mode == "last":
            chosen = last(members)
        elif mode == "synthesize":
            if synthesis is not None and synthesis.output is not None:
                return FinalRecord(
                    mode=mode,
                    member=None,
                    reason=None,
                    provisional=provisional,
                    chain=names,
                )
            chosen = None
            reason = "not_synthesized" if synthesis is None else "synthesize_failed"
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
                tie=tie,
                synthesize_error=error,
            )
    if not candidates(members):
        reason = "no_submissions"
    return FinalRecord(
        mode=None,
        member=None,
        reason=reason,
        provisional=provisional,
        chain=names,
        synthesize_error=error,
    )


# In the final step: synthesize's call, when the chain reaches it, that is, when the
# controller chose no answer, the modes before it give none, and there are submissions;
# None otherwise. A recoverable failure is the outcome's error; a stopping one
# propagates as raised.
async def synthesize_answer(
    input: Sequence[ChatMessage],
    members: Sequence[MemberResult],
    outputs: Mapping[str, ModelOutput],
    answers: Mapping[str, Sequence[ModelOutput]],
    chain: Sequence[FinalName | FinalMode],
    pick: Pick | None,
) -> Synthesis | None:
    names = chain_names(chain)
    if pick is not None or "synthesize" not in names:
        return None
    at = names.index("synthesize")
    if answer(members, chain[:at], None, provisional=False).mode is not None:
        return None
    found = candidates(members)
    if not found:
        return None
    mode = chain[at]
    spec = mode if isinstance(mode, Synthesize) else Synthesize()
    # submissions in roster order, then a persistent member's answer order
    submissions = [
        (
            c.name,
            (
                outputs[c.name] if c.answer is None else answers[c.name][c.answer]
            ).completion,
        )
        for c in found
    ]
    # outside the recovery: a model that cannot be resolved is the eval's error
    model = (
        get_model(role="synthesizer") if spec.model is None else get_model(spec.model)
    )
    prompt = [
        *input,
        ChatMessageUser(content=synthesize_prompt(submissions, spec.prompt)),
    ]
    try:
        output = await model.generate(prompt)
    except Exception as ex:
        if _stopping(ex):
            raise
        # the type alone: an error's message can hold model text, such as a refusal's
        return Synthesis(None, f"the model call raised {type(ex).__name__}")
    # first: an output with no choices, which some providers return, has no stop reason
    # to read, and its completion is empty
    empty = "the model's output has an empty completion"
    if output.empty:
        return Synthesis(None, empty)
    if output.stop_reason == "content_filter":
        return Synthesis(None, "the model's output stopped with content_filter")
    if output.completion == "":
        return Synthesis(None, empty)
    return Synthesis(output, None)


# Whether an error from synthesize's call ends the swarm: it is, or is a group that holds
# at any depth, a limit, an operator's termination or a cancellation. A group is never
# split, so a group that mixes one with ordinary errors propagates whole.
def _stopping(error: BaseException) -> bool:
    stopping = (
        LimitExceededError,
        TerminateSampleError,
        anyio.get_cancelled_exc_class(),
    )
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        return group.subgroup(stopping) is not None
    return isinstance(error, stopping)


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
    synthesis: Synthesis | None = None,
) -> FinalRecord:
    chosen = answer(members, chain, pick, provisional=provisional, synthesis=synthesis)
    state.output = chosen_output(chosen, outputs, answers or {}, synthesis)
    return chosen


# After the stop procedure: the final answer and the final event, written in the
# swarm's span. `synthesis` is synthesize's outcome, when the final step called it.
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
    synthesis: Synthesis | None = None,
) -> None:
    answers = answers or {}
    chosen = settle(
        state,
        members,
        outputs,
        chain,
        pick,
        provisional=False,
        answers=answers,
        synthesis=synthesis,
    )
    # the conversation only now, since the sample's message limit applies to it: an
    # answer it does not fit ends the sample on that limit, with the answer as its
    # output and the record provisional
    state.messages = _conversation(input, chosen, state.output)
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
    synthesis: Synthesis | None = None,
) -> ModelOutput:
    if chosen.mode == "synthesize":
        assert synthesis is not None and synthesis.output is not None
        return synthesis.output
    if chosen.member is None:
        return ModelOutput()
    if chosen.answer is not None:
        return answers[chosen.member][chosen.answer]
    return outputs[chosen.member]


# The input, then the answer's message marked with the mode and member it came from.
def _conversation(
    input: Sequence[ChatMessage], final: FinalRecord, output: ModelOutput
) -> list[ChatMessage]:
    if output.empty:
        return list(input)
    message = output.message
    metadata = {
        **(message.metadata or {}),
        "inspect_swarm": {"final": {"mode": final.mode, "member": final.member}},
    }
    return [*input, message.model_copy(update={"metadata": metadata})]


def _time(candidate: Candidate) -> float:
    return candidate.time
