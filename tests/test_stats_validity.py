# -*- coding: utf-8 -*-
"""
``stats_valid`` means what it says: no cached answer survives it being false.

Before this, write paths cleared the flag but read paths ignored it, so a
count cached before a ``restat=False`` write kept being served afterwards.
These tests pin the whole contract -- which lookups are gated, which are
deliberately not, when the flag is cleared and restored, and that all of it is
decided by the database rather than by a Boolean on a Python object, which is
what makes it hold across processes and across a rollback.
"""
import threading
import time

import pytest

from psycopg.sql import SQL, Identifier

from conftest import sample_row
from psycodict.utils import DelayCommit

# Long enough that a loaded machine does not fail the concurrency tests
# spuriously, short enough that a genuine hang is reported rather than waited
# out.
TIMEOUT = 30

# Of sample_row(0), ..., sample_row(199), the rows with flag = True are those
# with i % 3 == 0.
NFLAGGED = len([i for i in range(200) if i % 3 == 0])  # 67


@pytest.fixture
def cached_table(table_factory):
    """
    A saving table with statistics computed and recorded, and the flag true.
    """
    table = table_factory()
    table.insert_many([sample_row(i) for i in range(200)])
    table.stats.saving = True
    table.stats.refresh_stats()
    return table


@pytest.fixture
def other_handle(config):
    """
    A second ``PostgresDatabase``, on its own connection.

    The point of most of what follows: a deployment runs several webserver
    processes, each holding its own table objects, and an invalidation from
    one of them has to be visible to the others.  A fixture rather than a
    second cursor because the object-level caching is what is under test.
    """
    from psycodict.database import PostgresDatabase

    other = PostgresDatabase(config=config)
    yield other
    other.conn.close()


def twin(table, other_handle):
    """
    The same search table, as seen by a second database handle.

    Its ``_stats_valid`` and ``total`` are read when this object is built and
    then left alone, exactly like those of a table object held by another
    webserver process.
    """
    other_handle.refresh_tables()
    return other_handle[table.search_table]


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
# the flag is the database's, not the Python object's
#
# A deployment runs several webserver processes, and a rollback undoes SQL and
# not Python, so ``self.table._stats_valid`` answers neither "is this table
# valid" nor "was this transition already made".  These are the cases where it
# disagrees with the row.
# ---------------------------------------------------------------------------

def test_a_second_handle_does_not_serve_a_count_invalidated_elsewhere(
    cached_table, other_handle
):
    """
    The headline case in a deployment: one process writes with restat=False,
    and every other process must stop serving the count it had cached.
    """
    query = {"flag": True}
    before = cached_table.stats.count(query, record=True)
    assert before == NFLAGGED

    reader = twin(cached_table, other_handle)
    assert reader.stats.quick_count(query) == before

    cached_table.update(query, {"flag": False}, restat=False)

    # The reader's copy of the flag still says true -- nothing told it
    # otherwise, and nothing ever would ...
    assert reader._stats_valid is True
    # ... but the row it would have answered from is not an answer any more.
    assert reader.stats.quick_count(query) is None
    assert reader.stats.count(query) == 0


def test_a_second_handle_does_not_serve_a_distinct_count_invalidated_elsewhere(
    cached_table, other_handle
):
    cols = ["flag"]
    assert cached_table.stats._slow_count_distinct(cols, record=True) == 2

    reader = twin(cached_table, other_handle)
    assert reader.stats.quick_count_distinct(cols) == 2

    cached_table.update({"flag": True}, {"flag": False}, restat=False)

    assert reader._stats_valid is True
    assert reader.stats.quick_count_distinct(cols) is None
    assert reader.stats.count_distinct("flag") == 1


