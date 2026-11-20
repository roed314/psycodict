# -*- coding: utf-8 -*-
"""
Tests for the write side of the psycodict API.

Covers ``insert_many``, ``update``, ``upsert``, ``delete``, the file based
``copy_to``/``copy_from``/``reload`` family, ``rewrite``, the schema helpers
``add_column``/``drop_column``, and the ``DelayCommit`` transaction context.

Row counts are always established with ``search`` or with a non-empty query.
``count()`` with an empty query returns the cached ``meta_tables.total``, which
no write path updates unless ``stats.saving`` is on; see
``test_count_with_empty_query_matches_number_of_rows`` for that bug.
"""
import pytest
from psycopg import IntegrityError
from psycopg.sql import SQL, Identifier

from conftest import sample_row
from psycodict.utils import DelayCommit


def _num_rows(table):
    """
    The number of rows actually stored in the search table.
    """
    return len(list(table.search({}, projection="id")))


def _all_rows(table):
    """
    Every row of the table including its id, ordered by id.
    """
    return sorted(table.search({}, projection=3), key=lambda rec: rec["id"])


def _read(filename):
    with open(filename) as F:
        return F.read()


##################################################################
# insert_many                                                    #
##################################################################


def test_insert_many_inserts_a_single_row(empty_table):
    empty_table.insert_many([sample_row(0)])
    assert _num_rows(empty_table) == 1
    assert empty_table.lucky({"n": 0}, projection="label") == "l0"


def test_insert_many_assigns_ids_continuing_from_max_id(empty_table):
    empty_table.insert_many([sample_row(i) for i in range(3)])
    empty_table.insert_many([sample_row(i) for i in range(3, 6)])
    assert [rec["id"] for rec in _all_rows(empty_table)] == [0, 1, 2, 3, 4, 5]
    assert empty_table.max_id() == 5
    assert empty_table.min_id() == 0


def test_insert_many_roundtrips_jsonb(empty_table):
    data = {"a": [1, 2], "s": "text", "nested": {"k": [True, None]}}
    empty_table.insert_many([{"n": 1, "label": "j", "data": data}])
    stored = empty_table.lucky({"n": 1}, projection="data")
    assert stored == data
    assert isinstance(stored, dict)


def test_insert_many_roundtrips_numeric(empty_table):
    # psycodict installs its own numeric converter: an integral numeric comes
    # back as an int (or a Sage Integer), not as a Decimal.
    empty_table.insert_many([
        {"n": 1, "label": "a", "num": 12345678901234567890},
        {"n": 2, "label": "b", "num": 1.25},
    ])
    big = empty_table.lucky({"n": 1}, projection="num")
    assert big == 12345678901234567890
    assert not isinstance(big, float)
    assert empty_table.lucky({"n": 2}, projection="num") == 1.25


def test_insert_many_roundtrips_arrays(empty_table):
    empty_table.insert_many([
        {"n": 1, "label": "a", "vec": [3, -1, 0], "mat": [2, 4, 8]},
        {"n": 2, "label": "b", "vec": [], "mat": [1.5, 2.5]},
    ])
    first = empty_table.lucky({"n": 1}, projection=["vec", "mat"])
    assert first == {"vec": [3, -1, 0], "mat": [2, 4, 8]}
    second = empty_table.lucky({"n": 2}, projection=["vec", "mat"])
    assert second == {"vec": [], "mat": [1.5, 2.5]}


def test_insert_many_roundtrips_double_precision_and_boolean(empty_table):
    empty_table.insert_many([{"n": 1, "label": "a", "x": -0.125, "flag": False}])
    record = empty_table.lucky({"n": 1}, projection=["x", "flag"])
    assert record["x"] == -0.125
    assert isinstance(record["x"], float)
    assert record["flag"] is False


def test_insert_many_omitted_columns_become_null(empty_table):
    empty_table.insert_many([{"n": 1, "label": "sparse"}])
    assert empty_table.lucky({"n": 1}, projection=1) == {
        "n": 1, "label": "sparse", "data": None, "num": None,
        "vec": None, "mat": None, "x": None, "flag": None,
    }
    assert empty_table.count({"data": None}) == 1
    assert empty_table.count({"vec": None}) == 1


