# -*- coding: utf-8 -*-
"""
Tests for psycodict.slowlog: parsing, normalizing and reporting on the
slow-query logs written by PostgresBase._execute.

Rather than fabricating log lines to match what we believe the logging code
emits, most tests here generate a real log: a Configuration with
``slowcutoff = 0`` makes every query slow, so running ordinary searches
against a small table produces a genuine slow-query log to analyze.  One
test does use a synthetic file, to cover formats from older versions of the
code (no ANSI colors) that the current code no longer writes.
"""
import os
import uuid

import pytest

from psycodict.slowlog import (
    normalize_query,
    parse_slow_log,
    show_slow_report,
    slow_query_report,
)

COLUMNS = [
    ("n", "integer"),
    ("label", "text"),
    ("data", "jsonb"),
    ("vec", "integer[]"),
    ("x", "double precision"),
]


@pytest.fixture
def slow_setup(db, tmp_path):
    """
    A (database, table, logfile) triple where every query gets logged.

    The ``db`` fixture is requested only for its server-reachability check
    (and the initial meta-table bootstrap); the database built here has its
    own Configuration, with ``slowcutoff = 0`` and ``slowlogfile`` pointing
    into ``tmp_path``, so that the queries run by a test are written to a
    log file the test can then analyze.  The log accumulated during setup
    is discarded so that each test sees only its own queries.
    """
    from psycodict.config import Configuration
    from psycodict.database import PostgresDatabase

    logfile = str(tmp_path / "slow_queries.log")
    config_file = str(tmp_path / "config.ini")
    with open(config_file, "w") as F:
        F.write("[logging]\nslowcutoff = 0.0\nslowlogfile = %s\n" % logfile)
        F.write("[postgresql]\n")
        F.write("host = %s\n" % os.environ.get("PGHOST", "localhost"))
        F.write("port = %s\n" % os.environ.get("PGPORT", 5432))
        F.write("user = %s\n" % os.environ.get("PGUSER", "postgres"))
        F.write("password = %s\n" % os.environ.get("PGPASSWORD", ""))
        F.write("dbname = %s\n" % os.environ.get("PGDATABASE", "psycodict_test"))
    config = Configuration(
        defaults={"config_file": config_file, "secrets_file": str(tmp_path / "secrets.ini")},
        readargs=False,
    )
    database = PostgresDatabase(config=config)
    name = "test_slow_%s" % uuid.uuid4().hex[:12]
    database.create_table(name, COLUMNS, label_col="label", sort=["n"])
    table = database[name]
    table.insert_many([
        {
            "n": i,
            "label": "l%d" % i,
            "data": {"a": [i, 2 * i], "s": "v%d" % (i % 7)},
            "vec": [i, i + 1],
            "x": i * 0.5,
        }
        for i in range(60)
    ])
    # Discard the log lines from the setup queries.  The handler has the
    # file open in append mode, so truncating it out from under the logger
    # is safe: subsequent writes start over at the beginning.
    open(logfile, "w").close()
    yield database, table, logfile
    try:
        database.drop_table(name, force=True)
    finally:
        database.conn.close()


def test_parse_slow_log(slow_setup):
    database, table, logfile = slow_setup
    for i in (5, 7, 11):
        table.search({"n": i}, ["label"], limit=5)
    table.search({"n": {"$gte": 3, "$lte": 17}}, ["label", "x"], limit=5)
    table.count({"n": {"$gte": 10}})
    # inlined values containing a newline produce a multi-line log record
    table.search({"label": "a\nb'c"}, ["n"], limit=5)

    stats = {}
    records = list(parse_slow_log(logfile, stats=stats))
    assert records
    assert stats["unparsed"] == 0
    assert stats["lines"] >= len(records)
    for rec in records:
        assert 0 < rec["duration"] < 60
        assert rec["timestamp"] is not None
        assert rec["kind"] == "query"
        assert rec["query"]
        assert rec["lines"] >= 1
    # each of the five searches produces a "Replicate with db.<table>.analyze"
    # hint line, attached to the record it follows
    hints = [rec for rec in records if rec["replicate"]]
    assert len(hints) == 5
    for rec in hints:
        assert rec["table"] == table.search_table
        assert rec["replicate"].startswith("db.%s.analyze(" % table.search_table)
        assert rec["lines"] >= 2
    # the newline inside the quoted value was reassembled
    multi = [rec for rec in records if "\n" in rec["query"]]
    assert len(multi) == 1
    assert "a\nb''c" in multi[0]["query"]


