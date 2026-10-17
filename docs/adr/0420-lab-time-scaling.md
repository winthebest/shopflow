# 0420. Lab experiments run on compressed time: one published factor, applied to every arm's SQL, floors in simulated days

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-data

## Context

Retention, maintenance and agent experiments are about history: weeks of snapshots, detection delays of days, floors
of a week (ADR 0410 sets 7d on the platform). Iceberg snapshot and file times are the real commit times. Trino and
PyIceberg stamp each commit with the wall clock, and nothing writes a snapshot dated in the past through them. Real
time would make one experiment take weeks. Compression must also be the same for every arm (method, baseline, agent),
or the comparison measures the clock instead of the method.

## Decision

1. **One factor, published before any result:** `lab.timescale.PUBLISHED`, 1 simulated day = 1 real minute (1440
   simulated seconds per real second). Changing it is a new decision, and every result records the factor it ran at.
2. **The harness rewrites the SQL of every arm.** Arms run statements through `lab.session.ArmSession`: the guard admits
   the connection (ADR 0419), then `Timescale.rewrite` scales the wall-clock windows, and the session keeps each
   statement as written and as run.
   - The retention threshold of `EXECUTE expire_snapshots(…)` and `remove_orphan_files(…)`, named or positional: the
     Trino duration is scaled, e.g. `'7d'` → `'7m'`, `'1h'` → `'2500ms'`. A call without a threshold is refused,
     because its default (7 real days) would not be compressed.
   - Ages from now, written as `current_timestamp[(p)] | localtimestamp | now() - INTERVAL '<n>' DAY|HOUR|MINUTE|SECOND`
     or `date_add('<unit>', <n>, <now>)` (time travel, `older_than` arguments): scaled.
   - Intervals on columns are business time and stay as written. Weeks, months and years are refused (no fixed
     length).
3. **Lab floors are in simulated days.** `lab_ops`, `lab_sbx`, `lab_ro` and every `lab_base_<run>` have 7 simulated
   days = `7m` for both `*_min_retention` properties; `lab_exp` (harness only) keeps 0s. Tests keep them in step with
   the factor (`lab/tests/test_timescale.py`), and the deny matrix checks each floor (`lakeops/tests/test_deny_matrix.py`).
   The platform's `lake` keeps its real 7d.
4. **History is written in compressed time.** A table's simulated history (one commit per simulated hour, for example)
   is spaced by `PUBLISHED.real(...)`, so every snapshot's age reads back in simulated time with `PUBLISHED.simulated`.

## Alternatives considered

| Option | Why not |
|---|---|
| Real time | A week of history takes a week; a 7d floor makes every expiry experiment a week long |
| Commit index instead of time | Fine for counting coverage, but Trino's procedures and floors take durations, so the windows would still need time |
| A fake clock for the engines (libfaketime) | Every service would need the same fake clock: S3 request signatures and Polaris's OAuth tokens carry times that the receiving service checks against its own |
| Back-dated snapshots written straight into metadata | Not what any engine does; it would bypass the catalog's commit checks the experiments are meant to exercise |
| Each arm scales its own SQL | One arm forgetting it changes the result; the session applies the rewrite to every statement an arm sends |

## Consequences

- Positive: a simulated week of history in seven minutes. The floors bind in the units the experiments reason in
  (6 simulated days are refused, 7 pass), and every arm sees the same clock.
- Negative / risks:
  - Only ages and windows scale. Query and procedure run times stay real, so a 30 s `optimize` is 12 simulated hours.
    Results report durations in real time and ages in simulated time.
  - Events closer than about one real second (24 simulated minutes) cannot be represented, since a Trino commit takes
    a fraction of a second; histories use coarser steps.
  - The rewrite is lexical, limited to the forms above; an arm writing an age another way escapes it.
- When to revisit: an experiment needs events closer than about 24 simulated minutes, or windows in another form
  (then extend the rewrite and its tests); Trino gains a way to stamp commits.
