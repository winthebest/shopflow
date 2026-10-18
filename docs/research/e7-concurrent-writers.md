# E7. Concurrent writers on Iceberg

**Status: dev.** The probes and the first dev pass are in. Held-out has not run, so every held-out cell below says
*pending*. The details, evidence and threats to validity are in
[e7-concurrent-writers-design.md](e7-concurrent-writers-design.md). Numbers come only from
`lab/results/e7_concurrency/`.

## Problem

Several writers commit to the same Iceberg tables: through Trino, or straight through the REST catalog, as the Kafka
Connect sink does. Optimistic concurrency should make a losing commit fail and retry. The question is whether
anything still goes wrong: an update lost, a row doubled by a retry after an ambiguous error, or a row brought back
by compaction. And what does keeping it correct cost in throughput?

## Hypothesis

The default writers either fail many commits or double rows when they retry after an ambiguous error. A method that
retries knowing the conflict, writes idempotently and compacts only cold partitions reaches LostUpdate = 0 and
Duplicates = 0, with a higher correct commit throughput.

## Setup

- **Lab stack:** Trino 483, a single node with 2 CPUs and 3 GiB, the platform's digest. Polaris 1.8 and SeaweedFS.
  The writers run in the lab network's `py` container, fixed at 2 CPUs and 2 GiB.
- **Semantics:** each op is a unique event `writer:seq` adding `delta` to `key`, and the correct state is the per-key
  sum over committed ops. Commit order can't change it.
- **Tables:** `lab_e7.events`, which writers append to, and `lab_e7.counters`, read-modify-write per key. Each
  counter row carries a ledger of the op ids it applied, so `value = Σ delta(ledger)`. Tables are fresh per run and
  format v2.
- **Writers:**
  - W1 inserts events;
  - W2 adds to counters;
  - W2-py does the same adds through PyIceberg;
  - W3 is an OPTIMIZE loop, plus a sink-like appender.
- **Split:** dev is N ∈ {2, 4} at low and medium contention, plus N = 4 with OPTIMIZE. Held-out is N = 8 at high
  contention with OPTIMIZE, run once, only through the lab runner (freeze + ledger).
- **Oracle:** it reads only the op log and the final tables. Every op ends acked, definite-fail or unknown, and an
  op whose client died reads unknown. It counts:
  - LostUpdate: acked ops that are absent;
  - Duplicates: extra occurrences of an op;
  - WrongFinalRows: rows of failed or unlogged ops, ledger mismatches, split keys and torn batches.

  Its three self-tests pass on synthetic states and on real tables: an acked op dropped, a client killed after its
  commit, a stale retry.

## Baseline and method

- **Baseline:** one statement per op, no application retry, and Trino, Iceberg and the trino client at their
  defaults. OPTIMIZE runs on the whole table.
- **Method** (`lakeops.writes`):
  - **idempotent statements:** counters use `UPDATE … WHERE key = k AND NOT contains(ledger, op)`; events use
    `INSERT … WHERE NOT EXISTS`;
  - **one attempt at a time:** an ambiguous attempt is cancelled, then waited on until Trino reports it terminal,
    before any retry;
  - **one HTTP attempt per request**;
  - **ops routed to writers by key**;
  - **OPTIMIZE only on past days.**

  The variant `method-resolve` resolves an ambiguous attempt from the table's snapshots, which carry Trino's query
  id. Its events then go out as a plain INSERT first.
- **Why it is correct:** every statement is idempotent, and no two attempts of one write ever run at once. So a
  write lands at most once, whatever the errors say.

## Metric

**Correct Commit Throughput (CCT)** = acked commits ÷ seconds of the write phase. It counts only when LostUpdate = 0
and WrongFinalRows = 0. Unknown ops never count, even those that committed. Duplicates are reported separately, and
the method must have none on held-out.

## Results

**Dev, first pass** (2026-10-18): 20 s runs, 3 per arm and cell, ABBA order, 10 warm-ups. Every run of both arms was
valid, with no LostUpdate, Duplicate or WrongFinalRow. CCT is the median [range], in commits per second.

| Cell | Baseline | Method | |
|---|---|---|---|
| Counters, N=2 (low / medium) | 2.73 / 2.93 | 5.73 / 6.05 | method higher; the baseline left ~50% of ops uncommitted after conflicts |
| Counters, N=4 (low / medium) | 1.95 / 1.72 | 5.85 / 4.97 | method higher; the baseline left ~75% uncommitted |
| Events, N=2 | 8.82 / 8.94 | 8.50 / 8.26 | not distinguishable |
| Events, N=4 | 9.96 / 9.69 | 8.85 / 8.71 | **method lower** (see failure cases) |

**Held-out** (N = 8, high contention, OPTIMIZE, ≥10 runs per arm): *pending*. The CCT of baseline and method, plus
LostUpdate, Duplicates and WrongFinalRows, will be filled in from the single held-out run.

**What the lab showed** (probes, evidence in the design doc):
- Trino refuses a racing MERGE cleanly. But every MERGE is checked against all concurrent commits to the table,
  while an UPDATE by key is checked only within its partition.
- OPTIMIZE racing a MERGE fails the MERGE and resurrects nothing.
- Trino starts a statement only when its client first polls it, so the client's own re-send can't double a write.
- Every snapshot carries the query id that committed it.

## Failure cases

1. **The method loses on uncontended appends.** At N = 4, events ran about 10% below the baseline. The
   `NOT EXISTS` read before each append costs more than the retries win back. `method-resolve` drops that read; it
   is measured next on dev.
2. **The harness overloaded the catalog.** Dropping the tables every run made Polaris purge hundreds of files per
   drop in the background, which exhausted its S3 pool and stalled the runs. That first pass was discarded. The
   runner now renames old tables aside.

## Rerun

- Unit tests: `make lab-check`.
- The oracle's self-tests on real tables: `make lab-test`.
- Dev, inside the lab network: `lab/stack.sh py exp -- python -m e7_concurrency.dev --arms baseline method
  method-resolve --runs 7`.
- Held-out: `make lab-run EXP=e7_concurrency SPLIT=holdout`.
- Sources: no external documentation was relied on. The behaviour above was measured on the lab. The two library
  facts were read in the installed source: trino 0.340.0's HTTP retry, and PyIceberg 0.12.0's commit retry and
  isolation check.
