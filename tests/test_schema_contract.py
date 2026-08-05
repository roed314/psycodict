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
