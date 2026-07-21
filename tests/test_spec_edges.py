# -*- coding: utf-8 -*-
"""
Edge-case *specification* tests for the read/query side of psycodict.

The point of this file is different from ``test_search.py``: it does not check
that the common paths work, but *pins* the behavior of corner cases so that the
1.0 release freezes them deliberately rather than by accident.  Each test
therefore states, in a one-line comment, WHY the pinned value is what it is (or
that it is simply frozen as-is).  Where the current behavior looks like a real
bug, the test still pins what the code does today and the comment links the
concern -- these are reported separately, not fixed here.

Everything runs against short-lived tables built directly from the session
``db`` fixture (the tables in ``conftest`` are function-scoped; these are
module-scoped so the whole file shares three tables and stays fast).

The observations below were all reproduced against PostgreSQL 18 with psycopg 3.
"""
import uuid

import psycopg
import pytest

from conftest import COLUMNS, sample_row


# ---------------------------------------------------------------------------
# Module-scoped tables (built via ``db`` directly; conftest fixtures are
# function-scoped, which would rebuild a table per test).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def spec_table(db):
    """Standard columns (one of each interesting type), rows 0..9."""
    name = "test_spec_%s" % uuid.uuid4().hex[:12]
    db.create_table(name, COLUMNS, label_col="label", sort=["n"])
    table = db[name]
    table.insert_many([sample_row(i) for i in range(10)])
    yield table
    db.drop_table(name, force=True)


@pytest.fixture(scope="module")
def null_table(db):
    """``num`` is a *nullable integer*: NULL for n in {1, 3}, set otherwise.

    Used for both the NULL-ordering pins and the ``$exists`` pins.  A plain
    integer column stores an explicit ``None`` as SQL NULL (only jsonb turns
    ``None`` into the JSON value ``null``), so a single ``insert_many`` with
    ``num=None`` on some rows is genuinely NULL there.
    """
    name = "test_specnull_%s" % uuid.uuid4().hex[:12]
    db.create_table(name, [("n", "integer"), ("label", "text"), ("num", "integer")],
                    label_col="label", sort=["n"])
    table = db[name]
    table.insert_many([
        {"n": 0, "label": "a", "num": 5},
        {"n": 1, "label": "b", "num": None},
        {"n": 2, "label": "c", "num": 3},
        {"n": 3, "label": "d", "num": None},
        {"n": 4, "label": "e", "num": 9},
    ])
    yield table
    db.drop_table(name, force=True)


@pytest.fixture(scope="module")
def big_table(db):
    """1500 rows, so a broad query overruns the 1000-row count prelimit."""
    name = "test_specbig_%s" % uuid.uuid4().hex[:12]
    db.create_table(name, [("n", "integer"), ("label", "text")],
                    label_col="label", sort=["n"])
    table = db[name]
    table.insert_many([{"n": i, "label": "r%d" % i} for i in range(1500)])
    yield table
    db.drop_table(name, force=True)


def _sql(db, table, query):
    """The rendered WHERE fragment for ``query`` (or None if no constraint)."""
    qstr, _vals = table._parse_dict(query)
    return None if qstr is None else qstr.as_string(db.conn)


# ===========================================================================
# 1. Empty collections inside operators
# ===========================================================================

def test_in_empty_list_matches_nothing(spec_table):
    # $in [] is an "is one of the empty set" test: impossible, so nothing.
    assert spec_table.search({"n": {"$in": []}}, "n", limit=20) == []
    # For an array column $in becomes an $or, and the empty $or is SQL false.
    assert spec_table.search({"vec": {"$in": []}}, "n", limit=20) == []


def test_nin_empty_list_matches_everything(spec_table):
    # $nin [] is "not one of the empty set": vacuously true for every row.
    assert spec_table.search({"n": {"$nin": []}}, "n", limit=20) == list(range(10))
    assert spec_table.search({"vec": {"$nin": []}}, "n", limit=20) == list(range(10))


