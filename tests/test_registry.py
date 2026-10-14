import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable

import pytest
from inspect_ai._util.registry import (
    registry_arg,
    registry_info,
    registry_lookup,
    registry_params,
    registry_value,
)

from inspect_swarm._channel._channel import Channel, swarm_channel
from inspect_swarm._controller._control import SwarmControl
from inspect_swarm._controller._controller import (
    Controller,
    StopReason,
    swarm_controller,
)
from inspect_swarm._registry import SwarmRegistryType, resolve


def _controller() -> Controller:
    async def run(control: SwarmControl) -> StopReason:
        return "all_done"

    return Controller(run)


@dataclass(frozen=True)
class Kind:
    registry_type: SwarmRegistryType
    decorator: Any
    cls: type[Any]
    make: Callable[[], object]


CONTROLLER = Kind("swarm_controller", swarm_controller, Controller, _controller)
CHANNEL = Kind("swarm_channel", swarm_channel, Channel, Channel)
KINDS = [pytest.param(CONTROLLER, id="controller"), pytest.param(CHANNEL, id="channel")]


def _factory(
    kind: Kind,
    func_name: str,
    *,
    builtin: bool = False,
    name: str | None = None,
    returns: Callable[[], object] | None = None,
) -> Callable[..., Any]:
    def factory() -> object:
        return (returns or kind.make)()

    factory.__name__ = factory.__qualname__ = func_name
    if builtin:
        # registry_name() prefixes the package a factory is defined in
        factory.__module__ = "inspect_swarm._registry"
    decorated: Callable[..., Any] = (
        kind.decorator(name=name)(factory) if name else kind.decorator(factory)
    )
    return decorated


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "func_name,builtin,name,expected",
    [
        ("reg_plain", False, None, "reg_plain"),
        ("reg_func", False, "reg_renamed", "reg_renamed"),
        ("reg_plain_builtin", True, None, "inspect_swarm/reg_plain_builtin"),
        (
            "reg_func_builtin",
            True,
            "reg_renamed_builtin",
            "inspect_swarm/reg_renamed_builtin",
        ),
    ],
)
def test_registers_under_name(
    kind: Kind, func_name: str, builtin: bool, name: str | None, expected: str
) -> None:
    factory = _factory(kind, func_name, builtin=builtin, name=name)

    assert registry_lookup(kind.registry_type, expected) is factory
    instance = factory()
    assert isinstance(instance, kind.cls)
    info = registry_info(instance)
    assert (info.type, info.name) == (kind.registry_type, expected)


@pytest.mark.parametrize(
    "kind,returns",
    [
        pytest.param(CONTROLLER, Channel, id="controller"),
        pytest.param(CHANNEL, _controller, id="channel"),
    ],
)
def test_factory_returning_wrong_type_raises(
    kind: Kind, returns: Callable[[], object]
) -> None:
    factory = _factory(kind, "reg_wrong_type", returns=returns)

    with pytest.raises(TypeError, match=f"must return a {kind.cls.__name__}"):
        factory()


@pytest.mark.parametrize("kind", KINDS)
def test_logs_and_rebuilds_with_params(kind: Kind) -> None:
    def reg_logged(size: int, label: str = "a") -> object:
        return kind.make()

    factory = kind.decorator(reg_logged)
    instance = factory(3, label="b")

    logged = registry_value(instance)
    assert logged == {
        "type": kind.registry_type,
        "name": "reg_logged",
        "params": {"size": 3, "label": "b"},
    }
    rebuilt = registry_arg(logged)
    assert isinstance(rebuilt, kind.cls)
    assert registry_params(rebuilt) == {"size": 3, "label": "b"}


@pytest.fixture
def resolvable(request: pytest.FixtureRequest) -> Kind:
    kind: Kind = request.param
    _factory(kind, "res_builtin", builtin=True)
    _factory(kind, "res_shadowed", builtin=True)
    _factory(kind, "res_shadowed")

    def res_required(size: int) -> object:
        return kind.make()

    kind.decorator(res_required)
    return kind


@pytest.mark.parametrize("resolvable", KINDS, indirect=True)
@pytest.mark.parametrize(
    "name,expected",
    [
        ("inspect_swarm/res_builtin", "inspect_swarm/res_builtin"),
        ("res_builtin", "inspect_swarm/res_builtin"),
        ("res_shadowed", "res_shadowed"),
        ("inspect_swarm/res_shadowed", "inspect_swarm/res_shadowed"),
    ],
)
def test_resolves_names(resolvable: Kind, name: str, expected: str) -> None:
    resolved = resolve(resolvable.registry_type, name)

    assert isinstance(resolved, resolvable.cls)
    assert registry_info(resolved).name == expected
    assert registry_value(resolved) == {
        "type": resolvable.registry_type,
        "name": expected,
        "params": {},
    }


@pytest.mark.parametrize("resolvable", KINDS, indirect=True)
@pytest.mark.parametrize(
    "name,error,match",
    [
        ("res_unknown", ValueError, "Registered: .*inspect_swarm/res_builtin"),
        ("inspect_swarm/res_unknown", ValueError, "'inspect_swarm/res_unknown'"),
        ("res_required", TypeError, r"required parameters \(size\)"),
    ],
)
def test_resolve_rejects_names(
    resolvable: Kind, name: str, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        resolve(resolvable.registry_type, name)


@pytest.mark.parametrize("kind", KINDS)
def test_resolve_returns_registered_object_as_given(kind: Kind) -> None:
    instance = _factory(kind, "res_object")()

    assert resolve(kind.registry_type, instance) is instance


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "value,match",
    [
        ("unregistered", "was not made by"),
        ("factory", "is the factory"),
        ("other_kind", "got a"),
    ],
)
def test_resolve_rejects_objects(kind: Kind, value: str, match: str) -> None:
    other = CHANNEL if kind is CONTROLLER else CONTROLLER
    objects: dict[str, Callable[[], Any]] = {
        "unregistered": kind.make,
        "factory": lambda: _factory(kind, "res_rejected"),
        "other_kind": lambda: _factory(other, "res_rejected")(),
    }

    with pytest.raises(TypeError, match=match):
        resolve(kind.registry_type, objects[value]())


def test_entry_point_loads_without_importing_the_package() -> None:
    script = "\n".join(
        [
            "import sys",
            "from inspect_ai._util.registry import registry_lookup",
            "assert 'inspect_swarm' not in sys.modules",
            "registry_lookup('swarm_controller', 'inspect_swarm/not_registered')",
            "assert 'inspect_swarm._entrypoint' in sys.modules",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
