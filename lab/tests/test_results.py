"""lab.results: a run's Parquet file, its metadata, and the write-once rule."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lab.results import Row, collect_metadata, git_state, image_pins, new_run_id, read_run, write_run

ROWS = [
    Row("baseline", "fragmented", "latency", 1.25, "s", 0),
    Row("method", "fragmented", "latency", 0.75, "s", 0),
]


def test_a_run_round_trips_with_its_metadata(tmp_path: Path):
    meta = collect_metadata("e0_toy", "dev", 7, "warm", run_id="r20261018t013000")
    path = write_run(ROWS, meta, tmp_path)
    assert path == tmp_path / "e0_toy" / f"r20261018t013000-{meta.git_sha[:12]}.parquet"
    rows, stored = read_run(path)
    assert [(r["arm"], r["value"], r["split"], r["experiment"]) for r in rows] == [
        ("baseline", 1.25, "dev", "e0_toy"),
        ("method", 0.75, "dev", "e0_toy"),
    ]
    assert stored["seed"] == 7
    assert stored["timescale"] == "1 simulated day = 1m real (1440x)"
    assert len(stored["git_sha"]) == 40
    assert stored["finished_at"] >= stored["started_at"]
    assert stored["images"]["trino"].startswith("trinodb/trino:483@sha256:")
    assert {"python", "lab", "lakeops", "trino", "pyarrow"} <= stored["versions"].keys()
    assert stored["hardware"]["cpus"] > 0
    assert "docker" in stored  # an `error` entry when Docker is not running


def test_a_result_is_written_once(tmp_path: Path):
    meta = collect_metadata("e0_toy", "dev", 1, "warm", run_id="r20261018t013001")
    write_run(ROWS, meta, tmp_path)
    with pytest.raises(FileExistsError, match="written once"):
        write_run(ROWS, meta, tmp_path)


@pytest.mark.parametrize(("experiment", "split"), [("E0", "dev"), ("e0", "test"), ("e0/../x", "dev")])
def test_bad_names_and_splits_are_refused(experiment: str, split: str):
    with pytest.raises(ValueError):
        collect_metadata(experiment, split, 1, "warm")


def test_run_ids_sort_by_time_and_are_baseline_run_ids():
    assert new_run_id(datetime(2026, 10, 18, 1, 30, 5, 42_000, tzinfo=UTC)) == "r20261018t013005042"


def test_every_compose_service_with_an_image_is_pinned():
    pins = image_pins()
    assert set(pins) == {"catalog-db", "seaweedfs", "polaris-bootstrap", "polaris", "trino", "otel", "setup", "py"}
    assert all("@sha256:" in image for image in pins.values())


def test_the_git_state_can_come_from_the_host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAB_GIT_SHA", "a" * 40)
    monkeypatch.setenv("LAB_GIT_DIRTY", "true")
    assert git_state() == ("a" * 40, True)
