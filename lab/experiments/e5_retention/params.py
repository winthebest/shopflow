"""E5's incident parameters: the schema both splits use, and the rules a held-out file must follow.

docs/research/e5-retention-policy-design.md ("Incident parameters") is the specification; this module enforces it.
One scenario is one parameter set, from which the simulator draws its incidents:

    {id, seed, incidents, classes: {logic, schema, infra}, delays: [{from, to, share}, ...]}

`check_scenarios(scenarios, split)` refuses anything outside the ranges. The lab runner calls it before `run` on both
splits, and `python -m lab.scenarios check|encrypt` calls it on the plaintext before anything is encrypted. A
held-out file that fails after decryption would burn the attempt, so it is checked before it is written.
"""

import math
import re
from dataclasses import dataclass
from datetime import timedelta

ID = re.compile(r"[a-z0-9_]{1,40}")
DURATION = re.compile(r"([1-9][0-9]*)([mhd])")
UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}
KEYS = {"id", "seed", "incidents", "classes", "delays"}
CLASS_RANGES = {"logic": (0.50, 0.85), "schema": (0.05, 0.25), "infra": (0.10, 0.15)}
INCIDENTS = (100, 1000)
DEV_SEEDS, HOLDOUT_SEEDS = (0, 999), (1000, 2**31 - 1)
BANDS = (2, 5)
SHORTEST, LONGEST = timedelta(minutes=1), timedelta(days=60)
TAIL_FROM = timedelta(hours=48)
TAIL_SHARE = (0.05, 0.40)  # the held-out primary's share of delays above 48 h
HOLDOUT_SCENARIOS = 3  # "primary" and at most two more, reported as held-out sensitivity
EPS = 1e-9


class ParamsError(ValueError):
    """A scenario outside the E5 parameter schema."""


@dataclass(frozen=True)
class Band:
    start: timedelta
    end: timedelta
    share: float


@dataclass(frozen=True)
class Params:
    id: str
    seed: int
    incidents: int
    classes: dict[str, float]
    delays: tuple[Band, ...]

    def tail_share(self) -> float:
        """The share of delays above 48 h; a band across 48 h counts by its log-length above it (log-uniform)."""
        total = 0.0
        for band in self.delays:
            if band.start >= TAIL_FROM:
                total += band.share
            elif band.end > TAIL_FROM:
                above = math.log(band.end / TAIL_FROM) / math.log(band.end / band.start)
                total += band.share * above
        return total

    def longest(self) -> timedelta:
        return self.delays[-1].end


def duration(text: object, where: str) -> timedelta:
    match = DURATION.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise ParamsError(f"{where}: {text!r} is not a duration like 30m, 48h or 14d")
    return int(match[1]) * UNITS[match[2]]


