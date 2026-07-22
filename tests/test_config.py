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


def test_readargs_defaults_to_false_even_in_a_script(paths, monkeypatch):
    # The command line belongs to the host program: psycodict must not parse
    # it unless asked.  It used to auto-detect "running as a script" (via
    # __main__.__file__) and then argparse would reject the host's own
    # options with a SystemExit; pin that both are gone.
    main = types.ModuleType("fake_main")
    main.__file__ = "script.py"
    monkeypatch.setitem(sys.modules, "__main__", main)
    monkeypatch.setattr(sys, "argv", ["prog", "--no-such-option", "--postgresql-host", "cli.example.com"])
    config = Configuration(defaults=paths)
    assert config.options["postgresql"]["host"] == "localhost"


def test_extra_options_holds_the_arguments_not_stored_in_the_file(paths):
    config = Configuration(defaults=paths, readargs=False)
    assert config.extra_options == {
        "config_file": paths["config_file"],
        "secrets_file": paths["secrets_file"],
    }


# ---------------------------------------------------------------------------
# file discovery
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """
    An isolated environment for the discovery tests: an empty working
    directory, a throwaway HOME, and no PSYCODICT_CONFIG.

    Returns the fake home directory (so ``~/.psycodict`` resolves under it).
    """
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSYCODICT_CONFIG", raising=False)
    return home


def test_discovery_falls_back_to_the_psycodict_home(isolated):
    config = Configuration(readargs=False)
    created = isolated / ".psycodict" / "config.ini"
    assert created.exists()
    assert config.extra_options["config_file"] == str(created)
    assert config.options["postgresql"]["host"] == "localhost"
    # ... and nothing was created in the working directory
    assert not os.path.exists("config.ini")


def test_discovery_uses_an_existing_cwd_config(isolated):
    write_ini("config.ini", {"postgresql": {"host": "cwdhost"}})
    config = Configuration(readargs=False)
    assert config.options["postgresql"]["host"] == "cwdhost"
    assert config.extra_options["config_file"] == os.path.abspath("config.ini")
    # the fallback location is not touched
    assert not (isolated / ".psycodict").exists()


def test_discovery_env_var_beats_the_cwd_config(isolated, tmp_path, monkeypatch):
    write_ini("config.ini", {"postgresql": {"host": "cwdhost"}})
    target = tmp_path / "elsewhere" / "psycodict.ini"
    monkeypatch.setenv("PSYCODICT_CONFIG", str(target))
    config = Configuration(readargs=False)
    # created at the environment location, with the defaults
    assert target.exists()
    assert config.extra_options["config_file"] == str(target)
    assert config.options["postgresql"]["host"] == "localhost"


def test_an_explicit_config_file_beats_the_environment(isolated, tmp_path, monkeypatch, paths):
    monkeypatch.setenv("PSYCODICT_CONFIG", str(tmp_path / "env.ini"))
    config = Configuration(defaults=paths, readargs=False)
    assert config.extra_options["config_file"] == paths["config_file"]
    assert not (tmp_path / "env.ini").exists()


def test_the_default_secrets_file_sits_next_to_the_config_file(isolated):
    confdir = isolated / ".psycodict"
    confdir.mkdir()
    write_ini(str(confdir / "secrets.ini"), {"postgresql": {"password": "s3cret"}})
    config = Configuration(readargs=False)
    assert config.extra_options["secrets_file"] == str(confdir / "secrets.ini")
    assert config.options["postgresql"]["password"] == "s3cret"


# ---------------------------------------------------------------------------
# the slow-query log location
# ---------------------------------------------------------------------------


def test_the_default_slowlogfile_lands_in_logs_next_to_the_config(isolated):
    config = Configuration(readargs=False)
    logdir = isolated / ".psycodict" / "logs"
    assert config.options["logging"]["slowlogfile"] == str(logdir / "slow_queries.log")
    # PostgresDatabase attaches a FileHandler immediately, so the directory
    # must already exist
    assert logdir.is_dir()
    # the file itself records the default name; the redirect is applied when
    # the configuration is read, so editing the file still works
    written = read_ini(str(isolated / ".psycodict" / "config.ini"))
    assert written.get("logging", "slowlogfile") == "slow_queries.log"


def test_an_explicit_slowlogfile_is_respected(isolated, tmp_path):
    target = str(tmp_path / "my_slow.log")
    config = Configuration(
        defaults={"logging_slowlogfile": target}, readargs=False
    )
    assert config.options["logging"]["slowlogfile"] == target


