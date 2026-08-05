# Configuration for the psycodict documentation build (Sphinx).
#
# Build locally with
#
#     pip install '.[pgbinary]' -r docs/requirements.txt
#     python -m sphinx -W --keep-going -b html docs docs/_build/html
#
# The same build runs in CI (the docs job of ci.yml) and on Read the Docs
# (.readthedocs.yaml), with warnings treated as errors in both.
import shutil
from importlib.metadata import version as _dist_version
from pathlib import Path

_DOCS = Path(__file__).resolve().parent
_ROOT = _DOCS.parent

# The canonical copies of the narrative guides live at the repository root,
# where GitHub renders them; they are copied here at build time so that the
# site includes them and their relative links to one another keep working.
# The copies are ignored by git (see .gitignore).
_GUIDES = [
    "README.md",
    "QueryLanguage.md",
    "Searching.md",
    "DataManagement.md",
    "MetadataFormats.md",
    "Versioning.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
]
for _name in _GUIDES:
    shutil.copyfile(_ROOT / _name, _DOCS / _name)

project = "psycodict"
author = "David Roe and Edgar Costa"
copyright = "2019–2026, David Roe and Edgar Costa"
release = _dist_version("psycodict")
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

# Generate GitHub-style anchors for headings so that intra-page links in the
# Markdown guides ([`search`](#search) and friends) keep working on the site.
myst_heading_anchors = 4

# The API reference is generated from the docstrings, which follow the Sage
# documentation conventions (INPUT:/OUTPUT: bullet blocks, EXAMPLES:: with
# literal transcripts); those are plain reST, so autodoc renders them as-is.
autodoc_member_order = "bysource"
# No "undoc-members": with __all__ defined, autodoc considers only the names a
# module exports, and this keeps it from documenting the ones it cannot -- so a
# helper that merely carries a docstring no longer lands in the reference just
# because it has one.  That is an upper bound, not a proof of parity: autodoc
# also *drops* an exported name it has no docstring for, and (on a page without
# "imported-members"/"special-members") an exported name that is an import or a
# dunder.  tests/test_public_api.py supplies the parity half -- every __all__
# name has a docstring, and every module in the frozen surface has a page here.
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "psycopg": ("https://www.psycopg.org/psycopg3/docs", None),
}

exclude_patterns = ["_build"]

html_theme = "furo"
html_title = f"psycodict {release}"
