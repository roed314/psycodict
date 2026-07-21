# -*- coding: utf-8 -*-
"""
Tests for ``db.compare`` and ``db.show_differences`` (psycodict.dbdiff).

Comparing two databases needs two databases: alongside the main test
database (see conftest), this module uses a sibling whose name is the main
name with ``b`` appended (``psycodict_testb`` by default), reached with the
same host, port and credentials.  The sibling only needs to exist --
psycodict bootstraps its metadata tables on connection -- so

    createdb psycodict_testb

is enough to enable these tests.  If it is unreachable the module is
skipped, mirroring how conftest skips the whole suite without a server.

The tests plant a known set of differences between the two databases (a
table on each side only, a shared table with column, type, row-count and
null-count drift, a shared table whose columns differ only in a type
modifier, a shared table whose meta_tables settings disagree, and a shared
table with no differences at all) and check that every section of the
comparison reports exactly what was planted.
"""
import os
import uuid

import pytest

from psycopg.sql import SQL, Identifier


def _second_connection_kwargs():
    # The same environment variables used in conftest, with "b" appended to
    # the database name.
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", 5432)),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "dbname": os.environ.get("PGDATABASE", "psycodict_test") + "b",
    }


@pytest.fixture(scope="module")
def db2(tmp_path_factory):
    """
    A ``PostgresDatabase`` connected to the second test database through a
    second ``Configuration``, as a caller comparing beta with production
    would build one.  Skips the module if the database is missing.
    """
    import psycopg

    from psycodict.config import Configuration
    from psycodict.database import PostgresDatabase

    conn = _second_connection_kwargs()
    tmp = tmp_path_factory.mktemp("dbdiff")
    config_file = tmp / "config.ini"
    with open(config_file, "w") as F:
        F.write("[logging]\nslowcutoff = 0.1\nslowlogfile = %s\n" % (tmp / "slow_queries.log"))
        F.write("[postgresql]\n")
        for key, val in conn.items():
            F.write("%s = %s\n" % (key, val))
    config = Configuration(
        defaults={"config_file": str(config_file), "secrets_file": str(tmp / "secrets.ini")},
        readargs=False,
    )
    try:
        database = PostgresDatabase(config=config, create=True)
    except psycopg.OperationalError as err:
        pytest.skip(
            "no second test database at %s:%s/%s as %s (%s); create it with createdb "
            "to run the database comparison tests" % (
                conn["host"], conn["port"], conn["dbname"], conn["user"],
                str(err).strip().split("\n")[0],
            ),
            allow_module_level=True,
        )
    yield database
    database.conn.close()


@pytest.fixture(scope="module")
def scenario(db, db2):
    """
    Tables planted in the two databases, with every kind of difference that
    ``compare`` reports.  Yields a dictionary of the table names:

    - ``ident`` -- in both databases, identical schema and rows
    - ``drift`` -- in both databases, with a column on each side only, a
      column typed integer here and bigint there, 3 rows here and 5 there,
      and 1 null label here and 3 there
    - ``modif`` -- in both databases with the same rows, but a column typed
      varchar(16) here and varchar(64) there (the same base type, differing
      only in the modifier) alongside a column typed varchar(32) on both
      sides
    - ``meta`` -- in both databases with the same columns and rows, but
      label_col label vs None and sort ["n"] vs ["label"]
    - ``only1`` / ``only2`` -- in only the first / second database
    - ``all`` -- the six names above, for restricting comparisons to this
      scenario
    """
    tag = uuid.uuid4().hex[:12]
    names = {
        key: "test_diff_%s_%s" % (key, tag)
        for key in ["ident", "drift", "modif", "meta", "only1", "only2"]
    }
    names["all"] = sorted(names.values())
    base_cols = [("n", "integer"), ("label", "text")]
    created = []

    def create(database, name, columns, label_col, sort, rows):
        database.create_table(name, columns, label_col, sort=sort)
        created.append((database, name))
        # insert_many records the assigned ids in the given dictionaries, so
        # hand it copies: ident and meta reuse one list for both databases
        database[name].insert_many([dict(row) for row in rows])

    try:
        ident_rows = [{"n": i, "label": "a%d" % i} for i in range(3)]
        create(db, names["ident"], base_cols, "label", ["n"], ident_rows)
        create(db2, names["ident"], base_cols, "label", ["n"], ident_rows)
        create(
            db, names["drift"],
            base_cols + [("only_left", "smallint"), ("x", "integer")],
            "label", ["n"],
            [
                {"n": i, "label": lab, "only_left": i, "x": 10 * i}
                for i, lab in enumerate(["a", None, "b"])
            ],
        )
        create(
            db2, names["drift"],
            base_cols + [("only_right", "text"), ("x", "bigint")],
            "label", ["n"],
            [
                {"n": i, "label": lab, "only_right": "r%d" % i, "x": 10 * i}
                for i, lab in enumerate([None, None, None, "a", "b"])
            ],
        )
        # create_table only hands out unmodified types, so the modifier
        # drift is planted with raw DDL after identical creation: v differs
        # only in its varchar length and must be reported, while w carries
        # the same modifier on both sides and must not be
        modif_rows = [{"n": i, "label": "v%d" % i} for i in range(2)]
        create(db, names["modif"], base_cols, "label", ["n"], modif_rows)
        create(db2, names["modif"], base_cols, "label", ["n"], modif_rows)
        for database, size in [(db, 16), (db2, 64)]:
            database._execute(SQL(
                "ALTER TABLE {0} ADD COLUMN v varchar(%d), ADD COLUMN w varchar(32)" % size
            ).format(Identifier(names["modif"])))
        meta_rows = [{"n": i, "label": "m%d" % i} for i in range(2)]
        create(db, names["meta"], base_cols, "label", ["n"], meta_rows)
        create(db2, names["meta"], base_cols, None, ["label"], meta_rows)
        create(db, names["only1"], [("n", "integer")], None, ["n"], [{"n": 1}])
        create(db2, names["only2"], [("n", "integer")], None, ["n"], [{"n": 1}])
        yield names
    finally:
        for database, name in reversed(created):
            try:
                database.drop_table(name, force=True)
            except Exception:  # pragma: no cover - cleanup must not mask failures
                pass


