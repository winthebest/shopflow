"""Tables and a chart for an experiment's README, from its result files (lab.results).

For one metric: a summary per case and arm (n, median, IQR, p95, bootstrap CI of the median), and, when a baseline
arm is named, each arm's ratio of medians to the baseline with its CI, marked as a difference only when the CI lies
beyond the noise floor. The chart is a plain SVG: per case and arm, the median as a dot and its CI as a line.

Usage: uv run --project lab python -m lab.report <result.parquet>... --metric latency --split dev [--baseline arm]
       [--noise-floor 0.05] [--out DIR]   (writes DIR/report.md and DIR/<metric>.svg; default: next to the first file)
"""

import argparse
from collections import defaultdict
from html import escape
from pathlib import Path

from lab.results import read_run
from lab.timing import Summary, compare, summarize


def samples(rows: list[dict], metric: str) -> dict[tuple[str, str], list[float]]:
    """(case, arm) -> values of `metric`, in file order."""
    found: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        if row["metric"] == metric:
            found[(row["case"], row["arm"])].append(row["value"])
    return dict(found)


def _g(x: float) -> str:
    return f"{x:.4g}"


def summary_table(groups: dict[tuple[str, str], list[float]], unit: str) -> str:
    lines = [
        f"| Case | Arm | n | Median ({unit}) | IQR | p95 | 95% CI of the median |",
        "|---|---|---|---|---|---|---|",
    ]
    for (case, arm), values in sorted(groups.items()):
        s = summarize(values)
        ci = f"{_g(s.ci_low)} to {_g(s.ci_high)}"
        lines.append(f"| {case} | {arm} | {s.n} | {_g(s.median)} | {_g(s.iqr)} | {_g(s.p95)} | {ci} |")
    return "\n".join(lines)


def comparison_table(groups: dict[tuple[str, str], list[float]], baseline: str, noise_floor: float) -> str:
    lines = [
        f"| Case | Arm | Median / {baseline} | 95% CI | Beyond the noise floor (±{noise_floor:.1%}) |",
        "|---|---|---|---|---|",
    ]
    for (case, arm), values in sorted(groups.items()):
        if arm == baseline or (case, baseline) not in groups:
            continue
        c = compare(groups[(case, baseline)], values)
        beyond = c.ci_high < 1 - noise_floor or c.ci_low > 1 + noise_floor
        lines.append(
            f"| {case} | {arm} | {c.ratio:.3f} | {c.ci_low:.3f} to {c.ci_high:.3f} | {'yes' if beyond else 'no'} |"
        )
    return "\n".join(lines)


def svg_chart(summaries: dict[tuple[str, str], Summary], metric: str, unit: str) -> str:
    """One row per (case, arm): the median as a dot, its CI as a line, on a shared linear axis from 0."""
    row_h, left, width, top = 22, 220, 420, 30
    high = max(s.ci_high for s in summaries.values()) or 1.0
    x = lambda v: left + width * v / high  # noqa: E731
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{left + width + 80}" '
        f'height="{top + row_h * len(summaries) + 30}" font-family="sans-serif" font-size="12">',
        f'<text x="{left}" y="18">{escape(metric)} ({escape(unit)}): median and 95% CI</text>',
    ]
    for i, ((case, arm), s) in enumerate(sorted(summaries.items())):
        y = top + row_h * i + row_h / 2
        parts += [
            f'<text x="{left - 8}" y="{y + 4}" text-anchor="end">{escape(case)} · {escape(arm)}</text>',
            f'<line x1="{x(s.ci_low):.1f}" y1="{y}" x2="{x(s.ci_high):.1f}" y2="{y}" stroke="#555" stroke-width="2"/>',
            f'<circle cx="{x(s.median):.1f}" cy="{y}" r="4" fill="#1f6feb"/>',
            f'<text x="{x(s.ci_high) + 6:.1f}" y="{y + 4}">{_g(s.median)}</text>',
        ]
    axis_y = top + row_h * len(summaries) + 8
    parts += [
        f'<line x1="{left}" y1="{axis_y}" x2="{left + width}" y2="{axis_y}" stroke="#999"/>',
        f'<text x="{left}" y="{axis_y + 16}">0</text>',
        f'<text x="{left + width}" y="{axis_y + 16}" text-anchor="end">{_g(high)}</text>',
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


def write_report(
    paths: list[Path], metric: str, out: Path, split: str, baseline: str | None = None, noise_floor: float = 0.0
) -> Path:
    """The report of `split`'s runs among `paths` (dev and held-out are never pooled), for one experiment."""
    rows, metas = [], []
    for path in paths:
        file_rows, meta = read_run(path)
        if meta["split"] == split:
            rows += file_rows
            metas.append(meta)
    if len({m["experiment"] for m in metas}) > 1:
        raise ValueError(f"results of several experiments: {sorted({m['experiment'] for m in metas})}")
    groups = samples(rows, metric)
    if not groups:
        raise ValueError(f"no {split} rows with metric {metric!r} in {[str(p) for p in paths]}")
    units = {row["unit"] for row in rows if row["metric"] == metric}
    unit = units.pop() if len(units) == 1 else "mixed units"
    runs = ", ".join(
        f"{m['run_id']} ({m['split']}, {m['git_sha'][:12]}{', dirty' if m['git_dirty'] else ''})" for m in metas
    )
    text = [
        f"# {metas[0]['experiment']}: {metric} ({split})",
        "",
        f"Runs: {runs}. Time scale: {metas[0]['timescale']}.",
        "",
    ]
    text += [summary_table(groups, unit), ""]
    if baseline:
        text += [comparison_table(groups, baseline, noise_floor), ""]
    text += [f"![{metric}]({metric}.svg)", ""]
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{metric}.svg").write_text(svg_chart({k: summarize(v) for k, v in groups.items()}, metric, unit))
    report = out / "report.md"
    report.write_text("\n".join(text))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--metric", required=True)
    parser.add_argument("--split", required=True, choices=["dev", "holdout"])
    parser.add_argument("--baseline")
    parser.add_argument("--noise-floor", type=float, default=0.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or args.results[0].parent
    print(write_report(args.results, args.metric, out, args.split, args.baseline, args.noise_floor))


if __name__ == "__main__":
    main()
