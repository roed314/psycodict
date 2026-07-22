# -*- coding: utf-8 -*-
"""
Tests for staged (transactional) uploads: the ``staged`` context manager.

``table.staged()`` copies the search table to a ``_tmp`` suffixed table,
yields a table object pointed at the copy, and on a clean exit swaps the copy
into place with the same rename dance as ``reload`` (keeping the previous
version as ``_old1`` so that ``reload_revert``/``cleanup_from_reload`` apply).
On an exception the copy is dropped and the live table was never touched.

The live table is queried mid-context throughout, always from the same
connection, since the whole point is that readers never see a half-edited
table.
"""
import pytest
from psycopg import DataError, IntegrityError
from psycopg.sql import SQL, Identifier

from psycodict.database import PostgresDatabase
from psycodict.utils import LockError

from conftest import sample_row


def _num_rows(table):
    """
    The number of rows actually stored in the search table.
    """
    return len(list(table.search({}, projection="id")))


def _all_rows(table):
    """
    Every row of the table including its id, ordered by id.
    """
    return sorted(table.search({}, projection=3), key=lambda rec: rec["id"])


def _tmp_leftovers(table):
    """
    All _tmp suffixed postgres tables associated with the search table.
    """
    name = table.search_table
    return [
        t for t in table._all_tablenames()
        if t.startswith(name) and t.endswith("_tmp")
    ]


def _index_names(table, tablename):
    """
    The names of the postgres indexes on ``tablename``.
    """
    return [
        row[0] for row in table._execute(
            SQL("SELECT indexname FROM pg_indexes WHERE tablename = %s"), [tablename]
        )
    ]


##################################################################
# the commit path                                                #
##################################################################


def test_staged_commit_applies_writes_without_touching_live_midway(db, filled_table):
    name = filled_table.search_table
    filled_table.create_index(["label"])
    with filled_table.staged() as staged:
        staged.insert_many([sample_row(i) for i in range(200, 210)])
        staged.update({"n": 5}, {"label": "staged-update"})
        staged.delete({"n": {"$gte": 150, "$lt": 200}})
        # The live table is untouched mid-context, on the same connection.
        assert _num_rows(filled_table) == 200
        assert filled_table.lucky({"n": 5}, projection="label") == "l5"
        assert filled_table.count({"n": {"$gte": 150, "$lt": 200}}) == 50
        assert filled_table.count({"n": {"$gte": 200}}) == 0
        # The staged handle sees the pending state.
        assert staged.lucky({"n": 5}, projection="label") == "staged-update"
        assert staged.count({"n": {"$gte": 200}}) == 10
        assert staged.count({}) == 160
    # After the swap the database hands out a fresh table object, as reload
    # does after its final swap.
    table = db[name]
    assert table is not filled_table
    assert _num_rows(table) == 160
    assert table.lucky({"n": 5}, projection="label") == "staged-update"
    assert table.count({"n": {"$gte": 150, "$lt": 200}}) == 0
    assert table.count({"n": {"$gte": 200}}) == 10
    # Surviving rows keep their ids and inserted rows continue from max_id.
    assert table.lucky({"n": 42}, projection="id") == 42
    assert sorted(table.search({"n": {"$gte": 200}}, projection="id")) == list(range(200, 210))
    assert table.max_id() == 209
    # The primary key and the index recorded in meta_indexes were built on
    # the swapped-in table.
    built = set(table._list_built_indexes())
    assert name + "_pkey" in built
    assert name + "_label" in built
    # The previous version is kept as a reload-style backup and no _tmp
    # tables remain.
    assert table._table_exists(name + "_old1")
    assert _tmp_leftovers(table) == []
    # The swap invalidates statistics rather than refreshing them.
    assert table._stats_valid is False


