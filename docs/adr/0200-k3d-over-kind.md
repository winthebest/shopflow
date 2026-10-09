# 0200. Local Kubernetes on k3d (not kind or minikube)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

Every phase is verified on a laptop first (MacBook M4 Pro, 24GB, Docker VM 16GB), and at most two clusters run at
once (`sf-main` + one lane slot, `docs/contracts/environment.md`). The local cluster must: start with one command,
be cheap in RAM, expose one HTTPS entry point (the shop `Gateway`) on a fixed host port, offer a registry for a
fast dev loop, and have more than one node so scheduling and PDBs behave like a real cluster.

## Decision

Use k3d (k3s in Docker): 1 server + 1 agent, Traefik disabled, k3s servicelb kept to back the Envoy Gateway
`LoadBalancer` Service, the k3d load balancer mapping `127.0.0.1:<HTTPS_PORT>` to port 443, and a local registry
created with the cluster (`--registry-create`). `scripts/k3d-up.sh` takes the cluster name and derives every port
from the contract table; the k3s image is pinned by digest.

## Alternatives considered

| Option | Why not |
|---|---|
| kind | No LoadBalancer out of the box (needs MetalLB or cloud-provider-kind plus extra port plumbing); upstream kubelet + etcd per node costs more RAM than k3s with SQLite |
| minikube | One node per profile by default; driver/VM layer adds RAM and start time; multi-cluster on one Docker VM is clumsier |
| Docker Desktop Kubernetes | Single cluster, single node, version tied to Docker Desktop |

## Consequences

- Positive: measured on this machine, an idle 2-node cluster with Argo CD uses ~1.5GB (server ~970MiB,
  agent ~510MiB); `make up` / `make down` are one command each; two clusters fit side by side.
- Positive: kubeconfig contexts are merged without switching the current context, so parallel sessions never
  point each other's `kubectl` at the wrong cluster.
- Negative / risks: k3s bundles its own system images (CoreDNS, local-path, metrics-server, klipper-lb) by tag;
  they are fixed by the pinned k3s image digest rather than pinned individually. k3s differs from EKS in
  storage (local-path vs EBS) and load balancing (klipper vs NLB): those differences live in the `aws` overlays.
- When to revisit: if a component needs a kubelet/feature k3s does not ship, or if kind gains a built-in
  load balancer with lower RAM use than k3s.
