# -*- coding: utf-8 -*-
"""
Tests for ``PostgresTable._check_tmp_leftovers`` (LMFDB/lmfdb#3708).

A crashed or interrupted reload can leave indexes, constraints or whole
tables whose names end in ``_tmp`` attached to the live tables; the next
reload used to collide with them partway through, after the expensive data
loading.  ``reload`` and the non-inplace ``update_from_file`` and ``rewrite``
now fail up front with a ValueError that names each leftover and gives
copy-pasteable SQL to rename or drop it; ``rewrite`` checks before running
its callback over the table rather than after.

The stray objects are created with raw SQL: the public ``create_index`` and
``create_constraint`` refuse names ending in ``_tmp``, which is exactly why
only a crashed reload can produce them.
"""
import pytest
from psycopg.sql import SQL, Identifier


def _num_rows(table):
    """
    The number of rows actually stored in the search table.

    ``count()`` with an empty query returns the cached ``meta_tables.total``,
    which reload's failure paths are not supposed to touch, so rows are
    counted directly.
    """
    return len(list(table.search({}, projection="id")))


def _snapshot(table, tmp_path, filename="reload.txt"):
    """
    Export ``table`` to a datafile suitable for ``reload``.
    """
    searchfile = str(tmp_path / filename)
    table.copy_to(searchfile)
    return searchfile


def test_check_passes_on_a_clean_table(filled_table):
    assert filled_table._check_tmp_leftovers() is None
    assert filled_table._check_tmp_leftovers([filled_table.search_table]) is None


def test_reload_blocks_on_a_stray_tmp_index(db, filled_table, tmp_path):
    name = filled_table.search_table
    stray = name + "_stray_tmp"
    searchfile = _snapshot(filled_table, tmp_path)
    db._execute(SQL("CREATE INDEX {0} ON {1} (n)").format(Identifier(stray), Identifier(name)))
    with pytest.raises(ValueError) as exc:
        filled_table.reload(searchfile)
    message = str(exc.value)
    assert stray in message
    assert "RENAME" in message
    # The exact rename command from the message is copy-pasteable SQL.
    assert 'ALTER INDEX "%s" RENAME TO "%s"' % (stray, name + "_stray") in message
    # The reload failed before doing any work: no data change, no backup.
    assert _num_rows(filled_table) == 200
    assert not filled_table._table_exists(name + "_old1")
    assert not filled_table._table_exists(name + "_tmp")
    # Following the instructions unblocks the reload.
    db._execute(SQL("ALTER INDEX {0} RENAME TO {1}").format(
        Identifier(stray), Identifier(name + "_stray")
    ))
    filled_table.reload(searchfile)
    assert _num_rows(db[name]) == 200
    assert db[name]._table_exists(name + "_old1")


def test_reload_blocks_on_a_stray_tmp_check_constraint(db, filled_table, tmp_path):
    name = filled_table.search_table
    stray = name + "_strayc_tmp"
    searchfile = _snapshot(filled_table, tmp_path)
    db._execute(SQL("ALTER TABLE {0} ADD CONSTRAINT {1} CHECK (n IS NOT NULL)").format(
        Identifier(name), Identifier(stray)
    ))
    with pytest.raises(ValueError) as exc:
        filled_table.reload(searchfile)
    message = str(exc.value)
    assert stray in message
    assert 'RENAME CONSTRAINT "%s" TO "%s"' % (stray, name + "_strayc") in message
    assert 'DROP CONSTRAINT "%s"' % (stray,) in message
    # Dropping the stray constraint unblocks the reload.
    db._execute(SQL("ALTER TABLE {0} DROP CONSTRAINT {1}").format(
        Identifier(name), Identifier(stray)
    ))
    filled_table.reload(searchfile)
    assert _num_rows(db[name]) == 200


def test_a_unique_tmp_constraint_is_reported_once_as_a_constraint(db, filled_table):
    # A UNIQUE constraint is backed by an index of the same name; it must be
    # reported as a constraint (dropping the index directly is refused by
    # postgres), and only once.
    name = filled_table.search_table
    stray = name + "_strayu_tmp"
    db._execute(SQL("ALTER TABLE {0} ADD CONSTRAINT {1} UNIQUE (n)").format(
        Identifier(name), Identifier(stray)
    ))
    with pytest.raises(ValueError) as exc:
        filled_table._check_tmp_leftovers()
    message = str(exc.value)
    assert 'RENAME CONSTRAINT "%s"' % (stray,) in message
    assert "DROP INDEX" not in message
    assert message.count("- constraint %s" % stray) == 1
    assert "- index %s" % stray not in message


