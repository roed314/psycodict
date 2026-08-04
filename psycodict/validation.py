# -*- coding: utf-8 -*-
"""
What psycodict will put into a statement.

Almost all of psycodict's SQL is composed from bound placeholders and quoted
identifiers, which are safe whatever the values are.  The exceptions are the
things PostgreSQL has no placeholder for -- a column type above all -- which
have to be interpolated as SQL text.  This module is where those are checked,
in one place, so that the rule is the same wherever the value came from: a
caller, the header of a data file, or a row of a ``meta_*`` table that
something else edited.

Nothing here touches a connection: every function takes a value and returns the
spelling to emit, or raises.
"""
import re

from psycopg.sql import SQL


# This dictionary is used when creating new tables
# The value associated to each type is the typlen from the pg_type table
# Reverse sorting by this typlen improves space efficiency
# due to postgres' alignment requirements
number_types = {
    "int2": 2,
    "smallint": 2,
    "smallserial": 2,
    "serial2": 2,
    "int4": 4,
    "int": 4,
    "integer": 4,
    "serial": 4,
    "serial4": 4,
    "int8": 8,
    "bigint": 8,
    "bigserial": 8,
    "serial8": 8,
    "numeric": -1,
    "decimal": -1,
    "float4": 4,
    "real": 4,
    "float8": 8,
    "double precision": 8,
}
types_whitelist = {
    "boolean": 1,
    "bool": 1,
    "text": -1,
    "char": 1,
    "character": 1,
    "character varying": -1,
    "varchar": -1,
    "json": -1,
    "jsonb": -1,
    "xml": -1,
    "date": 4,
    "interval": 16,
    "time": 8,
    "time without time zone": 8,
    "time with time zone": 12,
    "timetz": 12,
    "timestamp": 8,
    "timestamp without time zone": 8,
    "timestamp with time zone": 8,
    "timestamptz": 8,
    "bytea": -1,
    "bit": -1,
    "bit varying": -1,
    "varbit": -1,
    "point": 16,
    "line": 24,
    "lseg": 32,
    "path": -1,
    "box": 32,
    "polygon": -1,
    "circle": 24,
    "tsquery": -1,
    "tsvector": -1,
    "txid_snapshot": -1,
    "uuid": 16,
    "cidr": -1,
    "inet": -1,
    "macaddr": 6,
    "money": 8,
    "pg_lsn": 8,
}
types_whitelist.update(number_types)
# add arrays
for elt in list(types_whitelist):
    types_whitelist[elt + "[]"] = -1


# Types that carry a length, a precision or a collation cannot be listed
# exhaustively, so they are described by a grammar.  Every pattern below is
# matched with fullmatch(): a type is accepted only if the *whole* string is a
# type, so a valid prefix such as "text" cannot carry a suffix of arbitrary SQL
# into the DDL these types are interpolated into.
#
# Keywords are matched case-insensitively through scoped (?i:...) groups rather
# than by lowercasing the input, because a collation name is a quoted
# identifier and therefore case-sensitive: "C" is a collation, "c" is not.

# A collation name, as it appears inside the double quotes of a COLLATE clause.
# Deliberately permissive about which collations exist -- "C", "POSIX",
# "en_US.utf8", "C.UTF-8", "und-x-icu" and every other ICU name are all real,
# and psycodict has no business deciding which a server has -- but restricted
# to characters that cannot end the quoted name early.
_COLLATION_NAME = r"[A-Za-z0-9][A-Za-z0-9_.@+-]*"

# A declaration is read in layers rather than by one regular expression per
# spelling: an optional COLLATE clause comes off the end, then the array
# suffixes, and what is left is a scalar type matched below.  That is the order
# PostgreSQL writes them in -- the collation follows the complete type,
# including its array brackets -- and it means a length, an array and a
# collation compose without a pattern for every combination.
_COLLATE_CLAUSE = re.compile(
    r'(?P<rest>.*\S)\s+(?P<clause>(?i:collate)\s+"' + _COLLATION_NAME + r'")',
    re.DOTALL,
)
_ARRAY_SUFFIXES = re.compile(r"(?:\s*\[\s*\])+$")