@pytest.mark.parametrize("kind,before,after", [("max", 199, 99), ("min", 0, 0), ("sum", 19900, 4950)])
def test_a_second_handle_does_not_serve_a_statistic_invalidated_elsewhere(
    cached_table, other_handle, kind, before, after
):
    from psycodict.encoding import Json

    assert getattr(cached_table.stats, kind)("n") == before

    reader = twin(cached_table, other_handle)
    empty = Json([])
    assert reader.stats._quick_statistic("n", empty, empty, kind) is not None

    cached_table.delete({"n": {"$gte": 100}}, restat=False)

    assert reader._stats_valid is True
    assert reader.stats._quick_statistic("n", empty, empty, kind) is None
    assert getattr(reader.stats, kind)("n", record=False) == after


def test_a_second_handle_sees_a_total_another_handle_changed(cached_table, other_handle):
    """
    ``total`` is maintained rather than cached, so it is exempt from the gate --
    which makes reading it from meta_tables rather than from ``self.total`` the
    only thing keeping it honest across processes.
    """
    reader = twin(cached_table, other_handle)

    cached_table.insert_many([sample_row(1000 + i) for i in range(5)], restat=False)
    cached_table.delete({"n": 0}, restat=False)

    # The value this reader was built with, untouched by either write ...
    assert reader.stats.total == 200
    # ... and the value it answers with.
    assert reader.stats.quick_count({}) == 204
    assert reader.count() == 204


def test_a_stale_local_false_does_not_skip_the_invalidation(cached_table, other_handle):
    """
    The inverse failure: a table object whose copy of the flag is already false
    would, if the transition were conditional on it, leave the row true while
    changing the data.
    """
    query = {"n": {"$lt": 100}}
    assert cached_table.stats.count(query, record=True) == 100

    cached_table.update({"label": "l0"}, {"x": 9.5}, restat=False)
    assert cached_table._stats_valid is False

    refresher = twin(cached_table, other_handle)
    refresher.stats.saving = True
    refresher.stats.refresh_stats()
    assert stats_valid_in_meta(cached_table) is True
    # ... which the first handle has no way of hearing about.
    assert cached_table._stats_valid is False

    cached_table.update({"label": "l1"}, {"x": 8.5}, restat=False)
    assert stats_valid_in_meta(cached_table) is False
    assert cached_table.stats.quick_count(query) is None


def test_a_rolled_back_restore_does_not_make_the_cache_usable(cached_table):
    """
    A rollback undoes the ``UPDATE`` and not the attribute, so after one the
    Python object claims a validity the database denies.  The database wins.
    """
    query = {"flag": True}
    assert cached_table.stats.count(query, record=True) == NFLAGGED
    cached_table.update(query, {"flag": False}, restat=False)
    assert cached_table.stats.quick_count(query) is None

    with pytest.raises(RuntimeError):
        with DelayCommit(cached_table):
            cached_table.stats.refresh_stats()
            assert cached_table._stats_valid is True
            raise RuntimeError("the caller changed its mind")

    # The object was left saying true and the row says false ...
    assert cached_table._stats_valid is True
    assert stats_valid_in_meta(cached_table) is False
    # ... so the stale row the rollback restored is still not an answer.
    assert cached_table.stats.quick_count(query) is None
    assert cached_table.stats.count(query) == 0


