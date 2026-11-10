import posixpath
import re
import weakref
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import anyio
from inspect_ai.tool._sandbox_tools_utils.sandbox import resolve_root_access
from inspect_ai.util import ExecResult, SandboxEnvironment, sandbox
from inspect_ai.util._sandbox._privileged import privileged_shell
from inspect_ai.util._sandbox.context import (
    sandbox_default_context_var,  # pyright: ignore[reportPrivateUsage]
    sandbox_environments_context_var,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._sandbox.identity import sandbox_identity, sandbox_identity_active
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment
from pydantic import BaseModel, ConfigDict, JsonValue

from ._record import MemberReap

if TYPE_CHECKING:
    # at run time this would import the runtime, which imports this module
    from ._controller._controller import MemberInfo

# the member's name in the home template
_MEMBER_FIELD = "{member}"


class Share(BaseModel):
    """A directory every member shares under users isolation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    """The directory, as an absolute path. Created if it does not exist."""

    access: Literal["rw", "ro"]
    """`rw`: every member can read, create, change and delete everything in it. `ro`: every member can read it and none can write it. Setup does not change a `ro` share, so others must already be able to read every entry in it and search every directory, and must not be able to write any."""


class UserIsolation(BaseModel):
    """Members of one sandbox, each run as its own Linux user. Create one with `users()`, which documents the fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["users"] = "users"
    """The kind of isolation."""

    sandbox: str = "default"
    """The sandbox environment the members share, by its name in the sample."""

    home: str = "/scratch/{member}"
    """Each member's private directory and `$HOME`, where `{member}` stands for the member's name."""

    shared: tuple[Share, ...] = (Share(path="/shared", access="rw"),)
    """The directories members share."""

    peers: Literal["none", "read"] = "none"
    """Whether members can read each other's private directories."""


def users(
    *,
    sandbox: str = "default",
    home: str = "/scratch/{member}",
    shared: Sequence[Share | Mapping[str, str]] = (Share(path="/shared", access="rw"),),
    peers: Literal["none", "read"] = "none",
) -> UserIsolation:
    """Run each member as its own Linux user in one shared sandbox.

    When the swarm starts, before any member does, it creates a non-root user `swarm-<member>` for each member, with a new private home directory (the image's `/etc/skel` is not copied), sets up the read-write shares, checks the read-only ones, and checks that the boundaries hold by running commands as the members. It refuses to start if any check fails. Each member then runs inside a sandbox identity scope, so that the built-in tools that run commands in the sandbox (`bash()`, `python()`, `bash_session()`, `text_editor()`, `grep()`, `list_files()` and the `read_file()` tool) run as its user and start in its home. Custom tools' `exec()`, `read_file()` and `write_file()` calls that name no user also run as the member. Raw `exec()` calls keep the sandbox's working directory unless they pass `cwd`; use absolute paths for raw file calls. Its preamble tells it its user and directories.

    When a member ends, the swarm kills every process still running as its user, and records what it found in the member's result (`MemberResult.reap`). If processes survive, or the sandbox does not answer, the result record's `isolation_incomplete` is `True`. When the swarm ends, it records who owns the files in the read-write shares and in `/tmp`, `/var/tmp`, `/dev/shm`, `/run/lock` and the other world-writable directories on the root filesystem, as a `census` evidence event.

    A member cannot read or change another member's home unless `peers="read"` lets it read, and cannot write a `ro` share. It can still see the other members' processes and command lines, share files through `/tmp`, and connect to their sockets on localhost. Members share one kernel and one sandbox root: this is a default boundary and a record of who made what, not containment.

    Requirements and limits:

    - The sandbox must let commands run as root and as other users: Docker, or Kubernetes with a root container. The `local` sandbox is refused.
    - The image needs `useradd` and `groupadd` (package `passwd`) and GNU `find`; with any share, also `setfacl` and `getfacl` (package `acl`), which the default Inspect image and `python:3.12-bookworm` lack.
    - Homes and shares must sit under directories owned by root and not writable by group or others, so a share under `/tmp` is refused. A share must not be `/tmp`, `/var/tmp`, `/dev/shm` or `/run/lock`, or above one. Homes must not exist yet. ACLs that setup did not set, in a share or on the homes' parent, and mounts inside a share or under the homes' parent are refused. So are hard-linked files, devices, sockets, FIFOs and set-user-id or set-group-id files in a `rw` share, and, in a `ro` share, an entry others cannot read (or search, for a directory) or can write: setup does not change a `ro` share, so set its modes in the image.
    - One swarm with users isolation per sandbox in a sample. Another is refused, whether it runs after the first, beside it, in a subtask or in a fork.
    - The image and sandbox configuration are trusted, like task code that runs before the swarm. Setup makes the checks listed here, and refuses an existing `inspect-swarm` group, `swarm-<member>` user or group, or a group that lists a `swarm-<member>` user, but it does not audit the image for every setting that could give a member authority. The image must also meet these conditions, which setup does not check: the same storage is not mounted at two shares, or at a share and in the homes' tree; member users have no sudo rules (setup only checks that `sudo -n true` fails); and the storage of a `rw` share or of the homes is not shared with another sandbox that uses users isolation. A `ro` input may be shared.
    - Members are not root: they cannot install system packages or write system directories. Tasks that need root while the members work are unsuitable.
    - Custom tools inherit the member's user: their `sandbox().exec()`, `read_file()` and `write_file()` calls that name no user run as the member. A call that names another user, such as `user="root"`, runs as that user; that is a grant made in code. `computer()`, `web_browser()` and `skill()` raise inside the scope, and so do built-in tools built with another `user`.
    - Bridged members (inspect_swe agents) need inspect_ai and inspect_swe versions with bridge support for users isolation, which they do not have yet.
    - Member names must be valid Linux user-name suffixes: lowercase letters, digits and `-`, starting with a letter, at most 26 characters.

    Args:
        sandbox: The sandbox environment the members share, by its name in the sample. `default` is the sample's default sandbox.
        home: Each member's private directory, which is also its `$HOME` and where its tools start. `{member}` stands for the member's name and must be in the last path component.
        shared: The directories every member shares, each read-write (`rw`) or read-only (`ro`). A `rw` share's files stay group-writable whatever a member's umask, through a default ACL. A `ro` share is used as it is. Directories that do not exist are created.
        peers: `none`: only the member can read its home. `read`: every member can read every other member's home, but not change it.

    Returns:
        The isolation, for `swarm(isolation=...)`.

    Raises:
        ValueError: If a path is not absolute and normalised, or holds a backslash or a character that is not printable (a control character or a line separator); `home` does not have `{member}` once, in its last component; two shares, or a share and the homes' parent directory, are the same or one is inside the other; or a share is, is inside or is above `/tmp`, `/var/tmp`, `/dev/shm` or `/run/lock`.
    """
    isolation = UserIsolation(
        sandbox=sandbox,
        home=home,
        shared=tuple(
            item if isinstance(item, Share) else Share.model_validate(dict(item))
            for item in shared
        ),
        peers=peers,
    )
    check_isolation(isolation, "users()")
    return isolation


# swarm(isolation=...): None, "users", a UserIsolation, or its fields as data (what
# replay passes back)
def resolve_isolation(value: object) -> UserIsolation | None:
    if value is None:
        return None
    if value == "users":
        isolation = UserIsolation()
    elif isinstance(value, UserIsolation):
        isolation = value
    elif isinstance(value, Mapping):
        isolation = UserIsolation.model_validate(dict(cast(Mapping[str, Any], value)))
    else:
        raise ValueError(
            f"swarm(): isolation must be None, 'users' or users(...), got {value!r}."
        )
    check_isolation(isolation, "swarm(): isolation")
    return isolation


def check_isolation(isolation: UserIsolation, where: str) -> None:
    for path, argument in [
        (isolation.home, "home"),
        *((share.path, "shared") for share in isolation.shared),
    ]:
        if (
            not posixpath.isabs(path)
            or path.startswith("//")
            or posixpath.normpath(path) != path
            # setup reads paths back line by line and passes them to awk -v, which
            # reads a backslash as an escape
            or any(c == "\\" or not c.isprintable() for c in path)
        ):
            raise ValueError(
                f"{where}: {argument} must be an absolute, normalised path with no '..', '.', repeated or trailing '/', backslash, or character that is not printable (such as a newline, tab or other control character, or a line separator), got {path!r}."
            )
    home = isolation.home
    last = posixpath.basename(home)
    if (
        home.count(_MEMBER_FIELD) != 1
        or _MEMBER_FIELD not in last
        or any(c in home.replace(_MEMBER_FIELD, "") for c in "{}")
    ):
        raise ValueError(
            f"{where}: home must have {_MEMBER_FIELD} once, in its last path component, and no other braces, got {home!r}."
        )
    parent = posixpath.dirname(home)
    paths = [share.path for share in isolation.shared]
    for i, path in enumerate(paths):
        for root in _TRANSIENT:
            if _nested(path, root):
                raise ValueError(
                    f"{where}: the share {path!r} overlaps {root!r}, a directory every user writes and that the swarm keeps as shared transient state; a share must not be {', '.join(_TRANSIENT)}, inside one or above one."
                )
        if _nested(path, parent):
            raise ValueError(
                f"{where}: the share {path!r} and the members' homes ({home!r}) overlap; a share must not be the homes' parent directory, inside it or above it."
            )
        for other in paths[:i]:
            if _nested(path, other):
                raise ValueError(
                    f"{where}: the shares {other!r} and {path!r} overlap; no share may be inside another, or given twice."
                )


# the directories every user writes, which setup scans for the ids files take and
# which no share may overlap
_TRANSIENT = ("/tmp", "/var/tmp", "/dev/shm", "/run/lock")


# whether one path is the other or inside it
def _nested(a: str, b: str) -> bool:
    return (
        a == b or a.startswith(f"{b.rstrip('/')}/") or b.startswith(f"{a.rstrip('/')}/")
    )


# `swarm-<name>` must be a portable user name of at most 32 characters
_NAME = re.compile(r"[a-z][a-z0-9-]{0,25}")


def check_isolated_roster(
    isolation: UserIsolation,
    roster: Sequence["MemberInfo"],
    own_sandboxes: bool,
    filesystem: tuple[str, str, str] | None,
) -> None:
    for info in roster:
        if not _NAME.fullmatch(info.name):
            raise ValueError(
                f"swarm(): under users isolation each member runs as the Linux user swarm-<name>, so member names must be lowercase letters, digits and '-', start with a letter and be at most 26 characters; {info.name!r} is not. Give the member another name with member(..., name=...)."
            )
        if info.choose_name:
            raise ValueError(
                f"swarm(): member {info.name!r} chooses its own name (choose_name=True), and under users isolation its user name and home show the swarm's name for it. Set choose_name=False, or leave isolation out."
            )
    if own_sandboxes:
        raise ValueError(
            "swarm(): users isolation runs the members in one shared sandbox, and these members have their own sandboxes (member(..., sandbox=...)). Use one or the other."
        )
    if filesystem is None:
        return
    shared, notes, scratch = filesystem
    if scratch != isolation.home:
        raise ValueError(
            f"swarm(): the filesystem channel's scratch ({scratch!r}) is not the members' home under users isolation ({isolation.home!r}), so the preamble would describe a directory the member cannot use. Give filesystem(scratch=...) and users(home=...) the same value."
        )
    rw = [share.path for share in isolation.shared if share.access == "rw"]
    if shared not in rw:
        raise ValueError(
            f"swarm(): the filesystem channel's shared directory ({shared!r}) is not a read-write share under users isolation ({', '.join(repr(p) for p in rw) or 'none'}), so members could not write it. Add Share(path={shared!r}, access='rw') to users(shared=...), or give filesystem(shared=...) one of them."
        )
    # here: at import time the channel's module would import the runtime
    from ._channel.filesystem import _inside  # pyright: ignore[reportPrivateUsage]

    if not any(_inside(notes, path) for path in rw):
        raise ValueError(
            f"swarm(): the filesystem channel's notes file ({notes!r}) is not inside a read-write share under users isolation ({', '.join(repr(p) for p in rw)}), so members could not add to it. Give filesystem(notes=...) a path inside one of them."
        )


# A member's Linux user, set up when the swarm started, and the sandbox it is in.
@dataclass(frozen=True)
class MemberIdentity:
    user: str
    uid: int
    home: str
    sandbox: str


# A directory the census counts the owners of the entries in: a rw share, one of the
# directories every user writes, or another world-writable directory.
@dataclass(frozen=True)
class CensusRoot:
    path: str
    kind: Literal["share", "transient", "world_writable"]


# What setup leaves for the swarm's run: the sandbox, by its object and its name, each
# member's identity by member name, and the census roots, with whether the
# world-writable directories among them were cut at their cap.
@dataclass(frozen=True)
class IsolationSetup:
    environment: SandboxEnvironment
    sandbox: str
    identities: dict[str, MemberIdentity]
    census_roots: list[CensusRoot]
    census_roots_capped: bool


def member_user(name: str) -> str:
    return f"swarm-{name}"


def member_home(isolation: UserIsolation, name: str) -> str:
    return isolation.home.replace(_MEMBER_FIELD, name)


# The member's identity scope: the built-in tools' sandbox commands run as its user,
# starting in its home.
@contextmanager
def member_identity(identity: MemberIdentity | None) -> Generator[None, None, None]:
    if identity is None:
        yield
        return
    with sandbox_identity(identity.user, cwd=identity.home, sandbox=identity.sandbox):
        yield


# The paragraph each member's preamble ends with.
def isolation_preamble(isolation: UserIsolation, name: str) -> str:
    home = member_home(isolation, name)
    access = (
        "other members can read it but not change it"
        if isolation.peers == "read"
        else "other members cannot read it"
    )
    parts = [
        f"You run as the Linux user {member_user(name)}, without root.",
        f"Your private directory is {home} (your $HOME); {access}.",
    ]
    if isolation.shared:
        modes = {"rw": "read and write", "ro": "read only"}
        shares = ", ".join(f"{s.path} ({modes[s.access]})" for s in isolation.shared)
        parts.append(f"Shared with all members: {shares}.")
    parts.append("Use $TMPDIR for temporary files.")
    if any(share.access == "rw" for share in isolation.shared):
        parts.append(
            "Git refuses a repository another user created until you run git config --global --add safe.directory <path>."
        )
    parts.append("Processes you leave running are stopped when you finish.")
    return " ".join(parts)


# the group every member is in, which owns the rw shares
_GROUP = "inspect-swarm"

# member uids and gids, and the group's gid, are chosen from this window
_FIRST_ID = 61000
_LAST_ID = 61999

# the census scans at most this many world-writable directories besides the shares and
# the directories every user writes
_WRITABLE_CAP = 200


# Refusals in the scripts below exit with this status and a message on stderr, as
# their last line.
_REFUSED = 3

# The scripts read their tools' output and diagnostics, so they run them in the C
# locale, whatever the image or sandbox sets.
_PRELUDE = r"""
export LC_ALL=C
fail() { printf '%s\n' "$*" >&2; exit 3; }
"""

# Whether the image has what setup needs and a layout it supports, which users and groups
# exist or are listed as group members, and which ids in the window are taken, by a
# passwd entry's user or primary group id, a group entry or the
# owner or group of a file on any filesystem the homes, shares or world-writable
# directories are on. Setup does not reconcile permission state it did not make, so it
# refuses any ACL in a share or on the homes' parent, a mount inside a share or under
# the homes' parent, whose files it would not reach, and a hard-linked file, a device,
# a socket, a FIFO or a set-id file in a rw share. It does not change a ro share, so it refuses a ro share
# whose modes would not let members read it, or would let them write it.
# It also lists the world-writable directories on the root filesystem, for the census.
# $1 group, $2 first id, $3 last id, $4 1 when there are shares, $5 the homes' parent,
# $6 how many world-writable directories to list, then the roots, `--`, and each
# share's access and path.
_PROBE = (
    _PRELUDE
    + r"""
group=$1 lo=$2 hi=$3 acl=$4 parent=$5 writable_cap=$6
shift 6
command -v useradd >/dev/null 2>&1 || fail "users isolation needs useradd in the sandbox (package passwd)"
command -v groupadd >/dev/null 2>&1 || fail "users isolation needs groupadd in the sandbox (package passwd)"
command -v awk >/dev/null 2>&1 || fail "users isolation needs awk in the sandbox"
find / -maxdepth 0 -writable -printf '' >/dev/null 2>&1 || fail "users isolation needs GNU find in the sandbox (package findutils)"
if [ "$acl" = 1 ]; then
  command -v setfacl >/dev/null 2>&1 && command -v getfacl >/dev/null 2>&1 || fail "users isolation needs setfacl and getfacl in the sandbox for shared directories (package acl)"
fi
dir=$(mktemp -d) || fail "users isolation could not create a temporary directory"
trap 'rm -rf "$dir"' EXIT

# find "$@" into $dir/found, refusing with $1 on any error but a removed entry: a scan of
# a live sandbox meets files and directories that processes remove while it runs, such
# as the sandbox tools' request files, and those take no id and hold nothing to refuse
scan() {
  what=$1
  shift
  find "$@" >"$dir/found" 2>"$dir/errors"
  status=$?
  [ "$status" -eq 0 ] && return 0
  # find exits 1 after errors it reports; a silent failure, another status or a signal
  # could leave out anything
  [ "$status" -eq 1 ] && [ -s "$dir/errors" ] \
    || fail "$what: find exited with status $status$(head -n 1 "$dir/errors" | sed 's/^/: /')"
  grep -v '^find: .*: No such file or directory$' "$dir/errors" >"$dir/other"
  [ $? -le 1 ] || fail "$what"
  [ ! -s "$dir/other" ] || fail "$what: $(head -n 1 "$dir/other")"
}

# a mount point strictly inside $1, as /proc/self/mountinfo writes it (\040 for a space;
# configured paths have no backslash, which awk -v would read as an escape, and no tab
# or other control character)
mount_inside() {
  awk -v p="$1" '
    function esc(s,  r, i, c) {
      r = ""
      for (i = 1; i <= length(s); i++) {
        c = substr(s, i, 1)
        if (c == " ") r = r "\\040"
        else r = r c
      }
      return r
    }
    BEGIN { p = esc(p); if (p != "/") p = p "/" }
    index($5, p) == 1 && $5 != p { print $5; exit }
  ' /proc/self/mountinfo
}

# the first ACL entry in getfacl's output (-n -p -E --skip-base) beyond the owner,
# group and other classes: a named entry or the mask in an access ACL, a named entry in
# a default ACL, or else a file's first default entry
foreign_acl() {
  awk '
    function finish() {
      if (first != "") { print first; done = 1; exit }
    }
    /^# file: / { finish(); file = substr($0, 9); first = ""; next }
    /^#/ || NF == 0 { next }
    {
      split($0, f, ":")
      if (f[1] == "default") { q = f[3]; if (first == "") first = file ": " $0 }
      else if (f[1] == "mask") q = "mask"
      else q = f[2]
      if (q != "") { print file ": " $0; done = 1; exit }
    }
    END { if (!done) finish() }
  '
}

# a directory setup would create inherits the default ACL of its nearest existing
# ancestor, so that ancestor must have none
inherits_acl() {
  a=$1
  while [ ! -e "$a" ] && [ ! -L "$a" ]; do a=$(dirname -- "$a"); done
  l=$(ls -ld -- "$a") || fail "users isolation could not inspect $a"
  case $l in
    ??????????+*) fail "users isolation would create $1 inside $a, whose ACL its new directories would inherit; remove it (setfacl -b)" ;;
  esac
}

[ -r /proc/self/mountinfo ] || fail "users isolation needs /proc/self/mountinfo to check for mounts inside its directories"
if [ -e "$parent" ]; then
  m=$(mount_inside "$parent") || fail "users isolation could not read /proc/self/mountinfo"
  [ -z "$m" ] || fail "users isolation does not support a mount under the members' homes' parent $parent, as at $m"
  l=$(ls -ld -- "$parent") || fail "users isolation could not inspect $parent"
  case $l in
    ??????????+*) fail "users isolation does not support ACLs on the members' homes' parent $parent, which homes would inherit; remove them (setfacl -b)" ;;
  esac
else
  inherits_acl "$parent"
fi
# a user's primary group id is taken even when no group entry has it
awk -F: '{ print $3 } FILENAME == "/etc/passwd" { print $4 }' /etc/passwd /etc/group >"$dir/ids" \
  || fail "users isolation could not read the ids in /etc/passwd and /etc/group"
# the real, effective, saved and filesystem user and group ids of the processes running
# now, such as a task's service started as a bare numeric user, with no passwd entry and
# no file: a member given its uid would have it killed when reaped. A process that exits
# while this runs takes no id.
for s in /proc/[0-9]*/status; do
  { while read -r k a b c d _; do
      case $k in Uid:|Gid:) printf '%s\n%s\n%s\n%s\n' "$a" "$b" "$c" "$d" ;; esac
    done <"$s"; } 2>/dev/null
done >>"$dir/ids" || fail "users isolation could not list the ids of the running processes"
while [ "$1" != -- ]; do
  root=$1
  shift
  [ -d "$root" ] || continue
  # an incomplete scan could miss an id a file takes
  scan "users isolation could not scan all of $root for the user ids its files take, so it cannot choose ids that own nothing" \
    -P "$root" -xdev -ignore_readdir_race \( \( -uid +$((lo - 1)) -uid -$((hi + 1)) \) -o \( -gid +$((lo - 1)) -gid -$((hi + 1)) \) \) -printf '%U\n%G\n'
  cat -- "$dir/found" >>"$dir/ids" || fail "users isolation could not keep the ids it found"
done
shift
while [ $# -gt 0 ]; do
  access=$1 share=$2
  shift 2
  if [ ! -e "$share" ] && [ ! -L "$share" ]; then
    inherits_acl "$share"
    continue
  fi
  [ -d "$share" ] || continue
  m=$(mount_inside "$share") || fail "users isolation could not read /proc/self/mountinfo"
  [ -z "$m" ] || fail "users isolation does not support a mount inside the shared directory $share, as at $m"
  acls=$(getfacl -R -P -n -p -E --skip-base -- "$share" 2>&1) \
    || fail "users isolation could not read the ACLs in $share: $(printf '%s' "$acls" | grep -v '^#' | head -n 1)"
  bad=$(printf '%s\n' "$acls" | foreign_acl) || fail "users isolation could not check the ACLs in $share"
  [ -z "$bad" ] || fail "users isolation does not support ACLs in a shared directory: $bad; remove them (setfacl -b)"
  if [ "$access" = ro ]; then
    # setup does not change a ro share, so with no ACL the other bits must already let
    # members read every entry and search every directory, and write none
    scan "users isolation could not scan the files in $share" \
      -P "$share" -xdev -ignore_readdir_race ! -type l \( \
        -perm /o=w -printf '%p is writable by others (mode %m)' -quit \
        -o ! -perm -o=r -printf '%p is not readable by others (mode %m)' -quit \
        -o -type d ! -perm -o=x -printf '%p is a directory others cannot search (mode %m)' -quit \)
    open=$(cat -- "$dir/found") || fail "users isolation could not scan the files in $share"
    [ -z "$open" ] || fail "users isolation does not change the read-only shared directory $share, so its modes must already let others read every entry and search every directory, and write none: $open; set its modes in the image"
    continue
  fi
  # setup changes each entry's inode, which a hard link shares with a name outside
  scan "users isolation could not scan the files in $share" \
    -P "$share" -xdev -ignore_readdir_race ! -type d -links +1 -printf '%p has %n links' -quit
  linked=$(cat -- "$dir/found") || fail "users isolation could not scan the files in $share"
  [ -z "$linked" ] || fail "users isolation does not support a hard-linked file in a shared directory: $linked; setup's changes to its mode, group and ACLs would reach its other names"
  # setup opens entries to members: a device would become their access to the device,
  # a socket or FIFO their access to the process behind it, and a set-id file a
  # program they could run with its owner's or group's rights
  scan "users isolation could not scan the files in $share" \
    -P "$share" -xdev -ignore_readdir_race \( -type b -o -type c \) -printf '%p is a device' -quit \
    -o -type s -printf '%p is a socket' -quit \
    -o -type p -printf '%p is a FIFO' -quit \
    -o -type f -perm /6000 -printf '%p is set-user-id or set-group-id (mode %m)' -quit
  special=$(cat -- "$dir/found") || fail "users isolation could not scan the files in $share"
  [ -z "$special" ] || fail "users isolation does not support a device, socket, FIFO, or set-user-id or set-group-id file in a read-write shared directory: $special; setup would give members access to it"
  # the default ACL setup writes takes its owner entry from the directory's owner
  # bits, and an owner entry beats the group's grant, so a member could not edit
  # the files it creates in a directory whose owner lacks rwx
  scan "users isolation could not scan the directories in $share" \
    -P "$share" -xdev -ignore_readdir_race -type d ! -perm -u=rwx -printf '%p is mode %m' -quit
  narrow=$(cat -- "$dir/found") || fail "users isolation could not scan the directories in $share"
  [ -z "$narrow" ] || fail "users isolation does not support a directory without owner rwx in a read-write shared directory: $narrow; its owner bits would become the default ACL of the files members create there"
done
# the world-writable directories on the root filesystem, the topmost of each tree, which
# the census scans as places every member can write; a path with a newline is left out,
# since the answer has one path per line. find -xdev still lists a mount point on /, so
# one on another filesystem (by its device) is left out too: the id scan of / did not
# reach its files, whose owners could have the ids setup chooses.
nl='
'
rootdev=$(find / -maxdepth 0 -printf '%D') && [ -n "$rootdev" ] \
  || fail "users isolation could not read the device of /"
scan "users isolation could not scan / for world-writable directories" \
  -P / -xdev -ignore_readdir_race -type d -perm -0002 -prune ! -path "*$nl*" -printf '%D %p\n'
awk -v d="$rootdev" '$1 == d { sub(/^[^ ]* /, ""); print }' "$dir/found" >"$dir/onroot" \
  || fail "users isolation could not list the world-writable directories"
head -n $((writable_cap + 1)) "$dir/onroot" | sed 's/^/writable /' >"$dir/writable" \
  || fail "users isolation could not list the world-writable directories"
# the answer: at most one line per id in the window, and the world-writable directories,
# between markers, so that output a provider cut short is found out rather than read as
# fewer taken ids. Each step is checked, since a step that fails would otherwise leave
# out ids or users.
awk -F: -v g="$group" '$1 == g { print "groupname", $3 }' /etc/group >"$dir/answer" \
  || fail "users isolation could not read /etc/group"
awk -F: '$1 ~ /^swarm-/ { print "username", $1 }' /etc/passwd >>"$dir/answer" \
  || fail "users isolation could not read /etc/passwd"
awk -F: '$1 ~ /^swarm-/ { print "membergroup", $1 }' /etc/group >>"$dir/answer" \
  || fail "users isolation could not read /etc/group"
# a user created with a name a group already lists would join that group
awk -F: '{ n = split($4, m, ","); for (i = 1; i <= n; i++) if (m[i] ~ /^swarm-/) print "listedmember", m[i], $1 }' /etc/group >>"$dir/answer" \
  || fail "users isolation could not read /etc/group"
awk -v lo="$lo" -v hi="$hi" '$1 >= lo && $1 <= hi { print "id", $1 }' "$dir/ids" >"$dir/window" \
  || fail "users isolation could not list the ids it found"
sort -u "$dir/window" >>"$dir/answer" || fail "users isolation could not list the ids it found"
cat -- "$dir/writable" >>"$dir/answer" || fail "users isolation could not list the world-writable directories"
echo begin && cat "$dir/answer" && echo end || fail "users isolation could not write the ids it found"
exit 0
"""
)

# Directories setup checks: each must be a real directory, owned by root and not
# writable by group or others, so that no member can move what is below it.
_CHECKS = r"""
check_dir() {
  info=$(find -P "$1" -maxdepth 0 -printf '%y %U %m') || fail "users isolation could not inspect $1"
  set -- "$1" "$2" $info
  [ "$3" = d ] || fail "$1 is a symlink or not a directory; users isolation needs every directory above $2 to be a real directory"
  [ "$4" = 0 ] || fail "$1 is owned by uid $4; users isolation needs every directory above $2 to be root-owned and not group- or world-writable"
  [ $((0$5 & 022)) -eq 0 ] || fail "$1 is mode $5; users isolation needs every directory above $2 to be root-owned and not group- or world-writable"
}
# $1 and every directory above it, created (root, 0755) where missing
check_chain() {
  d=""
  rest=${1#/}
  check_dir / "$2"
  while [ -n "$rest" ]; do
    case $rest in
      */*) comp=${rest%%/*}; rest=${rest#*/} ;;
      *) comp=$rest; rest="" ;;
    esac
    d="$d/$comp"
    [ -e "$d" ] || [ -L "$d" ] || mkdir -m 0755 -- "$d" || fail "users isolation could not create $d"
    check_dir "$d" "$2"
  done
}
"""

# Creates the group, the users and their homes, and prepares the rw shares. It runs once
# per sandbox, before any member exists, so no member can change a share while it does.
# $1 group, $2 its gid, $3 peers, $4 the homes' parent, then `user <name> <uid> <home>`
# and `share <rw|ro> <path>` items.
_SETUP = (
    _PRELUDE
    + _CHECKS
    + r"""
group=$1 gid=$2 peers=$3 parent=$4
shift 4

check_paths() {
  check_chain "$parent" "a member's home"
  while [ $# -gt 0 ]; do
    case $1 in
      user)
        [ -e "$4" ] || [ -L "$4" ] && fail "$4 already exists; users isolation creates each member's home, so leave it out of the image and the sample's files"
        shift 4 ;;
      share)
        check_chain "$(dirname -- "$3")" "a shared directory"
        if [ -e "$3" ] || [ -L "$3" ]; then
          [ "$(find -P "$3" -maxdepth 0 -printf '%y')" = d ] || fail "$3 is a symlink or not a directory; users isolation needs each shared directory to be a real directory"
        else
          mkdir -m 0755 -- "$3" || fail "users isolation could not create $3"
        fi
        shift 3 ;;
    esac
  done
}

add_users() {
  shell=/bin/sh
  [ -x /bin/bash ] && shell=/bin/bash
  while [ $# -gt 0 ]; do
    case $1 in
      user)
        name=$2 uid=$3 home=$4
        groupadd -g "$uid" "$name" || fail "users isolation could not create the group $name"
        # -M: no copy of the image's /etc/skel, whose modes could let peers write
        useradd -u "$uid" -g "$uid" -G "$group" -d "$home" -M -s "$shell" "$name" || fail "users isolation could not create the user $name"
        # ~/tmp takes the home's policy, so that peers can read what a member leaves in $TMPDIR
        for d in "$home" "$home/tmp"; do
          mkdir -m 0700 -- "$d" && chown -h "$uid:$uid" "$d" || fail "users isolation could not create $d"
          if [ "$peers" = read ]; then
            chgrp -h "$group" "$d" && chmod 2750 "$d" || fail "users isolation could not set the mode of $d"
          fi
        done
        for f in .profile .bashrc; do
          printf '%s\n' 'export TMPDIR="$HOME/tmp"' >"$home/$f" && chown -h "$uid:$uid" "$home/$f" || fail "users isolation could not write $home/$f"
        done
        shift 4 ;;
      share) shift 3 ;;
    esac
  done
}


rw_share() {
  s=$1
  chown -h 0 "$s" || fail "users isolation could not change the owner of $s"
  find -P "$s" -xdev -exec chgrp -h "$group" {} + || fail "users isolation could not change the group of the files in $s"
  find -P "$s" -xdev ! -type l -exec chmod g+rwX {} + || fail "users isolation could not change the modes of the files in $s"
  # setgid so that new files take the group; not sticky, so that every member can
  # rename and delete every member's files
  find -P "$s" -xdev -type d -exec chmod g+s,-t {} + || fail "users isolation could not change the modes of the directories in $s"
  find -P "$s" -xdev ! -type l -exec setfacl -P -m "g:$group:rwX,m:rwX" {} + || fail "users isolation could not set ACLs on the files in $s; its filesystem may not support them"
  find -P "$s" -xdev -type d -exec setfacl -P -d -m "g:$group:rwX,m:rwX" {} + || fail "users isolation could not set default ACLs on the directories in $s; its filesystem may not support them"
  if command -v git >/dev/null 2>&1; then
    # added without checking for an equal entry: setup runs once per sandbox, and a
    # duplicate entry is harmless, while a line-by-line match could take a path with a
    # newline for another
    git config --system --add safe.directory "$s" || fail "users isolation could not add $s to git's safe.directory"
    # a repository's .git directory, or the .git file of a linked worktree or a submodule
    find -P "$s" -xdev -maxdepth 4 -name .git \( -type d -prune -o -type f \) -exec sh -c '
      for g in "$@"; do
        repo=${g%/.git}
        git config --system --add safe.directory "$repo" || exit 1
        if [ -f "$g/config" ] && [ ! -L "$g/config" ]; then
          git config --file "$g/config" core.sharedRepository group || exit 1
        fi
      done
    ' sh {} + || fail "users isolation could not configure the git repositories in $s"
  fi
}

# ro shares are left as they are: the probe checked their modes
shares() {
  while [ $# -gt 0 ]; do
    case $1 in
      user) shift 4 ;;
      share)
        if [ "$2" = rw ]; then rw_share "$3"; fi
        shift 3 ;;
    esac
  done
}

check_paths "$@"
groupadd -g "$gid" "$group" || fail "users isolation could not create the group $group"
add_users "$@"
shares "$@"
"""
)

# The self-test, run as a member through the provider's own exec(user=...). Checks
# that the member runs as its uid and cannot become root, that it can read every entry
# of each share and write every entry of a rw share, and that it can write no entry of
# a ro share.
# $1 uid, then `rw <path>` and `ro <path>` items.
_SELF_TEST = (
    _PRELUDE
    + r"""
uid=$1
shift
actual=$(id -u)
[ "$actual" = "$uid" ] || fail "commands for this member run as uid $actual, not $uid: the sandbox does not run commands as the user it is given"
if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
  fail "the member can become root with sudo; remove its sudoers entry from the image"
fi
while [ $# -gt 0 ]; do
  # every entry readable and every directory, the share's ancestors included, searchable
  unreadable=$(find -P "$2" -xdev -ignore_readdir_race ! -type l \( ! -readable -o -type d ! -executable \) -printf '%p\n' -quit 2>&1) \
    || fail "the member cannot read all of the shared directory $2: $(printf '%s' "$unreadable" | head -n 1)"
  [ -z "$unreadable" ] || fail "the member cannot read $unreadable in the shared directory $2"
  if [ "$1" = rw ]; then
    w=$(find -P "$2" -xdev -ignore_readdir_race ! -type l ! -writable -printf '%p\n' -quit 2>&1) \
      || fail "the member cannot read all of the shared directory $2: $(printf '%s' "$w" | head -n 1)"
    [ -z "$w" ] || fail "the member cannot write $w in the read-write shared directory $2"
  else
    w=$(find -P "$2" -xdev -ignore_readdir_race -writable -print -quit 2>&1) \
      || fail "the member cannot read all of the shared directory $2: $(printf '%s' "$w" | head -n 1)"
    [ -z "$w" ] || fail "the member can write $w in the read-only shared directory $2"
  fi
  shift 2
done
"""
)

# The probes, run as each member once every member's self-test has passed, so that no
# self-test's scan meets another member's probes. Reserves a directory with mktemp -d
# in the member's home and in each rw share, prints each as it reserves it, so that
# cleanup removes those directories and nothing else, and puts a file `probe` in each
# for the pair check. Also checks the member can create nothing in a ro share.
# $1 home, then `rw <path>` and `ro <path>` items.
_PROBES = (
    _PRELUDE
    + r"""
home=$1
shift
reserve() {
  d=$(mktemp -d "$1/.swarm-probe-XXXXXXXX") || return 1
  printf '%s\n' "$d"
}
# mktemp makes a directory 0700; give it the mode mkdir would
{ reserve "$home" && chmod go+rx "$d" && printf probe >"$d/probe"; } || fail "the member could not create a file in its home $home"
while [ $# -gt 0 ]; do
  if [ "$1" = rw ]; then
    { reserve "$2" && chmod g+rwx "$d" && printf probe >"$d/probe"; } || fail "the member could not create a file in the shared directory $2"
  elif reserve "$2" 2>/dev/null; then
    fail "the member could create a directory in the read-only shared directory $2"
  fi
  shift 2
done
"""
)

# The pair check, run as a second member against the first's probe files.
# $1 the first member's home, $2 its probe file there, $3 peers, then its probe file in
# each rw share.
_PEER_TEST = (
    _PRELUDE
    + r"""
home=$1 file=$2 peers=$3
shift 3
fs="the filesystem at $(dirname -- "$home") does not enforce Unix permissions (a host bind mount?)"
if cat -- "$file" >/dev/null 2>&1; then
  [ "$peers" = read ] || fail "a peer could read $file: $fs"
else
  [ "$peers" = none ] || fail "a peer could not read $file, with peers='read'"
fi
if (printf x >>"$file") 2>/dev/null; then
  fail "a peer could write $file: $fs"
fi
# a rename to a sibling that does not exist, not into a directory (-T): one rename(2)
# in a directory no member can write, so none can create the sibling after the check,
# and on one filesystem, so mv does not fall back to a copy
moved=$home.swarm-probe
if [ -e "$moved" ] || [ -L "$moved" ]; then
  fail "$moved already exists, so the self-test cannot check that a peer cannot rename $home; remove it from the image and the sample's files"
fi
if mv -T -- "$home" "$moved" 2>/dev/null; then
  mv -T -- "$moved" "$home"
  fail "a peer could rename $home"
fi
for f in "$@"; do
  s=$(dirname -- "$(dirname -- "$f")")
  (printf x >>"$f") 2>/dev/null || fail "a member could not change a file another member created in the shared directory $s; its filesystem may not keep ACLs"
  { mv -- "$f" "$f.moved" && mv -- "$f.moved" "$f"; } 2>/dev/null || fail "a member could not rename a file another member created in the shared directory $s"
done
"""
)


# Sets up users isolation in the sandbox, before any member starts: creates the users,
# homes and shares, and checks that the boundaries hold. Returns the sandbox, each
# member's identity and the census roots.
async def setup_isolation(
    isolation: UserIsolation, names: Sequence[str]
) -> "IsolationSetup":
    if sandbox_identity_active():
        raise RuntimeError(
            "users isolation cannot start inside a sandbox identity scope, such as a member of a swarm with users isolation: identity scopes cannot be nested. Run this swarm without isolation, or outside the scope."
        )
    env, env_name = _environment(isolation.sandbox)
    if _is_local(env):
        raise RuntimeError(
            "users isolation needs a sandbox that can run commands as other users; 'local' runs everything as the host user."
        )
    # once per sandbox: a later setup would rewrite shares that the members of the first
    # can change while it runs. The claim comes before any await, so of two concurrent
    # setups the second is refused.
    if not _claim(env):
        raise RuntimeError(
            f"users isolation sets up a sandbox once per sample, and another swarm with users isolation in this sample has set up sandbox {env_name!r}, or tried to. Run the other swarms without isolation, or give them another sandbox."
        )
    access = await resolve_root_access(env)
    if access.state != "usable":
        raise RuntimeError(
            f"users isolation needs root in sandbox {env_name!r} to create users; its root access is {access.state} ({access.reason})."
        )
    return await _setup(env, env_name, isolation, names)


async def _setup(
    env: SandboxEnvironment,
    env_name: str,
    isolation: UserIsolation,
    names: Sequence[str],
) -> "IsolationSetup":
    users_ = [(name, member_user(name), member_home(isolation, name)) for name in names]
    parent = posixpath.dirname(isolation.home)

    roots = [
        "/",
        *(share.path for share in isolation.shared),
        parent,
        *_TRANSIENT,
    ]
    probe = await _run(
        env,
        env_name,
        _PROBE,
        _GROUP,
        str(_FIRST_ID),
        str(_LAST_ID),
        "1" if isolation.shared else "0",
        parent,
        str(_WRITABLE_CAP),
        *dict.fromkeys(roots),
        "--",
        *(item for share in isolation.shared for item in (share.access, share.path)),
    )
    taken: set[int] = set()
    existing_group = False
    existing_users: set[str] = set()
    existing_groups: set[str] = set()
    # the groups that list each swarm-* user name as a member
    listed: dict[str, str] = {}
    writable: list[str] = []
    lines = _lines(probe.stdout)
    if lines[:1] != ["begin"] or lines[-1:] != ["end"]:
        raise RuntimeError(
            f"users isolation: the id scan's output in sandbox {env_name!r} came back incomplete (the provider may limit command output), so it cannot choose ids that own nothing."
        )
    for line in lines[1:-1]:
        kind, _, value = line.partition(" ")
        if kind == "id" and value.isdigit():
            taken.add(int(value))
        elif kind == "groupname":
            existing_group = True
        elif kind == "username":
            existing_users.add(value)
        elif kind == "membergroup":
            existing_groups.add(value)
        elif kind == "listedmember":
            listed_user, _, listed_group = value.partition(" ")
            listed.setdefault(listed_user, listed_group)
        elif kind == "writable":
            writable.append(value)
    if existing_group:
        raise RuntimeError(
            f"users isolation: the group {_GROUP} already exists in sandbox {env_name!r}. Users isolation creates it, so the image must not have it."
        )
    for _, user, _ in users_:
        if user in existing_users:
            raise RuntimeError(
                f"users isolation: the user {user} already exists in sandbox {env_name!r}. Users isolation creates its members' users, so the image must not have users named swarm-<member>."
            )
        if user in existing_groups:
            raise RuntimeError(
                f"users isolation: the group {user} already exists in sandbox {env_name!r}. Users isolation creates each member's own group, so the image must not have groups named swarm-<member>."
            )
        if user in listed:
            raise RuntimeError(
                f"users isolation: the group {listed[user]} already lists {user} as a member in sandbox {env_name!r}. Users isolation creates its members' users, so the image's groups must not list users named swarm-<member>."
            )
    free = (i for i in range(_FIRST_ID, _LAST_ID + 1) if i not in taken)
    gid = next(free, None)
    uids = [next(free, None) for _ in users_]
    if gid is None or None in uids:
        raise RuntimeError(
            f"users isolation: sandbox {env_name!r} has too few free user ids between {_FIRST_ID} and {_LAST_ID} for {len(users_)} members."
        )
    identities = {
        name: MemberIdentity(user, cast(int, uid), home, isolation.sandbox)
        for (name, user, home), uid in zip(users_, uids, strict=True)
    }

    items: list[str] = []
    for identity in identities.values():
        items += ["user", identity.user, str(identity.uid), identity.home]
    for share in isolation.shared:
        items += ["share", share.access, share.path]
    await _run(
        env,
        env_name,
        _SETUP,
        _GROUP,
        str(gid),
        isolation.peers,
        parent,
        *items,
    )

    await _self_test(env, env_name, isolation, list(identities.values()))
    return IsolationSetup(
        environment=env,
        sandbox=env_name,
        identities=identities,
        census_roots=_census_roots(isolation, writable),
        census_roots_capped=len(writable) > _WRITABLE_CAP,
    )


# The census roots: the rw shares, the directories every user writes, and the other
# world-writable directories on the root filesystem, up to the cap. A world-writable
# directory that overlaps a share or one of the others is left out, so that no entry is
# counted twice.
def _census_roots(
    isolation: UserIsolation, writable: Sequence[str]
) -> list[CensusRoot]:
    shares = [share.path for share in isolation.shared if share.access == "rw"]
    roots = [CensusRoot(path, "share") for path in shares]
    roots += [CensusRoot(path, "transient") for path in _TRANSIENT]
    for path in writable[:_WRITABLE_CAP]:
        if not any(_nested(path, root.path) for root in roots):
            roots.append(CensusRoot(path, "world_writable"))
    return roots


async def _self_test(
    env: SandboxEnvironment,
    env_name: str,
    isolation: UserIsolation,
    identities: list[MemberIdentity],
) -> None:
    # the probe directories the members reserved, by member and by the directory
    # each is in
    reserved: dict[str, dict[str, str]] = {}
    try:
        await _check_members(env, env_name, isolation, identities, reserved)
    except BaseException:
        # the self-test's own error is the one to report
        with anyio.move_on_after(_CLEANUP_BOUND, shield=True), suppress(Exception):
            await _remove_probes(env, env_name, reserved)
        raise
    with anyio.move_on_after(_CLEANUP_BOUND, shield=True):
        await _remove_probes(env, env_name, reserved)


# The probes' cleanup is shielded from cancellation, so it has bounds of its own: its
# exec's timeout, and a wall-clock bound past that for a provider that overruns it.
# Past the bound the probes are left in place.
_CLEANUP_TIMEOUT = 30
_CLEANUP_BOUND = 40.0


# removes the probe directories the self-test reserved, and nothing else
async def _remove_probes(
    env: SandboxEnvironment, env_name: str, reserved: dict[str, dict[str, str]]
) -> None:
    dirs = [d for by_parent in reserved.values() for d in by_parent.values()]
    if dirs:
        await _run(env, env_name, 'rm -rf -- "$@"', *dirs, timeout=_CLEANUP_TIMEOUT)


_PROBE_DIR = re.compile(r"\.swarm-probe-[A-Za-z0-9]{8}")


async def _check_members(
    env: SandboxEnvironment,
    env_name: str,
    isolation: UserIsolation,
    identities: list[MemberIdentity],
    reserved: dict[str, dict[str, str]],
) -> None:
    shares: list[str] = []
    for share in isolation.shared:
        shares += [share.access, share.path]
    rw = [share.path for share in isolation.shared if share.access == "rw"]

    # runs a script as each member, concurrently, with the member's own argument first
    async def each_member(
        script: str, own: Callable[[MemberIdentity], str], *args: str
    ) -> None:
        results: dict[str, ExecResult[str] | BaseException] = {}

        async def run(identity: MemberIdentity) -> None:
            try:
                result = await privileged_shell(
                    env,
                    script,
                    own(identity),
                    *args,
                    user=identity.user,
                    cwd=identity.home,
                )
            except Exception as ex:
                results[identity.user] = ex
                return
            results[identity.user] = result
            if script is _PROBES:
                # each line a directory it reserved, in its home or a share, whether
                # or not it went on to pass
                parents = {identity.home, *(share.path for share in isolation.shared)}
                reserved[identity.user] = {
                    posixpath.dirname(line): line
                    for line in result.stdout.splitlines()
                    if posixpath.dirname(line) in parents
                    and _PROBE_DIR.fullmatch(posixpath.basename(line))
                }

        async with anyio.create_task_group() as tg:
            for identity in identities:
                tg.start_soon(run, identity)
        for identity in identities:
            _check_result(results[identity.user], env_name, identity.user)

    await each_member(_SELF_TEST, lambda identity: str(identity.uid), *shares)
    await each_member(_PROBES, lambda identity: identity.home, *shares)
    if len(identities) > 1:
        first, second = identities[0], identities[1]
        probes = reserved[first.user]
        result = await privileged_shell(
            env,
            _PEER_TEST,
            first.home,
            f"{probes[first.home]}/probe",
            isolation.peers,
            *(f"{probes[path]}/probe" for path in rw),
            user=second.user,
            cwd=second.home,
        )
        _check_result(result, env_name, second.user)


def _check_result(
    result: ExecResult[str] | BaseException, env_name: str, user: str
) -> None:
    if isinstance(result, BaseException):
        raise result
    if not result.success:
        raise RuntimeError(
            f"users isolation: the self-test failed in sandbox {env_name!r} as {user}: {_message(result)}"
        )


async def _run(
    env: SandboxEnvironment,
    env_name: str,
    script: str,
    *args: str,
    timeout: int | None = None,
) -> ExecResult[str]:
    result = await privileged_shell(env, script, *args, user="root", timeout=timeout)
    if not result.success:
        raise RuntimeError(
            f"users isolation could not set up sandbox {env_name!r}: {_message(result)}"
        )
    return result


# A script's output lines. Only LF ends a line: the scripts write each record on one
# line, and a path in a record can hold any other character, such as a CR or a Unicode
# line separator, which `str.splitlines()` would take for the end of a line.
def _lines(text: str) -> list[str]:
    return text.removesuffix("\n").split("\n")


def _message(result: ExecResult[str]) -> str:
    text = (result.stderr or result.stdout).strip()
    if result.returncode == _REFUSED:
        # the script's own refusal is its last line
        return text.splitlines()[-1] if text else "refused"
    return text or f"exit status {result.returncode}"


# Kills every process in the sandbox whose real uid is the member's, as root: scans
# /proc, reads each match's Uid line again just before it kills it, so that a pid reused
# since the scan is not killed, and repeats until a scan finds none or five rounds have
# run, since a process can start another between rounds. A zombie that has fully exited
# waits for its parent to collect it, so it is not counted; one whose main thread exited
# while its other threads run is killed and counted. Prints the number killed and the
# number left, and the first 50 names of each, between markers. A name is read up to its
# first newline, and every character in it other than printable ASCII, and every ':', is
# printed as '?', so that no name can read as an attachment reference in the record.
# $1 the member's uid.
_REAP = (
    _PRELUDE
    + r"""
uid=$1
nl='
'
# $u the process's real uid, and $z set when it has fully exited: a zombie whose thread
# group has no other task; $u empty when it has gone. A zombie leader whose other
# threads still run is live, and a kill of its pid reaches them.
proc_uid() {
  u= z= state=
  while read -r k v _; do
    case $k in
      State:) state=$v ;;
      Uid:) u=$v ;;
      Threads:) [ "$state" = Z ] && [ "$v" = 1 ] && z=1; break ;;
    esac
  done 2>/dev/null <"/proc/$1/status"
}
# a pid killed in an earlier round is counted once, by a shell variable named after it,
# so that nothing here writes to a filesystem a member could have filled
killed=0 left=0 knames= lnames= round=0
while :; do
  found=0
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    case $pid in *[!0-9]*) continue ;; esac
    proc_uid "$pid"
    [ "$u" = "$uid" ] && [ -z "$z" ] || continue
    found=1
    name=
    { IFS= read -r name; } 2>/dev/null <"$d/comm"
    if [ "$round" -lt 5 ]; then
      proc_uid "$pid"
      [ "$u" = "$uid" ] && [ -z "$z" ] || continue
      kill -KILL "$pid" 2>/dev/null || continue
      eval "[ -z \"\${seen_$pid-}\" ]" || continue
      eval "seen_$pid=1"
      killed=$((killed + 1))
      [ "$killed" -gt 50 ] || knames="$knames$name$nl"
    else
      left=$((left + 1))
      [ "$left" -gt 50 ] || lnames="$lnames$name$nl"
    fi
  done
  { [ "$found" = 0 ] || [ "$round" -ge 5 ]; } && break
  round=$((round + 1))
  sleep 0.1
done
names() {
  printf '%s' "$2" | tr -c '[:print:]\n' '?' | tr ':' '?' | sed "s/^/$1 /"
}
{
  echo begin
  echo "killed $killed"
  echo "left $left"
  names killed-name "$knames" && names left-name "$lnames" && echo end
} || fail "could not report the processes it killed"
"""
)

# Reaping is shielded from cancellation, so that a hard stop does not skip it, and so has
# bounds of its own: its exec's timeout, and a wall-clock bound past that for a provider
# that overruns it.
_REAP_TIMEOUT = 30
_REAP_BOUND = 40.0


# Stops the processes a member left running. Never raises, so that it never replaces
# the member's own outcome: a failure is its result.
async def reap_member(env: SandboxEnvironment, identity: MemberIdentity) -> MemberReap:
    reap: MemberReap | None = None
    with anyio.move_on_after(_REAP_BOUND, shield=True):
        reap = await _reap(env, identity)
    if reap is None:
        return MemberReap(
            status="timeout",
            message=f"the sandbox did not finish stopping the member's processes within {_REAP_BOUND:g} seconds",
        )
    return reap


async def _reap(env: SandboxEnvironment, identity: MemberIdentity) -> MemberReap:
    try:
        result = await privileged_shell(
            env,
            _REAP,
            str(identity.uid),
            user="root",
            timeout=_REAP_TIMEOUT,
            timeout_retry=False,
            # bounded harness cleanup, which must not wait behind members' and other
            # samples' commands for a subprocess slot
            concurrency=False,
        )
    except TimeoutError as ex:
        return MemberReap(status="timeout", message=str(ex) or type(ex).__name__)
    except Exception as ex:
        return MemberReap(status="error", message=f"{type(ex).__name__}: {ex}")
    lines = _lines(result.stdout)
    if not result.success or lines[:1] != ["begin"] or lines[-1:] != ["end"]:
        return MemberReap(
            status="error",
            message=_message(result)
            if not result.success
            else "the output of the command that stops the member's processes came back incomplete",
        )
    counts: dict[str, int] = {}
    names: dict[str, list[str]] = {"killed-name": [], "left-name": []}
    for line in lines[1:-1]:
        kind, _, value = line.partition(" ")
        if kind in names:
            names[kind].append(value)
        elif value.isdigit():
            counts[kind] = int(value)
    left = counts.get("left", 0)
    return MemberReap(
        status="survivors" if left else "ok",
        killed=counts.get("killed", 0),
        killed_names=names["killed-name"],
        survivors=left,
        survivor_names=names["left-name"],
    )


# Counts who owns the entries under each census root, as root, from the root's own
# filesystem (-xdev), never following a symlink: for each owner, its entries and the
# bytes of its regular files, and for each member uid the first 50 entries' types and
# paths, any newline in a path as '?'. A root stops being counted at the cap, and says
# so. A root that is not a directory is absent; one the scan could not read all of, or
# whose scan failed, says so, with find's first error (its first bytes) or its exit
# status, besides what it counted. The sampled paths are bounded in bytes, so that the
# output stays far below what a provider returns: a path longer than the per-path bound
# is cut to it and marked (`tpath`), and once the paths written reach the census's
# total bound the rest are counted as omitted (`omitted`); the owners' counts are not
# affected.
# $1 the cap, $2 the member uids, space-separated, $3 the per-path bound in bytes, $4
# the total bound in bytes, then the roots.
_CENSUS = (
    _PRELUDE
    + r"""
cap=$1 members=$2 maxlen=$3 budget=$4
shift 4
dir=$(mktemp -d) || fail "could not create a temporary directory"
trap 'rm -rf "$dir"' EXIT
echo begin
for root in "$@"; do
  echo root
  if [ -L "$root" ] || [ ! -d "$root" ]; then
    echo absent
    continue
  fi
  rm -f "$dir/status" "$dir/capped" "$dir/spent"
  # find's status, which a pipeline does not report, is kept in a file; find stops once
  # awk has read one entry past the cap, which awk marks with a file of its own
  { find -P "$root" -xdev -ignore_readdir_race -mindepth 1 -printf '%U %y %s %p\0' 2>"$dir/errors"; echo $? >"$dir/status"; } \
    | tr '\n\0' '?\n' \
    | awk -v cap="$cap" -v members=" $members " -v flag="$dir/capped" \
        -v maxlen="$maxlen" -v budget="$budget" -v spentfile="$dir/spent" '
        NR > cap { print "capped"; printf "" > flag; exit }
        {
          n[$1]++
          if ($2 == "f") b[$1] += $3
          if (index(members, " " $1 " ") && k[$1]++ < 50) {
            path = $0
            sub(/^[^ ]* [^ ]* [^ ]* /, "", path)
            kind = "path"
            # bytes: the script runs in the C locale
            if (length(path) > maxlen) { path = substr(path, 1, maxlen); kind = "tpath" }
            if (spent + length(path) > budget) omitted++
            else { spent += length(path); print kind, $1, $2, path }
          }
        }
        END {
          for (u in n) printf "owner %s %d %.0f\n", u, n[u], b[u]
          if (omitted) printf "omitted %d\n", omitted
          printf "%d", spent > spentfile
        }
      ' || fail "could not count the entries in $root"
  spent=$(cat "$dir/spent") || fail "could not count the bytes of the paths in $root"
  budget=$((budget - spent))
  status=$(cat "$dir/status" 2>/dev/null) || status=unknown
  if [ -e "$dir/capped" ]; then
    # find was stopped at the cap, by SIGPIPE or a write error; an entry removed while
    # the scan ran is not an error
    other=$(grep -v -e ': No such file or directory$' -e 'Broken pipe$' "$dir/errors" | head -n 1 | cut -c "1-$maxlen")
    [ -z "$other" ] || printf 'error %s\n' "$other"
  else
    other=$(grep -v ': No such file or directory$' "$dir/errors" | head -n 1 | cut -c "1-$maxlen")
    if [ -n "$other" ]; then
      printf 'error %s\n' "$other"
    elif [ "$status" != 0 ] && { [ "$status" != 1 ] || [ ! -s "$dir/errors" ]; }; then
      # find exits 1 after errors it reports; a silent failure, another status or a
      # signal (such as the kernel's out-of-memory kill) could leave out anything
      echo "error find exited with status $status"
    fi
  fi
done
echo end
"""
)

# the census's cap on the entries it counts in each root
_CENSUS_CAP = 10_000

# the census's bounds on the bytes of a sampled path, and of all of them: a path is
# cut to the first, and paths past the second are left out
_CENSUS_PATH_BYTES = 1024
_CENSUS_PATHS_BYTES = 1024 * 1024

# the census is not shielded, but has the same bounds as reaping
_CENSUS_TIMEOUT = 60
_CENSUS_BOUND = 70.0

# find's letters for the types of entry
_TYPES = {
    "f": "file",
    "d": "directory",
    "l": "symlink",
    "p": "fifo",
    "s": "socket",
    "b": "block_device",
    "c": "char_device",
}


# Who owns the entries under each census root, once the members have ended: the census
# event's data. Never raises an error: a failure is its result.
async def take_census(setup: IsolationSetup) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] | None = None
    with anyio.move_on_after(_CENSUS_BOUND):
        data = await _census(setup)
    if data is None:
        return {
            "status": "timeout",
            "message": f"the sandbox did not finish the census within {_CENSUS_BOUND:g} seconds",
        }
    return data


