"""Daily exact reconciliation of silver with Postgres: the dbt tests tagged `reconciliation` (enabled by the var
`reconcile`, so the hourly run never sees them).

A two-way anti-join on the key plus status/money columns (data/dbt/tests/generic/reconciles_with_postgres.sql).
Rows changed in Postgres after silver was last written, or in the 15 minutes before (CDC lag allowance), are left
out; any other difference fails the task.

`on_time_guard` skips a scheduled run that starts more than an hour after its interval ended. When a cluster starts,
the scheduler at once runs the interval that ended before it (catchup=False still runs the latest one), while silver
does not exist yet: that run would fail and raise a false AirflowDagRunFailed ticket. The next scheduled run
reconciles; manual runs always run.
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
    # Exit code 99 is BashOperator's skip: the reconcile task is skipped too, and the run ends successful.
    on_time_guard = BashOperator(
        task_id="on_time_guard",
        bash_command=(
            "{% if dag_run.run_type == 'scheduled' %}"
            "late=$(( $(date +%s) - {{ data_interval_end.int_timestamp }} )); "
            'if [ "$late" -gt 3600 ]; then echo "interval ended ${late}s ago (cluster start?): skipped"; exit 99; fi; '
            "{% endif %}"
            "echo on time"
        ),
    )
    reconcile = BashOperator(
        task_id="reconcile",
        bash_command=dbt_command("test --select tag:reconciliation --vars '{reconcile: true}'"),
    )

    on_time_guard >> reconcile
