"""E5's incident parameter schema: dev.yaml and the design's example follow it, and a held-out file outside it is
refused, whichever rule it breaks."""

import copy
import math
import re
from datetime import timedelta

import pytest
import yaml
from e5_retention.params import DEV_PRIMARY, ParamsError, check_heavier_tail, check_scenarios, parse

from lab import scenarios
from lab.trino_client import REPO

DESIGN = REPO / "docs/research/e5-retention-policy-design.md"
HOLDOUT = {  # a held-out primary within every rule: 8 % above 48 h, up to 21 days
    "id": "primary",
    "seed": 4242,
    "incidents": 300,
    "classes": {"logic": 0.75, "schema": 0.12, "infra": 0.13},
    "delays": [
        {"from": "1m", "to": "1h", "share": 0.67},
        {"from": "1h", "to": "48h", "share": 0.25},
        {"from": "3d", "to": "21d", "share": 0.08},
    ],
}


def holdout(**changes) -> dict:
    scenario = copy.deepcopy(HOLDOUT)
    scenario.update(changes)
    return scenario


def test_dev_yaml_follows_the_schema_and_its_primary_is_the_published_one():
    params = check_scenarios(scenarios.load_dev("e5_retention"), "dev")
    assert [p.id for p in params] == ["primary", "tail_10", "tail_20"]
    assert params[0] == DEV_PRIMARY
    assert DEV_PRIMARY.tail_share() == pytest.approx(0.05) and DEV_PRIMARY.longest() == timedelta(days=14)


def test_the_design_example_follows_the_schema():
    block = re.search(r"```yaml\n(scenarios:.*?)```", DESIGN.read_text(), re.S)
    assert block, "the design has no YAML example of the parameters"
    assert parse(yaml.safe_load(block[1])["scenarios"][0], "holdout").seed == 4242


def test_a_heavier_held_out_primary_passes():
    [params] = check_scenarios([HOLDOUT], "holdout")
    assert params.tail_share() == pytest.approx(0.08)


def test_a_band_across_48_hours_counts_by_its_log_length_above_it():
    bands = [{"from": "1m", "to": "1h", "share": 0.5}, {"from": "1h", "to": "96h", "share": 0.5}]
    params = parse(holdout(delays=bands), "holdout")
    assert params.tail_share() == pytest.approx(0.5 * math.log(2) / math.log(96))


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        (holdout(seed=5), "holdout seed"),  # a dev seed
        (holdout(seed=2**31), "holdout seed"),
        (holdout(seed=True), "holdout seed"),
        (holdout(incidents=99), "incidents"),
        (holdout(extra=1), "keys must be exactly"),
        (holdout(id="Primary"), "id must be"),
        (holdout(classes={"logic": 0.70, "schema": 0.10, "infra": 0.20}), r"classes.infra = 0.2 is outside"),
        (holdout(classes={"logic": 0.75, "schema": 0.12}), "classes must be exactly"),
        (holdout(classes={"logic": 0.76, "schema": 0.12, "infra": 0.13}), "classes: the shares sum to 1.01"),
        (holdout(classes={"logic": 0.745, "schema": 0.125, "infra": 0.13}), "not a multiple of 0.01"),
        (holdout(delays=[{"from": "1m", "to": "1h", "share": 1.0}]), "2 to 5 bands"),
        (holdout(delays=[{"from": "1m", "to": "2m", "share": 0.2}] * 6), "2 to 5 bands"),
        (
            holdout(delays=[{"from": "1m", "to": "2h", "share": 0.5}, {"from": "1h", "to": "3d", "share": 0.5}]),
            "ascend without overlapping",
        ),
        (
            holdout(delays=[{"from": "1h", "to": "1h", "share": 0.5}, {"from": "2h", "to": "3d", "share": 0.5}]),
            "from must be shorter than to",
        ),
        (
            holdout(delays=[{"from": "1m", "to": "1h", "share": 0.5}, {"from": "1h", "to": "61d", "share": 0.5}]),
            r"within \[1m, 60d\]",
        ),
        (
            holdout(delays=[{"from": "30s", "to": "1h", "share": 0.5}, {"from": "1h", "to": "3d", "share": 0.5}]),
            "not a duration",
        ),
        (
            holdout(delays=[{"from": "1m", "to": "1h", "share": 0.5}, {"from": "1h", "to": "3d", "share": 0.4}]),
            "delays: the shares sum to 0.90",
        ),
        (
            holdout(delays=[{"from": "1m", "to": "1h"}, {"from": "1h", "to": "3d", "share": 1.0}]),
            "exactly {from, to, share}",
        ),
    ],
)
def test_a_scenario_outside_the_schema_is_refused(scenario: dict, message: str):
    with pytest.raises(ParamsError, match=message):
        check_scenarios([scenario], "holdout")


@pytest.mark.parametrize(
    ("delays", "message"),
    [
        ([("1m", "1h", 0.70), ("1h", "48h", 0.25), ("3d", "14d", 0.05)], "must be heavier"),  # dev's own tail
        # longer but thinner: dev's share above 48 h is also the bottom of the allowed range
        ([("1m", "1h", 0.71), ("1h", "48h", 0.25), ("3d", "30d", 0.04)], r"outside \[0.05, 0.4\]"),
        ([("1m", "1h", 0.60), ("1h", "48h", 0.30), ("3d", "10d", 0.10)], "not be lighter"),  # thicker but shorter
        ([("1m", "1h", 0.35), ("1h", "48h", 0.20), ("3d", "30d", 0.45)], r"outside \[0.05, 0.4\]"),
    ],
)
def test_the_held_out_primary_needs_a_heavier_tail_than_dev(delays: list, message: str):
    scenario = holdout(delays=[{"from": a, "to": b, "share": s} for a, b, s in delays])
    with pytest.raises(ParamsError, match=message):
        check_heavier_tail(parse(scenario, "holdout"))


def test_a_split_needs_one_primary_and_held_out_at_most_three_scenarios():
    with pytest.raises(ParamsError, match="exactly one scenario must have id 'primary'"):
        check_scenarios([holdout(id="other")], "holdout")
    extra = [holdout(id=f"s{i}", seed=5000 + i) for i in range(3)]
    with pytest.raises(ParamsError, match="at most 3 scenarios"):
        check_scenarios([HOLDOUT, *extra], "holdout")
    assert len(check_scenarios([HOLDOUT, *extra[:2]], "holdout")) == 3


def test_check_and_encrypt_refuse_a_bad_plaintext_before_anything_is_written(tmp_path):
    plain = tmp_path / "holdout-plain.yaml"
    plain.write_text(yaml.safe_dump({"scenarios": [HOLDOUT]}))
    assert [s["id"] for s in scenarios.check(plain, "e5_retention")] == ["primary"]
    plain.write_text(yaml.safe_dump({"scenarios": [holdout(seed=5)]}))
    with pytest.raises(ParamsError, match="holdout seed"):
        scenarios.check(plain, "e5_retention")
    with pytest.raises(ParamsError, match="holdout seed"):  # before sops runs, so before any file is written
        scenarios.encrypt(plain, "e5_retention", "age1unused", root=REPO)
    assert not scenarios.holdout_file("e5_retention").exists()
