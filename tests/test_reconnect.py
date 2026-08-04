# -*- coding: utf-8 -*-
"""
What a replacement connection has to be.

psycodict replaces its connection when a statement fails on a dead one.  The
replacement is not just any connection to the same server: it has to be made
with the same options (TLS, host, database, timeouts), carry the same session
settings, reach the same database as the same role, and have its capability
flags recomputed rather than inherited.  These tests hold each of those, and
also that reconnecting does not quietly replay an interrupted transaction.

They open their own ``PostgresDatabase`` objects rather than using the shared
``db`` fixture, since they close connections on purpose.
"""
import psycopg
import pytest

from psycopg.sql import SQL

import psycodict.database
from psycodict.database import PostgresDatabase
from psycodict.utils import DelayCommit


@pytest.fixture
def recorded_connect(monkeypatch):
    """
    Record the options every ``psycopg.connect`` call is made with.
    """
    calls = []
    real_connect = psycodict.database.connect

    def recording(**options):
        calls.append(dict(options))
        return real_connect(**options)

    monkeypatch.setattr(psycodict.database, "connect", recording)
    return calls


@pytest.fixture
def own_db(config):
    """
    A database object the test may break, closed when it ends.
    """
    created = []

    def make(**kwargs):
        database = PostgresDatabase(config=config, **kwargs)
        created.append(database)
        return database

    yield make

    for database in created:
        if not database.conn.closed:
            database.conn.close()


def setting(database, name):
    return database._execute(
        SQL("SELECT current_setting(%s)"), [name]
    ).fetchone()[0]


##################################################################
# connection options                                             #
##################################################################


def test_connection_options_carry_the_explicit_overrides(db, monkeypatch):
    """
    The overrides passed to the constructor are part of every connection's
    options, not just the first one's.

    Checked on the merge rather than by connecting, so that the security
    options here need no server that offers TLS and no certificate on disk.
    """
    overrides = {
        "sslmode": "verify-full",
        "sslrootcert": "/example/ca.pem",
        "sslcert": "/example/client.pem",
        "sslkey": "/example/client.key",
        "connect_timeout": 7,
        "options": "-c search_path=public",
        "keepalives_idle": 60,
    }
    monkeypatch.setattr(db, "_connect_kwargs", overrides)
    options = db._connection_options()
    for key, value in overrides.items():
        assert options[key] == value
    # and the configured options are still there underneath
    assert options["dbname"] == db.conn.info.dbname


def test_a_reconnect_uses_the_same_options_as_the_first_connection(own_db, recorded_connect):
    database = own_db(
        connect_timeout=7,
        options="-c search_path=public",
        application_name="psycodict-reconnect-test",
    )
    database.reset_connection()

    assert len(recorded_connect) == 2
    initial, replacement = recorded_connect
    assert initial == replacement
    assert initial["connect_timeout"] == 7
    assert initial["options"] == "-c search_path=public"
    assert initial["application_name"] == "psycodict-reconnect-test"


def test_a_reconnect_after_a_dropped_connection_keeps_the_options(own_db, recorded_connect):
    database = own_db(connect_timeout=7, application_name="psycodict-drop-test")
    database.conn.close()
    # a standalone statement: the connection is replaced and the statement retried
    assert database._execute(SQL("SELECT 1")).fetchone()[0] == 1

    assert len(recorded_connect) == 2
    assert recorded_connect[0] == recorded_connect[1]


##################################################################
# session settings                                               #
##################################################################


def test_session_settings_are_applied_and_survive_a_reconnect(own_db):
    database = own_db(session_settings={"statement_timeout": "7s", "lock_timeout": "3s"})
    assert setting(database, "statement_timeout") == "7s"
    assert setting(database, "lock_timeout") == "3s"

    database.conn.close()
    assert setting(database, "statement_timeout") == "7s"
    assert setting(database, "lock_timeout") == "3s"


