# -*- coding: utf-8 -*-
"""
Regression tests for the correctness fixes in the rc3 review round.

Most of these fail on the code as it stood at v1.0.0rc2; the rest pin down the
behavior next to a fix, so that a later change cannot quietly trade one of these
bugs for its neighbour.  They are grouped by the thing that was wrong rather
than by the method, since several of them are the same mistake made in two
places: a value formatted into SQL text instead of composed, and a result tested
for truth instead of for existence.
"""
import random

import pytest

from psycopg.sql import SQL, Identifier

import conftest


# ---------------------------------------------------------------------------
# identifiers in max_id / min_id
# ---------------------------------------------------------------------------

# Names that are legal PostgreSQL identifiers once quoted, and that a bare
# "SELECT MAX(id) FROM %s" would either mis-parse or execute as extra SQL.
AWKWARD_NAMES = [
    "plain_name_9",
    "has space",
    'has"quote',
    "semi;colon",
    "dash--dash",
    "slash/*star",
    "Ünïcødé",
]


@pytest.mark.parametrize("suffix", AWKWARD_NAMES)
def test_max_id_and_min_id_quote_the_table_they_are_given(db, empty_table, suffix):
    """
    max_id/min_id take a table name as an argument and used to format it into
    the statement as text.  A name needing quotes was a syntax error, and one
    containing a statement terminator was an injection.
    """
    scratch = "t_%s_%s" % (suffix, empty_table.search_table[-8:])
    db._execute(
        SQL("CREATE TABLE {0} (id bigint)").format(Identifier(scratch))
    )
    try:
        db._execute(
            SQL("INSERT INTO {0} (id) VALUES (3), (11)").format(Identifier(scratch))
        )
        assert empty_table.max_id(scratch) == 11
        assert empty_table.min_id(scratch) == 3
    finally:
        db._execute(SQL("DROP TABLE {0}").format(Identifier(scratch)))


def test_max_id_does_not_execute_an_injected_statement(db, empty_table):
    """
    The marker table must not exist afterwards: a name carrying its own
    statement has to fail to resolve as a relation, not run.

    The payload starts with a table that really exists and closes that statement
    cleanly, so on the old code the SELECT succeeds and the CREATE TABLE after
    it runs.  A payload naming a nonexistent relation would prove nothing: the
    first statement would fail and abort the transaction before the marker
    statement was reached, and the test would pass on vulnerable code.
    """
    marker = "marker_%s" % empty_table.search_table[-8:]
    injected = "%s; CREATE TABLE %s (x int); --" % (empty_table.search_table, marker)
    try:
        with pytest.raises(Exception):
            empty_table.max_id(injected)
        db.conn.rollback()
        assert not db._table_exists(marker)
    finally:
        # A no-op unless the injection ran, which is to say unless this test is
        # being run against vulnerable code -- where without it the marker table
        # would outlive the run.
        db.conn.rollback()
        db._execute(SQL("DROP TABLE IF EXISTS {0}").format(Identifier(marker)))


def test_max_id_reports_empty_as_minus_one(empty_table):
    assert empty_table.max_id() == -1


# ---------------------------------------------------------------------------
# approximate statistics use the owning table
# ---------------------------------------------------------------------------

def test_approx_most_common_scales_by_the_owning_table(db, table_factory):
    """
    Frequencies came from the right table but reltuples came from a hard-coded
    public.nf_fields, so on every other table the estimate was that table's
    frequencies scaled by an unrelated row count.

    Two tables with the same value distribution and very different row counts
    must therefore get very different estimates.
    """
    small = table_factory()
    big = table_factory()
    small.insert_many([conftest.sample_row(i) for i in range(50)])
    big.insert_many([conftest.sample_row(i) for i in range(1000)])
    for table in (small, big):
        db._execute(SQL("ANALYZE {0}").format(Identifier(table.search_table)))

    small_est = dict(small.stats._approx_most_common("flag", 2))
    big_est = dict(big.stats._approx_most_common("flag", 2))
    assert small_est and big_est

    # flag is boolean and never NULL, so the two values are the whole table and
    # their estimates have to add up to its size
    assert sum(small_est.values()) == pytest.approx(50, rel=0.25)
    assert sum(big_est.values()) == pytest.approx(1000, rel=0.25)
    assert sum(big_est.values()) > 5 * sum(small_est.values())


