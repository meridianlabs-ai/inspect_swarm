# Inspect Swarm: per-member users in one sandbox

Status: proposed, 2026-10-13. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

Decisions Ransom took on this design (2026-10-13), marked inline as "(decision: Ransom, 2026-10-13)": the per-member identity comes from an inspect_ai `sandbox_identity()` scope, a small general inspect_ai PR before this work, not a swarm-only wrapper; and a member's leftover processes are reaped when the member ends, not only at the drain.

## Overview

Several swarm members can share one sample sandbox and still be kept apart, the way SCHEME runs its agents: each member gets its own Linux user. With `swarm(isolation=users())` the swarm creates one non-root user per member when the sample starts, gives each a private home directory that is also its scratch directory, and sets up the shared directories the eval author lists, each read-write for all members or read-only. Every sandbox command a member's tools run, including a bridged Claude Code or Codex process, then runs as that member's user. The swarm does this by entering a small inspect_ai scope around each member, the same way it already enters a tool-state scope; inside it, a bridge's local model proxy also demands a per-member token, so members cannot use each other's bridges. Members are told their user and directories in their preamble. When a member ends, the swarm kills every process still running under its user (and records it if any survive), and at the end of the sample it records who owns the files in the shared and world-writable directories.

This buys a default boundary and attribution, not containment. A member cannot read or change another member's private directory unless the eval author allows reading, cannot write read-only inputs, cannot signal or read the environment of a peer's processes, and leaves its uid on every file it creates, even from a background process. It can still see peers' command lines, share files through `/tmp`, connect to peers' sockets on localhost, and, because all members share one kernel and one sandbox root, anything that gets it root gets it everything. The first version needs no extra container privileges. It works on Docker; on Kubernetes users and private homes work, and whether shared directories do depends on the filesystem under them, which a self-test checks. Stronger separation (hidden processes, private `/tmp`, per-member resource limits) needs container flags that weaken the sandbox's own boundary, so it is surveyed here and left as later, optional work; a sandbox per member remains the choice when an arm needs members truly apart.

## Why