# The scalar families a collation may be applied to.  PostgreSQL rejects
# COLLATE on anything else, so accepting it here would only produce DDL the
# server refuses.
_COLLATABLE_SCALAR = re.compile(
    r"(?i:text|character\s+varying|varchar|character|char)(?:\s*\([1-9][0-9]*\))?"
)

# Interval fields split by whether they can carry a precision: PostgreSQL's
# interval precision is a *fractional seconds* precision, so a field
# restriction that stops short of SECOND cannot have one.
_INTERVAL_FIELDS_WITHOUT_SECOND = (
    r"year\s+to\s+month|day\s+to\s+hour|day\s+to\s+minute|hour\s+to\s+minute"
    r"|year|month|day|hour|minute"
)
_INTERVAL_FIELDS_WITH_SECOND = (
    r"day\s+to\s+second|hour\s+to\s+second|minute\s+to\s+second|second"
)

param_types_whitelist = {
    # the char family with a length; the bare spellings are fixed types
    r"(?i:character\s+varying|varchar|character|char)\s*\([1-9][0-9]*\)": -1,
    # bit strings, which unlike the char family require a length here
    r"(?i:bit\s+varying|varbit|bit)\s*\([1-9][0-9]*\)": -1,
    # interval: a precision alone, a field restriction alone, or a field
    # restriction reaching SECOND together with a precision
    r"(?i:interval)(?:"
    r"\s*\([0-6]\)"
    r"|\s+(?i:" + _INTERVAL_FIELDS_WITHOUT_SECOND + r")"
    r"|\s+(?i:" + _INTERVAL_FIELDS_WITH_SECOND + r")(?:\s*\([0-6]\))?"
    r")?": 16,
    r"(?i:timestamp)\s*\([0-6]\)(?:\s+(?i:with|without)\s+(?i:time\s+zone))?": 8,
    # PostgreSQL caps time precision at 6 but only warns above it; the wider
    # range here is the one psycodict has always accepted.
    r"(?i:time)\s*\((?:[0-9]|10)\)(?:\s+(?i:without\s+time\s+zone))?": 8,
    r"(?i:time)\s*\((?:[0-9]|10)\)\s+(?i:with\s+time\s+zone)": 12,
    r"(?i:numeric|decimal)\s*\([1-9][0-9]*(?:,\s*(?:0|[1-9][0-9]*))?\)": -1,
}
param_types_whitelist = {re.compile(s): cost for (s, cost) in param_types_whitelist.items()}

# The only characters a column type may contain: letters and digits, the
# punctuation used by lengths, precisions, array markers and quoted collation
# names, and spaces between words.  Checking this first rejects NUL bytes,
# control characters, non-ASCII lookalikes, semicolons and comment markers with
# a clear message, and makes the case-folded copy used for the lookup below an
# ASCII-only transformation of the string that is actually emitted.
_TYPE_CHARSET = re.compile(r'[A-Za-z0-9_ ,.@()\[\]"-]*')

# Preconstructed SQL for the fixed types, so that the common case interpolates
# a constant chosen from a closed mapping rather than a caller-supplied string.
_FIXED_TYPE_SQL = {typ: SQL(typ) for typ in types_whitelist}


class InvalidColumnTypeError(ValueError, RuntimeError):
    """
    Raised for a column type psycodict will not put into a statement.

    A ``ValueError``, since an unusable type is a bad argument, and also a
    ``RuntimeError``, which is what psycodict raised for an invalid type
    before 1.0.0 and what existing callers may catch.
    """