def test_a_slowlogfile_from_the_config_file_is_respected(isolated, tmp_path):
    target = str(tmp_path / "configured.log")
    write_ini("config.ini", {"logging": {"slowlogfile": target}})
    config = Configuration(readargs=False)
    assert config.options["logging"]["slowlogfile"] == target
    assert not os.path.exists("logs")


def test_the_default_name_in_a_config_file_is_also_redirected(isolated):
    # An existing file that kept the default name gets the same treatment as
    # a fresh one: the name is treated as "unconfigured", not as a request
    # for a file of that name in the working directory.
    write_ini("config.ini", {"logging": {"slowlogfile": "slow_queries.log"}})
    config = Configuration(readargs=False)
    expected = os.path.join(os.path.abspath("logs"), "slow_queries.log")
    assert config.options["logging"]["slowlogfile"] == expected


def test_a_supplied_parser_gets_no_log_redirect(isolated, paths):
    # Callers with their own parser own their logging semantics.
    parser = make_parser(paths)
    parser.add_argument("--slowlogfile", dest="logging_slowlogfile",
                        default="slow_queries.log")
    config = Configuration(parser=parser, readargs=False)
    assert config.options["logging"]["slowlogfile"] == "slow_queries.log"


needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions",
)


@pytest.fixture
def read_only_config(isolated, tmp_path, monkeypatch):
    """
    A readable configuration file in a directory without write access, the
    shape of a system-managed /etc/psycodict/config.ini.  Restores the
    permissions afterward so tmp_path can be cleaned up.
    """
    confdir = tmp_path / "etc"
    confdir.mkdir()
    write_ini(str(confdir / "config.ini"), {"postgresql": {"host": "etchost"}})
    confdir.chmod(0o555)
    monkeypatch.setenv("PSYCODICT_CONFIG", str(confdir / "config.ini"))
    yield confdir
    confdir.chmod(0o755)


@needs_permissions
def test_a_config_in_a_read_only_directory_is_readable(isolated, read_only_config):
    # Reading a configuration must not require write access beside it; the
    # default slow log falls back to ~/.psycodict/logs.
    config = Configuration(readargs=False)
    assert config.options["postgresql"]["host"] == "etchost"
    expected = os.path.join(str(isolated), ".psycodict", "logs", "slow_queries.log")
    assert config.options["logging"]["slowlogfile"] == expected
    assert os.path.isdir(os.path.dirname(expected))


@needs_permissions
def test_no_usable_log_directory_keeps_the_plain_name(isolated, read_only_config, monkeypatch):
    # With the config directory read-only and no home either, the redirect
    # gives up and the historical cwd-relative default survives.
    monkeypatch.setattr(os.path, "expanduser", lambda path: path)
    config = Configuration(readargs=False)
    assert config.options["logging"]["slowlogfile"] == "slow_queries.log"


# ---------------------------------------------------------------------------
# missing home directory
# ---------------------------------------------------------------------------


def test_no_home_directory_is_a_clear_error_not_a_tilde_directory(isolated, monkeypatch):
    # In a container with HOME unset and a uid without a passwd entry,
    # expanduser returns the literal ~.  The home fallback must refuse
    # loudly instead of creating ./~/.psycodict in the working directory.
    monkeypatch.setattr(os.path, "expanduser", lambda path: path)
    with pytest.raises(RuntimeError, match="PSYCODICT_CONFIG"):
        Configuration(readargs=False)
    assert not os.path.exists("~")


def test_a_cwd_config_needs_no_home_directory(isolated, monkeypatch):
    # The error is confined to the home fallback: with a discoverable
    # configuration the homeless container works fine (and the log lands
    # next to the config, not in a literal ~).
    write_ini("config.ini", {"postgresql": {"host": "cwdhost"}})
    monkeypatch.setattr(os.path, "expanduser", lambda path: path)
    config = Configuration(readargs=False)
    assert config.options["postgresql"]["host"] == "cwdhost"
    expected = os.path.join(os.path.abspath("logs"), "slow_queries.log")
    assert config.options["logging"]["slowlogfile"] == expected
    assert not os.path.exists("~")


# ---------------------------------------------------------------------------
# postgresql_dbname
# ---------------------------------------------------------------------------


def test_postgresql_dbname_default_is_honoured(paths):
    # dbname used to be the one option whose default ignored the defaults
    # dictionary.
    config = Configuration(
        defaults=dict(paths, postgresql_dbname="mydb"), readargs=False
    )
    assert config.options["postgresql"]["dbname"] == "mydb"
    assert read_ini(paths["config_file"]).get("postgresql", "dbname") == "mydb"


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
