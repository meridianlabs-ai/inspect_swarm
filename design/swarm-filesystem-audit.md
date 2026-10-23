# Inspect Swarm: auditing the filesystem

## Overview

With `channels=[..., "filesystem_audit"]`, the swarm records every change to the files members work on: which file, when, what it now holds, and, where the sandbox allows it, which member made it. It adds no tools and says nothing to members. When the swarm starts, before any member does, it starts a small Python watcher in each sandbox the members use, running as the sandbox's default user. The watcher takes a baseline of the watched directories, then rescans them every second and streams each change back to the swarm: a file written (with its new content, or a diff against the version the audit last recorded, up to a byte cap), renamed or deleted, with its size, its hash and the interval in which the change happened. The swarm writes each change as an evidence event and counts them in the swarm's metrics. When every member has ended, it asks each watcher for a final scan and stops it.

Who made a change is known when the sandbox is the member's own (`member(sandbox=...)`): every change in it is that member's, whether a tool call, a redirect, a script or a process left running made it. In one sandbox the members share, and in a volume their sandboxes share, nothing in the sandbox says which member wrote a file, so those changes are recorded with no member; the time and content are still there for analysis to set against the members' tool calls. Polling misses what happens between two scans (a temporary file created and removed, intermediate versions of a file), and the audit says so in its own records. Reads are not tracked.

## Why

What an analysis of a swarm needs from its log is who wrote which file, when, and what they wrote. Today the log has the members' tool calls and nothing else: shared files are "observed, not recorded", and processes a member starts "can read and write files between logged tool calls, and after the member has ended" ([Channels: Shared files are observed, not recorded](../docs/channels.qmd#shared-files-are-observed-not-recorded)). The communication design leaves "instrumentation in the sandbox" to later work ([swarm-communication.md Not this design](swarm-communication.md#not-this-design)).

In an internal user's experiment runs the most important file changes were made by a command, a redirect or a script, so their content is in no tool argument. A `bash` call such as `python3 prove.py > out.txt` records the command, not what `out.txt` holds or that `prove.py` rewrote `lemma.lean` on its way. In a shared sandbox, which the first internal user ran ([swarm-wait.md](swarm-wait.md#who-waits-today)), nothing records which member's command it was either.

## Goals and non-goals

Goals:

- A `@swarm_channel` factory, `filesystem_audit()`, with plain-value configuration that logs faithfully, adds no tools and no instructions, and records changes to files under configured directories in every sandbox the members use.
- For each change: its kind (written, renamed, deleted), path, time bounds, size, hash, and the new content or a diff, under byte and event caps; the member when the topology allows it.
- An honest statement, in the design and in each run's records, of what it misses.
- How the events appear in evidence and metrics, and how they relate to the filesystem channel and to users isolation.
- Answers to the issue's questions: providers and images, caps and paths, reads, and whether `users()` is a prerequisite.

Non-goals:

