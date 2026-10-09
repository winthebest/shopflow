# shopflow

A data platform built and operated end to end: a small shop (3 services) → Postgres → CDC (Debezium, Kafka) →
Apache Iceberg lakehouse → dbt + Airflow → Trino / BI, running locally on k3d with one command and on
ephemeral AWS EKS with cost guardrails, with SLOs, chaos game days, postmortems and restore drills.

> Status: under construction. This README will carry the architecture diagram, demo video, and measured
> numbers when the platform phases are complete.

- How the work is organized: `docs/contracts/` (service contract, ownership, environment)
- Decisions: `docs/adr/`
