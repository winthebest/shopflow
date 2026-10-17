# 0108. Eval CI without an LLM: recorded LLM turns replay from cassettes, guard decisions check against a golden set

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-app

## Context

The eval pipeline (runner, independent scorer, metrics) has to be tested on every change, but CI holds no API key and
must not spend; a local model is too slow and too big for a CI runner. Two different things need checking: that the
pipeline scores a recorded run the same way each time, and that the guard (Phase 14) decides each proposal as
intended. Recordings are dev data in git, so they must not carry credentials; held-out runs must never be in git.

## Decision

- **LLM turns only are recorded.** `evals/replay.py` is an httpx2 transport (`Cassette`) placed under a provider's
  HTTP client: record mode passes requests to the real backend and stores each exchange; replay mode serves them back.
  Trino, the executor and the guard are never replayed: the scorer reads real state.
- **Key**: SHA-256 of method, path, the kept headers (`anthropic-beta`, `anthropic-version`, `content-type`) and the
  JSON body after `scrub` (per-run values such as `lab_evalstg_<id>` replaced by placeholders). Identical requests
  (repetitions of a prompt) replay in recorded order.
- **A request that was not recorded is a hard failure**: a 404 `cassette_miss`, listed in `Cassette.misses`, which
  neither the Anthropic SDK nor the Ollama adapter retries; both raise `ProviderRequestError`. Raising inside the
  transport would not do: the SDK turns any transport exception into a retried connection error.
- **Redaction before anything is stored or hashed**: every header outside the kept list (no API key, no
  Authorization), user info and query strings of the URL, and in bodies: credentials in URLs, Basic/Bearer values,
  `X-Trino-*` header lines, Discord webhook URLs.
- **Held-out** cassettes go under `evals/cassettes/holdout/`, ignored by git (a test checks the rule). `.gitleaks.toml`
  gains a `discord-webhook-url` rule: the default ruleset (gitleaks 8.30.1) does not catch one with a random token.
- **Golden decisions** (the guard's recorded proposals, SQL plus context, mapped to the decision the guard must take)
  run in CI once the guard exists, so a policy change is checked without re-recording any LLM turn. Until then CI runs
  replay only; there is no placeholder.
- No nightly live eval: a live run will be manual (a future `make eval-live`, through a GitHub environment with a
  reviewer and a cap), once the user approves spend.

## Alternatives considered

| Option | Why not |
|---|---|
| vcrpy | 8.3.0 stubs `httpx`, not `httpx2`, which the Anthropic SDK 1.x uses; a transport is the SDK's own injection point (`http_client`) |
| Key on the raw request | Per-run namespace names and SDK headers (`x-stainless-*`, retry counts) change every run, so nothing would replay |
| Replay the guard's Trino calls as well | The scorer must judge real state (`$snapshots`, `$refs`, data checksums), not a recording |
| Re-record live after each policy change | Costs money and time; golden decisions test the guard without any LLM call |

## Consequences

- Positive: CI exercises the providers and, later, the whole scoring pipeline with no key and no model; a stale
  recording fails loudly instead of answering an old prompt.
- Negative / risks: a recording ages with the prompts: any prompt, tool or schema change means re-recording that
  dev cassette (from Ollama at $0, or from Claude when spend is approved). Redaction is pattern-based: a secret in an
  unexpected shape could pass it, so gitleaks stays the gate on every commit.
- When to revisit: recordings need streaming timing (they store whole responses), or the SDK moves off httpx2.
