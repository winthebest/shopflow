# 0401. Kafka Connect image built in CI, not by Strimzi `spec.build`

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Kafka Connect needs the Debezium Postgres connector and the Iceberg sink on its plugin path. Strimzi can build
that image in the cluster (`KafkaConnect.spec.build`), but then the cluster needs push credentials for a registry,
runs a Kaniko/Buildah pod, and the image is neither signed nor scanned (red-team finding 8). Apache publishes no
binary of the Iceberg Kafka Connect runtime: it has to be compiled from source.

## Decision

`images/kafka-connect/Dockerfile` builds the image in GitHub Actions:

- `FROM quay.io/strimzi/kafka:1.2.0-kafka-4.3.1@sha256:…`;
- Debezium Postgres plugin from Maven Central, checked against a sha512 pinned in the Dockerfile;
- Iceberg Kafka Connect runtime compiled from the signed Apache source release (sha512 pinned), Kafka Connect
  modules only. Its main distribution already contains `iceberg-aws` with the S3, STS and Glue SDK modules
  needed on AWS (Phase 6), so no separate AWS bundle is added;
- the build stages run on the build platform (`--platform=$BUILDPLATFORM`): the jars are architecture neutral,
  so the arm64 image needs no emulation.

`data-ci.yml` builds both platforms on pull requests that touch the image, with the Actions cache (the Iceberg
stage only recompiles when its version/sha512 changes) and checks plugin discovery. `data-images.yml` (job `kafka-connect`) publishes
`ghcr.io/<owner>/shopflow-kafka-connect:sha-<short>` on merge to main with a clean build (no Actions cache in a
publishing workflow), actions pinned by SHA, no `id-token`. KafkaConnect uses `spec.image` pinned by digest.
Signing and SBOM come with Phase 8, like the app images.

## Alternatives considered

| Option | Why not |
|---|---|
| `KafkaConnect.spec.build` | Registry token inside the cluster, build pod, no signing/scan |
| Download a prebuilt Iceberg runtime zip (third-party mirrors) | No official binary; unverifiable provenance |
| Git tag tarball from GitHub instead of the Apache source release | Not the voted release artifact; no published checksum |

## Consequences

- Positive: the cluster only pulls; every artifact in the image is pinned by checksum; the image can be signed
  and scanned like the others; local and CI builds are identical (`make data-connect-image`).
- Measured: a clean local build (M4 Pro) takes ~5 minutes, most of it the Gradle build of the Iceberg runtime.
- Negative / risks: 1.5GB image (Strimzi base + AWS/GCP/Azure SDKs of the runtime); the first image only exists
  after the first merge, so the KafkaConnect manifest is pinned in a follow-up PR.
- When to revisit: Apache starts publishing the runtime as a binary, or the CI build exceeds 20 minutes.
