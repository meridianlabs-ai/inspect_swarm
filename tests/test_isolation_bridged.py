"""Bridged members under users isolation, in Docker: a Claude Code or Codex member beside a native member, each CLI scripted through its bridge's filter on mockllm.

Each sample runs one swarm: the native member `nat` and one CLI member, `cc` (Claude Code) or `cx` (Codex), one bridged agent type per sandbox. The CLI's filter answers its model requests: the CLI's first request with the swarm's tools holds until the native member has made its checks against the running proxy, then calls the bridged `list_members` tool, and the next request ends the CLI. Requests the checks send to the proxy's model route carry a marker, and the filter records the markers that reach it.

The CLIs are downloaded on the host and copied into the sandbox, which has no network.
"""

from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, as_solver
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalSample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    GenerateConfig,
    Model,
    ModelOutput,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo, bash
from inspect_ai.util import sandbox
from inspect_swe import claude_code, codex_cli

from inspect_swarm import (
    Controller,
    MemberEnded,
    StopReason,
    SwarmControl,
    member,
    messages,
    swarm,
    swarm_bridged_tools,
    swarm_controller,
    swarm_result,
    users,
)

_IMAGE = Path(__file__).parent / "isolation_bridged"

_COMPOSE = """\
services:
  default:
    build:
      context: {context}
    image: inspect-swarm-isolation-bridged-test
    command: tail -f /dev/null
    init: true
    network_mode: none
{extra}"""

# a loopback with no ::1, so the scoped proxy serves on 127.0.0.1 alone
_NO_IPV6 = "    sysctls:\n      net.ipv6.conf.lo.disable_ipv6: 1\n"

# inspect_swe numbers each agent type's bridge ports from 3000, per sample
_PORT = 3001

# Codex's release, as inspect_swe's own bridged tests pin it. With no `model_config`,
# inspect_swe gives a model that is not OpenAI's Codex's generic profile, which offers
# MCP tools directly rather than behind tool_search (which the bridge drops for such
# a model).
_CODEX_VERSION = "0.160.1"


@dataclass
class Case:
    cli: str
    """The CLI member's name: `cc` for Claude Code, `cx` for Codex."""
    checks: str
    """What the native member does: `all` (requests, binds and the retired-connection race against the running proxy), `ipv4` (none: the sandbox has no IPv6 loopback) or `listen-127.0.0.1` / `listen-::1` (listen first on the bridge's port at that address)."""
    ipv6: bool = True
    cli_up: anyio.Event = field(default_factory=anyio.Event)
    native_done: anyio.Event = field(default_factory=anyio.Event)
    listening: anyio.Event = field(default_factory=anyio.Event)
    cli_ended: anyio.Event = field(default_factory=anyio.Event)
    markers: list[str] = field(default_factory=lambda: [])
    """The markers of the requests that reached the filter."""
    tools: list[list[str]] = field(default_factory=lambda: [])
    """The tools each of the CLI's own requests offered, for failure messages."""
    tool_result: str | None = None
    """What the CLI's `list_members` call returned."""
    results: dict[str, Any] = field(default_factory=lambda: {})
    """What the native member saw."""


_CASES: dict[str, Case] = {}


# a request on the proxy's model route, carrying a marker the filter records
def _request(marker: str) -> str:
    return f'{{"model": "inspect", "max_tokens": 16, "messages": [{{"role": "user", "content": "{marker}"}}]}}'


