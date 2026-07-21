# -*- coding: utf-8 -*-
"""
Tests for :mod:`psycodict.encoding`.

Nothing here needs a database: ``numeric_converter`` is the loader Postgres
calls on ``numeric`` values, ``Json`` is the jsonb adapter/loader pair, and
``copy_dumps`` produces the text that ``COPY FROM`` parses, so all three can be
exercised on plain Python objects and strings.
"""
import datetime
import json

import pytest

import psycodict.encoding
from psycodict.encoding import SAGE_MODE, Array, Json, copy_dumps, numeric_converter


# COPY FROM applies these escapes to every text field it reads; undoing them is
# how we check that what copy_dumps writes is what Postgres will read back.
_COPY_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\",
}


def copy_unescape(text):
    """
    Undo the backslash escaping that ``COPY FROM`` performs on a text field.
    """
    out = []
    chars = iter(text)
    for char in chars:
        if char == "\\":
            nxt = next(chars)
            # Postgres: an unrecognised backslash escape stands for itself.
            out.append(_COPY_ESCAPES.get(nxt, nxt))
        else:
            out.append(char)
    return "".join(out)


NASTY_STRINGS = [
    "plain",
    "tab\there",
    "newline\nhere",
    "back\\slash",
    'double"quote',
    "all\tof\\them\"at\nonce",
    "",
]


# ---------------------------------------------------------------------------
# numeric_converter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("0", 0),
    ("5", 5),
    ("-3", -3),
    ("1" + "0" * 40 + "7", int("1" + "0" * 40 + "7")),
])
def test_numeric_converter_integers(value, expected):
    result = numeric_converter(value)
    assert result == expected
    # An integral numeric must not go through float, which would lose digits.
    assert not isinstance(result, float)


@pytest.mark.parametrize("value,expected", [
    ("1.5", 1.5),
    ("-2.25", -2.25),
    (".5", 0.5),
    ("1.", 1.0),
    ("1.5e3", 1500.0),
])
def test_numeric_converter_decimals(value, expected):
    assert numeric_converter(value) == expected


@pytest.mark.parametrize("value,prec", [
    # the g2c regulator from LMFDB/lmfdb#3569: 12 significant digits (40
    # bits), where counting all 16 characters gave 54 bits and printed
    # phantom digits
    ("0.00459244230167", 40),
    ("123.456", 20),
    # trailing zeros were stored, hence are significant
    ("1.500", 14),
    # the sign, the decimal point and leading zeros carry no information
    ("-0.5", 4),
    ("0.5", 4),
])
def test_numeric_precision_counts_significant_digits(value, prec):
    from psycodict.encoding import numeric_precision

    assert numeric_precision(value) == prec


def test_decimal_zero_is_float_zero_without_sage():
    assert numeric_converter("0.000") == 0.0
    assert isinstance(numeric_converter("-0.0"), float)


def test_decimal_zero_is_exact_and_does_not_degrade_sage_arithmetic():
    # Sage coerces a sum to the lowest precision of its operands, so a
    # floating zero of ANY fixed precision would drag higher-precision
    # partners down to it (a 2-bit zero turned 123.456 + 0.000 into a
    # 2-bit 130.).  An exact zero coerces into the partner's field instead.
    sage_integer = pytest.importorskip("sage.rings.integer")
    from psycodict.encoding import numeric_converter

    z = numeric_converter("0.000")
    assert isinstance(z, sage_integer.Integer)
    assert z == 0
    # the literal is preserved for printing and serialization
    assert str(z) == "0.000"
    assert repr(z) == "0.000"
    for value in ("123.456", "1.23456789012345678901234567890"):
        x = numeric_converter(value)
        total = x + z
        assert total.parent().precision() == x.parent().precision()
        assert total == x


def test_numeric_converter_none_and_unused_cursor():
    assert numeric_converter(None) is None
    assert numeric_converter(None, cur=object()) is None
    assert numeric_converter("7", cur=object()) == 7


def test_numeric_converter_dispatches_on_the_decimal_point():
    # The branch is chosen by the presence of ".", not by the value, so equal
    # numbers written differently come back as different types.
    assert numeric_converter("10") == numeric_converter("10.0")
    assert type(numeric_converter("10")) is not type(numeric_converter("10.0"))


def test_numeric_converter_needs_a_decimal_point_for_exponents():
    # A documented limitation rather than a live bug: Postgres' numeric output
    # never uses exponent notation, so "1e5" cannot arrive from a numeric
    # column.  It fails loudly rather than silently truncating -- with
    # ValueError from int() without Sage, TypeError from Integer() with it.
    with pytest.raises((ValueError, TypeError)):
        numeric_converter("1e5")