def test_a_refresh_and_a_concurrent_write_are_serialized(cached_table, other_handle):
    """
    A refresh claims the meta_tables row before it rebuilds anything, so a
    write that overlaps it waits, and the outcome is one of the two the
    ordering allows rather than a lost invalidation.

    Here the refresh goes first: the writer's rows are not in the caches it
    builds, so the writer's ``_break_stats`` lands afterwards and the table
    ends invalid.  Without the claim, the refresh's restore would have
    overwritten an invalidation that had already committed.
    """
    query = {"flag": True}
    assert cached_table.stats.count(query, record=True) == NFLAGGED
    cached_table.update({"label": "l0"}, {"x": 7.5}, restat=False)

    writer = twin(cached_table, other_handle)
    refresh_started = threading.Event()
    write_finished = threading.Event()
    failures = []

    def write():
        try:
            refresh_started.wait(TIMEOUT)
            # Blocks on the row lock the refresh took, until it commits.
            writer.update(query, {"flag": False}, restat=False)
        except Exception as err:  # pragma: no cover - reported below
            failures.append(err)
        finally:
            write_finished.set()

    thread = threading.Thread(target=write)
    thread.start()
    try:
        # Hooked at the very start of the rebuild, before refresh_stats writes
        # anything of its own to meta_tables: later than this and the total's
        # own UPDATE has already taken the row lock, and the test would pass
        # whether or not the refresh claimed the row deliberately.
        original = cached_table.stats._status

        def midway(*args, **kwds):
            refresh_started.set()
            # Long enough for the writer to reach the lock and block on it.
            time.sleep(1)
            assert not write_finished.is_set(), (
                "the write committed in the middle of the refresh"
            )
            return original(*args, **kwds)

        cached_table.stats._status = midway
        try:
            cached_table.stats.refresh_stats()
        finally:
            del cached_table.stats._status
    finally:
        refresh_started.set()
        thread.join(TIMEOUT)

    assert not thread.is_alive(), "the writer never unblocked"
    assert not failures, failures
    # The write landed after the refresh, so the caches it built do not
    # describe it, and the table says so.
    assert stats_valid_in_meta(cached_table) is False
    assert cached_table.stats.quick_count(query) is None
    assert cached_table.stats.count(query) == 0


# ---------------------------------------------------------------------------
# every write and swap path makes the transition, in its own transaction
#
# The row-level paths always cleared the flag; the bulk and replacement paths
# did not, so a swap could leave new data under old caches and a true flag.
# Each of these starts from a valid cached count of the rows it is about to
# change.
# ---------------------------------------------------------------------------

def _write_flag_file(path, labels, value):
    """An update file setting ``flag`` to ``value`` for the given labels."""
    with open(path, "w") as F:
        F.write("label|flag\ntext|boolean\n\n")
        for label in labels:
            F.write("%s|%s\n" % (label, "t" if value else "f"))


@pytest.fixture
def flagged(cached_table):
    """
    A cached, valid count of the flagged rows: what every path below is about
    to make wrong.
    """
    query = {"flag": True}
    assert cached_table.stats.count(query, record=True) == NFLAGGED
    assert cached_table.stats.quick_count(query) == NFLAGGED
    assert stats_valid_in_meta(cached_table) is True
    return query


def assert_disowned(table, query, after=0):
    """
    The persisted flag and the public answer, after a path that changed the
    data without rebuilding the caches.
    """
    assert stats_valid_in_meta(table) is False
    assert table.stats.quick_count(query) is None
    assert table.stats.count(query) == after


def test_in_place_update_from_file_without_restat_invalidates(cached_table, flagged, tmp_path):
    datafile = str(tmp_path / "flags.txt")
    _write_flag_file(datafile, ["l%d" % i for i in range(200)], value=False)
    cached_table.update_from_file(datafile, "label", inplace=True, restat=False)
    assert_disowned(cached_table, flagged)


def test_a_swapping_update_from_file_without_restat_invalidates(cached_table, flagged, tmp_path):
    """
    The worst of the untransitioned paths: the search table is replaced and the
    old counts and stats relations are kept, so every cached answer is now
    about data that is no longer there.
    """
    datafile = str(tmp_path / "flags.txt")
    _write_flag_file(datafile, ["l%d" % i for i in range(200)], value=False)
    cached_table.update_from_file(datafile, "label", inplace=False, restat=False)
    # The old counts row really did survive the swap ...
    assert cached_table.stats._cached_count({"flag": True}) == NFLAGGED
    # ... and is not served.
    assert_disowned(cached_table, flagged)


def test_rewrite_without_restat_invalidates(cached_table, flagged):
    cached_table.rewrite(lambda rec: dict(rec, flag=False), restat=False)
    assert_disowned(cached_table, flagged)


def test_reload_without_restat_invalidates(cached_table, flagged, tmp_path):
    searchfile = str(tmp_path / "data.txt")
    cached_table.copy_to(searchfile)
    cached_table.update({"flag": True}, {"flag": False}, restat=False)
    cached_table.stats.refresh_stats()
    assert stats_valid_in_meta(cached_table) is True

    # Reload the file dumped before that change: the data goes back to having
    # flagged rows, and the counts and stats swapped in are empty clones this
    # reload never wrote, so it does not vouch for them.
    cached_table.reload(searchfile, restat=False)
    table = cached_table._db[cached_table.search_table]
    assert stats_valid_in_meta(table) is False
    assert table.stats.count({"flag": True}) == NFLAGGED