def test_insert_many_explicit_none_becomes_null(empty_table):
    empty_table.insert_many([
        {"n": 1, "label": "a", "num": None, "vec": None, "x": None, "flag": None}
    ])
    assert empty_table.lucky({"n": 1}, projection=1) == {
        "n": 1, "label": "a", "data": None, "num": None,
        "vec": None, "mat": None, "x": None, "flag": None,
    }
    assert empty_table.count({"num": None}) == 1
    assert empty_table.count({"vec": None}) == 1
    assert empty_table.count({"flag": None}) == 1


def test_insert_many_none_in_jsonb_column_is_sql_null(empty_table):
    empty_table.insert_many([{"n": 1, "label": "a", "data": None}])
    assert empty_table.count({"data": None}) == 1


def test_insert_many_rejects_bad_input(empty_table):
    with pytest.raises(ValueError):
        empty_table.insert_many([])
    with pytest.raises(ValueError):
        empty_table.insert_many([{"n": 1, "label": "a"}, {"n": 2}])


def test_insert_many_bulk_restores_keys_indexes_and_data(empty_table):
    # More than 1000 rows turns reindex on, which drops the primary key and the
    # indexes before the insert and rebuilds them afterward.
    empty_table.create_index(["label"])
    rows = [sample_row(i) for i in range(1200)]
    expected = [dict(row) for row in rows]
    empty_table.insert_many(rows)
    assert empty_table.count({"n": {"$gte": 0}}) == 1200
    built = set(empty_table._list_built_indexes())
    assert empty_table.search_table + "_pkey" in built
    assert empty_table.search_table + "_label" in built
    got = sorted(empty_table.search({}, projection=1), key=lambda rec: rec["n"])
    assert got == expected


def test_insert_many_flags_do_not_change_the_result(empty_table):
    empty_table.insert_many([sample_row(0)], reindex=True, resort=True, restat=False)
    empty_table.insert_many([sample_row(1)], reindex=False, resort=False, restat=True)
    assert [rec["n"] for rec in _all_rows(empty_table)] == [0, 1]
    assert empty_table.search_table + "_pkey" in set(empty_table._list_built_indexes())


def test_insert_many_updates_ids_but_not_values(empty_table):
    # The documented contract, both halves: "the dictionaries will be updated
    # with the ids of the inserted records" -- and with nothing else.  The
    # jsonb values in particular must not come back wrapped in Json.
    rows = [sample_row(0), sample_row(1)]
    original = dict(rows[0]["data"])
    empty_table.insert_many(rows)
    assert rows[0]["data"] == original
    assert rows[1]["id"] == rows[0]["id"] + 1
    assert empty_table.lucky({"id": rows[1]["id"]}, "n") == 1


##################################################################
# update                                                         #
##################################################################


def test_update_sets_a_single_column(filled_table):
    filled_table.update({"n": 5}, {"label": "renamed"})
    assert filled_table.lucky({"n": 5}, projection="label") == "renamed"
    assert filled_table.lucky({"n": 6}, projection="label") == "l6"


def test_update_sets_several_columns_at_once(filled_table):
    filled_table.update({"n": 5}, {"label": "renamed", "x": 2.5, "flag": True})
    record = filled_table.lucky({"n": 5}, projection=["label", "x", "flag"])
    assert record == {"label": "renamed", "x": 2.5, "flag": True}


def test_update_applies_to_every_matching_row(filled_table):
    filled_table.update({"n": {"$lt": 10}}, {"label": "batch"})
    assert filled_table.count({"label": "batch"}) == 10


def test_update_with_an_empty_query_updates_all_rows(filled_table):
    filled_table.update({}, {"flag": True})
    assert filled_table.count({"flag": True}) == 200
    assert filled_table.count({"flag": False}) == 0


def test_update_jsonb_and_array_columns(filled_table):
    filled_table.update({"n": 3}, {"data": {"replaced": [1, 2]}, "vec": [9, 8], "mat": [5, 6]})
    record = filled_table.lucky({"n": 3}, projection=["data", "vec", "mat"])
    assert record == {"data": {"replaced": [1, 2]}, "vec": [9, 8], "mat": [5, 6]}


