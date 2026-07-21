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

The mirror tracks a live database, so most assertions here check structure
and types rather than specific values.  Values are pinned only where the
mathematics pins them: the number of imaginary quadratic fields of class
number 1 is a theorem, not a census, and the polredabs-reduced polynomial of
a labelled field is determined by its label.  Those cannot rot.
"""
import os
from collections import Counter

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
    assert mirror._is_read_only()


def test_lookup_by_label(mirror):
    field = mirror.nf_fields.lookup("2.2.5.1")
    assert field["degree"] == 2
    assert field["label"] == "2.2.5.1"


def test_lucky_with_projection(mirror):
    ainvs = mirror.ec_curvedata.lucky({"lmfdb_label": "11.a2"}, "ainvs")
    # A Weierstrass model is five exact integers -- Python ints without Sage,
    # Sage Integers with it, so assert integrality rather than a type.  For
    # 11.a2 the model itself is pinned: y^2 + y = x^3 - x^2 - 10x - 20.
    assert ainvs == [0, -1, 1, -10, -20]
    assert all(int(a) == a for a in ainvs)


def test_lookup_missing_label_returns_none(mirror):
    assert mirror.nf_fields.lookup("this.label.does.not.exist") is None


def test_count_with_constraint(mirror):
    # The unfiltered count comes from the cached stats total, which LMFDB
    # maintains, so this also exercises the quick-count path against totals
    # that are real -- and makes the comparison meaningful: a constrained
    # count must land strictly between zero and everything.
    total = mirror.nf_fields.count()
    cubics = mirror.nf_fields.count({"degree": 3})
    assert 0 < cubics < total


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


# The next two tests query the imaginary quadratic fields of class number 1
# and 2.  There are exactly 9 and 18 of them (Baker--Heegner--Stark), so both
# values are guaranteed to appear in a full result set -- whereas a range over
# a plentiful column, with a limit, fills up with the smallest value and never
# demonstrates more than one endpoint.  Being theorems, the counts cannot rot.


def test_search_range_query(mirror):
    seen = Counter(
        mirror.nf_fields.search(
            {"degree": 2, "r2": 1, "class_number": {"$gte": 1, "$lte": 2}},
            projection="class_number",
        )
    )
    assert seen == {1: 9, 2: 18}


def test_search_in_operator(mirror):
    values = list(
        mirror.nf_fields.search(
            {"degree": 2, "r2": 1, "class_number": {"$in": [1, 2]}},
            projection="class_number",
        )
    )
    assert len(values) == 27
    assert set(values) == {1, 2}


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


# A numeric column plays two roles in the LMFDB, and the converter treats
# them differently: values without a decimal point are exact integers of
# arbitrary size, while values with one are high-precision reals -- exact as
# *digit strings*, not as numbers.  One test per role.


def test_numeric_column_large_integers_are_exact(mirror):
    assert mirror.nf_fields.col_type["disc_abs"] == "numeric"
    row = mirror.nf_fields.lucky({"degree": 24}, ["label", "disc_abs"])
    d = row["disc_abs"]
    # Discriminants of degree-24 fields need far more than the 53 bits a
    # float carries, so surviving the round trip below proves exactness.
    assert d > 2**53
    assert int(d) == d
    assert mirror.nf_fields.count({"degree": 24, "disc_abs": d}) >= 1


def test_numeric_column_fractional_values(mirror):
    from psycodict.encoding import SAGE_MODE

    # 37.a1 is the first rank-1 elliptic curve, so its regulator is a
    # genuinely irrational number stored to high precision.
    assert mirror.ec_curvedata.col_type["regulator"] == "numeric"
    reg = mirror.ec_curvedata.lucky({"lmfdb_label": "37.a1"}, "regulator")
    assert 0.0511 < float(reg) < 0.0512
    if SAGE_MODE:
        # Under Sage the digits arrive wrapped in an LmfdbRealLiteral, which
        # prints exactly as Postgres sent them.
        from psycodict.encoding import LmfdbRealLiteral

        assert isinstance(reg, LmfdbRealLiteral)
    else:
        # Without Sage the documented fallback is a float: the tail of the
        # stored precision is deliberately given up.
        assert isinstance(reg, float)


def test_numeric_column_integral_values_stay_integral(mirror):
    # 11.a2 has rank 0, so its regulator is exactly 1 -- and must come back
    # as an integral 1 (int, or Sage Integer), not 1.0.
    reg = mirror.ec_curvedata.lucky({"lmfdb_label": "11.a2"}, "regulator")
    assert reg == 1
    assert not isinstance(reg, float)


def test_jsonb_column_round_trips(mirror):
    # iwdata is genuinely heterogeneous jsonb: within one document the
    # per-prime values are either a [lambda, mu] pair or a string marker.
    # (nf_fields.coeffs, the obvious-looking candidate, is numeric[], not
    # jsonb -- see test_numeric_array_column.)
    assert mirror.ec_iwasawa.col_type["iwdata"] == "jsonb"
    iwdata = mirror.ec_iwasawa.lookup("100.a1", "iwdata")
    assert isinstance(iwdata, dict) and iwdata
    assert all(isinstance(key, str) for key in iwdata)
    kinds = {type(value) for value in iwdata.values()}
    assert list in kinds and str in kinds
    pair = next(value for value in iwdata.values() if isinstance(value, list))
    assert all(int(entry) == entry for entry in pair)


def test_numeric_array_column(mirror):
    # x^2 - x - 1 is the polredabs-reduced polynomial of Q(sqrt 5), pinned
    # by the label, so the decoded value itself can be asserted.
    assert mirror.nf_fields.col_type["coeffs"] == "numeric[]"
    coeffs = mirror.nf_fields.lookup("2.2.5.1", "coeffs")
    assert coeffs == [-1, -1, 1]
    assert all(int(c) == c for c in coeffs)


def test_search_table_reports_its_columns(mirror):
    cols = mirror.nf_fields.search_cols
    assert "degree" in cols
    assert "label" in cols


def test_execute_raw_sql(mirror):
    from psycopg.sql import SQL

    (one,) = mirror._execute(SQL("SELECT 1")).fetchone()
    assert one == 1


def test_joined_search(mirror):
    # The query joins motivated: constrain and project across ec_nfcurves and
    # the nf_fields row it references by label.
    join = [("field_label", "nf_fields.label")]
    res = mirror.ec_nfcurves.search(
        {"rank": 1, "nf_fields.r2": 1},
        ["label", "field_label", "nf_fields.degree", "nf_fields.r2"],
        join=join,
        limit=3,
    )
    assert len(res) == 3
    for rec in res:
        assert set(rec) == {"label", "field_label", "nf_fields.degree", "nf_fields.r2"}
        assert rec["nf_fields.r2"] == 1
        # r1 + 2*r2 = degree, so one pair of complex places needs degree >= 2
        assert rec["nf_fields.degree"] >= 2
        # the joined columns agree with the field's own row
        field = mirror.nf_fields.lucky({"label": rec["field_label"]}, ["degree", "r2"])
        assert field == {"degree": rec["nf_fields.degree"], "r2": rec["nf_fields.r2"]}
    assert mirror.ec_nfcurves.lucky(
        {"label": res[0]["label"]}, "nf_fields.degree", join=join
    ) == res[0]["nf_fields.degree"]


def test_col_compares_columns_on_real_data(mirror):
    # disc_abs = |disc| equals disc_rad exactly when the discriminant is
    # squarefree; check $col against a Python-side comparison of the same rows.
    query = {"degree": 2, "disc_abs": {"$lte": 50}}
    rows = list(mirror.nf_fields.search(query, ["disc_abs", "disc_rad"], sort=[]))
    expected = sum(1 for rec in rows if rec["disc_abs"] == rec["disc_rad"])
    assert 0 < expected < len(rows)
    query["disc_abs"] = {"$lte": 50, "$col": "disc_rad"}
    assert mirror.nf_fields.count(query) == expected
