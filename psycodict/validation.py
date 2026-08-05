# -*- coding: utf-8 -*-
"""
What psycodict will put into a statement.

Almost all of psycodict's SQL is composed from bound placeholders and quoted
identifiers, which are safe whatever the values are.  The exceptions are the
things PostgreSQL has no placeholder for -- a column type above all -- which
have to be interpolated as SQL text.  This module is where those are checked,
in one place, so that the rule is the same wherever the value came from: a
caller, the header of a data file, or a row of a ``meta_*`` table that
something else edited.  Alongside them sit the identifier rules: what names
psycodict will create, and how it derives the name of a companion table or a
``_tmp`` index from one it was given.

Nothing here touches a connection: every function takes a value and returns the
spelling to emit, or raises.
"""
import hashlib
import re
from collections import namedtuple

from psycopg.sql import SQL, Identifier

# The supported public API of this module: the names psycodict promises to
# keep across 1.x.  Other non-underscore names are implementation that may
# change; see Versioning.md.
__all__ = [
    "InvalidColumnTypeError",
    "InvalidDefinitionError",
]


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


##################################################################
# index and constraint definitions                               #
##################################################################

# An index or constraint definition lives in meta_indexes or meta_constraints
# between the call that creates it and the DDL that rebuilds it, which may be
# years and several psycodict versions later.  In between it can be edited with
# plain SQL, exported to a file, carried to another database and imported, or
# restored from the _hist tables, so "it must once have passed through
# create_index" is not something a statement builder can rely on.  The
# validators here are therefore applied at both ends: when a definition is
# imported, and again immediately before it is turned into DDL.

# The index access methods psycodict creates indexes with, mapped to the
# non-default operator classes each one accepts.
_operator_classes = {
    "brin": ["inet_minmax_ops"],
    "btree": [
        "bpchar_pattern_ops",
        "cidr_ops",
        "record_image_ops",
        "text_pattern_ops",
        "varchar_ops",
        "varchar_pattern_ops",
    ],
    "gin": ["jsonb_path_ops", "array_ops"],
    "gist": ["inet_ops"],
    "hash": [
        "bpchar_pattern_ops",
        "cidr_ops",
        "text_pattern_ops",
        "varchar_ops",
        "varchar_pattern_ops",
    ],
    "spgist": ["kd_point_ops"],
}

# Valid storage parameters by access method, used in creating indexes.
_valid_storage_params = {
    "brin": ["pages_per_range", "autosummarize"],
    "btree": ["fillfactor"],
    "gin": ["fastupdate", "gin_pending_list_limit"],
    "gist": ["fillfactor", "buffering"],
    "hash": ["fillfactor"],
    "spgist": ["fillfactor"],
}

# What each storage parameter's value may be, as (kind, lower, upper) for an
# integer, (kind, allowed, None) for an enumeration, or (kind, None, None) for a
# boolean.  A value that is not of the expected kind is rejected rather than
# passed to the server, so that a metadata row cannot smuggle anything into the
# WITH clause.
#
# gin_pending_list_limit has no upper bound here: PostgreSQL derives its ceiling
# from MAX_KILOBYTES, which depends on the server's architecture, so a fixed
# client-side maximum would be wrong on some supported server.  The value is
# emitted as a Literal, so the server enforces its own.
_storage_param_specs = {
    "fillfactor": ("integer", 10, 100),
    "pages_per_range": ("integer", 1, 131072),
    "gin_pending_list_limit": ("integer", 64, None),
    "autosummarize": ("boolean", None, None),
    "fastupdate": ("boolean", None, None),
    "buffering": ("enum", ("auto", "on", "off"), None),
}

# The spellings PostgreSQL's parse_bool accepts, including the unambiguous
# prefixes it allows ("of" is a prefix of "off"; the ambiguous "o" is not
# accepted).  psycodict normalizes them all to a Python bool, so that one value
# reaches both the DDL and meta_indexes however it was spelled.
_PG_BOOLEAN_TEXT = {}
for _word, _value in (("true", True), ("yes", True), ("on", True), ("1", True),
                      ("false", False), ("no", False), ("off", False), ("0", False)):
    for _length in range(1, len(_word) + 1):
        _prefix = _word[:_length]
        if _prefix in ("o",):
            # ambiguous between "on" and "off"
            continue
        _PG_BOOLEAN_TEXT.setdefault(_prefix, _value)


