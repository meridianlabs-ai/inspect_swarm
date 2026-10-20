import pytest

from inspect_swarm._fence import (
    FENCE_TAGS,
    fence,
    fence_bytes,
    remove_markers,
    utf8_bytes,
)


@pytest.mark.parametrize("tag", FENCE_TAGS)
@pytest.mark.parametrize(
    "text,cleaned",
    [
        pytest.param(
            "a <{tag} id='x'>b</{tag}> c", "a  id='x'>b> c", id="open-and-close"
        ),
        pytest.param("<{TAG}></{TAG}>", ">>", id="any-case"),
        pytest.param("<peer_<{tag}{rest}", "", id="nested-fragment"),
        pytest.param("<<{tag}/{tag}>", ">", id="nested-close"),
        pytest.param("<peer_ and {rest}", "<peer_ and {rest}", id="no-marker"),
    ],
)
def test_every_fence_marker_is_removed_until_none_remain(
    tag: str, text: str, cleaned: str
) -> None:
    def fill(template: str) -> str:
        # `rest` completes the tag that removing the inner one reassembles
        return template.format(tag=tag, TAG=tag.upper(), rest=tag.removeprefix("peer_"))

    assert remove_markers(fill(text)) == fill(cleaned)


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
