# -*- coding: utf-8 -*-
"""
Tools for analyzing the slow-query logs that psycodict writes.

When a query takes longer than the ``slowcutoff`` configured in the
``[logging]`` section, ``PostgresBase._execute`` appends lines like the
following to the file configured as ``slowlogfile``::

    2026-07-20 20:19:14,940 - SELECT "label" FROM "curves" WHERE "n" = 5 ORDER BY "n" ran in \\x1b[91m 0.35s \\x1b[0m
    2026-07-20 20:19:14,940 - Replicate with db.curves.analyze({'n': 5}, ['label'], None, 0)

The timing is wrapped in ANSI color escapes, the ``Replicate with`` hint
line follows the query it describes, and versions of this code from before
2019 wrote ``... ran in 0.35s`` without the color escapes.  A query whose
inlined values contain newlines spans several physical lines.  Search
iterators log a third shape (normally only to the console, but captured
console output is worth parsing too)::

    Search iterator for curves {'n': 5} required a total of \\x1b[91m0.35s\\x1b[0m

The functions here parse such files, group the queries by *shape* (the
query with all constants removed), and produce a report aimed at two
questions: would raising ``slowcutoff`` shrink the log substantially, and
which queries might benefit from an index?

Typical usage::

    from psycodict.slowlog import show_slow_report
    show_slow_report("slow_queries.log", db=db)  # db optional

or equivalently ``db.show_slow_report("slow_queries.log")``.  Nothing here
writes to the database: when ``db`` is provided, the report reads the
indexes recorded in ``meta_indexes`` (through ``table.list_indexes()``) to
check whether the columns constrained by slow queries are covered.
"""

import re
from collections import defaultdict
from datetime import datetime
from math import ceil, floor, log10

_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - ")
_ANSI_RE = re.compile("\x1b\\[[0-9;]*m")
_NUMBER = r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
_RAN_IN_RE = re.compile(r"\s+ran in\s+(" + _NUMBER + r")s\s*$")
_ITERATOR_RE = re.compile(
    r"^Search iterator for (\S+)\s+(.*?)\s*required a total of\s*(" + _NUMBER + r")s\s*$",
    re.DOTALL,
)
_REPLICATE_RE = re.compile(r"^Replicate with (db\.([A-Za-z0-9_]+)\.[A-Za-z0-9_]+\(.*\))\s*$", re.DOTALL)


def _classify(message):
    """
    Match a logical log message (timestamp prefix removed) against the known
    line shapes.

    OUTPUT:

    - ``None`` if the message matches no known shape (possibly because it is
      the first part of a multi-line message);
    - ``("hint", table, call)`` for a ``Replicate with db.<table>...`` line;
    - ``("query", duration, sql)`` for a ``... ran in <t>s`` line;
    - ``("iterator", duration, querydict, table)`` for a search iterator line.
    """
    message = _ANSI_RE.sub("", message)
    m = _REPLICATE_RE.match(message)
    if m:
        return ("hint", m.group(2), m.group(1))
    m = _RAN_IN_RE.search(message)
    if m:
        return ("query", float(m.group(1)), message[:m.start()])
    m = _ITERATOR_RE.match(message)
    if m:
        return ("iterator", float(m.group(3)), m.group(2), m.group(1))
    return None


def _make_record(timestamp, nlines, kind):
    """
    Build a parsed-record dictionary from a buffered message.
    """
    if timestamp is not None:
        try:
            timestamp = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f")
        except ValueError:
            timestamp = None
    rec = {
        "timestamp": timestamp,
        "duration": kind[1],
        "query": kind[2],
        "kind": kind[0],
        "table": kind[3] if kind[0] == "iterator" else None,
        "replicate": None,
        "lines": nlines,
    }
    return rec


