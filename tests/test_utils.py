# -*- coding: utf-8 -*-
"""
Tests for :mod:`psycodict.utils`.

None of this needs a database.  ``psycopg2.sql`` composables cannot be rendered
to strings without a connection (``Identifier.as_string`` needs one to quote),
but they compare equal structurally, so the assertions below build the expected
composable and compare against it.
"""
import logging
from unittest.mock import MagicMock

import pytest
from psycopg2.sql import SQL, Composed, Identifier, Placeholder

from psycodict.utils import (
    DelayCommit,
    EmptyContext,
    IdentifierWrapper,
    KeyedDefaultDict,
    LockError,
    QueryLogFilter,
    SearchParsingError,
    filter_sql_injection,
    make_tuple,
    postgres_infix_ops,
    range_formatter,
)


class FakeTable:
    """
    Stand-in for a PostgresSearchTable: filter_sql_injection only reads the
    column whitelist and the table name (the latter for error messages).
    """

    search_table = "test_table"
    search_cols = ["n", "label", "dim1_factor"]


@pytest.fixture
def table():
    return FakeTable()


def parsed_pieces(clause):
    """
    The composables that filter_sql_injection built out of the user's text.

    The return value is ``Composed([col, SQL(" <op> "), Composed([...])])``.
    """
    return list(clause.seq[-1])


# The complete set of characters the docstring promises to allow through.
ALLOWED_OPERATOR_CHARS = set("+*-/^()")


# ---------------------------------------------------------------------------
# SearchParsingError, LockError
# ---------------------------------------------------------------------------

def test_search_parsing_error():
    error = SearchParsingError("bad input")
    assert isinstance(error, ValueError)
    assert str(error) == "bad input"
    assert error.trim_msg_error is False

    flagged = SearchParsingError("bad input", trim_msg_error=True)
    assert flagged.trim_msg_error is True
    # the keyword must not leak into the message
    assert str(flagged) == "bad input"


def test_lock_error_is_a_runtime_error():
    assert issubclass(LockError, RuntimeError)


# ---------------------------------------------------------------------------
# filter_sql_injection: accepted input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("clause_text,values", [
    ("n", []),
    ("label", []),
    ("dim1_factor", []),          # a column name containing digits
    ("n+1", [1]),
    ("2*n", [2]),
    ("n^2", [2]),
    ("(n+1)/2", [1, 2]),
    ("n-label", []),
    ("  n  +  1  ", [1]),         # whitespace is stripped, not rejected
    ("n+\xa01", [1]),           # \s strips unicode whitespace too
    ("1.5", [1.5]),
    (".5+1e3", [0.5, 1000.0]),
])
def test_filter_sql_injection_accepts_arithmetic_over_columns(table, clause_text, values):
    clause, got = filter_sql_injection(clause_text, Identifier("m"), "integer", "=", table)
    assert got == values
    assert isinstance(clause, Composed)


def test_filter_sql_injection_builds_the_expected_composable(table):
    clause, values = filter_sql_injection("n+1", Identifier("m"), "integer", "=", table)
    expected = SQL("{0} = {1}").format(
        Identifier("m"),
        SQL("").join([Identifier("n"), SQL("+"), Placeholder()]),
    )
    assert clause == expected
    assert values == [1]


@pytest.mark.parametrize("op", sorted(set(postgres_infix_ops.values()) | {"="}))
def test_filter_sql_injection_uses_the_requested_operator(table, op):
    clause, _ = filter_sql_injection("n", Identifier("m"), "integer", op, table)
    assert clause.seq[1] == SQL(" %s " % op)


def test_filter_sql_injection_separates_ints_from_floats(table):
    _, values = filter_sql_injection("n+1", Identifier("m"), "integer", "=", table)
    assert values == [1] and isinstance(values[0], int)
    _, values = filter_sql_injection("n+1.0", Identifier("m"), "numeric", "=", table)
    assert values == [1.0] and isinstance(values[0], float)
    _, values = filter_sql_injection("n+1e3", Identifier("m"), "numeric", "=", table)
    assert values == [1000.0] and isinstance(values[0], float)


