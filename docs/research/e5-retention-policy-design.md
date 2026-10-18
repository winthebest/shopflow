# E5. Retention policy per table profile: design

- Status: **design draft, 2026-10-18. Nothing measured yet.** This file fixes the question, the models, the arms, the
  metrics and the budgets before any run (protocol.md §1). Values marked *to measure* come from the first lab and
  k3d sessions; each change made after a measurement is recorded here, with its reason, before the dev runs.
- Code (planned): `lab/experiments/e5_retention/`. Scenarios: `lab/scenarios/e5_retention/`. Results:
  `lab/results/e5_retention/`.
- Lab: catalog `lab_exp` (identity `harness`, floors 0s, ADR 0417), compressed time 1 simulated day = 1 real minute
  (ADR 0420).

## Problem and hypothesis

Every table of the platform's `lake` catalog keeps the same history: the daily maintenance expires snapshots and
removes orphan files older than 7 days, the catalog floor, schema by schema (`data/airflow/dags/iceberg_maintenance.py`,
`data/dbt/macros/iceberg_maintenance.sql`). The usual alternative is a longer uniform TTL ("30 days for every
table"). A uniform TTL ignores four things that differ from table to table:

- **what a retained day costs**: the bytes no longer in the current snapshot but still referenced by a retained one;
- **how the table is repaired**: rebuilt from its source, or re-applied from its own history;
- **who writes it**: a writer outside the platform's control keeps committing while the table is repaired;
- **how late damage is found.**

**Hypothesis.** At the same storage budget, retention set per table profile gives a higher Recovery Coverage than
a uniform TTL. The mechanism: the cost of a retained day differs between profiles by more than the need for one.
Two predictions follow from the write patterns and are measured before any modelling:
- an append-only CDC table pays mainly for what compaction rewrites;
- a table rebuilt in full every hour pays for a whole copy of itself per rebuild.

Moving the budget to where lookback is cheap and needed buys coverage.

**Time travel is not backup.** Losing the bucket, losing the catalog, or `DROP … PURGE` destroys the history too.
Those incidents are counted in their own column and point to disaster recovery; no retention policy is credited for
them.

## What the platform fixes (sources read 2026-10-18)

| Fact | Source |
|---|---|
| 7 bronze tables (`customers`, `products`, `orders`, `order_items`, `payments`, `shipments`, `heartbeat`), written only by the Iceberg sink, one commit per 60 s; the sink never creates or alters them | `deploy/platform/kafka-connect/base/connectors.yaml` |
| Bronze keeps every CDC row of every epoch; a new epoch re-snapshots Postgres | ADR 0406, ADR 0413 |
| 5 silver and 6 gold tables, dbt `table` materializations rebuilt in full every hourly run with `CREATE OR REPLACE`; on Polaris each run adds a snapshot without a parent to the same table; staging is ephemeral | ADR 0413, `data/dbt/dbt_project.yml` |
| Daily 03:30: `optimize`, `optimize_manifests`, `expire_snapshots` and `remove_orphan_files` at 7 days, for bronze, silver and gold | the DAG and macro above |
| Floors of `lake`: `iceberg.expire-snapshots.min-retention` and `iceberg.remove-orphan-files.min-retention` = 7d | `deploy/platform/trino/base/values.yaml` |
| A tag keeps its snapshot and files; `max_ref_age_ms` counts from the snapshot, not from the tagging; Trino cannot create tags (PyIceberg can); no guarantee rests on tags | ADR 0418 |
| Postgres has a backup chain (WAL archive and base backups); the lake bucket has none. Locally both live in the same SeaweedFS (bucket `pg-backup`), so losing the object store loses the lake and the Postgres backups together | ADR 0511, `docs/contracts/environment.md`; no other backup ADR as of 2026-10-18 |

## Profiles

| Profile | Tables | Writer | Repair after logic damage |
|---|---|---|---|
| P1 raw CDC | `bronze.*` (incl. `bronze.payments`) | the sink, outside the platform (`external_writer: true`) | **re-apply**: write the difference between a good snapshot and the current one as new commits on top of the current snapshot, never rollback. Without a good snapshot, **re-snapshot** from Postgres (new epoch): the current state comes back, the change history between the damage and the re-snapshot does not |
| P2 curated | `silver.*` except payments, `gold.*` | dbt, hourly | fix the cause, then **rebuild** from bronze (`dbt build`); rolling back to the table's own snapshot is the fast path when one bad run is the cause |
| P3 regulated | `silver.payments` | dbt, hourly | as P2; additionally a month-end state must stay readable for audit (month-end tag) |

- `bronze.heartbeat` only proves liveness. It is P1 but carries no business data: no incidents land on it, and its
  bytes count toward the budget.
- **P2's lookback mostly buys speed, not recovery.** Every silver and gold row can be rebuilt from bronze's latest
  completed epoch. A direct write to a P2 table is undone by the next hourly rebuild, and a bad model is repaired by
  fixing it and rebuilding. Both arms get the same repairs, so if the prediction holds, the optimizer moves P2 towards
  "rebuilt from bronze, retention ≈ 0". That is a valid result, and the deployed floor then sets P2's cost.
- **The mapping is confirmed by measurement first:** commits per day, bytes per commit, bytes rewritten by the daily
  `optimize`, and table size per table, on k3d (*to measure*, data slot). At demo size the data files are probably
  all below Trino's default `optimize` threshold (100 MB), so the daily `optimize` may rewrite whole bronze tables
  every day. If so, P1's
  retained day costs about one table copy, not one day of ingest. This holds for this platform's data size only, and
  the results say so.

## Time

- **The simulation counts commits.** Each profile's commit schedule comes from the measured rates: P1 one commit per
  60 s, P2 and P3 one rebuild per hour, maintenance at 03:30. Retention and detection delays are durations on that
  schedule. The horizon is 180 simulated days, and incidents land only between day 90 and day 180 minus the largest
  delay, so every retention examined (up to 90 days) has its full history behind it.
- **The lab replays in compressed time** (`lab_exp`, floors 0s), at most 2 000 commits per table, with
  `write.metadata.delete-after-commit.enabled = true`. P1's real cadence cannot be replayed at 1 day = 1 minute,
  because that would be 24 commits per real second. The replay therefore commits P1 once per simulated hour, with the
  same bytes per simulated day. Separately, 2 000 back-to-back commits measure the metadata one sink commit adds.
- The expiry mechanism is checked on its own, with the compressed thresholds (`lab.timescale` rewrites them for
  every arm): `expire_snapshots` and `remove_orphan_files` must remove exactly the files the storage model says.

## Storage model

- **Bytes a policy holds at time t:** the data and delete files referenced by any retained snapshot or by a ref, but
  not by the current snapshot. Add the metadata kept: manifests, manifest lists and `metadata.json` versions. The
  current snapshot's own bytes are the same for every arm and are reported, not budgeted.
- **Accounting:** file paths across retained snapshots from `$all_entries`, sizes from `$files`, refs from `$refs`,
  so bytes pinned by tags are included. The columns are checked with `DESCRIBE` first; the fallback is PyIceberg's
  `inspect` tables.
- **Model per profile:** bytes(retention) from the replay. It is accepted when it predicts the measured bytes within
  10 % at three retentions per profile. Otherwise the simulation uses the measured curve, interpolated, and the README
  says so.

## Incident model

- **300 incidents per split** (at least 100), from a fixed seed. Classes and shares on dev:

| Class | Share | Kinds | Counted in |
|---|---|---|---|
| Logic | 75 % | a wrong DELETE; a wrong UPDATE or MERGE; a wrong overwrite (a bad model run, `CREATE OR REPLACE`); corruption found late (bad rows appended for a while, e.g. a decoding bug in the sink) | Recovery Coverage |
| Schema break | 12 % | a column dropped, renamed or retyped | its own column: a rollback is assumed not to restore the schema (Iceberg records a schema change without a new snapshot); *to check* by probe first |
| Infrastructure | 13 % | bucket lost, catalog lost, `DROP … PURGE` | its own column, pointing to DR; never credited to a policy |

- **Where damage lands:** uniform over the 17 business tables (6 bronze without heartbeat, 5 silver, 6 gold).
  Damage spreads downstream: bronze damage reaches silver and gold at the next hourly rebuild, and silver damage
  reaches gold.
- **Detection delay on dev:** 70 % between 1 min and 1 h, 25 % between 1 h and 48 h, 5 % between 3 and 14 days,
  log-uniform within each band. This distribution is an assumption. Two more distributions run on dev as a
  sensitivity check, never for tuning: a tail of 10 % up to 30 days, and a tail of 20 % up to 30 days.
- **Held-out:** another seed and a heavier tail. sf-docs, an independent lane, writes the parameters within the
  schema below and encrypts them (protocol.md §5); this lane never sees them before the run. The incidents are
  generated from them.

## Recoverable, strictly

An incident is **recovered** when, at detection, both hold:
- every table the damage reached still has a snapshot from before the damage, kept by retention or by a ref;
- the profile's repair (table above) brings back the good state without losing good data written after the damage.

- **P1:** recovered by re-apply when a good snapshot survives. Otherwise "recovered from the source, history lost",
  counted outside Recovery Coverage.
- **P2, P3:** recovered when their bronze sources are recovered or untouched, because the rebuild reads them. The
  rollback fast path only shortens the outage.
- **Good data lost by the repair** is its own metric, in rows and bytes. A diagnostic column also reports what a
  rollback of P1 would have lost: the rows the sink appended between the damage and its detection. It shows why
  rollback is not P1's repair.

## Arms

- **Baseline: uniform TTL T** for every table, as the platform does today (T = 7 days).
- **Method: retention per profile.** For each profile P, a retention r_P and a minimum snapshot count m_P. For P3,
  optionally also a month-end tag with `max_ref_age_ms`. The maintenance job that sets the tag owns it and removes it
  (ADR 0418). The tag only adds lookback, and its bytes count toward the budget. The guarantee for P3 is the floor of
  its catalog.
- **Optimizer:** exhaustive search over r_P ∈ {0, 1 h, 6 h, 1 d, 2 d, 3 d, 7 d, 14 d, 30 d, 60 d, 90 d},
  m_P ∈ {1, 5}, and the tag on or off.
  - It maximizes Recovery Coverage on dev subject to Σ bytes ≤ B, and breaks ties by fewer bytes.
  - That is 11³ × 2³ × 2 ≈ 2 × 10⁴ policies over 300 incidents, inside the 5-minute limit.
  - The objective is the primary metric, unweighted. The brief's headline Recovery Coverage counts every incident
    once. The phase plan's importance weights are an optimizer choice, and the result would depend on them. A weighted
    variant (P3 ×3, P1 ×2, P2 ×1, for objective and metric) is reported next to it.
- **Budgets, published now:** B(T) = the bytes the uniform T-day policy holds on the dev replay, for
  T ∈ {1, 3, 7, 14, 30} days. At each B(T), the baseline is the uniform T policy and the method is the optimizer
  within B(T). The primary comparisons are at B(7 d), today's platform, and B(30 d), the usual alternative.
- **Floors bind the deployed policy, not the research arms.** On `lake`, no table may go below 7 days, so no policy
  that respects the floor holds less than uniform 7 d. The method's gain at B(7 d) can therefore be shown on `lab_exp`
  only. The deployed policy, max(optimizer, floor), is compared at B(14 d) and B(30 d), with its own cost and coverage.

## Metrics

- **Primary: Recovery Coverage** = recovered logic incidents ÷ logic incidents, method against baseline at the same
  budget.
- **Secondary:**
  - storage factor (held bytes ÷ current-snapshot bytes);
  - mean lookback per profile;
  - RPO misses (detected after the last good snapshot expired);
  - good data lost by repair;
  - the schema, infrastructure and recovered-from-source columns;
  - the Pareto curve of storage against coverage, for both arms.
- **Uncertainty:** the simulation is deterministic, so the uncertainty is which incidents were drawn. Each
  difference gets a bootstrap 95 % CI over incidents (1 000 resamples, fixed seed). "Better" means the CI excludes 0.
  The timing noise floor (protocol.md) does not apply here.

## Validation by real runs

- **At least 10 incidents run for real on `lab_exp`** in compressed time. The tables are built with `lab.prodlike`
  and `lab.tables`, and the policy's expire and orphan removal run at the compressed thresholds. Each run:
  1. inject the damage;
  2. wait for the detection delay;
  3. repair the profile's way;
  4. compare the table fingerprints with the oracle's (`lakeops` verify), and count the good rows lost.
- **Choice of incidents:** they cover every class and both verdicts, recovered and not.
- **Acceptance:** the simulator's verdict matches every real run. A mismatch changes the model, and the change is
  recorded here before the held-out run.

## Dev and held-out

- Tuning (the grid, the sensitivity runs) uses dev only.
- Then `make lab-freeze EXP=e5_retention`, a dev run at the freeze, and one held-out attempt (protocol.md §5).
- The held-out run evaluates the frozen optimizer's policies at the published budgets.

### Incident parameters: the schema both splits use

`lab/scenarios/e5_retention/dev.yaml` and the held-out file have the same shape. Each scenario is one parameter set;
the experiment draws its incidents from it.

```yaml
scenarios:
  - id: primary          # [a-z0-9_]{1,40}; held-out: exactly one "primary", at most 2 more (held-out sensitivity)
    seed: 4242           # integer; dev uses seeds below 1000, held-out seeds are in [1000, 2^31 - 1]
    incidents: 300       # integer in [100, 1000]
    classes:             # shares, multiples of 0.01, summing to 1
      logic: 0.75        # [0.50, 0.85]
      schema: 0.12       # [0.05, 0.25]
      infra: 0.13        # [0.10, 0.15], the phase plan's range
    delays:              # 2 to 5 bands, ascending, not overlapping (gaps allowed); shares multiples of 0.01, sum 1
      - {from: 1m, to: 1h, share: 0.70}     # durations: <n>m, <n>h or <n>d; the first from >= 1m, the last to <= 60d
      - {from: 1h, to: 48h, share: 0.25}
      - {from: 3d, to: 14d, share: 0.05}
```

**A heavier tail, for the held-out `primary`,** compared with dev's primary (above):
- the share of delays above 48 h is at least 0.05 and at most 0.40;
- the longest delay is at least 14 days;
- at least one of the two is strictly greater than on dev.

A band that straddles 48 h counts by the part of its log-length above 48 h.

**What the writer may vary:** the seed, the incident count, the class shares and the delay bands (edges and shares),
within these ranges.

**What stays fixed,** because it is part of the model under test:
- the tables and where damage lands;
- how damage spreads, and the repairs;
- the kinds within a class (drawn uniformly);
- log-uniform delays within a band;
- the 180-day horizon and the window incidents land in. The 60-day maximum delay keeps that window at least 30 days.

**The writer's steps:**
1. Write the plaintext outside git.
2. Check it: `python -m lab.scenarios check <plain.yaml> e5_retention` (`lab/experiments/e5_retention/params.py`).
   `encrypt` runs the same check first. It runs again after decryption, but by then the attempt is recorded, so a
   file that failed there would burn the attempt.
3. Encrypt with `python -m lab.scenarios encrypt <plain.yaml> e5_retention`: public recipient only, written once.
4. Open a PR with `lab/scenarios/e5_retention/holdout.sops.yaml` only.

The file must land before `make lab-freeze`: the freeze hashes `lab/scenarios/e5_retention/`, so a later file makes
the held-out run refuse. The values stay with the writer until `make lab-publish-holdout`.

## Into the platform (after held-out)

- **`policies/retention.yaml`**: profile → `retention`, `min_snapshots`, `external_writer`, `rollback_window`, tag
  rule; and table → profile. The executor and the maintenance DAG read it.
- **Floors** are constants set by hand in the catalog properties, never derived from the policy. They get CODEOWNERS
  and a required CI check that `min(retention.yaml) ≥ floor`.
- **`lake_regulated`**: P3 in its own Polaris warehouse with a higher floor (e.g. 90 days), so that `lake` cannot see
  or expire it. ADR 0417's boundary is a catalog per warehouse.
  - Test: through `lake`, expiring a P3 table fails.
  - To measure as well: what a 90-day floor costs on a table rebuilt every hour.
- **ADRs** written with the results: time travel is not backup; retention policy as code; an independent retention
  floor.

## Threats to validity

- The delay distribution is assumed: hence the sensitivity runs, and no claim beyond this platform.
- **The floor limits what the platform can gain.** With a 7-day floor on every `lake` table, no compliant policy
  holds less than uniform 7 d. A gain at B(7 d) is therefore shown on `lab_exp` (floors 0s) only, and the deployed
  policy can only be compared at larger budgets (B(14 d), B(30 d)). The one-page README carries this in its failure
  case.
- Demo-size data: `optimize` behaviour and file sizes follow this scale. The storage model takes measured bytes, not
  formulas.
- Simulated repairs: checked by the real runs above. Repairing P1 while the sink commits can conflict; E7 measures
  that conflict, and its findings apply.
- Silver and gold history on AWS Glue may differ: Glue might refuse `CREATE OR REPLACE` (ADR 0413). These results are
  for the k3d stack (Polaris).

## First lab session (before any modelling)

1. `DESCRIBE` `$all_entries`, `$entries`, `$files`, `$refs` and `$snapshots` on Trino 483.
2. Probe: does a rollback restore the schema of the snapshot it goes back to?
3. The metadata one sink commit adds, with `write.metadata.delete-after-commit.enabled` and
   `write.metadata.previous-versions-max`.
4. Whether the daily `optimize` rewrites already-compacted bronze files at demo size.
5. Commit rates and table sizes on k3d (data slot, orchestrator's GO).
