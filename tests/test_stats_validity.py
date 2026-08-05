# -*- coding: utf-8 -*-
"""
``stats_valid`` means what it says: no cached answer survives it being false.

Before this, write paths cleared the flag but read paths ignored it, so a
count cached before a ``restat=False`` write kept being served afterwards.
These tests pin the whole contract -- which lookups are gated, which two are
deliberately not, and how the flag is restored.
"""
import pytest

from psycopg.sql import SQL, Identifier

from conftest import sample_row


@pytest.fixture
def cached_table(table_factory):
    """
    A saving table with statistics computed and recorded, and the flag true.
    """
    table = table_factory()
    table.insert_many([sample_row(i) for i in range(200)])
    table.stats.saving = True
    table.stats.refresh_stats()
    table._restore_stats() if hasattr(table, '_restore_stats') else None
    return table


def stats_valid_in_meta(table):
    """
    The flag as stored, rather than as cached on the Python object.
    """
    cur = table._execute(
        SQL("SELECT stats_valid FROM meta_tables WHERE name = %s"),
        [table.search_table],
    )
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# every gated lookup reports a miss while the flag is false
# ---------------------------------------------------------------------------

def test_a_stale_count_is_not_served_after_an_unrestatted_write(cached_table):
    """
    The case from the review: cache a nonempty query, change the rows it
    matches without refreshing, and the old number kept coming back.
    """
    query = {"flag": True}
    before = cached_table.stats.count(query, record=True)
    assert cached_table.stats.quick_count(query) == before

    cached_table.update(query, {"flag": False}, restat=False)
    assert not cached_table._stats_valid

    # the cached row is still physically there ...
    cur = cached_table._execute(
        SQL("SELECT count FROM {0} WHERE cols = %s").format(
            Identifier(cached_table.stats.counts)
        ),
        [cached_table.stats._split_dict(query)[0]],
    )
    assert cur.rowcount

    # ... but it is not an answer any more, and count() computes the truth
    assert cached_table.stats.quick_count(query) is None
    assert cached_table.stats.count(query) == 0


def test_quick_count_distinct_is_gated(cached_table):
    cols = ["flag"]
    cached_table.stats._slow_count_distinct(cols, record=True)
    assert cached_table.stats.quick_count_distinct(cols) is not None
    cached_table._break_stats()
    assert cached_table.stats.quick_count_distinct(cols) is None


def test_quick_statistic_is_gated(cached_table):
    from psycodict.encoding import Json

    assert cached_table.stats.max("n") == 199
    ccols, cvals = Json([]), Json([])
    assert cached_table.stats._quick_statistic("n", ccols, cvals, "max") is not None
    cached_table._break_stats()
    assert cached_table.stats._quick_statistic("n", ccols, cvals, "max") is None
    # and the public method still returns the right answer, the slow way
    assert cached_table.stats.max("n") == 199


def test_the_recompute_predicates_are_not_gated(cached_table):
    """
    _has_stats and _has_numstats answer "is this recorded", which is what
    add_stats and column_counts use to decide whether to compute.  Gating them
    makes those recompute the whole family on every call and never converge,
    because only refresh_stats restores the flag: measured on the LMFDB, that
    turned a four-minute downstream suite into one still running after
    forty-five.  They stay ungated, and the staleness that leaves is recorded
    in the test below.
    """
    from psycodict.encoding import Json

    cached_table.stats.add_stats(["flag"])
    cached_table.stats.add_numstats("num", ["flag"])
    jcols, empty = Json(["flag"]), Json([])
    assert cached_table.stats._has_stats(jcols, empty, empty, None)
    assert cached_table.stats._has_numstats(Json(["num"]), Json(["flag"]), empty, None)

    cached_table._break_stats()
    assert cached_table.stats._has_stats(jcols, empty, empty, None)
    assert cached_table.stats._has_numstats(Json(["num"]), Json(["flag"]), empty, None)


