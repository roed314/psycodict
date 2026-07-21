# -*- coding: utf-8 -*-
"""
Tests for the ``reindex`` contract of ``rewrite`` and ``update_from_file``
(LMFDB/lmfdb#6596 and LMFDB/lmfdb#5869).

Both methods update a table by building a replacement table and swapping it in
(so that ``reload_revert`` works and no locks are taken on the live table),
which forces every index to be rebuilt on the new table.  ``reindex=False``
cannot be honored there: ``rewrite`` no longer accepts the parameter at all,
and both methods raise an explanatory ``ValueError`` when it is requested.
With ``inplace=True`` the rows are edited on the live table and ``reindex`` is
a genuine speed knob (drop the indexes touching the updated columns, rebuild
them afterward), so there it keeps working -- in both directions.
"""
import inspect

import pytest
from psycopg.sql import SQL

from conftest import sample_row
from psycodict.table import PostgresTable


def _bump(record):
    record["num"] = record["n"] + 1
    return record


def _built_indexes(table):
    return set(table._list_built_indexes())


def _index_oid(table, index_name):
    """
    The OID of the named index, which changes when it is dropped and rebuilt.
    """
    cur = table._execute(SQL("SELECT oid FROM pg_class WHERE relname = %s"), [index_name])
    return cur.fetchone()[0]


def _write_num_file(path, rows):
    """
    A minimal update file setting ``num`` for the given (label, num) pairs.
    """
    with open(path, "w") as F:
        F.write("label|num\ntext|numeric\n\n")
        for label, num in rows:
            F.write("%s|%s\n" % (label, num))


##################################################################
# signatures                                                     #
##################################################################


def test_rewrite_no_longer_has_a_reindex_parameter():
    assert "reindex" not in inspect.signature(PostgresTable.rewrite).parameters


def test_update_from_file_reindex_defaults_to_automatic():
    # None means "decide from the number of updated rows" (inplace only); the
    # old default of True silently promised a rebuild the swap path does anyway.
    parameters = inspect.signature(PostgresTable.update_from_file).parameters
    assert parameters["reindex"].default is None


##################################################################
# reindex=False on the swap-based paths raises                   #
##################################################################


def test_rewrite_reindex_false_raises_up_front(filled_table):
    # The exact call from LMFDB/lmfdb#6596, minus the table name.
    with pytest.raises(ValueError, match="inplace=True"):
        filled_table.rewrite(_bump, reindex=False, restat=False)
    # The failure comes before the dump: nothing was modified or left behind.
    assert filled_table.lucky({"n": 5}, projection="num") == sample_row(5)["num"]
    assert not filled_table._table_exists(filled_table.search_table + "_tmp")


def test_update_from_file_reindex_false_raises_without_inplace(filled_table, tmp_path):
    fname = str(tmp_path / "update.txt")
    _write_num_file(fname, [("l0", 1000)])
    with pytest.raises(ValueError, match="reindex=False is impossible"):
        filled_table.update_from_file(fname, reindex=False)
    assert filled_table.lucky({"label": "l0"}, projection="num") == sample_row(0)["num"]
    assert not filled_table._table_exists("tmp_update_from_file")


def test_rewrite_reindex_true_is_forwarded_and_harmless(filled_table):
    # reindex=True's promise (indexes present afterward) is kept by the swap
    # path anyway, so it is still accepted and passed through the kwds.
    filled_table.rewrite(_bump, reindex=True)
    assert filled_table.lucky({"n": 5}, projection="num") == 6


##################################################################
# the swap paths really do rebuild every index                   #
##################################################################


def test_rewrite_rebuilds_indexes_on_the_swapped_in_table(filled_table):
    filled_table.create_index(["num"])
    index_name = filled_table.search_table + "_num"
    assert index_name in _built_indexes(filled_table)
    filled_table.rewrite(_bump)
    assert filled_table.lucky({"n": 5}, projection="num") == 6
    assert filled_table.lucky({"n": 5}, projection="label") == "l5"
    built = _built_indexes(filled_table)
    assert index_name in built
    assert filled_table.search_table + "_pkey" in built


def test_update_from_file_swap_path_updates_rows_and_rebuilds_indexes(filled_table, tmp_path):
    filled_table.create_index(["num"])
    fname = str(tmp_path / "update.txt")
    _write_num_file(fname, [("l0", 1000), ("l1", 1001)])
    filled_table.update_from_file(fname)
    assert filled_table.lucky({"label": "l0"}, projection="num") == 1000
    assert filled_table.lucky({"label": "l1"}, projection="num") == 1001
    assert filled_table.lucky({"label": "l2"}, projection="num") == sample_row(2)["num"]
    built = _built_indexes(filled_table)
    assert filled_table.search_table + "_num" in built
    assert filled_table.search_table + "_pkey" in built


##################################################################
# inplace updates: reindex is honored, in both directions        #
##################################################################


def test_update_from_file_inplace_honors_reindex_false(filled_table, tmp_path):
    filled_table.create_index(["num"])
    index_name = filled_table.search_table + "_num"
    oid = _index_oid(filled_table, index_name)
    fname = str(tmp_path / "update.txt")
    _write_num_file(fname, [("l0", 1000)])
    filled_table.update_from_file(fname, inplace=True, reindex=False)
    assert filled_table.lucky({"label": "l0"}, projection="num") == 1000
    # The index was maintained during the update, not dropped and rebuilt.
    assert _index_oid(filled_table, index_name) == oid


def test_update_from_file_inplace_reindex_true_drops_and_rebuilds(filled_table, tmp_path):
    filled_table.create_index(["num"])
    index_name = filled_table.search_table + "_num"
    oid = _index_oid(filled_table, index_name)
    fname = str(tmp_path / "update.txt")
    _write_num_file(fname, [("l0", 1000)])
    filled_table.update_from_file(fname, inplace=True, reindex=True)
    assert filled_table.lucky({"label": "l0"}, projection="num") == 1000
    assert index_name in _built_indexes(filled_table)
    assert _index_oid(filled_table, index_name) != oid


def test_update_from_file_inplace_default_reindex_uses_the_row_heuristic(filled_table, tmp_path):
    # The documented default: reindex=None decides from the number of updated
    # rows.  Far fewer than 1000 rows means the indexes are left in place.
    filled_table.create_index(["num"])
    index_name = filled_table.search_table + "_num"
    oid = _index_oid(filled_table, index_name)
    fname = str(tmp_path / "update.txt")
    _write_num_file(fname, [("l0", 1000)])
    filled_table.update_from_file(fname, inplace=True)
    assert filled_table.lucky({"label": "l0"}, projection="num") == 1000
    assert _index_oid(filled_table, index_name) == oid


def test_rewrite_passes_reindex_false_through_for_inplace(filled_table):
    filled_table.rewrite(_bump, inplace=True, reindex=False)
    assert filled_table.lucky({"n": 5}, projection="num") == 6
    assert filled_table.lucky({"n": 5}, projection="label") == "l5"
