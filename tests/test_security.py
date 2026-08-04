# -*- coding: utf-8 -*-
"""
Input that must never reach a statement.

Most of psycodict's SQL is composed from placeholders and quoted identifiers,
which are safe by construction.  A column type is the exception: PostgreSQL has
no placeholder for a type, so a type has to be interpolated into ``CREATE
TABLE`` and ``ALTER TABLE`` as SQL text.  Everything here checks the validator
that guards that interpolation, and checks it through the public entry points
that take a type from outside psycodict: ``create_table``, ``add_column`` and
the header line of a reloaded data file.

The negative tests do not stop at "an exception was raised".  Each one appends
a statement creating a uniquely named marker table to the injected type and
then asserts that no marker exists, that the intended relation or column was
not partially created, and that the Python objects were not mutated -- an
exception raised after the damage was done would satisfy the first check alone.
"""
import uuid

import pytest

from psycopg.sql import SQL

from psycodict.validation import (
    InvalidColumnTypeError,
    types_whitelist,
    validate_column_type,
)

import conftest


# Types psycodict accepts.  This list is the compatibility contract of the
# grammar: a change that drops one of these breaks existing databases, whose
# column types are read back out of the catalog and revalidated on every
# reload.
VALID_TYPES = [
    "text",
    "bigint",
    "integer",
    "boolean",
    "jsonb",
    "numeric",
    "double precision",
    "text[]",
    "numeric[]",
    "text[][]",
    "integer[][]",
    "varchar(16)[]",
    "character varying(64)[][]",
    "char(8)[]",
    "numeric(10, 2)[]",
    "bit(8)[]",
    "timestamp(6) with time zone[]",
    "interval day to second(6)[]",
    'text[] COLLATE "C"',
    'varchar(16)[] COLLATE "C"',
    'character varying(64)[][] COLLATE "POSIX"',
    "varchar",
    "varchar(16)",
    "character varying",
    "character varying(64)",
    "char",
    "char(8)",
    "numeric(10)",
    "numeric(10, 2)",
    "numeric(10,2)",
    "bit(8)",
    "bit varying(8)",
    "timestamp",
    "timestamp(6) with time zone",
    "timestamp(0) without time zone",
    "time(3)",
    "time(3) with time zone",
    "interval",
    "interval day to second(6)",
    "interval year",
    'text COLLATE "C"',
    'varchar(32) COLLATE "POSIX"',
    # keywords are case-insensitive
    "TEXT",
    "Bigint",
    'text collate "C"',
    # surrounding whitespace is not significant
    "  text  ",
]

# Locale collations exist only on a server whose locales include them, so they
# are checked against the grammar but not created.
VALID_TYPES_ANY_SERVER = VALID_TYPES + [
    'character varying COLLATE "en_US.utf8"',
    'text COLLATE "de_DE.UTF-8"',
]

# Types psycodict must refuse.  The first group is the injection the validator
# exists to stop: a valid type followed by more SQL.  The rest are malformed
# lengths and precisions, and characters that have no business in a type.
INVALID_TYPES = [
    "text; SELECT 1; --",
    "text /* comment */",
    "varchar(16); SELECT 1",
    "numeric(10,2)); SELECT 1; --",
    "text\nDROP TABLE anything",
    "text\x00",
    "text\x00; SELECT 1",
    "varchar(0)",
    "varchar(-1)",
    "text)",
    "text, x integer",
    "bigint DEFAULT nextval('s')",
    'text COLLATE "C"; SELECT 1',
    'text COLLATE "C" || pg_sleep(10)',
    "no_such_type",
    # arrays: the brackets are the only thing allowed between the type and the
    # collation, and the collation comes last
    "varchar(16)[]; SELECT 1",
    "text[][] /* comment */",
    "varchar(16)[][x]",
    "varchar(16)[",
    'varchar(16) COLLATE "C"[]',
    'integer[] COLLATE "C"',
    'text[] COLLATE "C"; SELECT 1',
    # an interval precision is a fractional-seconds precision, so a field
    # restriction that stops short of SECOND cannot carry one
    "interval year(6)",
    "interval month(2)",
    "interval day(3)",
    "interval day to hour(3)",
    "interval day to minute(3)",
    "interval hour to minute(3)",
    "interval second(7)",
    "",
    "   ",
    # a lookalike: the second character is a Cyrillic 'е'
    "tеxt",
]


def marker_name():
    """
    A table name that only an executed injection could bring into existence.
    """
    return "marker_%s" % uuid.uuid4().hex[:12]


def injected(typ, marker):
    """
    ``typ`` extended so that executing it as DDL would create ``marker``.

    The type is interpolated into ``CREATE TABLE t (col <typ>, ...)``, so
    closing the parenthesis ends that statement and what follows is a statement
    of its own.  psycopg sends a parameterless statement with the simple query
    protocol, which runs every statement in the string.
    """
    return "%s); CREATE TABLE %s (x integer); --" % (typ, marker)


