# -*- coding: utf-8 -*-
"""
Tests for joined queries: the ``join=`` option of search, count, lucky and
lookup.

The fixture mimics the LMFDB tables that motivated the feature: elliptic
curves over number fields pointing at their base field by label
(ec_nfcurves.field_label -> nf_fields.label), and fields pointing at their
Galois group (nf_fields -> gps_transitive), giving a chain for two-step
joins.  Two curves reference no field row (an unknown label and a null), so
INNER and LEFT joins genuinely differ.
"""
import pytest


@pytest.fixture
def jtables(table_factory):
    """
    Three joinable tables (curves, fields, groups) with data:

    curves: c1..c8 with fk -> f1, f1, f2, f3, f4, fX (no such field), None, f2
            and data = {"rank": n % 3}
    fields: f1 (deg 2, r2 2, gp g1)   f2 (deg 2, r2 1, gp g2)
            f3 (deg 3, r2 1, gp g1)   f4 (deg 4, r2 2, gp g2)
    groups: g1 (size 1), g2 (size 2)
    """
    curves = table_factory(
        columns=[("n", "integer"), ("label", "text"), ("fk", "text"), ("data", "jsonb")],
        suffix="_curves",
    )
    fields = table_factory(
        columns=[("n", "integer"), ("label", "text"), ("deg", "integer"), ("r2", "integer"),
                 ("gp", "text"), ("coeffs", "integer[]"), ("data", "jsonb")],
        suffix="_fields",
    )
    groups = table_factory(
        columns=[("n", "integer"), ("label", "text"), ("size", "integer")],
        suffix="_groups",
    )
    fields.insert_many([
        {"n": 1, "label": "f1", "deg": 2, "r2": 2, "gp": "g1", "coeffs": [1, 2], "data": {"s": "x", "nested": {"k": 1}}},
        {"n": 2, "label": "f2", "deg": 2, "r2": 1, "gp": "g2", "coeffs": [3, 4], "data": {"s": "y", "nested": {"k": 2}}},
        {"n": 3, "label": "f3", "deg": 3, "r2": 1, "gp": "g1", "coeffs": [5, 6], "data": {"s": "x", "nested": {"k": 1}}},
        {"n": 4, "label": "f4", "deg": 4, "r2": 2, "gp": "g2", "coeffs": [7, 8], "data": {"s": "z", "nested": {"k": 0}}},
        # referenced by no curve, so RIGHT and FULL joins differ from INNER
        {"n": 5, "label": "f5", "deg": 5, "r2": 0, "gp": None, "coeffs": [9, 10], "data": {"s": "w", "nested": {"k": 2}}},
    ])
    groups.insert_many([
        {"n": 1, "label": "g1", "size": 1},
        {"n": 2, "label": "g2", "size": 2},
    ])
    curves.insert_many([
        {"n": i, "label": "c%d" % i, "fk": fk, "data": {"rank": i % 3}}
        for i, fk in enumerate(["f1", "f1", "f2", "f3", "f4", "fX", None, "f2"], 1)
    ])
    return curves, fields, groups


def FK(fields):
    """The join specification linking curves to fields."""
    return [("fk", "%s.label" % fields.search_table)]


