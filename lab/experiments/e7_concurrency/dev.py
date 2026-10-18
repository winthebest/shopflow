"""The dev split: N ∈ {2, 4} x {low, medium}, both workloads, plus the counters without the ledger (its cost).

    cd lab/experiments && uv run --project .. python -m e7_concurrency.dev --arms baseline method [--runs 7]

Ten short warm-up runs first (`--warmups`; recorded with `warmup: true`, never analysed). Then every scenario runs
`--runs` times, the arms in ABBA order; repetition r of a scenario uses seed base + r for every arm, so the arms of
one repetition write the same ops. Rows are appended to DIR/dev-<utc>.jsonl (default $LAB_RESULTS_DIR/e7_concurrency,
else out/lab-e7). Held-out never runs from here: it goes through the lab's runner (freeze and ledger).
"""

import argparse
import dataclasses
import json
import subprocess
import time
from pathlib import Path

from .arms import ARMS
from .cli import default_out
from .runner import run
from .scenario import Arm, Scenario


def dev_scenarios() -> list[Scenario]:
    scenarios = []
    for workload in ("w1", "w2"):
        for writers in (2, 4):
            for contention in ("low", "medium"):
                seed = 1000 + 100 * (workload == "w2") + 10 * writers + (contention == "medium")
                name = f"dev-{workload}-n{writers}-{contention}"
                scenarios.append(Scenario(name, workload, writers, contention, seed))
    medium = [s for s in scenarios if s.contention == "medium"]
    scenarios += [dataclasses.replace(s, name=f"{s.name}-noledger", ledger=False) for s in medium if s.workload == "w2"]
    # W3 on dev: the OPTIMIZE loop beside the writers, at N = 4 only (N = 8 with OPTIMIZE is held-out).
    scenarios += [
        dataclasses.replace(s, name=f"{s.name}-optimize", maintenance="optimize") for s in medium if s.writers == 4
    ]
    return scenarios


def abba(arms: list, repetition: int) -> list:
    return arms if repetition % 2 == 0 else arms[::-1]


def git_state() -> dict:
    """The commit the run's code came from, and whether lab/ or lakeops/ had uncommitted changes."""
    repo = Path(__file__).resolve().parents[3]

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout  # noqa: S603, S607

    return {
        "git_sha": git("rev-parse", "HEAD").strip(),
        "git_dirty": bool(git("status", "--porcelain", "lab", "lakeops").strip()),
    }


def runnable(scenario: Scenario, arm: Arm) -> bool:
    """The methods and W2-py are idempotent only through the ledger; W2-py writes counters only."""
    if arm.name in ("method", "method-resolve", "w2-py") and not scenario.ledger:
        return False
    return not (arm.name == "w2-py" and scenario.workload != "w2")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=["baseline"])
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--only", default="", help="run only scenarios whose name contains this")
    parser.add_argument("--warmups", type=int, default=10, help="short runs first, never analysed")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    args.out = args.out or default_out()
    arms = [ARMS[name] for name in args.arms]
    scenarios = [dataclasses.replace(s, seconds=args.seconds) for s in dev_scenarios() if args.only in s.name]
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"dev-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl"

    state = git_state()

    def record(row: dict) -> None:
        row |= state
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"{row['run_id']}: valid={row['valid']} cct={row['cct']} lost={row.get('lost_update')}")

    for i in range(args.warmups):
        warmup = dataclasses.replace(scenarios[0], name=f"{scenarios[0].name}-warmup", seconds=5.0)
        record(run(warmup, arms[i % len(arms)], args.out) | {"warmup": True, "repetition": None})
    for scenario in scenarios:
        for repetition in range(args.runs):
            seeded = dataclasses.replace(scenario, seed=scenario.seed + repetition)
            for arm in abba([a for a in arms if runnable(scenario, a)], repetition):
                record(run(seeded, arm, args.out) | {"warmup": False, "repetition": repetition})
    print(f"rows: {path}")


if __name__ == "__main__":
    main()
