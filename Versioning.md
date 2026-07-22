# Versioning and API stability

Starting with 1.0.0, psycodict follows [semantic versioning](https://semver.org):
breaking changes to the public API happen only at major releases, new
functionality arrives in minor releases, and patch releases contain only fixes.
This document says what "public API" means for a package whose surface includes
not just Python names but also a query language, an on-disk export format, and
metadata tables living inside your database.

## What is public

 * **Non-underscore names** in the `psycodict` package that are documented — in
   the specification documents ([QueryLanguage.md](QueryLanguage.md),
   [Searching.md](Searching.md), [DataManagement.md](DataManagement.md),
   [MetadataFormats.md](MetadataFormats.md)) or in docstrings.  Names with a
   leading underscore are private, whatever module they live in, and may change
   in any release.
 * **The query language** as specified in [QueryLanguage.md](QueryLanguage.md):
   the meaning of a query dictionary is stable within a major version: new
   features may be added in minor versions, but functioning queries will
   remain functional.
 * **The re-exported SQL composition classes** (`from psycodict import SQL,
   Identifier, ...`).  Downstream code should import these from `psycodict`
   rather than from the driver; the re-export point is the stable name.
 * **The export file format** written by `copy_to` and read by `copy_from` /
   `reload` (three header lines, `|` delimiter, `\N` nulls — see
   [DataManagement.md](DataManagement.md)): files written by one 1.x release
   can be loaded by any other.
 * **The `meta_*` tables**, whose layout is governed by the metadata format
   protocol below.

## What is not covered

Underscore-prefixed names; the exact SQL text psycodict emits (only its
semantics); performance characteristics; the contents of log files; and
undocumented behavior generally, even where observable.  If something
undocumented matters to your project, open an issue — turning it into
documented (hence stable) behavior is usually easy.

## Database metadata compatibility

The layout of the `meta_*` tables is versioned by the **metadata format**
number stored in each database (`meta_format`, with a `min_compat` column
declaring the oldest client format the database still admits); the protocol —
including how clients degrade gracefully against older databases and when a
migration is required — is specified in
[MetadataFormats.md](MetadataFormats.md).  The format number is bumped only at
major releases, so within 1.x a database migrated once is understood by every
client.

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