def _share(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        raise ParamsError(f"{where}: {value!r} is not a share in (0, 1]")
    if abs(value * 100 - round(value * 100)) > EPS:
        raise ParamsError(f"{where}: {value!r} is not a multiple of 0.01")
    return float(value)


def _integer(value: object, low: int, high: int, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ParamsError(f"{where}: {value!r} is not an integer in [{low}, {high}]")
    return value


def _sums_to_one(shares: list[float], where: str) -> None:
    if sum(round(s * 100) for s in shares) != 100:
        raise ParamsError(f"{where}: the shares sum to {sum(shares):.2f}, not 1")


def parse(scenario: object, split: str) -> Params:
    """One scenario as Params, or ParamsError naming the first rule it breaks."""
    if not isinstance(scenario, dict):
        raise ParamsError(f"a scenario is a mapping, not {type(scenario).__name__}")
    where = f"scenario {scenario.get('id')!r}"
    if set(scenario) != KEYS:
        raise ParamsError(f"{where}: keys must be exactly {sorted(KEYS)}, not {sorted(scenario)}")
    if not isinstance(scenario["id"], str) or not ID.fullmatch(scenario["id"]):
        raise ParamsError(f"{where}: id must be [a-z0-9_]{{1,40}}")
    seed = _integer(scenario["seed"], *(DEV_SEEDS if split == "dev" else HOLDOUT_SEEDS), f"{where}: {split} seed")
    incidents = _integer(scenario["incidents"], *INCIDENTS, f"{where}: incidents")

    classes = scenario["classes"]
    if not isinstance(classes, dict) or set(classes) != set(CLASS_RANGES):
        raise ParamsError(f"{where}: classes must be exactly {sorted(CLASS_RANGES)}")
    for name, (low, high) in CLASS_RANGES.items():
        if not low - EPS <= _share(classes[name], f"{where}: classes.{name}") <= high + EPS:
            raise ParamsError(f"{where}: classes.{name} = {classes[name]} is outside [{low}, {high}]")
    _sums_to_one(list(classes.values()), f"{where}: classes")

    delays = scenario["delays"]
    if not isinstance(delays, list) or not BANDS[0] <= len(delays) <= BANDS[1]:
        raise ParamsError(f"{where}: delays must be a list of {BANDS[0]} to {BANDS[1]} bands")
    bands = []
    for i, band in enumerate(delays):
        at = f"{where}: delays[{i}]"
        if not isinstance(band, dict) or set(band) != {"from", "to", "share"}:
            raise ParamsError(f"{at}: a band is exactly {{from, to, share}}")
        bands.append(Band(duration(band["from"], f"{at}.from"), duration(band["to"], f"{at}.to"),
                          _share(band["share"], f"{at}.share")))  # fmt: skip
        if bands[-1].start >= bands[-1].end:
            raise ParamsError(f"{at}: from must be shorter than to")
        if i and bands[-1].start < bands[-2].end:
            raise ParamsError(f"{at}: bands must ascend without overlapping")
    if bands[0].start < SHORTEST or bands[-1].end > LONGEST:
        raise ParamsError(f"{where}: delays must lie within [1m, 60d]")
    _sums_to_one([b.share for b in bands], f"{where}: delays")
    return Params(scenario["id"], seed, incidents, dict(classes), tuple(bands))


# Dev's primary, as published in the design (lab/scenarios/e5_retention/dev.yaml; a test keeps the two equal).
DEV_PRIMARY = Params(
    "primary",
    1,
    300,
    {"logic": 0.75, "schema": 0.12, "infra": 0.13},
    (
        Band(timedelta(minutes=1), timedelta(hours=1), 0.70),
        Band(timedelta(hours=1), timedelta(hours=48), 0.25),
        Band(timedelta(days=3), timedelta(days=14), 0.05),
    ),
)


def check_heavier_tail(primary: Params, dev: Params = DEV_PRIMARY) -> None:
    """The held-out primary's tail: within TAIL_SHARE above 48 h, at least as long, and heavier on one of the two."""
    share, longest = primary.tail_share(), primary.longest()
    if not TAIL_SHARE[0] - EPS <= share <= TAIL_SHARE[1] + EPS:
        raise ParamsError(f"held-out primary: {share:.3f} of delays above 48 h, outside {list(TAIL_SHARE)}")
    if share < dev.tail_share() - EPS or longest < dev.longest():
        raise ParamsError("held-out primary: its tail must not be lighter than dev's on either measure")
    if share <= dev.tail_share() + EPS and longest <= dev.longest():
        raise ParamsError("held-out primary: its tail must be heavier than dev's (more above 48 h, or longer)")


def check_scenarios(scenarios: list[dict], split: str) -> list[Params]:
    """Every scenario of a split as Params; ParamsError on the first rule broken. Both splits need a "primary"."""
    params = [parse(scenario, split) for scenario in scenarios]
    ids = [p.id for p in params]
    if ids.count("primary") != 1:
        raise ParamsError(f"{split}: exactly one scenario must have id 'primary'")
    if split == "holdout":
        if len(params) > HOLDOUT_SCENARIOS:
            raise ParamsError(f"holdout: at most {HOLDOUT_SCENARIOS} scenarios, not {len(params)}")
        check_heavier_tail(next(p for p in params if p.id == "primary"))
    return params
