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
import os.path
import uuid

import pytest

from psycopg.sql import SQL, Identifier

from psycodict.validation import (
    InvalidColumnTypeError,
    InvalidDefinitionError,
    MAX_BACKUP_NUMBER_DIGITS,
    MAX_IDENTIFIER_LENGTH,
    MAX_SEARCH_TABLE_NAME_LENGTH,
    catalog_identifier,
    check_new_table_name,
    derived_identifier,
    identifier_bytes,
    normalize_storage_param,
    physical_table_name,
    search_table_family,
    types_whitelist,
    validate_column_type,
    validate_index_predicate,
    validate_search_table_name,
)
from psycodict.encoding import Json
from psycodict.table import PostgresTable
from psycodict.utils import safe_child_path

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


##################################################################
# index and constraint definitions out of meta_*                 #
##################################################################


def meta_index_rows(db, table):
    """
    This table's rows of meta_indexes, as dictionaries.
    """
    cur = db._execute(
        SQL(
            "SELECT index_name, type, columns, modifiers, storage_params, whereclause "
            "FROM meta_indexes WHERE table_name = %s ORDER BY index_name"
        ),
        [table],
    )
    keys = ["index_name", "type", "columns", "modifiers", "storage_params", "whereclause"]
    return [dict(zip(keys, row)) for row in cur]


def poison_meta_index(db, table, target, **columns):
    """
    Edit a meta_indexes row in place, the way a direct SQL edit would.
    """
    assignments = SQL(", ").join(
        SQL("{0} = %s").format(Identifier(col)) for col in columns
    )
    db._execute(
        SQL("UPDATE meta_indexes SET {0} WHERE table_name = %s AND index_name = %s").format(
            assignments
        ),
        [Json(val) if col in ("columns", "modifiers", "storage_params") else val
         for col, val in columns.items()] + [table, target],
    )


def indexes_file_with(tmp_path, table, **replacements):
    """
    Export ``table``'s indexes and rewrite fields of the exported row.

    Returns the path of the modified file.
    """
    path = tmp_path / "indexes.txt"
    table.copy_to_indexes(str(path))
    text = path.read_text()
    for old, new in replacements.items():
        text = text.replace(old, new)
    path.write_text(text)
    return str(path)


def test_reload_indexes_rejects_a_predicate_carrying_a_statement(db, empty_table, tmp_path):
    """
    A partial index predicate is raw SQL by design, and it is appended to
    CREATE INDEX; a metadata file must not be able to use it to append a
    statement of its own.
    """
    marker = marker_name()
    empty_table.create_index(["n"], where="n > 0")
    name = empty_table.search_table + "_n"
    before = meta_index_rows(db, empty_table.search_table)
    built_before = set(empty_table._list_built_indexes())

    path = indexes_file_with(
        tmp_path, empty_table,
        **{"n > 0": "n > 0; CREATE TABLE %s (x integer); --" % marker}
    )
    with pytest.raises(ValueError):
        empty_table.reload_indexes(path)

    assert not table_exists(db, marker)
    # The rejected import rolled back: the old definitions are still there ...
    assert meta_index_rows(db, empty_table.search_table) == before
    # ... and the built index was neither dropped nor replaced.
    assert set(empty_table._list_built_indexes()) == built_before
    assert "WHERE (n > 0)" in db._execute(
        SQL("SELECT indexdef FROM pg_indexes WHERE indexname = %s"), [name]
    ).fetchone()[0]


@pytest.mark.parametrize(
    "replacement",
    [
        # unknown access method
        {"btree": "btree; CREATE TABLE x (i int); --"},
        {"btree": "nosuchmethod"},
        # a modifier carrying SQL
        {'[[]]': '[["DESC) ; CREATE TABLE x (i int); --"]]'},
        # a modifier that is valid for another access method
        {'[[]]': '[["jsonb_path_ops"]]'},
        # more modifier lists than columns
        {'[[]]': '[[], []]'},
        # unknown storage parameter
        {'{"fillfactor": 100}': '{"nosuchparam": 100}'},
        # a storage parameter value out of range, and one of the wrong type
        {'{"fillfactor": 100}': '{"fillfactor": 1000}'},
        {'{"fillfactor": 100}': '{"fillfactor": "100); CREATE TABLE x (i int); --"}'},
        # malformed columns
        {'["n"]': '"n"'},
        {'["n"]': '[]'},
    ],
)
def test_reload_indexes_rejects_invalid_definitions(db, empty_table, tmp_path, replacement):
    empty_table.create_index(["n"])
    before = meta_index_rows(db, empty_table.search_table)
    path = indexes_file_with(tmp_path, empty_table, **replacement)
    with pytest.raises(ValueError):
        empty_table.reload_indexes(path)
    assert meta_index_rows(db, empty_table.search_table) == before


def test_a_column_name_carrying_sql_cannot_build_a_statement(db, empty_table, tmp_path):
    """
    Column names are quoted wherever they are used, and any string can be a
    real column name (the LMFDB has 2adic_index), so a name carrying SQL is not
    refused for its shape -- it is refused when the definition is turned into
    DDL, because it is not a column of the table.
    """
    marker = marker_name()
    empty_table.create_index(["n"])
    name = empty_table.search_table + "_n"
    poison = 'n"); CREATE TABLE %s (x integer); --' % marker
    poison_meta_index(db, empty_table.search_table, name, columns=[poison])
    empty_table.drop_index(name, permanent=False)
    with pytest.raises(InvalidDefinitionError):
        empty_table.restore_index(name)
    assert not table_exists(db, marker)
    assert name not in empty_table._list_built_indexes()


def test_reload_constraints_rejects_invalid_definitions(db, empty_table, tmp_path):
    empty_table.create_constraint(["label"], "unique")
    path = tmp_path / "constraints.txt"
    empty_table.copy_to_constraints(str(path))
    original = path.read_text()

    for old, new in [
        ("UNIQUE", "UNIQUE; CREATE TABLE x (i int); --"),
        ("UNIQUE", "FOREIGN KEY"),
        # a check function on a constraint that is not a CHECK
        ('["label"]|', '["label"]|nosuchfunc'),
    ]:
        path.write_text(original.replace(old, new, 1))
        with pytest.raises(ValueError):
            empty_table.reload_constraints(str(path))
        # the constraint is still recorded and still enforced
        assert list(empty_table.list_constraints()) == [empty_table.search_table + "_c_label"]


def test_restore_index_rejects_metadata_poisoned_in_place(db, empty_table):
    """
    A row can be edited with plain SQL after create_index validated it, so the
    check has to happen again when the row is turned back into DDL.
    """
    marker = marker_name()
    empty_table.create_index(["n"])
    name = empty_table.search_table + "_n"
    empty_table.drop_index(name, permanent=False)

    poison_meta_index(
        db, empty_table.search_table, name,
        whereclause="n > 0; CREATE TABLE %s (x integer); --" % marker,
    )
    with pytest.raises(ValueError):
        empty_table.restore_index(name)
    assert not table_exists(db, marker)
    assert name not in empty_table._list_built_indexes()


@pytest.mark.parametrize(
    "poison",
    [
        {"type": "btree; CREATE TABLE x (i int); --"},
        {"modifiers": [["DESC); CREATE TABLE x (i int); --"]]},
        {"storage_params": {"fillfactor); CREATE TABLE x (i int); --": 100}},
        {"columns": ["nosuchcolumn"]},
        {"columns": "n"},
    ],
)
def test_restore_index_rejects_poisoned_fields(db, empty_table, poison):
    empty_table.create_index(["n"])
    name = empty_table.search_table + "_n"
    empty_table.drop_index(name, permanent=False)
    poison_meta_index(db, empty_table.search_table, name, **poison)
    lookup = poison.get("index_name", name)
    with pytest.raises(ValueError):
        empty_table.restore_index(lookup)
    assert lookup not in empty_table._list_built_indexes()


def test_restore_constraint_rejects_an_unapproved_check_function(db, empty_table):
    empty_table.create_constraint(["label"], "unique")
    name = empty_table.search_table + "_c_label"
    empty_table.drop_constraint(name)
    db._execute(
        SQL(
            "UPDATE meta_constraints SET type = %s, check_func = %s "
            "WHERE table_name = %s AND constraint_name = %s"
        ),
        ["CHECK", "nosuchfunc", empty_table.search_table, name],
    )
    with pytest.raises(ValueError):
        empty_table.restore_constraint(name)
    assert name not in empty_table._list_built_constraints()


def test_revert_indexes_rejects_poisoned_history(db, empty_table, tmp_path):
    """
    The history table is as editable as any other, so a revert validates too.
    """
    marker = marker_name()
    empty_table.create_index(["n"], where="n > 0")
    path = tmp_path / "indexes.txt"
    empty_table.copy_to_indexes(str(path))
    # two versions, so that there is something to revert to
    empty_table.reload_indexes(str(path))
    empty_table.reload_indexes(str(path))

    db._execute(
        SQL(
            "UPDATE meta_indexes_hist SET whereclause = %s "
            "WHERE table_name = %s AND version = 0"
        ),
        ["n > 0; CREATE TABLE %s (x integer); --" % marker, empty_table.search_table],
    )
    before = meta_index_rows(db, empty_table.search_table)
    with pytest.raises(ValueError):
        empty_table.revert_indexes(version=0)
    assert not table_exists(db, marker)
    assert meta_index_rows(db, empty_table.search_table) == before


##################################################################
# the definitions psycodict does accept still work               #
##################################################################


def test_create_index_still_accepts_every_supported_shape(db, empty_table):
    empty_table.create_index(["n"])
    empty_table.create_index(["label"], modifiers=[["text_pattern_ops"]])
    empty_table.create_index(["n", "label"], modifiers=[["DESC"], ["nulls first"]])
    empty_table.create_index(["data"], type="gin")
    empty_table.create_index(["x"], storage_params={"fillfactor": 90})
    empty_table.create_index(["num"], where="num > 0")
    recorded = empty_table.list_indexes()
    built = set(empty_table._list_built_indexes())
    assert len(recorded) == 6
    assert set(recorded) <= built


