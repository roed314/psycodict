# -*- coding: utf-8 -*-
"""
Tests for LMFDB/lmfdb#4103: duplicate rows in the counts and stats tables.

The counts table has no unique index, but rows are keyed by
``(cols, values, split)``: ``quick_count`` fetches a single row per key and
the display helpers emit one table header per row.  Before the fix,
``add_stats`` and ``add_numstats`` inserted their freshly computed rows
without checking for existing ones, so a count previously recorded by a
one-off ``count(record=True)`` call (or by an overlapping statistics family)
ended up stored twice -- and every value with two rows produced an extra
column in displayed statistics.  The stats table had the same disease via
one-off ``max()``/``min()`` calls and the ``add_stats``/``add_numstats``
overlap.

These tests drive the pre-fix duplicate scenarios and assert on the raw
Postgres tables that each key is stored exactly once, and that the grouped
data returned to the display layer lists each value exactly once.
"""
import pytest

from psycopg.sql import SQL, Identifier

from conftest import sample_row
from psycodict.encoding import Json

# Of sample_row(0), ..., sample_row(199), the rows with flag = True are those
# with i % 3 == 0.
NFLAGGED = len([i for i in range(200) if i % 3 == 0])  # 67


@pytest.fixture
def saving_table(table_factory):
    """
    A filled table whose stats object persists what it computes, as the
    LMFDB subclass does (``saving`` is a class attribute; setting it on the
    instance keeps the change local to this table).
    """
    table = table_factory()
    table.insert_many([sample_row(i) for i in range(200)])
    table.stats.saving = True
    return table


def duplicated_count_keys(table):
    """
    Keys of the raw ``<table>_counts`` table stored in more than one row.
    """
    cur = table._execute(
        SQL(
            "SELECT cols, values, split, COUNT(*) FROM {0} "
            "GROUP BY cols, values, split HAVING COUNT(*) > 1"
        ).format(Identifier(table.search_table + "_counts"))
    )
    return list(cur)


def duplicated_stat_keys(table):
    """
    Keys of the raw ``<table>_stats`` table stored in more than one row.
    """
    cur = table._execute(
        SQL(
            "SELECT cols, stat, constraint_cols, constraint_values, threshold, COUNT(*) "
            "FROM {0} GROUP BY 1, 2, 3, 4, 5 HAVING COUNT(*) > 1"
        ).format(Identifier(table.search_table + "_stats"))
    )
    return list(cur)


def count_rows(table, cols, values, split=False):
    """
    The ``(count, extra)`` pairs of the raw counts rows for one key.
    """
    cur = table._execute(
        SQL(
            "SELECT count, extra FROM {0} WHERE cols = %s AND values = %s AND split = %s"
        ).format(Identifier(table.search_table + "_counts")),
        [Json(cols), Json(values), split],
    )
    return [(rec[0], rec[1]) for rec in cur]


# ------------------------------------------------ the scenario from the issue


def test_add_stats_replaces_one_off_counts(saving_table):
    stats = saving_table.stats
    assert stats.count({"flag": True}, record=True) == NFLAGGED
    assert stats.count({"flag": False}, record=True) == 200 - NFLAGGED
    # The one-off counts are stored once each, marked as extra.
    assert count_rows(saving_table, ["flag"], [True]) == [(NFLAGGED, True)]
    stats.add_stats(["flag"])
    # add_stats must replace those rows, not sit a second copy beside them.
    assert count_rows(saving_table, ["flag"], [True]) == [(NFLAGGED, False)]
    assert count_rows(saving_table, ["flag"], [False]) == [(200 - NFLAGGED, False)]
    assert duplicated_count_keys(saving_table) == []
    assert stats.quick_count({"flag": True}) == NFLAGGED


def test_display_data_lists_each_value_once(saving_table):
    # Pre-fix, each duplicated row produced its own header, i.e. an extra
    # column in the displayed statistics table.
    stats = saving_table.stats
    stats.count({"flag": True}, record=True)
    stats.count({"flag": False}, record=True)
    stats.add_stats(["flag"])
    headers, data = stats._get_values_counts(
        ["flag"],
        None,
        False,
        formatter={"flag": lambda x: x},
        query_formatter={"flag": lambda x: "flag=%s" % x},
        base_url="?",
    )
    assert sorted(headers) == [False, True]
    assert set(data) == {False, True}
    assert data[True]["count"] == NFLAGGED