# encoding.py wraps its Sage imports in try/except ImportError and behaves
# differently on each side of the guard, so each side gets its own tests: the
# unit CI jobs run this file without Sage, and the downstream LMFDB job runs
# the same file again under ``sage -python``.


@pytest.mark.skipif(SAGE_MODE, reason="tests the sage-free fallback")
def test_without_sage_falls_back_to_builtin_types():
    assert not hasattr(psycodict.encoding, "LmfdbRealLiteral")
    assert not hasattr(psycodict.encoding, "RealEncoder")
    # The module still converts, using builtin types -- at the price of
    # rounding fractional values to float precision.
    assert type(numeric_converter("5")) is int
    assert type(numeric_converter("5.5")) is float


@pytest.mark.skipif(not SAGE_MODE, reason="needs SageMath")
def test_with_sage_numeric_converter_is_exact():
    from sage.rings.integer import Integer

    from psycodict.encoding import LmfdbRealLiteral

    big = numeric_converter("368947264000000000000000000")
    assert isinstance(big, Integer)
    assert big == 368947264000000000000000000
    # The entire point of LmfdbRealLiteral: a fractional value prints exactly
    # as Postgres sent it, digit for digit, far beyond float precision.
    digits = "3.14159265358979323846264338327950288419716939937510"
    real = numeric_converter(digits)
    assert isinstance(real, LmfdbRealLiteral)
    assert str(real) == digits


@pytest.mark.skipif(not SAGE_MODE, reason="needs SageMath")
def test_with_sage_copy_dumps_round_trips_sage_types():
    from sage.rings.integer import Integer

    # Sage Integers take the same exact-string path as Python ints ...
    assert copy_dumps(Integer(2) ** 100, "numeric") == str(2**100)
    # ... and a RealLiteral is written back as its literal, so a value read
    # from a numeric column survives a copy_to/copy_from cycle unchanged.
    digits = "3.14159265358979323846264338327950288419716939937510"
    assert copy_dumps(numeric_converter(digits), "numeric") == digits


# ---------------------------------------------------------------------------
# Json
# ---------------------------------------------------------------------------

def test_json_wrapper_contract():
    # With psycopg2 this class subclassed psycopg2.extras.Json; under psycopg3
    # it is a plain wrapper adapted by JsonWrapperDumper.  The wrapped value
    # stays reachable under both spellings.
    wrapped = Json({"a": 1})
    assert wrapped.obj == {"a": 1}
    assert wrapped.adapted == {"a": 1}  # psycopg2-era attribute, kept for compatibility
    assert repr(wrapped) == "Json({'a': 1})"


@pytest.mark.parametrize("obj", [
    {},
    [1, 2, 3],
    {"a": [1, 2], "b": {"c": "d"}},
    "a string",
    None,
    1.5,
])
def test_json_dumps_roundtrips_plain_types(obj):
    assert json.loads(Json.dumps(obj)) == obj


def test_json_dumps_produces_standard_json_text():
    assert Json.dumps({"a": 1}) == '{"a": 1}'
    # bool must not degrade to int even though it is a subclass of int
    assert Json.dumps([True, False, None, 1]) == "[true, false, null, 1]"
    # and large integers are not routed through float
    big = 2 ** 100 + 1
    assert Json.dumps({"n": big}) == '{"n": %d}' % big


@pytest.mark.parametrize("obj,expected", [
    ((1, 2), [1, 2]),
    ({"a": (1, (2, 3))}, {"a": [1, [2, 3]]}),
    (1 + 2j, {"__complex__": 0, "data": [1.0, 2.0]}),
    ([1 + 2j, 3 - 1j], {"__ComplexList__": 0, "data": [[1.0, 2.0], [3.0, -1.0]]}),
    ({1: "a", 2: "b"}, {"__IntDict__": 0, "data": [[1, "a"], [2, "b"]]}),
    (datetime.date(2020, 1, 2), {"__date__": 0, "data": "2020-01-02"}),
    # the special encodings are all guarded by a truthiness check on the input
    ({}, {}),
    ([], []),
])
def test_json_prep_encodings(obj, expected):
    assert Json.prep(obj) == expected


@pytest.mark.parametrize("obj", [
    1 + 2j,
    [1 + 2j, 3j],
    {1: "a", 2: "b"},
    datetime.date(2020, 1, 2),
    datetime.time(12, 30, 0, 5),
    {"a": 1, "b": [1, 2], "c": {"d": "e"}},
    [1, "two", None, True, {"x": 1.5}],
])
def test_json_roundtrips_through_dumps_and_loads(obj):
    assert Json.loads(Json.dumps(obj)) == obj