def test_parse_old_and_mixed_formats(tmp_path):
    # A synthetic file: the current colored format, the pre-2019 format
    # without ANSI escapes, a search-iterator line, junk, an orphan hint
    # line, and a multi-line query.
    logfile = str(tmp_path / "synthetic.log")
    with open(logfile, "w") as F:
        F.write('2020-01-01 12:00:00,123 - SELECT "a" FROM "t" WHERE "n" = 5 ran in \x1b[91m 0.25s \x1b[0m\n')
        F.write("2020-01-01 12:00:00,124 - Replicate with db.t.analyze({'n': 5}, 1, 1000, 0)\n")
        F.write("2018-06-01 01:02:03,004 - SELECT label FROM old_table WHERE deg = 2 ran in 1.5s\n")
        F.write("some stray line that means nothing\n")
        F.write("2020-01-01 12:00:01,000 - Search iterator for t {'n': {'$gte': 5}} required a total of \x1b[91m2.5s\x1b[0m\n")
        F.write("2020-01-01 12:00:02,000 - Replicate with db.orphan.analyze({}, 1, 1000, 0)\n")
        F.write("2020-01-01 12:00:03,000 - SELECT \"a\" FROM \"t\" WHERE \"s\" = 'x\n")
        F.write("Y' ran in \x1b[91m 0.5s \x1b[0m\n")
        F.write('2020-01-01 12:00:04,000 - SELECT "z" FROM "t2" WHERE "m" = 3 ran in \x1b[91m 0.75s \x1b[0m\n')
        F.write("2020-01-01 12:00:04,001 - Replicate with db.elsewhere.analyze({'m': 3}, 1, 1000, 0)\n")

    stats = {}
    records = list(parse_slow_log(logfile, stats=stats))
    assert stats["lines"] == 10
    # unparsed: the stray line, the orphan hint following an iterator record,
    # and the hint naming a table that does not appear in the query before it
    # (as happens when two processes interleave their log lines)
    assert stats["unparsed"] == 3
    assert len(records) == 5

    first = records[0]
    assert first["kind"] == "query"
    assert first["duration"] == 0.25
    assert first["query"] == 'SELECT "a" FROM "t" WHERE "n" = 5'
    assert first["table"] == "t"
    assert first["replicate"] == "db.t.analyze({'n': 5}, 1, 1000, 0)"
    assert first["lines"] == 2
    assert first["timestamp"].year == 2020

    second = records[1]
    assert second["duration"] == 1.5
    assert second["query"] == "SELECT label FROM old_table WHERE deg = 2"
    assert second["table"] is None
    assert second["replicate"] is None

    third = records[2]
    assert third["kind"] == "iterator"
    assert third["table"] == "t"
    assert third["duration"] == 2.5
    assert third["query"] == "{'n': {'$gte': 5}}"

    fourth = records[3]
    assert fourth["duration"] == 0.5
    assert "'x\nY'" in fourth["query"]
    assert fourth["lines"] == 2

    fifth = records[4]
    assert fifth["duration"] == 0.75
    assert fifth["table"] is None
    assert fifth["replicate"] is None


