# 0500. OpenTofu instead of Terraform for the AWS layers

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

The AWS environment is code from day one (three layers, see 0501/0502) and must be testable offline, without an
AWS account or credentials, because most of Phase 6 is written before the first paid session. Terraform moved to
the BSL licence in 2023; OpenTofu is the MPL-licensed fork under the Linux Foundation.

## Decision

Use OpenTofu (1.13, `required_version >= 1.10`) with the S3 backend and `use_lockfile = true`, and test every layer
and module with `tofu test` and `mock_provider "aws"` (plan mode: mocked values without any API call).

## Alternatives considered

| Option | Why not |
|---|---|
| Terraform 1.x | BSL licence; no technical gain for this project; same HCL, so switching back stays cheap |
| Pulumi / CDK | a second language for infra and a runtime dependency; fewer offline testing tools for IAM policy content |
| S3 backend + DynamoDB lock | one more resource; S3 conditional writes (`use_lockfile`) give the same mutual exclusion |

## Consequences

- Positive: open licence; offline tests assert real policy content (boundaries, prefixes, OIDC subjects); the S3
  lockfile is also what the GitHub reaper takes to exclude a concurrent `cloud-up` (0501).
- Negative: some ecosystem tools target Terraform first (provider docs, scanners); `tflint` and `trivy` handle
  OpenTofu code fine today.
- When to revisit: a provider or tool the project needs stops supporting OpenTofu.
