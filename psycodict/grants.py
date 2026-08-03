# -*- coding: utf-8 -*-
"""
Who gets to read and write the relations psycodict creates.

Creating a search table creates several relations -- the table itself, its
counts and stats tables -- and a reload creates and swaps more.  Something has
to decide what privileges those come into existence with, and psycodict used to
decide it in the code: SELECT to ``lmfdb`` and ``webserver``, INSERT to
``webserver``, wherever those roles happened to exist.  That is the LMFDB's
deployment, not a fact about a PostgreSQL database, and it meant a new table
holding whatever you put in it was readable by two roles you may never have
heard of.

A :class:`GrantPolicy` states those privileges explicitly, by relation kind.
The default policy grants nothing, so a relation is reachable only by its owner
and by whatever the cluster's own defaults give away; :func:`LMFDBGrantPolicy`
reproduces what psycodict used to do, for the deployments that want it::

    db = PostgresDatabase(grant_policy=LMFDBGrantPolicy())

A policy is authoritative for the relations psycodict applies it to: it revokes
the actions it manages from the roles it names before granting, so the result
is the policy and not the policy plus whatever was there before.
"""
from dataclasses import dataclass, field

from .validation import InvalidDefinitionError, MAX_IDENTIFIER_LENGTH

# The actions a policy can grant.  A policy manages exactly these: privileges
# outside this set (TRUNCATE, REFERENCES, TRIGGER) are left alone.
GRANT_ACTIONS = ("SELECT", "INSERT", "UPDATE", "DELETE")

# The kinds of relation psycodict creates, which is what a policy is written in
# terms of -- a policy should not have to know that the counts table of foo is
# called foo_counts.
RELATION_KINDS = (
    "search",       # a search table
    "counts",       # its counts table
    "stats",        # its stats table
    "meta",         # meta_tables, meta_indexes, meta_constraints
    "meta_hist",    # their _hist counterparts
    "meta_format",  # the metadata format stamp
    "backup",       # the _oldN table a reload leaves behind
    "oldstats",     # the legacy Mongo statistics import made by create_oldstats
)

# What to do about a role a policy names that the cluster does not have.
MISSING_ROLE_ACTIONS = ("error", "skip")


def _validate_role_name(name):
    """
    Check a role name a policy names.

    Roles are quoted with ``Identifier`` wherever they are used, and PostgreSQL
    allows more in a role name than psycodict allows in a table name, so this
    checks only what would make the name unusable.
    """
    if not isinstance(name, str) or not name:
        raise InvalidDefinitionError("A role name must be a non-empty string")
    if len(name.encode("utf-8")) > MAX_IDENTIFIER_LENGTH:
        raise InvalidDefinitionError(
            "Role name %r is longer than PostgreSQL's %s byte limit"
            % (name, MAX_IDENTIFIER_LENGTH)
        )
    for char in name:
        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F:
            raise InvalidDefinitionError(
                "Role name %r contains the control character %r" % (name, char)
            )
    return name


@dataclass(frozen=True)
class GrantPolicy:
    """
    The privileges psycodict grants on the relations it creates.

    INPUT:

    - ``grants`` -- a mapping from relation kind (see ``RELATION_KINDS``) to a
      mapping from action (see ``GRANT_ACTIONS``) to the roles that get it.
      Kinds left out get nothing.
    - ``missing_role`` -- what to do when the policy names a role the cluster
      does not have: ``"error"`` (the default) refuses, so that a policy is
      never half-applied without saying so; ``"skip"`` warns and carries on,
      which is what a development machine without the deployment's roles
      wants.

    EXAMPLES::

        >>> GrantPolicy({"search": {"SELECT": ("readonly",)}})
        GrantPolicy(grants={'search': {'SELECT': ('readonly',)}}, missing_role='error')
    """

    grants: dict = field(default_factory=dict)
    missing_role: str = "error"

    def __post_init__(self):
        if self.missing_role not in MISSING_ROLE_ACTIONS:
            raise ValueError(
                "missing_role must be one of %s, not %r"
                % (", ".join(MISSING_ROLE_ACTIONS), self.missing_role)
            )
        normalized = {}
        for kind, actions in self.grants.items():
            if kind not in RELATION_KINDS:
                raise ValueError(
                    "Unknown relation kind %r; psycodict creates %s"
                    % (kind, ", ".join(RELATION_KINDS))
                )
            normalized[kind] = {}
            for action, roles in actions.items():
                if action.upper() not in GRANT_ACTIONS:
                    raise ValueError(
                        "Unknown action %r; a policy grants %s"
                        % (action, ", ".join(GRANT_ACTIONS))
                    )
                if isinstance(roles, str):
                    raise ValueError(
                        "The roles granted %s on a %s relation must be a "
                        "sequence of role names, not the string %r"
                        % (action, kind, roles)
                    )
                try:
                    # materialized before it is validated: validating a
                    # generator would consume it, and the policy would then
                    # silently name nobody
                    roles = tuple(roles)
                except TypeError as err:
                    raise ValueError(
                        "The roles granted %s on a %s relation must be an "
                        "iterable of role names, not %r" % (action, kind, roles)
                    ) from err
                for role in roles:
                    _validate_role_name(role)
                normalized[kind][action.upper()] = roles
        # frozen dataclass: this is the one place the fields are set
        object.__setattr__(self, "grants", normalized)

    @property
    def roles(self):
        """
        Every role this policy mentions.

        These are the roles whose privileges psycodict manages: applying the
        policy revokes the managed actions from them first, so that what a
        relation ends up with is the policy rather than the policy plus
        whatever it inherited.  Roles the policy does not mention are not
        touched.
        """
        return sorted({
            role
            for actions in self.grants.values()
            for roles in actions.values()
            for role in roles
        })

    def for_kind(self, kind):
        """
        The ``{action: roles}`` this policy gives a relation of ``kind``.
        """
        if kind not in RELATION_KINDS:
            raise ValueError("Unknown relation kind %r" % (kind,))
        return self.grants.get(kind, {})


def LMFDBGrantPolicy(missing_role="skip"):
    """
    The permissions psycodict granted before they were a policy.

    SELECT to ``lmfdb`` and ``webserver`` on search, counts, stats and metadata
    relations, and INSERT to ``webserver`` on counts and stats, which the
    website needs in order to record the counts it computes.  The legacy
    ``_oldstats`` relation gets SELECT only, as it always did.  Backup (``_oldN``)
    tables get nothing: they hold the data the live table held before a reload,
    and a rename carries the live table's privileges over to them, so the
    policy revokes what the live table had rather than leaving a copy of
    production readable by the application roles.

    INPUT:

    - ``missing_role`` -- defaults to ``"skip"``, since a development database
      typically has neither role; pass ``"error"`` on a deployment that should
      have both.
    """
    read = ("lmfdb", "webserver")
    write = ("webserver",)
    return GrantPolicy(
        {
            "search": {"SELECT": read},
            "counts": {"SELECT": read, "INSERT": write},
            "stats": {"SELECT": read, "INSERT": write},
            "meta": {"SELECT": read},
            "meta_hist": {"SELECT": read},
            "meta_format": {"SELECT": read},
            # create_oldstats only ever granted SELECT: the relation holds an
            # import of statistics computed elsewhere, and nothing writes to it
            # through the website.
            "oldstats": {"SELECT": read},
        },
        missing_role=missing_role,
    )
