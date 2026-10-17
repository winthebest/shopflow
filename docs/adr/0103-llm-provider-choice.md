# 0103. LLM providers: Claude Opus 5.5 through the Anthropic SDK, Qwen3 8B through Ollama, one contract

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-app

## Context

The operator agent (Phases 13–14) is evaluated on two models: a hosted one and a local one. A run is valid only if
every turn came from the model the run names: a server-side fallback that silently changes the model, or a local
prompt silently cut to fit the context, would make the numbers meaningless. No Anthropic API key exists yet and no LLM
spend is approved, so the code has to be proven without calling the API, against fakes that refuse what the API
refuses (a fake that accepts anything proves nothing).

## Decision

`agent/llm/`: one provider-neutral contract (`base.py`) and two adapters.

- **Claude** (`anthropic_provider.py`, SDK `anthropic==1.15.0`): `claude-opus-5-5`, adaptive thinking with an explicit
  effort (default `high`), strict tools with `tool_choice: auto`, cache breakpoint on the system prompt plus automatic
  caching of the history, streaming. The history is append-only: each response's content goes back unchanged.
  `Mode.EVAL` sends no `fallbacks`; a turn served by another model raises `InvalidRun`. `Mode.DEMO` sends
  `fallbacks: "default"` and records the model that served. Refusal, `max_tokens` and context exhaustion end the
  conversation and never yield tool calls.
- **Local** (`ollama_provider.py`, Ollama 0.32.0): `qwen3:8b` Q4_K_M pinned by digest (`500a1f06…`, checked before the
  first turn), `num_ctx` 16,384, the Qwen3 card's thinking-mode sampling (T 0.6, top_p 0.95, top_k 20, min_p 0) with a
  fixed seed, `truncate: false`. Measured 2026-10-18: without it a 4,515-token prompt at `num_ctx` 2,048 was answered
  from 1,026 tokens, silently; with it Ollama returns `exceed_context_size_error`, counted as a context-limit turn. A
  tool call that does not match its schema (or call markup Ollama could not parse) is blocked and counted. The KV cache
  type is a server setting; the brew service runs with OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0
  (`launchctl print gui/$(id -u)/homebrew.mxcl.ollama | grep OLLAMA_`).
- **Contract test** (`agent/tests/test_provider_contract.py`): the same round trip on both providers, against
  `fake_anthropic.py` and `fake_ollama.py`, which reject the cases in the table below; live runs against the real
  backends (the Claude API only with `ANTHROPIC_API_KEY`, otherwise skipped with the reason).
- **Metric names** for alerts, SLOs and the dashboard: `agent/instruments.py`.

Every API field, beta and refusal used here, and where it is documented (skill = the `claude-api` skill bundled with
Claude Code 2.1.293; SDK = `anthropic` 1.15.0):

| Feature | Source |
|---|---|
| `claude-opus-5-5`; $4 / $20 per MTok, cache write (5 min) $5, cache read $0.20 | skill `SKILL.md` § Current Models; `shared/model-migration.md` § Migrating to Claude Opus 5.5 (pricing) |
| `thinking: {type: "adaptive"}`; `disabled` and `budget_tokens` are a 400; sampling parameters are a 400 | skill § Thinking & Effort (table); `model-migration.md` § Opus 5.5 (error text); SDK `BetaThinkingConfigAdaptiveParam` |
| `output_config.effort`, set explicitly (the default is `medium`) | skill § Thinking & Effort; SDK `BetaOutputConfigParam.effort` |
| `strict: true` needs `additionalProperties: false` and `required`; `tool_choice` `any`/`tool` is a 400 | skill § Tool Use Patterns; `model-migration.md` § Opus 5.5 (forced `tool_choice`); SDK `BetaToolParam.strict` |
| Append-only history; thinking blocks tied to the conversation (an edited prefix is a 400) | skill § Claude Opus 5.5; `model-migration.md` § Opus 5.5, breaking change 3 and checklist |
| `stop_reason: "refusal"` before reading content; `stop_details.category`; `model_context_window_exceeded` | skill `python/claude-api/README.md` § Stop Reasons; `model-migration.md` § refusal; SDK `BetaRefusalStopDetails`, `BetaMessage.stop_reason` |
| `fallbacks: "default"` + beta `server-side-fallback-2026-07-01`; the array form uses `-2026-06-01`; a header with the other form is a 400 | `model-migration.md` § Migrating to Claude Opus 5, New API features (1), and § refusal (key semantics); SDK `Messages.stream(fallbacks: BetaFallbacksParam)` |
| `fallback` content block; after a mid-output fallback, echo only text before the last `fallback` block | `model-migration.md` § refusal (reading the response, echoing fallback turns back); SDK `BetaFallbackBlock`, stream accumulator |
| `usage.iterations` per attempt, each with its `model` | `model-migration.md` § refusal (billing); SDK `BetaUsage.iterations` |
| Breakpoint on the last system block covers tools + system; top-level automatic `cache_control`; at most 4 | skill `shared/prompt-caching.md` (render order, automatic vs explicit, API reference) |
| Streaming with `get_final_message()`, `max_tokens` 64,000 | skill § Defaults and Common Pitfalls (`max_tokens`) |
| Typed errors: 429 / 5xx / connection retryable, other 4xx not | skill README § Error Handling |
| No assistant prefill | skill § Common Pitfalls |
| Tool results immediately after their tool use, first in the content; error text "tool_use ids were found without tool_result blocks immediately after" | platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls (2026-10-18) |
| `eager_input_streaming` not set: with it the API stops validating tool input, which is what `strict` is for; the inputs are short | skill `shared/tool-use-concepts.md` § Eager input streaming (a deliberate exception to the skill's streaming default) |

## Alternatives considered

| Option | Why not |
|---|---|
| Fallback on in evals too | A refusal re-run on another model would count as the configured model's answer |
| Ollama's OpenAI-compatible endpoint | Its options cover less: no `truncate`, no `think` |
| Ollama's default context and silent truncation | 4,096 cuts the agent's prompts; a cut prompt still gets an answer, so the failure is invisible |
| Mocks that return canned responses for any request | Prove nothing about request shapes; the fakes here reject 14 documented invalid requests, and six injected adapter mistakes each fail the suite |

## Consequences

- Positive: an eval run either used exactly the named model, or it is marked invalid; context and malformed-call
  failures of the local model are counted apart from the guard's blocks. CI checks the request shapes without a key.
- Negative / risks: the fake encodes the docs as of 2026-10-18, not the live API. Where the docs give no error text the
  fake only names the field. The first run with a key must be `make eval-agent-test` with the key set, before any eval.
- Known gap: demo-mode cost prices each `usage.iterations` attempt at its model's list price; how a refusal before any
  output is billed, and fallback credits, are not modelled, and a model without a price here gives `cost_usd = None`.
  Eval mode has no fallbacks, so its cost is exact.
- When to revisit: the user picks other models or a spend cap; an SDK major version; the fallback betas go GA.