async def _census(setup: IsolationSetup) -> dict[str, JsonValue]:
    members = {str(i.uid): name for name, i in setup.identities.items()}
    try:
        result = await privileged_shell(
            setup.environment,
            _CENSUS,
            str(_CENSUS_CAP),
            " ".join(members),
            str(_CENSUS_PATH_BYTES),
            str(_CENSUS_PATHS_BYTES),
            *(root.path for root in setup.census_roots),
            user="root",
            timeout=_CENSUS_TIMEOUT,
            timeout_retry=False,
            # as for reaping: not queued behind members' commands
            concurrency=False,
        )
    except TimeoutError as ex:
        return {"status": "timeout", "message": str(ex) or type(ex).__name__}
    except Exception as ex:
        return {"status": "error", "message": f"{type(ex).__name__}: {ex}"}
    lines = _lines(result.stdout)
    if not result.success or lines[:1] != ["begin"] or lines[-1:] != ["end"]:
        return {
            "status": "error",
            "message": _message(result)
            if not result.success
            else "the census's output came back incomplete",
        }
    sections: list[list[str]] = []
    for line in lines[1:-1]:
        if line == "root":
            sections.append([])
        elif sections:
            sections[-1].append(line)
    if len(sections) != len(setup.census_roots):
        return {
            "status": "error",
            "message": "the census's output came back incomplete",
        }
    try:
        roots: list[JsonValue] = [
            _census_root(root, section, members)
            for root, section in zip(setup.census_roots, sections, strict=True)
        ]
    except ValueError:
        return {"status": "error", "message": "the census's output could not be read"}
    return {"status": "ok", "roots": roots}


