# -*- coding: utf-8 -*-
"""
Tests for the read side of the API: ``search``, ``lucky``, ``lookup``, the
query language understood by ``_parse_dict``, and the statistics shortcuts
(``count``, ``max``, ``min``, ``sum``, ...) that hang off a search table.

The sample table is described by ``conftest.sample_row``; the expected counts
below are all derived from it, so they are stated as literals rather than
recomputed with the same expression the fixture uses.
"""
import logging
from decimal import Decimal

import pytest

from conftest import sample_row


@pytest.fixture
def extras_table(table_factory):
    """
    A table split into a search table and an extras table.
    """
    table = table_factory(
        columns=[("n", "integer"), ("label", "text")],
        extra_columns=[("note", "text")],
    )
    table.insert_many([{"n": i, "label": "e%d" % i, "note": "N%d" % i} for i in range(5)])
    return table


@pytest.fixture
def unlabelled_table(table_factory):
    """
    A table without a label column, for the error paths of label lookups.
    """
    table = table_factory(columns=[("n", "integer")], label_col=None)
    table.insert_many([{"n": i} for i in range(3)])
    return table


@pytest.fixture
def nullable_table(table_factory):
    """
    A tiny table where the second row is null in every non-label column.

    The second row is inserted separately, listing only the columns it sets:
    the omitted ones are then genuinely NULL, whereas passing None explicitly
    would store the JSON value ``null`` in the jsonb column (see
    ``test_jsonb_explicit_none_is_stored_as_sql_null``).
    """
    table = table_factory()
    table.insert_many([{
        "n": 0, "label": "a", "num": Decimal("1.25"), "x": 0.5, "flag": True,
        "data": {"k": 1}, "vec": [1, 2], "mat": [Decimal("0.5"), Decimal(2)],
    }])
    table.insert_many([{"n": 1, "label": "b"}])
    return table


##################################################################
# search: result type and projections                            #
##################################################################

def test_search_result_type_depends_on_limit(filled_table):
    unlimited = filled_table.search({"n": {"$lt": 5}}, "n")
    assert iter(unlimited) is unlimited
    assert next(iter(unlimited)) == 0
    assert filled_table.search({"n": {"$lt": 5}}, "n", limit=5) == [0, 1, 2, 3, 4]


def test_search_projection_scalar_forms(filled_table):
    assert filled_table.search({"n": {"$lt": 3}}, "label", limit=3) == ["l0", "l1", "l2"]
    assert filled_table.search({"n": {"$lt": 3}}, 0, limit=3) == ["l0", "l1", "l2"]
    assert filled_table.search({"n": {"$lt": 3}}, "num", limit=3) == [7, 17, 27]


def test_search_projection_integer_forms(extras_table):
    assert extras_table.search({"n": 1}, 1, limit=1) == [{"n": 1, "label": "e1"}]
    assert extras_table.search({"n": 1}, 2, limit=1) == [{"n": 1, "label": "e1", "note": "N1"}]
    assert extras_table.search({"n": 1}, 3, limit=1) == [
        {"id": 1, "n": 1, "label": "e1", "note": "N1"}
    ]


def test_search_projection_list(filled_table):
    assert filled_table.search({"n": 3}, ["label", "num"], limit=1) == [
        {"label": "l3", "num": 37}
    ]
    assert filled_table.search({"n": 3}, ["id"], limit=1) == [{"id": 3}]


def test_search_projection_dict(filled_table):
    assert filled_table.search({"n": 3}, {"label": True, "n": True}, limit=1) == [
        {"label": "l3", "n": 3}
    ]
    excluded = filled_table.search({"n": 3}, {"data": False, "mat": False, "vec": False}, limit=1)
    assert sorted(excluded[0]) == ["flag", "label", "n", "num", "x"]


@pytest.mark.xfail(
    strict=True,
    reason="_parse_projection pops entries out of the caller's projection dict, so it cannot be reused",
)
def test_search_projection_dict_is_not_mutated(filled_table):
    projection = {"label": True, "n": True}
    filled_table.search({"n": 3}, projection, limit=1)
    assert projection == {"label": True, "n": True}
    assert filled_table.search({"n": 3}, projection, limit=1) == [{"label": "l3", "n": 3}]


def test_search_projection_invalid_raises(filled_table):
    with pytest.raises(ValueError):
        filled_table.search({}, {"n": True, "label": False}, limit=1)
    with pytest.raises(ValueError):
        filled_table.search({}, ["not_a_column"], limit=1)
    with pytest.raises(ValueError):
        filled_table.search({}, [], limit=1)


