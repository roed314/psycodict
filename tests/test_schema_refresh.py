# -*- coding: utf-8 -*-
"""
Refreshing schema snapshots on a live connection (LMFDB/lmfdb#3150).

A ``PostgresDatabase`` reads the schema -- the list of tables, and each
table's columns and metadata -- once, at connection time.  A long-running
process whose snapshot had gone stale used to require a restart after
another process changed the schema; ``db.refresh_tables()`` updates the
snapshot in place instead.  These tests drive schema changes through a
second connection, playing the role of that other process, and check that
the first connection catches up exactly when it refreshes, on the same
table objects that application code may be holding.
"""
import uuid

import pytest

from psycopg.errors import UndefinedColumn, UndefinedTable

import conftest


@pytest.fixture
def second_db(db, config):
    """
    A factory for extra ``PostgresDatabase`` objects on the same database,
    standing in for schema changes made from another process.

    A factory rather than a plain fixture so that the second connection can
    be opened *after* the test has created its tables, and therefore sees
    them.  Depends on ``db`` so that an unreachable server skips these tests.
    """
    from psycodict.database import PostgresDatabase

    created = []

    def make():
        other = PostgresDatabase(config=config, create=True)
        created.append(other)
        return other

    yield make
    for other in created:
        other.conn.close()


def test_refresh_tables_sees_added_column(db, second_db, table_factory):
    table = table_factory()
    name = table.search_table
    table.insert_many([conftest.sample_row(i) for i in range(5)])
    second_db()[name].add_column("newcol", "integer")
    # The snapshot predates the column, so it cannot be searched on ...
    assert "newcol" not in table.search_cols
    with pytest.raises(ValueError, match="is not a column"):
        table.lucky({"newcol": 17})
    db.refresh_tables()
    # ... but after a refresh it can, on the very same object
    assert table is db[name]
    assert "newcol" in table.search_cols
    assert table.col_type["newcol"] == "integer"
    table.update({"n": 3}, {"newcol": 17})
    assert table.lucky({"newcol": 17}, "n") == 3


def test_refresh_tables_sees_dropped_column(db, second_db, table_factory):
    table = table_factory()
    name = table.search_table
    table.insert_many([conftest.sample_row(i) for i in range(5)])
    second_db()[name].drop_column("x", force=True)
    # The stale snapshot still mentions the column, so queries break -- this
    # is the failure mode that used to force a website restart ...
    assert "x" in table.search_cols
    with pytest.raises(UndefinedColumn):
        table.lucky({"x": 0.5})
    db.refresh_tables()
    # ... while after a refresh the column is gone and searches on it give
    # the normal not-a-column error
    assert "x" not in table.search_cols
    assert "x" not in table.col_type
    with pytest.raises(ValueError, match="is not a column"):
        table.lucky({"x": 0.5})
    assert table.lucky({"n": 1}, "label") == "l1"


def test_refresh_tables_sees_new_table(db, second_db):
    other = second_db()
    name = "test_%s" % uuid.uuid4().hex[:12]
    try:
        other.create_table(name, [("n", "integer"), ("label", "text")], label_col="label", sort=["n"])
        assert name not in db.tablenames
        assert not hasattr(db, name)
        db.refresh_tables()
        assert name in db.tablenames
        table = db[name]
        assert table is getattr(db, name)
        assert table.search_cols == ["label", "n"]
        # The new table is fully usable from the refreshed connection
        table.insert_many([{"n": 1, "label": "a"}, {"n": 2, "label": "b"}])
        assert table.lookup("b", "n") == 2
    finally:
        if name in other.tablenames:
            other.drop_table(name, force=True)
        db.refresh_tables()


def test_refresh_tables_sees_dropped_table(db, second_db):
    other = second_db()
    name = "test_%s" % uuid.uuid4().hex[:12]
    try:
        other.create_table(name, [("n", "integer"), ("label", "text")], label_col="label", sort=["n"])
        db.refresh_tables()
        table = db[name]
        other.drop_table(name, force=True)
        assert name in db.tablenames  # stale
        db.refresh_tables()
        assert name not in db.tablenames
        assert not hasattr(db, name)
        with pytest.raises(ValueError, match="is not a search table"):
            db[name]
        # A reference held from before the drop fails on next use, as it
        # must: the underlying table no longer exists
        with pytest.raises(UndefinedTable):
            table.lucky({"n": 1})
    finally:
        if name in other.tablenames:
            other.drop_table(name, force=True)
        db.refresh_tables()


def test_refresh_tables_sees_metadata_changes(db, second_db, table_factory):
    table = table_factory()
    name = table.search_table
    table.insert_many([conftest.sample_row(i) for i in range(5)])
    other = second_db()
    other[name].set_label("num")
    other[name].set_sort(["label"], resort=False)
    assert table._label_col == "label"
    assert table._primary_sort == "n"
    db.refresh_tables()
    assert table._label_col == "num"
    assert table._sort_orig == ["label"]
    assert table._primary_sort == "label"
    assert table._sort_keys == {"label"}
    # lookup uses the refreshed label column: sample_row(3)["num"] == 37
    assert table.lookup(37, "n") == 3


def test_refresh_tables_without_changes_is_a_noop(db, table_factory):
    table = table_factory()
    table.insert_many([conftest.sample_row(i) for i in range(3)])
    tables_before = {name: db[name] for name in db.tablenames}
    stats_before = table.stats
    search_cols_before = list(table.search_cols)
    col_type_before = dict(table.col_type)
    sort_before = table._sort_orig
    total_before = table.stats.total
    db.refresh_tables()
    assert db.tablenames == sorted(tables_before)
    # The same objects: references held by application code stay valid
    for name, tab in tables_before.items():
        assert db[name] is tab
    assert table.stats is stats_before
    assert table.search_cols == search_cols_before
    assert table.col_type == col_type_before
    assert table._sort_orig == sort_before
    assert table._label_col == "label"
    assert table.stats.total == total_before == 3
    assert table.lookup("l1", "n") == 1


def test_single_table_refresh(db, second_db, table_factory):
    # PostgresTable._refresh with no arguments reads its own meta_tables row
    # and columns, refreshing just this table
    table = table_factory()
    untouched = table_factory()
    second_db()[table.search_table].add_column("extra_col", "text")
    table._refresh()
    assert "extra_col" in table.search_cols
    assert table.col_type["extra_col"] == "text"
    assert "extra_col" not in untouched.search_cols
