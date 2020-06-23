#!/usr/bin/env python3

from setuptools import setup, find_packages

setup(
    name="psycodict",
    version="0.1.1",
    packages=find_packages(),
    install_requires=["psycopg2>=2.7"],

    # metadata for PyPI. setuptools only support single authors
    license="GPL v2+",
    author="David Roe",
    author_email="roed.math@gmail.com",
    description="dictionary-based python interface to PostgreSQL databases",
    keywords="postgres database interface",
    url="https://github.com/roed314/psycodict"
)