def test_join_search_basic(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    res = curves.search(
        {"n": {"$lte": 4}, "%s.deg" % F: 2},
        ["label", "%s.r2" % F],
        join=FK(fields),
        limit=10,
    )
    # result keys are exactly the projection entries as given
    assert res == [
        {"label": "c1", "%s.r2" % F: 2},
        {"label": "c2", "%s.r2" % F: 2},
        {"label": "c3", "%s.r2" % F: 1},
    ]


def test_join_one_row_per_matching_pair(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    # joining groups to fields on gp: g1 matches f1 and f3, g2 matches f2 and f4
    res = groups.search({}, "label", join=[("label", "%s.gp" % F)], limit=10)
    assert res == ["g1", "g1", "g2", "g2"]
    assert groups.count(join=[("label", "%s.gp" % F)]) == 4


def test_join_projection_forms(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    # 0: labels of the primary table, filtered by the inner join
    assert curves.search({}, 0, join=join, limit=10) == ["c1", "c2", "c3", "c4", "c5", "c8"]
    # 1: all columns of the primary table (joined columns only by request)
    rec = curves.search({}, 1, join=join, limit=1)[0]
    assert set(rec) == {"n", "label", "fk", "data"}
    # 3: as 1 with id
    rec = curves.search({}, 3, join=join, limit=1)[0]
    assert set(rec) == {"id", "n", "label", "fk", "data"}
    # a single qualified string projects to bare values
    assert curves.search({}, "%s.deg" % F, join=join, limit=10) == [2, 2, 2, 3, 4, 2]
    # id can be requested explicitly, on either table
    rec = curves.search({}, ["id", "%s.id" % F, "label"], join=join, limit=1)[0]
    assert set(rec) == {"id", "%s.id" % F, "label"}


def test_join_constraint_placement(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    assert curves.count({}, join=join) == 6
    # constraint on the joined table only
    assert curves.count({"%s.gp" % F: "g1"}, join=join) == 3
    # on the primary table only
    assert curves.count({"n": {"$gte": 4}}, join=join) == 3
    # on both, including a jsonb path on the primary table
    assert curves.search({"data.rank": 1, "%s.gp" % F: "g1"}, 0, join=join, limit=10) == ["c1", "c4"]


def test_join_jsonb_path_on_joined_table(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    res = curves.search({"%s.data.nested.k" % F: 1}, 0, join=FK(fields), limit=10)
    assert res == ["c1", "c2", "c4"]
    # array path on the joined table (raw SQL index, as without join)
    assert curves.search({"%s.coeffs.1" % F: 1}, 0, join=FK(fields), limit=10) == ["c1", "c2"]


def test_join_left(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    left = [("fk", "%s.label" % F, "left")]
    assert curves.count(join=left) == 8
    assert curves.count(join=FK(fields)) == 6
    # unmatched rows surface NULLs for the joined columns
    assert curves.search({}, "%s.deg" % F, join=left, limit=10) == [2, 2, 2, 3, 4, None, None, 2]
    rec = curves.search({"label": "c6"}, ["label", "%s.deg" % F], join=left, limit=1)[0]
    # the unmatched row's joined column comes back as None
    assert rec == {"label": "c6", "%s.deg" % F: None}
    # the usual LEFT JOIN idiom for "has no match"
    assert curves.search({"%s.label" % F: None}, 0, join=left, limit=10) == ["c6", "c7"]


def test_join_chained(jtables):
    curves, fields, groups = jtables
    F, G = fields.search_table, groups.search_table
    join = [("fk", "%s.label" % F), ("%s.gp" % F, "%s.label" % G)]
    assert curves.search({"%s.size" % G: 1}, ["label", "%s.label" % G], join=join, limit=10) == [
        {"label": "c1", "%s.label" % G: "g1"},
        {"label": "c2", "%s.label" % G: "g1"},
        {"label": "c4", "%s.label" % G: "g1"},
    ]
    # join types are case-insensitive
    assert curves.count(join=[("fk", "%s.label" % F, "LEFT")]) == 8
    assert curves.count(join=[("fk", "%s.label" % F, "INNER")]) == 6


def test_join_sort_limit_offset(jtables):
    curves, fields, groups = jtables
    join = FK(fields)
    assert curves.search({}, 0, join=join, sort=[("n", -1)], limit=3) == ["c8", "c5", "c4"]
    assert curves.search({}, 0, join=join, limit=2, offset=2) == ["c3", "c4"]
    assert curves.search({}, 0, join=join, sort=["fk", "n"], limit=3) == ["c1", "c2", "c3"]


def test_join_count_is_not_cached(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    query = {"%s.deg" % F: 2}
    curves.stats.saving = True
    try:
        assert curves.count(query, join=FK(fields)) == 4
        assert curves.count(query, join=FK(fields)) == 4
    finally:
        curves.stats.saving = False
    # nothing was recorded in the counts table for the joined query
    assert curves.stats.quick_count(query) is None


def test_join_streaming(jtables):
    curves, fields, groups = jtables
    res = curves.search({}, 0, join=FK(fields))
    assert not isinstance(res, list)
    assert list(res) == ["c1", "c2", "c3", "c4", "c5", "c8"]
    # info works without a limit too
    info = {}
    res = curves.search({}, 0, join=FK(fields), info=info)
    assert info["number"] == 6
    assert list(res) == ["c1", "c2", "c3", "c4", "c5", "c8"]


def test_join_info(jtables):
    curves, fields, groups = jtables
    info = {}
    res = curves.search({}, 0, join=FK(fields), limit=4, info=info)
    assert res == ["c1", "c2", "c3", "c4"]
    assert info["number"] == 6
    assert info["exact_count"] is True
    assert info["count"] == 4
    assert info["start"] == 0
    # requesting a page past the end adjusts to the last page
    info = {}
    res = curves.search({}, 0, join=FK(fields), limit=2, offset=50, info=info)
    assert res == ["c5", "c8"]
    assert info["number"] == 6
    assert info["start"] == 4


def test_join_lucky_and_lookup(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    assert curves.lucky({"label": "c4"}, "%s.deg" % F, join=join) == 3
    assert curves.lucky({"label": "c4"}, ["label", "%s.deg" % F], join=join) == {"label": "c4", "%s.deg" % F: 3}
    # c6 references a missing field, so an inner join has no result
    assert curves.lucky({"label": "c6"}, "%s.deg" % F, join=join) is None
    assert curves.lucky({}, 0, join=join, sort=["n"], offset=1) == "c2"
    assert curves.lookup("c5", ["%s.deg" % F, "%s.gp" % F], join=join) == {"%s.deg" % F: 4, "%s.gp" % F: "g2"}


def test_join_col_and_raw_on_joined_table(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    # $col names resolve like query keys, so comparing two columns of the
    # joined table qualifies both
    assert curves.search({"%s.deg" % F: {"$col": "%s.r2" % F}}, 0, join=join, limit=10) == ["c1", "c2"]
    assert curves.search({"%s.deg" % F: {"$gt": {"$col": "%s.r2" % F}}}, 0, join=join, limit=10) == ["c3", "c4", "c5", "c8"]
    # $raw names resolve like query keys too, and come out qualified
    assert curves.search({"%s.deg" % F: {"$raw": "2*%s.r2" % F}}, 0, join=join, limit=10) == ["c3", "c5", "c8"]


def test_join_cross_table_col(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    # a curve whose n equals its field's n
    assert curves.search({"n": {"$col": "%s.n" % F}}, 0, join=join, limit=10) == ["c1"]
    # and inside a comparison operator
    assert curves.count({"n": {"$gt": {"$col": "%s.n" % F}}}, join=join) == 5


def test_join_or_and_not_across_tables(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    # $or branches may constrain different tables
    assert curves.count({"$or": [{"n": 1}, {"%s.gp" % F: "g2"}]}, join=join) == 4
    # $not over a joined-table constraint
    assert curves.count({"$not": {"%s.gp" % F: "g1"}}, join=join) == 3
    # nested: $or under a joined column, mixing $raw and a comparison
    assert curves.count({"%s.deg" % F: {"$or": [{"$raw": "2*%s.r2" % F}, {"$gte": 5}]}}, join=join) == 3


def test_join_cross_table_raw(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    # a curve whose n is one more than its field's n
    assert curves.count({"n": {"$raw": "%s.n+1" % F}}, join=join) == 4
    # bare names in a $raw expression are the primary table's, even under a
    # joined key
    assert curves.search({"%s.deg" % F: {"$raw": "n"}}, 0, join=join, limit=10) == ["c2"]
    with pytest.raises(ValueError, match="not a column"):
        curves.count({"n": {"$raw": "%s.missing+1" % F}}, join=join)


def test_join_sort_by_joined_column(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    assert curves.search({}, 0, join=join, sort=["%s.deg" % F, "n"], limit=10) == \
        ["c1", "c2", "c3", "c8", "c4", "c5"]
    assert curves.search({}, 0, join=join, sort=[("%s.deg" % F, -1), "n"], limit=10) == \
        ["c5", "c4", "c1", "c2", "c3", "c8"]
    with pytest.raises(ValueError, match="not a column"):
        curves.search({}, 0, join=join, sort=["%s.missing" % F], limit=1)


def test_join_right_and_full(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    right = [("fk", "%s.label" % F, "right")]
    full = [("fk", "%s.label" % F, "full")]
    # f5 is referenced by no curve; c6 (unknown field) and c7 (null fk) match none
    assert curves.count(join=right) == 7
    assert curves.count(join=full) == 9
    # the f5 row surfaces with this table's columns NULL
    degs = curves.search({}, "%s.deg" % F, join=right, limit=20)
    assert degs == [2, 2, 2, 3, 4, 2, 5]
    labels = curves.search({}, 0, join=full, limit=20)
    assert labels.count(None) == 1
    assert sorted(x for x in labels if x is not None) == \
        ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]


def test_join_array_slicer_projection(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    assert curves.search({"label": "c1"}, "%s.coeffs[0]" % F, join=FK(fields), limit=1) == [1]


def test_join_or_on_primary_table(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    assert curves.count({"$or": [{"n": 1}, {"n": {"$gte": 8}}]}, join=join) == 2
    assert curves.count({"$or": [{"n": 1}, {"n": {"$gte": 8}}], "%s.gp" % F: "g1"}, join=join) == 1


def test_join_spec_errors(jtables):
    curves, fields, groups = jtables
    F, G = fields.search_table, groups.search_table
    with pytest.raises(ValueError, match="not a search table"):
        curves.count(join=[("fk", "no_such_table.label")])
    with pytest.raises(ValueError, match="not a column"):
        curves.count(join=[("fk", "%s.missing" % F)])
    with pytest.raises(ValueError, match="not a column"):
        curves.count(join=[("missing", "%s.label" % F)])
    with pytest.raises(ValueError, match="already part of the join"):
        curves.count(join=[("fk", "%s.label" % F), ("fk", "%s.label" % F)])
    with pytest.raises(ValueError, match="already part of the join"):
        curves.count(join=[("fk", "%s.label" % curves.search_table)])
    with pytest.raises(ValueError, match="not part of the join yet"):
        curves.count(join=[("%s.gp" % F, "%s.label" % G)])
    with pytest.raises(ValueError, match="each join entry"):
        # the quadruple format of the former join_search method
        curves.count(join=[(curves.search_table, "fk", F, "label")])
    with pytest.raises(ValueError, match="must be qualified"):
        curves.count(join=[("fk", "label")])
    with pytest.raises(ValueError, match="join type"):
        curves.count(join=[("fk", "%s.label" % F, "outer")])
    with pytest.raises(ValueError, match="nonempty list"):
        curves.count(join=[])


def test_join_usage_errors(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    with pytest.raises(ValueError, match="split_ors"):
        curves.search({}, 0, join=join, split_ors=True, limit=1)
    with pytest.raises(ValueError, match="one_per"):
        curves.search({}, 0, join=join, one_per=["fk"], limit=1)
    with pytest.raises(ValueError, match="raw"):
        curves.search({}, 0, join=join, raw="n < 3", limit=1)
    with pytest.raises(ValueError, match="raw"):
        curves.lucky({}, 0, join=join, raw="n < 3")
    with pytest.raises(ValueError, match="groupby"):
        curves.count({}, groupby=["n"], join=join)
    with pytest.raises(ValueError, match="dictionary projections"):
        curves.search({}, {"label": True}, join=join, limit=1)
    with pytest.raises(ValueError, match="projection entries must be strings"):
        curves.search({}, ["label", (F, "deg")], join=join, limit=1)
    with pytest.raises(ValueError, match="Offset cannot be negative"):
        curves.search({}, 0, join=join, offset=-1, limit=1)
    # a qualified key without join falls back to the path interpretation
    with pytest.raises(ValueError, match="not a column"):
        curves.search({"%s.deg" % F: 2}, 0, limit=1)


def test_join_projection_and_sort_accept_paths(jtables):
    curves, fields, groups = jtables
    F = fields.search_table
    join = FK(fields)
    rec = curves.search(
        {"label": "c1"},
        ["label", "%s.data.nested.k" % F, "%s.coeffs.1" % F, "data.rank"],
        join=join, limit=1,
    )[0]
    assert rec == {"label": "c1", "%s.data.nested.k" % F: 1,
                   "%s.coeffs.1" % F: 1, "data.rank": 1}
    # sort by a jsonb path on the joined table
    assert curves.search({}, 0, join=join, sort=["%s.data.nested.k" % F, "n"], limit=10) == \
        ["c5", "c1", "c2", "c4", "c3", "c8"]
    with pytest.raises(ValueError, match="not a column"):
        curves.search({}, ["%s.missing.0" % F], join=join, limit=1)


def test_unjoined_projection_still_rejects_paths(filled_table):
    # dotted paths in projections outside joins are a separate, pre-existing
    # limitation (only array slicers are supported there)
    with pytest.raises(ValueError, match="not"):
        filled_table.search({}, ["data.s"], limit=1)


def test_join_analyze(jtables, capsys):
    curves, fields, groups = jtables
    F = fields.search_table
    curves.analyze({"%s.deg" % F: 2}, ["label", "%s.r2" % F], join=FK(fields), limit=5)
    out = capsys.readouterr().out
    assert "JOIN" in out
    assert "cost=" in out
    assert "Execution Time" in out
    curves.analyze({"%s.deg" % F: 2}, 0, join=FK(fields), limit=5, explain_only=True)
    out = capsys.readouterr().out
    assert "cost=" in out
    assert "Execution Time" not in out