def test_staged_upsert_and_rewrite(db, filled_table):
    name = filled_table.search_table

    def bump(rec):
        rec["num"] = rec["n"] + 1
        return rec

    with filled_table.staged() as staged:
        new_row, row_id = staged.upsert({"label": "l5"}, {"n": 5000})
        assert new_row is False
        assert row_id == 5
        new_row, row_id = staged.upsert({"label": "brand-new"}, {"n": 6000})
        assert new_row is True
        assert row_id == 200
        staged.rewrite(bump, query={"n": {"$lt": 3}})
        # None of it hit the live table.
        assert filled_table.lucky({"label": "l5"}, projection="n") == 5
        assert filled_table.lucky({"label": "brand-new"}, projection="n") is None
        assert filled_table.lucky({"n": 1}, projection="num") == 17
    table = db[name]
    assert table.lucky({"label": "l5"}, projection="n") == 5000
    assert table.lucky({"label": "brand-new"}, projection="n") == 6000
    assert table.lucky({"n": 1}, projection="num") == 2
    assert _num_rows(table) == 201


def test_staged_on_an_empty_table_inserts_from_id_zero(db, empty_table):
    name = empty_table.search_table
    with empty_table.staged() as staged:
        staged.insert_many([sample_row(i) for i in range(3)])
        assert _num_rows(empty_table) == 0
    table = db[name]
    assert [rec["id"] for rec in _all_rows(table)] == [0, 1, 2]
    assert table.max_id() == 2


def test_staged_with_no_writes_swaps_an_identical_copy(db, filled_table):
    name = filled_table.search_table
    before = _all_rows(filled_table)
    with filled_table.staged():
        pass
    table = db[name]
    assert _all_rows(table) == before
    assert table._table_exists(name + "_old1")


##################################################################
# the exception path                                             #
##################################################################


def test_staged_exception_drops_the_copy_and_preserves_live(db, filled_table):
    name = filled_table.search_table
    before = _all_rows(filled_table)
    with pytest.raises(RuntimeError, match="boom"):
        with filled_table.staged() as staged:
            staged.insert_many([sample_row(777)])
            staged.delete({})
            # The staged tables exist while the context is active.
            assert filled_table._table_exists(name + "_tmp")
            raise RuntimeError("boom")
    assert _all_rows(filled_table) == before
    assert db[name] is filled_table
    # No _tmp leftovers in pg_tables, and no backup was created either.
    assert _tmp_leftovers(filled_table) == []
    for ext in ["", "_counts", "_stats"]:
        assert not filled_table._table_exists(name + ext + "_tmp")
    assert not filled_table._table_exists(name + "_old1")


def test_staged_aborts_cleanly_after_a_failed_write(db, filled_table):
    before = _all_rows(filled_table)
    # A write rejected by validation, before any SQL runs.
    with pytest.raises(ValueError):
        with filled_table.staged() as staged:
            staged.insert_many([{"nosuchcol": 1}])
    assert _all_rows(filled_table) == before
    assert _tmp_leftovers(filled_table) == []
    # A write that fails inside postgres, leaving behind an aborted
    # transaction that the cleanup must clear before it can drop the copy.
    with pytest.raises(DataError):
        with filled_table.staged() as staged:
            staged.insert_many([{"n": "not an integer", "label": "x"}])
    assert _all_rows(filled_table) == before
    assert _tmp_leftovers(filled_table) == []


##################################################################
# the revert path                                                #
##################################################################


def test_staged_swap_can_be_reverted_and_reapplied(db, filled_table):
    name = filled_table.search_table
    with filled_table.staged() as staged:
        staged.delete({"n": {"$gte": 100}})
    table = db[name]
    assert _num_rows(table) == 100
    # The reload undo machinery applies to staged swaps unchanged.
    table.reload_revert()
    assert _num_rows(db[name]) == 200
    db[name].reload_revert()
    assert _num_rows(db[name]) == 100


def test_cleanup_from_reload_drops_the_staged_backup(db, filled_table):
    name = filled_table.search_table
    with filled_table.staged() as staged:
        staged.delete({"n": 0})
    table = db[name]
    assert table._table_exists(name + "_old1")
    table.cleanup_from_reload()
    for ext in ["", "_counts", "_stats"]:
        assert not table._table_exists(name + ext + "_old1")
    assert _num_rows(db[name]) == 199


##################################################################
# concurrent staging                                             #
##################################################################