def test_update_to_none_sets_null(filled_table):
    filled_table.update({"n": 4}, {"num": None})
    assert filled_table.lucky({"n": 4}, projection=1).get("num") is None
    assert filled_table.count({"num": None}) == 1


def test_update_matching_no_rows_changes_nothing(filled_table):
    before = _all_rows(filled_table)
    filled_table.update({"n": -1}, {"label": "never"})
    assert _all_rows(filled_table) == before


def test_update_flags_do_not_change_the_result(filled_table):
    filled_table.update({"n": 7}, {"label": "quiet"}, restat=False)
    filled_table.update({"n": 8}, {"label": "sorted"}, resort=True)
    assert filled_table.lucky({"n": 7}, projection="label") == "quiet"
    assert filled_table.lucky({"n": 8}, projection="label") == "sorted"


def test_update_marks_the_table_out_of_order_and_stats_invalid(filled_table):
    filled_table.update({"n": 7}, {"label": "dirty"})
    assert filled_table._out_of_order is True
    assert filled_table._stats_valid is False


##################################################################
# upsert                                                         #
##################################################################


def test_upsert_inserts_a_new_row(empty_table):
    new_row, row_id = empty_table.upsert({"label": "fresh"}, {"n": 42})
    assert new_row is True
    assert row_id == 0
    # The columns named in the query are stored on the new row as well.
    assert empty_table.lucky({"n": 42}, projection="label") == "fresh"


def test_upsert_updates_an_existing_row(filled_table):
    new_row, row_id = filled_table.upsert({"label": "l5"}, {"n": 999})
    assert new_row is False
    assert row_id == 5
    assert filled_table.lucky({"label": "l5"}, projection="n") == 999
    assert _num_rows(filled_table) == 200


def test_upsert_updates_several_columns_including_jsonb(filled_table):
    filled_table.upsert({"label": "l5"}, {"x": 3.5, "flag": True, "data": {"up": 1}})
    record = filled_table.lucky({"label": "l5"}, projection=["x", "flag", "data"])
    assert record == {"x": 3.5, "flag": True, "data": {"up": 1}}


def test_upsert_appends_after_the_largest_id(filled_table):
    new_row, row_id = filled_table.upsert({"label": "extra"}, {"n": 1000})
    assert new_row is True
    assert row_id == 200
    assert _num_rows(filled_table) == 201


def test_upsert_validates_its_arguments(filled_table):
    with pytest.raises(ValueError):  # empty query
        filled_table.upsert({}, {"n": 1})
    with pytest.raises(ValueError):  # empty data
        filled_table.upsert({"label": "l5"}, {})
    with pytest.raises(ValueError):  # the id is not settable
        filled_table.upsert({"label": "l5"}, {"id": 17})
    with pytest.raises(ValueError):  # an id query cannot insert
        filled_table.upsert({"id": 100000}, {"n": 1})
    with pytest.raises(ValueError):  # unknown column in the query
        filled_table.upsert({"nosuchcol": 1}, {"n": 1})
    with pytest.raises(ValueError):  # unknown column in the data
        filled_table.upsert({"label": "l5"}, {"nosuchcol": 1})


def test_upsert_rejects_a_query_matching_several_rows(filled_table):
    with pytest.raises(ValueError):
        filled_table.upsert({"flag": True}, {"x": 0.0})


##################################################################
# delete                                                         #
##################################################################


def test_delete_removes_matching_rows(filled_table):
    assert filled_table.delete({"n": {"$lt": 50}}) is None
    assert _num_rows(filled_table) == 150
    assert filled_table.count({"n": {"$lt": 50}}) == 0


def test_delete_with_an_empty_query_removes_everything(filled_table):
    filled_table.delete({})
    assert _num_rows(filled_table) == 0
    assert list(filled_table.search({})) == []


def test_delete_matching_no_rows_changes_nothing(filled_table):
    before = _all_rows(filled_table)
    filled_table.delete({"n": -1}, restat=False)
    assert _all_rows(filled_table) == before


