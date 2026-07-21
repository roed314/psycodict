# -*- coding: utf-8 -*-
"""
``update_from_file`` in its non-inplace form builds a replacement search table
and swaps it in.  When ``restat`` is requested it also recomputes the counts
and statistics into ``_tmp`` tables -- but those only reach the live names if
they are included in the final swap, which is what this file guards.
"""
import pytest

from conftest import sample_row


@pytest.fixture
def saving_table(table_factory):
    """A filled table whose stats object persists what it computes."""
    table = table_factory()
    table.insert_many([sample_row(i) for i in range(200)])
    table.stats.saving = True
    return table


def _write_flag_file(path, labels, value):
    """An update file setting ``flag`` to ``value`` for the given labels."""
    with open(path, "w") as F:
        F.write("label|flag\ntext|boolean\n\n")
        for label in labels:
            F.write("%s|%s\n" % (label, "t" if value else "f"))


def test_non_inplace_restat_publishes_refreshed_stats(saving_table, tmp_path):
    # A recorded stat that the update will invalidate: sample_row sets
    # flag = (i % 3 == 0), so 67 of the 200 rows start True.
    saving_table.stats.add_stats(["flag"])
    assert saving_table.stats.column_counts("flag") == {
        True: len([i for i in range(200) if i % 3 == 0]),
        False: len([i for i in range(200) if i % 3 != 0]),
    }

    # Flip every row to True, out of place, asking for a restat.
    datafile = str(tmp_path / "flags.txt")
    _write_flag_file(datafile, ["l%d" % i for i in range(200)], value=True)
    saving_table.update_from_file(datafile, "label", inplace=False, restat=True)

    # The refreshed counts must be live: every row is now True.  Before the
    # fix the swap covered only the search table, so the recomputed stats
    # stayed in the _tmp counts table and column_counts saw the stale split.
    assert list(saving_table.search({"flag": False}, "id")) == []
    assert saving_table.stats.column_counts("flag") == {True: 200}

    # ...and the _tmp counts/stats tables must not be left orphaned (they are
    # exactly what #95's leftover check would later trip over).
    assert not saving_table._table_exists(saving_table.stats.counts + "_tmp")
    assert not saving_table._table_exists(saving_table.stats.stats + "_tmp")


def test_non_inplace_without_restat_leaves_no_tmp_stats(saving_table, tmp_path):
    # The no-restat path must likewise leave nothing behind (it never builds
    # the _tmp stats tables, so there is nothing to swap either).
    saving_table.stats.add_stats(["flag"])
    datafile = str(tmp_path / "flags.txt")
    _write_flag_file(datafile, ["l%d" % i for i in range(200)], value=True)
    saving_table.update_from_file(datafile, "label", inplace=False, restat=False)

    assert list(saving_table.search({"flag": False}, "id")) == []
    assert not saving_table._table_exists(saving_table.stats.counts + "_tmp")
    assert not saving_table._table_exists(saving_table.stats.stats + "_tmp")
