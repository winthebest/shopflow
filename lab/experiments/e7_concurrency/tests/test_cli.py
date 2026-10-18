"""The command line the lab runner's adapter calls: a scenario from JSON, an arm by name, the result's path."""

import io
import json
from pathlib import Path

import pytest
from e7_concurrency import cli
from e7_concurrency.arms import ARMS


def test_every_arm_has_one_name():
    assert sorted(ARMS) == ["baseline", "method", "w2-py"]


def test_a_scenario_comes_from_its_fields():
    s = cli.scenario_from({"name": "h", "workload": "w1", "writers": 8, "contention": "high", "seed": 5})
    assert (s.writers, s.contention, s.seconds) == (8, "high", 45.0)
    with pytest.raises(ValueError, match="unknown scenario fields: speed"):
        cli.scenario_from({"name": "h", "workload": "w1", "writers": 8, "contention": "high", "seed": 5, "speed": 1})


def test_run_prints_where_its_result_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    seen = {}

    def fake_run(scenario, arm, out):
        seen.update(scenario=scenario.name, seed=scenario.seed, arm=arm.name, out=out)
        return {"run_id": "r1"}

    monkeypatch.setattr(cli, "run", fake_run)
    scenario = json.dumps({"name": "h", "workload": "w2", "writers": 2, "contention": "low", "seed": 1})
    monkeypatch.setattr("sys.stdin", io.StringIO(scenario))
    path = cli.main(["run", "--scenario-stdin", "--arm", "method", "--seed", "42", "--out", str(tmp_path)])
    assert path == tmp_path / "r1" / "result.json"
    assert seen == {"scenario": "h", "seed": 42, "arm": "method", "out": tmp_path}
    assert capsys.readouterr().out.strip().splitlines()[-1] == str(path)


def test_results_default_to_the_lab_results_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAB_RESULTS_DIR", "/results")
    assert cli.default_out() == Path("/results/e7_concurrency")
