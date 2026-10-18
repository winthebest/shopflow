"""E7's adapter for the lab runner (`make lab-run EXP=e7_concurrency SPLIT=dev|holdout`): `run(scenarios, context)`.

Runs on the host, after the runner's freeze and ledger checks. For each scenario it does `iterations` repetitions
with the arms in ABBA order. Each (scenario, arm, iteration) is one run of the e7 runner inside the lab network:

    lab/stack.sh py exp -- python -m e7_concurrency.cli run --scenario-stdin --arm A --seed S --out DIR

- The scenario goes on stdin: a held-out scenario stays secret until it is published, and a command-line argument
  would show in `ps` and in the container's metadata.
- Iteration i uses seed context.seed + i for every arm, so the arms of one iteration write the same ops.
- One warm-up run comes first and is not recorded.
- Each run's result.json becomes rows, one per metric. The full row, with the error classes and the settle counts,
  stays in that run's directory.

A scenario: {"id": …, "iterations": …, "arms": [...], "params": {Scenario fields except seed}}.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from .dev import abba
from .scenario import Scenario

REPO = Path(__file__).resolve().parents[3]
CONTAINER_RESULTS = "/results/e7_concurrency"
WARMUP_SECONDS = 10.0

# Metrics of a run's result, with their unit; a metric whose value is None (e.g. the CCT of an invalid run) is left
# out, and `valid` says why.
METRICS = {
    "valid": "bool",
    "cct": "commits/s",
    "lost_update": "ops",
    "duplicates": "ops",
    "wrong_final_rows": "rows",
    "acked_commits": "commits",
    "seconds": "s",
    "retries": "attempts",
    "abort_rate": "ratio",
    "unknown_rate": "ratio",
    "unknown_present": "ops",
    "latency_p50_ms": "ms",
    "latency_p95_ms": "ms",
    "write_amplification": "ratio",
    "max_ledger": "entries",
}

RunOne = Callable[[dict, str, int, str], dict]  # (scenario params, arm, seed, container out dir) -> result


def rows_from(result: dict, *, arm: str, case: str, iteration: int, row_type: type) -> list:
    return [
        row_type(arm=arm, case=case, metric=metric, value=float(result[metric]), unit=unit, iteration=iteration)
        for metric, unit in METRICS.items()
        if result.get(metric) is not None
    ]


def check(scenario: dict) -> None:
    """Refuses a scenario the e7 runner could not run, before anything starts."""
    missing = {"id", "iterations", "arms", "params"} - scenario.keys()
    if missing:
        raise ValueError(f"scenario {scenario.get('id')!r} has no {', '.join(sorted(missing))}")
    if "seed" in scenario["params"]:
        raise ValueError(f"scenario {scenario['id']}: the seed comes from the lab runner, not the scenario")
    Scenario(name=scenario["id"], seed=0, **scenario["params"])  # every field valid


def run(scenarios: list[dict], context, *, run_one: RunOne | None = None, row_type: type | None = None) -> list:
    if row_type is None:
        from lab.results import Row

        row_type = Row

    run_one = run_one or stack_run_one
    for scenario in scenarios:
        check(scenario)
    out = f"{CONTAINER_RESULTS}/{context.run_id}"
    first = scenarios[0]
    warmup = {**first["params"], "name": f"{first['id']}-warmup", "seconds": WARMUP_SECONDS}
    run_one(warmup, first["arms"][0], context.seed, out)

    rows = []
    for scenario in scenarios:
        params = {**scenario["params"], "name": scenario["id"]}
        for iteration in range(scenario["iterations"]):
            for arm in abba(scenario["arms"], iteration):
                result = run_one(params, arm, context.seed + iteration, out)
                rows += rows_from(result, arm=arm, case=scenario["id"], iteration=iteration, row_type=row_type)
    return rows


def host_path(container_path: str, host_results: Path) -> Path:
    if not container_path.startswith("/results/"):
        raise ValueError(f"result outside /results: {container_path}")
    return host_results / container_path.removeprefix("/results/")


def stack_run_one(params: dict, arm: str, seed: int, out: str, *, host_results: Path | None = None) -> dict:
    """One run in the lab network through `lab/stack.sh py exp`; reads back its result.json."""
    command = [str(REPO / "lab/stack.sh"), "py", "exp", "--", "python", "-m", "e7_concurrency.cli", "run"]
    command += ["--scenario-stdin", "--arm", arm, "--seed", str(seed), "--out", out]
    done = subprocess.run(command, input=json.dumps(params), text=True, check=True, capture_output=True)  # noqa: S603
    container_path = done.stdout.strip().splitlines()[-1]
    return json.loads(host_path(container_path, host_results or default_host_results()).read_text())


def default_host_results() -> Path:
    """The host directory lab/stack.sh py mounts at /results ($LAB_RESULTS_DIR, else <repo>/lab/results)."""
    from lab.results import results_dir

    return results_dir()