def test_search_projection_zero_without_label_column_raises(unlabelled_table):
    with pytest.raises(RuntimeError):
        unlabelled_table.search({}, 0, limit=1)


def test_search_projection_array_slice_is_zero_based(filled_table):
    # IdentifierWrapper converts Python slicers to Postgres ones, so vec[0] is
    # the first entry even though Postgres arrays are 1-indexed.
    assert filled_table.search({"n": 3}, ["vec[0]"], limit=1) == [{"vec[0]": 3}]
    assert filled_table.search({"n": 3}, ["vec[0:2]"], limit=1) == [{"vec[0:2]": [3, 4]}]


##################################################################
# search: sorting, limit and offset                              #
##################################################################

def test_search_sort_order(filled_table):
    assert filled_table.search({"n": {"$lt": 5}}, "n", limit=5) == [0, 1, 2, 3, 4]
    assert filled_table.search({}, "n", limit=3, sort=[("n", -1)]) == [199, 198, 197]
    assert filled_table.search({}, "n", limit=3, sort=["flag", ("n", -1)]) == [199, 197, 196]


def test_search_offset(filled_table):
    assert filled_table.search({}, "n", limit=3, offset=5) == [5, 6, 7]
    assert filled_table.search({}, "n", limit=5, offset=1000) == []


def test_search_negative_offset_raises(filled_table):
    with pytest.raises(ValueError):
        filled_table.search({}, limit=1, offset=-1)


def test_search_degenerate_queries(filled_table):
    assert len(list(filled_table.search({}, "n"))) == 200
    assert filled_table.search({"n": -1}, "n", limit=5) == []
    assert list(filled_table.search({"n": -1}, "n")) == []


##################################################################
# search: the info dictionary                                    #
##################################################################

def test_search_info_fields(filled_table):
    info = {}
    assert filled_table.search({"n": {"$lt": 10}}, 0, limit=3, info=info) == ["l0", "l1", "l2"]
    assert info == {
        "query": {"n": {"$lt": 10}},
        "number": 10,
        "count": 3,
        "start": 0,
        "exact_count": True,
    }


def test_search_info_without_limit_only_sets_number(filled_table):
    info = {}
    assert list(filled_table.search({"n": {"$lt": 3}}, 0, info=info)) == ["l0", "l1", "l2"]
    assert info == {"number": 3}


def test_search_info_offset_beyond_results_returns_last_page(filled_table):
    info = {}
    assert filled_table.search({"n": {"$lt": 5}}, "n", limit=2, offset=99, info=info) == [3, 4]
    assert info["start"] == 3
    assert info["number"] == 5


def test_search_info_inexact_count_above_cutoff(filled_table, monkeypatch):
    monkeypatch.setattr(filled_table, "_count_cutoff", 5)
    info = {}
    assert filled_table.search({"n": {"$lt": 100}}, "n", limit=2, info=info) == [0, 1]
    assert info["number"] == 5
    assert info["exact_count"] is False


@pytest.mark.xfail(
    strict=True,
    reason="info['number'] for the empty query is the stale meta_tables.total; see the stats.saving guard in table.py",
)
def test_search_info_number_for_empty_query(filled_table):
    info = {}
    filled_table.search({}, 0, limit=3, info=info)
    assert info["number"] == 200


##################################################################
# search: one_per, silent, split_ors                             #
##################################################################

def test_search_one_per(filled_table):
    assert filled_table.search({"n": {"$lt": 10}}, ["n", "flag"], limit=5, one_per=["flag"]) == [
        {"n": 0, "flag": True},
        {"n": 1, "flag": False},
    ]
    assert filled_table.search({"n": {"$lt": 6}}, 0, one_per=["flag"], limit=5) == ["l0", "l1"]


def test_search_one_per_respects_sort(filled_table):
    assert filled_table.search(
        {"n": {"$lt": 10}}, ["n", "flag"], limit=5, sort=[("n", -1)], one_per=["flag"]
    ) == [
        {"n": 9, "flag": True},
        {"n": 8, "flag": False},
    ]


def test_search_silent_suppresses_slow_query_log(filled_table, monkeypatch):
    records = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    monkeypatch.setattr(filled_table.logger, "handlers", [Collector()])
    monkeypatch.setattr(filled_table, "slow_cutoff", -1)  # every query is now slow
    assert list(filled_table.search({"n": 5}, "n", silent=True)) == [5]
    assert records == []
    assert list(filled_table.search({"n": 5}, "n")) == [5]
    assert records


