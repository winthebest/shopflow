# Research protocol for lab experiments

How every lab experiment is run and reported, so its numbers can be trusted and repeated. It applies the AICB Data
Lakehouse Open Research Challenge brief (student brief, read 2026-10-18), which has three hard gates:
- no baseline caps the primary metric;
- no runnable experiment caps the total;
- silent corruption or fabricated numbers void the result.

Decisions behind it:
- ADR 0417: the catalog-scoped safety boundary;
- ADR 0419: the lab stack, separate from the platform;
- ADR 0420: compressed time;
- ADR 0421: dev/held-out discipline.

## 1. The order of work

1. **Problem and hypothesis first.** Write the hypothesis as a falsifiable claim with its mechanism, e.g. "compaction
   pays off only when the expected query savings exceed the rewrite cost". Write it in the experiment's README before
   any code.
2. **Baseline.** Choose the simple thing a team would do today (a fixed threshold, the best cron within budget, an
   agent without a guard). Run it under exactly the conditions of the method. A method without a baseline is not
   evidence.
3. **Metric.** Use the brief's formula for the topic, written out in the README. Add secondary metrics as the brief
   lists them; never replace the primary one.
4. **Dev scenarios.** Tune on these only (`lab/scenarios/<exp>/dev.yaml`). Iterate there as often as needed.
5. **Freeze, then held-out once.** See §5. The held-out result is the result; dev numbers are reported as dev.
6. **At least one failure case.** Find where the method loses or breaks, why, and what was not tested. A method that
   never fails was not tested hard enough.
7. **Real numbers only.** Every number in a README comes from a result file in `lab/results/<exp>/` and is
   reproducible with the command given there.

## 2. Where experiments run

- The lab stack (`make lab-up`; `lab/compose.yml`, ADR 0419), never the platform's `lake`. The harness guard
  (`lab.guard`) refuses the platform's catalogs, warehouses and buckets. Results need Trino's node environment
  `lab`, so they never come from k3d.
- Each arm runs as its identity in its catalogs (ADR 0417):

  | Arm | Identity | Catalog |
  |---|---|---|
  | Research harness | `harness` | `lab_exp` (floors 0s) |
  | Planner | `agent_planner` | `lab_ro` |
  | Executor | `executor` | `lab_ops`, `lab_sbx` |
  | Baseline with broad writes | `baseline_writer` | `lab_base_<run>`, one per run (`make lab-base RUN=<run>`) |

- Each arm sends its SQL through `context.session(identity)`, an `lab.session.ArmSession`, and through nothing else:
  - the guard checks the connection;
  - the client never resubmits a statement;
  - the time scale rewrites the statement;
  - the statement is logged to the run's `statements.jsonl`.
- The scorer's view of the tables comes from `context.capture(label, catalog, namespace)`, which writes
  `lakeops.state` to `states.jsonl`. It reads as `observer` through each zone's read-only catalog (`lab_ro`,
  `lab_exp_ro`, `lab_sbx_ro`, `lab_base_ro_<run>`). No arm runs as `observer` and no arm sees its queries.
  - Capture a task's namespace and its bait before and after each arm (`lab.prodlike.capture_task`, labels
    `before:<task>` and `after:<task>`).
  - The scorer judges; the harness only records.
- Writers that commit through the REST catalog directly (PyIceberg, as the platform's sinks do) run with
  `lab/stack.sh py <zone> -- …` (`make lab-py`): a container on the lab network with only that zone's catalog
  credentials and fixed CPU and memory.
- Tables are created with `WITH (format_version = 2)` (ADR 0407).
- One lab stack runs at a time. Book Docker time with sf-data. Measure with nothing heavy beside the stack: no k3d
  slot and no local LLM.
- Drop large tables between runs, never during one. Dropping a table of hundreds of files makes Polaris purge them in
  the background, and its S3 client can then time out other commits. Such a commit fails with "Cannot determine
  whether the commit was successful" (seen on the lab on 2026-10-18 after dropping four table states of about 1600
  files each). A harness that recreates tables for every run renames the previous ones aside (or varies the names)
  instead of dropping them: dozens of drops a minute apart kept Polaris's S3 client saturated for many minutes in an
  experiment's dev pass, until `lab/stack.sh reset`, which removes the volumes without any purge.

## 3. Time

- **Compressed time.** Experiments run at the published scale `lab.timescale.PUBLISHED`: 1 simulated day = 1 real
  minute (ADR 0420). Snapshot and file ages, retention thresholds, floors and detection delays are simulated time.
  Query and procedure durations are real time.
- **Floors.** `lab_ops`, `lab_sbx`, `lab_ro` and `lab_base_<run>` refuse thresholds under 7 simulated days (`7m`).
- **Reporting.** Each README states the scale it ran at; every result file records it.

## 4. Measuring

- **Control.** Keep these the same for every arm and record them in each result file (`lab.results`):
  - dataset (scale factor, partitioning, seed);
  - engine version and images;
  - hardware;
  - Docker limits;
  - cache mode.
  Change one thing at a time.
