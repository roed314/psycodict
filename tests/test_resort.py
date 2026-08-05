# -*- coding: utf-8 -*-
"""
``resort()`` rebuilds the table so ascending id follows the configured sort.

Before rc3 it was a disabled no-op (``print("resorting disabled"); return None``)
because the old implementation renumbered every id in place, which stalls
replication and does not lay the rows out on disk in order.  It is now a
physical rebuild through reload's machinery, and the row-level writes that used
to accept a ``resort=`` flag reject ``resort=True`` and point at it.
"""
import pytest

from psycopg.sql import SQL, Identifier

from conftest import sample_row


def ids_in_disk_order(table):
    """
    The ids in physical (heap) order -- ``ctid`` follows the on-disk layout.
    A resorted table must have these ascending, not just the logical ids.
    """
    cur = table._execute(
        SQL("SELECT id FROM {0} ORDER BY ctid").format(Identifier(table.search_table))
    )
    return [rec[0] for rec in cur]


@pytest.fixture
def shuffled_table(table_factory):
    """
    An id_ordered table sorted by ``n`` whose rows were inserted in an order
    that does *not* match ``n``, so its ids do not follow the sort.
    """
    table = table_factory(sort=["n"])
    order = [3, 0, 4, 1, 2, 7, 5, 6, 9, 8]
    table.insert_many([sample_row(i) for i in order])
    table.stats.saving = True
    return table


def test_resort_puts_ids_in_sort_order(shuffled_table):
    table = shuffled_table
    # before: id order does not match n order
    by_id = [rec["n"] for rec in table.search({}, ["n"], sort=[["id", 1]])]
    assert by_id != sorted(by_id)

    assert table.resort() is True

    by_id = [rec["n"] for rec in table.search({}, ["n"], sort=[["id", 1]])]
    assert by_id == sorted(by_id)


def test_resort_assigns_contiguous_ids_from_one(shuffled_table):
    shuffled_table.resort()
    ids = sorted(rec["id"] for rec in shuffled_table.search({}, ["id"]))
    assert ids == list(range(1, len(ids) + 1))


def test_resort_lays_rows_out_on_disk_in_order(shuffled_table):
    """
    The point of id-ordering is sequential disk reads: an in-place renumber
    would leave the heap order untouched.  A rebuild must not.
    """
    shuffled_table.resort()
    assert ids_in_disk_order(shuffled_table) == sorted(
        ids_in_disk_order(shuffled_table)
    )


def test_resort_marks_the_table_ordered(shuffled_table):
    shuffled_table._break_order()
    assert shuffled_table._out_of_order is True
    shuffled_table.resort()
    assert shuffled_table._id_ordered is True
    assert shuffled_table._out_of_order is False
    # and persisted
    cur = shuffled_table._execute(
        SQL("SELECT id_ordered, out_of_order FROM meta_tables WHERE name = %s"),
        [shuffled_table.search_table],
    )
    assert cur.fetchone() == (True, False)


def test_resort_preserves_the_data(shuffled_table):
    before = {rec["n"]: rec["label"] for rec in shuffled_table.search({}, ["n", "label"])}
    shuffled_table.resort()
    after = {rec["n"]: rec["label"] for rec in shuffled_table.search({}, ["n", "label"])}
    assert before == after
    assert shuffled_table.count() == len(before)


def test_resort_keeps_a_backup(shuffled_table):
    shuffled_table.resort()
    # reload keeps the previous table as _old1
    assert shuffled_table._table_exists(shuffled_table.search_table + "_old1")


def test_resort_rebuilds_indexes(shuffled_table):
    shuffled_table.resort()
    built = set(shuffled_table._list_built_indexes())
    assert shuffled_table.search_table + "_pkey" in built


def test_resort_reports_nothing_to_do_when_already_ordered(shuffled_table):
    shuffled_table.resort()
    # a second resort with the table already ordered does nothing
    assert shuffled_table.resort() is False


def test_resort_force_rebuilds_even_when_ordered(shuffled_table):
    shuffled_table.resort()
    assert shuffled_table.resort(force=True) is True


def test_resort_requires_id_ordered(table_factory):
    table = table_factory(sort=["n"], id_ordered=False)
    table.insert_many([sample_row(i) for i in range(5)])
    with pytest.raises(ValueError, match="id_ordered"):
        table.resort()


def test_resort_requires_a_sort(table_factory):
    table = table_factory(sort=["n"])
    table.set_sort(None)
    table.insert_many([sample_row(i) for i in range(5)])
    with pytest.raises(ValueError, match="id_ordered|sort"):
        table.resort()


