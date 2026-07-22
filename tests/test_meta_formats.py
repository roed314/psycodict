# -*- coding: utf-8 -*-
"""
The metadata-format protocol of MetadataFormats.md: the (version, min_compat)
stamp in meta_format, the connect-time compatibility decision, degraded
operation against an older-format database, explicit migration with
upgrade_metadata, and cross-format metadata files.

The decision itself (_meta_format_action) is a pure function, so the whole
compatibility matrix is tested directly, without manufacturing a database in
each state.  The integration tests then walk a real database through the
states that matter: they rewrite the stamp and drop columns, which is too
destructive for the shared suite database, so they run against a *private*
database reached through a second ``Configuration``.  The private database is
created on the fly when it does not exist (so these tests run in CI, where
only the suite database is provisioned) and is left at the current format.
"""
import csv
import logging
import os
import uuid

import pytest

import conftest
from psycopg.sql import SQL, Identifier

from psycodict.base import META_FORMAT
from psycodict.database import PostgresDatabase


##################################################################
# The compatibility decision (pure)                              #
##################################################################


def test_same_format_is_silent():
    assert PostgresDatabase._meta_format_action(META_FORMAT, 0, False) == ("ok", None)


def test_older_compatible_format_warns_with_a_remedy():
    # format 0 -> current is compatible (the whereclause migration)
    action, message = PostgresDatabase._meta_format_action(0, 0, False)
    assert action == "warn"
    assert "upgrade_metadata" in message
    # ... and a read-only connection (a mirror, or a user without write
    # grants) is told the migration happens on the primary, not told to run
    # something it cannot run.
    action, message = PostgresDatabase._meta_format_action(0, 0, True)
    assert action == "warn"
    assert "primary" in message


def test_older_incompatible_format_refuses(monkeypatch):
    from psycodict import database as database_module

    monkeypatch.setitem(
        database_module.META_MIGRATIONS,
        META_FORMAT,
        dict(database_module.META_MIGRATIONS[META_FORMAT], compatible=False),
    )
    action, message = PostgresDatabase._meta_format_action(META_FORMAT - 1, 0, False)
    assert action == "error"
    assert "upgrade_metadata" in message


def test_older_format_with_unknown_migration_refuses():
    # A gap in the registry (no way to reach the current format) must refuse
    # rather than warn: nothing is known about the missing step.
    action, _ = PostgresDatabase._meta_format_action(-1, 0, False)
    assert action == "error"


def test_newer_format_admitting_us_warns():
    action, message = PostgresDatabase._meta_format_action(
        META_FORMAT + 1, META_FORMAT, False
    )
    assert action == "warn"
    assert "upgrade psycodict" in message
    action, _ = PostgresDatabase._meta_format_action(META_FORMAT + 1, 0, False)
    assert action == "warn"


def test_newer_format_excluding_us_refuses():
    action, message = PostgresDatabase._meta_format_action(
        META_FORMAT + 1, META_FORMAT + 1, False
    )
    assert action == "error"
    assert "upgrade psycodict" in message


def test_empty_stamp_refuses():
    action, message = PostgresDatabase._meta_format_action(None, None, False)
    assert action == "error"
    assert "empty" in message


##################################################################
# A private database to walk through the formats                 #
##################################################################

# Reached with the standard libpq environment variables, but on a private
# database that these destructive tests may rewrite freely; override the name
# with PSYCODICT_TEST_PRIVATE_DB.  The suite database must not be used here.
PRIVATE_DB = os.environ.get("PSYCODICT_TEST_PRIVATE_DB", "psycodict_t5900")


def _admin_execute(statement, args=None):
    """
    Run one statement on the *suite* database outside any transaction (for
    CREATE/DROP DATABASE), using the same connection parameters as the suite.
    """
    import psycopg

    conn = psycopg.connect(autocommit=True, **conftest._connection_kwargs())
    try:
        return conn.execute(statement, args).fetchall()
    except psycopg.ProgrammingError:
        return None
    finally:
        conn.close()


