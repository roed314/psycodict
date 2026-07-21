# -*- coding: utf-8 -*-
"""
Dotted-path (and array-slicer) support in plain, unjoined projections and
sorts.

``search``, ``lucky``, ``analyze`` and ``random_sample`` accept entries such as
``"data.s"`` (a jsonb key path), ``"data.nested.k"`` (a nested jsonb path) and
``"vec.1"`` (an array element, using the raw 1-based SQL index) in the
projection list and in the sort order, matching what joined queries already
allow and the resolution rule documented in QueryLanguage.md.  The generated
SQL mirrors the query-key path specifiers (``->`` for jsonb, ``[n]`` for
arrays), and the keys of the returned result dictionaries are the projection
entries verbatim.

The sample table is described by ``conftest.sample_row``.
"""
import pytest
from psycopg.sql import SQL

from psycodict.utils import IdentifierWrapper

from conftest import sample_row


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------

def test_projection_jsonb_and_array_paths(filled_table):
    row = sample_row(5)
    rec = filled_table.search(
        {"n": 5}, ["label", "data.s", "data.nested.k", "vec.1"], limit=1
    )[0]
    assert rec == {
        "label": row["label"],                         # "l5"
        "data.s": row["data"]["s"],                    # "v5"
        "data.nested.k": row["data"]["nested"]["k"],   # 2
        "vec.1": row["vec"][0],                        # vec[1] (SQL, 1-based) = 5
    }


def test_projection_result_keys_verbatim(filled_table):
    # the result dictionary is keyed by the projection entries exactly as given
    rec = filled_table.search({"n": 5}, ["data.nested.k", "vec.1"], limit=1)[0]
    assert set(rec) == {"data.nested.k", "vec.1"}


def test_projection_mixed_plain_slicer_path(filled_table):
    row = sample_row(5)
    rec = filled_table.search(
        {"n": 5}, ["n", "label", "vec[0:2]", "data.s"], limit=1
    )[0]
    assert rec == {
        "n": 5,
        "label": row["label"],
        "vec[0:2]": row["vec"][0:2],   # [5, 6]
        "data.s": row["data"]["s"],    # "v5"
    }


def test_string_projection_path_returns_bare_values(filled_table):
    # a single-string projection returns the value, not a dictionary
    assert filled_table.search({"n": 3}, "data.s", limit=1) == ["v3"]
    assert filled_table.search({"n": 5}, "vec.1", limit=1) == [5]


def test_lucky_path_projection(filled_table):
    row = sample_row(5)
    assert filled_table.lucky({"n": 5}, ["label", "data.s", "vec.1"]) == {
        "label": row["label"],
        "data.s": row["data"]["s"],
        "vec.1": row["vec"][0],
    }
    # a single-string path projection returns the bare value
    assert filled_table.lucky({"n": 5}, "data.nested.k") == 2


def test_random_sample_path_projection(filled_table):
    # exercises the SYSTEM/BERNOULLI SELECT construction (a distinct call site
    # from search/lucky) with a path projection
    res = list(
        filled_table.random_sample(
            0.5, {}, ["label", "data.s"], mode="bernoulli", repeatable=42
        )
    )
    assert res  # the repeatable seed yields a deterministic, non-empty sample
    assert all(set(r) <= {"label", "data.s"} for r in res)
    for r in res:
        i = int(r["label"][1:])
        assert r["data.s"] == "v%d" % (i % 7)


# ---------------------------------------------------------------------------
# Sorts
# ---------------------------------------------------------------------------

def test_sort_by_jsonb_path_ascending(filled_table):
    # data.nested.k = n % 3; ascending groups k=0 first, tie-broken by n
    res = filled_table.search(
        {}, ["n", "data.nested.k"], sort=["data.nested.k", "n"], limit=6
    )
    assert [(r["n"], r["data.nested.k"]) for r in res] == [
        (0, 0), (3, 0), (6, 0), (9, 0), (12, 0), (15, 0)
    ]


