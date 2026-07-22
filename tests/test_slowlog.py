# -*- coding: utf-8 -*-
"""
Tests for psycodict.slowlog: parsing, normalizing and reporting on the
slow-query logs written by PostgresBase._execute.

Rather than fabricating log lines to match what we believe the logging code
emits, most tests here generate a real log: a Configuration with
``slowcutoff = 0`` makes every query slow, so running ordinary searches
against a small table produces a genuine slow-query log to analyze.  Some
tests do use synthetic files, to cover formats from older versions of the
code (no ANSI colors) that the current code no longer writes, and to pin
down grouping, retention and suggestion behavior on exactly known input.
"""
import os
import uuid

import pytest

from psycodict.slowlog import (
    normalize_dict_query,
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
    # numeric jsonb path components are structure too, just like string ones
    assert normalize_query("\"data\"->0 = '\"v1\"'") == '"data"->0 = ?'
    assert normalize_query('"data"->>2 > 7') == '"data"->>2 > ?'
    assert normalize_query('"data"->-1 = 3') == '"data"->-1 = ?'
    assert normalize_query('SELECT "data"->0 FROM "t" WHERE "n" = 5') != normalize_query(
        'SELECT "data"->1 FROM "t" WHERE "n" = 5'
    )
    # while numbers used as values still collapse
    assert normalize_query('SELECT "data"->0 FROM "t" WHERE "n" = 5') == normalize_query(
        'SELECT "data"->0 FROM "t" WHERE "n" = 99'
    )
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


def test_normalize_dict_query():
    # dictionary keys and $-operators are structure and survive; values go
    assert normalize_dict_query("{'n': {'$gte': 5}}") == "{'n': {'$gte': ?}}"
    # different keys or operators give different shapes...
    assert normalize_dict_query("{'n': {'$gte': 5}}") != normalize_dict_query("{'label': {'$lte': 'z'}}")
    assert normalize_dict_query("{'n': {'$gte': 5}}") != normalize_dict_query("{'n': {'$lte': 5}}")
    # ...while values still collapse, whatever their type
    assert normalize_dict_query("{'n': {'$gte': 5}}") == normalize_dict_query("{'n': {'$gte': 1234}}")
    assert normalize_dict_query("{'label': 'abc'}") == normalize_dict_query("{'label': 'z'}") == "{'label': ?}"
    # a double-quoted value (the repr of a string containing ') is a value
    assert normalize_dict_query('{\'label\': "o\'brien"}') == "{'label': ?}"
    # $in lists collapse regardless of length
    assert normalize_dict_query("{'n': {'$in': [1, 2, 3]}}") == normalize_dict_query("{'n': {'$in': [7]}}")
    # True/False/None are values; numeric keys are structure
    assert normalize_dict_query("{'n': {'$exists': True}}") == "{'n': {'$exists': ?}}"
    assert normalize_dict_query("{'x': None}") == "{'x': ?}"
    assert normalize_dict_query("{1: 'x'}") == "{1: ?}"
    assert normalize_dict_query("{}") == "{}"


def test_iterator_shapes_report(tmp_path):
    # Search-iterator records carry Python dict reprs, whose keys are
    # structure: queries on different columns (or with different operators)
    # must not collapse to a single shape, while different values must.
    logfile = str(tmp_path / "iterators.log")
    with open(logfile, "w") as F:
        F.write("2020-01-01 12:00:00,000 - Search iterator for t {'n': {'$gte': 5}} required a total of 2.5s\n")
        F.write("2020-01-01 12:00:01,000 - Search iterator for t {'n': {'$gte': 8}} required a total of 1.5s\n")
        F.write("2020-01-01 12:00:02,000 - Search iterator for t {'label': {'$lte': 'z'}} required a total of 1.0s\n")
        F.write("2020-01-01 12:00:03,000 - Search iterator for t {'n': {'$lte': 5}} required a total of 0.5s\n")
    report = slow_query_report(logfile, top=10)
    assert report["records"] == 4 and report["unparsed"] == 0
    shapes = {data["shape"]: data for data in report["shapes"]}
    assert set(shapes) == {
        "Search iterator for t {'n': {'$gte': ?}}",
        "Search iterator for t {'label': {'$lte': ?}}",
        "Search iterator for t {'n': {'$lte': ?}}",
    }
    gte = shapes["Search iterator for t {'n': {'$gte': ?}}"]
    assert gte["kind"] == "iterator"
    assert gte["count"] == 2
    assert abs(gte["total"] - 4.0) < 1e-9
    # the example is the slowest raw dict of that shape
    assert gte["example"] == "{'n': {'$gte': 5}}"
    assert gte["tables"] == ["t"]


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


def test_join_suggestions_target_correct_table(slow_setup, tmp_path):
    # Two joined tables sharing a column name: a qualified constraint
    # ("tbl"."col") must be resolved against exactly that table, and an
    # unqualified one against every referenced table having the column,
    # naming each candidate rather than silently picking the first.
    database, table, logfile = slow_setup
    ja = table.search_table + "_ja"
    jb = table.search_table + "_jb"
    database.create_table(ja, COLUMNS, label_col="label", sort=["n"])
    database.create_table(jb, COLUMNS, label_col="label", sort=["n"])
    try:
        # ja has an index leading with "n"; jb does not
        database[ja].create_index(["n"])
        joinlog = str(tmp_path / "join.log")
        frm = 'FROM "%s" JOIN "%s" ON "%s"."id" = "%s"."id"' % (ja, jb, ja, jb)
        with open(joinlog, "w") as F:
            F.write('2020-01-01 12:00:00,000 - SELECT "%s"."label" %s WHERE "%s"."n" = 5 ran in 1.5s\n'
                    % (ja, frm, jb))
            F.write('2020-01-01 12:00:01,000 - SELECT "%s"."label" %s WHERE "%s"."n" = 7 ran in 1.25s\n'
                    % (ja, frm, ja))
            F.write('2020-01-01 12:00:02,000 - SELECT "%s"."label" %s WHERE "n" = 9 ran in 1.0s\n'
                    % (ja, frm))
        report = slow_query_report(joinlog, top=50, db=database)
        assert report["records"] == 3 and report["unparsed"] == 0
        assert len(report["shapes"]) == 3
        by_where = {}
        for data in report["shapes"]:
            assert data["tables"] == sorted([ja, jb])
            by_where[data["shape"].split("WHERE ")[1]] = data["suggestions"]

        # constraint qualified with the unindexed table: the suggestion names
        # that table, not the (indexed) one that happens to sort first
        qualified_jb = by_where['"%s"."n" = ?' % jb]
        assert any("db.%s.create_index(['n'])" % jb in s for s in qualified_jb), qualified_jb
        assert not any("db.%s.create_index" % ja in s for s in qualified_jb), qualified_jb

        # constraint qualified with the indexed table: no suggestion at all
        # (in particular none pointing at the other, unindexed table)
        qualified_ja = by_where['"%s"."n" = ?' % ja]
        assert not any("create_index(['n'])" in s for s in qualified_ja), qualified_ja

        # unqualified constraint on a column both tables have: the ambiguity
        # is stated explicitly, with both candidates named
        unqualified = by_where['"n" = ?']
        ambiguous = [s for s in unqualified if "create_index(['n'])" in s]
        assert len(ambiguous) == 1, unqualified
        assert ja in ambiguous[0] and jb in ambiguous[0]
        assert "several of the referenced tables" in ambiguous[0]
        assert "db.%s.create_index(['n'])" % jb in ambiguous[0]
    finally:
        database.drop_table(ja, force=True)
        database.drop_table(jb, force=True)


def test_top_bounds_example_retention(tmp_path):
    # More distinct shapes than the example-retention candidate set (4*top)
    # holds: the numeric aggregates must still be exact for every shape,
    # and every reported shape must come with an example.  Shape "aaa" is
    # admitted, evicted by a flood of heavier shapes, and climbs back: its
    # example is then the record that refilled it (not its globally
    # slowest), while count/total/max stay exact.
    logfile = str(tmp_path / "many_shapes.log")
    with open(logfile, "w") as F:
        def q(tbl, nval, dur):
            F.write('2020-01-01 12:00:00,000 - SELECT "x" FROM "%s" WHERE "n" = %d ran in %ss\n'
                    % (tbl, nval, dur))
        q("aaa", 1, "5.0")
        for i in range(15):
            q("t%02d" % i, 1, "6.0")
        q("bbb", 1, "7.0")
        q("aaa", 2, "4.0")

    report = slow_query_report(logfile, top=2)  # candidate set of 8 < 17 shapes
    assert report["records"] == 18 and report["unparsed"] == 0
    assert abs(report["total_time"] - 106.0) < 1e-9
    assert len(report["shapes"]) == 2
    a, b = report["shapes"]
    assert a["shape"] == 'SELECT "x" FROM "aaa" WHERE "n" = ?'
    assert (a["count"], a["max"]) == (2, 5.0)
    assert abs(a["total"] - 9.0) < 1e-9 and abs(a["mean"] - 4.5) < 1e-9
    # the example was refilled after eviction by the (faster) later record
    assert a["example"] == 'SELECT "x" FROM "aaa" WHERE "n" = 2'
    assert b["shape"] == 'SELECT "x" FROM "bbb" WHERE "n" = ?'
    assert (b["count"], b["max"]) == (1, 7.0)
    assert b["example"] == 'SELECT "x" FROM "bbb" WHERE "n" = 1'
    for data in report["shapes"]:
        assert data["example"] is not None
        assert "_exdur" not in data and "_seq" not in data

    # a run whose candidate set covers every shape agrees on the aggregates
    full = slow_query_report(logfile, top=50)
    assert len(full["shapes"]) == 17 > 4 * 2
    assert sum(data["count"] for data in full["shapes"]) == 18
    fa = [d for d in full["shapes"] if d["shape"] == a["shape"]][0]
    assert (fa["count"], fa["total"], fa["max"]) == (a["count"], a["total"], a["max"])
    # with an unbounded candidate set the example is the globally slowest
    assert fa["example"] == 'SELECT "x" FROM "aaa" WHERE "n" = 1'


def test_shapes_by_mean_ranking(tmp_path):
    # Mean and total time can disagree: a rare very-slow shape has the higher
    # mean, a frequent moderately-slow shape the higher total.  shapes ranks by
    # total, shapes_by_mean by mean; both list the same (shared) dicts.
    logfile = str(tmp_path / "means.log")
    with open(logfile, "w") as F:
        def q(tbl, nval, dur):
            F.write('2020-01-01 12:00:00,000 - SELECT "x" FROM "%s" WHERE "n" = %d ran in %ss\n'
                    % (tbl, nval, dur))
        q("rare", 1, "10.0")            # total 10, mean 10
        for i in range(5):
            q("common", i, "3.0")       # total 15, mean 3

    report = slow_query_report(logfile, top=10)
    by_total = report["shapes"]
    by_mean = report["shapes_by_mean"]
    # by total the frequent shape leads; by mean the rare slow one does
    assert [d["tables"][0] for d in by_total] == ["common", "rare"]
    assert [d["tables"][0] for d in by_mean] == ["rare", "common"]
    assert [d["total"] for d in by_total] == sorted((d["total"] for d in by_total), reverse=True)
    assert [d["mean"] for d in by_mean] == sorted((d["mean"] for d in by_mean), reverse=True)
    # the two rankings reorder the very same dictionaries, each fully finalized
    assert {id(d) for d in by_mean} == {id(d) for d in by_total}
    for d in by_mean:
        assert isinstance(d["tables"], list) and "mean" in d and "suggestions" in d
        assert "_exdur" not in d and "_seq" not in d


def test_mean_leader_keeps_example_via_max_retention(tmp_path):
    # A rare, very slow shape tops the mean ranking but not the total ranking,
    # so the total-time retention pool would drop its example.  The max-time
    # pool keeps it, so the by-mean entry is still reproducible.
    logfile = str(tmp_path / "rareslow.log")
    with open(logfile, "w") as F:
        def q(tbl, nval, dur):
            F.write('2020-01-01 12:00:00,000 - SELECT "x" FROM "%s" WHERE "n" = %d ran in %ss\n'
                    % (tbl, nval, dur))
        q("rareslow", 1, "30.0")            # 1 call, total 30, max 30
        for f in range(6):                  # 6 shapes, total 40 each, max 2
            for i in range(20):
                q("fill%d" % f, i, "2.0")

    report = slow_query_report(logfile, top=1)  # cap = 4 per pool
    by_total = report["shapes"]
    by_mean = report["shapes_by_mean"]
    # the total ranking leads with a frequent "fill" shape, not rareslow
    assert by_total[0]["tables"][0].startswith("fill")
    assert "rareslow" not in {d["tables"][0] for d in by_total}
    # the mean ranking leads with rareslow -- and it kept its example, because
    # its max duration made it a max-pool leader even though its total did not
    bm = by_mean[0]
    assert bm["tables"] == ["rareslow"] and bm["count"] == 1 and bm["max"] == 30.0
    assert bm["example"] == 'SELECT "x" FROM "rareslow" WHERE "n" = 1'


def test_example_replicate_atomic(tmp_path):
    # The example and its replicate hint must always come from the same
    # record: when a hintless slower record replaces a hinted faster one,
    # the stale hint must go too.
    logfile = str(tmp_path / "atomic.log")
    with open(logfile, "w") as F:
        # hinted faster, then hintless slower: example moves, hint cleared
        F.write('2020-01-01 12:00:00,000 - SELECT "a" FROM "ta" WHERE "n" = 1 ran in 0.2s\n')
        F.write("2020-01-01 12:00:00,001 - Replicate with db.ta.analyze({'n': 1}, ['a'], 1000, 0)\n")
        F.write('2020-01-01 12:00:01,000 - SELECT "a" FROM "ta" WHERE "n" = 2 ran in 0.9s\n')
        # hintless slower first, hinted faster second: pairing kept
        F.write('2020-01-01 12:00:02,000 - SELECT "a" FROM "tb" WHERE "n" = 3 ran in 0.9s\n')
        F.write('2020-01-01 12:00:03,000 - SELECT "a" FROM "tb" WHERE "n" = 4 ran in 0.2s\n')
        F.write("2020-01-01 12:00:03,001 - Replicate with db.tb.analyze({'n': 4}, ['a'], 1000, 0)\n")
        # hintless faster first, hinted slower second: both move together
        F.write('2020-01-01 12:00:04,000 - SELECT "a" FROM "tc" WHERE "n" = 5 ran in 0.2s\n')
        F.write('2020-01-01 12:00:05,000 - SELECT "a" FROM "tc" WHERE "n" = 6 ran in 0.9s\n')
        F.write("2020-01-01 12:00:05,001 - Replicate with db.tc.analyze({'n': 6}, ['a'], 1000, 0)\n")

    report = slow_query_report(logfile, top=10)
    assert report["records"] == 6 and report["unparsed"] == 0
    shapes = {data["shape"].split('FROM "')[1][:2]: data for data in report["shapes"]}
    assert set(shapes) == {"ta", "tb", "tc"}

    ta = shapes["ta"]
    assert ta["example"] == 'SELECT "a" FROM "ta" WHERE "n" = 2'
    assert ta["replicate"] is None  # not the stale hint of the faster record
    assert ta["max"] == 0.9 and ta["count"] == 2

    tb = shapes["tb"]
    assert tb["example"] == 'SELECT "a" FROM "tb" WHERE "n" = 3'
    assert tb["replicate"] is None  # the hint of the faster record is not stolen

    tc = shapes["tc"]
    assert tc["example"] == 'SELECT "a" FROM "tc" WHERE "n" = 6'
    assert tc["replicate"] == "db.tc.analyze({'n': 6}, ['a'], 1000, 0)"


def test_show_slow_report(slow_setup, capsys):
    database, table, logfile = slow_setup
    for i in (1, 2):
        table.search({"n": i}, ["label"], limit=5)
    show_slow_report(logfile, db=database)
    out = capsys.readouterr().out
    assert "parsed records" in out
    assert "query shapes by total time" in out
    assert "query shapes by mean time" in out
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
