# -*- coding: utf-8 -*-
"""
A database operates in exactly one schema.

Every relation psycodict creates goes there, every relation it looks for is
looked for there, and every catalog query is filtered to it -- so a relation of
the same name in another schema can neither stand in for one of these nor be
merged with it.  Before this, catalog queries were a mixture of hard-coded
``'public'`` and no filter at all, so two schemas holding ``same_name`` gave
answers assembled from both.
"""
import pytest

from psycopg.sql import SQL, Identifier

from psycodict.database import PostgresDatabase
from psycodict.validation import InvalidDefinitionError


OTHER = "psycodict_other"

# A schema whose name is a valid identifier but *not* a valid search_path, and
# the two schemas its text parses into if it is written to the setting as text.
SPLIT = "psycodict_path_a, psycodict_path_b"
DECOYS = ["psycodict_path_a", "psycodict_path_b"]


def relations_in(db, schema):
    """
    The names of the relations in ``schema``, asked of the catalog rather than
    of the search path.
    """
    return {
        rec[0]
        for rec in db._execute(
            SQL(
                "SELECT c.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s"
            ),
            [schema],
        )
    }


@pytest.fixture
def two_schemas(db):
    """
    ``<schema>.same_name`` in the configured schema and in a second one, with
    different columns, plus the metadata tables the second schema needs to be
    connectable in its own right.
    """
    db._execute(SQL("CREATE SCHEMA IF NOT EXISTS {0}").format(Identifier(OTHER)))
    db._execute(
        SQL("CREATE TABLE IF NOT EXISTS {0}.{1} (id bigint, only_here text)").format(
            Identifier(OTHER), Identifier("same_name")
        )
    )
    db._execute(
        SQL("CREATE TABLE IF NOT EXISTS {0} (id bigint, label text, n integer)").format(
            Identifier("same_name")
        )
    )
    yield db
    db._execute(SQL("DROP TABLE IF EXISTS {0}").format(Identifier("same_name")))
    db._execute(SQL("DROP SCHEMA IF EXISTS {0} CASCADE").format(Identifier(OTHER)))


def test_the_default_schema_is_public(db):
    assert db.schema == "public"


def test_an_invalid_schema_name_is_refused(config):
    for bad in ["", "a" * 64, "with\x00nul"]:
        with pytest.raises((InvalidDefinitionError, ValueError)):
            PostgresDatabase(config=config, schema=bad)


def test_schema_is_not_passed_to_the_driver(db):
    """
    psycopg.connect has no ``schema`` parameter; it is psycodict's own setting
    and must not end up among the connection overrides.
    """
    assert "schema" not in db._connect_kwargs
    assert "schema" not in db._connection_options()


def test_the_session_search_path_is_the_selected_schema(db):
    cur = db._execute(SQL("SELECT current_schema()"))
    assert cur.fetchone()[0] == db.schema


def test_a_reconnect_keeps_the_schema(db):
    db.reset_connection()
    cur = db._execute(SQL("SELECT current_schema()"))
    assert cur.fetchone()[0] == db.schema


# ---------------------------------------------------------------------------
# the schema is one search_path entry, and pg_temp comes after it
# ---------------------------------------------------------------------------

@pytest.fixture
def split_schemas(db):
    """
    A schema whose name contains a comma, alongside the two schemas that name's
    text would parse into.  All three exist, so that a search_path written as
    text selects a real schema rather than failing.
    """
    for name in [SPLIT] + DECOYS:
        db._execute(SQL("CREATE SCHEMA IF NOT EXISTS {0}").format(Identifier(name)))
    yield db
    for name in [SPLIT] + DECOYS:
        db._execute(SQL("DROP SCHEMA IF EXISTS {0} CASCADE").format(Identifier(name)))