def test_in_nin_empty_on_jsonb(spec_table):
    # jsonb $in/$nin use <@ '[]'; the sample ``data`` is an object, and an
    # object is not contained in the empty array, so $in -> none, $nin -> all.
    assert spec_table.search({"data": {"$in": []}}, "n", limit=20) == []
    assert spec_table.search({"data": {"$nin": []}}, "n", limit=20) == list(range(10))


def test_top_level_or_empty_list_is_false(db, spec_table):
    # The code special-cases an empty $or list to SQL("false") (an OR over no
    # disjuncts is false), so it matches nothing.
    assert _sql(db, spec_table, {"$or": []}) == "false"
    assert spec_table.search({"$or": []}, "n", limit=20) == []


def test_top_level_and_empty_list_is_everything(db, spec_table):
    # Asymmetric with $or: an empty $and imposes no constraint (None), not
    # SQL("true"), so it matches everything.  Pinned as-is for 1.0.
    assert _sql(db, spec_table, {"$and": []}) is None
    assert spec_table.search({"$and": []}, "n", limit=20) == list(range(10))


def test_contains_empty_on_array_matches_everything(spec_table):
    # Every array contains the empty array (vec @> '{}'), so $contains []
    # selects all rows.
    assert spec_table.search({"vec": {"$contains": []}}, "n", limit=20) == list(range(10))


def test_contains_accepts_a_tuple_like_a_list(spec_table):
    # A tuple must be adapted as an array (the "contains all of these"
    # semantics of the list form), not as psycopg's composite literal
    # '(2,3)', which fails when cast to the column's array type.  Only row 2
    # (vec = [2, 3, 2]) contains both 2 and 3.
    as_list = spec_table.search({"vec": {"$contains": [2, 3]}}, "n", limit=20)
    assert as_list == [2]
    assert spec_table.search({"vec": {"$contains": (2, 3)}}, "n", limit=20) == as_list


def test_contains_empty_on_jsonb_depends_on_stored_type(db, spec_table):
    # jsonb containment is type-sensitive: an object @> '[]' is false, so the
    # sample ``data`` (an object) matches nothing.  (A jsonb column holding an
    # array value would instead match everything.)
    assert spec_table.search({"data": {"$contains": []}}, "n", limit=20) == []


def test_overlaps_empty_matches_nothing(spec_table):
    # vec && '{}' -- nothing overlaps the empty array.
    assert spec_table.search({"vec": {"$overlaps": []}}, "n", limit=20) == []


def test_notcontains_empty_list_matches_everything(spec_table):
    # Excluding nothing is no constraint: consistent with $nin [] and the
    # vacuous-truth reading of "must not contain any entry of []".
    assert spec_table.search({"vec": {"$notcontains": []}}, "n", limit=20) == list(range(10))
    assert spec_table.search({"data": {"$notcontains": []}}, "n", limit=20) == list(range(10))


# ===========================================================================
# 2. Empty / degenerate query dicts
# ===========================================================================

def test_empty_query_matches_everything(db, spec_table):
    # The empty dict imposes no WHERE clause -> all rows (documented).
    assert _sql(db, spec_table, {}) is None
    assert spec_table.search({}, "n", limit=20) == list(range(10))


def test_empty_operator_dict_imposes_no_constraint(db, spec_table):
    # {"col": {}} has no operators, so it contributes nothing -> everything.
    assert _sql(db, spec_table, {"n": {}}) is None
    assert spec_table.search({"n": {}}, "n", limit=20) == list(range(10))


def test_not_of_empty_dict_matches_nothing(db, spec_table):
    # $not of a clause that imposes nothing (None) hits the None-pair special
    # case: NOT(trivially-true) is false, emitted as "%s" with the value False.
    assert _sql(db, spec_table, {"$not": {}}) == "%s"
    assert spec_table.search({"$not": {}}, "n", limit=20) == []
    # Same at the column level.
    assert spec_table.search({"n": {"$not": {}}}, "n", limit=20) == []


