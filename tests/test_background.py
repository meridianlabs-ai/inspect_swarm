from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Literal

import anyio
import pytest
from inspect_ai.util import Limit, LimitExceededError, background, token_limit
from inspect_ai.util._background import background_target

from inspect_swarm._background import MemberBackground
from inspect_swarm._budget import MemberContext


def _member() -> MemberContext:
    return MemberContext("m", bridged=False)


def _limit_error(
    source: Limit | None, type: Literal["token", "custom"] = "token"
) -> Callable[[], Awaitable[None]]:
    async def raises() -> None:
        await anyio.sleep(0.01)
        raise LimitExceededError(type, value=2, limit=1, source=source)

    return raises


@pytest.mark.anyio
async def test_the_owner_runs_background_work_and_cancels_what_is_left() -> None:
    finished: list[str] = []
    cleaned: list[bool] = []

    async def short(name: str) -> None:
        await anyio.sleep(0.01)
        finished.append(name)

    async def forever() -> None:
        try:
            await anyio.sleep_forever()
        finally:
            cleaned.append(True)

    owner = MemberBackground(_member(), contain=[])
    async with owner:
        assert background_target.get() is owner
        # at once with none running
        await owner.wait()
        background(short, "a")
        assert (owner.started, owner.running) == (1, 1)

        async def later() -> None:
            await anyio.sleep(0.005)
            background(short, "b")

        background(later)
        # includes the task started while waiting
        await owner.wait()
        assert finished == ["a", "b"]
        assert (owner.started, owner.running, owner.cancelled) == (3, 0, 0)
        background(forever)
        await anyio.sleep(0.01)
    # its finally ran before the block was left
    assert cleaned == [True]
    assert (owner.started, owner.running, owner.cancelled) == (4, 0, 1)
    assert background_target.get() is None
    with pytest.raises(RuntimeError, match="background work has ended"):
        owner.start_soon(short, "c")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "contained,type,raised",
    [
        pytest.param(True, "token", False, id="contained"),
        # a sample limit
        pytest.param(False, "token", True, id="outside-contain"),
        pytest.param(None, "token", True, id="sourceless"),
        # the task stopped itself
        pytest.param(None, "custom", False, id="sourceless-custom"),
    ],
)
async def test_a_limit_from_a_contained_node_ends_only_its_task(
    contained: bool | None, type: Literal["token", "custom"], raised: bool
) -> None:
    member_node, sample_node = token_limit(10), token_limit(10)
    source = None if contained is None else member_node if contained else sample_node
    sibling_done = anyio.Event()
    block_done = False

    async def sibling() -> None:
        await anyio.sleep(0.05)
        sibling_done.set()

    owner = MemberBackground(_member(), contain=[member_node])
    with pytest.raises(LimitExceededError) if raised else nullcontext():
        async with owner:
            background(_limit_error(source, type))
            background(sibling)
            await anyio.sleep(0.1)
            block_done = True
    assert sibling_done.is_set() is not raised
    assert block_done is not raised
    assert owner.cancelled == (1 if raised else 0)


@pytest.mark.anyio
async def test_two_task_errors_are_raised_as_a_group() -> None:
    async def fails(message: str) -> None:
        raise RuntimeError(message)

    with pytest.raises(ExceptionGroup) as info:
        async with MemberBackground(_member(), contain=[]):
            background(fails, "one")
            background(fails, "two")
            await anyio.sleep_forever()
    assert sorted(str(e) for e in info.value.exceptions) == ["one", "two"]


async def _caused(where: str, chain: Literal["cause", "context"] = "cause") -> None:
    async def fails() -> None:
        try:
            raise ValueError("original")
        except ValueError as ex:
            if chain == "cause":
                raise RuntimeError("wrapped") from ex
            raise RuntimeError("wrapped")  # noqa: B904 (the implicit context is tested)

    async with MemberBackground(_member(), contain=[]):
        if where == "body":
            await fails()
        background(fails)
        await anyio.sleep_forever()


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["body", "task"])
async def test_an_error_keeps_its_cause(where: str) -> None:
    with pytest.raises(RuntimeError, match="wrapped") as info:
        await _caused(where)
    assert isinstance(info.value.__cause__, ValueError)
    assert str(info.value.__cause__) == "original"


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["body", "task"])
async def test_an_error_keeps_its_implicit_context(where: str) -> None:
    with pytest.raises(RuntimeError, match="wrapped") as info:
        await _caused(where, "context")
    assert isinstance(info.value.__context__, ValueError)
    assert str(info.value.__context__) == "original"
    assert not info.value.__suppress_context__


@pytest.mark.anyio
async def test_a_shielded_cleanup_holds_the_block_until_it_ends() -> None:
    cleaned = False

    async def shielded() -> None:
        nonlocal cleaned
        try:
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0.05)
            cleaned = True

    async with MemberBackground(_member(), contain=[]):
        background(shielded)
        await anyio.sleep(0.01)
    assert cleaned


async def _returns() -> None:
    await anyio.sleep(0.01)


async def _absorbs() -> None:
    # deepagent's agent_cancel: the child's own scope absorbs the cancellation
    with anyio.CancelScope() as inner:
        inner.cancel()
        await anyio.sleep_forever()


async def _by_exit(owner: MemberBackground) -> None:
    async with owner:
        background(anyio.sleep_forever)
        await anyio.sleep(0.01)


async def _by_foreign_error(owner: MemberBackground) -> None:
    async def fails() -> None:
        await anyio.sleep(0.01)
        raise RuntimeError("foreign")

    with pytest.raises(RuntimeError):
        async with owner:
            background(anyio.sleep_forever)
            background(fails)
            await anyio.sleep_forever()


async def _by_block_error(owner: MemberBackground) -> None:
    with pytest.raises(ValueError, match="block failed"):
        async with owner:
            background(anyio.sleep_forever)
            await anyio.sleep(0.01)
            raise ValueError("block failed")


async def _by_enclosing_scope(owner: MemberBackground) -> None:
    reached_after = False
    with anyio.CancelScope() as scope:
        async with owner:
            background(anyio.sleep_forever)
            await anyio.sleep(0.01)
            scope.cancel()
            try:
                await anyio.sleep_forever()
            finally:
                # the task finishes its cancellation before the owner's exit runs
                with anyio.CancelScope(shield=True):
                    await anyio.sleep(0.01)
                assert (owner.running, owner.cancelled) == (0, 1)
        reached_after = True
    # the owner re-raises the cancellation rather than absorbing it
    assert scope.cancelled_caught
    assert not reached_after


async def _by_nothing(owner: MemberBackground) -> None:
    async with owner:
        background(_returns)
        background(_absorbs)
        await owner.wait()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario,cancelled",
    [
        pytest.param(_by_exit, 1, id="owner-exit"),
        pytest.param(_by_foreign_error, 1, id="foreign-error"),
        pytest.param(_by_block_error, 1, id="block-error"),
        pytest.param(_by_enclosing_scope, 1, id="enclosing-scope"),
        pytest.param(_by_nothing, 0, id="returned-or-absorbed"),
    ],
)
async def test_each_way_the_block_ends_cancels_and_counts_its_tasks(
    scenario: Callable[[MemberBackground], Awaitable[None]], cancelled: int
) -> None:
    owner = MemberBackground(_member(), contain=[])
    await scenario(owner)
    assert owner.running == 0
    assert owner.cancelled == cancelled
    assert background_target.get() is None