def test_concurrent_staged_contexts_fail_fast_at_enter(db, filled_table):
    name = filled_table.search_table
    with filled_table.staged() as staged:
        staged.update({"n": 0}, {"label": "first"})
        with pytest.raises(ValueError, match="already exists"):
            with filled_table.staged():
                pass
        # The failed attempt did not disturb the active staging.
        assert filled_table._table_exists(name + "_tmp")
        assert staged.lucky({"n": 0}, projection="label") == "first"
    assert db[name].lucky({"n": 0}, projection="label") == "first"


def test_staged_leftover_tmp_is_reported_and_drop_tmp_recovers(db, filled_table):
    name = filled_table.search_table
    # Simulate a staging interrupted by a crash after its copy committed.
    filled_table._clone(name, name + "_tmp")
    with pytest.raises(ValueError, match="drop_tmp"):
        with filled_table.staged():
            pass
    filled_table.drop_tmp()
    with filled_table.staged() as staged:
        staged.update({"n": 0}, {"label": "recovered"})
    assert db[name].lucky({"n": 0}, projection="label") == "recovered"


##################################################################
# concurrent writes to the live table                            #
##################################################################


def test_live_writes_blocked_while_staged(db, config, filled_table):
    name = filled_table.search_table
    # A separate database connection, as a concurrent uploader would have.
    other = PostgresDatabase(config=config)
    try:
        other_table = other[name]
        with pytest.raises(RuntimeError, match="boom"):
            with filled_table.staged() as staged:
                staged.insert_many([sample_row(200)])
                # psycodict writes to the live table are refused, from the
                # staging connection and from any other, so that they cannot
                # be silently discarded by the swap.
                with pytest.raises(LockError, match="staged"):
                    filled_table.update({"n": 0}, {"label": "lost"})
                with pytest.raises(LockError, match="drop_tmp"):
                    other_table.insert_many([sample_row(300)])
                # Reads on the live table are unaffected.
                assert filled_table.lucky({"n": 0}, projection="label") == "l0"
                raise RuntimeError("boom")
        # Once the context is aborted the copy is gone and writes work again.
        other_table.insert_many([sample_row(300)])
        assert filled_table.lucky({"n": 300}, projection="id") == 200
        assert filled_table.lucky({"n": 0}, projection="label") == "l0"
    finally:
        other.conn.close()


def test_raw_sql_drift_aborts_the_swap(db, config, filled_table):
    name = filled_table.search_table
    other = PostgresDatabase(config=config)
    try:
        with pytest.raises(RuntimeError, match="changed during staging"):
            with filled_table.staged() as staged:
                staged.update({"n": 1}, {"label": "staged-edit"})
                # A raw insert from another connection slips past the guard
                # in _check_locks; the commit-time check refuses the swap.
                other._execute(
                    SQL("INSERT INTO {0} (id, n, label) VALUES (%s, %s, %s)").format(Identifier(name)),
                    [200, 200, "raw"],
                )
        # The live table is intact, including the raw row, and nothing was
        # swapped in.
        assert _num_rows(filled_table) == 201
        assert filled_table.lucky({"n": 1}, projection="label") == "l1"
        assert filled_table.lucky({"n": 200}, projection="label") == "raw"
        assert db[name] is filled_table
        # The staged work is kept for repair, as after a failed swap;
        # drop_tmp discards it.
        for ext in ["", "_counts", "_stats"]:
            assert filled_table._table_exists(name + ext + "_tmp")
        filled_table.drop_tmp()
        assert _tmp_leftovers(filled_table) == []
    finally:
        other.conn.close()


def test_drift_error_explains_both_drop_and_force_swap(db, config, filled_table):
    name = filled_table.search_table
    other = PostgresDatabase(config=config)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            with filled_table.staged() as staged:
                staged.update({"n": 1}, {"label": "staged-edit"})
                other._execute(
                    SQL("INSERT INTO {0} (id, n, label) VALUES (%s, %s, %s)").format(Identifier(name)),
                    [200, 200, "raw"],
                )
        msg = str(excinfo.value)
        # Both ways forward are spelled out: discard, or force the swap.
        assert "drop_tmp()" in msg
        assert "restore_indexes(suffix='_tmp')" in msg
        assert "reload_final_swap()" in msg
        filled_table.drop_tmp()
    finally:
        other.conn.close()