@pytest.fixture(scope="module")
def private_config(tmp_path_factory):
    """
    A ``Configuration`` for the private format-tests database, built the way a
    caller pointing psycodict at a second database would build one.  The
    database is created when missing (and dropped again at the end in that
    case), and is brought to the current metadata format on entry and again on
    exit, so it is left healthy no matter how a test ends.
    """
    import psycopg

    from psycodict.config import Configuration

    try:
        existed = bool(_admin_execute(
            SQL("SELECT 1 FROM pg_database WHERE datname = %s"), [PRIVATE_DB]
        ))
        if not existed:
            _admin_execute(SQL("CREATE DATABASE {0}").format(Identifier(PRIVATE_DB)))
    except psycopg.OperationalError as err:
        pytest.skip(
            "no PostgreSQL server for the private format-tests database (%s)"
            % str(err).strip().split("\n")[0],
            allow_module_level=True,
        )

    conn = dict(conftest._connection_kwargs())
    conn["dbname"] = PRIVATE_DB
    tmp = tmp_path_factory.mktemp("private")
    config_file = tmp / "config.ini"
    with open(config_file, "w") as F:
        F.write("[logging]\nslowcutoff = 0.1\nslowlogfile = %s\n" % (tmp / "slow.log"))
        F.write("[postgresql]\n")
        for key, val in conn.items():
            F.write("%s = %s\n" % (key, val))
    config = Configuration(
        defaults={"config_file": str(config_file), "secrets_file": str(tmp / "secrets.ini")},
        readargs=False,
    )
    # Also the first proof that connect-and-upgrade works end to end.
    PostgresDatabase(config=config, create=True, upgrade=True).conn.close()
    yield config
    # Safety net: always leave the private database at the current format...
    PostgresDatabase(config=config, create=True, upgrade=True).conn.close()
    if not existed:
        # ...and no residue in the server when the database was ours.
        try:
            _admin_execute(SQL("DROP DATABASE {0} WITH (FORCE)").format(Identifier(PRIVATE_DB)))
        except psycopg.Error:  # pragma: no cover - leftovers must not fail the run
            pass


def _degrade(config, *, unstamped):
    """
    Turn the private database into a genuine format-0 database: drop the
    whereclause columns, and either remove the meta_format table entirely (an
    unstamped 0.x database) or stamp (0, 0) explicitly.

    The surgery runs over a fresh upgraded connection (the format check only
    runs at construction, so the connection keeps working while the database
    is altered under it).
    """
    db = PostgresDatabase(config=config, upgrade=True)
    db._execute(SQL("ALTER TABLE meta_indexes DROP COLUMN IF EXISTS whereclause"))
    db._execute(SQL("ALTER TABLE meta_indexes_hist DROP COLUMN IF EXISTS whereclause"))
    if unstamped:
        db._execute(SQL("DROP TABLE IF EXISTS meta_format"))
    else:
        db._execute(SQL("UPDATE meta_format SET version = 0, min_compat = 0"))
    db.conn.close()


def has_whereclause_column(db):
    """Whether meta_indexes currently has the whereclause column."""
    row = db._execute(
        SQL(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'meta_indexes' AND column_name = 'whereclause'"
        )
    ).fetchone()
    return row is not None


def stamp(db):
    """The (version, min_compat) row of meta_format."""
    return db._execute(SQL("SELECT version, min_compat FROM meta_format")).fetchone()


def make_table(db):
    """A uniquely named table on the private database, with a few rows."""
    name = "test_%s" % uuid.uuid4().hex[:12]
    db.create_table(name, [("n", "integer"), ("label", "text")],
                    label_col="label", sort=["n"])
    table = db[name]
    table.insert_many([{"n": i, "label": "l%d" % i} for i in range(5)])
    return table


