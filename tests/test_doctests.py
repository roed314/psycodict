# -*- coding: utf-8 -*-
"""
Run the doctests embedded in psycodict's docstrings.

The EXAMPLES blocks in the library are real transcripts: they run here,
against two small tables of genuine LMFDB data created for the purpose --
``test_fields``, 22 selected number fields of degree at most 3 (the
rationals, thirteen quadratic fields, and the nine cubic fields with
absolute discriminant at most 90), and ``test_curves``, a dozen elliptic
curves over three of those fields.  Each docstring can assume a connected
database ``db`` with those two tables and nothing else; anything a reader
should reproduce (``nf = db.test_fields`` and so on) is written out in
the example itself.

Examples that demonstrate operational workflows (reloads, staged writes,
comparing servers) are marked ``# doctest: +SKIP`` in their docstrings:
they show the shape of a session rather than a reproducible transcript.

Unlike the rest of the suite, which randomises its table names, these
tables have the fixed names the docstrings use, so this file must not run
concurrently with itself against one database (the suite runs serially,
so this only matters when experimenting with pytest-xdist).
"""
import doctest
import importlib

import pytest


# The columns and rows of the standard example tables.  Real LMFDB data:
# 22 selected number fields of degree <= 3 (the rationals, thirteen
# quadratic fields, and the nine cubic fields with |disc| <= 90), and
# elliptic curves over Q(sqrt(5)), Q(sqrt(2)) and Q(sqrt(13)) of small
# conductor norm.
FIELD_COLUMNS = [
    ("label", "text"),
    ("degree", "smallint"),
    ("r2", "smallint"),
    ("disc_abs", "integer"),
    ("disc_sign", "smallint"),
    ("ramps", "integer[]"),
    ("class_number", "integer"),
    ("class_group", "jsonb"),
]

FIELDS = [
    ("1.1.1.1", 1, 0, 1, 1, [], 1, []),
    ("2.0.3.1", 2, 1, 3, -1, [3], 1, []),
    ("2.0.4.1", 2, 1, 4, -1, [2], 1, []),
    ("2.2.5.1", 2, 0, 5, 1, [5], 1, []),
    ("2.0.7.1", 2, 1, 7, -1, [7], 1, []),
    ("2.0.8.1", 2, 1, 8, -1, [2], 1, []),
    ("2.2.8.1", 2, 0, 8, 1, [2], 1, []),
    ("2.0.11.1", 2, 1, 11, -1, [11], 1, []),
    ("2.2.12.1", 2, 0, 12, 1, [2, 3], 1, []),
    ("2.2.13.1", 2, 0, 13, 1, [13], 1, []),
    ("2.0.15.1", 2, 1, 15, -1, [3, 5], 2, [2]),
    ("2.0.23.1", 2, 1, 23, -1, [23], 3, [3]),
    ("2.0.47.1", 2, 1, 47, -1, [47], 5, [5]),
    ("3.1.23.1", 3, 1, 23, -1, [23], 1, []),
    ("3.1.31.1", 3, 1, 31, -1, [31], 1, []),
    ("3.1.44.1", 3, 1, 44, -1, [2, 11], 1, []),
    ("3.3.49.1", 3, 0, 49, 1, [7], 1, []),
    ("3.1.59.1", 3, 1, 59, -1, [59], 1, []),
    ("3.1.76.1", 3, 1, 76, -1, [2, 19], 1, []),
    ("3.3.81.1", 3, 0, 81, 1, [3], 1, []),
    ("3.1.83.1", 3, 1, 83, -1, [83], 1, []),
    ("3.1.87.1", 3, 1, 87, -1, [3, 29], 1, []),
]

CURVE_COLUMNS = [
    ("label", "text"),
    ("field_label", "text"),
    ("conductor_norm", "integer"),
    ("rank", "smallint"),
    ("torsion_order", "smallint"),
]

CURVES = [
    ("2.2.5.1-31.1-a1", "2.2.5.1", 31, 0, 8),
    ("2.2.5.1-31.1-a2", "2.2.5.1", 31, 0, 2),
    ("2.2.5.1-31.1-a3", "2.2.5.1", 31, 0, 4),
    ("2.2.5.1-31.1-a4", "2.2.5.1", 31, 0, 2),
    ("2.2.5.1-31.1-a5", "2.2.5.1", 31, 0, 8),
    ("2.2.8.1-9.1-a1", "2.2.8.1", 9, 0, 2),
    ("2.2.8.1-9.1-a2", "2.2.8.1", 9, 0, 10),
    ("2.2.8.1-9.1-a3", "2.2.8.1", 9, 0, 10),
    ("2.2.13.1-51.2-a1", "2.2.13.1", 51, 1, 1),
    ("2.2.13.1-51.2-a2", "2.2.13.1", 51, 1, 1),
    ("2.2.13.1-51.3-a1", "2.2.13.1", 51, 1, 1),
    ("2.2.13.1-51.3-a2", "2.2.13.1", 51, 1, 1),
]