def test_drift_force_swap_recipe_adopts_the_staged_copy(db, config, filled_table):
    # Following the recipe in the drift error swaps the staged copy in,
    # discarding the concurrent change -- and leaves the live table correctly
    # indexed, with no staging-only index left behind.
    name = filled_table.search_table
    other = PostgresDatabase(config=config)
    try:
        with pytest.raises(RuntimeError, match="reload_final_swap"):
            with filled_table.staged() as staged:
                staged.update({"n": 1}, {"label": "staged-edit"})
                other._execute(
                    SQL("INSERT INTO {0} (id, n, label) VALUES (%s, %s, %s)").format(Identifier(name)),
                    [200, 200, "raw"],
                )
        # The recipe: rebuild the copy's indexes (rolled back with the refused
        # swap) and swap it in.
        filled_table.restore_indexes(suffix="_tmp")
        filled_table.reload_final_swap()
        live = db[name]
        # The staged edit won and the concurrent raw row was discarded.
        assert live.lucky({"n": 1}, projection="label") == "staged-edit"
        assert live.lucky({"n": 200}, projection="label") is None
        assert _num_rows(live) == 200
        # No staging-only label index rode into the live table.
        assert not any("staged_label" in idx for idx in _index_names(live, name))
        live.cleanup_from_reload()
        assert _tmp_leftovers(live) == []
    finally:
        other.conn.close()


def test_staged_copy_has_a_label_index_for_lookups(db, filled_table):
    name = filled_table.search_table
    with filled_table.staged() as staged:
        # The copy carries a plain index on the label column (not just the
        # pkey), so label-keyed staged writes are index scans.
        staged_idxs = _index_names(filled_table, name + "_tmp")
        assert sum("staged_label" in idx for idx in staged_idxs) == 1
        staged.update({"label": "l5"}, {"num": 12345})
    # The staging index is not one of the real indexes, so it must not survive
    # the swap into the live table.
    assert not any("staged_label" in idx for idx in _index_names(db[name], name))


##################################################################
# sort order and id integrity after the swap                     #
##################################################################


def test_staged_swap_preserves_default_sort_and_id_invariants(db, filled_table):
    name = filled_table.search_table
    with filled_table.staged() as staged:
        staged.insert_many([sample_row(i) for i in range(200, 205)])
    table = db[name]
    # Search with the default sort (["n"]) works on the swapped-in table.
    ns = list(table.search({}, projection="n"))
    assert ns == sorted(ns)
    assert ns == list(range(205))
    assert table.min_id() == 0
    assert table.max_id() == 204
    # Later writes continue from the swapped-in ids.
    new_row, row_id = table.upsert({"label": "after-swap"}, {"n": 999})
    assert new_row is True
    assert row_id == 205


##################################################################
# constraints                                                    #
##################################################################


def test_staged_swap_restores_constraints(db, filled_table):
    name = filled_table.search_table
    filled_table.create_constraint(["label"], "unique")
    with filled_table.staged() as staged:
        staged.insert_many([sample_row(200)])
    table = db[name]
    assert name + "_c_label" in table._list_built_constraints()
    # The restored constraint is enforced on the swapped-in table.
    with pytest.raises(IntegrityError):
        table.insert_many([dict(sample_row(300), label="l5")])
    assert _num_rows(table) == 201


def test_staged_constraint_violation_fails_the_swap_and_keeps_the_copy(db, filled_table):
    name = filled_table.search_table
    filled_table.create_constraint(["label"], "unique")
    before = _all_rows(filled_table)
    # The copy carries no constraints during staging, so the duplicate is
    # only caught when the constraint is rebuilt at swap time -- at which
    # point it stops the swap instead of corrupting the live table.
    with pytest.raises(IntegrityError):
        with filled_table.staged() as staged:
            staged.insert_many([dict(sample_row(200), label="l5")])
    assert _all_rows(filled_table) == before
    assert db[name] is filled_table
    # The staged work is kept for repair, as after a failed reload; drop_tmp
    # discards it.
    assert filled_table._table_exists(name + "_tmp")
    filled_table.drop_tmp()
    assert _tmp_leftovers(filled_table) == []