##################################################################
# Stamping and migration                                         #
##################################################################


def test_upgraded_database_is_stamped_current(private_config):
    db = PostgresDatabase(config=private_config)
    try:
        assert stamp(db) == (META_FORMAT, 1)
        assert db.meta_format == META_FORMAT
        assert has_whereclause_column(db)
    finally:
        db.conn.close()


@pytest.mark.parametrize("unstamped", [True, False], ids=["unstamped", "stamped-0"])
def test_format0_database_warns_and_connects(private_config, caplog, unstamped):
    _degrade(private_config, unstamped=unstamped)
    with caplog.at_level(logging.WARNING):
        db = PostgresDatabase(config=private_config)
    try:
        assert any(
            "metadata format 0" in record.message and "upgrade_metadata" in record.message
            for record in caplog.records
        )
        assert db.meta_format == 0
    finally:
        db.conn.close()


def test_create_on_format0_database_stamps_what_it_found(private_config):
    # create=True fills in missing pieces but must not silently migrate: the
    # unstamped database gains an explicit (0, 0) stamp, nothing more.
    _degrade(private_config, unstamped=True)
    db = PostgresDatabase(config=private_config, create=True)
    try:
        assert stamp(db) == (0, 0)
        assert db.meta_format == 0
        assert not has_whereclause_column(db)
    finally:
        db.conn.close()


def test_upgrade_metadata_stamps_and_is_idempotent(private_config):
    _degrade(private_config, unstamped=False)
    # Plant the transient development-era stamp to check it is retired.
    db = PostgresDatabase(config=private_config)
    try:
        db._execute(SQL("CREATE TABLE meta_version (version integer NOT NULL)"))

        db.upgrade_metadata()
        assert stamp(db) == (META_FORMAT, 1)
        assert db.meta_format == META_FORMAT
        assert has_whereclause_column(db)
        assert not db._table_exists("meta_version")

        # Running it again on an already-current database is a no-op.
        db.upgrade_metadata()
        assert stamp(db) == (META_FORMAT, 1)
    finally:
        db.conn.close()


def test_connect_with_upgrade_true_migrates(private_config):
    _degrade(private_config, unstamped=True)
    db = PostgresDatabase(config=private_config, upgrade=True)
    try:
        assert stamp(db) == (META_FORMAT, 1)
        assert db.meta_format == META_FORMAT
        # The migrated database is fully functional for the new feature.
        table = make_table(db)
        table.create_index(["n"], where="n > 0")
        assert table.list_indexes()[table.search_table + "_n"]["where"] == "n > 0"
        db.drop_table(table.search_table, force=True)
    finally:
        db.conn.close()


def test_future_format_stamp(private_config, caplog):
    db = PostgresDatabase(config=private_config, upgrade=True)
    try:
        db._execute(SQL("UPDATE meta_format SET version = %s, min_compat = %s"),
                    [META_FORMAT + 1, 0])
        # A newer database that admits us connects with a warning, operating
        # at our own format...
        with caplog.at_level(logging.WARNING):
            other = PostgresDatabase(config=private_config)
        assert any("newer" in record.message for record in caplog.records)
        assert other.meta_format == META_FORMAT
        # ...and upgrade_metadata refuses to "upgrade" past what we know.
        with pytest.raises(RuntimeError, match="newer"):
            other.upgrade_metadata()
        other.conn.close()
        # A newer database that excludes us refuses outright.
        db._execute(SQL("UPDATE meta_format SET version = %s, min_compat = %s"),
                    [META_FORMAT + 1, META_FORMAT + 1])
        with pytest.raises(RuntimeError, match="upgrade psycodict"):
            PostgresDatabase(config=private_config)
    finally:
        db._execute(SQL("UPDATE meta_format SET version = %s, min_compat = %s"),
                    [META_FORMAT, 1])
        db.conn.close()