def test_a_comma_in_the_schema_name_is_not_a_path_separator(split_schemas, config):
    """
    search_path is a comma-separated *list*, so a schema name written into it
    as text is parsed rather than used.  Passed as text, this name selects
    ``psycodict_path_a`` -- and ``create=True`` then bootstraps the metadata
    tables there, while every catalog query keeps binding the literal name and
    reports a schema the session is not actually using.
    """
    other = PostgresDatabase(config=config, schema=SPLIT, create=True)
    try:
        assert other.schema == SPLIT
        cur = other._execute(SQL("SELECT current_schema()"))
        assert cur.fetchone()[0] == SPLIT

        # the metadata went to the schema that was asked for ...
        created = relations_in(other, SPLIT)
        assert {"meta_tables", "meta_indexes", "meta_constraints"} <= created
        # ... and to neither of the schemas its text parses into
        for decoy in DECOYS:
            assert relations_in(other, decoy) == set()

        # a replacement connection is pinned the same way
        other.reset_connection()
        cur = other._execute(SQL("SELECT current_schema()"))
        assert cur.fetchone()[0] == SPLIT
        for decoy in DECOYS:
            assert relations_in(other, decoy) == set()
    finally:
        other.conn.close()


def test_a_schema_needing_quotes_is_one_path_entry(db, config):
    """
    Mixed case and spaces: legal in a quoted identifier, and (unlike the comma)
    rejected outright by the server when written to the setting as text.
    """
    name = "Psycodict Mixed Case"
    db._execute(SQL("CREATE SCHEMA IF NOT EXISTS {0}").format(Identifier(name)))
    try:
        other = PostgresDatabase(config=config, schema=name, create=True)
        try:
            cur = other._execute(SQL("SELECT current_schema()"))
            assert cur.fetchone()[0] == name
            assert "meta_tables" in relations_in(other, name)
        finally:
            other.conn.close()
    finally:
        db._execute(SQL("DROP SCHEMA IF EXISTS {0} CASCADE").format(Identifier(name)))


def test_the_dollar_user_token_is_refused(config):
    """
    PostgreSQL unquotes each search_path element before looking for ``$user``,
    so ``"$user"`` is replaced by the connecting role's name and can never
    select a schema of that name.  Any other spelling is an ordinary identifier.
    """
    with pytest.raises(InvalidDefinitionError, match=r"\$user"):
        PostgresDatabase(config=config, schema="$user")


def test_a_missing_schema_is_refused_rather_than_fallen_back_from(config):
    """
    PostgreSQL drops a search_path entry naming a schema that does not exist
    (or one the role has no USAGE on) and resolves names further down the path.
    With pg_temp there, an unqualified CREATE TABLE would make a temporary one.
    """
    with pytest.raises(ValueError, match="Could not operate in schema"):
        PostgresDatabase(config=config, schema="psycodict_no_such_schema")


def test_a_temporary_relation_does_not_shadow_the_selected_schema(db):
    """
    pg_temp is searched first unless it is named, so a temporary relation would
    otherwise stand in for meta_tables or for a search table.
    """
    persistent = db._execute(
        SQL(
            "SELECT c.oid FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = 'meta_tables'"
        ),
        [db.schema],
    ).fetchone()[0]
    db._execute(SQL("CREATE TEMP TABLE meta_tables (not_the_real_one text)"))
    try:
        resolved = db._execute(SQL("SELECT %s::regclass::oid"), ["meta_tables"])
        assert resolved.fetchone()[0] == persistent
        # and the same thing from the other side: an ordinary metadata read
        # reaches the persistent table.  The temporary one has no ``name``
        # column, so this would raise UndefinedColumn if it were resolved.
        cur = db._execute(SQL("SELECT name FROM meta_tables"))
        assert [col.name for col in cur.description] == ["name"]
    finally:
        db._execute(SQL("DROP TABLE IF EXISTS pg_temp.meta_tables"))


# ---------------------------------------------------------------------------
# catalog queries see one schema
# ---------------------------------------------------------------------------

