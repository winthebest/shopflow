"""The dev split: N ∈ {2, 4} x {low, medium}, both workloads, plus the counters without the ledger (its cost).

    cd lab/experiments && uv run --project .. python -m e7_concurrency.dev --arms baseline [--runs 7] [--seconds 45]

One warm-up run first (recorded with `warmup: true`, never analysed). Then every scenario runs `--runs` times, the
arms in ABBA order; repetition r of a scenario uses seed base + r for every arm, so the arms of one repetition write
the same ops. Rows are appended to DIR/dev-<utc>.jsonl (default $LAB_RESULTS_DIR/e7_concurrency, else out/lab-e7).
Held-out never runs from here: it goes through the lab's runner (freeze and ledger).
"""

import argparse
import dataclasses
import json
import os
import time
from pathlib import Path

from .runner import run
from .scenario import BASELINE, Arm, Scenario

REPO = Path(__file__).resolve().parents[3]
ARMS: dict[str, Arm] = {"baseline": BASELINE}


def dev_scenarios() -> list[Scenario]:
    scenarios = []
    for workload in ("w1", "w2"):
        for writers in (2, 4):
            for contention in ("low", "medium"):
                seed = 1000 + 100 * (workload == "w2") + 10 * writers + (contention == "medium")
                name = f"dev-{workload}-n{writers}-{contention}"
                scenarios.append(Scenario(name, workload, writers, contention, seed))
    scenarios += [
        dataclasses.replace(s, name=f"{s.name}-noledger", ledger=False)
        for s in scenarios
        if s.workload == "w2" and s.contention == "medium"
    ]
    return scenarios


def abba(arms: list[Arm], repetition: int) -> list[Arm]:
    return arms if repetition % 2 == 0 else arms[::-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=["baseline"])
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--only", default="", help="run only scenarios whose name contains this")
    results = os.environ.get("LAB_RESULTS_DIR")
    parser.add_argument(
        "--out", type=Path, default=Path(results) / "e7_concurrency" if results else REPO / "out/lab-e7"
    )
    args = parser.parse_args()
    arms = [ARMS[name] for name in args.arms]
    scenarios = [dataclasses.replace(s, seconds=args.seconds) for s in dev_scenarios() if args.only in s.name]
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"dev-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl"

    def record(row: dict) -> None:
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"{row['run_id']}: valid={row['valid']} cct={row['cct']} lost={row.get('lost_update')}")

    warmup = dataclasses.replace(scenarios[0], name=f"{scenarios[0].name}-warmup", seconds=10.0)
    record(run(warmup, arms[0], args.out) | {"warmup": True, "repetition": None})
    for scenario in scenarios:
        for repetition in range(args.runs):
            seeded = dataclasses.replace(scenario, seed=scenario.seed + repetition)
            for arm in abba(arms, repetition):
                record(run(seeded, arm, args.out) | {"warmup": False, "repetition": repetition})
    print(f"rows: {path}")


if __name__ == "__main__":
    main()
