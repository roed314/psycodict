# -*- coding: utf-8 -*-
"""
Schema, metadata and database level operations.

These tests exercise the DDL side of psycodict: bootstrapping the six meta
tables, creating and dropping search tables, adding and removing columns, and
managing indexes and constraints.  The recurring theme is that psycodict keeps
its own record of the schema in ``meta_tables``, ``meta_indexes`` and
``meta_constraints``, so almost every test checks both what postgres thinks and
what psycodict recorded.
"""
import uuid

import pytest

from psycopg2.sql import SQL, Identifier

from psycodict.base import (
    _meta_tables_cols,
    _meta_tables_types,
    _meta_tables_defaults,
    _meta_indexes_cols,
    _meta_indexes_types,
    _meta_constraints_cols,
    _meta_constraints_types,
)
from psycodict.table import (
    _counts_cols,
    _counts_types,
    _stats_cols,
    _stats_types,
)

import conftest


META_BASE_TABLES = ["meta_tables", "meta_indexes", "meta_constraints"]
META_TABLES = META_BASE_TABLES + [name + "_hist" for name in META_BASE_TABLES]


def fresh_name():
    """
    A table name that no other test (or run) will collide with.
    """
    return "test_%s" % uuid.uuid4().hex[:12]


def pg_columns(db, table):
    """
    The (column name, type) pairs of a postgres table, in creation order.
    """
    cur = db._execute(
        SQL(
            "SELECT column_name, udt_name::regtype FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position"
        ),
        [table],
    )
    return [(name, str(typ)) for name, typ in cur]


def pg_defaults(db, table):
    """
    A dictionary of column defaults (as SQL source) for a postgres table.
    """
    cur = db._execute(
        SQL(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name = %s"
        ),
        [table],
    )
    return {name: default for name, default in cur}


def pg_row_count(db, table):
    return db._execute(SQL("SELECT count(*) FROM {0}").format(Identifier(table))).fetchone()[0]


def pkey_columns(db, table):
    """
    The columns of the primary key of a postgres table.
    """
    cur = db._execute(
        SQL(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = %s::regclass AND i.indisprimary"
        ),
        [table],
    )
    return sorted(rec[0] for rec in cur)


