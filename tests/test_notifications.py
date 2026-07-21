# -*- coding: utf-8 -*-
"""
Tests for psycodict's LISTEN/NOTIFY support (``psycodict/notifications.py``).

These exercise the two halves of the feature:

* *emission* -- ``db.notify`` and the schema-change hooks on
  ``create_table``/``drop_table``/``add_column``/``drop_column`` and the reload
  swap, all of which send ``pg_notify`` transactionally on the main connection;
* *listening* -- a :class:`~psycodict.notifications.NotificationListener` with
  its own ``autocommit`` connection, collected through ``poll`` /``listen``.

Cross-connection delivery is the whole point (the sender is the database's main
connection, the receiver a dedicated one), so every test opens a listener,
performs an operation, and then polls.  A notification is delivered only to a
``LISTEN`` issued *before* it was sent and only once its transaction commits, so
listeners are always created before the operation under test.

Like ``test_devmirror``, this module builds its own ``Configuration`` from the
standard libpq environment variables rather than leaning on the shared session
fixtures, so it is self-contained.  Poll windows are kept short (waits use
``stop_after`` semantics and so return as soon as the expected notification
arrives; the only full-length waits are the deliberately-empty polls) to keep
the added suite runtime to about a second.
"""
import os
import time
import uuid

import pytest

from psycodict.notifications import NotificationListener, SCHEMA_CHANNEL, validate_channel_name
from psycodict.utils import DelayCommit


# How long a poll may wait for a notification we expect to arrive.  poll()
# returns as soon as it does, so this is only an upper bound guarding against a
# hang, not the time a passing test takes.
WAIT = 2.0
# How long a poll waits when we expect *nothing*: long enough that a mistaken
# notification would have arrived, short enough to keep the suite quick.
QUIET = 0.4

COLUMNS = [("n", "integer"), ("label", "text"), ("data", "jsonb")]


def _connection_kwargs():
    # Mirror tests/conftest.py so the module reads the same libpq environment.
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", 5432)),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "dbname": os.environ.get("PGDATABASE", "psycodict_test"),
    }


@pytest.fixture(scope="module")
def notif_config(tmp_path_factory):
    """A ``Configuration`` from the libpq environment (see ``test_devmirror``)."""
    from psycodict.config import Configuration

    tmp = tmp_path_factory.mktemp("notif")
    conn = _connection_kwargs()
    config_file = tmp / "config.ini"
    with open(config_file, "w") as F:
        F.write("[logging]\nslowcutoff = 0.1\nslowlogfile = %s\n" % (tmp / "slow.log"))
        F.write("[postgresql]\n")
        for key, val in conn.items():
            F.write("%s = %s\n" % (key, val))
    return Configuration(
        defaults={"config_file": str(config_file), "secrets_file": str(tmp / "secrets.ini")},
        readargs=False,
    )


@pytest.fixture(scope="module")
def notif_db(notif_config):
    """A ``PostgresDatabase`` on the private test database, meta tables ensured."""
    import psycopg

    from psycodict.database import PostgresDatabase

    try:
        database = PostgresDatabase(config=notif_config, create=True)
    except psycopg.OperationalError as err:
        conn = _connection_kwargs()
        message = "no PostgreSQL server at %s:%s/%s as %s (%s)" % (
            conn["host"], conn["port"], conn["dbname"], conn["user"],
            str(err).strip().split("\n")[0],
        )
        if os.environ.get("PSYCODICT_TEST_DB_REQUIRED"):
            raise RuntimeError("PSYCODICT_TEST_DB_REQUIRED is set but there is %s" % message)
        pytest.skip(message, allow_module_level=True)
    yield database
    database.conn.close()


@pytest.fixture
def make_table(notif_db):
    """Factory for uniquely named tables on ``notif_db``, dropped afterwards."""
    created = []

    def make():
        name = "test_notif_%s" % uuid.uuid4().hex[:12]
        notif_db.create_table(name, COLUMNS, label_col="label", sort=["n"])
        created.append(name)
        return notif_db[name]

    yield make

    for name in reversed(created):
        try:
            if name in notif_db.tablenames:
                notif_db.drop_table(name, force=True)
        except Exception:  # pragma: no cover - cleanup must not mask failures
            pass


def _unique_name():
    return "test_notif_%s" % uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# db.notify / general pub-sub
# ---------------------------------------------------------------------------

def test_notify_is_received_by_listener(notif_db):
    channel = "psycodict_test_chan"
    with notif_db.listener(channels=channel) as listener:
        notif_db.notify(channel, "hello world")
        got = listener.poll(WAIT)
    assert got == [(channel, "hello world")]


def test_notify_defaults_to_empty_payload(notif_db):
    channel = "psycodict_test_chan"
    with notif_db.listener(channels=channel) as listener:
        notif_db.notify(channel)
        got = listener.poll(WAIT)
    assert got == [(channel, "")]


def test_notify_rejects_a_bad_channel(notif_db):
    for bad in ["has space", "1leading", "has-dash", "", "sneaky;DROP"]:
        with pytest.raises(ValueError):
            notif_db.notify(bad, "x")


def test_validate_channel_name_accepts_and_rejects():
    assert validate_channel_name("psycodict_schema") == "psycodict_schema"
    assert validate_channel_name("_ok9") == "_ok9"
    for bad in ["9no", "a b", "a.b", "a-b", "", 3]:
        with pytest.raises(ValueError):
            validate_channel_name(bad)


