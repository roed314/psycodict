# Contributing to psycodict

We are excited that you are interested in contributing to psycodict.
Contributions may be written manually or with automated tools,
including large language models. In either case, the person submitting
the contribution is responsible for its correctness, licensing, security,
and adherence to this guide.

Bug reports, documentation improvements, tests, performance work, and focused
code changes are all welcome.

## Before starting

For a small bug fix or documentation correction, opening a pull request
directly is fine.

For a new public API, significant behavioral change, metadata-format change, or
large refactor, please open an issue first. This lets maintainers confirm the
intended behavior and compatibility requirements before substantial work is
done.

Keep pull requests focused. Unrelated cleanup, formatting, and refactoring
should be submitted separately.

Security vulnerabilities must be reported according to
[SECURITY.md](SECURITY.md), not through a public issue.

## Sources of truth

Before changing behavior, read the relevant documentation and nearby tests:

- [QueryLanguage.md](QueryLanguage.md) specifies the dictionary query language.
- [Searching.md](Searching.md) specifies the stable read API.
- [DataManagement.md](DataManagement.md) covers writes, bulk operations,
  statistics, and schema management.
- [MetadataFormats.md](MetadataFormats.md) specifies metadata compatibility and
  the process for changing the metadata format.
- `tests/` records expected behavior and regression cases.

Public methods documented in these files are part of the 1.x compatibility
contract. Names beginning with an underscore are implementation details and
may change without notice.

If documentation, tests, and implementation disagree, do not silently choose
one. Explain the discrepancy in the issue or pull request.

## Development setup

psycodict supports Python 3.9 and newer. Avoid syntax or library features that
would raise the minimum Python version unintentionally.

Create a virtual environment and install the development dependencies:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[pgbinary,test]"
python -m pip install ruff
```

The `pgsource` extra may be used instead of `pgbinary` when testing against a
system installation of libpq.

## PostgreSQL test database

Most tests require PostgreSQL. Always use a dedicated, disposable test
database. Do not point the test suite at a production database or a database
containing valuable data.

The suite creates tables and metadata and may upgrade an older psycodict
metadata format.

A typical local setup is:

```sh
createdb psycodict_test
export PGDATABASE=psycodict_test
export PSYCODICT_TEST_DB_REQUIRED=1
pytest
```

Connection parameters use the standard libpq variables:

- `PGHOST`
- `PGPORT`
- `PGUSER`
- `PGPASSWORD`
- `PGDATABASE`

Setting `PSYCODICT_TEST_DB_REQUIRED=1` is important for a full validation run:
without it, tests requiring an unavailable database are skipped.

Database-free tests can be run with:

```sh
pytest tests/test_encoding.py tests/test_utils.py tests/test_config.py
```

The optional devmirror tests run read-only queries against the public LMFDB
mirror and require network access:

```sh
PSYCODICT_TEST_DEVMIRROR=1 pytest tests/test_devmirror.py
```

Do not send write queries, schema changes, or private data to the devmirror.

## Making changes

### Code

- Preserve compatibility with every supported Python and PostgreSQL version.
- Follow the existing style in the files being changed.
- Run `ruff check .`; do not perform unrelated bulk formatting.
- Prefer parameterized SQL and psycopg composition objects over string
  interpolation.
- Preserve transaction, lock, cleanup, and rollback behavior on failure.
- Do not catch broad exceptions merely to make a failing operation appear
  successful.
- Avoid changing public behavior as an incidental part of a refactor.

### Tests

Every bug fix should normally include a regression test that:

1. Fails for the original bug.
2. Exercises the public interface where practical.
3. Passes after the fix.
4. Tests the relevant failure or boundary cases, not just the happy path.

Database tests should use the fixtures in `tests/conftest.py`. They create
uniquely named tables and clean them up after each test.

Do not weaken, skip, or delete an existing test solely to make a proposed
change pass. If an expected behavior is intentionally changing, update the
test and explain the compatibility impact in the pull request.

### Documentation

Update the appropriate narrative document whenever observable behavior changes.
Update docstrings for changed public methods and parameters.

Changes to the following require particular care:

- Query operators, joins, projections, or sorting:
  `QueryLanguage.md` and possibly `Searching.md`.
- Search return values or public method signatures: `Searching.md`.
- Writes, reloads, statistics, indexes, or schema operations:
  `DataManagement.md`.
- Metadata tables, migrations, or compatibility: `MetadataFormats.md`.

Build the documentation without warnings:

```sh
python -m pip install ".[pgbinary]" -r docs/requirements.txt
python -m sphinx -W --keep-going -b html docs docs/_build/html
```

We write `CHANGELOG.md` as part of the release process.

### Metadata-format changes

A metadata-format change is a release-level compatibility decision, not an
ordinary schema edit. Follow the complete checklist in `MetadataFormats.md`.

Such a pull request must test:

- Creating a fresh database.
- Connecting to the current format.
- Connecting to older compatible formats.
- Refusing incompatible formats.
- Explicit migration and repeated migration.
- Read-only behavior.
- Compatibility with older psycodict clients where applicable.

Connecting to a database must not perform an undocumented or accidental
migration.

### Packaging changes

For packaging or release-related changes, run:

```sh
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
```

psycodict uses a flat repository layout. An import performed from the
repository root may load the source checkout instead of the installed wheel.
Wheel smoke tests must therefore run from a directory outside the checkout.
The CI package job performs this isolated check.

## Guidance for LLM and automated contributors

Automated contributors must follow all requirements above. In addition:

- Read the relevant documentation, tests, and surrounding implementation before
  editing.
- Treat the issue or requested task as the scope boundary. Do not add adjacent
  features or broad cleanup without explicit approval.
- Preserve unrelated working-tree changes.
- Do not invent APIs, configuration options, test results, or repository
  conventions.
- Do not claim that a command passed unless it was actually run. Report skipped,
  unavailable, or failing checks explicitly.
- Inspect the complete diff before finishing and remove debugging code,
  temporary files, generated artifacts, and accidental changes.
- Prefer the smallest change that fully addresses the demonstrated problem.
- Do not replace a precise failure with a broad fallback that conceals it.
- Never use real credentials, production databases, or private data in tests,
  examples, prompts, logs, or pull-request descriptions.
- When changing SQL generation, examine both the generated SQL and its bound
  parameters, and add adversarial tests for injection or quoting boundaries.
- When changing destructive or schema-mutating operations, test interruption,
  rollback, cleanup, and repeated execution.
- If requirements are ambiguous or authoritative sources disagree, stop and
  describe the ambiguity instead of guessing.

Use of an LLM does not transfer authorship or responsibility to the tool.
The submitter must understand and be able to explain the resulting change.
The initial draft of this document was written by GPT 5.6.

## Pull requests

A pull request should include:

- A concise description of the problem and the chosen solution.
- Any issue it fixes or relates to.
- Tests added or changed.
- The exact validation commands run and their results.
- Any checks that were not run and why.
- Public API, database, migration, performance, or security implications.
- Documentation and changelog updates where applicable.

Keep generated files and unrelated changes out of the pull request. Before
submission, review:

```sh
git status --short
git diff --check
git diff
ruff check .
PSYCODICT_TEST_DB_REQUIRED=1 pytest
```

CI also runs tests across supported Python and PostgreSQL versions, package
validation, documentation validation, and downstream regression suites. A
green CI run supplements local review; it does not replace understanding the
change.

## Licensing

By submitting a contribution, you agree that it may be distributed under
psycodict's GNU General Public License, version 2 or later.
