# psycodict

psycodict is a dictionary-based Python interface to PostgreSQL databases:
queries are Python dictionaries, results come back as Python values, and the
metadata that makes this ergonomic — column types, sort orders, statistics —
is managed for you.  It was extracted from the
[LMFDB](https://www.lmfdb.org) and powers several mathematical databases.

The guides below describe the system top-down; the API reference is
generated from the docstrings.

```{toctree}
:maxdepth: 2
:caption: Guides

Getting started <README>
QueryLanguage
Searching
DataManagement
MetadataFormats
```

```{toctree}
:maxdepth: 2
:caption: Reference

api/index
```