def test_sort_by_path_descending(filled_table):
    # a (path, -1) pair sorts descending (DESC NULLS LAST), k=2 first
    res = filled_table.search(
        {}, ["n", "data.nested.k"], sort=[("data.nested.k", -1), "n"], limit=6
    )
    assert [(r["n"], r["data.nested.k"]) for r in res] == [
        (2, 2), (5, 2), (8, 2), (11, 2), (14, 2), (17, 2)
    ]


def test_sort_by_jsonb_string_path(filled_table):
    # data.s = "v{n%7}"; ascending groups by the jsonb string, tie-broken by n.
    # The 200-row sample means the whole "v0" group (n % 7 == 0) comes first.
    res = filled_table.search({}, "n", sort=["data.s", "n"], limit=8)
    assert res == [0, 7, 14, 21, 28, 35, 42, 49]


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

def test_analyze_with_paths_runs(filled_table, capsys):
    filled_table.analyze({"n": 5}, ["label", "data.s", "vec.1"], limit=5)
    out = capsys.readouterr().out
    # the mogrified query (printed first) carries the jsonb path SQL, and the
    # EXPLAIN ANALYZE plan that follows confirms the query actually executed
    assert '"data"->\'s\'' in out
    assert "cost=" in out


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

def test_plain_projection_sql_identity(filled_table):
    # For plain columns and array slicers, building the SELECT column list with
    # _column_composable(tname=None) must be byte-identical to the previous
    # map(IdentifierWrapper, ...) construction.
    search_cols = filled_table._parse_projection(["n", "label", "vec[0:2]", "num"])
    old = SQL(", ").join(map(IdentifierWrapper, search_cols))
    new = SQL(", ").join(filled_table._column_composable(c) for c in search_cols)
    assert old.as_string() == new.as_string()


def test_path_projection_sql_shape(filled_table):
    # jsonb paths use ->; array paths use [n] with the raw (1-based) SQL index
    assert filled_table._column_composable("data.s").as_string() == '"data"->\'s\''
    assert (
        filled_table._column_composable("data.nested.k").as_string()
        == '"data"->\'nested\'->\'k\''
    )
    assert filled_table._column_composable("vec.1").as_string() == '"vec"[1]'


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_projection_unknown_base_column_raises(filled_table):
    with pytest.raises(ValueError, match="not column"):
        filled_table.search({}, ["nope.foo"], limit=1)
    with pytest.raises(ValueError, match="not column"):
        filled_table.lucky({"n": 1}, ["vec.1", "nope.bar"])


def test_dict_projection_rejects_paths(filled_table):
    # the dict projection form stays column-only: a path is not a column there
    with pytest.raises(ValueError, match="not column"):
        filled_table.search({}, {"data.s": True}, limit=1)


def test_split_ors_rejects_path_projection(filled_table):
    with pytest.raises(ValueError, match="path specifiers in the projection"):
        filled_table.search(
            {"$or": [{"n": 1}, {"n": 2}]}, ["label", "data.s"],
            split_ors=True, limit=5,
        )


def test_split_ors_rejects_path_sort(filled_table):
    with pytest.raises(ValueError, match="path specifiers in the sort"):
        filled_table.search(
            {"$or": [{"n": 1}, {"n": 2}]}, ["label"],
            sort=["data.nested.k"], split_ors=True, limit=5,
        )


def test_one_per_rejects_path_projection(filled_table):
    with pytest.raises(ValueError, match="path specifiers in the projection"):
        filled_table.search({}, ["data.s"], one_per=["flag"], limit=5)


def test_one_per_rejects_path_sort(filled_table):
    with pytest.raises(ValueError, match="path specifiers in the sort"):
        filled_table.search(
            {}, ["label"], one_per=["flag"], sort=["data.nested.k"], limit=5
        )
