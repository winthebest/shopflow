# 0100. Shop services in Python 3.12 + FastAPI (uv workspace)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-app

## Context

The shop (gateway, orders, payments) is only a workload that produces realistic traffic, traces and CDC rows for
the data platform. It has a 32h timebox and no UI. It must be easy to read for a reviewer, emit OpenTelemetry
traces with route templates, talk to Postgres asynchronously, own its schema with migrations (CDC and dbt depend
on it), and build into small multi-arch images that run as non-root with a read-only root filesystem. The rest
of the project (dbt, Airflow, lab, agent) is Python too.

## Decision

Python 3.12 + FastAPI, with SQLAlchemy 2 (async) + asyncpg for Postgres, Alembic as the only owner of the schema,
httpx for service calls, and pydantic-settings for env config. The three services are members of one uv
workspace with a small shared package (`services/common`: JSON logs with trace ids, OTel setup, server runner,
checkout schema); each service still builds its own image from a locked `uv.lock`.

## Alternatives considered

| Option | Why not |
|---|---|
| Go (net/http or chi) | Faster and smaller images, but a second language in the repo for a workload that is not the focus; the data and AI layers are Python anyway |
| Mix (Go gateway, Python orders) | Two toolchains, two CI paths, two OTel setups for no measurable gain at 20–50 RPS |
| Django + Django ORM migrations | Heavier, sync-first; schema ownership tied to an ORM migration format that dbt/CDC readers don't need |
| One repo-wide virtualenv without a workspace | Cannot build per-service images that contain only their own dependencies |

## Consequences

- Positive: one language across app, data and AI code; FastAPI + OTel auto-instrumentation gives `http.route`
  templates and server spans for the SLIs with little code; `alembic check` in tests keeps models and migrations
  in sync (tables, columns, types, keys, indexes, server defaults; not CHECK constraints or triggers); uv makes locked installs and Docker layer caching simple.
- Negative / risks: Python per-request overhead and memory (~45–70MB per service measured on compose) are higher than Go; the GIL
  limits one process to one core, so scaling is by replicas, not threads. Async SQLAlchemy needs care (no lazy
  loading, explicit transactions).
- When to revisit: if the baseline in `docs/perf-baseline.md` shows the services, not Postgres or the payments
  mock, are the bottleneck below the load the platform phases need (for example p99 > 300ms at 50 RPS on compose),
  or if image size/cold start becomes a problem for KEDA scale-from-zero in Phase 7.