def test_search_split_ors(filled_table):
    assert filled_table.search({"$or": [{"n": 1}, {"n": 2}]}, "n", limit=5, split_ors=True) == [1, 2]
    with pytest.raises(ValueError):
        filled_table.search({"$or": [{"n": 1}]}, "n", split_ors=True)


@pytest.mark.xfail(
    strict=True,
    reason="_split_ors sorts the split queries by the raw clause value, which fails when a branch is a range dict",
)
def test_search_split_ors_with_range_clauses(filled_table):
    assert filled_table.search(
        {"$or": [{"n": {"$lt": 3}}, {"n": {"$gt": 197}}]}, "n", limit=10, split_ors=True
    ) == [0, 1, 2, 198, 199]


##################################################################
# lucky, lookup, exists                                          #
##################################################################

def test_lucky_projection_forms(filled_table):
    assert filled_table.lucky({"n": 5}, "label") == "l5"
    assert filled_table.lucky({"n": 5}, 0) == "l5"
    assert filled_table.lucky({"n": 5}, ["n", "num"]) == {"n": 5, "num": 57}
    assert filled_table.lucky({"n": 5})["label"] == "l5"


def test_lucky_no_match_returns_none(filled_table):
    assert filled_table.lucky({"n": -1}) is None
    assert filled_table.lucky({"n": -1}, "label") is None


def test_lucky_offset(filled_table):
    assert filled_table.lucky({"n": {"$lt": 5}}, "n", offset=2, sort=["n"]) == 2


def test_lookup_by_label(filled_table):
    assert filled_table.lookup("l17")["n"] == 17
    assert filled_table.lookup("l17", projection="num") == 177
    assert filled_table.lookup(7, projection="label", label_col="n") == "l7"


def test_lookup_missing_label_returns_none(filled_table):
    assert filled_table.lookup("no_such_label") is None


def test_lookup_without_label_column_raises(unlabelled_table):
    with pytest.raises(ValueError):
        unlabelled_table.lookup("anything")


def test_exists(filled_table, table_factory):
    assert filled_table.exists({"n": 5}) is True
    assert filled_table.exists({"n": -1}) is False
    assert filled_table.label_exists("l5") is True
    assert filled_table.label_exists("l500") is False
    assert table_factory().exists({"n": 5}) is False


##################################################################
# counting and other statistics                                  #
##################################################################

def test_count_with_query(filled_table):
    assert filled_table.count({"n": {"$lt": 10}}) == 10
    assert filled_table.count({"n": -1}) == 0
    assert filled_table.count({"flag": True}) == 67


@pytest.mark.xfail(
    strict=True,
    reason="count() returns stale meta_tables.total; see the stats.saving guard in table.py",
)
def test_count_without_query(filled_table):
    assert filled_table.count({}) == 200


def test_count_groupby(filled_table):
    assert filled_table.count({"n": {"$lt": 6}}, groupby=["flag"]) == {(True,): 2, (False,): 4}


def test_count_groupby_does_not_print(filled_table, capsys):
    filled_table.count({"n": {"$lt": 6}}, groupby=["flag"])
    assert capsys.readouterr().out == ""


def test_count_distinct(filled_table, table_factory):
    assert filled_table.count_distinct("flag") == 2
    assert filled_table.count_distinct("n") == 200
    assert filled_table.count_distinct(["flag", "n"]) == 200
    assert filled_table.count_distinct("flag", {"n": {"$lt": 3}}) == 2
    assert filled_table.count_distinct("n", {"n": {"$lt": 3}}) == 3
    assert table_factory().count_distinct("n") == 0


def test_distinct(filled_table):
    assert filled_table.distinct("flag") == [False, True]
    assert filled_table.distinct("n", {"n": {"$lt": 3}}) == [0, 1, 2]


def test_max_min_sum(filled_table):
    assert filled_table.max("n") == 199
    assert filled_table.min("n") == 0
    assert filled_table.sum("n") == 19900
    assert filled_table.max("num") == 1997
    assert filled_table.min("x") == 0.0
    assert filled_table.max("label") == "l99"


def test_max_min_sum_with_constraint(filled_table):
    assert filled_table.max("n", {"n": {"$lt": 10}}) == 9
    assert filled_table.min("n", {"n": {"$gte": 10}}) == 10
    assert filled_table.sum("n", {"n": {"$lt": 4}}) == 6
    assert filled_table.sum("n", {"n": -1}) is None