def table_exists(db, name):
    return bool(
        db._execute(
            SQL("SELECT 1 FROM information_schema.tables WHERE table_name = %s"),
            [name],
        ).fetchone()
    )


def column_exists(db, table, column):
    return bool(
        db._execute(
            SQL(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s"
            ),
            [table, column],
        ).fetchone()
    )


##################################################################
# the validator itself                                           #
##################################################################


@pytest.mark.parametrize("typ", VALID_TYPES_ANY_SERVER)
def test_valid_types_are_accepted(typ):
    spelling, cost = validate_column_type(typ)
    assert spelling.strip() == spelling
    assert isinstance(cost, int)


@pytest.mark.parametrize("typ", INVALID_TYPES)
def test_invalid_types_are_rejected(typ):
    with pytest.raises(InvalidColumnTypeError):
        validate_column_type(typ)


@pytest.mark.parametrize(
    "typ", ["text", "bigint", "numeric(10, 2)", "varchar(16)[]", 'text COLLATE "C"']
)
def test_appended_sql_is_rejected(typ):
    """
    A valid type followed by a statement is not a valid type.

    This is the regression test for the vulnerability: the validator used to
    match a prefix, so every type in this list passed with the injection still
    attached and was then interpolated into DDL.
    """
    with pytest.raises(InvalidColumnTypeError):
        validate_column_type(injected(typ, marker_name()))


@pytest.mark.parametrize("typ", [None, 17, b"text", ["text"]])
def test_non_strings_are_rejected(typ):
    with pytest.raises(InvalidColumnTypeError):
        validate_column_type(typ)


def test_invalid_column_type_error_is_a_value_error():
    # ValueError is the documented type; RuntimeError is what psycodict raised
    # before 1.0.0, and callers catching it must keep working.
    assert issubclass(InvalidColumnTypeError, ValueError)
    assert issubclass(InvalidColumnTypeError, RuntimeError)


def test_the_validated_spelling_is_what_callers_must_emit():
    # Case-insensitive keywords are canonicalized, but a collation name is a
    # quoted identifier: "C" is a collation and "c" is not, so the case of the
    # spelling the validator returns has to be the case it validated.
    assert validate_column_type("TEXT")[0] == "text"
    assert validate_column_type("  Bigint  ")[0] == "bigint"
    assert validate_column_type('text COLLATE "C"')[0] == 'text COLLATE "C"'
    assert validate_column_type("numeric(10, 2)")[0] == "numeric(10, 2)"


def test_an_array_is_variable_width_whatever_it_holds():
    assert validate_column_type("varchar(16)[]")[1] == -1
    assert validate_column_type("text[][]")[1] == -1
    assert validate_column_type("integer[]")[1] == -1
    # the scalar keeps its own cost
    assert validate_column_type("integer")[1] == 4


def test_the_spelling_is_rebuilt_in_postgresqls_order():
    # scalar, then array brackets, then the collation
    assert validate_column_type("varchar(16)[]")[0] == "varchar(16)[]"
    assert (
        validate_column_type('varchar(16)[] COLLATE "C"')[0]
        == 'varchar(16)[] COLLATE "C"'
    )
    # a fixed scalar is still canonicalized, and the brackets follow it
    assert validate_column_type("TEXT[][]")[0] == "text[][]"
    assert validate_column_type('  TEXT[]  COLLATE "C" ')[0] == 'text[] COLLATE "C"'


def test_a_collation_belongs_only_to_a_collatable_type():
    for typ in ["integer", "integer[]", "numeric(10, 2)", "jsonb"]:
        with pytest.raises(InvalidColumnTypeError, match="collatable"):
            validate_column_type('%s COLLATE "C"' % typ)
    for typ in ["text", "varchar(16)", "character varying", "char(8)[]"]:
        assert validate_column_type('%s COLLATE "C"' % typ)


def test_every_fixed_type_validates_to_itself():
    for typ in types_whitelist:
        assert validate_column_type(typ) == (typ, types_whitelist[typ])


##################################################################
# create_table                                                   #
##################################################################


def test_create_table_rejects_an_injected_type(db):
    marker = marker_name()
    name = "test_%s" % uuid.uuid4().hex[:12]
    with pytest.raises(InvalidColumnTypeError):
        db.create_table(name, [("n", "integer"), ("c", injected("text", marker))], "n")
    assert not table_exists(db, marker)
    assert not table_exists(db, name)
    assert name not in db.tablenames


