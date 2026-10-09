"""Daily Iceberg maintenance of catalog `lake`: optimize, optimize_manifests, expire_snapshots, remove_orphan_files.

One task per schema runs the dbt macro `iceberg_maintenance` (data/dbt/macros/iceberg_maintenance.sql) as user
`dbt`. Retention is 7 days, the catalog floor (docs/adr/0410): a file of a commit in flight is far younger, so
remove_orphan_files never deletes it. Scheduled at :30, between hourly dbt runs that rewrite silver and gold.
This fixed schedule is the baseline that Phase 11 compares a maintenance controller against.
"""

from datetime import UTC, datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from shopflow_dbt import dbt_command

SCHEMAS = ("bronze", "silver", "gold")

with DAG(
    dag_id="iceberg_maintenance",
    schedule="30 3 * * *",
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=10)},
    tags=["iceberg", "sf-data"],
    doc_md=__doc__,
):
    for schema in SCHEMAS:
        BashOperator(
            task_id=f"maintain_{schema}",
            bash_command=dbt_command(f"run-operation iceberg_maintenance --args '{{schema: {schema}}}'"),
        )