def test_empty_stamp_refuses_to_connect_or_migrate(private_config):
    db = PostgresDatabase(config=private_config, upgrade=True)
    try:
        db._execute(SQL("DELETE FROM meta_format"))
        with pytest.raises(RuntimeError, match="empty"):
            PostgresDatabase(config=private_config)
        with pytest.raises(RuntimeError, match="empty"):
            db.upgrade_metadata()
    finally:
        db._execute(SQL("INSERT INTO meta_format (version, min_compat) VALUES (%s, %s)"),
                    [META_FORMAT, 1])
        db.conn.close()


##################################################################
# Degraded operation against a format-0 database                 #
##################################################################


def test_format0_ordinary_work_and_indexes(private_config, tmp_path):
    _degrade(private_config, unstamped=True)
    db = PostgresDatabase(config=private_config, create=True)
    try:
        table = make_table(db)
        name = table.search_table + "_n"

        # Ordinary reads and writes are untouched by the format.
        assert table.count() == 5
        assert list(table.search({"n": {"$gte": 3}}, "label", sort=["n"])) == ["l3", "l4"]

        # Plain index management works against the 6-column meta_indexes.
        table.create_index(["n"])
        assert table.list_indexes()[name] == {
            "type": "btree", "columns": ["n"], "modifiers": [[]],
        }

        # The format-1 feature fails with instructions, not a database error.
        with pytest.raises(ValueError, match="upgrade_metadata"):
            table.create_index(["label"], where="n > 0")

        # Export, reload and rebuild all speak the 6-column format.
        indexes_file = str(tmp_path / "indexes0.txt")
        table.copy_to_indexes(indexes_file)
        with open(indexes_file) as F:
            width = len(next(csv.reader(F, delimiter="|")))
        assert width == 6
        table.drop_index(name)
        assert table.list_indexes() == {}
        table.reload_indexes(indexes_file)
        table.restore_index(name)
        assert name in table._list_built_indexes()

        db.drop_table(table.search_table, force=True)
    finally:
        db.conn.close()


def test_format0_export_still_loads_after_migrating(private_config, tmp_path):
    # The upgrade guarantee for existing exports: metadata files written at
    # format 0 keep loading once the database moves on, with the columns the
    # file predates left NULL.
    _degrade(private_config, unstamped=True)
    db = PostgresDatabase(config=private_config, create=True)
    try:
        table = make_table(db)
        name = table.search_table + "_n"
        table.create_index(["n"])
        indexes_file = str(tmp_path / "indexes0.txt")
        table.copy_to_indexes(indexes_file)
        tablename = table.search_table
    finally:
        db.conn.close()

    db = PostgresDatabase(config=private_config, upgrade=True)
    try:
        table = db[tablename]
        table.drop_index(name)
        table.reload_indexes(indexes_file)
        indexes = table.list_indexes()
        assert set(indexes) == {name}
        assert "where" not in indexes[name]
        db.drop_table(tablename, force=True)
    finally:
        db.conn.close()


def test_newer_export_is_rejected_on_format0_database(private_config, tmp_path):
    # The other direction cannot work -- the file carries a column the
    # database has nowhere to put -- so it must fail with instructions before
    # touching anything.
    db = PostgresDatabase(config=private_config, upgrade=True)
    try:
        table = make_table(db)
        name = table.search_table + "_n"
        table.create_index(["n"], where="n > 0")
        indexes_file = str(tmp_path / "indexes1.txt")
        table.copy_to_indexes(indexes_file)
        tablename = table.search_table
    finally:
        db.conn.close()

    _degrade(private_config, unstamped=True)
    db = PostgresDatabase(config=private_config)
    try:
        table = db[tablename]
        with pytest.raises(ValueError, match="upgrade"):
            table.reload_indexes(indexes_file)
        # The failed reload left the recorded indexes alone.
        assert name in table.list_indexes()
        db.drop_table(tablename, force=True)
    finally:
        db.conn.close()
