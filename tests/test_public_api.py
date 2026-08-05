# -*- coding: utf-8 -*-
"""
The public API is exactly what ``__all__`` says it is.

Versioning.md defines the supported surface as the names each module exports in
``__all__``.  This test freezes those sets, so adding or removing a public name
is a deliberate, reviewed change rather than an accident -- and so that a name a
downstream project relies on cannot quietly leave the promise.
"""
import importlib

import pytest


# The frozen public surface, module by module.  Changing this is changing the
# 1.x API contract: update Versioning.md and coordinate downstream in the same
# breath.
EXPECTED = {
    "psycodict": {
        "__version__", "SQL", "Identifier", "Placeholder", "Literal",
        "Composable", "Composed", "DelayCommit",
    },
    "psycodict.base": {"PostgresBase"},
    "psycodict.database": {"PostgresDatabase"},
    "psycodict.table": {"PostgresTable"},
    "psycodict.searchtable": {"PostgresSearchTable"},
    "psycodict.statstable": {"PostgresStatsTable"},
    "psycodict.config": {"Configuration"},
    "psycodict.utils": {
        "DelayCommit", "IdentifierWrapper", "LockError", "SearchParsingError",
    },
    "psycodict.encoding": {"Json", "Array", "copy_dumps"},
    "psycodict.grants": {"GrantPolicy", "LMFDBGrantPolicy"},
    "psycodict.notifications": {"NotificationListener"},
    "psycodict.dbdiff": {"compare_databases", "format_differences"},
    "psycodict.slowlog": {"parse_slow_log", "slow_query_report", "show_slow_report"},
    "psycodict.validation": {"InvalidColumnTypeError", "InvalidDefinitionError"},
}


@pytest.mark.parametrize("modname", sorted(EXPECTED))
def test_all_matches_the_frozen_surface(modname):
    mod = importlib.import_module(modname)
    assert hasattr(mod, "__all__"), "%s has no __all__" % modname
    assert set(mod.__all__) == EXPECTED[modname], (
        "%s.__all__ changed; update Versioning.md and this snapshot deliberately"
        % modname
    )


@pytest.mark.parametrize("modname", sorted(EXPECTED))
def test_every_exported_name_resolves(modname):
    mod = importlib.import_module(modname)
    for name in mod.__all__:
        assert hasattr(mod, name), "%s exports %s, which does not exist" % (modname, name)


def test_star_import_gives_exactly_all():
    """
    ``from psycodict import *`` binds exactly the root ``__all__`` names.
    """
    ns = {}
    exec("from psycodict import *", ns)
    bound = {k for k in ns if not k.startswith("__") or k == "__version__"}
    assert bound == EXPECTED["psycodict"]


def test_downstream_imports_still_resolve():
    """
    The names LMFDB and seminars import, including a few private ones and a few
    kept-importable-but-unpromised ones, must keep resolving: ``__all__`` governs
    ``import *`` and the docs, not explicit imports.
    """
    from psycodict import SQL, DelayCommit  # noqa: F401
    from psycodict.database import PostgresDatabase  # noqa: F401
    from psycodict.searchtable import PostgresSearchTable  # noqa: F401
    from psycodict.statstable import PostgresStatsTable  # noqa: F401
    from psycodict.base import PostgresBase, number_types  # noqa: F401
    from psycodict.config import Configuration  # noqa: F401
    from psycodict.encoding import Json, Array, copy_dumps  # noqa: F401
    from psycodict.grants import LMFDBGrantPolicy  # noqa: F401
    from psycodict.utils import (  # noqa: F401
        IdentifierWrapper, SearchParsingError,
        range_formatter, KeyedDefaultDict,
    )
    # private names seminars imports for its schema bootstrap
    from psycodict.table import _counts_cols, _stats_cols  # noqa: F401
    from psycodict.base import (  # noqa: F401
        _meta_indexes_cols, _meta_constraints_cols, _meta_tables_cols,
    )