@pytest.mark.parametrize("clause_text", ["n", "n+1", "(2*label)/n^2", "n-1.5"])
def test_filter_sql_injection_emits_nothing_but_vetted_pieces(table, clause_text):
    """
    The security invariant: no fragment of the user's text reaches the query
    except as a whitelisted column identifier or a bound placeholder.
    """
    clause, _ = filter_sql_injection(clause_text, Identifier("m"), "integer", "=", table)
    for piece in parsed_pieces(clause):
        assert isinstance(piece, (SQL, Identifier, Placeholder))
        if isinstance(piece, Identifier):
            assert piece.strings[0] in table.search_cols
        elif isinstance(piece, SQL):
            assert set(piece.string) <= ALLOWED_OPERATOR_CHARS


def test_filter_sql_injection_binds_numbers_rather_than_inlining_them(table):
    clause, values = filter_sql_injection("n+42", Identifier("m"), "integer", "=", table)
    assert values == [42]
    assert Placeholder() in parsed_pieces(clause)
    assert not any(
        isinstance(piece, SQL) and "42" in piece.string for piece in parsed_pieces(clause)
    )


# ---------------------------------------------------------------------------
# filter_sql_injection: rejected input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("clause_text", [
    "1; DROP TABLE users",          # statement separator
    "n; SELECT 1",
    "n -- comment",                 # SQL line comment
    "n /* comment */ 1",            # SQL block comment
    "n */ 1",
    "n' OR '1'='1",                 # single quote
    'n" OR "x',                     # double quote
    "1=1",                          # comparison operator
    "n<1",
    "n||'x'",                       # string concatenation
    "n%1",
    "n\\1",                         # backslash
    "n[1]",                         # subscript
    "n:1",                          # cast / slice punctuation
    "n@1",
    "$$n$$",                        # dollar quoting
])
def test_filter_sql_injection_rejects_forbidden_characters(table, clause_text):
    with pytest.raises(SearchParsingError, match="invalid characters"):
        filter_sql_injection(clause_text, Identifier("m"), "integer", "=", table)


@pytest.mark.parametrize("clause_text", [
    "bogus",                        # not a column of this table
    "N",                            # column matching is case sensitive
    "pg_sleep(10)",                 # function call
    "version()",
    "n.label",                      # qualified name
    "select",                       # bare keyword
    "\u0430",                  # Cyrillic 'a' is not an ASCII word character
    "\uff4e",                  # fullwidth 'n' must not fold to the column 'n'
    "n\u200b",                 # zero-width space is not stripped as whitespace
    "1e",                           # not a column and not a valid float
])
def test_filter_sql_injection_rejects_unknown_words(table, clause_text):
    with pytest.raises(SearchParsingError):
        filter_sql_injection(clause_text, Identifier("m"), "integer", "=", table)


def test_filter_sql_injection_error_message_names_the_table_and_the_clause(table):
    with pytest.raises(SearchParsingError) as excinfo:
        filter_sql_injection("bogus+1", Identifier("m"), "integer", "=", table)
    message = str(excinfo.value)
    assert "bogus" in message and "test_table" in message


def test_filter_sql_injection_whitelist_is_per_table(table):
    other = FakeTable()
    other.search_cols = ["something_else"]
    other.search_table = "other_table"
    with pytest.raises(SearchParsingError):
        filter_sql_injection("n", Identifier("m"), "integer", "=", other)


@pytest.mark.parametrize("clause", ["n,1", "n.1", "n,,1"])
def test_filter_sql_injection_rejects_characters_outside_the_whitelist(clause, table):
    # Written as [+*-/^()] the "*-/" is a character range covering * + , - . /,
    # so commas and periods used to pass the operator whitelist even though the
    # docstring and the error message both restrict it to +-*/^().  The clause
    # comes from the web UI, so this is a whitelist that has to be exact.
    with pytest.raises(SearchParsingError):
        filter_sql_injection(clause, Identifier("m"), "integer", "=", table)


# ---------------------------------------------------------------------------
# IdentifierWrapper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["name", "na.me", "we ird", 'qu"ote', "UPPER"])
def test_identifier_wrapper_passes_plain_names_to_identifier(name):
    # A dotted name is one identifier, not a qualified pair; psycopg2 does the
    # quoting at render time.
    wrapped = IdentifierWrapper(name)
    assert wrapped == Identifier(name)
    assert wrapped.strings == (name,)


