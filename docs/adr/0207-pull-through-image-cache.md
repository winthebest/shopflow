# 0207. Shared pull-through image caches for local clusters

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

Phase 2 requires `make up` from a clean machine to reach all-Healthy in ≤ 10 minutes. The first cold measurement
of the `core` profile (2026-10-09, no cache) took about 56 minutes: cluster creation 26s, Argo CD install about
50 minutes, GitOps sync of the rest 304s. Almost all of it was image downloads at 85KB/s–750KB/s. One node needed
39 minutes for the 200MB Argo CD image, which the other node had already fetched. Every k3d node has its own
containerd store, so each image is downloaded once per node, and again after every `make down`. Up to two
clusters run at once (`sf-main` + one lane slot), multiplying the downloads, and anonymous Docker Hub pulls are
rate-limited.

## Decision

- Four k3d-managed registries in proxy mode, shared by every local cluster: `k3d-shopflow-cache-docker`
  (registry-1.docker.io), `-quay` (quay.io), `-ghcr` (ghcr.io), `-k8s` (registry.k8s.io).
  - Image: `registry:3.0.0` pinned by digest.
  - Host ports 127.0.0.1:5060–5063 (`docs/contracts/environment.md`).
  - Data in named volumes that survive `make down`; `make platform-cache-down` deletes them.
- `make up` creates or starts the caches. A new cluster gets `--registry-use` for each cache plus
  `scripts/k3d-registries.yaml` (containerd mirrors). `PULL_CACHE=0` opts out.
- Caches are anonymous. No credentials are configured, so they can never serve a private image.
- Argo CD's Redis image now comes from Docker Hub (same digest as the chart's ECR Public default), so it is
  cached too.

## Alternatives considered

| Option | Why not |
|---|---|
| `k3d image import` from the host Docker cache | Needs a pre-pull list kept in sync with every chart; still copies each image into every node |
| One shared registry, images pushed by a script | Same list problem; rewrites image references and breaks digest pins on upstream names |
| Spegel / in-cluster P2P cache | Shares between the nodes of one cluster only; lost on `make down` |
| Accept slow cold starts | The 10-minute target then depends on the network of the day, and Gate measurements are noisy |

## Consequences

- Positive: each image is downloaded from the internet at most once per machine. Repeated `make up` and the
  second node read from local disk. Digest pins are unchanged: a pull by digest is served byte-for-byte, and a
  digest mismatch fails as before.
- Negative / risks:
  - A cache that is down or stale makes containerd fall back to upstream: slower, but correct.
  - Disk grows with the images used (core ≈ 1.5GB), with a default 7-day TTL in the proxy.
  - Existing clusters only use the caches after they are recreated.
  - Registries other than the four (e.g. ECR Public) bypass the cache.
- Measured (core profile, same machine and network as the context): cold run with empty caches: _TBD_; warm
  run after `make down`: _TBD_.
- When to revisit: a profile pulls from another registry often enough to matter (add a cache and a port), or disk
  use becomes a problem (lower the TTL).
