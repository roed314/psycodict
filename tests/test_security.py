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

from psycopg.sql import SQL, Identifier

from psycodict.validation import (
    InvalidColumnTypeError,
    InvalidDefinitionError,
    MAX_IDENTIFIER_LENGTH,
    check_new_table_name,
    derived_identifier,
    normalize_storage_param,
    physical_table_name,
    types_whitelist,
    validate_column_type,
    validate_index_predicate,
)
from psycodict.encoding import Json
from psycodict.table import PostgresTable

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
    End to end at the longest name psycodict will create: the _tmp copies, the
    swap and the _old1 backups all address the relations the helper names.
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