@pytest.mark.parametrize("obj,error,match", [
    ({1: "a", "b": 2}, TypeError, "keys must be strings or integers"),
    ({1, 2}, ValueError, "Unsupported type"),
    (b"bytes", ValueError, "Unsupported type"),
    (object(), ValueError, "Unsupported type"),
])
def test_json_prep_rejects_what_it_cannot_encode(obj, error, match):
    with pytest.raises(error, match=match):
        Json.prep(obj)


def test_json_prep_escape_backslashes():
    original = "a\tb\\c\"d\ne\rf"
    assert Json.prep(original) == original
    assert Json.prep(original, escape_backslashes=True) == r'a\tb\\c\"d\ne\rf'


@pytest.mark.parametrize("obj", [{"a": 1 + 2j}, [[1 + 2j]]])
def test_json_roundtrips_nested_special_types(obj):
    # extract recurses into lists and dicts just as prep does
    assert Json.loads(Json.dumps(obj)) == obj


@pytest.mark.skipif(not SAGE_MODE, reason="needs SageMath")
def test_with_sage_json_roundtrips_nested_sage_types():
    from sage.all import QQ, RealField, vector

    for obj in [
        # a top-level Rational exercises the extract branch that must not
        # pass the denominator to Rational as its base argument
        QQ(3) / 7,
        {"r": QQ(3) / 7},
        {"v": vector(QQ, [1, QQ(1) / 2])},
        {"outer": [{"deep": RealField(100)("1.5")}]},
    ]:
        assert Json.loads(Json.dumps(obj)) == obj


def test_json_roundtrips_datetimes():
    # datetime.datetime is a subclass of datetime.date, so prep has to
    # dispatch on datetime first for the __datetime__ tag to be reachable
    stamp = datetime.datetime(2020, 1, 2, 3, 4, 5, 6)
    assert Json.loads(Json.dumps(stamp)) == stamp


def test_json_roundtrips_times_without_microseconds():
    # prep writes no fractional part when the microseconds are zero, and
    # extract accepts both spellings
    noon = datetime.time(12, 30)
    assert Json.loads(Json.dumps(noon)) == noon


@pytest.mark.parametrize("value", [
    datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc),
    datetime.datetime(2020, 1, 2, 3, 4, 5, 6,
                      tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))),
    datetime.time(12, 30, tzinfo=datetime.timezone.utc),
    datetime.time(23, 59, 59, 999999,
                  tzinfo=datetime.timezone(datetime.timedelta(hours=-8))),
])
def test_json_roundtrips_timezone_aware_values(value):
    # prep serializes aware values with their UTC offset (str() keeps it);
    # extract must parse that too, offset preserved, not just the naive forms
    decoded = Json.loads(Json.dumps(value))
    assert decoded == value
    assert decoded.utcoffset() == value.utcoffset()


# ---------------------------------------------------------------------------
# copy_dumps
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typ", ["text", "integer", "jsonb", "integer[]"])
def test_copy_dumps_none_is_the_copy_null_marker(typ):
    assert copy_dumps(None, typ) == r"\N"


@pytest.mark.parametrize("typ", ["text", "char", "varchar"])
def test_copy_dumps_text_escapes_control_characters(typ):
    assert copy_dumps("a\tb\nc\\d\"e\rf", typ) == r'a\tb\nc\\d\"e\rf'


def test_copy_dumps_text_coerces_and_quotes_braces():
    assert copy_dumps(17, "text") == "17"
    # Braces would otherwise be read as delimiters by the array parser, but
    # only when this value is an array element.
    assert copy_dumps("a{b}", "text", recursing=True) == '"a{b}"'
    assert copy_dumps("a{b}", "text") == "a{b}"


@pytest.mark.parametrize("value", NASTY_STRINGS)
def test_copy_dumps_text_survives_copy_unescaping(value):
    assert copy_unescape(copy_dumps(value, "text")) == value


@pytest.mark.parametrize("value,expected", [
    (0, "0"),
    (-5, "-5"),
    (1.5, "1.5"),
    (10 ** 30, "1" + "0" * 30),
])
def test_copy_dumps_numbers(value, expected):
    assert copy_dumps(value, "numeric") == expected


def test_copy_dumps_jsonb_escapes_twice():
    # prep escapes for COPY and json.dumps escapes for JSON; both layers have
    # to be undone in turn for the value to arrive intact.
    assert copy_dumps("a\tb", "jsonb") == r'"a\\tb"'
    assert copy_dumps({"a": [1, 2]}, "jsonb") == '{"a": [1, 2]}'


