# E7. Concurrent writers on Iceberg: a correctness oracle and correct commit throughput

- Status: **design, fixed before any dev run** (2026-10-18). No results yet; every number will come from
  `lab/results/e7_concurrency/`.
- Code: `lab/experiments/e7_concurrency/` (op log, oracle, scenarios, writers, direct PyIceberg writers, runner), and later
  `lakeops/src/lakeops/writes/` (the method) and `lakeops/src/lakeops/verify/`.
- Lab: the `lab_exp` catalog of the lab stack (`lab/stack.sh`, ADR 0417), as identity `harness`.

## Problem and hypothesis

Several writers commit to the same Iceberg tables through Trino, or directly through the REST catalog, as the Kafka
Connect sink does. Optimistic concurrency should make a conflicting commit fail and retry. The question is whether
anything still goes wrong: an update lost, a row doubled by a retry after an ambiguous error, or a row brought back by
a compaction.

**Hypothesis.** The default writers either fail many commits or double rows when they retry after an ambiguous
error. A method that retries knowing the conflict, writes idempotently and compacts per partition reaches
LostUpdate = 0 and Duplicates = 0, with a higher correct commit throughput.

## Semantics (fixed before any run)

The semantics are commutative, so the order in which commits land never changes the correct result:

- An **op** is one unique event `writer:seq` that adds `delta` to `key`. `writer` is unique per process (e.g.
  `w2-3`), and `seq` is unique within a writer. Ops sent in one statement or commit share a **group**: all of them
  commit, or none.
- The **correct final state** is, per key, the sum of `delta` over the ops that committed, each counted once.
- Deltas are positive integers (1–9), drawn from the run's seed.

## Tables

Created through Trino in `lab_exp.lab_e7`, Iceberg format v2. Each run drops and recreates both tables, so no run
inherits files, snapshots or ledgers from another.

```sql
CREATE TABLE lab_exp.lab_e7.events (
  writer_id varchar NOT NULL, seq bigint NOT NULL, key bigint NOT NULL, delta bigint NOT NULL, event_day date NOT NULL
) WITH (format_version = 2, partitioning = ARRAY['bucket(key, 8)', 'event_day']);

CREATE TABLE lab_exp.lab_e7.counters (
  key bigint NOT NULL, value bigint NOT NULL, applied array(varchar) NOT NULL
) WITH (format_version = 2, partitioning = ARRAY['bucket(key, 8)']);
-- Seeded with one row per key of the run: value 0, applied = ARRAY[].
```

- `events` is the append target (W1, the sink-like appender), with one row per op.
- `counters` holds one row per key (W2).
- **Why a ledger.** Trino cannot commit two Iceberg tables atomically, so a counter update cannot also write its event
  row in the same commit. Each counter row therefore carries the ids of the ops it applied (`applied`). "The counters
  equal the sum of the events present" becomes a per-row check, `value = Σ delta(applied)`. The oracle also knows
  exactly which unknown ops landed.
- The DDL is checked on the lab when the writers land; any change to it is recorded here before the dev runs.

## Op log: three outcomes

Each writer appends JSON lines to its own file (`oplog.py`):

- an `invoke` line (op, table, key, delta, group), written **before** the request leaves the client;
- then a `complete` line: outcome, number of attempts, error class, latency, Trino query ids, and the snapshot id when
  it can be read.

