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
