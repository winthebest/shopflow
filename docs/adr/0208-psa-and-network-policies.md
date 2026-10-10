# 0208. Pod Security `restricted` and default-deny NetworkPolicies

- Status: Accepted (core namespaces; data/observability namespaces follow when their profiles are tested)
- Date: 2026-10-09
- Lane: sf-platform

## Context

The security baseline (plan: PSA `restricted`, NetworkPolicy default-deny, admin UIs through port-forward) must exist
locally before the first cloud session, so that the same manifests are proven on k3d. The allowed flows are a
contract (`docs/contracts/environment.md` "Network flows"); owners add rows when they introduce a flow. k3s enforces
NetworkPolicy with its embedded controller (kube-router); on EKS the VPC CNI network policy agent does.

## Decision

- **Namespaces as objects**: the `network-policies` app (core profile, wave -2) declares the core namespaces.
  - Components keep `CreateNamespace=true`. The objects carry `Prune=false,Delete=false` so Argo CD never deletes
    a namespace.
  - Pod Security labels exist before the first pod.
- **Pod Security**: `restricted` (enforce, warn, audit; version latest) for `shop`, `envoy-gateway-system`,
  `cert-manager`, `cnpg-system`, `argocd`.
  - Before enforcing, every running pod passed a server-side dry-run of the label.
  - A fresh `make up` with the labels in place had no PodSecurity rejection. This includes the chart hook Jobs
    (certgen, startupapicheck, migrations).
  - Profile namespaces get their own apps, listed by each profile's owner: `network-policies-obs` (obs, obs-lite)
    and `network-policies-data` (data). Levels come from a server-side dry-run of the rendered workloads:
    - `observability`: **privileged**. node-exporter needs hostNetwork, hostPID and hostPath (/proc, /sys); the
      OTel agent reads pod logs through hostPath. warn/audit run at baseline.
    - `kafka`: **baseline**, until the Strimzi operator uses its restricted pod security provider.
    - `lakehouse`: **baseline**, until Trino sets runAsNonRoot and a seccomp profile.
    - For both, warn/audit run at restricted to show the gap.
- **NetworkPolicy** for the workload namespaces. Per-pod least privilege in `shop` and the edge
  (`envoy-gateway-system`). In `observability`, `kafka` and `lakehouse`, pods of one namespace trust each other and
  every cross-namespace flow of the contract is listed. Prometheus may scrape any port in the cluster and the node
  ports (read-only scraper).
  The rules for `shop` and the edge:
  - `default-deny` (ingress + egress) plus DNS to kube-dns.
  - Then one policy per workload with exactly the contract's flows: edge → gateway → orders → payments/Postgres;
    consumers in kafka/lakehouse/airflow/flink/observability → Postgres; Prometheus → metrics; CNPG operator →
    instance manager.
  - API server egress lives in its own policies, labelled `shopflow.io/egress=apiserver`. They select only the
    pods that need it where those have stable labels (`shop`: CNPG instances and the worker's copy Jobs;
    `envoy-gateway-system`: controller and certgen). In `observability`, `kafka`, `lakehouse` and `airflow` they
    select the whole namespace, because operators, Connect, sidecars and copy Jobs all need the API.
    - A pod selector cannot match the host-network API server, so the rule names a port or an address:
      - k3s: TCP 6443 to any address. Egress is matched after the Service DNAT (`kubernetes.default:443` →
        node IP:6443), so 443 stays closed.
      - EKS (overlay `aws`, component `components/apiserver-aws`): TCP 443 to the VPC CIDR (control-plane ENIs) and
        to the `kubernetes` Service IP (172.20.0.1, in case the policy agent matches before DNAT). The CIDR must
        equal `vpc_cidr` in `infra/tofu/network`; `platform-validate` checks it.
  - Any other egress rule without a peer reaches every address, the internet included. `platform-validate` refuses
    it unless the policy names its reason in the annotation `shopflow.io/any-destination`:
    - Alertmanager's chat webhook (HTTPS);
    - Prometheus → node-exporter and kubelet on the host network;
    - on AWS, shop-db → S3 for the backup plugin (`aws/shop-db-backup.yaml`, with the Pod Identity agent).
- Platform controller namespaces (`argocd`, `cert-manager`, `cnpg-system`) are `restricted` but have no
  default-deny yet. They need webhook calls from the API server and egress to Git/Helm registries; Argo CD's chart
  ships its own policies. Revisit in Phase 8 with Kyverno.

## Alternatives considered

| Option | Why not |
|---|---|
| `managedNamespaceMetadata` per Application | Several apps share a namespace (`shop`: shop, shop-db); the labels would have one owner per app, not per namespace |
| Cilium/Calico with FQDN or L7 policies | A CNI swap on k3d and EKS for features the flow table does not need yet |
| Default-deny everywhere at once | Operator namespaces need host-network and internet flows that need their own testing; the workload namespaces carry the data |
| PSA `baseline` everywhere | Every core pod already passes `restricted`; keeping the higher level costs nothing |

## Consequences

- Positive:
  - A new pod in `shop` can only talk where the contract says. Measured on sf-platform: gateway→payments,
    gateway→Postgres, payments→Postgres, the internet and another namespace → gateway are blocked, while checkout
    still returns 201.
  - Privileged pods are rejected at admission.
- Negative / risks:
  - Every new flow needs a policy change (that is the point: add the row to the contract, then the policy).
  - Locally the API server rule is port-based (6443 to any address); on AWS it is address-based. The first
    version opened 443 by port, which let every selected pod reach the internet over HTTPS. The probe on sf-data
    (trino, Connect, an Airflow pod → 1.1.1.1:443 open) caught it, and `platform-validate` now rejects that shape.
  - Not yet measured: the post-DNAT match on k3s (probe rows "apiserver open" for the API clients) and on EKS
    (first AWS session).
  - Measured on k3s: a brand-new pod is not isolated for its first few seconds. The policy controller adds it to
    its rule sets shortly after start, so an immediate connection to the internet succeeded. Only stealing data in
    the first seconds of a pod's life gets past this. The EKS network policy agent has a strict mode that closes
    the window; revisit in Phase 6.
  - Proof: `scripts/platform-netpol-probe.sh` runs labelled probe pods (positive and negative, waiting for the
    policy sync) and exits non-zero on any mismatch.
  - Policies for namespaces of later profiles must land with or before those profiles.
- When to revisit: the flow table grows past what plain NetworkPolicy expresses well (FQDN egress, L7), or Phase 8
  adds Kyverno to enforce these baselines cluster-wide.
