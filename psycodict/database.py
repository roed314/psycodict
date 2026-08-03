# -*- coding: utf-8 -*-
"""
The connection object at the center of psycodict.

:class:`PostgresDatabase` connects using a
:class:`~psycodict.config.Configuration` and exposes each table in the
database as an attribute (``db.nf_fields``), a
:class:`~psycodict.searchtable.PostgresSearchTable`.  It also provides the
database-wide operations: creating, dropping and renaming tables,
reloading or cleaning up every table at once, schema-change notifications
(:meth:`~PostgresDatabase.listener`), refreshing table metadata without a
restart (:meth:`~PostgresDatabase.refresh_tables`), and the metadata
format stamp and its migrations.
"""
import csv
import logging
from pathlib import Path
import time
import traceback
from collections import defaultdict, Counter

from typing import NamedTuple

from psycopg import connect, DatabaseError
from psycopg.errors import UndefinedTable
from psycopg.sql import SQL, Identifier, Placeholder
from psycopg.adapt import Loader
from psycopg.types.json import set_json_loads

from .encoding import (
    Json,
    numeric_converter,
    Array,
    ArrayDumper,
    JsonWrapperDumper,
    DictJsonDumper,
)
from .base import (
    PostgresBase,
    META_FORMAT,
    _meta_tables_cols,
    _meta_tables_defaults,
    _meta_cols_types_jsonb_idx,
)
from .validation import (
    InvalidDefinitionError,
    check_new_table_name,
    derived_identifier,
    physical_table_name,
    validate_search_table_name,
    validate_search_table_registry,
)
from .grants import GRANT_ACTIONS, GrantPolicy
from .searchtable import PostgresSearchTable
from .utils import DelayCommit, safe_child_path

# The registry of metadata-format migrations.  Entry N describes the step
# from format N-1 to format N; MetadataFormats.md has the checklist a new
# format must follow.
#
#   description -- one line, shown when the step is applied
#   compatible  -- whether a psycodict expecting format >= N still operates
#                  against a database at format < N (features introduced by
#                  the new format degrade gracefully, nothing else changes).
#                  Decides warn-and-proceed versus refuse at connect time.
#   min_compat  -- stamped into meta_format alongside the version: the oldest
#                  META_FORMAT a psycodict may have and still safely use a
#                  database at this format.  Raised to N whenever a client that
#                  predates format N would *mishandle* the new metadata rather
#                  than merely miss it -- which includes a purely additive
#                  column when older write paths would silently drop it (as an
#                  older restore_index/reload would drop a whereclause), not
#                  only outright breaking changes.
#   upgrade     -- name of the PostgresDatabase method performing the DDL
#                  (just the DDL, idempotently: stamping is done by
#                  upgrade_metadata itself).
META_MIGRATIONS = {
    1: {
        "description": "meta_indexes/meta_indexes_hist gain a whereclause column (partial indexes)",
        "compatible": True,
        # A format-0 client reads a format-1 database fine, but its
        # restore_index/reload would silently rebuild a partial index as a
        # full one and drop the predicate, so it must not *write* one: the
        # oldest format that can safely use a format-1 database is 1 itself.
        "min_compat": 1,
        "upgrade": "_upgrade_meta_0_to_1",
    },
}


def _stamped_min_compat(version):
    """
    The min_compat that stamping ``version`` should record (format 0 predates
    both the stamp and the registry, hence the default).
    """
    return META_MIGRATIONS[version]["min_compat"] if version in META_MIGRATIONS else 0


class NumericLoader(Loader):
    """
    Loads Postgres numeric values through :func:`numeric_converter`
    (Sage Integer/RealLiteral when Sage is available, int/float otherwise).
    """
    def load(self, data):
        """
        Convert the numeric's text representation to a Python/Sage number.
        """
        return numeric_converter(bytes(data).decode())


class _ConnectionCapabilities(NamedTuple):
    """
    What a session is allowed to do: the answers ``_detect_capabilities`` reads
    from one connection, kept together so that they are adopted together.
    """

    read_only: bool
    super_user: bool
    read_and_write_knowls: bool
    read_and_write_userdb: bool


def setup_connection(conn):
    """
    Prepare a fresh psycopg connection for psycodict: set the client
    encoding and register the loaders and dumpers that implement
    psycodict's value conversion (numerics, json, arrays, and -- when Sage
    is available -- Sage integers and reals).  Called for every connection
    the database opens.
    """
    # psycopg3 uses unicode (str) everywhere by default, so the psycopg2-era
    # UNICODE/UNICODEARRAY registrations are no longer needed.
    conn.execute("SET client_encoding TO 'UTF8'")
    conn.commit()
    # All registrations below are per-connection (psycopg3 improvement over
    # psycopg2's process-global register_adapter).
    conn.adapters.register_loader("numeric", NumericLoader)
    conn.adapters.register_dumper(dict, DictJsonDumper)
    conn.adapters.register_dumper(Json, JsonWrapperDumper)
    conn.adapters.register_dumper(Array, ArrayDumper)
    set_json_loads(Json.loads, conn)
    try:
        # RealNumber must come from real_mpfr: sage.all.RealNumber is the
        # create_RealNumber factory function (not a class), and dumpers can
        # only be registered on classes
        from sage.rings.integer import Integer
        from sage.rings.real_mpfr import RealNumber
        from .encoding import RealLiteralDumper, SageIntegerDumper, LmfdbRealLiteral
    except ImportError:
        pass
    else:
        conn.adapters.register_dumper(Integer, SageIntegerDumper)
        conn.adapters.register_dumper(RealNumber, RealLiteralDumper)
        conn.adapters.register_dumper(LmfdbRealLiteral, RealLiteralDumper)


