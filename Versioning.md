# Versioning and API stability

Starting with 1.0.0, psycodict follows [semantic versioning](https://semver.org):
breaking changes to the public API happen only at major releases, new
functionality arrives in minor releases, and patch releases contain only fixes.
This document says what "public API" means for a package whose surface includes
not just Python names but also a query language, an on-disk export format, and
metadata tables living inside your database.

## What is public

 * **The names each module exports in its `__all__`**, and their documented
   behavior.  A name is public exactly when it appears in its module's
   `__all__`; that single test decides it, with no exceptions.  It covers
   explicitly exported special names such as `psycodict.__version__`, and any
   name *absent* from `__all__` — underscore-prefixed or not, docstring or not —
   is implementation: an explicit `from psycodict.utils import range_formatter`
   still resolves, but the name is not part of this promise and may change in
   any release.  (`from psycodict.utils import *`, on the other hand, binds
   `__all__` and nothing else, so a wildcard import binds fewer names than it
   did before 1.0.)  The `__all__` names are exactly the names in the
   [API reference](https://psycodict.readthedocs.io/en/latest/api/index.html),
   and the snapshot test `tests/test_public_api.py` freezes them, so the promise
   and the code cannot drift apart.
 * **The query language** as specified in [QueryLanguage.md](QueryLanguage.md):
   the meaning of a query dictionary is stable within a major version: new
   features may be added in minor versions, but functioning queries will
   remain functional.
 * **The re-exported SQL composition classes** (`from psycodict import SQL,
   Identifier, ...`).  Downstream code should import these from `psycodict`
   rather than from the driver; the re-export point is the stable name.
 * **The export file format** written by `copy_to` and read by `copy_from` /
   `reload` (see [DataManagement.md](DataManagement.md)).  A file may begin with
   a `# psycodict-export-format: N` marker, and a file without one is format 0,
   the historical layout.  A **newer** 1.x reader reads files written by an
   earlier 1.x release; an **older** reader is not guaranteed to understand a
   file a newer release wrote (it may carry a format the older reader predates),
   and a reader refuses a marked file whose version it does not understand
   rather than mis-loading it.  An incompatible change to the file format
   requires a new major version.
 * **The `meta_*` tables**, whose layout is governed by the metadata format
   protocol below.

## Reaching a table

`db[name]` is the canonical, collision-free way to reach a search table, and the
one covered by this promise.  `db.<name>` (attribute access) is convenience
syntax for the same lookup, with one caveat: a real attribute or method of the
database object wins over a table of the same name, so a table called `config`
or `tablenames` is reachable only through `db["config"]`.  Adding a method to
the database class in a minor release therefore never makes a table inaccessible
through the canonical `db[name]` lookup, even if it shadows an attribute-access
name.

## What is not covered

Names absent from their module's `__all__`, whether or not they carry a
docstring and whatever they are named; the exact SQL text psycodict emits (only
its semantics); performance characteristics; the contents of log files; and
undocumented behavior generally, even where observable.  If something
undocumented matters to your project, open an issue — turning it into documented
(hence stable) behavior is usually easy.

## Database metadata compatibility

The layout of the `meta_*` tables is versioned by the **metadata format**
number stored in each database (`meta_format`, with a `min_compat` column
declaring the oldest client format the database still admits); the protocol —
including how clients degrade gracefully against older databases and when a
migration is required — is specified in
[MetadataFormats.md](MetadataFormats.md).  The metadata format number and the
export file format number are protocol revisions in their own right, not the
package major version: a compatible, additive revision may ship in a minor
release while keeping `min_compat` low enough for existing clients.  What
requires a new major version is a change that raises `min_compat` past an
earlier 1.x client, or otherwise makes that client unsafe.

## Deprecation policy

Where feasible, behavior slated for removal first spends at least one minor
release emitting a `DeprecationWarning` naming the replacement.  (The test
suite promotes psycodict's own deprecation warnings to errors, so deprecated
paths cannot linger inside the package itself.)  Removals then happen at the
next major release.

## Python and PostgreSQL support

The supported Python floor is declared in `pyproject.toml`
(`requires-python`); the supported PostgreSQL range is the one exercised in CI.
Dropping an interpreter or server version that has reached upstream end-of-life
is not considered a breaking change and may happen in a minor release — never
in a patch release.