##################################################################
# copy_to / copy_from                                            #
##################################################################


def test_copy_to_writes_name_and_type_header_lines(filled_table, tmp_path):
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile)
    lines = _read(searchfile).split("\n")
    assert lines[0].split("|")[0] == "id"
    assert set(lines[0].split("|")) == {"id"} | set(filled_table.search_cols)
    assert lines[1].split("|")[0] == "bigint"
    assert lines[2] == ""


def test_copy_to_copy_from_roundtrip(filled_table, table_factory, tmp_path):
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile)
    target = table_factory()
    target.copy_from(searchfile)
    assert _all_rows(target) == _all_rows(filled_table)


def test_copy_to_copy_from_roundtrips_nulls(empty_table, table_factory, tmp_path):
    empty_table.insert_many([
        {"n": 1, "label": "a", "num": None, "vec": None, "mat": None, "x": None, "flag": None},
        {"n": 2, "label": "b", "num": 3, "vec": [1], "mat": [2], "x": 0.5, "flag": True},
    ])
    searchfile = str(tmp_path / "search.txt")
    empty_table.copy_to(searchfile)
    target = table_factory()
    target.copy_from(searchfile)
    assert _all_rows(target) == _all_rows(empty_table)
    assert target.count({"num": None}) == 1
    assert target.count({"vec": None}) == 1


def test_copy_to_copy_from_respect_a_custom_null_marker(table_factory, tmp_path):
    # psycopg2's copy_to/copy_from accepted null=..., so the COPY statements
    # that replaced them must keep supporting it
    source = table_factory(columns=[("n", "integer"), ("label", "text")])
    source.insert_many([{"n": 0, "label": None}, {"n": 1, "label": "NULLISH"}])
    searchfile = str(tmp_path / "search.txt")
    source.copy_to(searchfile, null="NULL")
    lines = _read(searchfile).split("\n")
    header = lines[0].split("|")
    data = {}
    for line in lines[3:]:
        if line:
            rec = dict(zip(header, line.split("|")))
            data[rec["n"]] = rec["label"]
    # the null column is written as the custom marker; a value merely
    # starting with it is left alone
    assert data == {"0": "NULL", "1": "NULLISH"}
    target = table_factory(columns=[("n", "integer"), ("label", "text")])
    target.copy_from(searchfile, null="NULL")
    assert target.lucky({"n": 0}, "label") is None
    assert target.lucky({"n": 1}, "label") == "NULLISH"


def test_copy_to_copy_from_roundtrips_jsonb_and_arrays(empty_table, table_factory, tmp_path):
    empty_table.insert_many([{
        "n": 1,
        "label": "tricky",
        "data": {"s": "a|b", "nested": {"k": [1, 2]}, "t": True},
        "vec": [1, -2, 3],
        "mat": [1.5, 2.5],
    }])
    searchfile = str(tmp_path / "search.txt")
    empty_table.copy_to(searchfile)
    target = table_factory()
    target.copy_from(searchfile)
    record = target.lucky({"n": 1}, projection=["data", "vec", "mat"])
    assert record["data"] == {"s": "a|b", "nested": {"k": [1, 2]}, "t": True}
    assert record["vec"] == [1, -2, 3]
    assert record["mat"] == [1.5, 2.5]


def test_copy_to_with_a_query_exports_only_matching_rows(filled_table, table_factory, tmp_path):
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile, query={"n": {"$lt": 5}})
    body = [line for line in _read(searchfile).split("\n")[3:] if line]
    assert len(body) == 5
    target = table_factory()
    target.copy_from(searchfile)
    assert sorted(target.search({}, projection="n")) == [0, 1, 2, 3, 4]


def test_copy_to_with_columns_exports_only_those_columns(filled_table, tmp_path):
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile, columns=["n", "label"])
    lines = _read(searchfile).split("\n")
    assert lines[0] == "id|label|n"
    assert lines[3] == "0|l0|0"


