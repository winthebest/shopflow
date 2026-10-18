"""lab.timing: ABBA order, the summary statistics, the bootstrap intervals and the noise floor."""

import statistics

import pytest

from lab.timing import MIN_RUNS, abba_order, compare, measure, noise_floor, percentile, summarize

SAMPLES = [10.0, 12.0, 11.0, 13.0, 30.0, 11.5, 12.5]


def test_abba_mirrors_every_second_pass():
    assert abba_order(["A", "B"], 4) == ["A", "B", "B", "A", "A", "B", "B", "A"]
    assert abba_order(["A", "B", "C"], 2) == ["A", "B", "C", "C", "B", "A"]
    with pytest.raises(ValueError):
        abba_order(["A", "A"], 2)


def test_percentiles_match_the_standard_library():
    quartiles = statistics.quantiles(SAMPLES, n=4, method="inclusive")
    assert [percentile(SAMPLES, q) for q in (0.25, 0.5, 0.75)] == pytest.approx(quartiles)
    assert percentile(SAMPLES, 0) == 10.0
    assert percentile(SAMPLES, 1) == 30.0


def test_summary_is_robust_to_one_outlier():
    s = summarize(SAMPLES)
    assert (s.n, s.median, s.iqr) == (7, 12.0, pytest.approx(1.5))
    assert s.p95 == pytest.approx(30.0 - 0.3 * (30.0 - 13.0))
    assert s.ci_low <= s.median <= s.ci_high
    assert s.ci_high < 14  # the 30 s outlier moves the mean (and the CV), not the median's interval much
    assert s.cv > 0.3


def test_bootstrap_is_reproducible():
    assert summarize(SAMPLES) == summarize(SAMPLES)
    assert compare(SAMPLES, [x * 2 for x in SAMPLES]) == compare(SAMPLES, [x * 2 for x in SAMPLES])


def test_a_clear_difference_lies_beyond_the_noise_floor():
    a = [10.0, 10.2, 9.9, 10.1, 10.0, 9.8, 10.3]
    a_again = [10.1, 9.9, 10.0, 10.2, 9.9, 10.1, 10.0]
    floor = noise_floor(a, a_again)
    assert floor < 0.05
    faster = compare(a, [x * 0.7 for x in a])
    assert faster.ratio == pytest.approx(0.7)
    assert faster.ci_high < 1 - floor


def test_measure_warms_up_then_runs_abba_and_prefers_reported_times():
    calls = []
    ticks = iter(range(100))

    def arm(name: str, reported: float | None):
        def run():
            calls.append(name)
            return reported

        return run

    samples = measure({"A": arm("A", None), "B": arm("B", 2.5)}, runs=MIN_RUNS, warmup=1, clock=lambda: next(ticks))
    assert calls[:2] == ["A", "B"]  # warm-up
    assert calls[2:] == abba_order(["A", "B"], MIN_RUNS)
    assert samples["B"] == [2.5] * MIN_RUNS
    assert samples["A"] == [1] * MIN_RUNS  # one clock tick around each call


def test_fewer_than_seven_runs_are_refused():
    with pytest.raises(ValueError, match="at least 7"):
        measure({"A": lambda: 1.0}, runs=5)
