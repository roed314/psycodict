# -*- coding: utf-8 -*-
"""
Tests for ``PostgresStatsTable``: cached counts, aggregates and the stats
tables that back them.

Two things are worth knowing when reading these tests.

First, ``PostgresStatsTable.saving`` is False by default: a plain psycodict
table computes statistics on demand but does not persist them.  LMFDB
subclasses the stats table and sets ``saving = True``.  Tests that care about
persistence therefore use the ``saving_table`` fixture below.

Second, ``add_stats`` and friends run entirely in Python and SQL; the sage
imports that psycodict makes elsewhere are not needed, so these tests must
pass in a plain (sage-free) interpreter -- which is also how CI runs them.
"""
import pytest

from conftest import sample_row
from psycodict.encoding import SAGE_MODE


@pytest.fixture
def saving_table(table_factory):
    """
    A filled table whose stats object persists what it computes.

    ``saving`` is a class attribute; setting it on the instance keeps the
    change local to this table and therefore to this test.
    """
    table = table_factory()
    table.insert_many([sample_row(i) for i in range(200)])
    table.stats.saving = True
    return table


# ---------------------------------------------------------------- aggregates


def test_max(filled_table):
    assert filled_table.stats.max("n") == 199


def test_min(filled_table):
    assert filled_table.stats.min("n") == 0


def test_max_with_constraint(filled_table):
    assert filled_table.stats.max("n", constraint={"flag": True}) == 198


def test_min_with_constraint(filled_table):
    assert filled_table.stats.min("n", constraint={"n": {"$gte": 50}}) == 50


@pytest.mark.xfail(
    strict=True,
    reason="max()/min() on an empty table raise TypeError: _aggregate does "
           "`cur.fetchone()[0]` and fetchone() is None when nothing matched",
)
def test_max_on_empty_table(empty_table):
    assert empty_table.stats.max("n") is None


@pytest.mark.xfail(
    strict=True,
    reason="see test_max_on_empty_table; min() crashes the same way",
)
def test_min_on_empty_table(empty_table):
    assert empty_table.stats.min("n") is None


def test_sum_on_empty_table(empty_table):
    # SUM() over no rows yields a NULL row rather than no row at all, so this
    # path survives where max()/min() do not.
    assert empty_table.stats.sum("n") in (None, 0)


def test_sum(filled_table):
    assert filled_table.stats.sum("n") == sum(range(200))


def test_sum_with_constraint(filled_table):
    expected = sum(i for i in range(200) if i % 3 == 0)
    assert filled_table.stats.sum("n", constraint={"flag": True}) == expected


def test_sum_of_numeric_column_is_exact(filled_table):
    # `num` is a numeric column with integral values, so the sum must come
    # back exactly (int without Sage, Integer with it) -- never as a float.
    total = filled_table.stats.sum("num")
    assert total == sum(i * 10 + 7 for i in range(200))
    assert not isinstance(total, float)


def test_max_of_text_column(filled_table):
    # Text ordering, not numeric: "l99" is the largest label.
    assert filled_table.stats.max("label") == "l99"


# -------------------------------------------------------------------- counts


def test_count_with_query(filled_table):
    assert filled_table.stats.count({"n": {"$lt": 50}}) == 50


def test_count_matching_nothing(filled_table):
    assert filled_table.stats.count({"n": -1}) == 0


def test_count_groupby(filled_table):
    counts = filled_table.stats.count({}, groupby=["flag"])
    assert counts[(True,)] == len([i for i in range(200) if i % 3 == 0])
    assert counts[(False,)] == len([i for i in range(200) if i % 3 != 0])


def test_slow_count_sees_every_row(filled_table):
    # The uncached path is the one that always tells the truth; see
    # test_count_unfiltered_is_stale below for the cached one.
    assert filled_table.stats._slow_count({}, extra=False) == 200


@pytest.mark.xfail(
    strict=True,
    reason="count() returns the stale meta_tables.total: every write path "
           "guards the total update behind `if self.stats.saving:`, which is "
           "False by default, so a table filled through psycodict reports 0",
)
def test_count_unfiltered_reflects_inserted_rows(filled_table):
    assert filled_table.count() == 200


def test_count_distinct(filled_table):
    # data.s cycles through v0..v6, so `label` is unique and `flag` is not.
    assert filled_table.stats.count_distinct("label") == 200
    assert filled_table.stats.count_distinct("flag") == 2


def test_count_distinct_with_query(filled_table):
    assert filled_table.stats.count_distinct("flag", query={"n": {"$lt": 3}}) == 2