def test_create_table_rejects_an_injected_id_type(db):
    marker = marker_name()
    name = "test_%s" % uuid.uuid4().hex[:12]
    with pytest.raises(InvalidColumnTypeError):
        db.create_table(name, [("n", "integer")], "n", id_type=injected("bigint", marker))
    assert not table_exists(db, marker)
    assert not table_exists(db, name)


@pytest.mark.parametrize("typ", VALID_TYPES)
def test_create_table_accepts_every_valid_type(db, typ):
    """
    Every type the validator accepts is a type PostgreSQL accepts.

    A grammar that drifts from the server's is as much a bug as a permissive
    one: it would reject a column that an existing database already has.
    """
    name = "test_%s" % uuid.uuid4().hex[:12]
    db.create_table(name, [("n", "integer"), ("c", typ)], "n")
    try:
        assert column_exists(db, name, "c")
    finally:
        db.drop_table(name, force=True)


##################################################################
# add_column                                                     #
##################################################################


def test_add_column_rejects_an_injected_type(db, empty_table):
    marker = marker_name()
    before_cols = list(empty_table.search_cols)
    before_types = dict(empty_table.col_type)
    with pytest.raises(InvalidColumnTypeError):
        empty_table.add_column("bad", injected("text", marker))
    assert not table_exists(db, marker)
    assert not column_exists(db, empty_table.search_table, "bad")
    # The in-memory schema must not have been updated by the failed call.
    assert empty_table.search_cols == before_cols
    assert empty_table.col_type == before_types
    assert "bad" not in empty_table.col_type


def test_add_column_still_adds_a_valid_column(db, empty_table):
    empty_table.add_column("extra", 'text COLLATE "C"')
    assert column_exists(db, empty_table.search_table, "extra")
    assert "extra" in empty_table.search_cols
    empty_table.insert_many([{"n": 1, "extra": "hello"}])
    assert empty_table.lucky({"n": 1}, "extra") == "hello"


##################################################################
# types read from the header of a data file                      #
##################################################################


def _write_search_file(path, cols, types, rows=(), sep="|"):
    with open(path, "w") as F:
        F.write(sep.join(cols) + "\n")
        F.write(sep.join(types) + "\n")
        F.write("\n")
        for row in rows:
            F.write(sep.join(str(x) for x in row) + "\n")


def test_reload_with_adjust_schema_rejects_an_injected_header_type(db, filled_table, tmp_path):
    marker = marker_name()
    name = filled_table.search_table
    searchfile = str(tmp_path / "search.txt")
    _write_search_file(
        searchfile,
        ["id", "n", "label"],
        ["bigint", "integer", injected("text", marker)],
        [(1, 1, "one")],
    )
    with pytest.raises(InvalidColumnTypeError):
        filled_table.reload(searchfile, adjust_schema=True)
    assert not table_exists(db, marker)
    assert not table_exists(db, name + "_tmp")
    # The live table still holds its original rows.
    assert db[name].count() == 200


def test_reload_with_adjust_schema_accepts_a_valid_header_type(db, filled_table, tmp_path):
    name = filled_table.search_table
    searchfile = str(tmp_path / "search.txt")
    _write_search_file(
        searchfile,
        ["id", "n", "label"],
        ["bigint", "integer", "text"],
        [(1, 1, "one"), (2, 2, "two")],
    )
    filled_table.reload(searchfile, adjust_schema=True)
    table = db[name]
    assert table.count() == 2
    assert table.lucky({"n": 2}, "label") == "two"


def test_reload_all_with_adjust_schema_rejects_an_injected_header_type(db, filled_table, tmp_path):
    """
    The same check on the path that creates a table nobody has seen before.

    ``reload_all(adjust_schema=True)`` builds a table out of a data folder: the
    column types come from the header of a file on disk, which is exactly the
    input this validation exists for.
    """
    marker = marker_name()
    folder = tmp_path / "data"
    db.copy_to([filled_table.search_table], str(folder))

    new_name = "test_%s" % uuid.uuid4().hex[:12]
    old_name = filled_table.search_table
    for path in sorted(folder.glob(old_name + "*")):
        path.rename(folder / path.name.replace(old_name, new_name, 1))
    # The meta row names the table it describes, and reload_all checks it.
    metafile = folder / (new_name + "_meta.txt")
    metafile.write_text(metafile.read_text().replace(old_name, new_name, 1))
    searchfile = folder / (new_name + ".txt")
    lines = searchfile.read_text().split("\n")
    lines[1] = lines[1].replace("text", injected("text", marker), 1)
    searchfile.write_text("\n".join(lines))

    with pytest.raises(InvalidColumnTypeError):
        db.reload_all(str(folder), adjust_schema=True)
    assert not table_exists(db, marker)
    assert not table_exists(db, new_name)
    assert new_name not in db.tablenames
    # and the table the folder was exported from is untouched
    assert db[old_name].count() == 200
    assert conftest  # the fixtures above come from conftest
