import posixpath
import string

from .._controller._controller import MemberInfo
from ._channel import Channel, swarm_channel


class _Filesystem(Channel):
    def __init__(self, shared: str, notes: str, scratch: str) -> None:
        self.shared = shared
        self.notes = posixpath.join(shared, notes)
        self.scratch = scratch

    def instructions(self, member: MemberInfo) -> str | None:
        return (
            "The other members work in the same sandbox as you, at the same time, so files they change can change under you. "
            f"Share files with them in {self.shared}. "
            f"Read {self.notes} before you start and again as you work, and add what you learn or decide to the end of it, without changing what others wrote. "
            f"Keep your own work in {self.scratch.format(member=member.name)}, which no other member uses."
        )


@swarm_channel
def filesystem(
    shared: str = "/shared",
    notes: str = "notes.md",
    scratch: str = "/scratch/{member}",
) -> Channel:
    """The shared sandbox, with a convention for using it, stated in each member's preamble.

    Members share the sample's sandbox whatever the channels. This channel adds instructions only: a directory for shared files, a notes file members append to, and a scratch directory for each member. It adds no tools and does not create the directories.

    Args:
        shared: The directory for files members share.
        notes: The notes file members read and append to, relative to `shared` (or an absolute path).
        scratch: Each member's own directory. `{member}` is replaced by the member's name.

    Returns:
        The channel.

    Raises:
        ValueError: If `scratch` has no `{member}` placeholder, or has another placeholder.
    """
    try:
        fields = [
            (field, spec, conversion)
            for _, field, spec, conversion in string.Formatter().parse(scratch)
            if field is not None
        ]
    except ValueError as ex:
        raise _bad_scratch(scratch) from ex
    # without the member's name every member would be given the same directory
    if not fields or any(field != ("member", "", None) for field in fields):
        raise _bad_scratch(scratch)
    return _Filesystem(shared, notes, scratch)


def _bad_scratch(scratch: str) -> ValueError:
    return ValueError(
        f"filesystem(): scratch must use the {{member}} placeholder, and no other, got {scratch!r}."
    )
