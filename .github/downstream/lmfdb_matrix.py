"""
Build the job matrix for a full run of LMFDB's test suite.

LMFDB splits its own suite into hand-balanced groups in
`.github/workflows/matrix_includes.json` -- the slow files (lfunctions,
classical modular forms, abstract groups) each get a group to themselves.
Reusing that split keeps our parallelism sensible without us having to
re-tune it every time LMFDB's tests grow, and it stays correct as they edit
it, because we read the file at run time rather than copying it.

Their matrix does not cover quite everything, though: a handful of test files
exist that no group lists.  Anything left over is collected into a final
group, so that "full" really means every test file in the checkout.

Usage:  python lmfdb_matrix.py <path to lmfdb checkout>
Prints a JSON object suitable for `strategy: matrix: ${{ fromJson(...) }}`.
"""
import json
import os
import sys

# These two are the only tests that reach the public internet -- they check
# that third-party links still resolve.  That is a useful thing for LMFDB to
# check and a terrible thing to gate a psycodict release on, since it fails
# for reasons no change to this library could cause.
EXCLUDED = {
    "lmfdb/tests/test_homepage.py",
    "lmfdb/tests/test_workshoplinks.py",
}


def find_test_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "lmfdb")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py"):
                path = os.path.join(dirpath, name)
                found.append(os.path.relpath(path, root))
    return sorted(found)


def label_for(files):
    """
    A short job label: the distinct directories the group's files live in.
    """
    folders = sorted({os.path.dirname(f)[len("lmfdb/"):] for f in files})
    label = " ".join(folders)
    return label if len(label) <= 60 else label[:57] + "..."


def main(root):
    with open(os.path.join(root, ".github/workflows/matrix_includes.json")) as F:
        rows = json.load(F)

    groups = []
    covered = set()
    for row in rows:
        # Their matrix runs everything twice, once per server; proddb needs a
        # password we do not have, and "lint" is LMFDB's own linting.
        if row.get("server") != "devmirror" or row.get("files") == "lint":
            continue
        files = [f for f in row["files"].split() if f not in EXCLUDED]
        covered.update(row["files"].split())
        if files:
            groups.append(files)

    everything = [f for f in find_test_files(root) if f not in EXCLUDED]
    missing = [f for f in everything if f not in covered]
    if missing:
        groups.append(missing)

    include = [{"files": " ".join(files), "label": label_for(files)} for files in groups]

    stray = sorted(set(covered) - set(find_test_files(root)) - EXCLUDED)
    print(
        "%d groups, %d files (%d not in LMFDB's own matrix, %d excluded)"
        % (len(include), sum(len(g) for g in groups), len(missing), len(EXCLUDED)),
        file=sys.stderr,
    )
    if missing:
        print("not in LMFDB's matrix: %s" % " ".join(missing), file=sys.stderr)
    if stray:
        # Their matrix names a file that no longer exists; harmless for us
        # (pytest would error), but worth saying out loud.
        print("in LMFDB's matrix but not on disk: %s" % " ".join(stray), file=sys.stderr)
    return {"include": include}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1])))
