# 0421. Lab experiments tune on dev scenarios and run a frozen, encrypted held-out split once, every attempt on record

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-data

## Context

The research brief asks for a development scenario to tune on and at least one held-out scenario to check
generalization. It voids fabricated numbers. Here the experiments are built largely by agent sessions, so the main
risk is quiet overfitting: tuning on held-out results, peeking at the scenarios, changing code after seeing them, or
rerunning until a number looks good. Each of these happens without anyone deciding to cheat. A rule in a document
does not stop them; the tooling has to.

## Decision

1. **Two splits per experiment.** `lab/scenarios/<exp>/dev.yaml` is in the clear, to tune on.
   `lab/scenarios/<exp>/holdout.sops.yaml` is written by the user or another session, never by the lane that tunes
   the method.
2. **Held-out scenarios are encrypted with the public key only.** `python -m lab.scenarios encrypt` reads the single
   age recipient of the repository's `.sops.yaml` and runs `sops -e --age <recipient>` with `--config /dev/null`, so
   no creation rule adds a key. Agent workspaces cannot decrypt; any `sops -d` of real held-out data needs the user's
   approval. The held-out run decrypts in memory (`sops -d`, JSON on stdout), and nothing is written to disk.
3. **Freeze before the held-out run.** `make lab-freeze` hashes every tracked file under `lab.freeze.FROZEN`: the
   harness and its stack configuration, `lakeops/`, `agent/`, `evals/`, `policies/`, the lockfiles, and the
   experiment's code and scenarios. It refuses uncommitted or untracked files there, and writes
   `lab/freezes/<exp>.json`.
4. **The runner enforces it** (`lab.runner`, `make lab-run SPLIT=holdout`, on the host, where git is). It refuses
   without a freeze, on any changed frozen file (and names it), on uncommitted files under the frozen paths, and while
   the freeze file or the ledger has uncommitted changes, or while their commit is not pushed (HEAD on no
   remote-tracking branch), and until a dev run has finished with the code at this freeze
   (`lab/freezes/<exp>.dev.jsonl`), which proves the frozen code loads and runs before an attempt can be burned. None
   of the experiment's code runs before these checks; it is then loaded from the bytes the freeze hashed, as module
   `<exp>.experiment` of package `<exp>` (relative and absolute imports work), with the repository root last on the
   path for `agent/` and `evals/`.
5. **Once, with a ledger.** Under a lock, each held-out attempt is checked against and appended to
   `lab/freezes/<exp>.ledger.jsonl` as started before decryption, then as finished (with its result file) or failed
   (with the error). The ledger is committed and pushed after every attempt, since the next one requires it: the
   user runs the held-out from a separate clone, which then sees every earlier attempt.
   - After a finished attempt: never again.
   - After failed or unfinished attempts: only with a named infrastructure failure (`RERUN_REASON`).
   - Every attempt is reported. The freeze cannot change once an attempt exists.
6. **Publish after the final run.** `make lab-publish-holdout`, run with the user's key, writes the decrypted
   scenarios so others can reproduce the run. It is refused before a finished attempt.
7. **Results carry their provenance.** Each run writes one Parquet file, once (`lab.results`). It records the git sha
   and dirty flag, the freeze hash, seed, split, time scale and versions.

Tested on a toy experiment with known answers (`lab/experiments/e0_toy`, `lab/tests/test_runner.py`), in a throwaway
git repository and with a throwaway age key confined to the test (key, `HOME` and `XDG_CONFIG_HOME` in the test's
temporary directory). The tests cover:
- dev runs at will;
- the held-out runs once and gives the known answers;
- changed and uncommitted code are refused;
- a failed attempt (wrong key) needs a named infrastructure failure, and both attempts stay in the ledger;
- publishing only after the run;
- the repository's toy held-out encrypted to the repository recipient only.

## Alternatives considered

| Option | Why not |
|---|---|
| Held-out scenarios in the clear, by convention | An agent reads every file in its workspace; the convention fails without anyone noticing |
| A git hash as the freeze | It misses uncommitted changes, and commits to unrelated paths would invalidate it; hashing the files that matter does neither |
| Allow reruns, report the best | That turns the held-out into a second dev set |
| A separate repository for held-out data | Same protection as encryption here, but more moving parts; the ledger and freeze would still be needed |

## Consequences

- Positive: overfitting through the held-out needs a deliberate step the tooling refuses or records. Results show
  which code produced them.
- Negative / risks:
  - The held-out run needs the user (the key), once per experiment.
  - Every change under the frozen paths after a freeze, even a comment, blocks the held-out until the next freeze.
  - CI has no sops, so the decryption tests run locally only; the refusal tests also run in CI.
  - The ledger is a file in the repository. A held-out attempt starts only from a pushed commit with the freeze and
    the ledger, so a fresh clone sees every attempt recorded before it; two clones starting at the same moment from
    the same commit are not prevented (the user holds the key and runs it once).
- When to revisit: experiments need several held-out sets per freeze, or a held-out run must be resumable midway.
