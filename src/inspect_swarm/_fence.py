import re
from collections.abc import Sequence

# Every fence tag a swarm channel uses. Peer text holding any of them is refused, so
# that no channel's content can forge another channel's fence; a channel with a new tag
# adds it here.
FENCE_TAGS = ("peer_message", "peer_file", "peer_listing")

_MARKERS = re.compile(
    "|".join(f"</?{re.escape(tag)}" for tag in FENCE_TAGS), re.IGNORECASE
)


# Whether text holds an opening or closing fence marker, in any letter case.
def has_marker(text: str) -> bool:
    return _MARKERS.search(text) is not None


# Peer text between a fence's tags. Attribute values are swarm-generated, roster names
# or grammar-checked paths, none of which holds a quote or an angle bracket, so they are
# written as they are.
def fence(tag: str, attributes: Sequence[tuple[str, str]], body: str) -> str:
    attrs = "".join(f' {name}="{value}"' for name, value in attributes)
    return f"<{tag}{attrs}>\n{body}\n</{tag}>"


# The UTF-8 bytes a fence adds around its body, for attribute values of the given sizes
# in bytes; with the largest value of each, the most any fence of the tag adds.
def fence_bytes(tag: str, attributes: Sequence[tuple[str, int]]) -> int:
    return utf8_bytes(fence(tag, [(name, "x" * size) for name, size in attributes], ""))


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))
