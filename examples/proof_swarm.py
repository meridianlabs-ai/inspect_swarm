"""A verifier-gated swarm with per-member sandboxes and a shared-memory board.

Its shape follows a port of Epoch AI's LeanOpenProblems swarm, with a toy problem in place of a Lean proof:

- Each member works in its own container and reaches one shared service, the `comparator` that checks answers, by name (`visible_sandboxes`).
- The comparator mounts each member's `/workspace` read-only, at `/answers/<member>`, and checks an answer with one fixed command that exits non-zero unless the answer is proved.
- Members share what they learn on a `shared_memory()` board, the only channel between them: they share no files.
- `prover()` is a member builder registered with `@agent`. It gives each member a `check` tool, checks the member's answer again when its agent returns, and raises a member-local custom limit when that check fails. So `submitted` means the answer the member leaves passes, and `until_proved()` can stop the swarm at the first proof.
- The scorer takes each member's sandbox from the swarm's result record, and checks every member's answer again.

Needs Docker. `proof_swarm.yaml` has a container for each of `agent-1` to `agent-3`, so `k` is from 1 to 3.
"""

from typing import Any, Literal

from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    scorer,
)
from inspect_ai.solver import TaskState
from inspect_ai.tool import ToolDef, bash
from inspect_ai.util import LimitExceededError, sandbox
from stop_on_proof import until_proved

from inspect_swarm import (
    leaderless,
    member,
    shared_memory,
    swarm,
    swarm_tools,
)

PROMPT = """\
Find a whole number n from 1 to 100 for which n * n + n + 41 is not prime.

Write n, and nothing else, to /workspace/answer.txt, then call the check tool, which checks it. When a check passes, submit n. Before you test a value of n, view /shared_memory/ruled_out.md, and append each value you rule out to it, so that no member tests a value twice.
"""

# proof_swarm.yaml has a container for each of agent-1 to agent-3
MEMBERS = 3

# the comparator's check of the answer file a member left, run in the comparator's
# own container; it exits 0 only for a proof
VERIFY = """\
import sys

def check(path):
    try:
        with open(path) as f:
            n = int(f.read())
    except FileNotFoundError:
        return "FAIL: there is no answer file"
    except ValueError:
        return "FAIL: the answer is not a whole number"
    v = n * n + n + 41
    if not 1 <= n <= 100:
        return "FAIL: n is not from 1 to 100"
    if all(v % d for d in range(2, int(v**0.5) + 1)):
        return f"FAIL: {v} is prime"
    return f"PASS: {v} is not prime"

result = check(sys.argv[1])
print(result)
sys.exit(0 if result.startswith("PASS") else 1)
"""


async def run_check(member_sandbox: str) -> tuple[bool, str]:
    """Check the answer a member left in its sandbox, with the comparator: whether it is a proof, and why."""
    result = await sandbox("comparator").exec(
        ["python3", "-c", VERIFY, f"/answers/{member_sandbox}/answer.txt"],
        timeout=60,
    )
    return result.success, result.stdout.strip() or result.stderr.strip()


@agent
def prover(sandbox_name: str) -> Agent:
    """A member that may return normally only when the answer it leaves passes the check."""

    async def check() -> str:
        """Check the answer in /workspace/answer.txt.

        Returns:
            PASS or FAIL, and why.
        """
        _, result = await run_check(sandbox_name)
        return result

    inner = react(tools=[bash(timeout=60), ToolDef(check).as_tool(), swarm_tools()])

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        # the answer as the member leaves it, not as it was at an earlier check
        proved, result = await run_check(sandbox_name)
        if not proved:
            # with no `source`, this ends only this member, as member_limit:custom
            raise LimitExceededError(
                "custom",
                value=0,
                limit=0,
                message=f"returned without a proof: {result}",
            )
        return state

    return execute


@scorer(metrics=[accuracy()])
def any_member_proved() -> Scorer:
    """Check every member's answer again, with the comparator."""

    async def score(state: TaskState, target: Target) -> Score:
        # the swarm's result record; task code reads it but never writes it
        record: dict[str, Any] = state.store.get("inspect_swarm:result")
        checks: dict[str, str] = {}
        proved: list[str] = []
        for m in record["members"]:
            passed, checks[m["name"]] = await run_check(m["sandbox"])
            if passed:
                proved.append(m["name"])
        return Score(
            value=CORRECT if proved else INCORRECT,
            answer=state.output.completion,
            explanation=f"proved by {', '.join(proved)}"
            if proved
            else "no member proved it",
            metadata=checks,
        )

    return score


@task
def proof_swarm(
    k: int = 3, stop: Literal["first_proof", "all_done"] = "first_proof"
) -> Task:
    if not 1 <= k <= MEMBERS:
        raise ValueError(f"proof_swarm: k must be from 1 to {MEMBERS}, got {k}.")
    return Task(
        dataset=[Sample(input=PROMPT)],
        solver=swarm(
            # one record per member, so that every k names its members, and their
            # containers, agent-1 to agent-k (a count of 1 would name it `agent`)
            members=[
                member(
                    prover,
                    args={"sandbox_name": f"agent-{i}"},
                    name=f"agent-{i}",
                    sandbox=f"agent-{i}",
                    visible_sandboxes=["comparator"],
                )
                for i in range(1, k + 1)
            ],
            controller=until_proved()
            if stop == "first_proof"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=any_member_proved(),
        sandbox=("docker", "proof_swarm.yaml"),
        token_limit=2_000_000,
    )
