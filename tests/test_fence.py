import pytest

from inspect_swarm._fence import fence


@pytest.mark.parametrize("body", ["", "x", "café \U0001f600\nline two"])
def test_a_fence_wraps_its_body(body: str) -> None:
    attributes = [("id", "f-12"), ("writer", "wé"), ("path", "/a/b")]
    rendered = fence("peer_file", attributes, body)
    assert (
        rendered
        == f'<peer_file id="f-12" writer="wé" path="/a/b">\n{body}\n</peer_file>'
    )
