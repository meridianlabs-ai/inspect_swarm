from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args

ResultKind = Literal["answer", "artifact"]
"""What a task's result is: `answer`, the member's answer, which the task's scorer grades without reading it from the environment; or `artifact`, the environment the members leave behind, which the team shares."""


@dataclass(frozen=True)
class Verdict:
    """A verifier's verdict on one submission."""

    passed: bool
    """Whether the submission passed."""

    value: float | None = None
    """An optional graded verdict; higher is better."""

    explanation: str | None = None
    """Why, for the record. It may quote the answer, so a controller never sees it."""


class Verifier(Protocol):
    """The task's in-loop checker: visible tests, a proof checker, a format check, a model judge.

    The swarm calls it on each submission while members run, one at a time, with the submission's completion and the sample's metadata. It may use `sandbox()`. It is never given the target, and must not read it or call `score()`, which runs the task's scorers with the target. An exception it raises fails the sample.
    """

    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict:
        """Check a submission.

        Args:
            answer: The submission's completion.
            metadata: The sample's metadata.

        Returns:
            The verdict.
        """
        ...


class MemberArtifacts(Protocol):
    """Puts one member's artifact where the task's scorer looks for the team's, for an `artifact` task."""

    def materialize(self, member: str) -> AbstractAsyncContextManager[None]:
        """Put this member's artifact in place, and restore the team's on exit, including on error.

        Args:
            member: The member's roster name.

        Returns:
            A context manager that holds the member's artifact in place while it is open.
        """
        ...


@dataclass(frozen=True)
class ResultSpec:
    """The task's result contract: what kind of result it has, and what it offers for checking and comparing submissions.

    Build one with `answer_result()` or `artifact_result()` and pass it to `swarm(result=...)`.
    """

    kind: ResultKind
    """What the task's result is."""

    answer_key: Callable[[str], str | None] | None = None
    """Maps a completion to a comparable key (extracted and normalised, such as a boxed number), or `None` when it has no comparable answer. It never sees the target. An exception it raises fails the sample."""

    verifier: Verifier | None = None
    """The task's in-loop checker, called on each submission."""

    member_artifacts: MemberArtifacts | None = None
    """Puts each member's artifact in place for scoring. `artifact` tasks only."""

    def __post_init__(self) -> None:
        if self.kind not in get_args(ResultKind):
            kinds = ", ".join(repr(kind) for kind in get_args(ResultKind))
            raise ValueError(
                f"ResultSpec: kind must be one of {kinds}, got {self.kind!r}."
            )
        if self.member_artifacts is not None and self.kind != "artifact":
            raise ValueError(
                "ResultSpec: member_artifacts is for kind='artifact' only, since an answer task's members leave no artifacts of their own."
            )


def answer_result(
    *,
    answer_key: Callable[[str], str | None] | None = None,
    verifier: Verifier | None = None,
) -> ResultSpec:
    """The result contract of a task scored on its answer.

    The task's scorer is a function of the answer: the output, the sample's input, metadata and target. It may use the sandbox to evaluate the answer (compile and run it), but never as a source of the answer.

    Args:
        answer_key: Maps a completion to a comparable key, or `None` when it has none.
        verifier: The task's in-loop checker, called on each submission.

    Returns:
        The contract, for `swarm(result=...)`.
    """
    return ResultSpec(kind="answer", answer_key=answer_key, verifier=verifier)


def artifact_result(
    *,
    answer_key: Callable[[str], str | None] | None = None,
    verifier: Verifier | None = None,
    member_artifacts: MemberArtifacts | None = None,
) -> ResultSpec:
    """The result contract of a task scored on what the members leave in the environment, such as a repository they edit together.

    The team has one result. The verifier, when given, checks the environment; it is still called with the submitting member's completion.

    Args:
        answer_key: Maps a completion to a comparable key, or `None` when it has none.
        verifier: The task's in-loop checker, called on each submission.
        member_artifacts: Puts each member's artifact in place for scoring.

    Returns:
        The contract, for `swarm(result=...)`.
    """
    return ResultSpec(
        kind="artifact",
        answer_key=answer_key,
        verifier=verifier,
        member_artifacts=member_artifacts,
    )


# The verifier's label in the record, for audit: its `name`, else its qualified name,
# else its class's.
def verifier_label(verifier: Verifier) -> str:
    name = getattr(verifier, "name", None)
    if isinstance(name, str):
        return name
    qualname = getattr(verifier, "__qualname__", None)
    if isinstance(qualname, str):
        return qualname
    return type(verifier).__qualname__
