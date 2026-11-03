import pytest
from inspect_ai.model import get_model_info
from inspect_ai.model._tokens import count_text_tokens


@pytest.fixture(scope="session")
def warm_inspect_caches() -> None:
    """Load what inspect_ai loads on a process's first model call, once per test process: its model-info database and the token counter's encoding, which a fresh runner downloads. Use it in a test whose member time limit is shorter than that load, which would otherwise be cancelled before it reaches what it tests if it is the first in its process to call a model. Tests that use it need the encoding cached or the network."""
    get_model_info("mockllm/model")
    count_text_tokens("")


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Run every anyio-marked test on both backends. The runner's cancellation paths differ between them, so trio is a dev dependency."""
    return str(request.param)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runslow", action="store_true", help="Also run tests marked slow."
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # slow tests need Docker, so they run only when asked for
    if config.getoption("--runslow"):
        return
    skip = pytest.mark.skip(reason="slow: run with --runslow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