@pytest.mark.xfail(
    reason="column_counts can still report a value recorded before an "
           "unrefreshed write; closing this needs freshness per statistic "
           "rather than one flag per table",
    strict=True,
)
def test_column_counts_can_still_be_stale(cached_table):
    """
    The gap left by the paragraph above, pinned so that it is a known quantity
    rather than a surprise, and so that a future per-statistic freshness change
    turns this green.
    """
    cached_table.stats.add_stats(["flag"])
    flagged = cached_table.stats.column_counts("flag")[True]
    assert flagged > 0
    cached_table.update({"flag": True}, {"flag": False}, restat=False)
    assert cached_table.stats.column_counts("flag").get(True, 0) == 0


def test_null_counts_is_not_gated(cached_table):
    """
    A miss here costs one full count per search column, not one bounded query,
    so null_counts reads what is recorded like the other bulk paths.  This is
    the call LMFDB's results_complete makes for every query it checks, and
    gating it is what took the downstream suite past forty-five minutes.
    """
    cached_table.stats.refresh_null_counts()
    before = cached_table.stats.null_counts()
    cached_table._break_stats()
    assert cached_table.stats.null_counts() == before


# ---------------------------------------------------------------------------
# what is deliberately not gated
# ---------------------------------------------------------------------------

def test_the_empty_query_total_survives_invalidation(cached_table):
    """
    total is maintained on every write, so it is exact regardless of the flag;
    gating it would make count() do a full scan after every insert.
    """
    cached_table.insert_many([sample_row(1000)], restat=False)
    assert not cached_table._stats_valid
    assert cached_table.stats.quick_count({}) == 201
    assert cached_table.count() == 201


def test_status_still_reports_what_the_cache_holds(cached_table):
    """
    refresh_stats learns which statistics to recompute from _status, so gating
    it would make an invalid table forget what it is supposed to have.
    """
    cached_table.stats.add_stats(["flag"])
    before = cached_table.stats._status()
    cached_table._break_stats()
    assert cached_table.stats._status() == before


def test_a_suffixed_table_is_not_gated_by_the_live_flag(cached_table):
    """
    A _tmp copy carries its own caches; stats_valid describes the live table.
    """
    table = cached_table
    assert table.stats.count({"flag": True}, record=True) > 0
    tmp = table.search_table + "_tmp"
    table._db._execute(
        SQL("CREATE TABLE {0} AS TABLE {1}").format(
            Identifier(tmp), Identifier(table.search_table)
        )
    )
    table._db._execute(
        SQL("CREATE TABLE {0} AS TABLE {1}").format(
            Identifier(table.stats.counts + "_tmp"), Identifier(table.stats.counts)
        )
    )
    try:
        table._break_stats()
        assert table.stats.quick_count({"flag": True}) is None
        assert table.stats.quick_count({"flag": True}, suffix="_tmp") is not None
    finally:
        for name in (tmp, table.stats.counts + "_tmp"):
            table._db._execute(SQL("DROP TABLE IF EXISTS {0}").format(Identifier(name)))


# ---------------------------------------------------------------------------
# restoring the flag
# ---------------------------------------------------------------------------

def test_refresh_stats_restores_the_flag_and_the_cache(cached_table):
    cached_table.update({"flag": True}, {"flag": False}, restat=False)
    assert not cached_table._stats_valid
    assert stats_valid_in_meta(cached_table) is False

    cached_table.stats.refresh_stats()
    assert cached_table._stats_valid
    assert stats_valid_in_meta(cached_table) is True
    assert cached_table.stats.count({"flag": False}, record=True) == 200
    assert cached_table.stats.quick_count({"flag": False}) == 200


def test_a_failed_refresh_leaves_the_table_invalid(cached_table, monkeypatch):
    """
    The flag is set inside the refresh transaction, so a failure part-way
    cannot leave a table claiming a cache it does not have.
    """
    cached_table._break_stats()

    def boom(*args, **kwargs):
        raise RuntimeError("refresh blew up")

    monkeypatch.setattr(cached_table.stats, "refresh_null_counts", boom)
    with pytest.raises(RuntimeError):
        cached_table.stats.refresh_stats()
    cached_table._db.conn.rollback()

    cached_table._refresh()
    assert not cached_table._stats_valid
    assert stats_valid_in_meta(cached_table) is False


