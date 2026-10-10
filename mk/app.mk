# sf-app targets: shop services, compose dev loop, tests. Included by the root Makefile.
# Prefixed with `app-` (except `dev*`) so they never collide with other lanes' mk files.
COMPOSE ?= docker compose

.PHONY: dev dev-down dev-reset dev-logs app-sync app-lint app-fmt app-test app-test-unit app-hooks app-loadtest app-worker-check

dev: ## Build and start postgres + migrate + seed + 3 services on compose (gateway: http://localhost:8000)
	$(COMPOSE) up --build --detach --wait

dev-down: ## Stop the compose stack (keeps the Postgres volume)
	$(COMPOSE) down --remove-orphans

dev-reset: ## Stop the compose stack and delete the Postgres volume
	$(COMPOSE) down --volumes --remove-orphans

dev-logs: ## Follow compose logs (JSON lines)
	$(COMPOSE) logs --follow

app-sync: ## Install the uv workspace: all services + dev tools
	uv sync --all-packages --locked

app-lint: app-sync ## ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

app-fmt: app-sync ## ruff autofix and format
	uv run ruff check --fix .
	uv run ruff format .

app-test: app-sync ## All tests; integration tests start Postgres with testcontainers (needs Docker)
	uv run pytest

app-test-unit: app-sync ## Unit tests only (no Docker)
	uv run pytest -m "not integration"

app-hooks: ## Install the pre-commit hooks (gitleaks) in this clone
	pre-commit install

LOADTEST_PROFILE ?= constant
LOADTEST_RATE ?= 20
LOADTEST_DURATION ?= 5m
LOADTEST_STAMP := $(shell date -u +%Y%m%dT%H%M%SZ)

app-loadtest: ## k6 browse+checkout on the gateway (LOADTEST_PROFILE=constant|ramp, LOADTEST_RATE=20/s, LOADTEST_DURATION=5m); ack log + summary in out/
	@mkdir -p out
	k6 run --log-format raw \
		--console-output out/acks-$(LOADTEST_STAMP).jsonl \
		--summary-export out/k6-summary-$(LOADTEST_STAMP).json \
		-e PROFILE=$(LOADTEST_PROFILE) -e RATE=$(LOADTEST_RATE) -e DURATION=$(LOADTEST_DURATION) \
		loadtest/checkout.js

app-worker-check: ## fulfillment-worker on a cluster (CLUSTER=sf-main): copies, SASL_SSL group, KEDA 1->N->min under k6, 0 duplicate shipments (SKIP_LOAD=1: no load)
	scripts/app-worker-check.sh
