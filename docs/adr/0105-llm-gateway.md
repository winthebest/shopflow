# 0105. No LLM gateway: the agent calls the Claude API directly; budget in the app, spend limit per key

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-app

## Context

A gateway in front of the Claude API (LiteLLM) would add per-key budgets and central spend tracking. The plan admits
one only if its pass-through keeps every Claude Opus 5.5 feature the agent relies on (ADR 0103): adaptive thinking
with an explicit effort, strict tools, cache breakpoints, the `fallbacks` request field with its beta header, and in
the response the served model, `fallback` blocks, `usage.iterations` and `stop_details`. LiteLLM 1.82.7 and 1.82.8
were compromised on PyPI in 2026-03, so only a pinned release from 1.83.0 on is acceptable.

## Measurement

`agent/spikes/litellm_gateway_fit.py` (hash-locked in `litellm_gateway_fit.py.lock`): LiteLLM 1.104.0, the newest
release at least two weeks old (uploaded 2026-10-03), run as a local proxy process in front of the fake Messages API
(`agent/tests/fake_anthropic.py`) on 127.0.0.1, with any other egress sent to a dead proxy and the bundled price map,
so nothing could reach the real API. Three routes, 2026-10-18:

| Check | Pass-through, client headers | Pass-through, own upstream key | Unified `/v1/messages` |
|---|---|---|---|
| Eval request reaches the upstream unchanged | yes | yes | yes |
| Upstream key | the client's key for the proxy | the configured key | the configured key |
| `fallbacks: "default"` reaches the upstream | **no** (dropped) | **no** | **no** |
| Beta header `server-side-fallback-2026-07-01` | yes | **no** | yes |
| Served model, `fallback` block, `usage.iterations` back | yes | yes | yes |
| `stop_reason: refusal`, `stop_details` back | yes | yes | yes |
| Tool round trip, append-only history accepted | yes | yes | yes |
| Cost header (`x-litellm-response-cost`) on a streamed response | no | no | no (a non-streamed one had it, 0.0108, correct for the usage) |

The unified route drops `fallbacks` because LiteLLM's router reads a field of that name as its own fallback list
(`router.py`: `kwargs.pop("fallbacks", self.fallbacks)`). The pass-through drop was observed (the client sent it, the
upstream did not get it); where it happens was not traced.

## Decision

No gateway. The agent calls the Claude API with the SDK (ADR 0103); spend is bounded in two places:

- **In the app**: each turn's cost at list prices (`cost_usd`, ADR 0103) feeds `agent.task.cost.usd`; the eval runner
  and the demo will stop starting tasks once the run's spend reaches the cap the user sets (built with the runner).
- **On the Console**: one API key per environment (local, CI if live evals are ever allowed), each with its own spend
  limit, revocable on its own.

## Alternatives considered

| Option | Why not |
|---|---|
| LiteLLM, unified route | Drops `fallbacks`, so demo mode would lose the server-side fallback; per-key budgets need its database (one more container); the agent already prices every turn |
| LiteLLM, pass-through forwarding client headers | Drops `fallbacks` and sends the proxy's own key to the upstream |
| LiteLLM, pass-through with its own key | Drops `fallbacks` and the beta header; no cost tracking |
| LiteLLM for evals only (no fallbacks there) | Transparent in that mode, but one more process in the measurement path for spend the app already counts |

## Consequences

- Positive: one hop less; every documented API feature reaches the API; no extra process or database on the laptop.
- Negative / risks: the budget is only as good as the app's own accounting (exact in eval mode; demo mode's known
  gap is in ADR 0103) and the Console limit; there is no central view across tools that use other keys.
- When to revisit: a gateway release passes `fallbacks` and the beta headers through (re-run the spike), or several
  apps or providers need one place for keys and budgets.
