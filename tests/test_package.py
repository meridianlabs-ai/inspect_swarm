import anyio
import pytest

import inspect_swarm


def test_version_is_exposed() -> None:
    assert isinstance(inspect_swarm.__version__, str)
    assert inspect_swarm.__version__


@pytest.mark.anyio
async def test_async_tests_run() -> None:
    await anyio.sleep(0)
