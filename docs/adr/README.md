# Architecture Decision Records

One page per decision, using `0000-template.md`. File name: `NNNN-kebab-case-title.md`.

Parallel lanes draw numbers from their own range to avoid collisions. Lanes do not edit the index below; the
orchestrator adds rows when merging a PR that contains ADRs.

| Range | Lane |
|---|---|
| 0001–0099 | orchestrator |
| 0100–0199 | sf-app |
| 0200–0299 | sf-platform |
| 0300–0399 | sf-sre |
| 0400–0499 | sf-data |
| 0500–0599 | sf-cloud |

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0100](0100-app-language-python-fastapi.md) | Shop services in Python 3.12 + FastAPI (uv workspace) | Accepted |
| [0101](0101-data-contracts-in-ci.md) | Data contracts for CDC source tables, checked in CI against the migrated schema | Accepted |
| [0200](0200-k3d-over-kind.md) | Local Kubernetes on k3d (not kind or minikube) | Accepted |
| [0201](0201-argocd-over-flux.md) | GitOps with Argo CD app-of-apps (not Flux) | Accepted |
| [0202](0202-gateway-api-envoy-gateway.md) | Edge: Gateway API with Envoy Gateway, TLS from cert-manager | Accepted |
| [0203](0203-cnpg-over-rds.md) | Postgres with CloudNativePG on both k3d and EKS (not RDS) | Accepted |
| [0204](0204-sops-ksops-local-secrets.md) | Local secrets with SOPS + age, decrypted by KSOPS in Argo CD | Accepted |
| [0205](0205-admin-ui-port-forward-only.md) | Admin UIs only through `kubectl port-forward` | Accepted |
| [0206](0206-root-apps-overlays-and-params.md) | Root apps with overlays and session parameters | Accepted |
| [0207](0207-pull-through-image-cache.md) | Shared pull-through image caches for local clusters | Accepted |
| [0208](0208-psa-and-network-policies.md) | Pod Security `restricted` and default-deny NetworkPolicies | Accepted |
| [0300](0300-slo-tooling-sloth.md) | Generate SLO rules with Sloth (Pyrra as fallback) | Accepted |
| [0301](0301-otel-collector-single-pipeline.md) | One OpenTelemetry Collector pipeline; SLIs from span metrics | Accepted |
| [0302](0302-observability-backends-chart-sources.md) | Observability backends, chart sources and pinning | Accepted |
| [0400](0400-kafka-kraft-debezium-iceberg-versions.md) | Kafka 4.3 on Strimzi 1.2 (KRaft), Debezium 3.7, Iceberg 1.12 sink | Accepted |
| [0401](0401-connect-image-built-in-ci.md) | Kafka Connect image built in CI, not by Strimzi `spec.build` | Accepted |
| [0402](0402-freshness-exporter-custom.md) | Custom freshness exporter for lakehouse tables | Accepted |
| [0403](0403-strimzi-over-msk.md) | Kafka on Strimzi in both environments (not Amazon MSK) | Accepted |
| [0404](0404-kafka-tls-scram-acl.md) | Kafka clients authenticate with SCRAM-SHA-512 over TLS, one KafkaUser per role | Accepted |
| [0405](0405-json-converter-no-registry.md) | Schemaless JSON on Kafka, the bronze DDL is the schema (no Schema Registry) | Accepted |
| [0406](0406-append-only-bronze-cdc-epoch.md) | Append-only bronze with a CDC epoch, and one Iceberg control topic per epoch | Accepted |
| [0407](0407-iceberg-format-v2.md) | Apache Iceberg tables, format version 2, created by DDL | Accepted |
| [0408](0408-iceberg-rest-catalog-polaris.md) | Iceberg REST catalog: Apache Polaris (not Lakekeeper) | Accepted |
| [0409](0409-seaweedfs-over-minio.md) | Local object storage: SeaweedFS (not MinIO) | Accepted |
| [0410](0410-trino-catalogs-per-identity.md) | Trino as the query engine, with the safety boundary at the catalog | Accepted |
| [0411](0411-trino-https-internal-ca.md) | Trino HTTPS with a certificate from the internal cert-manager CA | Accepted |
| [0412](0412-dbt-core-over-sqlmesh.md) | dbt Core with dbt-trino for bronze → silver → gold | Accepted |
| [0413](0413-silver-latest-epoch-full-rebuild.md) | Silver = current state of the latest completed CDC epoch, rebuilt in full | Accepted |
| [0414](0414-airflow3-local-executor-cosmos.md) | Airflow 3 with LocalExecutor, dbt through Cosmos, DAGs baked into one image | Accepted |
| [0412](0412-dbt-core-over-sqlmesh.md) | dbt Core with dbt-trino for bronze → silver → gold | Accepted |
| [0413](0413-silver-latest-epoch-full-rebuild.md) | Silver = current state of the latest completed CDC epoch, rebuilt in full | Accepted |
| [0500](0500-opentofu-over-terraform.md) | OpenTofu instead of Terraform for the AWS layers | Accepted |
| [0501](0501-ephemeral-env-with-lease.md) | Ephemeral AWS sessions bounded by a lease and two reapers | Accepted |
| [0502](0502-layer2-state-aws-only.md) | Layer 2 state holds only AWS resources | Accepted |
| [0503](0503-no-nat-public-subnets.md) | Public subnets, no NAT gateway | Accepted |
| [0504](0504-single-az-node-group.md) | Node group in a single AZ | Accepted |
| [0505](0505-graviton-spot-nodes.md) | Graviton spot instances for nodes | Accepted |
| [0506](0506-glue-catalog-on-aws.md) | AWS Glue as the Iceberg catalog on AWS | Accepted |
| [0507](0507-ghcr-over-ecr.md) | Images stay in GHCR; no ECR | Accepted |
| [0508](0508-ssm-over-secrets-manager.md) | SSM Parameter Store (via External Secrets) instead of Secrets Manager | Accepted |
| [0509](0509-ci-cannot-apply-oidc-scoping.md) | CI cannot apply infrastructure; OIDC roles are scoped to one workflow | Accepted |
| [0510](0510-cost-guardrails-exclude-credits.md) | Cost guardrails that exclude credits | Accepted |
| [0511](0511-cnpg-backup-chain-fail-closed.md) | Postgres backup chain with a pointer, failing closed | Accepted |
| [0512](0512-security-baseline-before-cloud.md) | Security baseline from the first cloud session | Accepted |