def test_copy_to_rejects_an_unknown_column(filled_table, tmp_path):
    with pytest.raises(ValueError):
        filled_table.copy_to(str(tmp_path / "search.txt"), columns=["nosuchcol"])


def test_copy_from_a_file_without_ids_assigns_them(filled_table, table_factory, tmp_path):
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile, include_id=False)
    assert "id" not in _read(searchfile).split("\n")[0].split("|")
    target = table_factory()
    target.copy_from(searchfile)
    assert [rec["id"] for rec in _all_rows(target)] == list(range(200))


def test_copy_from_appends_to_an_existing_table_and_reindexes(empty_table, table_factory, tmp_path):
    empty_table.insert_many([sample_row(i) for i in range(5)])
    searchfile = str(tmp_path / "search.txt")
    empty_table.copy_to(searchfile, include_id=False)
    target = table_factory()
    target.create_index(["label"])
    target.insert_many([sample_row(i) for i in range(100, 103)])
    target.copy_from(searchfile, reindex=True)
    assert _num_rows(target) == 8
    assert target.search_table + "_label" in set(target._list_built_indexes())


def test_copy_from_rejects_colliding_ids(filled_table, tmp_path):
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile)
    with pytest.raises(IntegrityError):
        filled_table.copy_from(searchfile)
    assert _num_rows(filled_table) == 200


##################################################################
# reload and its undo operations                                 #
##################################################################


def _reload_from_snapshot(db, table, tmp_path, filename="reload.txt"):
    """
    Export ``table``, drop half of its rows, then reload the export.

    Returns the (re-created) table object and the path of the export.
    """
    searchfile = str(tmp_path / filename)
    table.copy_to(searchfile)
    table.delete({"n": {"$gte": 100}})
    table.reload(searchfile)
    return db[table.search_table], searchfile


def test_reload_replaces_the_contents_and_creates_a_backup(db, filled_table, tmp_path):
    name = filled_table.search_table
    table, _ = _reload_from_snapshot(db, filled_table, tmp_path)
    assert _num_rows(table) == 200
    assert table._table_exists(name + "_old1")
    assert not table._table_exists(name + "_tmp")
    # reload_final_swap replaces the cached table object on the database.
    assert table is not filled_table
    assert db[name] is table


def test_reload_revert_restores_the_previous_contents(db, filled_table, tmp_path):
    table, _ = _reload_from_snapshot(db, filled_table, tmp_path)
    table.reload_revert()
    assert _num_rows(db[table.search_table]) == 100
    # Reverting a second time undoes the first revert.
    table.reload_revert()
    assert _num_rows(db[table.search_table]) == 200


def test_reload_revert_without_a_backup_raises(filled_table):
    with pytest.raises(ValueError):
        filled_table.reload_revert()


def test_cleanup_from_reload_drops_the_backup(db, filled_table, tmp_path):
    name = filled_table.search_table
    table, _ = _reload_from_snapshot(db, filled_table, tmp_path)
    table.cleanup_from_reload()
    assert not table._table_exists(name + "_old1")
    assert _num_rows(db[name]) == 200


def test_cleanup_from_reload_keeps_and_renumbers_backups(db, filled_table, tmp_path):
    name = filled_table.search_table
    table, searchfile = _reload_from_snapshot(db, filled_table, tmp_path)
    table.reload(searchfile)
    table = db[name]
    assert table._table_exists(name + "_old2")
    table.cleanup_from_reload(keep_old=1)
    assert table._table_exists(name + "_old1")
    assert not table._table_exists(name + "_old2")


def test_reload_without_final_swap_leaves_the_tmp_table(filled_table, tmp_path):
    name = filled_table.search_table
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile)
    filled_table.delete({"n": {"$gte": 100}})
    filled_table.reload(searchfile, final_swap=False)
    assert filled_table._table_exists(name + "_tmp")
    assert _num_rows(filled_table) == 100
    filled_table.drop_tmp()
    assert not filled_table._table_exists(name + "_tmp")