def test_max_error_cases(filled_table):
    with pytest.raises(ValueError):
        filled_table.max("not_a_column")
    # documented: "Will raise an error if there are no non-null values"
    with pytest.raises(TypeError):
        filled_table.max("n", {"n": -1})


##################################################################
# query operators                                                #
##################################################################

def test_query_inequalities(filled_table):
    assert filled_table.count({"n": {"$gte": 5, "$lt": 8}}) == 3
    assert filled_table.count({"n": {"$gt": 197}}) == 2
    assert filled_table.count({"n": {"$lte": 2}}) == 3
    assert filled_table.search({"n": {"$gt": 4, "$lte": 6}}, "n", limit=5) == [5, 6]


def test_query_ne(filled_table):
    assert filled_table.count({"n": {"$ne": 5}}) == 199
    assert filled_table.count({"label": {"$ne": "l5"}}) == 199


def test_query_in(filled_table):
    assert filled_table.search({"n": {"$in": [1, 2, 3]}}, "n", limit=5) == [1, 2, 3]
    assert filled_table.count({"label": {"$in": ["l1", "l2", "nope"]}}) == 2


def test_query_nin(filled_table):
    assert filled_table.count({"n": {"$nin": [1, 2, 3]}}) == 197
    assert filled_table.search({"n": {"$lt": 4, "$nin": [1, 2]}}, "n", limit=5) == [0, 3]


def test_query_or_at_top_level(filled_table):
    assert filled_table.search({"$or": [{"n": 1}, {"n": 2}]}, "n", limit=5) == [1, 2]
    assert filled_table.count({"$or": [{"n": {"$lt": 3}}, {"n": {"$gt": 197}}]}) == 5
    # an empty $or is unsatisfiable
    assert filled_table.count({"$or": []}) == 0


def test_query_and_at_top_level(filled_table):
    assert filled_table.count({"$and": [{"n": {"$gte": 5}}, {"n": {"$lt": 8}}]}) == 3
    # an empty $and imposes no constraint, leaving only the label clause
    assert filled_table.count({"$and": [], "label": "l5"}) == 1


def test_query_not_at_top_level(filled_table):
    assert filled_table.count({"$not": {"n": {"$lt": 195}}}) == 5
    assert filled_table.count({"$not": {"flag": True}}) == 133


def test_query_or_and_not_within_a_column(filled_table):
    assert filled_table.count({"n": {"$or": [1, 2, 3]}}) == 3
    assert filled_table.count({"n": {"$lt": 10, "$not": 5}}) == 9


def test_query_like_and_ilike(filled_table):
    assert filled_table.count({"label": {"$like": "l1_"}}) == 10
    assert filled_table.count({"label": {"$like": "l1%"}}) == 111
    assert filled_table.count({"label": {"$ilike": "L1_"}}) == 10
    assert filled_table.count({"label": {"$like": "L1_"}}) == 0


def test_query_regex(filled_table):
    assert filled_table.count({"label": {"$regex": "^l1[0-9]$"}}) == 10
    assert filled_table.search({"label": {"$regex": "^l19[89]$"}}, "n", limit=5) == [198, 199]


def test_query_startswith_escapes_wildcards(filled_table):
    assert filled_table.count({"label": {"$startswith": "l19"}}) == 11
    # the underscore is escaped, so it does not act as a LIKE wildcard
    assert filled_table.count({"label": {"$startswith": "l1_"}}) == 0


def test_query_exists_and_null(nullable_table):
    assert nullable_table.count({"num": {"$exists": True}}) == 1
    assert nullable_table.count({"num": {"$exists": False}}) == 1
    assert nullable_table.search({"num": None}, "label", limit=5) == ["b"]
    assert nullable_table.search({"num": {"$exists": True}}, "label", limit=5) == ["a"]


def test_query_mod(filled_table):
    assert filled_table.count({"n": {"$mod": [1, 7]}}) == 29
    assert filled_table.search({"n": {"$lt": 10, "$mod": [3, 4]}}, "n", limit=5) == [3, 7]


def test_query_raw(filled_table):
    # num = 10 * n + 7, so n = num - 7 holds only for n = 0
    assert filled_table.search({"n": {"$raw": "num-7"}}, "n", limit=5) == [0]
    assert filled_table.search({}, "n", raw="n < 3", limit=5) == [0, 1, 2]
    assert filled_table.search({}, "n", raw="label = %s", raw_values=["l4"], limit=5) == [4]


