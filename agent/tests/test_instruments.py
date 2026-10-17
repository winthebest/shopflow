"""The metric table in agent/instruments.py: unique names, Prometheus names that follow its stated rule, and
explicit buckets on every histogram (an SLO target has to be a bucket boundary)."""

from agent.instruments import ALL


def test_names_are_unique():
    assert len({i.name for i in ALL}) == len({i.prometheus for i in ALL}) == len(ALL)


def test_prometheus_names_follow_the_translation_rule():
    for instrument in ALL:
        expected = instrument.name.replace(".", "_")
        expected += "_seconds" if instrument.unit == "s" else ""
        expected += "_total" if instrument.kind == "counter" else ""
        assert instrument.prometheus == expected, instrument.name
        assert instrument.unit == "s" or instrument.unit.startswith("{"), "a unit Prometheus would append"


def test_histograms_have_explicit_increasing_buckets():
    for instrument in ALL:
        if instrument.kind == "histogram":
            assert instrument.buckets == tuple(sorted(set(instrument.buckets))) != (), instrument.name
        else:
            assert instrument.buckets == (), instrument.name