| Outcome | When |
|---|---|
| `acked` | The server reported success to the client |
| `definite-fail` | The server reported a failure that guarantees nothing committed. Only error classes on an allowlist count. The allowlist starts empty, is filled from the conflict matrix on dev, and is frozen before held-out |
| `unknown` | Anything else: a client timeout, a lost connection, a killed client, an error that may follow a commit (Iceberg's `CommitStateUnknownException` is one). An `invoke` without a `complete` reads back as unknown |

- The file is line-buffered, so an `invoke` line survives a SIGKILL of the writer a moment later. The tests kill a
  real process to check this.
- A torn last line is ignored: for an `invoke`, the request was never sent; for a `complete`, the op stays unknown.
- Misclassification is safe in one direction only. An op wrongly called definite-fail that did commit shows up as
  `failed-present` (a wrong row). An op wrongly called unknown just weakens the check.

## Oracle

`oracle.grade(ops, events, counters, seeded_keys)` reads only the op log and the final rows, never a writer's own
verdict or retry logic. The rules:

1. an acked op is present **exactly once**;
2. a definite-fail op is **absent**;
3. an unknown op is present **at most once**;
4. each seeded key has exactly one counter row, with `value = Σ delta(applied)`;
5. a group is all present or all absent.

Counts:

- **LostUpdate**: acked ops absent;
- **Duplicates**: occurrences of any op beyond its first, in either table;
- **WrongFinalRows**: final rows that must not exist or hold a wrong value. These are:
  - rows of definite-fail ops;
  - rows not in the log, or altered;
  - a ledger that doesn't add up;
  - a second row for a key;
  - an unseeded key;
  - a row of a torn group;
  - a seeded key with no row, which counts as one.

Also reported: `unknown_present` / `unknown_absent` (how often an ambiguous op actually committed), `failed_present`
and `torn_groups`.

**Self-tests.** The oracle must catch each of these. The unit tests (`tests/test_oracle.py`) build each case as a
synthetic final state; the lab repeats each one on real tables:

| Self-test | Synthetic state (unit test) | On the lab |
|---|---|---|
| 1. An acked op dropped | Event row removed → LostUpdate 1. Add removed from ledger and value → LostUpdate 1. From the ledger only → also WrongFinalRows 1 | `DELETE` one acked event; strip one ledger entry |
| 2. Client killed right after commit | A real process killed after its `invoke` → unknown. Present once → valid, `unknown_present` 1. Retried blindly → Duplicates | `runner.Kill(writer, at)`: SIGKILL a writer mid-run; Trino keeps running its statement |
| 3. A stale retry after another writer's add | The absolute write overwrites B's add → LostUpdate 1. Stale value with the ledger appended in SQL → WrongFinalRows 1, Duplicates 1 | An application-level read-modify-write writer, forced to retry after another writer's ack |

Each oracle rule was also removed in turn, and each removal made the corresponding self-tests fail.

## Metric

- **Correct Commit Throughput (CCT)** = acked commits ÷ seconds of the write phase. A group counts as one commit.
- **A run counts only when LostUpdate = 0 and WrongFinalRows = 0.** An invalid run has no CCT and is reported as a
  violation.
- **Unknown ops never enter the numerator**, even those that committed. They are reported as `unknown_present`. Both
  arms use the same rule.
- **Duplicates** are reported on their own. They don't void a run's CCT, but the method fails if it has any on
  held-out.
- Secondary metrics:
  - conflict rate: attempts refused for a conflict ÷ attempts;
  - retries per op;
  - p95 latency of acked ops;
  - abort rate (definite-fail ÷ ops);
  - unknown rate;
  - write amplification: bytes of data files added, from the snapshot summaries, ÷ logical bytes;
  - the longest ledger.

## Writers

| Writer | Does | Baseline form | Contention it creates |
|---|---|---|---|
| W1 | Inserts events, N processes | `INSERT INTO events VALUES (...)`, one op per statement | Commit races on the table's metadata; partitions disjoint or shared |
| W2 | Adds to counters, read-modify-write on the same keys | `MERGE INTO counters ... WHEN MATCHED THEN UPDATE SET value = value + delta, applied = applied \|\| op` | Row-level conflicts on the same key, through Trino's MERGE |
| W2-py | The same adds, written directly | PyIceberg: read the row, compute, `upsert` | Conflicts checked only by the catalog's commit requirements |
| W3 | Maintenance under load | `ALTER TABLE ... EXECUTE optimize`, on the whole table or `WHERE event_day = ...`, in a loop, plus a sink-like appender (PyIceberg `append` of small batches through the REST catalog, as the Kafka Connect sink commits) | Rewrites racing appends; on `counters`, rewrites racing MERGE deletes |

- N ∈ {2, 4, 8}.
- Every writer process has its own Trino connection and op log, and the processes start together at a barrier.

## Configurations

- **Baseline (default writers):**
  - Trino's and Iceberg's default commit retries;
  - the trino client's defaults: 3 HTTP attempts per request and a 30 s timeout. In trino 0.340.0 that includes
    re-sending `POST /v1/statement` after a timeout or a lost connection, out of the op log's sight (question 7);
  - no application retry;
  - OPTIMIZE on the whole table;
  - ops dealt round robin from the shared stream.
- **Method** (`lakeops.writes`, tuned on dev, frozen before held-out):
  - **Idempotent writes.**
    - Events via `MERGE INTO events ... ON (writer_id, seq, plus key and event_day for partition pruning) WHEN NOT
      MATCHED THEN INSERT`.
    - Counters via `WHEN MATCHED AND NOT contains(applied, op) THEN UPDATE`.
    - A retry of an op that committed then changes nothing.
  - **Retry with full jitter** on a conflict and on unknown. An unknown attempt is first cancelled and waited on until
    it is terminal (`DELETE` then `GET /v1/query/{id}`). Otherwise the retry can race its own attempt still running on
    the server, and an insert-if-absent does not stop two concurrent inserts.
  - **One HTTP attempt per request** (`max_attempts=1`), so every statement sent is an attempt the method sees.
  - **Serialise only overlapping keys**: ops are routed to writers by key, so two writers never update one key.
  - **OPTIMIZE per partition**, skipping the partitions being written.
  - Iceberg `commit.retry.*`, if Trino honours them (question 3).
- Both arms process the same op stream, from the same seed.
- The method is `lakeops.writes` (`write`, `insert_if_absent`, `add_once`) plus the arm in `method.py`. Its starting
  parameters (5 attempts, backoff 0.1–3 s with full jitter, 60 s to settle an attempt, polled every 0.5 s) are tuned
  on dev and frozen before held-out. It is correct when two things hold: every statement is idempotent, and no two
  attempts of one write run at once. Then a write lands at most once, whatever the errors say.

## Contention levels and the dev / held-out split

| Level | Counters keys | Event partitions | Maintenance |
|---|---|---|---|
| Low | 16 per writer, disjoint | Days of its own per writer | None |
| Medium | 64 shared, uniform | Shared days, all buckets | None |
| High | 4 shared (hot keys) | Shared days | OPTIMIZE loop + sink-like appender |

The event day moves forward every 10 s of the write phase, so a run writes several day partitions, and a past day
can be compacted while writers append to the current one.

- **Dev:** N ∈ {2, 4} × {low, medium}. Used for the conflict matrix, the classification allowlist and tuning.
- **Held-out:** N = 8 × high, with OPTIMIZE.
  - Its scenario file (seeds, durations, op mix) is written by another session and frozen under the lab's held-out
    rules.
  - It runs once. A failed run is reported; it is rerun only for an infrastructure fault, and both runs are reported.

## Runner

`runner.run(scenario, arm, base_dir)`:

1. **Prepare.** Recreate the tables as `harness`, check with the lab guard that both are stored in the lab bucket, and
   seed the counters.
2. **Launch.** One process per writer (plus the maintenance loop), each with its own connection and op log. All start
   at one barrier.
3. **Write phase.** Writers send ops for a fixed time: 45 s, tuned on dev and frozen.
4. **Drain.** Writers stop sending and in-flight ops finish, up to 30 s. A process still running after that is killed,
   and its open op stays unknown.
5. **Collect.** Read both tables back, grade, and build one results row: config, seed, counts, CCT and the secondary
   metrics. The row's denominator is the time from the barrier until the last writer ended.
   - Secondary metrics: attempts, latency p50/p95, abort and unknown rates, error classes, the longest ledger, write
     amplification from `$snapshots`, the share of acked ops whose query id is in a snapshot summary (question 4),
     and the maintenance outcomes.
   - Each run keeps its op logs and `result.json` in its own directory. Rows go to `lab/results/` through the lab's
     `results` module once that exists.

Where it runs:
- **Dev:** the whole run goes inside the lab network, `lab/stack.sh py exp -- python -m e7_concurrency.dev …`.
- **Held-out:** only through the lab runner (`make lab-run EXP=e7_concurrency SPLIT=holdout`), which checks the
  freeze and the ledger on the host. Its adapter (`experiment.py`) then starts one `py` container per (scenario, arm,
  iteration), with the scenario on stdin, and reads back that run's `result.json`.
- The container mounts the working-tree bytes of `lab/` and `lakeops/` as they are when it starts, which are the bytes
  the freeze check hashed just before.
- The writers' Trino statements go through their own connections, so they are not in the harness's
  `statements.jsonl`. The op logs are the record of what each writer sent.

Fault injection for the lab self-tests uses the same runner (`Kill`). Each configuration stays under about 10
minutes.

Measurement:
- warm-up, then the arms in ABBA order;
- ≥ 7 runs per config on dev, ≥ 10 on held-out;
- median with IQR and range, and a bootstrap CI.

When two arms' intervals overlap, the result is reported as **not distinguishable**, not as a win.

## Questions answered by measurement

Nothing below is assumed. Each answer will be filled in here with its evidence: run ids, error text, and the dates of
any docs read.

| # | Question | Answer |
|---|---|---|
| 1 | Does Trino 483 detect a concurrent MERGE on the same key, and with which error? Does it honour the table's `write.merge.isolation-level`? | pending |
| 2 | OPTIMIZE racing a MERGE on `counters`: does one fail, or does a deleted row come back? | pending |
| 3 | Which commit-retry settings does Trino honour: table `commit.retry.*`, a catalog setting? | pending |
| 4 | Do Trino's snapshot summaries carry the query id (`trino_query_id`)? That would resolve unknown ops by query id | pending |
| 5 | Does Trino finish a write after its client is killed, and after how long does it abandon one? | pending |
| 6 | PyIceberg 0.12 retries a commit that lost a race (`commit.retry.num-retries`, 4 by default) and checks the retry against `write.*.isolation-level` (serializable by default), read in its source. Does a racing `upsert` (W2-py) still lose an add, or is it refused? | pending |
| 7 | Does the trino client's own re-send of a statement (3 attempts by default, after a timeout or a lost connection) write an op twice? | pending |

**Conflict matrix** (to come): pairs of concurrent operations (INSERT, MERGE on the same key, MERGE on another key,
OPTIMIZE on the table, OPTIMIZE on a partition, PyIceberg append, PyIceberg upsert) × the outcome observed: both
commit, retried then committed, one definite-fail, or an anomaly.

## Threats to validity

- **Ledger overhead.** `applied` grows with every op on a key, so a MERGE costs more as a run goes on. That is fair
  across arms but a confound for absolute throughput.
  - Fresh tables per run, and enough keys that the longest ledger stays at a few hundred entries. The runner reports
    the maximum.
  - On dev, the counter workload is run with and without the ledger at the same config. Without it, a run is graded
    only by bounds: per key, Σ acked ≤ value ≤ Σ acked + Σ unknown, since deltas are positive. The difference is
    reported.
- **Single-node Trino** with 2 CPUs and a 3 GiB limit (`lab/compose.yml`). Parallelism tops out early, so the results
  are not extrapolated to a cluster.
- **The writers share one `py` container**, fixed at 2 CPUs and 2 GiB, beside Trino's own 2 CPUs. Both arms run the
  same way, but a client-side CPU limit can cap the throughput of either arm at high writer counts.
- **Unknown ops weaken the oracle.** Above 5% unknown, the client timeout is raised and the rate is reported.
- **Trino may serialise commits itself**, so no conflicts appear. That is reported as a finding, and contention is
  raised with the direct PyIceberg writers.
- **The classification allowlist is tuned on dev.** It is frozen with the rest of the method before held-out.

## Failure cases

Pending: at least one, from the dev or held-out runs.

## How to rerun

The unit tests need no lab. They run in `make lab-check` (CI job `lab`), or alone:

```sh
cd lab && uv run --frozen pytest experiments/e7_concurrency
```

Runs on the lab: pending the runner.