swarm.md defers this topology ("one shared sandbox with a Linux user and write scope per member, as SCHEME does", [Sandbox topology](swarm.md#sandbox-topology)). Three things make it worth designing now, though not building before it is needed:

- **The shared default has only conventions.** Per-member scratch directories and worktrees are a prompt convention the swarm "can set up but not enforce" (swarm.md, [Concurrent work in one shared sandbox](swarm.md#sandbox-topology)). A member that writes into another's worktree, or a broken build that a peer's tests pick up, changes what an experiment measures. Per-member artifacts for shared-artifact tasks say "what was on that member's branch", not "what that member did alone", because "any member can write into another member's worktree or branch" ([swarm-scoring.md](swarm-scoring.md#shared-artifact-tasks-and-per-member-artifacts)).
- **The filesystem channel is observed only through tool calls.** Processes a member starts can read and write between logged calls and after it ends, and the swarm's drain cannot stop them ([swarm.md Members](swarm.md#members), [swarm-communication.md](swarm-communication.md#the-filesystem-channel)). With one uid for everyone, nothing in the sandbox says who wrote a file.
- **The containment questions need intermediate rungs.** swarm.md's eval questions include "Do agents build channels we did not give them?", with containment probes over "shared caches, shared directories, network" ([Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply)), and SCHEME's design (2–4 agents in one shared Docker sandbox, "each agent runs as its own Linux user", with "per-agent file read/write permissions ... enforced at the sandbox layer"; [arXiv 2605.29178](https://arxiv.org/abs/2605.29178), Appendix B) is the closest published relative of ours. A sandbox per member is the other end: LeanOpenProblems needed one ([swarm-lean-open-problems.md](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field)).

Ransom asked (2026-10-13) for a design that says which Linux isolation features are usable inside Inspect's sandboxes, how members' tools and bridged CLIs would run as their own users, how directories are laid out and shared, how the topology is configured and logged, how it interacts with scoring, limits and the LeanOpenProblems comparison, how it compares with the alternatives, and what it does and does not guarantee. The timeline is open: this is not M1 work.

## Goals and non-goals

Goals:

- A survey of Linux isolation features inside a sandbox (users and groups, permissions and ACLs, mount, user, PID and network namespaces, bubblewrap, Landlock, seccomp, capabilities, cgroups), with what each isolates, what it needs, whether it works in a default Docker container and on the default Kubernetes setup, and what it costs; checked against inspect_ai's sandbox code and by experiment.
- A complete design for the first useful rung: one non-root user per member in one shared sandbox, private home directories, read-write and read-only shared directories, peers' read access as a setting, with every model-facing sandbox command a member causes running as its user, including bridged CLIs.
- No extra container privileges for that rung, so it runs on the sandboxes tasks already use.
- Processes a member leaves running are stopped when the member ends.
- What the topology makes measurable that the shared default cannot, recorded in the swarm's evidence.
- Configuration that follows swarm-api.md's logging rule, so arms that differ in it are distinct in an eval set.
- An honest statement of what it does not guarantee.

Non-goals:

- **Containment.** Members share a kernel and a sandbox root. This design is not a boundary against a member that gets root, and does not claim to be one ([Security](#security)).
- **Hidden processes, private `/tmp`, private network, per-member CPU and memory limits.** Each needs container flags or kernel features the default sandboxes lack. They are surveyed and listed as later, optional work with their costs, not designed ([Later rungs](#later-rungs-surveyed-not-designed)).
- **A sandbox per member**, or private sandboxes with a shared volume. Those are the alternatives this design is compared with; per-member sandboxes are designed in [swarm-lean-open-problems.md](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field) and the two are not combined here.
- **Per-member isolation in the `local` sandbox**, which runs as the host user and cannot switch users.
- **Coordinator-only read access.** Coordinator topologies are later work in swarm.md; how they would get read access is stated, not built ([Peers' access](#peers-access-to-private-directories)).
- **Changing the existing designs.** [Changes to other designs](#changes-to-other-designs) lists the edits it would need, for Ransom to approve.

## Current behaviour

Code references are to inspect_ai `fccfb298e` (2026-10-07), the version this repository's lockfile installs. Of the files cited, `src/inspect_ai/agent/_bridge/sandbox/bridge.py` and the sandbox-tools `proxy.py` have changed on `main` since (at `81c1668d3`, 2026-10-13, the proxy launch is at `bridge.py:249-254`); the behaviour described here, the proxy's launch as the tools user and its unauthenticated loopback listener, is the same there. Also inspect_ai's `design/sandbox-trust-model.md` at `5f90df646` (2026-10-07), inspect_swe at `a54461e` (2026-10-05), inspect_k8s_sandbox at `18e4508` (2026-10-09) and gVisor at `783e129aa0a3` (2026-10-13). Paths are relative to each repository's root. Claims marked *(spike)* were checked on Docker Desktop 28.5.1 (arm64, linuxkit kernel 6.10.14, cgroup v2) in throwaway containers, or through an Inspect eval on mockllm with the `docker` sandbox and the `aisiuk/inspect-tool-support` image; nothing from them is committed.

### Exec identity in the sandbox interface

- `SandboxEnvironment.exec()` takes `cwd`, `env` and `user`, "Optional username or UID to run the command as" (`src/inspect_ai/util/_sandbox/environment.py:204-214`, `:249-251`). A provider raises `SandboxUserUnsupportedError` only "when the sandbox configuration makes execution as that user impossible" (`:38-45`, `:263-264`); a user that does not exist is left to the provider, and the conformance test accepts either an error or a failed result naming the user (`src/inspect_ai/util/_sandbox/self_check.py:641-652`).
- `read_file()` and `write_file()` take no user (`environment.py:283`, `:310`).
- `exec_remote()` takes a user, cwd and env in its options (`src/inspect_ai/util/_sandbox/exec_remote.py:101-107`). The user is resolved by `tools_user_param()`: "An explicit `user` wins; otherwise the sandbox's default exec identity" captured when the tools were injected (`src/inspect_ai/util/_sandbox/_cli.py:36-49`).

### Providers

- **Docker** passes `--user` and `--workdir` to `docker compose exec` (`src/inspect_ai/util/_sandbox/docker/docker.py:332-341`), and env as `--env` (`:345-348`). An unknown user comes back as a failed `ExecResult` with "unable to find user nosuch: no matching entries in passwd file" *(spike)*; the provider's failure classifier does not map it to an exception (`docker/failure.py:70-166`). `write_file` runs `mkdir -p` and `tee` through `exec()` with no user, as "the container's default user (root in most images)" (`docker.py:461-507`, the comment at `:469`), so the files it writes are root-owned *(spike: a file written into a member's home was `root root`)*. `read_file` uses `docker compose cp` (`docker.py:538-547`), which goes through the daemon, so in-container permissions do not restrict it *(spike: it read a member's file in a `0700` home)*.
- **The default compose file** adds no capability, seccomp, user or resource settings: `image: aisiuk/inspect-tool-support`, `init: true`, `network_mode: none` (`docker/config.py:113-121`). The image's default user is root; it ships an opt-in non-root user, uid 65532 (`src/inspect_tool_support/Dockerfile:7-13`).
- **Kubernetes** (inspect_k8s_sandbox) switches user by running `runuser -u <user> -- /bin/sh` inside one shell in the default container, skipping the switch when the container already runs as that user (`src/k8s_sandbox/_pod/execute.py:24-62`). Its docs: "To switch to a *different* user, you must run the container as root, ensure that `runuser` is installed in the container, and keep `CAP_SETGID`" (`docs/docs/design/limitations.md:180-183`). `cwd` and `env` apply after the switch (`execute.py:98-127`). An unknown user raises `RuntimeError`; a non-root container, dropped `CAP_SETGID` or missing `runuser` return a failed result with a warning (`:213-262`). `read_file`/`write_file` run as the container's own user, never switched (`_pod/read.py:27`, `_pod/write.py:26-42`).
- **The Kubernetes chart's defaults** suit this design: the main container's `securityContext` is empty (`resources/helm/agent-env/values.yaml:145-146`), so it runs as the image's user (root for the default `python:3.12-bookworm`, `:98`) with the runtime's default capabilities; the runtime class defaults to `gvisor` (`values.yaml:94-96`, `templates/services.yaml:44-47`); resources default to Guaranteed 2Gi/500m (`services.yaml:135-146`); egress is a Cilium policy with no internet by default (`values.yaml:7-12`, `:33-37`). The compose-to-Helm converter rejects `cap_add` and `privileged` (`src/k8s_sandbox/compose/_converter.py:396-399`), so a task cannot ask for extra capabilities through a compose file on Kubernetes; a values file's `securityContext` is free-form (`values.schema.json:223-225`).
- **local** raises `SandboxUserUnsupportedError` for any user but the current one (`src/inspect_ai/util/_sandbox/local.py:85-89`; `docs/sandboxing.qmd:110`).
- **Other providers** (inspect-podman, Daytona, E2B, Modal, EC2, Proxmox on Linux) implement `user=` through their exec API, `sudo -u`, `su` or `su -l`; Proxmox on Windows ignores it.

### Who runs as whom today

inspect_ai already has a second principal in a sandbox, documented in `design/sandbox-trust-model.md`:

- Injected tools run as the **tools user**, root when root is usable (`environment.py:195`; `src/inspect_ai/tool/_sandbox_tools_utils/sandbox.py:179-198`), decided by a probe at sample init, before the agent runs (`src/inspect_ai/util/_sandbox/context.py:293-297`, `sandbox.py:343-373`).
- "The user a tool runs as is host-side configuration, chosen by the eval author when the tool is constructed ... **The model chooses what to run, not as whom.**" (trust model, "Tool user confinement").
- The trust model names "a co-resident process under a different uid" as an attacker position, and its Boundary A (agent user against tools user) exists only "when every model-facing tool runs as a uid that is neither root nor the tools user's".
- Commands Inspect issues on its own behalf run through `privileged_exec()`/`privileged_shell()`, which pin `PATH` so that a non-root agent cannot plant the program root runs (`src/inspect_ai/util/_sandbox/_privileged.py:1-60`).

### Which tools honour a user

| Tool | `user` | How it runs | Source |
|---|---|---|---|
| `bash()`, `python()` | yes | `sandbox.exec(["bash", "--login", "-c", ...], user=user)` | `src/inspect_ai/tool/_tools/_execute.py:64-68`, `:105-107`, `:164-169` |
| `grep()`, `list_files()`, `read_file()` (tools) | yes | `exec(..., user=user)` | `_grep.py:67`, `_list_files.py:39`, `_read_file.py:53-56` |
| `bash_session()`, `text_editor()` | yes | the injected tools; the user goes to `tools_user_param()`, and the tools process (root) switches to it per call | `_bash_session.py:203`, `_text_editor.py:125-127` |
| `skill()` | yes | writes, then `chown`s to the user | `_skill/install.py:57-73` |
| `computer()` | no | `exec()` with no user | `_computer/_common.py:193` |
| `web_browser()` | no | legacy tool-support transport with no user | `_web_browser/_web_browser.py:416-442` |
| `memory()`, `think()`, `todo_write()`, ... | n/a | no sandbox | |

Spike: in an Inspect eval with two users created as root, `bash(user="swarm-w1")` ran as `uid=1000(swarm-w1)` with `HOME=/scratch/w1` and cwd `/`; `bash_session(user="swarm-w1")` ran as `swarm-w1`; `text_editor(user="swarm-w2")` created a file owned by `swarm-w2` and was refused a peer's private file with `Permission denied` *(spike)*.

Every one of these takes its user when the tool is **constructed**. A swarm member's agent is built once and invoked for every member its record stands for, in every sample (swarm-api.md, [the member contract](swarm-api.md#members)), so a tool cannot be built with a per-member user.

### How `sandbox()` resolves

`sandbox(name)` reads the private `sandbox_environments_context_var`; with no name, `"default"`, or a single environment, it returns the default environment (`src/inspect_ai/util/_sandbox/context.py:48-75`). The public `sandbox_default(name)` only chooses among existing names (`:421-432`). Every environment is wrapped in a `SandboxEnvironmentProxy` that records `SandboxEvent`s, including the exec's `user` in the event's options (`context.py:276-277`; `events.py:39-112`, the options at `:85-91`). `bash_session()` and `text_editor()` find their sandbox by injection, iterating the same ContextVar (`context.py:182-231`). LeanOpenProblems reset that private ContextVar per member to give each its own sandbox ([swarm-lean-open-problems.md](swarm-lean-open-problems.md#their-swarm)).

### Bridged agents

- inspect_swe's agents (`claude_code`, `codex_cli`, `gemini_cli`, `opencode`, `kimi_code`, `mini_swe_agent`) all take `user=`, `cwd=` and `env=` (`inspect_swe: src/inspect_swe/_claude_code/claude_code.py:143-146`, `_codex_cli/codex_cli.py:113-117`). The CLI is launched with `exec_remote(..., user=user, cwd=agent_cwd)` (`claude_code.py:547-556`); its config is written as that user under its `$HOME` (`claude_code.py:676-699`; Codex's `CODEX_HOME` defaults to `<cwd>/.codex`, `codex_cli.py:486-494`); `resolve_agent_cwd()` runs `pwd` as the user, and when the sandbox's working directory is `/` uses the user's home (`_util/sandbox.py:58-94`). The binary is installed under `/var/tmp/.5c95f967ca830048`, written with `write_file` and made executable as root (`_util/agentbinary.py:210-243`).
- `sandbox_agent_bridge()` has no user parameter. It runs its in-sandbox model proxy as the tools user (`src/inspect_ai/agent/_bridge/sandbox/bridge.py:245-257`). The proxy listens on `127.0.0.1:<port>` (`inspect_ai: src/inspect_sandbox_tools/src/inspect_sandbox_tools/_agent_bridge/proxy.py:56`, `:669`) and serves the model routes and the bridged tools' MCP endpoint `/mcp/{name}` (`:726`, `:2153-2180`). No route checks a credential; inspect_swe gives the CLI a dummy token (`inspect_swe: src/inspect_swe/_claude_code/env.py:97-98`).
- inspect_swe allocates each bridge's port from a counter in the sample store keyed by agent type, starting at 3000 for most types (`claude_code.py:311-314`, `codex_cli.py:356-359`), so a Claude Code member and a Codex member in one sandbox both take port 3001.

### What a default container allows

*(spike, all in a default `docker run` as root: no added capabilities, Docker's default seccomp profile; Docker Desktop has no AppArmor.)* The default capability set is chown, dac_override, fowner, fsetid, kill, setgid, setuid, setpcap, net_bind_service, net_raw, sys_chroot, mknod, audit_write and setfcap; a user switched to with `setpriv`, `runuser` or `su` has empty permitted and effective sets. The findings per feature are in the [survey](#survey-linux-isolation-inside-one-sandbox) below. The ones this design builds on:

- `useradd`, `su`, `runuser`, `setpriv` are present in `python:3.12-bookworm` and in `aisiuk/inspect-tool-support`; `setfacl`, `bwrap`, `socat`, `ip` and `iptables` are not. Neither image has `sudo`.
- Debian's `useradd -m` creates homes `0755`, so a peer can read them; `useradd -m -K HOME_MODE=0700` gives `0700`, and a peer then gets `Permission denied`.
- POSIX ACLs and default ACLs work on the container's overlay root and on a named volume; on a Docker Desktop bind mount from macOS `setfacl` fails with "Operation not supported", and that filesystem does not enforce Unix permissions at all: a peer read, wrote and deleted a file in another user's `0700` directory.
- In a shared directory with mode `2770` and group `swarm` but no default ACL, a file one member created with umask `022` could not be appended to by another: "Permission denied" *(spike, through Inspect)*.
- A peer sees every process and its full command line (`--secret-arg=hunter2` was visible), but `/proc/<pid>/environ`, `cwd` and `fd` are denied and `kill` fails with "Operation not permitted".
- A member's `nohup sleep 300 &` kept running after its tool call returned; a root shell that scanned `/proc/*/status` for the member's uid found it, `kill -KILL` ended it, and a second scan found none *(spike, through Inspect)*.

A spike with git: a repository in a shared directory owned by root and group `swarm` with `core.sharedRepository=group`, setgid directories and a default ACL. A member's `git status` there failed with "fatal: detected dubious ownership in repository"; after `git config --system --add safe.directory`, each member added a worktree on its own branch inside its `0700` home, committed, and merged the other's branch, and a member could not read the other's worktree files *(spike)*.

## Survey: Linux isolation inside one sandbox

"Default Docker" is a default container as root (the spike above). "Kubernetes" is inspect_k8s_sandbox's default chart: gVisor (`runsc`), root container, empty `securityContext`. gVisor rows are from its syscall table and documentation, not run here.

| Feature | Isolates | Needs | Default Docker | Kubernetes (gVisor) | Cost and notes |
|---|---|---|---|---|---|
| **Users and groups** | File access by owner, group and mode; signals; `/proc/<pid>/environ`, `cwd`, `fd` | Root in the sandbox with `setuid`/`setgid`; `useradd` in the image; a provider that execs as a user | Works *(spike)* | Works: `runuser` from a root container with `CAP_SETGID` (`execute.py:56-62`); gVisor supports `setuid`, `setgid`, `setgroups` (`pkg/sentry/syscalls/linux/linux64.go:140`, `:151`) | Free. Process list and command lines stay visible; `/tmp`, abstract sockets and localhost stay shared. Members lose root. |
| **Modes and setgid directories** | Private homes (`0700`), group-shared directories | Nothing beyond users | Works *(spike)* | Works (DAC is ordinary) | Free. Without a default ACL, peers cannot edit each other's new files (umask `022`). |
| **POSIX ACLs** | Shared read-write directories whose new files stay group-writable whatever the creator's umask; named readers | `setfacl` (package `acl`), a filesystem with ACLs | Works on the overlay root and named volumes; not on macOS bind mounts *(spike)*; `setfacl` absent from both images | gVisor implements ACLs in its VFS and on tmpfs (`pkg/sentry/vfs/posix_acl.go`), but its gofer filesystem returns `EOPNOTSUPP` for ACL setters (`pkg/sentry/fsimpl/gofer/filesystem.go:1959`), so whether a share works depends on the backend under it (a tmpfs volume, an overlay with a memory upper layer, or a gofer mount); unverified on a cluster | An image dependency. The owner of a file can change its ACL. |
| **Mount namespace** (`unshare --mount`, bind mounts) | A per-command view of the filesystem: private `/tmp`, read-only binds | `CAP_SYS_ADMIN`, or an unprivileged user namespace | `unshare: ... Operation not permitted`; works with `--cap-add SYS_ADMIN` *(spike)* | gVisor supports `mount`, `pivot_root`, `unshare` (partial) (`linux64.go:196`, `:200`, `:307`) | Per command, not per member: a namespace lasts as long as the process tree that holds it. |
| **User namespace** (`unshare --user`) | Lets an unprivileged process create the other namespaces | Docker's default seccomp allows it only with `CAP_SYS_ADMIN` | Fails; works with `--security-opt seccomp=unconfined` *(spike)*; a proc mount inside also needs `--security-opt systempaths=unconfined` | Supported, partially (`linux64.go:91`, `:307`) | Turning off seccomp exposes the whole kernel syscall surface to untrusted code. |
| **bubblewrap** (`bwrap`) | Per-command PID, mount, IPC and network namespaces; private `/tmp`; read-only binds | `bwrap` in the image; an unprivileged user namespace | "No permissions to create new namespace"; works as a member with `seccomp=unconfined` plus `systempaths=unconfined` *(spike)*: `ps -e` showed only its own processes, a peer's home was denied | Likely, since gVisor has user and mount namespaces; untested | As above, plus every member command wrapped. `SYS_ADMIN` alone fails at `pivot_root`, which Docker's seccomp profile blocks *(spike)*. |
| **PID namespace / `hidepid`** | Peers' processes and command lines | `CAP_SYS_ADMIN` to remount `/proc` | `mount: /proc: permission denied`; `mount -o remount,hidepid=invisible /proc` works with `--cap-add SYS_ADMIN` and keeps Docker's `/proc` masks *(spike)* | gVisor's `procMount` is unsupported in sandboxed pods ([GKE Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/sandbox-pods)); `hidepid` untested | `SYS_ADMIN` is the broadest capability; granting it to a container that runs untrusted code as root weakens the sandbox's own boundary. |
| **Landlock** | Unprivileged, per-process restriction of file access; from ABI 4 TCP bind and connect; from ABI 6 abstract Unix sockets and signals | A kernel built with Landlock and listing it in its LSMs | Not available: Docker Desktop's kernel is built without it (`ENOSYS` even `--privileged`) *(spike)*. Docker's default seccomp profile allows its syscalls, so on a Linux host whose kernel enables it, it should work unprivileged (not verified here) | `ENOSYS`: syscalls 444–446 are missing from gVisor's table (`linux64.go:391`, `:398-400`) | No flags where it exists; availability depends on the host kernel. The only route here to scoping localhost and abstract sockets without privileges. |
| **seccomp** (own filters) | Syscalls a process may make | `prctl(PR_SET_NO_NEW_PRIVS)` | Works: a member installed a filter that blocked `mkdir` *(spike)* | Filter mode only (`linux64.go:352`) | Restricts syscalls, not resources or files; useful only as a companion to the above. |
| **Capabilities** | What root can do | n/a | Non-root members have none *(spike)* | gVisor's capabilities are virtual | Members already have none; the sandbox root keeps the default set. |
| **cgroups v2** | Per-member memory, CPU and process limits; killing a member's processes atomically | A writable cgroup filesystem | `/sys/fs/cgroup` is read-only; `--security-opt writable-cgroups=true` (Docker 28) or `SYS_ADMIN` and a remount make it writable; then `memory.max` and `pids.max` per child group work, after moving every process out of the root group *(spike)* | gVisor: "resource limits are not enforced within the sandbox" ([compatibility](https://gvisor.dev/docs/user_guide/compatibility/)) | Docker-only; must not be delegated to the member (a member given its group raised its own `pids.max`) *(spike)*. |
| **rlimits** (`ulimit -u`, `-v`) | Process count per uid; address space per process | Nothing | Work, but `RLIMIT_NPROC` counts the uid's processes on the whole host, across containers, because Docker uses no user namespace: a limit in one container failed because another container ran processes as the same uid *(spike)* | Untested | Unsafe for per-member caps when parallel samples reuse uids. |
| **Network namespace / per-uid rules** | A member's network, including its TCP connections to peers' ports on loopback | `SYS_ADMIN` (namespace); `NET_ADMIN` and `iptables` (owner match) | `unshare --net` fails; `iptables -m owner --uid-owner` works with `--cap-add NET_ADMIN` *(spike, egress only)* | `cap_add` not convertible from compose; Cilium policies are per pod | Owner matches in `OUTPUT` apply to locally generated traffic, loopback included ([iptables-extensions](https://ipset.netfilter.org/iptables-extensions.man.html)), so they can restrict a member's localhost TCP as well as egress; they do not restrict Unix sockets. |

What follows for the design:

- **Users, modes and ACLs work with no extra privilege** on both providers that matter, so they are the first rung. ACLs are needed only for read-write shares, and are an image dependency.
- **Everything that hides processes, privatises `/tmp` or the network, or limits resources needs a container flag** (`SYS_ADMIN`, `seccomp=unconfined`, `writable-cgroups`, `NET_ADMIN`) or a kernel feature that neither Docker Desktop nor gVisor has (Landlock). Each flag widens what untrusted code running as the sandbox's root can do against the host kernel, which is the boundary that matters most, and Kubernetes cannot take them from a compose file. They are later rungs, opt-in per task ([Later rungs](#later-rungs-surveyed-not-designed)).

## Design

### At a glance

```python
from inspect_swarm import Share, member, swarm, users

swarm(
    members=member(worker(), count=4),
    channels=["filesystem"],
    isolation=users(
        shared=[Share(path="/shared", access="rw"), Share(path="/data", access="ro")],
        peers="none",
    ),
)
```

When the sample's swarm starts, before any member does, the runtime:

1. checks the sandbox can run commands as other users and that the configured paths are safe, and creates one user per member (`swarm-w-1`, ...), with a uid that owns no existing file and its home, the private directory, at `/scratch/w-1`;
2. prepares each shared directory for its access mode;
3. runs a self-test as each member, and refuses to start if any boundary it promises does not hold.

Each member then runs inside `sandbox_identity(user, cwd=home, sandbox=...)`, an inspect_ai scope, so its tools' sandbox commands run as its user in the shared sandbox. The member's preamble states its user and directories. When the member ends, the runtime kills what is still running under its uid. At finalisation it records who owns what in the shared and world-writable directories.

### Configuration

```python
# src/inspect_swarm/_isolation.py
class Share(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str                          # absolute; must not be inside another share or a private directory
    access: Literal["rw", "ro"]        # rw: every member reads and writes; ro: every member reads only


class UserIsolation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["users"] = "users"
    sandbox: str = "default"           # the sandbox environment members share, by its name in the sample
    home: str = "/scratch/{member}"    # each member's private directory and $HOME
    shared: tuple[Share, ...] = (Share(path="/shared", access="rw"),)
    peers: Literal["none", "read"] = "none"   # may members read each other's private directories?


def users(
    *,
    sandbox: str = "default",
    home: str = "/scratch/{member}",
    shared: Sequence[Share | Mapping[str, str]] = (Share(path="/shared", access="rw"),),
    peers: Literal["none", "read"] = "none",
) -> UserIsolation: ...


# swarm-api.md's swarm(), with one new keyword:
def swarm(
    members: ...,
    *,
    controller: ... = "leaderless",
    channels: ... = ("filesystem",),
    budget: Budget = Budget(),
    isolation: UserIsolation | Literal["users"] | None = None,   # None: today's shared sandbox
) -> Agent: ...
```

- **`isolation=None`** is today's behaviour, unchanged: the shared sandbox with conventions only (decision: Ransom, 2026-10-07, "a shared sandbox by default").
- **`"users"`** resolves to `users()` with its defaults, so a task can expose it as `-T isolation=users`. A mapping (what replay passes back) is validated with `UserIsolation.model_validate()`, as `swarm()` does for `Member` ([swarm-api.md Logging and replay](swarm-api.md#logging-and-replay)).
- **Why a pydantic model and no registry type.** No user needs to extend how isolation works, and the rungs above it change what a member can do in ways a third-party implementation could get wrong silently. A frozen pydantic model is logged as its fields and rebuilt from them, so arms that differ in any field are distinct in an eval set ([Arms and identity](#arms-and-identity)). A later rung adds a `kind` and its own model to the union.
- **Defaults line up with the filesystem channel.** swarm-api.md's `filesystem(shared="/shared", notes="notes.md", scratch="/scratch/{member}")` names the same paths, so `channels=["filesystem"]` and `isolation="users"` agree with no arguments.
- **What is refused, at `swarm()` construction**, with `ValueError` naming the argument:
  - a `home` without `{member}`, or a relative `home` or share path, or a path containing `..`, a newline or a NUL;
  - a share inside another share or inside the home template's parent, and two shares with the same path;
  - a member name that is not a valid user-name suffix: after count expansion each name must match `[a-z][a-z0-9-]{0,25}`, so that `swarm-<name>` is a portable Linux user name of at most 32 characters. Member names are the eval author's identifiers (swarm-api.md, [Members](swarm-api.md#members)), so this only constrains the author;
  - `filesystem` among the channels with a `scratch` other than `home`, or a `shared` that is not one of the `rw` shares: the preamble would otherwise describe directories the member cannot use;
  - a member with its own sandbox (`member(sandbox=...)`, LeanOpenProblems recommendation 1, which M2 builds): the two topologies are not combined until someone needs both.

### What the runtime does at swarm start

Setup runs once per swarm run, in the host process, before the controller's `run` is called. Every command runs through inspect_ai's `privileged_shell()` as `user="root"`, with `PATH` pinned to the system directories, because the sandbox may already hold content an agent shaped (a setup solver, or an earlier agent in the same sample). It is one shell script per sandbox, generated from the configuration with every path and name passed as a positional argument, never interpolated into the script text. Every traversal is `find -P <path> -xdev`, and changes are applied to the entries it lists, never through a symlink: `chmod` has no `-P` (GNU `chmod` rejects it; `chgrp` and `chown` take `-h`), so the script does not rely on recursive flags.

1. **Preconditions.**
   - The sandbox named by `isolation.sandbox` must be one of the sample's environments. The runtime checks the name against the environment mapping itself, not through `sandbox(name)`, which returns the default for any name when the sample has one environment (`src/inspect_ai/util/_sandbox/context.py:67-68`).
   - Root must be usable. The runtime reads the sandbox's root-access verdict (`resolve_root_access()`, `src/inspect_ai/tool/_sandbox_tools_utils/sandbox.py:343`) and refuses with `RuntimeError` unless it is `usable`: "users isolation needs root in sandbox 'default' to create users; its root access is unusable (...)". The `local` sandbox is refused by name: "users isolation needs a sandbox that can run commands as other users; 'local' runs everything as the host user".
   - The script checks that `useradd`, `groupadd` and a `find` with `-printf` and `-writable` (GNU findutils) exist, and `setfacl` and `getfacl` when any share is configured, and exits with a message naming the missing package (`passwd`, `findutils`, `acl`). Homes alone (`shared=()`) need no ACL tools.
2. **Safe paths.** A private directory or share is only as private as the path that reaches it: a peer that can write a home's parent can rename the `0700` home away and put its own directory in its place *(reproduced by the round-1 review in Docker, without root)*. So, for the home template's parent and for each share:
   - every existing directory **above** the declared path (for homes, the parent itself and everything above it; for a share, everything above the share) must be a directory, not a symlink, owned by root and not writable by group or other; the script creates missing ones as root, mode `0755`;
   - the declared path must resolve to itself (`realpath` equals the configured path);
   - a share itself, if it exists, must be a directory and not a symlink; its own owner, group and mode are not checked here, because steps 5 and 6 set them. So a share an earlier swarm run in the sample made group-writable and setgid is accepted and normalised again, whether this run declares it `rw` or `ro`;
   - an existing home is accepted only for a user the swarm's record lists (step 3), and is re-moded in step 4.

   Anything else is refused with `RuntimeError` naming the component, its owner and its mode ("/scratch is mode 1777; users isolation needs every directory above a member's home to be root-owned and not group- or world-writable"). Shares under `/tmp` are therefore refused.
3. **Group and users.** `groupadd inspect-swarm` (a fixed name). The runtime records the group, users and uids it created in the sample store under an `inspect_swarm` key; an existing group or `swarm-*` user that this record does not list was not made by the swarm, and setup refuses it. For each member, `useradd --uid <uid> --create-home --home-dir <home> -G inspect-swarm --shell <bash or sh> swarm-<member>`, then `chmod` the home as below (Debian's `useradd` creates it `0755`; `-K HOME_MODE` is not portable to older `shadow` releases). A user the record lists with the same home (a second swarm run in one sample) is reused.
   - **Uids are chosen, not allocated.** `useradd`'s first free uid is often 1000, which images commonly use for files copied in with a numeric owner. The runtime takes uids from 61000 upwards, skipping any that `getent passwd` lists or that owns a file: one `find -P / -xdev \( -uid <u1> -o -uid <u2> ... \) -printf '%U\n'` over every filesystem the census or the homes can attribute: the root filesystem, each share, the home template's parent, and each census root of step 8 (`/tmp`, `/var/tmp`, `/dev/shm`, `/run/lock`), each scanned from itself because `-xdev` does not cross into a separately mounted `/dev/shm` or home filesystem. It is repeated for replacements until a full set owns nothing. Filesystems outside those roots are not scanned, and nothing the swarm reports attributes files on them. That is one traversal of the image's files per sample, typically seconds.
4. **Private directories.** With `peers="none"`, the home is `0700`. With `peers="read"`, it is `2750` with group `inspect-swarm`, so files a member creates there inherit the group and, under the default umask, are group-readable. The script appends `export TMPDIR="$HOME/tmp"` to the member's `~/.profile` and `~/.bashrc` (after `mkdir $HOME/tmp`), so tools that honour `TMPDIR` do not collide in `/tmp` (two members writing `/tmp/out.txt` would otherwise fail with `Permission denied` because of the sticky bit). `bash()` and `python()` run login shells, which read `~/.profile`; `bash_session()` reads `~/.bashrc`; a bridged CLI's own shell may read neither, so this is a convenience, not a guarantee.
5. **Read-write shares.** The share's own directory becomes owned by root, so no member can change its mode. For every entry the traversal lists: group `inspect-swarm` (`chgrp -h`), `g+rwX`, setgid on directories, and an access and default ACL `g:inspect-swarm:rwX` with mask `rwX` (`setfacl -P`). If `git` is installed, each share and each repository found within three levels of it is added to `safe.directory` in the system git config, and found repositories get `core.sharedRepository=group`; repositories members create later need their own `safe.directory` entry in the member's `~/.gitconfig`, which the preamble mentions.
6. **Read-only shares.** No member may hold a write grant on any entry, whoever owned it before: for every entry the traversal lists, an owner that is a member's uid becomes root (`chown -h`), a group that is `inspect-swarm` or a member's own group becomes root's (`chgrp -h`), extended ACL entries are removed (`setfacl -P -b`), and the "other" bits lose `w` and gain `rX`. Owners and groups that are not members', and their bits, are left alone, so the task's own users keep their access. Member-owned entries arise in a second swarm run in one sample, or when a task shares a directory a previous run's member wrote.
7. **Self-test.** For each member, through the provider's own `exec(user=...)`, as tools will reach it:
   - `id -u` returns the member's uid and not 0; `sudo -n true`, if `sudo` exists, fails;
   - `find -P <share> -xdev -writable -print -quit` over each `ro` share prints nothing: no existing entry is writable by that member, whatever grants it (mode, group or ACL), because `-writable` asks the kernel (`access(2)`);
   - then, for one pair of members: a file the first writes in its home is unreadable by the second with `peers="none"` (readable with `"read"`) and unwritable either way; the second cannot rename the first's home; the second can append to a file the first created in each `rw` share; neither can create a file in a `ro` share.

   Any failure stops the swarm with `RuntimeError` naming the check and the path, for example "a peer could read /scratch/w-1/.swarm-probe: the filesystem at /scratch does not enforce Unix permissions (a host bind mount?)". The probe files are removed afterwards. This is the guard against the shapes the design does not support: a macOS bind mount, a provider that ignores `user`, a filesystem without ACLs, an image whose sudoers lets members become root.
8. **Baseline.** The census roots are recorded, each with one exclusion, the bridge token directory `/var/tmp/.inspect-bridge` ([Bridged members](#bridged-members)), whose files root creates and gives to a member's uid, so they are the harness's, not the member's: `/tmp`, `/var/tmp`, `/dev/shm` and `/run/lock`, each scanned from itself so that a separately mounted tmpfs (Docker mounts `/dev/shm` as its own filesystem, so `find / -xdev` never reaches it) is covered, plus any other world-writable directory `find -P / -xdev -type d -perm -0002` finds (capped at 200). A root that is absent, or that the scan cannot read, is recorded as such. The list goes, with each member's user, uid and home, into an `isolation` evidence event.

Setup takes one exec for the script, one for the uid scan, and two per member for the self-test, which run concurrently; the `ro` traversals in the self-test cost one pass of each `ro` share per member. Its commands are recorded as ordinary `SandboxEvent`s outside any member span.

### Members' commands run as their users

The member's tools are built once, so the user has to reach them at call time. The swarm cannot do that from outside without private, fragile code ([Alternatives](#alternatives-considered)), so this design asks inspect_ai for a scope, like the tool-state scope swarm.md already relies on ([Members](swarm.md#members)), as a small, general inspect_ai PR before this work (decision: Ransom, 2026-10-13):

```python
# inspect_ai/util/_sandbox/identity.py, exported from inspect_ai.util

@contextmanager
def sandbox_identity(user: str, *, cwd: str | None = None, sandbox: str = "default") -> Iterator[None]:
    """Model-facing work in `sandbox` inside this block runs as `user`, starting in `cwd`.

    Inside the block `sandbox` is also the default sandbox (as `sandbox_default()`), and
    tools that find their sandbox by injection use it. A ContextVar, so tasks started
    inside inherit it. Nested scopes raise `SandboxIdentityError`.
    """

def sandbox_identity_user(env: SandboxEnvironment, user: str | None, *, strict: bool) -> str | None:
    """The user a command in `env` runs as.

    Outside a scope, or for an environment other than the scope's: `user` unchanged.
    Inside a scope for `env`: None becomes the scope's user. An explicit user is kept when
    `strict` is False (Inspect's own launches), and raises `SandboxIdentityError` when
    `strict` is True and it is not the scope's user (a model-facing tool's configuration).
    """

def sandbox_identity_cwd(env: SandboxEnvironment, cwd: str | None) -> str | None: ...   # an explicit cwd wins

def sandbox_identity_active(env: SandboxEnvironment | None = None) -> bool: ...   # for scaffolds that must refuse

class SandboxIdentityError(RuntimeError): ...
```

- **Which sandbox.** The scope names one environment (`users(sandbox=...)`, default `"default"`). On entry it looks the name up in the sample's environments and raises if absent, makes it the default sandbox for the block (what `sandbox_default()` does, `src/inspect_ai/util/_sandbox/context.py:421-432`), and keeps the environment object, which resolution compares by identity. Tools that find their sandbox by injection (`bash_session()`, `text_editor()`) today take the first environment needing the fewest injections, independently of the default (`context.py:446-480`); inside a scope, injection targets the scope's environment only, injecting there if needed. A tool given an explicit other sandbox (`bash(sandbox="comparator")`) keeps that sandbox and that sandbox's user: the eval author's trusted services (LeanOpenProblems' `comparator`) are unaffected.
- **Model-facing tools resolve strictly.** `bash()`, `python()`, `grep()`, `list_files()`, the `read_file()` tool, `skill()`, `bash_session()` and `text_editor()` resolve their configured `user` with `strict=True` before anything else, and the exec-based ones their `cwd`. A member given `bash(user="root")`, or a peer's user, would cross the boundary the scope exists for; unlike an explicit tool-state instance, which is a deliberate channel, there is no legitimate reason for it, so it raises `SandboxIdentityError` naming the tool's user and the scope's, and the sample ends with that error at the first call rather than silently running as root.
- **Inspect's own launches resolve leniently.** `tools_user_param()` (`src/inspect_ai/util/_sandbox/_cli.py:36-49`) and `exec_remote()` use `strict=False`: an omitted user becomes the scope's, before the default exec identity, and an explicit one is kept. That is what lets `sandbox_agent_bridge()` start its model proxy as the tools user (`exec_remote(..., user=sandbox_env._tools_user)`, `src/inspect_ai/agent/_bridge/sandbox/bridge.py:245-257`) inside a member's scope, while the model service stays in the member's context and the CLI runs as the member. A raw `exec_remote()` with no user, from a scaffold, runs as the member.
- **`bash_session()` checks every reuse.** It resolves its user only when it creates the shell; later calls reuse the shell its store entry names (`src/inspect_ai/tool/_tools/_bash_session.py:195`, `:237`). So the store entry also records the identity the shell was created as, and inside a scope every call compares it with the scope's user and raises `SandboxIdentityError` on a mismatch: "bash_session instance 'team' belongs to swarm-w-1; under users isolation a shell cannot be shared between members". This refuses a deliberately shared `bash_session(instance="team")` and a session a setup solver opened at an explicit instance before the swarm; the default instance is already private to each member through the tool-state scope. Outside a scope there is no check, so sharing a shell between agents stays possible everywhere else.
- **`computer()` and `web_browser()`**, which cannot run as a user, raise `SandboxIdentityError` inside a scope for their sandbox instead of running as root.
- **What it does not touch.** `SandboxEnvironment.exec()`, `read_file()` and `write_file()` themselves are unchanged. Inspect's own commands (tool injection, root probes, checkpoint snapshots) pass explicit users or none and keep working, and so do scorers that call `exec()`, including a member's in-loop scoring: a scorer is trusted task code and keeps the sandbox's default user. Outside a scope nothing changes, so no existing eval is affected.
- **Custom tools** that call `sandbox().exec()` directly run as the sandbox's default user, as they do today, unless they resolve their user through `sandbox_identity_user(..., strict=True)`. This is the same contract as custom store keys and `tool_state_instance()`. The swarm cannot see them, so `member()`'s documentation states it, and the runtime reports what it can see: after a member ends, it counts the member's `SandboxEvent` execs (their options record the user, `events.py:85-91`) that ran with no user or another user, excluding the injected tools' own launcher, which runs as the tools user and switches per call. That count, `foreign_execs`, is a harness-validity diagnostic in the member's record, not a guarantee: `read_file()` and `write_file()` record no user at all.

The runtime enters, for each member, its tool-state scope and `sandbox_identity(f"swarm-{name}", cwd=home, sandbox=isolation.sandbox)`, around the member's `run()`.

#### Bridged members

inspect_swe does work of its own before it launches a CLI, and every member shares the sandbox's loopback, where today any process can call any bridge's model routes (spending in that member's name) and its bridged-tools endpoint ([Bridged agents](#bridged-agents)). Bridged members under users isolation therefore need the following, all scoped to the identity scope so that nothing changes for bridges outside it:

- **inspect_swe resolves the user once.** A supported agent takes `user = sandbox_identity_user(sbox, user, strict=True)` and `cwd = sandbox_identity_cwd(sbox, cwd)` at the start of its invocation, before writing its config and resolving its working directory, so its config lands in the member's `$HOME` and the CLI starts in the member's home. Without it, `tools_user_param()` would launch the CLI as the member while its settings were written as root, and the CLI would not find them.
- **A bridge started inside a scope requires a token, minted in the sandbox.** When `sandbox_identity_active()` is true for its sandbox, `sandbox_agent_bridge()` tells the proxy, through its launch environment, the member's user name and a token path, and nothing secret. The proxy, running as the tools user (root), generates a random 256-bit token, hex-encoded (64 characters), at startup and writes it to `/var/tmp/.inspect-bridge/<instance>`: a file created with `O_EXCL|O_NOFOLLOW`, owned by the member's uid, mode `0400`, inside a directory owned by root, mode `0711`, that the proxy creates or adopts under the guest verified-directory contract of inspect_ai's trust model (`src/inspect_sandbox_tools/src/inspect_sandbox_tools/_util/server_dir.py`), so a member cannot plant or redirect it. The proxy refuses every request without the token with HTTP 401 and a body saying what is required ("this bridge runs under users isolation; read the token from the file named by SandboxAgentBridge.token_file"). It accepts the token where each supported dialect sends a key: `Authorization: Bearer` (OpenAI Responses and Chat Completions, Anthropic's auth-token form, and MCP), `x-api-key` (Anthropic Messages) and `x-goog-api-key` (Google), compared in constant time. `SandboxAgentBridge.token_file` gives scaffolds the path, and the bridge's MCP server configs (`MCPServerConfigHTTP`, which has a `headers` field, `src/inspect_ai/tool/_mcp/_config.py:50`) carry `Authorization: Bearer ${INSPECT_BRIDGE_TOKEN}`, a placeholder the CLI expands from its environment. A bridge outside a scope mints no token and needs none, as today, so existing scaffolds, Inspect's examples and the documented custom-scaffold recipe, which send dummy keys, keep working.
- **The token is ready before the bridge yields, and lives only as long as the bridge.** `exec_remote()` returns once the proxy process exists, not once it serves (`src/inspect_sandbox_tools/src/inspect_sandbox_tools/_remote_tools/_exec_remote/_job.py:100-123`), and inspect_swe's MCP readiness gate cannot stand in for that, because it is skipped for bridges without bridged tools (`inspect_swe: src/inspect_swe/_util/mcp_ready.py:184-201`). So:
  - the proxy writes the token to a temporary file in the token directory and renames it into place only after its listener is bound, so the file at `token_file` is either absent or complete;
  - `sandbox_agent_bridge()` does not yield until a host check, run as the tools user, sees that file with exactly the token's length (`test -f` and `wc -c`, which read no content), polling inside `anyio.fail_after(60)` so cancellation and the sample's limits interrupt it; if the proxy exits first (the existing proxy monitor) or the bound expires, the bridge raises with the proxy's stderr, which holds no token, and the member errors as for any failed bridge start;
  - the CLI's wrapper reads the file with a check (`t="$(cat "$1")" && [ "${#t}" -eq 64 ] || { echo "bridge token unavailable: $1" >&2; exit 1; }`), so a missing, unreadable or short file fails the launch rather than exporting an empty key;
  - the proxy unlinks the file when it exits, and the bridge's cleanup, which already kills the proxy in a shielded scope (`src/inspect_ai/agent/_bridge/sandbox/bridge.py:270-273`), also removes it as root, so no token outlives its bridge.
- **The token never crosses the host channel.** Because the proxy mints it inside the sandbox, the host never holds it: no `exec()` argument, stdin, environment or JSON-RPC request the host sends contains it, so no `SandboxEvent`, recorded RPC request, transport or provider error message, trace line or Kubernetes diagnostic can carry it into the eval log, whatever the provider. That is why the host does not generate it: redacting it after the fact would mean finding every path that formats a request into an error, including the JSON-RPC transport's failure description (`src/inspect_ai/util/_sandbox/_json_rpc_transport.py:94`, `:134`), generic RPC error formatting (`src/inspect_ai/_util/_json_rpc.py:300`, `:337`) and inspect_k8s_sandbox's exec error context (`src/k8s_sandbox/_sandbox_environment.py:288`, `:380`, `:396`), and suppressing events with `SandboxEnvironmentProxy.no_events()` is unsafe under concurrency, because it toggles one flag on the shared environment (`events.py:220-226`) and would drop other members' events.
- **The member reads it from the file, never from a command line.** Peers can read every command line *(spike)* but not another uid's `/proc/<pid>/environ` *(spike)* or the `0400` file. For the two supported agents, inspect_swe:
  - launches the CLI through its existing `bash -c` wrapper, extended to `export INSPECT_BRIDGE_TOKEN="$(cat "$1")"` before `exec`, with the token file's path, not the token, as the argument;
  - for Claude Code, writes `apiKeyHelper` as `cat <token file>` instead of today's `echo <key>` inside a `bash -c` argument (`inspect_swe: src/inspect_swe/_claude_code/claude_code.py:676-699`), and takes the MCP header from `${INSPECT_BRIDGE_TOKEN}`, which Claude Code expands in MCP headers per its documentation;
  - for Codex, points the provider's `env_key` (`codex_cli.py:625`) at `INSPECT_BRIDGE_TOKEN` and the MCP server's `bearer_token_env_var` at the same variable, per its documentation;
  - runs its MCP readiness probe, which today sends `curl` through `exec()` with no credentials (`inspect_swe: src/inspect_swe/_util/mcp_ready.py:188-240`), as the member, building the header inside the sandbox from the file (`printf 'Authorization: Bearer %s\n' "$(cat "$1")" | curl -H @- ...`), so neither its argv nor its stdin as sent by the host holds the token.

  Step 3 confirms each CLI's variable expansion on the pinned versions; where one cannot read the variable, it gets a config file written by the member's own launcher, `0600` in a `0700` directory whatever `peers` is.
- **Unsupported scaffolds refuse.** `claude_code()` and `codex_cli()` are supported headless, as members run them. Their `centaur` option, which runs the CLI under Inspect's human agent instead (`inspect_swe: _claude_code/claude_code.py:461-468`, `_codex_cli/codex_cli.py:661-669`), exports its environment through a bashrc and connects as the sandbox's default user (`_util/centaur.py:25-31`; `inspect_ai: src/inspect_ai/agent/_human/agent.py:57`, `:79`), bypassing both the identity and the token wrapper, so either agent with `centaur` set raises inside a scope before any setup ("claude_code(centaur=...) does not support swarm users isolation"); interactive members would be a separate integration. Outside a scope `centaur` is unchanged. inspect_swe's `gemini_cli`, `opencode`, `kimi_code` (whose config holds a literal key), `mini_swe_agent`, `antigravity` and its ACP adapters raise inside a scope ("gemini_cli does not support swarm users isolation"), each until its identity and token handling is added. A custom scaffold gets the 401 above on its first request. ACP agents are not M1 members anyway (swarm-api.md, [Members](swarm-api.md#members)).
- **One bridged agent type per sandbox, until inspect_swe's ports are fixed.** inspect_swe gives each agent type its own port counter starting at 3000, so the first Claude Code member and the first Codex member in one sandbox both bind 3001 (`claude_code.py:311-314`, `codex_cli.py:356-359`). That collision exists in the shared default too and is not this design's to fix ([Not this design](#not-this-design)); until a single counter across types lands in inspect_swe, a swarm mixes bridged agent types only across sandboxes, and the tests below run Claude Code and Codex members in separate samples.

Until these land, `swarm()` cannot tell a bridged member from a native one (the member's agent may be a builder), so the restriction is documented on `users()`: bridged members under users isolation need inspect_swe and inspect_ai at or after those changes.

### Working directories

- **One private directory per member, which is its `$HOME`.** `/scratch/<member>` by default. Making it the home means per-user caches and configuration (`pip --user`, `~/.cache`, `~/.claude`, Codex's `.codex` under the working directory) are private without further setup, and shared caches cannot form between members by accident. It is created by `useradd`, so it starts with the image's `/etc/skel`.
- **Handed to the member three ways.** The scope's `cwd` makes the exec-based tools and bridged CLIs start there; `$HOME` points there; and the preamble names it. `bash_session()` starts its shell in the tools server's directory, as today; the preamble tells the member to `cd`.
- **Per-member git worktrees** are a task or prompt convention on top: a member runs `git worktree add ~/repo -b swarm/<member>` against a repository in a `rw` share, as the spike did. Its working tree is private; its branch is not (below).
- **A task that needs members to start somewhere else** (a SWE task whose repository is `/testbed`) shares that path as `rw` and says so in the task prompt; the private directory stays the scratch space.

### Shared directories

| What | How | Notes |
|---|---|---|
| A shared working area or repository | `Share(path, "rw")` | Every member can create, change and delete everything in it, including peers' files: the default ACL keeps new files group-writable whatever a member's umask. A shared git repository's refs are shared too, so any member can move any branch. |
| Read-only task inputs | `Share(path, "ro")` | Members read; nobody but root writes. |
| The filesystem channel's directory and notes file | the `rw` share named by `filesystem(shared=...)` | Unchanged convention; the notes file is append-only by convention only. |
| A shared integration branch | a branch in a repository in a `rw` share | Members merge into it; `materialize` and scorers read it as today. |
| Everything else | Unchanged | System directories stay root-owned and unwritable. World-writable directories (`/tmp`, `/var/tmp`, `/dev/shm`) stay world-writable, and are the main unsanctioned file channel ([Security](#security)). |

#### Peers' access to private directories

- `peers="none"` (default): only the member and root.
- `peers="read"`: every member may read every other member's private directory, not write it. It is enforced by group mode and setgid, so a member can still make its own files private (`chmod`, `umask 077`); a member's own files are its to share or withhold.
- **A coordinator only** is not offered: coordinator topologies are later work (swarm.md, [Implementation plan](swarm.md#implementation-plan)). When they exist, a coordinator's read access is an ACL entry `u:swarm-<lead>:rX` with a default ACL on each private directory, which the same setup step can add, and it would be a third value, `peers=["lead"]`. Like `"read"`, the owner could remove it.

### The preamble

The runtime adds a fixed paragraph to each member's preamble (swarm-api.md, `start()`), after the channels' instructions. Its only variables are the member's user name, its uid-free home path, and share paths and modes, all from the eval author's configuration:

> You run as the Linux user `swarm-w-1`, without root. Your private directory is `/scratch/w-1` (your `$HOME`); other members cannot read it. Shared with all members: `/shared` (read and write), `/data` (read only). Use `$TMPDIR` for temporary files. Processes you leave running are stopped when you finish.

With `peers="read"`, the second sentence says other members can read it but not change it. Members are not told what is not isolated; the eval author decides whether a prompt mentions it, because telling them is itself a treatment.

### Processes a member leaves running

- **When a member ends**, for any reason (submitted, stopped, its own limit, cancelled or errored), the runtime kills every process in the sandbox whose real uid is the member's. It is one `privileged_shell()` as root, with `timeout=30` and `timeout_retry=False` (the helper's default timeout is `None`, `src/inspect_ai/util/_sandbox/_privileged.py:178-190`). The script scans `/proc/[0-9]*/status`, re-reads each matching pid's `Uid` line immediately before sending it `SIGKILL`, and repeats until a scan finds none or five rounds have run; it prints the number killed, the survivors and their names. The call runs in a shielded cancel scope bounded by `move_on_after(40)`, so a hard stop's cancellation does not skip it and a stalled provider cannot hold the stop indefinitely.
- **Failure contract.** Reaping never raises and never replaces the member's own outcome: it runs in a `finally` after the member's status and any exception are recorded, its own errors are caught, and the member's error, if any, propagates unchanged. Its result is recorded on the member as `reap.status`: `ok` (nothing left), `survivors` (processes outlived five rounds; their count and names), `timeout` (the exec or the 40 s bound expired) or `error` (the provider failed; its message). Any status but `ok` sets `isolation_incomplete` in the sample's swarm metadata and fails the `reap_complete` harness-validity check (swarm.md, [Observer](swarm.md#observer-evidence-accounting-and-metrics)). The sample is not failed and the task's scorer still runs, because a survivor is a measurement problem, not a broken run; analyses of artifact or containment results exclude or report samples with `isolation_incomplete`, as they do other harness-validity failures. The promise is therefore "nothing a member starts outlives it, unless its record says so".
- **Why kill rather than wait** (decision: Ransom, 2026-10-13, over reaping only at the drain). swarm.md's ownership boundary says the swarm owns everything a member starts; today it cannot reach sandbox processes, which are "part of the filesystem observation limit" ([Members](swarm.md#members)). A uid per member makes them reachable. A member that ended has no further turns, so nothing it started should keep changing the shared state or using the sandbox's CPU. An eval that wants a long-running shared service starts it from the task, as root or as its own user, not from a member. If a task ever needs a member-started service to outlive its member, a `reap="drain"` field on `users()` can be added then.
- **What is recorded.** The number of processes killed per member, and their executable names (`/proc/<pid>/comm`, at most 16 bytes each, at most 50 names, recorded as JSON data), in the member's record: a count of work that outlived the member's tool calls, which the shared default cannot see at all.
- **The drain** reaps every member still running before finalisation, after the limits deep dive's stop procedure, with the same contract.
- **Bridged members.** The CLI and everything it started run as the member and are reaped with it. The bridge's model proxy runs as the tools user and is stopped by the bridge itself (`bridge.py:270-273`).
- **Native members' shells.** A member's `bash_session()` shell runs as the member and is reaped with it; the session state in the store is the member's own (tool-state scope), and a shell shared through an explicit instance is refused inside the scope ([Members' commands](#members-commands-run-as-their-users)), so no other member reuses it.
- **Limits.** Per-uid scanning stays inside the sandbox's PID namespace, so it never touches another sample's processes, unlike `RLIMIT_NPROC` *(spike)*. It is not atomic: a process that forks between scans survives a round, which the repeat bounds and the record reports. The cgroup rung would make it atomic (`cgroup.kill`).

### What the topology makes measurable

The shared default observes the filesystem only through tool calls. Users isolation adds evidence that does not depend on them, written as `InfoEvent`s with `source="inspect_swarm"` and a versioned payload, as swarm.md's evidence is ([Observer](swarm.md#observer-evidence-accounting-and-metrics)):

- **File ownership census at finalisation**, after the drain: for each `rw` share and each census root recorded at setup (`/tmp`, `/var/tmp`, `/dev/shm`, `/run/lock` and the other world-writable directories, each scanned from itself), the count and total size of files owned by each member's uid, and by anyone else, collected with `find -P <root> -xdev ... -printf` as root, pruning the bridge token directory (`-path /var/tmp/.inspect-bridge -prune`; its files are root-created credentials, and normally already removed when their bridges ended), and capped at 10,000 entries per root (the cap is reported when hit, and so is a root that is absent or unreadable). A file's owner is its creator, including a background process that created it between tool calls. It is residual ownership evidence: it does not see reads, modifications, deletions, or content passed through a file that no longer exists, and hard links can count one file's size twice, so it is not a bound on how much members communicated.
- **Unsanctioned file channels, by owner.** A member's files in `/tmp`, `/var/tmp` or `/dev/shm` are files it put where every member can read them, outside the shares the eval author sanctioned. The census reports them per member and per root, with up to 50 paths each as data, including `/dev/shm`, which Docker mounts as its own filesystem. This is the containment probe swarm.md asks for, for the filesystem part.
- **Private work is attributable to its owner, not proven to be its author.** A peer cannot create, rename or delete entries in a member's private directory, so a per-member artifact read from there is "what that member left", not "what was on its branch" ([Scoring](#scoring)). It does not prove that only the member wrote the contents: a member can expose its own file, for example by hard-linking it into a `rw` share where it is group-writable, let a peer change it, then remove the link and make the file `0600` again; the result has one link, the member's uid and no group or other write bit, and contains the peer's write (the round-2 review reproduced this in Docker). A snapshot cannot see that history. Checking that a file has one link and no group or other write bit tells only that no peer can write it now.
- **Reaped processes per member**, above.
- **`foreign_execs` per member**, above.

What it still cannot see: reads (nothing records who read a file), modifications of existing files, traffic over localhost TCP and abstract Unix sockets, and process command lines read by peers. Experiments that need those add sandbox instrumentation (Not this design) or use per-member sandboxes.

### Arms and identity

- `isolation` is an argument of `swarm()`, so it is in the plan step's `params_passed`, and `task_identifier()` hashes it: two arms that differ only in `isolation`, or in any field of `UserIsolation`, are distinct in an eval set ([swarm-api.md Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)). The string `"users"` and the model `users()` are logged differently but mean the same, as with channel names.
- **Isolation changes privilege, not only isolation.** In the shared default, members run as the sandbox's default user, root in most images. Under users isolation they are non-root: they cannot `apt-get install`, write system directories or `pip install` without `--user`. Comparing `isolation=None` against `"users"` therefore changes two things at once. An experiment whose question is isolation runs its baseline at the same privilege: a swarm of one under `users()` for the single-agent arm (swarm-scoring.md's `baseline()` is a swarm of one, so it takes the same argument), and, for a "members see everything" arm, `users(peers="read")` with the working area as a `rw` share. Tasks that need root during the solve are unsuitable for users isolation, and the self-test cannot detect that; `users()`'s documentation says so.
- **The record.** Each member's `MemberResult` (swarm-scoring.md, [What the swarm records](swarm-scoring.md#what-the-swarm-records)) gains `user`, `uid` and `home`, and the per-member counts above; the swarm's metadata records the isolation summary and the self-test's results.

### Scoring

- **The task's scorer** runs after the drain, outside every member scope, as the sandbox's default user. It sees shares and private directories as root, so shared-artifact tasks score the team's environment as in M1. A scorer that reads a path is unaffected by the group and ACL changes setup made, because it runs as root.
- **In-loop scoring** (a member's `react(attempts=...)` calling the task's scorer) also runs as the default user when the scorer calls `exec()` directly, as scorers normally do, because the scope changes only tools. A scorer that calls a built-in tool would run it as the member. A member therefore cannot use a scorer to read what it cannot read itself, except through what the scorer reports.
- **Per-member artifacts** become attributable. swarm-scoring.md's `member_artifacts.materialize()` is task code; under users isolation it can read a member's artifact from that member's private directory (its worktree), whose entries only that member could create. A `materialize` may also check that each file it reads has one link and no group or other write bit, and report the files that do not; that is a check of current exposure, not of who wrote the file earlier, because a member can share its file with a peer and take it back ([What the topology makes measurable](#what-the-topology-makes-measurable)). Branches in a shared repository stay writable by every member, so a `materialize` that wants attribution reads the private worktree's files, not the branch ref. Neither changes the result contract.
- **LeanOpenProblems** scored each member's own sandbox. Users isolation does not give that topology: their members shared nothing on disk, so per-member sandboxes ([recommendation 1](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field)) remain their fit.

### Limits

- **No change to the budget.** Users isolation changes no limit node, cap, share or the ledger (swarm-limits.md). Setup, the self-test, reaping and the census make no model calls.
- **Time.** Setup runs before the controller and counts towards the sample's time and working limits; it is a few execs. Reaping runs inside each member's end and inside the drain, and is bounded by its 40 s wall-clock bound.
- **No per-member resources.** Members share the sandbox's CPU, memory and process table. A member that exhausts memory can cause the kernel to kill a peer's process. Per-member limits need cgroups (Docker only, with a container flag) and are a later rung; rlimits are not used because `RLIMIT_NPROC` is global per uid across containers *(spike)*. The sandbox is sized for the member count, as swarm.md already says.

### Later rungs (surveyed, not designed)

Each is an optional later step with a container requirement the task states in its compose or values file. The runtime would probe for the capability at setup and refuse the rung with a message naming the missing flag, as the self-test does for the first rung. None is designed until an experiment needs it.

| Rung | What it adds | What it needs | Trade-off |
|---|---|---|---|
| `processes="hidden"` | Peers' processes and command lines invisible | `CAP_SYS_ADMIN` to remount `/proc` with `hidepid=invisible` | `SYS_ADMIN` in a container running untrusted code as root; not available from a compose file on Kubernetes |
| Namespaced commands (bubblewrap) | Private `/tmp`, PID, IPC and optionally network per command | `bwrap` in the image; `seccomp=unconfined` and `systempaths=unconfined` on Docker | Disables Docker's seccomp filter for the whole container. Every member command is wrapped, which also needs inspect_ai's injected tools to wrap theirs. Per command, not per member: a member's two commands do not share a `/tmp` unless it is bound from its home |
| Per-member resources | Memory, CPU and process limits; atomic reaping | `writable-cgroups=true` (Docker 28) | Docker only; gVisor does not enforce in-sandbox limits. The runtime must keep members out of their own groups' control files |
| Per-member network rules | TCP and UDP rules by uid, on loopback (peers' ports, bridge proxies) as well as egress | `NET_ADMIN`, `iptables` | Not Unix sockets; not available from a compose file on Kubernetes |
| Landlock | Unprivileged file scoping, and from ABI 4 and 6 scoping of TCP connects, abstract sockets and signals | A host kernel with Landlock; a launcher in the injected tools | No container flag, but unavailable on Docker Desktop and gVisor, so a task using it would not run on the providers most evals use |

The cheapest rung that removes most of the remaining channels is Landlock where the host has it; the most portable is a sandbox per member.

## Alternatives considered

**A sandbox per member** (Inspect's named sandboxes; `member(sandbox=...)` in [swarm-lean-open-problems.md](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field)).

- Real separation: process tables, `/tmp`, network, resources and root are all per member, enforced by the container runtime.
- N containers per sample, sized for one agent each: 32 copies of a 32 GiB container per sample in LeanOpenProblems' flagship config. No shared files unless the task adds a shared service.
- The right choice when an arm must match independent attempts (the k-epochs comparison), for a "no communication" arm, for containment baselines that need a real boundary, and when members need resource limits or root.

**Private sandboxes plus a shared volume** (compose services per member with one named volume mounted in each).

- Container-level separation with one explicit shared directory, which is what an arm with "files as the only channel" wants.
- Every member is root in its own container, so the shared volume has no write scopes unless each container also runs its agent as a user, and the uids in different containers must be agreed for ownership to mean anything. On Kubernetes a volume shared across pods needs a `ReadWriteMany` storage class; inspect_k8s_sandbox's chart support for that was not checked.
- Right for messages-or-files-only arms that need process and network separation; heavier than users isolation, lighter than nothing shared.

**Conventions only** (today's default).

- Free, and members keep root, which many tasks need.
- No enforcement and no attribution beyond tool calls.
- Right when sharing is the point (the C compiler's and Test-Time Communication's setups) and the experiment does not measure isolation or per-member artifacts.

**Users isolation (this design)** is right for shared-artifact tasks with private scratch and per-member work (SWE-style tasks with per-member worktrees), for role-based write scopes (SCHEME), and as a cheap intermediate rung in containment experiments, when members do not need root.

**Redirect members' tools from inspect_swarm alone**, by resetting inspect_ai's private `sandbox_environments_context_var` per member to wrappers that fill in `user` and `cwd`, as LeanOpenProblems reset it to choose sandboxes.

- No inspect_ai change.
- The wrapper sees only `exec()`. Injected tools decide their user from per-object state (`_tools_user`, `_tools_default_user`, `environment.py:192-201`) and run their own launcher as root with the target user in the request (`_text_editor.py:125-136`), so a wrapper either leaves `bash_session()` and `text_editor()` running as root or breaks them. Defaulting `exec(user=None)` to the member would also make the injection's default-identity probe record whichever member injected first as the sandbox's default user (`sandbox.py:434-450`), for every member, and a wrapper that is not a `SandboxEnvironmentProxy` makes the root-access probe run again after agents have run (`sandbox.py:358-372`), which it is designed to avoid. It also changes scorers' identity when they run inside a member.
- Rejected (decision: Ransom, 2026-10-13): the scope is a small, general inspect_ai change at the points where tools already resolve their user; the wrapper depends on private state in several places.

**Make the swarm build per-member tools** (a member builder that takes a user, with `bash(user=...)` per member).

- No inspect_ai change for native tools.
- The member contract builds one agent for all copies and samples, so each copy would need its own record; it does not reach deepagent's subagents' tools, inspect_swe's install steps, or tools a builder creates inside its invocation.
- Rejected for the same reason the tool-state scope was chosen over refusing shared tools (swarm.md, [Alternatives](swarm.md#alternatives-considered)): the common configuration should not need a workaround.

**Apply the identity in `SandboxEnvironment.exec()` itself** (every exec with no user inside a scope runs as the member).

- Also covers custom tools that call `exec()` directly.
- It also catches Inspect's own commands and scorers, which issue `exec(user=None)` to mean the default user (`_privileged.py` runs host-issued commands "as the sandbox default user"), and it cannot tell them from a custom tool's. Rejected; custom tools opt in, and `foreign_execs` shows those that did not.

**Require the bridge token for every bridge**, not only inside a scope.

- One code path, and a shared-default swarm would also be protected from a member calling a peer's bridge.
- It breaks every client that sends a dummy key today: Inspect's runnable Claude examples (`examples/http_proxy/claude.py:18`, `examples/evals_in_eval/claude.py:18`), the custom-scaffold recipe in `docs/agent-bridge.qmd`, and inspect_swe's agents other than the two this design integrates, ACP adapters included. That is a migration of its own, outside this topology. Rejected here; listed under Not this design.

**Namespaces from the start** (bubblewrap around every member command).

- Closes `/tmp`, the process table and, optionally, the network in one step.
- Needs `seccomp=unconfined` on Docker, which weakens the boundary between untrusted code and the host kernel for every process in the container, and wrapping inside inspect_ai's injected tools. Kept as a later rung.

**Linux users without ACLs**, relying on setgid directories and a shared group.

- No `acl` package in the image.
- New files are created `0644` under the default umask, so peers cannot edit each other's files in a shared repository *(spike)*; forcing a umask on every member process is not possible for bridged CLIs' own shells. Rejected for `rw` shares; `ro` shares and `peers="read"` need no ACLs.

## Compatibility and migration

- **inspect_swarm.** `isolation=None` is the default, so existing swarms are unchanged. `users()`, `UserIsolation` and `Share` are new public names. `MemberResult` gains optional fields, which the record's `version` covers.
- **Eval logs.** Only existing event types: `SandboxEvent`s for setup and reaping, `InfoEvent`s for evidence, sample metadata. No schema or viewer change.
- **inspect_ai.** `sandbox_identity()`, `sandbox_identity_user()`, `sandbox_identity_cwd()`, `sandbox_identity_active()`, `SandboxIdentityError` and `SandboxAgentBridge.token_file` are additive. Outside a scope every resolution returns its argument unchanged, injection chooses its sandbox as today, `bash_session()` performs no identity check, and a bridge requires no token, so behaviour is identical for every existing eval, scaffold and example, including clients that send dummy keys (Inspect's `examples/http_proxy/claude.py` and `examples/evals_in_eval/claude.py`, the custom-scaffold recipe in `docs/agent-bridge.qmd`, and inspect_swe's other agents). Inside a scope, `computer()`, `web_browser()`, a shared `bash_session()` and an explicit foreign user raise. A test runs an unscoped bridge with a dummy-key client to pin this. Making bridge authentication the default for every bridge would be a separate change with its own migration, not part of this design.
- **inspect_swe.** Resolving the user and cwd through the scope, the token handling, and the refusals change nothing outside a scope. The token needs an inspect_ai that provides one; inspect_swe tracks inspect_ai `main`.
- **Tasks and images.** Users isolation changes the sandbox it runs in for the rest of the sample: new users and a group, group and ACL changes on `rw` shares, owners, groups, ACLs and "other" bits normalised on `ro` shares, `safe.directory` entries in the system git config. Scorers running as root are unaffected; a scorer that runs commands as the image's own non-root user may find files in `rw` shares owned by members (group-writable, so still writable by `inspect-swarm` members only). Images need `useradd`/`groupadd`, GNU `find`, and the `acl` package for any share; the default Inspect image and `python:3.12-bookworm` lack `setfacl`, so a task with shares installs `acl` in its Dockerfile. Shares and homes must sit under root-owned directories that are not group- or world-writable, so a share under `/tmp` is refused.
- **Providers.** Docker is supported. inspect_k8s_sandbox's default chart is supported for users, homes and the self-test; whether shares work depends on whether the filesystem under them keeps ACLs in gVisor, which is unverified and which the self-test checks. `local` is refused. Other providers work if their `exec(user=...)` switches users and their filesystems enforce permissions, which the self-test checks.

## Security

**What it guarantees**, for members that do not get root and do not run code a peer wrote:

- A member's private directory is unreadable (with `peers="none"`) and unwritable by peers, and peers cannot rename or replace it, because setup refuses homes under directories members can write. `ro` shares are unwritable by any member, checked per member over every existing entry.
- A member cannot signal, trace, or read the environment, working directory or open files of a peer's processes *(spike)*.
- Every file a member creates carries its uid; members cannot give files to another uid. Member uids own no file in the image when setup starts.
- Nothing a member starts in the sandbox outlives it, unless its record says reaping found survivors, timed out or failed ([Processes](#processes-a-member-leaves-running)).
- A member's tools cannot be configured to run as root or as a peer inside its scope, and a shell cannot be shared between members.
- A bridged member's model proxy and bridged tools are reachable only with its token, which is minted in the sandbox, readable only by the member's uid and root, and never sent through the host, so it cannot appear in a command line, the eval log, an error message or a peer-readable file (after the inspect_ai and inspect_swe changes).

**What it does not guarantee:**

- **Containment.** All members and root share one kernel and one sandbox. A member that becomes root (a kernel exploit, a misconfigured setuid binary, `sudo` in the image) defeats every boundary here. The self-test refuses images where `sudo -n` succeeds and members' uid is 0, but cannot find every route. The default images' setuid binaries (`su`, `passwd`, `chfn`, `chsh`, `gpasswd`, `newgrp`, `mount`, `umount`, `ssh-keysign`) are the stock set; new users have locked passwords, so `su` fails *(spike)*. This is boundary A of inspect_ai's trust model, with N agent principals instead of one, and its "co-resident process under a different uid" position applies between members.
- **Running a peer's code.** In a `rw` share any member can plant code a peer later runs as itself: a Makefile, a test, a virtualenv, a git hook or `core.fsmonitor` in a shared repository's config. That code runs with the victim's uid and can read the victim's private directory. In shared-artifact tasks members run each other's code as a matter of course, so private-directory confidentiality against a hostile peer holds only for members that never execute shared code. `safe.directory` is set only for the shares the eval author listed, so a repository a member plants in `/tmp` is still refused by git for others.
- **Visible process table.** Peers see each other's command lines; a secret on a command line is visible to all. This is why the bridge token is read from a file inside the CLI's own wrapper and never passed as an argument.
- **Shared transient state.** `/tmp`, `/var/tmp` and `/dev/shm` are world-writable; files there under umask `022` are world-readable. Abstract Unix sockets and localhost TCP ports are shared, so a member can serve and another connect *(spike)*. Path sockets created under umask `022` refuse peers *(spike)*. These are the unsanctioned channels this rung leaves open; the census sees the files, not the sockets.
- **Shared-share integrity.** In a `rw` share, any member can change or delete any file, move any branch of a shared repository, and remove its own files' ACL entries. A member's own files are its own to expose, including by hard-linking them into a share.
- **Resource exhaustion.** No per-member CPU, memory or process limits.
- **The harness reads everything.** `read_file()` on Docker goes through the daemon, and the swarm's own setup, reaping and census run as root. Built-in tools never call `read_file()` or `write_file()` on a member's behalf, so this is not a member capability; custom tools that do bypass the boundary, and `foreign_execs` does not count them.
- **Explicitly named other sandboxes.** A member tool built with an explicit other sandbox (`bash(sandbox="comparator")`) runs there as that sandbox's user. Such a service is the eval author's trusted component, outside this topology.

**Untrusted input that reaches the new code:**

- **Paths and names in the configuration** come from the eval author, are validated at construction (absolute, no `..`, newline or NUL, member names in a fixed grammar), and reach the setup script only as positional arguments.
- **The sandbox's existing contents** may have been shaped by an agent before the swarm starts (a setup solver, or an earlier agent in the sample). Setup runs through `privileged_shell()` with a pinned `PATH`; traverses with `find -P -xdev` and changes only the entries it lists, with the no-follow forms (`chown -h`, `chgrp -h`, `setfacl -P`; `chmod` is applied to listed non-symlink entries only); refuses unsafe ancestors, symlinked paths, and an existing `inspect-swarm` group or `swarm-*` user its record does not list; chooses uids that own nothing; and checks the result with the self-test rather than trusting the commands' exit codes.
- **Files members create** reach the census, which runs as root after the drain over member-writable directories. It runs `find -P -xdev` from each root (no symlinks followed, no other filesystems), reads only metadata, caps entries, and records paths as JSON strings in an `InfoEvent`, never as Markdown and never interpreted. Paths can contain any byte but NUL; they are recorded with invalid UTF-8 replaced.
- **Processes members start** reach reaping, which reads only `/proc/<pid>/status`'s `Uid` line and `/proc/<pid>/comm`, and kills by pid as root. A pid that exits and is reused between the re-read and the kill could be a non-member process; the re-read immediately before each kill narrows that window to the kernel's pid-reuse latency, and the pid space of a sample sandbox makes reuse within it unlikely.
- **Requests to a bridge proxy** from any process in the sandbox are refused without the token inside a scope; the token is compared in constant time.
- **The token directory.** The proxy runs as root and writes the token file, so its path must not be one a member can influence: it lives in a root-owned `0711` directory under `/var/tmp` that the proxy creates or adopts only under the trust model's guest verified-directory contract, the file is created with `O_EXCL|O_NOFOLLOW` and then given to the member's uid, and the member's user name it is given resolves through `/etc/passwd`, which members cannot write.

**How this bears on swarm.md's questions.**

- **Unsanctioned channels.** Users isolation turns the filesystem part of "do agents build channels we did not give them?" into an owner census with a sanctioned/unsanctioned split, but leaves sockets and process listings open. It is an intermediate rung: shared default, users, users with a later rung, a sandbox per member.
- **Containment baselines.** A "no communication" arm under users isolation is not isolated: `/tmp`, sockets and command lines remain. It needs per-member sandboxes. What users isolation offers there is a measurement of how much members used the file channels that remain.
- **Messages as the only channel.** As swarm-communication.md says, an arm meant to have messages as its only channel needs filesystem isolation; `users(shared=())` with `peers="none"` removes the sanctioned file channel but not the world-writable directories or sockets, so it reduces and measures, but does not remove, the alternatives.

## Testing

In `tests/`, following swarm.md's testing split.

- **inspect_ai (its own suite, no Docker):**
  - Resolution: outside a scope `sandbox_identity_user()` and `sandbox_identity_cwd()` return their argument; inside a scope for an environment, `None` becomes the scope's user and cwd, an equal explicit user is kept, another explicit user raises with `strict=True` and is kept with `strict=False`; for a different environment the argument is returned; nested scopes and an unknown sandbox name raise; a task started inside the scope inherits it.
  - Tools, with stub `SandboxEnvironment`s that record calls: `bash()`, `python()`, `grep()`, `list_files()`, `read_file()` and `skill()` pass the scope's user and cwd and raise for `user="root"`; `text_editor()` and `bash_session()` send the scope's user as their run-as parameter; `exec_remote()` sends it and the cwd for an omitted user and keeps an explicit one; `computer()` and `web_browser()` raise inside a scope; scorers called inside a scope call `exec()` without a user.
  - Sandbox selection, with two environments, `default` and `workspace`, where the tools are already injected in `default`: inside `sandbox_identity(..., sandbox="workspace")`, `bash()` with no name and `bash_session()`/`text_editor()` run in `workspace` as the scope's user, injecting there; `bash(sandbox="default")` runs in `default` with its own user.
  - `bash_session()` reuse: two scopes for different users sharing `instance="team"`: the second raises; a session opened at `instance="team"` before any scope raises in a scope; outside scopes, two callers share one session as today.
  - The bridge, with a stub sandbox: `sandbox_agent_bridge()` inside a scope launches its proxy as the tools user without raising, passes the member's user and the token path (and no token) in its launch environment, waits for the complete token file before yielding (a stub proxy that writes it late is waited for; one that never writes it, or exits first, makes the bridge raise within the bound, with no bridged tools configured in either case, and a cancellation during the wait ends it at once), removes the file in its cleanup, sets `token_file`, and puts the `${INSPECT_BRIDGE_TOKEN}` header placeholder on its MCP configs; outside a scope it asks for no token. The proxy (inspect_sandbox_tools' own tests): asked for a token, it creates the file `0400` owned by the given user in a root `0711` directory only after its listener is bound (by rename, so the file is never partial), unlinks it on exit, refuses a planted symlink or file at either path, and a request without the token or with a wrong one gets 401 on each model route and on `/mcp/`, while requests with it in each accepted header succeed; not asked, a dummy-key request succeeds as today.
- **inspect_swe, with a stub sandbox:** inside a scope, `claude_code()` and `codex_cli()` write their config and resolve their cwd as the scope's user; no exec the host issues (argv, stdin or env) contains a token, since the host never has one; the CLI wrapper reads the token file named in its argv and exits non-zero with a message when the file is missing, unreadable or short, rather than exporting an empty key; `claude_code(centaur=...)` and `codex_cli(centaur=...)` raise inside a scope before any setup and behave as today outside one; the Claude Code settings carry `cat <token file>`, not a token; the MCP readiness probe runs as the member and builds its header from the file; `gemini_cli()`, `opencode()`, `kimi_code()`, `mini_swe_agent()`, `antigravity()` and the ACP adapters raise inside a scope and behave as today outside one.
- **inspect_swarm, no Docker** (mockllm, asyncio and trio): `users()` validation (every refusal in [Configuration](#configuration), each with its message); `"users"` and a mapping resolve; the plan logs `isolation` as its fields, `create_registry_object()` rebuilds it, and two arms differing only in a `UserIsolation` field have distinct task identifiers; each member runs inside its `sandbox_identity` and tool-state scopes (a stub tool reads both); the preamble paragraph for `peers="none"` and `"read"` contains only configured names and paths; the `filesystem` consistency check; `foreign_execs` counts a stub custom tool's `exec(user=None)` and ignores the tools launcher. Reaping, with a stub sandbox: it runs on each member status and in the drain, including after cancellation; a script reporting survivors gives `reap.status="survivors"` and `isolation_incomplete`; a stub exec that never returns gives `timeout` within the bound; a provider exception gives `error`; in each case the member's own status and exception are unchanged.
- **inspect_swarm with Docker** (marked slow, skipped in CI without Docker, as swarm.md's filesystem-channel tests are): a task image with `acl` and `git`, two `react()` members on mockllm with scripted tool calls:
  - setup creates the users with uids that own no file, even when files owned by uid 61000 exist on the root filesystem, in `/dev/shm` and on a tmpfs mounted at the homes' parent, and files owned by 1000 exist in the image; the self-test passes; `bash`, `python`, `bash_session` and `text_editor` run as each member and start in its home;
  - a member cannot read or write the other's home with `peers="none"`, can read but not write with `"read"`, and cannot rename it; both can edit each other's files in a `rw` share; neither can write any existing entry of a `ro` share, including one a previous run's member owned and one with an ACL granting the member write before setup;
  - unsafe paths are refused: a home under a `1777` directory, a share reached through a symlink, a share under a group-writable parent;
  - two swarm runs in one sample: the second accepts `/shared`, which the first left `2770` with ACLs, both as `rw` (members of the second run edit each other's files there) and, in a variant, as `ro` (no member of the second run can write any entry, including those the first run's members own);
  - git: each member adds a worktree in its home from a shared repository, commits and merges;
  - a member's `nohup sleep` is gone after it ends and its count is recorded;
  - attribution limits: w-1 hard-links a file from its home into `/shared`, w-2 appends to it, w-1 removes the link and sets the file `0600`; the file then passes the one-link, no-group-or-other-write check and contains w-2's write, and the test pins that the record calls this current exposure, not authorship;
  - the census attributes files in `/shared`, `/tmp` and `/dev/shm` to the right members, records a symlink as a link without following it, and records an absent root as absent;
  - refusals: an image without `setfacl` with a share, an image whose sudoers gives members `NOPASSWD`, a member tool built with `bash(user="root")`, a shared `bash_session(instance="team")`, the `local` sandbox. A macOS bind mount as the home's filesystem fails the self-test on Docker Desktop; that case is run by hand, since CI runs on Linux.
- **Kubernetes** (by hand, against a cluster with the default chart): the same Docker test with the k8s sandbox, to confirm `runuser`, and which share layouts keep ACLs on gVisor (the container root, an `emptyDir`, an `emptyDir` with `medium: Memory`); the self-test is the check where they are not kept.
- **Bridged** (by hand or on a schedule; Docker, inspect_swe and provider keys): in separate samples (one bridged agent type per sandbox, [Bridged members](#bridged-members)), a Claude Code member with a native member, and a Codex member with a native member, under users isolation, with the swarm's bridged tools; each CLI starts, passes its MCP readiness check and calls a bridged tool; each runs as its member with its config in its home; `ps` as the native member shows no token in any command line, and with `peers="read"` the native member can read no file containing it; the native member's `curl` to a CLI's bridge port, on a model route and on `/mcp/`, gets 401; the CLIs' processes are reaped when they end; and the token, read from the sandbox at the end of the sample, occurs nowhere in the eval log, including in a run where the proxy's launch is made to fail. A bridged member that makes no tool calls adds nothing to the census (no file in `/var/tmp` attributed to it, the token directory pruned and its file gone), while a file a native member creates in `/var/tmp` is still counted.

## Implementation plan

No milestone. It depends on M1's runtime (members, the tool-state scope, the record and evidence) and is buildable at any point after it, as one of the later items ("users isolation in one sandbox", [swarm-implementation-plan.md](swarm-implementation-plan.md#future-work)). Each step is one PR; discuss and review after each.

1. **inspect_ai: the sandbox identity scope.** `src/inspect_ai/util/_sandbox/identity.py` (`sandbox_identity`, `sandbox_identity_user`, `sandbox_identity_cwd`, `sandbox_identity_active`, `SandboxIdentityError`), exported from `inspect_ai.util`; the default sandbox and injection target inside a scope in `src/inspect_ai/util/_sandbox/context.py`; strict resolution in `src/inspect_ai/tool/_tools/_execute.py`, `_grep.py`, `_list_files.py`, `_read_file.py`, `_skill/install.py`, `_bash_session.py` (with the identity recorded in its store entry and checked on reuse) and `_text_editor.py`; lenient resolution in `src/inspect_ai/util/_sandbox/_cli.py` (`tools_user_param`) and `exec_remote.py` (user and cwd); refusal in `_computer/_computer.py` and `_web_browser/_web_browser.py`; a paragraph in `docs/sandboxing.qmd`; tests in `tests/util/sandbox/` and `tests/tools/`. Prerequisite for everything below.
2. **inspect_ai: a token for bridges inside a scope.** In `src/inspect_ai/agent/_bridge/sandbox/bridge.py`, when `sandbox_identity_active()`, the member's user and a token path in the proxy's launch environment, `token_file` on `SandboxAgentBridge` (`types.py`), and the header placeholder on the bridged-tools MCP configs; the bounded wait for the token file before yielding and its removal in cleanup; in `src/inspect_sandbox_tools/src/inspect_sandbox_tools/_agent_bridge/proxy.py`, minting the token by rename into a verified root directory once listening, unlinking it on exit, and the check on every route, active only when asked for; the sandbox-tools artifact rebuilt as that package's release process requires; tests including the unscoped dummy-key case. After step 1.
3. **inspect_swe: supported agents and refusals.** `claude_code()` and `codex_cli()` resolve `user` and `cwd` through the scope at the start of their invocation, read and check the token from `token_file` in the CLI's launch wrapper into its environment, refuse `centaur` inside a scope, point their key and MCP configuration at that variable (`_claude_code/claude_code.py`'s `apiKeyHelper` and settings, `_claude_code/env.py`, `_codex_cli/codex_cli.py`'s provider and MCP config), and run the MCP readiness probe as the member with the header built from the file (`_util/mcp_ready.py`); the other agents and the ACP adapters raise inside a scope. Confirms each CLI's environment-variable support for MCP headers on the pinned versions, falling back to `0600` files. After step 2.
4. **inspect_swarm: configuration and setup.** `src/inspect_swarm/_isolation.py` (`Share`, `UserIsolation`, `users()`, validation, the setup script with path checks, uid choice and share normalisation, the self-test); `swarm(isolation=)` in `_swarm.py` with resolution, construction checks and the `filesystem` consistency check; entering `sandbox_identity` per member beside the tool-state scope in `_member.py`; the preamble paragraph; exports from `__init__.py`. Tests: the no-Docker tests and the Docker setup, path, access and git tests. After step 1.
5. **inspect_swarm: reaping and evidence.** Reaping with its deadline and failure contract on member end and in the drain (`_member.py`, `_swarm.py`); the census roots, reaped counts, `isolation_incomplete` and `foreign_execs` in `_evidence.py`, `_metrics.py` and `_record.py` (`MemberResult` fields, the `reap_complete` check); the portable reaping tests and the Docker reaping and census tests.
6. **inspect_swarm: bridged members and docs.** The bridged end-to-end test; a user-docs section on topologies (when to use which, image requirements, supported bridged agents, what is and is not isolated, the privilege confound). After steps 3 and 5.

Later rungs ([table](#later-rungs-surveyed-not-designed)) are separate designs, each when an experiment needs it.

## Changes to other designs

Each for Ransom to approve:

- **swarm.md, [Sandbox topology](swarm.md#sandbox-topology):** link "one shared sandbox with a Linux user and write scope per member" to this document, and state the ladder (shared, users, later rungs, per-member sandboxes) with when each fits. **[Members](swarm.md#members), "Sandbox processes":** under users isolation, processes a member leaves running are reaped when it ends. **[Where each part lives](swarm.md#where-each-part-lives):** a row for the sandbox identity scope (inspect_ai) and the bridge token (inspect_ai and inspect_swe), both "when users isolation is built".
- **swarm-api.md, [The shape of the call](swarm-api.md#the-shape-of-the-call) and [Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct):** `isolation=` as a non-component argument like `budget=`, logged as a pydantic model; a row in the identity table. **[Members](swarm-api.md#members):** the contract gains "custom tools that call `sandbox().exec()` run as the default user unless they resolve their user with `sandbox_identity_user()`".
- **swarm-communication.md, [The filesystem channel](swarm-communication.md#the-filesystem-channel):** under users isolation the census attributes file creation per member; messages-only arms still need per-member sandboxes for full separation.
- **swarm-scoring.md, [Shared-artifact tasks and per-member artifacts](swarm-scoring.md#shared-artifact-tasks-and-per-member-artifacts):** "Attribution in a shared sandbox is by convention" gains the exception that private directories under users isolation are writable only by their member.
- **swarm-lean-open-problems.md, [Recommendation 1](swarm-lean-open-problems.md#1-m2-per-member-sandboxes-as-an-opt-in-member-field):** `member(sandbox=...)` and `isolation=` are refused together until a user needs both.

## Not this design

Noticed and left out, for Ransom to file if wanted:

- **inspect_swe bridge ports collide across agent types** (inspect_swe). `claude_code` and `codex_cli` both start their per-type counters at 3000, so the first member of each in one sandbox both take port 3001 (`claude_code.py:311-314`, `codex_cli.py:356-359`). This affects any heterogeneous bridged swarm in one sandbox, isolated or not; one counter across types fixes it. It is the prerequisite for mixing bridged agent types in one sandbox, which this design defers ([Bridged members](#bridged-members)).
- **inspect_swe reinstalls single-binary CLIs on every run** (inspect_swe). `claude` has no package entrypoint, so each agent execution rewrites the binary in the shared install directory (`_util/agentbinary.py:240-243`); with several Claude Code members running, a rewrite while another member executes the binary would likely fail with "Text file busy" (inferred, not run).
- **The Kubernetes provider resolves `runuser`, `timeout` and `base64` through the image's `PATH`** (inspect_k8s_sandbox: `execute.py:61`, `:139`, `:148`), where inspect_ai's provider contract asks for absolute paths or a fixed system `PATH` (`environment.py:230-244`). Matters once a non-root principal can write a directory on root's `PATH`; not verified as exploitable.
- **`sandbox(name)` returns the default sandbox for any name when only one exists** (`context.py:67-68`), so a misspelled sandbox name silently targets the default. A swarm validating `isolation.sandbox` must check the name against the environments itself.
- **`exec_remote()`'s relative `cwd` is not resolved against the sample's working directory**, and its `env` is merged over the tools server's own environment (`inspect_sandbox_tools: _remote_tools/_exec_remote/_job.py:94-108`; read, not run). Bridged CLIs pass absolute paths, so this design is unaffected.
- **Authentication for every bridge**, outside users isolation too, with a migration for clients that send dummy keys ([Alternatives](#alternatives-considered)). It would stop a member of a shared-default swarm from calling a peer's bridge.
- **Sandbox instrumentation** for reads, modifications and socket traffic (audit logs, fanotify), which the census cannot see. Already listed in swarm-communication.md.
- **A "files only" channel arm** built from private sandboxes and a shared volume, if experiments ask for container-level separation with one shared directory.
