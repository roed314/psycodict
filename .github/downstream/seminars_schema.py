"""
Create an empty researchseminars.org schema, for the `seminars` CI job.

The seminars project has no test suite and no schema migration; its database
is created by hand and its production copy is password-gated.  To exercise it
against a psycodict pull request we therefore have to build the schema
ourselves -- which we do through psycodict's own ``create_table``, so that the
bootstrap is itself a test of the code under review.

No data is loaded beyond the handful of topic rows that ``/ams`` needs: the
point is to check that seminars can talk to psycodict at all, not to test
seminars.

CAVEAT: the column lists below are hand-derived -- from seminars' Schema.md
where it documents a table, and from ``db.<table>.<col>`` usage in its source
where it does not (Schema.md still documents an obsolete `topics` table).  So
this file drifts when seminars changes its schema, and when it does, this job
fails for reasons that have nothing to do with psycodict.  If that becomes a
nuisance, the right fix is to upstream this script into the seminars repo
where it can be kept in sync; see the discussion in the PR that added it.

Usage: run from a seminars checkout that has a config.ini pointing at the
target database.
"""
from seminars import db

# name -> (columns by postgres type, label column)
TABLES = {
    # ---------------------------------------------- documented in Schema.md
    "institutions": (
        {
            "text": ["admin", "city", "deleted", "homepage", "name", "shortname",
                     "timezone", "type"],
            "timestamp with time zone": ["edited_at"],
            "bigint": ["edited_by"],
        },
        "shortname",
    ),
    "seminars": (
        {
            "text": ["access_hint", "access_registration", "chat_link", "comments",
                     "homepage", "language", "live_link", "name", "room", "shortname",
                     "stream_link", "timezone", "visibility_counter"],
            "text[]": ["institutions", "topics", "editors", "weekdays_ranges", "time_slots"],
            "smallint": ["access_control", "audience", "frequency", "per_day", "visibility"],
            "integer": ["access_time"],
            "boolean": ["deleted", "display", "is_conference", "online", "by_api"],
            "date": ["start_date", "end_date"],
            "timestamp with time zone": ["edited_at"],
            "bigint": ["edited_by", "owner"],
            "smallint[]": ["weekdays"],
        },
        "shortname",
    ),
    "talks": (
        {
            "text": ["abstract", "access_hint", "access_registration", "chat_link",
                     "comments", "language", "live_link", "paper_link", "room",
                     "seminar_id", "slides_link", "speaker", "speaker_affiliation",
                     "speaker_email", "speaker_homepage", "stream_link", "timezone",
                     "title", "token", "video_link"],
            "text[]": ["topics"],
            "smallint": ["access_control", "audience"],
            "integer": ["access_time", "seminar_ctr"],
            "boolean": ["deleted", "deleted_with_seminar", "display", "hidden",
                        "online", "by_api"],
            "timestamp with time zone": ["edited_at", "start_time", "end_time"],
            "bigint": ["edited_by"],
        },
        None,
    ),
    "seminar_organizers": (
        {
            "text": ["seminar_id", "email", "homepage", "name"],
            "boolean": ["curator", "display"],
            "integer": ["order"],
        },
        None,
    ),
    # seminars/users/pwdmanager.py subclasses PostgresSearchTable for this one
    # and calls PostgresSearchTable.__init__ directly, so it is the table that
    # matters most to this job.
    "users": (
        {
            "text": ["affiliation", "api_token", "email", "homepage", "name",
                     "password", "subject_admin", "timezone"],
            "text[]": ["seminar_subscriptions"],
            "boolean": ["admin", "creator", "email_confirmed"],
            "smallint": ["api_access"],
            "integer": ["endorser"],
            "timestamp with time zone": ["created"],
            "jsonb": ["talks_subscriptions"],
        },
        "email",
    ),
    # ------------------ not in Schema.md; columns inferred from source usage
    "new_topics": ({"text": ["topic_id", "name"], "text[]": ["children"]}, "topic_id"),
    "subjects": ({"text": ["subject_id", "name"]}, "subject_id"),
    "preendorsed_users": ({"text": ["email"], "bigint": ["endorser"]}, None),
    "talk_registrations": (
        {"text": ["seminar_id", "email", "name"], "integer": ["seminar_ctr"]},
        None,
    ),
    "seminar_registrations": ({"text": ["seminar_id", "email", "name"]}, None),
    "author_ids": ({"text": ["name", "regex", "display_name"]}, "name"),
}

# /ams walks the topic tree, which is empty otherwise.
SEED = {
    "new_topics": [
        {"topic_id": "math", "name": "Mathematics", "children": ["math_NT", "math_AG"]},
        {"topic_id": "math_NT", "name": "Number Theory", "children": []},
        {"topic_id": "math_AG", "name": "Algebraic Geometry", "children": []},
    ],
    "subjects": [{"subject_id": "math", "name": "Mathematics"}],
}


def main():
    existing = set(db.tablenames)
    for name, (columns, label_col) in TABLES.items():
        if name in existing:
            print("exists: %s" % name)
            continue
        db.create_table(name, columns, label_col)
        print("created: %s" % name)

    for name, rows in SEED.items():
        # count() reports a cached total that inserts do not update, so ask
        # the table itself rather than trusting the count.
        if not list(db[name].search({}, projection="id", limit=1)):
            db[name].insert_many(rows)
            print("seeded: %s (%d rows)" % (name, len(rows)))

    print("tables now: %s" % sorted(db.tablenames))


if __name__ == "__main__":
    main()