# One root's counts: by member, with the member's first paths, and for everyone else.
# Raises ValueError for a line the census script does not write.
def _census_root(
    root: CensusRoot, lines: list[str], members: Mapping[str, str]
) -> dict[str, JsonValue]:
    status = "ok"
    error: str | None = None
    capped = False
    counts: dict[str, dict[str, JsonValue]] = {}
    paths: dict[str, list[JsonValue]] = {}
    others = {"entries": 0, "bytes": 0}
    # sampled paths left out once the census's paths reached their total bound
    omitted = 0
    for line in lines:
        kind, _, rest = line.partition(" ")
        if kind == "absent":
            status = "absent"
        elif kind == "capped":
            capped = True
        elif kind == "error":
            status, error = "unreadable", rest
        elif kind == "owner":
            uid, entries, size = rest.split(" ")
            if uid in members:
                counts[members[uid]] = {"entries": int(entries), "bytes": int(size)}
            else:
                others["entries"] += int(entries)
                others["bytes"] += int(size)
        elif kind in ("path", "tpath"):
            uid, letter, path = rest.split(" ", 2)
            if uid in members:
                entry: dict[str, JsonValue] = {
                    "type": _TYPES.get(letter, letter),
                    "path": path,
                }
                if kind == "tpath":
                    # its first bytes, cut to the per-path bound
                    entry["truncated"] = True
                paths.setdefault(members[uid], []).append(entry)
        elif kind == "omitted":
            omitted = int(rest)
        else:
            raise ValueError(line)
    return {
        "path": root.path,
        "kind": root.kind,
        "status": status,
        "error": error,
        "capped": capped,
        # in roster order, those with entries
        "members": {
            name: {**counts[name], "paths": paths.get(name, [])}
            for name in members.values()
            if name in counts
        },
        "others": dict[str, JsonValue](others),
        "paths_omitted": omitted,
    }


