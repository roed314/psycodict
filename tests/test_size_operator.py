# -*- coding: utf-8 -*-
"""
Tests for the ``$size`` query operator, which constrains the number of elements
of an array or jsonb column: ``cardinality`` for array columns, and for jsonb
the array length or object key count (a jsonb scalar has no size, so it never
matches and never errors).  The value is either an integer (an equality test on
the length) or an operator dictionary that constrains the length like any
integer column.
"""
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def varr_table(table_factory):
    """
    A table whose ``arr`` (integer[]) and ``jarr`` (jsonb) columns hold arrays
    of varying length, including an empty array and a NULL, so the boundary
    behaviour of ``$size`` can be pinned down::

        label  arr            jarr        length
        a0     []             []          0
        a1     [10]           [1]         1
        a2     [10, 20]       [1, 2]      2
        a3     [10, 20, 30]   [1, 2, 3]   3
        a4     NULL           NULL        (none; length is NULL)
    """
    t = table_factory(
        columns=[("n", "integer"), ("label", "text"),
                 ("arr", "integer[]"), ("jarr", "jsonb")],
        suffix="_varr",
    )
    t.insert_many([
        {"n": 0, "label": "a0", "arr": [], "jarr": []},
        {"n": 1, "label": "a1", "arr": [10], "jarr": [1]},
        {"n": 2, "label": "a2", "arr": [10, 20], "jarr": [1, 2]},
        {"n": 3, "label": "a3", "arr": [10, 20, 30], "jarr": [1, 2, 3]},
        {"n": 4, "label": "a4", "arr": None, "jarr": None},
    ])
    return t


@pytest.fixture
def size_join_tables(table_factory):
    """
    Two joinable tables so ``$size`` can be exercised on a joined column,
    mirroring tests/test_joins.py.  Curves point at fields by label, and each
    field carries a ``tags`` array of a different length::

        fields: f1 tags=[1, 2]     (size 2)
                f2 tags=[1, 2, 3]  (size 3)
                f3 tags=[]         (size 0)
        curves: c1->f1  c2->f1  c3->f2  c4->f3  c5->f2
    """
    curves = table_factory(
        columns=[("n", "integer"), ("label", "text"), ("fk", "text")],
        suffix="_scurves",
    )
    fields = table_factory(
        columns=[("n", "integer"), ("label", "text"), ("tags", "integer[]")],
        suffix="_sfields",
    )
    fields.insert_many([
        {"n": 1, "label": "f1", "tags": [1, 2]},
        {"n": 2, "label": "f2", "tags": [1, 2, 3]},
        {"n": 3, "label": "f3", "tags": []},
    ])
    curves.insert_many([
        {"n": i, "label": "c%d" % i, "fk": fk}
        for i, fk in enumerate(["f1", "f1", "f2", "f3", "f2"], 1)
    ])
    return curves, fields


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

def test_size_sql_uses_cardinality(empty_table):
    # Array columns use cardinality, NOT array_length: array_length of an empty
    # array is NULL (so $size 0 could never match one) while cardinality is 0.
    stmt, vals = empty_table._parse_dict({"vec": {"$size": 3}})
    sql = stmt.as_string(empty_table.conn)
    assert 'cardinality("vec")' in sql
    assert "array_length" not in sql
    assert vals == [3]
    # jsonb columns (and jsonb sub-values reached by a path) dispatch on the
    # jsonb type: array length for arrays, key count for objects, NULL else.
    stmt, vals = empty_table._parse_dict({"data.a": {"$size": 2}})
    sql = stmt.as_string(empty_table.conn)
    assert 'jsonb_typeof("data"->\'a\')' in sql
    assert 'jsonb_array_length("data"->\'a\')' in sql
    assert 'jsonb_object_keys("data"->\'a\')' in sql
    assert vals == [2]


def test_size_operator_dict_generates_length_comparison(empty_table):
    # An operator dictionary recurses with the length expression as the outer
    # column, so it produces one comparison per key against cardinality(...).
    stmt, vals = empty_table._parse_dict({"vec": {"$size": {"$gte": 2, "$lte": 5}}})
    sql = stmt.as_string(empty_table.conn)
    assert sql == 'cardinality("vec") >= %s AND cardinality("vec") <= %s'
    assert vals == [2, 5]


def test_size_requires_array_or_jsonb(empty_table):
    # A scalar column has no notion of a number of elements.
    with pytest.raises(ValueError, match="requires an array or jsonb column"):
        empty_table._parse_dict({"n": {"$size": 2}})


# ---------------------------------------------------------------------------
# Array columns
# ---------------------------------------------------------------------------

