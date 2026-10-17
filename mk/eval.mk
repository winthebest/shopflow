# sf-app targets: the operator agent's LLM layer and its evals (agent/, evals/). Included by the root Makefile.
# Prefixed with `eval-` so they never collide with other lanes' mk files. agent/ is its own uv project.
OLLAMA_URL ?= http://127.0.0.1:11434
OLLAMA_MODEL ?= qwen3:8b

.PHONY: eval-agent-test eval-agent-test-ollama eval-ollama-unload

eval-agent-test: ## Agent lint + provider contract against fakes (offline; the live Claude test skips without a key)
	cd agent && uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run pytest -m "not ollama"

eval-agent-test-ollama: ## Live provider contract against local Ollama (loads qwen3:8b, ~5 GB; unloads it after)
	cd agent && uv sync --locked && uv run pytest -m ollama -v

eval-ollama-unload: ## Free the local model's memory now (do this before a k3d slot)
	curl -fsS $(OLLAMA_URL)/api/generate -d '{"model": "$(OLLAMA_MODEL)", "keep_alive": 0}' > /dev/null
	curl -fsS $(OLLAMA_URL)/api/ps
