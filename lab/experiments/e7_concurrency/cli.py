"""One run from the command line, for the lab runner's adapter (which runs on the host and starts this in the lab
network with `lab/stack.sh py exp -- …`):

    python -m e7_concurrency.cli run --scenario-stdin --arm method [--seed N] [--out DIR] < scenario.json

The scenario, a JSON object of `Scenario` fields, comes on stdin: a held-out scenario stays secret until it is
published, and a command-line argument would show in `ps` and in the container's metadata. `--seed` overrides the
scenario's seed (the lab runner's seed). The run's op logs and result.json go to DIR/<run id>/ (default
$LAB_RESULTS_DIR/e7_concurrency, else out/lab-e7); the last line printed is the path of result.json.
"""

import argparse
import json
import os
import sys
from dataclasses import fields
from pathlib import Path

from .arms import ARMS
from .runner import run
from .scenario import Scenario

REPO = Path(__file__).resolve().parents[3]


def scenario_from(data: dict) -> Scenario:
    known = {f.name for f in fields(Scenario)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown scenario fields: {', '.join(unknown)}")
    return Scenario(**data)


def default_out() -> Path:
    results = os.environ.get("LAB_RESULTS_DIR")
    return Path(results) / "e7_concurrency" if results else REPO / "out/lab-e7"


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="One run of the concurrent-writers experiment")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("run")
    one.add_argument("--scenario-stdin", action="store_true", required=True, help="read the scenario from stdin")
    one.add_argument("--arm", required=True, choices=sorted(ARMS))
    one.add_argument("--seed", type=int, default=None)
    one.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    out = args.out or default_out()
    data = json.loads(sys.stdin.read())
    if args.seed is not None:
        data["seed"] = args.seed
    row = run(scenario_from(data), ARMS[args.arm], out)
    path = out / row["run_id"] / "result.json"
    print(path)
    return path


if __name__ == "__main__":
    main()