def _script(case: Case) -> Any:
    async def script(
        model: Model,
        messages: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice | None,
        config: GenerateConfig,
    ) -> ModelOutput | None:
        text = messages[-1].text if messages else ""
        for marker in ("native-retired", "member-curl", "peer-curl", "root-curl"):
            if marker in text:
                case.markers.append(marker)
                return ModelOutput.from_content(str(model), "answered")
        case.tools.append([t.name for t in tools])
        names = [t.name for t in tools if t.name.endswith("list_members")]
        if not names:
            # the CLI's own utility requests, such as a title
            return ModelOutput.from_content(str(model), "ok")
        if isinstance(messages[-1], ChatMessageTool):
            case.tool_result = messages[-1].text
            return ModelOutput.from_content(str(model), "done")
        # the CLI runs, so its proxy serves: let the native member check it first
        case.cli_up.set()
        with anyio.fail_after(300):
            await case.native_done.wait()
        call = ToolCall(id="call-1", function=names[0], arguments={})
        return ModelOutput.from_message(
            ChatMessageAssistant(content="", tool_calls=[call])
        )

    return script


@agent
def cli(case: str) -> Agent:
    spec = swarm_bridged_tools(["messages"])
    if _CASES[case].cli == "cc":
        return claude_code(
            bridged_tools=[spec],
            filter=_script(_CASES[case]),
            model_config="claude-sonnet-4-5",
        )
    return codex_cli(
        bridged_tools=[spec],
        filter=_script(_CASES[case]),
        version=_CODEX_VERSION,
    )


async def _root(command: str) -> str:
    result = await sandbox().exec(["sh", "-c", command], user="root")
    return result.stdout + result.stderr


async def _as_member(command: str) -> str:
    return str(await bash(timeout=60)(command))


# a request to the proxy, printing its body and then its HTTP status
def _curl(host: str, path: str, marker: str, extra: str = "") -> str:
    return f"curl -s -g --max-time 30 {extra} -w '\\n%{{http_code}}' -H 'content-type: application/json' --data '{_request(marker)}' 'http://{host}:{_PORT}{path}'"


# the tests' own checks, in the image (tests/isolation_bridged/checks.py)
def _check(*args: object) -> str:
    return " ".join(["python3", "/opt/checks.py", *(f"'{a}'" for a in args)])


async def _race(case: Case, host: str, local_port: int) -> dict[str, str]:
    """With the proxy stopped, the native member retires a connection and the CLI's member connects from the same address pair; then the proxy continues."""
    [pid] = (await _root(_check("listener", _PORT))).split()
    await _root(f"kill -STOP {pid}")
    out: dict[str, str] = {}
    try:
        out["retire"] = await _as_member(_check("retire", host, local_port, _PORT))

        async def member_curl() -> None:
            curl = _curl(
                f"[{host}]" if ":" in host else host,
                "/v1/messages",
                "member-curl",
                f"--local-port {local_port}",
            )
            result = await sandbox().exec(["sh", "-c", curl], user=f"swarm-{case.cli}")
            out["member"] = result.stdout + result.stderr

        async with anyio.create_task_group() as tg:
            tg.start_soon(member_curl)
            # the member's connection waits in the stopped proxy's accept queue
            out["connected"] = await _root(
                _check("connected", host, local_port, _PORT, f"swarm-{case.cli}")
            )
            await _root(f"kill -CONT {pid}")
    finally:
        await _root(f"kill -CONT {pid}")
    return out


async def _check_running_proxy(case: Case) -> None:
    results = case.results
    # the owners of the processes running the CLI's binary, by their first argument:
    # without CAP_SYS_PTRACE root cannot read another user's /proc/<pid>/exe
    results["cli_users"] = await _root(
        "for p in /proc/[0-9]*; do tr '\\0' '\\n' < $p/cmdline 2>/dev/null | head -n 1 | grep -q -e /claude- -e /codex && stat -c %U $p; done | sort -u"
    )
    results["cli_home"] = await _root(
        f"find /scratch/{case.cli} -mindepth 1 -maxdepth 1 -printf '%u %f\\n'"
    )
    hosts = ["127.0.0.1", "[::1]"] if case.ipv6 else ["127.0.0.1"]
    for host in hosts:
        for path in ("/v1/messages", "/mcp/swarm"):
            results[f"peer {host}{path}"] = await _as_member(
                _curl(host, path, "peer-curl")
            )
            results[f"root {host}{path}"] = await _root(_curl(host, path, "root-curl"))
    if case.checks == "ipv4":
        results["if_inet6"] = await _root("cat /proc/net/if_inet6")
        return
    results["binds"] = await _as_member(_check("binds", _PORT))
    results["race 127.0.0.1"] = await _race(case, "127.0.0.1", 41001)
    results["race ::1"] = await _race(case, "::1", 41002)