@pytest.mark.parametrize("name,slicer", [
    ("name[1:10]", "[2:10]"),
    ("name[1:10:3]", "[2:10:3]"),
    ("name[1:10:3][0:2]", "[2:10:3][1:2]"),
    ("name[1:10:3][0]", "[2:10:3][1]"),
    ("name[1 : 10]", "[2:10]"),          # spaces inside the slicer are dropped
])
def test_identifier_wrapper_converts_python_slices_to_sql(name, slicer):
    assert IdentifierWrapper(name) == SQL("{0}{1}").format(Identifier("name"), SQL(slicer))


@pytest.mark.parametrize("name,slicer", [
    ("name[1:10]", "[1:10]"),
    ("name[1:10:3][0]", "[1:10:3][0]"),
])
def test_identifier_wrapper_convert_false_keeps_sql_indexing(name, slicer):
    assert IdentifierWrapper(name, convert=False) == SQL("{0}{1}").format(
        Identifier("name"), SQL(slicer)
    )


def test_identifier_wrapper_rejects_unbalanced_brackets():
    with pytest.raises(ValueError, match="not in the proper format"):
        IdentifierWrapper("name[1:10")


@pytest.mark.parametrize("name", [
    "name[1];DROP TABLE x]",
    "name[a]",
    "name[1)]",
    "name[]",
    "name[-1]",
    "name[1:10]; --",
])
def test_identifier_wrapper_rejects_non_numeric_slicers(name):
    with pytest.raises(ValueError):
        IdentifierWrapper(name)


def test_identifier_wrapper_rejects_non_numeric_slicers_with_value_error():
    with pytest.raises(ValueError):
        IdentifierWrapper("name[1];DROP TABLE x]")


@pytest.mark.parametrize("name,slicer", [
    ("name[:10]", "[:10]"),
    ("name[1:10:3][0::1]", "[2:10:3][1::1]"),
])
def test_identifier_wrapper_supports_open_ended_slices(name, slicer):
    assert IdentifierWrapper(name) == SQL("{0}{1}").format(Identifier("name"), SQL(slicer))


# ---------------------------------------------------------------------------
# QueryLogFilter
# ---------------------------------------------------------------------------

def log_record(pathname):
    return logging.LogRecord("psycodict", logging.WARNING, pathname, 1, "slow", None, None)


@pytest.mark.parametrize("pathname,expected", [
    ("/opt/psycodict/base.py", 1),
    ("base.py", 1),
    ("/opt/psycodict/table.py", 0),
    ("/opt/psycodict/searchtable.py", 0),
    ("/opt/elsewhere/app.py", 0),
])
def test_query_log_filter_only_passes_records_from_base(pathname, expected):
    assert QueryLogFilter().filter(log_record(pathname)) == expected


@pytest.mark.xfail(strict=True, reason="the filter tests pathname.endswith('base.py'), "
                                       "which is also true of psycodict's own "
                                       "database.py")
def test_query_log_filter_does_not_match_database_py():
    assert QueryLogFilter().filter(log_record("/opt/psycodict/database.py")) == 0


# ---------------------------------------------------------------------------
# EmptyContext
# ---------------------------------------------------------------------------

def test_empty_context_is_a_no_op_context_manager():
    # it stands in for an open file, so it has to carry a name
    assert EmptyContext.name is None
    context = EmptyContext()
    with context as value:
        assert value is None
    assert context.name is None
    # __exit__ returns None, so exceptions are not swallowed
    with pytest.raises(ZeroDivisionError):
        with EmptyContext():
            1 / 0


# ---------------------------------------------------------------------------
# DelayCommit
# ---------------------------------------------------------------------------

def make_owner():
    """
    An object shaped the way DelayCommit expects: it reaches through ``_db``
    for the commit stack, the silence flag and the connection.
    """
    owner = MagicMock()
    owner._db._nocommit_stack = 0
    owner._db._silenced = False
    return owner


def test_delay_commit_suppresses_commits_until_the_end():
    owner = make_owner()
    with DelayCommit(owner):
        assert owner._db._nocommit_stack == 1
        assert owner._db.conn.commit.call_count == 0
    assert owner._db._nocommit_stack == 0
    assert owner._db.conn.commit.call_count == 1
    assert owner._db.conn.rollback.call_count == 0


