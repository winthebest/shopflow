# sf-cloud: offline infra checks. Every cloud-check target runs with AWS credentials stripped from
# the environment.

# Offline checks must never pick up real AWS credentials.
CLOUD_OFFLINE := env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
	AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true
CLOUD_TOFU_DIRS := infra/tofu/bootstrap infra/tofu/network infra/tofu/cluster $(sort $(wildcard infra/tofu/modules/*))
# Pinned by digest (Trivy 0.74.0); bump deliberately.
CLOUD_TRIVY_IMAGE := aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969
CLOUD_ZIZMOR_VERSION := 1.30.1

.PHONY: cloud-check cloud-fmt cloud-validate cloud-test cloud-lint cloud-trivy cloud-pytest cloud-zizmor

cloud-check: cloud-fmt cloud-validate cloud-test cloud-lint cloud-trivy cloud-pytest ## All offline infra checks (what infra-ci runs)

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

cloud-pytest: ## Reaper unit tests (moto, no AWS)
	cd infra && $(CLOUD_OFFLINE) uv run --locked pytest

cloud-zizmor: ## zizmor on the infra workflow (CI runs it repo-wide in ci.yml)
	uvx "zizmor==$(CLOUD_ZIZMOR_VERSION)" --min-severity=low .github/workflows/infra-ci.yml
