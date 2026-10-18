"""lakeops.sql without a stack: table names and their quoting."""

import pytest

from lakeops.sql import TableName, table_name


def test_a_table_name_parses_and_quotes():
    t = TableName.parse("lab_exp.probe.t_1")
    assert (t.catalog, t.schema, t.table, str(t)) == ("lab_exp", "probe", "t_1", "lab_exp.probe.t_1")
    assert t.sql == '"lab_exp"."probe"."t_1"'
    assert t.metadata("snapshots") == '"lab_exp"."probe"."t_1$snapshots"'
    assert table_name(t) is t


@pytest.mark.parametrize("name", ["probe.t", "a.b.c.d", 'a."b".c', "a..c", "A.b.c", "a.b.c; DROP TABLE x"])
def test_other_names_are_refused(name: str):
    with pytest.raises(ValueError, match=r"catalog\.schema\.table"):
        TableName.parse(name)


def test_metadata_kinds_are_plain_names():
    with pytest.raises(ValueError):
        TableName.parse("a.b.c").metadata('files" UNION SELECT')
