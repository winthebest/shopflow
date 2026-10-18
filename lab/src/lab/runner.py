"""Runs an experiment on a split; the held-out split runs once, from frozen code, and every attempt is on record.

An experiment is lab/experiments/<exp>/experiment.py with `run(scenarios: list[dict], context: RunContext) ->
list[lab.results.Row]`. `make lab-run EXP=<exp> SPLIT=dev|holdout`:

- dev: the dev scenarios, any number of times; results carry the git sha and whether the tree was dirty.
- holdout, in this order, refusing at the first failed check, and running none of the experiment's code before 4:
  1. the freeze exists and the code still hashes to it (lab.freeze.verify);
  2. a dev run finished with the code at this freeze (lab/freezes/<exp>.dev.jsonl), so the frozen code is known to
     load and run: an import or configuration error cannot burn a held-out attempt;
  2b. the freeze file and the ledger, lab/freezes/<exp>.ledger.jsonl, are committed and pushed (HEAD is on a
     remote-tracking branch): every earlier attempt is on record in the shared repository before a new one starts,
     whichever clone runs it;
  3. under a lock, the ledger allows an attempt: none before, or every earlier attempt failed or never finished and
     the caller names the infrastructure failure (`--rerun-after-infra-failure REASON`); after an attempt that
     finished, never again. The attempt is written as started, before anything is decrypted, so a crash still counts;
  4. the experiment is loaded from the bytes the freeze hashed, the scenarios are decrypted in memory, the experiment
     runs, and its result file is written with the freeze hash;
  5. the ledger records finished (with the result file) or failed (with the error). A failed attempt is reported
     like a finished one; `ledger` prints every attempt for the README.
- publish (after a finished held-out attempt only): the decrypted held-out scenarios, for reproduction.

The run's metadata is taken when it starts. Next to the result file the run leaves its bundle, appended as it goes:
`<stem>.statements.jsonl`, every statement an arm ran through `context.session(identity)` (lab.session's
StatementRecord), and `<stem>.states.jsonl`, every `context.capture(label, catalog, namespace)`: {label, at, catalog,
read_catalog, namespace, tables: {table: lakeops.state.TableState}}, read as observer through the zone's read-only
catalog (READ_CATALOG), so no arm sees the capture.
"""

import argparse
import fcntl
import hashlib
import importlib
import json
import sys
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lab import freeze, scenarios
from lab.freeze import attempts, ledger, ledger_file
from lab.guard import CAPTURE_IDENTITY, active_base_catalog, guarded_connect
from lab.results import Row, collect_metadata, new_run_id, write_run
from lab.results import results_dir as default_results_dir
from lab.session import ArmSession, StatementRecord
from lab.timescale import PUBLISHED, Timescale
from lab.trino_client import REPO, Settings
from lakeops.state import capture_namespace

# The observer's read-only catalog for each zone's catalog (lab_base_<run> -> lab_base_ro_<run>).
READ_CATALOG = {"lab_ops": "lab_ro", "lab_ro": "lab_ro", "lab_exp": "lab_exp_ro", "lab_sbx": "lab_sbx_ro"}


class HeldOutScenarioError(RuntimeError):
    """The decrypted held-out scenarios fail the experiment's check; the attempt is recorded as failed."""


class RunRefused(RuntimeError):
    """The held-out run may not start: no valid freeze, or the ledger does not allow another attempt."""


@dataclass(frozen=True)
class RunContext:
    experiment: str
    split: str
    run_id: str
    seed: int
    timescale: Timescale = PUBLISHED
    bundle: Path | None = None  # <results>/<experiment>/<run_id>-<sha12>, the stem of the bundle files
    settings: Settings | None = None

    def _append(self, suffix: str, entry: dict) -> None:
        if self.bundle is None:
            raise RuntimeError("this context has no run bundle")
        self.bundle.parent.mkdir(parents=True, exist_ok=True)
        with self.bundle.with_name(self.bundle.name + suffix).open("a") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

    def _log(self, record: StatementRecord) -> None:
        self._append(".statements.jsonl", record.to_json())

    @property
    def base_catalog(self) -> str | None:
        """The active baseline run's catalog (`lab_base_<run>`, for identity baseline_writer), or None: `make lab-base
        RUN=<run>` sets it. Raises while that run is set but not yet loaded by Trino."""
        return active_base_catalog(self.settings)

    def session(self, identity: str) -> ArmSession:
        """An arm's session as `identity`: guarded, never resubmitting, time-scaled, every statement in the bundle."""
        return ArmSession.open(identity, timescale=self.timescale, settings=self.settings, on_statement=self._log)

    def capture(self, label: str, catalog: str, namespace: str) -> dict:
        """The state of every table in `catalog.namespace`, read as observer, appended to the bundle and returned."""
        read_catalog = READ_CATALOG.get(catalog) or (
            catalog.replace("lab_base_", "lab_base_ro_", 1) if catalog.startswith("lab_base_") else None
        )
        if read_catalog is None:
            raise ValueError(f"no read-only catalog for {catalog}")
        conn = guarded_connect(CAPTURE_IDENTITY, settings=self.settings)
        tables = {name: state.to_json() for name, state in capture_namespace(conn, read_catalog, namespace).items()}
        entry = {"label": label, "at": datetime.now(UTC).isoformat(timespec="milliseconds"), "catalog": catalog,
                 "read_catalog": read_catalog, "namespace": namespace, "tables": tables}  # fmt: skip
        self._append(".states.jsonl", entry)
        return entry


