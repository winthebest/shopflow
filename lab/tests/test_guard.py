"""lab.guard without a stack: the repository's lab config passes, anything that could reach `lake` is refused."""

from pathlib import Path

import pytest

from lab.guard import (
    LakeGuardError,
    check_catalog_files,
    check_environment,
    check_location,
    check_polaris_spec,
    check_target_catalog,
    check_visible_catalogs,
    shown_location,
)
from lab.trino_client import REPO

LAB_CATALOG = (REPO / "lab/trino/etc/catalog/lab_exp.properties").read_text()


def test_the_lab_catalog_files_pass():
    assert check_catalog_files(REPO / "lab/trino/etc/catalog") == ["lab_exp", "lab_ops", "lab_ro", "lab_sbx"]
    assert len(check_catalog_files(REPO / "lab/spike/trino/catalog")) == 9


def test_the_lab_polaris_specs_pass():
    assert check_polaris_spec(REPO / "lab/polaris.json") == ["lab_exp", "lab_evalprod", "lab_sandbox"]
    assert check_polaris_spec(REPO / "lab/spike/polaris.json") == ["spike"]


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("lab_exp", "warehouse=lab_exp", "warehouse=lake"),
        ("lab_exp", "uri=http://polaris:8181", "uri=http://polaris.lakehouse.svc:8181"),
        ("lab_exp", "endpoint=http://seaweedfs:8333", "endpoint=http://seaweedfs.lakehouse.svc:8333"),
        ("lab_exp", "iceberg.catalog.type=rest", "iceberg.catalog.type=glue"),
        ("lake", "", ""),
        ("lake_ro", "", ""),
    ],
)
def test_a_catalog_that_could_reach_the_platform_is_refused(tmp_path: Path, name: str, old: str, new: str):
    (tmp_path / f"{name}.properties").write_text(LAB_CATALOG.replace(old, new) if old else LAB_CATALOG)
    with pytest.raises(LakeGuardError, match=name):
        check_catalog_files(tmp_path)


def test_a_non_iceberg_catalog_is_not_a_lakehouse(tmp_path: Path):
    (tmp_path / "tpch.properties").write_text("connector.name=tpch\n")
    assert check_catalog_files(tmp_path) == []


@pytest.mark.parametrize(
    "catalog",
    [
        '{"name": "lake", "location": "s3://lab-exp/warehouse"}',
        '{"name": "lab_exp", "location": "s3://lake/warehouse"}',
        '{"name": "lab_exp", "location": "s3://lab_exp/warehouse"}',
    ],
)
def test_a_polaris_spec_with_the_platform_warehouse_is_refused(tmp_path: Path, catalog: str):
    (tmp_path / "spec.json").write_text(f'{{"catalogs": [{catalog}]}}')
    with pytest.raises(LakeGuardError):
        check_polaris_spec(tmp_path / "spec.json")


def test_an_identity_that_sees_a_platform_catalog_is_not_on_the_lab():
    check_visible_catalogs(["lab_exp", "system", "tpch"])
    for catalogs in (["lake", "system"], ["lake_ro"]):
        with pytest.raises(LakeGuardError, match="platform"):
            check_visible_catalogs(catalogs)


def test_results_need_the_lab_environment():
    check_environment("lab", require_lab=True)
    check_environment("production", require_lab=False)
    with pytest.raises(LakeGuardError, match="production"):
        check_environment("production", require_lab=True)


@pytest.mark.parametrize("catalog", ["lab_exp", "lab_ops", "lab_ro", "lab_sbx", "lab_base_r20261018_1"])
def test_lab_catalogs_are_targets(catalog: str):
    check_target_catalog(catalog)


@pytest.mark.parametrize("catalog", ["lake", "lake_ro", "pg", "spike_open", "tpch", "lab_exp_x", "LAB_EXP"])
def test_other_catalogs_are_not_targets(catalog: str):
    with pytest.raises(LakeGuardError):
        check_target_catalog(catalog)


def test_locations_must_be_in_a_lab_bucket():
    check_location("s3://lab-exp/warehouse/probe/t-1a2b")
    for location in ("s3://lake/warehouse/bronze/orders", "s3://lab-exp", "s3a://lab-exp/x", "file:///tmp/t"):
        with pytest.raises(LakeGuardError):
            check_location(location)


def test_the_location_is_read_from_show_create():
    shown = (
        "CREATE TABLE lab_exp.probe.t (\n   id bigint\n)\nWITH (\n   format = 'PARQUET',\n   format_version = 2,\n"
        "   location = 's3://lab-exp/warehouse/probe/t-1a2b'\n)"
    )
    assert shown_location(shown) == "s3://lab-exp/warehouse/probe/t-1a2b"
    with pytest.raises(LakeGuardError):
        shown_location("CREATE TABLE x (id bigint)")
