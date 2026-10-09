"""Hourly dbt run: bronze -> silver -> gold (docs/adr/0412, 0413).

1. `cdc_epoch_guard`: fails when no CDC epoch has completed its snapshot, so silver is never rebuilt empty.
2. `models`: every silver and gold model, then its tests (Cosmos, from the manifest baked into the image).
   Reconciliation tests are not in that manifest (enabled only by the var `reconcile`); they have their own DAG.
3. `source_freshness`: bronze heartbeat freshness (warn 10 min, error 30 min), alongside, never blocking the models.
"""

from datetime import UTC, datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from cosmos import DbtTaskGroup, RenderConfig
from cosmos.constants import LoadMode
from shopflow_dbt import EXECUTION_CONFIG, PROFILE_CONFIG, PROJECT_CONFIG, dbt_command

with DAG(
    dag_id="dbt_build",
    schedule="@hourly",
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
    tags=["dbt", "sf-data"],
    doc_md=__doc__,
):
    epoch_guard = BashOperator(
        task_id="cdc_epoch_guard",
        bash_command=dbt_command("test --select assert_completed_cdc_epoch_exists"),
    )
    models = DbtTaskGroup(
        group_id="models",
        project_config=PROJECT_CONFIG,
        profile_config=PROFILE_CONFIG,
        execution_config=EXECUTION_CONFIG,
        # Ephemeral staging models compile into the silver SQL; as tasks they would only start dbt to do nothing.
        # A test with several parents (relationships) gets its own task after all of them: attached to one parent,
        # it would query a sibling table that this run has not written yet.
        render_config=RenderConfig(
            load_method=LoadMode.DBT_MANIFEST,
            exclude=["config.materialized:ephemeral"],
            should_detach_multiple_parents_tests=True,
        ),
        operator_args={"install_deps": False},
    )
    source_freshness = BashOperator(task_id="source_freshness", bash_command=dbt_command("source freshness"))

    epoch_guard >> models
