# 0508. SSM Parameter Store (via External Secrets) instead of Secrets Manager

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

Locally, secrets are SOPS + age in git. On AWS the age private key must not be copied into the cluster, and secrets
must survive cluster destruction. Secrets Manager costs $0.40 per secret per month; the project needs ~10–20.

## Decision

Secrets live in SSM Parameter Store as **SecureString** with the default `aws/ssm` key under
`/shopflow/aws/<namespace>/<name>`, written only by `scripts/aws-seed-params.sh` from stdin (never on a command line;
idempotent; `--rotate` overwrites). External Secrets Operator reads them with one `SecretStore` per namespace, each
assuming a role that can read only its own path. OpenTofu never manages parameter values, so no secret enters state.

Control values that are **not secret** (`/shopflow/aws/control/*`: lease, backup pointer, initdb marker, session,
resume size; and `/shopflow/aws/kafka/cdc-epoch`) are plain **String** parameters, so the reapers read the lease
without `kms:Decrypt`. Rule: a secret is never a String parameter.

## Alternatives considered

| Option | Why not |
|---|---|
| Secrets Manager | $0.40/secret/month; rotation features not needed |
| SOPS + KSOPS on AWS | the age private key would have to live in the cluster |
| Parameters created by OpenTofu | values (and secrets) would be stored in the state file |

## Consequences

- Positive: standard tier is free; secrets outlive clusters; least privilege per namespace.
- Negative: a SecretStore could name another namespace's role (ESO does not restrict it); Kyverno (Phase 8) can
  enforce the mapping.
- When to revisit: if automatic rotation or cross-account sharing is needed.
