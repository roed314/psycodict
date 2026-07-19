# -*- coding: utf-8 -*-
"""
Tests for :mod:`psycodict.config`.

``Configuration`` merges an argument parser, a config file and a secrets file,
and writes the config file when it is missing, so every test here points it at
``tmp_path``; nothing is ever written to the repository or the cwd.
"""
import argparse
import os
import sys
import types
from configparser import ConfigParser

import pytest

from psycodict.config import Configuration, strbool


@pytest.fixture
def paths(tmp_path):
    """
    Throwaway locations for the config and secrets files.
    """
    return {
        "config_file": str(tmp_path / "config.ini"),
        "secrets_file": str(tmp_path / "secrets.ini"),
    }


def write_ini(path, sections):
    parser = ConfigParser()
    for section, options in sections.items():
        parser.add_section(section)
        for key, value in options.items():
            parser.set(section, key, str(value))
    with open(path, "w") as handle:
        parser.write(handle)


def read_ini(path):
    parser = ConfigParser()
    parser.read(path)
    return parser


def make_parser(paths, **kwargs):
    """
    A minimal custom parser: Configuration requires the two file arguments.
    """
    parser = argparse.ArgumentParser(description="test parser")
    parser.add_argument("-c", "--config-file", dest="config_file",
                        default=paths["config_file"])
    parser.add_argument("-s", "--secrets-file", dest="secrets_file",
                        default=paths["secrets_file"])
    parser.add_argument("--postgresql-host", dest="postgresql_host",
                        default=kwargs.get("host", "customhost"))
    parser.add_argument("--postgresql-port", dest="postgresql_port", type=int,
                        default=kwargs.get("port", 9999))
    return parser


# ---------------------------------------------------------------------------
# creating and reading the config file
# ---------------------------------------------------------------------------

def test_creates_config_file_with_defaults_when_absent(paths):
    assert not os.path.exists(paths["config_file"])
    config = Configuration(defaults=paths, readargs=False)
    assert os.path.exists(paths["config_file"])

    written = read_ini(paths["config_file"])
    assert sorted(written.sections()) == ["logging", "postgresql"]
    assert written.get("postgresql", "host") == "localhost"
    assert written.get("postgresql", "port") == "5432"
    assert written.get("logging", "slowcutoff") == "0.1"
    # and the object agrees with what it just wrote
    assert config.options["postgresql"]["host"] == "localhost"


def test_the_written_file_omits_the_file_locations(paths):
    # config_file/secrets_file are deleted from the defaults before writing:
    # recording them inside the file they name would be circular.
    Configuration(defaults=paths, readargs=False)
    with open(paths["config_file"]) as handle:
        text = handle.read()
    assert "config_file" not in text
    assert "secrets_file" not in text
    assert paths["secrets_file"] not in text


def test_reads_an_existing_config_file(paths):
    write_ini(paths["config_file"], {
        "postgresql": {"host": "filehost", "port": 1234, "user": "fileuser",
                       "password": "filepass", "dbname": "filedb"},
        "logging": {"slowcutoff": 3.5, "slowlogfile": "file.log"},
    })
    config = Configuration(defaults=paths, readargs=False)
    assert config.options["postgresql"] == {
        "host": "filehost", "port": 1234, "user": "fileuser",
        "password": "filepass", "dbname": "filedb",
    }
    assert config.options["logging"] == {"slowcutoff": 3.5, "slowlogfile": "file.log"}


def test_an_existing_config_file_is_left_untouched(paths):
    write_ini(paths["config_file"], {"postgresql": {"host": "filehost"}})
    with open(paths["config_file"]) as handle:
        before = handle.read()
    Configuration(defaults=paths, readargs=False)
    with open(paths["config_file"]) as handle:
        assert handle.read() == before


def test_keys_missing_from_the_config_file_fall_back_to_defaults(paths):
    write_ini(paths["config_file"], {"postgresql": {"host": "filehost"}})
    config = Configuration(defaults=paths, readargs=False)
    assert config.options["postgresql"]["host"] == "filehost"
    assert config.options["postgresql"]["port"] == 5432
    assert config.options["logging"]["slowcutoff"] == 0.1


# ---------------------------------------------------------------------------
# defaults=
# ---------------------------------------------------------------------------