def parse_slow_log(logfile, stats=None):
    """
    Iterate over the records in a slow-query log file.

    INPUT:

    - ``logfile`` -- the filename of a slow-query log (as configured by the
      ``slowlogfile`` logging option), or an open file object
    - ``stats`` -- an optional dictionary, updated in place with the keys
      ``lines`` (physical lines read) and ``unparsed`` (lines that were not
      part of any recognized record)

    OUTPUT:

    An iterator of dictionaries, one per logged query, with keys:

    - ``timestamp`` -- a datetime from the logging prefix (None if absent)
    - ``duration`` -- the logged runtime in seconds, as a float
    - ``query`` -- the SQL that was logged; for search iterator records,
      the query dictionary as a string
    - ``kind`` -- ``"query"`` for ordinary statements, ``"iterator"`` for
      ``Search iterator`` records
    - ``table`` -- the table the query was issued against, when the log
      provides it (from the adjacent ``Replicate with`` hint line, or from
      the search iterator line); otherwise None.  A hint is attached only
      when its table appears in the query, since several processes
      appending to one log can interleave their lines.
    - ``replicate`` -- the ``db.<table>.analyze(...)`` call from the hint
      line when present, for replaying the query
    - ``lines`` -- the number of physical log lines this record occupies,
      including its hint line and any continuation lines

    Multi-line SQL (inlined values containing newlines) is reassembled.
    Unrecognized lines are skipped and counted in ``stats``, since a log
    that has accumulated for years contains lines in formats no longer in
    use.
    """
    if stats is None:
        stats = {}
    stats["lines"] = 0
    stats["unparsed"] = 0
    if hasattr(logfile, "read"):
        for rec in _parse_stream(logfile, stats):
            yield rec
    else:
        with open(logfile, "r", errors="replace") as F:
            for rec in _parse_stream(F, stats):
                yield rec


def _parse_stream(F, stats):
    """
    The implementation of ``parse_slow_log`` on an open file object.

    Physical lines are grouped into logical messages: a line with a
    timestamp prefix starts a new message, and a line without one continues
    the previous message when that message is not yet complete.  A parsed
    query record is held back until the next message so that a following
    ``Replicate with`` hint can be attached to it.
    """
    pending = None  # a query record awaiting a possible hint line
    buf = None  # [timestamp, message, nlines, classification]
    lines = iter(F)
    while True:
        raw = next(lines, None)
        if raw is None:
            new = None
        else:
            stats["lines"] += 1
            line = raw.rstrip("\r\n")
            tm = _TIMESTAMP_RE.match(line)
            if tm is None and buf is not None and buf[3] is None:
                # continuation of a multi-line message
                buf[1] += "\n" + line
                buf[2] += 1
                buf[3] = _classify(buf[1])
                continue
            if tm is None:
                new = [None, line, 1, _classify(line)]
            else:
                message = line[tm.end():]
                new = [tm.group(1), message, 1, _classify(message)]
        if buf is not None:
            kind = buf[3]
            if kind is None:
                stats["unparsed"] += buf[2]
            elif kind[0] == "hint":
                # Attach the hint to the preceding query, but only if its
                # table appears there: several processes appending to one log
                # can interleave a hint with another process' query.
                if (
                    pending is not None
                    and pending["kind"] == "query"
                    and re.search(r"\b%s\b" % re.escape(kind[1]), pending["query"])
                ):
                    pending["table"] = kind[1]
                    pending["replicate"] = kind[2]
                    pending["lines"] += buf[2]
                    yield pending
                    pending = None
                else:
                    # a hint with no query to attach to
                    stats["unparsed"] += buf[2]
            else:
                if pending is not None:
                    yield pending
                pending = _make_record(buf[0], buf[2], kind)
        if raw is None:
            break
        buf = new
    if pending is not None:
        yield pending


##################################################################
# Query normalization                                            #
##################################################################

_NUMBER_RE = re.compile(_NUMBER)
_ARRAY_RE = re.compile(r"\bARRAY\[")
_ARRAY_CONTENT_RE = re.compile(r"^[?,\s\[\]]*$")
_PLACEHOLDER_LIST_RE = re.compile(r"\(\s*\?\s*(?:,\s*\?\s*)+\)")
_DUP_CLAUSE_RE = re.compile(r"(?<![\w\"'?])((?:NOT )?[^()]{1,400}?) (OR|AND) \1(?![\w\"'?])")
_JSONB_PATH_OPS = ("->", "->>", "#>", "#>>")