def test_quick_count_returns_none_when_uncached(filled_table):
    # Nothing has been recorded for this query, so there is no cached answer.
    assert filled_table.stats.quick_count({"n": {"$lt": 50}}) is None


def test_count_records_into_counts_table(saving_table):
    assert saving_table.stats.count({"n": {"$lt": 50}}, record=True) == 50
    # Having been recorded, the same query is now answerable from the cache.
    assert saving_table.stats.quick_count({"n": {"$lt": 50}}) == 50


def test_count_with_record_false_does_not_cache(saving_table):
    assert saving_table.stats.count({"n": {"$lt": 60}}, record=False) == 60
    assert saving_table.stats.quick_count({"n": {"$lt": 60}}) is None


def test_null_counts_ignores_columns_without_nulls(filled_table):
    # Every column of every sample row is populated.
    assert filled_table.stats.null_counts() == {}


def test_null_counts_finds_nulls(empty_table):
    empty_table.insert_many([{"n": 1, "label": "a"}, {"n": 2, "label": "b"}])
    nulls = empty_table.stats.null_counts()
    assert nulls["x"] == 2
    assert nulls["data"] == 2
    assert "n" not in nulls


# --------------------------------------------------------------- add_stats


def test_add_stats_returns_true(filled_table):
    assert filled_table.stats.add_stats(["flag"]) is True


@pytest.mark.skipif(SAGE_MODE, reason="asserts the sage-free code path")
def test_add_stats_without_sage(filled_table):
    # add_stats used to import sage.all unconditionally, which made the whole
    # code path unusable in a plain interpreter.  Keep it honest.
    import sys

    assert "sage" not in sys.modules
    filled_table.stats.add_stats(["flag"])


def test_add_stats_populates_stats_table(saving_table):
    saving_table.stats.add_stats(["flag"])
    rows = list(saving_table.stats.column_counts(["flag"]).items())
    assert dict(rows)[(True,)] == len([i for i in range(200) if i % 3 == 0])


def test_column_counts_single_column(saving_table):
    saving_table.stats.add_stats(["flag"])
    counts = saving_table.stats.column_counts("flag")
    assert counts[True] + counts[False] == 200


def test_column_counts_threshold_drops_rare_values(saving_table):
    # data.s takes 7 values, each on fewer than 40 rows; a threshold above
    # that must discard all of them.
    saving_table.stats.add_stats(["n"], threshold=1000)
    assert saving_table.stats.column_counts(["n"], threshold=1000) == {}


def test_add_stats_two_columns(saving_table):
    saving_table.stats.add_stats(["flag", "n"], threshold=1)
    counts = saving_table.stats.column_counts(["flag", "n"], threshold=1)
    assert counts[(True, 0)] == 1
    assert sum(counts.values()) == 200


def test_add_numstats_and_numstats(saving_table):
    # Group the numeric column `n` by the boolean `flag`.
    saving_table.stats.add_numstats("n", ["flag"])
    stats = saving_table.stats.numstats("n", ["flag"])
    assert stats[(True,)]["max"] == 198
    assert stats[(False,)]["min"] == 1


def test_add_bucketed_counts(saving_table):
    buckets = {"n": ["0", "50", "100", "150", "200"]}
    saving_table.stats.add_bucketed_counts(["n"], buckets)
    counts = saving_table.stats.column_counts(["n"], threshold=None)
    assert counts  # buckets recorded something


def test_refresh_stats_recomputes_total(filled_table):
    filled_table.stats.refresh_stats()
    assert filled_table.stats.total == 200


def test_status_reports_recorded_stats(saving_table):
    saving_table.stats.add_stats(["flag"])
    stat_cmds, split_cmds, nstat_cmds = saving_table.stats._status()
    assert any("flag" in cols for cols, _, _, _ in stat_cmds)


def test_extra_counts_empty_initially(filled_table):
    assert filled_table.stats.extra_counts() == {}


# ------------------------------------------------------- stats table wiring


def test_stats_and_counts_tables_exist(db, empty_table):
    name = empty_table.search_table
    tables = {
        r[0]
        for r in db._execute(
            _information_schema_query(), [name + "_stats", name + "_counts"]
        )
    }
    assert tables == {name + "_stats", name + "_counts"}


def _information_schema_query():
    from psycopg2.sql import SQL

    return SQL(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN (%s, %s)"
    )


def test_total_starts_at_zero_for_new_table(empty_table):
    assert empty_table.stats.total == 0


def test_saving_defaults_to_false(empty_table):
    # This default is why an unmodified psycodict table does not persist
    # counts; LMFDB's subclass flips it.  Pinning it here so the behaviour
    # cannot change silently.
    assert empty_table.stats.saving is False