def test_a_partial_index_survives_export_and_restore(db, empty_table, tmp_path):
    empty_table.create_index(["n"], where="n > 0 AND n < 100")
    name = empty_table.search_table + "_n"
    path = str(tmp_path / "indexes.txt")
    empty_table.copy_to_indexes(path)
    empty_table.drop_index(name)
    empty_table.reload_indexes(path)
    empty_table.restore_index(name)
    indexdef = db._execute(
        SQL("SELECT indexdef FROM pg_indexes WHERE indexname = %s"), [name]
    ).fetchone()[0]
    assert "WHERE" in indexdef and "n > 0" in indexdef


def test_predicates_that_are_merely_unusual_are_still_accepted():
    for predicate in [
        "n > 0",
        "n IS NOT NULL",
        "label LIKE 'a%'",
        "(n > 0 AND label IS NOT NULL) OR num = 3",
        "n > 0\n  AND num < 10",
    ]:
        assert validate_index_predicate(predicate) == predicate.strip()


@pytest.mark.parametrize(
    "predicate",
    [
        "n > 0; DROP TABLE anything",
        "n > 0 -- comment",
        "n > 0 /* comment */",
        "n > 0 $$ x $$",
        "n > 0 $tag$ x $tag$",
        "n > 0\x00",
        "",
        "   ",
        "x" * 5000,
    ],
)
def test_predicates_that_could_end_the_statement_are_rejected(predicate):
    with pytest.raises(InvalidDefinitionError):
        validate_index_predicate(predicate)


def test_a_long_index_name_still_survives_a_reload(db, empty_table):
    """
    psycodict makes the names it puts in DDL by appending _tmp or _oldN to one
    it already has, and create_index generates names right up to PostgreSQL's
    63-byte limit.  Holding the suffixed name to that limit would fail every
    reload of a table with a long index name.
    """
    name = ("i_%s" % uuid.uuid4().hex) * 2
    name = name[:63]
    empty_table.create_index(["n"], name=name)
    empty_table.drop_index(name, permanent=False)
    empty_table.restore_index(name, suffix="")
    assert name in empty_table._list_built_indexes()


def test_an_index_on_a_column_named_like_a_number_is_restorable(db, table_factory):
    """
    A column that exists is a column whatever it is called: the LMFDB has one
    named 2adic_index, which is not an identifier psycodict could have created
    unquoted.
    """
    table = table_factory(
        columns=[("2adic_index", "integer"), ("label", "text")], sort=["label"]
    )
    table.create_index(["2adic_index"], name="idx_%s" % uuid.uuid4().hex[:8])
    name = list(table.list_indexes())[0]
    table.drop_index(name, permanent=False)
    table.restore_index(name)
    assert name in table._list_built_indexes()


@pytest.mark.parametrize(
    "typ",
    [
        'text COLLATE "C.UTF-8"',
        'text COLLATE "und-x-icu"',
        'varchar(5) COLLATE "de_DE@euro"',
        'text[] COLLATE "C"',
    ],
)
def test_collations_psycodict_does_not_get_to_veto(typ):
    """
    Which collations exist is the server's business; the grammar's business is
    that the name cannot end the quoted string early.
    """
    spelling, _ = validate_column_type(typ)
    assert spelling == typ


##################################################################
# identifiers psycodict quotes rather than restricts             #
##################################################################


def catalog_index_names(db, table):
    """
    The index names PostgreSQL itself reports for a table.
    """
    cur = db._execute(
        SQL("SELECT indexname FROM pg_indexes WHERE tablename = %s"), [table]
    )
    return {rec[0] for rec in cur}


def catalog_constraint_names(db, table):
    cur = db._execute(
        SQL(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass"
        ),
        [table],
    )
    return {rec[0] for rec in cur}


AWKWARD_NAMES = [
    "idx-with-hyphen",
    "idx with spaces",
    "idx_é_accented",
    'idx"with"quotes',
    "idx; not a statement --",
]


@pytest.mark.parametrize("name", AWKWARD_NAMES)
def test_an_index_name_needing_quotes_round_trips(db, empty_table, name):
    """
    A quoted identifier can hold anything but a NUL, and psycodict quotes every
    name it emits, so a name is refused for what would make it unusable and not
    for its shape.  The name containing a semicolon is the interesting one: it
    is one identifier, not a statement.
    """
    marker_free = set(db._all_tablenames())
    empty_table.create_index(["n"], name=name)
    assert name in catalog_index_names(db, empty_table.search_table)
    assert name in empty_table.list_indexes()
    # nothing was executed on the side by the name that looks like SQL
    assert set(db._all_tablenames()) == marker_free

    empty_table.drop_index(name, permanent=False)
    assert name not in catalog_index_names(db, empty_table.search_table)
    empty_table.restore_index(name)
    assert name in catalog_index_names(db, empty_table.search_table)


@pytest.mark.parametrize("name", ["c-with-hyphen", "c with spaces", "c_é"])
def test_a_constraint_name_needing_quotes_round_trips(db, empty_table, name):
    empty_table.create_constraint(["label"], "unique", name=name)
    assert name in catalog_constraint_names(db, empty_table.search_table)
    empty_table.drop_constraint(name)
    assert name not in catalog_constraint_names(db, empty_table.search_table)
    empty_table.restore_constraint(name)
    assert name in catalog_constraint_names(db, empty_table.search_table)


def test_an_index_on_a_column_needing_quotes_round_trips(db, table_factory):
    table = table_factory(columns=[("x-y", "integer"), ("label", "text")], sort=["label"])
    name = "idx_%s" % uuid.uuid4().hex[:8]
    table.create_index(["x-y"], name=name)
    assert name in catalog_index_names(db, table.search_table)
    table.drop_index(name, permanent=False)
    table.restore_index(name)
    assert name in catalog_index_names(db, table.search_table)


@pytest.mark.parametrize("name", ["", "idx\x00name", "idx\nname", "x" * 64])
def test_names_psycodict_still_refuses(db, empty_table, name):
    """
    Empty, NUL and control characters (a psycodict policy, so that names stay
    printable in its logs and export files), and a name PostgreSQL would
    truncate.
    """
    with pytest.raises(ValueError):
        empty_table.create_index(["n"], name=name)


##################################################################
# names psycodict derives from other names                       #
##################################################################


