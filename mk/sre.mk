# sf-sre targets: observability stack + SLOs (docs/contracts/ownership.md). Logic lives in scripts/sre-check.sh.
SRE_CHECK := ./scripts/sre-check.sh

.PHONY: sre-ci sre-render sre-kubeconform sre-slo sre-slo-drift sre-rules sre-configs sre-lint sre-chaos-check

sre-ci: ## Run every offline sre check (same as .github/workflows/sre-ci.yml)
	$(SRE_CHECK) all

sre-render: ## Render all sre Argo apps (Helm + Kustomize) into out/sre/rendered
	$(SRE_CHECK) render

sre-kubeconform: ## Schema-check rendered manifests, app dirs and profiles with kubeconform
	$(SRE_CHECK) kubeconform

sre-slo: ## Regenerate SLO rules from every slo/*.yaml with Sloth + the rules kustomization (commit the result)
	$(SRE_CHECK) slo

sre-slo-drift: ## Fail if generated SLO rules or the rules kustomization are stale
	$(SRE_CHECK) slo-drift

sre-rules: ## Runbook links + promtool check/test of the SLO rules
	$(SRE_CHECK) rules

sre-configs: ## Validate OTel Collector, Loki, Tempo, Alertmanager configs with their own binaries
	$(SRE_CHECK) configs

sre-lint: ## shellcheck, dashboard JSON, secrets encrypted, every rendered image pinned by digest
	$(SRE_CHECK) lint

# Game days (docs/runbooks/gameday.md). Local overlay; on EKS add `chaos` to the session's profiles instead.
SRE_KUBE_CONTEXT ?= k3d-$(CLUSTER)

.PHONY: sre-chaos-on sre-chaos-off
sre-chaos-on: ## Game day start: install Chaos Mesh (profile chaos) on CLUSTER at GIT_REVISION
	KUBE_CONTEXT=$(SRE_KUBE_CONTEXT) ./scripts/platform-root-apps.sh --overlay local --revision $(GIT_REVISION) --profiles chaos

sre-chaos-off: ## Game day end: remove Chaos Mesh completely (experiments, CRDs, daemon, webhooks) and verify
	KUBE_CONTEXT=$(SRE_KUBE_CONTEXT) ./scripts/sre-chaos-off.sh

sre-chaos-check: ## Game-day experiments: CRD schema, allowed namespaces, auto-stop; chaos-mesh only in profile chaos
	$(SRE_CHECK) chaos

# Gate check on a running cluster (read-only). Default: the cluster of CLUSTER (sf-main during gates).
SRE_GATE_CONTEXT ?= k3d-$(CLUSTER)
SRE_GATE_WINDOW  ?= 2h

.PHONY: sre-gate-check
sre-gate-check: ## Gate check, read-only: targets, SLO data, WAL series, Tempo memory, alerts (SRE_GATE_CONTEXT, SRE_GATE_WINDOW)
	./scripts/sre-gate-check.sh --context $(SRE_GATE_CONTEXT) --window $(SRE_GATE_WINDOW)
