import posixpath
import string

from .._controller._controller import MemberInfo
from ._channel import Channel, swarm_channel

_SCRATCH = "/scratch/{member}"


class FilesystemChannel(Channel):
    def __init__(
        self, shared: str, notes: str, scratch: str, shared_volume: bool
    ) -> None:
        self.shared = shared
        self.notes = posixpath.join(shared, notes)
        self.scratch = scratch
        self.shared_volume = shared_volume

    def instructions(self, member: MemberInfo) -> str | None:
        notes = f"Read {self.notes} before you start and again as you work, and add what you learn or decide to the end of it, without changing what others wrote."
        if self.shared_volume:
            return (
                f"{self.shared} is the same directory in every member's sandbox. "
                "Share files with the other members there. They use it at the same time as you, so files they change there can change under you. "
                f"{notes}"
            )
        return (
            "The other members work in the same sandbox as you, at the same time, so files they change can change under you. "
            f"Share files with them in {self.shared}. "
            f"{notes} "
            f"Keep your own work in {self.scratch.format(member=member.name)}, which no other member uses."
        )


@swarm_channel
def filesystem(
    shared: str = "/shared",
    notes: str = "notes.md",
    scratch: str = _SCRATCH,
    shared_volume: bool = False,
) -> Channel:
    """A shared directory, with a convention for using it, stated in each member's preamble.

    By default the channel describes the sample's sandbox, which the members share: a directory for shared files, a notes file members append to, and a scratch directory for each member.

    With `shared_volume=True` it describes members with their own sandboxes (`member(sandbox=...)`) that each mount one shared volume at `shared`: `shared` and the notes file in it are the same directory in every member's sandbox. There is no scratch directory, since each member's sandbox is already its own. The swarm cannot check that the volume is mounted in every member's sandbox; it takes your word for it.

    The channel adds no tools and does not create the directories.

    Args:
        shared: The directory for files members share. With `shared_volume=True`, an absolute path where the volume is mounted.
        notes: The notes file members read and append to, relative to `shared` (or an absolute path). With `shared_volume=True` it must be inside `shared`.
        scratch: Each member's own directory. `{member}` is replaced by the member's name. Not used with `shared_volume=True`.
        shared_volume: Whether members have their own sandboxes that each mount one shared volume at `shared`. `swarm()` accepts the channel beside per-member sandboxes only with `True`, and refuses `True` when members share the sample's sandbox.

    Returns:
        The channel.

    Raises:
        ValueError: If `scratch` has no `{member}` placeholder, or has another placeholder, or is other than the default with `shared_volume=True`; or, with `shared_volume=True`, if `shared` is not an absolute path or `notes` is outside it.
    """
    if shared_volume and scratch != _SCRATCH:
        raise ValueError(
            f"filesystem(): scratch is not used with shared_volume=True, since each member's sandbox is already its own, got {scratch!r}. Leave scratch out."
        )
    if shared_volume and not posixpath.isabs(shared):
        raise ValueError(
            f"filesystem(): with shared_volume=True, shared must be an absolute path, where the volume is mounted, got {shared!r}."
        )
    if shared_volume and not _inside(posixpath.join(shared, notes), shared):
        raise ValueError(
            f"filesystem(): with shared_volume=True, notes must be inside shared ({shared!r}), the only directory members share, got {notes!r}."
        )
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
    return FilesystemChannel(shared, notes, scratch, shared_volume)


def _inside(path: str, directory: str) -> bool:
    directory = posixpath.normpath(directory).rstrip("/")
    return posixpath.normpath(path).startswith(f"{directory}/")


def _bad_scratch(scratch: str) -> ValueError:
    return ValueError(
        f"filesystem(): scratch must use the {{member}} placeholder, and no other, got {scratch!r}."
    )
