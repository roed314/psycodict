# Metadata formats

psycodict keeps its bookkeeping in a handful of *metadata tables* —
`meta_tables`, `meta_indexes`, `meta_constraints`, and their `_hist`
counterparts.  The layout of those tables is versioned by a single integer,
the **metadata format**.  This document specifies how the format is recorded,
what happens when a psycodict meets a database using a different format, and
the checklist to follow when changing the format.

The goal of the design is that a format change never strands anyone: a newer
psycodict keeps working against a not-yet-migrated database (with the new
features unavailable), an older psycodict keeps working against a migrated
database, read-only mirrors need no action at all, and migration is a single
deliberate step rather than a side effect of connecting.

## Format history

| format | psycodict | change |
|--------|-----------|--------|
| 0 | 0.x | Baseline: `meta_tables`, `meta_indexes`, `meta_constraints` and their `_hist` counterparts, with no format stamp. |
| 1 | 1.0 | `meta_indexes` / `meta_indexes_hist` gain a nullable `whereclause` column, holding the predicate of a partial index (NULL for an ordinary index).  Compatible with format 0: everything keeps working against a format-0 database except creating partial indexes. |

The format number is aligned with psycodict's major version: format *N* is
introduced by psycodict *N*.0, and plans to change the format should be
scheduled for a major release.

## The stamp

A database records its format in the single-row `meta_format` table:

| column | meaning |
|--------|---------|
| `version` | the metadata format of this database's layout |
| `min_compat` | the oldest `META_FORMAT` a psycodict may implement and still safely use this database |

A database that has meta tables but no `meta_format` table is a format-0
database (psycodict 0.x never stamped).  A `meta_format` table that exists
but is empty is an error: the format is unknowable, and psycodict refuses to
guess.  The stamp is written when a fresh database is bootstrapped
(`PostgresDatabase(create=True)`) and updated by each migration step.

`min_compat` is what lets an *older* psycodict decide whether it can use a
*newer* database, a situation the older code cannot reason about on its own.
Additive changes (new columns) keep `min_compat` low; a breaking change
(renaming, retyping or repurposing something that older code reads or
writes) must raise it.

## Connecting across formats

Every connection compares the database's stamp with the format it implements
(`META_FORMAT` in `psycodict/base.py`):

- **Same format** — connects silently.
- **Database older** — if every intervening migration is marked *compatible*
  (see the registry below), psycodict connects with a warning and operates at
  the database's format: everything present in that format works, and
  features introduced by newer formats raise an informative error (for
  example, `create_index(where=...)` against a format-0 database).  If some
  intervening migration is not compatible, the connection is refused with
  instructions to migrate or to use a matching psycodict release.
- **Database newer** — if the stamped `min_compat` admits this psycodict, it
  connects with a warning and simply does not touch the columns it does not
  know about (this is why new columns must be appended; see the checklist).
  Otherwise the connection is refused: upgrade psycodict.

The format a connection actually operates at is exposed as `db.meta_format`,
which downstream code can use to gate features.

Read-only databases (replicas such as the LMFDB devmirror, or users without
write grants) can never migrate themselves, which is precisely why an older
compatible format *warns* instead of refusing.  A replica inherits the
migration when the primary is migrated.

### Pre-1.0 psycodict cannot be repelled

This negotiation only binds clients that implement the `meta_format`
protocol — that is, psycodict 1.0 and later.  A **pre-1.0** psycodict knows
nothing of `meta_format`; it will happily connect to a format-1 database and,
because its `restore_index` and metadata reload/revert paths predate the
`whereclause` column, it can silently rebuild a partial index as a full one
and drop the predicate.  `meta_format` and `min_compat` cannot stop it, since
it never reads them.

> **Do not run a pre-1.0 psycodict against a database that a 1.0+ psycodict
> has created or migrated (format ≥ 1).**  If it has already happened, what is
> recoverable depends on which paths the pre-1.0 client ran.  Its
> `restore_index` only rebuilds the physical index, leaving the predicate
> recorded in `meta_indexes`: drop the wrongly-full index, then run
> `restore_index` from a 1.0 client to rebuild it correctly.  (Restoring
> without dropping first keeps the bad index behind under a `_depN` name --
> and a unique index kept that way still over-constrains writes.)  The
> pre-1.0 metadata *reload and revert* paths, however, rewrite the
> `meta_indexes` rows with the six pre-1.0 columns, destroying the recorded
> predicate -- after that the partial index cannot be reconstructed from the
> database itself, only from a metadata backup (see *Rolling back* below).  This matters only during a
> mixed-version rollout; once every process has been upgraded to 1.0+ there is
> nothing to watch for.  (The alternative — leaving a tombstone in the old
> `meta_version` table so pre-1.0 clients refuse — was declined in favor of the
> single-stamp design; the numbering aligns with the major version instead.)

## Migrating

Migration is deliberate, never automatic: connecting does not alter a
database.  To migrate, an administrator runs

```python
db.upgrade_metadata()          # on a live connection
# or, in one step at connection time:
db = PostgresDatabase(config=config, upgrade=True)
```