def test_check_flags_a_tmp_pkey_leftover(db, filled_table):
    # An interrupted swap can leave the primary key of the _tmp table, named
    # <table>_tmp_pkey, on the live table; restore_pkeys would then fail.
    name = filled_table.search_table
    stray = name + "_tmp_pkey"
    db._execute(SQL("CREATE INDEX {0} ON {1} (n)").format(Identifier(stray), Identifier(name)))
    with pytest.raises(ValueError) as exc:
        filled_table._check_tmp_leftovers()
    message = str(exc.value)
    assert stray in message
    # The suggested rename strips _tmp, not _tmp_pkey.
    assert 'RENAME TO "%s"' % (name + "_pkey",) in message


def test_counts_table_leftovers_are_flagged_only_when_stats_are_saved(db, filled_table):
    # reload only touches the counts and stats tables when stats.saving is
    # on (as in the LMFDB), so leftovers there only block the reload then.
    counts = filled_table.stats.counts
    stray = counts + "_stray_tmp"
    db._execute(SQL("CREATE INDEX {0} ON {1} (cols)").format(Identifier(stray), Identifier(counts)))
    assert filled_table._check_tmp_leftovers() is None
    filled_table.stats.saving = True
    with pytest.raises(ValueError) as exc:
        filled_table._check_tmp_leftovers()
    message = str(exc.value)
    assert stray in message
    assert counts in message


def test_reload_blocks_on_a_leftover_tmp_search_table(db, filled_table, tmp_path):
    name = filled_table.search_table
    searchfile = _snapshot(filled_table, tmp_path)
    db._execute(SQL("CREATE TABLE {0} (id bigint)").format(Identifier(name + "_tmp")))
    with pytest.raises(ValueError) as exc:
        filled_table.reload(searchfile)
    message = str(exc.value)
    assert name + "_tmp" in message
    assert 'DROP TABLE "%s"' % (name + "_tmp",) in message
    assert "drop_tmp" in message
    db._execute(SQL("DROP TABLE {0}").format(Identifier(name + "_tmp")))
    filled_table.reload(searchfile)
    assert _num_rows(db[name]) == 200


def test_check_tolerates_a_reused_tmp_counts_table(db, filled_table):
    # When no countsfile is given, reload reuses an existing _tmp counts
    # table (refresh_stats clears and repopulates it), so its existence must
    # only be reported when a fresh clone is requested.
    counts = filled_table.stats.counts
    filled_table.stats.saving = True
    db._execute(SQL("CREATE TABLE {0} (cols jsonb)").format(Identifier(counts + "_tmp")))
    assert filled_table._check_tmp_leftovers([filled_table.search_table]) is None
    with pytest.raises(ValueError) as exc:
        filled_table._check_tmp_leftovers([filled_table.search_table, counts])
    assert counts + "_tmp" in str(exc.value)
    db._execute(SQL("DROP TABLE {0}").format(Identifier(counts + "_tmp")))


def test_rewrite_blocks_on_a_stray_tmp_index(db, filled_table):
    # rewrite goes through update_from_file, which clones the search table
    # and rebuilds _tmp indexes just like reload, so it gets the same check.
    # rewrite also runs the check itself, before dumping func(rec) for every
    # row to the data file: the failure must come before func is ever called,
    # not after an hours-long pass over a large table.
    name = filled_table.search_table
    stray = name + "_strayr_tmp"
    db._execute(SQL("CREATE INDEX {0} ON {1} (n)").format(Identifier(stray), Identifier(name)))
    calls = []

    def bump(record):
        calls.append(record["n"])
        record["num"] = record["n"] + 1
        return record

    with pytest.raises(ValueError) as exc:
        filled_table.rewrite(bump)
    message = str(exc.value)
    assert stray in message
    assert "RENAME" in message
    assert calls == []
    assert filled_table.lucky({"n": 5}, projection="num") == 57
    db._execute(SQL("DROP INDEX {0}").format(Identifier(stray)))
    filled_table.rewrite(bump)
    assert len(calls) == 200
    assert filled_table.lucky({"n": 5}, projection="num") == 6


def test_inplace_rewrite_ignores_a_stray_tmp_index(db, filled_table):
    # An inplace rewrite (inplace is passed through to update_from_file)
    # updates the search table directly, without cloning it or rebuilding
    # indexes under _tmp names, so leftovers cannot collide with it and the
    # check must not block it.
    name = filled_table.search_table
    stray = name + "_strayi_tmp"
    db._execute(SQL("CREATE INDEX {0} ON {1} (n)").format(Identifier(stray), Identifier(name)))
    calls = []

    def bump(record):
        calls.append(record["n"])
        record["num"] = record["n"] + 1
        return record

    filled_table.rewrite(bump, inplace=True)
    assert len(calls) == 200
    assert filled_table.lucky({"n": 5}, projection="num") == 6