@pytest.mark.parametrize("obj", NASTY_STRINGS + [
    {"k": "a\tb\\c\"d", "l": [1, 2]},
    {"nested": {"deep": ["x\ny"]}},
    [1, 2, {"a": None}],
])
def test_copy_dumps_jsonb_survives_copy_then_json(obj):
    assert json.loads(copy_unescape(copy_dumps(obj, "jsonb"))) == obj


@pytest.mark.parametrize("value,typ,expected", [
    ([], "integer[]", "{}"),
    ([1, 2, 3], "integer[]", "{1,2,3}"),
    ((1, 2), "integer[]", "{1,2}"),
    ([[1, 2], [3, 4]], "integer[]", "{{1,2},{3,4}}"),
    ([[]], "integer[]", "{{}}"),
    (["a", "b"], "text[]", "{a,b}"),
    (["a{b"], "text[]", '{"a{b"}'),
    ([1.5, 2.5], "numeric[]", "{1.5,2.5}"),
])
def test_copy_dumps_arrays(value, typ, expected):
    assert copy_dumps(value, typ) == expected


@pytest.mark.parametrize("value,error,match", [
    (5, TypeError, "list or tuple"),
    ([[1, 2], [3]], ValueError, "Array dimensions must be uniform"),
    ([1, [2]], ValueError, "Array dimensions must be uniform"),
    ([[1], 2], ValueError, "Array dimensions must be uniform"),
])
def test_copy_dumps_rejects_bad_array_shapes(value, error, match):
    with pytest.raises(error, match=match):
        copy_dumps(value, "integer[]")


def test_copy_dumps_boolean_from_non_numeric_input():
    assert copy_dumps("yes", "boolean") == "t"
    assert copy_dumps("", "boolean") == "f"


@pytest.mark.parametrize("value,typ,expected", [
    (datetime.date(2020, 1, 2), "date", "2020-01-02"),
    (datetime.time(3, 4, 5), "time", "03:04:05"),
    (datetime.datetime(2020, 1, 2, 3, 4, 5), "timestamp", "2020-01-02 03:04:05"),
])
def test_copy_dumps_dates_and_times(value, typ, expected):
    assert copy_dumps(value, typ) == expected


def test_copy_dumps_rejects_unsupported_types():
    with pytest.raises(TypeError) as excinfo:
        copy_dumps({1, 2}, "integer")
    message = str(excinfo.value)
    assert "Invalid input" in message and "set" in message and "integer" in message


@pytest.mark.parametrize("value,expected", [
    (["a,b"], '{"a,b"}'),       # unquoted, the comma would split the element
    ([""], '{""}'),             # unquoted, this would be the empty array
    ([" a"], '{" a"}'),         # unquoted, Postgres would trim the space
    (["NULL"], '{"NULL"}'),     # unquoted, this would be an SQL NULL
    # backslashes and double quotes are escaped once for the array parser and
    # once more for COPY's field-level unescaping, while a tab is a plain
    # character to the array parser and escaped only at the field level
    (['he said "hi"'], r'{"he said \\\"hi\\\""}'),
    (["back\\slash"], r'{"back\\\\slash"}'),
    (["tab\there"], r'{"tab\there"}'),
])
def test_copy_dumps_quotes_array_elements_that_need_it(value, expected):
    assert copy_dumps(value, "text[]") == expected


def test_copy_dumps_writes_null_array_elements_as_null():
    # the field-level marker \N would be unescaped by COPY to the letter N,
    # so inside an array a None must become the array literal NULL
    assert copy_dumps([None, 1], "integer[]") == "{NULL,1}"


@pytest.mark.parametrize("value,expected", [(True, "t"), (False, "f")])
def test_copy_dumps_booleans(value, expected):
    # bool is a subclass of int, so the boolean branch has to come before
    # the numeric one
    assert copy_dumps(value, "boolean") == expected


def test_copy_dumps_bytea():
    assert copy_dumps(b"abc", "bytea") == r"\\x616263"


# ---------------------------------------------------------------------------
# Array
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seq,expected", [
    ([1, 2, 3], b"{1,2,3}"),
    ([], b"{}"),
    (["a", "b"], b'{"a","b"}'),
    ([1.5, None, True], b"{1.5,NULL,t}"),
    ([[1, 2], [3, 4]], b"{{1,2},{3,4}}"),
    (['he said "hi"', "back\\slash"], b'{"he said \\"hi\\"","back\\\\slash"}'),
])
def test_array_getquoted(seq, expected):
    # Under psycopg2 this produced an ARRAY[...] expression spliced into the
    # SQL client-side; with server-side binding the wrapper renders a postgres
    # array literal instead (sent with unknown oid, so the server casts by
    # context, via ArrayDumper).
    assert Array(seq).getquoted() == expected
