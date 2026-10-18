"""lab.report: the README tables and the SVG chart from result files."""

import xml.etree.ElementTree as ET
from pathlib import Path

from lab.report import write_report
from lab.results import Row, collect_metadata, write_run

BASE = [1.00, 1.02, 0.98, 1.01, 0.99, 1.03, 1.00]


def result(tmp_path: Path) -> Path:
    rows = [Row("baseline", "fragmented", "latency", v, "s", i) for i, v in enumerate(BASE)]
    rows += [Row("method", "fragmented", "latency", v * 0.6, "s", i) for i, v in enumerate(BASE)]
    rows += [Row("noop", "fragmented", "latency", v * 1.005, "s", i) for i, v in enumerate(BASE)]
    rows += [Row("baseline", "fragmented", "files", 40.0, "count")]
    return write_run(rows, collect_metadata("e0_toy", "dev", 3, "warm", run_id="r20261018t014000"), tmp_path)


def test_the_report_has_summaries_comparisons_and_a_chart(tmp_path: Path):
    report = write_report([result(tmp_path)], "latency", tmp_path / "out", "dev", baseline="baseline", noise_floor=0.03)
    text = report.read_text()
    assert text.startswith("# e0_toy: latency (dev)")
    assert "r20261018t014000 (dev, " in text
    assert "| fragmented | baseline | 7 | 1 |" in text
    assert "| fragmented | method | 0.600 |" in text and text.count("| yes |") == 1  # method: beyond the floor
    assert "| fragmented | noop | 1.005 |" in text and "| no |" in text  # noop: within it
    assert "files" not in text.split("![")[0].split("Time scale")[1]  # only the chosen metric
    svg = ET.fromstring((tmp_path / "out/latency.svg").read_text())  # noqa: S314 (the SVG this test just wrote)
    assert len(svg.findall("{http://www.w3.org/2000/svg}circle")) == 3


def test_dev_and_holdout_are_never_pooled(tmp_path: Path):
    import pytest

    path = result(tmp_path)
    with pytest.raises(ValueError, match="no holdout rows"):
        write_report([path], "latency", tmp_path / "out", "holdout")
