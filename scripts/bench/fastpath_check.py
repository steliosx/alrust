#!/usr/bin/env python3
"""Differential check of alerta_fast.py's `noecho` patch against Alerta as
released, on a real database. Run with the reference Alerta environment's
Python and DATABASE_URL set to a copy of the seeded database:

    <venv>/bin/python -I scripts/bench/fastpath_check.py <result.json>

For each case two copies of one seeded alert are made with the same stored
tags and attributes. Alerta's own function runs on one and the patched
function on the other, with equal arguments. Then, for both rows:
  - the stored attributes must be the same JSON text, and the stored tags
    the same list after sorting (the patch's file header: order);
  - what the two functions returned must agree in the same way;
  - both must raise, or neither, and the same exception type;
  - Alerta's function must have written its row (the check sees writes);
  - the patched function must have written, or not, as the case says.
A write is seen as a new `xmin` of the row. Exit status 1 on any failure.
"""

import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["ALERTA_FAST"] = "noecho"

import alerta_fast as af  # noqa: E402
from alerta.app import db  # noqa: E402

# name, stored tags, sent tags, must the patched function write?
TAG_CASES = [
    ("same", ["a", "b"], ["a", "b"], False),
    ("same, other order", ["a", "b"], ["b", "a"], False),
    ("both empty", [], [], False),
    ("one fewer", ["a", "b"], ["a"], True),
    ("one more", ["a"], ["a", "b"], True),
    ("other tag", ["a"], ["b"], True),
    ("stored NULL", None, [], True),
    ("stored twice", ["a", "a"], ["a"], True),
    ("sent twice", ["a"], ["a", "a"], True),
    ("stored NULL element", ["a", None], ["a"], True),
]
# name, stored attributes, the alert's attributes, the new attributes, write?
ATTR_CASES = [
    ("same", {"region": "eu", "n": 1}, {"region": "eu", "n": 1}, {"region": "eu", "n": 1}, False),
    ("same, nested", {"a": {"b": [1, "x", None]}}, {"a": {"b": [1, "x", None]}}, {}, False),
    ("both empty", {}, {}, {}, False),
    ("new value", {"region": "eu"}, {"region": "eu"}, {"region": "us"}, True),
    ("new key", {"region": "eu"}, {"region": "eu"}, {"site": "a"}, True),
    ("null removes a key", {"region": "eu", "site": "a"}, {"region": "eu", "site": "a"}, {"site": None}, True),
    ("null for a key not stored", {"region": "eu"}, {"region": "eu"}, {"site": None}, False),
    ("stale alert object", {"region": "eu", "site": "a"}, {"region": "eu"}, {}, True),
    ("1 and 1.0", {"n": 1}, {"n": 1.0}, {}, True),
    ("1 and true", {"n": 1}, {"n": True}, {}, True),
    ("stored null value", {"region": "eu", "site": None}, {"region": "eu"}, {}, True),
]

COPY = """
    INSERT INTO alerts
    SELECT (jsonb_populate_record(a, %(over)s::jsonb)).*
      FROM alerts a WHERE a.resource = 'host-0000'
"""
STATE = "SELECT tags, attributes::text AS attributes, xmin::text AS xmin FROM alerts WHERE id=%(id)s"


def make(stored_tags, stored_attrs):
    """A new row with these stored values; returns its id."""
    new = str(uuid.uuid4())
    over = {"id": new, "resource": new, "tags": stored_tags, "attributes": stored_attrs}
    cursor = db.get_db().cursor()
    cursor.execute(COPY, {"over": json.dumps(over)})
    if cursor.rowcount != 1:
        raise SystemExit(f"the copy made {cursor.rowcount} rows; the seeded alert host-0000 is needed")
    db.get_db().commit()
    return new


def state(id):
    cursor = db.get_db().cursor()
    cursor.execute(STATE, {"id": id})
    row = cursor.fetchone()
    db.get_db().commit()
    return row


def run(function, *args):
    """(kind, value): what the call returned, or the type it raised."""
    try:
        return "returned", function(*args)
    except Exception as e:  # compared by type with the other side
        db.get_db().rollback()
        return "raised", type(e).__name__


def text(value):
    return json.dumps(value, sort_keys=True)


def tags_of(value):
    try:
        return sorted(value)
    except TypeError:
        return value


def main(out):
    results, failed = [], 0
    with af.app.app_context():
        for name, stored, sent, must_write in TAG_CASES:
            sides = {}
            for side, function in (("alerta", af._orig_update_tags), ("patched", type(db).update_tags)):
                id = make(stored, {})
                before = state(id)
                kind, value = run(function, db, id, list(sent))
                after = state(id)
                sides[side] = {"kind": kind, "wrote": after.xmin != before.xmin,
                               "tags": tags_of(after.tags),
                               "result": tags_of(value.tags) if kind == "returned" and value is not None
                               else value}
            a, p = sides["alerta"], sides["patched"]
            problems = [what for what, bad in (
                ("stored tags differ", a["tags"] != p["tags"]),
                ("results differ", (a["kind"], a["result"]) != (p["kind"], p["result"])),
                ("Alerta's function did not write", not a["wrote"]),
                (f"patched wrote {p['wrote']}, wanted {must_write}", p["wrote"] != must_write)) if bad]
            failed += bool(problems)
            results.append({"function": "update_tags", "case": name, "problems": problems, **sides})
        for name, stored, held, new, must_write in ATTR_CASES:
            sides = {}
            for side, function in (("alerta", af._orig_update_attributes),
                                   ("patched", type(db).update_attributes)):
                id = make(["t"], stored)
                before = state(id)
                kind, value = run(function, db, id, json.loads(json.dumps(held)), dict(new))
                after = state(id)
                sides[side] = {"kind": kind, "wrote": after.xmin != before.xmin,
                               "attributes": after.attributes,
                               "result": text(value) if kind == "returned" else value}
            a, p = sides["alerta"], sides["patched"]
            problems = [what for what, bad in (
                ("stored attributes differ", a["attributes"] != p["attributes"]),
                ("results differ", (a["kind"], a["result"]) != (p["kind"], p["result"])),
                ("Alerta's function did not write", not a["wrote"]),
                (f"patched wrote {p['wrote']}, wanted {must_write}", p["wrote"] != must_write)) if bad]
            failed += bool(problems)
            results.append({"function": "update_attributes", "case": name, "problems": problems, **sides})
        # An id with no row: both sides must do the same.
        missing = str(uuid.uuid4())
        for function_name, args in (("update_tags", (["a"],)), ("update_attributes", ({}, {"k": "v"}))):
            a = run(getattr(af, "_orig_" + function_name), db, missing, *args)
            p = run(getattr(type(db), function_name), db, missing, *args)
            problems = [] if a == p else [f"Alerta {a!r}, patched {p!r}"]
            failed += bool(problems)
            results.append({"function": function_name, "case": "no such id", "problems": problems,
                            "alerta": {"kind": a[0], "result": a[1]},
                            "patched": {"kind": p[0], "result": p[1]}})
    Path(out).write_text(json.dumps({"ok": not failed, "cases": len(results), "failed": failed,
                                     "results": results}, indent=1, default=str))
    for r in results:
        if r["problems"]:
            print(f"FAILED {r['function']} / {r['case']}: {r['problems']}", file=sys.stderr)
    print(f"noecho differential check: {len(results) - failed} of {len(results)} cases agree")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
