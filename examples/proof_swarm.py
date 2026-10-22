"""A verifier-gated swarm with per-member sandboxes and a shared-memory board.

Its shape follows a port of Epoch AI's LeanOpenProblems swarm, with a toy problem in place of a Lean proof:

- Each member works in its own container (`sandbox="{member}"`) and reaches one shared service, the `comparator` that checks answers, by name (`visible_sandboxes`).
- Members share what they learn on a `shared_memory()` board, the only channel between them: they share no files.
- `prover()` is a member builder registered with `@agent`. It builds each member's own `check` tool and raises a member-local custom limit when its agent returns without a passing check, so `submitted` means checked and `until_proved()` can stop the swarm at the first proof.
- The scorer names each member's sandbox, from the swarm's result record, and checks every member's answer again.

Needs Docker. `proof_swarm.yaml` has three member containers, so `k` is at most 3.
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
    swarm_on_continue,
    swarm_tools,
)

PROMPT = """\
Find a whole number n from 1 to 100 for which n * n + n + 41 is not prime.

Write n, and nothing else, to /workspace/answer.txt, then call the check tool, which checks it. When a check passes, submit n. Before you test a value of n, view /shared_memory/ruled_out.md, and append each value you rule out to it, so that no member tests a value twice.
"""

ANSWER = "/workspace/answer.txt"

# the comparator's check, run in its own container
VERIFY = """\
import sys
try:
    n = int(sys.argv[1])
except ValueError:
    print("FAIL: the answer is not a whole number")
    raise SystemExit
v = n * n + n + 41
if not 1 <= n <= 100:
    print("FAIL: n is not from 1 to 100")
elif all(v % d for d in range(2, int(v**0.5) + 1)):
    print(f"FAIL: {v} is prime")
else:
    print(f"PASS: {v} is not prime")
"""


async def run_check(answer_sandbox: str | None) -> tuple[str | None, str]:
    """Check the answer file in a member's sandbox (`None`: the caller's own) with the comparator."""
    try:
        answer = (await sandbox(answer_sandbox).read_file(ANSWER)).strip()
    except FileNotFoundError:
        return None, f"FAIL: {ANSWER} does not exist"
    result = await sandbox("comparator").exec(
        ["python3", "-c", VERIFY, answer], timeout=60
    )
    return answer, result.stdout.strip() or result.stderr.strip()


@agent
def prover() -> Agent:
    """A member that may return normally only after a passing check."""
    # the swarm calls this builder once for each member in each sample, so the tool
    # and `verified` belong to one member
    verified = False

    async def check() -> str:
        """Check the answer in /workspace/answer.txt.

        Returns:
            PASS or FAIL, and why.
        """
        nonlocal verified
        _, result = await run_check(None)
        verified = verified or result.startswith("PASS")
        return result

    inner = react(
        tools=[bash(timeout=60), ToolDef(check).as_tool(), swarm_tools()],
        on_continue=swarm_on_continue(),
    )

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        if not verified:
            # with no `source`, this ends only this member, as member_limit:custom
            raise LimitExceededError(
                "custom", value=0, limit=0, message="returned without a passing check"
            )
        return state

    return execute


@scorer(metrics=[accuracy()])
def any_member_proved() -> Scorer:
    """Check every member's answer file again, in its own sandbox."""

    async def score(state: TaskState, target: Target) -> Score:
        # the swarm's result record; task code reads it but never writes it
        record: dict[str, Any] = state.store.get("inspect_swarm:result")
        checks: dict[str, dict[str, str | None]] = {}
        for m in record["members"]:
            answer, result = await run_check(m["sandbox"])
            checks[m["name"]] = {"answer": answer, "check": result}
        proved = [
            name for name, c in checks.items() if (c["check"] or "").startswith("PASS")
        ]
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
    return Task(
        dataset=[Sample(input=PROMPT)],
        solver=swarm(
            members=member(
                prover,
                name="agent",
                count=k,
                sandbox="{member}",
                visible_sandboxes=["comparator"],
            ),
            controller=until_proved()
            if stop == "first_proof"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=any_member_proved(),
        sandbox=("docker", "proof_swarm.yaml"),
        token_limit=2_000_000,
    )
