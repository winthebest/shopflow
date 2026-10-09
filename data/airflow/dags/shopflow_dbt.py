"""Shared dbt settings for the shopflow DAGs (this module defines no DAG).

The image (data/airflow/Dockerfile) puts the dbt project, with a manifest parsed at image build, in
SHOPFLOW_DBT_PROJECT and dbt itself in its own virtualenv (SHOPFLOW_DBT_EXECUTABLE). Trino credentials reach dbt
through the environment that data/dbt/profiles.yml reads (Secret trino-dbt, the internal CA file).
"""

import os

from cosmos import ExecutionConfig, ProfileConfig, ProjectConfig

DBT_PROJECT = os.environ.get("SHOPFLOW_DBT_PROJECT", "/opt/shopflow/dbt")
DBT_EXECUTABLE = os.environ.get("SHOPFLOW_DBT_EXECUTABLE", "/opt/dbt-venv/bin/dbt")

PROJECT_CONFIG = ProjectConfig(dbt_project_path=DBT_PROJECT, manifest_path=f"{DBT_PROJECT}/target/manifest.json")
PROFILE_CONFIG = ProfileConfig(
    profile_name="shopflow", target_name="lake", profiles_yml_filepath=f"{DBT_PROJECT}/profiles.yml"
)
EXECUTION_CONFIG = ExecutionConfig(dbt_executable_path=DBT_EXECUTABLE)


def dbt_command(args: str) -> str:
    """Shell command for a BashOperator: dbt on the shopflow project, artifacts in a fresh temporary directory.

    The project directory is image content; each run writes its own artifacts so parallel tasks never share them.
    """
    return (
        f'{DBT_EXECUTABLE} {args} --project-dir {DBT_PROJECT} --profiles-dir {DBT_PROJECT} --target-path "$(mktemp -d)"'
    )