def test_refreshing_a_tmp_copy_does_not_validate_the_live_table(cached_table):
    table = cached_table
    for base in (table.search_table, table.stats.counts, table.stats.stats):
        table._db._execute(
            SQL("CREATE TABLE {0} AS TABLE {1}").format(
                Identifier(base + "_tmp"), Identifier(base)
            )
        )
    try:
        table._break_stats()
        table.stats.refresh_stats(suffix="_tmp")
        assert not table._stats_valid
        assert stats_valid_in_meta(table) is False
    finally:
        for base in (table.search_table, table.stats.counts, table.stats.stats):
            table._db._execute(
                SQL("DROP TABLE IF EXISTS {0}").format(Identifier(base + "_tmp"))
            )


# ---------------------------------------------------------------------------
# planner statistics
# ---------------------------------------------------------------------------

def analyzed(table, name=None):
    """
    Whether PostgreSQL holds planner statistics for a relation.
    """
    cur = table._execute(
        SQL(
            "SELECT c.reltuples >= 0 AND s.last_analyze IS NOT NULL "
            "OR s.last_analyze IS NOT NULL "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid "
            "WHERE n.nspname = current_schema() AND c.relname = %s"
        ),
        [name or table.search_table],
    )
    row = cur.fetchone()
    return bool(row and row[0])


def test_a_reload_analyzes_before_the_swap(cached_table, tmp_path):
    """
    A bulk-loaded relation has no planner statistics until autovacuum reaches
    it, and a rename carries the catalog entry along, so the _tmp copy is
    analyzed while it is still _tmp.
    """
    searchfile = tmp_path / "data.txt"
    cached_table.copy_to(str(searchfile))

    seen = []
    original = type(cached_table)._analyze

    def record(self, tables, suffix=""):
        seen.append((list(tables), suffix))
        return original(self, tables, suffix)

    type(cached_table)._analyze = record
    try:
        cached_table.reload(str(searchfile))
    finally:
        type(cached_table)._analyze = original

    assert seen, "the reload did not analyze anything"
    tables, suffix = seen[0]
    assert suffix == "_tmp"
    assert cached_table.search_table in tables
    assert analyzed(cached_table)


def test_copy_from_analyzes_the_live_table(cached_table, table_factory, tmp_path):
    searchfile = tmp_path / "more.txt"
    cached_table.copy_to(str(searchfile))
    target = table_factory()

    seen = []
    original = type(target)._analyze

    def record(self, tables, suffix=""):
        seen.append((list(tables), suffix))
        return original(self, tables, suffix)

    type(target)._analyze = record
    try:
        target.copy_from(str(searchfile), restat=False)
    finally:
        type(target)._analyze = original

    assert seen == [([target.search_table], "")]
    assert target.count() == 200


# ---------------------------------------------------------------------------
# no changed-data path keeps a cached answer by forgetting the flag
#
# The whole gate above is worth nothing if a write changes rows, does not
# refresh the caches, and leaves stats_valid true: the cached rows are then
# still served as answers.  These check the flag, the cache and the truth.
# ---------------------------------------------------------------------------

QUERY = {"flag": True}


@pytest.fixture
def counted(cached_table):
    """
    ``cached_table`` with a nonempty-query count actually recorded and served.
    """
    before = cached_table.stats.count(QUERY, record=True)
    assert before == 67
    assert cached_table.stats.quick_count(QUERY) == before
    assert stats_valid_in_meta(cached_table) is True
    return cached_table


def assert_invalidated(table, expected):
    """
    The flag is false, no cached answer is served, and the truth is available.
    """
    assert table._stats_valid is False
    assert stats_valid_in_meta(table) is False
    assert table.stats.quick_count(QUERY) is None
    assert table.count(QUERY) == expected


def _flag_flip_file(table, tmp_path, name="u.txt"):
    """
    An update file clearing ``flag`` on the rows the cached query counts.
    """
    labels = [
        rec["label"]
        for rec in table.search(QUERY, projection=["label"])
    ]
    path = tmp_path / name
    with open(path, "w") as F:
        F.write("label|flag\ntext|boolean\n\n")
        for label in labels:
            F.write("%s|f\n" % label)
    return str(path)


def test_inplace_update_from_file_without_restat_invalidates(counted, tmp_path):
    counted.update_from_file(
        _flag_flip_file(counted, tmp_path), inplace=True, restat=False
    )
    assert_invalidated(counted, 0)