def test_reload_with_a_metafile(db, filled_table, tmp_path):
    name = filled_table.search_table
    searchfile = str(tmp_path / "search.txt")
    metafile = str(tmp_path / "meta.txt")
    filled_table.copy_to(searchfile, metafile=metafile)
    assert _read(metafile).split("|")[0] == name
    filled_table.reload(searchfile, metafile=metafile)
    assert _num_rows(db[name]) == 200


def test_reload_with_a_metafile_and_resort(db, filled_table, tmp_path):
    # Regression: the metafile branch of the resort logic indexed the csv row
    # and the jsonb position tuple with column *names*, so any
    # reload(metafile=..., resort=True) raised TypeError.
    name = filled_table.search_table
    searchfile = str(tmp_path / "search.txt")
    metafile = str(tmp_path / "meta.txt")
    filled_table.copy_to(searchfile, metafile=metafile)
    filled_table.reload(searchfile, metafile=metafile, resort=True)
    assert _num_rows(db[name]) == 200


def test_reload_of_an_idless_file_with_a_metafile(db, filled_table, tmp_path):
    # Same regression as above through the default path: a searchfile without
    # ids turns resort on automatically.
    name = filled_table.search_table
    searchfile = str(tmp_path / "search.txt")
    metafile = str(tmp_path / "meta.txt")
    filled_table.copy_to(searchfile, include_id=False, metafile=metafile)
    filled_table.reload(searchfile, metafile=metafile)
    table = db[name]
    assert _num_rows(table) == 200
    assert table.lucky({"n": 5}, projection="label") == "l5"


def test_reload_resort_rejects_a_malformed_metafile(filled_table, tmp_path):
    from psycodict.base import _meta_tables_cols

    searchfile = str(tmp_path / "search.txt")
    metafile = str(tmp_path / "meta.txt")
    filled_table.copy_to(searchfile, metafile=metafile)
    fields = _read(metafile).rstrip("\n").split("|")
    fields[_meta_tables_cols.index("id_ordered")] = "x"
    with open(metafile, "w") as F:
        F.write("|".join(fields) + "\n")
    with pytest.raises(RuntimeError, match="different from 't' or 'f'"):
        filled_table.reload(searchfile, metafile=metafile, resort=True)


def test_reload_revert_recounts_the_total(db, filled_table, tmp_path):
    # Regression: reload_revert swapped the tables back without recounting,
    # so count() kept answering with the swapped-away table's total.
    name = filled_table.search_table
    table, _ = _reload_from_snapshot(db, filled_table, tmp_path)
    assert table.count() == 200
    table.reload_revert()
    assert table.count() == 100
    assert db[name].count() == 100
    total = db._execute(SQL("SELECT total FROM meta_tables WHERE name = %s"), [name]).fetchone()[0]
    assert total == 100


def test_reload_revert_restores_the_column_list(db, filled_table, tmp_path):
    name = filled_table.search_table
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile, columns=[c for c in filled_table.search_cols if c != "x"])
    filled_table.reload(searchfile, adjust_schema=True)
    assert "x" not in db[name].search_cols
    db[name].reload_revert()
    assert "x" in db[name].search_cols
    assert db[name].lucky({"n": 4}, projection="x") == 2.0


##################################################################
# reload(adjust_schema=True) with statistics saved               #
##################################################################


def _saving(table):
    """
    Turn on ``stats.saving`` for ``table``, as LMFDB's stats class does.

    Only with saving on does reload stage counts and stats tables and refresh
    statistics on the ``_tmp`` table, which is where these tests look.
    """
    table.stats.saving = True
    return table


def _counted(table, suffix=""):
    """
    The column lists in ``table``'s counts table, as tuples, split into the
    null counts (``values = [None]``) and the rest.
    """
    cur = table._execute(
        SQL("SELECT cols, values FROM {0}").format(Identifier(table.stats.counts + suffix))
    )
    nulls, others = set(), set()
    for cols, values in cur:
        (nulls if values == [None] else others).add(tuple(cols))
    return nulls, others


def _stat_cols(table):
    """
    Every column named by a row of ``table``'s stats table.
    """
    cur = table._execute(
        SQL("SELECT cols, constraint_cols FROM {0}").format(Identifier(table.stats.stats))
    )
    return {col for cols, ccols in cur for col in (cols or []) + (ccols or [])}


