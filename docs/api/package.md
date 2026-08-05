# psycodict

The package root.  Its `__all__` is the version marker and the names psycodict
re-exports for convenience, so that downstream code need not import them from a
submodule or from the driver; everything else lives in the modules below.

The autodoc options here are deliberately wider than on the module pages: seven
of the eight root exports are *imported* members (`imported-members`) and
`__version__` is a special data member (`special-members`), so without them
Sphinx would silently render an empty page for a module whose entire public
surface is re-exports.  The options are local to this page — the module pages
keep documenting exactly their own `__all__`.

```{eval-rst}
.. automodule:: psycodict
   :imported-members:
   :special-members: __version__
```
