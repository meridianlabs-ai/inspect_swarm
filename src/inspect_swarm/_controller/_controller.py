from dataclasses import dataclass
from typing import Awaitable, Callable, ParamSpec, Sequence, overload

from .._final import FinalName, check_final
from .._registry import register_factory
from ._control import SwarmControl

P = ParamSpec("P")

StopReason = str
"""Why a controller's `run` returned: a topology reason such as `all_done`, or the controller's own, recorded as `controller:<reason>`."""


@dataclass(frozen=True)
class MemberInfo:
    """A member as the controller's `check` and a channel's `instructions()` see it."""

    name: str
    """The member's name."""

    role: str | None
    """The member's role, if it has one."""


class Controller:
    """How a swarm runs: which members start, when and with what input, and when the swarm is done.

    A `Controller` holds configuration only: one object serves every sample, so per-sample state belongs in `run`'s local variables. Return one from a factory decorated with `@swarm_controller`.
    """

    def __init__(
        self,
        run: Callable[[SwarmControl], Awaitable[StopReason]],
        *,
        final: FinalName | None = None,
        check: Callable[[Sequence[MemberInfo]], None] | None = None,
    ) -> None:
        """Create a controller.

        Args:
            run: Called once per sample. Starts members and waits for their events through the `SwarmControl`, and returns the stop reason. Returning requests the stop.
            final: How the swarm chooses its answer from the submissions, for the provisional answer and when the swarm ends: `first`, the earliest submission, or `last`, the latest. `None` is the default, `first`. When `run` names the answer with `SwarmControl.choose_answer()`, that choice is the answer instead.
            check: Called with the roster when the swarm is built. Raise `ValueError` for a roster the controller cannot run.

        Raises:
            ValueError: If `final` is not `first`, `last` or `None`.
        """
        check_final(final)
        self.run = run
        self.final: FinalName | None = final
        self.check = check


@overload
def swarm_controller(func: Callable[P, Controller], /) -> Callable[P, Controller]: ...


@overload
def swarm_controller(
    *, name: str | None = None
) -> Callable[[Callable[P, Controller]], Callable[P, Controller]]: ...


def swarm_controller(
    func: Callable[P, Controller] | None = None, /, *, name: str | None = None
) -> (
    Callable[P, Controller]
    | Callable[[Callable[P, Controller]], Callable[P, Controller]]
):
    """Decorator for registering swarm controllers.

    The factory's arguments are recorded with the controller, so they appear in the log and the controller can be rebuilt from it. A factory in a package is registered as `<package>/<name>`.

    Args:
        func: A function returning a `Controller`.
        name: The registered name, in place of the function's name.

    Returns:
        The factory, registered under the `swarm_controller` type.

    Raises:
        TypeError: When the factory is called, if it does not return a `Controller`.
    """
    if func is not None:
        return register_factory("swarm_controller", Controller, func, None)

    def decorate(func: Callable[P, Controller]) -> Callable[P, Controller]:
        return register_factory("swarm_controller", Controller, func, name)

    return decorate