@pytest.mark.parametrize("coltype", ['text COLLATE "C"', 'text[] COLLATE "C"'])
def test_approx_most_common_handles_a_collated_column(db, table_factory, coltype):
    """
    The cast target was built by appending "[]" to the column declaration, which
    for a collated column produced 'text COLLATE "C"[]': COLLATE is a clause on
    the column, not part of the type, so the brackets cannot follow it.

    add_column stores the spelling it was given, so this is reachable without
    reconnecting -- the method has to work on the table object that added the
    column.  Collation "C" is the one every server has.
    """
    table = table_factory()
    table.add_column("coll", coltype)
    scalar = not coltype.startswith("text[]")
    rows = [
        dict(conftest.sample_row(i), coll=("v%d" % (i % 3)) if scalar else ["v%d" % (i % 3)])
        for i in range(200)
    ]
    table.insert_many(rows)
    db._execute(SQL("ANALYZE {0}").format(Identifier(table.search_table)))

    # three values over 200 rows: they are all common, so PostgreSQL keeps every
    # one of them in most_common_vals and they account for the whole table
    common = dict(table.stats._approx_most_common("coll", 3))
    assert len(common) == 3
    assert sum(common.values()) == pytest.approx(200, rel=0.25)
    if scalar:
        assert common.keys() == {"v0", "v1", "v2"}


# ---------------------------------------------------------------------------
# update_from_file does not share log state between calls
# ---------------------------------------------------------------------------

def _write_update(path, table, rows):
    """
    A minimal update file: the label column first, as update_from_file requires,
    then one column to change.
    """
    cols = ["label", "num"]
    with open(path, "w") as F:
        F.write("|".join(cols) + "\n")
        F.write("|".join(table.col_type[c] for c in cols) + "\n\n")
        for label, num in rows:
            F.write("%s|%s\n" % (label, num))


def test_update_from_file_does_not_carry_log_state_between_calls(filled_table, tmp_path):
    """
    The default was a shared dictionary literal that the method wrote logid and
    aborted into, so the second default call started out holding the first
    call's values -- and a caller who passed a dictionary got it modified.
    """
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    _write_update(first, filled_table, [("l0", 111)])
    _write_update(second, filled_table, [("l1", 222)])

    filled_table.update_from_file(str(first), inplace=True, restat=False)
    filled_table.update_from_file(str(second), inplace=True, restat=False)

    # Both updates landed, and the second call logged its own operation.
    assert filled_table.lucky({"label": "l0"}, "num") == 111
    assert filled_table.lucky({"label": "l1"}, "num") == 222

    # The default really is rebuilt per call.
    import inspect

    from psycodict.table import PostgresTable

    default = inspect.signature(PostgresTable.update_from_file).parameters["logging"].default
    assert default is None


def test_update_from_file_leaves_a_supplied_dictionary_alone(filled_table, tmp_path):
    datafile = tmp_path / "u.txt"
    _write_update(datafile, filled_table, [("l0", 333)])

    supplied = {"operation": "caller_owned"}
    filled_table.update_from_file(
        str(datafile), inplace=True, restat=False, logging=supplied
    )
    assert supplied == {"operation": "caller_owned"}


def test_update_from_file_leaves_a_supplied_dictionary_alone_on_failure(
    filled_table, tmp_path
):
    bad = tmp_path / "bad.txt"
    bad.write_text("label|nosuchcolumn\ntext|text\n\nl0|x\n")
    supplied = {"operation": "caller_owned"}
    with pytest.raises(Exception):
        filled_table.update_from_file(
            str(bad), inplace=True, restat=False, logging=supplied
        )
    filled_table._db.conn.rollback()
    assert supplied == {"operation": "caller_owned"}


# ---------------------------------------------------------------------------
# random() edge cases
# ---------------------------------------------------------------------------