def validate_column_type(typ):
    """
    Check that ``typ`` is a PostgreSQL column type psycodict is willing to
    create, and return the spelling that callers must put into DDL.

    Validation is centralized here because a column type is interpolated into
    ``CREATE TABLE`` and ``ALTER TABLE`` statements as SQL text rather than
    bound as a value: PostgreSQL has no placeholder for a type.  Callers must
    emit the returned spelling and never the string they passed in, since the
    two are equal only for input that needed no normalization.

    INPUT:

    - ``typ`` -- a string, e.g. ``'bigint'``, ``'numeric(10, 2)'``,
      ``'varchar(16)[]'`` or ``'text COLLATE "C"'``.  Surrounding whitespace is
      ignored.

    OUTPUT:

    A pair ``(sql_spelling, storage_cost)``.  ``storage_cost`` is the width of
    the type in bytes, or -1 if it is variable (which every array is), and is
    used to order columns when creating a table.

    The declaration is read in the order PostgreSQL writes it: a scalar type,
    then any array brackets, then a collation.  Each layer is validated and the
    spelling is rebuilt from the validated pieces, so no part of the caller's
    string reaches the DDL unchecked.

    Raises ``InvalidColumnTypeError`` (a ``ValueError``) on anything else,
    including a type that merely starts with a valid type.

    EXAMPLES::

        >>> from psycodict.validation import validate_column_type
        >>> validate_column_type("bigint")
        ('bigint', 8)
        >>> validate_column_type("  TEXT  ")
        ('text', -1)
        >>> validate_column_type('varchar(16)[] COLLATE "C"')
        ('varchar(16)[] COLLATE "C"', -1)
        >>> validate_column_type("text; DROP TABLE students; --")
        Traceback (most recent call last):
        ...
        psycodict.validation.InvalidColumnTypeError: 'text; DROP TABLE students; --' is not a valid type: it contains the character ';'
    """
    if not isinstance(typ, str):
        raise InvalidColumnTypeError("Column type must be a string, not %s" % type(typ).__name__)
    typ = typ.strip()
    if not typ:
        raise InvalidColumnTypeError("Column type must not be empty")
    if not _TYPE_CHARSET.fullmatch(typ):
        bad = next(c for c in typ if not _TYPE_CHARSET.fullmatch(c))
        raise InvalidColumnTypeError(
            "%r is not a valid type: it contains the character %r"
            % (typ, bad)
        )

    # the collation, off the end
    collation = None
    collated = _COLLATE_CLAUSE.fullmatch(typ)
    if collated is not None:
        typ, collation = collated.group("rest"), collated.group("clause")

    # then the array brackets
    arrays = _ARRAY_SUFFIXES.search(typ)
    depth = 0
    if arrays is not None:
        depth = arrays.group(0).count("[")
        typ = typ[: arrays.start()].rstrip()

    spelling, cost = _validate_scalar_type(typ)
    if collation is not None and not _COLLATABLE_SCALAR.fullmatch(spelling):
        raise InvalidColumnTypeError(
            "%s is not a collatable type, so it cannot take a COLLATE clause"
            % (spelling,)
        )
    if depth:
        # an array is variable-width whatever its element type is
        spelling, cost = spelling + "[]" * depth, -1
    if collation is not None:
        spelling = "%s %s" % (spelling, collation)
    return spelling, cost


def _validate_scalar_type(typ):
    """
    The scalar half of :func:`validate_column_type`: no array brackets and no
    collation, which the caller has already taken off.
    """
    if not typ:
        raise InvalidColumnTypeError("Column type must not be empty")
    fixed = types_whitelist.get(typ.lower())
    if fixed is not None:
        # Emit the canonical spelling from the closed mapping rather than the
        # caller's casing.
        return typ.lower(), fixed
    for regexp, cost in param_types_whitelist.items():
        if regexp.fullmatch(typ):
            return typ, cost
    raise InvalidColumnTypeError("%s is not a valid type" % (typ,))


def column_type_sql(typ):
    """
    The SQL fragment for a column type, validated by
    :func:`validate_column_type`.

    INPUT:

    - ``typ`` -- a string giving a PostgreSQL column type

    OUTPUT:

    A ``psycopg.sql.SQL`` fragment naming the type, ready to be interpolated
    into a ``CREATE TABLE`` or ``ALTER TABLE`` statement.

    EXAMPLES::

        >>> from psycodict.validation import column_type_sql
        >>> column_type_sql("numeric(10, 2)").as_string()
        'numeric(10, 2)'
    """
    spelling, _ = validate_column_type(typ)
    fixed = _FIXED_TYPE_SQL.get(spelling)
    return SQL(spelling) if fixed is None else fixed