def test_replacement_update_from_file_without_restat_invalidates(db, counted, tmp_path):
    name = counted.search_table
    counted.update_from_file(_flag_flip_file(counted, tmp_path), restat=False)
    assert_invalidated(db[name], 0)


def test_rewrite_without_restat_invalidates(db, counted):
    name = counted.search_table

    def clear_flag(rec):
        rec["flag"] = False
        return rec

    counted.rewrite(clear_flag, restat=False)
    assert_invalidated(db[name], 0)


def test_inplace_rewrite_without_restat_invalidates(counted):
    def clear_flag(rec):
        rec["flag"] = False
        return rec

    counted.rewrite(clear_flag, inplace=True, restat=False)
    assert_invalidated(counted, 0)


def test_reload_without_matching_caches_invalidates(db, counted, tmp_path):
    """
    A reload with no counts/stats files swaps changed search data in beside
    companions it merely cloned; with restat off nothing rebuilds them.
    """
    name = counted.search_table
    searchfile = tmp_path / "data.txt"
    counted.copy_to(str(searchfile), include_id=False)
    # a file whose rows differ from the cached ones
    text = open(searchfile).read().replace("|t|", "|f|")
    with open(searchfile, "w") as F:
        F.write(text)

    counted.reload(str(searchfile), restat=False)
    assert_invalidated(db[name], 0)


def test_a_metafile_cannot_vouch_for_caches_the_reload_did_not_build(db, counted, tmp_path):
    """
    meta_tables is replaced wholesale by the metafile, stats_valid included.
    That flag describes the database the file came from; it must not be
    installed over caches this reload never rebuilt.
    """
    name = counted.search_table
    searchfile = tmp_path / "data.txt"
    metafile = tmp_path / "meta.txt"
    counted.copy_to(str(searchfile), include_id=False, metafile=str(metafile))
    assert "|t|" in open(metafile).read()  # the file asserts stats_valid

    text = open(searchfile).read().replace("|t|", "|f|")
    with open(searchfile, "w") as F:
        F.write(text)
    counted.reload(str(searchfile), metafile=str(metafile), restat=False)

    assert_invalidated(db[name], 0)


def test_a_reload_carrying_its_own_caches_stays_valid(db, counted, tmp_path):
    """
    The other half of the rule: an export that supplies counts and stats
    alongside the search file installed caches built for exactly that data,
    so the table is entitled to go on serving them.
    """
    name = counted.search_table
    searchfile = tmp_path / "data.txt"
    countsfile = tmp_path / "counts.txt"
    statsfile = tmp_path / "stats.txt"
    counted.copy_to(str(searchfile), countsfile=str(countsfile), statsfile=str(statsfile))

    counted.reload(str(searchfile), countsfile=str(countsfile), statsfile=str(statsfile))

    table = db[name]
    assert table._stats_valid is True
    assert stats_valid_in_meta(table) is True
    assert table.stats.quick_count(QUERY) == 67


def test_a_refreshing_replacement_restores_validity(db, counted, tmp_path):
    """
    And a replacement that does recompute its caches ends valid, serving the
    new answer rather than the old one.
    """
    name = counted.search_table
    counted.update_from_file(_flag_flip_file(counted, tmp_path), restat=True)

    table = db[name]
    assert table._stats_valid is True
    assert stats_valid_in_meta(table) is True
    assert table.count(QUERY) == 0


def test_a_failed_swap_does_not_leave_validity_behind(counted, tmp_path, monkeypatch):
    """
    The flag moves in the same transaction as the swap, so a swap that fails
    leaves the table exactly as it was -- still valid, still holding the data
    the caches describe.
    """
    name = counted.search_table
    searchfile = tmp_path / "data.txt"
    counted.copy_to(str(searchfile), include_id=False)

    def boom(*args, **kwargs):
        raise RuntimeError("swap blew up")

    monkeypatch.setattr(type(counted), "_swap_in_tmp", boom)
    with pytest.raises(RuntimeError):
        counted.reload(str(searchfile), restat=False)
    counted._db.conn.rollback()
    counted.drop_tmp()

    counted._refresh()
    assert counted._stats_valid is True
    assert stats_valid_in_meta(counted) is True
    assert counted.stats.quick_count(QUERY) == 67
    assert counted.search_table == name
