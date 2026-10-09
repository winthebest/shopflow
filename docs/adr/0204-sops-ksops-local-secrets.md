# 0204. Local secrets with SOPS + age, decrypted by KSOPS in Argo CD

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

The repo is public. Admin passwords (Argo CD now, Grafana and others later) must not use chart defaults and must
not appear in Git in plain text, yet `make up` on a clean machine must restore them without manual steps. Only
one person holds keys. On AWS, secrets come from SSM Parameter Store via External Secrets (Phase 6); this ADR
covers local clusters.

## Decision

- Secrets are SOPS-encrypted files committed next to their overlay
  (`deploy/platform/<component>/<overlay>/secrets/*.enc.yaml`; cluster-wide ones in `deploy/secrets/local/`).
  `.sops.yaml` encrypts only `data`/`stringData` for the owner's age public recipient, so metadata stays readable.
- The age private key exists only at `~/.config/sops/age/keys.txt` (plus an offline copy). `make up` copies it into
  the `argocd/sops-age` Secret without printing it. The Argo CD repo-server gets `ksops` + `kustomize` from the
  pinned KSOPS image through an init container, and decrypts at build time via a Kustomize exec generator.
- The Argo CD admin password is the exception that cannot wait for KSOPS: `k3d-up.sh` decrypts it with the sops
  CLI, bcrypts it with `htpasswd`, and passes the hash to Helm through process substitution (never on disk or in
  the process list). The same file also becomes the `argocd-admin` Secret through KSOPS.
- Argo CD Kustomize build options are `--enable-alpha-plugins --enable-exec` only. The load restrictor stays at
  its default (`LoadRestrictionsRootOnly`): app definitions are directories so profiles can reference them
  without reading files outside their root.

## Alternatives considered

| Option | Why not |
|---|---|
| helm-secrets plugin in the repo-server | Works only for Helm values; plain-manifest components would need a second mechanism |
| Sealed Secrets | Ciphertext is bound to one cluster's key; every `make down`/`make up` creates a new cluster and would need re-sealing |
| External Secrets locally (Vault/Bitwarden backend) | One more stateful service on the laptop just to hold a few passwords |
| `LoadRestrictionsNone` so profiles can list app files | Lets any kustomization read arbitrary files on the repo-server, including the mounted age key; directories avoid the need |

## Consequences

- Positive: a clean machine with the age key gets every secret back from Git; secrets are reviewable (names,
  keys) without being readable; rotating a value is `sops <file>` + commit.
- Negative / risks: `--enable-exec` lets a kustomization in this repo run binaries on the repo-server, so the
  repo-server trusts the repo contents (single owner, CODEOWNERS review). Losing the age key means re-creating and
  re-encrypting every secret; the offline copy exists for that reason. CI cannot decrypt, so
  `scripts/platform-validate.sh` drops KSOPS generators and `make up` is the end-to-end test of decryption.
- When to revisit: a second person needs access (add a recipient and `sops updatekeys`), or KSOPS stops being
  maintained (switch to helm-secrets or External Secrets with a local backend).
