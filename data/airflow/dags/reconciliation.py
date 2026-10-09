"""Daily exact reconciliation of silver with Postgres: the dbt tests tagged `reconciliation` (enabled by the var
`reconcile`, so the hourly run never sees them).

A two-way anti-join on the key plus status/money columns (data/dbt/tests/generic/reconciles_with_postgres.sql).
Rows changed in Postgres after silver was last written, or in the 15 minutes before (CDC lag allowance), are left
out; any other difference fails the task.
"""

from datetime import UTC, datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from shopflow_dbt import dbt_command

with DAG(
    dag_id="reconciliation",
    schedule="45 4 * * *",
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["dbt", "reconciliation", "sf-data"],
    doc_md=__doc__,
):
    BashOperator(
        task_id="reconcile",
        bash_command=dbt_command("test --select tag:reconciliation --vars '{reconcile: true}'"),
    )