def test_a_swapping_update_from_file_with_restat_ends_valid(cached_table, tmp_path):
    """
    The other half of the contract: when the counts and stats reaching the live
    names were rebuilt for the data reaching the live name, the swap says so,
    and the rebuilt value is served.
    """
    cached_table.stats.add_stats(["flag"])
    assert cached_table.stats.quick_count({"flag": True}) == NFLAGGED

    # Start invalid, so that ending valid is a transition this swap made
    # rather than one it inherited from the table it replaced.
    cached_table.update({"label": "l0"}, {"x": 6.5}, restat=False)
    assert stats_valid_in_meta(cached_table) is False

    datafile = str(tmp_path / "flags.txt")
    _write_flag_file(datafile, ["l%d" % i for i in range(200)], value=True)
    cached_table.update_from_file(datafile, "label", inplace=False, restat=True)

    assert stats_valid_in_meta(cached_table) is True
    assert cached_table.stats.quick_count({"flag": True}) == 200


def test_a_reload_with_restat_ends_valid(cached_table, tmp_path):
    cached_table.stats.add_stats(["flag"])
    searchfile = str(tmp_path / "data.txt")
    cached_table.copy_to(searchfile)

    cached_table.update({"flag": True}, {"flag": False}, restat=False)
    assert stats_valid_in_meta(cached_table) is False

    cached_table.reload(searchfile, restat=True)
    table = cached_table._db[cached_table.search_table]
    assert stats_valid_in_meta(table) is True
    assert table.stats.quick_count({"flag": True}) == NFLAGGED


def test_a_reload_with_a_metafile_does_not_take_its_validity_from_the_file(
    cached_table, tmp_path
):
    """
    A metafile's ``stats_valid`` describes the table it was exported from at
    export time, and cannot know whether this reload rebuilt anything.  The
    swap's own answer wins, and the rest of the row still lands.
    """
    searchfile = str(tmp_path / "data.txt")
    metafile = str(tmp_path / "meta.txt")
    cached_table.copy_to(searchfile, metafile=metafile)
    with open(metafile) as F:
        assert "|t|" in F.read()  # exported while valid

    cached_table.reload(searchfile, metafile=metafile, restat=False, silence_meta=True)
    table = cached_table._db[cached_table.search_table]
    assert stats_valid_in_meta(table) is False


def test_reload_revert_invalidates(cached_table, tmp_path):
    """
    A backup carries no validity bit of its own: meta_tables has one row, and
    it stayed with the live name.  So a backup taken while the table was
    invalid can be swapped back under a flag that has since been set true, and
    the only safe answer is to clear it.
    """
    table = cached_table
    table.stats.add_stats(["flag"])
    searchfile = str(tmp_path / "data.txt")
    table.copy_to(searchfile)

    # Back up an invalid state: change the data, do not restat, then reload
    # (which files the current relations away as _old1 and ends valid).
    table.update({"flag": True}, {"flag": False}, restat=False)
    assert stats_valid_in_meta(table) is False
    table.reload(searchfile, restat=True)
    table = table._db[table.search_table]
    assert stats_valid_in_meta(table) is True

    table.reload_revert()
    assert stats_valid_in_meta(table) is False
    # and the total describes what is now live, not what was
    assert table.stats.quick_count({}) == table.stats._slow_count({}, record=False)


