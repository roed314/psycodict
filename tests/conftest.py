# -*- coding: utf-8 -*-
"""
Shared fixtures for the psycodict test suite.

The tests need a live PostgreSQL server.  Connection parameters are taken from
the standard libpq environment variables, so a stock server needs no
configuration at all:

    PGHOST      (default: localhost)
    PGPORT      (default: 5432)
    PGUSER      (default: postgres)
    PGPASSWORD  (default: empty)
    PGDATABASE  (default: psycodict_test)

The database named by ``PGDATABASE`` must exist and the user must be able to
create tables in it; ``PostgresDatabase(create=True)`` bootstraps the meta
tables on first connection, so an empty database is enough.  Nothing else in
the database is touched: every test works on tables with a unique random
suffix and drops them again afterwards.

If no server is reachable the whole suite is skipped, so that ``pytest`` in a
checkout without PostgreSQL is not a wall of errors.  Continuous integration
sets ``PSYCODICT_TEST_DB_REQUIRED=1``, which turns that skip into a failure.
"""
import os
import uuid

import pytest


def _connection_kwargs():
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", 5432)),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "dbname": os.environ.get("PGDATABASE", "psycodict_test"),
    }


@pytest.fixture(scope="session")
def config(tmp_path_factory):
    """
    A ``Configuration`` pointed at a throwaway config file.

    ``Configuration`` writes its config file if it does not exist and parses
    ``sys.argv`` when run from a script; both are unwanted here, hence the
    temporary path and ``readargs=False`` (without it, ``Configuration`` would
    try to interpret pytest's own command line).
    """
    from psycodict.config import Configuration

    tmp = tmp_path_factory.mktemp("config")
    conn = _connection_kwargs()
    config_file = tmp / "config.ini"
    with open(config_file, "w") as F:
        F.write("[logging]\nslowcutoff = 0.1\nslowlogfile = %s\n" % (tmp / "slow_queries.log"))
        F.write("[postgresql]\n")
        for key, val in conn.items():
            F.write("%s = %s\n" % (key, val))
    return Configuration(
        defaults={"config_file": str(config_file), "secrets_file": str(tmp / "secrets.ini")},
        readargs=False,
    )


@pytest.fixture(scope="session")
def db(config):
    """
    A ``PostgresDatabase`` connected to the test database, meta tables created.
    """
    import psycopg

    from psycodict.database import PostgresDatabase

    try:
        database = PostgresDatabase(config=config, create=True)
    except psycopg.OperationalError as err:
        # Only a failure to reach the server means "skip".  Catching every
        # exception here would turn a genuine regression in the constructor
        # into a skip, and a run that skips everything looks like a run that
        # passed -- anything else must propagate.
        conn = _connection_kwargs()
        message = "no PostgreSQL server at %s:%s/%s as %s (%s: %s)" % (
            conn["host"], conn["port"], conn["dbname"], conn["user"],
            type(err).__name__, str(err).strip().split("\n")[0],
        )
        if os.environ.get("PSYCODICT_TEST_DB_REQUIRED"):
            raise RuntimeError(
                "PSYCODICT_TEST_DB_REQUIRED is set but there is %s" % message
            )
        pytest.skip(message, allow_module_level=True)
    yield database
    database.conn.close()


# The column layout used by most tests: one column of each interesting type,
# since the point of psycodict is translating Python values to and from them.
COLUMNS = [
    ("n", "integer"),
    ("label", "text"),
    ("data", "jsonb"),
    ("num", "numeric"),
    ("vec", "integer[]"),
    ("mat", "numeric[]"),
    ("x", "double precision"),
    ("flag", "boolean"),
]


def sample_row(i):
    """
    The i-th row of the standard sample table; see the ``filled_table`` fixture.
    """
    return {
        "n": i,
        "label": "l%d" % i,
        "data": {"a": [i, 2 * i], "s": "v%d" % (i % 7), "nested": {"k": i % 3}},
        "num": i * 10 + 7,
        "vec": [i, i + 1, i % 5],
        "mat": [i, i * i],
        "x": i * 0.5,
        "flag": (i % 3 == 0),
    }


@pytest.fixture
def table_factory(db):
    """
    Factory creating uniquely named search tables, dropped when the test ends.

    Table names are randomised so that a crashed run leaves no debris that a
    later run would trip over, and so that tests could in principle share a
    database.
    """
    created = []

    def make(columns=None, label_col="label", suffix="", **kwargs):
        name = "test_%s%s" % (uuid.uuid4().hex[:12], suffix)
        db.create_table(
            name,
            COLUMNS if columns is None else columns,
            label_col=label_col,
            sort=kwargs.pop("sort", ["n"]),
            **kwargs
        )
        created.append(name)
        return db[name]

    yield make

    for name in reversed(created):
        try:
            db.drop_table(name, force=True)
        except Exception:  # pragma: no cover - cleanup must not mask failures
            pass


@pytest.fixture
def empty_table(table_factory):
    """
    A freshly created search table with the standard columns and no rows.
    """
    return table_factory()


@pytest.fixture
def filled_table(empty_table):
    """
    A search table holding ``sample_row(0), ..., sample_row(199)``.

    Note that this is the *same* table as ``empty_table``: a test asking for
    both fixtures gets one populated table, not two.  Call ``table_factory()``
    directly when a test genuinely needs a second table.
    """
    empty_table.insert_many([sample_row(i) for i in range(200)])
    return empty_table