def test_table_exists_does_not_see_the_other_schema(two_schemas):
    db = two_schemas
    assert db._table_exists("same_name")
    db._execute(SQL("DROP TABLE {0}").format(Identifier("same_name")))
    # still present in the other schema, but not in ours
    assert not db._table_exists("same_name")
    db._execute(
        SQL("CREATE TABLE {0} (id bigint, label text, n integer)").format(
            Identifier("same_name")
        )
    )


def test_all_tablenames_does_not_merge_schemas(two_schemas):
    db = two_schemas
    names = db._all_tablenames()
    assert names.count("same_name") == 1


def test_schema_relations_is_confined(two_schemas):
    db = two_schemas
    relations = db._schema_relations()
    assert "same_name" in relations
    db._execute(SQL("DROP TABLE {0}").format(Identifier("same_name")))
    assert "same_name" not in db._schema_relations()
    db._execute(
        SQL("CREATE TABLE {0} (id bigint, label text, n integer)").format(
            Identifier("same_name")
        )
    )


def test_column_discovery_does_not_mix_the_two(two_schemas):
    """
    The columns of ``same_name`` differ between the schemas.  Reading them
    unfiltered used to raise "Type mismatch" or silently union them.
    """
    db = two_schemas
    cols, col_type, has_id = db._column_types("same_name")
    assert sorted(cols) == ["label", "n"]
    assert "only_here" not in col_type


def test_relation_columns_is_confined(two_schemas):
    db = two_schemas
    assert db._relation_columns("same_name") == {"id", "label", "n"}


def test_index_lookups_are_confined(two_schemas):
    db = two_schemas
    db._execute(
        SQL("CREATE INDEX {0} ON {1}.{2} (id)").format(
            Identifier("same_name_idx"), Identifier(OTHER), Identifier("same_name")
        )
    )
    # the index exists, but in the other schema
    assert not db._index_exists("same_name_idx", "same_name")
    assert db._index_exists("same_name_idx", "same_name") is False
    assert "same_name_idx" not in db._list_indexes("same_name")


def test_constraint_lookups_are_confined(two_schemas):
    db = two_schemas
    db._execute(
        SQL("ALTER TABLE {0}.{1} ADD CONSTRAINT {2} CHECK (id > 0)").format(
            Identifier(OTHER), Identifier("same_name"), Identifier("same_name_chk")
        )
    )
    assert not db._constraint_exists("same_name_chk", "same_name")
    assert "same_name_chk" not in db._list_constraints("same_name")


def test_table_sizes_report_one_schema(two_schemas):
    db = two_schemas
    sizes = db.table_sizes()
    assert sizes.get("same_name") is not None


# ---------------------------------------------------------------------------
# a database pointed at the other schema sees only that one
# ---------------------------------------------------------------------------

def test_another_schema_does_not_inherit_the_metadata_tables(two_schemas, config):
    """
    The clearest demonstration of the confinement: meta_tables exists, but not
    in the other schema, so connecting there without create=True is refused
    rather than quietly operating on the configured schema's metadata.
    """
    with pytest.raises(ValueError, match="metadata tables"):
        PostgresDatabase(config=config, schema=OTHER)


def test_a_database_in_another_schema_sees_only_its_own(two_schemas, config):
    db = two_schemas
    other = PostgresDatabase(config=config, schema=OTHER, create=True)
    try:
        assert other.schema == OTHER
        cur = other._execute(SQL("SELECT current_schema()"))
        assert cur.fetchone()[0] == OTHER

        # same relation name, and each database sees its own columns
        assert other._relation_columns("same_name") == {"id", "only_here"}
        assert db._relation_columns("same_name") == {"id", "label", "n"}

        # each schema has its own metadata, and the configured schema's
        # search tables are not visible from the other one
        assert "same_name" in other._all_tablenames()
        assert set(db.tablenames) - set(other.tablenames) == set(db.tablenames)
    finally:
        other.conn.close()
