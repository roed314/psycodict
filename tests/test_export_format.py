# -*- coding: utf-8 -*-
"""
Search-data files carry an optional format marker.

A new file begins with ``# psycodict-export-format: N``; a file without one is
format 0, the historical layout, so every file psycodict has ever written still
reads.  A version this psycodict does not understand is refused before any data
is loaded.  This is a property of the *data file*, distinct from the metadata
format of the ``meta_*`` tables.
"""
import pytest

from psycodict.base import (
    EXPORT_FORMAT,
    EXPORT_FORMAT_MARKER,
    export_format_line,
    export_format_version,
)

# A column may be called anything printable, so this is a legal column name --
# and it is character for character what a current marker line looks like.
MARKER_NAME = export_format_line()


def read(path):
    with open(path) as F:
        return F.read()


def strip_marker(path, dest):
    """
    The same file as a format-0 file: byte for byte, minus the marker line.
    """
    body = read(str(path)).split("\n", 1)[1]
    assert read(str(path)).startswith(EXPORT_FORMAT_MARKER)
    dest.write_text(body)
    return str(dest)


def test_a_new_export_starts_with_the_marker(filled_table, tmp_path):
    searchfile = str(tmp_path / "s.txt")
    filled_table.copy_to(searchfile)
    first = read(searchfile).split("\n")[0]
    assert first == "%s %s" % (EXPORT_FORMAT_MARKER, EXPORT_FORMAT)


def test_a_marked_file_round_trips(filled_table, table_factory, tmp_path):
    searchfile = str(tmp_path / "s.txt")
    filled_table.copy_to(searchfile)
    assert export_format_line() in read(searchfile)
    target = table_factory()
    target.copy_from(searchfile)
    assert target.count() == filled_table.count()
    assert {r["n"] for r in target.search({}, ["n"])} == set(range(200))


def test_a_legacy_unmarked_file_still_loads(filled_table, table_factory, tmp_path):
    """
    A format-0 file -- no marker, just names/types/blank/data -- is what every
    older export looks like, and must keep loading.
    """
    marked = str(tmp_path / "marked.txt")
    filled_table.copy_to(marked, include_id=False)
    legacy = strip_marker(marked, tmp_path / "legacy.txt")
    assert not read(legacy).startswith(EXPORT_FORMAT_MARKER)

    target = table_factory()
    target.copy_from(legacy)
    assert target.count() == 200


def test_a_future_format_is_refused_before_loading(filled_table, table_factory, tmp_path):
    """
    A file claiming a newer format than this psycodict understands is rejected
    with a clear message, and nothing is loaded.
    """
    searchfile = filled_table_dump(filled_table, tmp_path)
    rewrite_first_line(searchfile, "%s %s" % (EXPORT_FORMAT_MARKER, EXPORT_FORMAT + 5))

    target = table_factory()
    with pytest.raises(ValueError, match="export format"):
        target.copy_from(searchfile)
    assert target.count() == 0


def test_a_malformed_marker_is_rejected(filled_table, table_factory, tmp_path):
    searchfile = filled_table_dump(filled_table, tmp_path)
    rewrite_first_line(searchfile, "%s banana" % EXPORT_FORMAT_MARKER)
    target = table_factory()
    with pytest.raises(ValueError, match="[Mm]arker"):
        target.copy_from(searchfile)
    assert target.count() == 0


def test_a_negative_version_is_rejected(filled_table, table_factory, tmp_path):
    """
    A version is a nonnegative decimal.  ``int()`` would happily read ``-1``,
    but psycodict has never written such a marker, so a file carrying one was
    not written by any psycodict and must not be read as format -1 (which,
    being "not newer than the current format", would otherwise be accepted).
    """
    searchfile = filled_table_dump(filled_table, tmp_path)
    rewrite_first_line(searchfile, "%s -1" % EXPORT_FORMAT_MARKER)
    target = table_factory()
    with pytest.raises(ValueError, match="[Mm]arker"):
        target.copy_from(searchfile)
    assert target.count() == 0


def test_export_format_version_reads_only_the_exact_grammar():
    assert export_format_version(export_format_line()) == EXPORT_FORMAT
    assert export_format_version("%s 0" % EXPORT_FORMAT_MARKER) == 0
    assert export_format_version("  %s 12\n" % EXPORT_FORMAT_MARKER) == 12
    for bad in [
        "%s -1" % EXPORT_FORMAT_MARKER,
        "%s +1" % EXPORT_FORMAT_MARKER,
        "%s 1.0" % EXPORT_FORMAT_MARKER,
        "%s banana" % EXPORT_FORMAT_MARKER,
        "%s" % EXPORT_FORMAT_MARKER,
        "%s 1 and more" % EXPORT_FORMAT_MARKER,
        "%s 1|another_column" % EXPORT_FORMAT_MARKER,
    ]:
        with pytest.raises(ValueError, match="[Mm]arker"):
            export_format_version(bad)