def normalize_query(query):
    """
    Normalize an SQL query (or a query dictionary rendered as a string) to
    its *shape*: literal values are replaced by ``?`` so that queries
    differing only in their constants compare equal.

    - numbers, quoted strings and boolean literals become ``?`` (string
      literals directly after the jsonb path operators ``->``, ``->>``,
      ``#>``, ``#>>`` are kept, since they select which field is queried
      rather than which value)
    - the contents of ``ARRAY[...]`` literals collapse to ``ARRAY[?]``
    - comma-separated lists of replaced values, as in ``IN (1, 2, 3)``,
      collapse to ``(?)``
    - a clause repeated with ``OR`` (or ``AND``), as produced for example
      by ``$in`` on a jsonb column, collapses to a single copy
    - runs of whitespace (including newlines) become a single space

    Identifiers are left alone, even when they contain digits.

    EXAMPLES::

        sage: from psycodict.slowlog import normalize_query
        sage: normalize_query("SELECT \\"a\\" FROM \\"t\\" WHERE \\"n\\" = 5 AND \\"s\\" = 'x1' LIMIT 4")
        'SELECT "a" FROM "t" WHERE "n" = ? AND "s" = ? LIMIT ?'
    """
    out = []
    tail = ""  # the last few non-space characters emitted
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if c == "'":
            # a string literal, with '' escapes
            j = i + 1
            while j < n:
                if query[j] == "'":
                    if j + 1 < n and query[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if tail.endswith(_JSONB_PATH_OPS):
                # part of the query shape, not a value
                out.append(query[i:j + 1])
                tail = (tail + query[i:j + 1])[-4:]
            else:
                out.append("?")
                tail = (tail + "?")[-4:]
            i = j + 1
        elif c == '"':
            # a quoted identifier, with "" escapes
            j = i + 1
            while j < n:
                if query[j] == '"':
                    if j + 1 < n and query[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            out.append(query[i:j + 1])
            tail = (tail + query[i:j + 1])[-4:]
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (query[j].isalnum() or query[j] == "_"):
                j += 1
            word = query[i:j]
            if word in ("E", "e") and j < n and query[j] == "'":
                # an E'...' escaped string: skip the prefix, the literal
                # itself is handled (and replaced) on the next pass
                i = j
                continue
            if word in ("true", "false", "TRUE", "FALSE"):
                out.append("?")
                tail = (tail + "?")[-4:]
            else:
                out.append(word)
                tail = (tail + word)[-4:]
            i = j
        elif c.isdigit() or (c in ".-" and i + 1 < n and query[i + 1].isdigit()):
            if c == "-" and (tail[-1:].isalnum() or tail[-1:] in ")?\"'"):
                # binary minus, not a sign
                out.append(c)
                tail = (tail + c)[-4:]
                i += 1
                continue
            j = i + (1 if c == "-" else 0)
            m = _NUMBER_RE.match(query, j)
            if m is None:
                # "." or "-" not actually starting a number
                out.append(c)
                tail = (tail + c)[-4:]
                i += 1
                continue
            out.append("?")
            tail = (tail + "?")[-4:]
            i = m.end()
        elif c.isspace():
            if out and not out[-1].endswith(" "):
                out.append(" ")
            i += 1
        else:
            out.append(c)
            tail = (tail + c)[-4:]
            i += 1
    s = "".join(out).strip()
    s = _collapse_arrays(s)
    s = _PLACEHOLDER_LIST_RE.sub("(?)", s)
    s = _collapse_repeats(s)
    return s


def _collapse_arrays(s):
    """
    Replace the contents of ``ARRAY[...]`` literals (whose values have
    already been replaced by ``?``) with a single ``?``.
    """
    out = []
    pos = 0
    for m in _ARRAY_RE.finditer(s):
        if m.start() < pos:
            continue
        # find the matching closing bracket
        depth = 0
        for j in range(m.end() - 1, len(s)):
            if s[j] == "[":
                depth += 1
            elif s[j] == "]":
                depth -= 1
                if depth == 0:
                    break
        else:
            break  # unbalanced; leave the rest alone
        content = s[m.end():j]
        if _ARRAY_CONTENT_RE.match(content):
            out.append(s[pos:m.end()])
            out.append("?")
            pos = j
    out.append(s[pos:])
    return "".join(out)


def _collapse_repeats(s):
    """
    Collapse a clause repeated with ``OR`` or ``AND`` (with identical text
    after normalization) into a single copy, so that e.g. the expansion of
    ``$in`` on a jsonb column groups independently of the list length.
    """
    for _ in range(30):
        new = _DUP_CLAUSE_RE.sub(r"\1", s)
        if new == s:
            break
        s = new
    return s


##################################################################
# Aggregation and reporting                                      #
##################################################################

# Candidate values for raising slowcutoff, in seconds
_LADDER = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)

_TABLES_RE = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+(?:"([A-Za-z_][A-Za-z0-9_]*)"|([A-Za-z_][A-Za-z0-9_]*))')
_NONTABLE_WORDS = frozenset(["SELECT", "STDIN", "STDOUT", "UNNEST"])


def _tables_in_sql(sql):
    """
    The table names appearing after FROM/JOIN/INTO/UPDATE in an SQL string.
    This is a heuristic (it does not parse SQL), but psycodict's generated
    queries quote their table names, making them easy to find.
    """
    found = set()
    for m in _TABLES_RE.finditer(sql):
        name = m.group(1) or m.group(2)
        if name.upper() not in _NONTABLE_WORDS:
            found.add(name)
    return found


def _bucket(duration):
    """
    Round a duration down to 3 significant digits, for the histogram used
    to compute percentiles and threshold counts with bounded memory.
    """
    if duration <= 0:
        return 0.0
    scale = 10.0 ** (floor(log10(duration)) - 2)
    return floor(duration / scale) * scale


def _percentile(buckets, total, p):
    """
    The ``p``-th percentile (nearest-rank) of a duration histogram given as
    a sorted list of ``(bucket, count)`` pairs with ``total`` entries.
    """
    if total == 0:
        return None
    rank = max(1, int(ceil(p / 100.0 * total)))
    cumulative = 0
    for value, count in buckets:
        cumulative += count
        if cumulative >= rank:
            return value
    return buckets[-1][0]


def slow_query_report(logfile, top=20, cutoff=None, db=None):
    """
    Analyze a slow-query log file, grouping queries by shape.

    INPUT:

    - ``logfile`` -- the filename of a slow-query log (as configured by the
      ``slowlogfile`` logging option), or an open file object
    - ``top`` -- the number of query shapes to include, ordered by total time
    - ``cutoff`` -- only consider queries at least this slow (in seconds).
      This simulates raising ``slowcutoff``: the report shows what the log
      would have contained with that threshold.
    - ``db`` -- an optional ``PostgresDatabase``.  When provided, the
      suggestions check the columns constrained by each query shape against
      the indexes recorded in ``meta_indexes`` (via ``list_indexes``) for
      the search tables involved, and suggest ``create_index`` calls for
      constrained columns that no existing index leads with.

    OUTPUT:

    A dictionary with keys:

    - ``logfile``, ``cutoff`` -- the corresponding inputs
    - ``lines`` -- the number of physical lines in the file
    - ``unparsed`` -- lines that were not part of any recognized record
    - ``records`` -- the number of parsed query records
    - ``skipped`` -- records below ``cutoff`` (0 when no cutoff is given);
      the rest of the report describes the ``records - skipped`` others
    - ``total_time`` -- their summed duration in seconds
    - ``percentiles`` -- a dictionary with keys ``p50``, ``p90``, ``p99``
      and ``max``.  The percentiles are computed from a histogram with 3
      significant digits, so they are lower bounds accurate to about 1%;
      the max is exact.
    - ``thresholds`` -- a list of dictionaries with keys ``cutoff``,
      ``records``, ``lines`` and ``percent``: raising ``slowcutoff`` to
      that value would have logged that many records, occupying that many
      physical lines, i.e. that percentage of the current line volume.
      The candidate cutoffs mix a fixed ladder with the observed
      percentiles.
    - ``shapes`` -- a list of dictionaries, one per query shape, sorted by
      total time, with keys ``shape``, ``kind``, ``count``, ``total``,
      ``mean``, ``max``, ``tables``, ``example`` (the slowest query seen
      with this shape), ``replicate`` (its hint-line call, if any) and
      ``suggestions`` (a list of strings; see the module documentation).
    """
    stats = {}
    shapes = {}
    histogram = defaultdict(lambda: [0, 0])  # bucket -> [records, lines]
    records = skipped = considered_lines = 0
    total_time = 0.0
    max_duration = None
    for rec in parse_slow_log(logfile, stats=stats):
        records += 1
        duration = rec["duration"]
        if cutoff is not None and duration < cutoff:
            skipped += 1
            continue
        total_time += duration
        considered_lines += rec["lines"]
        if max_duration is None or duration > max_duration:
            max_duration = duration
        entry = histogram[_bucket(duration)]
        entry[0] += 1
        entry[1] += rec["lines"]
        shape = normalize_query(rec["query"])
        if rec["kind"] == "iterator":
            shape = "Search iterator for %s %s" % (rec["table"], shape)
        data = shapes.get(shape)
        if data is None:
            tables = _tables_in_sql(shape) if rec["kind"] == "query" else set()
            data = shapes[shape] = {
                "shape": shape,
                "kind": rec["kind"],
                "count": 0,
                "total": 0.0,
                "max": 0.0,
                "tables": tables,
                "example": rec["query"],
                "replicate": rec["replicate"],
            }
        data["count"] += 1
        data["total"] += duration
        if rec["table"]:
            data["tables"].add(rec["table"])
        if duration >= data["max"]:
            data["max"] = duration
            data["example"] = rec["query"]
            if rec["replicate"]:
                data["replicate"] = rec["replicate"]

    considered = records - skipped
    buckets = sorted((value, counts[0]) for value, counts in histogram.items())
    percentiles = {
        "p50": _percentile(buckets, considered, 50),
        "p90": _percentile(buckets, considered, 90),
        "p99": _percentile(buckets, considered, 99),
        "max": max_duration,
    }
    thresholds = []
    if considered:
        candidates = {percentiles["p50"], percentiles["p90"], percentiles["p99"]}
        candidates.update(c for c in _LADDER if c <= max_duration)
        if cutoff is not None:
            candidates = {c for c in candidates if c >= cutoff}
        for c in sorted(candidates):
            kept_records = kept_lines = 0
            for value, counts in histogram.items():
                if value >= c:
                    kept_records += counts[0]
                    kept_lines += counts[1]
            thresholds.append({
                "cutoff": c,
                "records": kept_records,
                "lines": kept_lines,
                "percent": 100.0 * kept_lines / considered_lines,
            })

    top_shapes = sorted(shapes.values(), key=lambda data: -data["total"])[:top]
    for data in top_shapes:
        data["mean"] = data["total"] / data["count"]
        data["tables"] = sorted(data["tables"])
        data["suggestions"] = _suggestions(data, db)

    return {
        "logfile": getattr(logfile, "name", logfile),
        "cutoff": cutoff,
        "lines": stats["lines"],
        "unparsed": stats["unparsed"],
        "records": records,
        "skipped": skipped,
        "total_time": total_time,
        "percentiles": percentiles,
        "thresholds": thresholds,
        "shapes": top_shapes,
    }


##################################################################
# Suggestions                                                    #
##################################################################

# A column reference as it appears in psycodict-generated SQL: a quoted
# identifier, optionally followed by jsonb path accesses or array slices
_COLREF = r'"(?P<col>[A-Za-z_][A-Za-z0-9_]*)"(?P<path>(?:(?:->>?|#>>?)(?:\'(?:[^\']|\'\')*\'|\?)|\[[^\]]*\])*)'
_CONSTRAINT_RE = re.compile(_COLREF + r"\s*(?P<op>=\s*ANY\s*\(|@>|<@|&&|<=|>=|!=|<>|=|<|>)")
_LIKE_RE = re.compile(_COLREF + r"\s+(?:NOT\s+)?(?:LIKE|ILIKE)\s+(?P<pat>'(?:[^']|'')*')", re.IGNORECASE)
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\s+(?P<cols>[^()]*?)(?:\s+LIMIT\b|\s+OFFSET\b|\)|$)", re.IGNORECASE | re.DOTALL)
_SORT_COL_RE = re.compile(r'^"([A-Za-z_][A-Za-z0-9_]*)"')
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)