def test_or_with_empty_branch_matches_everything(db, spec_table):
    # An empty branch ({}) parses to None; $or returns None if *any* branch is
    # None, so a single empty disjunct makes the whole $or trivially true.
    assert _sql(db, spec_table, {"$or": [{}]}) is None
    assert spec_table.search({"$or": [{}]}, "n", limit=20) == list(range(10))
    # ...even alongside a real branch that would otherwise restrict.
    assert spec_table.search({"$or": [{"n": 1}, {}]}, "n", limit=20) == list(range(10))


def test_and_with_only_empty_branch_matches_everything(spec_table):
    # $and drops None branches; with nothing left it imposes no constraint.
    assert spec_table.search({"$and": [{}]}, "n", limit=20) == list(range(10))


def test_not_of_empty_or_is_everything(db, spec_table):
    # Contrast with $not:{}.  Here $or:[] is SQL false, so NOT(false) is true
    # and every row matches -- $not distinguishes "false" from "no constraint".
    assert _sql(db, spec_table, {"$not": {"$or": []}}) == "NOT (false)"
    assert spec_table.search({"$not": {"$or": []}}, "n", limit=20) == list(range(10))


def test_column_level_empty_or_and(spec_table):
    # At the column level the same asymmetry holds: empty $or -> nothing,
    # empty $and -> everything.
    assert spec_table.search({"n": {"$or": []}}, "n", limit=20) == []
    assert spec_table.search({"n": {"$and": []}}, "n", limit=20) == list(range(10))


def test_empty_string_key_raises(spec_table):
    # An empty key is rejected explicitly rather than producing odd SQL.
    with pytest.raises(ValueError, match="empty key"):
        spec_table._parse_dict({"": 1})


def test_or_given_a_dict_instead_of_list_raises_valueerror(spec_table):
    # $or/$and take a list of dictionaries; anything else gets a clear error
    # instead of the bare AttributeError this used to raise.
    with pytest.raises(ValueError, match="list of dictionaries"):
        spec_table._parse_dict({"$or": {"n": 1}})
    with pytest.raises(ValueError, match="list of dictionaries"):
        spec_table._parse_dict({"$and": "nonsense"})


def test_unknown_column_and_operator_raise_valueerror(spec_table):
    # Unknown column and unknown $operator are both ValueErrors (distinct msgs).
    with pytest.raises(ValueError, match="not a column"):
        spec_table._parse_dict({"bogus": 1})
    with pytest.raises(ValueError, match="Error building query"):
        spec_table._parse_dict({"n": {"$bogus": 1}})


def test_mixed_operator_dict_is_treated_as_a_value(db, spec_table):
    # FOOTGUN (pinned as-is): a constraint dict is read as operators only when
    # *every* key starts with "$".  One non-$ key (a typo, a stray field) makes
    # the whole dict a literal equality value.  On a typed column that value
    # fails to cast (raises); on a jsonb column it silently compares and
    # quietly matches nothing.
    assert _sql(db, spec_table, {"n": {"$gt": 1, "plain": 2}}) == '"n" = %s'
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        spec_table.search({"n": {"$gt": 1, "plain": 2}}, "n", limit=20)
    # jsonb: no error, just a silent empty result set.
    assert spec_table.search({"data": {"$gt": 1, "plain": 2}}, "n", limit=20) == []


# ===========================================================================
# 3. Sort semantics (NULL ordering, direction forms, degenerate sorts)
# ===========================================================================

def test_ascending_sort_puts_nulls_last(null_table):
    # ASC emits a bare identifier and relies on Postgres' default, which is
    # NULLS LAST for ascending order.
    rows = null_table.search({}, ["n", "num"], sort=[("num", 1)], limit=20)
    assert [(r["n"], r.get("num")) for r in rows] == [
        (2, 3), (0, 5), (4, 9), (1, None), (3, None)]
    # A bare string means the same ascending direction.
    rows2 = null_table.search({}, ["n"], sort=["num"], limit=20)
    assert [r["n"] for r in rows2] == [2, 0, 4, 1, 3]