# ---------------------------------------------------------------------------
# schema-change emission
# ---------------------------------------------------------------------------

def test_create_table_and_drop_table_emit_on_schema_channel(notif_db):
    name = _unique_name()
    with notif_db.listener() as listener:  # defaults to the schema channel
        notif_db.create_table(name, COLUMNS, label_col="label", sort=["n"])
        try:
            created = listener.poll(WAIT)
            assert created == [(SCHEMA_CHANNEL, name)]
        finally:
            notif_db.drop_table(name, force=True)
        dropped = listener.poll(WAIT)
    assert dropped == [(SCHEMA_CHANNEL, name)]


def test_add_column_and_drop_column_emit(notif_db, make_table):
    table = make_table()
    name = table.search_table
    with notif_db.listener() as listener:
        table.add_column("newcol", "integer")
        assert listener.poll(WAIT) == [(SCHEMA_CHANNEL, name)]
        table.drop_column("newcol", force=True)
        assert listener.poll(WAIT) == [(SCHEMA_CHANNEL, name)]


def test_reload_swap_emits_exactly_once(notif_db, make_table, tmp_path):
    table = make_table()
    name = table.search_table
    table.insert_many([{"n": i, "label": "l%d" % i, "data": {"k": i}} for i in range(10)])
    searchfile = str(tmp_path / "search.txt")
    table.copy_to(searchfile)
    with notif_db.listener() as listener:
        notif_db[name].reload(searchfile)
        got = listener.poll(WAIT)
        # The reload rewrites _tmp tables and swaps them in with internal DDL,
        # but only the final swap announces the table -- exactly once.
        extra = listener.poll(QUIET)
    assert got == [(SCHEMA_CHANNEL, name)]
    assert extra == []


# ---------------------------------------------------------------------------
# transactional semantics
# ---------------------------------------------------------------------------

def test_rolled_back_transaction_delivers_no_notification(notif_db):
    channel = "psycodict_test_chan"
    with notif_db.listener(channels=channel) as listener:
        with pytest.raises(RuntimeError):
            with DelayCommit(notif_db):
                notif_db.notify(channel, "doomed")
                raise RuntimeError("boom")  # forces DelayCommit to roll back
        # The NOTIFY rode the rolled-back transaction, so nothing is delivered.
        assert listener.poll(QUIET) == []


def test_committed_delaycommit_delivers_once(notif_db):
    channel = "psycodict_test_chan"
    with notif_db.listener(channels=channel) as listener:
        with DelayCommit(notif_db):
            notif_db.notify(channel, "kept")
            # Not delivered yet: the transaction has not committed.
            assert listener.poll(QUIET) == []
        # DelayCommit committed on exit; now it arrives.
        assert listener.poll(WAIT) == [(channel, "kept")]


# ---------------------------------------------------------------------------
# channel subscription and listener lifecycle
# ---------------------------------------------------------------------------

def test_listener_only_receives_subscribed_channels(notif_db):
    subscribed = "psycodict_test_sub"
    other = "psycodict_test_other"
    with notif_db.listener(channels=subscribed) as listener:
        notif_db.notify(other, "ignored")
        notif_db.notify(subscribed, "wanted")
        got = listener.poll(WAIT)
        got += listener.poll(QUIET)  # give the unsubscribed one time to (not) show
    assert got == [(subscribed, "wanted")]


def test_listener_can_subscribe_to_several_channels(notif_db):
    a, b = "psycodict_test_a", "psycodict_test_b"
    with notif_db.listener(channels=(a, b)) as listener:
        notif_db.notify(a, "1")
        notif_db.notify(b, "2")
        got = listener.poll(WAIT)
        got += listener.poll(QUIET)
    assert sorted(got) == [(a, "1"), (b, "2")]


def test_poll_timeout_returns_empty_quickly(notif_db):
    with notif_db.listener() as listener:
        start = time.time()
        got = listener.poll(QUIET)
        elapsed = time.time() - start
    assert got == []
    # Waited about QUIET seconds, and certainly not a whole extra second.
    assert elapsed < QUIET + 1.0


def test_context_manager_closes_the_connection(notif_db):
    listener = notif_db.listener()
    conn = listener._conn
    with listener as entered:
        assert entered is listener
        assert not conn.closed
    assert listener._conn is None
    assert conn.closed
    # Using a closed listener is an error, not a silent no-op.
    with pytest.raises(RuntimeError):
        listener.poll(0)


def test_close_is_idempotent(notif_db):
    listener = notif_db.listener()
    listener.close()
    listener.close()  # second close must not raise
    assert listener._conn is None


def test_listener_requires_a_channel(notif_db):
    with pytest.raises(ValueError):
        notif_db.listener(channels=())


def test_listen_iterator_yields_until_timeout(notif_db):
    channel = "psycodict_test_iter"
    with notif_db.listener(channels=channel) as listener:
        notif_db.notify(channel, "a")
        notif_db.notify(channel, "b")
        received = []
        # A bounded timeout makes the generator stop on its own; without it the
        # iterator would block forever waiting for more.
        for item in listener.listen(timeout=WAIT):
            received.append(item)
            if len(received) == 2:
                break
    assert received == [(channel, "a"), (channel, "b")]


def test_standalone_listener_matches_the_factory(notif_db):
    # db.listener() is a thin convenience over the public class.
    channel = "psycodict_test_direct"
    with NotificationListener(notif_db.config, channels=channel) as listener:
        notif_db.notify(channel, "direct")
        assert listener.poll(WAIT) == [(channel, "direct")]
