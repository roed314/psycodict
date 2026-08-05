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

from psycopg.sql import SQL, Identifier

from conftest import sample_row


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