# The environment named by `users(sandbox=...)`, and its name, checked against the
# sample's environments: sandbox(name) returns the default for any name when the
# sample has only one.
def _environment(name: str) -> tuple[SandboxEnvironment, str]:
    environments = sandbox_environments_context_var.get(None)
    if not environments:
        raise RuntimeError("users isolation needs a sandbox, and this sample has none.")
    if name == "default":
        return sandbox(), sandbox_default_context_var.get("default")
    if name not in environments:
        raise ValueError(
            f"users(sandbox={name!r}): this sample has no such sandbox. Its sandboxes are: {', '.join(repr(e) for e in environments)}."
        )
    return environments[name], name


def _is_local(env: SandboxEnvironment) -> bool:
    try:
        env.as_type(LocalSandboxEnvironment)
    except TypeError:
        return False
    return True


# The sandboxes users isolation has set up, or started to, by the identity of the
# provider's object, which need not be hashable and may compare equal to another
# sandbox's. Kept with the sandbox's object in the host process rather than in the
# store, which a subtask or fork has its own copy of: every swarm run in the sample
# that uses the sandbox sees it. An entry goes when its object does, before its id can
# be reused.
_SET_UP: "dict[int, weakref.ReferenceType[SandboxEnvironment]]" = {}


# claims the sandbox, or returns False if it was claimed already
def _claim(env: SandboxEnvironment) -> bool:
    provider = _provider(env)
    key = id(provider)
    claimed = _SET_UP.get(key)
    if claimed is not None and claimed() is provider:
        return False

    def release(ref: "weakref.ReferenceType[SandboxEnvironment]") -> None:
        if _SET_UP.get(key) is ref:
            del _SET_UP[key]

    _SET_UP[key] = weakref.ref(provider, release)
    return True


# the provider's object, behind the sample's event-recording proxy
def _provider(env: SandboxEnvironment) -> SandboxEnvironment:
    while (inner := env.as_type(SandboxEnvironment)) is not env:
        env = inner
    return env
