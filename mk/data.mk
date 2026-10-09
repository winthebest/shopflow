# sf-data targets (docs/contracts/environment.md: public `duckdb`, everything else `data-*`).

CONNECT_IMAGE ?= shopflow-kafka-connect:dev

.PHONY: data-connect-image data-connect-smoke data-validate data-exporter-test data-dbt-check

data-connect-image: ## Build the Kafka Connect image for this machine's architecture (CONNECT_IMAGE)
	docker buildx build --load -t $(CONNECT_IMAGE) images/kafka-connect

data-connect-smoke: ## End-to-end test of the Connect image in docker compose (~3.6GB, no host ports)
	CONNECT_IMAGE=$(CONNECT_IMAGE) images/kafka-connect/smoke/run.sh

data-validate: ## Render data components like Argo CD and validate them (kubeconform -strict, pinned CRDs)
	scripts/data-validate.sh

data-exporter-test: ## Lint and unit-test services/freshness-exporter
	cd services/freshness-exporter && uv run --frozen ruff check . && uv run --frozen ruff format --check . \
		&& uv run --frozen pytest

data-dbt-check: ## dbt parse + sqlfluff of data/dbt (no database: the password is a parse-time placeholder)
	cd data/dbt && DBT_TRINO_PASSWORD=parse-only uv run --frozen dbt parse --no-partial-parse \
		&& uv run --frozen sqlfluff lint models tests
