# sf-data targets (docs/contracts/environment.md: public `duckdb`, everything else `data-*`).

CONNECT_IMAGE ?= shopflow-kafka-connect:dev
AIRFLOW_IMAGE ?= shopflow-airflow:dev
FLINK_IMAGE ?= shopflow-flink:dev

.PHONY: data-connect-image data-connect-smoke data-validate data-exporter-test data-dbt-check data-airflow-image \
	data-airflow-check data-flink-image data-flink-check

data-connect-image: ## Build the Kafka Connect image for this machine's architecture (CONNECT_IMAGE)
	docker buildx build --load -t $(CONNECT_IMAGE) images/kafka-connect

data-connect-smoke: ## End-to-end test of the Connect image in docker compose (~3.6GB, no host ports)
	CONNECT_IMAGE=$(CONNECT_IMAGE) images/kafka-connect/smoke/run.sh

data-validate: ## Render data components like Argo CD and validate them (kubeconform -strict, pinned CRDs)
	scripts/data-validate.sh

data-exporter-test: ## Lint and unit-test services/freshness-exporter
	cd services/freshness-exporter && uv run --frozen ruff check . && uv run --frozen ruff format --check . \
		&& uv run --frozen pytest

data-dbt-check: ## dbt parse (with and without reconciliation tests) + sqlfluff of data/dbt; no database needed
	cd data/dbt && export DBT_TRINO_PASSWORD=parse-only && uv run --frozen dbt parse --no-partial-parse \
		&& uv run --frozen dbt parse --no-partial-parse --vars '{reconcile: true}' \
		&& uv run --frozen sqlfluff lint models tests

data-airflow-image: ## Build the Airflow image (DAGs + dbt) for this machine's architecture (AIRFLOW_IMAGE)
	docker buildx build --load -t $(AIRFLOW_IMAGE) -f data/airflow/Dockerfile data

data-airflow-check: ## Import every DAG inside AIRFLOW_IMAGE and check DAG ids, schedules and tasks (no database)
	docker run --rm --network none -e AIRFLOW__CORE__LOAD_EXAMPLES=False $(AIRFLOW_IMAGE) \
		python /opt/shopflow/airflow-tests/check_dags.py

data-flink-image: ## Build the Flink KPI job image for this machine's architecture (FLINK_IMAGE)
	docker buildx build --load -t $(FLINK_IMAGE) data/flink

data-flink-check: ## Plan the KPI job inside FLINK_IMAGE: connectors load, SQL valid (no Kafka or Postgres needed)
	docker run --rm --network none -e CDC_EPOCH=0 -e KAFKA_USER=flink -e KAFKA_PASSWORD=plan-only \
		-e SERVING_USER=flink_serving -e SERVING_PASSWORD=plan-only $(FLINK_IMAGE) \
		java -cp '/opt/flink/lib/*:/opt/flink/usrlib/sql-runner.jar' io.shopflow.flink.SqlRunner \
		/opt/flink/sql/kpi_minute.sql --explain