def test_tables_only_on_one_side(db, db2, scenario):
    diff = db.compare(db2, tables=scenario["all"])
    assert diff["only_in_self"] == [scenario["only1"]]
    assert diff["only_in_other"] == [scenario["only2"]]


def test_schema_section(db, db2, scenario):
    schema = db.compare(db2, tables=scenario["all"])["schema"]
    # Identical tables and tables on one side only are not reported
    assert scenario["ident"] not in schema
    assert scenario["only1"] not in schema
    assert scenario["only2"] not in schema
    assert schema[scenario["drift"]] == {
        "only_in_self": [("only_left", "smallint")],
        "only_in_other": [("only_right", "text")],
        "type_changed": [("x", "integer", "bigint")],
    }
    # Modifier-only drift is reported with the full rendered types; the
    # exact equality also checks that w, whose varchar(32) modifier agrees
    # on both sides, is not reported
    assert schema[scenario["modif"]] == {
        "type_changed": [("v", "character varying(16)", "character varying(64)")],
    }
    assert schema[scenario["meta"]] == {
        "meta_changed": [("label_col", "label", None), ("sort", ["n"], ["label"])],
    }


def test_row_counts(db, db2, scenario):
    # The default (cheap) counts come from the totals cached in meta_tables,
    # which the insert_many calls in the scenario fixture maintained
    diff = db.compare(db2, tables=scenario["all"])
    assert diff["row_counts"] == {scenario["drift"]: (3, 5)}
    # and the exact counts agree with them here
    diff = db.compare(db2, tables=scenario["all"], exact=True)
    assert diff["row_counts"] == {scenario["drift"]: (3, 5)}


def test_row_counts_can_be_disabled(db, db2, scenario):
    diff = db.compare(db2, tables=scenario["all"], row_counts=False)
    assert "row_counts" not in diff


def test_null_counts(db, db2, scenario):
    # Off by default: a full scan of every compared table on both sides
    # should only happen on request
    assert "null_counts" not in db.compare(db2, tables=scenario["all"])
    diff = db.compare(db2, tables=scenario["all"], null_counts=True)
    # Only the drifted table differs, and only in the label column: n and x
    # have no nulls on either side, and the one-sided columns are not shared
    assert diff["null_counts"] == {scenario["drift"]: {"label": (1, 3)}}


def test_identical_table_reports_nothing(db, db2, scenario, capsys):
    # tables may be passed as a single name; every section comes back empty
    diff = db.compare(db2, tables=scenario["ident"], null_counts=True)
    assert diff == {
        "only_in_self": [],
        "only_in_other": [],
        "schema": {},
        "row_counts": {},
        "null_counts": {},
    }
    db.show_differences(db2, tables=scenario["ident"], null_counts=True)
    assert "No differences found" in capsys.readouterr().out


def test_compare_with_itself_is_clean(db):
    # The full tables=None path: compared with itself, a database shows no
    # differences no matter what tables it holds
    diff = db.compare(db, null_counts=True, exact=True)
    assert diff == {
        "only_in_self": [],
        "only_in_other": [],
        "schema": {},
        "row_counts": {},
        "null_counts": {},
    }


def test_unknown_table_raises(db, db2, scenario):
    with pytest.raises(ValueError, match="not a search table"):
        db.compare(db2, tables="this_table_exists_nowhere")


def test_show_differences_report(db, db2, scenario, capsys):
    db.show_differences(db2, tables=scenario["all"], null_counts=True)
    out = capsys.readouterr().out
    assert "No differences found" not in out
    # Sides are labelled with their connection details
    assert "Tables only in self (" in out
    assert "Tables only in other (" in out
    assert scenario["only1"] in out
    assert scenario["only2"] in out
    # Schema section: one-sided columns, type changes, meta changes
    assert "Schema differences:" in out
    assert "only_left (smallint)" in out
    assert "only_right (text)" in out
    assert "type of x changed: integer vs bigint" in out
    assert "type of v changed: character varying(16) vs character varying(64)" in out
    assert "label_col changed: label vs None" in out
    assert "sort changed: ['n'] vs ['label']" in out
    # Row and null count sections
    assert "Row count differences:" in out
    assert "%s: 3 vs 5" % scenario["drift"] in out
    assert "Null count differences:" in out
    assert "%s.label: 1 vs 3" % scenario["drift"] in out


def test_sections_omitted_when_empty(db, db2, scenario, capsys):
    # Restricted to the metadata-drifted table there are no missing tables,
    # no row count differences and no null count differences, so only the
    # schema section appears
    db.show_differences(db2, tables=scenario["meta"], null_counts=True)
    out = capsys.readouterr().out
    assert "Schema differences:" in out
    assert "Tables only in" not in out
    assert "Row count differences:" not in out
    assert "Null count differences:" not in out
