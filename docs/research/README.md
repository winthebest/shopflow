# Lab experiments

Each experiment has a one-page README here (`<eNN>-<topic>.md`). Its code lives in `lab/experiments/<exp>/`, its
scenarios in `lab/scenarios/<exp>/` and its results in `lab/results/<exp>/`. How experiments are run and reported:
[protocol.md](protocol.md).

| Experiment | Topic (brief) | Code | Status |
|---|---|---|---|
| `e0_toy` | none: exercises the protocol's mechanics, with known answers | `lab/experiments/e0_toy/` | mechanics tested (`lab/tests/test_runner.py`) |
| [E5](e5-retention-policy-design.md) | 5. Retention policy per table profile | `lab/experiments/e5_retention/` (scenario check; simulator after the first measurements) | design fixed, nothing measured |
| [E7](e7-concurrent-writers.md) | 7. Concurrent writers | `lab/experiments/e7_concurrency/` | design fixed, no results yet |

An experiment plugs into the runner with one function, in `lab/experiments/<exp>/experiment.py`: `run(scenarios:
list[dict], context: lab.runner.RunContext) -> list[lab.results.Row]`. `make lab-run EXP=<exp> SPLIT=dev|holdout` calls
it, with the scenarios of the split, and writes the result file. An optional second function,
`check_scenarios(scenarios: list[dict], split: str) -> None`, raises on a scenario the experiment cannot use. It runs
before `run` on both splits, and in `python -m lab.scenarios check|encrypt` before a held-out file is written. The file
is loaded as module `<exp>.experiment` of package `<exp>` (`lab/experiments/<exp>/`), so its own modules import
relatively or as `<exp>.…`. The repository root is on the path too: `agent.…` and `evals.…` import, on the agent's pins,
which the lab project carries (`lab/tests/test_agent_pins.py`).

- A scenario is one entry of the split's file; the only key the runner requires is a unique `id`. The files are
  `lab/scenarios/<exp>/{dev.yaml,holdout.sops.yaml}`, or, when another lane owns them, in the directory under `evals/`
  that `lab/scenarios/<exp>/location` names (e.g. `evals/datasets/operator`).
- `context` carries `experiment`, `split`, `run_id`, `seed` and `timescale`; `base_catalog` names the active baseline
  run's catalog (`lab_base_<run>`, from `make lab-base RUN=<run>`) or is None. It gives:
  - `session(identity)`: an arm's guarded, logged, time-scaled session;
  - `capture(label, catalog, namespace)`: the state of a namespace's tables, read as `observer`.
- `Row(arm, case, metric, value, unit, iteration)` is one measured value.

## Template

Copy this into `<eNN>-<topic>.md` and keep it to one page. Every number comes from a result file.

```markdown
# ENN. <Title>

- Status: design | dev | held-out done (<date>)
- Code: `lab/experiments/<exp>/`. Scenarios: `lab/scenarios/<exp>/`. Results: `lab/results/<exp>/`.
- Lab: catalogs and identities used (ADR 0417); time scale (ADR 0420).

## Problem and hypothesis
The pain point, in one paragraph. **Hypothesis:** a falsifiable claim, with the mechanism behind it.

## Setup
Dataset (scale factor, partitioning, seed), workload, engine and image versions, hardware, Docker limits, cache mode.

## Baseline and method
The baseline (what a team would do today) and the method, run under the same conditions.

## Metric
The brief's primary metric with its formula; secondary metrics.

## Results
Held-out: the table from `make lab-report` (median, IQR, p95, CI; ratio to the baseline; beyond the noise floor
or not), the result file name, and the ledger entries (`make lab-ledger`). Dev numbers, if shown, are labeled dev.

## Failure case
Where the method loses or breaks, why, and what was not tested.

## Reproduce
make lab-up; make lab-run EXP=<exp> SPLIT=dev; versions and seed. Sources and the dates they were read.
```