def test_a_generated_name_at_the_limit_disambiguates_within_it(db, table_factory):
    """
    The automatic name is cut to the identifier limit, and a collision
    disambiguator has its bytes reserved before the cut rather than appended to
    a name that is already full -- which would have been truncated by
    PostgreSQL into a name meta_indexes does not have.

    The two indexes differ only in their partial predicates, so both generate
    the *same* candidate name and the second one really does go through the
    disambiguation loop.  (An index of a different access method would not: the
    method is part of the generated candidate.)
    """
    long_column = "c" + "o" * 50
    table = table_factory(
        columns=[(long_column, "integer"), ("label", "text")], sort=["label"]
    )
    candidate = derived_identifier("_".join([table.search_table, long_column]))
    assert len(candidate.encode("utf-8")) == MAX_IDENTIFIER_LENGTH

    table.create_index([long_column], where='"%s" > 0' % long_column)
    table.create_index([long_column], where='"%s" <= 0' % long_column)

    names = sorted(table.list_indexes())
    assert len(names) == 2
    first, second = names[0], names[1]
    # the first index took the candidate; the second could not, and was
    # disambiguated within the limit rather than past it
    assert candidate in names
    assert first != second
    assert second == derived_identifier(candidate, "0") or first == derived_identifier(
        candidate, "0"
    )
    built = catalog_index_names(db, table.search_table)
    for name in names:
        assert len(name.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
        # what psycodict recorded is what PostgreSQL created, with no
        # truncated alias standing in for it
        assert name in built
    # and the two indexes really are different indexes
    predicates = {
        idx["where"] for idx in table.list_indexes().values() if idx.get("where")
    }
    assert len(predicates) == 2


def test_a_maximum_length_index_survives_a_suffixed_restore(db, empty_table):
    """
    A 63-byte index name plus ``_tmp`` does not fit, so the derived name has to
    be shortened deterministically -- by the same helper everywhere, or the
    path that creates it and the path that looks for it would disagree.
    """
    name = ("i%s" % uuid.uuid4().hex) * 2
    name = name[:MAX_IDENTIFIER_LENGTH]
    assert len(name) == MAX_IDENTIFIER_LENGTH
    empty_table.create_index(["n"], name=name)
    empty_table._clone(empty_table.search_table, empty_table.search_table + "_tmp")
    try:
        empty_table.restore_index(name, suffix="_tmp")
        derived = derived_identifier(name, "_tmp")
        assert len(derived.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
        assert derived in catalog_index_names(db, empty_table.search_table + "_tmp")
    finally:
        db._execute(
            SQL("DROP TABLE IF EXISTS {0}").format(
                Identifier(empty_table.search_table + "_tmp")
            )
        )


def test_a_maximum_length_constraint_survives_a_suffixed_restore(db, empty_table):
    name = ("c%s" % uuid.uuid4().hex) * 2
    name = name[:MAX_IDENTIFIER_LENGTH]
    empty_table.create_constraint(["label"], "unique", name=name)
    empty_table._clone(empty_table.search_table, empty_table.search_table + "_tmp")
    try:
        empty_table.restore_constraint(name, suffix="_tmp")
        derived = derived_identifier(name, "_tmp")
        assert len(derived.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
        assert derived in catalog_constraint_names(
            db, empty_table.search_table + "_tmp"
        )
    finally:
        db._execute(
            SQL("DROP TABLE IF EXISTS {0}").format(
                Identifier(empty_table.search_table + "_tmp")
            )
        )


def test_a_deprecated_name_collision_stays_within_the_limit(db, empty_table):
    """
    _rename_if_exists moves an index out of the way as _depN; at the byte limit
    the suffix has to survive the shortening, or the second _depN would land on
    the first.
    """
    name = ("d%s" % uuid.uuid4().hex) * 2
    name = name[:MAX_IDENTIFIER_LENGTH]
    empty_table.create_index(["n"], name=name)
    empty_table.drop_index(name, permanent=False)
    empty_table.restore_index(name)
    # the index exists again; restoring once more must move the existing one
    # aside rather than collide with it
    empty_table.restore_index(name)
    empty_table.restore_index(name)
    built = catalog_index_names(db, empty_table.search_table)
    deprecated = [n for n in built if "_dep" in n]
    assert len(deprecated) == 2
    assert len(set(deprecated)) == 2
    for n in built:
        assert len(n.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH


def test_a_multibyte_name_is_cut_on_a_character_boundary():
    # é is two bytes: a naive cut at 63 bytes would split one in half
    base = "é" * 40
    derived = derived_identifier(base, "_tmp")
    assert len(derived.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
    assert derived.endswith("_tmp")
    # it is a real string, not mojibake
    assert derived.encode("utf-8").decode("utf-8") == derived


def test_a_name_that_fits_is_left_exactly_alone():
    assert derived_identifier("foo", "_tmp") == "foo_tmp"
    assert derived_identifier("foo") == "foo"
    assert derived_identifier("é" * 5, "_old1") == "é" * 5 + "_old1"


def test_derived_names_differing_only_in_the_cut_tail_do_not_collide():
    a = "x" * 60 + "aaa"
    b = "x" * 60 + "bbb"
    assert derived_identifier(a, "_tmp") != derived_identifier(b, "_tmp")


##################################################################
# storage parameters                                             #
##################################################################


def test_gin_pending_list_limit_has_no_client_side_ceiling():
    # the ceiling PostgreSQL applies comes from MAX_KILOBYTES, which depends on
    # the server's architecture, so the client cannot know it
    with pytest.raises(InvalidDefinitionError):
        normalize_storage_param("gin_pending_list_limit", 63, "gin")
    assert normalize_storage_param("gin_pending_list_limit", 64, "gin") == 64
    assert normalize_storage_param("gin_pending_list_limit", 2_097_153, "gin") == 2_097_153


@pytest.mark.parametrize("value", [True, 64.0, "64", [64], None])
def test_integer_storage_parameters_take_integers(value):
    with pytest.raises(InvalidDefinitionError):
        normalize_storage_param("fillfactor", value, "btree")


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True), (False, False), (1, True), (0, False),
        ("on", True), ("ON", True), ("of", False), ("off", False),
        ("true", True), ("t", True), ("yes", True), ("y", True),
        ("false", False), ("f", False), ("no", False), ("n", False),
        ("1", True), ("0", False),
    ],
)
def test_boolean_storage_parameters_normalize(value, expected):
    assert normalize_storage_param("fastupdate", value, "gin") is expected


@pytest.mark.parametrize("value", [0.0, 1.0, 2, "o", "maybe", b"on", None, []])
def test_boolean_storage_parameters_reject_everything_else(value):
    # 0.0 == False and 1.0 == True in Python: a float must be refused by type
    with pytest.raises(InvalidDefinitionError):
        normalize_storage_param("fastupdate", value, "gin")


def test_a_boolean_storage_parameter_is_stored_normalized(db, empty_table, tmp_path):
    name = "idx_%s" % uuid.uuid4().hex[:8]
    empty_table.create_index(
        ["data"], type="gin", name=name, storage_params={"fastupdate": "on"}
    )
    assert meta_index_rows(db, empty_table.search_table)[0]["storage_params"] == {
        "fastupdate": True
    }
    # and it survives export, reload and restore unchanged
    path = str(tmp_path / "indexes.txt")
    empty_table.copy_to_indexes(path)
    empty_table.drop_index(name)
    empty_table.reload_indexes(path)
    empty_table.restore_index(name)
    assert meta_index_rows(db, empty_table.search_table)[0]["storage_params"] == {
        "fastupdate": True
    }
    assert name in catalog_index_names(db, empty_table.search_table)


##################################################################
# the CHECK constraints psycodict does build                     #
##################################################################


@pytest.fixture
def check_function(db):
    """
    An approved, schema-qualified CHECK function, dropped when the test ends.

    A qualified name is what makes the quoting matter: it is two identifiers,
    and quoting it as one would name a function whose own name contains a dot.
    """
    schema = "psycodict_check_%s" % uuid.uuid4().hex[:8]
    qualified = "%s.nonneg" % schema
    db._execute(SQL("CREATE SCHEMA {0}").format(Identifier(schema)))
    db._execute(
        SQL(
            "CREATE FUNCTION {0}(integer) RETURNS boolean "
            "AS 'SELECT $1 >= 0' LANGUAGE sql IMMUTABLE"
        ).format(Identifier(schema, "nonneg"))
    )
    db.conn.commit()
    original = list(PostgresTable._valid_check_functions)
    PostgresTable._valid_check_functions.append(qualified)
    try:
        yield qualified
    finally:
        PostgresTable._valid_check_functions[:] = original
        db._execute(SQL("DROP SCHEMA {0} CASCADE").format(Identifier(schema)))
        db.conn.commit()


def test_an_approved_check_function_survives_the_whole_round_trip(
    db, empty_table, check_function, tmp_path
):
    name = "chk_%s" % uuid.uuid4().hex[:8]
    empty_table.create_constraint(["n"], "check", name=name, check_func=check_function)
    assert name in catalog_constraint_names(db, empty_table.search_table)

    # PostgreSQL is really enforcing it
    empty_table.insert_many([{"n": 1, "label": "ok"}])
    with pytest.raises(Exception):
        empty_table.insert_many([{"n": -1, "label": "bad"}])
    db.conn.rollback()

    # the statement names the function as two quoted identifiers
    definition = db._execute(
        SQL("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s"),
        [name],
    ).fetchone()[0]
    schema, function = check_function.split(".")
    assert schema in definition and function in definition

    # drop and restore on the live table
    empty_table.drop_constraint(name)
    assert name not in catalog_constraint_names(db, empty_table.search_table)
    empty_table.restore_constraint(name)
    assert name in catalog_constraint_names(db, empty_table.search_table)

    # export the metadata, reload it, and restore from what was reloaded
    path = str(tmp_path / "constraints.txt")
    empty_table.copy_to_constraints(path)
    empty_table.drop_constraint(name)
    empty_table.reload_constraints(path)
    empty_table.restore_constraint(name)
    assert name in catalog_constraint_names(db, empty_table.search_table)

    # and on a suffixed table, which is the path a reload takes
    empty_table._clone(empty_table.search_table, empty_table.search_table + "_tmp")
    try:
        empty_table.restore_constraint(name, suffix="_tmp")
        assert derived_identifier(name, "_tmp") in catalog_constraint_names(
            db, empty_table.search_table + "_tmp"
        )
    finally:
        db._execute(
            SQL("DROP TABLE IF EXISTS {0}").format(
                Identifier(empty_table.search_table + "_tmp")
            )
        )
        empty_table.drop_constraint(name, permanent=True)


def test_an_unapproved_check_function_is_still_refused(db, empty_table):
    with pytest.raises(InvalidDefinitionError, match="approved"):
        empty_table.create_constraint(
            ["n"], "check", name="chk_bad", check_func="public.nonneg"
        )


def test_a_check_function_that_is_not_nameable_is_refused():
    from psycodict.validation import parse_check_function

    approved = ("a.b.c", "", "sch.", ".func", "f\x00")
    for name in approved:
        with pytest.raises(InvalidDefinitionError):
            parse_check_function(name, approved)
    assert parse_check_function("f", ("f",)) == ("f",)
    assert parse_check_function("s.f", ("s.f",)) == ("s", "f")


##################################################################
# a stored name is what the relation is called                   #
##################################################################

# 64 ASCII bytes: one past what PostgreSQL can name a relation.
OVERLONG = "x" * 64


def meta_index_names(db, table):
    cur = db._execute(
        SQL("SELECT index_name FROM meta_indexes WHERE table_name = %s"), [table]
    )
    return sorted(rec[0] for rec in cur)


def test_reload_indexes_refuses_an_overlong_name_atomically(db, empty_table, tmp_path):
    """
    A name over the identifier limit cannot be the name of a relation, so it
    cannot be a metadata key either: the row would describe an index that does
    not exist.  Shortening it here would build the index under a name the
    metadata does not have, which is exactly the disagreement the derived-name
    helper exists to prevent.
    """
    empty_table.create_index(["n"])
    name = empty_table.search_table + "_n"
    before_rows = meta_index_rows(db, empty_table.search_table)
    before_built = set(empty_table._list_built_indexes())

    path = indexes_file_with(tmp_path, empty_table, **{name: OVERLONG})
    with pytest.raises(InvalidDefinitionError, match="byte limit"):
        empty_table.reload_indexes(path)

    assert meta_index_rows(db, empty_table.search_table) == before_rows
    assert set(empty_table._list_built_indexes()) == before_built
    # and nothing was created under a shortened spelling
    assert derived_identifier(OVERLONG) not in catalog_index_names(
        db, empty_table.search_table
    )


def test_reload_constraints_refuses_an_overlong_name_atomically(db, empty_table, tmp_path):
    empty_table.create_constraint(["label"], "unique")
    name = empty_table.search_table + "_c_label"
    path = tmp_path / "constraints.txt"
    empty_table.copy_to_constraints(str(path))
    path.write_text(path.read_text().replace(name, OVERLONG, 1))

    with pytest.raises(InvalidDefinitionError, match="byte limit"):
        empty_table.reload_constraints(str(path))
    assert list(empty_table.list_constraints()) == [name]
    assert name in catalog_constraint_names(db, empty_table.search_table)


def test_revert_indexes_refuses_an_overlong_historical_name(db, empty_table, tmp_path):
    empty_table.create_index(["n"])
    path = str(tmp_path / "indexes.txt")
    empty_table.copy_to_indexes(path)
    empty_table.reload_indexes(path)
    empty_table.reload_indexes(path)

    db._execute(
        SQL("UPDATE meta_indexes_hist SET index_name = %s "
            "WHERE table_name = %s AND version = 0"),
        [OVERLONG, empty_table.search_table],
    )
    before = meta_index_rows(db, empty_table.search_table)
    with pytest.raises(InvalidDefinitionError, match="byte limit"):
        empty_table.revert_indexes(version=0)
    assert meta_index_rows(db, empty_table.search_table) == before


def test_revert_constraints_refuses_an_overlong_historical_name(db, empty_table, tmp_path):
    empty_table.create_constraint(["label"], "unique")
    path = str(tmp_path / "constraints.txt")
    empty_table.copy_to_constraints(path)
    empty_table.reload_constraints(path)
    empty_table.reload_constraints(path)

    db._execute(
        SQL("UPDATE meta_constraints_hist SET constraint_name = %s "
            "WHERE table_name = %s AND version = 0"),
        [OVERLONG, empty_table.search_table],
    )
    before = list(empty_table.list_constraints())
    with pytest.raises(InvalidDefinitionError, match="byte limit"):
        empty_table.revert_constraints(version=0)
    assert list(empty_table.list_constraints()) == before


def test_a_directly_poisoned_overlong_index_row_cannot_be_restored(db, empty_table):
    """
    The row is refused as it stands, before a physical name is derived from
    it -- otherwise the restore would succeed under a shortened name that the
    metadata does not record.
    """
    empty_table.create_index(["n"])
    name = empty_table.search_table + "_n"
    empty_table.drop_index(name, permanent=False)
    db._execute(
        SQL("UPDATE meta_indexes SET index_name = %s "
            "WHERE table_name = %s AND index_name = %s"),
        [OVERLONG, empty_table.search_table, name],
    )
    with pytest.raises(InvalidDefinitionError, match="byte limit"):
        empty_table.restore_index(OVERLONG)
    built = catalog_index_names(db, empty_table.search_table)
    assert derived_identifier(OVERLONG) not in built
    assert derived_identifier(OVERLONG, "_tmp") not in built
    # the metadata was not rewritten either
    assert meta_index_names(db, empty_table.search_table) == [OVERLONG]


def test_a_directly_poisoned_overlong_constraint_row_cannot_be_restored(db, empty_table):
    empty_table.create_constraint(["label"], "unique")
    name = empty_table.search_table + "_c_label"
    empty_table.drop_constraint(name)
    db._execute(
        SQL("UPDATE meta_constraints SET constraint_name = %s "
            "WHERE table_name = %s AND constraint_name = %s"),
        [OVERLONG, empty_table.search_table, name],
    )
    with pytest.raises(InvalidDefinitionError, match="byte limit"):
        empty_table.restore_constraint(OVERLONG)
    assert derived_identifier(OVERLONG) not in catalog_constraint_names(
        db, empty_table.search_table
    )


@pytest.mark.parametrize(
    "name,fits",
    [
        ("a" * 63, True),
        ("a" * 64, False),
        # é is two bytes: the limit is bytes, not characters
        ("é" * 31 + "a", True),
        ("é" * 32, False),
    ],
)
def test_the_persistent_name_limit_is_measured_in_bytes(name, fits):
    from psycodict.validation import validate_persistent_name

    if fits:
        assert validate_persistent_name(name, "Index") == name
    else:
        with pytest.raises(InvalidDefinitionError, match="byte limit"):
            validate_persistent_name(name, "Index")


##################################################################
# the names of a table's own relations                           #
##################################################################


def test_a_search_tables_relations_have_one_formula():
    """
    Every path computes these the same way, which is what keeps creation,
    lookup, swap and cleanup addressing the same relation.
    """
    assert physical_table_name("foo") == "foo"
    assert physical_table_name("foo", "counts") == "foo_counts"
    assert physical_table_name("foo", "stats") == "foo_stats"
    assert physical_table_name("foo", "search", "_tmp") == "foo_tmp"
    assert physical_table_name("foo", "counts", "_tmp") == "foo_counts_tmp"
    assert physical_table_name("foo", "stats", "_old2") == "foo_stats_old2"


def test_a_relation_psycodict_would_have_to_truncate_is_refused():
    """
    These names are concatenated rather than shortened, because PostgreSQL
    truncates consistently and an existing database's companions are already
    under the truncated spelling -- shortening them here would look for
    relations that do not exist.  What truncation does break is distinctness,
    so a relation about to be *created* under an over-long name is refused.
    """
    legacy = "l" * 60
    # addressing an existing relation is left alone ...
    assert physical_table_name(legacy, "counts") == legacy + "_counts"
    # ... but creating one under that name is not
    with pytest.raises(InvalidDefinitionError, match="truncated"):
        check_new_table_name(physical_table_name(legacy, "counts", "_old1"))
    # a name with room for the longest suffix psycodict appends can never
    # reach it
    ordinary = "o" * (MAX_IDENTIFIER_LENGTH - len("_counts_old999"))
    for component in ("search", "counts", "stats"):
        for lifecycle in ("", "_tmp", "_old999"):
            assert check_new_table_name(
                physical_table_name(ordinary, component, lifecycle)
            )


def schema_relations(db):
    """
    The relations in psycodict's schema, as PostgreSQL names them.
    """
    cur = db._execute(
        SQL(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')"
        )
    )
    return {rec[0] for rec in cur}


def test_a_legacy_long_table_cannot_silently_alias_its_own_backup(db):
    """
    A 61-byte name's _tmp copy is 65 bytes, which PostgreSQL would truncate --
    and <61 bytes>_old1 and _old2 truncate to the same relation.  A reload of
    such a table has to stop rather than overwrite its own backup.

    Such a table cannot be created through psycodict any more; this one is made
    behind its back, the way a database predating the limit would have it.
    """
    name = "t" + "l" * 60
    assert len(physical_table_name(name, lifecycle="_tmp")) > MAX_IDENTIFIER_LENGTH
    for suffix in ("", "_counts", "_stats"):
        db._execute(
            SQL("CREATE TABLE {0} (id bigint, n integer)").format(
                Identifier(name + suffix)
            )
        )
    try:
        with pytest.raises(InvalidDefinitionError, match="truncated"):
            db._clone(name, physical_table_name(name, lifecycle="_tmp"))
        # and nothing was created under the truncated spelling
        truncated = physical_table_name(name, lifecycle="_tmp")[:MAX_IDENTIFIER_LENGTH]
        assert truncated not in schema_relations(db)
    finally:
        for suffix in ("", "_counts", "_stats", "_tmp"):
            db._execute(
                SQL("DROP TABLE IF EXISTS {0}").format(Identifier(name + suffix))
            )


def test_a_long_name_table_reloads_and_backs_up_through_canonical_names(db, tmp_path):
    """
    End to end: the _tmp copies, the swap and the _old1 backups all address the
    relations the helper names.
    """
    # long enough to be interesting, short enough that every derived name fits
    name = "t" + "e" * 39
    db.create_table(name, [("n", "integer"), ("label", "text")], "label")
    try:
        table = db[name]
        table.insert_many([{"n": 1, "label": "one"}])
        searchfile = str(tmp_path / "search.txt")
        table.copy_to(searchfile)
        table.reload(searchfile)

        relations = schema_relations(db)
        for component in ("search", "counts", "stats"):
            assert physical_table_name(name, component) in relations
            # the _tmp copies are gone, and the backup is under its own name
            assert physical_table_name(name, component, "_tmp") not in relations
        assert physical_table_name(name, "search", "_old1") in relations
        assert db[name].count() == 1

        # a second reload makes a second backup, distinct from the first
        db[name].reload(searchfile)
        relations = schema_relations(db)
        assert physical_table_name(name, "search", "_old1") in relations
        assert physical_table_name(name, "search", "_old2") in relations
    finally:
        for lifecycle in ("_old1", "_old2", "_tmp"):
            for component in ("search", "counts", "stats"):
                db._execute(
                    SQL("DROP TABLE IF EXISTS {0}").format(
                        Identifier(physical_table_name(name, component, lifecycle))
                    )
                )
        db.drop_table(name, force=True)


def test_a_swap_checks_every_destination_before_renaming_any(db):
    """
    A swap renames a batch of relations.  PostgreSQL truncates a name over its
    limit rather than refusing it, so a batch whose later destination does not
    fit has to stop before it has renamed the earlier ones -- otherwise the
    reload is half done and the failure names a relation that was never
    touched.
    """
    short = "swapok_%s" % uuid.uuid4().hex[:8]
    # long enough that adding the swap's suffix does not fit
    long = "s" + "w" * 60
    for name in (short, long):
        db._execute(
            SQL("CREATE TABLE {0} (n integer)").format(Identifier(name))
        )
    try:
        with pytest.raises(InvalidDefinitionError, match="truncated"):
            # the first destination fits, the second does not
            db._swap([short, long], "", "_old1")
        relations = schema_relations(db)
        assert short in relations
        assert long in relations
        assert short + "_old1" not in relations
    finally:
        for name in (short, long, short + "_old1"):
            db._execute(
                SQL("DROP TABLE IF EXISTS {0}").format(Identifier(name))
            )


def test_a_swap_refuses_destinations_that_are_not_distinct(db):
    """
    Two sources whose destinations coincide would make one relation out of two.
    """
    base = "swapdup_%s" % uuid.uuid4().hex[:8]
    with pytest.raises(InvalidDefinitionError, match="same name"):
        db._swap([base, base], "", "_old1")


def test_create_table_refuses_a_relation_it_would_have_to_truncate(db):
    """
    The guard is in _create_table, so every caller inherits it -- including
    header-driven creation and the internal copies.
    """
    name = "c" + "t" * 63
    with pytest.raises(InvalidDefinitionError, match="truncated"):
        db._create_table(name, [("n", "integer")])
    assert name[:MAX_IDENTIFIER_LENGTH] not in schema_relations(db)
##################################################################
# search table names, and the files they generate                #
##################################################################


BAD_TABLE_NAMES = [
    "/tmp/escape",
    "../escape",
    "foo/bar",
    "foo\\bar",
    ".",
    "..",
    "",
    " tab",
    "tab ",
    "foo bar",
    "foo;bar",
    'foo"bar',
    "Foo_bar",          # a search table name is lowercase
    "1foo",             # and starts with a letter
    "foo\x00bar",
    "tablе_x",     # Cyrillic lookalike
    # names psycodict itself makes out of a search table's name
    "foo_tmp",
    "foo_old1",
    "foo_dep2",
    "foo_counts",
    "foo_stats",
    "foo_pkey",
    "foo_dep3",
    # names the database object already uses
    "conn",
    "config",
    "tables",
    "tablenames",
    "_execute",
    "reset_connection",
    # over PostgreSQL's identifier limit
    "t" + "o" * 63,
]


@pytest.mark.parametrize("name", BAD_TABLE_NAMES)
def test_create_table_rejects_unusable_names(db, name):
    with pytest.raises(ValueError):
        db.create_table(name, [("n", "integer")], "n")
    assert name not in db.tablenames


@pytest.mark.parametrize("name", BAD_TABLE_NAMES)
def test_rename_table_rejects_unusable_names(db, empty_table, name):
    with pytest.raises(ValueError):
        db.rename_table(empty_table.search_table, name)
    # the table is still there under its own name
    assert empty_table.search_table in db.tablenames


def test_core_attributes_survive_a_table_named_after_them(db, empty_table):
    """
    A table object cannot take the place of the connection or a method.

    Table objects used to be stored in the database object's instance
    dictionary, where a table called `conn` would have replaced the connection.
    """
    conn, config = db.conn, db.config
    db.tables["conn"] = empty_table
    db.tables["_execute"] = empty_table
    try:
        assert db.conn is conn
        assert db.config is config
        assert callable(db._execute)
        # the real attributes win; the table is still reachable by subscript
        assert db["conn"] is empty_table
    finally:
        del db.tables["conn"]
        del db.tables["_execute"]


def test_refresh_tables_rejects_a_poisoned_meta_tables_row(db, empty_table, tmp_path):
    """
    meta_tables is a table like any other, and a name in it becomes an
    identifier, an attribute name and the stem of an export's filenames.
    """
    conn, config = db.conn, db.config
    tablenames = db.tablenames
    escape = str(tmp_path / "escaped")
    db._execute(
        SQL("UPDATE meta_tables SET name = %s WHERE name = %s"),
        [escape, empty_table.search_table],
    )
    try:
        with pytest.raises(ValueError):
            db.refresh_tables()
        assert db.conn is conn
        assert db.config is config
        assert callable(db._execute)
        assert not os.path.exists(escape)
    finally:
        db._execute(
            SQL("UPDATE meta_tables SET name = %s WHERE name = %s"),
            [empty_table.search_table, escape],
        )
        db.refresh_tables()
    assert db.tablenames == tablenames


def test_attribute_and_subscript_access_agree(db, empty_table):
    name = empty_table.search_table
    assert getattr(db, name) is db[name]
    assert name in db.tablenames


def test_a_dropped_table_is_gone_from_both_kinds_of_access(db, table_factory):
    table = table_factory()
    name = table.search_table
    assert getattr(db, name) is db[name]
    db.drop_table(name, force=True)
    assert name not in db.tablenames
    assert not hasattr(db, name)
    with pytest.raises(ValueError):
        db[name]


def test_a_renamed_table_moves_in_both_kinds_of_access(db, table_factory, tmp_path):
    table = table_factory()
    old = table.search_table
    new = "test_%s" % uuid.uuid4().hex[:12]
    db.rename_table(old, new)
    try:
        assert new in db.tablenames
        assert old not in db.tablenames
        assert getattr(db, new) is db[new]
        assert not hasattr(db, old)
    finally:
        db.drop_table(new, force=True)


def test_generated_export_paths_stay_in_the_export_folder(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    assert safe_child_path(root, "table.txt") == (root / "table.txt").resolve()
    for escape in ["../escape.txt", "/tmp/escape.txt", "a/../../escape.txt"]:
        with pytest.raises(ValueError):
            safe_child_path(root, escape)


def test_a_symlink_out_of_the_export_folder_is_not_inside_it(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        safe_child_path(root, "link/escape.txt")


def test_copy_to_writes_only_inside_the_folder(db, filled_table, tmp_path):
    folder = tmp_path / "data"
    db.copy_to([filled_table.search_table], str(folder))
    written = list(folder.glob("*"))
    assert written
    for path in written:
        assert path.resolve().parent == folder.resolve()


def rename_family(db, old, new):
    """
    Rename a search table's whole family, in the database and in meta_tables,
    the way an administrator renaming a legacy table would.

    Through a scratch name, since the interesting case renames ``foo`` to
    ``foo_stats``, which is what ``foo``'s stats table is already called.
    """
    scratch = "swap_%s" % uuid.uuid4().hex[:10]
    for source, target in ((old, scratch), (scratch, new)):
        for suffix in ("", "_counts", "_stats"):
            db._execute(
                SQL("ALTER TABLE {0} RENAME TO {1}").format(
                    Identifier(source + suffix), Identifier(target + suffix)
                )
            )
    db._execute(
        SQL("UPDATE meta_tables SET name = %s WHERE name = %s"), [new, old]
    )


def legacy_family(db, name):
    """
    Build a search table's whole family behind psycodict's back, the way a
    database predating MAX_SEARCH_TABLE_NAME_LENGTH has it, and record it in
    meta_tables.  Returns a callable that takes it all away again.
    """
    db._execute(SQL("CREATE TABLE {0} (id bigint, label text)").format(
        Identifier(physical_table_name(name))))
    db._execute(SQL(
        "CREATE TABLE {0} (cols jsonb, values jsonb, count bigint, extra boolean, "
        "split boolean)").format(Identifier(physical_table_name(name, "counts"))))
    db._execute(SQL(
        "CREATE TABLE {0} (cols jsonb, stat text, value numeric, constraint_cols jsonb, "
        "constraint_values jsonb, threshold integer)").format(
            Identifier(physical_table_name(name, "stats"))))
    db._execute(
        SQL("INSERT INTO meta_tables (name, sort, id_ordered, out_of_order, "
            "stats_valid, label_col, total) VALUES (%s, NULL, false, false, "
            "true, 'label', 0)"),
        [name],
    )

    def drop():
        db._execute(SQL("DELETE FROM meta_tables WHERE name = %s"), [name])
        for component in ("search", "counts", "stats"):
            db._execute(SQL("DROP TABLE IF EXISTS {0}").format(
                Identifier(physical_table_name(name, component))))

    return drop


@pytest.mark.parametrize("length", [57, 58, 59, 60, 61])
def test_a_legacy_long_family_still_loads_through_refresh_tables(db, length):
    """
    A legacy name long enough that <name>_counts does not fit in 63 bytes.
    PostgreSQL holds that relation under the truncated spelling; psycodict goes
    on addressing it by the long one, and the registry check -- which
    refresh_tables runs before it builds anything, so a false negative here
    means the database cannot be connected to at all -- has to compare the
    spelling the catalog actually holds.
    """
    name = "legacy%s_" % length + "n" * (length - 7 - len(str(length)))
    assert identifier_bytes(name) == length
    counts = physical_table_name(name, "counts")
    assert identifier_bytes(counts) > MAX_IDENTIFIER_LENGTH
    drop = legacy_family(db, name)
    try:
        # the premise: PostgreSQL really did store the counts table short
        relations = schema_relations(db)
        assert counts not in relations
        assert catalog_identifier(counts) in relations

        db.refresh_tables()
        assert name in db.tablenames
        assert db[name].count() == 0
        # and the companions, whose names are the truncated ones, are reached
        table = db[name]
        assert table.stats.counts == counts and table.stats.stats == \
            physical_table_name(name, "stats")
        table.insert_many([{"label": "a"}, {"label": "b"}])
        assert table.count({}, record=True) == 2
        table.stats._record_count({}, 2)
        assert table.stats.quick_count({}) == 2
        table.stats.add_stats(["label"])
        # rows really landed in the relations the catalog holds truncated
        for companion in (counts, physical_table_name(name, "stats")):
            cur = db._execute(SQL("SELECT count(*) FROM {0}").format(Identifier(companion)))
            assert cur.fetchone()[0] > 0, companion
    finally:
        drop()
        db.refresh_tables()
    assert name not in db.tablenames


@pytest.mark.parametrize("length", [62, 63])
def test_a_legacy_name_with_indistinguishable_companions_is_refused(db, length):
    """
    At 62 bytes <name>_counts and <name>_stats both truncate to <name>_, and at
    63 the whole family collapses onto the search table.  Those really are one
    relation, so the failure has to be reported rather than papered over by
    comparing catalog spellings.
    """
    name = "z" * length
    family = search_table_family(name)
    assert len({catalog_identifier(relation) for relation in family}) < len(family)
    for relation in {catalog_identifier(relation) for relation in family}:
        db._execute(SQL("CREATE TABLE {0} (id bigint)").format(Identifier(relation)))
    db._execute(
        SQL("INSERT INTO meta_tables (name, sort, id_ordered, out_of_order, "
            "stats_valid, label_col, total) VALUES (%s, NULL, false, false, "
            "true, NULL, 0)"),
        [name],
    )
    before = dict(db.tables)
    try:
        with pytest.raises(InvalidDefinitionError, match="single relation"):
            db.refresh_tables()
        assert db.tables == before
    finally:
        db._execute(SQL("DELETE FROM meta_tables WHERE name = %s"), [name])
        for relation in {catalog_identifier(relation) for relation in family}:
            db._execute(SQL("DROP TABLE IF EXISTS {0}").format(Identifier(relation)))
        db.refresh_tables()


def test_a_missing_companion_is_still_reported_for_a_long_name(db):
    """
    Comparing catalog spellings must not turn the missing-relation check into a
    no-op: a long name whose counts table is genuinely absent is still refused,
    and the message names both spellings.
    """
    name = "legacy_absent_" + "n" * 43
    assert identifier_bytes(name) == 57
    drop = legacy_family(db, name)
    db._execute(SQL("DROP TABLE {0}").format(
        Identifier(physical_table_name(name, "counts"))))
    try:
        with pytest.raises(InvalidDefinitionError, match="PostgreSQL stores as"):
            db.refresh_tables()
    finally:
        drop()
        db.refresh_tables()


def test_a_legacy_name_ending_in_stats_is_accepted_with_its_family(db, empty_table):
    """
    What makes a name a search table is owning a complete family, not how it is
    spelled: the LMFDB has a search table called hgcwa_per_group_stats, whose
    own counts and stats tables are hgcwa_per_group_stats_counts and
    hgcwa_per_group_stats_stats.
    """
    old = empty_table.search_table
    new = old + "_stats"
    rename_family(db, old, new)
    try:
        db.refresh_tables()
        assert new in db.tablenames
        assert db[new].count() == 0
    finally:
        rename_family(db, new, old)
        db.refresh_tables()
    assert old in db.tablenames


def test_a_row_claiming_another_tables_companion_is_refused(db, empty_table):
    """
    A row naming foo_counts when foo is a search table would put foo's counts
    table into the registry as a table in its own right.
    """
    name = empty_table.search_table
    before = dict(db.tables)
    db._execute(
        SQL("INSERT INTO meta_tables (name, sort, id_ordered, out_of_order, "
            "stats_valid, label_col, total) VALUES (%s, NULL, false, false, "
            "true, NULL, 0)"),
        [name + "_counts"],
    )
    try:
        with pytest.raises(ValueError, match="claim"):
            db.refresh_tables()
        # nothing was registered, and the tables that were there are the same
        # objects they were
        assert db.tables == before
    finally:
        db._execute(SQL("DELETE FROM meta_tables WHERE name = %s"), [name + "_counts"])
        db.refresh_tables()


@pytest.mark.parametrize(
    "poison", ["meta_tables", "meta_indexes", "meta_format", "meta_constraints_hist"]
)
def test_a_row_naming_a_metadata_relation_is_refused(db, empty_table, poison):
    before = dict(db.tables)
    db._execute(
        SQL("INSERT INTO meta_tables (name, sort, id_ordered, out_of_order, "
            "stats_valid, label_col, total) VALUES (%s, NULL, false, false, "
            "true, NULL, 0)"),
        [poison],
    )
    try:
        with pytest.raises(ValueError, match="psycodict's own relations"):
            db.refresh_tables()
        assert db.tables == before
        with pytest.raises(ValueError):
            db[poison]
    finally:
        db._execute(SQL("DELETE FROM meta_tables WHERE name = %s"), [poison])
        db.refresh_tables()


@pytest.mark.parametrize("suffix", ["_tmp", "_old1", "_dep0", "_pkey"])
def test_a_row_naming_scratch_space_is_refused(db, empty_table, suffix):
    name = empty_table.search_table + suffix
    before = dict(db.tables)
    db._execute(
        SQL("INSERT INTO meta_tables (name, sort, id_ordered, out_of_order, "
            "stats_valid, label_col, total) VALUES (%s, NULL, false, false, "
            "true, NULL, 0)"),
        [name],
    )
    try:
        with pytest.raises(ValueError, match="swaps through"):
            db.refresh_tables()
        assert db.tables == before
    finally:
        db._execute(SQL("DELETE FROM meta_tables WHERE name = %s"), [name])
        db.refresh_tables()


def test_a_family_missing_its_companions_is_refused(db, empty_table):
    """
    Renaming only the base relation leaves a name whose counts and stats
    tables are somebody else's -- or nobody's.
    """
    old = empty_table.search_table
    new = old + "x"
    before = dict(db.tables)
    db._execute(
        SQL("ALTER TABLE {0} RENAME TO {1}").format(Identifier(old), Identifier(new))
    )
    db._execute(SQL("UPDATE meta_tables SET name = %s WHERE name = %s"), [new, old])
    try:
        with pytest.raises(ValueError, match="counts table and its stats table"):
            db.refresh_tables()
        assert db.tables == before
    finally:
        db._execute(
            SQL("ALTER TABLE {0} RENAME TO {1}").format(
                Identifier(new), Identifier(old)
            )
        )
        db._execute(SQL("UPDATE meta_tables SET name = %s WHERE name = %s"), [old, new])
        db.refresh_tables()


def test_a_name_over_the_identifier_limit_is_refused_from_metadata(db, empty_table):
    """
    A string longer than 63 bytes cannot be the name of a PostgreSQL relation:
    the server truncates it, so keeping the longer string as a registry key
    while the SQL reaches a shorter relation is an alias.
    """
    with pytest.raises(InvalidDefinitionError, match="byte limit"):
        validate_search_table_name("a" * 64, strict=False)
    assert validate_search_table_name("a" * 63, strict=False)

    name = empty_table.search_table
    before = dict(db.tables)
    db._execute(
        SQL("UPDATE meta_tables SET name = %s WHERE name = %s"), ["a" * 64, name]
    )
    try:
        with pytest.raises(ValueError, match="byte limit"):
            db.refresh_tables()
        assert db.tables == before
    finally:
        db._execute(
            SQL("UPDATE meta_tables SET name = %s WHERE name = %s"), [name, "a" * 64]
        )
        db.refresh_tables()


##################################################################
# room for the names psycodict generates                         #
##################################################################


def test_the_longest_accepted_name_survives_a_whole_reload(db, tmp_path):
    """
    A new name has to leave room for the relations psycodict names after it:
    the companions at creation, then _tmp on a reload and _oldN for the backup.
    """
    name = "t" + "e" * (MAX_SEARCH_TABLE_NAME_LENGTH - 1)
    assert len(name) == MAX_SEARCH_TABLE_NAME_LENGTH
    db.create_table(name, [("n", "integer"), ("label", "text")], "label")
    try:
        table = db[name]
        table.insert_many([{"n": 1, "label": "one"}])
        searchfile = str(tmp_path / "search.txt")
        table.copy_to(searchfile)
        table.reload(searchfile)

        # every relation the reload made is the one psycodict names, untruncated
        relations = db._schema_relations()
        for component in ("search", "counts", "stats"):
            assert physical_table_name(name, component) in relations
            # and the _tmp copies are gone again
            assert physical_table_name(name, component, "_tmp") not in relations
        assert physical_table_name(name, "search", "_old1") in relations
        # every relation it could name still fits
        for suffix in ["_counts_old1", "_stats_old1", "_counts_tmp",
                       "_counts_old%s" % ("9" * MAX_BACKUP_NUMBER_DIGITS)]:
            assert len((name + suffix).encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
        assert db[name].count() == 1

        # a second reload makes a second backup, distinct from the first
        db[name].reload(searchfile)
        relations = db._schema_relations()
        assert physical_table_name(name, "search", "_old1") in relations
        assert physical_table_name(name, "search", "_old2") in relations
    finally:
        for lifecycle in ("_old1", "_old2", "_tmp"):
            for component in ("search", "counts", "stats"):
                db._execute(
                    SQL("DROP TABLE IF EXISTS {0}").format(
                        Identifier(physical_table_name(name, component, lifecycle))
                    )
                )
        db.drop_table(name, force=True)


def test_one_byte_past_the_limit_creates_nothing(db):
    name = "t" + "e" * MAX_SEARCH_TABLE_NAME_LENGTH
    with pytest.raises(ValueError, match="at most %s" % MAX_SEARCH_TABLE_NAME_LENGTH):
        db.create_table(name, [("n", "integer")], "n")
    relations = db._schema_relations()
    for suffix in ["", "_counts", "_stats"]:
        assert name + suffix not in relations
    assert name not in db.tablenames
    assert not db._execute(
        SQL("SELECT 1 FROM meta_tables WHERE name = %s"), [name]
    ).fetchone()


def test_rename_table_refuses_a_destination_with_no_room(db, empty_table):
    name = empty_table.search_table
    too_long = "t" + "e" * MAX_SEARCH_TABLE_NAME_LENGTH
    with pytest.raises(ValueError, match="at most"):
        db.rename_table(name, too_long)
    # the original family is untouched
    relations = db._schema_relations()
    for suffix in ["", "_counts", "_stats"]:
        assert name + suffix in relations
    assert name in db.tablenames


def test_no_two_generated_relation_names_collide_after_truncation():
    """
    The reserved room is only useful if it covers every suffix psycodict
    appends to a search table's name.
    """
    name = "t" * MAX_SEARCH_TABLE_NAME_LENGTH
    suffixes = [""]
    for companion in ["", "_counts", "_stats"]:
        suffixes.append(companion)
        suffixes.append(companion + "_tmp")
        for backup in range(1, 10 ** MAX_BACKUP_NUMBER_DIGITS):
            if backup > 3 and backup < 10 ** MAX_BACKUP_NUMBER_DIGITS - 1:
                continue
            suffixes.append("%s_old%s" % (companion, backup))
    generated = [name + suffix for suffix in suffixes]
    for relation in generated:
        assert len(relation.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH, relation
    truncated = [relation.encode("utf-8")[:MAX_IDENTIFIER_LENGTH] for relation in generated]
    assert len(set(truncated)) == len(set(generated))


def test_a_name_that_could_escape_the_export_folder_is_refused_anywhere(db, empty_table, tmp_path):
    """
    What is checked of every name, however it arrives, is what would make it
    unsafe to use.
    """
    name = empty_table.search_table
    for bad in [str(tmp_path / "escape"), "../escape", "foo/bar", "foo bar"]:
        db._execute(
            SQL("UPDATE meta_tables SET name = %s WHERE name = %s"), [bad, name]
        )
        try:
            with pytest.raises(ValueError):
                db.refresh_tables()
        finally:
            db._execute(
                SQL("UPDATE meta_tables SET name = %s WHERE name = %s"), [name, bad]
            )
    db.refresh_tables()
    assert name in db.tablenames


##################################################################
# names psycodict derives when a table is renamed                #
##################################################################


def test_a_long_index_name_survives_a_table_rename(db, table_factory):
    """
    Renaming a table renames its indexes by substituting the table's name, and
    a short name replaced by a long one can push the result past the identifier
    limit.  Both the metadata and the catalog have to get the same fitted name,
    or every later drop and restore looks for a relation that is not there.
    """
    table = table_factory()
    old = table.search_table
    index = (old + "_i" + "x" * MAX_IDENTIFIER_LENGTH)[:MAX_IDENTIFIER_LENGTH]
    table.create_index(["n"], name=index)

    new = "r" + "e" * (MAX_SEARCH_TABLE_NAME_LENGTH - 1)
    candidate = index.replace(old, new)
    assert len(candidate.encode("utf-8")) > MAX_IDENTIFIER_LENGTH

    db.rename_table(old, new)
    try:
        renamed = list(db[new].list_indexes())
        assert len(renamed) == 1
        stored = renamed[0]
        assert len(stored.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
        assert stored == derived_identifier(candidate)
        # what psycodict recorded is what PostgreSQL has, and the truncation
        # the server would have produced is not standing in for it
        built = catalog_index_names(db, new)
        assert stored in built
        assert candidate[:MAX_IDENTIFIER_LENGTH] not in built or stored == candidate[
            :MAX_IDENTIFIER_LENGTH
        ]
        assert index not in built
        # and the metadata name is usable
        db[new].drop_index(stored, permanent=False)
        db[new].restore_index(stored)
        assert stored in catalog_index_names(db, new)
    finally:
        db.drop_table(new, force=True)


def test_a_long_constraint_name_survives_a_table_rename(db, table_factory):
    table = table_factory()
    old = table.search_table
    constraint = (old + "_c" + "x" * MAX_IDENTIFIER_LENGTH)[:MAX_IDENTIFIER_LENGTH]
    table.create_constraint(["label"], "unique", name=constraint)

    new = "r" + "e" * (MAX_SEARCH_TABLE_NAME_LENGTH - 1)
    candidate = constraint.replace(old, new)
    assert len(candidate.encode("utf-8")) > MAX_IDENTIFIER_LENGTH

    db.rename_table(old, new)
    try:
        stored = list(db[new].list_constraints())[0]
        assert len(stored.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
        assert stored == derived_identifier(candidate)
        assert stored in catalog_constraint_names(db, new)
        db[new].drop_constraint(stored)
        db[new].restore_constraint(stored)
        assert stored in catalog_constraint_names(db, new)
    finally:
        db.drop_table(new, force=True)


def test_current_and_history_metadata_agree_after_a_rename(db, table_factory, tmp_path):
    """
    The same old name occurs in meta_indexes and meta_indexes_hist; both have
    to be rewritten to the same thing.
    """
    table = table_factory()
    old = table.search_table
    index = (old + "_i" + "x" * MAX_IDENTIFIER_LENGTH)[:MAX_IDENTIFIER_LENGTH]
    table.create_index(["n"], name=index)
    path = str(tmp_path / "indexes.txt")
    table.copy_to_indexes(path)
    table.reload_indexes(path)          # writes a history version

    new = "r" + "e" * (MAX_SEARCH_TABLE_NAME_LENGTH - 1)
    db.rename_table(old, new)
    try:
        current = {rec[0] for rec in db._execute(
            SQL("SELECT index_name FROM meta_indexes WHERE table_name = %s"), [new]
        )}
        history = {rec[0] for rec in db._execute(
            SQL("SELECT index_name FROM meta_indexes_hist WHERE table_name = %s"), [new]
        )}
        assert current == history
        assert len(current) == 1
        assert current.pop() == derived_identifier(index.replace(old, new))
    finally:
        db.drop_table(new, force=True)


def test_a_rename_whose_fitted_name_is_taken_picks_another(db, table_factory):
    """
    Two indexes whose fitted names would collide must not both become the same
    relation.
    """
    table = table_factory()
    old = table.search_table
    first = (old + "_i" + "x" * MAX_IDENTIFIER_LENGTH)[:MAX_IDENTIFIER_LENGTH]
    second = first[:-1] + "y"
    table.create_index(["n"], name=first)
    table.create_index(["label"], name=second)

    new = "r" + "e" * (MAX_SEARCH_TABLE_NAME_LENGTH - 1)
    db.rename_table(old, new)
    try:
        stored = sorted(db[new].list_indexes())
        assert len(stored) == 2
        assert len(set(stored)) == 2
        built = catalog_index_names(db, new)
        for name in stored:
            assert len(name.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
            assert name in built
    finally:
        db.drop_table(new, force=True)


##################################################################
# the transient index a staged write builds                      #
##################################################################


def test_a_staged_write_on_the_longest_name_uses_a_fitted_index(db, tmp_path):
    """
    <49 bytes>_tmp_staged_label is 66 bytes, so the transient index has to be
    fitted -- and by the same call in the path that creates it and the paths
    that drop it.
    """
    name = "s" + "t" * (MAX_SEARCH_TABLE_NAME_LENGTH - 1)
    db.create_table(name, [("n", "integer"), ("label", "text")], "label")
    try:
        table = db[name]
        table.insert_many([{"n": 1, "label": "one"}])
        expected = derived_identifier(name + "_tmp", "_staged_label")
        raw = name + "_tmp_staged_label"
        assert len(raw.encode("utf-8")) > MAX_IDENTIFIER_LENGTH
        assert len(expected.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH

        with db[name].staged() as staged:
            built = catalog_index_names(db, name + "_tmp")
            assert expected in built
            staged.update({"n": 1}, {"label": "changed"})

        # the transient index is gone, under either spelling
        relations = db._schema_relations()
        assert expected not in relations
        assert raw[:MAX_IDENTIFIER_LENGTH] not in relations
        assert name + "_tmp" not in relations
        assert db[name].lucky({"n": 1}, "label") == "changed"
    finally:
        for lifecycle in ("_old1", "_tmp"):
            for component in ("search", "counts", "stats"):
                db._execute(
                    SQL("DROP TABLE IF EXISTS {0}").format(
                        Identifier(physical_table_name(name, component, lifecycle))
                    )
                )
        db.drop_table(name, force=True)


def test_the_staged_label_index_is_in_the_generated_name_set():
    name = "t" * MAX_SEARCH_TABLE_NAME_LENGTH
    staged = derived_identifier(name + "_tmp", "_staged_label")
    assert len(staged.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH
    # and it is distinct from the relations of the same table
    others = {
        physical_table_name(name, component, lifecycle)
        for component in ("search", "counts", "stats")
        for lifecycle in ("", "_tmp", "_old1")
    }
    assert staged not in others


def test_a_table_rename_leaves_a_custom_index_name_alone(db, table_factory):
    """
    An index whose name does not mention the table keeps it -- and psycodict
    must not ask PostgreSQL to rename it to itself, which is an error rather
    than a no-op.  Its metadata still moves to the new table.
    """
    table = table_factory()
    old = table.search_table
    index = "custom_index_%s" % uuid.uuid4().hex[:8]
    table.create_index(["n"], name=index)
    new = "renamed_%s" % uuid.uuid4().hex[:8]

    db.rename_table(old, new)
    try:
        assert list(db[new].list_indexes()) == [index]
        assert index in catalog_index_names(db, new)
        # and the metadata followed the table
        rows = meta_index_rows(db, new)
        assert [row["index_name"] for row in rows] == [index]
        assert meta_index_rows(db, old) == []
        db[new].drop_index(index, permanent=False)
        db[new].restore_index(index)
        assert index in catalog_index_names(db, new)
    finally:
        db.drop_table(new, force=True)


def test_a_table_rename_leaves_a_custom_constraint_name_alone(db, table_factory):
    table = table_factory()
    old = table.search_table
    constraint = "custom_constraint_%s" % uuid.uuid4().hex[:8]
    table.create_constraint(["label"], "unique", name=constraint)
    new = "renamed_%s" % uuid.uuid4().hex[:8]

    db.rename_table(old, new)
    try:
        assert list(db[new].list_constraints()) == [constraint]
        assert constraint in catalog_constraint_names(db, new)
        db[new].drop_constraint(constraint)
        db[new].restore_constraint(constraint)
        assert constraint in catalog_constraint_names(db, new)
    finally:
        db.drop_table(new, force=True)


def test_a_rename_target_taken_by_an_unrelated_index_is_moved_aside(db, table_factory):
    """
    The name a renamed index would take may already belong to an index on some
    other table.  An index is a pg_class relation, not a table, so a planner
    that only looked at pg_tables would not see it.
    """
    table = table_factory()
    old = table.search_table
    new = "renamed_%s" % uuid.uuid4().hex[:8]
    table.create_index(["n"])
    source_index = old + "_n"
    would_take = source_index.replace(old, new)

    # an unrelated table already has an index by that name
    other = table_factory()
    other.create_index(["n"], name=would_take)

    db.rename_table(old, new)
    try:
        stored = list(db[new].list_indexes())
        assert len(stored) == 1
        assert stored[0] != would_take
        assert stored[0] in catalog_index_names(db, new)
        # the unrelated index is where it was
        assert would_take in catalog_index_names(db, other.search_table)
        db[new].drop_index(stored[0], permanent=False)
        db[new].restore_index(stored[0])
        assert stored[0] in catalog_index_names(db, new)
    finally:
        db.drop_table(new, force=True)


def test_planned_rename_targets_are_shared_across_kinds(db, table_factory, monkeypatch):
    """
    An index target and a constraint target are allocated from one set: two
    dependents must not be given the same name merely because they are of
    different kinds.
    """
    table = table_factory()
    old = table.search_table
    table.create_index(["n"], name=old + "_shared")
    table.create_constraint(["label"], "unique", name=old + "_shared_c")
    new = "renamed_%s" % uuid.uuid4().hex[:8]

    # make both candidates collapse to the same name, which is what the shared
    # set has to resolve
    import psycodict.database as database_module

    real = database_module.derived_identifier
    monkeypatch.setattr(
        database_module,
        "derived_identifier",
        lambda base, suffix="": real(new + "_collide", suffix),
    )
    plan = db._dependent_rename_plan(old, new)
    targets = [entry["target"] for entries in plan.values() for entry in entries.values()]
    assert len(targets) == 2
    assert len(set(targets)) == 2


def test_renaming_one_table_leaves_another_tables_metadata_alone(
    db, table_factory, check_function, tmp_path
):
    """
    A constraint name is only unique within its table, so two tables can share
    one.  Updating metadata by the constraint's name alone would move the other
    table's rows -- current and historical.
    """
    first, second = table_factory(), table_factory()
    shared = "shared_check_%s" % uuid.uuid4().hex[:8]
    for table in (first, second):
        table.create_constraint(["n"], "check", name=shared, check_func=check_function)
        path = str(tmp_path / ("%s.txt" % table.search_table))
        table.copy_to_constraints(path)
        table.reload_constraints(path)          # gives each table history

    def rows(table_name, meta):
        cur = db._execute(
            SQL("SELECT constraint_name FROM {0} WHERE table_name = %s").format(
                Identifier(meta)
            ),
            [table_name],
        )
        return sorted(rec[0] for rec in cur)

    old, other = first.search_table, second.search_table
    before_other = (rows(other, "meta_constraints"), rows(other, "meta_constraints_hist"))
    new = "renamed_%s" % uuid.uuid4().hex[:8]

    db.rename_table(old, new)
    try:
        # the renamed table's rows moved, current and historical
        assert rows(new, "meta_constraints") == [shared]
        assert rows(new, "meta_constraints_hist") == [shared]
        assert rows(old, "meta_constraints") == []
        assert rows(old, "meta_constraints_hist") == []
        # the other table's did not
        assert (rows(other, "meta_constraints"), rows(other, "meta_constraints_hist")) == before_other
        # and both constraints are still on their own tables
        assert shared in catalog_constraint_names(db, new)
        assert shared in catalog_constraint_names(db, other)
    finally:
        db[new].drop_constraint(shared, permanent=True)
        db.drop_table(new, force=True)


def test_a_check_constraint_keeping_its_name_is_reserved_first(
    db, table_factory, check_function
):
    """
    A CHECK constraint has no backing index, so pg_class has no row for its
    name -- only pg_constraint does.  Renaming a table with constraints
    ``<old>_guard`` and ``<new>_guard`` therefore has two dependents wanting
    the same name: the first is renamed into it, the second already has it.

    The one that keeps its name is allocated first, so the outcome does not
    depend on the order the metadata query happened to return, which has no
    ORDER BY.  (A UNIQUE constraint would not show this: it *is* visible in
    pg_class, through its index.)
    """
    table = table_factory()
    old = table.search_table
    new = "renamed_%s" % uuid.uuid4().hex[:8]
    keeps_name = new + "_guard"
    table.create_constraint(["n"], "check", name=old + "_guard", check_func=check_function)
    table.create_constraint(["n"], "check", name=keeps_name, check_func=check_function)

    plan = db._dependent_rename_plan(old, new)["constraint"]
    assert plan[keeps_name]["target"] == keeps_name
    assert plan[keeps_name]["rename_physical"] is False
    # the one that would have taken that name is moved aside
    assert plan[old + "_guard"]["target"] != keeps_name
    targets = [entry["target"] for entry in plan.values()]
    assert len(set(targets)) == len(targets)

    db.rename_table(old, new)
    try:
        stored = sorted(db[new].list_constraints())
        assert len(set(stored)) == 2
        built = catalog_constraint_names(db, new)
        for name in stored:
            assert name in built
        assert keeps_name in stored
    finally:
        for name in list(db[new].list_constraints()):
            db[new].drop_constraint(name, permanent=True)
        db.drop_table(new, force=True)


def test_an_unmanaged_constraint_name_is_not_handed_out(db, table_factory, check_function):
    """
    Reserving from pg_constraint rather than from meta_constraints also covers
    a constraint psycodict did not create.
    """
    table = table_factory()
    old = table.search_table
    new = "renamed_%s" % uuid.uuid4().hex[:8]
    table.create_constraint(["n"], "check", name=old + "_guard", check_func=check_function)
    # added behind psycodict's back, so it is in no metadata
    db._execute(
        SQL("ALTER TABLE {0} ADD CONSTRAINT {1} CHECK (n IS NOT NULL)").format(
            Identifier(old), Identifier(new + "_guard")
        )
    )
    plan = db._dependent_rename_plan(old, new)["constraint"]
    assert plan[old + "_guard"]["target"] != new + "_guard"


def test_a_history_only_name_that_keeps_itself_is_reserved_too(db, table_factory, tmp_path):
    """
    A name only the history remembers has no relation behind it, so nothing in
    the catalog reserves it.  If it keeps its name -- it does not mention the
    table -- a current dependent whose new name lands on it would take it, and
    the two metadata rows would end up calling different indexes the same
    thing.  The dependents that keep their names are allocated first for
    exactly this reason.
    """
    table = table_factory()
    old = table.search_table
    new = "renamed_%s" % uuid.uuid4().hex[:8]
    survivor = new + "_n"

    # a name that lives only in the history: created, recorded, then dropped
    table.create_index(["n"], name=survivor)
    path = str(tmp_path / "indexes.txt")
    table.copy_to_indexes(path)
    table.reload_indexes(path)
    table.drop_index(survivor, permanent=True)
    # and a current index whose renamed name would be exactly that
    table.create_index(["n"])
    assert derived_identifier((old + "_n").replace(old, new)) == survivor

    plan = db._dependent_rename_plan(old, new)["index"]
    assert plan[survivor]["target"] == survivor
    assert plan[survivor]["rename_physical"] is False
    assert plan[old + "_n"]["target"] != survivor
    targets = [entry["target"] for entry in plan.values()]
    assert len(set(targets)) == len(targets)


def test_a_history_only_constraint_reserves_its_name_against_an_index(
    db, table_factory, check_function, tmp_path
):
    """
    The reservation has to be global, not per kind.

    A CHECK constraint that only the history remembers has no row in
    pg_constraint and none in pg_class, so nothing in the catalog holds its
    name.  If indexes were allocated before constraints were even read, a
    current index's new name would take it -- and restoring that constraint
    later would make _rename_if_exists move the index aside, leaving the
    index's metadata naming a relation that is no longer there.
    """
    table = table_factory()
    old = table.search_table
    new = "renamed_%s" % uuid.uuid4().hex[:8]
    survivor = new + "_n"

    # a constraint that lives only in the history
    table.create_constraint(["n"], "check", name=survivor, check_func=check_function)
    path = str(tmp_path / "constraints.txt")
    table.copy_to_constraints(path)
    table.reload_constraints(path)
    table.drop_constraint(survivor, permanent=True)
    assert survivor not in catalog_constraint_names(db, old)

    # and a current index whose renamed name would be exactly that
    table.create_index(["n"])
    assert derived_identifier((old + "_n").replace(old, new)) == survivor

    plan = db._dependent_rename_plan(old, new)
    assert plan["constraint"][survivor]["target"] == survivor
    assert plan["constraint"][survivor]["rename_physical"] is False
    assert plan["index"][old + "_n"]["target"] != survivor
    targets = [entry["target"] for entries in plan.values() for entry in entries.values()]
    assert len(set(targets)) == len(targets)

    db.rename_table(old, new)
    try:
        index = list(db[new].list_indexes())[0]
        assert index != survivor
        assert index in catalog_index_names(db, new)
        # the history kept the constraint's name, on the new table
        history = {rec[0] for rec in db._execute(
            SQL("SELECT constraint_name FROM meta_constraints_hist WHERE table_name = %s"),
            [new],
        )}
        assert history == {survivor}
    finally:
        db.drop_table(new, force=True)


def test_two_keepers_of_different_kinds_may_share_a_name(db, table_factory, check_function):
    """
    PostgreSQL keeps index names and constraint names in different catalogs, so
    an index and a CHECK constraint on one table may be called the same thing.
    psycodict will not create that itself -- create_constraint refuses a name a
    relation already has -- but a database can arrive in that state from
    outside, and a rename that moves neither of them must not object.
    """
    table = table_factory()
    old = table.search_table
    new = "renamed_%s" % uuid.uuid4().hex[:8]
    shared = "shared_%s" % uuid.uuid4().hex[:8]
    table.create_index(["n"], name=shared)
    # added the way an older psycodict or an administrator would have
    db._execute(
        SQL("ALTER TABLE {0} ADD CONSTRAINT {1} CHECK ({2}({3}))").format(
            Identifier(old), Identifier(shared),
            Identifier(*check_function.split(".")), Identifier("n"),
        )
    )
    db._execute(
        SQL("INSERT INTO meta_constraints (constraint_name, table_name, type, "
            "columns, check_func) VALUES (%s, %s, %s, %s, %s)"),
        [shared, old, "CHECK", Json(["n"]), check_function],
    )

    plan = db._dependent_rename_plan(old, new)
    assert plan["index"][shared]["target"] == shared
    assert plan["constraint"][shared]["target"] == shared

    db.rename_table(old, new)
    try:
        assert shared in catalog_index_names(db, new)
        assert shared in catalog_constraint_names(db, new)
    finally:
        db._execute(
            SQL("DELETE FROM meta_constraints WHERE constraint_name = %s"), [shared]
        )
        db.drop_table(new, force=True)
