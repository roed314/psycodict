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


def _sort_key_update(tmp_path, name="u.txt"):
    """
    An update file moving one row's ``n``, the configured sort column.
    """
    dump = tmp_path / name
    with open(dump, "w") as F:
        F.write("label|n\ntext|integer\n\nl5|9999\n")
    return str(dump)


def test_explicit_resort_false_still_records_a_broken_order_inplace(ordered, tmp_path):
    """
    ``resort=False`` asks not to rebuild; it does not claim the sort was left
    alone.  The two used to be one variable, so passing it suppressed the
    bookkeeping as well as the rebuild and left the table asserting that
    ORDER BY id still matched the sort.
    """
    ordered.update_from_file(
        _sort_key_update(tmp_path), inplace=True, resort=False, restat=False
    )
    assert flags(ordered)["out_of_order"] is True
    assert ordered._out_of_order is True


def test_explicit_resort_false_survives_the_swap_on_the_replacement_path(ordered, tmp_path):
    """
    Same for the non-inplace path: the replacement table was not renumbered,
    so the table that goes live is out of order and must say so afterwards.
    """
    ordered.update_from_file(str(_sort_key_update(tmp_path)), resort=False, restat=False)
    assert flags(ordered)["out_of_order"] is True


def test_a_resorted_replacement_is_marked_ordered(ordered, tmp_path):
    """
    The other half: a replacement that really was renumbered in sort order may
    claim to be ordered.
    """
    ordered.update_from_file(_sort_key_update(tmp_path), resort=True, restat=False)
    assert flags(ordered)["out_of_order"] is False


def test_inplace_rewrite_marks_the_table_out_of_order(ordered):
    """
    An in-place rewrite runs an arbitrary function over every row and dumps
    every search column, so nothing can establish that the sort keys came
    through unchanged.  Its forced resort=False must not be read as evidence
    that they did.
    """
    ordered.rewrite(lambda rec: rec, inplace=True, restat=False)
    assert flags(ordered)["out_of_order"] is True


def test_inplace_rewrite_refuses_an_explicit_resort(ordered):
    """
    And it refuses to pretend it can rebuild, before running func over the
    table rather than after.
    """
    with pytest.raises(ValueError, match="resort"):
        ordered.rewrite(lambda rec: rec, inplace=True, resort=True)


def test_an_update_touching_no_sort_column_keeps_the_order_flag(ordered, tmp_path):
    """
    The flag is only pessimistic where it has to be: a file that cannot move
    any sort key leaves a healthy table healthy.
    """
    dump = tmp_path / "u.txt"
    with open(dump, "w") as F:
        F.write("label|data\ntext|jsonb\n\nl5|{\"a\": 1}\n")
    ordered.update_from_file(str(dump), inplace=True, restat=False)
    assert flags(ordered)["out_of_order"] is False


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


def test_file_updates_without_restat_clear_stats_valid(db, ordered, tmp_path):
    """
    Every file-based write that does not rebuild the caches has to say so in
    meta_tables, on both the in-place and the replacement path.  Otherwise a
    count cached before the write is still served after it; the observable
    half of that is pinned in test_stats_validity.py.
    """
    dump = tmp_path / "u.txt"
    with open(dump, "w") as F:
        F.write("label|num\ntext|numeric\n\nl5|12345\n")

    ordered.update_from_file(str(dump), inplace=True, restat=False)
    assert flags(ordered)["stats_valid"] is False

    ordered.stats.refresh_stats()
    assert flags(ordered)["stats_valid"] is True

    name = ordered.search_table
    ordered.update_from_file(str(dump), restat=False)
    assert flags(db[name])["stats_valid"] is False


def test_rewrite_without_restat_clears_stats_valid(db, ordered):
    name = ordered.search_table
    ordered.rewrite(lambda rec: rec, restat=False)
    assert flags(db[name])["stats_valid"] is False


def test_reload_without_fresh_caches_clears_stats_valid(db, ordered, tmp_path):
    name = ordered.search_table
    dump = tmp_path / "d.txt"
    ordered.copy_to(str(dump), include_id=False)
    ordered.reload(str(dump), restat=False)
    assert flags(db[name])["stats_valid"] is False
    # and total is still exact: it is maintained separately, on every write
    assert flags(db[name])["total"] == 100


def test_a_restatted_replacement_keeps_stats_valid(db, ordered, tmp_path):
    name = ordered.search_table
    dump = tmp_path / "d.txt"
    ordered.copy_to(str(dump), include_id=False)
    ordered.reload(str(dump), restat=True)
    assert flags(db[name])["stats_valid"] is True
