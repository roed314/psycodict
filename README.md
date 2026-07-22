# psycodict: dictionary-based python interface to PostgreSQL databases

This project was split off from the [L-functions and modular forms database](https://www.lmfdb.org)
so that other projects could use the SQL interface that we created for that project.

Built upon [psycopg2](https://pypi.org/project/psycopg2/), the core of the interface is the ability to create
SELECT queries using a dictionary.  In addition, the package provides a number of other features that were useful for the LMFDB:

 * Data management tools wrapping PostgreSQL's mechanisms for loading from and saving to files (see [DataManagement.md](DataManagement.md))
 * Statistics tables for storing statistics and counts (this is particularly useful in the LMFDB's context since the data changes rarely)

# Install

```
pip3 install -U "psycodict[pgbinary] @ git+https://github.com/roed314/psycodict.git"
```
or
```
pip3 install -U "psycodict[pgsource] @ git+https://github.com/roed314/psycodict.git"
```

# Getting started

You will first need to install [postgres](https://www.postgresql.org/) and create a [user](https://www.postgresql.org/docs/current/sql-createuser.html) and a [database](https://www.postgresql.org/docs/current/sql-createdatabase.html).  For example, you might execute the following commands in psql:

    CREATE DATABASE database_name;
    CREATE USER username;
    ALTER USER psetpartners WITH password 'good password';
    GRANT ALL PRIVILEGES ON DATABASE database_name TO username;

# Running the tests

Install the test dependencies and point the standard PostgreSQL environment
variables at a database you do not mind being written to:

```
pip3 install -e ".[pgbinary,test]"
createdb psycodict_test
PGDATABASE=psycodict_test pytest
```

The connection is configured through `PGHOST`, `PGPORT`, `PGUSER`,
`PGPASSWORD` and `PGDATABASE`, defaulting to `postgres@localhost:5432` and a
database named `psycodict_test`.  The database only needs to be empty: the
meta tables are bootstrapped on first connection, and every test creates its
own randomly named tables and drops them afterwards.

If no server is reachable the tests that need one are skipped, so
`pytest tests/test_encoding.py tests/test_utils.py tests/test_config.py`
works with no database at all.

Two further sets of tests are opt-in:

```
PSYCODICT_TEST_DEVMIRROR=1 pytest tests/test_devmirror.py
```

runs read-only queries against LMFDB's public mirror at `devmirror.lmfdb.xyz`,
which checks psycodict against a real 190-table schema, and

```
PSYCODICT_TEST_DB_REQUIRED=1 pytest
```

turns "no database, skip" into a hard failure — which is what continuous
integration wants, so that a misconfigured job cannot report success by
quietly skipping everything.

