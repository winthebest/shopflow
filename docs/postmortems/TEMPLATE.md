# Postmortem: <what users saw> (<one-line cause>)

<!-- Copy to docs/postmortems/YYYY-MM-DD-<slug>.md. Blameless: describe systems and decisions, not people.
     One page. Every number comes from a measurement (Prometheus, k6, logs) and says where it came from. -->

- Date / time window: YYYY-MM-DD HH:MM–HH:MM (UTC+7)
- Environment: <lane cluster / sf-main / EKS session>, profiles <…>, load <k6 rate>
- Type: game day <n> (planned) | incident (unplanned)
- Severity: page | ticket | none — which SLO alerts fired
- Status: action items open / all closed

## Game day plan (planned experiments only; written before the run)

| | |
|---|---|
| Hypothesis | What we expect to happen, with the number we expect (e.g. "page within 5 min") |
| Experiment | `chaos/<file>.yaml` or the manual steps |
| Blast radius | Which namespaces / components can be affected; what must not be |
| Stop conditions | When we abort early (e.g. error ratio > 50% for 5 min, anything outside the blast radius degrades) |
| Measurement window | Start/end; no heavy Docker work on the VM; laptop kept awake |

## Summary

Three or four sentences: what happened, impact, cause, how it ended.

## Impact

| | Value | Source |
|---|---|---|
| Requests in the window | | |
| Bad events (SLO definition) | | |
| Error budget used (28-day, at the measured rate) | | |
| Worst 5-minute SLI | | |

## Timeline (UTC+7)

| Time | Event |
|---|---|
| | Experiment started / first symptom |
| | Alert fired (which, severity) |
| | Detected by a human |
| | Mitigation / experiment stopped |
| | SLI back to normal; alert resolved |

## Root cause and contributing factors

1. …

## Result vs hypothesis

What matched, what did not, and why (planned experiments only).

## What went well

- …

## What went wrong

- …

## Action items

| Action | Owner | Status |
|---|---|---|
| | | open / done (commit or PR) |

## Re-run after the fixes (when an action item changes behaviour)

| Metric | Before | After |
|---|---|---|
| Burn rate / bad events / detection time | | |