def _abbreviate(text, length=40):
    if len(text) > length:
        return text[:length] + "..."
    return text


def _constrained_columns(example):
    """
    Extract the constrained columns of a query, from its text.

    INPUT:

    - ``example`` -- an SQL query as logged (with its literal values)

    OUTPUT:

    A dictionary with keys ``equality``, ``range``, ``containment`` (the
    array/jsonb operators ``@>``, ``<@``, ``&&``), ``path`` (equality on a
    path inside a jsonb column), ``like`` (a list of (column, pattern)
    pairs) and ``order`` (the columns of the outermost ORDER BY), each a
    list of column names in order of first appearance.  Only quoted column
    names are recognized, which is how psycodict renders columns; the
    parsing is heuristic and makes no attempt to understand subqueries.
    """
    found = {"equality": [], "range": [], "containment": [], "path": [], "like": [], "order": []}

    def add(kind, col):
        if col not in found[kind]:
            found[kind].append(col)

    m = _WHERE_RE.search(example)
    if m:
        where = example[m.end():]
        tail = _ORDER_BY_RE.search(where)
        if tail:
            where = where[:tail.start()]
        for m in _CONSTRAINT_RE.finditer(where):
            col, path, op = m.group("col"), m.group("path"), m.group("op")
            op = op.rstrip()
            if op.startswith("=") and op.endswith("("):
                op = "ANY"
            if op in ("!=", "<>"):
                continue  # inequality does not benefit from an index
            if path:
                # a jsonb path or an array slice: a plain index on the
                # column does not support these directly
                if ("->" in path or "#>" in path) and op in ("=", "ANY"):
                    add("path", col)
                continue
            if op in ("=", "ANY"):
                add("equality", col)
            elif op in ("<", "<=", ">", ">="):
                add("range", col)
            else:  # @>, <@, &&
                add("containment", col)
        for m in _LIKE_RE.finditer(where):
            found["like"].append((m.group("col"), m.group("pat").strip("'")))
    m = _ORDER_BY_RE.search(example)
    if m:
        for piece in m.group("cols").split(","):
            cm = _SORT_COL_RE.match(piece.strip())
            if cm:
                add("order", cm.group(1))
    return found