def test_random_with_pick_first_returns_none_when_nothing_matches(filled_table):
    """
    distinct() over a query nothing satisfies is empty, and random.choice([])
    raised IndexError where the documented behavior is None.
    """
    assert filled_table.random({"n": -1}, pick_first="label") is None


def test_random_finds_the_only_row_when_its_id_is_zero(db, table_factory):
    """
    -1 is the empty sentinel from max_id, so a table whose single row has id 0
    is not empty.  Testing `maxid < 1` reported it as such.
    """
    table = table_factory()
    table.insert_many([conftest.sample_row(1)])
    db._execute(
        SQL("UPDATE {0} SET id = 0").format(Identifier(table.search_table))
    )
    assert table.max_id() == 0
    assert table.random() == "l1"


def test_random_returns_a_false_valued_projection(db, table_factory):
    """
    `if res:` discarded a row whose projected value was 0, False or "", so a
    table of them exhausted maxtries and raised "Random selection failed!".
    """
    table = table_factory()
    table.insert_many([dict(conftest.sample_row(i), num=0) for i in range(20)])
    for _ in range(10):
        assert table.random({}, "num") == 0


def test_random_returns_none_for_a_null_scalar_projection(db, table_factory):
    """
    For a projection naming one column, lucky returns None both when no row has
    the sampled id and when the row is there with NULL in that column.  Treating
    the second as a missing id exhausted maxtries and raised "Random selection
    failed!" on a table whose ids are consecutive.
    """
    table = table_factory()
    table.insert_many([dict(conftest.sample_row(i), num=None) for i in range(20)])
    # the no-query branch: consecutive ids, so every id it samples is a real row
    assert (table.min_id(), table.max_id()) == (0, 19)
    for _ in range(10):
        assert table.random({}, "num") is None


def test_random_retries_a_missing_id_before_giving_up(db, table_factory):
    """
    The other half of the same branch: an id that really is absent must send the
    loop round again rather than returning None for it.
    """
    table = table_factory()
    table.insert_many([conftest.sample_row(i) for i in range(20)])
    # everything between the extremes is gone, so most draws miss
    db._execute(
        SQL("DELETE FROM {0} WHERE id > 0 AND id < 19").format(
            Identifier(table.search_table)
        )
    )
    assert (table.min_id(), table.max_id()) == (0, 19)
    for _ in range(10):
        assert table.random({}, "num") in (7, 197)


def test_random_returns_none_for_an_empty_table(empty_table):
    assert empty_table.random() is None


# ---------------------------------------------------------------------------
# random_sample() mode handling and RNG isolation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["nonsense", "SYSTEMATIC", "", "choise"])
def test_random_sample_rejects_an_unknown_mode(filled_table, mode):
    """
    An unrecognized mode matched no branch and the method returned None, which
    is indistinguishable from an empty result.
    """
    with pytest.raises(ValueError, match="mode"):
        filled_table.random_sample(0.5, mode=mode)


def test_random_sample_rejects_a_non_string_mode(filled_table):
    with pytest.raises(ValueError, match="mode"):
        filled_table.random_sample(0.5, mode=17)


@pytest.mark.parametrize("mode", ["system", "bernoulli", "choice", "CHOICE"])
def test_random_sample_accepts_every_documented_mode(filled_table, mode):
    result = filled_table.random_sample(0.5, mode=mode)
    assert list(result) is not None


def test_repeatable_choice_sampling_leaves_the_global_rng_alone(filled_table):
    """
    random.seed(repeatable) reseeded the process-wide generator, so asking for
    a reproducible sample made every later random number in the program repeat.
    """
    random.seed(12345)
    baseline = [random.random() for _ in range(5)]

    random.seed(12345)
    filled_table.random_sample(0.5, mode="choice", repeatable=99)
    after = [random.random() for _ in range(5)]

    assert baseline == after


def test_repeatable_choice_sampling_is_still_repeatable(filled_table):
    first = filled_table.random_sample(0.5, mode="choice", repeatable=7)
    second = filled_table.random_sample(0.5, mode="choice", repeatable=7)
    assert first == second
