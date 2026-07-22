# Searching

This document specifies the read API of a psycodict search table
(`psycodict.searchtable.PostgresSearchTable`): the methods used to retrieve
rows, count them and compute simple statistics.  It is the companion to
[QueryLanguage.md](QueryLanguage.md), which specifies the dictionary-to-`WHERE`
language used by the `query`/`constraint` arguments below; this document
specifies everything else — parameters, return types, projections, the `info`
contract, sorting, sampling, caching and result-value semantics.

## Stability

This describes psycodict 1.0.  The methods and behaviors documented here are
the stable read interface: their signatures, return-type rules and the `info`
dictionary contract are frozen for the 1.0 series.

Names beginning with an underscore (`_parse_dict`, `_build_query`,
`_search_iterator`, `_process_sort`, `_count_cutoff`, `_include_nones`, …) are
private implementation details.  They are referenced in this document only to
explain observable behavior; they are **not** part of the stable API and may
change without notice.  Code outside psycodict should call only the
non-underscore methods described below (on the table, and on its public
`table.stats` attribute).

A search table is reached as `db.<table_name>` (equivalently `db["<table_name>"]`)
on a `PostgresDatabase`.  Every example in this document uses `db` for the
database and a table variable such as `nf = db.nf_fields`.

## Contents

- [Stability](#stability)
- [`search`](#search)
  - [Parameters](#search-parameters)
  - [Return type: iterator vs. list](#return-type-iterator-vs-list)
  - [Projections](#projections)
  - [Sorting](#sorting)
  - [The `info` dictionary](#the-info-dictionary)
  - [`split_ors`](#split_ors)
  - [`one_per`](#one_per)
  - [`silent` and slow-query logging](#silent-and-slow-query-logging)
  - [`raw` and `raw_values`](#raw-and-raw_values)
- [`lucky`, `lookup`, `exists`, `label_exists`](#lucky-lookup-exists-label_exists)
- [`random` and `random_sample`](#random-and-random_sample)
- [Counts and statistics](#counts-and-statistics)
- [Joins](#joins)
- [Result-value semantics](#result-value-semantics)

<a name="search"></a>
## `search`

```python
search(query={}, projection=1, limit=None, offset=0, sort=None,
       info=None, split_ors=False, one_per=None, silent=False,
       raw=None, raw_values=None, join=None)
```

`search` is the primary interface for retrieving multiple rows.  It selects the
rows matching `query` (parsed by the [query language](QueryLanguage.md)),
applies the projection, sort, limit and offset, and returns either an iterator
or a list depending on `limit` (see below).

<a name="search-parameters"></a>
### Parameters

- **`query`** — a query dictionary; see [QueryLanguage.md](QueryLanguage.md).
  The empty dictionary `{}` matches every row.
- **`projection`** — which columns to return; see [Projections](#projections).
  Default `1` (all search columns).
- **`limit`** — an integer maximum number of rows, or `None` (default).  `None`
  returns an iterator over *all* matches; an integer returns a list.  This
  choice governs the return type — see below.  A negative limit raises
  `ValueError`.
- **`offset`** — a non-negative integer (default `0`); the number of leading
  rows to skip.  A negative offset raises `ValueError`.
- **`sort`** — the sort order, or `None` (default) for the table's default
  sort.  See [Sorting](#sorting).  Use `[]` for an explicitly unsorted query.
- **`info`** — an optional dictionary that `search` fills in with metadata
  about the result set (query, count, page).  See
  [The `info` dictionary](#the-info-dictionary).
- **`split_ors`** — boolean (default `False`).  Run one query per top-level
  `$or` branch and merge; only valid when `limit` is given.  See
  [`split_ors`](#split_ors).
- **`one_per`** — a list of columns, or `None` (default).  Return only one row
  per distinct tuple of values in those columns.  See [`one_per`](#one_per).
- **`silent`** — boolean (default `False`).  Suppress slow-query log warnings.
- **`raw`** — a string used verbatim as the `WHERE` clause.  **Never pass
  untrusted input here** — see [`raw` and `raw_values`](#raw-and-raw_values).
- **`raw_values`** — a list of values substituted for `%s` placeholders in
  `raw`.
- **`join`** — a list of join tuples making other tables' columns available;
  see [Joins](#joins) and the Joined queries section of
  [QueryLanguage.md](QueryLanguage.md).

<a name="return-type-iterator-vs-list"></a>
### Return type: iterator vs. list

The return type is governed **solely** by `limit`:

- **`limit=None`** → an **iterator** (a generator) over all matching rows.
- **`limit` an integer** → a **list** of at most `limit` rows.

This is a hard rule: `limit=None` never returns a list and an integer `limit`
never returns a bare iterator, regardless of the other arguments.

**Buffered-cursor implication for iterators.**  When `limit=None`, the
underlying query runs on a **server-side (named) cursor** created with
`withhold=True` and `itersize=2000`; rows are streamed from PostgreSQL in
batches of 2000 as the iterator is consumed, rather than all being materialized
in the client.  Consequences you must design around:

- The cursor is opened inside a transaction that stays open until the iterator
  is exhausted (or explicitly closed / garbage-collected).  On exhaustion the
  iterator closes the cursor and, if nothing else in the current context needs
  committing, commits the connection to end the transaction.  A `limit=None`
  iterator that you never finish consuming therefore leaves a cursor and
  transaction open on the connection.
- Because the cursor is declared `WITH HOLD`, it survives commits, so iteration
  is not invalidated by intervening commits on the same connection — but you
  should still consume such iterators promptly rather than interleaving other
  work.
- If you need a concrete, fully-materialized collection, either pass an integer
  `limit` (which fetches with `fetchmany` into a list) or wrap the call in
  `list(...)`.

An integer `limit`, by contrast, uses an ordinary client-side cursor: the query
is executed, up to `limit` rows are fetched into a list, and the transaction is
committed immediately.

Yielded values follow the projection: for `projection=0` or a string
projection, the iterator/list contains bare column values; otherwise it contains
dictionaries.

<a name="projections"></a>
### Projections

The `projection` argument selects the columns to return.  A search table has a
list of *search columns* (`table.search_cols`, not including the `id` column)
and, usually, a designated *label column* (`table._label_col`).

| `projection` | Result |
| --- | --- |
| `0` | Just the label column, returned as a **bare value** (not a dict).  Raises `RuntimeError` if the table has no label column. |
| `1` (default for `search`) | All search columns, as a dict. |
| `2` | All search columns, as a dict — a **historical alias for `1`**, kept deliberately.  Dates from when a table could be split into a search table and an "extras" table; `2` meant "search + extras".  Tables are no longer split, so `1` and `2` are identical, and both are retained. |
| `3` | As `1`, but with the `id` column included.  Also a historical form, kept deliberately. |
| a string `"col"` | Just that column, returned as a **bare value** (not a dict). |
| a list `["c1", "c2", …]` | Those columns, as a dict.  `"id"` may be listed explicitly to include the id.  Entries may use an **array/jsonb slicer** `"col[i]"` (the result key is the string `"col[i]"`). |
| a dict `{"c1": True, …}` | *Include* mode: the listed columns (all values truthy), as a dict.  `id` is included only if given a truthy value. |
| a dict `{"c1": False, …}` | *Exclude* mode: all search columns **except** the listed ones (all values falsy), as a dict. |

Rules and errors:

- A projection dict may not mix include and exclude: `{"a": True, "b": False}`
  raises `ValueError("You cannot both include and exclude.")`.
- A falsy projection other than `0` (e.g. `[]`, `{}`, `""`) raises
  `ValueError("You must specify at least one key.")`.
- An unknown column raises `ValueError("<col> not column of <table>")`.
- A **list** projection may not repeat a column: `["a", "a"]` raises
  `ValueError("Duplicate column(s) in projection: a")`.
- List projections accept path specifiers, as query keys do: both the
  `"col[i]"` array **slicer** syntax and the dotted json/array-path syntax
  (`"data.key"`, `"ainvs.1"`).  The selected value is the element at that path.
  (Two exceptions: neither form is allowed together with `split_ors` or
  `one_per`, which key the fetched rows by plain column name and so raise a
  `ValueError` for a path or slicer in the projection or sort.)
- Dictionary projections are **not** supported together with `join` (see
  [Joins](#joins)).

Examples:

```python
nf.search({"degree": 2}, projection=0, limit=3)          # ['2.2.5.1', '2.2.8.1', ...]
nf.search({"degree": 2}, projection="class_number", limit=3)  # [1, 1, ...]
nf.search({"degree": 2}, projection=["label", "galt"], limit=1)
    # [{'label': '2.2.5.1', 'galt': 1}]
nf.search({"degree": 2}, projection={"coeffs": False}, limit=1)  # all but 'coeffs'
```

<a name="sorting"></a>
### Sorting

`sort` is either `None`, or a list whose entries are column names (ascending) or
pairs `(col, 1)` / `(col, -1)` for ascending/descending.  Descending order is
emitted as `ORDER BY col DESC NULLS LAST`.

- **`sort=[]`** — explicitly unsorted: no `ORDER BY` is emitted, so the row
  order is whatever PostgreSQL returns (arbitrary and not stable).
- **An explicit non-empty list** — used verbatim.  A column may not appear more
  than once (its first appearance already fixes the order); a repeat raises
  `ValueError("Duplicate column 'col' in sort order")`.
- **`sort=None` (the default for `search`)** — the table's default sort, with a
  query-planner optimization:
  - If the table has **no default sort**, results are ordered by `id` when a
    `limit` is given, and left unsorted otherwise.
  - If the table **has a default sort**, it is used — **except** that when the
    table is not flagged out-of-order *and* the primary (most significant) sort
    column is not constrained by the query, `ORDER BY id` is substituted.  For
    an id-ordered table, id order agrees with the default sort, and this
    substitution steers the planner away from an unnecessary sort/sequential
    scan.  When the primary sort column *does* appear in the query, or the
    table is flagged out-of-order, the real default sort is used.

Note the different defaults across methods: `search` defaults to `sort=None`
(the table's default sort), whereas `lucky`/`lookup` default to `sort=[]`
(unsorted — see below).

<a name="the-info-dictionary"></a>
### The `info` dictionary

If an `info` dictionary is passed, `search` mutates it in place to describe the
result set.  This is used by search-result web pages to render counts and
pagination.  The keys set depend on whether a `limit` was given.

**When `limit` is an integer**, `search` sets exactly these five keys:

| key | value |
| --- | --- |
| `query` | a **copy** of `query` (`dict(query)`; the caller's dict is not aliased). |
| `number` | the number of results — exact, or a lower bound (see below). |
| `count` | the page size, i.e. the `limit` argument. |
| `start` | the offset of the first returned row (see the past-the-last-page adjustment below). |
| `exact_count` | boolean: whether `number` is exact or merely a lower bound. |

**When `limit is None`**, only `info["number"]` is set, to the exact count
`self.count(query)`; `query`, `count`, `start` and `exact_count` are **not**
set.  (The other keys describe a page, and an unbounded iterator has no page.)

#### `exact_count` and the count-estimation prelimit

Computing an exact `COUNT(*)` for every search is expensive on large tables, so
`search` estimates the count as a side effect of fetching the page, capping the
work at a per-table cutoff `_count_cutoff` (**default 1000**, configurable per
table).  The logic:

1. If the count is already known — the query is empty (`{}`, answered from the
   cached row total) or its count is cached in the counts table — that exact
   value is used for `number`, `exact_count` is `True`, and the page is fetched
   with the plain `limit`.

2. Otherwise `search` inflates the fetch limit to a **prelimit**

   ```
   prelimit = max(limit, _count_cutoff - offset)
   ```

   and runs the query with that prelimit (still starting at `offset`).  It then
   returns only the first `limit` rows to the caller (the extra rows exist only
   to probe how many matches there are), and infers:

   - If the query returned **fewer than `prelimit`** rows, the match set was
     exhausted, so `number = offset + rowcount` is **exact** and
     `exact_count = True`.
   - If the query returned **exactly `prelimit`** rows, there may be more, so
     `number = offset + prelimit` is a **lower bound** and `exact_count = False`.

   In other words, the true count is reported exactly whenever it is below
   `_count_cutoff` (accounting for the offset); at or above the cutoff, `number`
   is reported as the cutoff value with `exact_count = False`.  A consequence
   worth noting: when the true count is **exactly** `_count_cutoff`, it is still
   reported as a lower bound (`exact_count = False`), because the prelimit was
   reached.

   (Edge case: with `offset > 0`, a query that returns **zero** rows also yields
   `exact_count = False` — the true count could be anything from 0 up to the
   offset.  When `info` is supplied this situation is resolved by the
   past-the-last-page adjustment below.)

Empty-query counts (`query == {}`) are always exact: they come from the table's
cached row total, not from a scan.

#### Past-the-last-page adjustment

When `info` is supplied and the requested `offset` is at least the number of
results (`offset >= number > 0`) — i.e. the caller asked for a page beyond the
end — `search` does not return an empty page.  Instead it recomputes the count
exactly if it was only a lower bound (`self.stats.count(query)`), sets
`offset = number - limit` (clamped at 0) to land on the **last** page, and
re-runs the search at that offset.  The returned rows and `info["start"]` then
describe the last page rather than the empty page the caller asked for.  (If
the count was a lower bound, the recursive re-run may re-derive `number` through
the estimation path again, so `exact_count` can still read `False` even though
the rows shown are the genuine last page.)

Example:

```python
info = {}
nf.search({'degree': 2, 'class_number': 1, 'disc_sign': -1},
          projection=0, limit=4, info=info)
info['number'], info['exact_count']   # (9, True)   -- fewer than the cutoff

info = {}
nf.search({'ramps': {'$contains': [2, 7]}}, limit=4, info=info)
info['number'], info['exact_count']   # (1000, False) -- at/above the cutoff
```

<a name="split_ors"></a>
### `split_ors`

Some queries with a top-level `$or` are executed far more efficiently as
several separate queries than as one disjunction, because PostgreSQL can use a
different index per branch.  With `split_ors=True`, `search` splits the query on
its top-level `$or`, runs one query per branch (each branch AND-combined with
the rest of the query — see `_split_ors`), and merges the results.

- `split_ors` requires a `limit`; `search` raises
  `ValueError("split_ors only supported when a limit is provided")` when
  `limit is None`.
- It is silently ignored if there is no top-level `$or` to split, or if only
  one branch survives, or when `raw` is used (`raw` forces `split_ors=False`).
- It is **not** compatible with `one_per` or `join` (both raise `ValueError`).

**Merge and sort.**  The branches are fetched independently (each with its own
count-estimation prelimit — `max(limit + offset, _count_cutoff)` when the count
is not cached), concatenated, and then sorted in Python by the effective sort
order and sliced to `[offset : offset + limit]`.  The sort is a full re-sort of
the merged list (not a streaming heap merge); when every sort key is ascending
or numeric a single tuple key is used, otherwise the list is sorted by each
key in turn from least to most significant.  The count reported in `info`
follows the same exact/lower-bound rule as the single-query path (exact total
when no branch hit its prelimit, otherwise `min(total, _count_cutoff)` with
`exact_count = False`), so it is consistent with the non-split path.

<a name="one_per"></a>
### `one_per`

`one_per` is a list of columns; the search returns only one row for each
distinct tuple of values those columns take, using PostgreSQL's `DISTINCT ON`.
The row kept for each group is the **first** according to the effective sort
order.  Internally the query becomes

```sql
SELECT <projection> FROM (
    SELECT DISTINCT ON (<one_per cols>) <cols + sort cols>
    FROM <table> WHERE <query> ORDER BY <one_per cols>, <sort>
) temp ORDER BY <sort>
```

so the inner `ORDER BY` begins with the `one_per` columns (as `DISTINCT ON`
requires) followed by the requested sort, and the outer query re-imposes the
requested sort on the deduplicated rows.  Any sort columns that are not part of
the projection are added to the inner selection and then stripped from the
result dictionaries.

`one_per` disables the count cache (the count is always estimated via the
prelimit path) and is incompatible with `split_ors` and `join`.

```python
# one row per jinv, the first by the table's sort order:
db.ec_curvedata.search({...}, projection=['lmfdb_label'], one_per=['jinv'], limit=100)
```

<a name="silent-and-slow-query-logging"></a>
### `silent` and slow-query logging

Every executed query is timed.  If it runs longer than the table's
`slow_cutoff` (from the `[logging] slowcutoff` configuration, in seconds) the
query is logged at INFO level, together with a "Replicate with db.…" line
reconstructing the call, to aid diagnosis.  In addition, `_search_iterator`
logs the *total* time spent pulling rows from the cursor if it exceeds
`slow_cutoff`.

Passing `silent=True` suppresses these slow-query warnings for the call (both
the execution warning and the iterator's total-time warning).  It does not
change results.  Whole-connection silencing is also available via the database's
`_silenced` flag / `DelayCommit` contexts, but per-call `silent` is the public
control.

<a name="raw-and-raw_values"></a>
### `raw` and `raw_values`

`raw` is a string inserted **verbatim** as the `WHERE` clause, bypassing the
query-language parser entirely; `raw_values` is a list of values substituted for
`%s` placeholders in it (use placeholders for any value that might contain
quotes, rather than interpolating into the string).

> **Security warning.**  `raw` is not sanitized in any way — it is concatenated
> directly into SQL.  **Never** build `raw` from untrusted input (web form
> parameters, API callers, file contents).  It exists only for trusted,
> programmatic call sites that need SQL the query language cannot express.  For
> untrusted input, always use the `query` dictionary, whose values are passed as
> parameters.  (The `$raw` query-language key, by contrast, *is* filtered
> against injection; see QueryLanguage.md.  `raw` here has no such protection.)

When `raw` is given, `split_ors` is forced off, and `join` is rejected
(`ValueError("raw is not supported with join")`).  A `raw_values` list passed in
by the caller is never mutated (the limit and offset are appended to an internal
copy).

<a name="lucky-lookup-exists-label_exists"></a>
## `lucky`, `lookup`, `exists`, `label_exists`

### `lucky`

```python
lucky(query={}, projection=2, offset=0, sort=[], raw=None, raw_values=None, join=None)
```

`lucky` returns a **single** record — the first row matching `query` — or `None`
if there is no match.  It is the single-result counterpart of `search`, and
shares its projection semantics: with `projection=0` or a string projection it
returns a bare value; otherwise a dictionary.

Two defaults differ from `search`:

- **`projection=2`** (all columns), versus `search`'s `1`.  Since `2` is an
  alias for `1`, both return all search columns; the difference is only in the
  default.
- **`sort=[]`** — **unsorted by default.**  Because no order is imposed unless
  you pass one, when more than one row matches, *which* row you get is arbitrary
  and not stable across calls or database states.  Pass an explicit `sort` (or
  `sort=None` for the table's default sort) when you need a well-defined "first"
  row.

`offset` skips that many matching rows before taking one, so
`lucky(query, offset=k, sort=[...])` returns the `k`-th match in the sort order
(0-indexed).  `raw`/`raw_values` behave as in `search` and are incompatible
with `join`.

```python
nf.lucky({'degree': 2, 'disc_sign': 1, 'disc_abs': 5}, projection=0)  # '2.2.5.1'
nf.lucky({'label': '6.6.409587233.1'}, projection=['regulator'])
    # {'regulator': 455.191694993}
nf.lucky({'label': 'no.such.label'})   # None
```

### `lookup`

```python
lookup(label, projection=2, label_col=None, join=None)
```

A convenience wrapper: `lookup(label)` returns the single record whose label
column equals `label` (or `None`).  It is exactly
`lucky({label_col: label}, projection=projection, sort=[], join=join)`, where
`label_col` defaults to the table's label column.  If the table has no label
column and none is supplied, it raises `ValueError`.  Because it delegates to
`lucky` with `sort=[]`, it assumes the label is unique (as labels normally are).

### `exists`

```python
exists(query)
```

Returns a boolean: whether at least one row matches `query`.  Implemented as
`self.lucky(query, projection="id") is not None`, so it fetches a single id
rather than counting.

### `label_exists`

```python
label_exists(label, label_col=None)
```

Returns whether a row with the given label exists: `exists({label_col: label})`.
As with `lookup`, `label_col` defaults to the table's label column and a missing
label column raises `ValueError`.

<a name="random-and-random_sample"></a>
## `random` and `random_sample`

### `random`

```python
random(query={}, projection=0, pick_first=None)
```

Returns one row chosen uniformly at random from those matching `query`
(`projection=0` by default, so a random label), or `None` if nothing matches.
The algorithm depends on the arguments:

- **`pick_first` given** — a column name.  `random` first fetches the distinct
  values of that column (subject to `query`) via `distinct`, chooses one
  uniformly, adds it to the query, and recurses.  This samples *column values*
  uniformly rather than *rows* — so with an unevenly distributed column, rows
  with rare values are over-represented.  The distinct-value list is computed
  and held in memory, so `pick_first` should name a column with few distinct
  values.

- **Non-empty `query`, no `pick_first`** — `random` asks the counts cache
  (`stats.quick_count`) how many rows match:
  - If the count is **cached**, it picks a uniform `offset` in `[0, count)` and
    returns `lucky(query, projection=projection, offset=offset, sort=[])`.
    Because the offset is applied to an **unsorted** query, this is a valid
    uniform draw only if the (arbitrary) row order is stable between the count
    and the fetch.
  - If the count is **not cached**, it must materialize the match set to sample
    uniformly: it fetches all matching labels (`projection=0`) or all matching
    ids (any other projection) unsorted, records the resulting count in the
    counts table as a side effect, and returns a `random.choice` (re-fetching
    the full row by id if a non-label projection was requested).  `None` if the
    set is empty.

- **Empty `query`, no `pick_first`** — an id-based retry loop.  `random` reads
  `MIN(id)` and `MAX(id)`, and up to 100 times picks a random id uniformly in
  that range and tries to fetch it, returning the first hit.  This is fast and
  index-only, but if ids are sparse (many rows deleted) it can fail; after 100
  misses it raises `RuntimeError("Random selection failed!")`.  On a genuinely
  empty table (no rows) it returns `None`.

> Side-effect note: on a counts-cache miss the uncached non-empty-`query` branch
> computes the exact count (it materializes the full match set anyway) and
> records it in the counts table — but only when count-saving is enabled, the
> same rule `count` follows.  See [Counts and statistics](#counts-and-statistics).

### `random_sample`

```python
random_sample(ratio, query={}, projection=1, mode=None, repeatable=None, silent=False)
```

Returns an approximately-`ratio` fraction of the rows matching `query`.  `ratio`
must be in `(0, 1]`; anything else raises `ValueError`.  The fraction is
approximate (except in `choice` mode) and the amount of randomness varies by
mode:

- **`mode='system'`** — PostgreSQL `TABLESAMPLE SYSTEM`: the fastest option, but
  it samples random *pages* rather than random rows, which clusters the sample.
  The `WHERE` clause is applied *after* sampling.
- **`mode='bernoulli'`** — `TABLESAMPLE BERNOULLI`: each row is included
  independently with probability `ratio`, then the `WHERE` clause is applied.
  Slower than `system` but not clustered.
- **`mode='choice'`** — fetch *all* rows matching `query`, then take a
  `random.sample` of `int(len(results) * ratio)` of them.  Slow when many rows
  match, but accurate on the ratio and efficient when few match.
- **`mode=None` (default)** — choose automatically: `bernoulli` if
  `count(query) > _count_cutoff`, else `choice`.

`repeatable` is an integer random seed for reproducible samples (it seeds
Python's RNG in `choice` mode, and becomes the SQL `REPEATABLE (seed)` clause in
`system`/`bernoulli` mode).  `silent` suppresses slow-query warnings.

**Return type varies by mode** (unlike `search`): `choice` returns a **list**
(from `random.sample`); `system` and `bernoulli` return an **iterator** (they
run an unlimited `search`-style query on a server-side cursor); and `ratio == 1`
short-circuits to `search(query, projection, sort=[])`, which is also an
iterator.  Wrap in `list(...)` if you need a uniform container.

<a name="counts-and-statistics"></a>
## Counts and statistics

These methods live on the search table and (except `distinct`) delegate to the
public statistics table `table.stats`.  Two auxiliary tables back the cache:
`<table>_counts` (row counts for queries) and `<table>_stats` (distinct-value
counts and min/max/sum statistics); the table's total row count is additionally
cached in memory and in `meta_tables.total`.

### `count`

```python
count(query={}, groupby=None, record=True, join=None)
```

Returns the number of rows matching `query`.

- **Empty query** (`{}`) is answered from the **cached total** (`stats.total`,
  mirrored in `meta_tables`) — no scan.
- **Non-empty query** is looked up in the `<table>_counts` cache
  (`quick_count`); on a miss it runs `SELECT COUNT(*)` (`_slow_count`) and, if
  recording is enabled, stores the result.
- **`groupby`** — a list of columns.  Instead of a single integer, returns a
  dictionary mapping each distinct tuple of values of those columns (a 1-tuple
  for a single column) to its count, via `GROUP BY`.  Grouped counts are never
  cached, and `groupby` is not supported together with `join`.
- **`record`** — see [The `record` parameter](#the-record-parameter) below.
- **`join`** — see [Joins](#joins); joined counts are computed directly and
  never cached, and `record`/`groupby` do not apply.

```python
nf.count({'degree': 6, 'galt': 7})          # 244006
nf.count({}, groupby=['degree'])            # {(1,): ..., (2,): ..., ...}
```

### `count_distinct`

```python
count_distinct(col, query={}, record=True)
```

Returns the number of distinct values taken by `col` (a column name or a list of
column names) among rows matching `query`.  It checks the `<table>_stats` cache
(`quick_count_distinct`) and, on a miss, runs `COUNT(DISTINCT …)` and optionally
records it.  Equivalent to `len(distinct(col, query))` for a single column, but
faster and cacheable.

### `distinct`

```python
distinct(col, query={})
```

Returns the sorted list of distinct values of `col` among rows matching
`query`, via `SELECT DISTINCT … ORDER BY`.  Unlike `count_distinct`, `distinct`
is **never cached** — every call scans.

### `max`, `min`, `sum`

```python
max(col, constraint={})
min(col, constraint={})
sum(col, constraint={})
```

Return the maximum, minimum and sum of `col` over rows matching `constraint`.
Each checks the `<table>_stats` cache first (`_quick_statistic`) and, on a miss,
computes the value (`_slow_statistic`) and may record it.

- `max`/`min` raise `ValueError` if there are no non-null values of `col` in the
  constrained set.
- `max('id')` is special-cased to return `count()` (the table's row count).
- Note the argument name is **`constraint`** here, not `query` as elsewhere;
  the meaning is the same (a query dictionary).

### The `record` parameter

`count` and `count_distinct` take `record=True`, and `max`/`min`/`sum` record by
default; the intent is to persist a freshly computed value into the cache tables
so later calls are fast.  **On this branch, whether recording actually happens
depends on the `PostgresStatsTable.saving` flag, which is `False` by default in
psycodict** (it is meant to be enabled by a subclass in a data-management
deployment).  With the default `saving = False`:

- `count(..., record=True)`, `count_distinct(..., record=True)`, `max`, `min`
  and `sum` record **nothing** — the `record` argument is effectively inert, and
  the computed value is returned without being cached.
- `random` behaves the same way: on a counts-cache miss it computes the exact
  count as a side effect (it has the full match set in hand), but stores it only
  when `saving = True`.

When a subclass sets `saving = True`, all of the above record their results and
this is the intended mode for the tools that populate a database.  (Writes still
fail silently if the connection lacks write permission, e.g. a read-only web
user, so recording is best-effort even under `saving`.)

<a name="joins"></a>
## Joins

`search`, `lucky`, `lookup` and `count` accept a `join` argument that makes
columns of other search tables available to the query, projection, sort and
results.  **The join language itself — the shape of the `join` list, name
resolution, which special keys apply, `$col`/`$raw` across tables — is specified
in the Joined queries section of [QueryLanguage.md](QueryLanguage.md) and is not
repeated here.**  This section states only how `join` changes the read-method
contracts documented above.

- **Result keys.**  Result dictionaries use the projection entries **verbatim**
  as keys, so a joined column projected as `"other_table.col"` comes back under
  that exact qualified string.  Integer projections (`0`/`1`/`2`/`3`) refer to
  the primary table's columns only.
- **Projections.**  Only string, list and integer projections are allowed;
  **dictionary** projections raise `ValueError` with `join`.  The `"col[i]"`
  slicer form is still accepted.
- **Return types and the `info` contract are unchanged** — iterator vs. list by
  `limit`, the same five `info` keys, the same past-the-last-page adjustment —
  with one difference in how counts are obtained (next point).
- **Counts are never cached.**  The count used for `info["number"]` (and the
  result of `count(..., join=…)`) is computed directly with `SELECT COUNT(*)`
  over the joined `FROM` clause every time; the counts cache and the `record`
  parameter play no role.  Empty-query joined counts are **not** shortcut
  through the total.
- **Unsupported options.**  `split_ors`, `one_per`, `raw`/`raw_values` and
  `groupby` do not combine with `join` and raise `ValueError`.

```python
db.ec_nfcurves.search(
    {"rank": 1, "nf_fields.r2": 1},
    ["label", "nf_fields.degree"],
    join=[("field_label", "nf_fields.label")],
    limit=3)
# [{'label': '2.0.1003.1-9.1-c1', 'nf_fields.degree': 2}, ...]
```

<a name="result-value-semantics"></a>
## Result-value semantics

### `None` values and `include_nones`

When a projection produces a dictionary, whether columns whose value is SQL
`NULL` appear in that dictionary is governed by the table's `include_nones`
setting (a **per-table** meta setting, `_include_nones`, default `True`):

- **`include_nones = True` (default)** — every projected column is present in
  the result dictionary, with `None` for nulls.  This applies uniformly to
  `search` (iterator and list forms), `lucky`, `lookup` and joined queries.
- **`include_nones = False`** — key/value pairs whose value is `None` are
  **omitted** from the result dictionary, so a returned dict contains only the
  non-null columns among those projected and callers must treat a missing key
  as "null" (e.g. with `dict.get`).  Pass `include_nones=False` at
  `create_table` to select this per table.

Because the setting is per-table, two tables in the same database can differ,
and the same projection can yield dicts with different key sets depending on the
table it came from.  (Bare-value projections — `0` or a string — are unaffected:
they can return `None` as the value.)

This interacts with left/right/full joins: an unmatched side contributes `NULL`
columns.  Under the default `include_nones = True` they appear as `None`; under
`include_nones = False` they simply do not appear in the result dict — so on a
table set that way, to detect unmatched rows project a column you expect to be
non-null and test for its absence, or query `{"table.col": None}`.  See the
Joined queries section of [QueryLanguage.md](QueryLanguage.md).

### Type mapping in results

Values come back as Python objects decoded from PostgreSQL by psycopg plus
psycodict's registered adapters (`psycodict/encoding.py`,
`psycodict/database.py`):

- **`jsonb`/`json`** columns decode to the corresponding Python objects — dicts,
  lists, strings, numbers, booleans, `None` — natively.  Array and composite
  json values thus become nested Python structures.
- **Array** columns (`integer[]`, `numeric[]`, …) decode to Python lists.
- **`numeric`/`decimal`** columns go through a custom converter
  (`numeric_converter`): a value with no decimal point becomes an integer and a
  value with a decimal point becomes a real number.  The exact Python type
  **depends on whether Sage is importable**: with Sage, integers become Sage
  `Integer`s and decimals become Sage real literals (`LmfdbRealLiteral`, whose
  precision tracks the number of digits and which re-prints as it was stored) —
  except an all-zero decimal, which becomes an exact integer zero
  (`LmfdbDecimalZero`) so it does not drag a partner down to a few bits of
  precision in later arithmetic;
  without Sage, they fall back to Python `int` and `float` respectively.  Other
  numeric SQL types (`integer`, `bigint`, `double precision`, `boolean`, `text`)
  map to the obvious Python types.

See `psycodict/encoding.py` for the full adapter set and the Sage-aware
encoding/decoding of additional types.
