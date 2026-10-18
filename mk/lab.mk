# Lakehouse lab targets (docs/contracts/environment.md: `lab-*`, sf-data). The stack is docker compose, outside k3d:
# lab/compose.yml through lab/stack.sh (docs/adr/0419-lab-stack-separate-from-platform.md).

.PHONY: lab-up lab-down lab-reset lab-base lab-base-drop lab-mem lab-test lab-check

lab-up: ## Start the lab stack: Trino https://127.0.0.1:28443, OTLP 127.0.0.1:24317/24318 (limits 4.9 GiB)
	lab/stack.sh up

lab-down: ## Stop the lab stack; volumes and generated credentials stay
	lab/stack.sh down

lab-reset: ## Remove the lab stack's containers, volumes and every generated credential (out/lab)
	lab/stack.sh reset

lab-base: ## Make RUN the active baseline run: catalog lab_base_<RUN> for baseline_writer (restarts Trino)
	@test -n "$(RUN)" || { echo "usage: make lab-base RUN=<run id>"; exit 2; }
	lab/stack.sh base $(RUN)

lab-base-drop: ## No active baseline run (its warehouse and data stay until lab-reset; restarts Trino)
	lab/stack.sh base-drop

lab-mem: ## One memory/CPU sample of the running lab containers
	lab/stack.sh mem

lab-test: ## Deny matrix and harness tests against the running lab stack (fails if it is not up; activates run matrix)
	lab/stack.sh base matrix
	cd lakeops && LAB_REQUIRED=1 uv run --frozen pytest -m lab
	cd lab && LAB_REQUIRED=1 uv run --frozen pytest -m lab

lab-check: ## Lint lab/ and lakeops/ and run their tests that need no stack (CI)
	@# pytest exits 5 when it collects nothing: lakeops may have no test without the stack yet.
	cd lakeops && uv run --frozen ruff check . && uv run --frozen ruff format --check . \
		&& { uv run --frozen pytest -m "not lab"; status=$$?; [ $$status -eq 0 ] || [ $$status -eq 5 ]; }
	cd lab && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen pytest -m "not lab"
