# SLOs and alerts: the operator agent

Owner: sf-sre. Metric names and labels: [`agent/instruments.py`](../../agent/instruments.py) (sf-app). Spec:
[`slo/agent.yaml`](../../slo/agent.yaml) → generated rules
[`deploy/platform/slo/base/agent-slo.prometheusrule.yaml`](../../deploy/platform/slo/base/agent-slo.prometheusrule.yaml)
(Sloth, [ADR 0300](../adr/0300-slo-tooling-sloth.md)). Alerts:
[`agent-alerts.prometheusrule.yaml`](../../deploy/platform/slo/base/agent-alerts.prometheusrule.yaml). Tests:
[`slo/tests/agent.test.yaml`](../../slo/tests/agent.test.yaml). Dashboard: Shopflow / Agent. Runbooks:
[agent-unsafe-action](../runbooks/agent-unsafe-action.md), [agent-task-latency](../runbooks/agent-task-latency.md),
[agent-task-cost](../runbooks/agent-task-cost.md).

## What is measured where

| Mode | Where it runs | Where its telemetry goes | What judges it |
|---|---|---|---|
| `eval` | the host, against the lab stack | the lab Collector | the eval scorer's report: Safety/Utility, false-block rate with its Wilson interval, Unsafe Action Rate with its Clopper-Pearson bound |
| `demo` | k3d | the gateway Collector → Prometheus (OTLP, [ADR 0301](../adr/0301-otel-collector-single-pipeline.md)) | the page and the SLOs below |

Every rule selects `agent_mode="demo"`. The false-block rate is not a Prometheus series: telling a wrong block from a
right one needs the task's ground truth, which only the eval has.

## Unsafe actions: a page, not an SLO

`AgentUnsafeActionExecuted` pages on **any** unsafe action that the agent executed, that is a change without a valid
approval or beyond the approved bounds. Its budget is zero, and a burn rate over a zero budget divides by zero. A
proposal that the guard blocked is a task outcome (`agent_outcome="blocked"`) and does not page.

The agent starts its counters at 0 only for the guard's allowlisted action classes. An unsafe action of any other
class first appears already at 1, where `increase()` sees no change. So the rule also pages on a series that is new
within its 15-minute window and already above 0.

## SLIs

| SLO | Good task | Objective | Error budget (28 days) |
|---|---|---|---|
| `task-latency` | request to final decision in **120 s** or less, approval waits excluded | **95%** | 5% of tasks |
| `task-cost` | **$0.40** or less at list prices (provisional) | **95%** | 5% of tasks |

- **Events, not time.** Each task counts once, from the histograms `agent_task_duration_seconds` and
  `agent_task_cost_usd`. Bad tasks are all tasks minus those in the target's bucket. A target must be an explicit
  bucket boundary. The `le` matcher also accepts the `120.0` spelling of the 120 s bucket.
- **Only `gen_ai_request_model="claude-opus-5-5"`.** This is the configured model, so a turn served by a fallback
  still counts under it. The local `qwen3:8b` takes far longer per task and costs nothing. It gets a latency target
  (300 or 600 s, both buckets) once an eval has measured it.
- **$0.40** is the plan's per-task alarm level. When the user sets the LLM budget, sf-app adds it as a bucket and the
  target moves to it.
- **No NaN.** Most windows hold no task, and the denominator `… > 0` makes such a window give no sample rather than
  0/0, which would poison the 28-day average.

## Alerts: tickets with a minimum task count

The demo runs a few dozen tasks a day at most. At that volume Sloth's page windows (5m and 1h) would turn one slow or
expensive task into a page, and its ticket windows would raise a ticket on a handful of tasks. So `slo/agent.yaml`
disables both of Sloth's alerts. The tickets in `agent-alerts.prometheusrule.yaml` read Sloth's 1-day and 3-day SLI rules
instead:

| Ticket | Over the last day | Over the last 3 days |
|---|---|---|
| `AgentTaskLatencyBudgetBurn`, `AgentTaskCostBudgetBurn` | more than 15% bad (3× the budget) **and** at least 10 tasks | more than 5% bad (1×) **and** at least 30 tasks |

At 48 tasks a day, 20% slow raises the 1-day ticket, and a steady 8% raises the 3-day ticket once 30 tasks are in. At
five tasks a day, one slow task (20%) raises nothing. These cases are in the unit tests.

## Gate check

`scripts/sre-gate-check.sh` skips the agent SLOs while no demo agent runs. It also skips them when no
`claude-opus-5-5` demo task finished in the last day. Otherwise it expects their 1-day SLI samples: a 5-minute window
is usually empty at this volume.

## Error budget policy

- **Unsafe action:** stop the agent at once. It stays stopped until the guard is fixed and the case is in the eval
  dataset ([runbook](../runbooks/agent-unsafe-action.md)).
- **Latency or cost budget spent:** no new prompt, policy or effort version for the demo until the cause is found.
  Changes that cut latency or cost go first, through the eval like any other version.
