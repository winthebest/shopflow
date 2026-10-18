# Runbook: agent tasks cost too much

| | |
|---|---|
| Alert | `AgentTaskCostBudgetBurn` (ticket) |
| SLO | [`slo/agent.yaml`](../../slo/agent.yaml) `task-cost`: 95% of `claude-opus-5-5` demo tasks at most $0.40 ([docs/slo/agent.md](../slo/agent.md)) |
| Rule | [`deploy/platform/slo/base/agent.prometheusrule.yaml`](../../deploy/platform/slo/base/agent.prometheusrule.yaml) (tests: `slo/tests/agent.test.yaml`) |
| Related | [agent-task-latency.md](agent-task-latency.md), [docs/cost.md](../cost.md) |

## What it means

Too many of the agent's demo tasks on `claude-opus-5-5` cost more than $0.40 at list prices. Two conditions raise the
ticket:
- **Over the last day:** more than 15% of tasks (3× the 5% budget), with at least 10 tasks.
- **Over the last 3 days:** more than 5% (1×), with at least 30 tasks.

$0.40 is a provisional target: the plan's alarm level per task, until the user sets the LLM budget. The cost is
computed by the agent from the API's `usage` at list prices. In demo mode, how a refusal before any output is
billed, and fallback credits, are not modelled ([ADR 0103](../adr/0103-llm-provider-choice.md)): a burst of fallbacks
can make the figure less exact.

## Check

```promql
# Spend: total over the last day, mean and p95 per task (Grafana: Shopflow / Agent)
sum(increase(agent_task_cost_usd_sum{agent_mode="demo",gen_ai_request_model="claude-opus-5-5"}[1d]))
sum(increase(agent_task_cost_usd_sum{agent_mode="demo",gen_ai_request_model="claude-opus-5-5"}[1d])) / sum(increase(agent_task_cost_usd_count{agent_mode="demo",gen_ai_request_model="claude-opus-5-5"}[1d]))
histogram_quantile(0.95, sum by (le) (increase(agent_task_cost_usd_bucket{agent_mode="demo",gen_ai_request_model="claude-opus-5-5"}[1d])))
# Where the tokens go: per LLM call by token type
histogram_quantile(0.95, sum by (le, gen_ai_token_type) (increase(gen_ai_client_token_usage_bucket{agent_mode="demo"}[1d])))
```

1. **More input tokens per call:** a larger prompt, more tool results in the history, or prompt caching no longer
   hitting (a system prompt or tool list that changes between calls breaks the cache).
2. **More calls per task:** malformed tool calls, context limit hits and fallbacks
   (`agent_tool_calls_malformed_total`, `agent_context_limit_hits_total`, `agent_fallbacks_total`).
3. **More output tokens:** a higher effort setting or longer thinking. Spans carry `agent.prompt.version` and
   `agent.policy.version`.

## Act

- **A prompt, policy or effort change:** take it to the lane that owns the agent (sf-app) with the numbers above. The
  change goes back through the eval with its cost measured.
- **The demo is used more than planned:** the per-task share is fine, but the total spend is not. That is the
  user's budget decision ([docs/cost.md](../cost.md)), not this SLO.
- **The target is wrong:** once the user sets the per-task budget, sf-app adds it as a bucket of
  `agent_task_cost_usd`. Then change the threshold in `slo/agent.yaml` and `docs/slo/agent.md`.
