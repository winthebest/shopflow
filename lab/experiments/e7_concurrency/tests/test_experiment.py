"""The lab runner's adapter: warm-up, iterations in ABBA order with the runner's seeds, one row per metric."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from e7_concurrency import experiment


@dataclass(frozen=True)
class Row:
    arm: str
    case: str
    metric: str
    value: float
    unit: str
    iteration: int = 0


SCENARIO = {
    "id": "holdout-w1-n8-high",
    "iterations": 2,
    "arms": ["baseline", "method"],
    "params": {"workload": "w1", "writers": 8, "contention": "high", "maintenance": "optimize"},
}
CONTEXT = SimpleNamespace(run_id="r20261018t120000123", seed=500)


def test_runs_warm_up_then_abba_with_the_runners_seeds():
    calls = []

    def run_one(params: dict, arm: str, seed: int, out: str) -> dict:
        calls.append((params["name"], params.get("seconds"), arm, seed, out))
        return {"valid": True, "cct": 2.5, "lost_update": 0, "latency_p95_ms": None}

    rows = experiment.run([SCENARIO], CONTEXT, run_one=run_one, row_type=Row)
    out = "/results/e7_concurrency/r20261018t120000123"
    assert calls == [
        ("holdout-w1-n8-high-warmup", 10.0, "baseline", 500, out),
        ("holdout-w1-n8-high", None, "baseline", 500, out),
        ("holdout-w1-n8-high", None, "method", 500, out),
        ("holdout-w1-n8-high", None, "method", 501, out),
        ("holdout-w1-n8-high", None, "baseline", 501, out),
    ]
    assert len(rows) == 4 * 3  # warm-up not recorded; latency_p95_ms None is left out
    assert Row("method", "holdout-w1-n8-high", "cct", 2.5, "commits/s", 1) in rows
    assert Row("baseline", "holdout-w1-n8-high", "valid", 1.0, "bool", 0) in rows


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"iterations": None, "remove": "iterations"}, "has no iterations"),
        ({"params": {**SCENARIO["params"], "seed": 1}}, "seed comes from the lab runner"),
        ({"params": {**SCENARIO["params"], "writers": 0}}, "at least one writer"),
    ],
)
def test_scenarios_are_checked_before_anything_runs(change: dict, message: str):
    scenario = {**SCENARIO, **{k: v for k, v in change.items() if k != "remove"}}
    scenario.pop(change.get("remove", ""), None)
    with pytest.raises(ValueError, match=message):
        experiment.run([scenario], CONTEXT, run_one=lambda *_: pytest.fail("ran"), row_type=Row)


def test_result_paths_map_from_the_container_to_the_host(tmp_path: Path):
    assert experiment.host_path("/results/e7_concurrency/r1/x/result.json", tmp_path) == (
        tmp_path / "e7_concurrency/r1/x/result.json"
    )
    with pytest.raises(ValueError, match="outside /results"):
        experiment.host_path("/repo/lab/x.json", tmp_path)