def test_resort_handles_a_descending_sort(table_factory):
    table = table_factory(sort=[("n", -1)])
    table.insert_many([sample_row(i) for i in [2, 0, 4, 1, 3]])
    table.resort()
    by_id = [rec["n"] for rec in table.search({}, ["n"], sort=[["id", 1]])]
    assert by_id == sorted(by_id, reverse=True)


def test_resort_on_an_empty_table(table_factory):
    table = table_factory(sort=["n"])
    table._break_order()
    assert table.resort() is True
    assert table.count() == 0


def test_resort_does_not_recurse_through_reload(shuffled_table, monkeypatch):
    """
    resort() reuses reload, and reload must renumber via the private helper,
    not by calling public resort() again.
    """
    calls = {"resort": 0}
    original = type(shuffled_table).resort

    def counting(self, *a, **k):
        calls["resort"] += 1
        return original(self, *a, **k)

    monkeypatch.setattr(type(shuffled_table), "resort", counting)
    shuffled_table.resort()
    assert calls["resort"] == 1  # the outer call only; reload did not re-enter


# ---------------------------------------------------------------------------
# reload still establishes order through the rebuild helper
# ---------------------------------------------------------------------------

def test_reload_orders_a_no_id_file(table_factory, tmp_path):
    table = table_factory(sort=["n"])
    table.insert_many([sample_row(i) for i in [4, 1, 3, 0, 2]])
    dump = tmp_path / "d.txt"
    table.copy_to(str(dump), include_id=False)
    table.reload(str(dump), resort=True)
    by_id = [rec["n"] for rec in table.search({}, ["n"], sort=[["id", 1]])]
    assert by_id == sorted(by_id)
    assert table._out_of_order is False


# ---------------------------------------------------------------------------
# the rebuild follows the relation it is actually rebuilding
#
# Under reload(adjust_schema=True) the _tmp relation is built from the data
# file's header, so it need not have the columns of the live table object.
# ---------------------------------------------------------------------------

def _reload_file(path, header, types, rows, sep="|"):
    """
    A reload search file: names, types, a blank line, then the rows.
    """
    with open(path, "w") as F:
        F.write(sep.join(header) + "\n")
        F.write(sep.join(types) + "\n\n")
        for row in rows:
            F.write(sep.join(row) + "\n")
    return str(path)


ROW_ORDER = [3, 1, 4, 0, 2]


@pytest.fixture
def two_col_table(table_factory):
    """
    A small id_ordered table sorted by ``n``, with rows loaded out of order.
    """
    table = table_factory(columns=[("n", "integer"), ("label", "text")], sort=["n"])
    table.insert_many([{"n": i, "label": "l%d" % i} for i in ROW_ORDER])
    return table


@pytest.fixture
def three_col_table(table_factory):
    """
    The same, plus an ``extra`` column running against ``n``.
    """
    table = table_factory(
        columns=[("n", "integer"), ("label", "text"), ("extra", "text")], sort=["n"]
    )
    table.insert_many(
        [{"n": i, "label": "l%d" % i, "extra": "e%d" % (4 - i)} for i in ROW_ORDER]
    )
    return table


def test_adjusted_schema_reload_preserves_an_added_column(db, two_col_table, tmp_path):
    """
    A column the file adds exists in the _tmp relation but not in the live
    table object.  Copying the rows by the object's columns dropped it from
    the INSERT, so every value loaded from the file came back NULL.
    """
    name = two_col_table.search_table
    path = _reload_file(
        tmp_path / "d.txt",
        ["n", "label", "extra"],
        ["integer", "text", "text"],
        [[str(i), "l%d" % i, "e%d" % i] for i in ROW_ORDER],
    )
    two_col_table.reload(path, adjust_schema=True, resort=True)

    table = db[name]
    rows = list(table.search({}, projection=["n", "label", "extra"], sort=[["id", 1]]))
    assert [rec["n"] for rec in rows] == [0, 1, 2, 3, 4]
    assert [rec["extra"] for rec in rows] == ["e0", "e1", "e2", "e3", "e4"]


def test_adjusted_schema_reload_can_drop_a_column(db, three_col_table, tmp_path):
    """
    And the other direction: a column the file leaves out is gone from the
    _tmp relation, so naming it in the copy failed with an undefined column.
    """
    name = three_col_table.search_table
    path = _reload_file(
        tmp_path / "d.txt",
        ["n", "label"],
        ["integer", "text"],
        [[str(i), "l%d" % i] for i in ROW_ORDER],
    )
    three_col_table.reload(path, adjust_schema=True, resort=True)

    table = db[name]
    assert "extra" not in table.search_cols
    rows = list(table.search({}, projection=["n", "label"], sort=[["id", 1]]))
    assert [rec["n"] for rec in rows] == [0, 1, 2, 3, 4]
    assert [rec["label"] for rec in rows] == ["l0", "l1", "l2", "l3", "l4"]


