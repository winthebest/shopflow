"""Fair timing: a warm-up, at least 7 measured runs per arm in ABBA order, robust summaries, bootstrap CIs, noise floor.

- Order: arms alternate in mirrored blocks (A B B A A B B A …), so a drift of the machine (thermal, caches, a
  neighbour) lands on every arm alike. Warm-up runs come first, in the same order, and are not measured: 10 by
  default, 25 measured runs per arm (at least 7).
- Summary of one arm: median, quartiles and IQR, p95 (inclusive interpolation), mean, coefficient of variation, and a
  percentile bootstrap CI of the median (fixed seed: the same samples always give the same interval).
- Comparison of two arms: the ratio of medians, with a bootstrap CI of that ratio.
- Noise floor: an A/A comparison (the same arm measured as both sides). A difference between real arms counts only
  when its CI lies beyond the noise floor (docs/research/protocol.md).
"""

import random
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

MIN_RUNS = 7
# Defaults measured on the lab (docs/research/protocol.md section 4): with 2 warm-up runs the A/A floor of a
# sub-second query was 14-31 %, with 10 warm-up and 25 measured runs 5-9 % (Trino's JIT warms up over several runs).
WARMUP = 10
RUNS = 25


@dataclass(frozen=True)
class Summary:
    n: int
    median: float
    q1: float
    q3: float
    iqr: float
    p95: float
    mean: float
    cv: float  # stdev / mean (population), 0 for a single sample
    ci_low: float
    ci_high: float  # bootstrap CI of the median


@dataclass(frozen=True)
class Comparison:
    ratio: float  # median(b) / median(a)
    ci_low: float
    ci_high: float


def percentile(samples: Sequence[float], q: float) -> float:
    """Inclusive linear interpolation (the same as statistics.quantiles(method="inclusive")), q in [0, 1]."""
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("no samples")
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _bootstrap(statistic: Callable[[random.Random], float], resamples: int, confidence: float) -> tuple[float, float]:
    rng = random.Random(0)  # noqa: S311 (statistical resampling with a fixed seed, not cryptography)
    values = sorted(statistic(rng) for _ in range(resamples))
    tail = (1 - confidence) / 2
    return percentile(values, tail), percentile(values, 1 - tail)


def summarize(samples: Sequence[float], *, confidence: float = 0.95, resamples: int = 10_000) -> Summary:
    if not samples:
        raise ValueError("no samples")
    mean = statistics.fmean(samples)
    cv = statistics.pstdev(samples) / mean if len(samples) > 1 and mean else 0.0
    ci = _bootstrap(lambda rng: statistics.median(rng.choices(samples, k=len(samples))), resamples, confidence)
    q1, q3 = percentile(samples, 0.25), percentile(samples, 0.75)
    return Summary(
        n=len(samples),
        median=statistics.median(samples),
        q1=q1,
        q3=q3,
        iqr=q3 - q1,
        p95=percentile(samples, 0.95),
        mean=mean,
        cv=cv,
        ci_low=ci[0],
        ci_high=ci[1],
    )


def compare(a: Sequence[float], b: Sequence[float], *, confidence: float = 0.95, resamples: int = 10_000) -> Comparison:
    """median(b) / median(a), with a bootstrap CI (both sides resampled independently)."""

    def ratio(rng: random.Random) -> float:
        return statistics.median(rng.choices(b, k=len(b))) / statistics.median(rng.choices(a, k=len(a)))

    low, high = _bootstrap(ratio, resamples, confidence)
    return Comparison(statistics.median(b) / statistics.median(a), low, high)


def noise_floor(a: Sequence[float], a_again: Sequence[float]) -> float:
    """The relative difference an A/A comparison cannot tell from 0: the larger distance of its CI from 1."""
    same = compare(a, a_again)
    return max(abs(1 - same.ci_low), abs(same.ci_high - 1))


def abba_order(arms: Sequence[str], rounds: int) -> list[str]:
    """`rounds` passes over the arms, every second pass reversed: A B, B A, A B, … (A B B A A B for 3 rounds)."""
    if len(set(arms)) != len(arms) or not arms:
        raise ValueError(f"arms must be distinct and non-empty: {arms}")
    return [arm for i in range(rounds) for arm in (arms if i % 2 == 0 else list(reversed(arms)))]


def measure(
    arms: Mapping[str, Callable[[], float | None]],
    *,
    runs: int = RUNS,
    warmup: int = WARMUP,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, list[float]]:
    """Run every arm `warmup` times unmeasured, then `runs` times measured, in ABBA order; seconds per arm.

    An arm returns its own measurement (e.g. Trino's elapsed time of the query it ran) or None, in which case the
    wall-clock time of the call is used.
    """
    if runs < MIN_RUNS:
        raise ValueError(f"at least {MIN_RUNS} measured runs per arm, not {runs}")
    names = list(arms)
    for name in abba_order(names, warmup):
        arms[name]()
    samples: dict[str, list[float]] = {name: [] for name in names}
    for name in abba_order(names, runs):
        start = clock()
        reported = arms[name]()
        samples[name].append(clock() - start if reported is None else reported)
    return samples
