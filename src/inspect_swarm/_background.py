from collections.abc import Callable, Coroutine, Sequence
from contextvars import Token
from types import TracebackType
from typing import Any, TypeVarTuple, Unpack

import anyio
from anyio.abc import TaskGroup
from inspect_ai.util import Limit, LimitExceededError
from inspect_ai.util._background import BackgroundTarget, background_target

from ._budget import MemberContext, bridged_background_error

PosArgsT = TypeVarTuple("PosArgsT")


# an error an agent raised to stop itself: a sourceless custom limit; here so that the
# member wrapper and the owner share it
def self_stop(error: BaseException) -> bool:
    return (
        isinstance(error, LimitExceededError)
        and error.type == "custom"
        and error.source is None
    )


# The owner of one member's background work: the target of inspect_ai's
# background_target while the member runs, entered once. Leaving it cancels the tasks
# still running and waits for them. A task that raises a bare limit error from one of
# `contain` (the member's nodes and the swarm's caps), or a sourceless custom limit
# error, which the task raised to stop itself, ends alone; any other error from
# a task cancels the block and the other tasks and is raised from it, as from the
# sample's task group. A bridged member's nodes cancel the whole member rather than
# raise, so the owner refuses background work in a member found bridged.
class MemberBackground:
    def __init__(self, member: MemberContext, contain: Sequence[Limit]) -> None:
        # deferred: the bus imports the runtime, which imports this module
        from ._bus._bus import current_bus

        self._member = member
        # the bus of the member's run, which work the member starts may not inherit, as
        # when a nested swarm's controller starts it
        self._bus = current_bus.get()
        self._contain = list(contain)
        # tasks background() has started in this owner
        self.started = 0
        # of those, not finished
        self.running = 0
        # of those, ended by a cancellation from the owner or an enclosing scope
        self.cancelled = 0
        self._tg: TaskGroup | None = None
        self._token: Token[BackgroundTarget | None] | None = None
        self._ended = False
        self._changed: anyio.Event | None = None

    async def __aenter__(self) -> "MemberBackground":
        self._changed = anyio.Event()
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._token = background_target.set(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._tg is not None and self._token is not None
        background_target.reset(self._token)
        self._tg.cancel_scope.cancel()
        single: BaseException | None = None
        try:
            await self._tg.__aexit__(exc_type, exc, tb)
        except BaseExceptionGroup as ex:
            # a task group wraps whatever ends it: one exception is raised bare
            if len(ex.exceptions) != 1:
                raise
            single = ex.exceptions[0]
        finally:
            self._ended = True
        if single is not None:
            # outside the handler, so the exception keeps its own cause and context
            raise single
        # the group's own cancel would absorb a cancellation from outside (the hard
        # stop) that reached the block; keep it
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            raise exc

    def start_soon(
        self,
        func: Callable[[Unpack[PosArgsT]], Coroutine[Any, Any, Any]],
        *args: Unpack[PosArgsT],
    ) -> None:
        if self._ended or self._tg is None:
            # a context saved inside the member and used after it ended
            raise RuntimeError(
                "background(): the member's background work has ended, so no new background work can start."
            )
        if self._member.bridged:
            raise bridged_background_error(self._member.name)
        # bridge use found from now on is refused
        self._member.background = True
        self._tg.start_soon(self._run, func, args)
        self.started += 1
        self.running += 1
        self._member.background_running += 1

    # returns once no task is running, including tasks started while waiting
    async def wait(self) -> None:
        while self.running:
            assert self._changed is not None
            await self._changed.wait()

    async def _run(
        self,
        func: Callable[..., Coroutine[Any, Any, Any]],
        args: tuple[Any, ...],
    ) -> None:
        try:
            await func(*args)
        except anyio.get_cancelled_exc_class():
            # counted as it leaves the task, so each task is counted once, whichever
            # scope cancelled it
            self.cancelled += 1
            raise
        except LimitExceededError as ex:
            if not (self_stop(ex) or any(ex.source is node for node in self._contain)):
                raise
        finally:
            self.running -= 1
            self._member.background_running -= 1
            assert self._changed is not None
            self._changed.set()
            self._changed = anyio.Event()
            if self._member.background_running == 0 and self._bus is not None:
                # the member may now be waiting, and so may every member
                self._bus.recheck()
