"""The dev split as designed: N ∈ {2, 4} x {low, medium}, both workloads, the ledger-cost variant; ABBA order."""

from e7_concurrency.arms import ARMS
from e7_concurrency.dev import abba, dev_scenarios, runnable
from e7_concurrency.scenario import BASELINE, Arm


def test_the_dev_split_covers_the_designed_grid_and_nothing_held_out():
    scenarios = dev_scenarios()
    grid = {(s.workload, s.writers, s.contention, s.ledger) for s in scenarios}
    assert grid == {
        *((w, n, c, True) for w in ("w1", "w2") for n in (2, 4) for c in ("low", "medium")),
        ("w2", 2, "medium", False),
        ("w2", 4, "medium", False),
    }
    assert all(s.writers < 8 and s.contention != "high" and s.maintenance == "none" for s in scenarios)
    assert len({s.name for s in scenarios}) == len({s.seed for s in scenarios if s.ledger}) + 2 == len(scenarios)


def test_arms_alternate_abba():
    other = Arm("other")
    assert [[a.name for a in abba([BASELINE, other], r)] for r in range(4)] == [
        ["baseline", "other"],
        ["other", "baseline"],
        ["baseline", "other"],
        ["other", "baseline"],
    ]


def test_arms_skip_scenarios_they_cannot_run():
    noledger = next(s for s in dev_scenarios() if not s.ledger)
    events = next(s for s in dev_scenarios() if s.workload == "w1")
    assert [name for name, arm in ARMS.items() if runnable(noledger, arm)] == ["baseline"]
    assert [name for name, arm in ARMS.items() if runnable(events, arm)] == ["baseline", "method"]
