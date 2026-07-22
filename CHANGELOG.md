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
  (`pip install "psycodict[pgbinary]"`), and import the SQL composition classes
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