# --- legacy headers that look like a marker --------------------------------
#
# validate_column_name rules out only an empty name and control characters, so
# a format-0 names row may begin with the marker prefix, or be exactly a valid
# marker.  Deciding on the prefix alone would refuse to load those older files.

MARKER_LIKE_COLUMNS = [(MARKER_NAME, "integer"), ("another_column", "text")]
MARKER_ONLY_COLUMNS = [(MARKER_NAME, "integer")]


def marker_like_table(table_factory, columns):
    table = table_factory(columns=columns, label_col=None, sort=None)
    table.insert_many([{col: i if typ == "integer" else "v%d" % i
                        for col, typ in columns} for i in range(5)])
    return table


def test_a_legacy_names_row_beginning_with_the_prefix_loads(table_factory, tmp_path):
    """
    ``# psycodict-export-format: 1|another_column`` is a valid format-0 names
    row for a two-column table, not a malformed marker.
    """
    source = marker_like_table(table_factory, MARKER_LIKE_COLUMNS)
    marked = str(tmp_path / "marked.txt")
    source.copy_to(marked, include_id=False)
    legacy = strip_marker(marked, tmp_path / "legacy.txt")
    assert read(legacy).split("\n")[0] == "%s|another_column" % MARKER_NAME

    target = table_factory(columns=MARKER_LIKE_COLUMNS, label_col=None, sort=None)
    target.copy_from(legacy)
    assert target.count() == 5
    assert {r[MARKER_NAME] for r in target.search({}, [MARKER_NAME])} == set(range(5))


def test_a_one_column_legacy_names_row_equal_to_a_marker_loads(table_factory, tmp_path):
    """
    The exactly ambiguous case: a one-column format-0 file whose names row *is*
    a syntactically valid marker.  The blank line tells the two apart -- a
    format-0 header is names/types/blank, a marked one marker/names/types/blank.
    """
    source = marker_like_table(table_factory, MARKER_ONLY_COLUMNS)
    marked = str(tmp_path / "marked.txt")
    source.copy_to(marked, include_id=False)
    legacy = strip_marker(marked, tmp_path / "legacy.txt")
    assert read(legacy).split("\n")[0] == MARKER_NAME

    target = table_factory(columns=MARKER_ONLY_COLUMNS, label_col=None, sort=None)
    target.copy_from(legacy)
    assert target.count() == 5
    assert {r[MARKER_NAME] for r in target.search({}, [MARKER_NAME])} == set(range(5))


def test_a_marker_like_legacy_names_row_loads_with_a_custom_separator(
    table_factory, tmp_path
):
    """
    The parser takes ``sep=``, and the names row is split with it, so the
    disambiguation must hold for a file written with a separator other than
    ``|`` too.
    """
    source = marker_like_table(table_factory, MARKER_LIKE_COLUMNS)
    marked = str(tmp_path / "marked.txt")
    source.copy_to(marked, include_id=False, sep=";")
    legacy = strip_marker(marked, tmp_path / "legacy.txt")
    assert read(legacy).split("\n")[0] == "%s;another_column" % MARKER_NAME

    target = table_factory(columns=MARKER_LIKE_COLUMNS, label_col=None, sort=None)
    target.copy_from(legacy, sep=";")
    assert target.count() == 5


def test_a_marked_file_round_trips_with_a_custom_separator(table_factory, tmp_path):
    source = marker_like_table(table_factory, MARKER_LIKE_COLUMNS)
    marked = str(tmp_path / "marked.txt")
    source.copy_to(marked, include_id=False, sep=";")
    assert read(marked).split("\n")[0] == MARKER_NAME

    target = table_factory(columns=MARKER_LIKE_COLUMNS, label_col=None, sort=None)
    target.copy_from(marked, sep=";")
    assert target.count() == 5


def test_a_header_without_its_blank_line_says_so(filled_table, table_factory, tmp_path):
    """
    The error names what is wrong, not which physical line it was on: the blank
    line is the third line of a format-0 header and the fourth of a marked one.
    """
    searchfile = filled_table_dump(filled_table, tmp_path)
    lines = read(searchfile).split("\n")
    del lines[3]  # the blank line of a marked header
    with open(searchfile, "w") as F:
        F.write("\n".join(lines))
    target = table_factory()
    with pytest.raises(ValueError, match="blank line"):
        target.copy_from(searchfile)


