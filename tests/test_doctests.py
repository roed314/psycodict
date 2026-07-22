# -*- coding: utf-8 -*-
"""
Run the doctests embedded in psycodict's docstrings.

The EXAMPLES blocks in the library are real transcripts: they run here,
against two small tables of genuine LMFDB data created for the purpose --
``test_fields``, 22 selected number fields of degree at most 3 (the
rationals, twelve quadratic fields, and the nine cubic fields with
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
so this only matters when experimenting with pytest-xdist).  For the same
reason the fixture refuses to run at all while *anything* occupies its
namespace (``test_fields``/``test_curves`` and their derived ``_tmp``,
``_old*``, ``_counts`` and ``_stats`` names): it never guesses whether an
existing table is disposable.  After a crashed run, clean up by hand as
the failure message describes, or point the tests at a scratch database.
"""
import doctest
import importlib

import pytest


# The columns and rows of the standard example tables.  Real LMFDB data:
# 22 selected number fields of degree <= 3 (the rationals, twelve
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


def _occupying_tables(db):
    """
    Every table in the public schema whose name lies in this file's
    namespace: the two fixed names and anything derived from them
    (``_tmp``, ``_old*``, ``_counts``, ``_stats``, ...), whether or not
    psycodict's meta tables know about it.
    """
    from psycodict import SQL

    cur = db._execute(
        SQL(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND (tablename IN ('test_fields', 'test_curves') "
            "OR tablename LIKE %s OR tablename LIKE %s)"
        ),
        ["test\\_fields\\_%", "test\\_curves\\_%"],
    )
    return sorted(row[0] for row in cur)


@pytest.fixture(scope="module")
def doc_tables(db):
    # These tables have fixed names (the docstrings use them), unlike the
    # rest of the suite, which randomises its names.  Never guess whether
    # an existing table is disposable: refuse to run while anything
    # occupies the namespace, and require explicit cleanup instead.
    occupied = _occupying_tables(db)
    if occupied:
        pytest.fail(
            "The database already contains %s.  These names are reserved "
            "by tests/test_doctests.py; refusing to drop anything it did "
            "not create in this run.  If they are leftovers of a crashed "
            "earlier run, remove them by hand -- for the main tables "
            "db.drop_table('test_fields', force=True) (likewise "
            "test_curves), and DROP TABLE for bare _tmp/_old debris -- or "
            "point the tests at a scratch database."
            % ", ".join(occupied)
        )
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
    yield db
    # The namespace was verified empty above and the suite runs serially,
    # so everything in it now is ours: the two tables, their stats/counts
    # companions, and the staged() example's test_fields_old1 backup.
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