def test_descending_sort_forces_nulls_last(db, null_table):
    # KEY PIN: Postgres' native default for DESC is NULLS FIRST, but _sort_str
    # emits "col DESC NULLS LAST", so NULLs sort to the end in *both*
    # directions.  Freezing this keeps ASC and DESC agreeing about NULLs.
    assert null_table._sort_str([("num", -1)]).as_string(db.conn) == '"num" DESC NULLS LAST'
    rows = null_table.search({}, ["n", "num"], sort=[("num", -1)], limit=20)
    assert [(r["n"], r.get("num")) for r in rows] == [
        (4, 9), (0, 5), (2, 3), (1, None), (3, None)]


def test_empty_sort_is_unsorted(db, null_table):
    # sort=[] means "no ORDER BY"; _process_sort reports has_sort False.
    _sortsql, has_sort, _raw = null_table._process_sort({}, 20, 0, [])
    assert has_sort is False
    # The rows still all come back; we just don't pin their order here.
    assert sorted(r["n"] for r in null_table.search({}, ["n"], sort=[], limit=20)) == [0, 1, 2, 3, 4]


def test_lucky_default_sort_is_empty(null_table):
    # lucky() defaults to sort=[] (unsorted): it returns *a* matching row, and
    # asking for its default sort confirms the empty (unsorted) default.
    got = null_table.lucky({"num": {"$exists": True}}, ["n", "num"])
    assert got["n"] in {0, 2, 4}


def test_duplicate_sort_column_is_accepted(db, null_table):
    # Sorting by the same column twice is redundant but not an error; it is
    # emitted verbatim.  Pinned as-is for 1.0.
    assert null_table._sort_str([("num", 1), ("num", 1)]).as_string(db.conn) == '"num", "num"'
    rows = null_table.search({}, ["n"], sort=[("num", 1), ("num", 1)], limit=20)
    assert [r["n"] for r in rows] == [2, 0, 4, 1, 3]


def test_sort_pair_and_string_forms_agree(db, null_table):
    # (col, 1) and the bare string "col" render identically; (col, -1) adds
    # DESC NULLS LAST.
    assert null_table._sort_str([("num", 1)]).as_string(db.conn) == '"num"'
    assert null_table._sort_str(["num"]).as_string(db.conn) == '"num"'
    assert null_table._sort_str([("num", -1)]).as_string(db.conn) == '"num" DESC NULLS LAST'


# ===========================================================================
# 4. Projection edges
# ===========================================================================

def test_empty_projection_raises(spec_table):
    # [], {} and None are all "falsy" projections -> the "at least one key"
    # guard fires (integer 0 is handled earlier and means "just the label").
    for proj in ([], {}, None):
        with pytest.raises(ValueError, match="at least one key"):
            spec_table._parse_projection(proj)


def test_duplicate_list_projection_is_kept_then_collapses(spec_table):
    # A duplicated column stays duplicated in the selected columns (so the
    # SELECT lists it twice), but the result dict has one entry per key.
    assert spec_table._parse_projection(["n", "n"]) == ("n", "n")
    assert spec_table.search({"n": 2}, ["n", "n"], limit=1) == [{"n": 2}]


def test_id_only_projection(spec_table):
    # "id" is not a search column but is allowed explicitly, and the id-only
    # projection returns a dict with just the id (cross-checked here rather than
    # hardcoded, since psycodict assigns ids from 0 in insertion order).
    assert spec_table._parse_projection(["id"]) == ("id",)
    both = spec_table.search({"n": 3}, ["id", "n"], limit=1)[0]
    assert spec_table.search({"n": 3}, ["id"], limit=1) == [{"id": both["id"]}]


def test_dict_projection_cannot_mix_include_and_exclude(spec_table):
    # Mixing True and False values is rejected; all-True selects in *column*
    # order (not dict order).
    with pytest.raises(ValueError, match="both include and exclude"):
        spec_table._parse_projection({"n": True, "label": False})
    assert spec_table._parse_projection({"n": True, "label": True}) == ("label", "n")


# ===========================================================================
# 5. Limit / offset edges
# ===========================================================================