def test_stale_one_off_count_is_replaced(saving_table):
    # The replaced row also refreshes the count: if the one-off count had
    # gone stale, keeping both rows would leave quick_count picking one of
    # the two at the whim of the query plan.
    stats = saving_table.stats
    stats.count({"flag": True}, record=True)
    saving_table._execute(
        SQL("UPDATE {0} SET count = 999 WHERE cols = %s AND values = %s").format(
            Identifier(saving_table.search_table + "_counts")
        ),
        [Json(["flag"]), Json([True])],
    )
    assert stats.quick_count({"flag": True}) == 999  # stale, as arranged
    stats.add_stats(["flag"])
    assert stats.quick_count({"flag": True}) == NFLAGGED
    assert count_rows(saving_table, ["flag"], [True]) == [(NFLAGGED, False)]


def test_range_count_survives_add_stats(saving_table):
    # A recorded count whose values are a query dictionary (here a range) is
    # not part of the grouped family, so add_stats must leave it alone.
    stats = saving_table.stats
    assert stats.count({"n": {"$lt": 50}}, record=True) == 50
    stats.add_stats(["n"])
    assert stats.quick_count({"n": {"$lt": 50}}) == 50
    assert count_rows(saving_table, ["n"], [{"$lt": 50}]) == [(50, True)]
    assert (({"$lt": 50},), 50) in stats.extra_counts()[("n",)]
    assert duplicated_count_keys(saving_table) == []


# --------------------------------------------- overlapping statistics families


def test_overlapping_families_share_rows(saving_table):
    # add_stats(["n"], {"flag": True}) stores its counts under
    # cols = ["flag", "n"], exactly where add_stats(["flag", "n"]) stores
    # every row, so before the fix the constrained family's rows were
    # duplicated by the unconstrained run.
    stats = saving_table.stats
    stats.add_stats(["n"], {"flag": True})
    stats.add_stats(["flag", "n"])
    assert duplicated_count_keys(saving_table) == []
    constrained = stats.column_counts(["n"], constraint={"flag": True})
    assert sum(constrained.values()) == NFLAGGED
    both = stats.column_counts(["flag", "n"])
    assert len(both) == 200 and sum(both.values()) == 200


def test_refresh_stats_leaves_single_rows(saving_table):
    # refresh_stats replays every recorded add_stats command; before the fix
    # it faithfully recreated the overlap duplicates it had just deleted.
    stats = saving_table.stats
    stats.count({"flag": True}, record=True)
    stats.count({"n": {"$lt": 50}}, record=True)
    stats.add_stats(["n"], {"flag": True})
    stats.add_stats(["flag", "n"])
    stats.refresh_stats()
    assert duplicated_count_keys(saving_table) == []
    assert duplicated_stat_keys(saving_table) == []
    assert stats.quick_count({"n": {"$lt": 50}}) == 50
    assert sum(stats.column_counts(["n"], constraint={"flag": True}).values()) == NFLAGGED


def test_threshold_families_share_rows(saving_table):
    # Counts rows do not record the threshold, so re-adding the same columns
    # under a different threshold used to duplicate every row above it.
    stats = saving_table.stats
    stats.add_stats(["flag"], threshold=5)
    stats.add_stats(["flag"])
    assert duplicated_count_keys(saving_table) == []
    cur = saving_table._execute(
        SQL("SELECT COUNT(*) FROM {0} WHERE cols = %s").format(
            Identifier(saving_table.search_table + "_counts")
        ),
        [Json(["flag"])],
    )
    assert cur.fetchone()[0] == 2  # one row per value of flag


