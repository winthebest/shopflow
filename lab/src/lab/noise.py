"""The lab's noise floor: one query on one Iceberg table, timed as both arms of an A/A comparison (lab.timing).

Two fixed cases, because the floor depends on how long a query runs (a short one is dominated by fixed overhead):
- `short`: a group-by over 2 million rows (lab_exp.lab_noise.t_2m), about 0.06 s on the lab;
- `long`: a group-by with a distinct count over 20 million rows (lab_exp.lab_noise.t_20m), about 0.44 s.
Tables are built once (Parquet, format v2). Both arms run the same statement, in ABBA order after a warm-up; each
run's time is Trino's own elapsed time (lab.trino_client.query_stats). The result file (lab/results/e0_noise/) holds
every sample and each case's floor: the relative difference this lab cannot tell from zero, which
docs/research/protocol.md quotes.

Usage: make lab-noise [RUNS=25]   (as harness, on the lab stack, on a quiet host)
"""

import argparse

from lab.guard import guarded_connect
from lab.results import Row, collect_metadata, write_run
from lab.timing import RUNS, WARMUP, measure, noise_floor, summarize
from lab.trino_client import Settings, query, query_stats, run_measured, server_info

# case -> (table, outer x inner rows: Trino's sequence() returns at most 10 000 entries, query)
CASES = {
    "short": ("lab_exp.lab_noise.t_2m", 2000, 1000, "SELECT k, count(*), sum(x) FROM {t} GROUP BY k"),
    "long": (
        "lab_exp.lab_noise.t_20m",
        10000,
        2000,
        "SELECT k, count(*), sum(x), approx_distinct(x) FROM {t} GROUP BY k",
    ),
}


def build(conn, table: str, outer: int, inner: int) -> None:
    query(
        conn,
        f"CREATE TABLE IF NOT EXISTS {table} WITH (format_version = 2, format = 'PARQUET') AS "  # noqa: S608
        f"SELECT a * {inner} + b AS x, (a * {inner} + b) % 97 AS k FROM UNNEST(sequence(0, {outer - 1})) AS t(a) "
        f"CROSS JOIN UNNEST(sequence(0, {inner - 1})) AS u(b)",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="the lab's noise floor (A/A)")
    parser.add_argument("--runs", type=int, default=RUNS)
    parser.add_argument("--warmup", type=int, default=WARMUP)
    args = parser.parse_args()
    settings = Settings.from_env()
    conn = guarded_connect("harness", settings=settings, max_attempts=1)
    meta = collect_metadata("e0_noise", "dev", 0, "warm", trino_info=server_info("harness", settings))
    query(conn, "CREATE SCHEMA IF NOT EXISTS lab_exp.lab_noise")
    rows = []
    for case, (table, outer, inner, sql) in CASES.items():
        build(conn, table, outer, inner)
        statement = sql.format(t=table)

        def timed(statement: str = statement) -> float:
            _, query_id = run_measured(conn, statement)
            return query_stats(query_id, "harness", settings)["elapsed_s"]

        samples = measure({"A": timed, "A_again": timed}, runs=args.runs, warmup=args.warmup)
        floor = noise_floor(samples["A"], samples["A_again"])
        rows += [Row(arm, case, "elapsed", v, "s", i) for arm, values in samples.items() for i, v in enumerate(values)]
        rows.append(Row("A_vs_A_again", case, "noise_floor", floor, "ratio"))
        for arm, values in samples.items():
            s = summarize(values)
            print(f"{case} {arm}: n={s.n} median={s.median:.4f}s IQR={s.iqr:.4f} p95={s.p95:.4f} CV={s.cv:.3f} "
                  f"CI=[{s.ci_low:.4f}, {s.ci_high:.4f}]")  # fmt: skip
        print(f"{case} noise floor: {floor:.4f} ({floor:.1%})")
    print(f"result: {write_run(rows, meta)}")


if __name__ == "__main__":
    main()
