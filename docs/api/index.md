# API reference

Generated from the docstrings.  The map of the library:

- {mod}`psycodict.database` — {class}`~psycodict.database.PostgresDatabase`,
  the connection object; each table in the database is an attribute of it.
- {mod}`psycodict.searchtable` —
  {class}`~psycodict.searchtable.PostgresSearchTable`, the read API
  (`search`, `lucky`, `lookup`, `count`, `random`, …) driven by the query
  dictionaries of [the query language](../QueryLanguage.md).
- {mod}`psycodict.table` — {class}`~psycodict.table.PostgresTable`, the
  write and schema half (row writes, bulk file import/export, reloads,
  indexes, columns); base class of the search table.
- {mod}`psycodict.statstable` —
  {class}`~psycodict.statstable.PostgresStatsTable`, precomputed counts and
  statistics, available as `table.stats`.
- {mod}`psycodict.encoding` — value conversion between Python and
  PostgreSQL, including the file formats used by the bulk operations.
- {mod}`psycodict.config` — configuration discovery and parsing
  (`config.ini`, command-line arguments, `$PSYCODICT_CONFIG`).
- {mod}`psycodict.grants` — {class}`~psycodict.grants.GrantPolicy`, the
  privileges the relations psycodict creates come into existence with.
- {mod}`psycodict.utils` — {class}`~psycodict.utils.DelayCommit` and other
  helpers shared across the library.
- {mod}`psycodict.notifications` — LISTEN/NOTIFY support: schema-change
  announcements and a small publish/subscribe primitive.
- {mod}`psycodict.dbdiff` — comparing the contents of two databases.
- {mod}`psycodict.slowlog` — parsing and reporting on the slow-query log.
- {mod}`psycodict.validation` — what psycodict checks before putting it into a
  statement: the column types it will create, the identifiers it will name a
  relation with, and the index and constraint definitions it will build DDL
  from.
- {mod}`psycodict.base` — {class}`~psycodict.base.PostgresBase`, the shared
  execution and logging plumbing underneath everything else.

```{toctree}
:maxdepth: 1

database
searchtable
table
statstable
encoding
config
grants
utils
notifications
dbdiff
slowlog
validation
base
```