class PostgresDatabase(PostgresBase):
    """
    The interface to the postgres database.

    It creates and stores the global connection object,
    and collects the table interfaces.

    A single psycopg connection is shared by this database object and every
    table interface registered on it (see ``register_object`` and
    ``reset_connection``).  The psycopg connection itself is thread-safe --
    it serializes concurrent cursor use with an internal lock -- but sharing
    one connection means sharing one transaction and session state, and
    psycodict layers unsynchronized mutable bookkeeping on top (the
    commit-deferral stack behind ``DelayCommit``, the server-side cursor
    counter, the per-object connection references reset together).  So a
    ``PostgresDatabase`` instance is not thread-safe; use one instance per
    process or thread (this is how LMFDB deploys it).

    INPUT:

    - ``create`` -- if True, create psycodict's metadata tables (meta_tables,
      meta_indexes, meta_constraints and their _hist counterparts) when they
      are missing, allowing use of a fresh database
    - ``upgrade`` -- if True, migrate the metadata tables to the format this
      psycodict implements before connecting (see upgrade_metadata and
      MetadataFormats.md).  Without it, a database using an older but
      compatible metadata format connects with a warning and operates at the
      older format.
    - ``session_settings`` -- a dictionary of PostgreSQL session settings
      (``statement_timeout``, ``lock_timeout``,
      ``idle_in_transaction_session_timeout``, ``application_name``) applied to
      the main database connection and to every replacement main connection.
      Defaults to a 25 second statement timeout when connecting as
      ``webserver``, and to nothing otherwise.  A notification listener's
      connection deliberately does not inherit them: it exists to wait, and a
      statement timeout on it would be more likely to be wrong than right.  It
      does inherit the connection overrides below, so that it reaches the same
      server and database as the sender.
    - ``**kwargs`` -- passed on to psycopg's connect method.  These override the
      configured options for every connection this database opens, so TLS
      settings, an alternate host or database, and timeouts given here survive
      a reconnect.

    ATTRIBUTES:

    The following public attributes are stored on the db object.

    - ``server_side_counter`` -- an integer tracking how many buffered connections have been created
    - ``conn`` -- the psycopg connection object
    - ``tables`` -- the search tables, by name
    - ``tablenames`` -- the names of the search tables, sorted
    - ``meta_format`` -- the metadata format this connection operates at (see
      MetadataFormats.md)

    Each table is also reachable as an attribute, so that db.ec_curvedata works
    for example.  The tables themselves live in ``db.tables``, not in the
    instance dictionary: a table name comes out of meta_tables and must not be
    able to take the place of the connection or of a method, so anything the
    database object really has wins over a table of the same name.

    These table objects are snapshots: if another process later changes the schema
    (adding or dropping columns or tables), call ``refresh_tables`` to update them
    in place instead of restarting the process.

    EXAMPLES::

        >>> db
        Interface to Postgres database
        >>> db.conn
        <psycopg.Connection [IDLE] (host=... database=...) at 0x...>
        >>> 'test_fields' in db.tablenames
        True
        >>> db.test_fields
        Interface to Postgres table test_fields
    """
    # Override the following to use a different class for search tables
    _search_table_class_ = PostgresSearchTable

    # The session settings psycodict will apply, and what it applies to a
    # webserver connection when the caller asks for nothing else.  Settings are
    # applied with set_config, which takes the name and the value as bound
    # parameters; the closed set here is what keeps a caller from setting
    # something unrelated through the same door.
    _allowed_session_settings = (
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
        "application_name",
    )
    _webserver_session_settings = {"statement_timeout": "25s"}

    def _resolve_session_settings(self, session_settings):
        """
        The session settings to apply to every connection of this database.

        INPUT:

        - ``session_settings`` -- a dictionary of PostgreSQL settings, or None
          to take the default for the connecting role

        A connection as ``webserver`` has always been given a 25 second
        statement timeout; that is now the default rather than a special case
        applied once, so it survives a reconnect and can be overridden.  The
        settings apply to the main connection and its replacements, not to a
        listener's separate connection.
        """
        if session_settings is None:
            user = self._connection_options().get("user")
            session_settings = (
                self._webserver_session_settings if user == "webserver" else {}
            )
        unknown = set(session_settings) - set(self._allowed_session_settings)
        if unknown:
            raise ValueError(
                "psycodict will not set the session setting(s) %s; it applies "
                "%s" % (", ".join(sorted(unknown)),
                        ", ".join(self._allowed_session_settings))
            )
        return dict(session_settings)

    def _connection_options(self):
        """
        The options every connection this database opens is made with.

        The configured ``postgresql`` options, overridden by the keyword
        arguments the constructor was given.  There is one of these rather than
        a ``_new_connection(**kwargs)`` because the overrides carry the
        connection's security: TLS mode and certificates, the host and database
        actually connected to, libpq ``options``, timeouts.  A reconnect that
        rebuilt the options without them would come back on a connection weaker
        than the one it replaced -- or to a different server.
        """
        options = dict(self.config.options["postgresql"])
        options.update(self._connect_kwargs)
        return options

    def _new_connection(self):
        """
        Open a connection with this database's options, and configure it.

        Every connection the database makes -- the first one and every
        replacement -- goes through here, so they are made alike.
        """
        options = self._connection_options()
        logging.info(
            "Connecting to PostgresSQL server as: user=%s host=%s port=%s dbname=%s..."
            % (
                options.get("user"),
                options.get("host"),
                options.get("port"),
                options.get("dbname"),
            )
        )
        connection = connect(**options)
        logging.info("Done!\n connection = %s" % connection)
        try:
            self._configure_session(connection)
        except Exception:
            # The caller never sees this connection, so nothing else can close
            # it; leaving it to the garbage collector holds a server slot for
            # as long as the traceback keeps the frame alive.
            try:
                connection.close()
            except Exception:
                logging.info(
                    "Could not close a connection that failed during setup",
                    exc_info=True,
                )
            raise
        return connection

    def _configure_session(self, conn):
        """
        Apply psycodict's per-session setup to a fresh connection.

        Everything a session needs beyond the connection itself: the type
        adapters, and the session settings (statement, lock and
        idle-in-transaction timeouts) that protect the server from this
        connection.  A replacement connection that skipped this would be a
        connection without those protections.

        This is for the main connection and its replacements.  A notification
        listener opens its own connection and does not come through here.

        Runs directly on ``conn`` rather than through ``_execute``, because it
        is called while recovering from a failure in ``_execute``.
        """
        # The following function controls how Python classes are converted to
        # strings for passing to Postgres, and how the results are decoded upon
        # extraction from the database.
        # Note that it has some global effects, since register_adapter
        # is not limited to just one connection
        setup_connection(conn)
        for name, value in self._session_settings.items():
            # set_config takes both as bound values, so nothing is interpolated
            conn.execute("SELECT set_config(%s, %s, false)", [name, str(value)])
        conn.commit()

    def _detect_capabilities(self, conn):
        """
        What a connection is allowed to do, read from the server.

        INPUT:

        - ``conn`` -- the connection to ask about, which may be a candidate
          this database has not adopted

        OUTPUT:

        A :class:`_ConnectionCapabilities`.  Nothing is written to ``self``:
        the answers are only about ``conn``, and a candidate that fails
        halfway through must not leave this object describing a session it
        never adopted.  ``_apply_capabilities`` is the one place they land.

        Called for the initial connection and again for every replacement: a
        reconnect can reach a standby, or a role whose grants have changed, and
        the previous session's answers would then be wrong in the unsafe
        direction.  Like ``_configure_session``, this runs directly on ``conn``.
        """
        user = conn.info.user

        def query(sql, args):
            return conn.execute(sql, args).fetchall()

        if query("SELECT pg_is_in_recovery()", [])[0][0]:
            read_only = True
        else:
            # Check if there is a table where we can insert/update
            privileges = ["INSERT", "UPDATE"]
            rows = query(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE grantee = %s AND table_schema = %s "
                "AND privilege_type IN (" + ",".join(["%s"] * len(privileges)) + ")",
                [user, "public"] + privileges,
            )
            read_only = rows[0][0] == 0

        super_user = query("SELECT current_setting('is_superuser')", [])[0][0] == "on"

        if read_only:
            read_and_write_knowls = False
            read_and_write_userdb = False
        elif super_user:
            read_and_write_knowls = True
            read_and_write_userdb = True
        else:
            privileges = ["INSERT", "SELECT", "UPDATE"]
            knowls_tables = ["kwl_knowls"]
            rows = sorted(query(
                "SELECT table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE grantee = %s AND table_name IN ("
                + ",".join(["%s"] * len(knowls_tables))
                + ") AND privilege_type IN ("
                + ",".join(["%s"] * len(privileges))
                + ")",
                [user] + knowls_tables + privileges,
            ))
            read_and_write_knowls = rows == sorted(
                [(table, priv) for table in knowls_tables for priv in privileges]
            )

            rows = sorted(query(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = %s AND table_schema = %s "
                "AND table_name = %s AND privilege_type IN ("
                + ",".join(["%s"] * len(privileges))
                + ")",
                [user, "userdb", "users"] + privileges,
            ))
            read_and_write_userdb = rows == sorted([(priv,) for priv in privileges])
        conn.commit()

        logging.info("User: %s", user)
        logging.info("Read only: %s", read_only)
        logging.info("Super user: %s", super_user)
        logging.info("Read/write to userdb: %s", read_and_write_userdb)
        logging.info("Read/write to knowls: %s", read_and_write_knowls)
        return _ConnectionCapabilities(
            read_only, super_user, read_and_write_knowls, read_and_write_userdb
        )

    def _apply_capabilities(self, capabilities):
        """
        Adopt a capability snapshot.

        The only place the four capability attributes are written, so that they
        change together or not at all.
        """
        self._read_only = capabilities.read_only
        self._super_user = capabilities.super_user
        self._read_and_write_knowls = capabilities.read_and_write_knowls
        self._read_and_write_userdb = capabilities.read_and_write_userdb

    def _verify_identity(self, conn):
        """
        Check that a replacement connection reached the same place as the one
        it replaces.

        The database and the authenticated role are what psycodict's cached
        view of the schema, and every permission decision made from it, are
        about, so a replacement that reaches a different database or
        authenticates as a different role is refused.

        That is the whole of the check: it compares the database name and the
        role name, and two different clusters can agree on both.  It is not a
        proof that the replacement reached the same server -- the host is
        deliberately not compared, since a failover to a standby is a
        legitimate way for it to change -- and it does not detect every DNS or
        connection-service redirection.
        """
        found = (conn.info.dbname, conn.info.user)
        if found != self._identity:
            raise RuntimeError(
                "Refusing to reconnect: expected to reach database %s as %s, "
                "but the new connection reached database %s as %s"
                % (self._identity + found)
            )

    def reset_connection(self):
        """
        Replace the connection, and everything that was set up on it.

        The replacement is made with the same options, configured with the same
        session settings, and checked to have reached the same database as the
        same role; only then does it replace the old one on this object and on
        every table registered with it.  Capability flags are recomputed from
        it, since they describe the session and not the object.
        """
        logging.info("Connection broken (status %s); resetting...", self.conn.closed)
        old = self.conn
        candidate = self._new_connection()
        try:
            self._verify_identity(candidate)
            capabilities = self._detect_capabilities(candidate)
        except Exception:
            # Nothing has been adopted: the old connection, the registered
            # objects and the capability flags are all as they were.
            candidate.close()
            raise

        # The candidate has passed everything, so it is adopted here -- the
        # connection and the capabilities together, before any hook runs, so
        # that a hook sees a consistent object.  Note that self is the first
        # entry in self._objects.
        for obj in self._objects:
            obj.conn = candidate
        self._apply_capabilities(capabilities)
        try:
            for obj in tuple(self._objects):
                obj._connection_reset()
        finally:
            if old is not None and not old.closed:
                try:
                    old.close()
                except Exception:
                    logging.info(
                        "Could not close the replaced connection", exc_info=True
                    )

    def _register_object(self, obj):
        """
        The database holds references to tables, etc so that connections can be refreshed if they fail.
        """
        obj.conn = self.conn
        self._objects.append(obj)

    def __init__(self, config=None, secretsfile=None, create=False, upgrade=False,
                 session_settings=None, grant_policy=None, **kwargs):
        if config is None:
            from .config import Configuration
            config = Configuration()
        self.config = config
        self.server_side_counter = 0
        self._nocommit_stack = 0
        self._silenced = False
        self._objects = []
        # The connection overrides passed here take precedence over config for
        # every connection this database opens, including the separate one a
        # listener() opens (otherwise it could subscribe on a different server
        # or database than the sender writes to).
        self._connect_kwargs = dict(kwargs)
        self._session_settings = self._resolve_session_settings(session_settings)
        # What the relations psycodict creates are readable and writable by.
        # The default grants nothing: a deployment that wants the LMFDB's roles
        # asks for them (see psycodict.grants).
        self.grant_policy = GrantPolicy() if grant_policy is None else grant_policy
        self.conn = self._new_connection()
        self._user = self.conn.info.user
        # What a replacement connection has to reach to be adopted.
        self._identity = (self.conn.info.dbname, self.conn.info.user)
        PostgresBase.__init__(self, "db_all", self)

        # Check the policy's roles here, rather than when the first relation is
        # created: a policy naming a role this cluster does not have is a
        # mistake worth hearing about before it interrupts a bootstrap or a
        # reload halfway through.
        if self.grant_policy.roles:
            self._existing_roles(
                self.grant_policy.roles, missing=self.grant_policy.missing_role
            )

        if create:
            # Create any missing metadata tables before the read-only detection
            # below, which concludes read-only when no tables are visible
            self._bootstrap_meta()

        if upgrade:
            # Migrate the metadata tables up to the format this psycodict
            # expects.  This runs before the format check below, so that
            # ``PostgresDatabase(..., upgrade=True)`` connects at the current
            # format (and without the older-format warning) in one call.
            self.upgrade_metadata()

        # After create/upgrade: the read-only detection below concludes
        # read-only when it can see no table it may write to, which a database
        # whose meta tables have just been bootstrapped now has.
        self._apply_capabilities(self._detect_capabilities(self.conn))

        # Refuse to run against a database that still uses the removed
        # search/extras table split
        legacy = self._execute(SQL(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'meta_tables' AND column_name = 'has_extras'"
        ))
        if legacy.rowcount:
            cur = self._execute(SQL("SELECT name FROM meta_tables WHERE has_extras"))
            if cur.rowcount:
                raise RuntimeError(
                    "The following tables still use an extras table, which is no "
                    "longer supported by psycodict: %s.  Merge each extras table "
                    "into its search table before upgrading psycodict."
                    % (", ".join(rec[0] for rec in cur))
                )

        # Check the database's metadata format (see MetadataFormats.md): an
        # older-but-compatible or acceptably-newer format connects with a
        # warning and this connection operates at the shared format,
        # self._meta_format; a layout this psycodict cannot safely use is
        # refused.  _stored_meta_format explains how the format of an
        # unstamped database is inferred.
        stored, min_compat = self._stored_meta_format()
        action, message = self._meta_format_action(stored, min_compat, self._read_only)
        if action == "error":
            raise RuntimeError(message)
        if action == "warn":
            logging.warning(message)
        # The metadata format this connection operates at: everything that
        # reads or writes meta_* columns consults this, so that a psycodict
        # ahead of the database (or behind it) only touches the columns both
        # sides have.
        self._meta_format = min(stored, META_FORMAT)

        # The search tables, by name.  A dedicated mapping rather than the
        # instance dictionary, so that a table name -- which comes out of
        # meta_tables and can therefore be anything a row holds -- cannot take
        # the place of the connection, the configuration or a method.
        self.tables = {}
        self.refresh_tables()

    def refresh_tables(self):
        """
        Update the table objects to match the current state of the database.

        The set of tables, and each table's columns, types, sort order and
        other metadata, are read from the database when this object is
        created.  A long-running process (such as a website) therefore does
        not see schema changes made from other processes: after a column is
        dropped, for example, its queries still mention the column and fail,
        while a newly added column is silently invisible.  Rather than
        restarting every such process, call this method to bring the process
        up to date -- on a schedule, say, or upon catching an
        ``errors.UndefinedColumn``.

        Existing table objects are updated in place, so table references held
        by application code (``nf = db.nf_fields``) remain valid.  Tables
        created since the last refresh become accessible as attributes and are
        added to ``tablenames``; tables that have been dropped are removed.  A
        reference held to a dropped table's object will raise an error on its
        next query, as it must, since the underlying table no longer exists.
        """
        cur = self._execute(SQL(
            "SELECT table_name, column_name, udt_name::regtype "
            "FROM information_schema.columns ORDER BY table_name, ordinal_position"
        ))
        data_types = {}
        for table_name, column_name, regtype in cur:
            if table_name not in data_types:
                data_types[table_name] = []
            data_types[table_name].append((column_name, regtype))

        try:
            cur = self._execute(SQL(
                "SELECT name, label_col, sort, count_cutoff, id_ordered, out_of_order, "
                "stats_valid, total, include_nones FROM meta_tables"
            ))
        except UndefinedTable:
            raise ValueError(
                "This database does not contain psycodict's metadata tables. "
                "If this is a fresh database, connect with PostgresDatabase(create=True) "
                "to create them."
            )
        # Read everything, validate everything, and only then touch the
        # registry.  meta_tables is data like any other -- a row can be
        # inserted or edited with plain SQL -- and a name is not a search table
        # on its own: it has to own a whole family of relations that nothing
        # else claims.  Refreshing table by table would act on the rows before
        # the poisoned one.
        rows = list(cur)
        names = [tabledata[0] for tabledata in rows]
        for tablename in names:
            self._validate_table_name(tablename, source="meta_tables")
        validate_search_table_registry(names, self._schema_relations())

        current = set()
        for tabledata in rows:
            tablename = tabledata[0]
            current.add(tablename)
            if tablename in self.tables:
                self.tables[tablename]._refresh(tabledata[1:], data_types)
            else:
                tabledata += (data_types,)
                self.tables[tablename] = self._search_table_class_(self, *tabledata)
        for tablename in [name for name in self.tables if name not in current]:
            del self.tables[tablename]

    def __repr__(self):
        return "Interface to Postgres database"

    def _cursor(self, buffered=False):
        """
        Returns a new cursor.

        If buffered, then it creates a server side cursor that must be manually
        closed after done using it.
        """
        if buffered:
            self.server_side_counter += 1
            cur = self.conn.cursor(str(self.server_side_counter), withhold=True)
            # psycopg3's ServerCursor defaults to itersize=100 (psycopg2's
            # named cursors used 2000), which multiplies round trips when
            # iterating over large result sets.
            cur.itersize = 2000
            return cur
        else:
            return self.conn.cursor()

    def _log_db_change(self, operation, tablename=None, logid=None, aborted=False, **data):
        """
        By default we don't log changes (from updates, etc), but you can
        override this method if you want to do some logging.
        """
        pass

    def _existing_roles(self, users, missing="error"):
        """
        The roles among ``users`` that the cluster actually has.

        INPUT:

        - ``users`` -- role names
        - ``missing`` -- ``"error"`` to refuse when one of them does not exist,
          ``"skip"`` to warn and leave it out

        A policy that names a role the cluster does not have has not been
        applied, whatever it says; the default is to say so rather than to
        leave the caller believing otherwise.
        """
        existing = {rec[0] for rec in self._execute(SQL("SELECT rolname FROM pg_roles"))}
        absent = [user for user in users if user not in existing]
        if absent:
            if missing == "error":
                raise ValueError(
                    "Postgres role(s) %s do not exist, so the grant policy "
                    "cannot be applied; create them, correct the policy, or "
                    "use a policy with missing_role='skip'"
                    % (", ".join(absent),)
                )
            logging.warning(
                "Postgres role(s) %s do not exist; skipping grants",
                ", ".join(absent),
            )
        return [user for user in users if user in existing]

    def _grant(self, action, table_name, users, missing="error"):
        """
        Utility function for granting permissions on tables.
        """
        action = action.upper()
        if action not in GRANT_ACTIONS:
            raise ValueError("%s is not a valid action" % action)
        grantor = SQL("GRANT " + action + " ON TABLE {0} TO {1}")
        for user in self._existing_roles(users, missing=missing):
            self._execute(grantor.format(Identifier(table_name), Identifier(user)), silent=True)

    def _apply_grant_policy(self, table_name, kind):
        """
        Give a relation psycodict has just created the privileges the policy
        says a relation of its kind has.

        INPUT:

        - ``table_name`` -- the relation
        - ``kind`` -- one of ``psycodict.grants.RELATION_KINDS``

        The policy is authoritative for the roles it names: the actions it
        manages are revoked from all of them first, so a relation ends up with
        what the policy says rather than with that plus whatever it inherited.
        A rename carries privileges with it, which is how a backup table would
        otherwise keep the live table's, so this matters most there.  Roles the
        policy does not name are not touched at all.
        """
        policy = self.grant_policy
        if not policy.roles:
            # the default policy: nothing to grant, and nothing to look up
            return
        roles = self._existing_roles(policy.roles, missing=policy.missing_role)
        if not roles:
            return
        self._execute(
            SQL("REVOKE {0} ON TABLE {1} FROM {2}").format(
                SQL(", ").join(SQL(action) for action in GRANT_ACTIONS),
                Identifier(table_name),
                SQL(", ").join(Identifier(role) for role in roles),
            ),
            silent=True,
        )
        for action, granted in policy.for_kind(kind).items():
            wanted = [role for role in granted if role in roles]
            if wanted:
                self._grant(action, table_name, wanted, missing=policy.missing_role)

    def _grantees(self, table_name):
        """
        The roles that have been granted something on a relation, other than
        the grantor's own privileges on it.
        """
        cur = self._execute(
            SQL(
                "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
                "WHERE table_name = %s AND grantee <> grantor"
            ),
            [table_name],
            silent=True,
        )
        return {rec[0] for rec in cur}

    def _warn_if_access_was_lost(self, table_name, previous_name):
        """
        Say so when a swap has replaced a relation others could read with one
        they cannot.

        A reload swaps in a clone, which PostgreSQL creates with no privileges
        of its own, so under a policy that says nothing about the relation the
        replacement is reachable only by its owner -- an application role that
        could read the table a moment ago now cannot.  That is what the policy
        asks for, but it should not be discovered by a website going dark.
        """
        lost = self._grantees(previous_name) - self._grantees(table_name)
        if lost:
            logging.warning(
                "%s was replaced by a copy that %s cannot access; the grant "
                "policy in force grants them nothing on it. Pass a grant_policy "
                "to PostgresDatabase (LMFDBGrantPolicy() reproduces psycodict's "
                "pre-1.0 grants) if they should keep it.",
                table_name,
                ", ".join(sorted(lost)),
            )

    def grant_select(self, table_name, users):
        """
        Grant users the ability to run SELECT statements on a given table

        INPUT:

        - ``table_name`` -- a string, the name of the table
        - ``users`` -- a list of users to grant this permission

        Note that this grants exactly what it is asked to, to exactly the roles
        it is given: what a relation psycodict creates starts out with is the
        grant policy's business (see :class:`psycodict.grants.GrantPolicy`).
        """
        self._grant("SELECT", table_name, users)

    def grant_insert(self, table_name, users):
        """
        Grant users the ability to run INSERT statements on a given table

        INPUT:

        - ``table_name`` -- a string, the name of the table
        - ``users`` -- a list of users to grant this permission
        """
        self._grant("INSERT", table_name, users)

    def grant_update(self, table_name, users):
        """
        Grant users the ability to run UPDATE statements on a given table

        INPUT:

        - ``table_name`` -- a string, the name of the table
        - ``users`` -- a list of users to grant this permission
        """
        self._grant("UPDATE", table_name, users)

    def grant_delete(self, table_name, users):
        """
        Grant users the ability to run DELETE statements on a given table

        INPUT:

        - ``table_name`` -- a string, the name of the table
        - ``users`` -- a list of users to grant this permission
        """
        self._grant("DELETE", table_name, users)

    def _is_read_only(self):
        """
        Whether this instance of the database is read only.
        """
        return self._read_only

    def _can_read_write_knowls(self):
        """
        Whether this instance of the database has permission to read and write to the knowl tables
        """
        return self._read_and_write_knowls

    def _can_read_write_userdb(self):
        """
        Whether this instance of the database has permission to read and write to the user info tables.
        """
        return self._read_and_write_userdb

    def _is_alive(self):
        """
        Check that the connection to the database is active.
        """
        try:
            cur = self._execute(SQL("SELECT 1"))
            if cur.rowcount == 1:
                return True
        except Exception:
            pass
        return False

    # The relkinds that can hold rows, which is what a search table's family is
    # made of: ordinary and partitioned tables, views, materialized views and
    # foreign tables.
    _TABLE_RELKINDS = ("r", "p", "v", "m", "f")

    def _schema_relations(self, relkinds=_TABLE_RELKINDS):
        """
        The names of relations in the schema psycodict is using.

        INPUT:

        - ``relkinds`` -- the ``pg_class.relkind`` values to include; the
          default is the kinds that can be a search table or one of its
          companions.  Pass None for every relation, which is the namespace a
          *name* has to be free in: indexes, sequences and tables share it.

        Restricted to the current schema rather than taken from an unqualified
        ``information_schema`` query, so that a relation of the same name in
        another schema -- ``userdb.users``, say -- cannot stand in for one this
        database is deciding about.
        """
        query = (
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema()"
        )
        values = None
        if relkinds is not None:
            query += " AND c.relkind = ANY(%s)"
            values = [list(relkinds)]
        cur = self._execute(SQL(query), values, silent=True)
        return {rec[0] for rec in cur}

    @property
    def tablenames(self):
        """
        The names of the search tables, sorted.
        """
        return sorted(self.tables)

    def _validate_table_name(self, name, source=None):
        """
        Check a search table name.

        INPUT:

        - ``name`` -- the proposed or recorded name
        - ``source`` -- where it came from, if not from a caller, which also
          means the name is one this database already has

        A name psycodict is asked to create must follow the conventions in
        full.  A name read out of ``meta_tables`` is checked for the things
        that would make it unsafe to use -- a path separator, a ``..``
        component, whitespace, anything that is not an identifier -- but not
        for the conventions, which an existing database is entitled to have
        broken long before this check existed.
        """
        try:
            return validate_search_table_name(
                name, reserved=self._reserved_names(), strict=source is None
            )
        except ValueError as err:
            if source is None:
                raise
            raise InvalidDefinitionError("%s (from %s)" % (err, source))

    def _reserved_names(self):
        """
        The names a search table may not have, because the database object
        already uses them for something else.
        """
        return set(dir(type(self))) | set(self.__dict__)

    def __getitem__(self, name):
        """
        Accesses a PostgresSearchTable object by name.
        """
        try:
            return self.tables[name]
        except KeyError:
            raise ValueError("%s is not a search table" % name)

    def __getattr__(self, name):
        """
        Accesses a PostgresSearchTable object as an attribute.

        Table objects live in ``self.tables`` rather than in the instance
        dictionary, so that a table can never take the place of the
        connection, the configuration or a method.  This is only reached when
        ordinary attribute lookup has already failed, so anything the database
        object really has wins over a table of the same name.
        """
        try:
            tables = self.__dict__["tables"]
        except KeyError:
            # before __init__ has made the registry; without this an attribute
            # touched during construction would recurse
            raise AttributeError(name)
        try:
            return tables[name]
        except KeyError:
            raise AttributeError(name)

    def table_sizes(self):
        """
        Returns a dictionary containing information on the sizes of the search tables.

        OUTPUT:

        A dictionary with a row for each search table
        (as well as a few others such as kwl_knowls), with entries

        - ``nrows`` -- an estimate for the number of rows in the table
        - ``nstats`` -- an estimate for the number of rows in the stats table
        - ``ncounts`` -- an estimate for the number of rows in the counts table
        - ``total_bytes`` -- the total number of bytes used by the main table, as well as stats, counts, indexes, ancillary storage....
        - ``index_bytes`` -- the number of bytes used for indexes on the main table
        - ``toast_bytes`` -- the number of bytes used for storage of variable length data types, such as strings and jsonb
        - ``table_bytes`` -- the number of bytes used for fixed length storage on the main table
        - ``counts_bytes`` -- the number of bytes used by the counts table
        - ``stats_bytes`` -- the number of bytes used by the stats table
        """
        query = """
SELECT table_name, row_estimate, total_bytes, index_bytes, toast_bytes,
       total_bytes-index_bytes-COALESCE(toast_bytes,0) AS table_bytes FROM (
  SELECT relname as table_name,
         c.reltuples AS row_estimate,
         pg_total_relation_size(c.oid) AS total_bytes,
         pg_indexes_size(c.oid) AS index_bytes,
         pg_total_relation_size(reltoastrelid) AS toast_bytes
  FROM pg_class c
  LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND relkind = 'r'
) a"""
        sizes = defaultdict(lambda: defaultdict(int))
        cur = self._execute(SQL(query))
        for (
            table_name,
            row_estimate,
            total_bytes,
            index_bytes,
            toast_bytes,
            table_bytes,
        ) in cur:
            if table_name.endswith("_stats"):
                name = table_name[:-6]
                sizes[name]["nstats"] = int(row_estimate)
                sizes[name]["stats_bytes"] = total_bytes
            elif table_name.endswith("_counts"):
                name = table_name[:-7]
                sizes[name]["ncounts"] = int(row_estimate)
                sizes[name]["counts_bytes"] = total_bytes
            else:
                name = table_name
                sizes[name]["nrows"] = int(row_estimate)
                # use the cached account for an accurate count
                if name in self.tablenames:
                    row_cached = self[name].stats.quick_count({})
                    if row_cached is not None:
                        sizes[name]["nrows"] = row_cached
                sizes[name]["index_bytes"] = index_bytes
                sizes[name]["toast_bytes"] = toast_bytes
                sizes[name]["table_bytes"] = table_bytes
            sizes[name]["total_bytes"] += total_bytes
        return sizes

    def _meta_creator(self, meta_name, hist=False, fmt=None):
        """
        Create one of the six metadata tables, with columns generated from
        the shared definitions in base.py (_meta_*_cols and _meta_*_types),
        so that the DDL cannot drift from the code that reads and writes
        these tables.  ``fmt`` gives the metadata format to create the table
        at (default: the current one), so that filling in a table missing
        from an older-format database does not smuggle in a partial upgrade.
        """
        meta_cols, meta_types, _ = _meta_cols_types_jsonb_idx(meta_name, fmt)
        cols = list(meta_cols)
        types = dict(meta_types)
        if hist:
            cols.append("version")
            types["version"] = "integer"
        defaults = _meta_tables_defaults if meta_name == "meta_tables" else {}
        parts = []
        for col in cols:
            # The types and defaults are module constants, not user input
            part = SQL("{0} " + types[col]).format(Identifier(col))
            if col in defaults:
                part = part + SQL(" DEFAULT " + defaults[col])
            parts.append(part)
        tbl = meta_name + ("_hist" if hist else "")
        self._execute(SQL("CREATE TABLE {0} ({1})").format(Identifier(tbl), SQL(", ").join(parts)))
        self._apply_grant_policy(tbl, "meta_hist" if hist else "meta")

    @property
    def meta_format(self):
        """
        The metadata format this connection operates at.

        This is the format stamped in the database, capped at the format this
        psycodict implements (``META_FORMAT``).  When it is lower than
        ``META_FORMAT``, features introduced by newer formats are unavailable
        (their methods raise with instructions) until the database is
        migrated with :meth:`upgrade_metadata`; see MetadataFormats.md.
        """
        return self._meta_format

    def _stored_meta_format(self):
        """
        The (version, min_compat) pair recorded in this database's single-row
        ``meta_format`` table.

        A database that has meta tables but no ``meta_format`` is a format-0
        database: psycodict 0.x never stamped.  A database with no meta
        tables at all is fresh, so it reports the current format (the missing
        ``meta_tables`` is diagnosed elsewhere).  ``(None, None)`` means the
        stamp exists but is empty; its format is unknowable, which the caller
        reports rather than guessing.
        """
        if self._table_exists("meta_format"):
            row = self._execute(SQL("SELECT version, min_compat FROM meta_format")).fetchone()
            return (None, None) if row is None else (row[0], row[1])
        elif self._table_exists("meta_tables"):
            return (0, 0)
        return (META_FORMAT, _stamped_min_compat(META_FORMAT))

    @staticmethod
    def _meta_format_action(stored, min_compat, read_only):
        """
        How to treat a database whose stamped metadata format is ``stored``
        (with ``min_compat``, the oldest format allowed to use it) when this
        psycodict implements ``META_FORMAT``: one of ``("ok", None)``,
        ``("warn", message)`` or ``("error", message)``.

        A pure function of its inputs, so the whole compatibility matrix can
        be tested without manufacturing a database in each state.  The policy
        (MetadataFormats.md):

        - the same format connects silently;
        - an older format connects with a warning when every intervening
          migration is marked compatible (the connection then operates at the
          older format, newer features unavailable), and is refused otherwise;
        - a newer format connects with a warning when its stamped
          ``min_compat`` admits this psycodict, and is refused otherwise.
        """
        if stored is None:
            return ("error",
                    "This database's meta_format table is empty, so its "
                    "metadata format is unknown; insert the correct "
                    "(version, min_compat) row to assert compatibility.")
        if stored == META_FORMAT:
            return ("ok", None)
        if stored > META_FORMAT:
            if min_compat is not None and min_compat <= META_FORMAT:
                return ("warn",
                        "This database uses metadata format %s, newer than this "
                        "psycodict (format %s) but marked compatible with it.  "
                        "Metadata added by the newer format is preserved but "
                        "invisible here; upgrade psycodict to use it."
                        % (stored, META_FORMAT))
            return ("error",
                    "This database uses metadata format %s and requires a "
                    "psycodict with format at least %s (this one implements "
                    "format %s): upgrade psycodict."
                    % (stored, min_compat, META_FORMAT))
        # stored < META_FORMAT: possible only when every step up to the
        # current format is registered and marked compatible
        if all(META_MIGRATIONS.get(fmt, {}).get("compatible", False)
               for fmt in range(stored + 1, META_FORMAT + 1)):
            if read_only:
                remedy = ("The warning will go away once the primary "
                          "database is migrated (upgrade_metadata, run by an "
                          "administrator on a read-write connection).")
            else:
                remedy = ("Migrate it with upgrade_metadata() -- or connect "
                          "with PostgresDatabase(upgrade=True) -- at your "
                          "convenience.")
            return ("warn",
                    "This database uses metadata format %s, older than this "
                    "psycodict (format %s).  Everything present in format %s "
                    "keeps working, but features introduced by newer formats "
                    "(see MetadataFormats.md) are unavailable.  %s"
                    % (stored, META_FORMAT, stored, remedy))
        return ("error",
                "This database uses metadata format %s, which this psycodict "
                "(format %s) cannot operate against.  Migrate the database "
                "with upgrade_metadata() (or connect with upgrade=True)%s, or "
                "use the psycodict release matching its format."
                % (stored, META_FORMAT,
                   " on a read-write connection" if read_only else ""))

    def upgrade_metadata(self):
        """
        Migrate this database's metadata tables up to the format that this
        version of psycodict implements (``META_FORMAT``), applying each
        registered migration in order and stamping the format as it goes.

        Connecting to an older-format database only warns (when the formats
        are compatible; see MetadataFormats.md), so migrating is a deliberate
        act -- this method, or ``PostgresDatabase(config=..., upgrade=True)``,
        which bootstraps (when ``create=True``), migrates, and then connects,
        all in one call.

        Migrations only move forward.  A database already at the current
        format is left untouched, so calling this is idempotent; a database
        whose format is *newer* than this psycodict is an error (upgrade
        psycodict instead), as is a ``meta_format`` table that exists but is
        empty (its format is unknown, so it cannot be migrated blindly).
        """
        stored, _ = self._stored_meta_format()
        if stored is None:
            raise RuntimeError(
                "Cannot upgrade the metadata: the meta_format table is empty, "
                "so the current format is unknown.  Insert the correct "
                "(version, min_compat) row into meta_format first."
            )
        if stored > META_FORMAT:
            raise RuntimeError(
                "Cannot upgrade the metadata: this database uses format %s, "
                "which is newer than this psycodict understands (%s); upgrade "
                "psycodict instead." % (stored, META_FORMAT)
            )
        while stored < META_FORMAT:
            target = stored + 1
            step = META_MIGRATIONS.get(target)
            if step is None:
                raise RuntimeError(
                    "No metadata migration from format %s to %s is known"
                    % (stored, target)
                )
            # The DDL and the stamp commit together, so an interrupted
            # migration leaves the database cleanly at the old format.
            with DelayCommit(self, silence=True):
                getattr(self, step["upgrade"])()
                self._stamp_meta_format(target)
            print("Upgraded metadata format %s -> %s (%s)"
                  % (stored, target, step["description"]))
            stored = target
        # A post-construction upgrade lifts the format this connection
        # operates at; during __init__ (upgrade=True) this is recomputed by
        # the format check that follows.
        self._meta_format = META_FORMAT

    def _upgrade_meta_0_to_1(self):
        """
        The DDL migrating metadata format 0 to 1: meta_indexes and its history
        table gain a nullable ``whereclause`` column, holding the predicate of
        a partial index (NULL for an ordinary index).

        The column is added with ``IF NOT EXISTS`` so that re-running the
        migration -- or running it on a database that already grew the column
        out of band -- is a harmless no-op.
        """
        for tbl in ("meta_indexes", "meta_indexes_hist"):
            self._execute(SQL(
                "ALTER TABLE {0} ADD COLUMN IF NOT EXISTS whereclause text"
            ).format(Identifier(tbl)))

    def _stamp_meta_format(self, version):
        """
        Record ``(version, min_compat)`` in the single-row ``meta_format``
        table, creating the table when the database predates it.  Also drops
        the transient ``meta_version`` table that briefly played this role
        during development (it never appeared in a release, and its numbering
        was off by one from the meta_format one).
        """
        min_compat = _stamped_min_compat(version)
        with DelayCommit(self, silence=True):
            if self._table_exists("meta_format"):
                self._execute(SQL("UPDATE meta_format SET version = %s, min_compat = %s"),
                              [version, min_compat])
            else:
                self._execute(SQL(
                    "CREATE TABLE meta_format (version integer NOT NULL, "
                    "min_compat integer NOT NULL)"
                ))
                self._execute(SQL("INSERT INTO meta_format (version, min_compat) VALUES (%s, %s)"),
                              [version, min_compat])
                self._apply_grant_policy("meta_format", "meta_format")
            self._execute(SQL("DROP TABLE IF EXISTS meta_version"))
        print("Stamped metadata format %s (min_compat %s)" % (version, min_compat))

    def _create_meta_tables(self, fmt=None):
        with DelayCommit(self, silence=True):
            self._meta_creator("meta_tables", fmt=fmt)
        print("Table meta_tables created")

    def _create_meta_indexes(self, fmt=None):
        with DelayCommit(self, silence=True):
            self._meta_creator("meta_indexes", fmt=fmt)
        print("Table meta_indexes created")

    def _create_meta_constraints(self, fmt=None):
        with DelayCommit(self, silence=True):
            self._meta_creator("meta_constraints", fmt=fmt)
        print("Table meta_constraints created")

    def _create_meta_hist(self, meta_name, fmt=None):
        """
        Create the _hist counterpart of a metadata table, copying any rows
        already present in the base table at version 0.
        """
        meta_cols, _, jsonb_idx = _meta_cols_types_jsonb_idx(meta_name, fmt)
        with DelayCommit(self, silence=True):
            self._meta_creator(meta_name, hist=True, fmt=fmt)
            cols = meta_cols + ("version",)
            rows = self._execute(SQL("SELECT {0} FROM {1}").format(
                SQL(", ").join(map(Identifier, meta_cols)),
                Identifier(meta_name),
            ))
            inserter = SQL("INSERT INTO {0} ({1}) VALUES ({2})").format(
                Identifier(meta_name + "_hist"),
                SQL(", ").join(map(Identifier, cols)),
                SQL(", ").join(Placeholder() * len(cols)),
            )
            for row in rows:
                row = [
                    Json(elt) if i in jsonb_idx else elt for i, elt in enumerate(row)
                ]
                self._execute(inserter, row + [0])
        print("Table %s_hist created" % meta_name)

    def _bootstrap_meta(self):
        """
        Create any missing psycodict metadata tables (meta_tables, meta_indexes,
        meta_constraints, their _hist counterparts, and the meta_format stamp),
        for use on a fresh database.  Called from the constructor when
        ``create=True`` is passed.

        Missing tables are created at the format the database is already at:
        a fresh database comes up at the current format, while filling in the
        gaps of an older-format database must not silently migrate it -- that
        is upgrade_metadata's job, deliberately invoked.
        """
        existing = {
            rec[0] for rec in self._execute(SQL(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ))
        }
        stored, _ = self._stored_meta_format()
        fmt = META_FORMAT if stored is None else min(stored, META_FORMAT)
        # The base tables must be created before the _hist creators, which copy from them
        for name, creator in [
            ("meta_tables", self._create_meta_tables),
            ("meta_indexes", self._create_meta_indexes),
            ("meta_constraints", self._create_meta_constraints),
            ("meta_tables_hist", self._create_meta_tables_hist),
            ("meta_indexes_hist", self._create_meta_indexes_hist),
            ("meta_constraints_hist", self._create_meta_constraints_hist),
        ]:
            if name not in existing:
                creator(fmt=fmt)
        if "meta_format" not in existing:
            # A database that already had meta tables predates the stamp, so
            # it is stamped with the format it actually has (0); moving it to
            # the current format is a deliberate migration, not a side effect
            # of connecting.  A fresh database is created at the current
            # format.
            self._stamp_meta_format(META_FORMAT if "meta_tables" not in existing else 0)

    def _create_meta_indexes_hist(self, fmt=None):
        self._create_meta_hist("meta_indexes", fmt=fmt)

    def _create_meta_constraints_hist(self, fmt=None):
        self._create_meta_hist("meta_constraints", fmt=fmt)

    def _create_meta_tables_hist(self, fmt=None):
        self._create_meta_hist("meta_tables", fmt=fmt)

    def _clone_storage_settings(self, new_name, table):
        """
        Copy per-column storage settings (the state set by ``ALTER COLUMN ...
        SET STORAGE`` and ``SET COMPRESSION``) from the search table of
        ``table`` to the newly created table ``new_name``.

        Creating a table from column types alone silently resets these to the
        type defaults, so a copy of a source with customized column storage
        would store its rows differently from the source (compressing values
        the source keeps external, or vice versa) and the two tables would
        differ in size even though they hold the same data.  This is the one
        way ``create_table_like`` could produce a copy whose on-disk size
        disagrees with its source (see LMFDB/lmfdb#6775): the row copy itself
        preserves toast compression byte for byte.

        INPUT:

        - ``new_name`` -- the name of the new table, already created
        - ``table`` -- a PostgresSearchTable object giving the source table
        """
        server_version = self._execute(
            SQL("SELECT current_setting('server_version_num')::int")
        ).fetchone()[0]
        # Column level compression settings (attcompression) appeared in
        # postgres 14; on older servers there is only attstorage to copy.
        if server_version >= 140000:
            selecter = SQL(
                "SELECT attname, attstorage, attcompression FROM pg_attribute "
                "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped"
            )
        else:
            selecter = SQL(
                "SELECT attname, attstorage, '' FROM pg_attribute "
                "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped"
            )

        def settings(tname):
            return {rec[0]: (rec[1], rec[2]) for rec in self._execute(selecter, [tname])}

        source = settings(table.search_table)
        target = settings(new_name)
        # The keywords are whitelisted here, so it's okay to use string
        # formatting to insert them into the SQL commands below.
        storage_keywords = {"p": "PLAIN", "e": "EXTERNAL", "m": "MAIN", "x": "EXTENDED"}
        compression_keywords = {"p": "pglz", "l": "lz4"}
        with DelayCommit(self, silence=True):
            for col, (storage, compression) in source.items():
                if col not in target:
                    continue
                target_storage, target_compression = target[col]
                if storage != target_storage and storage in storage_keywords:
                    self._execute(
                        SQL(
                            "ALTER TABLE {0} ALTER COLUMN {1} SET STORAGE "
                            + storage_keywords[storage]
                        ).format(Identifier(new_name), Identifier(col))
                    )
                if compression != target_compression and compression in compression_keywords:
                    self._execute(
                        SQL(
                            "ALTER TABLE {0} ALTER COLUMN {1} SET COMPRESSION "
                            + compression_keywords[compression]
                        ).format(Identifier(new_name), Identifier(col))
                    )

    def create_table_like(self, new_name, table, tablespace=None, data=False, indexes=False):
        """
        Creates a new table with the same schema as an existing one, including
        each column's storage and compression settings.  By default neither
        data, indexes nor stats are copied.

        INPUT:

        - ``new_name`` -- a string giving the desired table name.
        - ``table`` -- a string or PostgresSearchTable object giving an existing table.
        - ``tablespace`` -- the tablespace for the new table
        - ``data`` -- whether to copy over data from the source table
        - ``indexes`` -- whether to copy over indexes from the source table
        """
        if isinstance(table, str):
            table = self[table]
        search_columns = {
            typ: [col for col in table.search_cols if table.col_type[col] == typ]
            for typ in set(table.col_type.values())
        }
        # Remove empty lists
        for typ, cols in list(search_columns.items()):
            if not cols:
                search_columns.pop(typ)
        label_col = table._label_col
        table_description = table.description()
        col_description = table.column_description()
        sort = table._sort_orig
        id_ordered = table._id_ordered
        self.create_table(
            new_name,
            search_columns,
            label_col,
            table_description,
            col_description,
            sort,
            id_ordered,
            tablespace=tablespace,
            # Without this an integer id would silently widen to the
            # default bigint in the copy.
            id_type=table.col_type["id"],
            # keep the source's behavior rather than the current default
            include_nones=table._include_nones,
        )
        # Copy the column storage settings before any data, so that rows are
        # stored (kept external, compressed, ...) the same way as the source's.
        self._clone_storage_settings(new_name, table)
        if data:
            logid = table._check_locks("create_table_like")
            aborted = True
            try:
                cols = SQL(", ").join(map(Identifier, ["id"] + table.search_cols))
                self._execute(
                    SQL("INSERT INTO {0} ( {1} ) SELECT {1} FROM {2}").format(
                        Identifier(new_name), cols, Identifier(table.search_table)
                    ),
                )
                aborted = False
            finally:
                table._log_db_change("create_table_like", logid=logid, aborted=aborted, new_name=new_name)
        if indexes:
            for idata in table.list_indexes(verbose=False).values():
                self[new_name].create_index(**idata)
        if data:
            # The bulk insert leaves the new table without planner statistics
            # until autovacuum notices it; analyze right away so that queries
            # against the copy (including the stats refresh below) plan sanely.
            self._execute(SQL("ANALYZE {0}").format(Identifier(new_name)))
            self[new_name].stats.refresh_stats()

    def create_table(
        self,
        name,
        search_columns,
        label_col,
        table_description=None,
        col_description=None,
        sort=None,
        id_ordered=None,
        tablespace=None,
        force_description=False,
        id_type="bigint",
        include_nones=True,
    ):
        """
        Add a new search table to the database.  See also :meth:`create_table_like`.

        INPUT:

        - ``name`` -- the name of the table: lowercase letters, digits and
          underscores, starting with a letter, at most 63 bytes, and not ending
          in one of the suffixes psycodict appends to a search table's own name
          (``_counts``, ``_stats``, ``_pkey``, ``_tmp``, ``_oldN``, ``_depN``).
          By convention it includes an underscore; see existing names for
          consistency.
        - ``search_columns`` -- either a dictionary whose keys are valid postgres types and whose values
            are lists of column names (or just a string if only one column has the specified type);
            or a list of pairs (col, type).
            An id column of type ``id_type`` will be added as a primary key if not present.
        - ``label_col`` -- the column holding the LMFDB label.  This will be used in the ``lookup`` method
            and in the display of results on the API.  Use None if there is no appropriate column.
        - ``table_description`` -- a text description of this table
        - ``col_description`` -- a dictionary giving descriptions for the columns
        - ``sort`` -- If not None, provides a default sort order for the table, in formats accepted by
            the ``_sort_str`` method.
        - ``id_ordered`` -- boolean (default None).  If set, the table will be sorted by id when
            pushed to production, speeding up some kinds of search queries.  Defaults to True
            when sort is not None.
        - ``tablespace`` -- (optional) a postgres tablespace to use for the new table
        - ``force_description`` -- whether to require descriptions
        - ``id_type`` -- what postgres type to use for the id column
        - ``include_nones`` -- whether search results should include columns
          whose value is None (default True).  Pass False to omit None values
          from result dictionaries, as was the default before psycodict 1.0.
          The value is stored explicitly in meta_tables, so the flipped
          default reaches databases created before it without any migration.

        COMMON TYPES:

        The postgres types most commonly used are:

        - smallint -- a 2-byte signed integer.
        - integer -- a 4-byte signed integer.
        - bigint -- an 8-byte signed integer.
        - numeric -- exact, high precision integer or decimal.
        - real -- a 4-byte float.
        - double precision -- an 8-byte float.
        - text -- string (see collation note above).
        - boolean -- true or false.
        - jsonb -- data iteratively built from numerics, strings, booleans, nulls, lists and dictionaries.
        - timestamp -- 8-byte date and time with no timezone.

        A type has to be interpolated into the ``CREATE TABLE`` statement as
        SQL text, since PostgreSQL has no placeholder for a type, so types are
        restricted to the ones ``psycodict.validation.validate_column_type``
        accepts:
        the fixed types above, arrays of them, and lengths, precisions and
        collations (``numeric(10, 2)``, ``varchar(16)``, ``text COLLATE "C"``).
        Anything else raises ``InvalidColumnTypeError`` before any statement
        runs.
        """
        self._validate_table_name(name)
        if name in self.tablenames:
            raise ValueError("%s already exists" % name)
        now = time.time()
        if id_ordered is None:
            id_ordered = sort is not None

        if not isinstance(search_columns, dict):
            search_columns = self._pairs_to_dict(search_columns)
        for typ, L in list(search_columns.items()):
            if isinstance(L, str):
                search_columns[typ] = [L]
        valid_list = sum(search_columns.values(), [])
        valid_set = set(valid_list)
        # Check that columns aren't listed twice
        if len(valid_list) != len(valid_set):
            C = Counter(valid_list)
            raise ValueError("Column %s repeated" % (C.most_common(1)[0][0]))
        # Check that label_col is valid
        if label_col is not None and label_col not in valid_set:
            raise ValueError("label_col must be a search column")

        # Check that sort is valid
        if sort is not None:
            for col in sort:
                if isinstance(col, tuple):
                    if len(col) != 2:
                        raise ValueError("Sort terms must be either strings or pairs")
                    if col[1] not in [1, -1]:
                        raise ValueError("Sort terms must be of the form (col, 1) or (col, -1)")
                    col = col[0]
                if col not in valid_set:
                    raise ValueError("Column %s does not exist" % (col))

        # Check that descriptions are provided if required
        description_columns = []
        for col in search_columns.values():
            if col == 'id':
                continue
            if isinstance(col, str):
                description_columns.append(col)
            else:
                description_columns.extend([c for c in col if c != "id"])
        if force_description:
            if table_description is None or col_description is None:
                raise ValueError("You must provide table and column descriptions")
            if set(col_description) != set(description_columns):
                raise ValueError("Must provide descriptions for all columns")
        else:
            if table_description is None:
                table_description = ""
            if col_description is None:
                col_description = {col: "" for col in description_columns}

        with DelayCommit(self, silence=True):
            self._create_table(name, search_columns, addid=id_type, tablespace=tablespace)
            self._apply_grant_policy(name, "search")
            tablespace = self._tablespace_clause(tablespace)
            creator = SQL(
                "CREATE TABLE {0} "
                "(cols jsonb, values jsonb, count bigint, "
                "extra boolean, split boolean DEFAULT FALSE){1}"
            )
            creator = creator.format(
                Identifier(physical_table_name(name, "counts")), tablespace
            )
            self._execute(creator)
            self._apply_grant_policy(name + "_counts", "counts")
            creator = SQL(
                "CREATE TABLE {0} "
                '(cols jsonb, stat text COLLATE "C", value numeric, '
                "constraint_cols jsonb, constraint_values jsonb, threshold integer){1}"
            )
            creator = creator.format(
                Identifier(physical_table_name(name, "stats")), tablespace
            )
            self._execute(creator)
            self._apply_grant_policy(name + "_stats", "stats")
            # FIXME use global constants ?
            # include_nones is written explicitly rather than left to the
            # column default: existing databases keep the DDL default their
            # meta_tables was created with, so relying on it would give new
            # tables different behavior in old and new databases.
            inserter = SQL(
                "INSERT INTO meta_tables "
                "(name, sort, id_ordered, out_of_order, label_col, include_nones) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            )
            self._execute(
                inserter,
                [
                    name,
                    Json(sort),
                    id_ordered,
                    not id_ordered,
                    label_col,
                    include_nones,
                ],
            )
            self._notify_schema_change(name)  # rides this transaction
        new_table = self._search_table_class_(
            self,
            name,
            label_col,
            sort=sort,
            id_ordered=id_ordered,
            out_of_order=(not id_ordered),
            total=0,
            include_nones=include_nones,
        )
        # Add the primary key on id, so that methods like drop_pkeys (used by
        # insert_many and reload) find the constraint they expect
        new_table.restore_pkeys()
        new_table.description(table_description)
        new_table.column_description(description=col_description)
        self.tables[name] = new_table
        self._log_db_change(
            "create_table",
            tablename=name,
            name=name,
            search_columns=search_columns,
            label_col=label_col,
            sort=sort,
            id_ordered=id_ordered,
        )
        print("Table %s created in %.3f secs" % (name, time.time() - now))

    def drop_table(self, name, force=False):
        """
        Drop a table.

        INPUT:

        - ``name`` -- the name of the table
        - ``force`` -- refrain from asking for confirmation

        NOTE:

        You cannot drop a table that has been marked important.  You must first set it as not important if you want to drop it.
        """
        table = self[name]
        selecter = SQL("SELECT important FROM meta_tables WHERE name=%s")
        if self._execute(selecter, [name]).fetchone()[0]:
            raise ValueError("You cannot drop an important table.  Use the set_importance method on the table if you actually want to drop it.")
        if not force:
            ok = input("Are you sure you want to drop %s? (y/N) " % (name))
            if not (ok and ok[0] in ["y", "Y"]):
                return
        with DelayCommit(self, silence=True):
            table.cleanup_from_reload()
            indexes = list(self._execute(
                SQL("SELECT index_name FROM meta_indexes WHERE table_name = %s"),
                [name],
            ))
            if indexes:
                self._execute(SQL("DELETE FROM meta_indexes WHERE table_name = %s"), [name])
                print("Deleted indexes {0}".format(", ".join(index[0] for index in indexes)))
            constraints = list(self._execute(
                SQL("SELECT constraint_name FROM meta_constraints WHERE table_name = %s"),
                [name],
            ))
            if constraints:
                self._execute(SQL("DELETE FROM meta_constraints WHERE table_name = %s"), [name])
                print("Deleted constraints {0}".format(", ".join(constraint[0] for constraint in constraints)))
            self._execute(SQL("DELETE FROM meta_tables WHERE name = %s"), [name])
            for tbl in [physical_table_name(name, component) for component in
                        ("search", "counts", "stats")]:
                self._execute(SQL("DROP TABLE {0}").format(Identifier(tbl)))
                print("Dropped {0}".format(tbl))
            del self.tables[name]
            self._notify_schema_change(name)  # rides this transaction

    def _next_backup_number_for(self, name):
        """
        One past the highest backup number a table has, so that callers can
        enumerate its existing ``_oldN`` relations.
        """
        number = 1
        while any(
            self._table_exists(physical_table_name(name, component, "_old%s" % number))
            for component in ("search", "counts", "stats")
        ):
            number += 1
        return number

    def _dependent_rename_plan(self, old_name, new_name):
        """
        What each of a table's indexes and constraints will be called after the
        table is renamed.

        INPUT:

        - ``old_name``, ``new_name`` -- the table's current and new names

        OUTPUT:

        ``{"index": {old: new, ...}, "constraint": {old: new, ...}}``, covering
        every name in the current and history metadata.

        Planned in one place, before anything is renamed, for two reasons.  The
        metadata and the catalog must end up with the same spelling, and the
        candidate -- the old name with the table's name substituted -- can be
        longer than PostgreSQL will keep, so it goes through
        ``derived_identifier``; computing it separately in each metadata loop
        would risk two paths deciding differently.  And a fitted name can land
        on one that another object already has, which has to be resolved here
        rather than discovered halfway through the renames.
        """
        plan = {}
        # An index is a pg_class relation, not a table, so pg_tables cannot see
        # the names an index rename can collide with -- and neither can the
        # table-shaped default here.  Every relation in the schema shares one
        # namespace, so that is the set a new name has to be free in.
        #
        # A CHECK constraint is not a relation at all: pg_class has no row for
        # it, only pg_constraint does (a UNIQUE constraint is visible through
        # its backing index, which is why it is easy to miss).  The table's own
        # constraints are added from there, which also covers constraints
        # psycodict does not manage.
        occupied = self._schema_relations(relkinds=None)
        occupied |= set(self._list_constraints(old_name))

        # Read both kinds before allocating any of them.  The reservation below
        # has to be global: an index and a constraint can hold the same name --
        # PostgreSQL keeps them in different catalogs -- so allocating one kind
        # completely before looking at the other would let an index's new name
        # take a name a constraint is keeping.
        dependents = {}
        for kind, meta_tables, column in (
            ("index", ("meta_indexes", "meta_indexes_hist"), "index_name"),
            ("constraint", ("meta_constraints", "meta_constraints_hist"), "constraint_name"),
        ):
            names, current = [], set()
            for meta in meta_tables:
                cur = self._execute(
                    SQL("SELECT DISTINCT {0} FROM {1} WHERE table_name = %s").format(
                        Identifier(column), Identifier(meta)
                    ),
                    [old_name],
                    silent=True,
                )
                for rec in cur:
                    if rec[0] not in names:
                        names.append(rec[0])
                    if not meta.endswith("_hist"):
                        current.add(rec[0])
            dependents[kind] = (names, current)
            plan[kind] = {}

        targets = {
            (kind, name): derived_identifier(name.replace(old_name, new_name))
            for kind, (names, _) in dependents.items()
            for name in names
        }

        # First pass, across both kinds: the dependents that keep their name.
        # A name that does not mention the table stays as it is -- the object
        # does not move, and "ALTER ... RENAME TO" its own name is an error
        # rather than a no-op -- but its metadata still follows the table.
        # Nothing else may be given that name, including a dependent of the
        # other kind, and including a name only the history remembers, which no
        # catalog reserves.
        #
        # Two keepers may share a name: an index and a CHECK constraint are
        # allowed to, and if they already do, neither is being renamed.
        planned = set()
        for kind, (names, current) in dependents.items():
            for name in names:
                if targets[kind, name] == name:
                    plan[kind][name] = {"target": name, "rename_physical": False}
                    planned.add(name)

        # Second pass, across both kinds: everything that is being renamed,
        # against the relations that exist and every name already spoken for.
        for kind, (names, current) in dependents.items():
            for name in names:
                if name in plan[kind]:
                    continue
                candidate = targets[kind, name]
                target, disamb = candidate, 0
                while target in occupied or target in planned:
                    target = derived_identifier(candidate, str(disamb))
                    disamb += 1
                    if disamb > 100:
                        raise ValueError(
                            "Cannot find a free name for the %s %s after "
                            "renaming %s to %s" % (kind, name, old_name, new_name)
                        )
                plan[kind][name] = {
                    "target": target,
                    # only a relation that exists can be renamed; a name that
                    # only history remembers has nothing to rename
                    "rename_physical": name in current,
                }
                planned.add(target)

        # No dependent that is being renamed may share its new name with
        # anything else in the plan.  Keepers are exempt, since they are not
        # being renamed and may legitimately already share.
        renamed = [
            entry["target"]
            for entries in plan.values()
            for name, entry in entries.items()
            if entry["target"] != name
        ]
        keepers = {
            entry["target"]
            for entries in plan.values()
            for name, entry in entries.items()
            if entry["target"] == name
        }
        if len(set(renamed)) != len(renamed) or keepers & set(renamed):
            raise ValueError(
                "Renaming %s to %s would give two of its indexes or "
                "constraints the same name" % (old_name, new_name)
            )
        return plan

    def rename_table(self, old_name, new_name):
        """
        Rename a table.

        INPUT:

        - ``old_name`` -- the current name of the table, as a string
        - ``new_name`` -- the new name of the table, as a string
        """
        if old_name == new_name:
            raise ValueError("The new name is the same as the old one")
        self._validate_table_name(new_name)
        if new_name in self.tablenames:
            raise ValueError("%s already exists" % new_name)
        # Everything the rename will produce is decided and checked before any
        # of it happens.  The dependent names have to agree between the
        # metadata and the catalog, and every destination relation has to be a
        # name PostgreSQL will keep -- a rename that discovered otherwise
        # halfway through would leave the table in pieces.
        renames = self._dependent_rename_plan(old_name, new_name)
        for component in ("search", "counts", "stats"):
            for lifecycle in [""] + [
                "_tmp"
            ] + ["_old%s" % n for n in range(1, self._next_backup_number_for(old_name))]:
                source = physical_table_name(old_name, component, lifecycle)
                if lifecycle and not self._table_exists(source):
                    continue
                check_new_table_name(
                    physical_table_name(new_name, component, lifecycle)
                )
        with DelayCommit(self, silence=True):
            table = self[old_name]
            # first rename indexes and constraints
            icols = [Identifier(s) for s in ["index_name", "table_name"]]
            ccols = [Identifier(s) for s in ["constraint_name", "table_name"]]
            rename_index = SQL("ALTER INDEX IF EXISTS {0} RENAME TO {1}")
            rename_constraint = SQL("ALTER TABLE {0} RENAME CONSTRAINT {1} TO {2}")
            for meta, mname, cols in [
                ("meta_indexes", "index_name", icols),
                ("meta_indexes_hist", "index_name", icols),
                ("meta_constraints", "constraint_name", ccols),
                ("meta_constraints_hist", "constraint_name", ccols),
            ]:
                kind = "index" if "indexes" in meta else "constraint"
                indexes = list(self._execute(
                    SQL("SELECT {0} FROM {1} WHERE table_name = %s").format(
                        Identifier(mname), Identifier(meta)
                    ),
                    [old_name],
                ))
                if indexes:
                    # Scoped by the table as well as by the dependent's name: a
                    # constraint name is only unique within its own table, so
                    # two tables can legitimately share one, and renaming this
                    # table must not move the other table's rows.
                    rename_index_in_meta = SQL(
                        "UPDATE {0} SET ({1}) = ({2}) WHERE {3} = {4} AND {5} = {6}"
                    )
                    rename_index_in_meta = rename_index_in_meta.format(
                        Identifier(meta),
                        SQL(", ").join(cols),
                        SQL(", ").join(Placeholder() * len(cols)),
                        cols[0],
                        Placeholder(),
                        Identifier("table_name"),
                        Placeholder(),
                    )
                    for old_index_name in indexes:
                        old_index_name = old_index_name[0]
                        planned = renames[kind][old_index_name]
                        new_index_name = planned["target"]
                        self._execute(
                            rename_index_in_meta,
                            [new_index_name, new_name, old_index_name, old_name],
                        )
                        if not planned["rename_physical"]:
                            continue
                        if meta == "meta_indexes":
                            self._execute(rename_index.format(
                                Identifier(old_index_name),
                                Identifier(new_index_name),
                            ))
                        elif meta == "meta_constraints":
                            self._execute(rename_constraint.format(
                                Identifier(old_name),
                                Identifier(old_index_name),
                                Identifier(new_index_name),
                            ))
            else:
                print("Renamed all indexes, constraints and the corresponding metadata")

            # rename meta_tables and meta_tables_hist
            rename_table_in_meta = SQL("UPDATE {0} SET name = %s WHERE name = %s")
            for meta in ["meta_tables", "meta_tables_hist"]:
                self._execute(rename_table_in_meta.format(Identifier(meta)), [new_name, old_name])
            else:
                print("Renamed all entries meta_tables(_hist)")

            rename = SQL("ALTER TABLE {0} RENAME TO {1}")
            for suffix in ["", "_counts", "_stats"]:
                self._execute(rename.format(Identifier(old_name + suffix), Identifier(new_name + suffix)))
                print("Renamed {0} to {1}".format(old_name + suffix, new_name + suffix))

            # rename oldN tables
            for backup_number in range(table._next_backup_number()):
                for ext in ["", "_counts", "_stats"]:
                    old_name_old = "{0}{1}_old{2}".format(old_name, ext, backup_number)
                    new_name_old = "{0}{1}_old{2}".format(new_name, ext, backup_number)
                    if self._table_exists(old_name_old):
                        self._execute(rename.format(Identifier(old_name_old), Identifier(new_name_old)))
                        print("Renamed {0} to {1}".format(old_name_old, new_name_old))
            for ext in ["", "_counts", "_stats"]:
                old_name_tmp = "{0}{1}_tmp".format(old_name, ext)
                new_name_tmp = "{0}{1}_tmp".format(new_name, ext)
                if self._table_exists(old_name_tmp):
                    self._execute(rename.format(Identifier(old_name_tmp), Identifier(new_name_tmp)))
                    print("Renamed {0} to {1}".format(old_name_tmp, new_name_old))

            # initialized table
            tabledata = self._execute(
                SQL(
                    "SELECT name, label_col, sort, count_cutoff, id_ordered, "
                    "out_of_order, stats_valid, total, include_nones "
                    "FROM meta_tables WHERE name = %s"
                ),
                [new_name],
            ).fetchone()
            table = self._search_table_class_(self, *tabledata)
            self.tables[new_name] = table
            # Also drop the old name (as drop_table does), so that
            # db.<old_name> does not keep handing out a table object whose
            # postgres table no longer exists.
            self.tables.pop(old_name, None)
            # A rename touches both names: the old one is gone, the new one
            # appeared.  Announce both so a listener can drop the stale
            # metadata and pick up the new table (rides this transaction).
            self._notify_schema_change(old_name)
            self._notify_schema_change(new_name)

    def copy_to(self, search_tables, data_folder, fail_on_error=True, **kwds):
        """
        Copy a set of search tables to a folder on the disk.

        INPUT:

        - ``search_tables`` -- a list of strings giving names of tables to copy
        - ``data_folder`` -- a path to a folder to save the data.  The folder must not currently exist.
        - ``**kwds`` -- other arguments are passed on to the ``copy_to`` method of each table.
        """
        if fail_on_error:
            for tablename in search_tables:
                if tablename not in self.tablenames:
                    raise ValueError(f"{tablename} is not in tablenames")

        data_folder = Path(data_folder)

        if data_folder.exists():
            raise ValueError("The path {} already exists".format(data_folder))
        data_folder.mkdir(parents=True)
        failures = []
        for tablename in search_tables:
            if tablename in self.tablenames:
                table = self[tablename]
                # These names are generated from the table name rather than
                # given by the caller, so they go through safe_child_path:
                # whatever a name in meta_tables turns out to be, the files an
                # export writes stay in the folder it was asked for.
                searchfile = safe_child_path(data_folder, tablename + ".txt")
                statsfile = safe_child_path(data_folder, tablename + "_stats.txt")
                countsfile = safe_child_path(data_folder, tablename + "_counts.txt")
                indexesfile = safe_child_path(data_folder, tablename + "_indexes.txt")
                constraintsfile = safe_child_path(data_folder, tablename + "_constraints.txt")
                metafile = safe_child_path(data_folder, tablename + "_meta.txt")
                table.copy_to(
                    searchfile=searchfile,
                    countsfile=countsfile,
                    statsfile=statsfile,
                    indexesfile=indexesfile,
                    constraintsfile=constraintsfile,
                    metafile=metafile,
                    **kwds
                )
            else:
                print("%s is not in tablenames " % (tablename,))
                failures.append(tablename)
        if failures:
            print("Failed to copy %s (not in tablenames)" % (", ".join(failures)))

    def copy_to_from_remote(self, search_tables, data_folder, remote_opts=None, fail_on_error=True, **kwds):
        """
        Copy data to a folder from a postgres instance on another server.

        INPUT:

        - ``search_tables`` -- a list of strings giving names of tables to copy
        - ``data_folder`` -- a path to a folder to save the data.  The folder must not currently exist.
        - ``remote_opts`` -- options for the remote connection (passed on to psycopg's connect method)
        - ``**kwds`` -- other arguments are passed on to the ``copy_to`` method of each table.
        """
        if remote_opts is None:
            remote_opts = self.config.get_postgresql_default()

        source = PostgresDatabase(**remote_opts)

        # copy all the data
        source.copy_to(search_tables, data_folder, fail_on_error=fail_on_error, **kwds)

    def reload_all(
        self,
        data_folder,
        halt_on_errors=True,
        resort=None,
        restat=None,
        adjust_schema=False,
        sequential_swap=False,
        **kwds
    ):
        """
        Reloads all tables from files in a given folder.  The filenames must match
        the names of the tables, with ``_counts`` and ``_stats`` appended as appropriate.

        INPUT:

        - ``data_folder`` -- the folder that contains files to be reloaded
        - ``halt_on_errors`` -- whether to stop if a DatabaseError is
          encountered while trying to reload one of the tables
        - ``sequential_swap`` -- if True, then the whole transaction will not
          be wrapped in a DelayCommit, which can sometimes prevent deadlocks
        - ``resort``, ``restat``, ``adjust_schema``, and any extra keywords
          are passed on to the ``reload`` method of each
          :class:`~psycodict.table.PostgresTable`

        Note that this function currently does not reload data that is not in a
        search table, such as knowls or user data.
        """
        data_folder = Path(data_folder)

        if not data_folder.is_dir():
            raise ValueError("The path {} is not a directory".format(data_folder))
        sep = kwds.get("sep", "|")
        with DelayCommit(self, silence=True, active=not sequential_swap):
            file_list = []
            tablenames = []
            non_existent_tables = []
            possible_endings = [
                "_counts.txt",
                "_stats.txt",
                "_indexes.txt",
                "_constraints.txt",
                "_meta.txt",
            ]
            for path in data_folder.glob("*.txt"):
                filename = path.name
                if any(filename.endswith(elt) for elt in possible_endings):
                    continue
                tablename = path.stem
                if tablename not in self.tablenames:
                    non_existent_tables.append(tablename)
            if non_existent_tables:
                if not adjust_schema:
                    raise ValueError(
                        "non existent tables: {0}; use adjust_schema=True to create them".format(
                            ", ".join(non_existent_tables)
                        )
                    )
                print("Creating tables: {0}".format(", ".join(non_existent_tables)))
                for tablename in non_existent_tables:
                    search_table_file = data_folder / (tablename + ".txt")
                    metafile = data_folder / (tablename + "_meta.txt")
                    if not metafile.exists():
                        raise ValueError("meta file missing for {0}".format(tablename))
                    # read metafile
                    with metafile.open("r") as F:
                        rows = list(csv.reader(F, delimiter=str(sep)))
                    if len(rows) != 1:
                        raise RuntimeError("Expected only one row in {0}")
                    meta = dict(zip(_meta_tables_cols, rows[0]))
                    if meta["name"] != tablename:
                        raise ValueError(
                            "The meta file %s describes the table %s, but its "
                            "name says %s"
                            % (metafile, meta["name"], tablename)
                        )

                    with search_table_file.open("r") as F:
                        search_columns_pairs = self._read_header_lines(F, sep=sep)

                    search_columns = defaultdict(list)
                    for name, typ in search_columns_pairs:
                        if name != "id":
                            search_columns[typ].append(name)

                    # the rest of the meta arguments will be replaced on the reload_all
                    # We use force_description=False so that beta and prod can be out-of-sync with respect to columns and/or descriptions
                    self.create_table(tablename, search_columns, None, force_description=False)

            for tablename in self.tablenames:
                included = []

                searchfile = data_folder / (tablename + ".txt")
                if not searchfile.exists():
                    continue
                included.append(tablename)

                table = self[tablename]

                extrafile = data_folder / (tablename + "_extras.txt")
                if extrafile.exists():
                    raise ValueError(
                        "Unexpected file %s: extras tables are no longer supported; "
                        "merge the extras columns into the search table file" % extrafile
                    )

                countsfile = data_folder / (tablename + "_counts.txt")
                if countsfile.exists():
                    included.append(tablename + "_counts")
                else:
                    countsfile = None

                statsfile = data_folder / (tablename + "_stats.txt")
                if statsfile.exists():
                    included.append(tablename + "_stats")
                else:
                    statsfile = None

                indexesfile = data_folder / (tablename + "_indexes.txt")
                if not indexesfile.exists():
                    indexesfile = None

                constraintsfile = data_folder / (tablename + "_constraints.txt")
                if not constraintsfile.exists():
                    constraintsfile = None

                metafile = data_folder / (tablename + "_meta.txt")
                if not metafile.exists():
                    metafile = None

                file_list.append(
                    (
                        table,
                        (
                            searchfile,
                            countsfile,
                            statsfile,
                            indexesfile,
                            constraintsfile,
                            metafile,
                        ),
                        included,
                    )
                )
                tablenames.append(tablename)
            print("Reloading {0}".format(", ".join(tablenames)))
            failures = []
            for table, filedata, included in file_list:
                try:
                    table.reload(
                        *filedata,
                        resort=resort,
                        restat=restat,
                        final_swap=False,
                        silence_meta=True,
                        adjust_schema=adjust_schema,
                        **kwds
                    )
                except DatabaseError:
                    if halt_on_errors or non_existent_tables:
                        raise
                    else:
                        traceback.print_exc()
                        failures.append(table)
            for table, filedata, included in file_list:
                if table in failures:
                    continue
                table.reload_final_swap(tables=included, metafile=filedata[-1], sep=sep)

        if failures:
            print("Reloaded %s" % (", ".join(tablenames)))
            print(
                "Failures in reloading %s"
                % (", ".join(table.search_table for table in failures))
            )
        else:
            print("Successfully reloaded %s" % (", ".join(tablenames)))

    def reload_all_revert(self, data_folder):
        """
        Reverts the most recent ``reload_all`` by swapping with the backup table
        for each search table modified.

        INPUT:

        - ``data_folder`` -- the folder used in ``reload_all``;
            determines which tables
            were modified.
        """
        data_folder = Path(data_folder)

        if not data_folder.is_dir():
            raise ValueError("The path {} is not a directory".format(data_folder))

        with DelayCommit(self, silence=True):
            for tablename in self.tablenames:
                searchfile = data_folder / (tablename + ".txt")
                if not searchfile.exists():
                    continue
                self[tablename].reload_revert()

    def cleanup_all(self):
        """
        Drops all ``_tmp`` and ``_old`` tables created by the reload() method.
        """
        with DelayCommit(self, silence=True):
            for tablename in self.tablenames:
                table = self[tablename]
                table.cleanup_from_reload()

    def _get_queries(self):
        """
        The queries currently running in this database, excluding this
        connection's own, as (pid, duration, username, query) tuples in
        starting order (oldest first).
        """
        return list(self._execute(SQL(
            "SELECT pid, age(clock_timestamp(), query_start), usename, query "
            "FROM pg_stat_activity "
            "WHERE state = 'active' AND datname = current_database() "
            "AND pid != pg_backend_pid() ORDER BY query_start"
        ), silent=True))

    def show_queries(self):
        """
        Prints the queries currently running in this database (which may be
        holding the locks shown by ``show_locks``; see ``show_blocked`` for
        statements that are stuck behind them).
        """
        queries = self._get_queries()
        if not queries:
            print("No queries currently running")
            return
        # Collapse each query's whitespace once, up front, so the pid, duration
        # and user columns are measured (and aligned) against what prints; the
        # query itself is last, so it needs no padding.
        rows = [("pid %s" % pid, str(duration), user, " ".join(query.split()))
                for pid, duration, user, query in queries]
        pidlen = max(len(pidstr) for pidstr, _, _, _ in rows) + 2
        durlen = max(len(duration) for _, duration, _, _ in rows) + 2
        userlen = max(len(user) for _, _, user, _ in rows) + 2
        for pidstr, duration, user, query in rows:
            print(
                pidstr
                + " " * (pidlen - len(pidstr))
                + duration
                + " " * (durlen - len(duration))
                + user
                + " " * (userlen - len(user))
                + query
            )

    def _get_blocked(self):
        """
        Statements waiting on locks, each paired with a session that actually
        blocks them, as tuples (blocked_pid, blocked_user, blocking_pid,
        blocking_user, blocked_statement, blocking_statement).

        Blockers are determined by pg_blocking_pids, which accounts for lock
        modes, grant status and the wait queue; the well-known pg_locks
        self-join matches on resource identity alone and reports bystanders
        (e.g. an unrelated ACCESS SHARE holder) as blockers.
        """
        return list(self._execute(SQL(
            "SELECT blocked.pid, blocked.usename, "
            "blocking.pid, blocking.usename, "
            "blocked.query, blocking.query "
            "FROM pg_stat_activity blocked "
            "JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(bpid) ON true "
            "JOIN pg_stat_activity blocking ON blocking.pid = b.bpid "
            "WHERE blocked.datname = current_database() "
            "ORDER BY blocked.pid, blocking.pid"
        ), silent=True))

    def show_blocked(self):
        """
        Prints the statements that are waiting on locks held by other
        sessions, together with the sessions holding them.
        """
        for bpid, buser, gpid, guser, bquery, gquery in self._get_blocked():
            print("pid %s (%s) blocked on pid %s (%s)" % (bpid, buser, gpid, guser))
            print("  waiting: %s" % " ".join(bquery.split()))
            print("  blocker: %s" % " ".join(gquery.split()))

    def show_locks(self):
        """
        Prints information on all locks currently held on any table.
        """
        locks = sorted(self._get_locks())
        if locks:
            namelen = max(len(name) for (name, locktype, pid, t) in locks) + 3
            typelen = max(len(locktype) for (name, locktype, pid, t) in locks) + 3
            pidlen = max(len(str(pid)) for (name, locktype, pid, t) in locks) + 3
            for name, locktype, pid, t in locks:
                print(
                    name
                    + " " * (namelen - len(name))
                    + locktype
                    + " " * (typelen - len(locktype))
                    + "pid %s" % pid
                    + " " * (pidlen - len(str(pid)))
                    + "age %s" % t
                )
        else:
            print("No locks currently held")

    def tablespaces(self):
        """
        Returns a dictionary giving giving the tablespace for all tables
        """
        D = {rec[0]: rec[1] for rec in self._execute(SQL("SELECT tablename, tablespace FROM pg_tables"))}
        return {name: space if space else "" for (name, space) in D.items()}

    def compare(self, other, tables=None, row_counts=True, null_counts=False, exact=False):
        """
        Returns the differences between the search tables of this database
        and those of another one, such as a beta and a production server.

        The comparison is read-only on both sides: only SELECT statements are
        issued, and (unlike the ``count`` method of a table, which may cache
        its results when count saving is enabled) nothing is recorded in the
        counts tables or meta_tables of either database, so ``other`` may
        safely be a production server.

        INPUT:

        - ``other`` -- another ``PostgresDatabase`` instance; you connect to
          the second server yourself (see the examples below)
        - ``tables`` -- a table name or list of table names (default None,
          meaning every search table of either database); restricts every
          part of the comparison to those tables
        - ``row_counts`` -- boolean (default True).  Whether to compare the
          number of rows in tables present in both databases
        - ``null_counts`` -- boolean (default False).  Whether to compare the
          number of NULL entries in each column shared by both sides.  This
          requires a full scan of each compared table in each database, which
          can take minutes for the largest LMFDB tables, so consider
          restricting ``tables``
        - ``exact`` -- boolean (default False).  By default row counts are
          read from the total cached in meta_tables, which psycodict's write
          paths maintain and ``count()`` reports; that is a single cheap
          query per database, but it can be stale if a table was modified
          from outside psycodict or its statistics were never refreshed.  Set
          to True to run SELECT COUNT(*) on each compared table instead,
          which is exact but slow on big tables (the counting scans run with
          whatever statement timeout each connection has)

        OUTPUT:

        A dictionary with keys

        - ``only_in_self``, ``only_in_other`` -- sorted lists of the names of
          search tables present in only one of the databases
        - ``schema`` -- a dictionary indexed by the tables present in both
          databases whose schemas differ, with values dictionaries containing
          whichever of the following differences occur: ``only_in_self`` and
          ``only_in_other`` (lists of pairs ``(col, type)`` of columns
          present on one side only), ``type_changed`` (a list of triples
          ``(col, type_self, type_other)``, with types rendered in full so
          that e.g. ``numeric(10,2)`` vs ``numeric(20,4)`` is reported) and
          ``meta_changed`` (a list of
          triples ``(item, value_self, value_other)`` recording disagreements
          in the label_col, sort, id_ordered, include_nones or count_cutoff
          settings from meta_tables)
        - ``row_counts`` (if requested) -- ``{table: (rows_self,
          rows_other)}``, only for the tables where the counts differ
        - ``null_counts`` (if requested) -- ``{table: {col: (nulls_self,
          nulls_other)}}``, only for the columns where the counts differ

        EXAMPLES:

        Comparing with a server described by a second configuration file::

            >>> from psycodict.config import Configuration
            >>> from psycodict.database import PostgresDatabase
            >>> prod_config = Configuration(defaults={"config_file": "prod-config.ini"}, readargs=False)  # doctest: +SKIP
            >>> prod = PostgresDatabase(config=prod_config)  # doctest: +SKIP
            >>> db.compare(prod)["only_in_self"]  # doctest: +SKIP
            ['mf_newspaces_test']

        Keyword arguments override the configuration, so a server that
        differs only in its host can reuse this database's configuration::

            >>> prod = PostgresDatabase(config=db.config, host="proddb.lmfdb.xyz")  # doctest: +SKIP
            >>> db.compare(prod, tables="mf_newspaces", null_counts=True)  # doctest: +SKIP
            {'only_in_self': [], 'only_in_other': [], 'schema': {}, 'row_counts': {}, 'null_counts': {}}
        """
        from .dbdiff import compare_databases
        return compare_databases(
            self, other,
            tables=tables,
            row_counts=row_counts,
            null_counts=null_counts,
            exact=exact,
        )

    def show_differences(self, other, tables=None, row_counts=True, null_counts=False, exact=False):
        """
        Prints a readable report of the differences between the search tables
        of this database and those of another one.

        Sections without differences are omitted; if the databases agree,
        prints "No differences found".  See ``compare`` for the meaning of
        the arguments, the cost of the optional comparisons, and the
        guarantee that both databases are only read from.
        """
        from .dbdiff import compare_databases, format_differences
        diff = compare_databases(
            self, other,
            tables=tables,
            row_counts=row_counts,
            null_counts=null_counts,
            exact=exact,
        )
        print(format_differences(diff, self, other))

    def show_slow_report(self, logfile, top=20, cutoff=None):
        """
        Prints an analysis of a slow-query log file: which query shapes take
        the most time, how much smaller the log would be with a higher
        ``slowcutoff``, and which constrained columns lack a supporting index
        (checked against the indexes recorded in ``meta_indexes``).

        See :mod:`psycodict.slowlog` for the underlying functions, which can
        also be used without a database connection.

        INPUT:

        - ``logfile`` -- the filename of a log written via the ``slowlogfile``
          logging option
        - ``top`` -- the number of query shapes to show
        - ``cutoff`` -- only consider queries at least this slow, in seconds
        """
        from .slowlog import show_slow_report
        show_slow_report(logfile, top=top, cutoff=cutoff, db=self)

    # ---------------------------------------------------------------------
    # LISTEN/NOTIFY support (see psycodict/notifications.py for the design).
    # ---------------------------------------------------------------------

    def notify(self, channel, payload=""):
        """
        Send a PostgreSQL notification on ``channel`` with the given payload.

        The notification is sent with ``pg_notify`` on the main connection,
        through ``_execute``, so it is *transactional*: PostgreSQL delivers it
        when the surrounding transaction commits and drops it on rollback.
        Called on its own (outside a ``DelayCommit``) it commits immediately and
        so is delivered at once; called inside a ``DelayCommit`` it rides that
        transaction and is delivered (or dropped) with it.

        INPUT:

        - ``channel`` -- the channel name; must be a plain identifier (letters,
          digits and underscores, not starting with a digit)
        - ``payload`` -- a string payload (default ``""``); received verbatim by
          listeners

        Subscribe with :meth:`listener` (or the standalone
        :class:`~psycodict.notifications.NotificationListener`).
        """
        from .notifications import validate_channel_name
        validate_channel_name(channel)
        self._execute(SQL("SELECT pg_notify(%s, %s)"), [channel, payload])

    def _notify_schema_change(self, tablename):
        """
        Announce that ``tablename``'s schema changed, on the schema channel.

        Used by the schema-changing operations (create/drop/rename table,
        add/drop column, reload swap).  Because it goes through :meth:`notify`
        on the main connection, the announcement is part of the same
        transaction as the change itself.
        """
        from .notifications import SCHEMA_CHANNEL
        self.notify(SCHEMA_CHANNEL, tablename)

    def listener(self, channels=None):
        """
        Return a :class:`~psycodict.notifications.NotificationListener`.

        The listener opens its *own* ``autocommit`` connection from this
        database's configuration and ``LISTEN``s on ``channels`` (default: the
        schema channel ``"psycodict_schema"`` alone).  It is pull-based: call
        ``poll(timeout)`` for a bounded batch, or iterate ``listen()``; use it
        as a context manager to close the connection when done.

        The intended follow-up use is a long-running website process that keeps
        a listener on ``"psycodict_schema"`` and, whenever a table name arrives,
        refreshes that table's cached metadata so newly created columns and
        reloaded tables become visible without a restart.  That refresh
        mechanism is proposed in a separate PR; psycodict ships the notification
        plumbing here without depending on it, so the two can land in either
        order.

        Under a pre-forking web server each worker must build its own listener
        after the fork, and a server in recovery (a hot standby) refuses
        ``LISTEN`` outright; see the *Forking* and *Hot standbys* sections of
        :mod:`psycodict.notifications`.

        INPUT:

        - ``channels`` -- a channel name or iterable of them; ``None`` (default)
          means the schema channel only
        """
        from .notifications import NotificationListener, SCHEMA_CHANNEL
        if channels is None:
            channels = (SCHEMA_CHANNEL,)
        # Pass the same connection overrides the database itself was opened
        # with, so the listener's dedicated connection reaches the same server
        # and database as the sender.
        return NotificationListener(self.config, channels, **self._connect_kwargs)