- **Attributing changes in one shared sandbox or a shared volume.** It needs to know the writing process, which no unprivileged facility gives ([Attribution](#attribution-by-topology)).
- **Reads.** Not tracked, now or as a planned step ([Reads](#reads)).
- **Containment or tamper resistance.** A member with root in its sandbox can blind or confuse the audit; it measures what members do, it is not a boundary ([Security](#security)).
- **Showing changes to members**, as notices or tool results. The audit only records; telling members about it would be a treatment.
- **Building users isolation** ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md)), or changing the `filesystem` channel.

## Current behaviour

Paths are relative to each repository's root; `src/inspect_ai/...` and `src/inspect_sandbox_tools/...` are in inspect_ai. Claims marked *(spike)* were checked by running an Inspect eval on mockllm with the `docker` sandbox and `python:3.12-slim`, in a throwaway script; nothing from it is committed.

### The filesystem channel has no records

- `filesystem()` is an instructions-only channel: `FilesystemChannel` overrides `instructions()` only, so its `name`, `prefix` and `open()` keep `Channel`'s defaults, `None` (`src/inspect_swarm/_channel/filesystem.py:10-32`, `src/inspect_swarm/_channel/_channel.py:35-70`). It writes no evidence and opens no state. How the audit's events relate to the filesystem channel's existing records therefore has a short answer: there are none; this channel is the instrumentation the communication design's observation limit asks for.
- With `shared_volume=True` it describes per-member sandboxes that each mount one volume at `shared`, an absolute path, and the swarm cannot check that the volume is mounted (`filesystem.py:35-85`). `_swarm.py` already reads the channel's `shared_volume` to choose the preamble and to check the topology (`src/inspect_swarm/_swarm.py:149`, `:313-350`).

### Channels, the bus and the runtime

- `Channel.open(run)` returns a per-run `ChannelState` or `None`; `ChannelRun` carries the run's bus and roster (`_channel.py:61-92`). The bus calls `open()` for every channel in its constructor and refuses a state from a channel with no `name` or `prefix` (`src/inspect_swarm/_bus/_bus.py:68-84`); `SwarmRun` builds the bus in its constructor, before any member starts (`src/inspect_swarm/_controller/_control.py:265-269`).
- `Bus.deliver()` is for records a member's tool call makes: it raises outside a member (`_bus.py:220-226`), and its steps are storm controls, `sent`/`delivered` evidence and the channel's `apply()`.
- A channel with a `name` is a bus channel, and a bridged member is refused beside one (`_swarm.py:273-310`). Instructions-only channels are accepted beside bridged members.
- `SwarmRun.run()` sets the swarm's start, enters the caps, runs the members' task group (inside which the controller starts members), then the final step, and in a `finally` writes the ledger and the metrics (`_control.py:380-525`). There is no hook for work that must run beside the members for the whole run.
- The time cap is a timer started inside the members' task group that sleeps the cap's full length and then stops the swarm (`_control.py:417-424`, `:642-644`), while `swarm_budget()` reports time used from the swarm's start (`src/inspect_swarm/_budget.py:695-697`). Anything the run does between setting its start and starting that timer delays the stop by as long. A member that never started is a supported shape: it ends `pending`, with no status ([Analysis](../docs/analysis.qmd)).

### Evidence and metrics

- Evidence is an `InfoEvent` with `source="inspect_swarm"` and a payload of `version`, `type`, `kind`, `member` and `span_id` plus fields, never member text in harness-written lines (`src/inspect_swarm/_evidence.py:20-43`). The ledger's `EventLog` collects the `comm` events live from the transcript (`src/inspect_swarm/_ledger.py:318-327`), and `swarm_metrics()` counts them into per-member `CommMetrics` (`src/inspect_swarm/_metrics.py:19-35`, `:274-302`).
- `MemberMetrics` and `SwarmMetrics` already carry harness-validity lists (`never_acted`, `notify_unwired`, `tools_unwired`) and give every newer field a default, so older metrics still load (`_metrics.py:37-111`).

### Sandbox topologies and who uses each

The topologies are described in [Sandboxes](../docs/sandboxes.qmd). Who uses each, and what it means for the audit:

- **One shared sandbox** ([Shared](../docs/sandboxes.qmd#shared)), the default. The first internal user ran a leaderless swarm there with the `filesystem` channel ([swarm-wait.md Who waits today](swarm-wait.md#who-waits-today)); `examples/shared_sandbox.py` is the same shape, whose `examples/compose.yaml` sets no working directory, so commands start in `/`. Members run as the sandbox's default user, root in these images.
- **A sandbox per member** ([Per member](../docs/sandboxes.qmd#per-member)). Each member's tools resolve the member's own environment (`src/inspect_swarm/_member.py:227`, `:317-320`; `_swarm.py:352-363`). `examples/proof_swarm.py`, the shape of Epoch's port, gives each member a container whose working directory is a `/workspace` volume, plus a `comparator` service in `visible_sandboxes` (`examples/proof_swarm.yaml`).
- **A shared volume** between per-member sandboxes, with `filesystem(shared_volume=True)` ([A shared volume](../docs/sandboxes.qmd#a-shared-volume)). Tested at construction (`tests/test_member_sandboxes.py:280-345`); no example or known user runs one.
- **Users isolation** in one sandbox is designed, not built ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md)).
- No package in the satellite checkouts beside this one (inspect_ai, inspect_evals, inspect_swe, inspect_flow, inspect_scout, inspect_sentinel) imports inspect_swarm.

### Running a long-lived process in a sandbox

- `SandboxEnvironment.exec_remote()` starts a command through the injected sandbox tools, injecting them first if needed (`src/inspect_ai/util/_sandbox/environment.py:374-450`). With `ExecRemoteStreamingOptions(stdin_open=True)` the host can write to the process's stdin and close it later (`exec_remote.py:160`, `:573`); `kill()` terminates it (`exec_remote.py:604`), so work the process should do before it exits has to be asked for through stdin. Docker and Kubernetes both use this base implementation; inspect_k8s_sandbox does not override it. `local` runs commands on the host.
- **Its user.** With no `user`, the process runs as the sandbox's default exec identity, the one `exec()` uses with no `user`: `_start()` sends `tools_user_param()`'s value, the sandbox's `_tools_default_user` (`exec_remote.py:356`, `src/inspect_ai/util/_sandbox/_cli.py:36-49`). The root identity the tools may hold is only the RPC wrapper's (`exec_remote.py:316-324`).
- The host polls the process for output. The first poll is immediate, then at least every 5 seconds whatever the sandbox's own interval (`MIN_POLL_INTERVAL = 5`, `exec_remote.py:224`, `:290-291`). A poll returns at most 1 MiB, and the tools server buffers up to 100 MiB of output before a write by the process blocks (`src/inspect_sandbox_tools/src/inspect_sandbox_tools/_remote_tools/_exec_remote/_job.py:34-35`).
- Each poll runs inside `SandboxEnvironmentProxy.no_events()` so that it logs no `SandboxEvent` (`exec_remote.py:479`). `no_events()` sets a flag on the environment's proxy object, shared by every task using that environment, and each `exec()`, `read_file()` and `write_file()` checks the flag when it finishes (`src/inspect_ai/util/_sandbox/events.py:38`, `:99`, `:156`, `:190`, `:223-229`). So a member's command that finishes while a poll is in flight loses its `SandboxEvent`. *(spike: with a watcher polled by `exec_remote`, 150 concurrent `exec(["true"])` calls on the same sandbox left 140 `SandboxEvent`s.)* swarm-sandbox-isolation.md noted the same flag for the bridge ([Bridged members](swarm-sandbox-isolation.md#bridged-members)); every `sandbox_agent_bridge()` already polls its proxy this way.
- **The handle's lifecycle.** Iterating the `ExecRemoteProcess` polls until there is output, sleeping the poll interval between empty polls, and kills the process only when the iterating task is cancelled; a poll that still fails after its retries raises and leaves the process running (`exec_remote.py:403-444`). `kill()` marks the handle killed before its RPC and swallows a failed RPC, so its return, or a second call (a no-op), does not say the process exited (`exec_remote.py:612-627`). Polls, `close_stdin()` and `kill()` all acknowledge output with the handle's one `_last_seq` and queue what they receive in one pending list, unsynchronized (`exec_remote.py:395-431`, `:478-485`, `:596-602`): a `close_stdin()` sent while a poll is in flight, both acknowledging the same sequence, gets the same output again, so the iterator yields it twice.

### What a watcher in the sandbox sees *(spike)*

A Python stdlib watcher, started with `exec_remote()` on `python:3.12-slim` and given its program on stdin, took a baseline of `/work` and rescanned it every 0.5 s. It reported, with content: a file written by `echo hi > a.txt`; a log appended to three times in a loop (one event, the final content); a file a `python3 -c` command wrote; an existing file overwritten; `a.txt` renamed to `b.txt` (matched by inode); the log deleted; and a file a `nohup` background process wrote two seconds after its tool call had returned. It did not report a file created and removed in one command, and skipped the excluded `.git`. Its events arrived at the host up to 5 s after its own timestamps, one poll interval. Closing its stdin made it run a final scan, report, and exit 0. The same program, run with `docker run` on `python:3.8-slim` (Python 3.8.20), reported a write, a rename and a deletion. `python3` with `ctypes`, `hashlib` and `difflib` is in `python:3.12-slim` and in `aisiuk/inspect-tool-support`; `inotifywait` is in neither.

### What ownership says under users isolation

A file's owner is the user that created it, and writing to an existing file does not change its owner. swarm-sandbox-isolation.md's census relies on exactly that ("A file's owner is its creator") and says what it cannot see: "reads, ... modifications of existing files" ([What the topology makes measurable](swarm-sandbox-isolation.md#what-the-topology-makes-measurable)). Its survey finds no unprivileged facility that reports which process made a change: process-level tracing needs `CAP_SYS_ADMIN` or similar, which default containers and gVisor do not give ([Survey](swarm-sandbox-isolation.md#survey-linux-isolation-inside-one-sandbox)).

## Design

### At a glance

```
 swarm start                       during the run                          swarm end
 ───────────                       ──────────────                          ─────────
 for each watched sandbox:         watcher (in the sandbox, python3):      every member ended
   probe: python3, cwd, clock        every `interval` s: scan, diff,       for each watcher:
   exec_remote(watcher) ──────────►  print one JSON line per change          close stdin ──► final scan, "done", exit
   wait for "ready" (baseline)     host poller (every 5 s):                write audit_ended
 then the controller starts          read lines, attribute, cap,           metrics count the fs events
 members                             write `fs` evidence
```

| Topology | Watchers | `member` of a change |
|---|---|---|
| A sandbox per member | one in each member's sandbox | that member |
| ... and a shared volume, `filesystem(shared_volume=True)` | also one for the volume, in the first member's sandbox; the members' own watchers skip it | none |
| One shared sandbox | one, in the sample's default sandbox | none |

The shared sandbox and a declared shared volume are recorded in the first version, with `member: null` and the attribution limit stated (decision: Ransom, 2026-10-23).

### Configuration

```python
# src/inspect_swarm/_channel/filesystem_audit.py
DEFAULT_EXCLUDE = (
    ".git", ".hg", ".svn", "__pycache__", ".venv", "node_modules", ".lake",
    ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
)

@swarm_channel
def filesystem_audit(
    paths: Sequence[str] | None = None,       # directories to watch; None: each watched sandbox's working directory
    exclude: Sequence[str] = DEFAULT_EXCLUDE, # file and directory names skipped at any depth
    interval: float = 1.0,                    # seconds between scans in the sandbox
    max_file_bytes: int = 32_000,             # largest text file whose content or diff is recorded
    max_bytes: int = 1_000_000,               # content and diff bytes recorded per member over the run
    max_events: int = 1_000,                  # change events recorded per member over the run
) -> Channel: ...
```

- **Names.** Registered as `inspect_swarm/filesystem_audit`, so `channels=["filesystem", "filesystem_audit"]` and `-T channels=filesystem,filesystem_audit` resolve it with its defaults ([swarm-api.md Resolving names](swarm-api.md#resolving-names)). The returned channel has `name=None` and `prefix=None` (it makes no records), adds no instructions (`instructions()` returns `None`) and opens no `ChannelState`. It is therefore accepted beside bridged members, whose CLIs' writes are among the changes nothing records today.
- **Logging and identity.** Every parameter is a plain value; `paths` and `exclude` come back from a log as lists and are accepted as any sequence. An arm with the channel and one without differ in `channels=`, so their eval-set identifiers differ, which is right: the watcher is a process members can see ([Security](#security)).
- **Validation** when `filesystem_audit()` is called raises `ValueError` naming the parameter: a path that is not absolute, is not in normal form (`posixpath.normpath(p) != p`), contains a NUL or a newline, is `/`, or is under `/proc`, `/sys` or `/dev`; two paths one of which contains the other; an `exclude` name that is empty, `.` or `..`, or contains `/` or a NUL; `interval` outside 0.2 to 60; `max_file_bytes` outside 0 to 1,000,000 (0 records no content); `max_bytes` outside 0 to 100,000,000; `max_events` outside 1 to 1,000,000.
- **Why the caps are per member.** A shared sandbox has one watcher for every member's work, so its allowances are the members' together: a watcher's caps are `max_bytes` and `max_events` times the number of members it covers (one for a member's own sandbox; all of them for a shared sandbox or a shared volume). With the defaults the audit adds at most about 1.5 MB per member and watcher to a sample's log, uncompressed.
- **Why `paths` defaults to the working directory.** In the per-member shape the members' work is where their commands start (`/workspace` in `examples/proof_swarm.yaml`), and the whole filesystem is too broad (package installs, caches). A shared sandbox started in `/`, as `examples/compose.yaml` is, is refused at start with the fix ("filesystem_audit(): sandbox 'default' starts commands in /, the whole filesystem; give the directories to watch with filesystem_audit(paths=[...]), such as the filesystem channel's /shared and /scratch"). The channel does not copy the `filesystem` channel's paths: two channels' configurations stay independent, and a task states what it audits.

### Which sandboxes are watched

`SwarmRun` gives the audit's service ([Channel changes](#changes-to-channel-and-the-runtime)) each member's own sandbox name (`member_sandbox()`, `None` when it shares the sample's) and the `filesystem` channel's `shared` path when that channel has `shared_volume=True`.

- **Per-member sandboxes.** One watcher in each member's environment, looked up by name in the sample's environments (as `_member.py` does). Its roots are `paths`, or that environment's working directory. When a shared volume is declared, every member's watcher skips the volume's path wherever it meets it, and one more watcher, covering every member, watches the volume from the first member's sandbox in roster order. A root equal to or inside the volume's path is refused at start, since its changes would be reported by every member's watcher. The `visible_sandboxes` services are not watched.
- **One shared sandbox.** One watcher in the sample's default environment, the one members' `sandbox()` returns, covering every member.
- **Refused at start**, before any member, with `RuntimeError` naming the sandbox: the `local` sandbox (its commands run on the host); a sandbox without `python3` 3.8 or later on a system `PATH`, or without a writable temporary directory (for the watcher's lock, below); a default root of `/`; a watched root inside a declared shared volume; and a watcher that does not report its baseline within 120 seconds or exits first (with its stderr, at most 4 KB). A run configured with the audit never runs without it.
- **A volume mounted without being declared** (a compose file that mounts one volume in every member's service while the swarm has no `filesystem(shared_volume=True)`) is reported once per member's watcher, attributed to each. The swarm cannot see mounts it was not told about; the channel's docstring says to declare the volume.

### The watcher

One program using only the standard library, `src/inspect_swarm/_channel/_fs_watcher.py`, held to Python 3.8 ([Providers and images](#providers-and-images)). It is an importable module whose functions the tests call directly, and it runs only under `if __name__ == "__main__"`.

**Launch and user.** At start the service runs one probe through the environment's `exec()` with no `user`: `pwd` (the default root), `id -u` (the user it runs as), the absolute path of `python3` resolved with `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, its version, `tempfile.gettempdir()` (where the watcher keeps its lock) and the sandbox's `time.time()`. The clock offset is the sandbox's time minus the midpoint of the host's wall clock around the exec, with half the round trip as its error. It then starts `exec_remote([python3, "-c", BOOT], ExecRemoteStreamingOptions(input=..., stdin_open=True))`, also with no `user`, where `BOOT` reads one line of JSON configuration and then a length-prefixed program from stdin and runs it. The program and its configuration therefore never appear in a command line; the process list shows `python3 -c "import sys,json;..."`. Probe and watcher both run as the sandbox's default user ([Current behaviour](#running-a-long-lived-process-in-a-sandbox)), the user members' commands run as unless a tool names another, so the watcher can read what members' commands can and no more; in the examples' images that is root. A file it cannot open is recorded with `omitted: "unreadable"`, and a directory it cannot list is carried, not reported (below).

**The snapshot.** The watcher keeps, for each regular file and symlink, its signature (device, inode, size, `st_mtime_ns`, `st_ctime_ns`) and `seen`, the start time of the scan that observed that signature; and for each directory it has met, `listed`, the start time of the last scan that listed it completely, or none.

**Scans.** A scan takes its start time before its first `lstat` and its end time after its last read. It walks each root with `os.scandir`, never following symlinks, skipping `exclude` names and the skip paths. Other entries (FIFOs, sockets, devices) are never opened, and directories are walked, not reported. A directory is *listed completely* when its iteration ran to its end with no error and within the scan's limit of 200,000 entries; a root that does not exist, or is not a directory, counts as an empty, completely listed directory, so a directory members create later is picked up. A directory the scan cannot open or finish (a permission or I/O error, or the entry limit, which also marks the scan truncated) is not listed completely, and the previous snapshot's entries below it are carried into the new snapshot unchanged, with their old `seen`. A directory that disappears or stops being a directory between its parent's listing and its own (`ENOENT`, `ENOTDIR`) is not listed completely either; the parent's next listing establishes whether it is gone. An entry whose `lstat` fails with `ENOENT` after its directory listed it is treated as not found, since it was absent when the scan looked; any other error on an entry's `lstat` makes its directory incomplete. The baseline is the first scan; it reports nothing and holds no content.

**Changes.** A path in the previous snapshot is *gone* only when this scan established it: the nearest of its ancestor directories that this scan reached was listed completely, and the path was not found. A path below a directory that was not listed completely is carried, never gone, so an unreadable directory or a truncated scan reports no deletion. Each later scan compares with the previous snapshot:

- a gone path whose device and inode appear at a path new in this scan: `renamed` (`old_path`, `path`); if the signature also changed, a `wrote` for the new path follows. An inode that appears at a new path while its old path is carried is a `wrote`, not a rename;
- a path that is new and not a rename's target: `wrote`, `new: true`;
- a path whose signature changed: read it. No event only when the inode is unchanged and an available hash proves the content unchanged (the previous and the current hash are both known and equal; for a symlink, the same target), as after `touch` or `chmod`. Otherwise `wrote`, `new: false`, with `replaced: true` when the inode changed (an editor's or `sed -i`'s write-and-rename). A changed file with no hash on either side (over 64 MiB, unreadable, in flux) is therefore always a `wrote`, with `sha256: null` and `omitted` saying why;
- a gone path that was not renamed: `deleted`.

Events of one scan are written renames first, then writes, then deletions, each sorted by path, each with the watcher's sequence number `n`.

**Time bounds.** A scan is not atomic, so each change carries its own bounds, in the sandbox's clock: the change happened in `(after, time]`. `time` is the end of the scan that observed the new state. `after` is the start of the scan that last observed the earlier state, which is no later than that observation: for a `wrote` on an existing path and for a `deleted`, the previous entry's `seen`; for a `renamed`, the old path's `seen`; for a new path, the `listed` time, in the previous snapshot, of its nearest ancestor directory there, the last complete listing that showed the path absent, or `null` when that directory was never listed completely (the file may predate the audit). A path left for a later scan keeps its bound: an existing path keeps its previous entry, and a new one is held as pending with the `after` computed when it was first found. A long scan widens the bounds; it never makes them wrong.

**Reading a file.** `os.open` with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_NOCTTY`, then `fstat`: it must still be a regular file with the scanned device and inode, or the path is left for the next scan. A file `open` refuses with a permission error is recorded with `omitted: "unreadable"` and no hash. The watcher hashes the whole file with SHA-256 up to 64 MiB (a larger file has `sha256: null` and `omitted: "size"`), keeping its first `max_file_bytes + 1` bytes. After reading it takes `lstat` again; if the size, mtime or ctime moved, the file was being written, so the path is left for the next scan. A file still moving after ten scans is reported with `omitted: "in_flux"` and no hash.

**Races are per path.** Members delete, rename and replace files while the watcher reads them, so every error from an operation on one path is that path's, never the watcher's. `open`, `fstat`, `read`, `readlink` and the second `lstat` failing with `ENOENT` or `ENOTDIR` (the path went away), `ELOOP` (it became a symlink, which `O_NOFOLLOW` refuses), `EINVAL` from `readlink` (it is no longer a symlink), or any other `OSError`, and a returned `fstat` with another type, device or inode, all leave the path for the next scan: an existing path keeps its previous entry and `seen`, and a new one stays pending with its `after`, so the next scan observes it afresh and its bounds stay honest. Only a permission error records the path as unreadable, and a path deferred ten scans in a row is reported `in_flux`. An exception outside the per-path operations (writing stdout, a bug) is the only thing that ends a running watcher ([Ending](#the-watcher)).

**What a write records.** The content is text when the bytes decode as UTF-8, hold no NUL and number at most `max_file_bytes`. For each path the watcher holds the text of the last version the audit recorded, with its hash: one written in full or as a diff, so a version the log alone can rebuild. It holds nothing after the baseline or a restart. For a text write it records one of:

- `diff`, with `base_sha256`: a unified diff (`difflib.unified_diff`, three lines of context) against the held text, when it holds one and the diff is shorter than the content;
- `content`: the whole new text otherwise, which makes the first recorded version of each path after the baseline or a restart always its full content.

Anything else records no content, with `omitted`: `"binary"`, `"size"`, `"unreadable"`, `"budget"` (the watcher's `max_bytes` allowance is spent; size and hash are still recorded) or `"in_flux"`. A write that records neither content nor a diff drops the path's held text, so its next text write records full content again; the held texts are bounded at 64 MiB in total, oldest evicted first, with the same effect. A rename moves the held text to the new path; a deletion drops it. Every diff's base is therefore the last version the audit recorded for that path, and `base_sha256` names it. A symlink records its `target` (at most 4,096 bytes). A path that is not valid UTF-8 is written with each undecodable byte as `\xNN` and `lossy: true`.

**Caps.** The watcher keeps its own count against its allowances. Once `max_events` events are written it writes no more change events but keeps scanning, so its snapshot stays current, and counts what it would have written by kind. Content and diff bytes count against `max_bytes`. The service applies the same allowances again to what it records, so a watcher that writes more is cut off at the host too.

**Output and pacing.** One JSON object per line, ASCII only, flushed after each scan: `ready` (baseline time, files, unreadable directories, truncated), the change events, `tick`, `done` (counts by kind, dropped counts, truncated scans). A `tick` (`{"kind":"tick","scan":n,"time":t}`) is written whenever two seconds have passed since the watcher's last line, waking for it in its wait between scans, so every poll of a watcher that is not mid-scan finds output and the host's iterator returns within one poll interval ([The service on the host](#the-service-on-the-host)); ticks are not evidence. Change events are bounded by the caps and ticks by the run's length, about 20 bytes a second, so at the host's 1 MiB per 5 s output is read within seconds of being written. If the host falls behind, the tools server's buffer fills and the watcher's write blocks; scans pause and the next scan reports what changed in the longer interval, with bounds that cover it.

**One watcher at a time.** Each watcher holds an exclusive `fcntl.flock` on a lock file named for the run and the watcher, `<tempdir>/inspect_swarm_audit_<token>/<watcher>.lock` (the directory made with mode 0700, the token a random hex string the service draws per run), and writes its pid into it. The directory is a skip path for every watcher. A watcher that finds the lock held is a replacement whose predecessor still runs: it sends the pid in the file `SIGTERM`, waits up to five seconds for the lock, then `SIGKILL` and five more seconds, and takes the lock; if it still cannot, it writes `{"kind":"refused","reason":"predecessor"}` and exits 3 without scanning. The lock is the confirmation the host's `kill()` cannot give: it is released only when its holder exits, and a pid is signalled only while its holder still holds it, so a reused pid is never signalled.

**Ending.** The watcher waits for the next scan with `select()` on stdin. End of input means stop: it runs a final scan, writes its events and `done`, and exits 0. An exception outside the per-path operations writes a traceback to stderr and exits 1.

### The service on the host

`FilesystemAuditService`, opened per run by `filesystem_audit()`'s `service()`:

- **`start(tg, swarm_start)`** probes and launches every watched sandbox concurrently, waits for each `ready`, writes one `audit_started` event per watcher, and starts one poller per watcher in `tg`. It raises `RuntimeError` for every refusal above, before the controller runs, so no member starts. Every handle it launches is registered with the service before anything is awaited on it, so `stop()` owns it even when `start()` fails or is cancelled part way.
- **One owner per handle.** A watcher's `ExecRemoteProcess` is used by its poller task alone: no other task polls it, closes its stdin or kills it, so no two of its operations acknowledge the same output ([Current behaviour](#running-a-long-lived-process-in-a-sandbox)). Other tasks reach it by setting the poller's `stop_requested` event. The poller checks the event each time the iterator returns, which the watcher's ticks make at most one poll interval apart; when it is set, the poller calls `close_stdin()` itself, between two iterator steps, so its output joins the pending list in order, and goes on iterating until the process completes.
- **The poller** iterates the watcher's `ExecRemoteProcess`, splits stdout into lines, validates each against a pydantic model of the line types, converts its times to swarm time (sandbox time minus the clock offset, mapped from the host's wall clock to `anyio.current_time()` at start, minus the swarm's start), widens `after` down and `time` up by the clock error, applies the caps, and writes the evidence. A line that does not validate is recorded as `audit_error` with its length, not its text, and skipped. The poller keeps, for each path, the hash of the last version it recorded with content or a diff; a `diff` whose `base_sha256` is not that hash (the host cut the base, or the line was forged) is recorded with `omitted: "base"` instead, so no recorded diff applies to a version the log does not hold. It catches every exception from the sandbox or the parsing: nothing a service does after `start()` ends the swarm.
- **A watcher lost.** If the process exits before stop, or polling fails after `exec_remote()`'s own retries, the poller writes `audit_lost` (time, `reason`: `exited` or `poll_failed`, exit code, stderr up to 4 KB as data). After a poll failure the process may still run, so the poller first calls the old handle's `kill()`, best effort, and keeps the handle. It then starts a new watcher after one second, with a new baseline; the new watcher's lock retires the old process if it still runs ([One watcher at a time](#the-watcher)), and its `ready` says whether it did. The poller writes `audit_restarted`. A replacement that exits `refused` (its predecessor could not be stopped) is not retried: the sandbox's audit ends there, with the predecessor recorded as unretired, so two watchers never scan one sandbox. Changes during the gap are not reported. At most three restarts per watcher; after that the sandbox's audit ends there. A member that runs `pkill python3` loses at most the changes of one gap.
- **`stop()`**, after every member has ended, sets every poller's `stop_requested` and waits for each watcher's `done` and exit, bounded by `anyio.move_on_after(30)` (the poller sees the next tick within one poll interval and the exit at the poll after the final scan, so this takes about ten seconds plus the final scan). It then cancels the pollers still running, whose iterators kill their processes, best effort.
- **Retirement.** Last, `stop()` confirms that no watcher of this run is left: one `exec()` of a short `python3 -c` script, bounded by 15 seconds, takes each lock file in the run's lock directory, sends a holder still running `SIGTERM` and then `SIGKILL` as a replacement watcher would, prints the watchers whose lock it could not take, and removes the directory. A watcher whose process completed, or whose lock the script took, is retired; one whose lock is still held, or every watcher of the sandbox when the exec fails or times out, is unretired: its `audit_ended` has `retired: false`, it is not complete, and the service logs a warning naming the sandbox, since that process may go on scanning into scoring. `stop()` then writes `audit_ended` for each watcher. It never raises.
- **Cancellation.** When the sample itself is cancelled (a sample limit, the operator), the pollers are cancelled with it and their iterators kill their processes, best effort. `stop()`, called from the runtime's `finally`, runs only the retirement step and the `audit_ended` events, inside a shielded cancel scope bounded by 15 seconds, marking each audit incomplete: there is no final scan.

### Attribution by topology

- **A member's own sandbox.** Every change in it is attributed to the member, whatever made it: a tool call, a redirect, a script, a bridged CLI, a background process, or a process the member left running after it ended. Metrics count changes certainly after the member's end separately ([Metrics](#metrics)). The task's own setup before the swarm is in the baseline, not reported; a scorer's writes after the swarm come after the final scan.
- **A shared sandbox or volume.** Changes have `member: null`. The writing process is not knowable from a scan, and no unprivileged kernel facility reports it ([Current behaviour](#what-ownership-says-under-users-isolation)). A change's bounds `(after, time]`, in swarm time, let analysis list the members with a tool call that overlapped them; that is a hint, since a background process of any member may have made it, and a change with `after: null` overlaps every call before `time`.
- **Users isolation is not a prerequisite, and would not be sufficient.** Under `users()` a file's owner is the member that created it, so the audit could attribute `wrote` events with `new: true` or `replaced: true` (a new inode, created by the writer) to the owner's member. It could not attribute writes into an existing file (`>`, `>>`, in-place editors, `text_editor`), deletions or renames, which are much of what members do to shared files. So `users()` does not unlock this channel; it would add attribution for one kind of change. It stays its own future item, built when an isolation experiment needs it ([swarm-implementation-plan.md Future work](swarm-implementation-plan.md#future-work)); attributing new and replaced files by owner under it is one step for then ([Not this design](#not-this-design)) (decision: Ransom, 2026-10-23).

### What the audit misses

Each run's records say which of these happened (`truncated`, `unreadable`, `dropped`, `omitted`, gaps); the rest are properties of polling the design states here and the docs repeat:

- **Between scans.** Changes coalesce: several writes to a file in one interval are one `wrote` with the last version; a file created and removed within an interval is never seen; a file changed and changed back within an interval, to the same content and inode, shows no event. The default interval is one second.
- **Content.** Larger than `max_file_bytes`, binary, unreadable, past the budget, or still being written after ten scans: size and hash only (and no hash past 64 MiB or for an unreadable file). A diff is against the last version the audit recorded, which may be several versions back when changes coalesced, but is always in the log.
- **What is not a file's content.** Directories, empty directories, permissions, owners, timestamps and extended attributes. Hard links are separate paths.
- **Where.** Outside the watched roots, under excluded names, on skip paths, below directories the watcher cannot list or a truncated scan did not reach, and in sandboxes not watched (shared services).
- **When.** Before the baseline; during a gap after a watcher is lost; after the final scan; the whole end of a run when the sample is cancelled; and everything in a sandbox whose replacement watcher was refused.
- **Who**, in a shared sandbox or volume.
- **Time precision.** `(after, time]` spans from the start of the scan that last saw the old state to the end of the one that saw the new state, about two intervals for an ordinary change and more after a deferral; it is in the sandbox's clock, mapped to the host's by an offset measured once at start and widened by its error. On Docker the clocks are the host's; on Kubernetes a node's clock can drift during a long run.

### Reads

Reads are not tracked, and this design does not plan them as a later step. Polling cannot see a read (access times are not updated on `relatime` and `noatime` mounts), and inotify's open and access events say that a file was read, not by whom. In a member's own sandbox, its reads of its own files say nothing about communication; the reads that would, of a shared sandbox's or volume's files by another member, are exactly the ones that cannot be attributed without process-level tracing. The read side of the filesystem channel is what `read` evidence gives the shared-memory board ([swarm-memory-channel.md Evidence](swarm-memory-channel.md#evidence)); for files, only privileged tracing would.

### Providers and images

- **Docker** (Desktop and Linux): supported; the spike ran there. Each watcher costs one `exec_remote()` poll every 5 s from the host, a `docker compose exec` of the sandbox tools, plus the in-sandbox scan.
- **Kubernetes** (inspect_k8s_sandbox, gVisor by default): `exec_remote()` uses the same base implementation through `exec()`; scanning uses only `stat`, `open` and `read`, which gVisor implements. Not run here; a manual test is in [Testing](#testing). The clock offset matters there.
- **Other providers** work if `exec_remote()`'s tools injection works on them; a failure is a refusal at start.
- **`local`**: refused.
- **Images** need `python3` 3.8 or later on a system `PATH`; an image without it is refused at start with the requirement (decision: Ransom, 2026-10-23). Epoch's Lean image is to be checked when that port runs; a watcher built into inspect_ai's sandbox tools is the fallback if a real image lacks `python3` ([Alternatives](#alternatives-considered)).
- **Why 3.8 and not the package's 3.11.** inspect_swarm needs Python 3.11 on the host, where the swarm runs. The watcher runs on the sandbox image's own interpreter, never imports inspect_swarm or anything outside the standard library, and so needs only what its own code uses: `os.scandir`, `hashlib`, `difflib`, `json`, `select`. A 3.11 floor would refuse images based on Ubuntu 22.04 (3.10), Debian bullseye (3.9) and Ubuntu 20.04 (3.8) for nothing the watcher needs.
- **Keeping the floor real.** `_fs_watcher.py` is written to Python 3.8's syntax and standard library, with `from __future__ import annotations` so its annotations keep the repository's style. Three checks hold it there: ruff's `per-file-target-version` sets the file to `py38`, which rejects newer syntax such as `match`; a pyright execution environment whose `root` is the file sets `pythonVersion = "3.8"`, which also rejects runtime subscripts such as `list[str]()`; and a slow Docker test runs the watcher on `python:3.8-slim`, which catches standard-library functions newer than 3.8 that neither checker does (`str.removeprefix` passes both). *(Both configuration settings were checked against a scratch project with this repository's ruff and pyright.)*
- **No inotify**, so no watch limits, no per-directory watch races, and the same behaviour under gVisor and on volumes shared between containers ([Alternatives](#alternatives-considered)).

### Evidence

Each event is an `InfoEvent` with `source="inspect_swarm"`, `version: 1` and `type: "fs"`, written in the swarm's span. `member` is the attributed member or `null`; `span_id` is `null`, because a change is the swarm's observation, not an act in a member's span (analysis joins a member to its span through its `member_result` event). Every event names its `sandbox` (the environment's name) and its `watcher`, the sandbox name, or `"<sandbox>:<volume path>"` for a shared volume's. Times are swarm time, as in member results.

| Kind | When | Fields beyond the common ones |
|---|---|---|
| `audit_started` | a watcher's baseline is ready | `scope` (`member`, `shared_sandbox`, `shared_volume`), `covers` (members), `paths`, `exclude`, `skip`, `missing` (roots absent at start), `interval`, `user` (the default user's uid), `files`, `unreadable`, `truncated`, `python`, `clock_offset`, `clock_error`, `max_bytes` and `max_events` for this watcher |
| `wrote` | a file or symlink was created or changed | `n`, `path`, `lossy`, `new`, `replaced`, `file_type` (`file`, `symlink`), `size`, `sha256`, one of `content`, `diff` (with `base_sha256`), `target`, `omitted`, `time`, `after` |
| `renamed` | a file moved | `n`, `old_path`, `path`, `lossy`, `sha256`, `time`, `after` |
| `deleted` | a file or symlink disappeared | `n`, `path`, `lossy`, `sha256` (its last recorded hash, or `null`), `time`, `after` |
| `audit_lost`, `audit_restarted` | a watcher stopped unasked, and its replacement's baseline | `time`, `reason` (`exited`, `poll_failed`), `exit_code`, `stderr`; `files`, `retired_predecessor` |
| `audit_error` | a line from a watcher did not validate | `time`, `length` |
| `audit_ended` | `stop()` | `counts` by kind, `dropped` (`events` by kind, `content`), `restarts`, `gaps` (`[lost, restarted]` pairs), `truncated_scans`, `unreadable`, `final_scan` (whether it completed), `retired` (no process of this watcher is left), `complete` |

`after` is a number or `null` ([Time bounds](#the-watcher)). `omitted` is one of `binary`, `size`, `unreadable`, `budget`, `in_flux` and, from the host, `base`. `complete` is true when the watcher was never lost, no cap dropped an event or content, no scan was truncated, no directory was unreadable, the final scan completed, and the watcher was retired. Content and diffs are JSON strings in the payload: data for analysis, never shown to a member. A path's recorded versions are rebuilt from its first `content` and the `diff`s after it, each checked against its `base_sha256`.

### Metrics

`EventLog` collects `fs` events beside `comm` events, and `swarm_metrics()` counts them:

```python
class FsMetrics(BaseModel):
    writes: int = 0       # wrote events
    created: int = 0      # of them, new: true
    renames: int = 0
    deletes: int = 0
    files: int = 0        # distinct paths written, renamed to or deleted
    after_end: int = 0    # events whose `after` is at or after the member's end (attributed only)

class MemberMetrics(BaseModel):
    ...
    fs: FsMetrics | None = None           # None without filesystem_audit or in a shared sandbox

class SwarmMetrics(BaseModel):
    ...
    fs_unattributed: FsMetrics | None = None   # changes with no member: a shared sandbox or volume
    fs_incomplete: list[str] = []              # harness validity: watchers whose audit_ended is not complete
```

`after_end` uses the member's end time from `MemberTimes` and the event's lower bound, so it counts only what certainly happened after the member ended: what its leftover processes wrote. A change whose bounds contain the end, or whose `after` is `null`, is not in `after_end`; the `wrote` events stay attributed to the member either way. `fs_incomplete` is a harness-validity list like `never_acted`: an analysis of file traffic leaves those samples out or reports them.

### Changes to `Channel` and the runtime

The audit is the first channel that runs work beside the members for the whole run. It needs, all in this package:

1. **`Channel.service(run) -> ChannelService | None`**, returning `None` by default, beside `open()`; `filesystem_audit()`'s channel returns its service.
2. **`ChannelService`**, a protocol in `_channel/_channel.py`:

   ```python
   class ChannelService(Protocol):
       async def start(self, tg: TaskGroup, swarm_start: float) -> None:
           """Start, before any member starts; work that runs beside the members goes in `tg`. Raises to refuse the run."""

       async def stop(self) -> None:
           """Finish, after every member has ended. Never raises."""
   ```

   It is named for its lifecycle, work that runs beside the members, not for a judgment: the swarm's evidence and metrics component is already its Observer ([swarm-api.md Observer](swarm-api.md#observer)), and inspect_sentinel's monitors observe a step and return an `Observation` (decision: Ransom, 2026-10-23).
3. **`ChannelRun`** gains `sandboxes: Mapping[str, str | None]` (each member's own sandbox, or `None`) and `shared_volume: str | None` (the `filesystem` channel's `shared` when it has `shared_volume=True`). `SwarmRun` builds the `ChannelRun` once, from its records and channels, and passes it to the `Bus` (for `open()`) and to `service()`.
4. **`SwarmRun.run()`** opens a task group for services around the caps and the members' task group, starts each service in it after the swarm's start and span are set and before the controller runs, and calls each service's `stop()` once the members' task group has exited, in a `finally` that also runs when a member or the controller raised, then cancels the services' task group. On the normal path and after a swarm stop (whose grace and hard stop end members, not the run), `stop()` runs its final scan; when the run itself is cancelled it only records, as above. The final step, the ledger and the metrics follow, so the metrics see every `fs` event.
5. **The time cap is a deadline.** Services start inside the swarm's time, which `swarm_budget()` already counts, so the cap becomes an absolute deadline, `deadline = swarm start + cap`. `_time_cap()` sleeps until it (`anyio.sleep_until(deadline)`) instead of for the cap's length, so startup does not move the stop later. Services start under `anyio.move_on_at(deadline)`. If the deadline passes before every service is ready, `run()` stops the swarm with `swarm_time` and never enters the members' task group, so no controller runs and no member starts; the services' `stop()` runs as on any other path (a service whose `start()` was cancelled still owns and retires what it launched), and the final step runs with every member `pending`. Without a time cap, startup is bounded by each watcher's 120-second wait for its baseline and by the sample's own limits.

This minimal additive extension of the public `Channel` and `ChannelRun` is approved (decision: Ransom, 2026-10-23). The bus is unchanged: the audit makes no records, so `deliver()`, storm controls, notices and the bridged-member refusal do not apply to it.

## Alternatives considered

**inotify, with polling where it is unavailable.** The issue's first suggestion. It gives exact times and sees short-lived files, but not the writer either, so it adds nothing to attribution. It needs a watch per directory, with a race for every new directory (files created before its watch is added are missed, so a rescan is needed anyway), it is bounded by `max_user_watches` on large trees, under gVisor it misses changes made outside the sandbox's kernel (a volume shared between pods), and between Docker containers it reports a shared volume's changes to every container's watcher. Polling at one second covers the changes the issue names, whose results persist, with one code path on every provider. inotify could later trigger an immediate scan inside the same watcher, with the same events, if an experiment needs sharper times.

**Scans driven from the host** (`find -printf` through `exec()` each tick, diffed on the host, changed files read with `read_file()`). No process in the sandbox to lose. Rejected: every tick is a host exec (a `docker compose exec` each) per sandbox, so timing is bounded by exec cost; each changed file is another exec or copy; and the execs either log a `SandboxEvent` each or call the unwrapped environment through private state. The watcher keeps the per-tick work in the sandbox and costs the host one poll per 5 s.

**Scans at turn boundaries or around tool calls.** Would tie each change to a tool call. Rejected: bridged members have no turn boundary the swarm sees, background processes write between turns, not every member wires `swarm_on_continue()`, and in a shared sandbox it still cannot say which member's call it was.

**Process-level tracing** (fanotify reporting the writer's pid, the audit subsystem, eBPF). The only way to attribute writes and reads in a shared sandbox. Rejected: each needs `CAP_SYS_ADMIN`, `CAP_AUDIT_CONTROL` or BPF privileges that default containers and gVisor do not give, and granting them to a container running untrusted code as root weakens the sandbox's own boundary ([swarm-sandbox-isolation.md Survey](swarm-sandbox-isolation.md#survey-linux-isolation-inside-one-sandbox)).

**A watcher in inspect_ai's sandbox tools**, the binary `exec_remote()` already injects. No `python3` requirement, so any image works. Rejected for now: an inspect_ai feature and a rebuilt tools artifact, for an image we have not seen fail. It is the fallback if a real user's image lacks `python3`.

**The watcher as root.** `exec_remote(user="root")` would let it read files members' default user cannot. Rejected: it would read what members' commands cannot, so the log could hold content no member could have seen; not every provider can switch user (Kubernetes runs as the pod's user); and in today's images the default user is root anyway.

**Changes as bus records.** Rejected: `deliver()` is for what a member's tool call sends, needs a member's context, and applies storm controls and notices that make no sense for an observation; and a channel with records refuses bridged members, whose writes the audit is most needed for.

**An `audit=` argument on `swarm()`, or always on.** swarm-api.md gives the observer no argument because what it records is the same in every arm ([Observer](swarm-api.md#observer)). The audit is not free: a process members can see, sandbox CPU, a `python3` requirement and log volume. So it is opt-in, and as a channel it gets registration, logging, `-T channels=` and a distinct arm identity with no new mechanism.

**Default `paths` from the `filesystem` channel's directories.** Would make `channels=["filesystem", "filesystem_audit"]` work in a shared sandbox with no arguments. Rejected: it couples two channels' configurations, and the per-member shape has no such channel; a refusal with the fix at start is clearer.

**Full content on every write, no diffs.** Simpler analysis. Rejected for log volume: a 30 KB file edited a hundred times would log 3 MB; a diff records what changed. **Diffs against the baseline's text**, without recording it, would save the first full copy of each file, but the log could not rebuild what the file holds; the first recorded version is full content instead.

## Compatibility and migration

- **inspect_ai:** a fix to `SandboxEnvironmentProxy.no_events()`, which only the calling task's events skip ([Implementation plan](#implementation-plan), step 1). Events that other tasks' commands log during a suppressed poll were meant to be logged and now are, for the bridge's polls too. No API change.
- **inspect_swarm API:** new `filesystem_audit()` and its registry name, `Channel.service()`, `ChannelService`, and two `ChannelRun` fields. Like everything in inspect_swarm, experimental ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 6).
- **Eval logs:** only `InfoEvent`s, of a new evidence type `fs`, `version: 1`, and new optional metrics fields with defaults, so metrics written before them load. No schema or viewer change.
- **Existing swarms:** unchanged unless they list the channel. Listing it changes the arm's identity.
- **Tasks:** the audited sandboxes need `python3` 3.8+ and `exec_remote()` support; a shared sandbox starting in `/` needs `paths`. Each sample starts about one poll interval (5 s) later, counted against the swarm's time cap, and ends up to about 45 s later in the worst case (the 30 s drain and the 15 s retirement), typically about 10 s.

## Security

Untrusted input reaching the new code, and how it is handled:

- **File contents** (model output, possibly an injection relayed into a file): bounded by `max_file_bytes` and `max_bytes`, recorded only when valid UTF-8 without NUL, as JSON strings in `InfoEvent` payloads; never in a notice, a tool result, an instruction or any text a member sees. Binary content is never recorded.
- **File names** (any bytes but NUL and `/`): recorded as JSON strings, with undecodable bytes escaped and `lossy` set; never interpolated into a command, a script or a harness-written line. The watcher's configuration comes from the eval author and reaches it as JSON on stdin.
- **Special files.** Symlinks are never followed (`os.scandir` without following, `O_NOFOLLOW`); only their target string is recorded. FIFOs, sockets and devices are never opened; `O_NONBLOCK` and the `fstat` check after `open` cover a path swapped between scan and open.
- **Size and shape bombs.** Hashing stops at 64 MiB, a scan at 200,000 entries, the held texts at 64 MiB; event and byte caps bound the output; output beyond the tools server's buffer blocks the watcher rather than growing memory.
- **What the watcher can read.** It runs as the sandbox's default user, the user members' commands run as by default, so it reads nothing their commands could not; a file or directory that user cannot read is recorded as unreadable. The eval author chooses `paths`; secrets the task keeps in the sandbox belong outside them.
- **Tampering.** A member with root in its sandbox sees the watcher in the process list and can kill it, flood a watched directory to exhaust the caps, make a directory unreadable, delete or hold the watcher's lock file, or write forged lines into the watcher's stdout through `/proc`. Losses are recorded and restarted; caps and unreadable directories are recorded; every line is schema-validated and capped again on the host, a malformed one is recorded by length only, and a diff whose base the host did not record is not recorded. Attribution is by sandbox on the host, so a member can forge events only about its own sandbox (or, in shared topologies, unattributed ones). The audit records what cooperative members did, and says when its record has gaps; it is not evidence against a member trying to hide.
- **The member's `SandboxEvent`s.** Without the inspect_ai fix, the watcher's polls would drop some of the member's own sandbox events from the log ([Current behaviour](#running-a-long-lived-process-in-a-sandbox)); the fix is a prerequisite, not an option.
- **Isolation between samples:** each run's service holds its own watchers and counters; the `Channel` holds configuration only.

## Testing

Tests use mockllm with scripted tool calls and run on asyncio and trio; those marked `slow` need Docker and run only with `--runslow`, as `tests/conftest.py` arranges.

- **The watcher** (`tests/test_fs_watcher.py`, no Docker; it runs the module as a subprocess with the host's Python over a temporary directory, and calls its functions directly for the cases that need a controlled race or a lowered limit), table-driven: create; append in place; overwrite; write-and-rename over a path (`replaced: true`); rename; rename and modify in one interval; delete; a symlink (target recorded, not followed); a FIFO in the tree (never opened, the scan does not block); a binary file and one over `max_file_bytes` (hash and size only); `touch` with no content change (no event); a hashless file (with a lowered hash limit) appended with its inode unchanged (a `wrote` with `sha256: null`); a file created and removed within one interval (no event); an excluded name at depth; a non-UTF-8 name (`lossy`); a file whose `lstat` moves between read and re-check (left for the next scan, keeping its `after`, then `in_flux` after ten); an unreadable file (`omitted: "unreadable"`) and an unreadable directory (no deletions below it, `unreadable` counted); an entry limit of two over a baseline of `a`, `b` followed by a tree of `0`, `a`, `b` (no deletion of `b`, the scan truncated); a root created after the baseline and a file in a new subdirectory (`after` from the parent's listing); a directory unreadable at baseline that becomes readable (`after: null`); bounds that contain the change when the previous scan was slow (a controlled clock); the first write after the baseline recording `content`, the next a `diff` whose `base_sha256` is the first's hash, an omitted write followed by full `content`, and a rename carrying the chain; `max_events` and `max_bytes` reached (counts in `done`); end of stdin (final scan, `done`, exit 0). Races, injected between the scan's steps by calling its functions with the file changed in between: a file deleted after its `lstat` and before `open` (`ENOENT`), a file replaced by a symlink before `open` (`ELOOP`), a file deleted between the read and the second `lstat`, a symlink replaced by a file before `readlink` (`EINVAL`), a directory removed between its parent's listing and its own, and an entry removed between the listing and its `lstat`; in each the watcher keeps running, reports no event for the path in that scan, and the next scan reports the path's real state with an `after` no later than its last observation. Ticks on a quiet tree at least every two seconds. Two watcher processes for one lock: the second signals the first, takes the lock once the first has exited, and reports the retirement in `ready`; a first process that ignores `SIGTERM` is killed; a lock that cannot be taken (the watcher's signalling stubbed out) gives `refused` and exit 3 with no scan.
- **The service plumbing** (`tests/test_channel.py`, with a test channel whose service records calls): `start()` runs before any member's first model call and `stop()` after every member ended, a background drain included; `stop()` runs when a member raises and after a swarm stop's hard stop; a `start()` that raises fails the sample with no member started; a service's task never ends the swarm; a channel with a service and no records is accepted beside a bridged member; `ChannelRun.sandboxes` and `shared_volume` for each topology.
- **The channel without Docker** (`tests/test_filesystem_audit.py`): `"filesystem_audit"` and `inspect_swarm/filesystem_audit` resolve with defaults, including from `-T channels=...`; the plan step logs its parameters and `create_registry_object()` rebuilds it, lists back to sequences; arms with and without it have distinct identifiers; each validation bound raises. With stub sandboxes whose `exec_remote()` runs the watcher locally over a temporary directory per environment: attribution for two per-member sandboxes; `member: null` for a shared sandbox; a declared shared volume (one temporary directory both stubs expose at one path) reported once, unattributed, and skipped by the members' watchers; a root inside the volume refused; `local`, a missing `python3` and a default root of `/` refused before any member starts; probe and watcher started with no `user`; a watcher killed mid-run gives `audit_lost`, `audit_restarted` and a gap, and `complete: false`; a fourth loss gives no restart; a poll that fails while the stub's process is still alive kills the old handle and the replacement retires the old process through the lock, leaving one running; a replacement that reports `refused` ends the sandbox's audit with `retired: false`; a stub whose `kill()` RPC fails and whose retirement exec fails gives `retired: false`, `complete: false` and a warning; a quiet watcher (ticks only) stopped with no duplicate or missing events, against a stub transport that acknowledges output by sequence number as the tools server does, and with `close_stdin()` only ever called by the poller between iterator steps; a stop whose watcher never answers is cancelled after 30 seconds and still retired; a malformed line gives `audit_error` with no text; host-side caps applied to a watcher that ignores its own, and its next diff for a cut path recorded as `omitted: "base"`; times in swarm time with the measured offset, the bounds widened by its error.
- **The time cap during startup** (`tests/test_channel.py`, with a test service whose `start()` waits a controlled time): startup shorter than the cap, with an explicit `Budget(time=...)` and with a cap derived from the sample's time limit, stops the swarm at swarm start plus the cap, not later; startup longer than the cap stops it with `swarm_time` before any member starts, runs the service's `stop()` and the final step, and leaves every member `pending`.
- **Metrics** (`tests/test_metrics.py`): `FsMetrics` per member from scripted `fs` events, `after_end` against the member's end time (a change whose bounds contain the end, and one with `after: null`, not counted), `fs_unattributed`, `fs_incomplete`, and metrics written before these fields loading.
- **Docker** (`tests/test_filesystem_audit.py`, `slow`): a shared sandbox with two `react()` members writing by redirect, by `python3 -c` and by a `nohup` background command, recorded with content and `member: null`; per-member sandboxes with a declared shared volume, each member's changes attributed to it and the volume's once and unattributed; a member's `pkill python3`, which kills its watcher, followed by a restart; every member `bash()` call has its `SandboxEvent` while watchers poll (the inspect_ai fix); a sandbox on `python:3.8-slim`, which the probe accepts and whose watcher reports a write, a rename and a deletion; an image without `python3` (`debian:bookworm-slim`) refused.
- **Kubernetes** (by hand, against a cluster with inspect_k8s_sandbox's default chart): the per-member Docker test with the k8s sandbox, to confirm `exec_remote()` and scanning under gVisor and to record the measured clock offset.
- **inspect_ai** (its own suite): inside `no_events()` in one task, another task's `exec()` on the same proxy logs its `SandboxEvent`, and the calling task's does not.

## Implementation plan

Each step is one pull request.

1. **inspect_ai: task-local `no_events()`.** In `src/inspect_ai/util/_sandbox/events.py`, a `ContextVar` holding the proxies suppressed in the current context replaces the flag on the proxy, so only the task inside the block skips its events; tests in `tests/util/sandbox/`. A `fix:` pull request of its own in inspect_ai; step 4 depends on it (decision: Ransom, 2026-10-23).
2. **inspect_swarm: the watcher.** `src/inspect_swarm/_channel/_fs_watcher.py` and `tests/test_fs_watcher.py`; in `pyproject.toml`, `per-file-target-version` under `[tool.ruff]` and a `[[tool.pyright.executionEnvironments]]` entry with `pythonVersion = "3.8"`, both for that file. Independent of the rest.
3. **inspect_swarm: channel services.** `Channel.service()`, `ChannelService` and the `ChannelRun` fields in `src/inspect_swarm/_channel/_channel.py`; `SwarmRun` building the `ChannelRun` and starting and stopping services and the time cap's deadline in `src/inspect_swarm/_controller/_control.py`; the `Bus` taking the `ChannelRun` in `src/inspect_swarm/_bus/_bus.py`; the sandbox and volume facts from `src/inspect_swarm/_swarm.py`; the plumbing and startup-deadline tests in `tests/test_channel.py`.
4. **inspect_swarm: the channel.** `src/inspect_swarm/_channel/filesystem_audit.py` (factory, validation, `FilesystemAuditService`, the probe, launch, poller, restart, stop and retirement); `fs` evidence helpers in `src/inspect_swarm/_evidence.py`; `filesystem_audit` exported from `src/inspect_swarm/__init__.py` and imported by `src/inspect_swarm/_entrypoint.py`; `tests/test_filesystem_audit.py` with the stub-sandbox and Docker tests. After steps 1 to 3.
5. **inspect_swarm: metrics and docs.** `fs` events in `EventLog` (`src/inspect_swarm/_ledger.py`) and `FsMetrics`, `fs_unattributed` and `fs_incomplete` in `src/inspect_swarm/_metrics.py`, with `tests/test_metrics.py`; a section in `docs/channels.qmd` (what is recorded and attributed in each topology, what is missed, the `python3` requirement and why it is 3.8, declaring a shared volume) and a pointer from `docs/sandboxes.qmd`.

### Changes to other designs

For Ransom to approve, after this design:

- **swarm-communication.md, [The filesystem channel](swarm-communication.md#the-filesystem-channel)** and its Not this design item "Sandbox instrumentation for the filesystem channel": link here as that instrumentation, with its limit (no writer in a shared sandbox).
- **swarm-sandbox-isolation.md, [What the topology makes measurable](swarm-sandbox-isolation.md#what-the-topology-makes-measurable):** the audit sees modifications that the census cannot, unattributed; under users isolation it could attribute new and replaced files by owner.
- **swarm-implementation-plan.md, [Future work](swarm-implementation-plan.md#future-work):** a line for the filesystem audit, with step 1's inspect_ai fix.

## Open questions

None.

## Not this design

Noticed and left out, for Ransom to file if wanted:

- **`no_events()` drops other tasks' sandbox events today** (inspect_ai). Step 1 fixes it; it already affects every `sandbox_agent_bridge()` poll in a sandbox where other tools run, which a swarm's shared sandbox with a bridged member is.
- **Attributing new and replaced files by owner under users isolation**, once users isolation is built: map the owner's uid to its member for `wrote` events with `new` or `replaced`. Under it the watcher would also need its own identity, since members would no longer share the default user.
- **An analysis helper** that reads a sample's `fs` events as rows, rebuilds each path's recorded versions, and lists, for each unattributed change, the members whose tool calls overlapped its time bounds.
- **`fs` events in members' timelines.** They are in the swarm's span, so a member's timeline in the viewer does not show its file changes.
- **inotify as a trigger** for immediate scans inside the same watcher, for sharper times.
- **A final snapshot** of the watched trees (paths, sizes, hashes) at the swarm's end, for scorers and analysis that want the end state without replaying events.
