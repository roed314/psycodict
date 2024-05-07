# psycodict: dictionary-based python interface to PostgreSQL databases

This project was split off from the [L-functions and modular forms database](https://www.lmfdb.org)
so that other projects could use the SQL interface that we created for that project.

Built upon [psycopg2](https://pypi.org/project/psycopg2/), the core of the interface is the ability to create
SELECT queries using a dictionary.  In addition, the package provides a number of other features that were useful for the LMFDB:

 * Data management tools wrapping PostgreSQL's mechanisms for loading from and saving to files
 * Statistics tables for storing statistics and counts (this is particularly useful in the LMFDB's context since the data changes rarely)

# Install

```
pip3 install -U git+https://github.com/roed314/psycodict.git@toml#egg=project[pgbinary]
```
or
```
pip3 install -U git+https://github.com/roed314/psycodict.git@toml#egg=project[pgsource]
```

# Getting started

You will first need to install [postgres](https://www.postgresql.org/) and create a [user](https://www.postgresql.org/docs/current/sql-createuser.html) and a [database](https://www.postgresql.org/docs/current/sql-createdatabase.html).  For example, you might execute the following commands in psql:

    CREATE DATABASE database_name;
    CREATE USER username;
    ALTER USER psetpartners WITH password 'good password';
    GRANT ALL PRIVILEGES ON DATABASE database_name TO username;