def normalize_storage_param(key, value, access_method):
    """
    Check one index storage parameter and return the value to emit.

    INPUT:

    - ``key`` -- the parameter name
    - ``value`` -- what it was given, from a caller or from ``meta_indexes``
    - ``access_method`` -- the index type, whose parameters ``key`` must be one of

    OUTPUT:

    The normalized value, which is what both the ``WITH`` clause and the
    metadata row get: a boolean spelled ``"on"`` is stored as ``True``, so a
    definition means the same thing however it was written.
    """
    if key not in _valid_storage_params.get(access_method, ()):
        raise InvalidDefinitionError(
            "Invalid storage parameter %r for a %s index" % (key, access_method)
        )
    kind, lower, upper = _storage_param_specs[key]
    if kind == "integer":
        # bool is a subclass of int, and WITH (fillfactor = true) is not a thing
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidDefinitionError(
                "Storage parameter %s must be an integer, not %s"
                % (key, _type_name(value))
            )
        if value < lower or (upper is not None and value > upper):
            raise InvalidDefinitionError(
                "Storage parameter %s must be %s, not %s"
                % (key,
                   "at least %s" % lower if upper is None
                   else "between %s and %s" % (lower, upper),
                   value)
            )
        return value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        # 0.0 == False and 1.0 == True in Python, so a float must be refused by
        # type rather than by membership
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.lower() in _PG_BOOLEAN_TEXT:
            return _PG_BOOLEAN_TEXT[value.lower()]
        raise InvalidDefinitionError(
            "Storage parameter %s must be a boolean, not %r" % (key, value)
        )
    if value not in lower:
        raise InvalidDefinitionError(
            "Storage parameter %s must be one of %s, not %r"
            % (key, ", ".join(lower), value)
        )
    return value

# The column modifiers that are not operator classes.  Each maps to the SQL it
# is emitted as, so that the statement is built from constants rather than from
# the stored string, and to its slot: an index column takes at most one
# operator class, one direction and one null placement, and PostgreSQL wants
# them in that order.
_index_modifiers = {
    "asc": ("direction", SQL("ASC")),
    "desc": ("direction", SQL("DESC")),
    "nulls first": ("nulls", SQL("NULLS FIRST")),
    "nulls last": ("nulls", SQL("NULLS LAST")),
}

# PostgreSQL truncates an identifier at 63 bytes, which would make a name and
# its _tmp variant indistinguishable, so psycodict refuses the longer name
# instead.
MAX_IDENTIFIER_LENGTH = 63

# A partial index predicate is raw SQL by design (see create_index), but it is
# appended to CREATE INDEX, where a statement terminator or a comment would let
# a metadata row carry a second statement along with it.
_MAX_PREDICATE_LENGTH = 4096


_valid_constraint_types = ("UNIQUE", "CHECK", "NOT NULL")


def _type_name(value):
    """
    The name of a value's type, for error messages.

    A helper because ``type`` is the name of a parameter in the validators
    below, following the column of ``meta_indexes`` it holds.
    """
    return value.__class__.__name__


class InvalidDefinitionError(ValueError):
    """
    Raised for an index or constraint definition psycodict will not build DDL
    from, whether it came from a caller, a metadata file or a ``meta_*`` row.
    """


def validate_persistent_name(name, kind="Relation"):
    """
    Check a name that is stored as the key of a relation.

    A persistent name -- an index name in ``meta_indexes``, a constraint name
    in ``meta_constraints``, the table name they refer to -- has to be the
    catalog name of the relation it describes.  PostgreSQL truncates a name
    over its limit rather than refusing it, so a longer string cannot be one:
    the metadata would be naming a relation that does not exist.

    This is the distinction :func:`derived_identifier` must not blur.  A
    physical name psycodict builds by appending ``_tmp`` may be shortened,
    because psycodict computes it the same way everywhere; a name the metadata
    stores may not, because it is what the relation is called.
    """
    return validate_relation_name(name, kind, max_length=MAX_IDENTIFIER_LENGTH)


def validate_relation_name(name, kind="Relation", max_length=None):
    """
    Check that ``name`` can be used as a PostgreSQL identifier.

    INPUT:

    - ``name`` -- the name of a table, index, constraint or column
    - ``kind`` -- what the name names, used in the error message
    - ``max_length`` -- a byte length to hold the name to, for a name psycodict
      is being asked to record.  Not applied by default: psycodict derives the
      names it puts in DDL from an existing one (see :func:`derived_identifier`),
      and a name already in a database is a fact rather than a proposal.

    OUTPUT:

    ``name`` itself.

    A quoted identifier can hold anything but a NUL, and psycodict quotes every
    name with ``Identifier``, so this checks what would actually make a name
    unusable rather than what it looks like: ``x-y``, ``index with spaces`` and
    ``idx_é`` are all legal PostgreSQL names, and a name that looks like SQL is
    inert once quoted.  Control characters are refused as a psycodict policy --
    they would make its logs and export files unreadable -- not because
    PostgreSQL minds.
    """
    if not isinstance(name, str):
        raise InvalidDefinitionError(
            "%s name must be a string, not %s" % (kind, _type_name(name))
        )
    if not name:
        raise InvalidDefinitionError("%s name must not be empty" % (kind,))
    for char in name:
        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F:
            raise InvalidDefinitionError(
                "%s name %r contains the control character %r" % (kind, name, char)
            )
    if max_length is not None and identifier_bytes(name) > max_length:
        raise InvalidDefinitionError(
            "%s name %r is %s bytes, longer than PostgreSQL's %s byte limit"
            % (kind, name, identifier_bytes(name), max_length)
        )
    return name