##################################################################
# schema changes are out of scope inside staging                 #
##################################################################


def test_staged_blocks_schema_changes(db, filled_table):
    name = filled_table.search_table
    with filled_table.staged() as staged:
        for blocked in [
            lambda: staged.add_column("extra_note", "text"),
            lambda: staged.drop_column("num", force=True),
            lambda: staged.create_index(["label"]),
            lambda: staged.set_sort(["label"]),
            lambda: staged.reload("nofile"),
            lambda: staged.reload_revert(),
            lambda: staged.staged(),
        ]:
            with pytest.raises(ValueError, match="not supported on a staged table"):
                blocked()
        with pytest.raises(ValueError, match="in place"):
            staged.update_from_file("nofile", inplace=False)
    # The failed calls did not poison the context: the swap still happens.
    assert _num_rows(db[name]) == 200
    assert "extra_note" not in db[name].search_cols


def _has_pkey(db, tablename):
    cur = db._execute(
        SQL("SELECT COUNT(*) FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'p'"),
        [tablename],
    )
    return cur.fetchone()[0] == 1


def test_staged_blocks_pkey_and_meta_tampering(db, filled_table):
    name = filled_table.search_table
    with filled_table.staged() as staged:
        # The commit only rebuilds what meta_indexes and meta_constraints
        # record, which does not include the primary key, so a drop_pkeys
        # here would strip it from the swapped-in table for good; and the
        # meta reload/revert methods would write meta_* rows keyed on the
        # scratch name.
        for blocked in [
            lambda: staged.drop_pkeys(),
            lambda: staged.restore_pkeys(),
            lambda: staged.reload_meta("nofile"),
            lambda: staged.reload_indexes("nofile"),
            lambda: staged.reload_constraints("nofile"),
            lambda: staged.revert_meta(),
            lambda: staged.revert_indexes(),
            lambda: staged.revert_constraints(),
        ]:
            with pytest.raises(ValueError, match="not supported on a staged table"):
                blocked()
        staged.insert_many([sample_row(200)])
    # The swapped-in live table still has its primary key.
    table = db[name]
    assert _num_rows(table) == 201
    assert _has_pkey(db, name)
    assert name + "_pkey" in table._list_built_indexes()


def test_staged_bulk_insert_over_reindex_threshold_keeps_pkey(db, filled_table):
    # More than 1000 rows makes insert_many default to dropping the primary
    # key around the insert and rebuilding it afterward; on the staged
    # handle that pair is blocked, so the handle forces reindex off and the
    # key must survive to the swap.
    name = filled_table.search_table
    with filled_table.staged() as staged:
        staged.insert_many([sample_row(i) for i in range(200, 1401)])
    table = db[name]
    assert _num_rows(table) == 1401
    assert table.max_id() == 1400
    assert _has_pkey(db, name)


##################################################################
# per-column storage settings survive the swap                   #
##################################################################


def test_staged_swap_preserves_column_storage(db, filled_table):
    name = filled_table.search_table
    version = int(db._execute(
        SQL("SELECT current_setting('server_version_num')")
    ).fetchone()[0])
    db._execute(SQL("ALTER TABLE {0} ALTER COLUMN label SET STORAGE EXTERNAL").format(Identifier(name)))
    if version >= 140000:
        db._execute(SQL("ALTER TABLE {0} ALTER COLUMN label SET COMPRESSION pglz").format(Identifier(name)))

    def att(prop):
        cur = db._execute(
            SQL("SELECT {0} FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s").format(Identifier(prop)),
            [name, "label"],
        )
        return cur.fetchone()[0]

    with filled_table.staged() as staged:
        staged.update({"n": 0}, {"label": "stored"})
    # The swapped-in table is the staged clone, so a bare CREATE TABLE LIKE
    # would have silently reset these to the defaults.
    assert att("attstorage") == "e"
    if version >= 140000:
        assert att("attcompression") == "p"
    assert db[name].lucky({"n": 0}, projection="label") == "stored"