@pytest.mark.xfail(
    strict=True,
    reason="a nested $raw passes the {'$raw': ...} dict to filter_sql_injection instead of its value",
)
def test_query_raw_nested_in_comparison(filled_table):
    # num = 10 * n + 7 < 11 * n exactly when n > 7
    assert filled_table.count({"num": {"$lt": {"$raw": "n*11"}}}) == 192


##################################################################
# array columns                                                  #
##################################################################

def test_array_equality(filled_table):
    assert filled_table.search({"vec": [5, 6, 0]}, "n", limit=5) == [5]
    assert filled_table.count({"vec": [5, 6, 1]}) == 0
    # numeric[] needs an explicit cast, which _create_typecast supplies
    assert filled_table.search({"mat": [3, 9]}, "n", limit=5) == [3]


def test_array_contains(filled_table):
    assert filled_table.search({"vec": {"$contains": [5, 6]}}, "n", limit=5) == [5]
    # a scalar is wrapped in a singleton array; 5 appears in vec for n = 4 and n = 5
    assert filled_table.count({"vec": {"$contains": 5}}) == 2


def test_array_containedin(filled_table):
    assert filled_table.search({"vec": {"$containedin": [0, 1, 2]}}, "n", limit=5) == [0, 1]


def test_array_overlaps(filled_table):
    assert filled_table.search({"vec": {"$overlaps": [199, 200]}}, "n", limit=5) == [198, 199]
    assert filled_table.count({"vec": {"$overlaps": [1000]}}) == 0


def test_array_notcontains(filled_table):
    # vec = [n, n + 1, n % 5] contains 0 or 1 exactly when n is 0 or 1 mod 5,
    # which is 80 of the 200 rows
    assert filled_table.count({"vec": {"$notcontains": [0, 1]}}) == 120
    # only n = 198 and n = 199 have 199 in vec
    assert filled_table.count({"vec": {"$notcontains": [199]}}) == 198


def test_array_in_matches_whole_arrays(filled_table):
    assert filled_table.search({"vec": {"$in": [[5, 6, 0], [7, 8, 2]]}}, "n", limit=5) == [5, 7]
    assert filled_table.count({"vec": {"$nin": [[5, 6, 0]]}}) == 199


def test_array_index_is_one_based(filled_table):
    assert filled_table.search({"vec.1": 5}, "n", limit=5) == [5]
    assert filled_table.search({"vec.2": 5}, "n", limit=5) == [4]
    assert filled_table.count({"vec.3": 0}) == 40
    assert filled_table.count({"vec.0": 5}) == 0


def test_array_anylte(filled_table):
    assert filled_table.count({"vec": {"$anylte": 0}}) == 40


@pytest.mark.xfail(
    strict=True,
    reason="$maxgte emits array_max(), a function that psycodict never creates",
)
def test_array_maxgte(filled_table):
    # max(vec) = n + 1
    assert filled_table.count({"vec": {"$maxgte": 200}}) == 1


##################################################################
# jsonb columns                                                  #
##################################################################

def test_jsonb_dotted_paths(filled_table):
    assert filled_table.count({"data.s": "v3"}) == 29
    assert filled_table.count({"data.nested.k": 1}) == 67
    assert filled_table.search({"data.s": "v3", "n": {"$lt": 15}}, "n", limit=5) == [3, 10]
    # data.a = [n, 2 * n]; jsonb paths are 0-indexed, unlike the array paths
    # exercised by test_array_index_is_one_based
    assert filled_table.search({"data.a.0": 6}, "n", limit=5) == [6]
    assert filled_table.search({"data.a.1": 6}, "n", limit=5) == [3]


def test_jsonb_document_equality(filled_table):
    assert filled_table.search({"data": sample_row(5)["data"]}, "n", limit=5) == [5]
    # a dict whose keys do not all start with $ is a value, not a constraint
    assert filled_table.count({"data": {"not": "an operator dict"}}) == 0


def test_jsonb_in_with_scalars(filled_table):
    assert filled_table.count({"data.s": {"$in": ["v1", "v2"]}}) == 58
    assert filled_table.count({"data.s": {"$nin": ["v1", "v2"]}}) == 142


def test_jsonb_in_with_documents(filled_table):
    values = [sample_row(5)["data"], sample_row(6)["data"]]
    assert filled_table.search({"data": {"$in": values}}, "n", limit=5) == [5, 6]
    assert filled_table.count({"data": {"$nin": values}}) == 198