def meta_tables_row(db, name):
    """
    The ``meta_tables`` row for a search table, as a dictionary.
    """
    cur = db._execute(
        SQL("SELECT {0} FROM meta_tables WHERE name = %s").format(
            SQL(", ").join(map(Identifier, _meta_tables_cols))
        ),
        [name],
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    return dict(zip(_meta_tables_cols, rows[0]))


def meta_index_row(db, table, index_name):
    cur = db._execute(
        SQL(
            "SELECT type, columns, modifiers, storage_params FROM meta_indexes "
            "WHERE table_name = %s AND index_name = %s"
        ),
        [table, index_name],
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    return dict(zip(["type", "columns", "modifiers", "storage_params"], rows[0]))


def index_reloptions(db, index_name):
    cur = db._execute(
        SQL("SELECT reloptions FROM pg_class WHERE relname = %s"), [index_name]
    )
    return cur.fetchone()[0]


@pytest.fixture
def transient(db):
    """
    Names of tables created by a test outside ``table_factory``.

    Appending a name to the returned list schedules it for dropping, since the
    test database is shared and must be left as it was found.
    """
    names = []
    yield names
    for name in reversed(names):
        try:
            if name in db.tablenames:
                db[name].set_importance(False)
                db.drop_table(name, force=True)
        except Exception:  # pragma: no cover - cleanup must not mask failures
            pass


@pytest.fixture
def hist_cleanup(db):
    """
    Names of tables whose ``meta_*_hist`` rows should be removed afterwards.

    ``drop_table`` deliberately keeps the history rows, so tests that write to
    the ``_hist`` tables have to tidy up themselves.
    """
    names = []
    yield names
    for name in names:
        for meta in ["meta_tables_hist", "meta_indexes_hist", "meta_constraints_hist"]:
            col = "name" if meta.startswith("meta_tables") else "table_name"
            try:
                db._execute(
                    SQL("DELETE FROM {0} WHERE {1} = %s").format(
                        Identifier(meta), Identifier(col)
                    ),
                    [name],
                )
            except Exception:  # pragma: no cover - cleanup must not mask failures
                pass


##################################################################
# The six meta tables                                            #
##################################################################


def test_meta_tables_exist(db):
    existing = set(db._all_tablenames())
    assert set(META_TABLES) <= existing


def test_bootstrap_meta_is_idempotent(db):
    before_cols = {name: pg_columns(db, name) for name in META_TABLES}
    before_counts = {name: pg_row_count(db, name) for name in META_TABLES}
    db._bootstrap_meta()
    assert {name: pg_columns(db, name) for name in META_TABLES} == before_cols
    assert {name: pg_row_count(db, name) for name in META_TABLES} == before_counts


def test_reconnecting_with_create_sees_the_same_tables(db, config):
    from psycodict.database import PostgresDatabase

    other = PostgresDatabase(config=config, create=True)
    try:
        assert other.is_alive()
        assert sorted(other.tablenames) == sorted(db.tablenames)
    finally:
        other.conn.close()


@pytest.mark.parametrize(
    "meta_name,cols,types",
    [
        ("meta_tables", _meta_tables_cols, _meta_tables_types),
        ("meta_indexes", _meta_indexes_cols, _meta_indexes_types),
        ("meta_constraints", _meta_constraints_cols, _meta_constraints_types),
    ],
)
def test_meta_ddl_matches_declaration(db, meta_name, cols, types):
    expected = [(col, types[col]) for col in cols]
    assert pg_columns(db, meta_name) == expected
    assert pg_columns(db, meta_name + "_hist") == expected + [("version", "integer")]


def test_meta_tables_defaults_match_declaration(db):
    for name in ["meta_tables", "meta_tables_hist"]:
        defaults = pg_defaults(db, name)
        for col in _meta_tables_cols:
            assert defaults[col] == _meta_tables_defaults.get(col)


def test_meta_indexes_and_constraints_have_no_defaults(db):
    for meta_name in ["meta_indexes", "meta_constraints"]:
        for name in [meta_name, meta_name + "_hist"]:
            assert set(pg_defaults(db, name).values()) == {None}


def test_meta_tables_defaults_are_applied_to_new_rows(db, empty_table):
    row = meta_tables_row(db, empty_table.search_table)
    assert row["count_cutoff"] == 1000
    assert row["stats_valid"] is True
    assert row["total"] == 0
    assert row["important"] is False
    assert row["include_nones"] is False


##################################################################
# create_table                                                   #
##################################################################


def test_create_table_from_list_of_pairs(db, empty_table):
    expected = dict(conftest.COLUMNS)
    expected["id"] = "bigint"
    assert dict(pg_columns(db, empty_table.search_table)) == expected


def test_create_table_from_dict_of_types(db, transient):
    name = fresh_name()
    db.create_table(
        name,
        {"integer": "n", "text": ["label", "kind"], "jsonb": ["data"]},
        label_col="label",
        sort=["n"],
    )
    transient.append(name)
    assert db[name].search_cols == ["data", "kind", "label", "n"]
    assert dict(pg_columns(db, name)) == {
        "id": "bigint",
        "n": "integer",
        "label": "text",
        "kind": "text",
        "data": "jsonb",
    }


def test_create_table_adds_id_primary_key(db, empty_table):
    name = empty_table.search_table
    assert empty_table.col_type["id"] == "bigint"
    assert empty_table.has_id
    assert pkey_columns(db, name) == ["id"]
    assert db._constraint_exists(name + "_pkey", name)


def test_create_table_honors_id_type(db, transient):
    name = fresh_name()
    db.create_table(name, conftest.COLUMNS, label_col="label", sort=["n"], id_type="integer")
    transient.append(name)
    assert db[name].col_type["id"] == "integer"
    assert dict(pg_columns(db, name))["id"] == "integer"
    assert pkey_columns(db, name) == ["id"]


def test_create_table_creates_counts_and_stats_tables(db, empty_table):
    counts = empty_table.search_table + "_counts"
    stats = empty_table.search_table + "_stats"
    assert pg_columns(db, counts) == [(col, _counts_types[col]) for col in _counts_cols]
    assert pg_defaults(db, counts)["split"] == "false"
    assert pg_columns(db, stats) == [(col, _stats_types[col]) for col in _stats_cols]


def test_create_table_records_row_in_meta_tables(db, empty_table):
    row = meta_tables_row(db, empty_table.search_table)
    assert row["name"] == empty_table.search_table
    assert row["sort"] == ["n"]
    assert row["label_col"] == "label"
    assert row["id_ordered"] is True
    assert row["out_of_order"] is False
    assert row["has_extras"] is False


def test_create_table_without_sort_is_not_id_ordered(db, transient):
    name = fresh_name()
    db.create_table(name, conftest.COLUMNS, label_col="label", sort=None)
    transient.append(name)
    row = meta_tables_row(db, name)
    assert row["sort"] is None
    assert row["id_ordered"] is False
    assert row["out_of_order"] is True


def test_create_table_registers_the_table_on_the_database(db, empty_table):
    name = empty_table.search_table
    assert name in db.tablenames
    assert db.tablenames == sorted(db.tablenames)
    assert db[name] is empty_table
    assert getattr(db, name) is empty_table


def test_create_table_with_extra_columns(db, transient):
    name = fresh_name()
    db.create_table(
        name,
        conftest.COLUMNS,
        label_col="label",
        sort=["n"],
        extra_columns={"text": ["notes"], "jsonb": ["blob"]},
    )
    transient.append(name)
    table = db[name]
    assert table.extra_table == name + "_extras"
    assert table.extra_cols == ["blob", "notes"]
    assert "notes" not in table.search_cols
    assert table.col_type["notes"] == "text"
    assert dict(pg_columns(db, table.extra_table)) == {
        "id": "bigint",
        "notes": "text",
        "blob": "jsonb",
    }
    assert pkey_columns(db, table.extra_table) == ["id"]
    assert meta_tables_row(db, name)["has_extras"] is True


def test_create_table_validates_columns_label_and_sort(db, empty_table):
    with pytest.raises(ValueError):
        db.create_table(empty_table.search_table, conftest.COLUMNS, label_col="label")
    with pytest.raises(ValueError):
        db.create_table(fresh_name(), conftest.COLUMNS, label_col="nonexistent")
    with pytest.raises(ValueError):
        db.create_table(fresh_name(), conftest.COLUMNS, label_col="label", sort=["nonexistent"])
    with pytest.raises(ValueError):
        db.create_table(fresh_name(), [("n", "integer"), ("n", "text")], label_col="n")
    with pytest.raises(RuntimeError):
        db.create_table(fresh_name(), [("n", "no_such_type")], label_col="n")


def test_create_table_force_description(db, transient):
    name = fresh_name()
    with pytest.raises(ValueError):
        db.create_table(name, [("n", "integer")], label_col="n", force_description=True)
    with pytest.raises(ValueError):
        db.create_table(
            name,
            [("n", "integer"), ("label", "text")],
            label_col="n",
            table_description="things",
            col_description={"n": "a number"},
            force_description=True,
        )
    assert name not in db.tablenames
    db.create_table(
        name,
        [("n", "integer")],
        label_col="n",
        table_description="things",
        col_description={"n": "a number"},
        force_description=True,
    )
    transient.append(name)
    assert name in db.tablenames


##################################################################
# create_table_like                                              #
##################################################################


def test_create_table_like_copies_the_schema(db, transient):
    source = fresh_name()
    db.create_table(
        source,
        conftest.COLUMNS,
        label_col="label",
        sort=["n"],
        extra_columns={"text": ["notes"]},
    )
    transient.append(source)
    target = fresh_name()
    db.create_table_like(target, db[source])
    transient.append(target)
    copy = db[target]
    assert copy.search_cols == db[source].search_cols
    assert copy.extra_cols == db[source].extra_cols
    assert {col: copy.col_type[col] for col in copy.search_cols} == {
        col: db[source].col_type[col] for col in copy.search_cols
    }
    row = meta_tables_row(db, target)
    assert row["sort"] == ["n"]
    assert row["label_col"] == "label"
    assert row["has_extras"] is True


def test_create_table_like_can_copy_data_and_indexes(db, transient):
    source = fresh_name()
    db.create_table(source, conftest.COLUMNS, label_col="label", sort=["n"])
    transient.append(source)
    db[source].insert_many([conftest.sample_row(i) for i in range(5)])
    db[source].create_index(["label"])
    target = fresh_name()
    db.create_table_like(target, db[source], data=True, indexes=True)
    transient.append(target)
    copy = db[target]
    assert copy.count({"n": {"$gte": 0}}) == 5
    assert copy.lookup("l3")["n"] == 3
    assert [idx["columns"] for idx in copy.list_indexes().values()] == [["label"]]
    assert target + "_label" in copy._list_built_indexes()


@pytest.mark.xfail(
    strict=True,
    reason="create_table_like does not pass the source id_type on to create_table, "
           "so an integer id is silently widened to bigint",
)
def test_create_table_like_preserves_id_type(db, transient):
    source = fresh_name()
    db.create_table(
        source, conftest.COLUMNS, label_col="label", sort=["n"], id_type="integer"
    )
    transient.append(source)
    target = fresh_name()
    db.create_table_like(target, db[source])
    transient.append(target)
    assert db[target].col_type["id"] == "integer"


##################################################################
# drop_table                                                     #
##################################################################


def test_drop_table_drops_the_table_and_its_companions(db, table_factory):
    table = table_factory(extra_columns={"text": ["notes"]})
    name = table.search_table
    companions = [name, name + "_extras", name + "_counts", name + "_stats"]
    assert set(companions) <= set(db._all_tablenames())
    db.drop_table(name, force=True)
    assert not (set(companions) & set(db._all_tablenames()))
    assert name not in db.tablenames
    assert not hasattr(db, name)


def test_drop_table_removes_all_meta_rows(db, table_factory):
    table = table_factory()
    name = table.search_table
    table.create_index(["label"])
    table.create_constraint(["label"], "unique")
    db.drop_table(name, force=True)
    for meta, col in [
        ("meta_tables", "name"),
        ("meta_indexes", "table_name"),
        ("meta_constraints", "table_name"),
    ]:
        cur = db._execute(
            SQL("SELECT count(*) FROM {0} WHERE {1} = %s").format(
                Identifier(meta), Identifier(col)
            ),
            [name],
        )
        assert cur.fetchone()[0] == 0


def test_drop_table_refuses_an_important_table(db, table_factory):
    table = table_factory()
    name = table.search_table
    table.set_importance(True)
    try:
        assert meta_tables_row(db, name)["important"] is True
        with pytest.raises(ValueError):
            db.drop_table(name, force=True)
        assert name in db.tablenames
        assert name in db._all_tablenames()
    finally:
        table.set_importance(False)


def test_drop_table_succeeds_once_importance_is_cleared(db, table_factory):
    table = table_factory()
    name = table.search_table
    table.set_importance(True)
    table.set_importance(False)
    assert meta_tables_row(db, name)["important"] is False
    db.drop_table(name, force=True)
    assert name not in db.tablenames


##################################################################
# rename_table                                                   #
##################################################################


def test_rename_table_renames_table_companions_and_meta(db, transient):
    old = fresh_name()
    db.create_table(old, conftest.COLUMNS, label_col="label", sort=["n"])
    transient.append(old)
    db[old].create_index(["label"])
    new = fresh_name()
    db.rename_table(old, new)
    transient.append(new)
    assert new in db.tablenames
    assert old not in db.tablenames
    tablenames = set(db._all_tablenames())
    assert {new, new + "_counts", new + "_stats"} <= tablenames
    assert not ({old, old + "_counts", old + "_stats"} & tablenames)
    assert meta_tables_row(db, new)["label_col"] == "label"
    assert list(db[new].list_indexes()) == [new + "_label"]
    assert db._index_exists(new + "_label", new)


@pytest.mark.xfail(
    strict=True,
    reason="rename_table removes the old name from tablenames but leaves the stale "
           "attribute on the database object (drop_table does delattr)",
)
def test_rename_table_removes_the_old_attribute(db, transient):
    old = fresh_name()
    db.create_table(old, conftest.COLUMNS, label_col="label", sort=["n"])
    transient.append(old)
    new = fresh_name()
    db.rename_table(old, new)
    transient.append(new)
    assert not hasattr(db, old)


##################################################################
# Columns                                                        #
##################################################################


def test_add_column_adds_a_search_column(db, empty_table):
    empty_table.add_column("extra_int", "smallint")
    assert "extra_int" in empty_table.search_cols
    assert empty_table.search_cols == sorted(empty_table.search_cols)
    assert empty_table.col_type["extra_int"] == "smallint"
    assert dict(pg_columns(db, empty_table.search_table))["extra_int"] == "smallint"


def test_add_column_adds_an_extra_column(db, table_factory):
    table = table_factory(extra_columns={"text": ["notes"]})
    table.add_column("more_notes", "text", extra=True)
    assert table.extra_cols == ["more_notes", "notes"]
    assert "more_notes" not in table.search_cols
    assert dict(pg_columns(db, table.extra_table))["more_notes"] == "text"


def test_add_column_can_set_the_label_column(db, empty_table):
    empty_table.add_column("new_label", "text", label=True)
    assert empty_table.get_label() == "new_label"
    assert meta_tables_row(db, empty_table.search_table)["label_col"] == "new_label"


def test_add_column_validates_its_arguments(empty_table):
    with pytest.raises(ValueError):
        empty_table.add_column("label", "text")
    with pytest.raises(RuntimeError):
        empty_table.add_column("bad", "no_such_type")
    with pytest.raises(ValueError):
        empty_table.add_column("both", "text", extra=True, label=True)
    with pytest.raises(ValueError):
        empty_table.add_column("undocumented", "text", force_description=True)
    with pytest.raises(ValueError):
        empty_table.add_column("no_extras_table", "text", extra=True)
    assert empty_table.extra_cols == []


def test_drop_column_removes_the_column(db, empty_table):
    empty_table.add_column("doomed", "text")
    empty_table.drop_column("doomed", force=True)
    assert "doomed" not in empty_table.search_cols
    assert "doomed" not in empty_table.col_type
    assert "doomed" not in dict(pg_columns(db, empty_table.search_table))


def test_drop_column_removes_indexes_referring_to_it(db, empty_table):
    empty_table.create_index(["data"], type="gin")
    index_name = empty_table.search_table + "_data_gin"
    assert index_name in empty_table._list_built_indexes()
    empty_table.drop_column("data", force=True)
    assert empty_table.list_indexes() == {}
    assert index_name not in empty_table._list_built_indexes()


def test_drop_column_validates_its_argument(empty_table):
    with pytest.raises(ValueError):
        empty_table.drop_column("n", force=True)
    assert "n" in empty_table.search_cols
    with pytest.raises(ValueError):
        empty_table.drop_column("no_such_column", force=True)


def test_set_label_and_get_label(db, empty_table):
    assert empty_table.get_label() == "label"
    empty_table.set_label("n")
    assert empty_table.get_label() == "n"
    assert meta_tables_row(db, empty_table.search_table)["label_col"] == "n"
    empty_table.set_label(None)
    assert empty_table.get_label() is None
    assert meta_tables_row(db, empty_table.search_table)["label_col"] is None
    with pytest.raises(ValueError):
        empty_table.set_label("no_such_column")


def test_create_extra_table_splits_off_an_extra_table(db, empty_table):
    empty_table.create_extra_table(["data"])
    assert empty_table.extra_table == empty_table.search_table + "_extras"
    assert empty_table.extra_cols == ["data"]
    assert "data" not in empty_table.search_cols
    assert meta_tables_row(db, empty_table.search_table)["has_extras"] is True


def test_create_extra_table_migrates_constraints(db, empty_table):
    # A constraint on a moved column must be dropped from the search table
    # (whose classification depends on search_cols, so ordering matters) and
    # recreated on the extras table.
    from psycopg2.sql import SQL

    empty_table.create_constraint(["x"], "unique", name="mig_c_x")
    empty_table.create_extra_table(["x"])
    (count,) = db._execute(
        SQL(
            "SELECT COUNT(*) FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE con.conname = %s AND rel.relname = %s"
        ),
        ["mig_c_x", empty_table.extra_table],
    ).fetchone()
    assert count == 1
    assert "mig_c_x" in empty_table.list_constraints()


##################################################################
# Indexes                                                        #
##################################################################


def test_create_index_round_trips_through_meta_indexes(db, empty_table):
    empty_table.create_index(["label"])
    name = empty_table.search_table + "_label"
    assert empty_table.list_indexes() == {
        name: {"type": "btree", "columns": ["label"], "modifiers": [[]]}
    }
    assert name in empty_table._list_built_indexes()
    assert meta_index_row(db, empty_table.search_table, name)["storage_params"] == {
        "fillfactor": 100
    }


def test_create_index_defaults_gin_modifiers_by_column_type(empty_table):
    empty_table.create_index(["data"], type="gin")
    empty_table.create_index(["vec"], type="gin")
    indexes = empty_table.list_indexes()
    assert indexes[empty_table.search_table + "_data_gin"]["modifiers"] == [["jsonb_path_ops"]]
    assert indexes[empty_table.search_table + "_vec_gin"]["modifiers"] == [["array_ops"]]


def test_create_index_accepts_name_modifiers_and_storage_params(db, empty_table):
    name = "idx_%s" % uuid.uuid4().hex[:10]
    empty_table.create_index(
        ["label", "n"],
        modifiers=[["text_pattern_ops"], ["DESC"]],
        name=name,
        storage_params={"fillfactor": 90},
    )
    row = meta_index_row(db, empty_table.search_table, name)
    assert row["columns"] == ["label", "n"]
    assert row["modifiers"] == [["text_pattern_ops"], ["DESC"]]
    assert row["storage_params"] == {"fillfactor": 90}
    assert index_reloptions(db, name) == ["fillfactor=90"]


def test_create_index_validates_its_arguments(empty_table):
    with pytest.raises(ValueError):
        empty_table.create_index(["no_such_column"])
    with pytest.raises(ValueError):
        empty_table.create_index(["label"], type="no_such_type")
    with pytest.raises(ValueError):
        empty_table.create_index(["label", "n"], modifiers=[["DESC"]])
    with pytest.raises(ValueError):
        empty_table.create_index(["label"], modifiers=[["SIDEWAYS"]])
    with pytest.raises(ValueError):
        empty_table.create_index(["label"], storage_params={"no_such_param": 1})
    assert empty_table.list_indexes() == {}


def test_create_index_rejects_a_name_already_in_use(empty_table):
    empty_table.create_index(["label"])
    with pytest.raises(ValueError):
        empty_table.create_index(["n"], name=empty_table.search_table + "_label")


@pytest.mark.xfail(
    strict=True,
    reason="_check_restricted_suffix uses re.match instead of re.search, so the "
           "_tmp/_oldN/_pkey suffix check never fires",
)
def test_create_index_rejects_restricted_suffixes(empty_table):
    with pytest.raises(ValueError):
        empty_table.create_index(["label"], name=empty_table.search_table + "_lbl_tmp")


def test_drop_index_permanently_removes_the_meta_row(empty_table):
    empty_table.create_index(["label"])
    name = empty_table.search_table + "_label"
    empty_table.drop_index(name)
    assert empty_table.list_indexes() == {}
    assert name not in empty_table._list_built_indexes()


def test_drop_index_and_restore_index_round_trip(empty_table):
    empty_table.create_index(["label"])
    name = empty_table.search_table + "_label"
    before = empty_table.list_indexes()
    empty_table.drop_index(name, permanent=False)
    assert empty_table.list_indexes() == before
    assert name not in empty_table._list_built_indexes()
    empty_table.restore_index(name)
    assert name in empty_table._list_built_indexes()


def test_restore_index_needs_a_meta_row(empty_table):
    with pytest.raises(ValueError):
        empty_table.restore_index(empty_table.search_table + "_label")


def test_drop_pkeys_and_restore_pkeys_round_trip(db, empty_table):
    name = empty_table.search_table
    empty_table.drop_pkeys()
    assert pkey_columns(db, name) == []
    empty_table.restore_pkeys()
    assert pkey_columns(db, name) == ["id"]
    assert db._constraint_exists(name + "_pkey", name)


def test_drop_indexes_drops_indexes_and_constraints(empty_table):
    empty_table.create_index(["label"])
    empty_table.create_constraint(["n"], "unique")
    empty_table.drop_indexes()
    assert empty_table.search_table + "_label" not in empty_table._list_built_indexes()
    assert empty_table.search_table + "_c_n" not in empty_table._list_built_constraints()


def test_restore_indexes_rebuilds_indexes_and_constraints(empty_table):
    empty_table.create_index(["label"])
    empty_table.create_constraint(["label"], "unique")
    index_name = empty_table.search_table + "_label"
    constraint_name = empty_table.search_table + "_c_label"
    empty_table.drop_index(index_name, permanent=False)
    empty_table.drop_constraint(constraint_name)
    assert index_name not in empty_table._list_built_indexes()
    assert constraint_name not in empty_table._list_built_constraints()
    empty_table.restore_indexes()
    assert index_name in empty_table._list_built_indexes()
    assert constraint_name in empty_table._list_built_constraints()


##################################################################
# Constraints                                                    #
##################################################################


def test_create_constraint_round_trips_through_meta_constraints(empty_table):
    empty_table.create_constraint(["label"], "unique")
    name = empty_table.search_table + "_c_label"
    assert empty_table.list_constraints() == {
        name: {"type": "UNIQUE", "columns": ["label"], "check_func": None}
    }
    assert name in empty_table._list_built_constraints()


def test_drop_constraint_permanently_removes_the_meta_row(empty_table):
    empty_table.create_constraint(["label"], "unique")
    name = empty_table.search_table + "_c_label"
    empty_table.drop_constraint(name, permanent=True)
    assert empty_table.list_constraints() == {}
    assert name not in empty_table._list_built_constraints()


def test_drop_constraint_keeps_the_meta_row_by_default(empty_table):
    empty_table.create_constraint(["label"], "unique")
    name = empty_table.search_table + "_c_label"
    before = empty_table.list_constraints()
    empty_table.drop_constraint(name)
    assert empty_table.list_constraints() == before
    assert name not in empty_table._list_built_constraints()


def test_create_constraint_validates_its_arguments(empty_table):
    with pytest.raises(ValueError):
        empty_table.create_constraint(["label"], "no_such_type")
    with pytest.raises(ValueError):
        empty_table.create_constraint(["label"], "check")
    with pytest.raises(ValueError):
        empty_table.create_constraint(["label"], "unique", check_func="my_func")
    with pytest.raises(ValueError):
        empty_table.create_constraint(["no_such_column"], "unique")
    with pytest.raises(ValueError):
        empty_table.create_constraint(["id"], "unique")
    assert empty_table.list_constraints() == {}


def test_create_constraint_refuses_to_mix_search_and_extra_columns(table_factory):
    table = table_factory(extra_columns={"text": ["notes"]})
    with pytest.raises(ValueError):
        table.create_constraint(["label", "notes"], "unique")


@pytest.mark.xfail(
    strict=True,
    reason='create_constraint accepts type "NOT NULL" but the statement builders '
           'only recognise the misspelled "NON NULL", so no SQL is generated',
)
def test_create_constraint_not_null(db, empty_table):
    empty_table.create_constraint(["n"], "not null")
    cur = db._execute(
        SQL(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s"
        ),
        [empty_table.search_table, "n"],
    )
    assert cur.fetchone()[0] == "NO"


##################################################################
# meta_* history                                                 #
##################################################################


def test_reload_indexes_records_a_new_history_version(empty_table, tmp_path, hist_cleanup):
    hist_cleanup.append(empty_table.search_table)
    empty_table.create_index(["label"])
    dump = str(tmp_path / "indexes.txt")
    empty_table.copy_to_indexes(dump)
    assert empty_table._get_current_index_version() == -1
    empty_table.reload_indexes(dump)
    assert empty_table._get_current_index_version() == 0
    empty_table.reload_indexes(dump)
    assert empty_table._get_current_index_version() == 1
    assert list(empty_table.list_indexes()) == [empty_table.search_table + "_label"]


@pytest.mark.xfail(
    strict=True,
    reason="_revert_meta passes the table and column arguments to format() in the "
           "wrong order, producing SELECT <hist table> FROM <column list>",
)
def test_revert_indexes_restores_the_previous_version(empty_table, tmp_path, hist_cleanup):
    hist_cleanup.append(empty_table.search_table)
    empty_table.create_index(["label"])
    dump = str(tmp_path / "indexes.txt")
    empty_table.copy_to_indexes(dump)
    before = empty_table.list_indexes()
    empty_table.reload_indexes(dump)
    empty_table.reload_indexes(dump)
    empty_table.revert_indexes()
    assert empty_table.list_indexes() == before


##################################################################
# Database and table accessors                                   #
##################################################################


def test_db_is_alive(db):
    assert db.is_alive() is True


def test_db_getitem_rejects_an_unknown_table(db):
    with pytest.raises(ValueError):
        db["no_such_table"]


def test_table_columns_and_types(db, empty_table):
    assert empty_table.search_cols == sorted(col for col, _ in conftest.COLUMNS)
    assert empty_table.extra_cols == []
    assert empty_table.extra_table is None
    expected = dict(conftest.COLUMNS)
    expected["id"] = "bigint"
    assert empty_table.col_type == expected


def test_description_hooks_are_no_ops_by_default(empty_table):
    assert empty_table.description() is None
    assert empty_table.description("a description") is None
    assert empty_table.column_description() is None
    assert empty_table.column_description("label", "a column") is None
    assert empty_table.column_description(description={"label": "a column"}) is None
