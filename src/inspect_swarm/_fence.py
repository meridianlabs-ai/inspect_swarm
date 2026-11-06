from collections.abc import Sequence


# Peer text between a fence's tags. Attribute values are swarm-generated, roster names
# or grammar-checked paths, none of which holds a quote or an angle bracket, so they are
# written as they are.
def fence(tag: str, attributes: Sequence[tuple[str, str]], body: str) -> str:
    attrs = "".join(f' {name}="{value}"' for name, value in attributes)
    return f"<{tag}{attrs}>\n{body}\n</{tag}>"


# A tool's result, ended by the status lines that have something to say.
def with_lines(result: str, *lines: str | None) -> str:
    return "\n".join([result, *(line for line in lines if line)])


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


# The fenced records, in order, that one read shows: the first whole, then each that
# keeps the result within `budget`, where `size` is the result's size without them.
def pack(fenced: Sequence[str], size: int, budget: int) -> list[str]:
    shown: list[str] = []
    for text in fenced:
        grown = size + utf8_bytes(text) + (len("\n") if shown else 0)
        if shown and grown > budget:
            break
        shown.append(text)
        size = grown
    return shown
