
## Introduction

Psycodict's query language defines a process for converting Python dictionaries into SQL `WHERE` clauses.  It is not complete: many SQL clauses are not possible to express in the language.  Rather, it aims to enable the iterative creation of queries based on input from a search page correspoding to a table, such as [this example](https://www.lmfdb.org/EllipticCurve/Q/).

It is designed to interface with PostgreSQL, though it could be modified to work with other SQL dialects.  This document covers reading data; for creating tables and getting data into them, see [DataManagement.md](DataManagement.md).

## Overall structure

 * A query is evalauted in the context of a single table and does not contain information on which table it applies to.  Columns of other tables can be brought into a query by joining the tables; see [Joined queries](#joined-queries).
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

## Comparing columns: the `$col` special key

The special keys above compare a column to a fixed value.  To compare a column to *another column of the same table*, use `$col` with the other column's name:

 * `{"col1": {"$col": "col2"}}` translates to `WHERE col1 = col2`;
 * `{"col1": {"$lte": {"$col": "col2"}}}` translates to `WHERE col1 <= col2`, and `{"$col": ...}` can likewise be used as the value for any of the infix operator keys (`$lt`, `$gte`, `$gt`, `$ne`, `$like`, `$ilike`, `$regex`);
 * an array slicer is allowed on the named column, so `{"n": {"$col": "vec[2]"}}` compares `n` with the third entry of `vec` (slicers are written in Python style, starting at zero).

The named column resolves exactly like a query key — a bare name is a column of the table being searched, and in a [joined query](#joined-queries) `"table.column"` names a joined table's column, so `$col` can compare columns of different tables.  Path specifiers are not supported, and the two columns must have comparable types.

## The `$raw` special key

For constraints beyond column-to-column comparison, `$raw` accepts a limited arithmetic expression in the table's columns: `{"abvar_count": {"$lte": {"$raw": "q^g"}}}` translates to `WHERE abvar_count <= q^g`, and a direct `{"col": {"$raw": "expr"}}` to `WHERE col = expr`.  Names in the expression resolve like query keys, so in a [joined query](#joined-queries) `"table.column"` brings in a joined table's column.  The expression is not passed through as SQL: it may only contain column names, numeric literals, and the characters `+-*/^()`, so that untrusted input cannot inject SQL.

## Joined queries

The `search`, `count`, `lucky` and `lookup` methods accept a `join` option that makes columns of other search tables available to the query, the projection and the results:

```python
db.ec_nfcurves.search(
    {"rank": 1, "nf_fields.r2": 1},
    ["label", "nf_fields.degree"],
    join=[("field_label", "nf_fields.label")],
    limit=3,
)
```

 * **Join specification.**  `join` is a list of tuples `(col1, col2)` or `(col1, col2, jointype)`, each adding one table to the query via `JOIN ... ON col1 = col2`.  `col2` must be written as `"table.column"` and names the table being joined; `col1` is a column of the primary table, or of a previously joined table if written as `"table.column"`.  `jointype` is `"inner"` (the default), `"left"`, `"right"` or `"full"`.  Each table can be joined at most once, and join columns must be plain columns (no path specifiers).
 * **Name resolution.**  When `join` is given, keys in the query, entries in the projection and sort, and column names given to `$col` are split at the first period: if the prefix names a joined table, the name refers to that table's column, and any further periods are a path specifier within that column.  Otherwise the whole name refers to the primary table as usual, with periods keeping their [path specifier](#column-part-specifiers) meaning.  Result dictionaries use the projection entries verbatim as keys, so joined columns come back under their qualified names.
 * **What works.**  The special keys above all apply to joined columns, including path specifiers; `$or`, `$and` and `$not` clauses may mix constraints on several tables; `$col` may compare, and `$raw` expressions may combine, columns of different tables — names in both resolve like query keys — covering cross-table conditions beyond the `ON` clauses.  A LEFT join yields NULLs for the joined columns of unmatched rows, so `{"table.col": None}` finds the rows of the primary table without a match; RIGHT and FULL joins likewise surface rows with the *primary* table's columns NULL — project a joined column to identify them, since None values are omitted from result dictionaries.
 * **Restrictions** (violations raise `ValueError`): `split_ors`, `one_per`, `raw` and `groupby` do not combine with `join`; dictionary projections are not supported.  Counts of joined queries are computed directly and never cached.

## Typecasts

In some cases, typecasts will be added to values.  Specifically,
 * if the column type is `smallint[]` and the constraint key is `$contains` or `$containedin`, an `int[]` cast will be added to the column in order to test for containment.
 * otherwise, if the column is an array column then the value is explicitly cast into the array type since some array types (e.g. `numeric[]` do not automatically typecast input.
