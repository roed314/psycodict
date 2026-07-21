# -*- coding: utf-8 -*-
"""
Partial-index support in ``create_index``: a ``where=`` keyword that stores a
predicate in ``meta_indexes.whereclause`` and emits it as the ``WHERE`` clause
of ``CREATE INDEX``.  These tests use the shared suite database through the
standard fixtures, on freshly named tables, and check the predicate survives
every place an index is recorded, rebuilt or copied.

The ``whereclause`` column arrived with metadata format 1; how psycodict
behaves against databases at other formats (including a format-0 database,
where partial indexes are unavailable) is tested in tests/test_meta_formats.py.
"""
import uuid

import pytest

from psycopg.sql import SQL


##################################################################
# Helpers                                                        #
##################################################################


def fresh_name():
    return "test_%s" % uuid.uuid4().hex[:12]


def pg_indexdef(db, index_name):
    """
    The ``CREATE INDEX`` statement postgres reconstructs for an index (which
    includes any ``WHERE`` predicate), or None if no such index is built.
    """
    row = db._execute(
        SQL("SELECT indexdef FROM pg_indexes WHERE indexname = %s"), [index_name]
    ).fetchone()
    return None if row is None else row[0]


def meta_whereclause(db, table, index_name):
    """
    The ``whereclause`` recorded in meta_indexes for an index (asserting the
    row exists).
    """
    row = db._execute(
        SQL(
            "SELECT whereclause FROM meta_indexes "
            "WHERE table_name = %s AND index_name = %s"
        ),
        [table, index_name],
    ).fetchone()
    assert row is not None
    return row[0]


@pytest.fixture
def cleanup(db):
    """
    Names of tables created outside ``table_factory`` (e.g. by create_table_like),
    dropped when the test ends so the shared database is left as it was found.
    """
    names = []
    yield names
    for name in reversed(names):
        try:
            if name in db.tablenames:
                db.drop_table(name, force=True)
        except Exception:  # pragma: no cover - cleanup must not mask failures
            pass


##################################################################
# create_index(where=...)                                        #
##################################################################


def test_create_index_with_where_stores_and_builds_the_predicate(db, empty_table):
    empty_table.create_index(["n"], where="n > 0")
    name = empty_table.search_table + "_n"
    # psycodict recorded the predicate...
    assert meta_whereclause(db, empty_table.search_table, name) == "n > 0"
    # ...and postgres built a genuinely partial index with it.
    assert "WHERE (n > 0)" in pg_indexdef(db, name)


def test_plain_index_records_a_null_predicate(db, empty_table):
    empty_table.create_index(["label"])
    name = empty_table.search_table + "_label"
    assert meta_whereclause(db, empty_table.search_table, name) is None
    assert "WHERE" not in pg_indexdef(db, name)
    # list_indexes omits the key entirely for a plain index, so existing
    # callers (and their assertions) are unaffected.
    assert "where" not in empty_table.list_indexes()[name]


def test_list_indexes_reports_the_predicate(empty_table):
    empty_table.create_index(["n"], where="n > 0")
    name = empty_table.search_table + "_n"
    assert empty_table.list_indexes()[name] == {
        "type": "btree",
        "columns": ["n"],
        "modifiers": [[]],
        "where": "n > 0",
    }


def test_partial_index_name_collision_is_auto_suffixed(empty_table):
    # A plain and a partial index on the same column would generate the same
    # name; the existing numeric-suffix disambiguation keeps them distinct
    # without the caller having to name the partial one.
    empty_table.create_index(["n"])
    empty_table.create_index(["n"], where="n > 0")
    base = empty_table.search_table + "_n"
    indexes = empty_table.list_indexes()
    assert base in indexes and base + "0" in indexes
    assert "where" not in indexes[base]
    assert indexes[base + "0"]["where"] == "n > 0"


def test_partial_index_is_usable_by_a_matching_query(config, filled_table):
    # The predicate has to reach postgres, not just meta_indexes: check the
    # planner will read the partial index for a query that satisfies it.  A
    # throwaway connection carries the ``SET`` (which makes the choice
    # deterministic on a small table) so it never leaks into the shared session.
    from psycodict.database import PostgresDatabase

    filled_table.create_index(["label"], where="n >= 100")
    name = filled_table.search_table + "_label"
    other = PostgresDatabase(config=config)
    try:
        other._execute(SQL("ANALYZE {0}").format(SQL(filled_table.search_table)))
        other._execute(SQL("SET enable_seqscan = off"))
        plan = other._execute(
            SQL(
                "EXPLAIN SELECT id FROM {0} WHERE n >= 100 AND label = 'l150'"
            ).format(SQL(filled_table.search_table))
        ).fetchall()
    finally:
        other.conn.close()
    assert name in "\n".join(row[0] for row in plan)


def test_partial_index_survives_copy_reload_restore(db, empty_table, tmp_path):
    empty_table.create_index(["n"], where="n > 0")
    name = empty_table.search_table + "_n"
    indexes_file = str(tmp_path / "indexes.txt")

    empty_table.copy_to_indexes(indexes_file)
    # The exported metafile carries the predicate as its final column.
    assert "n > 0" in open(indexes_file).read()

    # Drop it everywhere, then rebuild the whole way from the file.
    empty_table.drop_index(name)
    assert name not in empty_table._list_built_indexes()
    assert empty_table.list_indexes() == {}

    empty_table.reload_indexes(indexes_file)
    assert meta_whereclause(db, empty_table.search_table, name) == "n > 0"

    empty_table.restore_index(name)
    assert "WHERE (n > 0)" in pg_indexdef(db, name)


def test_drop_partial_index_removes_it_everywhere(db, empty_table):
    empty_table.create_index(["n"], where="n > 0")
    name = empty_table.search_table + "_n"
    empty_table.drop_index(name)
    assert empty_table.list_indexes() == {}
    assert name not in empty_table._list_built_indexes()


def test_create_table_like_copies_a_partial_index(db, table_factory, cleanup):
    source = table_factory()
    source.create_index(["n"], where="n > 0")
    new_name = fresh_name()
    db.create_table_like(new_name, source, indexes=True)
    cleanup.append(new_name)

    copied = db[new_name].list_indexes()
    partial = [nm for nm, idx in copied.items() if idx.get("where") == "n > 0"]
    assert len(partial) == 1
    assert "WHERE (n > 0)" in pg_indexdef(db, partial[0])