def test_split_list_overlap(saving_table):
    # Split counts collide between overlapping families too: the constrained
    # family stores its split counts under cols = ["mat", "vec"], which the
    # unconstrained two-column family then recomputes.
    stats = saving_table.stats
    stats.add_stats(["vec"], {"mat": [1, 1]}, split_list=True)
    assert len(count_rows(saving_table, ["mat", "vec"], [1, 1], split=True)) == 1
    stats.add_stats(["mat", "vec"], split_list=True)
    assert duplicated_count_keys(saving_table) == []
    assert len(count_rows(saving_table, ["mat", "vec"], [1, 1], split=True)) == 1


# ------------------------------------------------------------ the stats table


def test_one_off_max_then_add_stats(saving_table):
    # max(col) records a stats row; add_stats on a single numeric column
    # inserts a max row with the same key and used to duplicate it.
    stats = saving_table.stats
    assert stats.max("n") == 199
    stats.add_stats(["n"])
    assert duplicated_stat_keys(saving_table) == []
    cur = saving_table._execute(
        SQL(
            "SELECT value FROM {0} WHERE cols = %s AND stat = %s AND constraint_cols = %s"
        ).format(Identifier(saving_table.search_table + "_stats")),
        [Json(["n"]), "max", Json([])],
    )
    assert [rec[0] for rec in cur] == [199]
    assert stats.max("n") == 199


def test_add_numstats_overlap(saving_table):
    # add_numstats("n", ["flag"]) records counts for each value of flag and
    # avg/min/max stats rows constrained to each value of flag -- colliding
    # with rows from add_stats(["flag"]), from a recorded one-off max, and
    # (on a second run under another guard) with itself.
    stats = saving_table.stats
    stats.add_stats(["flag"])
    assert stats.max("n", constraint={"flag": True}, record=True) == 198
    stats.add_numstats("n", ["flag"])
    assert duplicated_count_keys(saving_table) == []
    assert duplicated_stat_keys(saving_table) == []
    nstats = stats.numstats("n", ["flag"])
    assert nstats[(True,)]["max"] == 198
    assert nstats[(False,)]["min"] == 1
    assert stats.quick_count({"flag": True}) == NFLAGGED


# ---------------------------------------- constrained numstats: the ntotal row


@pytest.fixture
def constrained_table(table_factory):
    """
    A table whose constraint column ``z`` sorts after the grouping column
    ``a``.  The ntotal row is keyed by the constraint columns followed by the
    grouping columns (``["z", "a"]``, so that ``_status`` can split the two
    apart again), which then differs from the sorted union ``["a", "z"]``
    keying the avg/min/max and counts rows.
    """
    table = table_factory(
        columns=[("n", "integer"), ("a", "integer"), ("z", "integer"), ("label", "text")]
    )
    table.insert_many(
        [{"n": i, "a": i % 3, "z": i % 2, "label": "l%d" % i} for i in range(30)]
    )
    table.stats.saving = True
    return table


def ntotal_rows(table):
    """
    The ``(constraint_cols, value)`` pairs of the raw ntotal stats rows.
    """
    cur = table._execute(
        SQL("SELECT constraint_cols, value FROM {0} WHERE stat = %s").format(
            Identifier(table.search_table + "_stats")
        ),
        ["ntotal"],
    )
    return [(rec[0], rec[1]) for rec in cur]


def test_repeated_constrained_add_numstats(constrained_table):
    # The existence guard used to look the ntotal row up under the sorted
    # union of the constraint and grouping columns, so repeated constrained
    # calls never found the previous run's row (stored constraint-first) and
    # stacked up duplicates -- and the dictionary form of the constraint did
    # not survive the key construction at all (tuple + list).
    stats = constrained_table.stats
    stats.add_numstats("n", ["a"], constraint={"z": 1})
    stats.add_numstats("n", ["a"], constraint={"z": 1})
    # refresh_stats replays the command in its (ccols, cvals) form
    stats.add_numstats("n", ["a"], constraint=(["z"], [1]))
    assert ntotal_rows(constrained_table) == [(["z", "a"], 15)]
    assert duplicated_stat_keys(constrained_table) == []
    assert duplicated_count_keys(constrained_table) == []
    # Odd n grouped by n % 3: 3,9,..,27 / 1,7,..,25 / 5,11,..,29.
    assert stats.numstats("n", ["a"], constraint={"z": 1}) == {
        (0,): {"avg": 15, "min": 3, "max": 27},
        (1,): {"avg": 13, "min": 1, "max": 25},
        (2,): {"avg": 17, "min": 5, "max": 29},
    }
    # The read found the stored statistics rather than recomputing them.
    assert ntotal_rows(constrained_table) == [(["z", "a"], 15)]
    # The counts rows are keyed by the sorted union, where quick_count looks.
    assert stats.quick_count({"a": 0, "z": 1}) == 5