def test_defaults_replace_the_builtin_values(paths):
    config = Configuration(defaults=dict(
        paths,
        postgresql_host="db.example.com",
        postgresql_port=6543,
        postgresql_user="alice",
        logging_slowcutoff=2.5,
        logging_slowlogfile="slow.log",
    ), readargs=False)
    assert config.options["postgresql"]["host"] == "db.example.com"
    assert config.options["postgresql"]["port"] == 6543
    assert config.options["postgresql"]["user"] == "alice"
    assert config.options["logging"]["slowcutoff"] == 2.5
    assert config.options["logging"]["slowlogfile"] == "slow.log"


def test_the_config_file_wins_over_defaults(paths):
    write_ini(paths["config_file"], {"postgresql": {"host": "filehost"}})
    config = Configuration(
        defaults=dict(paths, postgresql_host="defaulthost"), readargs=False
    )
    assert config.options["postgresql"]["host"] == "filehost"


@pytest.mark.xfail(strict=True, reason="config.py reads defaults['postgres_password'], "
                                       "not 'postgresql_password'")
def test_postgresql_password_default_is_honoured(paths):
    config = Configuration(
        defaults=dict(paths, postgresql_password="hunter2"), readargs=False
    )
    assert config.options["postgresql"]["password"] == "hunter2"


# ---------------------------------------------------------------------------
# the secrets file
# ---------------------------------------------------------------------------

def test_secrets_file_overrides_the_config_file(paths):
    write_ini(paths["config_file"], {
        "postgresql": {"host": "filehost", "user": "fileuser", "password": "filepass"},
    })
    write_ini(paths["secrets_file"], {
        "postgresql": {"user": "secretuser", "password": "s3cret"},
    })
    config = Configuration(defaults=paths, readargs=False)
    assert config.options["postgresql"]["password"] == "s3cret"
    assert config.options["postgresql"]["user"] == "secretuser"
    # values the secrets file says nothing about are untouched
    assert config.options["postgresql"]["host"] == "filehost"


def test_a_missing_secrets_file_is_not_an_error(paths):
    write_ini(paths["config_file"], {"postgresql": {"host": "filehost"}})
    assert not os.path.exists(paths["secrets_file"])
    config = Configuration(defaults=paths, readargs=False)
    assert config.options["postgresql"]["host"] == "filehost"
    # ... and it is not created either
    assert not os.path.exists(paths["secrets_file"])


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

def test_values_are_coerced_using_the_parser_types(paths):
    write_ini(paths["config_file"], {
        "postgresql": {"host": "filehost", "port": "1234"},
        "logging": {"slowcutoff": "2.5"},
    })
    config = Configuration(defaults=paths, readargs=False)
    assert config.options["postgresql"]["port"] == 1234
    assert type(config.options["postgresql"]["port"]) is int
    assert config.options["logging"]["slowcutoff"] == 2.5
    assert type(config.options["logging"]["slowcutoff"]) is float
    # untyped arguments stay strings
    assert type(config.options["postgresql"]["host"]) is str


def test_get_postgresql_default_reports_the_parser_defaults(paths):
    write_ini(paths["config_file"], {
        "postgresql": {"host": "filehost", "port": "1234", "user": "fileuser"},
    })
    config = Configuration(
        defaults=dict(paths, postgresql_host="defaulthost"), readargs=False
    )
    defaults = config.get_postgresql_default()
    # deliberately the defaults, not the file: this is what a fresh config
    # file would be written with
    assert defaults["host"] == "defaulthost"
    assert defaults["user"] == "postgres"
    assert defaults["port"] == 5432
    assert type(defaults["port"]) is int
    assert config.options["postgresql"]["host"] == "filehost"
    # the caller gets a copy, not the object's own state
    defaults["host"] = "mutated"
    assert config.get_postgresql_default()["host"] == "defaulthost"


# ---------------------------------------------------------------------------
# strbool
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("true", True), ("True", True), ("TRUE", True), ("t", True), ("T", True),
    ("yes", True), ("Y", True), ("y", True),
    ("false", False), ("False", False), ("FALSE", False), ("f", False), ("F", False),
    ("no", False), ("N", False), ("n", False),
])
def test_strbool_accepted_spellings(text, expected):
    assert strbool(text) is expected


@pytest.mark.parametrize("text", ["", "1", "0", "on", "off", "maybe", "None"])
def test_strbool_rejects_anything_else(text):
    with pytest.raises(ValueError):
        strbool(text)


def test_store_true_options_are_coerced_by_strbool(paths):
    write_ini(paths["config_file"], {"misc": {"verbose": "yes"}})
    parser = make_parser(paths)
    parser.add_argument("--verbose", dest="misc_verbose", action="store_true")
    config = Configuration(parser=parser, readargs=False)
    assert config.options["misc"]["verbose"] is True