def test_the_flag_and_the_swap_roll_back_together(cached_table, tmp_path, monkeypatch):
    """
    The transition is written in the transaction that does the renames, so a
    failure between them cannot commit one without the other.
    """
    table = cached_table
    searchfile = str(tmp_path / "data.txt")
    table.copy_to(searchfile)
    table.update({"label": "l0"}, {"x": 4.5}, restat=False)
    assert stats_valid_in_meta(table) is False

    def boom(self, *args, **kwds):
        raise RuntimeError("the swap blew up")

    # The last thing reload_final_swap does inside its transaction, after both
    # the renames and the flag.
    monkeypatch.setattr(type(table._db), "_notify_schema_change", boom)
    with pytest.raises(RuntimeError):
        table.reload(searchfile, restat=True)
    table._db.conn.rollback()

    # Neither half committed: the flag is still what it was, and there is no
    # backup, so the renames did not happen either.  (DDL is transactional, so
    # the rollback took the _tmp relations the reload had built with it.)
    assert stats_valid_in_meta(table) is False
    assert not table._table_exists(table.search_table + "_old1")
    assert not table._table_exists(table.search_table + "_tmp")
    assert table.stats.count({}) == 200


@pytest.mark.parametrize(
    "path,after",
    [
        ("update", 0),
        ("delete", 0),
        ("upsert", 0),
        ("insert_many", 0),
        ("copy_from", 2 * NFLAGGED),
    ],
)
def test_the_row_level_paths_still_invalidate(cached_table, flagged, tmp_path, path, after):
    """
    The paths that always did, re-checked now that the transition is
    unconditional -- a table object whose copy of the flag has drifted must not
    skip it.
    """
    table = cached_table
    if path == "update":
        table.update({"flag": True}, {"flag": False}, restat=False)
    elif path == "delete":
        table.delete({"flag": True}, restat=False)
    elif path == "upsert":
        for i in range(0, 200, 3):
            table.upsert({"label": "l%d" % i}, {"flag": False})
    elif path == "insert_many":
        table.delete({"flag": True}, restat=False)
        table._restore_stats()
        table.insert_many([dict(sample_row(1000), flag=False)], restat=False)
    else:
        # include_id=False so the appended rows get fresh ids after the
        # existing ones rather than colliding with them.
        searchfile = str(tmp_path / "more.txt")
        table.copy_to(searchfile, query={"flag": True}, include_id=False)
        table.copy_from(searchfile, restat=False)
    assert_disowned(table, flagged, after)


def test_a_staged_commit_invalidates(cached_table, flagged):
    """
    A staged commit swaps in empty counts and stats tables, so whatever the
    live ones held is gone and nothing has been rebuilt.
    """
    with cached_table.staged() as staged:
        staged.update({"flag": True}, {"flag": False}, restat=False)
    table = cached_table._db[cached_table.search_table]
    assert_disowned(table, flagged)


# ---------------------------------------------------------------------------
# "may this be served" is not "is this row here"
#
# Cache maintenance chooses between INSERT and UPDATE by asking whether the row
# it is about to write already exists.  Routed through the gated lookups, that
# question is answered "no" on every invalid table -- which is every table the
# maintenance runs on -- and each recorder leaves a second row behind under a
# key the rest of the code takes to identify at most one.
# ---------------------------------------------------------------------------

def stored_counts_for(table, query, split=False):
    """
    Every count physically stored under this cache key, gate or no gate.
    """
    cols, vals = table.stats._split_dict(query)
    cur = table._execute(
        SQL(
            "SELECT count FROM {0} WHERE cols = %s AND values = %s AND split = %s"
        ).format(Identifier(table.stats.counts)),
        [cols, vals, split],
    )
    return [row[0] for row in cur]


def stored_stat_values(table, cols, kind, query={}):
    """
    Every statistic physically stored under this cache key.
    """
    from psycodict.encoding import Json

    ccols, cvals = table.stats._split_dict(query)
    cur = table._execute(
        SQL(
            "SELECT value FROM {0} WHERE stat = %s AND cols = %s "
            "AND constraint_cols = %s AND constraint_values = %s"
        ).format(Identifier(table.stats.stats)),
        [kind, Json(cols), ccols, cvals],
    )
    return [int(row[0]) for row in cur]


