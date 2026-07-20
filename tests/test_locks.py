# -*- coding: utf-8 -*-
"""
Tests for the lock-visibility tools (issue #24): show_queries, show_blocked,
show_locks, and the reload lock warning.

These tests open extra raw psycopg connections so that another session
genuinely runs queries, holds locks, or waits on them.
"""
import os
import threading
import time

import psycopg
import pytest

from psycodict.utils import LockError


def raw_connection():
    """
    A second connection to the test database, configured from the same
    environment variables as the ``db`` fixture.
    """
    return psycopg.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "psycodict_test"),
    )


def wait_until(condition, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_show_queries_sees_other_sessions(db, capsys):
    conn = raw_connection()
    try:
        pid = conn.info.backend_pid

        def sleeper():
            try:
                conn.execute("SELECT pg_sleep(30)")
            except psycopg.errors.QueryCanceled:
                pass

        runner = threading.Thread(target=sleeper)
        runner.start()
        try:
            assert wait_until(lambda: any(
                q[0] == pid and "pg_sleep" in q[3] for q in db._get_queries()
            )), "the other session's query never showed up in _get_queries"
            db.show_queries()
            out = capsys.readouterr().out
            assert "pid %s" % pid in out
            assert "pg_sleep" in out
        finally:
            conn.cancel()
            runner.join(timeout=10)
    finally:
        conn.close()


def test_show_blocked_pairs_waiter_with_holder(db, empty_table, capsys):
    name = empty_table.search_table
    holder = raw_connection()
    waiter = raw_connection()
    try:
        hpid = holder.info.backend_pid
        wpid = waiter.info.backend_pid
        holder.execute('LOCK TABLE "%s" IN ACCESS EXCLUSIVE MODE' % name)

        def blocked_lock():
            try:
                waiter.execute('LOCK TABLE "%s" IN ACCESS SHARE MODE' % name)
            except psycopg.Error:
                pass

        blocked = threading.Thread(target=blocked_lock)
        blocked.start()
        try:
            assert wait_until(lambda: any(
                row[0] == wpid and row[2] == hpid for row in db._get_blocked()
            )), "the blocked statement never showed up in _get_blocked"
            db.show_blocked()
            out = capsys.readouterr().out
            assert "pid %s" % wpid in out
            assert "pid %s" % hpid in out
            assert "LOCK TABLE" in out
            # the holder's lock also shows up in the existing show_locks
            db.show_locks()
            out = capsys.readouterr().out
            assert name in out
            assert "AccessExclusiveLock" in out
        finally:
            holder.rollback()
            blocked.join(timeout=10)
    finally:
        waiter.close()
        holder.close()


def test_check_locks_warns_for_reload_when_table_locked(db, empty_table, capsys):
    name = empty_table.search_table
    conn = raw_connection()
    try:
        pid = conn.info.backend_pid
        conn.execute('LOCK TABLE "%s" IN EXCLUSIVE MODE' % name)
        # warns but does not raise: the reload can proceed and only the final
        # swap needs the table to itself
        assert empty_table._check_locks("reload") is None
        out = capsys.readouterr().out
        assert "Warning" in out
        assert str(pid) in out
        assert "show_queries" in out
    finally:
        conn.rollback()
        conn.close()


def test_check_locks_reload_is_quiet_without_locks(empty_table, capsys):
    assert empty_table._check_locks("reload") is None
    assert "Warning" not in capsys.readouterr().out


def test_check_locks_still_raises_for_writes(db, empty_table):
    name = empty_table.search_table
    conn = raw_connection()
    try:
        conn.execute('LOCK TABLE "%s" IN ACCESS EXCLUSIVE MODE' % name)
        with pytest.raises(LockError):
            empty_table._check_locks("insert_many")
    finally:
        conn.rollback()
        conn.close()
