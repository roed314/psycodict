# -*- coding: utf-8 -*-
"""
The meta_tables flags mean what B0 of the rc3 review says they mean, after
every supported write.

- ``total`` is the exact live row count, independent of stat saving.
- ``out_of_order`` records whether ascending id still matches the sort.
- ``stats_valid`` records whether the cached statistics agree with the data.

These are checked directly against meta_tables (not just the Python object), so
a write that updates one but forgets the other is caught.
"""
import pytest

from psycopg.sql import SQL

from conftest import sample_row


def flags(table):
    cur = table._execute(
        SQL(
            "SELECT total, out_of_order, stats_valid FROM meta_tables WHERE name = %s"
        ),
        [table.search_table],
    )
    total, out_of_order, stats_valid = cur.fetchone()
    return {"total": total, "out_of_order": out_of_order, "stats_valid": stats_valid}


@pytest.fixture
def ordered(table_factory):
    """An ordered, stats-valid table of 100 rows sorted by n."""
    table = table_factory(sort=["n"])
    table.insert_many([sample_row(i) for i in range(100)])
    table.stats.saving = True
    table.resort()
    table.stats.refresh_stats()
    assert flags(table)["total"] == 100
    return table


# ---------------------------------------------------------------------------
# total is exact after every operation
# ---------------------------------------------------------------------------

def test_insert_many_keeps_total_exact(ordered):
    ordered.insert_many([sample_row(1000), sample_row(1001)], restat=False)
    assert flags(ordered)["total"] == 102
    assert ordered.count() == 102


def test_delete_keeps_total_exact(ordered):
    ordered.delete({"n": {"$lt": 10}}, restat=False)
    assert flags(ordered)["total"] == 90
    assert ordered.count() == 90


def test_upsert_insert_keeps_total_exact(ordered):
    ordered.upsert({"label": "brand_new"}, {"n": 5000})
    assert flags(ordered)["total"] == 101


def test_upsert_update_does_not_change_total(ordered):
    ordered.upsert({"label": "l5"}, {"n": 5000})
    assert flags(ordered)["total"] == 100


def test_copy_from_keeps_total_exact(ordered, table_factory, tmp_path):
    source = table_factory(sort=["n"])
    source.insert_many([sample_row(i) for i in range(200, 205)])
    dump = tmp_path / "d.txt"
    source.copy_to(str(dump), include_id=False)
    ordered.copy_from(str(dump), restat=False)
    assert flags(ordered)["total"] == 105


def test_total_is_exact_without_stat_saving(table_factory):
    """
    total is maintained even when the stats object is not saving custom counts.
    """
    table = table_factory(sort=["n"])
    assert table.stats.saving is False
    table.insert_many([sample_row(i) for i in range(7)])
    assert flags(table)["total"] == 7
    table.delete({"n": 0})
    assert flags(table)["total"] == 6


def test_reload_recounts_total(ordered, tmp_path):
    dump = tmp_path / "d.txt"
    ordered.copy_to(str(dump), include_id=False)
    # dump holds 100 rows; reload replaces the table with them
    ordered.reload(str(dump))
    assert flags(ordered)["total"] == 100


# ---------------------------------------------------------------------------
# order-flag transitions, per B0's rules
# ---------------------------------------------------------------------------

def test_delete_preserves_order(ordered):
    ordered.delete({"n": {"$lt": 5}})
    assert flags(ordered)["out_of_order"] is False


def test_insert_many_breaks_order(ordered):
    ordered.insert_many([sample_row(1000)], restat=False)
    assert flags(ordered)["out_of_order"] is True


def test_update_of_a_non_sort_column_still_marks_out_of_order(ordered):
    """
    update() is conservative: it marks order broken on any update.  (B0 notes
    this is safe if pessimistic; the important half is that it is never
    falsely left ordered.)
    """
    ordered.update({"n": 5}, {"label": "changed"}, restat=False)
    assert flags(ordered)["out_of_order"] is True


def test_resort_restores_order(ordered):
    ordered.insert_many([sample_row(1000)], restat=False)
    assert flags(ordered)["out_of_order"] is True
    ordered.resort()
    assert flags(ordered)["out_of_order"] is False


def test_inplace_update_of_a_sort_key_breaks_order(ordered, tmp_path):
    """
    B0: an in-place update_from_file that changes a sort key must set
    out_of_order, since it did not rebuild.
    """
    dump = tmp_path / "u.txt"
    with open(dump, "w") as F:
        F.write("label|n\ntext|integer\n\nl5|9999\n")
    ordered.update_from_file(str(dump), inplace=True, restat=False)
    assert flags(ordered)["out_of_order"] is True


# ---------------------------------------------------------------------------
# stats_valid transitions
# ---------------------------------------------------------------------------

def test_writes_invalidate_stats(ordered):
    ordered.insert_many([sample_row(1000)], restat=False)
    assert flags(ordered)["stats_valid"] is False


def test_refresh_stats_revalidates(ordered):
    ordered.insert_many([sample_row(1000)], restat=False)
    ordered.stats.refresh_stats()
    assert flags(ordered)["stats_valid"] is True
