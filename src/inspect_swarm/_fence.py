from collections.abc import Iterable, Sequence
from xml.sax.saxutils import escape

# what an XML attribute value in double quotes escapes beyond `&`, `<` and `>`
_ATTRIBUTE_ENTITIES = {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


# Peer text between a fence's tags, written as it is: the fence is a label, not a
# boundary. Attribute values are escaped as XML attribute values. The channels' values
# (swarm-generated ids, roster names checked against the name grammar, grammar-checked
# paths) hold nothing to escape, so they are written as they are; synthesize's roster
# names have no grammar.
def fence(tag: str, attributes: Sequence[tuple[str, str]], body: str) -> str:
    attrs = "".join(
        f' {name}="{escape(value, _ATTRIBUTE_ENTITIES)}"' for name, value in attributes
    )
    return f"<{tag}{attrs}>\n{body}\n</{tag}>"


# A tool's result, ended by the status lines that have something to say.
def with_lines(result: str, *lines: str | None) -> str:
    return "\n".join([result, *(line for line in lines if line)])


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


# The fenced records, in order, that one read shows: the first whole, then each that
# keeps the result within `budget`, where `size` is the result's size without them. It
# draws from `fenced` only until a record does not fit, so a lazy iterable renders no
# more of a long backlog than one record past what is shown.
def pack(fenced: Iterable[str], size: int, budget: int) -> list[str]:
    shown: list[str] = []
    for text in fenced:
        grown = size + utf8_bytes(text) + (len("\n") if shown else 0)
        if shown and grown > budget:
            break
        shown.append(text)
        size = grown
    return shown
