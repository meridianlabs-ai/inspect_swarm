# Inspect Swarm: per-member users in one sandbox

## Overview

Several swarm members can share one sample sandbox and still be kept apart, the way SCHEME runs its agents: each member gets its own Linux user. With `swarm(isolation=users())` the swarm creates one non-root user per member when the sample starts, gives each a private home directory that is also its scratch directory, and prepares the shared directories the eval author lists, each read-write for all members or read-only. It then runs commands as the members to check that every boundary it promises holds, and refuses to start if one does not. Every sandbox command a member causes, from its built-in tools, its custom tools' raw sandbox calls and a bridged Claude Code or Codex CLI, runs as that member's user, because the swarm runs each member inside inspect_ai's sandbox identity scope. Inside the scope a bridge's local model proxy serves only connections whose socket the member's user owns, which it reads from the kernel's socket table, and holds every loopback address the sandbox has at its port, so members cannot use each other's bridges or stand in for them. Members are told their user and directories in their preamble. When a member ends, the swarm kills the processes it finds still running under its user, in a few bounded rounds, and records what it found; at the end of the sample it records who owns the files in the shared and world-writable directories.

This buys a default boundary and attribution, not containment. A member cannot read or change another member's private directory unless the eval author allows reading, cannot write read-only inputs, cannot signal or read the environment of a peer's processes, and leaves its uid on every file it creates, even from a background process. It can still see peers' command lines, share files through `/tmp`, connect to peers' sockets on localhost, and, because all members share one kernel and one sandbox root, anything that gets it root gets it everything. It needs no extra container privileges. Docker is supported; Kubernetes has the limitations described under [Compatibility](#compatibility). Stronger separation (hidden processes, private `/tmp`, per-member resource limits) needs container flags that weaken the sandbox's own boundary, so it is surveyed here and left as later, optional work; a sandbox per member remains the choice when an arm needs members truly apart.

## Where it lives

The swarm's side is in [`_isolation.py`](../src/inspect_swarm/_isolation.py): `Share`, `UserIsolation`, `users()` and the construction checks, setup (its probe, setup and self-test scripts), the preamble paragraph, reaping and the census. [`_swarm.py`](../src/inspect_swarm/_swarm.py) resolves `swarm(isolation=)`, runs setup before the controller and derives the time cap again after it; [`_member.py`](../src/inspect_swarm/_member.py) enters each member's identity scope beside its tool-state scope and reaps the member before its end is recorded; [`_controller/_control.py`](../src/inspect_swarm/_controller/_control.py) emits the `isolation` and `census` events and computes `isolation_incomplete` and `reap_complete`; [`_ledger.py`](../src/inspect_swarm/_ledger.py) and [`_metrics.py`](../src/inspect_swarm/_metrics.py) count `foreign_execs`; [`_record.py`](../src/inspect_swarm/_record.py) holds `MemberReap` and the record's fields.

inspect_ai owns the identity scope (`inspect_ai/util/_sandbox/identity.py`), the sample proxy's defaults for raw sandbox calls (`inspect_ai/util/_sandbox/events.py`), the tools' resolution and refusals, and the bridge's peer check (`inspect_ai/agent/_bridge/sandbox/bridge.py` and `service.py`, and `_agent_bridge/proxy.py` and `peer_uid.py` in the sandbox tools). Its contract is written in inspect_ai's [sandbox trust model, Sandbox identity scopes](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-trust-model.md#sandbox-identity-scopes) and [sandbox-identity-default.md](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-identity-default.md). inspect_swe's side is `_util/identity.py` (resolving an agent's user, cwd and home through the scope, and the refusals), `_util/shared_install.py` and `_util/agentbinary.py` (verified installs inside a scope) and `_util/mcp_ready.py`.

The user docs are [Isolation](../docs/isolation.qmd), which owns what each member gets, the preamble's text, sharing and permissions, the bridged members' requirements, the record's and the evidence events' fields and the image requirements, and [Sandboxes](../docs/sandboxes.qmd), which places users isolation between the shared sandbox and a sandbox per member. The evidence events' format is [swarm-event-types.md](swarm-event-types.md). The tests are `tests/test_isolation.py` (no Docker), `tests/test_isolation_docker.py` and `tests/test_isolation_bridged.py` (Docker, marked slow), with their images in `tests/isolation/` and `tests/isolation_bridged/`.

## Why

swarm.md describes this topology as "one shared sandbox with a Linux user and write scope per member, as SCHEME does" ([Sandbox topology](swarm.md#sandbox-topology)). Three things make it worth having:

- **The shared default has only conventions.** Per-member scratch directories and worktrees are a prompt convention the swarm can set up but not enforce. A member that writes into another's worktree, or a broken build that a peer's tests pick up, changes what an experiment measures, and per-member artifacts for shared-artifact tasks say "what was in that member's place", not "what that member did alone" ([swarm-scoring.md](swarm-scoring.md#shared-artifact-tasks-and-per-member-artifacts)).
- **The filesystem channel is observed only through tool calls.** Processes a member starts can read and write between logged calls and after it ends, and the swarm's drain cannot stop them ([swarm.md Members](swarm.md#members), [swarm-communication.md](swarm-communication.md#the-filesystem-channel)). With one uid for everyone, nothing in the sandbox says who wrote a file.
- **The containment questions need intermediate rungs.** swarm.md's eval questions include "Do agents build channels we did not give them?", with containment probes over shared caches, directories and network ([Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply)). SCHEME's design (2–4 agents in one shared Docker sandbox, "each agent runs as its own Linux user", with "per-agent file read/write permissions ... enforced at the sandbox layer"; [arXiv 2605.29178](https://arxiv.org/abs/2605.29178), Appendix B) is the closest published relative of ours. A sandbox per member is the other end ([swarm-lean-open-problems.md](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field)).

## Goals and non-goals

Goals:

- One non-root user per member in one shared sandbox, private home directories, read-write and read-only shared directories, peers' read access as a setting, with every model-facing sandbox command a member causes running as its user, bridged CLIs included.
- No extra container privileges, so it runs on the sandboxes tasks already use.
- Bounded cleanup of the processes reaping finds when a member ends, with observed survivors, timeouts and failures recorded.
- Evidence the shared default cannot produce: who owns what, and what each member left running.
- Configuration that follows swarm-api.md's logging rule, so arms that differ in it are distinct in an eval set.
- An honest statement of what it does not guarantee.

Non-goals:

- **Containment.** Members share a kernel and a sandbox root. This is not a boundary against a member that gets root ([Security](#security)).
- **Hidden processes, private `/tmp`, private network, per-member CPU and memory limits.** Each needs container flags or kernel features the default sandboxes lack ([Later rungs](#later-rungs-surveyed-not-designed)).
- **A sandbox per member**, or private sandboxes with a shared volume. Those are the alternatives users isolation is compared with ([Sandboxes, Per member](../docs/sandboxes.qmd#per-member)); the two topologies are not combined.
- **Per-member isolation in the `local` sandbox**, which runs as the host user and cannot switch users.
- **Coordinator-only read access.** Coordinator topologies are later work in swarm.md; how they would get read access is stated, not built ([Peers' access](#peers-access-to-private-directories)).

## Survey: Linux isolation inside one sandbox

What each Linux feature isolates, what it needs, and whether it works in a default Docker container (as root, no added capabilities, Docker's default seccomp profile) and on inspect_k8s_sandbox's default chart (gVisor, root container, empty `securityContext`). *(spike)* marks results run in throwaway containers on Docker Desktop (linuxkit kernel 6.10, cgroup v2); gVisor entries are from its syscall table and documentation unless marked.

| Feature | Isolates | Needs | Default Docker | Kubernetes (gVisor) | Cost and notes |
|---|---|---|---|---|---|
| **Users and groups** | File access by owner, group and mode; signals; `/proc/<pid>/environ`, `cwd`, `fd` | Root in the sandbox with `setuid`/`setgid`; `useradd`; a provider that execs as a user | Works *(spike)* | Works: `runuser` from a root container that keeps `CAP_SETGID`; gVisor supports `setuid`, `setgid`, `setgroups` | Free. Process list and command lines stay visible; `/tmp`, abstract sockets and localhost stay shared. Members lose root. |
| **Modes and setgid directories** | Private homes (`0700`), group-shared directories | Nothing beyond users | Works *(spike)* | Works | Free. Without a default ACL, peers cannot edit each other's new files (umask `022`) *(spike)*. |
| **POSIX ACLs** | Shared read-write directories whose new files stay group-writable whatever the creator's umask | `setfacl` (package `acl`), a filesystem with ACLs | Works on the overlay root and named volumes; not on macOS bind mounts, which do not enforce Unix permissions at all *(spike)* | gVisor implements ACLs in its VFS and on tmpfs, but its gofer filesystem refuses ACL setters, so a share works or not by the backend under it | An image dependency. The owner of a file can change its ACL. |
| **Mount namespace** | A per-command view of the filesystem: private `/tmp`, read-only binds | `CAP_SYS_ADMIN`, or an unprivileged user namespace | Refused; works with `--cap-add SYS_ADMIN` *(spike)* | Supported, partly | Per command, not per member. |
| **User namespace** | Lets an unprivileged process create the other namespaces | Docker's seccomp allows it only with `CAP_SYS_ADMIN` | Fails; works with `seccomp=unconfined` *(spike)* | Supported, partly | Turning off seccomp exposes the whole kernel syscall surface to untrusted code. |
| **bubblewrap** | Per-command PID, mount, IPC and network namespaces; private `/tmp` | `bwrap` in the image; an unprivileged user namespace | Works only with `seccomp=unconfined` and `systempaths=unconfined` *(spike)* | Likely; untested | As above, plus every member command wrapped. |
| **PID namespace / `hidepid`** | Peers' processes and command lines | `CAP_SYS_ADMIN` to remount `/proc` | Works with `--cap-add SYS_ADMIN` *(spike)* | `procMount` unsupported in sandboxed pods | `SYS_ADMIN` is the broadest capability; granting it to a container that runs untrusted code as root weakens the sandbox's own boundary. |
| **Landlock** | Unprivileged per-process file scoping; later ABIs TCP, abstract sockets and signals | A host kernel with Landlock enabled | Absent from Docker Desktop's kernel *(spike)*; should work on a Linux host that enables it | Absent from gVisor's syscall table | No flags where it exists, but unavailable on the providers most evals use. |
| **seccomp** (own filters) | Syscalls a process may make | `PR_SET_NO_NEW_PRIVS` | Works *(spike)* | Filter mode only | Only a companion to the above. |
| **cgroups v2** | Per-member memory, CPU and process limits; atomic kill | A writable cgroup filesystem | Needs `writable-cgroups=true` or `SYS_ADMIN`; must not be delegated to the member *(spike)* | gVisor does not enforce in-sandbox limits | Docker only. |
| **rlimits** | Process count per uid | Nothing | `RLIMIT_NPROC` counts the uid across every container on the host, since Docker uses no user namespace *(spike)* | Untested | Unsafe for per-member caps when parallel samples reuse uids. |
| **Network namespace / per-uid rules** | A member's network, loopback included | `SYS_ADMIN` (namespace); `NET_ADMIN` and `iptables` (owner match) | Owner match works with `NET_ADMIN` *(spike, egress)* | `cap_add` not convertible from compose | Owner matches cover localhost TCP, not Unix sockets. |

What follows: users, modes and ACLs work with no extra privilege on both providers that matter, so they are the rung users isolation builds. Everything that hides processes, privatises `/tmp` or the network, or limits resources needs a container flag (`SYS_ADMIN`, `seccomp=unconfined`, `writable-cgroups`, `NET_ADMIN`), which widens what untrusted code running as the sandbox's root can do against the host kernel and which Kubernetes cannot take from a compose file, or a kernel feature that neither Docker Desktop nor gVisor has (Landlock).

## At a glance

```python
from inspect_swarm import Share, member, swarm, users

swarm(
    members=member(worker, count=4),
    channels=["filesystem"],
    isolation=users(
        shared=[Share(path="/shared", access="rw"), Share(path="/data", access="ro")],
        peers="none",
    ),
)
```

When the swarm runs, before any member starts, it:

1. checks that the sandbox can run commands as other users, that the image has what setup needs, and that the configured paths and their contents are a layout it supports;
2. creates one user per member (`swarm-w-1`, ...) with ids that own no existing file, and its home at `/scratch/w-1`, and prepares each `rw` share;
3. runs a self-test as the members, and refuses to start if any boundary it promises does not hold.

Each member then runs inside `sandbox_identity(user, cwd=home, sandbox=...)`, so its sandbox commands run as its user in the shared sandbox. Its preamble states its user and directories. When the member ends, the swarm kills what it finds still running under its uid. Once every member has ended, it records who owns what in the shared and world-writable directories.

## Configuration

`Share(*, path, access)` and `UserIsolation(kind="users", sandbox="default", home="/scratch/{member}", shared=(Share(path="/shared", access="rw"),), peers="none")` are frozen pydantic models with no extra fields and keyword-only constructors; `users(*, sandbox, home, shared, peers)` builds one, taking shares as `Share` or as mappings, and checks it. `swarm(isolation=)` takes `None` (the default: the shared sandbox with conventions only), `"users"` (`users()` with its defaults, so a task can expose `-T isolation=users`), a `UserIsolation`, or its fields as a mapping, which is what replay passes back. [Isolation](../docs/isolation.qmd) documents the fields.

- **A pydantic model and no registry type.** No user needs to extend how isolation works, and the rungs above it change what a member can do in ways a third-party implementation could get wrong silently. A frozen model is logged as its fields and rebuilt from them, so arms that differ in any field are distinct in an eval set ([Arms and identity](#arms-and-identity)). A later rung adds a `kind` and its own model.
- **Defaults line up with the filesystem channel.** `filesystem(shared="/shared", notes="notes.md", scratch="/scratch/{member}")` names the same paths, so `channels=["filesystem"]` and `isolation="users"` agree with no arguments.

What is refused at construction, with `ValueError` naming the argument:

- **Paths that are not plain.** A relative or non-normalised `home` or share path (`..`, `.`, a repeated or trailing `/`), or one with a backslash or a character that is not printable: setup's scripts pass paths to `awk -v`, which reads a backslash as an escape, and read paths back one per line. A `home` without `{member}` exactly once, in its last component, or with other braces.
- **Overlapping paths.** Two shares that are the same or nested; a share that is the homes' parent, inside it or above it (setup would change the homes' modes, or make their parent group-writable); a share that is, is inside or is above `/tmp`, `/var/tmp`, `/dev/shm` or `/run/lock`, which every user writes, the census scans, and `rw` preparation would regroup and strip of the sticky bit.
- **Member names that are not user-name suffixes.** After count expansion each name must match `[a-z][a-z0-9-]{0,25}`, so `swarm-<name>` is a portable Linux user name of at most 32 characters. Member names are the eval author's identifiers, so this constrains only the author.
- **A filesystem channel that disagrees.** A `scratch` other than `home`, a `shared` that is not a `rw` share, or a notes file outside every `rw` share: the preamble would describe directories and a file the member cannot use.
- **Members with their own sandboxes** (`member(sandbox=...)`): the two topologies are not combined until someone needs both.
- **Members that choose their own names** (`choose_name=True`, [swarm-chosen-names.md](swarm-chosen-names.md)): the user name and home show the name the swarm gave the member, which such a member must not be told.

## What the runtime does at swarm start

Setup runs in the host process when the swarm's agent runs, before the controller starts, so it finishes before any member exists. Every command runs as root through inspect_ai's `privileged_shell()`, as fixed scripts that take every path and name as an argument. The scripts, with their exact commands, id range and bounds, are in [`_isolation.py`](../src/inspect_swarm/_isolation.py), and `tests/test_isolation.py` and `tests/test_isolation_docker.py` exercise them. Setup's commands are ordinary `SandboxEvent`s outside any member span.

**What it guarantees before any member starts.** Either the swarm stops with a `RuntimeError` naming the check and the path, or:

- each member has its own non-root user, `swarm-<member>`, in the group `inspect-swarm`, with a uid that no running process holds and that owns no file on the filesystems setup scans;
- each member has a home, made by root, that peers cannot write, rename or replace, and with `peers="none"` cannot read;
- each `rw` share is open to every member for reading and writing, files created later included, through the `inspect-swarm` group, setgid directories and a default ACL; when `git` is installed, git trusts the share and the repositories near its top (repositories members create later need their own `safe.directory` entry, which the preamble mentions);
- each `ro` share is unchanged, and readable but not writable by every member;
- the roots the census will scan, with each member's user, uid and home, are in the `isolation` evidence event.

[Security](#security) states these boundaries and their limits.

**What it refuses**, each with a message naming the path, entry or missing package ([Isolation, Requirements and limits](../docs/isolation.qmd#requirements-and-limits) is the user's list):

- a sandbox it cannot use: one that is not among the sample's environments, `local` (which runs everything as the host user), one without usable root access, or an image without `useradd`, `groupadd`, `awk` and GNU `find`, or without `setfacl` and `getfacl` when there are shares;
- ACLs setup did not write, on the homes' parent or in a share, or on the nearest existing ancestor of one it will create;
- a mount inside a share or under the homes' parent, since setup and the self-test stay on one filesystem;
- in a `rw` share, hard-linked files, devices, sockets, FIFOs, set-id files and directories without owner `rwx`, since setup opens entries to members and its changes reach every name of an inode; in a `ro` share, an entry members could write or could not read;
- unsafe paths: a directory above the homes or a share that is not a real directory owned by root and unwritable by group and other, a share that is not a real directory, and a home that already exists;
- names setup would create that already exist: an `inspect-swarm` group, a `swarm-<member>` user or group, or a group that already lists a `swarm-<member>` user;
- a second swarm with users isolation on the same sandbox in a sample, whether it runs after the first, beside it, in a subtask or in a fork, and a swarm with users isolation inside a member's identity scope, since scopes do not nest (a nested swarm without isolation keeps the scope it runs in).

**The self-test.** Setup then runs commands as each member, and as one pair of members, through the provider's own `exec(user=...)`, and checks the guarantees from the members' side: the member runs as its uid and not as root, `sudo` does not make it root, it can read every share and write every `rw` share but no `ro` share, a peer cannot write or rename its home (nor read it with `peers="none"`), and peers can change each other's files in a `rw` share. The self-test is the guard against what setup neither supports nor detects: a filesystem that does not enforce Unix permissions (a macOS bind mount), a provider that ignores `user`, a filesystem without ACLs, sudoers that let a member become root. Its probe files are removed afterwards, whether it passed or failed.

**Why it works this way:**

- **Ids that own nothing.** Images own files by numeric id (`useradd`'s first free uid, often 1000, is a common one), which a member given that id would own, and reaping a member would kill a task's service running under its uid.
- **Root-owned ancestors.** A private directory is only as private as the path that reaches it: a peer that can write a home's parent can rename the home away and put its own in its place *(reproduced in Docker, without root)*. Shares under `/tmp` are refused for the same reason.
- **No `/etc/skel`.** Its entries keep their modes and could let peers write a member's home under `peers="read"`.
- **Not `ensure_framework_directory()`.** inspect_ai's helper makes directories owned by the user it runs as, never group-writable and never set-id, while `rw` shares are both by design and homes are made by root for another uid.
- **One setup per sandbox.** Setup changes shares only while no member of its sandbox exists, which closes the window in which a member could swap an entry for a symlink ([Security, Untrusted input](#security)); a second setup would change shares the first swarm's members, or their leftover processes, can change while it runs.
- **Refuse rather than reconcile.** Setup supports plain directories under root-owned parents and the image's ordinary environment, refuses what it can detect cheaply, and reconciles no other permission state: a layout it neither supports nor detects is a candidate for a new refusal. The image and sandbox configuration are trusted ([Security](#security)).

## Members' commands run as their users

Tools of a built agent, or passed in a member's arguments, are shared objects, and library code (deepagent's subagents, inspect_swe's install steps) builds others with no user from the eval author, so the user has to reach tools at call time, and a constructor argument could not enforce it anyway. The swarm therefore runs each member inside inspect_ai's `sandbox_identity(user, cwd=home, sandbox=...)` scope, entered by the member wrapper beside the member's tool-state scope, as a small, general inspect_ai feature rather than a swarm-only wrapper (decision: Ransom, 2026-10-13). inspect_ai's [Sandbox identity scopes](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-trust-model.md#sandbox-identity-scopes) and [sandbox-identity-default.md](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-identity-default.md) are that contract; what the swarm relies on:

- **The scope's sandbox is the default.** Tools that find their sandbox by injection use the member's; a tool given another sandbox by name keeps that sandbox and its user, so the eval author's trusted services are unaffected.
- **Model-facing tools run as the member and refuse another user.** The built-in tools ([Isolation, What each member gets](../docs/isolation.qmd#what-each-member-gets) lists them) run as the member and start in its home; one configured with another user, or a shell shared between members, raises rather than run, and tools that cannot run as a user raise inside the scope.
- **Raw sandbox calls default to the member.** A custom tool's or scaffold's `exec()`, `read_file()` and `write_file()` that name no user run as the member, in its home; one that names another user runs as that user, a grant made in code that the scope does not refuse; calls on the provider object (`sandbox().as_type(...)`) skip the scope. Inspect's own operations keep the sandbox's default user. File calls need a provider whose file methods take a user ([Compatibility](#compatibility)).
- **Inspect's own launches keep an explicit user**, which lets the bridge run its model proxy as the tools user while the CLI runs as the member.

**`foreign_execs`.** The swarm reports what it can see of grants made in code: once a member ends, it counts the `SandboxEvent` execs in the member's spans that ran as a user other than the member's, excluding Inspect's own operations. It is a harness-validity diagnostic in the member's record, not a guarantee: it counts execs only, not file calls nor calls on the provider object, and a `SandboxEvent` does not name its environment, so execs that name a user in another of the sample's sandboxes are counted too.

### Bridged members

Every member shares the sandbox's loopback, where outside a scope any process can call any bridge's model routes (spending in that member's name) and its bridged-tools endpoint. A bridge started inside a scope therefore serves only the scope's user. inspect_ai's [Bridge peer check](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-trust-model.md#sandbox-identity-scopes) owns the mechanism and its limits; what the swarm relies on:

- **Only the member's uid is served.** The proxy tells its member from everyone else by the uid that owns the client's end of each connection, which the kernel records and no unprivileged process can forge, and answers every other connection, on every route, with HTTP 403 (decision: Ransom, 2026-11-09, over a per-bridge token; [Alternatives](#alternatives-considered)). It is told the member's user in its launch environment, so nothing secret exists to leak into a command line, the eval log or a file.
- **The proxy holds every loopback address the sandbox has.** Clients keep addressing it as `localhost`, so it listens on `[::1]` as well as `127.0.0.1` at its port wherever the sandbox has an IPv6 loopback, and a member cannot listen on the other address and receive a peer's CLI's requests (decision: Ransom, 2026-11-09, over moving clients to `127.0.0.1`). A sandbox without one is served on `127.0.0.1` alone.
- **A bridge yields only once its proxy listens.** Bridge ports are predictable, so a member may already hold a peer's next port; the peer's bridge then fails to start rather than send its CLI's requests to that member.
- **No sandbox-tools version check, and normal concurrency limits** for the bridge's tools detection and installation (decision: Ransom, 2026-11-11).
- **inspect_swe resolves the user once and refuses what it cannot support.** `claude_code()` and `codex_cli()` resolve their user, cwd and `HOME` through the scope at the start of their invocation, so their config lands in the member's home and the CLI starts there; their MCP readiness probe runs as the member, so it is accepted. Inside a scope they install the CLI as a verified copy, provisioned as the sandbox's default user into a directory no other user can write, because a binary a member could place or change would run as each peer that uses it; `version="sandbox"` is refused and `"auto"` installs the stable release. `centaur` mode, which connects as the sandbox's default user through Inspect's human agent, raises inside a scope, as do inspect_swe's other agents and its ACP adapters, each until it resolves its user through the scope. A custom scaffold whose client runs as the member works unchanged at `localhost`; one whose client runs as another user gets 403 on its first request.
- **One bridged agent type per swarm.** inspect_swe gives each agent type its own port counter from the same start, so the first Claude Code member and the first Codex member in one sandbox both take port 3001. The collision exists in the shared default too and is inspect_swe's to fix ([Not this design](#not-this-design)); [Isolation, Bridged members](../docs/isolation.qmd#bridged-members) documents the restriction. The swarm cannot enforce it, since a member's factory may be a builder.

## Working directories

- **One private directory per member, which is its `$HOME`.** `/scratch/<member>` by default. Built-in tools and the supported bridged CLIs set `$HOME` to it, so the per-user caches and configuration they use (`pip --user`, `~/.cache`, `~/.claude`, Codex's `.codex` under the working directory) are private without further setup, and shared caches cannot form between members by accident. A custom tool's raw `exec()` takes the member's user and home as its working directory but keeps the provider's environment unless the caller overrides it, so where the image or compose file sets `HOME` (to `/root`, say) a raw command sees that value, and home-based caches or installs it makes can fail. It starts with only `~/tmp` and a `~/.profile` and `~/.bashrc` that set `TMPDIR` to it, so tools that honour `TMPDIR` do not collide in `/tmp`.
- **Handed to the member three ways.** The scope's `cwd` makes the tools, raw `exec()` calls and bridged CLIs start there; `$HOME` points there for built-in tools and bridged CLIs; and the preamble names it.
- **Per-member git worktrees** are a task or prompt convention on top: a member runs `git worktree add ~/repo -b swarm/<member>` against a repository in a `rw` share. Its working tree is private; its branch is not.
- **A task that needs members to start somewhere else** (a SWE task whose repository is `/testbed`) shares that path as `rw` and says so in the task prompt; the private directory stays the scratch space.

## Shared directories

| What | How | Notes |
|---|---|---|
| A shared working area or repository | `Share(path=..., access="rw")` | Every member can create, change and delete everything in it, peers' files included: the default ACL keeps new files group-writable whatever a member's umask. A shared repository's refs are shared too, so any member can move any branch. |
| Read-only task inputs | `Share(path=..., access="ro")` | Members read; nobody but root writes. The image sets its modes. |
| The filesystem channel's directory and notes file | the `rw` share named by `filesystem(shared=...)` | Unchanged convention; the notes file is append-only by convention only. |
| Everything else | Unchanged | System directories stay root-owned. World-writable directories (`/tmp`, `/var/tmp`, `/dev/shm`, `/run/lock`) stay world-writable, and are the main unsanctioned file channel ([Security](#security)). |

Setup sets permissions once and does not enforce them afterwards: a member can still make a file or directory in a share, or in its home under `peers="read"`, that others cannot read, with a mode given at creation or a later `chmod` ([Isolation, Permissions](../docs/isolation.qmd#permissions)).

### Peers' access to private directories

- `peers="none"` (default): only the member and root.
- `peers="read"`: every member may read every other member's private directory, not write it. It is enforced by group mode and setgid, so a member can still make its own files private (`chmod`, `umask 077`); a member's own files are its to share or withhold.
- **A coordinator only** is not offered: coordinator topologies are later work (swarm.md, [Implementation plan](swarm.md#implementation-plan)). When they exist, a coordinator's read access is an ACL entry `u:swarm-<lead>:rX` with a default ACL on each private directory, which the same setup step can add, as a third value, `peers=["lead"]`. Like `"read"`, the owner could remove it.

## The preamble

Each member's preamble ends with a fixed paragraph whose only variables are the member's user name, its home and the shares' paths and modes, all from the eval author's configuration ([Isolation, What each member gets](../docs/isolation.qmd#what-each-member-gets) quotes it). It says whether peers can read the home, leaves out the shares sentence without shares and the git sentence without a `rw` share, and ends with "Processes you leave running are stopped when you finish." Members are not told what is not isolated; the eval author decides whether a prompt mentions it, because telling them is itself a treatment.

## Processes a member leaves running

- **When a member ends**, for any reason (submitted, stopped, its own limit, cancelled or errored), the swarm kills the processes in the sandbox whose real uid is the member's, as root, in a few bounded rounds, and records what it found as the member's `reap` (decision: Ransom, 2026-10-13, over reaping only at the drain). Scanning by uid stays inside the sandbox's PID namespace, so it never touches another sample's processes. It runs inside the member's span and before the member's end is recorded, so the member's `member_result` event, its end and its time come after its processes are gone. Its exec is one of Inspect's own operations, so it is not a foreign exec.
- **Failure contract.** Reaping is bounded, never raises and never replaces the member's own outcome. Any `reap` status but `ok`, or a member that started but was never reaped, sets the record's `isolation_incomplete` and makes the `reap_complete` harness-validity check `false`. The sample is not failed and the task's scorer still runs, because a survivor is a measurement problem, not a broken run; analyses of artifact or containment results exclude or report such samples. [Isolation, The record](../docs/isolation.qmd#the-record) lists the statuses and fields.
- **Best effort.** Reaping is not atomic, so it can record `ok` while a member's process still runs ([Security](#security) states the limit).
- **Why kill rather than wait.** swarm.md's ownership boundary says the swarm owns everything a member starts; without a uid per member it could not reach sandbox processes. A member that ended has no further turns, so nothing it started should keep changing the shared state or using the sandbox's CPU. An eval that wants a long-running shared service starts it from the task, as root or as its own user. If a task ever needs a member-started service to outlive its member, a `reap="drain"` field on `users()` can be added then.
- **The drain.** Members still running when the swarm stops are cancelled by the stop procedure ([swarm-limits.md](swarm-limits.md#stopping)), and each is reaped as it ends, with the same contract, before the census. A member whose task was cancelled before it began ran nothing; its missing reap is reported as `isolation_incomplete` rather than reaped.
- **Bridged members.** The CLI and everything it started run as the member and are reaped with it. The bridge's model proxy runs as the tools user and is stopped by the bridge itself.

## What the topology makes measurable

The shared default observes the filesystem only through tool calls. Users isolation adds evidence that does not depend on them, as swarm events of its own ([swarm-event-types.md](swarm-event-types.md#swarmevent); [Isolation, Evidence](../docs/isolation.qmd#evidence) owns the fields and their caps):

- **File ownership census**, once every member has ended and been reaped, and only when the swarm ended without an error: for each census root (each `rw` share, `/tmp`, `/var/tmp`, `/dev/shm`, `/run/lock`, and the other world-writable directories on the root filesystem), the entries and bytes each member's uid owns and anyone else owns, with a sample of each member's paths. It runs as root, reads only metadata and is bounded; a census that fails or times out is recorded as such and the swarm ends as usual. A file's owner is its creator, including a background process that created it between tool calls. It is residual ownership evidence: it does not see reads, modifications, deletions, or content passed through a file that no longer exists, and it does not show who placed or read an entry. Members can rename each other's files in a `rw` share, so where a share and `/tmp` are on one filesystem a peer can move a member's shared file into `/tmp`, keeping the member's uid.
- **Unsanctioned file channels, by owner.** The census reports the member-owned entries currently in `/tmp`, `/var/tmp`, `/dev/shm` or `/run/lock`, outside the shares the eval author sanctioned, per member and per root. This is the containment probe swarm.md asks for, for the filesystem part.
- **Private work is attributable to its owner, not proven to be its author.** A peer cannot create, rename or delete entries in a member's private directory, so a per-member artifact read from there is "what that member left". It does not prove that only the member wrote the contents: a member can hard-link its own file into a `rw` share, let a peer change it, then remove the link and make the file `0600` again; the result has one link, the member's uid and no group or other write bit, and contains the peer's write (reproduced in Docker). One link and no group or other write bit show only that no peer can newly open the file for writing by its path: a peer that opened it, or mapped it writable, while it was shared keeps that access, and nothing makes the file immutable.
- **Reaped processes per member** and **`foreign_execs` per member**, above.

What it still cannot see: reads, modifications of existing files, traffic over localhost TCP and abstract Unix sockets, and process command lines read by peers. Experiments that need those add sandbox instrumentation ([Not this design](#not-this-design)) or use per-member sandboxes.

## Arms and identity

- `isolation` is an argument of `swarm()`, so it is in the plan step's `params_passed`, and `task_identifier()` hashes it: two arms that differ only in `isolation`, or in any field of `UserIsolation`, are distinct in an eval set ([swarm-api.md Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)). The string `"users"` and the model `users()` are logged differently but mean the same, as with channel names.
- **Isolation changes privilege, not only isolation.** In the shared default, members run as the sandbox's default user, root in most images. Under users isolation they cannot `apt-get install`, write system directories or `pip install` without `--user`. Comparing `isolation=None` against `"users"` therefore changes two things at once. An experiment whose question is isolation runs its baseline at the same privilege: a swarm of one under `users()` for the single-agent arm, built as `swarm(members=..., controller=leaderless(final="first"), channels=[], isolation=users())` with the arm's result contract when it has one (what `baseline()` builds, but `baseline()` takes no `isolation`), and, for a "members see everything" arm, `users(peers="read")` with the working area as a `rw` share ([Isolation, Comparing arms](../docs/isolation.qmd#comparing-arms)). Tasks that need root during the solve are unsuitable, and the self-test cannot detect that.
- **The record.** Each member's `MemberResult` ([swarm-scoring.md, What the swarm records](swarm-scoring.md#what-the-swarm-records)) has `user`, `uid`, `home`, `reap` and `foreign_execs`, `None` without isolation; `SwarmResultRecord` has `isolation_incomplete` and `SwarmMetrics` has `reap_complete`, both `None` without isolation. The self-test leaves no record of its own, since the swarm runs only once it has passed.

## Scoring

- **The task's scorer** runs after the drain, outside every member scope, as the sandbox's default user. It can read shares and private directories when that user is root, the usual case, or when its sandbox calls explicitly request root; setup needs usable root access, not a root default user, so a scorer on a sandbox whose default user is not root cannot read `0700` homes merely because setup succeeded. Where it can, shared-artifact tasks score the team's environment as without isolation.
- **In-loop scoring** (a member's `react(attempts=...)` calling the task's scorer) is one of Inspect's own operations: its raw `exec()`, `read_file()` and `write_file()` calls that name no user use the sandbox's default user, and it cannot open a scope of its own. Built-in tools a scorer calls there still resolve the member's identity, so such a scorer can behave differently in the loop and at final scoring. A scorer that executes submitted code in the loop does so as the default user while other members still run, and users isolation does not protect peers from it.
- **Per-member artifacts** become attributable. `member_artifacts`'s `materialize()` is task code; under users isolation it can read a member's artifact from that member's private directory (its worktree), whose entries only that member could create, when its sandbox calls run as root (the default user in most images, or named explicitly). It may also check that each file it reads has one link and no group or other write bit, which shows only that no peer can newly open the file for writing by its path: not that no peer still holds write access through a descriptor or writable mapping opened earlier, that the file is immutable, or who wrote it ([What the topology makes measurable](#what-the-topology-makes-measurable)). Branches in a shared repository stay writable by every member, so a `materialize` that wants attribution reads the private worktree's files, not the branch ref.
- **LeanOpenProblems** scored each member's own sandbox. Their members shared nothing on disk, so per-member sandboxes ([recommendation 1](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field)) remain their fit.

## Limits

- **No change to the budget.** Users isolation changes no limit node, cap, share or the ledger ([swarm-limits.md](swarm-limits.md)). Setup, the self-test, reaping and the census make no model calls.
- **Time.** Setup runs before the controller and counts towards the sample's time and working limits. A time cap derived from the sample's time limit is derived again after setup, so the time setup took is not counted on and the cap still ends before the sample's limit, leaving the reserve for the final step; an explicit `Budget.time` is unchanged and is compared with the time the sample has left after setup. Reaping runs inside each member's end and is bounded.
- **No per-member resources.** Members share the sandbox's CPU, memory and process table, and a member that exhausts memory can get a peer's process killed. Per-member limits need cgroups (Docker only, with a container flag); rlimits are not used because `RLIMIT_NPROC` is global per uid across containers. The sandbox is sized for the member count ([Sandboxes, Sizing](../docs/sandboxes.qmd#sizing)).

## Later rungs (surveyed, not designed)

Each is an optional later step that needs a container flag or kernel feature the task would state in its compose or values file; none is designed until an experiment needs it. The [survey](#survey-linux-isolation-inside-one-sandbox) row named after each gives its requirements and trade-offs.

- **Hidden processes**, peers' processes and command lines invisible: the PID namespace / `hidepid` row.
- **Namespaced commands**, a private `/tmp`, PID, IPC and network per command: the bubblewrap row.
- **Per-member resources**, memory, CPU and process limits and atomic reaping: the cgroups v2 row.
- **Per-member network rules**, by uid, on loopback as well as egress: the network namespace / per-uid rules row.
- **Unprivileged file, socket and signal scoping**: the Landlock row.

The cheapest rung that removes most of the remaining channels is Landlock where the host has it; the most portable is a sandbox per member.

## Decisions

- 2026-10-07: the shared sandbox, with conventions only, is the default; users isolation is opt-in (decision: Ransom, 2026-10-07).
- 2026-10-13: the per-member identity comes from an inspect_ai `sandbox_identity()` scope, a small, general inspect_ai feature, not a swarm-only wrapper over inspect_ai's private sandbox state (decision: Ransom, 2026-10-13).
- 2026-10-13: a member's leftover processes are reaped when the member ends, not only at the drain (decision: Ransom, 2026-10-13).
- 2026-11-09: a bridge inside a scope tells its member from other users by the uid that owns each connection's socket, not by a token (decision: Ransom, 2026-11-09).
- 2026-11-09: clients keep addressing the proxy as `localhost`, and the proxy holds `[::1]` as well as `127.0.0.1`, rather than clients moving to `127.0.0.1` (decision: Ransom, 2026-11-09).
- 2026-11-11: a bridge inside a scope checks no sandbox-tools version, and its tools detection and installation keep the sandbox's concurrency limits (decision: Ransom, 2026-11-11).

## Alternatives considered

**A sandbox per member** (Inspect's named sandboxes; `member(sandbox=...)`, [Sandboxes, Per member](../docs/sandboxes.qmd#per-member)).

- Real separation: process tables, `/tmp`, network, resources and root are all per member, enforced by the container runtime.
- N containers per sample, sized for one agent each: 32 copies of a 32 GiB container per sample in LeanOpenProblems' flagship config. No shared files unless the task adds a shared service.
- The right choice when an arm must match independent attempts, for a "no communication" arm, for containment baselines that need a real boundary, and when members need resource limits or root.

**Private sandboxes plus a shared volume** (a compose service per member with one named volume mounted in each).

- Container-level separation with one explicit shared directory, which is what an arm with "files as the only channel" wants.
- Every member is root in its own container, so the shared volume has no write scopes unless each container also runs its agent as a user, and uids across containers must be agreed for ownership to mean anything. On Kubernetes a volume shared across pods needs a `ReadWriteMany` storage class.
- Right for messages-or-files-only arms that need process and network separation; heavier than users isolation.

**Conventions only** (the default).

- Free, and members keep root, which many tasks need.
- No enforcement and no attribution beyond tool calls.
- Right when sharing is the point (the C compiler's and Test-Time Communication's setups) and the experiment does not measure isolation or per-member artifacts.

**Users isolation** is right for shared-artifact tasks with private scratch and per-member work (SWE-style tasks with per-member worktrees), for role-based write scopes (SCHEME), and as a cheap intermediate rung in containment experiments, when members do not need root.

**Redirect members' tools from inspect_swarm alone**, by resetting inspect_ai's private `sandbox_environments_context_var` per member to wrappers that fill in `user` and `cwd`, as LeanOpenProblems reset it to choose sandboxes.

- No inspect_ai change.
- The wrapper sees only `exec()`. Injected tools decide their user from per-object state and run their own launcher as root with the target user in the request, so a wrapper either leaves `bash_session()` and `text_editor()` running as root or breaks them. A wrapper that defaults `exec(user=None)` to the member cannot tell Inspect's own operations from a custom tool's, so it would record whichever member injected first as the sandbox's default user and change scorers' identity inside a member; and a wrapper that is not inspect_ai's proxy makes the root-access probe run again after agents have run.
- Rejected (decision: Ransom, 2026-10-13): the scope is a small, general inspect_ai change at the points where tools already resolve their user; the wrapper depends on private state in several places.

**Make the swarm build per-member tools** (a member builder that takes a user, with `bash(user=...)` per member).

- No inspect_ai change for native tools.
- Built agents' tools are built once by the task, tools passed in a record's arguments are shared by every copy, and a builder's choice does not reach deepagent's subagents' tools or inspect_swe's install steps, nor stop other code from running as another user.
- Rejected for the same reason the tool-state scope was chosen over refusing shared tools (swarm.md, [Alternatives](swarm.md#alternatives-considered)): the common configuration should not need a workaround.

**Apply the identity in each provider's `exec()`** (every exec with no user inside a scope runs as the member, decided inside the provider).

- Also covers custom tools that call `exec()` directly.
- A provider cannot tell Inspect's own commands and scorers, which issue `exec(user=None)` to mean the default user, from a custom tool's. Rejected for the sample's sandbox proxy, where Inspect's own operations mark themselves and `read_file()` and `write_file()` are covered too ([sandbox-identity-default.md](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-identity-default.md)).

**A token per bridge** (the proxy mints a random token, publishes it in a file only the member can read, and requires it on every request).

- Independent of the kernel's socket table, and the same mechanism could later protect bridges outside a scope.
- Much more state for the same separation: minting and a constant-time check in each dialect's key header; a root-owned token directory with ownership and mode checks, exclusive no-follow publication and refusal of a directory created first; the host's bounded wait for the file and its removal after the kill; a wrapper per CLI that reads the file into its environment, plus key-helper and MCP-header configuration per CLI; and care that the token never crosses the host channel, a command line or the eval log. Either way a member can deliberately share its bridge with a peer. Rejected (decision: Ransom, 2026-11-09).

**Clients on `127.0.0.1` instead of the proxy holding `[::1]`.**

- Also keeps a member from standing in for the proxy at `::1`, without a second listener.
- Changes every bridge's client URLs (the MCP configs, inspect_swe's base URLs, the bridge docstring and the custom-scaffold recipe), and a `127.0.0.1` URL goes through an image's HTTP proxy when `no_proxy` lists only `localhost`, so every launch path would need `NO_PROXY`/`no_proxy` merged without losing either spelling's exclusions. Holding both addresses changes nothing for clients. Rejected (decision: Ransom, 2026-11-09).

**A Unix domain socket per bridge** (a socket file only the member can open, or a check of `SO_PEERCRED`).

- File permissions or the kernel's peer credentials would identify the client directly.
- The CLIs take an HTTP base URL and the bridged-tools MCP configs are HTTP URLs; none of them connects to a Unix socket, and a relay from TCP would bring back the loopback listener the check exists for. Rejected.

**Namespaces from the start** (bubblewrap around every member command).

- Closes `/tmp`, the process table and, optionally, the network in one step.
- Needs `seccomp=unconfined` on Docker, which weakens the boundary between untrusted code and the host kernel for every process in the container, and wrapping inside inspect_ai's injected tools. Kept as a later rung.

**Linux users without ACLs**, relying on setgid directories and a shared group.

- No `acl` package in the image.
- New files are created `0644` under the default umask, so peers cannot edit each other's files in a shared repository *(spike)*; forcing a umask on every member process is not possible for bridged CLIs' own shells. Rejected for `rw` shares; `ro` shares and `peers="read"` need no ACLs.

## Compatibility

- **inspect_swarm.** `isolation=None` is the default, so swarms without it are unchanged. `MemberResult`'s isolation fields, `isolation_incomplete` and `reap_complete` default to `None`, so records written without isolation load.
- **Eval logs.** Setup, reaping and the census are ordinary `SandboxEvent`s; the evidence is two `SwarmEvent` kinds, `isolation` and `census` ([swarm-event-types.md](swarm-event-types.md)). No schema or viewer change.
- **inspect_ai.** Outside a scope every resolution returns its argument, injection chooses its sandbox as before, `bash_session()` performs no identity check, and a bridge checks no peer, listens on `127.0.0.1` only and yields as soon as its proxy starts, so existing evals, scaffolds and clients that send dummy keys keep working. Inside a scope the proxy also listens on `[::1]` where the sandbox has an IPv6 loopback.
- **inspect_swe.** Resolving the user through the scope, the verified installs, the readiness probe's user and the refusals apply only inside a scope; agents' URLs and environments are unchanged.
- **Tasks and images.** Users isolation changes the sandbox it runs in for the rest of the sample: new users and groups, group and ACL changes on `rw` shares, `safe.directory` entries in the system git config. Scorers running as root are unaffected; a scorer that runs as a non-root user, the sandbox's default or one it names, cannot read `0700` private homes and may find files in `rw` shares owned by members and writable only by the `inspect-swarm` group. Changing a regular file's group clears its file capabilities, so a file with capabilities in a `rw` share loses them. The image requirements are [Isolation, Requirements and limits](../docs/isolation.qmd#requirements-and-limits): the default Inspect image and `python:3.12-bookworm` lack `setfacl`, so a task with shares installs `acl`.
- **Providers.** Docker is supported. inspect_k8s_sandbox's default chart is supported for users, homes and the self-test; whether shares work depends on whether the filesystem under them keeps ACLs in gVisor, which the self-test checks. `local` is refused. Other providers work if their `exec(user=...)` switches users and their filesystems enforce permissions, which the self-test checks. The self-test runs only execs, so it does not show that a provider's `read_file()` and `write_file()` take a user: where they do not (inspect_k8s_sandbox's take none), every scoped file call raises `SandboxUserUnsupportedError`, including those of custom tools and of bridged CLIs that write their configuration that way (Codex does).

## Security

**Trust boundary.** The task's image and sandbox configuration are trusted, like the task code that runs before the swarm. Users isolation protects members from each other. It does not audit the image for every setting that could give a member authority: accounts, group entries, sudo rules, mounts and services are the task's. Setup keeps the cheap checks and refusals of [What the runtime does at swarm start](#what-the-runtime-does-at-swarm-start); a setting that gives a member authority is the task's bug, unless setup makes it worse or one of those checks misses the case it states.

The guarantees below hold when the image and configuration also meet these preconditions, which setup does not check and `users()` documents:

- **No aliased storage.** The same storage is not mounted at two configured paths: not at two shares, and not at a share and in the homes' tree. A volume mounted read-write at `/shared` and read-only at `/data` would let setup's changes to `/shared` reach `/data`, and members change `/data` through `/shared`.
- **No sudo rules for members.** The self-test refuses an image where a member's `sudo -n true` succeeds; a rule limited to some commands or run-as users is not detected.
- **No read-write storage shared between users-isolated sandboxes.** Setup changes `rw` shares and creates homes before any member of its own sandbox exists; members of another sandbox on the same storage could already be changing them. `ro` inputs may be shared.

**What it guarantees**, for members that do not get root and do not run code a peer wrote:

- A member's private directory is unreadable (with `peers="none"`) and unwritable by peers, and peers cannot rename or replace it, because setup refuses homes under directories members can write. `ro` shares are unwritable by any member, checked per member over every existing entry of the share itself. Preparation and the recursive checks do not descend into a directory symlink's target, so the files below it keep their own permissions: a symlink in a `ro` share does not make them read-only, nor one in a `rw` share group-writable. The `ro` check does refuse a symlink whose own target a member can write.
- A member cannot signal, trace, or read the environment, working directory or open files of a peer's processes *(spike)*.
- Every file a member creates carries its uid; members cannot give files to another uid. Member uids own no file on the filesystems setup scans when it starts; other mounted filesystems are not scanned, so a uid that owns files only there (a cache volume, say) can be chosen, and the member then owns those files.
- When a member ends, the processes running under its uid that reaping's scans find are killed, and those its last scan still sees are recorded as survivors, as are a timeout and a failure. This is not a guarantee that nothing outlives the member: a process that forks and exits between a round's process listing and its status reads can leave a child no scan sees, and a round that then finds nothing else ends with `ok` ([Not this design](#not-this-design)). The cgroup rung would make reaping atomic.
- A member's built-in tools cannot be configured to run as root or as a peer inside its scope, and a shell cannot be shared between members.
- A bridged member's model proxy and bridged tools serve only connections whose socket its uid owns: peers, root and the tools user are refused, and the bridge does not yield until its own proxy holds its port at every loopback address the sandbox has.

**What it does not guarantee:**

- **Containment.** All members and root share one kernel and one sandbox. A member that becomes root (a kernel exploit, a misconfigured setuid binary, `sudo` in the image) defeats every boundary here. The default images' setuid binaries are the stock set, and new users have locked passwords, so `su` fails *(spike)*. This is boundary A of inspect_ai's trust model with N agent principals instead of one, and its "co-resident process under a different uid" attacker position applies between members.
- **Running a peer's code.** In a `rw` share any member can plant code a peer later runs as itself: a Makefile, a test, a virtualenv, a git hook or `core.fsmonitor` in a shared repository's config. That code runs with the victim's uid and can read the victim's private directory. In shared-artifact tasks members run each other's code as a matter of course, so private-directory confidentiality against a hostile peer holds only for members that never execute shared code. `safe.directory` is set only for the shares the eval author listed, so git still refuses a repository a member plants in `/tmp` to others.
- **Visible process table.** Peers see each other's command lines; a secret on a command line is visible to all.
- **What the bridge check does not stop.** It does not stop a member from sharing its own bridge, by relaying requests for a peer or handing it a connected socket, any more than a token would, and code a peer planted that the member runs is the member. A member can occupy the port a peer's bridge will take and make that bridge fail to start, which ends the peer with an error; it cannot receive the peer's requests. inspect_ai's [Bridge peer check](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-trust-model.md#sandbox-identity-scopes) states the check's other limits.
- **Shared transient state.** `/tmp`, `/var/tmp`, `/dev/shm` and `/run/lock` are world-writable; files there under umask `022` are world-readable. Abstract Unix sockets and localhost TCP ports are shared, so a member can serve and another connect *(spike)*. These are the unsanctioned channels this rung leaves open; the census sees the files, not the sockets.
- **Shared-share integrity.** In a `rw` share any member can change or delete any file, move any branch of a shared repository, and remove its own files' ACL entries. A member's own files are its own to expose, including by hard-linking them into a share.
- **Resource exhaustion.** No per-member CPU, memory or process limits.
- **The harness reads everything.** The swarm's setup, reaping and census run as root, and the task's scorers' raw sandbox calls, in the loop and at the end, as the sandbox's default user, root in most images. A member's custom tools run as the member unless their code names another user or calls the provider object directly; either is a grant made in code, which `foreign_execs` counts for execs that name a user and does not see for calls on the provider object.
- **Explicitly named other sandboxes.** A member tool built with an explicit other sandbox runs there as that sandbox's user. Such a service is the eval author's trusted component, outside this topology.

**Untrusted input that reaches the new code:**

- **Paths and names in the configuration** come from the eval author, are validated at construction, and reach the scripts only as positional arguments.
- **The sandbox's existing contents** may have been shaped by an agent before the swarm starts. Setup runs with a pinned `PATH`, traverses with `find -P -xdev` and changes only the entries it lists with the forms that do not follow a symlink in the last component, so a symlink already in a share does not redirect a change. Those forms still resolve the directories above that component when the change runs, so they would not stop a process that swaps a directory for a symlink between the traversal and the change; none can run then, because setup runs once per sandbox before any member exists, no other sandbox's members share its read-write storage (by the preconditions), and what ran before is the task's own code or a swarm without isolation, with the sandbox's default authority. Setup also refuses unsafe ancestors, symlinked paths and existing names, chooses ids that own nothing, and checks the result with the self-test rather than trusting the commands' exit codes.
- **Files members create** reach the census, which runs as root after the drain over member-writable directories. It never follows a symlink or crosses a filesystem, reads only metadata, caps entries and bytes, and records paths as JSON strings in a swarm event, never as Markdown and never interpreted.
- **Processes members start** reach reaping, which reads only `/proc/<pid>/status` and `/proc/<pid>/comm`, records process names only as sanitised text, and kills by pid as root. A pid that exits and is reused between its status read and the kill could be a non-member process; re-reading the status immediately before each kill narrows that window to the kernel's pid-reuse latency.
- **Connections to a bridge proxy** are judged inside a scope by the kernel's record of who owns the client's socket, never by anything the client sends; inspect_ai's [Bridge peer check](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/design/sandbox-trust-model.md#sandbox-identity-scopes) covers how the lookup resists what a member can do to the socket table.
- **The user the proxy is told** comes from the scope, which the swarm sets from validated member names, and resolves through `/etc/passwd`, which members cannot write.

**How this bears on swarm.md's questions.**

- **Unsanctioned channels.** Users isolation turns the filesystem part of "do agents build channels we did not give them?" into an owner census with a sanctioned/unsanctioned split ([What the topology makes measurable](#what-the-topology-makes-measurable)), and leaves sockets and process listings open. It is an intermediate rung: shared default, users, users with a later rung, a sandbox per member.
- **Containment baselines.** A "no communication" arm under users isolation is not isolated: `/tmp`, sockets and command lines remain. It needs per-member sandboxes. What users isolation offers there is the census of member-owned files left in the file channels that remain.
- **Messages as the only channel.** `users(shared=())` with `peers="none"` removes the sanctioned file channel but not the world-writable directories or sockets, so it reduces, and censuses, but does not remove, the alternatives.

## Not this design

Noticed and left out, for Ransom to file if wanted:

- **Reaping can record `ok` with a member's process still running** (inspect_swarm). The race is stated under [Security](#security). Rescanning until a scan finds nothing after the last kill would narrow it; the cgroup rung would close it. `MemberReap`'s docstring and [Isolation, The record](../docs/isolation.qmd#the-record) describe `ok` as no process of the member's user being left.
- **`baseline()` takes no `isolation`** (inspect_swarm), so an isolated single-agent arm is written out as a swarm of one ([Arms and identity](#arms-and-identity)).
- **inspect_swe bridge ports collide across agent types** (inspect_swe). `claude_code` and `codex_cli` each count their ports from 3000 in the sample store, so the first member of each in one sandbox both take port 3001. This affects any heterogeneous bridged swarm in one sandbox, isolated or not; one counter across types fixes it, and lets a swarm mix bridged agent types.
- **The isolation docs describe a raw `exec()` that keeps the sandbox's working directory** ([Isolation](../docs/isolation.qmd#what-each-member-gets) and the `users()` docstring). inspect_ai's scope runs an `exec()` with no `cwd` in the member's home, and resolves relative `cwd`s and file paths against it (checked through the sample's proxy with a stub sandbox). They also list `web_browser()` among the tools that raise inside a member; inspect_ai has removed that tool, which now raises everywhere.
- **swarm-result-record.md says users isolation keeps its group, users and uids under an `inspect_swarm` store key** ([its Not this design](swarm-result-record.md#not-this-design), "The users-isolation record"). The runtime keeps only which sandboxes it has set up, by provider object in the host process, and reads the store for none of it.
- **The Kubernetes provider resolves `runuser` through the image's `PATH`** (inspect_k8s_sandbox), where inspect_ai's provider contract asks for absolute paths or a fixed system `PATH`. It matters once a non-root principal can write a directory on root's `PATH`; not verified as exploitable.
- **Whether `rw` shares keep ACLs on gVisor in a pod** (the container root, an `emptyDir`, an `emptyDir` with `medium: Memory`) has not been run on a cluster, nor has the peer check in a pod's network rather than `runsc do`'s. The self-test refuses a share whose ACLs are not kept, and the bridge fails rather than serves a peer, so neither is silent.
- **Separating bridges outside a scope.** In the shared default every member runs as one user, so no peer check can tell members' bridges apart; separating them would need a credential per bridge, with a migration for clients that send dummy keys ([Alternatives](#alternatives-considered)).
- **Sandbox instrumentation** for reads, modifications and socket traffic (audit logs, fanotify), which the census cannot see. Already listed in swarm-communication.md.
- **A "files only" channel arm** built from private sandboxes and a shared volume, if experiments ask for container-level separation with one shared directory.
