# Runbook: the agent executed an unsafe action

| | |
|---|---|
| Alert | `AgentUnsafeActionExecuted` (page) |
| Rule | [`deploy/platform/slo/base/agent-alerts.prometheusrule.yaml`](../../deploy/platform/slo/base/agent-alerts.prometheusrule.yaml) (tests: `slo/tests/agent.test.yaml`) |
| Metric | `agent_unsafe_actions_total{agent_mode="demo"}` ([`agent/instruments.py`](../../agent/instruments.py)) |
| Related | [agent-task-latency.md](agent-task-latency.md), [agent-task-cost.md](agent-task-cost.md), [docs/slo/agent.md](../slo/agent.md), [ADR 0417](../adr/0417-catalog-scoped-safety-boundary.md) |

## What it means

The operator agent's demo deployment **executed** a change that its guard did not allow: there was no valid human
approval, or the change went beyond the bounds that were approved. The guard failed. A proposal that the guard
blocked is counted as a task outcome (`agent_tasks_total{agent_outcome="blocked"}`) and never pages.

Any single unsafe action pages. There is no SLO and no error budget for this: the budget is zero. The page names the
arm (`guarded` or `baseline`) and the action class. It stays up for 15 minutes after the action, and a second action
pages again.

Two cases page by design:
- **An action class outside the guard's allowlist.** The agent starts its counters at 0 only for allowlisted classes,
  so an unsafe action of another class first appears already at 1. The rule catches that new series too.
- **A counter that was already above 0 when Prometheus lost its data** (a new Prometheus, or one restarted without its
  volume). That counter pages once more, for 15 minutes. It is the same action as before: check the earlier page.

## Act first

1. **Stop the agent:** scale its Deployment to 0. The namespace and name are in its Argo CD application. Argo CD
   brings the replica back unless you also disable self-heal, so pause the application's automated sync first.
2. **Do not let the agent repair its own change**, and do not run its rollback.

## Check

1. **What it changed.** The task's trace in Tempo: the `invoke_agent` span and its `execute_tool` children give the
   tool, the outcome and the prompt and policy versions (`agent.prompt.version`, `agent.policy.version`). Spans
   carry no SQL or table contents, by design. In Trino, through the read-only catalog `lake_ro`, the affected
   table's `$snapshots` shows the commit with its operation and summary:
   ```sql
   SELECT committed_at, snapshot_id, parent_id, operation, summary
   FROM lake_ro.<schema>."<table>$snapshots" ORDER BY committed_at DESC LIMIT 10;
   ```
2. **Whether anything committed after it.** A later snapshot from another writer means a rollback would discard
   that writer's data too.

## Repair

- **Roll back the table to the snapshot before the change only if** that change is still the newest snapshot and the
  table has no writer outside the agent. The lake's identities and what each may run are in ADR 0417.
- **Otherwise restore** from a copy taken before the change, if there is one, or rebuild the affected rows from
  their source. Agree on this with the table's owner.
- `expire_snapshots` or `remove_orphan_files` run beyond what was approved can delete the snapshots a rollback would
  need. Find the earliest snapshot that is still there before you plan anything.

## Afterwards

- Keep the agent stopped until the guard is fixed. The lane that owns the agent (sf-app) adds the case to the eval
  dataset, so the guard is tested against it from then on.
- Write a postmortem from [TEMPLATE.md](../postmortems/TEMPLATE.md).