def _leading_index_columns(db, tables):
    """
    For each of the given tables known to ``db``, the set of columns that
    some index leads with, grouped by index type.

    OUTPUT:

    A dictionary ``{table: {index_type: set of first columns}}``, with only
    the tables present in ``db.tablenames`` as keys.
    """
    leaders = {}
    for tname in tables:
        if tname in db.tablenames:
            per_type = defaultdict(set)
            for info in db[tname].list_indexes().values():
                columns = info.get("columns") or []
                if columns:
                    per_type[info.get("type") or "btree"].add(columns[0])
            leaders[tname] = per_type
    return leaders


def _owning_table(col, tables, db):
    """
    The table among ``tables`` (already filtered to ``db.tablenames``)
    having ``col`` as a search column, or None.
    """
    for tname in tables:
        if col in db[tname].search_cols:
            return tname
    return None


def _suggestions(data, db):
    """
    Heuristic suggestions for one query shape; see ``slow_query_report``.

    Each suggestion states what was observed in the query text and what to
    try.  With ``db`` provided, equality/range/containment observations are
    checked against the indexes recorded in ``meta_indexes`` and reported
    only when no index leads with the constrained column.
    """
    if data["kind"] != "query":
        return []
    example = data["example"]
    found = _constrained_columns(example)
    suggestions = []

    checked = None
    if db is not None:
        checked = _leading_index_columns(db, data["tables"])

        def leaders(tname, types=None):
            per_type = checked.get(tname, {})
            cols = set()
            for typ, first in per_type.items():
                if types is None or typ in types:
                    cols |= first
            return cols

        for kind, types, description in [
            ("equality", None, "an equality constraint"),
            ("range", ("btree",), "a range constraint"),
        ]:
            for col in found[kind]:
                if col == "id":
                    continue  # id always has its primary key index
                tname = _owning_table(col, checked, db)
                if tname is not None and col not in leaders(tname, types):
                    suggestions.append(
                        '"%s" appears in %s but no index on %s leads with it; '
                        "consider db.%s.create_index(['%s'])"
                        % (col, description, tname, tname, col)
                    )
        for col in found["containment"]:
            tname = _owning_table(col, checked, db)
            if tname is not None and col not in leaders(tname, ("gin",)):
                suggestions.append(
                    '"%s" is filtered with a containment/overlap operator (@>, <@, &&) '
                    "but has no GIN index; consider db.%s.create_index(['%s'], type='gin')"
                    % (col, tname, col)
                )
        if found["order"]:
            first = found["order"][0]
            tname = _owning_table(first, checked, db)
            if tname is not None and first not in leaders(tname, ("btree",)):
                suggestions.append(
                    "the sort ORDER BY %s is not supported by an index leading "
                    "with \"%s\"; consider db.%s.create_index(%s)"
                    % (", ".join('"%s"' % c for c in found["order"]), first, tname, found["order"])
                )
    else:
        observed = [
            (kind, found[kind])
            for kind in ("equality", "range", "containment", "order")
            if found[kind]
        ]
        if observed:
            suggestions.append(
                "columns constrained in this shape -- %s -- pass db= to check "
                "them against existing indexes"
                % "; ".join(
                    "%s: %s" % (kind, ", ".join('"%s"' % c for c in cols))
                    for kind, cols in observed
                )
            )

    for col in found["path"]:
        suggestions.append(
            'the query compares a path inside the jsonb column "%s" with =, which no '
            "btree or GIN index supports directly; consider restructuring as a "
            "containment ($contains) query backed by a GIN index, or an expression index"
            % col
        )
    for col, pattern in found["like"]:
        if pattern.startswith("%") or pattern.startswith("_"):
            suggestions.append(
                'LIKE pattern with a leading wildcard (%s) on "%s" cannot use a btree '
                "index; consider a trigram (pg_trgm) GIN index or anchoring the pattern"
                % (_abbreviate(pattern), col)
            )

    # the same create_index call can be suggested through several routes
    seen = set()
    unique = []
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


