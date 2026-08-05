# -*- coding: utf-8 -*-
"""
The read-only id-order audit and the write-path invariants it protects.

``scripts/audit_id_order.py`` walks each id_ordered table in sort order and
checks the ids come out increasing.  It exists because the flag can drift, so
the audit -- not the flag -- is the ground truth during the one-time
production check.
"""
import importlib.util
import pathlib

import pytest

import psycopg
from psycopg.sql import SQL, Identifier

from conftest import sample_row


def _idle(db):
    """
    Whether the connection is between transactions rather than inside one.
    """
    return db.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


# Load the script as a module, since scripts/ is not a package.
_AUDIT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "audit_id_order.py"
_spec = importlib.util.spec_from_file_location("audit_id_order", _AUDIT_PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


@pytest.fixture
def ordered_table(table_factory):
    table = table_factory(sort=["n"])
    table.insert_many([sample_row(i) for i in range(20)])
    table.resort()
    return table


def test_audit_reports_ok_for_an_ordered_table(db, ordered_table):
    status, detail = audit.audit_table(db, ordered_table.search_table)
    assert status == "OK"
    assert detail == 20


def test_audit_reads_nothing_but_reports_the_row_count(db, ordered_table):
    status, detail = audit.audit_table(db, ordered_table.search_table)
    assert status == "OK" and detail == ordered_table.count()


def test_audit_catches_a_table_whose_ids_do_not_match_the_sort(db, table_factory):
    """
    A table that claims to be ordered but whose ids run against the sort must
    be caught, even though its flag says out_of_order = false.
    """
    table = table_factory(sort=["n"])
    table.insert_many([sample_row(i) for i in range(10)])
    # Reverse the ids against n, then lie in the metadata: ordered, not stale.
    table._execute(
        SQL("UPDATE {0} SET id = 100 - n").format(Identifier(table.search_table))
    )
    table._execute(
        SQL("UPDATE meta_tables SET id_ordered = true, out_of_order = false WHERE name = %s"),
        [table.search_table],
    )
    table._db.conn.commit()

    status, detail = audit.audit_table(db, table.search_table)
    assert status == "MISMATCH"


def test_audit_errors_on_a_non_ordered_table(db, table_factory):
    table = table_factory(sort=["n"], id_ordered=False)
    table.insert_many([sample_row(i) for i in range(3)])
    status, detail = audit.audit_table(db, table.search_table)
    assert status == "ERROR"
    assert "id_ordered" in detail


def test_audit_errors_on_a_missing_table(db):
    status, detail = audit.audit_table(db, "no_such_table_xyz")
    assert status == "ERROR"


def test_audit_does_not_modify_the_database(db, ordered_table):
    before = [rec["id"] for rec in ordered_table.search({}, ["id"], sort=[["id", 1]])]
    audit.audit_table(db, ordered_table.search_table)
    after = [rec["id"] for rec in ordered_table.search({}, ["id"], sort=[["id", 1]])]
    assert before == after


def test_audit_on_an_empty_ordered_table(db, table_factory):
    table = table_factory(sort=["n"])
    status, detail = audit.audit_table(db, table.search_table)
    assert status == "OK" and detail == 0


def test_audit_errors_when_id_ordered_but_no_sort(db, table_factory):
    """
    ``id_ordered`` with no sort configured cannot be audited: ordering by id
    itself would call every such table OK by construction, which asserts
    nothing.  It is a broken metadata state, so say so.
    """
    table = table_factory(sort=None, id_ordered=True)
    table.insert_many([sample_row(i) for i in range(5)])
    assert table._id_ordered and table._sort is None

    status, detail = audit.audit_table(db, table.search_table)
    assert status == "ERROR"
    assert "sort" in detail


def _break_the_sort_under_the_metadata(table):
    """
    Drop the column a table is sorted by, behind psycodict's back, leaving
    meta_tables still naming it in the sort.

    That is the shape of failure the audit meets in the wild -- metadata that
    no longer describes the database -- and it makes the generated ``SELECT
    ... ORDER BY`` fail on the server, aborting the audit's transaction.
    """
    table._execute(
        SQL("ALTER TABLE {0} DROP COLUMN {1}").format(
            Identifier(table.search_table), Identifier("n")
        )
    )
    table._db.conn.commit()


def test_an_audit_error_does_not_poison_the_tables_audited_after_it(db, table_factory):
    """
    A failed audit must end its own transaction.  Otherwise the connection
    stays in an aborted transaction and every later table reports
    InFailedSqlTransaction instead of being audited -- so a single broken
    table would turn a whole ``--all`` run into a wall of false errors.
    """
    broken = table_factory(sort=["n"])
    broken.insert_many([sample_row(i) for i in range(5)])
    healthy = table_factory(sort=["n"])
    healthy.insert_many([sample_row(i) for i in range(5)])
    healthy.resort()
    _break_the_sort_under_the_metadata(broken)

    status, detail = audit.audit_table(db, broken.search_table)
    assert status == "ERROR"
    assert "InFailedSqlTransaction" not in detail

    status, detail = audit.audit_table(db, healthy.search_table)
    assert (status, detail) == ("OK", 5)


def test_the_connection_is_idle_after_every_kind_of_result(db, table_factory, ordered_table):
    """
    Each table is audited in a bounded read transaction, whatever the result,
    so the next table starts from a fresh snapshot rather than inheriting a
    transaction opened by the previous one.
    """
    assert audit.audit_table(db, ordered_table.search_table)[0] == "OK"
    assert _idle(db)

    mismatched = table_factory(sort=["n"])
    mismatched.insert_many([sample_row(i) for i in range(10)])
    mismatched._execute(
        SQL("UPDATE {0} SET id = 100 - n").format(Identifier(mismatched.search_table))
    )
    mismatched._db.conn.commit()
    assert audit.audit_table(db, mismatched.search_table)[0] == "MISMATCH"
    assert _idle(db)

    broken = table_factory(sort=["n"])
    broken.insert_many([sample_row(i) for i in range(3)])
    _break_the_sort_under_the_metadata(broken)
    assert audit.audit_table(db, broken.search_table)[0] == "ERROR"
    assert _idle(db)


def test_audit_streams_through_a_server_side_cursor_and_closes_it(db, ordered_table, monkeypatch):
    """
    The audit reads whole tables, so it must keep the sort and the result set
    on the server: a client-side cursor would pull every row of a production
    table into memory.  The cursor must also be closed once the audit is done
    with it, including when it stops early.
    """
    handed_out = []
    real_cursor = db._cursor

    def spy(buffered=False):
        cur = real_cursor(buffered=buffered)
        handed_out.append((buffered, cur))
        return cur

    monkeypatch.setattr(db, "_cursor", spy)

    assert audit.audit_table(db, ordered_table.search_table)[0] == "OK"
    assert handed_out, "the audit did not open a cursor"
    assert all(buffered for buffered, _ in handed_out)
    # A named cursor is what makes it server-side; see _cursor(buffered=True).
    assert all(cur.name for _, cur in handed_out)
    assert all(cur.closed for _, cur in handed_out)


def test_mismatch_closes_the_cursor_before_returning(db, table_factory, monkeypatch):
    """
    The mismatch return leaves the streaming generator part way through, so
    the cursor is only closed if the audit closes it deliberately.
    """
    table = table_factory(sort=["n"])
    table.insert_many([sample_row(i) for i in range(10)])
    table._execute(
        SQL("UPDATE {0} SET id = 100 - n").format(Identifier(table.search_table))
    )
    table._db.conn.commit()

    handed_out = []
    real_cursor = db._cursor

    def spy(buffered=False):
        cur = real_cursor(buffered=buffered)
        handed_out.append(cur)
        return cur

    monkeypatch.setattr(db, "_cursor", spy)

    assert audit.audit_table(db, table.search_table)[0] == "MISMATCH"
    assert handed_out and all(cur.closed for cur in handed_out)


def test_main_audits_the_database_it_is_given(db, ordered_table, capsys):
    """
    The documented command line has to actually run.  ``main`` takes the
    database as an argument so the test can hand it the fixture connection;
    without one it builds a PostgresDatabase from the usual configuration.
    """
    rc = audit.main([ordered_table.search_table], db=db)
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
    assert ordered_table.search_table in out


def test_main_reports_a_mismatch_with_a_nonzero_status(db, table_factory, capsys):
    table = table_factory(sort=["n"])
    table.insert_many([sample_row(i) for i in range(10)])
    table._execute(
        SQL("UPDATE {0} SET id = 100 - n").format(Identifier(table.search_table))
    )
    table._db.conn.commit()

    rc = audit.main([table.search_table], db=db)
    assert rc == 1
    assert "MISMATCH" in capsys.readouterr().out
