import posixpath
import re
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import anyio
from inspect_ai.tool._sandbox_tools_utils.sandbox import resolve_root_access
from inspect_ai.util import ExecResult, SandboxEnvironment, sandbox, store
from inspect_ai.util._sandbox._privileged import privileged_shell
from inspect_ai.util._sandbox.context import (
    sandbox_default_context_var,  # pyright: ignore[reportPrivateUsage]
    sandbox_environments_context_var,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._sandbox.identity import sandbox_identity
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment
from pydantic import BaseModel, ConfigDict

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
    """`rw`: every member can read, create, change and delete everything in it. `ro`: every member can read it and none can write it."""


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

    When the swarm starts, before any member does, it creates a non-root user `swarm-<member>` for each member, with a new private home directory (the image's `/etc/skel` is not copied), sets up the shared directories, and checks that the boundaries hold by running commands as the members. It refuses to start if any check fails. Each member then runs inside a sandbox identity scope, so that the built-in tools that run commands in the sandbox (`bash()`, `python()`, `bash_session()`, `text_editor()`, `grep()`, `list_files()` and the `read_file()` tool) run as its user and start in its home. Its preamble tells it its user and directories.

    A member cannot read or change another member's home unless `peers="read"` lets it read, and cannot write a `ro` share. It can still see the other members' processes and command lines, share files through `/tmp`, and connect to their sockets on localhost. Members share one kernel and one sandbox root: this is a default boundary and a record of who made what, not containment.

    Requirements and limits:

    - The sandbox must let commands run as root and as other users: Docker, or Kubernetes with a root container. The `local` sandbox is refused.
    - The image needs `useradd` and `groupadd` (package `passwd`) and GNU `find`; with any share, also `setfacl` and `getfacl` (package `acl`), which the default Inspect image and `python:3.12-bookworm` lack.
    - Homes and shares must sit under directories owned by root and not writable by group or others, so a share under `/tmp` is refused. Homes must not exist yet. ACLs that setup did not set, in a share or on the homes' parent, and mounts inside a share or under the homes' parent are refused.
    - Members are not root: they cannot install system packages or write system directories. Tasks that need root while the members work are unsuitable.
    - Custom tools that call `sandbox().exec()` directly run as the sandbox's default user, usually root, unless they pass the member's user themselves. `computer()`, `web_browser()` and `skill()` raise inside the scope, and so do tools built with another `user`.
    - Bridged members (inspect_swe agents) need inspect_ai and inspect_swe versions with bridge support for users isolation, which they do not have yet.
    - Member names must be valid Linux user-name suffixes: lowercase letters, digits and `-`, starting with a letter, at most 26 characters.

    Args:
        sandbox: The sandbox environment the members share, by its name in the sample. `default` is the sample's default sandbox.
        home: Each member's private directory, which is also its `$HOME` and where its tools start. `{member}` stands for the member's name and must be in the last path component.
        shared: The directories every member shares, each read-write (`rw`) or read-only (`ro`). A `rw` share's files stay group-writable whatever a member's umask, through a default ACL. Directories that do not exist are created.
        peers: `none`: only the member can read its home. `read`: every member can read every other member's home, but not change it.

    Returns:
        The isolation, for `swarm(isolation=...)`.

    Raises:
        ValueError: If a path is not absolute and normalised, or holds a newline, NUL or backslash; `home` does not have `{member}` once, in its last component; or two shares, or a share and the homes' parent directory, are the same or one is inside the other.
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
    for path, field in [
        (isolation.home, "home"),
        *((share.path, "shared") for share in isolation.shared),
    ]:
        if (
            not posixpath.isabs(path)
            or path.startswith("//")
            or posixpath.normpath(path) != path
            or any(c in path for c in "\n\0\\")
        ):
            raise ValueError(
                f"{where}: {field} must be an absolute, normalised path with no '..', '.', repeated or trailing '/', newline, NUL or backslash, got {path!r}."
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
        if _nested(path, parent):
            raise ValueError(
                f"{where}: the share {path!r} and the members' homes ({home!r}) overlap; a share must not be the homes' parent directory, inside it or above it."
            )
        for other in paths[:i]:
            if _nested(path, other):
                raise ValueError(
                    f"{where}: the shares {other!r} and {path!r} overlap; no share may be inside another, or given twice."
                )


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
    filesystem: tuple[str, str] | None,
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
    shared, scratch = filesystem
    if scratch != isolation.home:
        raise ValueError(
            f"swarm(): the filesystem channel's scratch ({scratch!r}) is not the members' home under users isolation ({isolation.home!r}), so the preamble would describe a directory the member cannot use. Give filesystem(scratch=...) and users(home=...) the same value."
        )
    rw = [share.path for share in isolation.shared if share.access == "rw"]
    if shared not in rw:
        raise ValueError(
            f"swarm(): the filesystem channel's shared directory ({shared!r}) is not a read-write share under users isolation ({', '.join(repr(p) for p in rw) or 'none'}), so members could not write it. Add Share(path={shared!r}, access='rw') to users(shared=...), or give filesystem(shared=...) one of them."
        )


# A member's Linux user, set up when the swarm started, and the sandbox it is in.
@dataclass(frozen=True)
class MemberIdentity:
    user: str
    uid: int
    home: str
    sandbox: str


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
    return " ".join(parts)


# the group every member is in, which owns the rw shares
_GROUP = "inspect-swarm"

# member uids and gids, and the group's gid, are chosen from this window
_FIRST_ID = 61000
_LAST_ID = 61999

# what the swarm made in each sandbox, by sandbox name: the group's gid and each
# user's uid, so that a later swarm run in the sample accepts them
_RECORD_KEY = "inspect_swarm:isolation"

# Refusals in the scripts below exit with this status and a message on stderr, as
# their last line.
_REFUSED = 3

_PRELUDE = r"""
fail() { printf '%s\n' "$*" >&2; exit 3; }
"""

# Whether the image has what setup needs and a layout it supports, which users and group
# exist, and which ids in the window are taken, by a passwd or group entry or by the
# owner or group of a file on any filesystem the homes, shares or world-writable
# directories are on. Setup does not reconcile permission state it did not make, so it
# refuses ACL entries other than its own in a share or on the homes' parent, and a
# mount inside a share or under the homes' parent, whose files it would not reach.
# $1 group, $2 first id, $3 last id, $4 1 when there are shares, $5 the homes' parent,
# then the roots, `--`, and each share's access and path.
_PROBE = (
    _PRELUDE
    + r"""
group=$1 lo=$2 hi=$3 acl=$4 parent=$5
shift 5
command -v useradd >/dev/null 2>&1 || fail "users isolation needs useradd in the sandbox (package passwd)"
command -v groupadd >/dev/null 2>&1 || fail "users isolation needs groupadd in the sandbox (package passwd)"
command -v awk >/dev/null 2>&1 || fail "users isolation needs awk in the sandbox"
find / -maxdepth 0 -writable -printf '' >/dev/null 2>&1 || fail "users isolation needs GNU find in the sandbox (package findutils)"
if [ "$acl" = 1 ]; then
  command -v setfacl >/dev/null 2>&1 && command -v getfacl >/dev/null 2>&1 || fail "users isolation needs setfacl and getfacl in the sandbox for shared directories (package acl)"
fi
gid=$(awk -F: -v g="$group" '$1 == g { print $3 }' /etc/group)
dir=$(mktemp -d) || fail "users isolation could not create a temporary directory"
trap 'rm -rf "$dir"' EXIT

# a mount point strictly inside $1, as /proc/self/mountinfo writes it (\040 for a space;
# configured paths have no backslash, which awk -v would read as an escape)
mount_inside() {
  awk -v p="$1" '
    function esc(s,  r, i, c) {
      r = ""
      for (i = 1; i <= length(s); i++) {
        c = substr(s, i, 1)
        if (c == " ") r = r "\\040"
        else if (c == "\t") r = r "\\011"
        else r = r c
      }
      return r
    }
    BEGIN { p = esc(p); if (p != "/") p = p "/" }
    index($5, p) == 1 && $5 != p { print $5; exit }
  ' /proc/self/mountinfo
}

# the first ACL entry in getfacl's output (-n -p -E) that setup did not write: in an
# access ACL, a named entry other than group $1's; in a default ACL, anything but
# user::rwx, group $1 with rwx, mask::rwx and the group and other classes
foreign_acl() {
  awk -v g="$1" '
    function finish() {
      if (file != "" && defaults && !ours) { print file ": a default ACL setup did not write"; done = 1; exit }
    }
    /^# file: / { finish(); file = substr($0, 9); defaults = 0; ours = 0; next }
    /^#/ || NF == 0 { next }
    {
      n = split($0, f, ":")
      if (f[1] == "default") { d = 1; t = f[2]; q = f[3]; m = f[4] } else { d = 0; t = f[1]; q = f[2]; m = f[3] }
      bad = 0
      if (d) {
        defaults = 1
        if (t == "user") bad = (q != "" || m != "rwx")
        else if (t == "group" && q != "") { if (g != "" && q == g && m == "rwx") ours = 1; else bad = 1 }
        else if (t == "mask") bad = (m != "rwx")
      } else if (q != "" && !(t == "group" && g != "" && q == g)) bad = 1
      if (bad) { print file ": " $0; done = 1; exit }
    }
    END { if (!done) finish() }
  '
}

# a directory setup would create inherits the default ACL of its nearest existing
# ancestor, so that ancestor must have none
inherits_acl() {
  a=$1
  while [ ! -e "$a" ] && [ ! -L "$a" ]; do a=$(dirname -- "$a"); done
  case $(ls -ld -- "$a") in
    ??????????+*) fail "users isolation would create $1 inside $a, whose ACL its new directories would inherit; remove it (setfacl -b)" ;;
  esac
}

[ -r /proc/self/mountinfo ] || fail "users isolation needs /proc/self/mountinfo to check for mounts inside its directories"
if [ -e "$parent" ]; then
  m=$(mount_inside "$parent")
  [ -z "$m" ] || fail "users isolation does not support a mount under the members' homes' parent $parent, as at $m"
  case $(ls -ld -- "$parent") in
    ??????????+*) fail "users isolation does not support ACLs on the members' homes' parent $parent, which homes would inherit; remove them (setfacl -b)" ;;
  esac
else
  inherits_acl "$parent"
fi
awk -F: '{ print $3 }' /etc/passwd /etc/group >"$dir/ids"
while [ "$1" != -- ]; do
  root=$1
  shift
  [ -d "$root" ] || continue
  # an incomplete scan could miss an id a file takes
  find -P "$root" -xdev \( \( -uid +$((lo - 1)) -uid -$((hi + 1)) \) -o \( -gid +$((lo - 1)) -gid -$((hi + 1)) \) \) -printf '%U\n%G\n' >>"$dir/ids" 2>"$dir/errors" \
    || fail "users isolation could not scan all of $root for the user ids its files take, so it cannot choose ids that own nothing: $(head -n 1 "$dir/errors")"
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
  m=$(mount_inside "$share")
  [ -z "$m" ] || fail "users isolation does not support a mount inside the shared directory $share, as at $m"
  acls=$(getfacl -R -P -n -p -E --skip-base -- "$share" 2>&1) \
    || fail "users isolation could not read the ACLs in $share: $(printf '%s' "$acls" | grep -v '^#' | head -n 1)"
  bad=$(printf '%s\n' "$acls" | foreign_acl "$gid")
  [ -z "$bad" ] || fail "users isolation does not support ACL entries it did not set in a shared directory: $bad; remove them (setfacl -b)"
  if [ "$access" = rw ]; then
    # the default ACL setup writes takes its owner entry from the directory's owner
    # bits, and an owner entry beats the group's grant, so a member could not edit
    # the files it creates in a directory whose owner lacks rwx
    narrow=$(find -P "$share" -xdev -type d ! -perm -u=rwx -printf '%p is mode %m' -quit 2>&1) \
      || fail "users isolation could not scan the directories in $share: $(printf '%s' "$narrow" | head -n 1)"
    [ -z "$narrow" ] || fail "users isolation does not support a directory without owner rwx in a read-write shared directory: $narrow; its owner bits would become the default ACL of the files members create there"
  fi
done
# the answer: at most one line per id in the window, between markers, so that output a
# provider cut short is found out rather than read as fewer taken ids
echo begin
awk -F: -v g="$group" '$1 == g { print "groupname", $3 }' /etc/group
awk -F: '$1 ~ /^swarm-/ { print "username", $1 }' /etc/passwd
awk -v lo="$lo" -v hi="$hi" '$1 >= lo && $1 <= hi { print "id", $1 }' "$dir/ids" | sort -u
echo end
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

# Creates the group, the users and their homes, and prepares the shares.
# $1 group, $2 its gid, $3 1 to create it, $4 peers, $5 the homes' parent, $6 the uids
# of every member user in the sandbox (this run's and earlier runs'), then
# `user <name> <uid> <home>` and `share <rw|ro> <path>` items.
_SETUP = (
    _PRELUDE
    + _CHECKS
    + r"""
group=$1 gid=$2 newgroup=$3 peers=$4 parent=$5 ids=$6
shift 6

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
        mkdir -m 0700 -- "$home" && chown -h "$uid:$uid" "$home" || fail "users isolation could not create $home"
        if [ "$peers" = read ]; then
          chgrp -h "$group" "$home" && chmod 2750 "$home" || fail "users isolation could not set the mode of $home"
        fi
        mkdir -m 0700 -- "$home/tmp" && chown -h "$uid:$uid" "$home/tmp" || fail "users isolation could not create $home/tmp"
        for f in .profile .bashrc; do
          printf '%s\n' 'export TMPDIR="$HOME/tmp"' >"$home/$f" && chown -h "$uid:$uid" "$home/$f" || fail "users isolation could not write $home/$f"
        done
        shift 4 ;;
      share) shift 3 ;;
    esac
  done
}

safe_directory() {
  git config --system --get-all safe.directory 2>/dev/null | grep -Fxq -- "$1" || git config --system --add safe.directory "$1"
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
    safe_directory "$s" || fail "users isolation could not add $s to git's safe.directory"
    find -P "$s" -xdev -maxdepth 4 -type d -name .git -prune -exec sh -c '
      for g in "$@"; do
        repo=${g%/.git}
        git config --system --get-all safe.directory 2>/dev/null | grep -Fxq -- "$repo" || git config --system --add safe.directory "$repo" || exit 1
        if [ -f "$g/config" ] && [ ! -L "$g/config" ]; then
          git config --file "$g/config" core.sharedRepository group || exit 1
        fi
      done
    ' sh {} + || fail "users isolation could not configure the git repositories in $s"
  fi
}

ro_share() {
  s=$1
  set --
  for id in $ids; do
    [ $# -eq 0 ] || set -- "$@" -o
    set -- "$@" -uid "$id"
  done
  if [ $# -gt 0 ]; then
    find -P "$s" -xdev \( "$@" \) -exec chown -h 0 {} + || fail "users isolation could not change the owner of the members' files in $s"
  fi
  set -- -group "$group"
  for id in $ids; do set -- "$@" -o -gid "$id"; done
  find -P "$s" -xdev \( "$@" \) -exec chgrp -h 0 {} + || fail "users isolation could not change the group of the members' files in $s"
  find -P "$s" -xdev ! -type l -exec setfacl -P -b {} + || fail "users isolation could not remove the ACLs in $s"
  find -P "$s" -xdev ! -type l -exec chmod o-w,o+rX {} + || fail "users isolation could not change the modes of the files in $s"
}

shares() {
  while [ $# -gt 0 ]; do
    case $1 in
      user) shift 4 ;;
      share)
        if [ "$2" = rw ]; then rw_share "$3"; else ro_share "$3"; fi
        shift 3 ;;
    esac
  done
}

check_paths "$@"
if [ "$newgroup" = 1 ]; then
  groupadd -g "$gid" "$group" || fail "users isolation could not create the group $group"
fi
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
  unreadable=$(find -P "$2" -xdev ! -type l \( ! -readable -o -type d ! -executable \) -printf '%p\n' -quit 2>&1) \
    || fail "the member cannot read all of the shared directory $2: $(printf '%s' "$unreadable" | head -n 1)"
  [ -z "$unreadable" ] || fail "the member cannot read $unreadable in the shared directory $2"
  if [ "$1" = rw ]; then
    w=$(find -P "$2" -xdev ! -type l ! -writable -printf '%p\n' -quit 2>&1) \
      || fail "the member cannot read all of the shared directory $2: $(printf '%s' "$w" | head -n 1)"
    [ -z "$w" ] || fail "the member cannot write $w in the read-write shared directory $2"
  else
    w=$(find -P "$2" -xdev -writable -print -quit 2>&1) \
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
if mv -- "$home" "$home.swarm-probe" 2>/dev/null; then
  mv -- "$home.swarm-probe" "$home"
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
# homes and shares, and checks that the boundaries hold. Returns each member's
# identity, by member name.
async def setup_isolation(
    isolation: UserIsolation, names: Sequence[str]
) -> dict[str, MemberIdentity]:
    env, env_name = _environment(isolation.sandbox)
    if _is_local(env):
        raise RuntimeError(
            "users isolation needs a sandbox that can run commands as other users; 'local' runs everything as the host user."
        )
    access = await resolve_root_access(env)
    if access.state != "usable":
        raise RuntimeError(
            f"users isolation needs root in sandbox {env_name!r} to create users; its root access is {access.state} ({access.reason})."
        )
    records = _records()
    record = records.get(env_name, {})
    recorded_gid = cast(int | None, record.get("gid"))
    recorded_users = cast(dict[str, int], record.get("users", {}))
    users_ = [(name, member_user(name), member_home(isolation, name)) for name in names]
    parent = posixpath.dirname(isolation.home)

    roots = [
        "/",
        *(share.path for share in isolation.shared),
        parent,
        "/tmp",
        "/var/tmp",
        "/dev/shm",
        "/run/lock",
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
        *dict.fromkeys(roots),
        "--",
        *(item for share in isolation.shared for item in (share.access, share.path)),
    )
    taken: set[int] = set(recorded_users.values())
    if recorded_gid is not None:
        taken.add(recorded_gid)
    existing_gid: int | None = None
    existing_users: set[str] = set()
    lines = probe.stdout.splitlines()
    if lines[:1] != ["begin"] or lines[-1:] != ["end"]:
        raise RuntimeError(
            f"users isolation: the id scan's output in sandbox {env_name!r} came back incomplete (the provider may limit command output), so it cannot choose ids that own nothing."
        )
    for line in lines[1:-1]:
        kind, _, value = line.partition(" ")
        if kind == "id" and value.isdigit():
            taken.add(int(value))
        elif kind == "groupname" and value.isdigit():
            existing_gid = int(value)
        elif kind == "username":
            existing_users.add(value)
    if existing_gid is not None and existing_gid != recorded_gid:
        raise RuntimeError(
            f"users isolation: the group {_GROUP} already exists in sandbox {env_name!r}, and no swarm in this sample made it."
        )
    for _, user, _ in users_:
        if user in existing_users:
            raise RuntimeError(
                f"users isolation: the user {user} already exists in sandbox {env_name!r}. Member names must be unique across the swarm runs in a sample, and the image must not have users named swarm-<member>."
            )
    free = (i for i in range(_FIRST_ID, _LAST_ID + 1) if i not in taken)
    gid = existing_gid if existing_gid is not None else next(free, None)
    uids = [next(free, None) for _ in users_]
    if gid is None or None in uids:
        raise RuntimeError(
            f"users isolation: sandbox {env_name!r} has too few free user ids between {_FIRST_ID} and {_LAST_ID} for {len(users_)} members."
        )
    identities = {
        name: MemberIdentity(user, cast(int, uid), home, isolation.sandbox)
        for (name, user, home), uid in zip(users_, uids, strict=True)
    }

    member_ids = [*recorded_users.values(), *(i.uid for i in identities.values())]
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
        "1" if existing_gid is None else "0",
        isolation.peers,
        parent,
        " ".join(str(i) for i in member_ids),
        *items,
    )
    records[env_name] = {
        "gid": gid,
        "users": {
            **recorded_users,
            **{i.user: i.uid for i in identities.values()},
        },
    }
    store().set(_RECORD_KEY, records)

    await _self_test(env, env_name, isolation, list(identities.values()))
    return identities


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
        with anyio.CancelScope(shield=True), suppress(Exception):
            await _remove_probes(env, env_name, reserved)
        raise
    with anyio.CancelScope(shield=True):
        await _remove_probes(env, env_name, reserved)


# removes the probe directories the self-test reserved, and nothing else
async def _remove_probes(
    env: SandboxEnvironment, env_name: str, reserved: dict[str, dict[str, str]]
) -> None:
    dirs = [d for by_parent in reserved.values() for d in by_parent.values()]
    if dirs:
        await _run(env, env_name, 'rm -rf -- "$@"', *dirs)


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
    env: SandboxEnvironment, env_name: str, script: str, *args: str
) -> ExecResult[str]:
    result = await privileged_shell(env, script, *args, user="root")
    if not result.success:
        raise RuntimeError(
            f"users isolation could not set up sandbox {env_name!r}: {_message(result)}"
        )
    return result


def _message(result: ExecResult[str]) -> str:
    text = (result.stderr or result.stdout).strip()
    if result.returncode == _REFUSED:
        # the script's own refusal is its last line
        return text.splitlines()[-1] if text else "refused"
    return text or f"exit status {result.returncode}"


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


def _records() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = store().get(_RECORD_KEY) or {}
    return dict(records)