def test_normalize_query():
    # quoted strings vanish, even when they contain digits or '' escapes
    assert (
        normalize_query("SELECT \"a\" FROM \"t\" WHERE \"s\" = 'ab''c 123' AND \"n\" = -5")
        == 'SELECT "a" FROM "t" WHERE "s" = ? AND "n" = ?'
    )
    # numbers in all their shapes
    assert (
        normalize_query('SELECT "a" FROM "t" WHERE "x" <= 2.5e-3 OR "x" > .5 LIMIT 100')
        == 'SELECT "a" FROM "t" WHERE "x" <= ? OR "x" > ? LIMIT ?'
    )
    # queries differing only in constants share a shape
    assert normalize_query('SELECT "a" FROM "t" WHERE "n" = 5 ORDER BY "n" LIMIT 4') == normalize_query(
        'SELECT "a" FROM "t" WHERE "n" = 1234 ORDER BY "n" LIMIT 50'
    )
    # ARRAY literals collapse regardless of length
    assert normalize_query('"vec" @> ARRAY[1, 2, 3]::integer[]') == '"vec" @> ARRAY[?]::integer[]'
    assert normalize_query('"vec" @> ARRAY[7]::integer[]') == '"vec" @> ARRAY[?]::integer[]'
    # the '{...}'::type[] rendering psycopg3 uses for arrays
    assert normalize_query("\"n\" = ANY('{1,2,3}'::int2[]::integer[])") == normalize_query(
        "\"n\" = ANY('{44,55}'::int2[]::integer[])"
    )
    # IN lists collapse regardless of length
    assert normalize_query("privilege_type IN ('INSERT','UPDATE','DELETE')") == normalize_query(
        "privilege_type IN ('SELECT')"
    )
    # a clause repeated by $in expansion on a jsonb column collapses
    assert normalize_query(
        '("data" = \'{"a": 1}\' OR "data" = \'{"a": 2}\' OR "data" = \'{"a": 3}\')'
    ) == normalize_query('("data" = \'{"a": 7}\')')
    # jsonb paths are part of the shape: the field stays, the value goes
    assert normalize_query("\"data\"->'s' = '\"v1\"'") == "\"data\"->'s' = ?"
    assert normalize_query("\"data\"->'s' = '\"v1\"'") != normalize_query("\"data\"->'t' = '\"v1\"'")
    # identifiers containing digits are not values
    assert (
        normalize_query('SELECT "dim1_factor" FROM "av_fq_isog" WHERE "dim1_factor" = 5')
        == 'SELECT "dim1_factor" FROM "av_fq_isog" WHERE "dim1_factor" = ?'
    )
    assert normalize_query("SELECT important FROM meta_tables WHERE name='test_5'") == (
        "SELECT important FROM meta_tables WHERE name=?"
    )
    # booleans are values; whitespace runs (and newlines) collapse
    assert normalize_query('SELECT count FROM "t_counts" WHERE split = false') == (
        'SELECT count FROM "t_counts" WHERE split = ?'
    )
    assert normalize_query('SELECT "a"\n    FROM "t"') == 'SELECT "a" FROM "t"'


def test_slow_query_report(slow_setup):
    database, table, logfile = slow_setup
    tname = table.search_table
    for i in (5, 7, 11):
        table.search({"n": i}, ["label"], limit=5)
    table.search({"n": {"$gte": 3, "$lte": 17}}, ["label", "x"], limit=5)
    table.count({"n": {"$gte": 10}})

    report = slow_query_report(logfile, top=50)
    records = list(parse_slow_log(logfile))
    assert report["records"] == len(records) > 0
    assert report["skipped"] == 0
    assert report["unparsed"] == 0

    # the three searches differing only in the constant share a shape
    main = [
        data for data in report["shapes"]
        if tname in data["tables"] and '"n" = ?' in data["shape"]
    ]
    assert len(main) == 1
    main = main[0]
    assert main["count"] == 3
    assert main["kind"] == "query"
    assert 0 < main["mean"] <= main["max"] <= main["total"]
    assert abs(main["total"] - main["count"] * main["mean"]) < 1e-9
    # the example is a real query, not the normalized shape
    assert main["example"] != main["shape"]
    assert 'FROM "%s"' % tname in main["example"]
    assert main["replicate"] is not None and main["replicate"].startswith("db.%s.analyze(" % tname)

    # shapes are sorted by total time and cover all parsed records
    totals = [data["total"] for data in report["shapes"]]
    assert totals == sorted(totals, reverse=True)
    assert sum(data["count"] for data in report["shapes"]) == report["records"]

    p = report["percentiles"]
    assert 0 < p["p50"] <= p["p90"] <= p["p99"] <= p["max"]
    assert p["max"] == max(rec["duration"] for rec in records)
    assert abs(report["total_time"] - sum(rec["duration"] for rec in records)) < 1e-9

    # threshold table: consistent with the records, monotone in the cutoff
    thresholds = report["thresholds"]
    assert thresholds
    cutoffs = [row["cutoff"] for row in thresholds]
    assert cutoffs == sorted(cutoffs)
    kept = [row["records"] for row in thresholds]
    assert kept == sorted(kept, reverse=True)
    total_lines = sum(rec["lines"] for rec in records)
    for row in thresholds:
        # the histogram buckets durations at 3 significant digits, so allow
        # for records sitting exactly at the cutoff
        low = sum(1 for rec in records if rec["duration"] >= row["cutoff"] * (1 + 1e-9))
        high = sum(1 for rec in records if rec["duration"] >= row["cutoff"] * (1 - 1e-9))
        assert low <= row["records"] <= high
        assert 0 <= row["lines"] <= total_lines
        assert 0 <= row["percent"] <= 100
        assert abs(row["percent"] - 100.0 * row["lines"] / total_lines) < 1e-9

    # a cutoff above every duration filters everything out
    report2 = slow_query_report(logfile, cutoff=2 * p["max"])
    assert report2["records"] == report["records"]
    assert report2["skipped"] == report2["records"]
    assert report2["shapes"] == []
    assert report2["thresholds"] == []