def _metafile_with_sort(table, tmp_path, sort_json, name="meta.txt"):
    """
    This table's metafile with its ``sort`` column replaced, still claiming
    to be ordered.
    """
    from psycodict.base import _meta_tables_cols

    path = tmp_path / name
    table.copy_to_meta(str(path))
    with open(path) as F:
        fields = F.read().rstrip("\n").split("|")
    fields[_meta_tables_cols.index("sort")] = sort_json
    fields[_meta_tables_cols.index("id_ordered")] = "t"
    fields[_meta_tables_cols.index("out_of_order")] = "f"
    with open(path, "w") as F:
        F.write("|".join(fields) + "\n")
    return str(path)


def test_a_metafile_reload_numbers_by_the_sort_it_installs(db, two_col_table, tmp_path):
    """
    A reload carrying a metafile replaces the sort.  Numbering the rows by the
    sort being replaced and then installing the new one asserts an ordering the
    ids do not have -- the metadata would claim ORDER BY id is ORDER BY extra
    while the ids in fact followed n.
    """
    name = two_col_table.search_table
    path = _reload_file(
        tmp_path / "d.txt",
        ["n", "label", "extra"],
        ["integer", "text", "text"],
        # extra runs against n, so ordering by one is visibly not the other
        [[str(i), "l%d" % i, "e%d" % (4 - i)] for i in ROW_ORDER],
    )
    metafile = _metafile_with_sort(two_col_table, tmp_path, '["extra"]')
    two_col_table.reload(path, metafile=metafile, adjust_schema=True, resort=True)

    table = db[name]
    assert table._sort_orig == ["extra"]
    rows = list(table.search({}, projection=["n", "extra"], sort=[["id", 1]]))
    # the added column survived the rebuild ...
    assert [rec["extra"] for rec in rows] == ["e0", "e1", "e2", "e3", "e4"]
    # ... and the ids follow the new sort, not the old one
    assert [rec["n"] for rec in rows] == [4, 3, 2, 1, 0]
    assert table._out_of_order is False

    from test_id_order_audit import audit

    assert audit.audit_table(db, name)[0] == "OK"


def test_a_metafile_reload_refuses_a_sort_the_imported_schema_lacks(db, three_col_table, tmp_path):
    """
    If the file and the metafile disagree -- the sort names a column the
    imported schema does not have -- say so before anything is dropped, rather
    than failing part way through the rebuild.
    """
    name = three_col_table.search_table
    before = list(three_col_table.search({}, projection=["n", "extra"], sort=[["n", 1]]))
    path = _reload_file(
        tmp_path / "d.txt",
        ["n", "label"],
        ["integer", "text"],
        [[str(i), "l%d" % i] for i in ROW_ORDER],
    )
    metafile = _metafile_with_sort(three_col_table, tmp_path, '["extra"]')
    with pytest.raises(ValueError, match="extra"):
        three_col_table.reload(path, metafile=metafile, adjust_schema=True, resort=True)

    # The live table is untouched, and no half-built relation is left behind.
    table = db[name]
    assert list(table.search({}, projection=["n", "extra"], sort=[["n", 1]])) == before
    assert not table._table_exists(name + "_tmp")


def test_resort_refuses_a_relation_with_no_columns_besides_id(db, table_factory):
    """
    An id-only table has nothing to sort by.  Building the copy from an empty
    column list would emit ``INSERT INTO t (id, ) SELECT ...``; refuse it with
    an explanation instead.
    """
    table = table_factory(columns=[("n", "integer")], label_col=None, sort=None)
    table.drop_column("n", force=True)
    assert table.search_cols == []

    with pytest.raises(ValueError, match="no columns besides id"):
        table._generate_sorted_ids(sort=["n"])
    # and through the public entry point it never gets that far
    with pytest.raises(ValueError, match="id_ordered"):
        table.resort()


def test_a_metafile_sort_may_be_a_direction_pair(db, two_col_table, tmp_path):
    """
    The sort arrives from the metafile as JSON, so a ``(column, -1)`` pair
    comes back as a two-element list rather than a tuple.
    """
    name = two_col_table.search_table
    path = _reload_file(
        tmp_path / "d.txt",
        ["n", "label"],
        ["integer", "text"],
        [[str(i), "l%d" % i] for i in ROW_ORDER],
    )
    metafile = _metafile_with_sort(two_col_table, tmp_path, '[["n", -1]]')
    two_col_table.reload(path, metafile=metafile, resort=True)

    table = db[name]
    assert table._sort_orig == [["n", -1]]
    assert [rec["n"] for rec in table.search({}, projection=["n"], sort=[["id", 1]])] == [
        4, 3, 2, 1, 0
    ]

    from test_id_order_audit import audit

    assert audit.audit_table(db, name)[0] == "OK"
