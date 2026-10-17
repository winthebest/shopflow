"""Names of the agent's OpenTelemetry metrics: the contract for its alerts, SLOs and dashboard (owned by sf-sre).

Where they go. In an eval the agent runs on the host against the lab stack and exports OTLP to the lab Collector
(24317/24318, docs/contracts/environment.md); kube-prometheus-stack never sees eval runs, whose results are the
scorer's report. On k3d the agent is a demo deployment that exports OTLP to the gateway Collector, which writes to
Prometheus's OTLP receiver like the shop services (ADR 0301). Alerts and SLOs are for `agent.mode="demo"`.

Prometheus names. The receiver uses `translationStrategy: UnderscoreEscapingWithSuffixes`
(deploy/platform/kube-prometheus-stack): dots become underscores, a unit in braces adds nothing, `s` adds `_seconds`,
a counter adds `_total`, and a histogram is queried as `_bucket`, `_sum` and `_count`. The `prometheus` field is that
result. The two `gen_ai.client.*` histograms are the OpenTelemetry GenAI semantic conventions' own metrics.

Start-up. The agent records `add(0)` on every counter for every label set it can emit (each `agent.mode`, `agent.arm`
and, for unsafe actions, each action class of the guard's allowlist), so the first real increment shows in increase().

Attributes stay low-cardinality: `agent.mode` (eval | demo), `agent.arm` (guarded | baseline), `agent.outcome`,
`agent.action_class`, `gen_ai.provider.name` (anthropic | ollama), `gen_ai.request.model`, `gen_ai.response.model`
(the model that served), `gen_ai.token.type` (input | output). Never a prompt, SQL, table name or error text.

Not a metric: false blocks. Telling a false block from a right one needs a task's ground truth, which only the eval
has; the false-block rate is an eval result (with its Wilson interval), not a Prometheus series.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Instrument:
    name: str
    kind: Literal["counter", "histogram"]
    unit: str
    prometheus: str
    description: str
    buckets: tuple[float, ...] = ()  # histograms: explicit boundaries; an SLO target must be one of them


UNSAFE_ACTIONS = Instrument(
    "agent.unsafe_actions",
    "counter",
    "{action}",
    "agent_unsafe_actions_total",
    "Changes EXECUTED outside what the guard allowed: no valid approval, or an effect beyond the approved bounds. "
    'A proposal the guard blocked is not counted here (it is agent_tasks_total{agent_outcome="blocked"}). Any '
    'increase with agent_mode="demo" pages. Attributes: agent.mode, agent.arm, agent.action_class.',
)
TASKS = Instrument(
    "agent.tasks",
    "counter",
    "{task}",
    "agent_tasks_total",
    "Finished tasks by outcome: executed, approved (executed after a human approval), blocked, clarified, failed, "
    "invalid (an eval turn served by another model). Attributes: agent.mode, agent.arm, agent.outcome.",
)
TASK_DURATION = Instrument(
    "agent.task.duration",
    "histogram",
    "s",
    "agent_task_duration_seconds",
    "Wall-clock time of a task, request to final decision, excluding time waiting for a human approval. "
    "Attributes: agent.mode, agent.arm, agent.outcome.",
    buckets=(1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600),
)
TASK_COST = Instrument(
    "agent.task.cost.usd",
    "histogram",
    "{USD}",
    "agent_task_cost_usd",
    "LLM spend of one task at list prices; _sum is the total spend. Attributes: agent.mode, agent.arm, "
    "gen_ai.request.model. 0.40 is the plan's per-task alarm level; the budget the user sets is added as a bucket.",
    buckets=(0.05, 0.10, 0.25, 0.40, 0.55, 0.75, 1.0, 2.0),
)
TOKEN_USAGE = Instrument(
    "gen_ai.client.token.usage",
    "histogram",
    "{token}",
    "gen_ai_client_token_usage",
    "Tokens per LLM call (GenAI semantic conventions). Attributes: agent.mode, gen_ai.token.type, "
    "gen_ai.provider.name, gen_ai.request.model, gen_ai.response.model.",
    buckets=(256, 1024, 4096, 16384, 65536, 262144, 1048576),
)
OPERATION_DURATION = Instrument(
    "gen_ai.client.operation.duration",
    "histogram",
    "s",
    "gen_ai_client_operation_duration_seconds",
    "Duration of one LLM call (GenAI semantic conventions). Attributes as for token usage, plus error.type.",
    buckets=(0.5, 1, 2.5, 5, 10, 20, 40, 80, 160, 320),
)
MALFORMED_TOOL_CALLS = Instrument(
    "agent.tool_calls.malformed",
    "counter",
    "{call}",
    "agent_tool_calls_malformed_total",
    "Tool calls blocked because they did not match the tool's schema; reported apart from the guard's blocks. "
    "Attributes: agent.mode, gen_ai.provider.name, gen_ai.request.model.",
)
CONTEXT_LIMIT_HITS = Instrument(
    "agent.context_limit_hits",
    "counter",
    "{turn}",
    "agent_context_limit_hits_total",
    "Turns that reached the model's context size (prompt refused, or context filled while generating). "
    "Attributes: agent.mode, gen_ai.provider.name, gen_ai.request.model.",
)
FALLBACKS = Instrument(
    "agent.fallbacks",
    "counter",
    "{turn}",
    "agent_fallbacks_total",
    "Turns served by a fallback model after a refusal; only in demo mode, since in an eval such a turn is invalid. "
    "Attributes: agent.mode, gen_ai.request.model, gen_ai.response.model.",
)

ALL = (
    UNSAFE_ACTIONS,
    TASKS,
    TASK_DURATION,
    TASK_COST,
    TOKEN_USAGE,
    OPERATION_DURATION,
    MALFORMED_TOOL_CALLS,
    CONTEXT_LIMIT_HITS,
    FALLBACKS,
)
