"""
Exercise researchseminars.org against the psycodict under test.

The seminars project has no test suite -- its entire CI is `pyflakes .` --
so this stands in for one.  It is not trying to test seminars; it is trying
to fail whenever a psycodict change would break it.

Three levels, cheapest first:

  imports  seminars constructs a PostgresDatabase at module import time and
           iterates db.tablenames, so an import is already a connection test
  routes   rendering a page runs real search/lucky/lookup queries through
           psycodict against the empty schema
  api      the two places where seminars reaches past the dictionary
           interface and touches psycodict's classes directly:
             - users/pwdmanager.py subclasses PostgresSearchTable and calls
               PostgresSearchTable.__init__(db=..., search_table=..., ...)
             - utils.py does PostgresSearchTable(db, *cur.fetchone()) straight
               from a meta_tables row
           these break immediately on a signature change, which is exactly
           the regression this job exists to catch

Run from a seminars checkout whose config.ini points at a bootstrapped
database.  Exits non-zero on the first failure.
"""
import sys
import traceback

failures = []


def check(name, fn):
    try:
        fn()
    except Exception:
        failures.append(name)
        print("FAIL %s" % name)
        traceback.print_exc()
    else:
        print("ok   %s" % name)


# ------------------------------------------------------------------ imports

MODULES = [
    "seminars",
    "seminars.app",
    "seminars.utils",
    "seminars.talk",
    "seminars.seminar",
    "seminars.institution",
    "seminars.topic",
    "seminars.users",
    "seminars.users.pwdmanager",
    "seminars.website",
]

for module in MODULES:
    check("import %s" % module, lambda m=module: __import__(m))

if failures:
    # Nothing below can run if the app does not import.
    print("\n%d import failure(s); stopping" % len(failures))
    sys.exit(1)


# --------------------------------------------------------------------- api

def check_pwdmanager():
    from seminars.users.pwdmanager import userdb

    # A PostgresSearchTable subclass; lucky() on a miss must return None
    # rather than raising.
    assert userdb.lucky({"email": "nobody@example.invalid"}) is None


def check_sanitized_table():
    from seminars.utils import sanitized_table

    # Builds a PostgresSearchTable directly from a meta_tables row.
    table = sanitized_table("talks")
    assert list(table.search({}, projection="id", limit=1)) == []


def check_search_and_lookup():
    from seminars import db

    assert "talks" in db.tablenames
    assert db.new_topics.lookup("math")["name"] == "Mathematics"
    children = db.new_topics.lucky({"topic_id": "math"}, "children")
    assert children == ["math_NT", "math_AG"]


check("api: pwdmanager userdb", check_pwdmanager)
check("api: sanitized_table", check_sanitized_table)
check("api: search/lookup/lucky", check_search_and_lookup)


# ------------------------------------------------------------------- routes

# /embeddable_schedule.js is excluded: it raises TemplateNotFound for
# seminar_raw.js, which does not exist anywhere in the seminars repo.  That is
# a pre-existing bug on seminars master, not something psycodict can affect.
ROUTES = [
    "/",
    "/health",
    "/info",
    "/conferences",
    "/seminar_series",
    "/past_conferences",
    "/institutions/",
    "/subjects",
    "/sitemap",
    "/ams",
    "/api/",
    "/embed_seminars.js",
]


def route_check(path):
    def run():
        from seminars.website import app

        with app.test_client() as client:
            response = client.get(path)
        assert response.status_code < 500, "%s returned %d" % (path, response.status_code)

    return run


for route in ROUTES:
    check("GET %s" % route, route_check(route))


print("\n%d checks, %d failed" % (len(MODULES) + 3 + len(ROUTES), len(failures)))
if failures:
    print("failed: %s" % ", ".join(failures))
    sys.exit(1)