def test_index_suggestions(slow_setup):
    database, table, logfile = slow_setup
    tname = table.search_table
    for i in (2, 3):
        table.search({"n": i}, ["label"], limit=5)
    table.search({"data": {"$contains": {"s": "v1"}}}, ["label"], limit=5)
    table.search({"label": {"$like": "%3"}}, ["n"], limit=5)

    def all_suggestions(report):
        return [s for data in report["shapes"] for s in data["suggestions"]]

    report = slow_query_report(logfile, top=50, db=database)
    suggestions = all_suggestions(report)
    # "n" is unindexed, so the equality shape (and the ORDER BY on "n")
    # must produce a create_index suggestion naming it
    n_index = [s for s in suggestions if "db.%s.create_index(['n'])" % tname in s]
    assert n_index, suggestions
    assert any('"n"' in s for s in n_index)
    # the jsonb containment search suggests a GIN index
    gin = [s for s in suggestions if "type='gin'" in s and "'data'" in s]
    assert gin, suggestions
    # the leading-wildcard LIKE gets a (db-independent) note
    like = [s for s in suggestions if "wildcard" in s and '"label"' in s]
    assert like, suggestions

    # once the indexes exist, the corresponding suggestions disappear
    table.create_index(["n"])
    report2 = slow_query_report(logfile, top=50, db=database)
    assert not [s for s in all_suggestions(report2) if "create_index(['n'])" in s]
    table.create_index(["data"], type="gin")
    report3 = slow_query_report(logfile, top=50, db=database)
    assert not [s for s in all_suggestions(report3) if "type='gin'" in s and "'data'" in s]
    # the LIKE note does not depend on indexes
    assert [s for s in all_suggestions(report3) if "wildcard" in s]

    # without a database, the constrained columns are reported for manual checking
    report4 = slow_query_report(logfile, top=50)
    assert any("pass db=" in s and '"n"' in s for s in all_suggestions(report4))


def test_show_slow_report(slow_setup, capsys):
    database, table, logfile = slow_setup
    for i in (1, 2):
        table.search({"n": i}, ["label"], limit=5)
    show_slow_report(logfile, db=database)
    out = capsys.readouterr().out
    assert "parsed records" in out
    assert "query shapes by total time" in out
    assert "cutoff" in out
    assert table.search_table in out
    assert '"n" = ?' in out

    # and the delegating method on the database object
    database.show_slow_report(logfile, top=5)
    out = capsys.readouterr().out
    assert "query shapes by total time" in out


def test_show_slow_report_empty(tmp_path, capsys):
    logfile = str(tmp_path / "empty.log")
    open(logfile, "w").close()
    show_slow_report(logfile)
    out = capsys.readouterr().out
    assert "No slow queries" in out
