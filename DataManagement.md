# Data management

## Introduction

This guide covers getting data *into* a psycodict database and maintaining it: creating tables, writing rows, the bulk file-based import/export tools that psycodict is built around, and the statistics machinery.  For reading data out (the dictionary-to-`WHERE` translation used by `search`, `count`, `lucky` and `lookup`), see [QueryLanguage.md](QueryLanguage.md).

Everything here is a method on either the database object (`db`) or a search table (`db.mytable`).  psycodict was written for the LMFDB, where tables are large and change rarely, so the design favors bulk file operations and cached statistics over row-by-row mutation.

A note on permissions: most write methods first check `db._read_only`, and several branches of behavior depend on whether your table's statistics class has `saving` turned on (see [Statistics and counts](#statistics-and-counts)).  A bare psycodict install has `saving = False`.

Permissions on the relations psycodict *creates* are a separate matter, decided by the database's grant policy (`psycodict.grants`).  The default policy grants nothing, so a new search table, its `_counts` and `_stats` companions and the meta tables are reachable only by their owner.  A deployment that wants the LMFDB's roles asks for them:

```python
from psycodict.grants import LMFDBGrantPolicy
db = PostgresDatabase(grant_policy=LMFDBGrantPolicy())
```

which grants `SELECT` to `lmfdb` and `webserver` and `INSERT` on the counts and stats tables to `webserver`, as psycodict used to do unconditionally.  A policy names a role that must exist: by default a policy naming a role the cluster does not have raises — at connection time, rather than partway through the first thing that creates a relation — and `missing_role="skip"` (what `LMFDBGrantPolicy` uses, since development databases rarely have those roles) warns and carries on.

The policy is authoritative **for the roles it names**.  Applying it revokes `SELECT`, `INSERT`, `UPDATE` and `DELETE` from those roles before granting what the policy says, so a relation psycodict creates or swaps ends up with the policy rather than with the policy plus whatever it inherited; roles the policy does not name are never touched.  That distinction matters for the default policy, which names no roles at all: it therefore revokes nothing.  A *freshly created* relation has no non-owner grants to begin with, so under the default it stays owner-only — but a relation psycodict *renames* (a backup, say) keeps the ACL it already had, and the default policy will not take it away.  `LMFDBGrantPolicy` does take it away, because it names `lmfdb` and `webserver` and gives the `backup` kind nothing.

Policies are applied where psycodict creates or swaps a relation, not on connection: pointing a policy at an existing database does not retroactively change the privileges on relations that are already there.  Cleaning up historical ACLs is an administrative job.