def test_updating_the_total_while_invalid_leaves_one_row(cached_table):
    """
    The reproducible case: a write clears the flag and then maintains the
    total, and the maintenance asked the gated lookup whether the ``{}`` row
    was there.  It said no, because the flag was false, so a second ``{}`` row
    was inserted -- by the write that had just cleared the flag itself.
    """
    assert stored_counts_for(cached_table, {}) == [200]

    cached_table.insert_many([sample_row(1000)], restat=False)
    assert stats_valid_in_meta(cached_table) is False

    assert stored_counts_for(cached_table, {}) == [201]
    assert cached_table.stats.quick_count({}) == 201


def test_recording_a_count_again_while_invalid_leaves_one_row(cached_table):
    query = {"n": {"$lt": 100}}
    assert cached_table.stats.count(query, record=True) == 100
    assert stored_counts_for(cached_table, query) == [100]

    cached_table.delete({"n": {"$lt": 50}}, restat=False)
    assert stats_valid_in_meta(cached_table) is False

    assert cached_table.stats.count(query, record=True) == 50
    # One row, holding the recomputed value -- not two that happen to agree.
    assert stored_counts_for(cached_table, query) == [50]


def test_recording_a_distinct_count_again_while_invalid_leaves_one_row(cached_table):
    """
    Also the first thing ever to run ``_record_count_distinct``'s update
    statement, which named a column ``stats`` that has always been called
    ``stat``: while the gated lookup answered this question, the update branch
    was unreachable on exactly the tables that needed it.
    """
    cols = ["flag"]
    assert cached_table.stats._slow_count_distinct(cols, record=True) == 2
    assert stored_stat_values(cached_table, cols, "distinct") == [2]

    cached_table.update({"flag": True}, {"flag": False}, restat=False)
    assert stats_valid_in_meta(cached_table) is False

    assert cached_table.stats._slow_count_distinct(cols, record=True) == 1
    assert stored_stat_values(cached_table, cols, "distinct") == [1]


@pytest.mark.parametrize("kind,before,after", [("max", 199, 99), ("min", 0, 0), ("sum", 19900, 4950)])
def test_recording_a_statistic_again_while_invalid_leaves_one_row(
    cached_table, kind, before, after
):
    assert getattr(cached_table.stats, kind)("n") == before
    assert stored_stat_values(cached_table, ["n"], kind) == [before]

    cached_table.delete({"n": {"$gte": 100}}, restat=False)
    assert stats_valid_in_meta(cached_table) is False

    assert getattr(cached_table.stats, kind)("n") == after
    assert stored_stat_values(cached_table, ["n"], kind) == [after]


def test_refreshing_an_invalid_table_leaves_every_key_unique(cached_table):
    """
    A refresh records the total through the same recorder, on a table it has
    just marked invalid, so it could duplicate the very keys it was rebuilding.
    """
    from test_stats_duplicates import duplicated_count_keys, duplicated_stat_keys

    table = cached_table
    table.stats.add_stats(["flag"])
    assert table.stats.count({"n": {"$lt": 100}}, record=True) == 100  # an extra count
    table.stats.max("n")

    table.delete({"n": {"$gte": 150}}, restat=False)
    assert stats_valid_in_meta(table) is False

    table.stats.refresh_stats()
    assert stats_valid_in_meta(table) is True

    assert duplicated_count_keys(table) == []
    assert duplicated_stat_keys(table) == []
    assert stored_counts_for(table, {}) == [150]
    assert stored_counts_for(table, {"n": {"$lt": 100}}) == [100]
    assert stored_counts_for(table, {"flag": True}) == [len([i for i in range(150) if i % 3 == 0])]


# ---------------------------------------------------------------------------
# reload_all finalizes what each reload prepared
#
# reload_all runs every reload first and swaps afterwards, so the second pass
# has to be handed the swap list the first pass built.  Reconstructing it there
# from the files in the folder is not the same list: a saving table's reload
# always prepares both cache companions, refreshing them when a file is
# missing, so a folder without a _counts.txt yielded a swap list naming only
# the search table -- leaving the refreshed companions under _tmp and the old
# live ones, describing replaced data, marked valid.
# ---------------------------------------------------------------------------

