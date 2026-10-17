"""Freeze: the hash of everything that can change an experiment's result, taken before its held-out run.

`make lab-freeze EXP=<exp>` writes lab/freezes/<exp>.json: the sha256 of every git-tracked file under FROZEN (the lab
harness and its stack configuration, lakeops, agent/, evals/, policies/, the lockfiles, the experiment's code and its
scenarios) and one hash over them all. A freeze is taken only from committed content: uncommitted or untracked files
under those paths refuse it, and so does an existing held-out attempt (the ledger, lab/freezes/<exp>.ledger.jsonl),
since a new freeze after an attempt would let changed code run the held-out again. The held-out runner recomputes the
hash and refuses on any difference, naming the files.
"""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lab.trino_client import REPO

FROZEN = (
    "lab/src",
    "lab/pyproject.toml",
    "lab/uv.lock",
    "lab/compose.yml",
    "lab/stack.sh",
    "lab/polaris.json",
    "lab/polaris-setup.py",
    "lab/trino",
    "lab/otel",
    "lakeops",
    "agent",
    "evals",
    "policies",
)


class FreezeError(RuntimeError):
    """No valid freeze: missing, taken from uncommitted content, or the code has changed since."""


@dataclass(frozen=True)
class Freeze:
    experiment: str
    hash: str
    git_sha: str
    created_at: str
    paths: tuple[str, ...]
    files: dict[str, str]  # path -> sha256 of its content


def frozen_paths(experiment: str) -> tuple[str, ...]:
    return (*FROZEN, f"lab/experiments/{experiment}", f"lab/scenarios/{experiment}")


def freeze_file(experiment: str, root: Path = REPO) -> Path:
    return root / "lab/freezes" / f"{experiment}.json"


def _git(root: Path, *args: str) -> str:
    # Fixed git subcommands; the paths come from FROZEN and a validated experiment name.
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout  # noqa: S603, S607


def tree_hash(files: dict[str, str]) -> str:
    """One hash over `sha256  path` lines sorted by path (the sha256sum format)."""
    listing = "".join(f"{digest}  {path}\n" for path, digest in sorted(files.items()))
    return "sha256:" + hashlib.sha256(listing.encode()).hexdigest()


def current_files(paths: tuple[str, ...], root: Path = REPO) -> dict[str, str]:
    """sha256 of every tracked file under `paths`; refuses uncommitted or untracked changes there."""
    dirty = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    if dirty.strip():
        raise FreezeError(f"uncommitted or untracked files under the frozen paths:\n{dirty.rstrip()}")
    tracked = [p for p in _git(root, "ls-files", "-z", "--", *paths).split("\0") if p]
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in tracked}


def ledger_file(experiment: str, root: Path = REPO) -> Path:
    """Every held-out attempt of an experiment, one JSON line per event (lab.runner writes it)."""
    return root / "lab/freezes" / f"{experiment}.ledger.jsonl"


def ledger(experiment: str, root: Path = REPO) -> list[dict]:
    path = ledger_file(experiment, root)
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.is_file() else []


def attempts(entries: list[dict]) -> dict[str, str]:
    """run_id -> its last event (started, finished, failed), in attempt order."""
    last: dict[str, str] = {}
    for entry in entries:
        last[entry["run_id"]] = entry["event"]
    return last


def uncommitted(root: Path, *paths: Path) -> str:
    """`git status --porcelain` of `paths` (empty when all are committed and unchanged)."""
    return _git(
        root, "status", "--porcelain", "--untracked-files=all", "--", *(str(p.relative_to(root)) for p in paths)
    )


def pushed(root: Path = REPO) -> bool:
    """Whether HEAD is on a remote-tracking branch: a clone of the remote has this commit, and its records."""
    return bool(_git(root, "branch", "--remotes", "--contains", "HEAD").strip())


def take(experiment: str, root: Path = REPO) -> Freeze:
    """Writes lab/freezes/<experiment>.json, unless the ledger already has a held-out attempt."""
    if previous := attempts(ledger(experiment, root)):
        raise FreezeError(f"{experiment} has {len(previous)} held-out attempt(s): its freeze cannot change any more")
    paths = frozen_paths(experiment)
    files = current_files(paths, root)
    if not any(p.startswith(f"lab/experiments/{experiment}/") for p in files):
        raise FreezeError(f"no tracked code under lab/experiments/{experiment}")
    freeze = Freeze(
        experiment=experiment,
        hash=tree_hash(files),
        git_sha=_git(root, "rev-parse", "HEAD").strip(),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        paths=paths,
        files=files,
    )
    path = freeze_file(experiment, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(freeze.__dict__, indent=1, sort_keys=True) + "\n")
    return freeze


def load(experiment: str, root: Path = REPO) -> Freeze:
    path = freeze_file(experiment, root)
    if not path.is_file():
        raise FreezeError(f"no freeze for {experiment} ({path}): make lab-freeze EXP={experiment}")
    data = json.loads(path.read_text())
    return Freeze(**{**data, "paths": tuple(data["paths"])})


def verify(experiment: str, root: Path = REPO) -> Freeze:
    """The freeze, if the code still hashes the same; otherwise FreezeError naming what changed."""
    freeze = load(experiment, root)
    files = current_files(freeze.paths, root)
    if tree_hash(files) == freeze.hash:
        return freeze
    changed = sorted(p for p in files.keys() | freeze.files.keys() if files.get(p) != freeze.files.get(p))
    raise FreezeError(f"{experiment}: code differs from its freeze {freeze.hash} in: {', '.join(changed)}")
