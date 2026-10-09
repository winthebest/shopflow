# Local platform: k3d cluster + Argo CD + GitOps profiles (owner: sf-platform).
#   make up                                                       # sf-main, profile core, tracks main
#   make up CLUSTER=sf-platform GIT_REVISION=<pushed branch> PROFILES=core,obs-lite
#   make down CLUSTER=sf-platform
CLUSTER      ?= sf-main
PROFILES     ?= core
GIT_REVISION ?= main

.PHONY: up down status platform-argocd-ui platform-argocd-password platform-validate platform-cache-down

up: ## Create the k3d cluster, install Argo CD, apply root apps (CLUSTER, PROFILES, GIT_REVISION)
	@CLUSTER=$(CLUSTER) PROFILES=$(PROFILES) GIT_REVISION=$(GIT_REVISION) scripts/k3d-up.sh

down: ## Delete the k3d cluster and its local registry (CLUSTER)
	@CLUSTER=$(CLUSTER) scripts/k3d-down.sh

status: ## Show nodes, Argo CD Applications, routes and memory (CLUSTER)
	@CLUSTER=$(CLUSTER) scripts/k3d-status.sh

platform-argocd-ui: ## Port-forward the Argo CD UI to localhost (sf-main 18080, lanes 1808x; Ctrl-C stops it)
	@CLUSTER=$(CLUSTER) scripts/k3d-argocd.sh ui

platform-argocd-password: ## Copy the Argo CD admin password (user admin) from SOPS to the clipboard
	@CLUSTER=$(CLUSTER) scripts/k3d-argocd.sh password

platform-validate: ## Render deploy/ (Helm + Kustomize) and validate it like platform-ci does
	@scripts/platform-validate.sh

platform-cache-down: ## Delete the shared pull-through image caches and their data (they survive make down)
	@scripts/k3d-cache-down.sh