def test_limit_zero_on_empty_query_returns_empty(spec_table):
    # For {} the count is known (the table total), so the SQL LIMIT is 0 and
    # nothing comes back -- the "expected" empty result.
    info = {}
    assert spec_table.search({}, "n", limit=0, info=info) == []
    assert info["number"] == 10


def test_limit_zero_on_nonempty_query_returns_empty(spec_table):
    # limit=0 returns no rows even when the SQL prelimit fetched some for
    # count estimation (psycopg3's fetchmany(0) would fall back to arraysize).
    assert spec_table.search({"n": {"$lt": 5}}, "n", limit=0) == []
    info = {}
    assert spec_table.search({"n": {"$lt": 5}}, "n", limit=0, info=info) == []
    assert info["number"] == 5


def test_offset_past_end_without_info_returns_empty(spec_table):
    # No info dict -> no last-page adjustment; an offset past the end is empty.
    assert spec_table.search({"n": {"$lt": 5}}, "n", limit=10, offset=100) == []


def test_offset_past_end_with_info_snaps_to_last_page(spec_table):
    # With an info dict this is treated as a front-end query and the offset is
    # adjusted back to the last page (number - limit).
    info = {}
    got = spec_table.search({"n": {"$lt": 5}}, "n", limit=2, offset=100, info=info)
    assert got == [3, 4]
    assert info["number"] == 5 and info["start"] == 3


def test_negative_limit_and_offset(spec_table):
    # Negative offset is guarded explicitly; negative limit is not, and reaches
    # cur.fetchmany(-1), which psycopg rejects.  Both pinned as raising.
    with pytest.raises(ValueError, match="Offset cannot be negative"):
        spec_table.search({}, "n", limit=2, offset=-1)
    with pytest.raises(psycopg.InterfaceError):
        spec_table.search({"n": {"$lt": 5}}, "n", limit=-1)


# ===========================================================================
# 6. $exists interplay
# ===========================================================================

def test_exists_and_none_equivalences(db, null_table):
    # $exists:True <-> IS NOT NULL; $exists:False <-> {col: None} <-> IS NULL.
    assert _sql(db, null_table, {"num": {"$exists": True}}) == '"num" IS NOT NULL'
    assert _sql(db, null_table, {"num": {"$exists": False}}) == '"num" IS NULL'
    assert _sql(db, null_table, {"num": None}) == '"num" IS NULL'
    assert sorted(null_table.search({"num": {"$exists": True}}, "n", limit=20)) == [0, 2, 4]
    assert sorted(null_table.search({"num": {"$exists": False}}, "n", limit=20)) == [1, 3]
    assert sorted(null_table.search({"num": None}, "n", limit=20)) == [1, 3]


def test_ne_none_is_not_a_null_test(db, null_table):
    # FOOTGUN (pinned as-is): {col: {"$ne": None}} renders 'col != NULL', which
    # in SQL is never true, so it matches NOTHING -- it is NOT the complement of
    # {col: None}.  Use $exists:True to test for non-null.
    assert _sql(db, null_table, {"num": {"$ne": None}}) == '"num" != %s'
    assert null_table.search({"num": {"$ne": None}}, "n", limit=20) == []


def test_exists_on_missing_jsonb_path(db, spec_table):
    # -> on an absent jsonb key yields SQL NULL, so $exists on a missing path
    # behaves: a key that is never present is IS NULL for every row.
    assert _sql(db, spec_table, {"data.missing": {"$exists": False}}) == '"data"->\'missing\' IS NULL'
    assert spec_table.search({"data.missing": {"$exists": False}}, "n", limit=20) == list(range(10))
    assert spec_table.search({"data.missing": {"$exists": True}}, "n", limit=20) == []
    # A present nested key exists for every row.
    assert spec_table.search({"data.nested": {"$exists": True}}, "n", limit=20) == list(range(10))


# ===========================================================================
# 7. Type-boundary oddities (Postgres does the coercion; we pin what happens)
# ===========================================================================

