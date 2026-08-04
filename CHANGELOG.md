# Changelog

All notable changes to psycodict are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 1.0.0

The first release with a stable, documented API. It consolidates a large body
of standalone bug fixes, a driver port, a rebuilt query and data-management
layer, and full test coverage. Because it tightens several long-standing
defaults, it contains breaking changes; each is listed below with a one-line
migration note.

### Breaking changes

- **The search/extras table split is gone.** Tables are now a single table
  instead of a `search` table paired with an `extras` table. *Migration:* rerun
  `create_table` without the extras argument; columns that used to live in the
  extras table are ordinary columns now. (#59)
- **Ported from psycopg2 to psycopg 3.** psycodict no longer depends on
  psycopg2, and it re-exports `SQL`, `Identifier`, `Placeholder`, `Literal`,
  `Composable` and `Composed` from `psycodict` so callers need not import a
  driver directly. *Migration:* install with the `pgbinary` or `pgsource` extra
  (`pip install "psycodict[pgbinary]"`; psycopg 3.2.4 or newer -- the
  notification listener relies on psycopg 3.2 APIs and on 3.2.4's
  notification-delivery fix), and import the SQL composition classes
  from `psycodict` rather than `psycopg2.sql`. (#88)
- **The old `join_search` method is removed.** *Migration:* pass `join=` to
  `search` (or `count` / `lucky`) instead; see the joins section of
  QueryLanguage.md. (#89)
- **Non-public methods are underscore-prefixed.** The following are now private:
  `cursor` → `_cursor`, `log_db_change` → `_log_db_change`,
  `is_alive` → `_is_alive`, `is_read_only` → `_is_read_only`,
  `can_read_write_knowls` → `_can_read_write_knowls`,
  `can_read_write_userdb` → `_can_read_write_userdb`,
  `register_object` → `_register_object`, `logger` → `_logger`,
  `has_id` → `_has_id`. *Migration:* add the leading underscore at call sites,
  or stop calling them — none were part of the intended public API. (#92)
- **`count` and `count_distinct` no longer cache by default** (`record=False`).
  Search-page counts are still recorded. *Migration:* pass `record=True` to
  cache a count you will ask for repeatedly. (#93)
- **Numeric precision is derived from significant digits**, and an all-zero
  decimal round-trips to an exact integer zero rather than a slightly different
  float. *Migration:* none needed unless you relied on the previous, less
  precise rounding. (#94)
- **`reindex=False` is now an error in `rewrite` and `update_from_file`**
  instead of being silently ignored; `rewrite` no longer accepts the parameter
  at all, and its trailing parameters are keyword-only. *Migration:* drop
  `reindex=False`, or reindex explicitly after the operation. (#96)
- **`include_nones` defaults to `True`**, and the choice is stored per table.
  *Migration:* pass `include_nones=False` at `create_table` to keep the old
  behavior of omitting `None`-valued keys from result dictionaries. (#103)
- **Metadata format 1.** The layout of the `meta_*` tables is now versioned
  (format 0 is the unstamped 0.x baseline; format 1, aligned with this major
  release, adds `meta_indexes.whereclause`), stamped in the new `meta_format`
  table and checked on connect.  A format-0 database keeps working — every
  connection warns and operates at the old format, with the format-1 features
  unavailable — and read-only replicas need no action.  See
  MetadataFormats.md for the compatibility policy.  *Migration:* connect once
  with `PostgresDatabase(upgrade=True)` (or run `db.upgrade_metadata()`) to
  migrate the `meta_*` tables in place and silence the warning.  **Caveat:**
  do not run a pre-1.0 psycodict against a database a 1.0+ psycodict has
  created or migrated — it predates `meta_format`, so it cannot be told to
  refuse, and its index/reload paths would silently drop partial-index
  predicates.  This only matters during a mixed-version rollout. (#90, #110)
- **`Configuration` behaves like a library.** It no longer parses the host
  program's command line by default (`readargs` was auto-enabled in scripts
  and an unrecognized option raised `SystemExit`); with no explicit location
  the configuration file is discovered — `$PSYCODICT_CONFIG`, then
  `./config.ini` if it already exists, then `~/.psycodict/config.ini` — and a
  missing file is created under `~/.psycodict`, never in the working
  directory; the default `slow_queries.log` lands in a `logs` directory next
  to the configuration file (falling back to `~/.psycodict/logs` when that
  location is not writable); and the default secrets file sits next to the
  configuration file.  Existing `./config.ini` setups keep working unchanged.
  *Migration:* pass `readargs=True` in a script whose command line psycodict
  should parse; set `PSYCODICT_CONFIG` (or pass `config_file`) to pin a
  location.  Subclasses supplying their own parser (LMFDB, seminars) are
  unaffected. (#119)
- **`sum` and `random` honor the `saving` flag.** With the default
  `saving = False` they no longer write to the counts/stats cache tables (they
  were the only two paths that did). *Migration:* enable `saving` on the stats
  table — as data-management deployments already do — to persist computed
  statistics. (#118)
- **Python 3.9 or newer is required** (3.8 is past end of life). *Migration:*
  upgrade the interpreter; no code changes. (#121)

### Added

- **Joined queries.** `join=` on `search`, `count` and `lucky` supports inner,
  left, right and full joins, with `"table.column"` qualification in queries,
  projections, sorts, `$col` and `$raw`, and dotted paths in joined
  projections. (#89)
- **New query operators.** `$col` compares two columns, `$raw` documents the
  raw-SQL escape hatch, and `$size` constrains array/jsonb cardinality; path
  specifiers now work in plain projections and sorts, not just queries.
  (#89, #107, #108)
- **Partial indexes**, declared through the meta-index machinery (part of
  metadata format 1). (#110)
- **`staged()` transactional uploads** — a context manager that groups writes
  with write exclusion and drift detection. (#100)
- **`db.refresh_tables()`** re-reads the schema so structural changes are picked
  up without restarting the process. (#99)
- **Metadata format protocol.** The `meta_format` stamp — `(version,
  min_compat)` — written at creation and checked on connect; older-but-
  compatible databases warn and degrade gracefully instead of being refused,
  newer databases are accepted when their stamped `min_compat` admits this
  psycodict, migrations run stepwise via `db.upgrade_metadata()` /
  `PostgresDatabase(upgrade=True)`, and `db.meta_format` exposes the format a
  connection operates at.  MetadataFormats.md documents the policy and the
  checklist for future format changes. (#90, #110)
- **Query and lock introspection.** `show_queries` and `show_blocked` report
  running and blocked queries via `pg_blocking_pids`, and reloads now warn about
  outstanding locks. (#91)
- **Slow-query log analysis** in the new `psycodict.slowlog` module. (#102)
- **`db.compare` / `db.show_differences`** detect drift between two databases.
  (#101)
- **Schema-change notifications** and a small publish/subscribe layer built on
  PostgreSQL LISTEN/NOTIFY. (#111)
- **Documentation.** `DataManagement.md` (write side) and `Searching.md` (read
  API), plus docstring housekeeping across the package (#104, #105, #106);
  `Versioning.md` states what the version number promises (#123), and
  `CONTRIBUTING.md` and `SECURITY.md` set out the contribution workflow and
  the security policy.
- **A documentation site.** Sphinx/MyST build of the guides plus an autodoc
  API reference, built warning-free in CI and publishable on Read the Docs;
  the canonical copies of the guides stay at the repository root. (#122)
- **`psycodict.__version__`** — the package version as an attribute, and the
  single source of truth for packaging. (#121)

### Fixed

Roughly two dozen fixes landed while splitting the search/extras tables and
hardening standalone use; the highlights:

- Counts-table totals are now maintained correctly on writes rather than drifting
  out of sync. (#61–#87)
- `text[]` values no longer corrupt on `COPY` because of quoting. (#61–#87)
- A Python `None` in a jsonb value maps to SQL `NULL` with consistent semantics,
  including nested jsonb decoding. (#61–#87)
- `$maxgte` and related operators work without requiring custom SQL functions.
  (#61–#87)
- Statistics and counts no longer accumulate duplicate rows, and `refresh_stats`
  converges. (#97)
- Sage-free statistics, fresh-database bootstrap (`create=True`), and jsonb
  `$in` / `$nin` on composite values all work in standalone (non-LMFDB) use.
  (#56, #57)
- A leftover `_tmp` index or constraint is caught by a preflight check before a
  reload or rewrite. (#95)
- `create_table_like` preserves per-column `STORAGE` / `COMPRESSION` settings and
  analyzes the copy. (#98)
- `reload` with a metafile and resorting no longer fails with a `TypeError`.
  (#114)
- Non-inplace `update_from_file(restat=True)` publishes the refreshed
  statistics it computes (they used to be left orphaned in `_tmp` tables) and
  rebuilds the counts indexes on the table it swaps in. (#115)
- `copy_dumps` escapes the COPY delimiter inside text, json and array values,
  so its output round-trips through `COPY FROM` and matches `COPY TO` byte for
  byte. (#116)
- `postgresql_dbname` was the one option whose default ignored the `defaults`
  dictionary passed to `Configuration`. (#119)

### Security

- **Column types are validated against the whole string, not a prefix.** The
  type of a column has to be interpolated into `CREATE TABLE` and `ALTER TABLE`
  as SQL text, since PostgreSQL has no placeholder for a type. The check that
  guarded that interpolation matched a prefix, so a type beginning with a valid
  type — `text; ...`, `varchar(16); ...` — was accepted with the rest attached
  and executed. `create_table`, `create_table_like`, `add_column` and the
  column types read from the header of a reloaded data file
  (`reload(adjust_schema=True)`, `reload_all(adjust_schema=True)`) were all
  affected. Types are now validated by
  `psycodict.validation.validate_column_type`,
  which matches the complete string and returns the spelling that callers must
  emit; the character-type grammar it replaces was also malformed, so
  `varchar(N)`, `char(N)` and `character varying(N)` are now described
  deliberately rather than accepted as prefixes of `varchar`. A declaration is
  read in the order PostgreSQL writes it — a scalar type, then any array
  brackets, then a collation — so arrays of parameterized types and
  multidimensional arrays (`varchar(16)[]`, `numeric(10, 2)[][]`,
  `varchar(16)[] COLLATE "C"`) compose without a pattern for every combination,
  and a `COLLATE` clause is accepted only on a collatable type. An interval
  precision is a fractional-seconds precision, so it is now accepted only
  without a field restriction or with one that reaches `SECOND`
  (`interval day to second(6)`, not `interval day(3)`). An invalid type raises
  `InvalidColumnTypeError`, a subclass of both `ValueError` and the
  `RuntimeError` raised before. What psycodict checks before putting it into a
  statement now lives in its own module, `psycodict.validation`; the names are
  re-exported from `psycodict.base`, where they used to be.

- **Index and constraint definitions are validated when imported and again
  when used.** `meta_indexes` and `meta_constraints` hold what an index or
  constraint is rebuilt from, sometimes years and several psycodict versions
  after it was created, and in between the rows can be edited with plain SQL,
  exported to a file, carried to another database and imported, or restored
  from history. `create_index` and `create_constraint` checked their arguments,
  but nothing rechecked a row on the way back out, and the access method,
  column modifiers, storage-parameter names, check function and partial-index
  predicate were formatted into the statement as text. A poisoned
  `meta_indexes` row could therefore run its own statements the next time the
  index was restored. Definitions are now checked by
  `validate_index_definition` / `validate_constraint_definition` at import
  (`reload_indexes`, `reload_constraints`, the `revert_*` methods) and again
  immediately before the DDL is built, and everything they are built from is
  either a quoted identifier or fixed SQL selected by a validated key. An
  invalid definition raises `InvalidDefinitionError` (a `ValueError`). Names
  are held to what they must be rather than to a convention: every identifier
  is quoted, so a name may contain punctuation, spaces or non-ASCII characters,
  and what is refused is an empty name, a control character, or a name over
  PostgreSQL's 63-*byte* limit that the server would truncate into something
  the metadata does not name — refused wherever it arrives, including from a
  reloaded file, a reverted history version or a row edited in place, and
  refused rather than shortened, since a stored name is what the relation is
  called. Names psycodict *derives* — `_tmp`, `_oldN`, `_depN`, `_pkey` — go
  through one helper that keeps the suffix and shortens the base on a UTF-8
  character boundary, so no path relies on server-side truncation and no two
  paths can disagree about a derived name. A search table's own relations
  (`_counts`, `_stats` and their `_tmp`/`_oldN` copies) are concatenated rather
  than shortened, because PostgreSQL truncates consistently and an existing
  database already has them under the truncated spelling; creating one under an
  over-long name is refused instead. A
  `CHECK` constraint's function may be schema-qualified and is emitted as
  `"schema"."function"` rather than as one identifier. Boolean index storage
  parameters accept PostgreSQL's spellings (`on`, `yes`, `t`, `1`) and are
  stored normalized, floats are refused, and `gin_pending_list_limit` no longer
  carries a client-side maximum, since its ceiling depends on the server's
  architecture. Because
  the import runs inside the reload transaction, the metadata that was there
  before survives intact. A partial-index predicate remains raw administrative
  SQL, but may no longer contain a semicolon, a comment, a dollar-quoted string
  or control characters. The unused, unvalidated `_copy_from_meta` helper is
  gone. These rules live in `psycodict.validation`, next to the column-type
  grammar; `psycodict.base` no longer holds any of them.

- **A reconnect reproduces the connection it replaces.** `reset_connection`
  rebuilt the connection from the configuration alone, dropping the overrides
  passed to `PostgresDatabase(...)` — TLS mode and certificates, an alternate
  host, port, database or user, libpq `options`, connect and keepalive
  timeouts. A connection that dropped could come back weaker than the one it
  replaced, or somewhere else entirely. Options now come from one place
  (`_connection_options`), used for the first connection and every replacement;
  session setup moved into `_configure_session`, so the statement timeout and
  friends are reapplied rather than lost with the session they were set on; and
  a replacement is checked to have reached the same database as the same role,
  with the read-only, superuser, knowls and userdb capability flags recomputed
  from it instead of carried over. Retry behavior is unchanged: a standalone
  statement is retried once, and a statement inside `DelayCommit` still raises
  rather than replaying a transaction whose earlier statements are gone.
- **`PostgresDatabase(session_settings=...)`** applies `statement_timeout`,
  `lock_timeout`, `idle_in_transaction_session_timeout` or `application_name`
  to the main connection and to every replacement main connection. The 25
  second statement timeout for the `webserver` role is now the default value of
  this setting rather than a one-off `SET` in the constructor. A notification
  listener's connection deliberately does not inherit them — it exists to
  wait — though it does inherit the connection overrides that decide which
  server and database it reaches.

- **Search table names have one grammar, and table objects have their own
  registry.** A table name is used as a PostgreSQL identifier, as the key the
  table object is reached by, and as the stem of the files `copy_to` writes,
  but nothing checked it against any of those uses. A name with a path
  separator or a `..` component sent an export out of the folder it was asked
  for, and because table objects were stored in the database object's instance
  dictionary, a table named `conn`, `config` or `tablenames` — from
  `create_table` or from a `meta_tables` row edited with plain SQL — replaced
  that attribute. Names are now checked by `validate_search_table_name`
  (lowercase letters, digits and underscores, starting with a letter, at most
  63 bytes, not ending in a suffix psycodict appends to a table's own name, not
  the name of something else on the database object) in `create_table`,
  `create_table_like` and `rename_table`, where the limit is 49 bytes rather
  than PostgreSQL's 63 so that the relations psycodict names after the table
  (`<name>_counts_old<N>` is the longest) cannot be truncated into each other;
  names already in `meta_tables` may keep a legacy spelling but are checked as
  a set, before anything is registered, against the relations the database
  actually has — a row is refused if it names one of psycodict's own relations,
  ends in scratch space (`_tmp`, `_oldN`, `_depN`, `_pkey`), exceeds 63 bytes,
  claims a relation another row already claims, or lacks its counts or stats
  table, where "has" is decided against the spelling the catalog holds, since a
  legacy name long enough that `<name>_counts` does not fit in 63 bytes has that
  relation under the truncated one (a name with 62 or 63 bytes, whose counts and
  stats tables PostgreSQL would store as a single relation, is refused and
  said so); renaming a table plans the new names of its indexes and constraints
  before renaming anything, so the metadata and the catalog cannot end up with
  different spellings when substituting a longer table name pushes one past the
  limit; the transient index a staged write builds is named by one helper
  rather than by three concatenations; table objects live in `db.tables`; and
  the files an export generates go through
  `safe_child_path`, which resolves symlinks and refuses anything that is not
  under the requested folder. *Migration:* `db.tablenames` is now a sorted
  property derived from `db.tables` rather than a list to mutate, and
  attribute access (`db.mytable`) is unchanged. `reload_all` and `rename_table`
  raise `ValueError` where they used to `assert`.

- **Table creation no longer grants privileges to hard-coded roles.** Creating
  a search table granted `SELECT` on it to `lmfdb` and `webserver`, and
  `SELECT`/`INSERT` on its counts and stats tables, wherever those roles
  happened to exist; the metadata tables did the same, and a reload reapplied
  it. That is the LMFDB's deployment rather than a fact about a PostgreSQL
  database, so a new table holding anything at all was readable by two roles
  the administrator may never have heard of. Privileges are now stated by a
  `psycodict.grants.GrantPolicy`, passed as
  `PostgresDatabase(grant_policy=...)` and applied through one helper wherever
  psycodict creates or swaps a relation. *Migration:* the default policy grants
  nothing; LMFDB and any other deployment that wants the old behavior passes
  `LMFDBGrantPolicy()`. A policy that names a role the cluster does not have
  raises by default (`LMFDBGrantPolicy` warns and skips, as before), and
  `grant_select`, `grant_insert`, `grant_update` and `grant_delete` now require
  their `users` argument instead of defaulting to those roles.
- **A reload warns when it takes access away.** A reload swaps in a clone, and
  PostgreSQL gives a clone no privileges of its own, so under the default
  policy the replacement is reachable only by its owner even when the table it
  replaced was readable by an application role. That is what the policy asks
  for, but it should not be discovered by a website going dark, so psycodict
  logs a warning naming the roles that lost access.
- **A reload's backup table is no longer left as readable as the table it
  replaced.** PostgreSQL carries privileges along with a rename, so the `_oldN`
  table — a copy of the live data — inherited the live table's grants. The
  policy is now applied to it as its own relation kind, which grants nothing
  under `LMFDBGrantPolicy`. `reload_revert` applies the same rule, so reverting
  a reload no longer hands the restored table its backup's privileges and the
  backup the live table's. The legacy `_oldstats` relation has its own
  `oldstats` kind and keeps the read-only access it always had, rather than
  being treated as an ordinary stats table (which under `LMFDBGrantPolicy`
  would have given the website `INSERT` on it). Note that the *default* policy
  names no roles, so it revokes nothing: a freshly created relation has no
  non-owner grants to begin with, but a renamed one keeps the ACL it had.

### Infrastructure

- A test suite of 500+ tests and a continuous-integration workflow covering the
  supported Python and PostgreSQL versions, plus downstream regression jobs that
  run LMFDB and seminars against the new code. (#58)
- `config.ini` is no longer tracked in the repository; copy
  `config.ini.example` (or rely on the configuration discovery above). (#120)
- A trusted-publishing release workflow: publishing a GitHub release builds
  the distributions, checks their metadata and uploads to PyPI via OpenID
  Connect — no long-lived token is stored anywhere. (#113)
- `CITATION.cff`, so GitHub renders a citation for the package. (#124)

### Fixed after the first release candidates

- **`max_id` and `min_id` compose the table name they are given.** Both took a
  `table=` argument and formatted it into the statement as text, so a name
  needing quotes was a syntax error and a name carrying its own statement ran
  it. Both now use `Identifier`. `max_id` returns -1 for an empty table, which
  is the only empty sentinel: 0 is a real id, and `random()` treated a table
  whose single row had id 0 as empty.
- **Approximate statistics are scaled by the table they describe.**
  `_approx_most_common` took `reltuples` from a hard-coded `public.nf_fields`
  while taking frequencies from the real table, so on every other table the
  estimate was that table's frequencies multiplied by an unrelated row count.
  The column type it interpolates now goes through the validated
  `column_type_sql`.
- **`update_from_file` no longer shares log state between calls.** Its
  `logging` default was a dictionary literal that the method wrote `logid` and
  `aborted` into, so consecutive default calls saw each other's values and a
  caller-supplied dictionary came back modified. The default is now `None` and
  the mapping is copied per call.
- **Random selection edge cases.** `random(query, pick_first=...)` returned
  `None` rather than raising `IndexError` when nothing satisfies the query; a
  projected value of `0`, `False`, `""` or `[]` counts as a result instead of
  being skipped until `maxtries` ran out; `random_sample` raises `ValueError`
  naming the accepted modes instead of silently returning `None` for an
  unknown one; and a repeatable `choice` sample uses a local
  `random.Random(seed)` rather than reseeding the process-wide generator.

- **`stats_valid` is enforced, not just recorded.** Write paths cleared the
  flag but read paths ignored it, so a count cached before a `restat=False`
  write kept being served afterwards -- verified: a query counted at 67, then
  every matching row changed, still answered 67. Every lookup that would serve
  a cached answer now goes through one predicate and reports a miss while the
  flag is false: `quick_count`, `quick_count_distinct` and `_quick_statistic`,
  which is what makes `count`, `max`, `min` and `sum` compute the answer
  instead of returning a recorded one. The line is whether a miss costs one
  bounded query or a rebuild, so these are deliberately not gated: the
  empty-query `total`, maintained on every write and so exact; the `_status` /
  `status` / `extra_counts` inventory, which is how `refresh_stats` discovers
  what to recompute; `_has_stats` / `_has_numstats`, which decide whether a
  whole statistics family needs computing; and `null_counts`, whose fallback
  is one full count *per search column*. Gating that last group made
  `column_counts`, `numstats` and `null_counts` rebuild on every call with
  nothing to converge on, since only `refresh_stats` restores the flag --
  measured on the LMFDB, four minutes of downstream suite became over
  forty-five. **The gap that leaves:** `column_counts`, `numstats` and
  `null_counts` can still report a value recorded before an unrefreshed
  write.
  Closing it needs freshness per statistic rather than one flag per table,
  which is a metadata format change; `refresh_stats()` is the remedy
  meanwhile. A suffixed (`_tmp`, `_oldN`) table is not gated by the live
  table's flag, since it carries its own caches. The flag is restored only by
  `refresh_stats()`, inside the transaction that rebuilt the caches, so a
  failed refresh leaves the table invalid; refreshing a `_tmp` copy does not
  validate the live table.
- **Bulk paths run `ANALYZE`.** A relation that has just been bulk loaded has
  no planner statistics until autovacuum reaches it, so queries against it are
  costed as though it were tiny. Replacement tables are analyzed while still
  named `_tmp` -- before the swap, and outside its transaction, since the
  catalog entry follows the relation through the rename -- which covers
  `reload`, `rewrite`, non-inplace `update_from_file` and staged commits
  through the one helper they share; `copy_from` analyzes the live table it
  loaded into.

### Release candidates

1.0.0 is published as a sequence of release candidates first. `pip` ignores
them unless asked (`pip install --pre psycodict`), so a deployment can run
against the real artifact before the version everybody gets by default exists.
Each candidate is the whole of 1.0.0 as it stood when it was built; this list
records only what changed between them.

- **1.0.0rc2** — adds everything under **Security** above: the five findings of
  the August 2026 audit, two of which were confirmed exploitable end to end
  (a column type carrying its own statements into DDL, and a poisoned
  `meta_indexes` row executing on `restore_index`). Also `psycodict.validation`,
  the module those checks now live in. **rc1 has none of this; upgrade rather
  than pin to it.** One of the five changes a default a deployment may be
  relying on: the default grant policy grants nothing, so a deployment that
  wants the privileges psycodict used to hand out has to say so with
  `grant_policy=`. Read the *Migration* note on that entry before upgrading
  anything that creates or reloads tables. (#133–#137)
- **1.0.0rc1** — the first candidate, and the rehearsal of the trusted-publishing
  path: it created the project on PyPI without touching the version `pip`
  installs by default.

## 0.1.x and earlier

Pre-1.0 releases did not keep a changelog.