@contextmanager
def _ledger_lock(experiment: str, root: Path) -> Iterator[None]:
    """Held across the check of the ledger and the append of "started": two held-out runs cannot both start."""
    path = ledger_file(experiment, root).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _record(experiment: str, root: Path, **entry: object) -> None:
    path = ledger_file(experiment, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), **entry}, sort_keys=True) + "\n")


def check_attempt(experiment: str, root: Path, rerun_reason: str | None) -> int:
    """The number of this attempt (1 for the first), or RunRefused."""
    previous = attempts(ledger(experiment, root))
    finished = [run for run, event in previous.items() if event == "finished"]
    if finished:
        raise RunRefused(f"{experiment}: the held-out already ran ({finished[0]}); it runs once")
    if previous and not rerun_reason:
        raise RunRefused(
            f"{experiment}: earlier held-out attempt(s) {', '.join(f'{r}={e}' for r, e in previous.items())}; "
            "rerun only after an infrastructure failure, naming it with --rerun-after-infra-failure"
        )
    return len(previous) + 1


def load_module(experiment: str, root: Path = REPO, frozen: freeze.Freeze | None = None) -> types.ModuleType:
    """The experiment's module. With a freeze, from exactly the bytes the freeze hashed (refused if they differ)."""
    relative = f"lab/experiments/{experiment}/experiment.py"
    path = root / relative
    if not path.is_file():
        raise RunRefused(f"no experiment at {path}")
    if experiment in sys.stdlib_module_names:
        raise RunRefused(f"experiment name {experiment!r} is a standard library module")
    source = path.read_bytes()
    if frozen is not None and frozen.files.get(relative) != hashlib.sha256(source).hexdigest():
        raise RunRefused(f"{relative} differs from the freeze")
    # experiment.py is module <exp>.experiment of package <exp>, so both relative and absolute imports of the
    # experiment's own modules work; this repository's lab/experiments comes first for it, and the repository root
    # last, for the agent/ and evals/ packages.
    experiments = str(root / "lab/experiments")
    if experiments in sys.path:
        sys.path.remove(experiments)
    sys.path.insert(0, experiments)
    if str(root) not in sys.path:
        sys.path.append(str(root))
    loaded = sys.modules.get(experiment)
    if loaded is not None and experiments not in {str(Path(p).parent) for p in getattr(loaded, "__path__", [])}:
        for cached in [m for m in sys.modules if m == experiment or m.startswith(f"{experiment}.")]:
            del sys.modules[cached]  # the same package, loaded from another checkout
    importlib.import_module(experiment)
    name = f"{experiment}.experiment"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = experiment
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102 (the repository's own, verified code)
    return module


def entry(module: types.ModuleType) -> Callable[[list[dict], RunContext], list[Row]]:
    """The module's `run(scenarios, context)`, or RunRefused."""
    if not callable(getattr(module, "run", None)):
        raise RunRefused(f"{module.__file__} defines no run(scenarios, context)")
    return module.run


def load_experiment(
    experiment: str, root: Path = REPO, frozen: freeze.Freeze | None = None
) -> Callable[[list[dict], RunContext], list[Row]]:
    """The experiment's `run` (see load_module)."""
    return entry(load_module(experiment, root, frozen))


def check_scenarios(module: types.ModuleType, items: list[dict], split: str) -> list[dict]:
    """The experiment's own check of a split's scenarios, `check_scenarios(scenarios, split)` in its experiment.py
    when it defines one (it raises on a bad scenario); the scenarios, unchanged."""
    check = getattr(module, "check_scenarios", None)
    if check is not None:
        check(items, split)
    return items


def run(
    experiment: str,
    split: str,
    *,
    seed: int = 0,
    cache_mode: str = "warm",
    rerun_reason: str | None = None,
    root: Path = REPO,
    results_dir: Path | None = None,
) -> Path:
    """Runs `experiment` on `split` and returns its result file (see the module docstring for the held-out rules)."""
    if split not in ("dev", "holdout"):
        raise ValueError(f"split {split!r}: dev or holdout")
    run_id = new_run_id()
    if split == "dev":
        module = load_module(experiment, root)
        experiment_run = entry(module)
        items = check_scenarios(module, scenarios.load_dev(experiment, root), split)
        meta = collect_metadata(experiment, split, seed, cache_mode, run_id=run_id, root=root)
        context = RunContext(experiment, split, run_id, seed, bundle=_bundle(meta, results_dir))
        path = write_run(experiment_run(items, context), meta, results_dir)
        _record_dev_run(experiment, root, run_id, path)
        return path
    # Held-out: every check before any of the experiment's code runs.
    try:
        frozen = freeze.verify(experiment, root)
    except freeze.FreezeError as error:
        raise RunRefused(str(error)) from error
    records = (freeze.freeze_file(experiment, root), ledger_file(experiment, root))
    if pending := freeze.uncommitted(root, *records):
        raise RunRefused(f"commit the freeze and the ledger before a held-out attempt:\n{pending.rstrip()}")
    if not any(entry["freeze_hash"] == frozen.hash for entry in freeze.dev_runs(experiment, root)):
        raise RunRefused(
            f"{experiment}: no dev run finished with the code at its freeze {frozen.hash}: "
            f"make lab-run EXP={experiment} SPLIT=dev first, so the frozen code is known to load and run"
        )
    try:
        is_pushed = freeze.pushed(root)
    except freeze.FreezeError as error:
        raise RunRefused(str(error)) from error
    if not is_pushed:
        raise RunRefused("push the commit with the freeze and the ledger first: HEAD is on no remote branch")
    meta = collect_metadata(experiment, split, seed, cache_mode, run_id=run_id, freeze_hash=frozen.hash, root=root)
    context = RunContext(experiment, split, run_id, seed, bundle=_bundle(meta, results_dir))
    with _ledger_lock(experiment, root):
        attempt = check_attempt(experiment, root, rerun_reason)
        _record(experiment, root, run_id=run_id, event="started", attempt=attempt, freeze_hash=frozen.hash,
                rerun_reason=rerun_reason)  # fmt: skip
    try:
        module = load_module(experiment, root, frozen)
        experiment_run = entry(module)
        items = scenarios.decrypt_holdout(experiment, root)
        try:
            check_scenarios(module, items, split)
        except Exception as error:
            # The ledger is committed, and the check's message may quote held-out values: it stays in the traceback.
            raise HeldOutScenarioError("the held-out scenarios fail the experiment's check (details above)") from error
        rows = experiment_run(items, context)
        path = write_run(rows, meta, results_dir)
    except Exception as error:
        _record(
            experiment, root, run_id=run_id, event="failed", attempt=attempt, error=f"{type(error).__name__}: {error}"
        )
        raise
    _record(experiment, root, run_id=run_id, event="finished", attempt=attempt, result=path.name)
    return path


def _record_dev_run(experiment: str, root: Path, run_id: str, result: Path) -> None:
    """A finished dev run is recorded for the held-out check when the code matches the experiment's freeze."""
    try:
        frozen = freeze.verify(experiment, root)
    except freeze.FreezeError:
        return  # no freeze, or code changed since: this dev run proves nothing for a held-out attempt
    path = freeze.dev_runs_file(experiment, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"run_id": run_id, "freeze_hash": frozen.hash, "result": result.name,
             "at": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _bundle(meta, results_dir: Path | None) -> Path:
    return (results_dir or default_results_dir()) / meta.experiment / f"{meta.run_id}-{meta.git_sha[:12]}"


def publish(experiment: str, root: Path = REPO) -> Path:
    if "finished" not in attempts(ledger(experiment, root)).values():
        raise RunRefused(f"{experiment}: the held-out scenarios are published only after the held-out run finished")
    return scenarios.publish(experiment, root)


def main() -> None:
    parser = argparse.ArgumentParser(description="lab experiment runner (docs/research/protocol.md)")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("experiment")
    run_parser.add_argument("split", choices=["dev", "holdout"])
    run_parser.add_argument("--seed", type=int, default=0)
    run_parser.add_argument("--cache-mode", default="warm")
    run_parser.add_argument("--rerun-after-infra-failure", dest="rerun_reason")
    for name in ("freeze", "ledger", "publish"):
        sub.add_parser(name).add_argument("experiment")
    args = parser.parse_args()
    try:
        if args.command == "run":
            print(run(args.experiment, args.split, seed=args.seed, cache_mode=args.cache_mode,
                      rerun_reason=args.rerun_reason))  # fmt: skip
        elif args.command == "freeze":
            frozen = freeze.take(args.experiment)
            print(f"{freeze.freeze_file(args.experiment)}: {frozen.hash} ({len(frozen.files)} files)")
        elif args.command == "ledger":
            for entry in ledger(args.experiment):
                print(json.dumps(entry, sort_keys=True))
        else:
            print(publish(args.experiment))
    except (RunRefused, freeze.FreezeError, scenarios.ScenarioError) as error:
        sys.exit(f"refused: {error}")


if __name__ == "__main__":
    main()
