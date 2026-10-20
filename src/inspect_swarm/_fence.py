import re
from collections.abc import Sequence

# Every fence tag a swarm channel uses. Peer text is cleaned of all of them, so that no
# channel's content can forge another channel's fence; a channel with a new tag adds it
# here.
FENCE_TAGS = ("peer_message", "peer_file", "peer_listing")

_MARKER = "|".join(f"</?{re.escape(tag)}" for tag in FENCE_TAGS)
_MARKERS = re.compile(_MARKER, re.IGNORECASE)
_MARKER_AT_END = re.compile(f"(?:{_MARKER})\\Z", re.IGNORECASE)
_LONGEST = max(len(f"</{tag}") for tag in FENCE_TAGS)


# Peer text with every opening and closing fence marker removed, case-insensitively,
# until none remain, so that nested fragments such as `<peer_<peer_filefile` cannot
# reassemble a marker. Each marker is removed as soon as the kept text ends with it, so
# the work is linear in the text: removing one marker at a time from the whole text was
# quadratic. Markers start with `<` and hold no other, so no two overlap, and the
# result is the same in whatever order they are removed.
def remove_markers(text: str) -> str:
    if not _MARKERS.search(text):
        return text
    kept: list[str] = []
    for char in text:
        kept.append(char)
        marker = _MARKER_AT_END.search("".join(kept[-_LONGEST:]))
        if marker:
            del kept[len(kept) - len(marker.group()) :]
    return "".join(kept)


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
