# psycodict: a dictionary-based python interface to an PostgreSQL database

This project was split off from the [L-functions and modular forms database](https://www.lmfdb.org)
so that other projects could use the SQL interface that we created for that project.

Built upon [psycopg2](https://pypi.org/project/psycopg2/), the core of the interface is the ability to create
SELECT queries using a dictionary.  In addition, the package provides a number of other features that were useful for the LMFDB:

 * Data management tools wrapping PostgreSQL's mechanisms for loading from and saving to files
 * Statistics tables for storing statistics and counts (this is particularly useful in the LMFDB's context since the data changes rarely)
 
