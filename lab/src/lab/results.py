"""Results of one run: a Parquet file <results>/<experiment>/<run_id>-<git sha>.parquet, with the run's metadata.

Rows are long form, one per measured value: arm, case, metric, value, unit, iteration. The file's key-value metadata
(key `lab.run`, JSON) holds what is needed to judge and repeat the run: git sha and whether the tree was dirty, the
freeze hash (held-out runs), seed, split, time scale, cache mode, package versions, the lab's image pins, Trino's
version and environment, the hardware, Docker's limits. A result file is written once and never overwritten.
Lanes write under LAB_RESULTS_DIR (default lab/results); only the orchestrator's workspace commits results.
"""

import json
import os
import platform
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import metadata as packages
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from lab.timescale import PUBLISHED, Timescale
from lab.trino_client import REPO

METADATA_KEY = b"lab.run"
SCHEMA = pa.schema(
    [
        ("experiment", pa.string()),
        ("run_id", pa.string()),
        ("split", pa.string()),
        ("arm", pa.string()),
        ("case", pa.string()),
        ("metric", pa.string()),
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("iteration", pa.int32()),
    ]
)
SPLITS = ("dev", "holdout")
UNRECORDED = (":(exclude)lab/results", ":(exclude)lab/freezes")  # written by runs, not part of the code
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class Row:
    arm: str
    case: str
    metric: str
    value: float
    unit: str
    iteration: int = 0


@dataclass(frozen=True)
class RunMetadata:
    experiment: str
    run_id: str
    split: str
    seed: int
    cache_mode: str  # e.g. "warm" (after the warm-up runs) or "cold"
    git_sha: str
    git_dirty: bool
    started_at: str
    timescale: str = PUBLISHED.describe()
    freeze_hash: str | None = None
    finished_at: str | None = None
    versions: dict = field(default_factory=dict)
    images: dict = field(default_factory=dict)
    trino: dict = field(default_factory=dict)
    hardware: dict = field(default_factory=dict)
    docker: dict = field(default_factory=dict)


def new_run_id(now: datetime | None = None) -> str:
    """r<UTC yyyymmddthhmmss><milliseconds>: sortable, and a valid baseline run id (lab/stack.sh base)."""
    now = now or datetime.now(UTC)
    return "r" + now.strftime("%Y%m%dt%H%M%S") + f"{now.microsecond // 1000:03d}"


def _run(*command: str) -> str:
    # Fixed commands (git, docker, sysctl) with arguments from this module, no shell.
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout.strip()  # noqa: S603


def git_state(repo: Path = REPO) -> tuple[str, bool]:
    """The checkout's commit and whether files differ from it, results and freezes aside (they are written by runs).

    Inside lab/stack.sh py there is no git metadata: stack.sh passes the host's state as LAB_GIT_SHA and LAB_GIT_DIRTY.
    """
    if os.environ.get("LAB_GIT_SHA"):
        return os.environ["LAB_GIT_SHA"], os.environ.get("LAB_GIT_DIRTY") == "true"
    changed = _run("git", "-C", str(repo), "status", "--porcelain", "--", ".", *UNRECORDED)
    return _run("git", "-C", str(repo), "rev-parse", "HEAD"), bool(changed)


def image_pins(compose: Path = REPO / "lab/compose.yml") -> dict[str, str]:
    """service -> image reference (with digest), read from lab/compose.yml."""
    pins, service = {}, None
    for line in compose.read_text().splitlines():
        if match := re.fullmatch(r"  ([a-z0-9-]+):", line):
            service = match.group(1)
        elif (match := re.fullmatch(r"    image: (\S+)", line)) and service:
            pins[service] = match.group(1)
    return pins


def hardware() -> dict:
    info = {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count()}
    try:
        if platform.system() == "Darwin":
            info["memory_bytes"] = int(_run("sysctl", "-n", "hw.memsize"))
            info["cpu_model"] = _run("sysctl", "-n", "machdep.cpu.brand_string")
        else:
            info["memory_bytes"] = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text())[1]) * 1024
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        info["error"] = str(error)
    return info


def docker_limits(container: str = "shopflow-lab-trino-1") -> dict:
    """The Docker VM's size and the Trino container's limits; an `error` entry when Docker cannot be asked."""
    try:
        vm = json.loads(_run("docker", "info", "--format", "{{json .}}"))
        limits = json.loads(_run("docker", "inspect", "--format", "{{json .HostConfig}}", container))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {"error": str(error)}
    return {
        "server_version": vm.get("ServerVersion"),
        "vm_cpus": vm.get("NCPU"),
        "vm_memory_bytes": vm.get("MemTotal"),
        "trino_memory_bytes": limits.get("Memory"),
        "trino_cpus": (limits.get("NanoCpus") or 0) / 1e9,
    }


def versions() -> dict[str, str]:
    found = {"python": platform.python_version()}
    for name in ("lab", "lakeops", "trino", "pyarrow"):
        try:
            found[name] = packages.version(name)
        except packages.PackageNotFoundError:
            found[name] = "missing"
    return found


def collect_metadata(
    experiment: str,
    split: str,
    seed: int,
    cache_mode: str,
    *,
    run_id: str | None = None,
    freeze_hash: str | None = None,
    timescale: Timescale = PUBLISHED,
    trino_info: dict | None = None,
    root: Path = REPO,
) -> RunMetadata:
    """Everything about this machine and checkout (`root`) that a result depends on, at the start of a run."""
    if not _NAME.fullmatch(experiment):
        raise ValueError(f"experiment name {experiment!r}: [a-z][a-z0-9_]*")
    if split not in SPLITS:
        raise ValueError(f"split {split!r}: one of {SPLITS}")
    sha, dirty = git_state(root)
    return RunMetadata(
        experiment=experiment,
        run_id=run_id or new_run_id(),
        split=split,
        seed=seed,
        cache_mode=cache_mode,
        git_sha=sha,
        git_dirty=dirty,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        timescale=timescale.describe(),
        freeze_hash=freeze_hash,
        versions=versions(),
        images=image_pins(),
        trino={k: trino_info.get(k) for k in ("nodeVersion", "environment")} if trino_info else {},
        hardware=hardware(),
        docker=docker_limits(),
    )


def results_dir() -> Path:
    return Path(os.environ.get("LAB_RESULTS_DIR", REPO / "lab/results"))


def write_run(rows: list[Row], meta: RunMetadata, directory: Path | None = None) -> Path:
    """Writes the run's file once; refuses to overwrite an existing one."""
    if meta.finished_at is None:
        meta = RunMetadata(**{**asdict(meta), "finished_at": datetime.now(UTC).isoformat(timespec="seconds")})
    path = (directory or results_dir()) / meta.experiment / f"{meta.run_id}-{meta.git_sha[:12]}.parquet"
    if path.exists():
        raise FileExistsError(f"{path} exists: a run's result is written once")
    columns = {name: [] for name in SCHEMA.names}
    for row in rows:
        for name, value in (("experiment", meta.experiment), ("run_id", meta.run_id), ("split", meta.split)):
            columns[name].append(value)
        for name, value in asdict(row).items():
            columns[name].append(value)
    table = pa.table(columns, schema=SCHEMA).replace_schema_metadata({METADATA_KEY: json.dumps(asdict(meta))})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp)
    tmp.rename(path)
    return path


def read_run(path: Path) -> tuple[list[dict], dict]:
    table = pq.read_table(path)
    return table.to_pylist(), json.loads(table.schema.metadata[METADATA_KEY])
