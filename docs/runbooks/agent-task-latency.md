# Runbook: agent tasks are too slow

| | |
|---|---|
| Alert | `AgentTaskLatencyBudgetBurn` (ticket) |
| SLO | [`slo/agent.yaml`](../../slo/agent.yaml) `task-latency`: 95% of `claude-opus-5-5` demo tasks under 120 s ([docs/slo/agent.md](../slo/agent.md)) |
| Rule | [`deploy/platform/slo/base/agent-alerts.prometheusrule.yaml`](../../deploy/platform/slo/base/agent-alerts.prometheusrule.yaml) (tests: `slo/tests/agent.test.yaml`) |
| Related | [agent-task-cost.md](agent-task-cost.md), [agent-unsafe-action.md](agent-unsafe-action.md) |

## What it means

Too many of the agent's demo tasks on `claude-opus-5-5` took more than 120 s from request to final decision. Time
spent waiting for a human approval is not counted. Two conditions raise the ticket:
- **Over the last day:** more than 15% of tasks were slow (3× the 5% budget), with at least 10 tasks.
- **Over the last 3 days:** more than 5% were slow (1×), with at least 30 tasks.

There is no page: at a few dozen tasks a day, a short window would turn a single slow task into one. Only tasks whose
configured model is `claude-opus-5-5` count. A turn served by a fallback model still counts under it, and tasks on
the local `qwen3:8b` are not part of this SLO.

## Check

```promql
# Task durations by outcome, p50 and p95 over the last day (Grafana: Shopflow / Agent)
histogram_quantile(0.95, sum by (le, agent_outcome) (increase(agent_task_duration_seconds_bucket{agent_mode="demo",gen_ai_request_model="claude-opus-5-5"}[1d])))
# Is the model call itself slow? p95 of one LLM call, and the share served by a fallback
histogram_quantile(0.95, sum by (le, gen_ai_response_model) (rate(gen_ai_client_operation_duration_seconds_bucket{agent_mode="demo"}[1h])))
sum(increase(agent_fallbacks_total{agent_mode="demo"}[1d]))
# More turns per task: context limit hits and malformed tool calls cost extra turns
sum(increase(agent_context_limit_hits_total{agent_mode="demo"}[1d]))
sum(increase(agent_tool_calls_malformed_total{agent_mode="demo"}[1d]))
```

1. **The model call is slow** (high `gen_ai_client_operation_duration_seconds`), from API latency, larger prompts or
   higher effort. Compare `gen_ai_client_token_usage` by token type with the previous days.
2. **More turns per task:** malformed tool calls and context limit hits each add turns. A new prompt or policy version
   is the usual cause; spans carry `agent.prompt.version` and `agent.policy.version`.
3. **Slow tools:** the `execute_tool` spans of slow tasks in Tempo. A Trino query that waits on the lake is a lake
   problem, not an agent one.
4. **A single task that never ends** shows as one 600 s bucket. Look at its trace.

## Act

- **A prompt or policy change slowed the agent:** take it to the lane that owns the agent (sf-app), with the versions
  and the numbers above. The change goes back through the eval before a new version ships.
- **API latency:** nothing to fix locally. Note the time window on the ticket and let the window age out.
- **The target itself is wrong:** for example the agent now does more work per task. Change it in `slo/agent.yaml`
  and `docs/slo/agent.md`. A new target must be a bucket boundary of `agent_task_duration_seconds`.