def reload_folder(table, tmp_path, counts=True, stats=True):
    """
    A ``reload_all`` input folder for one table, optionally without its cache
    files, exported before whatever change the test is about.
    """
    folder = tmp_path / ("data_%d_%d" % (counts, stats))
    table._db.copy_to([table.search_table], str(folder))
    if not counts:
        (folder / (table.search_table + "_counts.txt")).unlink()
    if not stats:
        (folder / (table.search_table + "_stats.txt")).unlink()
    return folder


def assert_coherent_after_reload_all(table, expect_valid):
    """
    Nothing stranded under _tmp, and the flag saying what the swap did.
    """
    live = table._db[table.search_table]
    for name in (live.search_table, live.stats.counts, live.stats.stats):
        assert not live._table_exists(name + "_tmp"), "%s_tmp was left behind" % name
    assert stats_valid_in_meta(live) is expect_valid
    return live


@pytest.mark.parametrize("counts,stats", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("sequential_swap", [False, True])
def test_reload_all_with_a_cache_file_missing_swaps_the_whole_family(
    cached_table, tmp_path, counts, stats, sequential_swap
):
    """
    The refreshed companions must reach the live names, and the flag may only
    be true because they did.
    """
    cached_table.stats.add_stats(["flag"])
    assert cached_table.stats.quick_count({"flag": True}) == NFLAGGED
    folder = reload_folder(cached_table, tmp_path, counts=counts, stats=stats)

    # Make the live caches disagree with what the folder holds, so that
    # serving them after the reload is visibly wrong.
    cached_table.update({"flag": True}, {"flag": False}, restat=False)
    cached_table.stats.refresh_stats()
    assert cached_table.stats.quick_count({"flag": True}) is None

    cached_table._db.reload_all(
        str(folder), restat=True, sequential_swap=sequential_swap
    )

    live = assert_coherent_after_reload_all(cached_table, expect_valid=True)
    # the rebuilt value, from the reloaded data -- not the pre-reload cache
    assert live.stats.quick_count({"flag": True}) == NFLAGGED
    assert live.stats.count({"flag": True}) == NFLAGGED


@pytest.mark.parametrize("counts,stats", [(False, False), (True, False)])
def test_reload_all_without_restat_leaves_the_table_invalid(
    cached_table, tmp_path, counts, stats
):
    """
    Retaining old caches is allowed; claiming they are valid is not.
    """
    cached_table.stats.add_stats(["flag"])
    folder = reload_folder(cached_table, tmp_path, counts=counts, stats=stats)
    cached_table.update({"flag": True}, {"flag": False}, restat=False)
    cached_table.stats.refresh_stats()

    cached_table._db.reload_all(str(folder), restat=False)

    live = assert_coherent_after_reload_all(cached_table, expect_valid=False)
    assert live.stats.quick_count({"flag": True}) is None
    assert live.stats.count({"flag": True}) == NFLAGGED


def test_reload_all_with_both_cache_files_ends_valid(cached_table, tmp_path):
    """
    The unchanged case, kept honest: both companions come from the folder, so
    the swap has all three relations and may say so.
    """
    cached_table.stats.add_stats(["flag"])
    folder = reload_folder(cached_table, tmp_path)
    cached_table.update({"flag": True}, {"flag": False}, restat=False)

    cached_table._db.reload_all(str(folder))

    live = assert_coherent_after_reload_all(cached_table, expect_valid=True)
    assert live.stats.quick_count({"flag": True}) == NFLAGGED


def test_a_deferred_swap_is_told_what_the_reload_prepared(cached_table, tmp_path):
    """
    The plan itself: a saving table's reload prepares both cache companions
    whatever files it was given, and says so, so a caller deferring the swap
    has no reason to reconstruct the list.
    """
    searchfile = str(tmp_path / "data.txt")
    cached_table.copy_to(searchfile)
    plan = cached_table.reload(searchfile, restat=True, final_swap=False)
    try:
        assert set(plan.tables) == {
            cached_table.search_table,
            cached_table.stats.counts,
            cached_table.stats.stats,
        }
        assert plan.stats_valid is True
    finally:
        cached_table.drop_tmp()
