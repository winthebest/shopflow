# 0206. Root apps with overlays and session parameters

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

The same Git tree must drive a laptop cluster and an EKS cluster that is created and destroyed per session.
Differences between the two must live in `aws` overlays, plus a few values only known at session start: the
operator's IP for the NLB, the VPC ID, the account ID for role ARNs, the Postgres backup chain to restore from.
`make up` (sf-platform) and `make cloud-up` (sf-cloud) must create root apps the same way. Constraints found while
testing with kustomize 5.8.1:

- An overlay cannot include its own parent directory (`apps/<c>/aws` → `..` is a "cycle").
- A replacement inside a Component or a nested kustomization runs before the profile's top-level `patches`, where
  Argo CD puts the root app's patch. So it would copy the unpatched value (same lesson as the Git revision,
  `gitops.md` §4).
- Kustomize cannot express "if no backup chain, initdb, else recover".

## Decision

- **One mechanism**: `scripts/platform-root-apps.sh --overlay local|aws --revision R --profiles P --param k=v...`.
  - It validates the profile rules (exclusive `obs`/`obs-lite`, `data` needs one of them), deletes the root app
    of an excluded profile, and applies `root-<profile>` from `deploy/argocd/root-app.yaml`.
  - Kube context: `KUBE_CONTEXT`, else the current one in `KUBECONFIG`.
  - `--check` and `--print` work without a cluster.
- **Overlay trees**:
  - Local: `deploy/argocd/apps/<c>/` and `deploy/argocd/profiles/<p>/`.
  - AWS: sibling trees `deploy/argocd/apps-aws/<c>/` (`resources: [../../apps/<c>]` + patches swapping local
    value files/paths for aws) and `deploy/argocd/profiles-aws/<p>/`.
- **Session parameters**:
  - Declared in `deploy/argocd/profiles/_common/platform-params.yaml` (local-config ConfigMap; keys keep their dots).
  - The root app patches the ConfigMap with every `--param`.
  - An aws profile copies a key into `spec.sources.[chart=<chart>].helm.valuesObject.<path>` with a top-level
    replacement.
  - Parameters reach components **only as Helm values**. A component that needs one, or logic driven by one,
    becomes a chart (in-repo when needed: `deploy/charts/edge`, `deploy/charts/shop-db`).
- **Fail-closed**:
  - Unknown keys are refused by the script.
  - Keys listed in `shopflow.io/required-aws` must be non-empty on aws.
  - `scripts/platform-validate.sh` builds every aws profile with dummy values and fails if a profile reads an
    undeclared key or a replacement does not resolve.
- **Argo CD on AWS**: `deploy/argocd/bootstrap/values-aws.yaml` removes KSOPS, the age key mount and Kustomize
  exec plugins (secrets come from SSM through External Secrets). CI renders it and checks this.
- **AWS ordering** (`gitops.md` §1b):
  - External Secrets and the AWS LB Controller run in `profiles-aws/core` at wave -1, because waves only order the
    children of one root app.
  - Envoy Gateway runs at wave 0, with `loadBalancerClass: service.k8s.aws/nlb` so a Classic ELB is never created.
  - ExternalSecrets live in each component's aws overlay.

## Alternatives considered

| Option | Why not |
|---|---|
| `apps/<c>/aws/` overlays | Kustomize rejects an overlay that includes its parent directory |
| Replacement in each app's own kustomization | Runs before the root app's patch, so it copies the default value |
| Params into plain manifests via `kustomize.commonAnnotations` + a second replacement | Works, but a second mechanism for every lane to learn; Helm values cover all cases with one rule |
| ApplicationSet with cluster generators | Hides per-component waves and sync options; the session params would still need templating |
| Scripts `sed` the manifests before apply | Breaks GitOps: the cluster would run something that is not in Git |

## Consequences

- Positive:
  - `make up` and `cloud-up` share one tested path.
  - Every difference between local and AWS is a file in Git or a declared parameter.
  - A typo in a parameter name fails the command, not the deploy.
- Negative / risks:
  - AWS variants duplicate a little structure (one `apps-aws/<c>` per component).
  - Components needing params must be Helm charts.
  - Profiles list their param replacements inline (verbose but explicit).
- When to revisit: Argo CD gains native parameter passing between apps (e.g. ApplicationSet with values), or a
  third environment appears (then generalise the overlay list).
