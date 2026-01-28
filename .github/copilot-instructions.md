# Copilot Instructions for psycodict

## Project Overview

psycodict is a dictionary-based Python interface to PostgreSQL databases, originally developed for the [L-functions and Modular Forms Database (LMFDB)](https://www.lmfdb.org). It provides a high-level query interface built on top of psycopg2.

## Core Purpose

This library enables users to:
- Create SQL SELECT queries using Python dictionaries instead of raw SQL
- Manage data import/export with PostgreSQL
- Store and query statistics efficiently
- Access PostgreSQL array and JSONB types with a Pythonic interface

## Repository Structure

### Main Package: `/psycodict`
- `__init__.py` - Main module exports
- `database.py` - PostgresDatabase class, connection management
- `base.py` - PostgresBase class, core functionality
- `table.py` - PostgresTable class for table operations
- `searchtable.py` - PostgresSearchTable for query operations
- `statstable.py` - PostgresStatsTable for statistics
- `encoding.py` - JSON and numeric type handling for PostgreSQL
- `utils.py` - Utility functions (DelayCommit, etc.)
- `config.py` - Configuration management

### Documentation
- `README.md` - Installation and basic usage
- `QueryLanguage.md` - Comprehensive query language specification
- `LICENSE` - GPL v2+ license

## Key Technologies and Dependencies

- **Python 3.x** - Primary language
- **psycopg2** - PostgreSQL adapter for Python (required dependency)
  - Available as `psycopg2` (source) or `psycopg2-binary` (binary distribution)
- **PostgreSQL** - Target database system
- **Sage** (optional) - SageMath integration for mathematical types

## Coding Patterns and Conventions

### 1. Database Connections
- Use the `setup_connection()` function to properly configure psycopg2 connections
- Unicode encoding is enforced (UTF8)
- Custom type adapters are registered for numeric types and JSON

### 2. Query Language
- Queries are dictionaries where keys are column names
- Special keys start with `$` (e.g., `$or`, `$and`, `$not`, `$gte`, `$lt`)
- Column parts can be accessed with dot notation (e.g., `ainvs.2` for array elements)
- See `QueryLanguage.md` for complete specification

### 3. Type Handling
- Custom JSON encoder in `encoding.py` handles PostgreSQL's JSONB type
- Numeric types are converted using custom converters
- Array types are supported with proper type casting
- Optional Sage integration for mathematical types (Integer, RealNumber)

### 4. Code Style
- UTF-8 encoding declarations at top of files (`# -*- coding: utf-8 -*-`)
- Docstrings with EXAMPLES sections (Sage-style)
- Logging via Python's logging module
- Use of psycopg2.sql for query composition (SQL, Identifier, Placeholder)

## Testing

This repository does not appear to have a formal test suite in the codebase. When making changes:
- Verify functionality manually with PostgreSQL
- Test query generation for correctness
- Ensure backward compatibility with LMFDB usage patterns

## Common Tasks

### Adding New Query Operators
1. Update the query parser in relevant table classes
2. Document in `QueryLanguage.md`
3. Ensure SQL injection safety using psycopg2.sql composition

### Modifying Type Handling
1. Update `encoding.py` for new type conversions
2. Register adapters in `database.py`'s `setup_connection()`
3. Test with actual PostgreSQL types

### Configuration Changes
- Configuration is handled via `config.py`
- Default config in `config.ini`
- Support for database connection parameters

## Important Considerations

1. **SQL Injection Safety**: Always use psycopg2's SQL composition tools (SQL, Identifier, Placeholder)
2. **Unicode Handling**: All text should be Unicode; UTF-8 is enforced
3. **Database State**: Be mindful of connection state and transactions
4. **LMFDB Compatibility**: Changes should not break LMFDB's usage patterns
5. **Optional Dependencies**: Handle missing Sage gracefully with try/except

## Documentation Standards

- Keep `README.md` updated for user-facing changes
- Update `QueryLanguage.md` for query syntax changes
- Use docstrings with EXAMPLES sections (following Sage conventions)
- Include inline comments for complex logic

## Getting Started for Development

1. Install PostgreSQL
2. Create a test database and user
3. Install psycodict: `pip3 install -U "psycodict[pgbinary] @ git+https://github.com/roed314/psycodict.git"`
4. Test with basic queries as shown in README.md

## Related Resources

- PostgreSQL documentation: https://www.postgresql.org/docs/
- psycopg2 documentation: https://www.psycopg.org/docs/
- LMFDB project: https://www.lmfdb.org