def test_jsonb_contains_and_containedin(filled_table):
    assert filled_table.count({"data": {"$contains": {"s": "v1"}}}) == 29
    assert filled_table.count({"data.s": {"$containedin": ["v1", "v2"]}}) == 58


def test_jsonb_overlaps_raises(filled_table):
    with pytest.raises(ValueError):
        filled_table.count({"data": {"$overlaps": ["v1"]}})


def test_jsonb_exists(filled_table, nullable_table):
    assert filled_table.count({"data.s": {"$exists": True}}) == 200
    assert filled_table.count({"data.missing": {"$exists": True}}) == 0
    assert nullable_table.count({"data": {"$exists": False}}) == 1
    assert nullable_table.count({"data": {"$exists": True}}) == 1


@pytest.mark.xfail(
    strict=True,
    reason="insert_many wraps jsonb values in Json unconditionally, so an explicit None is stored as the JSON value null rather than SQL NULL",
)
def test_jsonb_explicit_none_is_stored_as_sql_null(table_factory):
    table = table_factory()
    table.insert_many([{"n": 0, "label": "a", "data": None}])
    # the value reads back as None, so it should also be searchable as one
    assert table.lucky({"n": 0}, ["data"]) == {}
    assert table.count({"data": None}) == 1
    assert table.count({"data": {"$exists": False}}) == 1


##################################################################
# type round-tripping                                            #
##################################################################

def test_numeric_and_float_roundtrip(filled_table, nullable_table):
    integral = filled_table.lucky({"n": 3}, ["num", "mat", "x"])
    assert integral == {"num": 37, "mat": [3, 9], "x": 1.5}
    assert isinstance(integral["num"], int)
    assert isinstance(integral["x"], float)
    # numeric values are converted to int or float, never to Decimal
    fractional = nullable_table.lucky({"n": 0}, ["num", "mat", "x"])
    assert fractional == {"num": 1.25, "mat": [0.5, 2], "x": 0.5}
    assert isinstance(fractional["num"], float)
    assert isinstance(fractional["mat"][1], int)


def test_nulls_are_omitted_from_results(nullable_table, monkeypatch):
    assert nullable_table.lucky({"n": 1}, 1) == {"n": 1, "label": "b"}
    assert list(nullable_table.search({"n": 1})) == [{"n": 1, "label": "b"}]
    # unless the table asks for them
    monkeypatch.setattr(nullable_table, "_include_nones", True)
    record = nullable_table.lucky({"n": 1}, 1)
    assert sorted(record) == ["data", "flag", "label", "mat", "n", "num", "vec", "x"]
    assert record["num"] is None
    assert record["vec"] is None


##################################################################
# random selection                                               #
##################################################################

def test_random_returns_a_row_of_the_table(filled_table):
    label = filled_table.random()
    assert filled_table.label_exists(label)
    record = filled_table.random({"n": 4}, 1)
    assert record == sample_row(4)


def test_random_with_query(filled_table):
    assert filled_table.random({"n": {"$lt": 5}}) in ["l0", "l1", "l2", "l3", "l4"]
    assert filled_table.random({"n": -1}) is None
    assert filled_table.random({"n": {"$lt": 5}}, pick_first="flag") in ["l0", "l1", "l2", "l3", "l4"]


@pytest.mark.xfail(
    strict=True,
    reason="random() on an empty table calls random.randint(0, -1) because max_id() returns -1 rather than 0",
)
def test_random_on_empty_table_returns_none(empty_table):
    assert empty_table.random() is None


def test_random_sample_choice_mode(filled_table):
    sample = filled_table.random_sample(
        0.5, {"n": {"$lt": 10}}, projection="n", mode="choice"
    )
    assert len(sample) == 5
    assert set(sample) <= set(range(10))


def test_random_sample_ratio_bounds(filled_table):
    assert sorted(filled_table.random_sample(1.0, {"n": {"$lt": 5}}, projection="n")) == [
        0, 1, 2, 3, 4
    ]
    with pytest.raises(ValueError):
        filled_table.random_sample(0, projection="n")
    with pytest.raises(ValueError):
        filled_table.random_sample(1.5, projection="n")


@pytest.mark.xfail(
    strict=True,
    reason="random_sample rebinds `repeatable` to an SQL object before calling int(repeatable)",
)
def test_random_sample_repeatable(filled_table):
    first = list(filled_table.random_sample(0.5, projection="n", mode="bernoulli", repeatable=42))
    second = list(filled_table.random_sample(0.5, projection="n", mode="bernoulli", repeatable=42))
    assert first == second