@pytest.mark.xfail(strict=True, reason="get() looks the type up under "
                                       "'<section>_<option>', so a dest without an "
                                       "underscore (section 'misc') never matches "
                                       "and strbool is skipped, leaving the truthy "
                                       "string 'False'")
def test_store_true_options_with_a_plain_dest_are_coerced(paths):
    parser = make_parser(paths)
    parser.add_argument("--quiet", dest="quiet", action="store_true")
    config = Configuration(parser=parser, readargs=False)
    assert config.options["misc"]["quiet"] is False


# ---------------------------------------------------------------------------
# readargs / the command line
# ---------------------------------------------------------------------------

def test_readargs_false_ignores_sys_argv(paths, monkeypatch):
    # argparse would exit on these; readargs=False must never look at them.
    monkeypatch.setattr(sys, "argv", ["prog", "--no-such-option", "zzz", "extra"])
    config = Configuration(defaults=paths, readargs=False)
    assert config.options["postgresql"]["host"] == "localhost"


def test_readargs_true_parses_sys_argv(paths, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "prog", "--postgresql-host", "cli.example.com", "--postgresql-port", "7777",
    ])
    config = Configuration(defaults=paths, readargs=True)
    assert config.options["postgresql"]["host"] == "cli.example.com"
    assert config.options["postgresql"]["port"] == 7777


def test_command_line_values_beat_the_config_file(paths, monkeypatch):
    write_ini(paths["config_file"], {"postgresql": {"host": "filehost", "user": "fileuser"}})
    monkeypatch.setattr(sys, "argv", ["prog", "--postgresql-host", "cli.example.com"])
    config = Configuration(defaults=paths, readargs=True)
    assert config.options["postgresql"]["host"] == "cli.example.com"
    # arguments left at their default still come from the file
    assert config.options["postgresql"]["user"] == "fileuser"


@pytest.mark.parametrize("has_file,expected", [(True, "cli.example.com"), (False, "localhost")])
def test_readargs_defaults_to_whether_main_is_a_script(paths, monkeypatch, has_file, expected):
    main = types.ModuleType("fake_main")
    if has_file:
        main.__file__ = "script.py"
    monkeypatch.setitem(sys.modules, "__main__", main)
    monkeypatch.setattr(sys, "argv", ["prog", "--postgresql-host", "cli.example.com"])
    config = Configuration(defaults=paths)
    assert config.options["postgresql"]["host"] == expected


def test_extra_options_holds_the_arguments_not_stored_in_the_file(paths):
    config = Configuration(defaults=paths, readargs=False)
    assert config.extra_options == {
        "config_file": paths["config_file"],
        "secrets_file": paths["secrets_file"],
    }


# ---------------------------------------------------------------------------
# a supplied parser
# ---------------------------------------------------------------------------

def test_a_supplied_parser_replaces_the_builtin_one(paths):
    parser = make_parser(paths)
    parser.add_argument("--extra-thing", dest="extra_thing", default="e")
    config = Configuration(parser=parser, readargs=False)
    assert config.options["postgresql"] == {"host": "customhost", "port": 9999}
    assert config.options["extra"] == {"thing": "e"}
    # the builtin arguments are gone
    assert "dbname" not in config.options["postgresql"]
    assert "logging" not in config.options


def test_a_supplied_parser_is_used_for_the_command_line(paths, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog", "--postgresql-port", "4321"])
    config = Configuration(parser=make_parser(paths), readargs=True)
    assert config.options["postgresql"]["port"] == 4321
    assert config.options["postgresql"]["host"] == "customhost"


# ---------------------------------------------------------------------------
# writeargstofile
# ---------------------------------------------------------------------------

def test_writeargstofile_saves_the_command_line(paths, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog", "--postgresql-host", "written.example.com"])
    config = Configuration(defaults=paths, writeargstofile=True, readargs=True)
    assert read_ini(paths["config_file"]).get("postgresql", "host") == "written.example.com"
    assert config.options["postgresql"]["host"] == "written.example.com"


def test_without_writeargstofile_the_file_gets_the_defaults(paths, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog", "--postgresql-host", "cli.example.com"])
    config = Configuration(defaults=paths, writeargstofile=False, readargs=True)
    # the file records the defaults, so editing it is how you change them ...
    assert read_ini(paths["config_file"]).get("postgresql", "host") == "localhost"
    # ... while this run still uses the command line
    assert config.options["postgresql"]["host"] == "cli.example.com"
