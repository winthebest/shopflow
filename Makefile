# Root Makefile: each lane owns its own mk/<lane>.mk (see docs/contracts/ownership.md).
.DEFAULT_GOAL := help
-include mk/*.mk

.PHONY: help
help: ## List available targets
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-24s %s\n", $$1, $$2}'