def identifier_bytes(name):
    """
    The length of an identifier as PostgreSQL measures it: UTF-8 bytes, not
    Python characters.
    """
    return len(name.encode("utf-8"))


def utf8_prefix(name, max_bytes):
    """
    The longest prefix of ``name`` that fits in ``max_bytes`` UTF-8 bytes,
    cut on a character boundary.
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    return encoded[:max_bytes].decode("utf-8", "ignore")


def validate_schema_name(name):
    """
    Check a PostgreSQL schema name psycodict will operate in.

    A schema name is an identifier like any other, and it is quoted wherever
    psycodict emits it, so the rules are the identifier rules: not empty, no
    control characters, and short enough that the server will not truncate it
    into a different schema.  It is checked once, in the constructor, rather
    than at each use.
    """
    return validate_relation_name(name, kind="Schema", max_length=MAX_IDENTIFIER_LENGTH)


def catalog_identifier(name):
    """
    The spelling the catalog holds ``name`` under.

    PostgreSQL truncates an over-long identifier as it parses the statement,
    so a relation created as ``<57 bytes>_counts`` is in ``pg_class`` under the
    first 63 bytes of that name.  SQL keeps reaching it by the long spelling --
    the same truncation happens every time, which is why
    :func:`physical_table_name` concatenates rather than shortens -- and a
    comparison the *server* makes is fine too, since ``pg_class.relname`` is of
    type ``name`` and a bound value is coerced to it.

    What is not fine is comparing the long spelling to a catalog name in
    Python, where nothing truncates anything.  Put a name through here first.
    """
    return utf8_prefix(name, MAX_IDENTIFIER_LENGTH)


def derived_identifier(base, suffix="", max_length=MAX_IDENTIFIER_LENGTH):
    """
    The name of a relation psycodict derives from another one.

    INPUT:

    - ``base`` -- the name it is derived from
    - ``suffix`` -- what psycodict appends: ``"_tmp"``, ``"_old1"``, ``"_dep0"``,
      ``"_pkey"`` and so on
    - ``max_length`` -- the server's identifier limit, in bytes

    OUTPUT:

    ``base + suffix`` when that fits, which is the overwhelmingly common case
    and what psycodict has always produced.  When it does not fit, the base is
    cut on a UTF-8 character boundary and a short digest of the whole base is
    inserted before the suffix, so that two names differing only in the part
    that was cut do not collide.

    Every path that creates, drops, renames or looks for a derived relation
    must call this, and none may build ``name + suffix`` itself: PostgreSQL
    truncates a name it is given, so a path that computed the name differently
    would look for a relation that another path had created under a different
    name.
    """
    candidate = base + suffix
    if identifier_bytes(candidate) <= max_length:
        return candidate
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    tail = "_%s%s" % (digest, suffix)
    return utf8_prefix(base, max_length - identifier_bytes(tail)) + tail


# The relations a search table's family is made of, and the suffix each one's
# name carries.
_TABLE_COMPONENT_SUFFIX = {"search": "", "counts": "_counts", "stats": "_stats"}


def physical_table_name(logical_name, component="search", lifecycle=""):
    """
    The name of one relation of a search table's family.

    INPUT:

    - ``logical_name`` -- the search table's name, as ``meta_tables`` records it
    - ``component`` -- ``"search"``, ``"counts"`` or ``"stats"``
    - ``lifecycle`` -- ``""`` for the live relation, or the suffix of a copy
      psycodict swaps through: ``"_tmp"``, ``"_old3"``

    OUTPUT:

    The relation's name, as every path must compute it.

    Unlike :func:`derived_identifier`, this does not shorten a name that does
    not fit, and deliberately so.  PostgreSQL truncates an over-long identifier
    *consistently* -- a legacy table whose counts table was created as 67 bytes
    has it in the catalog under the first 63, and a reference by the 67-byte
    name still reaches it -- so concatenation is what addresses the relations
    an existing database already has.  Shortening them here would make
    psycodict look for relations that do not exist.

    What truncation does break is distinctness: ``<63 bytes>_old1`` and
    ``<63 bytes>_old2`` are the same relation.  So a relation psycodict is
    about to *create* goes through :func:`check_new_table_name` first, which
    refuses the name rather than letting two of them become one.  A name within
    ``MAX_SEARCH_TABLE_NAME_LENGTH`` -- which every table created since that
    limit is -- can never reach either case.
    """
    return logical_name + _TABLE_COMPONENT_SUFFIX[component] + lifecycle


def check_new_table_name(name, kind="Table"):
    """
    Refuse a relation psycodict is about to create under a name PostgreSQL
    would truncate.

    Only reachable for a table whose name predates
    ``MAX_SEARCH_TABLE_NAME_LENGTH``; the point is that it stops rather than
    creating a second relation that turns out to be the first one.
    """
    if identifier_bytes(name) > MAX_IDENTIFIER_LENGTH:
        raise InvalidDefinitionError(
            "%s %r is %s bytes, over PostgreSQL's %s byte limit: it would be "
            "truncated into the name of another relation. Rename the table to "
            "something shorter first."
            % (kind, name, identifier_bytes(name), MAX_IDENTIFIER_LENGTH)
        )
    return name


def parse_check_function(name, valid_check_functions=()):
    """
    The identifier components of an approved CHECK function.

    INPUT:

    - ``name`` -- the function as recorded in ``meta_constraints``, either
      ``"function"`` or ``"schema.function"``
    - ``valid_check_functions`` -- the approved names

    OUTPUT:

    A tuple of components, to be emitted as ``Identifier(*components)``.  A
    qualified name is two identifiers and must not be quoted as one: PostgreSQL
    reads ``"schema.function"`` as a single function whose name contains a dot,
    which is not the function that was approved.
    """
    if not isinstance(name, str):
        raise InvalidDefinitionError(
            "A check function must be a string, not %s" % _type_name(name)
        )
    if name not in valid_check_functions:
        raise InvalidDefinitionError(
            "%r is not an approved check function; add it to "
            "PostgresTable._valid_check_functions to allow it" % (name,)
        )
    components = name.split(".")
    if len(components) > 2:
        raise InvalidDefinitionError(
            "%r is not a check function psycodict can name: it takes a "
            "function or a schema-qualified function" % (name,)
        )
    for component in components:
        validate_relation_name(
            component, "Check function", max_length=MAX_IDENTIFIER_LENGTH
        )
    return tuple(components)


def validate_column_name(name):
    """
    Check a column name an index or constraint definition refers to.

    Columns are quoted with ``Identifier`` wherever they are used, and a column
    that exists is a column whatever it is called -- the LMFDB has one called
    ``2adic_index`` -- so this checks only that the name is a string psycodict
    can put in a statement at all.
    """
    if not isinstance(name, str):
        raise InvalidDefinitionError(
            "Column name must be a string, not %s" % _type_name(name)
        )
    if not name:
        raise InvalidDefinitionError("Column name must not be empty")
    for char in name:
        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F:
            raise InvalidDefinitionError(
                "Column name %r contains the control character %r" % (name, char)
            )
    return name


# A search table's name is used in three namespaces at once: as a PostgreSQL
# identifier, as the key a table object is reached by on the database, and as
# the stem of the files an export writes.  The grammar below is what all three
# can agree on -- and it is the LMFDB's existing convention.
_SEARCH_TABLE_NAME = re.compile(r"[a-z][a-z0-9_]*")

# The relations every search table owns: itself, and the two named after it.
_SEARCH_TABLE_COMPONENTS = ("search", "counts", "stats")
_SEARCH_TABLE_COMPANIONS = tuple(
    _TABLE_COMPONENT_SUFFIX[component]
    for component in _SEARCH_TABLE_COMPONENTS
    if component != "search"
)

# Suffixes psycodict appends to a search table's name to make the names of its
# companion relations and of the temporary and backup tables it swaps through.
# A *new* search table called foo_counts would collide with the counts table of
# a search table called foo.
_RESERVED_TABLE_SUFFIXES = _SEARCH_TABLE_COMPANIONS + ("_pkey", "_tmp")
_RESERVED_TABLE_SUFFIX_PATTERNS = (r".*_old[0-9]+", r".*_dep[0-9]+")

# Suffixes that are psycodict's scratch space rather than a name anything is
# permanently called.  Unlike _counts and _stats these are refused whatever the
# source: a meta_tables row naming one would make a reload's temporary table or
# a backup look like a search table.
_SCRATCH_TABLE_SUFFIXES = ("_pkey", "_tmp")
_SCRATCH_TABLE_SUFFIX_PATTERNS = _RESERVED_TABLE_SUFFIX_PATTERNS

# psycodict's own relations, which are not search tables however meta_tables
# describes them.
_META_RELATION_NAMES = frozenset({
    "meta_tables", "meta_indexes", "meta_constraints",
    "meta_tables_hist", "meta_indexes_hist", "meta_constraints_hist",
    "meta_format",
    # the 0.x stamp, replaced by meta_format but still recognized on migration
    "meta_version",
})

# How much of the identifier limit a new search table's name has to leave
# unused.  psycodict names the table's companions, temporaries and backups
# after it, and the longest of those is <name>_counts_old<N>; PostgreSQL
# truncates a name over the limit rather than refusing it, which would make two
# of those relations the same relation.
MAX_BACKUP_NUMBER_DIGITS = 3
RESERVED_SEARCH_TABLE_SUFFIX_LENGTH = len("_counts_old") + MAX_BACKUP_NUMBER_DIGITS
MAX_SEARCH_TABLE_NAME_LENGTH = (
    MAX_IDENTIFIER_LENGTH - RESERVED_SEARCH_TABLE_SUFFIX_LENGTH
)


def search_table_family(name):
    """
    The relations a search table owns: itself and its companions.

    The spellings :func:`physical_table_name` produces, which are the ones
    psycodict puts into DDL.  For the spellings the catalog holds them under,
    put each through :func:`catalog_identifier`.
    """
    return tuple(
        physical_table_name(name, component)
        for component in _SEARCH_TABLE_COMPONENTS
    )


def validate_search_table_registry(names, relations):
    """
    Check the recorded search-table names as a set, against what the database
    actually has.

    INPUT:

    - ``names`` -- the names recorded in ``meta_tables``
    - ``relations`` -- the names of the relations in psycodict's schema

    OUTPUT:

    ``names``, unchanged.

    Validating one name at a time is not enough: what makes a name a search
    table is that it owns a whole family of relations -- itself, its counts
    table and its stats table -- and that nothing else owns any of them.  A row
    naming ``foo_counts`` when ``foo`` is a search table would otherwise put
    ``foo``'s counts table into the registry as a table in its own right, and
    every write to one would go to the other.

    That is a rule about ownership rather than about spelling, which is why a
    name ending in ``_stats`` is not refused out of hand: the LMFDB has a
    search table called ``hgcwa_per_group_stats``, and it owns
    ``hgcwa_per_group_stats``, ``hgcwa_per_group_stats_counts`` and
    ``hgcwa_per_group_stats_stats`` -- a complete family that overlaps nothing.

    ``relations`` holds catalog spellings, so the family is put through
    :func:`catalog_identifier` before being looked up in it.  A legacy table
    whose name leaves fewer than seven bytes for ``_counts`` has that relation
    in the catalog under a truncated name, and asking for the untruncated one
    here would report a table the database plainly has as missing -- which
    ``refresh_tables`` runs before building anything, so it would refuse to
    connect at all.
    """
    seen = set()
    for name in names:
        if name in seen:
            raise InvalidDefinitionError(
                "meta_tables records the search table %r more than once" % (name,)
            )
        seen.add(name)

    owner = {}
    for name in names:
        family = search_table_family(name)
        # Compare the spellings the catalog holds, not the ones psycodict
        # emits: a legacy table long enough that <name>_counts is over 63 bytes
        # has that relation under the truncated name, and the two are the same
        # relation only as far as the *server* is concerned.  Python is not the
        # server.
        stored = tuple(catalog_identifier(relation) for relation in family)

        claimed = {}
        for component, relation, spelling in zip(_SEARCH_TABLE_COMPONENTS, family, stored):
            # Two of a table's own relations sharing a catalog name is a
            # different failure from two tables colliding, and a worse one: no
            # renaming of anything else can separate them.
            if spelling in claimed:
                raise InvalidDefinitionError(
                    "The search table %r is %s bytes, so PostgreSQL stores its "
                    "%s and %s relations under one name, %r: they are a single "
                    "relation, and psycodict would write both to it"
                    % (name, identifier_bytes(name), claimed[spelling], component, spelling)
                )
            claimed[spelling] = component

            other = owner.get(spelling)
            if other is not None and other != name:
                raise InvalidDefinitionError(
                    "The search tables %r and %r both claim the relation %r"
                    % (other, name, spelling)
                )
            owner[spelling] = name

        missing = [
            (relation, spelling)
            for relation, spelling in zip(family, stored)
            if spelling not in relations
        ]
        if missing:
            raise InvalidDefinitionError(
                "meta_tables records the search table %r, but this database "
                "has no %s; a search table owns itself, its counts table and "
                "its stats table"
                % (name, " or ".join(
                    repr(relation) if relation == spelling
                    else "%r (which PostgreSQL stores as %r)" % (relation, spelling)
                    for relation, spelling in missing
                ))
            )
    return names


def validate_search_table_name(name, reserved=(), strict=True):
    """
    Check that ``name`` can be used as the name of a search table.

    INPUT:

    - ``name`` -- the proposed or recorded name
    - ``reserved`` -- names that are taken for another purpose on the database
      object (its attributes and methods), which a new table may not shadow
    - ``strict`` -- whether to apply the conventions a *new* name must follow,
      as opposed to the rules that keep an existing one safe to use

    OUTPUT:

    ``name`` itself.

    A search table's name is used in more places than a relation name: as an
    identifier, as the key of a table object on the database, and as the stem
    of the files ``copy_to`` generates.  A name containing a path separator or
    a ``..`` component would send an export outside the directory it was asked
    for, so that much is checked of every name, however it arrives.

    The rest -- lowercase spelling, and staying clear of the suffixes psycodict
    appends to a search table's own name -- is a convention for names psycodict
    is being asked to create.  It is not applied to a name a database already
    has, since that database is a fact: the LMFDB, for one, has a search table
    called ``hgcwa_per_group_stats``, and refusing to connect to a database on
    account of a name that has worked for years would be a worse failure than
    the one being prevented.
    """
    # The byte limit applies to every name, whatever its source: a longer
    # string cannot be the name of a PostgreSQL relation at all, since the
    # server truncates it, so keeping it as a registry key and a file stem
    # while the SQL reaches a shorter relation is an alias, not compatibility.
    validate_relation_name(name, "Search table", max_length=MAX_IDENTIFIER_LENGTH)
    if name in _META_RELATION_NAMES:
        raise InvalidDefinitionError(
            "%r is one of psycodict's own relations, not a search table" % (name,)
        )
    for suffix in _SCRATCH_TABLE_SUFFIXES:
        if name.endswith(suffix):
            raise InvalidDefinitionError(
                "Search table name %r ends with %s, which psycodict appends to "
                "name the relations it swaps through" % (name, suffix)
            )
    for pattern in _SCRATCH_TABLE_SUFFIX_PATTERNS:
        if re.fullmatch(pattern, name):
            raise InvalidDefinitionError(
                "Search table name %r ends with a suffix psycodict appends to "
                "name the relations it swaps through" % (name,)
            )
    if not strict:
        return name
    # A new name additionally has to leave room for the relations psycodict
    # will name after it.
    if identifier_bytes(name) > MAX_SEARCH_TABLE_NAME_LENGTH:
        raise InvalidDefinitionError(
            "Search table name %r is %s bytes; a new name may be at most %s, "
            "so that psycodict can name its companion, temporary and backup "
            "relations (the longest is %s_counts_old%s) without PostgreSQL "
            "truncating them at %s bytes"
            % (name, identifier_bytes(name), MAX_SEARCH_TABLE_NAME_LENGTH,
               name, "9" * MAX_BACKUP_NUMBER_DIGITS, MAX_IDENTIFIER_LENGTH)
        )
    if not _SEARCH_TABLE_NAME.fullmatch(name):
        raise InvalidDefinitionError(
            "Search table name %r must be lowercase letters, digits and "
            "underscores, starting with a letter" % (name,)
        )
    for suffix in _RESERVED_TABLE_SUFFIXES:
        if name.endswith(suffix):
            raise InvalidDefinitionError(
                "Search table name %r ends with %s, which psycodict appends to "
                "a search table's own name" % (name, suffix)
            )
    for pattern in _RESERVED_TABLE_SUFFIX_PATTERNS:
        if re.fullmatch(pattern, name):
            raise InvalidDefinitionError(
                "Search table name %r ends with a suffix psycodict appends to "
                "the tables it swaps through" % (name,)
            )
    if name in reserved:
        raise InvalidDefinitionError(
            "Search table name %r is the name of something else on the "
            "database object" % (name,)
        )
    return name


def validate_index_predicate(predicate):
    """
    Check the predicate of a partial index.

    The predicate is administrative raw SQL: psycodict does not parse it, and
    ``create_index`` documents that it is trusted input.  What this rules out
    is a predicate that does not stay a predicate -- one that ends the
    ``CREATE INDEX`` statement it is appended to, or comments out the rest of
    it -- so that a poisoned ``meta_indexes`` row cannot turn a restore into
    two statements.

    INPUT:

    - ``predicate`` -- a string giving the ``WHERE`` clause of a partial index

    OUTPUT:

    The predicate, stripped of surrounding whitespace.

    This is deliberately conservative: a predicate that needs a semicolon, a
    comment or a dollar-quoted string is rejected rather than analyzed.
    """
    if not isinstance(predicate, str):
        raise InvalidDefinitionError(
            "Index predicate must be a string, not %s" % type(predicate).__name__
        )
    stripped = predicate.strip()
    if not stripped:
        raise InvalidDefinitionError("Index predicate must not be empty")
    if len(stripped) > _MAX_PREDICATE_LENGTH:
        raise InvalidDefinitionError(
            "Index predicate is longer than %s characters" % _MAX_PREDICATE_LENGTH
        )
    bad = {
        "\x00": "a NUL character",
        ";": "a semicolon",
        "--": "a comment",
        "/*": "a comment",
        "*/": "a comment",
        "$$": "a dollar-quoted string",
    }
    for token, description in bad.items():
        if token in stripped:
            raise InvalidDefinitionError(
                "Index predicate %r contains %s, which is not allowed: the "
                "predicate is appended to CREATE INDEX and must not be able "
                "to end the statement" % (predicate, description)
            )
    for char in stripped:
        if char not in "\t\n\r" and (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F):
            raise InvalidDefinitionError(
                "Index predicate %r contains the control character %r"
                % (predicate, char)
            )
    # $tag$ ... $tag$ quoting, which the checks above would otherwise miss
    if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", stripped):
        raise InvalidDefinitionError(
            "Index predicate %r contains a dollar-quoted string, which is not "
            "allowed" % (predicate,)
        )
    return stripped


def index_modifier_sql(modifier, type):
    """
    The SQL for one modifier of one index column.

    INPUT:

    - ``modifier`` -- a modifier normalized by :func:`validate_index_definition`
    - ``type`` -- the access method of the index

    OUTPUT:

    A fixed ``SQL`` constant for a direction or null placement, and a quoted
    identifier for an operator class.  Nothing here is built by formatting the
    stored string into SQL text.
    """
    if modifier in _index_modifiers:
        return _index_modifiers[modifier][1]
    if modifier in _operator_classes.get(type, ()):
        return Identifier(modifier)
    raise InvalidDefinitionError("Invalid modifier %r for a %s index" % (modifier, type))


# The fields are named for what they are rather than for the meta_* columns
# they come from: the column is called "type" in both tables, which says less
# than access_method and constraint_type do.
IndexDefinition = namedtuple(
    "IndexDefinition",
    ["name", "table", "access_method", "columns", "modifiers", "storage_params",
     "whereclause"],
)

ConstraintDefinition = namedtuple(
    "ConstraintDefinition",
    ["name", "table", "constraint_type", "columns", "check_func"],
)


def _validate_columns(columns, valid_columns, kind):
    """
    Check the column list of an index or constraint definition.

    ``valid_columns`` is the set of columns of the relation the definition
    applies to, or None when it is unknown -- at import time the relation the
    definition will be built on may not exist yet, so the columns are checked
    for shape there and for existence at use time.
    """
    if isinstance(columns, str) or not isinstance(columns, (list, tuple)):
        raise InvalidDefinitionError(
            "%s columns must be a list, not %s" % (kind, type(columns).__name__)
        )
    if not columns:
        raise InvalidDefinitionError("%s must have at least one column" % kind)
    columns = list(columns)
    for col in columns:
        validate_column_name(col)
        if valid_columns is not None and col not in valid_columns:
            raise InvalidDefinitionError(
                "%s refers to %s, which is not a column of the table" % (kind, col)
            )
    return columns


def validate_index_definition(
    name, table, type, columns, modifiers, storage_params, whereclause=None,
    valid_columns=None,
):
    """
    Check an index definition and return it normalized.

    INPUT:

    - ``name``, ``table`` -- the names of the index and of the relation it is
      built on.  ``name`` may be None when the caller has not generated it yet
      (``create_index`` derives it from the columns it is validating here).
    - ``type`` -- the access method, one of the keys of ``_operator_classes``
    - ``columns`` -- a nonempty list of column names
    - ``modifiers`` -- a list, of the same length as ``columns``, of lists of
      modifiers for each column: an operator class valid for ``type``, a
      direction and a null placement
    - ``storage_params`` -- a dictionary of storage parameters valid for
      ``type``
    - ``whereclause`` -- the predicate of a partial index, or None
    - ``valid_columns`` -- the columns of the relation, if known; when given,
      every column of the index must be one of them

    OUTPUT:

    An ``IndexDefinition``.  Its ``modifiers`` are canonicalized to the
    spellings in ``_operator_classes`` and ``_index_modifiers`` and sorted into
    the order PostgreSQL expects, so the statement builder never emits a string
    that came out of the metadata.
    """
    if name is not None:
        # the name the metadata stores, so it has to fit exactly
        validate_persistent_name(name, "Index")
    validate_persistent_name(table, "Table")
    if type not in _operator_classes:
        raise InvalidDefinitionError(
            "Unrecognized index type %r; psycodict supports %s"
            % (type, ", ".join(sorted(_operator_classes)))
        )
    columns = _validate_columns(columns, valid_columns, "Index")

    if modifiers is None:
        modifiers = [[]] * len(columns)
    if isinstance(modifiers, str) or not isinstance(modifiers, (list, tuple)):
        raise InvalidDefinitionError(
            "Index modifiers must be a list, not %s" % _type_name(modifiers)
        )
    if len(modifiers) != len(columns):
        raise InvalidDefinitionError(
            "Index has %s columns but %s modifier lists"
            % (len(columns), len(modifiers))
        )
    normalized_modifiers = []
    for mods in modifiers:
        if mods is None:
            mods = []
        if isinstance(mods, str) or not isinstance(mods, (list, tuple)):
            raise InvalidDefinitionError(
                "Index modifiers for a column must be a list, not %s" % _type_name(mods)
            )
        slots = {}
        for mod in mods:
            if not isinstance(mod, str):
                raise InvalidDefinitionError(
                    "Index modifier must be a string, not %s" % _type_name(mod)
                )
            key = " ".join(mod.lower().split())
            if key in _index_modifiers:
                slot, _ = _index_modifiers[key]
            elif key in _operator_classes[type]:
                slot = "opclass"
            else:
                raise InvalidDefinitionError(
                    "Invalid modifier %r for a %s index" % (mod, type)
                )
            if slot in slots:
                raise InvalidDefinitionError(
                    "Index column has two %s modifiers: %r and %r"
                    % (slot, slots[slot], key)
                )
            slots[slot] = key
        normalized_modifiers.append(
            [slots[slot] for slot in ("opclass", "direction", "nulls") if slot in slots]
        )

    if storage_params is None:
        storage_params = {}
    if not isinstance(storage_params, dict):
        raise InvalidDefinitionError(
            "Index storage parameters must be a dictionary, not %s"
            % _type_name(storage_params)
        )
    storage_params = {
        key: normalize_storage_param(key, val, type)
        for key, val in storage_params.items()
    }

    if whereclause is not None:
        whereclause = validate_index_predicate(whereclause)

    return IndexDefinition(
        name, table, type, columns, normalized_modifiers, storage_params,
        whereclause,
    )


def validate_constraint_definition(
    name, table, type, columns, check_func, valid_columns=None,
    valid_check_functions=(),
):
    """
    Check a constraint definition and return it normalized.

    INPUT:

    - ``name``, ``table`` -- the names of the constraint and of the relation it
      applies to
    - ``type`` -- ``"UNIQUE"``, ``"CHECK"`` or ``"NOT NULL"``
    - ``columns`` -- a nonempty list of column names; ``NOT NULL`` takes one
    - ``check_func`` -- for a CHECK constraint, the name of the function it
      calls, which must be one of ``valid_check_functions``; None otherwise
    - ``valid_columns`` -- the columns of the relation, if known
    - ``valid_check_functions`` -- the approved check functions, normally
      ``PostgresTable._valid_check_functions``

    OUTPUT:

    A ``ConstraintDefinition``.
    """
    if name is not None:
        # None while ``create_constraint`` is still deriving the name from the
        # columns it is validating here.
        validate_persistent_name(name, "Constraint")
    validate_persistent_name(table, "Table")
    if not isinstance(type, str) or type not in _valid_constraint_types:
        raise InvalidDefinitionError(
            "Unrecognized constraint type %r; psycodict supports %s"
            % (type, ", ".join(_valid_constraint_types))
        )
    columns = _validate_columns(columns, valid_columns, "Constraint")
    if type == "NOT NULL" and len(columns) != 1:
        raise InvalidDefinitionError(
            "A NOT NULL constraint has one column, not %s" % len(columns)
        )
    if (check_func is None) == (type == "CHECK"):
        raise InvalidDefinitionError(
            "A check function belongs to a CHECK constraint and only to one"
        )
    if check_func is not None:
        # parsed rather than merely checked: the statement builder emits the
        # components, and a name that cannot be split into them is not a
        # function psycodict can call
        parse_check_function(check_func, valid_check_functions)
    return ConstraintDefinition(name, table, type, columns, check_func)