def test_size_exact_match(filled_table):
    # vec is [i, i+1, i%5], always length 3
    assert filled_table.count({"vec": {"$size": 3}}) == 200
    assert filled_table.count({"vec": {"$size": 2}}) == 0
    assert filled_table.count({"vec": {"$size": 4}}) == 0


def test_size_zero_matches_empty_but_not_null(varr_table):
    # cardinality of an empty array is 0 (array_length would give NULL); the
    # NULL row has length NULL, so $size 0 does not match it
    assert varr_table.search({"arr": {"$size": 0}}, 0, limit=10) == ["a0"]
    # the NULL row really is present -- $exists finds it
    assert varr_table.search({"arr": {"$exists": False}}, 0, limit=10) == ["a4"]


def test_size_operator_dict_forms(varr_table):
    # $gte / $lte
    assert varr_table.search({"arr": {"$size": {"$gte": 2}}}, 0, limit=10) == ["a2", "a3"]
    assert varr_table.search(
        {"arr": {"$size": {"$gte": 1, "$lte": 2}}}, 0, limit=10
    ) == ["a1", "a2"]
    # $in
    assert varr_table.search({"arr": {"$size": {"$in": [1, 3]}}}, 0, limit=10) == ["a1", "a3"]
    # $ne excludes the matching length AND the NULL row (NULL != k is unknown)
    assert varr_table.search({"arr": {"$size": {"$ne": 2}}}, 0, limit=10) == ["a0", "a1", "a3"]
    # even $size >= 0 excludes the NULL row, confirming a NULL length is not 0
    assert varr_table.search({"arr": {"$size": {"$gte": 0}}}, 0, limit=10) == ["a0", "a1", "a2", "a3"]


# ---------------------------------------------------------------------------
# jsonb columns
# ---------------------------------------------------------------------------

def test_size_jsonb_top_level_array(varr_table):
    # jarr holds a top-level jsonb array
    assert varr_table.search({"jarr": {"$size": 2}}, 0, limit=10) == ["a2"]
    assert varr_table.search({"jarr": {"$size": 0}}, 0, limit=10) == ["a0"]
    assert varr_table.search({"jarr": {"$size": {"$gte": 2}}}, 0, limit=10) == ["a2", "a3"]


def test_size_jsonb_path_subarray(filled_table):
    # data = {"a": [i, 2*i], ...}, so data.a is a length-2 jsonb array everywhere
    assert filled_table.count({"data.a": {"$size": 2}}) == 200
    assert filled_table.count({"data.a": {"$size": 3}}) == 0


def test_size_jsonb_object_counts_keys(filled_table):
    # data is a jsonb *object* with three keys (a, s, nested) on every row, so
    # $size matches it by key count -- an object has a size too, no longer an
    # error.
    assert filled_table.count({"data": {"$size": 3}}) == 200
    assert filled_table.count({"data": {"$size": 2}}) == 0
    assert filled_table.count({"data": {"$size": {"$gte": 3}}}) == 200
    # a nested single-key object reached by a path (data.nested == {"k": ...})
    assert filled_table.count({"data.nested": {"$size": 1}}) == 200


def test_size_jsonb_scalar_never_matches_and_does_not_raise(filled_table):
    # data.s is a jsonb *string* scalar: it has no size, so $size matches
    # nothing -- and, crucially, does not raise the way a bare
    # jsonb_array_length on a non-array would (that was the point of the CASE).
    assert filled_table.count({"data.s": {"$size": 1}}) == 0
    assert filled_table.count({"data.s": {"$size": 0}}) == 0
    # even the operator-dict form, which would otherwise touch every row's
    # scalar, stays quiet
    assert filled_table.count({"data.s": {"$size": {"$gte": 0}}}) == 0


# ---------------------------------------------------------------------------
# Composition: $or and joins
# ---------------------------------------------------------------------------

def test_size_under_or(varr_table):
    # $size composes inside $or like any other constraint: a3 has size 3, a1
    # has n == 1
    assert varr_table.search(
        {"$or": [{"arr": {"$size": 3}}, {"n": 1}]}, 0, limit=10
    ) == ["a1", "a3"]


def test_size_on_joined_column(size_join_tables):
    curves, fields = size_join_tables
    F = fields.search_table
    join = [("fk", "%s.label" % F)]
    # f1 has 2 tags; c1 and c2 point at f1
    assert curves.search(
        {"%s.tags" % F: {"$size": 2}}, 0, join=join, sort=["n"], limit=10
    ) == ["c1", "c2"]
    # operator-dict form on the joined column: f1 (2) and f2 (3) qualify
    assert curves.search(
        {"%s.tags" % F: {"$size": {"$gte": 2}}}, 0, join=join, sort=["n"], limit=10
    ) == ["c1", "c2", "c3", "c5"]