def test_the_resolver_gives_a_webserver_connection_its_timeout(monkeypatch):
    """
    The real resolver, on the option set it would see: the 25 second timeout
    for the webserver role is chosen from the effective connection user, and
    the test must not stand in for the code it is checking.

    No database is opened -- this is about the choice, not the connection -- so
    a server with a ``webserver`` role is not needed.
    """
    database = PostgresDatabase.__new__(PostgresDatabase)
    monkeypatch.setattr(
        PostgresDatabase, "_connection_options", lambda self: {"user": "webserver"}
    )
    assert database._resolve_session_settings(None) == {"statement_timeout": "25s"}
    # an explicit setting overrides the default for that role
    assert database._resolve_session_settings({"lock_timeout": "3s"}) == {
        "lock_timeout": "3s"
    }


def test_the_resolver_gives_other_roles_nothing_implicitly(monkeypatch):
    database = PostgresDatabase.__new__(PostgresDatabase)
    monkeypatch.setattr(
        PostgresDatabase, "_connection_options", lambda self: {"user": "postgres"}
    )
    assert database._resolve_session_settings(None) == {}


def test_unknown_session_settings_are_refused(own_db):
    with pytest.raises(ValueError, match="session setting"):
        own_db(session_settings={"log_statement": "all"})


##################################################################
# identity and capabilities                                      #
##################################################################


def test_a_replacement_connection_reaching_elsewhere_is_refused(own_db):
    database = own_db()
    conn = database.conn
    # what the object believes it is connected to no longer matches the server
    database._identity = ("some_other_database", database.conn.info.user)
    with pytest.raises(RuntimeError, match="Refusing to reconnect"):
        database.reset_connection()
    # the old connection was not replaced by the rejected one
    assert database.conn is conn


def test_capabilities_are_recomputed_on_reconnect(own_db):
    database = own_db()
    assert not database._read_only
    # a stale flag from the previous session must not be carried over
    database._read_only = True
    database._super_user = False
    database.reset_connection()
    assert not database._read_only
    assert database._super_user == (
        database.conn.execute("SELECT current_setting('is_superuser')").fetchone()[0] == "on"
    )


##################################################################
# retry semantics                                                #
##################################################################


def test_a_standalone_statement_is_retried_once(own_db, recorded_connect):
    database = own_db()
    database.conn.close()
    assert database._execute(SQL("SELECT 1")).fetchone()[0] == 1
    # one replacement, not a loop of them
    assert len(recorded_connect) == 2


def test_a_transaction_is_not_silently_replayed(own_db):
    """
    Inside DelayCommit the statements before the failure are gone with the
    connection; retrying only the last one would commit a fragment of the
    transaction as though the rest had succeeded.
    """
    database = own_db()
    with pytest.raises(psycopg.OperationalError):
        with DelayCommit(database):
            database._execute(SQL("SELECT 1"))
            database.conn.close()
            database._execute(SQL("SELECT 2"))


def test_a_server_that_stays_unreachable_does_not_loop(own_db, monkeypatch):
    database = own_db()
    database.conn.close()

    def dead(**options):
        raise psycodict.database.DatabaseError("server is not there")

    monkeypatch.setattr(psycodict.database, "connect", dead)
    with pytest.raises(psycopg.DatabaseError, match="server is not there"):
        database._execute(SQL("SELECT 1"))


##################################################################
# adopting a candidate, or not                                   #
##################################################################


class Recorder:
    """
    A registered object that records what it saw when the connection reset.
    """

    def __init__(self, database):
        self.calls = []
        self._db = database
        database._register_object(self)

    def _connection_reset(self):
        self.calls.append(
            {
                "conn": self.conn,
                "read_only": self._db._read_only,
                "super_user": self._db._super_user,
                "knowls": self._db._read_and_write_knowls,
                "userdb": self._db._read_and_write_userdb,
            }
        )


class FailingCandidate:
    """
    A connection that answers the first capability probe and then fails.

    ``fail_on`` is the number of successful ``execute`` calls before the
    failure; ``fail_commit`` fails in ``commit`` instead, which is the other
    way capability detection can stop halfway.
    """

    def __init__(self, real, fail_on=1, fail_commit=False):
        self._real = real
        self._fail_on = fail_on
        self._fail_commit = fail_commit
        self._calls = 0
        self.closed_count = 0
        self.info = real.info

    def execute(self, *args, **kwargs):
        self._calls += 1
        if not self._fail_commit and self._calls > self._fail_on:
            raise psycopg.OperationalError("the candidate died mid-probe")
        return self._real.execute(*args, **kwargs)

    def commit(self):
        if self._fail_commit:
            raise psycopg.OperationalError("the candidate died at commit")
        return self._real.commit()

    def close(self):
        self.closed_count += 1
        return self._real.close()

    @property
    def closed(self):
        return self._real.closed


