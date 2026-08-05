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
the table.
"""
import argparse
import sys

from psycopg.sql import SQL, Identifier


def _server_side_rows(table):
    """
    Stream ``(id,)`` for every row of ``table`` in configured-sort order plus
    ``id`` as the final tie-breaker, using a named cursor so the whole table is
    never materialized in the client.
    """
    db = table._db
    order = table._sort  # an SQL fragment, or None
    if order is None:
        clause = SQL("ORDER BY id")
    else:
        clause = SQL("ORDER BY {0}, id").format(order)
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
        cur.close()


def audit_table(db, name):
    """
    Return ``("OK", n)``, ``("MISMATCH", detail)`` or ``("ERROR", message)``
    for one table, reading it in sort order without modifying anything.
    """
    if name not in db.tablenames:
        return ("ERROR", "no such table")
    table = db[name]
    if not table._id_ordered:
        return ("ERROR", "not id_ordered")
    prev = None
    count = 0
    try:
        for rid in _server_side_rows(table):
            if prev is not None and rid <= prev:
                return (
                    "MISMATCH",
                    "id %s follows id %s in sort order (row %s)" % (rid, prev, count + 1),
                )
            prev = rid
            count += 1
    except Exception as err:  # pragma: no cover - surfaced to the operator
        return ("ERROR", "%s: %s" % (type(err).__name__, err))
    return ("OK", count)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true", help="audit every id_ordered table"
    )
    group.add_argument("tables", nargs="*", default=[], help="tables to audit")
    args = parser.parse_args(argv)

    from psycodict import db

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
