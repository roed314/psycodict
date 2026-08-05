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

from psycodict.base import EXPORT_FORMAT, EXPORT_FORMAT_MARKER, export_format_line


def read(path):
    with open(path) as F:
        return F.read()


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
    # strip the marker line to reproduce a format-0 file byte-for-byte
    body = read(marked).split("\n", 1)[1]
    assert read(marked).startswith(EXPORT_FORMAT_MARKER)
    legacy = tmp_path / "legacy.txt"
    legacy.write_text(body)
    assert not read(str(legacy)).startswith(EXPORT_FORMAT_MARKER)

    target = table_factory()
    target.copy_from(str(legacy))
    assert target.count() == 200


def test_a_future_format_is_refused_before_loading(filled_table, table_factory, tmp_path):
    """
    A file claiming a newer format than this psycodict understands is rejected
    with a clear message, and nothing is loaded.
    """
    searchfile = filled_table_dump(filled_table, tmp_path)
    lines = read(searchfile).split("\n")
    lines[0] = "%s %s" % (EXPORT_FORMAT_MARKER, EXPORT_FORMAT + 5)
    with open(searchfile, "w") as F:
        F.write("\n".join(lines))

    target = table_factory()
    with pytest.raises(ValueError, match="export format"):
        target.copy_from(searchfile)
    assert target.count() == 0


def test_a_malformed_marker_is_rejected(filled_table, table_factory, tmp_path):
    searchfile = filled_table_dump(filled_table, tmp_path)
    lines = read(searchfile).split("\n")
    lines[0] = "%s banana" % EXPORT_FORMAT_MARKER
    with open(searchfile, "w") as F:
        F.write("\n".join(lines))
    target = table_factory()
    with pytest.raises(ValueError, match="[Mm]arker"):
        target.copy_from(searchfile)


def test_reload_reads_a_marked_file(filled_table, tmp_path):
    searchfile = str(tmp_path / "s.txt")
    filled_table.copy_to(searchfile, include_id=False)
    filled_table.reload(searchfile)
    assert filled_table.count() == 200


def test_create_table_from_header_reads_the_marker(db, filled_table, tmp_path):
    """
    adjust_schema builds a table from a file's header; that path reads through
    the same header reader, so the marker is handled there too.
    """
    folder = tmp_path / "data"
    db.copy_to([filled_table.search_table], str(folder))
    searchfile = folder / (filled_table.search_table + ".txt")
    assert read(str(searchfile)).startswith(EXPORT_FORMAT_MARKER)


# --- helpers ---------------------------------------------------------------

def filled_table_dump(table, tmp_path):
    searchfile = str(tmp_path / "s.txt")
    table.copy_to(searchfile, include_id=False)
    return searchfile
