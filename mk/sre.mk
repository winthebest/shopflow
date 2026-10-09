# sf-sre targets: observability stack + SLOs (docs/contracts/ownership.md). Logic lives in scripts/sre-check.sh.
SRE_CHECK := ./scripts/sre-check.sh

.PHONY: sre-ci sre-render sre-kubeconform sre-slo sre-slo-drift sre-rules sre-configs sre-lint

sre-ci: ## Run every offline sre check (same as .github/workflows/sre-ci.yml)
	$(SRE_CHECK) all

sre-render: ## Render all sre Argo apps (Helm + Kustomize) into out/sre/rendered
	$(SRE_CHECK) render

sre-kubeconform: ## Schema-check rendered manifests, app dirs and profiles with kubeconform
	$(SRE_CHECK) kubeconform

sre-slo: ## Regenerate SLO rules from slo/checkout.yaml with Sloth (commit the result)
	$(SRE_CHECK) slo

sre-slo-drift: ## Fail if the committed SLO rules are stale
	$(SRE_CHECK) slo-drift

sre-rules: ## Runbook links + promtool check/test of the SLO rules
	$(SRE_CHECK) rules

sre-configs: ## Validate OTel Collector, Loki, Tempo, Alertmanager configs with their own binaries
	$(SRE_CHECK) configs

sre-lint: ## shellcheck, dashboard JSON, secrets encrypted, every rendered image pinned by digest
	$(SRE_CHECK) lint