This matters most for the `_oldN` table a [reload](#reload) leaves behind: PostgreSQL carries privileges along with a rename, so the backup would otherwise stay as readable as the live table it used to be, while holding a copy of the same production data.  The `backup` relation kind decides what it gets instead — nothing, under `LMFDBGrantPolicy`.  `reload_revert` applies the same rule in the other direction, so reverting does not hand the restored table its backup's privileges.  The legacy `_oldstats` relation has its own kind, and gets read access only.

The reverse is worth knowing too: a reload swaps in a clone, and PostgreSQL creates a clone with no privileges of its own, so under the default policy the replacement is reachable only by its owner even if the table it replaced was readable by an application role.  psycodict warns when a swap takes access away like this, naming the roles that lost it.  `db.grant_select` and its siblings remain, for granting something to roles you name explicitly.

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

 * **`name`** must be lowercase letters, digits and underscores, start with a letter, be at most **49 bytes** (`MAX_SEARCH_TABLE_NAME_LENGTH`), and not end in one of the suffixes psycodict appends to a search table's own name (`_counts`, `_stats`, `_pkey`, `_tmp`, `_oldN`, `_depN`) or collide with an attribute of the database object.  By convention it contains an underscore.  The length limit is PostgreSQL's 63-byte identifier limit less the room psycodict needs for the relations it names after the table — the longest is `<name>_counts_old<N>` — because PostgreSQL truncates a longer name rather than refusing it, which would make two of those relations the same relation.  The name is used as a PostgreSQL identifier, as the key the table object is reached by (`db.name`), and as the stem of the files `copy_to` writes.

   A name already in `meta_tables` is held to less: it may be up to the full 63 bytes and may be spelled in ways a new name would not be — the LMFDB has a search table called `hgcwa_per_group_stats` — but it must be a name that can only mean itself.  `refresh_tables` reads every row, checks them as a set against the relations the database actually has, and only then updates the registry, so nothing is acted on before a bad row is found.  A row is refused if it names one of psycodict's own relations (`meta_tables` and friends), if it ends in scratch space (`_tmp`, `_oldN`, `_depN`, `_pkey`), if two rows claim the same relation (a row for `foo_counts` when `foo` is a search table), if it exceeds 63 bytes, or if the database does not have all three of `name`, `name_counts` and `name_stats`.

Renaming a table renames its indexes and constraints with it, by substituting the table's name into theirs.  A short name replaced by a long one can push the result past the identifier limit, so every dependent name is planned before anything is renamed: each is fitted the same way psycodict fits any derived name, a fitted name that is already taken is moved aside — by any relation in the schema (index, sequence or table, since they share one namespace) or by one of the table's own constraints, which for a `CHECK` constraint exists only in `pg_constraint` and not as a relation at all, index and constraint targets are allocated from one set so two dependents cannot be given the same name, and the same spelling goes into the current metadata, the history metadata and the `ALTER`.  A dependent whose name does not mention the table keeps it, and is not renamed to itself; those are reserved first across *both* kinds, so a dependent that is being renamed cannot take a name another one is keeping — including a name only the history remembers, which no catalog holds, and including one held by a dependent of the other kind, since PostgreSQL keeps index and constraint names in different catalogs and a table may legitimately have one of each by the same name.  Metadata updates are scoped to the table being renamed: a constraint name is only unique within its own table, so two tables may share one.  A rename that cannot allocate a name, or whose destination relations would not fit, raises before renaming anything.  The call also creates the companion tables `name_counts` and `name_stats`, and writes one row to `meta_tables`.
 * **`search_columns`** takes two forms: a dictionary whose keys are Postgres types and whose values are lists of column names (a bare string is allowed for a single column), or a list of `(column, type)` pairs.  The two forms are interchangeable.
 * **the `id` column** is added automatically as a `bigint` primary key unless you already list one; use `id_type=` to choose a different integer type.  Columns are physically laid out ordered by type (widest alignment first) for storage efficiency, so the on-disk column order is not your declaration order — but every file operation is header-driven, so this never matters to you.
 * **`label_col`** names the column used by `lookup`; it must be one of the search columns, or `None`.
 * **`sort`** is the default sort order: a list of column names or `(column, 1|-1)` pairs.
 * **`id_ordered`** defaults to `True` when `sort` is given, `False` otherwise.  It records the intent that, in production, the `id` column runs in the same order as `sort` (which lets some range queries use the primary-key index).  See [resorting](#resorting).

The most common Postgres types are `smallint`/`integer`/`bigint`, `numeric` (exact), `real`/`double precision`, `text`, `boolean`, `jsonb`, `timestamp`, and the array forms (`integer[]`, `numeric[]`, ...).

### `create_table_like`

```python
db.create_table_like("demo_copy", "demo_circles")            # schema only
db.create_table_like("demo_copy", "demo_circles", data=True, indexes=True)
```

Copies the column layout, `label_col`, `sort`, `id_ordered`, `id` type and descriptions from an existing table.  By default it copies **no** data, **no** indexes and **no** statistics; pass `data=True` and/or `indexes=True` to include them.

### The meta tables

psycodict keeps its own bookkeeping in a handful of tables that live alongside your search tables:

psycodict operates in one PostgreSQL schema, `public` unless the constructor is
given another (`PostgresDatabase(schema="myschema")`).  Every relation it
creates goes there, every relation it looks for is looked for there, and every
catalog query is filtered to it, so a relation of the same name in another
schema is neither mistaken for one of these nor merged with it.  The schema is
pinned in each connection's `search_path`, including replacement connections
after a reconnect.

 * **`meta_tables`** — one row per search table, holding `name`, `sort`, `count_cutoff`, `id_ordered`, `out_of_order`, `stats_valid`, `label_col`, `total`, `important` and `include_nones`.  This is the source of truth psycodict reads on connection to reconstruct each table object.
 * **`meta_indexes`** and **`meta_constraints`** — one row per index / constraint, recording how to rebuild it.  `reload` and `restore_indexes` rebuild from these rows, **not** from whatever is physically on the table (see [reload](#reload)).
 * **`meta_tables_hist`**, **`meta_indexes_hist`**, **`meta_constraints_hist`** — versioned history of the three tables above, so that `reload_meta`/`revert_meta` can roll a table's metadata forward and back.
 * **`meta_format`** — a single-row `(version, min_compat)` stamp of the metadata *format*: the layout of the `meta_*` tables, versioned by an integer aligned with psycodict's major version (`META_FORMAT`).  Every connection checks it.  The same format connects silently; an older but compatible format connects with a warning and operates at that older format (newer features unavailable) so a not-yet-migrated or read-only database — the LMFDB devmirror, say — keeps working; a newer format connects when its stamped `min_compat` admits this psycodict, and is otherwise refused.  A database that has meta tables but no `meta_format` is the unstamped 0.x baseline (format 0); one with no meta tables at all is fresh and must be connected to with `PostgresDatabase(create=True)`, which bootstraps all of the above.  Migrating to a newer format is deliberate — `db.upgrade_metadata()` or `PostgresDatabase(upgrade=True)`, never a side effect of connecting.  See [MetadataFormats.md](MetadataFormats.md) for the full policy (including why a pre-1.0 psycodict must not be pointed at a migrated database).

Rows of `meta_indexes` and `meta_constraints` become DDL when an index or
constraint is rebuilt, so psycodict validates them both when they are imported
(`reload_indexes`, `reload_constraints`, `revert_indexes`, `revert_constraints`)
and again immediately before the `CREATE INDEX` or `ALTER TABLE` statement is
built. The access method, the column modifiers, the storage-parameter names and
the check function of a `CHECK` constraint must be ones psycodict recognizes,
and are emitted as quoted identifiers or fixed SQL rather than as the stored
text. A metadata file is therefore no more trusted than the database it is
loaded into: an invalid definition raises, and because the import runs in a
transaction, the metadata that was there before is left untouched. A `CHECK`
constraint's function must be listed in `PostgresTable._valid_check_functions`
both to create it and to rebuild it.

Index and constraint names are quoted like every other identifier, so a name may contain punctuation, spaces or non-ASCII characters; what psycodict refuses is an empty name, a control character (its own policy, so that names stay printable in logs and export files) and a name longer than PostgreSQL's 63-*byte* identifier limit, which the server would silently truncate into something the metadata does not name.

The distinction that matters is between a name that is *stored* and a name psycodict *derives*:

 * A **stored** name — an index name in `meta_indexes`, a constraint name in `meta_constraints`, the table they refer to — is what the relation is called, so it must fit exactly.  A name over the limit is refused wherever it arrives: from `create_index`, from a reloaded metadata file, from a reverted history version, or from a row edited in place.  It is never shortened to make it usable, because the shortened name is not the name the metadata records.
 * A **derived** name — `_tmp`, `_oldN`, `_depN`, `_pkey` — is one psycodict computes, so it may be shortened: one helper keeps the suffix, cuts the base on a character boundary and inserts a short digest when it has to, and every path that creates, drops or looks for such a relation calls it, so none of them can disagree.  A generated index name is fitted this way too; a name you choose is refused rather than silently changed.

A search table's own relations — `name_counts`, `name_stats` and the `_tmp` and `_oldN` copies of all three — are the exception to the shortening: they are plain concatenations.  PostgreSQL truncates an over-long identifier *consistently*, so a legacy table whose counts table was created over the limit still has it in the catalog under the truncated name and still reaches it by the long one; shortening these would make psycodict look for relations that do not exist.  What truncation does break is distinctness — `<63 bytes>_old1` and `<63 bytes>_old2` are one relation — so a relation psycodict is about to *create* under an over-long name is refused instead, with a message saying to rename the table.  A name within the creation limit can never reach that case.

A `CHECK` constraint's function may be schema-qualified (`schema.function`); psycodict parses it into its components and emits them as `"schema"."function"`, and it must be listed in `PostgresTable._valid_check_functions` both to create the constraint and to rebuild it.  Index storage parameters are normalized: a boolean written `on`, `yes`, `t` or `1` is stored and emitted as `true`, and `gin_pending_list_limit` is checked only for its documented lower bound, since its ceiling depends on the server's architecture.

The predicate of a partial index (`create_index(..., where=...)`) stays raw SQL
— psycodict does not parse it — but it is checked for the things that would let
it stop being a predicate: a semicolon, a comment, a dollar-quoted string, a
control character or an unreasonable length. It is administrative input, not
website input.

## Row-level writes

These mutate the live table directly.  They are convenient for small edits; for anything large prefer the [file operations](#bulk-file-operations).  Each records an entry in the change log and, on success, updates the maintained row `total`.

 * **`insert_many(data, resort=False, reindex=None, restat=True)`** — `data` is a list of dictionaries that must all have the same keys.  Each row is assigned `id = max_id() + 1` (so the first row of an empty table gets `id = 0`), and the caller's dictionaries are updated in place with the assigned ids.  `reindex` defaults to `True` when there are more than 1000 rows, dropping the indexes and primary key around the insert and rebuilding them after — if an exception interrupts this you must call `restore_indexes` yourself.  Faster than repeated `upsert`, slower than `copy_from`.
 * **`upsert(query, data)`** — updates the single row matching `query`, or inserts one if none matches; raises if `query` matches more than one row.  Returns `(new_row, row_id)`.  You cannot set `id`.  A new insert uses `id = max_id() + 1`.
 * **`update(query, changes, resort=False, restat=True)`** — a plain SQL `UPDATE` of every row matching `query`; `changes` maps column names to constants.
 * **`delete(query, restat=True)`** — deletes every row matching `query` and decrements `total`.

**Statistics invalidation.**  Any write that can change the data calls `_break_stats`, which sets `meta_tables.stats_valid = false` so that cached statistics are known to be stale.  If the table has `saving` on and you left `restat=True`, statistics are refreshed at the end of the call; otherwise they are simply marked invalid.

`stats_valid` is enforced rather than merely recorded: while it is false, a
cached nonempty-query count, distinct count, minimum, maximum or sum reports a
miss, and the method computes the answer instead of returning the stored one.
The empty-query `total` is the exception, since it is maintained on every write
and so stays exact.

`column_counts`, `numstats` and `null_counts` are the other exception, and a
caveat worth knowing.  The line is what a cache miss costs: the counts above
fall back to a single statement about the rows in question, while these fall
back to rebuilding a whole statistics family, or to one full count per search
column.  Making them miss while the table is invalid would rebuild on every
call and never converge, since only `refresh_stats()` restores the flag, so
they read what is recorded.  A value recorded before an unrefreshed write is
therefore still reported by them; run `refresh_stats()` after a write you did
not `restat`.  The flag goes back to true only in `refresh_stats()`, inside
the transaction that rebuilt the caches, so a refresh that fails part-way leaves
the table marked invalid rather than claiming a cache it does not have.
Refreshing a `_tmp` copy does not validate the live table.

Bulk paths also run PostgreSQL's own `ANALYZE`, which is a different thing from
psycodict's statistics: a freshly loaded relation has no planner statistics
until autovacuum reaches it.  Replacement tables are analyzed while still named
`_tmp`, before the swap, since the catalog entry follows the relation through
the rename; `copy_from` analyzes the live table it loaded into.  Inserting rows (and updating a sort-key column) also calls `_break_order`, setting `out_of_order = true` to record that the `id` order no longer matches `sort`; `delete` leaves the order flag alone.  `resort()` (or a rebuild through `reload`/`rewrite`) sets it back to `false`.

### Resorting

`table.resort()` rebuilds the table so that ascending `id` matches the
configured sort, laying the rows out on disk in that order.  It is a full-table
operation: the data is dumped, loaded into a fresh table whose ids are assigned
`1..N` in sort order, and swapped into place through the same machinery `reload`
uses, so the primary key, indexes, constraints, grants and counts/stats
companions are rebuilt, `ANALYZE` is run, and the previous table is kept as a
`name_old<n>` backup.  **The ids change** — anything outside psycodict that
stored them as durable identifiers must be updated — and the operation needs
temporary disk space for the replacement, its indexes, the dump and the backup.
By default a table already marked ordered reports that there is nothing to do;
pass `force=True` to rebuild anyway.

The earlier in-place implementation renumbered every id with an `UPDATE`, which
stalled replication and left the rows in their old physical order (defeating the
point of id-ordering); it was disabled, and is now replaced by this rebuild.

Because a resort is expensive, it is no longer a side effect of a small write.
`resort=True` on `insert_many`, `update`, `copy_from` or an in-place
`update_from_file` **raises**, pointing at `resort()`; `resort=False` (the
default) is unaffected.  The replacement operations that rebuild the whole table
anyway — `reload`, `rewrite`, and a non-inplace `update_from_file` — still
establish id order as part of building the new table.  The workflow for a
maintained table is therefore:

```python
table.update(...)          # marks out_of_order
# later, in a maintenance window:
table.resort()             # rebuild in sort order
```

`scripts/audit_id_order.py` checks the flag against reality: it streams each
`id_ordered` table in sort order and reports `OK`, `MISMATCH` or an error
without modifying anything.  Use it before trusting `out_of_order = false` on a
table whose history includes non-inplace updates, which could have left the flag
stale.

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
