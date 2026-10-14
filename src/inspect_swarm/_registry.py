import inspect
from functools import wraps
from typing import TYPE_CHECKING, Callable, Literal, ParamSpec, TypeVar, cast, overload

from inspect_ai._util.registry import (
    RegistryInfo,
    create_registry_object,
    is_registry_object,
    registry_add,
    registry_find,
    registry_info,
    registry_lookup,
    registry_name,
    registry_tag,
)

if TYPE_CHECKING:
    from ._channel._channel import Channel
    from ._controller._controller import Controller

SwarmRegistryType = Literal["swarm_controller", "swarm_channel"]

P = ParamSpec("P")
T = TypeVar("T")

PKG_NAME = "inspect_swarm"


def register_factory(
    registry_type: SwarmRegistryType,
    cls: type[T],
    factory: Callable[P, T],
    name: str | None,
) -> Callable[P, T]:
    registered_name = registry_name(factory, name or factory.__name__)
    info = RegistryInfo(type=registry_type, name=registered_name)

    @wraps(factory)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        instance: object = factory(*args, **kwargs)
        if not isinstance(instance, cls):
            raise TypeError(
                f"A @{registry_type} factory must return a {cls.__name__}, not a {type(instance).__name__}; {registered_name} did."
            )
        registry_tag(factory, instance, info.model_copy(deep=True), *args, **kwargs)
        return instance

    registry_add(wrapper, info)
    return wrapper


@overload
def resolve(
    registry_type: Literal["swarm_controller"], value: "str | Controller"
) -> "Controller": ...


@overload
def resolve(
    registry_type: Literal["swarm_channel"], value: "str | Channel"
) -> "Channel": ...


@overload
def resolve(
    registry_type: SwarmRegistryType, value: "str | Controller | Channel"
) -> "Controller | Channel": ...


def resolve(
    registry_type: SwarmRegistryType, value: "str | Controller | Channel"
) -> "Controller | Channel":
    # deferred: both modules import this one for their decorators
    from ._channel._channel import Channel
    from ._controller._controller import Controller

    cls = Controller if registry_type == "swarm_controller" else Channel
    if not isinstance(value, str):
        return _check_object(registry_type, cls, value)

    name, factory = _lookup(registry_type, value)
    required = [
        param.name
        for param in inspect.signature(factory).parameters.values()
        if param.default is param.empty
        and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
    ]
    if required:
        raise TypeError(
            f"{name} has required parameters ({', '.join(required)}), so it cannot be given by name; call {name}(...) and pass the object instead."
        )
    instance = create_registry_object(registry_type, name, {})
    return _check_object(registry_type, cls, instance)


def _lookup(
    registry_type: SwarmRegistryType, name: str
) -> tuple[str, Callable[..., object]]:
    # a bare name is the user's own unprefixed factory first, then a built-in
    candidates = [name] if "/" in name else [name, f"{PKG_NAME}/{name}"]
    for candidate in candidates:
        factory = registry_lookup(registry_type, candidate)
        if factory is not None:
            return candidate, cast(Callable[..., object], factory)
    registered = sorted(
        registry_info(o).name
        for o in registry_find(lambda info: info.type == registry_type)
    )
    raise ValueError(
        f"{name!r} is not a registered {registry_type}. Registered: {', '.join(registered) or 'none'}."
    )


def _check_object(registry_type: SwarmRegistryType, cls: type[T], value: object) -> T:
    if isinstance(value, cls) and is_registry_object(value, registry_type):
        return value
    if is_registry_object(value, registry_type):
        hint = f"{registry_info(value).name} is the factory; call it to make one."
    elif isinstance(value, cls):
        hint = f"this {cls.__name__} was not made by a @{registry_type} factory, so it cannot be logged."
    else:
        hint = f"got a {type(value).__name__}."
    raise TypeError(
        f"Expected a {registry_type} name or a {cls.__name__} from a @{registry_type} factory; {hint}"
    )
