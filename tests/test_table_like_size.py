# -*- coding: utf-8 -*-
"""
Tests that ``create_table_like`` produces a *faithful* copy: the same rows,
the same indexes, the same per-column storage settings, and, for a table
with TOASTed values, essentially the same on-disk size.

Regression tests for LMFDB/lmfdb#6775, where a copy of ``mf_hecke_cc`` came
out almost twice the size of its source, the difference living entirely in
the TOAST relation.  The row copy itself (a server side ``INSERT ...
SELECT``) passes toasted values through with their compression intact, so
the only way the sizes can drift is through the per-column storage settings,
which used to be silently reset to the type defaults.
"""
import random
import uuid

import pytest

from psycopg.sql import SQL, Identifier


def fresh_name():
    """
    A table name that no other test (or run) will collide with.
    """
    return "test_%s" % uuid.uuid4().hex[:12]


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
                db.drop_table(name, force=True)
        except Exception:  # pragma: no cover - cleanup must not mask failures
            pass


def _server_version(db):
    return db._execute(
        SQL("SELECT current_setting('server_version_num')::int")
    ).fetchone()[0]


def _column_settings(db, table, columns):
    """
    The (attstorage, attcompression) pair for each of the given columns.

    On servers before postgres 14 the compression entry is always ``""``.
    """
    if _server_version(db) >= 140000:
        selecter = SQL(
            "SELECT attname, attstorage, attcompression FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped"
        )
    else:
        selecter = SQL(
            "SELECT attname, attstorage, '' FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped"
        )
    found = {rec[0]: (rec[1], rec[2]) for rec in db._execute(selecter, [table])}
    return {col: found[col] for col in columns}


def _sizes(db, table):
    """
    The (heap, heap + toast) sizes of a table in bytes.
    """
    return db._execute(
        SQL("SELECT pg_relation_size(%s::regclass), pg_table_size(%s::regclass)"),
        [table, table],
    ).fetchone()


##################################################################
# Functional round trip                                          #
##################################################################


def test_create_table_like_data_copy_round_trips(db, filled_table, transient):
    filled_table.create_index(["n"])
    target = fresh_name()
    db.create_table_like(target, filled_table, data=True, indexes=True)
    transient.append(target)
    copy = db[target]
    source_rows = sorted(filled_table.search({}, projection=3), key=lambda rec: rec["id"])
    copy_rows = sorted(copy.search({}, projection=3), key=lambda rec: rec["id"])
    assert len(copy_rows) == 200
    assert copy_rows == source_rows
    assert [["n"]] == [idx["columns"] for idx in copy.list_indexes().values()]
    assert target + "_n" in copy._list_built_indexes()
    # The data path analyzes the new table, so the planner statistics are
    # populated immediately rather than whenever autovacuum gets to it.
    reltuples = db._execute(
        SQL("SELECT reltuples FROM pg_class WHERE oid = %s::regclass"), [target]
    ).fetchone()[0]
    assert reltuples > 0


##################################################################
# Column storage settings                                        #
##################################################################


def test_create_table_like_preserves_column_storage_settings(db, empty_table, transient):
    source = empty_table.search_table
    db._execute(
        SQL("ALTER TABLE {0} ALTER COLUMN {1} SET STORAGE EXTERNAL").format(
            Identifier(source), Identifier("mat")
        )
    )
    check_compression = _server_version(db) >= 140000
    if check_compression:
        # An explicit pglz differs from the default (empty) setting without
        # requiring the server to have been built with lz4 support.
        db._execute(
            SQL("ALTER TABLE {0} ALTER COLUMN {1} SET COMPRESSION pglz").format(
                Identifier(source), Identifier("vec")
            )
        )
    target = fresh_name()
    db.create_table_like(target, empty_table)
    transient.append(target)
    settings = _column_settings(db, target, ["mat", "vec", "label"])
    assert settings["mat"][0] == "e"
    assert settings["label"][0] == "x"
    if check_compression:
        assert settings["vec"][1] == "p"
        assert settings["label"][1] == ""


##################################################################
# On-disk size parity                                            #
##################################################################


def _write_toasty_file(filename, nrows):
    """
    A ``copy_from`` file whose rows carry a large ``double precision[]``.

    The arrays interleave exact zeros with full precision random values, so
    that (like the Fourier coefficient tables of LMFDB/lmfdb#6775) they are
    large enough to be toasted and compress to roughly half their raw size.
    """
    rng = random.Random(6775)
    tail = ",".join(
        "0" if j % 2 == 0 else repr(rng.uniform(-2, 2)) for j in range(600)
    )
    with open(filename, "w") as F:
        F.write("n|label|an\ninteger|text|double precision[]\n\n")
        for i in range(nrows):
            F.write("%d|l%d|{%r,%s}\n" % (i, i, i * 0.5, tail))


def test_create_table_like_copy_has_the_same_size(db, table_factory, transient, tmp_path):
    source = table_factory(
        columns=[("n", "integer"), ("label", "text"), ("an", "double precision[]")]
    )
    datafile = str(tmp_path / "toasty.txt")
    _write_toasty_file(datafile, 1200)
    source.copy_from(datafile)
    source_heap, source_table = _sizes(db, source.search_table)
    # The point of the test is toast accounting, so the source must be big
    # enough to have been toasted at all.
    assert source_table - source_heap > 0
    target = fresh_name()
    db.create_table_like(target, source, data=True)
    transient.append(target)
    copy_heap, copy_table = _sizes(db, target)
    assert len(list(db[target].search({}, projection="id"))) == 1200
    # Both tables hold identical rows, so their sizes must agree closely;
    # the copy coming out much bigger is exactly the regression of #6775.
    assert 0.8 <= copy_table / source_table <= 1.2
    if _server_version(db) >= 140000:
        # The row copy passes toasted values through compressed; recompressing
        # (or storing raw) would show up as a changed compression histogram.
        histogram = SQL(
            "SELECT pg_column_compression(an), count(*) FROM {0} GROUP BY 1 ORDER BY 1"
        )
        source_hist = db._execute(histogram.format(Identifier(source.search_table))).fetchall()
        copy_hist = db._execute(histogram.format(Identifier(target))).fetchall()
        assert copy_hist == source_hist