def test_reload_adjust_schema_can_drop_a_column_while_saving_stats(db, filled_table, tmp_path):
    # Regression: refresh_stats(suffix="_tmp") counted the nulls of every live
    # column in the _tmp table, so this reload aborted with UndefinedColumn.
    name = filled_table.search_table
    _saving(filled_table).stats.add_stats(["flag"])
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile, columns=[c for c in filled_table.search_cols if c != "x"])
    filled_table.reload(searchfile, adjust_schema=True)
    table = db[name]
    assert "x" not in table.search_cols
    assert table.count() == 200
    assert _counted(table)[0] == {(col,) for col in table.search_cols}
    assert table.stats.quick_count({"flag": True}) == 67


def test_reload_adjust_schema_skips_stats_on_dropped_columns(db, filled_table, tmp_path):
    # Each kind of recorded statistic names a column that the reload drops.
    # The countsfile carries the extra count into the _tmp counts table,
    # which is where refresh_stats looks for extra counts.
    name = filled_table.search_table
    stats = _saving(filled_table).stats
    stats.add_stats(["label"], constraint={"flag": True})
    stats.add_stats(["vec"], split_list=True)
    stats.add_numstats("n", ["flag"])
    stats.add_stats(["num"])
    assert stats.count({"flag": True, "n": {"$lt": 50}}, record=True) == 17
    searchfile = str(tmp_path / "search.txt")
    countsfile = str(tmp_path / "counts.txt")
    kept = [c for c in filled_table.search_cols if c not in ("flag", "vec")]
    filled_table.copy_to(searchfile, countsfile=countsfile, columns=kept)
    filled_table.reload(searchfile, countsfile=countsfile, adjust_schema=True)
    table = db[name]
    assert sorted(table.search_cols) == sorted(kept)
    nulls, others = _counted(table)
    assert nulls == {(col,) for col in kept}
    assert others == {(), ("num",)}
    assert _stat_cols(table) == {"num"}


def test_reload_adjust_schema_counts_nulls_in_an_added_column(db, filled_table, table_factory, tmp_path):
    from conftest import COLUMNS

    name = filled_table.search_table
    source = table_factory(columns=COLUMNS + [("note", "text")])
    source.insert_many([dict(sample_row(i), note="even" if i % 2 == 0 else None) for i in range(200)])
    searchfile = str(tmp_path / "search.txt")
    source.copy_to(searchfile)
    _saving(filled_table).reload(searchfile, adjust_schema=True)
    table = db[name]
    assert "note" in table.search_cols
    assert _counted(table)[0] == {(col,) for col in table.search_cols}
    assert table.stats.quick_count({"note": None}) == 100


def test_null_counts_of_a_staged_reload_use_its_columns(filled_table, tmp_path):
    searchfile = str(tmp_path / "search.txt")
    filled_table.copy_to(searchfile, columns=[c for c in filled_table.search_cols if c != "x"])
    _saving(filled_table).reload(searchfile, adjust_schema=True, final_swap=False)
    try:
        assert filled_table.stats.null_counts(suffix="_tmp") == {}
    finally:
        filled_table.drop_tmp()


##################################################################
# resort, rewrite and the schema helpers                         #
##################################################################


def test_resort_is_a_disabled_noop(filled_table):
    # resort() is deliberately short circuited in table.py: resorting without a
    # reload makes replication stall.
    assert filled_table.resort() is None
    assert [rec["n"] for rec in _all_rows(filled_table)][:5] == [0, 1, 2, 3, 4]


def test_rewrite_applies_the_function_to_every_row(filled_table):
    def bump(record):
        record["num"] = record["n"] + 1
        return record

    filled_table.rewrite(bump)
    assert filled_table.lucky({"n": 5}, projection="num") == 6


def test_rewrite_preserves_labels_and_row_count(filled_table):
    def bump(record):
        record["num"] = record["n"] + 1
        return record

    filled_table.rewrite(bump)
    assert filled_table.lucky({"n": 5}, projection="num") == 6
    assert filled_table.lucky({"n": 5}, projection="label") == "l5"
    assert _num_rows(filled_table) == 200