# The modules holding EXAMPLES blocks, in execution order.  Modules whose
# examples only read the tables can go in any order; statstable's examples
# add statistics rows (to the side tables test_fields_counts and
# test_fields_stats), which is why it comes after the pure readers.
MODULES = [
    "psycodict",
    "psycodict.utils",
    "psycodict.encoding",
    "psycodict.base",
    "psycodict.searchtable",
    "psycodict.statstable",
    "psycodict.table",
    "psycodict.database",
    "psycodict.slowlog",
]


def _rows(columns, data):
    names = [col for col, typ in columns]
    return [dict(zip(names, row)) for row in data]


def _is_our_leftover(db, name, columns, data):
    """
    Whether an existing table with one of our fixed names is a leftover
    from a crashed earlier run of this file: same columns and the same
    set of labels.  Anything else is somebody's real table.
    """
    table = db[name]
    if sorted(table.search_cols) != sorted(col for col, typ in columns):
        return False
    try:
        return set(table.distinct("label")) == {row[0] for row in data}
    except Exception:
        return False


@pytest.fixture(scope="module")
def doc_tables(db):
    # These tables have fixed names (the docstrings use them), unlike the
    # rest of the suite, which randomises its names.  Refuse to touch an
    # existing table unless it is recognizably a leftover of our own.
    for name, columns, data in [
        ("test_curves", CURVE_COLUMNS, CURVES),
        ("test_fields", FIELD_COLUMNS, FIELDS),
    ]:
        if name in db.tablenames:
            if not _is_our_leftover(db, name, columns, data):
                pytest.fail(
                    "The database already contains a table named %r that "
                    "does not look like this test file's own leftover; "
                    "refusing to drop it.  Point the tests at a scratch "
                    "database or remove the table by hand." % name
                )
            db.drop_table(name, force=True)
    db.create_table(
        "test_fields",
        FIELD_COLUMNS,
        label_col="label",
        sort=["degree", "disc_abs", "label"],
    )
    db.test_fields.insert_many(_rows(FIELD_COLUMNS, FIELDS))
    db.create_table(
        "test_curves",
        CURVE_COLUMNS,
        label_col="label",
        sort=["conductor_norm", "label"],
    )
    db.test_curves.insert_many(_rows(CURVE_COLUMNS, CURVES))
    # Clear any _tmp/_old debris of ours from a crashed earlier run (the
    # staged() example creates a test_fields_old1 backup, for instance).
    db.test_fields.cleanup_from_reload()
    db.test_curves.cleanup_from_reload()
    yield db
    for name in ("test_curves", "test_fields"):
        try:
            db[name].cleanup_from_reload()
            db.drop_table(name, force=True)
        except Exception:  # pragma: no cover - cleanup must not mask failures
            pass


@pytest.mark.parametrize("modname", MODULES)
def test_doctests(modname, doc_tables, tmp_path, monkeypatch):
    db = doc_tables
    # The getting-started example calls PostgresDatabase() with no
    # arguments, which finds its configuration through $PSYCODICT_CONFIG;
    # point that at this test database.
    opts = db.config.options["postgresql"]
    cfgfile = tmp_path / "config.ini"
    with open(cfgfile, "w") as F:
        F.write("[logging]\nslowcutoff = 0.1\nslowlogfile = %s\n" % (tmp_path / "slow_queries.log"))
        F.write("[postgresql]\n")
        for key in ["host", "port", "user", "password", "dbname"]:
            F.write("%s = %s\n" % (key, opts[key]))
    monkeypatch.setenv("PSYCODICT_CONFIG", str(cfgfile))

    mod = importlib.import_module(modname)
    results = doctest.testmod(
        mod,
        extraglobs={"db": db},
        optionflags=doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS,
        verbose=False,
    )
    assert results.failed == 0, "%s doctest failure(s) in %s" % (results.failed, modname)
    assert results.attempted > 0 or modname in ("psycodict.notifications",), (
        "expected doctests in %s" % modname
    )
