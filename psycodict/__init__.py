# -*- coding: utf-8 -*-
"""
This module provides an interface to Postgres supporting
the kinds of queries needed by the LMFDB.

EXAMPLES::

    sage: from lmfdb import db
    sage: db
    Interface to Postgres database
    sage: len(db.tablenames)
    53
    sage: db.tablenames[0]
    'artin_field_data'
    sage: db.artin_field_data
    Interface to Postgres table artin_field_data

You can search using the methods ``search``, ``lucky`` and ``lookup``::

    sage: G = db.gps_groups.lookup('8.2')
    sage: G['exponent']
    4

- ``count_table`` -- a string or None.  If provided, gives the name of a table that caches counts for searches on the search table.  These counts are relevant when many results are returned, allowing the search pages to report the number of records even when it would take Postgres a long time to compute this count.

"""

# Single source of truth for the package version: pyproject.toml reads it via
# ``[tool.setuptools.dynamic]``, and it works from an uninstalled checkout too.
__version__ = "1.0.0"

try:
    import psycopg
    assert psycopg
except ImportError:
    print('Missing psycopg dependency; either do "pip install psycopg[binary]" or "pip install psycopg" (requires libpq installed on your system)')
    raise

from .utils import DelayCommit

assert DelayCommit
# Re-export the SQL composition classes of the driver psycodict is built on.
# Downstream projects that compose queries for _execute should import these
# from psycodict rather than from a driver directly, so that their code does
# not depend on which driver psycodict uses.
from psycopg.sql import SQL, Identifier, Placeholder, Literal, Composable, Composed

assert SQL and Identifier and Placeholder and Literal and Composable and Composed