def test_rewrite_can_restrict_to_a_query(filled_table):
    def relabel(record):
        record["label"] = "picked"
        return record

    filled_table.rewrite(relabel, query={"n": {"$lt": 3}})
    assert filled_table.count({"label": "picked"}) == 3
    assert filled_table.lucky({"n": 10}, projection="label") == "l10"


def test_add_column_makes_a_new_column_usable(filled_table):
    filled_table.add_column("extra_note", "text")
    assert "extra_note" in filled_table.search_cols
    assert filled_table.col_type["extra_note"] == "text"
    filled_table.update({"n": 1}, {"extra_note": "hello"})
    assert filled_table.lucky({"n": 1}, projection="extra_note") == "hello"
    assert filled_table.count({"extra_note": None}) == 199


def test_add_column_validates_its_arguments(filled_table):
    with pytest.raises(ValueError):
        filled_table.add_column("label", "text")
    with pytest.raises(RuntimeError):
        filled_table.add_column("bad", "not_a_postgres_type")


def test_drop_column_removes_the_column_and_keeps_the_rest(filled_table):
    filled_table.add_column("extra_note", "text")
    filled_table.update({"n": 1}, {"extra_note": "hello"})
    filled_table.drop_column("extra_note", force=True)
    assert "extra_note" not in filled_table.search_cols
    assert "extra_note" not in filled_table.col_type
    with pytest.raises(ValueError):
        filled_table.lucky({"n": 1}, projection="extra_note")
    assert _num_rows(filled_table) == 200
    assert filled_table.lucky({"n": 5}, projection="label") == "l5"


def test_drop_column_validates_its_argument(filled_table):
    with pytest.raises(ValueError):
        filled_table.drop_column("n", force=True)
    assert "n" in filled_table.search_cols
    with pytest.raises(ValueError):
        filled_table.drop_column("nosuchcol", force=True)


def test_finalize_changes_is_a_noop(filled_table):
    assert filled_table.finalize_changes() is None
    assert _num_rows(filled_table) == 200


##################################################################
# transactions                                                   #
##################################################################


def test_writes_outside_delaycommit_are_committed(db, empty_table):
    empty_table.insert_many([sample_row(0)])
    db.conn.rollback()
    assert _num_rows(empty_table) == 1


def test_delaycommit_defers_the_commit(db, empty_table):
    with DelayCommit(empty_table):
        assert db._nocommit_stack == 1
        empty_table.insert_many([sample_row(0)])
        assert _num_rows(empty_table) == 1
        db.conn.rollback()
        assert _num_rows(empty_table) == 0
    assert db._nocommit_stack == 0


def test_delaycommit_batches_writes_and_commits_at_the_end(db, empty_table):
    with DelayCommit(empty_table):
        empty_table.insert_many([sample_row(0)])
        empty_table.insert_many([sample_row(1)])
        empty_table.update({"n": 0}, {"label": "batched"})
    db.conn.rollback()
    assert _num_rows(empty_table) == 2
    assert empty_table.lucky({"n": 0}, projection="label") == "batched"


def test_delaycommit_rolls_back_on_an_exception(db, empty_table):
    with pytest.raises(RuntimeError):
        with DelayCommit(empty_table):
            empty_table.insert_many([sample_row(i) for i in range(3)])
            raise RuntimeError("boom")
    assert _num_rows(empty_table) == 0
    assert db._nocommit_stack == 0


def test_nested_delaycommit_only_commits_at_the_outermost_exit(db, empty_table):
    with DelayCommit(empty_table):
        with DelayCommit(empty_table):
            assert db._nocommit_stack == 2
            empty_table.insert_many([sample_row(0)])
        assert db._nocommit_stack == 1
        db.conn.rollback()
        assert _num_rows(empty_table) == 0
    assert db._nocommit_stack == 0


##################################################################
# a known bug in the cached row count                            #
##################################################################


def test_count_with_empty_query_matches_number_of_rows(filled_table):
    assert filled_table.count({}) == 200
