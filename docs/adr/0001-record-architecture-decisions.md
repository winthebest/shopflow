# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-10-09
- Lane: orchestrator

## Context

Shopflow is built by several parallel workstreams (lanes) and reviewed by one orchestrator. Decisions taken in one
lane affect the others, and the project is also a portfolio: reviewers and interviewers will ask *why* each tool
was chosen.

## Decision

Every non-trivial technical choice gets a one-page ADR in `docs/adr/`, using `0000-template.md`. Each lane uses its
own number range (see `README.md`) so parallel work never collides.

## Alternatives considered

| Option | Why not |
|---|---|
| Decisions only in PR descriptions | Hard to find later; no index |
| One long design document | Becomes stale; merge conflicts between lanes |

## Consequences

- Positive: each choice has a findable rationale and alternatives.
- Negative: small writing overhead per decision.
- When to revisit: if ADRs stop being read or updated, replace with a shorter decision log.
