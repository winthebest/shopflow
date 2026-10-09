# syntax=docker/dockerfile:1
# Build context: data/ (data/.dockerignore lets in only airflow/dags, airflow/tests and dbt).
# Airflow 3 slim + only what runs here (Postgres metadata DB, FAB auth manager, Cosmos), under Airflow's published
# constraints; dbt in its own virtualenv (pins from data/dbt/uv.lock, so they never fight Airflow's constraints); the
# dbt project with a manifest parsed at image build (Cosmos renders the task graph from it without running dbt); the
# DAGs. Slim, not the full image: the full one ships dozens of unused providers with CRITICAL CVEs.
ARG AIRFLOW_IMAGE=apache/airflow:slim-3.3.2-python3.12@sha256:a54e7bd6c4d7a17e55cd14b0ead257877b8f8111680d2692cbdba2ff32bd3ff3

FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

# dbt virtualenv on the same Python as the final image, so the copied venv runs there unchanged.
FROM ${AIRFLOW_IMAGE} AS dbt
COPY --from=uv /uv /usr/local/bin/uv
USER root
ENV VIRTUAL_ENV= UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/python/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/dbt-venv
WORKDIR /opt/shopflow/dbt
COPY dbt/pyproject.toml dbt/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY dbt/ ./
# Parse needs no database; the placeholder only satisfies env_var() in profiles.yml.
RUN DBT_TRINO_PASSWORD=parse-only /opt/dbt-venv/bin/dbt parse --profiles-dir . --target-path target --no-partial-parse \
    && rm -rf logs

FROM ${AIRFLOW_IMAGE}
ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.12
ARG COSMOS_VERSION=1.15.1
LABEL org.opencontainers.image.source="https://github.com/winthebest/shopflow" \
      org.opencontainers.image.title="shopflow-airflow" \
      org.opencontainers.image.description="shopflow Airflow 3: Cosmos, dbt (separate venv), dbt project and DAGs"
# Debian security fixes newer than the base image (the slim base carries no unused packages to begin with).
USER root
RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get clean && rm -rf /var/lib/apt/lists/*
USER airflow
# Pinning apache-airflow under its release constraints keeps pip from moving Airflow or any provider.
RUN pip install --no-cache-dir "apache-airflow[postgres,fab]==${AIRFLOW_VERSION}" "astronomer-cosmos==${COSMOS_VERSION}" \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
COPY --from=dbt --chown=airflow:0 /opt/dbt-venv /opt/dbt-venv
COPY --from=dbt --chown=airflow:0 /opt/shopflow/dbt /opt/shopflow/dbt
COPY --chown=airflow:0 airflow/dags/ /opt/airflow/dags/
COPY --chown=airflow:0 airflow/tests/ /opt/shopflow/airflow-tests/
# dbt writes logs next to the project by default; the project directory is image content.
ENV SHOPFLOW_DBT_PROJECT=/opt/shopflow/dbt \
    SHOPFLOW_DBT_EXECUTABLE=/opt/dbt-venv/bin/dbt \
    DBT_LOG_PATH=/tmp/dbt-logs \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false