def test_constrained_numstats_read_computes_once(constrained_table):
    # numstats performs the same existence check before deciding to call
    # add_numstats: a constrained read used to miss the stored ntotal row
    # every time, recomputing the statistics (and inserting another copy)
    # on every call.
    stats = constrained_table.stats
    for _ in range(2):
        nstats = stats.numstats("n", "a", constraint={"z": 1})
        assert nstats[0] == {"avg": 15, "min": 3, "max": 27}
    assert ntotal_rows(constrained_table) == [(["z", "a"], 15)]
    assert duplicated_stat_keys(constrained_table) == []


def test_legacy_ntotal_duplicates_cleaned_by_refresh(constrained_table):
    # ntotal duplicates left behind by pre-fix runs are not deleted by a
    # plain re-run -- the existence check now finds the rows and returns
    # before the delete-and-insert machinery -- but they no longer multiply,
    # and refresh_stats, which wipes the tables and replays each recorded
    # command through the fixed check, converges to a single row.
    stats = constrained_table.stats
    stats.add_numstats("n", ["a"], constraint={"z": 1})
    # a second identical ntotal row, as a pre-fix run would have left
    constrained_table._execute(
        SQL(
            "INSERT INTO {0} (cols, stat, value, constraint_cols, constraint_values, threshold) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        ).format(Identifier(constrained_table.search_table + "_stats")),
        [Json(["n"]), "ntotal", 15, Json(["z", "a"]), Json([1]), None],
    )
    assert len(ntotal_rows(constrained_table)) == 2
    stats.add_numstats("n", ["a"], constraint={"z": 1})
    assert len(ntotal_rows(constrained_table)) == 2  # no growth
    # This table records numstats only, which also exercises the regeneration
    # loop's deleted-column check (it used to read the stats commands' loop
    # variable, unbound here).
    stats.refresh_stats()
    assert ntotal_rows(constrained_table) == [(["z", "a"], 15)]
    assert duplicated_stat_keys(constrained_table) == []
    assert duplicated_count_keys(constrained_table) == []
    nstats = stats.numstats("n", ["a"], constraint={"z": 1})
    assert nstats[(1,)] == {"avg": 13, "min": 1, "max": 25}


# -------------------------------------------- _record_count keys rows properly


def test_record_count_checks_the_split_flag(saving_table):
    # Whitebox: rows differing only in `split` are different keys, and the
    # INSERT-or-UPDATE decision must test the key actually being written.
    # Before the fix the split row's existence check ignored split_list, so
    # writing a split count while an unsplit row existed updated nothing and
    # recorded nothing.
    stats = saving_table.stats
    stats._record_count({"flag": True}, NFLAGGED)
    stats._record_count({"flag": True}, 40, split_list=True)
    assert count_rows(saving_table, ["flag"], [True], split=False) == [(NFLAGGED, True)]
    assert count_rows(saving_table, ["flag"], [True], split=True) == [(40, True)]
    assert stats.quick_count({"flag": True}) == NFLAGGED
    assert stats.quick_count({"flag": True}, split_list=True) == 40


def test_set_total_inserts_missing_total_row(saving_table):
    # The empty-query check used to short-circuit to the in-memory total,
    # which says nothing about whether the row exists; _set_total then ran an
    # UPDATE that matched nothing and the total was never recorded.  The row
    # is maintained by the library, so it must not be flagged as extra.
    stats = saving_table.stats
    assert count_rows(saving_table, [], []) == []
    stats._set_total(stats.total)
    assert count_rows(saving_table, [], []) == [(200, False)]
    assert stats.extra_counts() == {}
    stats._set_total(123)
    assert count_rows(saving_table, [], []) == [(123, False)]
