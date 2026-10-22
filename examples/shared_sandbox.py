"""A shared sandbox: `bash()` members build one script together in a container they all use.

The default `filesystem` channel tells each member, in its preamble, to share files in `/shared`, to keep notes in `/shared/notes.md`, and to keep its own work in `/scratch/<member>`. The scorer runs the script the members left in `/shared`, so the swarm lets every member run to its end with `leaderless(stop_on_submission=False)`: a member still working when another submits can still change what is scored.

Needs Docker.
"""

from inspect_ai import Task, task
from inspect_ai.agent import react
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
from inspect_ai.tool import bash
from inspect_ai.util import sandbox

from inspect_swarm import leaderless, member, swarm

PROMPT = """\
Write /shared/wordcount.py, a Python script that prints the number of words in the text file named by its first argument, as a number only. Words are separated by whitespace.

Split the work: one member writes the script, the others write test files in their scratch directories and check the script on them. Fix what fails. Submit "done" when the script works.
"""

TEXT = "the quick brown fox\njumps over\n\nthe lazy dog\n"


@scorer(metrics=[accuracy()])
def script_works() -> Scorer:
    """Run `/shared/wordcount.py` on a file the members have not seen."""

    async def score(state: TaskState, target: Target) -> Score:
        await sandbox().write_file("/tmp/check.txt", TEXT)
        result = await sandbox().exec(
            ["python3", "/shared/wordcount.py", "/tmp/check.txt"], timeout=60
        )
        output = result.stdout.strip()
        return Score(
            value=CORRECT if result.success and output == target.text else INCORRECT,
            answer=output,
            explanation=result.stderr or None,
        )

    return score


@task
def shared_sandbox(k: int = 3) -> Task:
    return Task(
        dataset=[Sample(input=PROMPT, target="9", files={"/shared/notes.md": ""})],
        solver=swarm(
            members=member(react, args={"tools": [bash(timeout=60)]}, count=k),
            controller=leaderless(stop_on_submission=False),
        ),
        scorer=script_works(),
        sandbox=("docker", "compose.yaml"),
        token_limit=1_000_000,
    )