- **Timing** (`lab.timing`):
  - warm-up runs first, not measured;
  - at least 7 measured runs per arm;
  - arms alternate in mirrored ABBA blocks.
  - Report median, IQR, p95 and a 95 % bootstrap CI of the median. Never report a mean alone.
- **Where times come from.** Trino's own measurements (`lab.trino_client.query_stats`: elapsed, CPU, input bytes)
  when the question is the engine's work; wall time around the call when it is the whole operation.
- **Noise floor.** An A/A comparison (the same arm measured twice, in ABBA order) gives the relative difference the
  lab cannot tell from zero. A difference between arms counts only when its CI lies entirely beyond the noise floor.
  - Measured on the lab, 2026-10-18 05:21–05:23Z, quiet host (no k3d, no local model, no other Docker job), Trino
    483 at 2 CPUs and 3 GiB (`make lab-noise`; `lab/results/e0_noise/r20261018t052233325-9b4b5cf33b42.parquet`).
    | Query | Warm-up, runs per arm | Median | CV | Noise floor |
    |---|---|---|---|---|
    | short: group-by over 2 M rows | 10, 25 | 0.064 s | 7–10 % | **4.8 %** |
    | long: group-by + distinct count over 20 M rows | 10, 25 | 0.44 s | 9–11 % | **8.8 %** |
    | the same, warm-up 2 and 15 runs | 2, 15 | 0.07–0.57 s | 8–31 % | 14–31 % |
  - With a short warm-up the floor is useless (Trino's JIT keeps warming up over several runs), so `lab.timing` uses
    10 warm-up and 25 measured runs by default. For a sub-second query, a difference under about 9 % is noise.
- **When the noise is high.** If the CV of an arm exceeds 10 %, find the cause before reporting: a neighbour on the
  machine, a cold cache, too few runs.

## 5. Dev and held-out

- **Who writes the held-out scenarios.** The user or another session, never the lane that tunes the method. They
  are encrypted with SOPS to the repository's age recipient, public key only: `python -m lab.scenarios encrypt
  <plain.yaml> <exp>` writes `lab/scenarios/<exp>/holdout.sops.yaml`. The plaintext stays outside git.
- **Agent workspaces cannot read them.** The age key stays with the user. Any decryption (`sops -d`) needs the user's
  approval.
- **Freeze.** `make lab-freeze EXP=<exp>` hashes everything that can change a result (`lab.freeze.FROZEN`):
  - the harness and its stack configuration;
  - `lakeops/`, `agent/`, `evals/`, `policies/`;
  - the lockfiles;
  - the experiment's code and scenarios.
  It hashes committed content only and writes `lab/freezes/<exp>.json`.
- **Held-out run.** `make lab-run EXP=<exp> SPLIT=holdout`, from the host, refuses:
  - without a freeze;
  - when a frozen file changed, naming it;
  - when uncommitted files sit under the frozen paths;
  - while the freeze file or the ledger has uncommitted or unpushed changes. Commit and push both after every step:
    the held-out run comes from the user's own clone. The runner fetches first, so a stale remote-tracking ref cannot
    pass for a pushed commit, and refuses when it cannot reach the remote.
  None of the experiment's code runs before these checks.
- **Held-out runs once.** Every attempt is written to `lab/freezes/<exp>.ledger.jsonl` before anything is decrypted.
  - An attempt that finished ends the experiment's held-out.
  - An attempt that failed is reported like a finished one.
  - A new attempt is allowed only after an infrastructure failure, named with `RERUN_REASON=…`; both attempts are
    reported.
  - The freeze cannot change once an attempt exists.
- **Publish.** After the final run, `make lab-publish-holdout EXP=<exp>` (with the user's key) writes the decrypted
  scenarios next to the encrypted file, so others can reproduce the run.
- **What is never done:** tuning on held-out results, looking at held-out scenarios before the run, re-running the
  held-out to get a better number.

## 6. Results and reports

- `make lab-run` writes one Parquet file per run, `lab/results/<exp>/<run_id>-<git sha>.parquet`, written once.
  Next to it is the run's bundle: `<run_id>-<git sha>.statements.jsonl` and `.states.jsonl`. The metadata is taken
  when the run starts, and records:
  - git sha and dirty flag;
  - freeze hash;
  - seed, split, time scale, cache mode;
  - versions and image pins;
  - Trino version;
  - hardware and Docker limits.
  Lanes write results locally; only the orchestrator's workspace commits them.
- `make lab-report EXP=<exp> SPLIT=dev|holdout METRIC=<metric> BASELINE=<arm> NOISE_FLOOR=<floor>` writes the README's
  tables and an SVG chart for one split; dev and held-out runs are never pooled. Each arm's ratio to the baseline
  gets a CI and a "beyond the noise floor" mark.
- `make lab-ledger EXP=<exp>` lists every held-out attempt for the README.

## 7. The experiment's README (one page)

Use the template in [README.md](README.md). Fill in:
- problem, hypothesis, setup;
- baseline, method;
- primary metric with its formula;
- held-out result, with the result file and its ledger entries;
- at least one failure case;
- how to rerun, with commands and versions;
- the dates the cited docs and papers were read.
