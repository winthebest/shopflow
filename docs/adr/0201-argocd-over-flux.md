# 0201. GitOps with Argo CD app-of-apps (not Flux)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

The same Git repo must drive a laptop cluster (k3d) and an ephemeral EKS cluster, with differences only in
overlays. Several lanes add components in parallel, each owning its own app file. Installation order matters
(CRDs → operators → config → workloads), the shop needs its database migration to run before new pods roll out,
and the result is shown to reviewers, so a visual view of "what is deployed, from which commit, and is it
healthy" has real value.

## Decision

Argo CD, installed once by `scripts/k3d-up.sh` from a pinned Helm chart (`deploy/argocd/bootstrap/`). After that,
everything comes from Git: one root Application per profile (`deploy/argocd/profiles/<profile>`) lists the
component Applications (`deploy/argocd/apps/<component>/`). The root app passes the Git revision to every child
(`main` on `sf-main`, the lane branch on a lane cluster). App-level sync waves order the install
(`docs/contracts/gitops.md` §3); Argo CD's health check for `Application` is enabled so waves wait for health.

## Alternatives considered

| Option | Why not |
|---|---|
| Flux (Kustomization + HelmRelease) | Solid and lighter, but no built-in UI to demo; ordering via `dependsOn` is fine, yet there is no direct equivalent of sync hooks for the migration Job |
| Plain `helm`/`kubectl apply` from scripts | No drift detection or self-heal; the "commit changes the cluster" loop would not exist |
| ApplicationSet (Git generator) for everything | Hides per-component settings (waves, sync options, multi-source values) behind templates; kept for later if the app count grows |

## Consequences

- Positive: drift is reverted (`selfHeal`), a commit is the only way to change the cluster, and the UI shows sync
  and health per component. PreSync hooks run the shop migration before rollout.
- Positive: lanes add an app directory and one line in their profile; no lane edits another lane's files.
- Negative / risks: Argo CD itself is not GitOps-managed yet (bootstrap only; self-management is optional in the
  `core` profile). Argo CD costs ~300MB RAM locally. Polling (60s) replaces webhooks, which cannot reach a laptop.
- When to revisit: if Argo CD RAM becomes the limit on the laptop profiles, or if self-management of Argo CD
  is needed for upgrades without `make up`.
