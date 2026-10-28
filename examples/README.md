# Examples

Runnable swarms. Each file defines one task at the bottom. Read them in this order:

| Example | What it shows | Needs |
|---------|---------------|-------|
| `getting_started.py` | `k` `react()` members on each question; the first submission is the answer | |
| `shared_memory.py` | The shared-memory channel: members split a count, `append` their parts to a board, and wait for each other with `swarm_wait` | |
| `stop_on_proof.py` | Writing a controller: `until_proved()` stops the swarm at the first checked answer, and a member builder that makes a normal return mean a passed check | |
| `budget_sweep.py` | Agent count, budget and split as task parameters; the `swarm_resources()` tool | Cost data for the model |
| `shared_sandbox.py` | Members with `bash()` in one shared container, the `filesystem` channel, and a scorer that reads what they left | Docker |
| `proof_swarm.py` | A verifier-gated swarm shaped like Epoch AI's LeanOpenProblems port: a container per member, a shared checking service that reads each member's workspace, a shared-memory board, `until_proved()`, and a scorer that checks each member's answer | Docker |

`questions.py` holds the questions that `getting_started.py`, `stop_on_proof.py` and `budget_sweep.py` share. `compose.yaml` and `proof_swarm.yaml` are the Docker configurations of the last two.

## Running

Install Inspect Swarm ([installation](../README.md#installation)) and your model provider's SDK, then run an example from the repository root:

```bash
inspect eval examples/getting_started.py --model openai/gpt-5-mini
```

Task parameters go in `-T`:

```bash
inspect eval examples/getting_started.py --model openai/gpt-5-mini -T k=1
inspect eval examples/stop_on_proof.py --model openai/gpt-5-mini -T stop=all_done
inspect eval examples/budget_sweep.py --model openai/gpt-5-mini -T k=8 -T budget=8 -T split=equal
```

Each example caps its samples with a token or cost limit, from which the swarm takes its budget.

## Tests

`tests/test_examples.py` runs every example with scripted mockllm members. The four that need no sandbox run in CI. `shared_sandbox.py` and `proof_swarm.py` need Docker, so their tests are marked slow and run with `pytest --runslow`.
