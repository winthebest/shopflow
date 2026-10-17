# Lakehouse lab targets (docs/contracts/environment.md: `lab-*`, sf-data). The stack is docker compose, outside k3d:
# lab/compose.yml through lab/stack.sh (docs/adr/0419-lab-stack-separate-from-platform.md).

.PHONY: lab-up lab-down lab-reset lab-base lab-base-drop lab-py lab-mem lab-test lab-check lab-report lab-freeze \
	lab-run lab-ledger lab-publish-holdout

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

lab-py: ## Python in a container on the lab network with a zone's catalog credentials (ZONE=exp, ARGS="python -m …")
	@test -n "$(ZONE)" && test -n "$(ARGS)" || { echo 'usage: make lab-py ZONE=exp ARGS="python -m <module> …"'; exit 2; }
	lab/stack.sh py $(ZONE) -- $(ARGS)

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

LAB_RESULTS_DIR ?= $(CURDIR)/lab/results
lab-report: ## README tables and chart of one split's results (EXP=…, SPLIT=dev|holdout, METRIC=…, BASELINE=arm, NOISE_FLOOR=…)
	@test -n "$(EXP)" && test -n "$(SPLIT)" && test -n "$(METRIC)" \
		|| { echo "usage: make lab-report EXP=<experiment> SPLIT=dev|holdout METRIC=<metric>"; exit 2; }
	cd lab && uv run --frozen python -m lab.report $(LAB_RESULTS_DIR)/$(EXP)/*.parquet --metric $(METRIC) --split $(SPLIT) \
		$(if $(BASELINE),--baseline $(BASELINE)) --noise-floor $(or $(NOISE_FLOOR),0) \
		--out $(LAB_RESULTS_DIR)/$(EXP)/report/$(SPLIT)

.PHONY: lab-noise
lab-noise: ## Measure the lab's noise floor: A/A comparisons of two queries (RUNS=25), written to lab/results/e0_noise
	cd lab && uv run --frozen python -m lab.noise $(if $(RUNS),--runs $(RUNS))

# Experiments (docs/research/protocol.md): dev runs at will; the held-out split once, from frozen code.
lab-freeze: ## Freeze an experiment's code before its held-out run (EXP=…; writes lab/freezes/<EXP>.json)
	@test -n "$(EXP)" || { echo "usage: make lab-freeze EXP=<experiment>"; exit 2; }
	cd lab && uv run --frozen python -m lab.runner freeze $(EXP)

lab-run: ## Run an experiment (EXP=…, SPLIT=dev|holdout, SEED=0; RERUN_REASON=… only after an infra failure)
	@test -n "$(EXP)" && test -n "$(SPLIT)" || { echo "usage: make lab-run EXP=<experiment> SPLIT=dev|holdout"; exit 2; }
	cd lab && uv run --frozen python -m lab.runner run $(EXP) $(SPLIT) --seed $(or $(SEED),0) \
		$(if $(RERUN_REASON),--rerun-after-infra-failure "$(RERUN_REASON)")

lab-ledger: ## Every held-out attempt of an experiment, for its README (EXP=…)
	cd lab && uv run --frozen python -m lab.runner ledger $(EXP)

lab-publish-holdout: ## After the held-out run finished: write the decrypted held-out scenarios (EXP=…; the age key)
	cd lab && uv run --frozen python -m lab.runner publish $(EXP)
