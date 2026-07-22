
## Introduction

This guide covers getting data *into* a psycodict database and maintaining it: creating tables, writing rows, the bulk file-based import/export tools that psycodict is built around, and the statistics machinery.  For reading data out (the dictionary-to-`WHERE` translation used by `search`, `count`, `lucky` and `lookup`), see [QueryLanguage.md](QueryLanguage.md).

Everything here is a method on either the database object (`db`) or a search table (`db.mytable`).  psycodict was written for the LMFDB, where tables are large and change rarely, so the design favors bulk file operations and cached statistics over row-by-row mutation.

A note on permissions: most write methods first check `db._read_only`, and several branches of behavior depend on whether your table's statistics class has `saving` turned on (see [Statistics and counts](#statistics-and-counts)).  A bare psycodict install has `saving = False`.

## Creating tables

### `create_table`

```python
db.create_table(
    "demo_circles",
    {"integer": ["radius"], "text": ["label"], "integer[]": ["center"], "jsonb": ["props"]},
    label_col="label",
    sort=["radius"],
)
```

 * **`name`** must contain an underscore.  The call also creates the companion tables `name_counts` and `name_stats`, and writes one row to `meta_tables`.
 * **`search_columns`** takes two forms: a dictionary whose keys are Postgres types and whose values are lists of column names (a bare string is allowed for a single column), or a list of `(column, type)` pairs.  The two forms are interchangeable.
 * **the `id` column** is added automatically as a `bigint` primary key unless you already list one; use `id_type=` to choose a different integer type.  Columns are physically laid out ordered by type (widest alignment first) for storage efficiency, so the on-disk column order is not your declaration order — but every file operation is header-driven, so this never matters to you.
 * **`label_col`** names the column used by `lookup`; it must be one of the search columns, or `None`.
 * **`sort`** is the default sort order: a list of column names or `(column, 1|-1)` pairs.
 * **`id_ordered`** defaults to `True` when `sort` is given, `False` otherwise.  It records the intent that, in production, the `id` column runs in the same order as `sort` (which lets some range queries use the primary-key index).  It does **not** sort anything now; see [resorting is disabled](#resorting-is-disabled).

The most common Postgres types are `smallint`/`integer`/`bigint`, `numeric` (exact), `real`/`double precision`, `text`, `boolean`, `jsonb`, `timestamp`, and the array forms (`integer[]`, `numeric[]`, ...).

### `create_table_like`

```python
db.create_table_like("demo_copy", "demo_circles")            # schema only
db.create_table_like("demo_copy", "demo_circles", data=True, indexes=True)
```

Copies the column layout, `label_col`, `sort`, `id_ordered`, `id` type and descriptions from an existing table.  By default it copies **no** data, **no** indexes and **no** statistics; pass `data=True` and/or `indexes=True` to include them.

### The meta tables

psycodict keeps its own bookkeeping in a handful of tables that live alongside your search tables:

 * **`meta_tables`** — one row per search table, holding `name`, `sort`, `count_cutoff`, `id_ordered`, `out_of_order`, `stats_valid`, `label_col`, `total`, `important` and `include_nones`.  This is the source of truth psycodict reads on connection to reconstruct each table object.
 * **`meta_indexes`** and **`meta_constraints`** — one row per index / constraint, recording how to rebuild it.  `reload` and `restore_indexes` rebuild from these rows, **not** from whatever is physically on the table (see [reload](#reload)).
 * **`meta_tables_hist`**, **`meta_indexes_hist`**, **`meta_constraints_hist`** — versioned history of the three tables above, so that `reload_meta`/`revert_meta` can roll a table's metadata forward and back.
 * **`meta_format`** — a single-row `(version, min_compat)` stamp of the metadata *format*: the layout of the `meta_*` tables, versioned by an integer aligned with psycodict's major version (`META_FORMAT`).  Every connection checks it.  The same format connects silently; an older but compatible format connects with a warning and operates at that older format (newer features unavailable) so a not-yet-migrated or read-only database — the LMFDB devmirror, say — keeps working; a newer format connects when its stamped `min_compat` admits this psycodict, and is otherwise refused.  A database that has meta tables but no `meta_format` is the unstamped 0.x baseline (format 0); one with no meta tables at all is fresh and must be connected to with `PostgresDatabase(create=True)`, which bootstraps all of the above.  Migrating to a newer format is deliberate — `db.upgrade_metadata()` or `PostgresDatabase(upgrade=True)`, never a side effect of connecting.  See [MetadataFormats.md](MetadataFormats.md) for the full policy (including why a pre-1.0 psycodict must not be pointed at a migrated database).

## Row-level writes

These mutate the live table directly.  They are convenient for small edits; for anything large prefer the [file operations](#bulk-file-operations).  Each records an entry in the change log and, on success, updates the maintained row `total`.

 * **`insert_many(data, resort=False, reindex=None, restat=True)`** — `data` is a list of dictionaries that must all have the same keys.  Each row is assigned `id = max_id() + 1` (so the first row of an empty table gets `id = 0`), and the caller's dictionaries are updated in place with the assigned ids.  `reindex` defaults to `True` when there are more than 1000 rows, dropping the indexes and primary key around the insert and rebuilding them after — if an exception interrupts this you must call `restore_indexes` yourself.  Faster than repeated `upsert`, slower than `copy_from`.
 * **`upsert(query, data)`** — updates the single row matching `query`, or inserts one if none matches; raises if `query` matches more than one row.  Returns `(new_row, row_id)`.  You cannot set `id`.  A new insert uses `id = max_id() + 1`.
 * **`update(query, changes, resort=False, restat=True)`** — a plain SQL `UPDATE` of every row matching `query`; `changes` maps column names to constants.
 * **`delete(query, restat=True)`** — deletes every row matching `query` and decrements `total`.

**Statistics invalidation.**  Any write that can change the data calls `_break_stats`, which sets `meta_tables.stats_valid = false` so that cached statistics are known to be stale.  If the table has `saving` on and you left `restat=True`, statistics are refreshed at the end of the call; otherwise they are simply marked invalid.  Inserting rows (and updating a sort-key column) also calls `_break_order`, setting `out_of_order = true` to record that the `id` order no longer matches `sort`; `delete` leaves the order flag alone.

### Resorting is disabled

`resort()` is a **no-op on this branch**: it prints `resorting disabled` and returns `None` without touching the table.  In-place resorting was found to stall replication and to not persist correctly on disk, and since the tables are effectively read-only in production the supported way to renumber ids is to dump the data in sorted order and `reload` it.  Consequently the `resort=` keyword on `insert_many`, `update`, `copy_from`, `reload` and `update_from_file` currently does nothing.

### Locking

Before mutating, each method calls `_check_locks`, which asks Postgres (via `pg_locks`) whether any *other* session holds a lock that conflicts with the operation (inserts, updates and deletes each map to their own set of conflicting lock modes).  If so, it prints the offending lock types and process ids and raises `LockError`:

```
AccessExclusiveLock   51870
LockError: Table is locked.  Please resolve the lock by killing the above processes and try again
```

Use [`db.show_locks()`](#operational-tips) to see who holds what, and resolve the conflict before retrying.

## Bulk file operations

This is the heart of psycodict.  The workflow is: `copy_to` a table (or the current data) to a file, and `copy_from`/`reload`/`update_from_file` a file back in.  The file format is shared by all of them.

### The file format

A **search file** begins with three header lines followed by one line per row:

 1. the column names, separated by the delimiter (default `|`), with `id` first if present;
 2. the Postgres type of each column, in the same order;
 3. a blank line.

The reader (`_read_header_lines`/`_check_header_lines`) checks that these names and types match the table before loading, so the file is self-describing and column order is whatever the header says.  The default delimiter is `|` (do **not** use a comma) and the default null marker is `\N`.  Values are written by Postgres' `COPY` (mirrored on the write side by `copy_dumps`, which `rewrite` uses):

 * text is written literally, with the delimiter, newlines and backslashes escaped; a whole-column SQL `NULL` becomes `\N`;
 * numbers, `date`/`timestamp` and booleans (`t`/`f`) are written as literals;
 * arrays use Postgres array syntax `{1,2,3}`; the empty array is `{}`, a `NULL` *array* is `\N`, and a `NULL` *element inside* an array is the bare word `NULL`;
 * `jsonb` is written as JSON text — note Postgres normalizes it, so keys come back reordered and whitespace regularized.

Here is a genuine two-row export (`db.demo_circles.copy_to("demo_circles.txt")`) of the table created above:

```
id|center|label|props|radius
bigint|integer[]|text|jsonb|integer

0|{0,0}|unit|{"unit": true}|1
1|{3,4}|big|{"note": "3-4-5", "unit": false}|5
```

Note the id column first, the `integer[]` written as `{0,0}`, the `jsonb` keys reordered by Postgres, and (had a value contained a `|`) it would be escaped as `\|`.

### `copy_to` and `copy_from`

**`copy_to(searchfile, countsfile=None, statsfile=None, indexesfile=None, constraintsfile=None, metafile=None, columns=None, query=None, include_id=True)`** exports via `COPY ... TO STDOUT`.  Only the *search* file gets the three header lines; the counts, stats, meta, index and constraint files are headerless raw rows (their column layout is fixed and known).  `query` restricts which rows are exported; `columns` restricts which columns.  The counts and stats files are written only if the table has `saving` on.

**`copy_from(searchfile, resort=False, reindex=None, restat=True)`** streams a search file into the **existing** table via `COPY ... FROM STDIN`, *appending* rows.  It validates the header, and if the file has no `id` column it assigns ids contiguously starting just after the current maximum (using a temporary sequence).  `reindex` defaults to `True` for files with more than 1000 data rows.  `total` is incremented and statistics invalidated.

### `reload`

`reload(searchfile, countsfile=None, statsfile=None, indexesfile=None, constraintsfile=None, metafile=None, ...)` is the safe way to *replace* a table's contents.  It is a **clone-build-swap**:

 1. clone the live table's schema into `name_tmp` (via `CREATE TABLE ... (LIKE ...)`, which copies the column names, their types and `NOT NULL` constraints, but not defaults, storage settings, indexes, constraints or the primary key);
 2. `COPY` the file into `name_tmp`;
 3. restore the primary key and rebuild indexes/constraints on `name_tmp` from `meta_indexes`/`meta_constraints` (or from `indexesfile`/`constraintsfile`);
 4. **final swap**: rename the live `name` to `name_old<n>` and `name_tmp` to `name`.

Because of the swap, the previous table survives as **`name_old<n>`**, where `<n>` is the next unused backup number — the live table is never mutated in place, so a failed load leaves the original untouched.  A second reload produces `name_old2` (and warns you that backups are accumulating).  When `saving` is on, the `_counts` and `_stats` tables are cloned and swapped the same way.

Sharp edges — what reload does **not** preserve:

 * indexes and constraints are rebuilt from the `meta_indexes`/`meta_constraints` catalog, **not** from whatever is physically on the live table.  An index that exists on the table but is missing from the catalog is silently lost.
 * the `meta_tables` row (`sort`, `label_col`, `id_ordered`, ...) is updated **only** if you pass a `metafile`; otherwise it is kept as-is.
 * old ids survive **only** if the search file contains an `id` column — which `copy_to` writes by default (`include_id=True`).

Pass `final_swap=False` to build `name_tmp` without swapping (then finish later with `reload_final_swap`), or `adjust_schema=True` to build the temp table's columns from the file header rather than requiring them to match the current schema.

### Reverting and cleaning up

 * **`reload_revert(backup_number=None)`** swaps the live table with `name_old<n>` (the most recent backup by default); calling it twice returns you to where you started.  It refuses to run while a `name_tmp` table is still present — a sign a reload did not finish — and tells you to `drop_tmp` first.
 * **`drop_tmp()`** drops the `_tmp` tables left behind by an interrupted reload.
 * **`cleanup_from_reload(keep_old=0)`** drops the `_tmp` and all `_old*` backup tables (keeping the `keep_old` most recent, renumbered from 1).  Running this makes `reload_revert` impossible, so do it only once you are confident in the new data.

### `update_from_file` and `rewrite`

**`update_from_file(datafile, label_col=None, *, inplace=False, resort=None, reindex=None, restat=True)`** updates *existing* rows.  Unlike `reload`, the file need only contain a **subset** of the columns; its first column must be the key that identifies rows (`label_col`, defaulting to the table's label column), and the remaining columns are overwritten on the matching rows.  You cannot update `id`.  By default (`inplace=False`) it builds the updated table by cloning and swapping, so it leaves a `name_old<n>` backup and is revertible with `reload_revert`; `inplace=True` does a direct SQL `UPDATE` (faster, but not revertible).  The parameters after `label_col` are keyword-only.  `reindex` is meaningful only with `inplace`, where it drops the indexes touching the updated columns and rebuilds them afterward (defaulting to that when more than 1000 rows change); the clone-and-swap path always rebuilds every index, so `reindex=True` there is redundant and `reindex=False` raises.

For example, a file whose header is `label|m` (plus a type line and a blank line) with body `l1|111` / `l3|333` sets column `m` on rows `l1` and `l3` and leaves every other column, and every other row, untouched.

**`rewrite(func, query={}, ...)`** reads every row matching `query`, applies `func(record) -> record` in Python, writes the results to a temporary file and feeds it through `update_from_file`.  Use it to transform columns wholesale or to populate a new column — but call `add_column` first, since `rewrite` will not add columns for you.

## Statistics and counts

Every search table has two companion tables:

 * **`name_counts`** caches result counts: columns `cols` (a JSON array of column names), `values` (a parallel JSON array of the values queried), `count`, `extra` (whether this count came from an ad-hoc user search rather than a systematic pass), and `split`.
 * **`name_stats`** stores aggregates: columns `cols`, `stat` (`total`, `avg`, `min`, `max`, `distinct`, ...), `value`, `constraint_cols`, `constraint_values`, and `threshold`.

**`stats.saving`.**  The default `PostgresStatsTable` has `saving = False`, meaning psycodict does **not** persist counts or statistics — the companion tables exist but stay empty.  To turn persistence on you subclass `PostgresStatsTable` with `saving = True` and point your search-table class's `_stats_table_class_` at it (the LMFDB does exactly this).  Almost every branch below that mentions "recording" is gated on `saving`.

**The total is special.**  The row count for the empty query is maintained in `meta_tables.total` on *every* write path regardless of `saving`, and `count({})` is answered from it in O(1).  So the grand total is always cheap and always available even with `saving` off.

**Building stats.**  `add_stats(cols, constraint=None, threshold=None, split_list=False)` records, for each distinct tuple of values of `cols` occurring at least `threshold` times, its count into `name_counts`; for a single numeric column it also records `total`/`avg`/`min`/`max` into `name_stats`.  `refresh_stats(total=True)` recomputes everything currently recorded — it uses the existing `total` stat rows to know *which* statistics to regenerate, and preserves the `extra = true` counts accumulated by user searches.

**Count caching contract** (from the current `count` docstrings).  A non-joined `count(query, record=True)` first consults the `name_counts` cache (`quick_count`); on a miss it runs a real `SELECT COUNT(*)` and, when `record` and `saving` are both true, records the result back into `name_counts`.  With the default `saving = False` this means nothing is ever cached — every `count` is a fresh scan (except `count({})`, always answered from `total`).  A **joined** count (`count(query, join=...)`) is computed directly and **never** cached, so `record` is ignored there.  `count_cutoff` (a `meta_tables` column, default 1000) is the threshold above which search pages report a cached or bounded count rather than paying for an exact scan.

## Operational tips

 * **`db.show_locks()`** prints every lock currently held on any table — name, mode, pid and age.  This is how you find the process id to kill when a write raised `LockError`.
 * **`table.analyze(query, projection=1, limit=1000, sort=None, explain_only=False)`** prints the SQL it would run and its `EXPLAIN ANALYZE` plan (or just `EXPLAIN`, with `explain_only=True`).  Use it when tuning indexes for a slow search.
 * **Slow-query logging** is configured in `config.ini`: `[logging] slowcutoff` (seconds, default `0.1`) and `slowlogfile` (default `slow_queries.log`).  Any statement slower than `slowcutoff` is logged with its interpolated SQL; slow searches additionally log a `Replicate with db.table.analyze(...)` hint so you can reproduce them.
 * On **PostgreSQL 18+** a `reload` or `update_from_file` prints a harmless warning like `Constraint of ... with name ..._id_not_null does not end with the suffix _tmp` during the swap — Postgres now catalogs `NOT NULL` as a named constraint that the swap logic does not recognize.  The constraint is preserved; the warning can be ignored.