# listen at the bridge's port before the bridge starts, recording each connection
def _listener(host: str) -> str:
    log = "/scratch/nat/listener.log"
    return f"""nohup {_check("listen", host, _PORT, log)} > /dev/null 2>&1 &
for i in $(seq 50); do grep -q listening {log} 2>/dev/null && break; sleep 0.1; done
cat {log}"""


@agent
def native(case: str) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        current = _CASES[case]
        try:
            if current.checks.startswith("listen-"):
                host = current.checks.removeprefix("listen-")
                current.results["listener"] = await _as_member(_listener(host))
                current.listening.set()
                # the CLI's failed bridge fails the swarm, which cancels this wait
                with anyio.fail_after(300):
                    await current.cli_ended.wait()
            else:
                # until the CLI's first request, or the CLI's end if it makes none
                with anyio.fail_after(600):
                    while not (current.cli_up.is_set() or current.cli_ended.is_set()):
                        await anyio.sleep(0.2)
                if current.cli_up.is_set():
                    await _check_running_proxy(current)
        finally:
            current.native_done.set()
        return state

    return execute


async def _start_both(control: SwarmControl, case: str) -> StopReason:
    current = _CASES[case]
    nat, cli_member = control.members
    control.start(nat)
    if current.checks.startswith("listen-"):
        with anyio.fail_after(300):
            await current.listening.wait()
    control.start(cli_member)
    events = control.events()
    async with aclosing(events):
        async for event in events:
            if isinstance(event, MemberEnded) and event.member == current.cli:
                current.cli_ended.set()
    return "all_done"


@swarm_controller(name="isolation_bridged_test_start_both")
def start_both(case: str) -> Controller:
    async def run(control: SwarmControl) -> StopReason:
        return await _start_both(control, case)

    return Controller(run)


@solver
def case_solver() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        case = str(state.sample_id)
        team = swarm(
            [
                member(native(case), name="nat"),
                member(cli(case), name=_CASES[case].cli, bridged=True),
            ],
            controller=start_both(case),
            channels=[messages()],
            isolation=users(shared=[]),
        )
        if not _CASES[case].checks.startswith("listen-"):
            return await as_solver(team)(state, generate)
        try:
            return await as_solver(team)(state, generate)
        except Exception as ex:
            _CASES[case].results["error"] = repr(ex)
        _CASES[case].results["accepted"] = await _root("cat /scratch/nat/listener.log")
        return state

    return solve


def _eval(tmp_path: Path, cases: dict[str, Case]) -> dict[str, EvalSample]:
    _CASES.update(cases)
    samples: list[Sample] = []
    for name, case in cases.items():
        compose = tmp_path / f"compose-{name}.yaml"
        compose.write_text(
            _COMPOSE.format(context=_IMAGE, extra="" if case.ipv6 else _NO_IPV6)
        )
        samples.append(Sample(id=name, input="x", sandbox=("docker", str(compose))))
    [log] = eval(
        Task(dataset=samples, solver=case_solver()),
        model="mockllm/model",
        log_dir=str(tmp_path / "logs"),
        display="none",
        fail_on_error=False,
    )
    assert log.samples is not None
    return {str(s.id): s for s in log.samples}


def _status(output: str) -> tuple[str, str]:
    # curl's body, then its HTTP status on the last line
    body, _, status = output.rpartition("\n")
    return body, status.strip()


