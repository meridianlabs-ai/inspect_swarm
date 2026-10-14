from typing import Callable, ParamSpec, overload

from .._controller._controller import MemberInfo
from .._registry import register_factory

P = ParamSpec("P")


class Channel:
    """A sanctioned way for members to share information.

    Subclass it and override `instructions()`, and return an instance from a factory decorated with `@swarm_channel`. A `Channel` holds configuration only: one object serves every sample.
    """

    def instructions(self, member: MemberInfo) -> str | None:
        """Text for the member's preamble.

        Args:
            member: The member the text is for.

        Returns:
            The text, or `None` to add nothing.
        """
        return None


@overload
def swarm_channel(func: Callable[P, Channel], /) -> Callable[P, Channel]: ...


@overload
def swarm_channel(
    *, name: str | None = None
) -> Callable[[Callable[P, Channel]], Callable[P, Channel]]: ...


def swarm_channel(
    func: Callable[P, Channel] | None = None, /, *, name: str | None = None
) -> Callable[P, Channel] | Callable[[Callable[P, Channel]], Callable[P, Channel]]:
    """Decorator for registering swarm channels.

    The factory's arguments are recorded with the channel, so they appear in the log and the channel can be rebuilt from it. A factory in a package is registered as `<package>/<name>`.

    Args:
        func: A function returning a `Channel`.
        name: The registered name, in place of the function's name.

    Returns:
        The factory, registered under the `swarm_channel` type.

    Raises:
        TypeError: When the factory is called, if it does not return a `Channel`.
    """
    if func is not None:
        return register_factory("swarm_channel", Channel, func, None)

    def decorate(func: Callable[P, Channel]) -> Callable[P, Channel]:
        return register_factory("swarm_channel", Channel, func, name)

    return decorate
