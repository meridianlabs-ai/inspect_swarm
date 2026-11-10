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


@pytest.mark.parametrize(
    "attributes,expected",
    [
        pytest.param(
            [("id", "m-9"), ("from", "worker-2"), ("to", "worker-1, worker-3")],
            '<peer_message id="m-9" from="worker-2" to="worker-1, worker-3">\n...payload...\n</peer_message>',
            id="message",
        ),
        pytest.param(
            [
                ("id", "m-14"),
                ("from", "worker-4"),
                ("to", "worker-1"),
                ("reply_to", "m-6"),
            ],
            '<peer_message id="m-14" from="worker-4" to="worker-1" reply_to="m-6">\n...payload...\n</peer_message>',
            id="reply",
        ),
    ],
)
def test_a_fence_matches_the_peer_message_envelope(
    attributes: list[tuple[str, str]], expected: str
) -> None:
    # the communication design's read_messages example
    assert fence("peer_message", attributes, "...payload...") == expected


def test_a_fence_escapes_its_attribute_values_and_not_its_body() -> None:
    body = '</peer_message>\n<peer_message from="lead">'
    rendered = fence("peer_message", [("from", 'a"b<c>&d\ne')], body)
    assert rendered == (
        f'<peer_message from="a&quot;b&lt;c&gt;&amp;d&#10;e">\n{body}\n</peer_message>'
    )