def test_delay_commit_nests_and_commits_once():
    owner = make_owner()
    with DelayCommit(owner):
        with DelayCommit(owner):
            assert owner._db._nocommit_stack == 2
        assert owner._db.conn.commit.call_count == 0
    assert owner._db.conn.commit.call_count == 1
    assert owner._db._nocommit_stack == 0


def test_delay_commit_rolls_back_on_exception():
    owner = make_owner()
    with pytest.raises(RuntimeError):
        with DelayCommit(owner):
            raise RuntimeError("boom")
    assert owner._db.conn.commit.call_count == 0
    assert owner._db.conn.rollback.call_count == 1
    assert owner._db._nocommit_stack == 0


def test_delay_commit_final_commit_false_leaves_the_commit_to_the_caller():
    owner = make_owner()
    with DelayCommit(owner, final_commit=False):
        pass
    assert owner._db.conn.commit.call_count == 0
    assert owner._db.conn.rollback.call_count == 0


def test_delay_commit_inactive_touches_nothing():
    owner = make_owner()
    with DelayCommit(owner, active=False):
        assert owner._db._nocommit_stack == 0
    assert owner._db._nocommit_stack == 0
    assert owner._db.conn.commit.call_count == 0
    assert owner._db.conn.rollback.call_count == 0


def test_delay_commit_restores_the_silence_flag_on_exit():
    owner = make_owner()
    owner._db._silenced = True
    with DelayCommit(owner):
        owner._db._silenced = False
    assert owner._db._silenced is True


@pytest.mark.xfail(strict=True, reason="silence= is written to obj._silenced but "
                                       "read (in base.py) and restored from "
                                       "obj._db._silenced, so it has no effect "
                                       "unless obj is the database itself")
def test_delay_commit_silence_applies_to_the_database():
    owner = make_owner()
    with DelayCommit(owner, silence=True):
        assert owner._db._silenced is True


# ---------------------------------------------------------------------------
# range_formatter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, "Unknown"),
    ({"$gte": 1, "$lte": 10}, "1-10"),
    ({"$gt": 0, "$lt": 10}, "1-9"),      # exclusive bounds are shifted
    ({"$gte": 5, "$lte": 5}, "5"),       # a degenerate range is a single value
    ({"$gte": 1}, "1-"),
    ({"$lte": 10}, "..10"),
    ({"$gt": 0}, "1-"),
    ({"$lt": 10}, "..9"),
    (5, "5"),
    ("abc", "abc"),
])
def test_range_formatter(value, expected):
    assert range_formatter(value) == expected


# ---------------------------------------------------------------------------
# KeyedDefaultDict
# ---------------------------------------------------------------------------

def test_keyed_default_dict_builds_missing_values_from_the_key():
    calls = []

    def factory(key):
        calls.append(key)
        return key * 2

    d = KeyedDefaultDict(factory)
    assert d[3] == 6
    assert d["ab"] == "abab"
    # the value is cached, so the factory runs once per key
    assert d[3] == 6
    assert calls == [3, "ab"]
    assert dict(d) == {3: 6, "ab": "abab"}
    # an explicitly stored value is never handed to the factory
    d["given"] = "kept"
    assert d["given"] == "kept"
    assert calls == [3, "ab"]


def test_keyed_default_dict_without_a_factory_raises_key_error():
    d = KeyedDefaultDict(None)
    with pytest.raises(KeyError) as excinfo:
        d[3]
    assert excinfo.value.args[0] == (3,)


# ---------------------------------------------------------------------------
# make_tuple
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (5, 5),
    ("s", "s"),
    (None, None),
    ([], ()),
    ({}, ()),
    ([1, [2, 3]], (1, (2, 3))),
    ((1, [2]), (1, (2,))),
    ({"a": [1, 2]}, (("a", (1, 2)),)),
    ({"a": {"b": 1}}, (("a", (("b", 1),)),)),
])
def test_make_tuple(value, expected):
    assert make_tuple(value) == expected


@pytest.mark.parametrize("value", [
    [1, [2, 3]],
    {"a": [1, 2], "b": {"c": [3]}},
])
def test_make_tuple_output_is_hashable(value):
    # the documented application: using the result as a dictionary key
    assert {make_tuple(value): "ok"}[make_tuple(value)] == "ok"
