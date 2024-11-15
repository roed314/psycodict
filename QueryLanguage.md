
## Introduction

Psycodict's query language defines a process for converting Python dictionaries into SQL `WHERE` clauses.  It is not complete: many SQL clauses are not possible to express in the language.  Rather, it aims to enable the iterative creation of queries based on input from a search page correspoding to a table, such as [this example](https://www.lmfdb.org/EllipticCurve/Q/).

It is designed to interface with PostgreSQL, though it could be modified to work with other SQL dialects.

## Overall structure

 * A query is evalauted in the context of a single table and does not contain information on which table it applies to.
 * The constructed query is the conjunction of the terms defined by the key-value pairs: all terms must be satisfied.  In particular, the empty dictionary corresponds to omitting a `WHERE` clause, yielding all rows.
 * The top-level keys may be
   * a column name,
   * one of the [top-level special keys](#top-level-special-keys),
   * a [column-part specifier](#column-part-specifiers).
 * For columns and column-parts, the corresponding value may be
   * A constant (a string, integer, float, list or other Python type matching the type of the column or column-part),
   * None, which translates to requiring that the column be null,
   * A dictionary, specified as in the [column constraints](#column-constraints) section below.
 * For top-level special keys, the value should be a dictionary or list of dictionaries, as explained [below](#top-level-special-keys).

## Examples

 1. `{"rank": 1, "torsion_structure": [2,8]}` translates to `WHERE rank=1 AND torsion_structure='{2,8}'`
 1. `{"ainvs.2": 1}` translates to `WHERE ainvs[2] = 1`
 1. `{"conductor": {"$gte": 100, "$lt": 1000}}` translates to `WHERE conductor >= 100 AND conductor < 1000`
 1. `{"$or": [{"conductor": 64, "torsion": 2}, {"absD": 128}]}` translates to `WHERE (conductor = 64 AND torsion = 2) OR (absD = 128)`
 1. `{"manin_constant": None}` translates to `WHERE manin_constant IS NULL`
 1. `{"manin_constant": {"$exists": True}}` translates to `WHERE manin_constant IS NOT NULL`
 1. `{"nonmax_primes": {"$contains": [3,5]}}` translates to `WHERE nonmax_primes @> '{3,5}'::int[]` (here `nonmax_primes` has type `smallint[]`; see [typecasts](#typecasts) below).

## Column constraints

The value associated to a column or column-part can be another dictionary, all of whose keys are [lower-level special keys](#lower-level-special-keys) (all of which start with `$`).  The column is then constrained to satisfy all of the conditions imposed this dictionary.

## Column part specifiers

For columns that have an [array](https://www.postgresql.org/docs/current/arrays.html) or [jsonb](https://www.postgresql.org/docs/current/datatype-json.html) type, you can access a part of the column by appending a path specifier.  For example, to get the `n`th entry of a one dimensional array append `.n` to the name of the column.  In general, a key containing a "." will be interpreted as specifying a path; the first part will be treated as the name of the column, later parts will be translated to `->n` (for jsonb columns) or `[n]` (for array columns).

## Top-level special keys

There are three valid top-level special keys: `$or`, `$and` and `$not`. The first two cases take a list of dictionaries as the value, parse them as full queries, and then join them using `OR` or `AND` respectively.  The last takes a single dictionary as the value and negates the resulting clause.

## Lower-level special keys

The following are valid keys for a column constraint.

### `$or`, `$and`, `$not`

These keys behave similarly to their use at the top level, but since the column has been specified already it can be omitted.  For example, `{"rank": {"$or": [0, 2, 4]}}` translates to `WHERE rank = 0 OR rank = 2 OR rank = 4` and `{"rank": {"$lt": 5, "$not": 2}}` translates to `WHERE rank < 5 AND NOT (rank = 2)`

### `$lte`, `$lt`, `$gte`, `$gt`, `$ne`, `$like`, `$ilike`, `$regex`

These translate to infix operators in postgres (`<=`, `<`, `>=`, `>`, `!=`, `LIKE`, `ILIKE`, and `~` respectively).

### `$exists`

If the corresponding value is true, translates to `IS NOT NULL`; otherwise, to `IS NULL`.

### `$contains`

This key specifies array containment using the `@>` operator.  The column should have array or jsonb type, and the query seeks rows where that column contains the given value (which can be either be a list or a single value).

### `$containedin`

This key specifies array containment using the `<@` operator.  The column should have array or jsonb type, and the query seeks rows where that column is contained in the given value (which should be a list).  Note that GIN indexes for `jsonb` columns do not support this operator so such indexes are not effective in this case.

### `$in`

For jsonb columns this is the same as `$containedin`, but for array columns this translates to `value = ANY(column)` which should be logically the same but may be optimized differently.

### `$nin`

Searches for rows where the column does not contain a single given value.  Translates to `NOT column @> value` (for `jsonb` columns) or `NOT (value = ANY(column))` (for array columns).

### `$notcontains`

Searches for rows where the column does not contain any of a list of `v`.  This translates to a conjunction (over entries `v` of the input list) of subclauses as in `$nin`.

### `$startswith`

Uses Postgres' `LIKE` operator to search for rows where the column starts with a given string.

### `$maxgte`

For an array column, requires that the maximum value in the array is at least the input.

### `$anylte`

For an array column, requires that the input is at least the minimum value in the array.  Translates to `value >= ANY(column)`.

### `$mod`

The value should be a pair of integers `[a, b]` with `0 <= a < b`, and rows are sought where column is congruent to `a` modulo `b`.  Translates to `MOD(b + MOD(column, b), b) = a` (the circumlocution is due to the fact that `MOD(-1, 5) = -1` in Postgres, which is stupid).

### `$overlaps`

For array columns, searches for rows where the column overlaps with a given list of values.  Translates to the `&&` operator.

## The `$raw` special key

Note that the column constraints enabled by all of the above special keys only allow comparing a column to a fixed value, not columns to each other.  As a limited mechanism to get around this limitation, the `$raw` key is available.  Details to be added.

## Typecasts

In some cases, typecasts will be added to values.  Specifically,
 * if the column type is `smallint[]` and the constraint key is `$contains` or `$containedin`, an `int[]` cast will be added to the column in order to test for containment.
 * otherwise, if the column is an array column then the value is explicitly cast into the array type since some array types (e.g. `numeric[]` do not automatically typecast input.