def capabilities(database):
    return (
        database._read_only,
        database._super_user,
        database._read_and_write_knowls,
        database._read_and_write_userdb,
    )


def test_a_successful_reset_refreshes_registered_objects(own_db):
    database = own_db()
    recorder = Recorder(database)
    old_conn = database.conn

    database.reset_connection()

    assert len(recorder.calls) == 1
    seen = recorder.calls[0]
    # the hook saw the replacement, not the connection it replaced
    assert seen["conn"] is database.conn is not old_conn
    # and the capabilities that were read from it
    assert (seen["read_only"], seen["super_user"]) == capabilities(database)[:2]
    assert old_conn.closed


@pytest.mark.parametrize("fail_commit", [False, True])
def test_a_candidate_that_fails_mid_detection_is_not_adopted(own_db, monkeypatch, fail_commit):
    """
    Capabilities are read into a snapshot and applied together, so a candidate
    that dies partway through cannot leave this object describing a session it
    never adopted.
    """
    database = own_db()
    recorder = Recorder(database)
    old_conn = database.conn
    before = capabilities(database)
    # a state that is wrong for the real connection, to see whether a rejected
    # candidate's answers overwrite it
    database._read_only = True
    database._read_and_write_knowls = True

    candidates = []
    real_new = database._new_connection

    def failing_new_connection():
        candidate = FailingCandidate(real_new(), fail_commit=fail_commit)
        candidates.append(candidate)
        return candidate

    monkeypatch.setattr(database, "_new_connection", failing_new_connection)
    with pytest.raises(psycopg.OperationalError):
        database.reset_connection()

    # nothing was adopted
    assert database.conn is old_conn
    assert not old_conn.closed
    assert capabilities(database) == (True, before[1], True, before[3])
    assert candidates[0].closed_count == 1
    assert recorder.calls == []


def test_a_candidate_reaching_elsewhere_does_not_run_the_hook(own_db):
    database = own_db()
    recorder = Recorder(database)
    database._identity = ("some_other_database", database.conn.info.user)
    with pytest.raises(RuntimeError, match="Refusing to reconnect"):
        database.reset_connection()
    assert recorder.calls == []


def test_the_replaced_connection_is_closed_even_if_a_hook_raises(own_db):
    database = own_db()

    class Angry(Recorder):
        def _connection_reset(self):
            super()._connection_reset()
            raise RuntimeError("this hook could not refresh its state")

    angry = Angry(database)
    old_conn = database.conn
    with pytest.raises(RuntimeError, match="could not refresh"):
        database.reset_connection()
    # the reset is not undone -- the old connection is gone either way -- but
    # it is not leaked, and the failure is not swallowed
    assert len(angry.calls) == 1
    assert old_conn.closed
    assert database.conn is not old_conn


def test_a_connection_whose_setup_fails_is_closed(own_db, monkeypatch):
    database = own_db()
    closed = []

    class Spy:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True
            closed.append(self)

    spy = Spy()
    monkeypatch.setattr(psycodict.database, "connect", lambda **options: spy)
    monkeypatch.setattr(
        database, "_configure_session",
        lambda conn: (_ for _ in ()).throw(psycopg.OperationalError("setup failed")),
    )
    live = database.conn
    with pytest.raises(psycopg.OperationalError, match="setup failed"):
        database._new_connection()
    assert closed == [spy]
    assert database.conn is live


def test_a_close_failure_does_not_hide_the_setup_failure(own_db, monkeypatch):
    database = own_db()

    class Stubborn:
        closed = False

        def close(self):
            raise RuntimeError("close also failed")

    monkeypatch.setattr(psycodict.database, "connect", lambda **options: Stubborn())
    monkeypatch.setattr(
        database, "_configure_session",
        lambda conn: (_ for _ in ()).throw(psycopg.OperationalError("setup failed")),
    )
    with pytest.raises(psycopg.OperationalError, match="setup failed"):
        database._new_connection()
