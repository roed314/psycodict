# -*- coding: utf-8 -*-
"""
Read-only regression tests against the public LMFDB devmirror.

psycodict's own suite runs against tables it creates itself, which cannot
catch the class of bug that only shows up on a real schema: 190-odd tables,
columns of every supported type, jsonb documents that are genuinely nested,
numerics too large for a float, and statistics tables populated by years of
LMFDB use.  This module points psycodict at that database and checks the read
path still works.

The mirror at devmirror.lmfdb.xyz is public and read-only -- the credentials
below are the documented ones from LMFDB's GettingStarted.md, not secrets.
Because it is both a network dependency and a shared community resource,
these tests are skipped unless ``PSYCODICT_TEST_DEVMIRROR=1`` is set; CI sets
it in one job.

Assertions here deliberately check structure and types rather than specific
values: the mirror tracks a live database, so anything that pins a count or a
row would rot.
"""
import os

import pytest

pytestmark = [
    pytest.mark.devmirror,
    pytest.mark.skipif(
        not os.environ.get("PSYCODICT_TEST_DEVMIRROR"),
        reason="set PSYCODICT_TEST_DEVMIRROR=1 to run tests against devmirror.lmfdb.xyz",
    ),
]

DEVMIRROR = {
    "host": "devmirror.lmfdb.xyz",
    "port": 5432,
    "user": "lmfdb",
    "password": "lmfdb",
    "dbname": "lmfdb",
    # devmirror is a streaming replica reached over the open internet; without
    # keepalives a long query can come back as a dropped connection, which is
    # a documented source of spurious failures in LMFDB's own CI.
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


@pytest.fixture(scope="module")
def mirror(tmp_path_factory):
    from psycodict.config import Configuration
    from psycodict.database import PostgresDatabase

    tmp = tmp_path_factory.mktemp("devmirror")
    config_file = tmp / "config.ini"
    with open(config_file, "w") as F:
        F.write("[logging]\nslowcutoff = 10.0\nslowlogfile = %s\n" % (tmp / "slow.log"))
        F.write("[postgresql]\n")
        for key in ("host", "port", "user", "password", "dbname"):
            F.write("%s = %s\n" % (key, DEVMIRROR[key]))
    config = Configuration(
        defaults={"config_file": str(config_file), "secrets_file": str(tmp / "secrets.ini")},
        readargs=False,
    )
    keepalives = {k: v for k, v in DEVMIRROR.items() if k.startswith("keepalives")}
    database = PostgresDatabase(config=config, **keepalives)
    yield database
    database.conn.close()


def test_connects_and_sees_the_lmfdb_tables(mirror):
    # The mirror carried 193 tables when this test was written; assert only
    # that it is a real LMFDB rather than pinning a number that grows.
    assert len(mirror.tablenames) > 100
    assert "nf_fields" in mirror.tablenames
    assert "ec_curvedata" in mirror.tablenames


def test_mirror_is_read_only(mirror):
    assert mirror.is_read_only()


def test_lookup_by_label(mirror):
    field = mirror.nf_fields.lookup("2.2.5.1")
    assert field["degree"] == 2
    assert field["label"] == "2.2.5.1"


def test_lucky_with_projection(mirror):
    ainvs = mirror.ec_curvedata.lucky({"lmfdb_label": "11.a2"}, "ainvs")
    # A Weierstrass model is five integers; the point is that an integer[]
    # column round-trips to a list of Python ints.
    assert len(ainvs) == 5
    assert all(isinstance(a, int) for a in ainvs)


def test_lookup_missing_label_returns_none(mirror):
    assert mirror.nf_fields.lookup("this.label.does.not.exist") is None


def test_count_with_constraint(mirror):
    # Cubic fields exist and are fewer than all fields.
    cubics = mirror.nf_fields.count({"degree": 3})
    assert cubics > 0


def test_count_is_monotone_under_refinement(mirror):
    loose = mirror.nf_fields.count({"degree": 3})
    tight = mirror.nf_fields.count({"degree": 3, "r2": 1})
    assert 0 < tight <= loose


def test_search_respects_limit_and_sort(mirror):
    labels = list(
        mirror.nf_fields.search(
            {"degree": 2}, projection="label", sort=["label"], limit=5
        )
    )
    assert len(labels) == 5
    assert labels == sorted(labels)


def test_search_range_query(mirror):
    degrees = list(
        mirror.nf_fields.search(
            {"degree": {"$gte": 3, "$lte": 4}}, projection="degree", limit=50
        )
    )
    assert degrees
    assert set(degrees) <= {3, 4}


def test_search_in_operator(mirror):
    degrees = set(
        mirror.nf_fields.search(
            {"degree": {"$in": [2, 5]}}, projection="degree", limit=50
        )
    )
    assert degrees <= {2, 5}


def test_exists(mirror):
    assert mirror.nf_fields.exists({"degree": 2})
    assert not mirror.nf_fields.exists({"degree": -1})


def test_random_returns_a_matching_row(mirror):
    label = mirror.nf_fields.random({"degree": 2}, "label")
    assert label.startswith("2.")


def test_max_on_a_real_column(mirror):
    assert mirror.nf_fields.stats.max("degree") >= 2


def test_stats_column_counts(mirror):
    # ec_curvedata has cached rank statistics; this exercises the stats table
    # read path against data psycodict did not generate.
    counts = mirror.ec_curvedata.stats.column_counts("rank")
    assert counts
    assert all(isinstance(v, int) for v in counts.values())


def test_numeric_column_is_exact(mirror):
    from decimal import Decimal

    # Regulators are numeric, not float: they must not arrive as a float.
    reg = mirror.ec_curvedata.lucky({"lmfdb_label": "11.a2"}, "regulator")
    assert isinstance(reg, (Decimal, int)) or reg is None


def test_jsonb_column_round_trips(mirror):
    # A jsonb column should come back as ordinary Python containers.
    row = mirror.nf_fields.lucky({"degree": 4}, ["label", "coeffs"])
    assert isinstance(row["coeffs"], list)


def test_search_table_reports_its_columns(mirror):
    cols = mirror.nf_fields.search_cols
    assert "degree" in cols
    assert "label" in cols


def test_execute_raw_sql(mirror):
    from psycopg2.sql import SQL

    (one,) = mirror._execute(SQL("SELECT 1")).fetchone()
    assert one == 1
