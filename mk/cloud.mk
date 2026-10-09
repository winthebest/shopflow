# sf-cloud: AWS session lifecycle (cloud-up/down/pause/resume/extend) and offline infra checks.
# Session targets touch AWS through the shopflow-operator profile; every cloud-check target runs
# offline with AWS credentials stripped from the environment.

CLOUD_ARGS ?=
HOURS ?= 2

# Offline checks must never pick up real AWS credentials.
CLOUD_OFFLINE := env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
	AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true
CLOUD_TOFU_DIRS := infra/tofu/bootstrap infra/tofu/network infra/tofu/cluster $(sort $(wildcard infra/tofu/modules/*))
CLOUD_SCRIPTS := scripts/cloud-lib.sh $(filter-out scripts/cloud-lib.sh,$(wildcard scripts/cloud-*.sh)) $(wildcard scripts/aws-*.sh) scripts/export-evidence.sh
# Pinned by digest (Trivy 0.74.0, ShellCheck v0.11.0); bump deliberately.
CLOUD_TRIVY_IMAGE := aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969
CLOUD_SHELLCHECK_IMAGE := koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d
CLOUD_ZIZMOR_VERSION := 1.30.1

.PHONY: cloud-up cloud-down cloud-pause cloud-resume cloud-extend cloud-orphan-check cloud-seed-params cloud-evidence \
	cloud-check cloud-fmt cloud-validate cloud-test cloud-lint cloud-trivy cloud-shellcheck cloud-pytest cloud-zizmor \
	cloud-manifests

cloud-up: ## Start an AWS session (CLOUD_ARGS="--dry-run|--resume|--hours N|--pitr TIME")
	scripts/cloud-up.sh $(CLOUD_ARGS)

cloud-down: ## End the AWS session, leave nothing billed by the hour (CLOUD_ARGS="--dry-run|--force-api")
	scripts/cloud-down.sh $(CLOUD_ARGS)

cloud-pause: ## Scale the session's node group to zero for a short break (< 4h)
	scripts/cloud-pause.sh $(CLOUD_ARGS)

cloud-resume: ## Scale the node group back after cloud-pause
	scripts/cloud-pause.sh --resume $(CLOUD_ARGS)

cloud-extend: ## Extend the session lease by HOURS (default 2)
	scripts/cloud-extend.sh --hours $(HOURS) $(CLOUD_ARGS)

cloud-orphan-check: ## List billable leftovers in every region (CLOUD_ARGS="--delete-tagged")
	scripts/aws-orphan-check.sh $(CLOUD_ARGS)

cloud-seed-params: ## Seed app secrets into SSM from stdin JSON: make cloud-seed-params < secrets.json
	scripts/aws-seed-params.sh $(CLOUD_ARGS)

cloud-evidence: ## Upload the current session's evidence to S3 (CLOUD_ARGS="--session ID")
	scripts/export-evidence.sh $(CLOUD_ARGS)

cloud-check: cloud-fmt cloud-validate cloud-test cloud-lint cloud-trivy cloud-shellcheck cloud-pytest cloud-manifests ## All offline infra checks (what infra-ci runs)

cloud-fmt: ## tofu fmt -check
	tofu fmt -check -recursive infra/tofu

cloud-validate: ## tofu validate for every layer and module (no backend, no credentials)
	@set -e; for d in $(CLOUD_TOFU_DIRS); do \
		echo "validate $$d"; \
		$(CLOUD_OFFLINE) tofu -chdir=$$d init -backend=false -input=false -lockfile=readonly >/dev/null; \
		$(CLOUD_OFFLINE) tofu -chdir=$$d validate -no-color; \
	done

cloud-test: cloud-validate ## tofu test with a mocked AWS provider for every layer and module
	@set -e; for d in $(CLOUD_TOFU_DIRS); do echo "test $$d"; $(CLOUD_OFFLINE) tofu -chdir=$$d test -no-color; done

cloud-lint: ## tflint (AWS ruleset) on every layer and module
	cd infra/tofu && tflint --init --config "$$PWD/.tflint.hcl" && tflint --recursive --config "$$PWD/.tflint.hcl"

cloud-trivy: ## trivy config scan of the OpenTofu code (fails on any unjustified finding)
	docker run --rm -v "$(CURDIR)/infra:/src:ro" $(CLOUD_TRIVY_IMAGE) config --quiet --exit-code 1 /src/tofu

cloud-shellcheck: ## shellcheck the cloud scripts
	docker run --rm -v "$(CURDIR):/mnt:ro" -w /mnt $(CLOUD_SHELLCHECK_IMAGE) -x -S style $(CLOUD_SCRIPTS)

cloud-pytest: ## Reaper unit tests (moto) and cloud script tests (fake CLIs)
	cd infra && $(CLOUD_OFFLINE) uv run --locked pytest

cloud-manifests: ## Render the AWS-only components (ESO, LB controller, OpenCost); validate CRs and the cloud contract
	scripts/cloud-manifests-check.sh

cloud-zizmor: ## zizmor on the cloud workflows (CI runs it repo-wide in ci.yml)
	uvx "zizmor==$(CLOUD_ZIZMOR_VERSION)" --min-severity=low .github/workflows/infra-ci.yml .github/workflows/cloud-reaper.yml
