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
idempotent; `--rotate` overwrites). A Secret with several keys is one parameter holding a JSON object. External
Secrets Operator reads them through one **ClusterSecretStore per namespace** (`ssm-<namespace>`): its `conditions`
admit only ExternalSecrets from that namespace, and its role (`shopflow-eso-<namespace>`, assumed by the ESO
controller through Pod Identity) can read only `/shopflow/aws/<namespace>/*`. Stores are cluster-scoped, so only
cluster admins (Argo CD) create them. OpenTofu never manages parameter values, so no secret enters state.

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
- Negative: the account ID is a session parameter of the store chart (role ARNs need it); a missing parameter
  renders a placeholder that cannot assume any role, so ExternalSecrets fail visibly instead of reading the
  wrong place. Namespaced SecretStores were rejected: any namespace could name another namespace's role.
- When to revisit: if automatic rotation or cross-account sharing is needed.
