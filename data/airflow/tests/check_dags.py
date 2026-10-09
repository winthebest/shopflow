"""DAG import check, run inside the shopflow-airflow image (`make data-airflow-check`, data-ci): every DAG file
imports without errors, and the three DAGs have their schedules and tasks. Needs no metadata database.
"""

import json
import os
import sys

try:  # Airflow >= 3.2 location; earlier 3.x exposes it under airflow.models
    from airflow.dag_processing.dagbag import DagBag
except ImportError:
    from airflow.models.dagbag import DagBag

DAGS_FOLDER = "/opt/airflow/dags"
MANIFEST = os.path.join(os.environ.get("SHOPFLOW_DBT_PROJECT", "/opt/shopflow/dbt"), "target", "manifest.json")


def dbt_models(*schemas: str) -> set[str]:
    with open(MANIFEST) as f:
        nodes = json.load(f)["nodes"].values()
    return {n["name"] for n in nodes if n["resource_type"] == "model" and n["config"]["schema"] in schemas}


def main() -> None:
    # Airflow's DAG processor puts the DAGs folder on sys.path (shared modules such as shopflow_dbt); do the same.
    sys.path.insert(0, DAGS_FOLDER)
    bag = DagBag(dag_folder=DAGS_FOLDER)
    problems = [f"import error in {path}: {error}" for path, error in bag.import_errors.items()]

    expected = {
        "dbt_build": ("@hourly", {"cdc_epoch_guard", "source_freshness"}),
        "iceberg_maintenance": ("30 3 * * *", {"maintain_bronze", "maintain_silver", "maintain_gold"}),
        "reconciliation": ("45 4 * * *", {"reconcile"}),
    }
    if set(bag.dag_ids) != set(expected):
        problems.append(f"DAG ids {sorted(bag.dag_ids)} != {sorted(expected)}")
    for dag_id, (schedule, tasks) in expected.items():
        dag = bag.dags.get(dag_id)
        if dag is None:
            continue
        if dag.schedule != schedule:
            problems.append(f"{dag_id}: schedule {dag.schedule!r} != {schedule!r}")
        missing = tasks - set(dag.task_ids)
        if missing:
            problems.append(f"{dag_id}: missing tasks {sorted(missing)}")

    # Cosmos renders one task group per model under `models`; every silver and gold model must be there, and no
    # reconciliation test (those run in their own DAG).
    dbt_build = bag.dags.get("dbt_build")
    if dbt_build is not None:
        model_tasks = [t for t in dbt_build.task_ids if t.startswith("models.")]
        for model in sorted(dbt_models("silver", "gold")):
            if not any(t.startswith(f"models.{model}") for t in model_tasks):
                problems.append(f"dbt_build: no task for model {model}")
        if any("reconciles_with_postgres" in t for t in model_tasks):
            problems.append("dbt_build: renders reconciliation tests")
        if any(t.startswith("models.stg_") for t in model_tasks):
            problems.append("dbt_build: renders ephemeral staging models as tasks")
        guard = dbt_build.get_task("cdc_epoch_guard")
        if not {t for t in dbt_build.task_ids if t.startswith("models.")} & guard.get_flat_relative_ids(upstream=False):
            problems.append("dbt_build: cdc_epoch_guard is not upstream of the models")

    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        sys.exit(1)
    print(
        f"OK {len(bag.dag_ids)} DAGs: "
        + ", ".join(f"{d} ({len(bag.dags[d].task_ids)} tasks)" for d in sorted(bag.dag_ids))
    )


if __name__ == "__main__":
    main()