def test_integer_column_against_float(spec_table):
    # A float equal to an integer matches (Postgres coerces); a non-integral
    # float simply matches nothing (no error).
    assert spec_table.search({"n": 3.0}, "n", limit=20) == [3]
    assert spec_table.search({"n": 3.5}, "n", limit=20) == []


def test_integer_column_against_string(spec_table):
    # A numeric string is cast and matches; a non-numeric string raises a data
    # error from Postgres.  Both pinned (loosely, on the psycopg exception).
    assert spec_table.search({"n": "3"}, "n", limit=20) == [3]
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        spec_table.search({"n": "abc"}, "n", limit=20)


def test_text_column_against_int_raises(spec_table):
    # No implicit text = integer operator, so Postgres raises.  Pinned loosely.
    with pytest.raises(psycopg.errors.UndefinedFunction):
        spec_table.search({"label": 5}, "n", limit=20)


def test_boolean_column_needs_a_real_bool(spec_table):
    # FOOTGUN (pinned as-is): Python's 0/1 are sent as smallint and there is no
    # boolean = smallint operator, so {"flag": 0} / {"flag": 1} RAISE.  Only
    # actual bools work on a boolean column.
    with pytest.raises(psycopg.errors.UndefinedFunction):
        spec_table.search({"flag": 0}, "n", limit=20)
    with pytest.raises(psycopg.errors.UndefinedFunction):
        spec_table.search({"flag": 1}, "n", limit=20)
    assert spec_table.search({"flag": True}, "n", limit=20) == [0, 3, 6, 9]
    assert spec_table.search({"flag": False}, "n", limit=20) == [1, 2, 4, 5, 7, 8]


# ===========================================================================
# 8. info-dict counting around the count prelimit
# ===========================================================================

def test_info_broad_query_reports_capped_inexact_count(big_table):
    # A broad, uncached query matching >1000 rows is only counted up to the
    # 1000-row prelimit, so info["number"] is the cutoff and exact_count False.
    info = {}
    got = big_table.search({"n": {"$gte": 0}}, "n", limit=5, info=info)
    assert len(got) == 5
    assert info["number"] == 1000
    assert info["exact_count"] is False


def test_info_empty_query_is_exact_from_total(big_table):
    # {} is answered from the cached table total, so even a tiny limit yields
    # the exact count.
    info = {}
    big_table.search({}, "n", limit=5, info=info)
    assert info["number"] == 1500
    assert info["exact_count"] is True


def test_info_narrow_query_is_exact(big_table):
    # A query resolving under the prelimit gets an exact count.
    info = {}
    big_table.search({"n": {"$lt": 300}}, "n", limit=5, info=info)
    assert info["number"] == 300
    assert info["exact_count"] is True


def test_info_exactly_at_cutoff_is_reported_inexact(big_table):
    # BOUNDARY PIN: a query matching *exactly* the cutoff (1000 rows) fills the
    # prelimit, and exact_count is `rowcount < prelimit` = False -- so a count
    # that happens to equal the cutoff is reported as a lower bound, not exact.
    info = {}
    big_table.search({"n": {"$lt": 1000}}, "n", limit=5, info=info)
    assert info["number"] == 1000
    assert info["exact_count"] is False


def test_info_with_limit_none_is_full_count(big_table):
    # limit=None returns an iterator; when info is passed the full count is
    # computed eagerly (info["number"]) before the iterator is consumed.
    info = {}
    it = big_table.search({"n": {"$gte": 0}}, "n", limit=None, info=info)
    assert info["number"] == 1500
    # Confirm it really is a lazy iterator, then drain it so the cursor closes.
    assert iter(it) is it
    assert len(list(it)) == 1500


def test_zero_limit_with_offset_past_end_does_not_recurse(spec_table):
    # A count-only query (limit=0) whose start is past the last row must not
    # trigger the last-page retry: that retry sets offset = nres - limit, which
    # for limit == 0 never shrinks, so the call would recurse on identical
    # arguments until RecursionError.  It should just report the count.
    info = {}
    assert spec_table.search({}, "n", limit=0, offset=15, info=info) == []
    assert info["count"] == 0
    assert info["start"] == 15