##################################################################
# Printing                                                       #
##################################################################


def _fmt_duration(t):
    if t is None:
        return "-"
    return "%.3gs" % t


def show_slow_report(logfile, top=20, cutoff=None, db=None):
    """
    Print the report produced by ``slow_query_report``; see its
    documentation for the inputs.

    EXAMPLES::

        sage: from psycodict.slowlog import show_slow_report
        sage: show_slow_report("slow_queries.log", db=db)  # db optional
    """
    report = slow_query_report(logfile, top=top, cutoff=cutoff, db=db)
    print(
        "Slow query log %s: %s lines, %s parsed records (%s unparsed lines)"
        % (report["logfile"], report["lines"], report["records"], report["unparsed"])
    )
    if cutoff is not None:
        print(
            "Only queries taking at least %s are considered (%s records below the cutoff)"
            % (_fmt_duration(cutoff), report["skipped"])
        )
    if report["records"] == report["skipped"]:
        print("No slow queries to analyze")
        return
    p = report["percentiles"]
    print(
        "Total query time %.3fs; durations p50 %s, p90 %s, p99 %s, max %s"
        % (report["total_time"], _fmt_duration(p["p50"]), _fmt_duration(p["p90"]),
           _fmt_duration(p["p99"]), _fmt_duration(p["max"]))
    )
    if report["thresholds"]:
        print("With a higher slowcutoff, the log would have kept:")
        print("    %10s %12s %12s %12s" % ("cutoff", "records", "lines", "% of lines"))
        for row in report["thresholds"]:
            print(
                "    %10s %12s %12s %11.1f%%"
                % (_fmt_duration(row["cutoff"]), row["records"], row["lines"], row["percent"])
            )
    print("Top %s query shapes by total time:" % len(report["shapes"]))
    for i, data in enumerate(report["shapes"], 1):
        tables = ", ".join(data["tables"])
        print(
            "#%s: %s times, total %.3fs (mean %.3fs, max %.3fs)%s"
            % (i, data["count"], data["total"], data["mean"], data["max"],
               " on " + tables if tables else "")
        )
        print("    %s" % data["shape"])
        if data["count"] > 1 or data["example"] != data["shape"]:
            print("    example: %s" % _abbreviate(data["example"], 500))
        if data["replicate"]:
            print("    replicate: %s" % _abbreviate(data["replicate"], 500))
        for suggestion in data["suggestions"]:
            print("    * %s" % suggestion)
