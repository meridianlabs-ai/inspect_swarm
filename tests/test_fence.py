import pytest

from inspect_swarm._fence import (
    FENCE_TAGS,
    fence,
    fence_bytes,
    has_marker,
    utf8_bytes,
)


@pytest.mark.parametrize("tag", FENCE_TAGS)
@pytest.mark.parametrize(
    "text,found",
    [
        pytest.param("a <{tag} id='x'>b", True, id="open"),
        pytest.param("b</{tag}> c", True, id="close"),
        pytest.param("<{TAG}>", True, id="any-case"),
        pytest.param("<peer_ {rest} <peer-{rest} {tag}>", False, id="near-misses"),
    ],
)
def test_every_fence_marker_is_found_in_any_case(
    tag: str, text: str, found: bool
) -> None:
    filled = text.format(tag=tag, TAG=tag.upper(), rest=tag.removeprefix("peer_"))
    assert has_marker(filled) is found


@pytest.mark.parametrize("body", ["", "x", "café \U0001f600\nline two"])
def test_a_fence_adds_its_overhead_to_its_body(body: str) -> None:
    attributes = [("id", "f-12"), ("writer", "wé"), ("path", "/a/b")]
    sizes = [(name, utf8_bytes(value)) for name, value in attributes]
    rendered = fence("peer_file", attributes, body)
    assert (
        rendered
        == f'<peer_file id="f-12" writer="wé" path="/a/b">\n{body}\n</peer_file>'
    )
    assert utf8_bytes(rendered) == fence_bytes("peer_file", sizes) + utf8_bytes(body)
