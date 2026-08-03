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

## 0.1.x and earlier

Pre-1.0 releases did not keep a changelog.
