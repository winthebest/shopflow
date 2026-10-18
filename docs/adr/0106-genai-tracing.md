# 0106. Agent telemetry: GenAI spans and agent metrics from one wrapper, content-free by allowlist

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-app

## Context

Every step of the agent has to be traceable (which prompt and policy version, which model actually served, tokens,
cost, latency), and its alerts and SLOs need metrics with fixed names (`agent/instruments.py`, agreed with sf-sre).
Prompts, tool inputs, tool results, SQL and error messages carry data from the lakehouse and must not leave the
process. The OpenTelemetry GenAI conventions are still moving: in opentelemetry-semantic-conventions 0.66b0 every
`gen_ai.*` attribute is marked as moved to the semantic-conventions-genai repository, which has no release yet.

## Decision

`agent/tracing.py` is the only place that names telemetry.

- **Spans**: `invoke_agent <agent>` per task (prompt and policy versions, requested model, outcome, cost), a `chat
  <model>` child per LLM call (provider, requested and served model, finish reason, tokens including cache reads and
  writes, cost, fallback, malformed tool calls, context-limit hit), and `execute_tool <name>` per tool call (name and
  call id only). Attribute names are those of semantic-conventions 0.66b0; a test checks each against the package,
  so an upgrade that renames one fails CI. Token counts are as the provider reports them: Claude's
  `input_tokens` excludes cache reads and writes, which have their own attributes.
- **Content-free, twice**: spans are only ever given attributes from `ALLOWED_ATTRIBUTES`, and errors become their
  exception class (`error.type`); `ContentFreeExporter` then drops any other attribute, every event and the status
  description at export, so a mistake upstream (or a library's instrumentation) cannot leak content.
- **Metrics**: every instrument of `agent/instruments.py`, histograms with their explicit buckets; `prime()` records
  0 on each counter for every label set at start-up, so the first unsafe action is an increase.
- **Export**: OTLP/HTTP, by default to the lab Collector (127.0.0.1:24318) for evals, or `OTEL_EXPORTER_OTLP_ENDPOINT`
  (the gateway Collector on k3d for the demo). OpenTelemetry SDK and exporter 1.45.0, the newest release at least two
  weeks old.

## Alternatives considered

| Option | Why not |
|---|---|
| A framework's built-in instrumentation (Pydantic AI, `include_content=False`) | Its attribute set follows the framework's version, and the framework itself is still under evaluation (ADR 0104); one wrapper keeps names stable for sf-sre's rules |
| Filter content in the Collector only | Content would already have left the process; the Collector filter stays as a second line, not the only one |
| Record exceptions with `record_exception` | Puts the message (often a table name or SQL) into a span event |
| Wait for a semantic-conventions-genai release | No date; the names are unchanged, and the pin plus test catch a rename |

## Consequences

- Positive: names fixed in one module and tested; no path for content into exported spans; the metrics sf-sre's rules
  query exist from the first run.
- Negative / risks: GenAI names may change once the new repository releases; the test points at the place to edit.
  Durations of `chat` spans come from the provider's measured turn time, ending when the span is recorded.
- Verified end to end on 2026-10-18 (11:51 UTC+7) against the lab Collector: one traced task's spans and metrics
  arrived, and a marker placed in the prompt, the tool input and the tool result appeared in neither file
  (`agent/tests/test_lab_collector.py`, `make eval-agent-test-lab`, marker `lab`).
- When to revisit: semantic-conventions-genai publishes a release; the agent loop moves into a framework.