@pytest.mark.slow
@pytest.mark.parametrize("cli_name", ["cc", "cx"])
def test_bridged_member_runs_as_its_user_behind_its_proxy(
    tmp_path: Path, cli_name: str
) -> None:
    name = f"proxy-{cli_name}"
    samples = _eval(tmp_path, {name: Case(cli=cli_name, checks="all")})
    case = _CASES[name]
    error = samples[name].error
    assert error is None, error.message if error else ""
    user = f"swarm-{cli_name}"
    # the CLI started, passed its MCP readiness check and called a bridged tool
    assert case.tool_result is not None and "nat" in case.tool_result, case.tools
    # it ran as its member, with its config in its home
    assert case.results["cli_users"].split() == [user], case.results["cli_users"]
    home = case.results["cli_home"].splitlines()
    assert {line.split()[0] for line in home} == {user}, home
    config = ".claude" if cli_name == "cc" else ".codex"
    assert config in {line.split()[1] for line in home}, home
    # the peer's and root's requests are refused, on a model route and on /mcp/, at
    # both loopback addresses, and none reaches the model
    for who in ("peer", "root"):
        for host in ("127.0.0.1", "[::1]"):
            for path in ("/v1/messages", "/mcp/swarm"):
                body, status = _status(case.results[f"{who} {host}{path}"])
                assert status == "403", (who, host, path, body)
                assert f"serves only user '{user}'" in body, body
    # the peer cannot bind the proxy's port at any address, with or without
    # SO_REUSEPORT
    binds = case.results["binds"].split("\n")
    binds = [line for line in binds if line.strip()]
    assert len(binds) == 8 and all(line.endswith("EADDRINUSE") for line in binds), binds
    # a connection the peer retired before the proxy accepted it is not routed; the
    # member's connection from the same address pair is answered
    for host in ("127.0.0.1", "::1"):
        race = case.results[f"race {host}"]
        assert race["retire"].strip() == "reset", race
        assert race["connected"].strip() == "connected", race
        assert _status(race["member"])[1] == "200", race
    assert sorted(case.markers) == ["member-curl", "member-curl"]
    # the CLI's processes were gone when it ended
    record = swarm_result(samples[name])
    assert record is not None
    [cli_record] = [m for m in record.members if m.name == cli_name]
    assert cli_record.reap is not None and cli_record.reap.status == "ok"
    assert record.isolation_incomplete is False


@pytest.mark.slow
@pytest.mark.parametrize("cli_name", ["cc", "cx"])
def test_bridged_member_runs_without_an_ipv6_loopback(
    tmp_path: Path, cli_name: str
) -> None:
    name = f"ipv4-{cli_name}"
    samples = _eval(tmp_path, {name: Case(cli=cli_name, checks="ipv4", ipv6=False)})
    case = _CASES[name]
    error = samples[name].error
    assert error is None, error.message if error else ""
    assert case.results["if_inet6"].strip() == ""
    assert case.tool_result is not None and "nat" in case.tool_result, case.tools
    for who in ("peer", "root"):
        for path in ("/v1/messages", "/mcp/swarm"):
            assert _status(case.results[f"{who} 127.0.0.1{path}"])[1] == "403"


@pytest.mark.slow
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_peer_listening_first_fails_the_bridge_before_the_cli_starts(
    tmp_path: Path, host: str
) -> None:
    name = f"listen-{host}"
    samples = _eval(tmp_path, {name: Case(cli="cc", checks=f"listen-{host}")})
    case = _CASES[name]
    assert samples[name].error is None
    assert case.results["listener"].strip() == "listening", case.results
    # the proxy could not bind, so the bridge failed, and with it the swarm
    address = f"[{host}]" if ":" in host else host
    assert (
        f"cannot listen on {address}:{_PORT}: Address already in use"
        in case.results["error"]
    ), case.results["error"]
    # the CLI never started, and nothing connected to the peer's listener
    assert not case.cli_up.is_set() and case.tool_result is None
    assert case.results["accepted"].split() == ["listening"]