def test_reload_reads_a_marked_file(filled_table, tmp_path):
    searchfile = str(tmp_path / "s.txt")
    filled_table.copy_to(searchfile, include_id=False)
    filled_table.reload(searchfile)
    assert filled_table.count() == 200


def test_adjust_schema_builds_the_table_from_a_marked_header(db, filled_table, tmp_path):
    """
    ``adjust_schema=True`` builds the replacement table from the file's header
    instead of cloning the current one, and it reads that header through the
    same marker-aware reader.  Exporting a strict subset of the columns proves
    the header really drove the creation: the reloaded table has exactly the
    columns the file named.
    """
    searchfile = str(tmp_path / "s.txt")
    name = filled_table.search_table
    filled_table.copy_to(searchfile, columns=["n", "label"], include_id=False)
    assert read(searchfile).startswith(EXPORT_FORMAT_MARKER)

    # resort=False because renumbering ids selects the *old* table's columns,
    # which a narrowed schema no longer has; that is a limitation of
    # adjust_schema, not of the header reader this test is about.
    filled_table.reload(searchfile, adjust_schema=True, resort=False)

    # reload swaps in a new table, so the assertions must be made against the
    # object the database hands out now, not the pre-reload one.
    reloaded = db[name]
    assert set(reloaded.search_cols) == {"n", "label"}
    assert reloaded.count() == 200
    assert {r["n"] for r in reloaded.search({}, ["n"])} == set(range(200))
    assert reloaded.lucky({"n": 7}, "label") == "l7"


# --- the automatic reindex decision ----------------------------------------
#
# copy_from reindexes when more than 1000 *rows* are added.  A marked file's
# header is four lines and a format-0 file's is three, so a count of physical
# lines would put that boundary in a different place for each format.

SIMPLE_COLUMNS = [("n", "integer"), ("label", "text")]


def write_search_file(path, nrows, marked=True, sep="|"):
    with open(path, "w") as F:
        if marked:
            F.write(export_format_line() + "\n")
        F.write(sep.join(col for col, _ in SIMPLE_COLUMNS) + "\n")
        F.write(sep.join(typ for _, typ in SIMPLE_COLUMNS) + "\n\n")
        for i in range(nrows):
            F.write("%d%sl%d\n" % (i, sep, i))
    return str(path)


@pytest.mark.parametrize("marked", [True, False], ids=["marked", "legacy"])
@pytest.mark.parametrize("nrows", [0, 1, 1000, 1001])
def test_count_data_rows_ignores_the_header(empty_table, tmp_path, marked, nrows):
    searchfile = write_search_file(tmp_path / "s.txt", nrows, marked=marked)
    assert empty_table._count_data_rows(searchfile) == nrows


def test_count_data_rows_honors_a_custom_separator(empty_table, tmp_path):
    searchfile = write_search_file(tmp_path / "s.txt", 3, sep=";")
    assert empty_table._count_data_rows(searchfile, sep=";") == 3


@pytest.mark.parametrize("marked", [True, False], ids=["marked", "legacy"])
@pytest.mark.parametrize(
    "nrows,expected", [(1000, False), (1001, True)], ids=["at-the-limit", "over-it"]
)
def test_copy_from_reindexes_above_1000_rows(
    table_factory, tmp_path, monkeypatch, marked, nrows, expected
):
    """
    Whether the indexes were dropped is asked of ``drop_indexes`` itself: both
    branches leave the same indexes in place afterward, so the final state of
    the database cannot tell them apart.
    """
    target = table_factory(columns=SIMPLE_COLUMNS)
    searchfile = write_search_file(tmp_path / "s.txt", nrows, marked=marked)

    dropped = []
    original = target.drop_indexes

    def spy(*args, **kwds):
        dropped.append((args, kwds))
        return original(*args, **kwds)

    monkeypatch.setattr(target, "drop_indexes", spy)
    target.copy_from(searchfile)

    assert bool(dropped) is expected
    assert target.count() == nrows


# --- helpers ---------------------------------------------------------------

def filled_table_dump(table, tmp_path):
    searchfile = str(tmp_path / "s.txt")
    table.copy_to(searchfile, include_id=False)
    return searchfile


def rewrite_first_line(path, line):
    lines = read(path).split("\n")
    lines[0] = line
    with open(path, "w") as F:
        F.write("\n".join(lines))
