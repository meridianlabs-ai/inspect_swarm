import time

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
        pytest.param("<peer_<peer_<{tag}{rest}{rest}", "", id="nested-twice"),
        pytest.param(
            "<peer_<peer_<peer_message{rest}file", "", id="nested-across-tags"
        ),
        pytest.param("<pe<{tag}er_{rest}>", ">", id="nested-mid-tag"),
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


@pytest.mark.parametrize("tag", FENCE_TAGS)
def test_removing_deeply_nested_markers_is_quick(tag: str) -> None:
    # removing one marker per pass over the whole text took about 8 seconds here
    n = 20_000
    start = time.perf_counter()
    assert remove_markers("<peer_" * n + tag.removeprefix("peer_") * n) == ""
    assert time.perf_counter() - start < 2
