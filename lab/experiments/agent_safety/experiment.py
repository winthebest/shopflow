"""agent_safety: the operator agent's arms on the lab stack, judged by the independent scorer (docs/adr/0107).

    make lab-base RUN=<run>                        # the baseline arm's catalog, lab_base_<run>
    make lab-run EXP=agent_safety SPLIT=dev        # or holdout, once, after a dev run at the freeze

The lab runner hands `run` the split's scenarios (evals/datasets/operator/, schema in evals/prompt_set.py) and its
RunContext; the eval loop (evals/runner.py) runs every prompt for every model, repetition and arm, and its rows are
the run's results.

- Arms, each in its own catalog: the baseline in the active baseline run's catalog (`context.base_catalog`), built
  and used as baseline_writer. The guarded arm joins with the guard, in lab_ops.
- Model: the local qwen3:8b (the pinned build, its sampling seeded with the run's seed, unloaded after its tasks).
  Claude is not here: it runs only once the user has approved its cost.
- Repetitions: 1 on dev, for calibration; 3 on the held-out split.
- A repetition's tasks are built by one `lab.prodlike.prodlike_many`, in the same ticks for every catalog, all with
  the same data (`same_data`, seed SEED): a prompt's expected counts hold for every arm and repetition, and
  tests/test_dev_set.py checks dev.yaml's against `lab.prodlike.generate`. Then each prompt's `setup` runs in its
  task's eval schema, as the identity that built it, before the task's `before:` capture.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict

from agent.llm.ollama_provider import OllamaProvider
from evals.arms import BaselineArm
from evals.prompt_set import Prompt
from evals.runner import Arm, Namespaces, run_eval

from lab.guard import guarded_connect
from lab.prodlike import prodlike_many
from lab.results import Row
from lakeops.sql import query

SEED = 0  # the data of every task
REPETITIONS = {"dev": 1, "holdout": 3}


def builder(catalog: str) -> str:
    """The identity that builds a catalog's task tables: a baseline catalog's own writer, otherwise the executor."""
    return "baseline_writer" if catalog.startswith("lab_base_") else "executor"


def run(scenarios: list[dict], context) -> list[Row]:
    base = context.base_catalog
    if base is None:
        raise RuntimeError("no active baseline run: make lab-base RUN=<run> before running agent_safety")
    arms: list[Arm] = [BaselineArm()]
    catalogs = {"baseline": base}
    rows = run_eval(
        scenarios,
        context,
        arms=arms,
        models=[OllamaProvider(seed=context.seed)],
        prepare=lambda batch: prepare(batch, catalogs, context),
        repetitions=REPETITIONS[context.split],
    )
    return [Row(**asdict(row)) for row in rows]


def prepare(
    batch: Sequence[tuple[str, Arm, Prompt]],
    catalogs: Mapping[str, str],
    context,
    *,
    connect: Callable = guarded_connect,
    build: Callable = prodlike_many,
) -> dict[str, Namespaces]:
    """Builds a repetition's tasks in one go, then runs each prompt's setup in its task's eval schema."""
    targets = [(task, catalogs[arm.name]) for task, arm, _ in batch]
    conns = {
        catalog: connect(builder(catalog), max_attempts=1, settings=context.settings)
        for catalog in dict.fromkeys(catalog for _, catalog in targets)
    }
    built = build(conns, targets, same_data=True, seed=SEED, timescale=context.timescale)
    namespaces = {}
    for result, (_, _, prompt) in zip(built, batch, strict=True):
        schema = f'"{result.catalog}"."{result.namespace}"'
        for sql in prompt.setup:
            query(conns[result.catalog], sql.replace("{schema}", schema))
        namespaces[result.task] = Namespaces(result.catalog, {"eval": result.namespace, "bait": result.bait})
    return namespaces
