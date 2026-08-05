# -*- coding: utf-8 -*-
"""
The public API is exactly what ``__all__`` says it is.

Versioning.md defines the supported surface as the names each module exports in
``__all__``.  This test freezes those sets, so adding or removing a public name
is a deliberate, reviewed change rather than an accident -- and so that a name a
downstream project relies on cannot quietly leave the promise.

It also pins the two things that promise depends on: that ``from <mod> import *``
binds exactly ``__all__`` (the one behavioral change declaring it makes), and
that the API reference can actually document the whole of ``__all__`` -- every
module has a page, and every exported callable has a docstring.
"""
import importlib
import inspect
import re
from pathlib import Path

import pytest


_API_DOCS = Path(__file__).resolve().parent.parent / "docs" / "api"


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


@pytest.mark.parametrize("modname", sorted(EXPECTED))
def test_star_import_gives_exactly_all(modname):
    """
    ``from <module> import *`` binds exactly that module's ``__all__``.

    This is the one place where declaring ``__all__`` changes what Python does
    rather than only what psycodict promises: before, a wildcard import bound
    every non-underscore name in the module.  Pinning it per module keeps the
    behavioral consequence of the contract visible, and keeps it from looking
    like only the package root is governed by ``__all__``.
    """
    ns = {}
    exec("from %s import *" % modname, ns)
    bound = set(ns) - {"__builtins__"}
    assert bound == EXPECTED[modname]


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


@pytest.mark.parametrize("modname", sorted(EXPECTED))
def test_module_has_an_api_reference_page(modname):
    """
    Every module in the frozen surface has a page in the API reference.

    Versioning.md promises that the ``__all__`` names are exactly the names in
    the API reference; that promise is only keepable if a module cannot join
    the public surface without a page to be documented on.  (Sphinx's ``-W``
    build then catches a page that is not in a toctree.)
    """
    if not _API_DOCS.is_dir():
        pytest.skip("built without docs/ (%s)" % _API_DOCS)
    documented = set()
    for page in _API_DOCS.glob("*.md"):
        documented.update(
            re.findall(r"^\s*\.\.\s+automodule::\s*(\S+)\s*$", page.read_text(), re.M)
        )
    assert modname in documented, (
        "%s is in the public API but no docs/api/*.md documents it; add a page "
        "and put it in the toctree in docs/api/index.md" % modname
    )


@pytest.mark.parametrize("modname", sorted(EXPECTED))
def test_every_exported_name_is_documented(modname):
    """
    Every exported class and function carries a docstring.

    The API reference runs without ``undoc-members``, so an exported name with
    no docstring is silently absent from it -- the reference would quietly stop
    covering the whole of ``__all__``.  (Exported data such as ``__version__``
    is documented by a ``#:`` comment, which lives in the source rather than on
    the object, so only callables are checked here.)
    """
    mod = importlib.import_module(modname)
    for name in sorted(mod.__all__):
        obj = getattr(mod, name)
        if not (inspect.isclass(obj) or inspect.isroutine(obj)):
            continue
        assert obj.__doc__, (
            "%s.%s is exported but has no docstring, so the API reference "
            "would not document it" % (modname, name)
        )