which applies each registered step in order and re-stamps the format as it
goes (each step's DDL and stamp commit together).  Steps are idempotent, so
re-running a migration — or running it on a database that grew a column out
of band — is harmless.  Downgrades are not supported.

### Rolling back

Because downgrades are not supported, take a backup before migrating, in the
custom format that `pg_restore` consumes (the default plain-SQL format cannot
be given to `pg_restore`):

```sh
pg_dump -F c -f pre_migration.dump $DBNAME
```

To roll back, recreate the database from the dump — restoring over the
existing database would stop on existing-object errors and leave the new
`meta_format` stamp in place:

```sh
pg_restore -d postgres --clean --create --exit-on-error pre_migration.dump
# equivalently: dropdb $DBNAME && createdb $DBNAME
#               && pg_restore -d $DBNAME pre_migration.dump
```

run as a role allowed to drop and recreate the database.  **This restores the
whole database to the moment of the dump — application data included** — so
quiesce writes for the migration window and roll back immediately or not at
all.

If writes cannot be stopped or lost, the surgical variant is to back up only
the six meta tables:

```sh
pg_dump -F c -f pre_migration_meta.dump \
    -t meta_tables -t meta_tables_hist -t meta_indexes \
    -t meta_indexes_hist -t meta_constraints -t meta_constraints_hist \
    $DBNAME
```

and to roll back by replacing them, leaving the application tables (and any
writes since the dump) untouched:

```sh
psql -c 'DROP TABLE meta_tables, meta_tables_hist, meta_indexes,
                    meta_indexes_hist, meta_constraints, meta_constraints_hist;
         DROP TABLE IF EXISTS meta_format' $DBNAME
pg_restore -d $DBNAME --exit-on-error pre_migration_meta.dump
```

The *metadata* still rewinds to the dump: an index or constraint created
after it becomes an unlisted orphan (drop it by hand), a column added after
it disappears from psycodict's view, and `count()` on the empty query answers
from a cached total in `meta_tables` that will be stale if rows were written
in between.  So the surgical variant, too, is best used immediately after
migrating, before other schema activity.

Both procedures have been rehearsed against a live format-0 database built
with an actual pre-1.0 psycodict: after either restore, the pre-1.0 client
operates exactly as before the migration, and a 1.0 client connects with the
usual format-0 warning and degrades, as if the migration had never run.

## Exported metadata files

`copy_to_indexes` and friends write the columns the database actually has,
and the files carry no header, so the format of a file is recovered from its
width when it is reloaded.  Because format bumps only append columns, a file
exported at an older format keeps loading after the database migrates (the
missing trailing columns load as NULL), and a file exported at a newer format
is rejected with instructions rather than half-loaded.

## Checklist: changing the metadata format

Suppose the layout of the meta tables must change.  Then:

1. **Append, never mutate.**  New columns go at the *end* of the relevant
   `_meta_*_cols` tuple in `psycodict/base.py`, and are recorded in
   `_meta_col_formats` with the format that introduced them.  The
   cross-format guarantees (older clients, older files) rest on the columns
   of an older format being a prefix of the newer ones.  Renaming, retyping,
   reordering or repurposing an existing column is a *breaking* change; avoid
   it if at all possible, and see step 4 if not.
2. **Bump `META_FORMAT`** in `psycodict/base.py`, add a line to its History
   comment and to the table at the top of this file.  Schedule the bump for
   the next major release, so the format number and the major version stay
   aligned.
3. **Register the migration** in `META_MIGRATIONS` in
   `psycodict/database.py`: a one-line description, the `compatible` flag,
   the `min_compat` to stamp, and the name of a `PostgresDatabase` method
   performing the DDL.  The method does only the DDL, idempotently
   (`IF NOT EXISTS` / `IF EXISTS`); stamping is handled by
   `upgrade_metadata`.
4. **Choose the compatibility flags honestly.**
   - A purely additive change: `compatible=True`, and `min_compat` stays
     what it was (0 today).
   - A breaking change: `compatible=False` (older-format databases are
     refused rather than warned) and `min_compat` raised to the new format
     (older psycodicts are refused by newer databases).  Say so loudly in
     the CHANGELOG.
5. **Gate the feature.**  Code that reads or writes the new columns must
   consult `self._db._meta_format` and either degrade (reads: the column is
   absent, so treat it as its default) or raise with instructions (writes
   that would lose information).  Follow the `whereclause` sites in
   `psycodict/table.py` — `list_indexes`, `restore_index`, `create_index` —
   as the model.  Generic metadata I/O (`_copy_to_meta`, `_reload_meta`,
   `_revert_meta`, `_meta_creator`) is already format-aware through
   `_meta_cols_types_jsonb_idx(meta_name, fmt)` and needs no per-change
   work.
6. **Test the seam.**  Extend the format tests (`tests/test_meta_formats.py`)
   with: connecting to the previous format warns and operates degraded; the
   new feature raises its informative error there; `upgrade_metadata`
   migrates, stamps and is idempotent; files exported at the previous format
   still reload; and the new format's stamp is what you registered.
7. **Document it**: the CHANGELOG, and (for a new feature) the docstrings of
   the methods involved.
