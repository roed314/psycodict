#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check whether the ``id_ordered`` tables really are ordered.

A table marked ``id_ordered`` with ``out_of_order = false`` asserts that
ascending ``id`` is the configured default sort.  Search code relies on that:
it may replace ``ORDER BY <sort>`` with ``ORDER BY id``.  Historically the flag
could drift -- a non-inplace ``update_from_file`` left it stale -- so before
trusting it, audit it.

This is read-only.  For each selected table it streams the rows in configured
sort order (with ``id`` as the final tie-breaker) and checks that the ids come
out strictly increasing.  Nothing is written.

Usage::

    python scripts/audit_id_order.py --all
    python scripts/audit_id_order.py TABLE [TABLE ...]

Exit status is 0 when every audited table is OK, 1 when any is out of order or
errored, 2 for a usage problem.

The audit reads every row in sort order, which for a large table is a
substantial database sort; schedule it accordingly.  Rows are streamed from the
server (a named cursor), so the client stays at constant memory however large
the table.  Each table is audited in a read transaction of its own, so one
snapshot is not held open across the whole run and a table that errors does not
carry that error into the tables audited after it.
"""
import argparse
import contextlib
import sys

import psycopg
from psycopg.sql import SQL, Identifier


def _server_side_rows(table):
    """
    Stream ``(id,)`` for every row of ``table`` in configured-sort order plus
    ``id`` as the final tie-breaker, using a named cursor so the whole table is
    never materialized in the client.

    ``table._sort`` must be set; :func:`audit_table` has already checked.  The
    caller owns the read transaction the named cursor runs in and must close
    this generator, so that the ``finally`` below closes the cursor even when
    the audit stops early on a mismatch.
    """
    db = table._db
    clause = SQL("ORDER BY {0}, id").format(table._sort)
    query = SQL("SELECT id FROM {0} {1}").format(Identifier(table.search_table), clause)
    # A server-side (named) cursor keeps the sort on the server and hands back
    # rows in batches; see PostgresBase._cursor(buffered=True).
    cur = db._cursor(buffered=True)
    try:
        cur.execute(query)
        while True:
            batch = cur.fetchmany(10000)
            if not batch:
                break
            for row in batch:
                yield row[0]
    finally:
        # CLOSE is itself a statement, so it fails when the audit failed and
        # left the transaction aborted.  The audit's own error is the one
        # worth reporting, and the cursor dies with the transaction that
        # audit_table is about to end regardless.
        with contextlib.suppress(psycopg.Error):
            cur.close()


def _end_read_transaction(db):
    """
    Finish the transaction the audit read in, leaving the connection idle.

    The audit writes nothing, so rolling back is as good as committing, and
    unlike a commit it also works after a failed statement: without this, one
    table's error would leave the connection in an aborted transaction and
    every table audited afterwards would fail with ``InFailedSqlTransaction``
    instead of being audited.  Ending the transaction also stops a long
    multi-table run from holding a single MVCC snapshot open from the first
    table to process exit.

    Never raises: a failure to clean up must not replace the audit result the
    caller is about to return.
    """
    with contextlib.suppress(psycopg.Error):
        db.conn.rollback()


def audit_table(db, name):
    """
    Return ``("OK", n)``, ``("MISMATCH", detail)`` or ``("ERROR", message)``
    for one table, reading it in sort order without modifying anything.

    Each call runs in a read transaction of its own which is finished on every
    exit path, so that tables are audited independently of each other.
    """
    if name not in db.tablenames:
        return ("ERROR", "no such table")
    table = db[name]
    if not table._id_ordered:
        return ("ERROR", "not id_ordered")
    if table._sort is None:
        # Falling back to "ORDER BY id" here would report every such table as
        # OK by construction.  id_ordered with no sort does not describe an
        # invariant, so there is nothing this script can confirm.
        return ("ERROR", "id_ordered but no sort configured")
    prev = None
    count = 0
    try:
        # closing() so that returning early on a mismatch runs the generator's
        # finally and closes the named cursor there and then, rather than
        # leaving it open until the generator is garbage collected.
        with contextlib.closing(_server_side_rows(table)) as rows:
            for rid in rows:
                if prev is not None and rid <= prev:
                    return (
                        "MISMATCH",
                        "id %s follows id %s in sort order (row %s)" % (rid, prev, count + 1),
                    )
                prev = rid
                count += 1
    except Exception as err:
        return ("ERROR", "%s: %s" % (type(err).__name__, err))
    finally:
        _end_read_transaction(db)
    return ("OK", count)


def main(argv=None, db=None):
    """
    Run the audit over the tables named on the command line.

    ``db`` is the database to audit; when it is None (the command-line case)
    one is opened from the usual configuration.  The tests pass their own
    fixture database rather than opening a second connection to it.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true", help="audit every id_ordered table"
    )
    group.add_argument("tables", nargs="*", default=[], help="tables to audit")
    args = parser.parse_args(argv)

    if db is None:
        # The package exports no ready-made database object, so build one the
        # supported way: PostgresDatabase() picks up $PSYCODICT_CONFIG or
        # config.ini exactly as an application using psycodict would.
        from psycodict.database import PostgresDatabase

        db = PostgresDatabase()

    if args.all:
        names = sorted(
            name for name in db.tablenames if db[name]._id_ordered
        )
        if not names:
            print("No id_ordered tables.")
            return 0
    else:
        names = args.tables

    worst = 0
    for name in names:
        status, detail = audit_table(db, name)
        if status == "OK":
            print("OK       %-40s %s rows" % (name, detail))
        elif status == "MISMATCH":
            print("MISMATCH %-40s %s" % (name, detail))
            worst = max(worst, 1)
        else:
            print("ERROR    %-40s %s" % (name, detail))
            worst = max(worst, 1)
    return worst


if __name__ == "__main__":
    sys.exit(main())
