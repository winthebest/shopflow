# 0104. Agent framework: our own tool loop on `agent/llm`, not Pydantic AI

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-app

## Context

The operator agent (Phase 14) needs a tool loop: send the conversation, run the tools the model calls, send the
results back. The plan admits a framework only if it works with Claude Opus 5.5 without forcing `tool_choice`, keeps
the history append-only and emits OpenTelemetry without content; otherwise the loop is written in house. The loop
must also keep the guarantees of the provider layer (ADR 0103): an eval run is invalid when another model served a
turn, demo mode handles the server-side fallback, and the local model never answers from a silently cut prompt.

## Measurement

`agent/spikes/pydantic_ai_fit.py` (Pydantic AI 2.57.0, `anthropic` 1.15.0): offline against the fake Messages API
(`agent/tests/fake_anthropic.py`, which rejects what the API documents as invalid and accepts what it does not count
as an edit), and live against the local `qwen3:8b` through Pydantic AI's Ollama model (2026-10-18 11:48–11:49 UTC+7,
model unloaded after). No Claude API call.

| Check | Result |
|---|---|
| Tool loop: adaptive thinking, explicit effort, `strict` tools, `tool_choice: auto` | yes |
| History append-only as the API compares it (no stale thinking block) | yes |
| A rejected thinking block (documented 400) | dropped and the request retried, silently unless warnings are errors |
| Opt-out: `block_binding.prefix_mismatch_behavior: "error"` | yes: the run fails with the 400 |
| Demo mode: a response with a `fallback` block (`fallbacks` sent through `extra_body`) | **crashes** (`AssertionError: unexpected item type BetaFallbackBlock`) |
| Eval: a turn served by another model is noticed | **no**: the run completes; `ModelResponse.model_name` would need our own check |
| OTel GenAI spans with `include_content=False` carry no content | yes |
| Ollama (OpenAI-compatible route): tool loop on qwen3:8b | yes (13.4 s), but the model loads with context **4096**: the route cannot set `num_ctx`, `truncate` or `think` |
| Ollama: a prompt over the context fails instead of being cut | **no**: about 13,500 tokens at context 4096 were answered ("OK") from a cut prompt |

## Decision

Decisive: through Pydantic AI's Ollama route the local model answered from a silently cut prompt (about 13,500
tokens at context 4096). That is the failure ADR 0103 rules out for every eval run, and the route has no option
to prevent it. The demo-mode crash on a `fallback` block and the missing served-model check point the same way.

The agent's loop is written in house on top of `agent/llm` (ADR 0103): providers that already enforce the served
model, handle the fallback, keep the history append-only and refuse an over-long local prompt, and `agent/tracing.py`
(ADR 0106) for telemetry. The loop itself (send, run the requested tools, return the results, stop on a terminal turn)
is small; what a framework would add is what the provider layer already does, or does not do the way the eval needs.

## Alternatives considered

| Option | Why not |
|---|---|
| Pydantic AI for Claude, own loop for Ollama | Two loops to keep equivalent for a fair comparison of the arms; Claude's demo mode still crashes on a fallback and eval mode still needs a served-model check |
| Pydantic AI with patches (custom Ollama model, fallback block handling, served-model check, retry off) | Most of the framework replaced by our code, with a framework version to track on top |
| Claude Agent SDK | A coding agent with its own built-in tools; the operator agent must have only its typed tools (Phase 14) |

## Consequences

- Positive: one loop for both models and both arms; every guarantee measured in ADR 0103 holds; telemetry names stay
  ours.
- Negative / risks: we maintain the loop (retries, tool dispatch, history) ourselves; each provider feature a
  framework would get for free has to be added and tested here.
- When to revisit: Pydantic AI handles `fallback` blocks and exposes a served-model hook, and its Ollama model gains
  the native options (`num_ctx`, `truncate`); re-run the spike.
