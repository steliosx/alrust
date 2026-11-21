#!/usr/bin/env python3
"""Differential check of alerta_fast.py's `noecho` patch against Alerta as
released, on a real database. Run with the reference Alerta environment's
Python and DATABASE_URL set to a copy of the seeded database:

    <venv>/bin/python -I scripts/bench/fastpath_check.py <result.json>

Equal cases. For each case two copies of one seeded alert are made with the
same stored tags and attributes (the attributes from raw JSON text, so a
number keeps the digits written here). Alerta's own function runs on one
and the patched function on the other, with equal arguments. Then:
  - the stored attributes must be the same jsonb text, and the stored tags
    the same list after sorting (the patch's file header: order);
  - what the two functions returned must agree in the same way;
  - both must raise, or neither, and the same exception type;
  - Alerta's function must have written its row (the check sees writes);
  - the patched function must have written, or not, as the case says.
Every state is read on a second connection, so only what was committed
counts, and nothing in the check commits the request's connection after
the call. A write is seen as a new `xmin` of the row.

Commit boundary. Before each call the request's connection changes the
`text` of one more row and does not commit. After the call, the second
connection must see that text, for Alerta's function and the patched one,
whether the write was left out or not.

Other ids. Alerta's `id LIKE '<id>%'` also matches other rows. With rows
whose ids collide (one id begins with the other; or `_` in the id used as
a wildcard), Alerta's function must change the other row, and the patched
function must leave it when it leaves the write out, and change it as
Alerta does when it writes. This is the documented difference; the case
fails when either side does anything else.

Exit status 1 on any failure.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import NamedTupleCursor

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
# name, stored attributes (raw JSON text), the alert's attributes, the new
# attributes, must the patched function write?
ATTR_CASES = [
    ("same", '{"region": "eu", "n": 1}', {"region": "eu", "n": 1}, {"region": "eu", "n": 1}, False),
    ("same, other key order", '{"b": 1, "a": 2}', {"a": 2, "b": 1}, {}, False),
    ("same, nested", '{"a": {"b": [1, "x", null, true, false]}}',
     {"a": {"b": [1, "x", None, True, False]}}, {}, False),
    ("both empty", "{}", {}, {}, False),
    ("new value", '{"region": "eu"}', {"region": "eu"}, {"region": "us"}, True),
    ("new key", '{"region": "eu"}', {"region": "eu"}, {"site": "a"}, True),
    ("null removes a key", '{"region": "eu", "site": "a"}', {"region": "eu", "site": "a"},
     {"site": None}, True),
    ("null for a key not stored", '{"region": "eu"}', {"region": "eu"}, {"site": None}, False),
    ("stale alert object", '{"region": "eu", "site": "a"}', {"region": "eu"}, {}, True),
    ("stored null value", '{"region": "eu", "site": null}', {"region": "eu"}, {}, True),
    ("stored attributes NULL", "null", {}, {}, True),
    ("1.0 and 1.0", '{"n": 1.0}', {"n": 1.0}, {}, False),
    ("1 and 1.0", '{"n": 1}', {"n": 1.0}, {}, True),
    ("1.0 and 1", '{"n": 1.0}', {"n": 1}, {}, True),
    ("1.0000000000000001 and 1.0", '{"n": 1.0000000000000001}', {"n": 1.0}, {}, True),
    ("1.50 and 1.5", '{"n": 1.50}', {"n": 1.5}, {}, True),
    ("1e2 and 100", '{"n": 1e2}', {"n": 100}, {}, False),
    ("20-digit integer", '{"n": 12345678901234567890}', {"n": 12345678901234567890}, {}, False),
    ("1 and true", '{"n": 1}', {"n": True}, {}, True),
    ("true and true", '{"n": true}', {"n": True}, {}, False),
    ("false and 0", '{"n": false}', {"n": 0}, {}, True),
    ('"1" and 1', '{"n": "1"}', {"n": 1}, {}, True),
]

COPY = """
    INSERT INTO alerts
    SELECT (jsonb_populate_record(a, %(over)s::jsonb)).*
      FROM alerts a WHERE a.resource = 'host-0000'
"""
STATE = ("SELECT tags, attributes::text AS attributes, text, xmin::text AS xmin "
         "FROM alerts WHERE id=%(id)s")
MARK = "UPDATE alerts SET text=%(text)s WHERE id=%(id)s"

second = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=NamedTupleCursor)
second.autocommit = True


def make(stored_tags, stored_attrs="{}", id=None):
    """A new, committed row with these stored values; returns its id."""
    new = id or str(uuid.uuid4())
    over = ('{"id": %s, "resource": %s, "tags": %s, "attributes": %s}'
            % (json.dumps(new), json.dumps(new), json.dumps(stored_tags), stored_attrs))
    cursor = second.cursor()
    cursor.execute(COPY, {"over": over})
    if cursor.rowcount != 1:
        raise SystemExit(f"the copy made {cursor.rowcount} rows; the seeded alert host-0000 is needed")
    return new


def state(id):
    """The committed row, as the second connection sees it."""
    cursor = second.cursor()
    cursor.execute(STATE, {"id": id})
    return cursor.fetchone()


def call(function, *args):
    """Runs the call as a request would: an earlier statement of the same
    request is open on the connection. Returns (kind, value, marker seen):
    what the call returned or the type it raised, and whether the second
    connection then sees the earlier statement's change."""
    marker_row, marker = make([]), str(uuid.uuid4())
    cursor = db.get_db().cursor()
    cursor.execute(MARK, {"id": marker_row, "text": marker})
    try:
        kind, value = "returned", function(*args)
    except Exception as e:  # compared by type with the other side
        kind, value = "raised", type(e).__name__
    seen = state(marker_row).text == marker
    db.get_db().rollback()
    return kind, value, seen


def tags_of(value):
    try:
        return sorted(value)
    except TypeError:
        return value


def compare(a, p, key, must_write):
    return [what for what, bad in (
        (f"stored {key} differ", a[key] != p[key]),
        ("results differ", (a["kind"], a["result"]) != (p["kind"], p["result"])),
        ("Alerta's function did not write", not a["wrote"]),
        (f"patched wrote {p['wrote']}, wanted {must_write}", p["wrote"] != must_write),
        ("Alerta's function did not commit the earlier statement", not a["earlier_committed"]),
        ("patched did not commit the earlier statement", not p["earlier_committed"])) if bad]


def main(out):
    results = []
    functions = {"update_tags": (af._orig_update_tags, type(db).update_tags),
                 "update_attributes": (af._orig_update_attributes, type(db).update_attributes)}
    with af.app.app_context():
        for name, stored, sent, must_write in TAG_CASES:
            sides = {}
            for side, function in zip(("alerta", "patched"), functions["update_tags"]):
                id = make(stored)
                before = state(id)
                kind, value, seen = call(function, db, id, list(sent))
                after = state(id)
                sides[side] = {"kind": kind, "wrote": after.xmin != before.xmin, "earlier_committed": seen,
                               "tags": tags_of(after.tags),
                               "result": tags_of(value.tags) if kind == "returned" and value is not None
                               else value}
            results.append({"function": "update_tags", "case": name, **sides,
                            "problems": compare(sides["alerta"], sides["patched"], "tags", must_write)})
        for name, stored, held, new, must_write in ATTR_CASES:
            sides = {}
            for side, function in zip(("alerta", "patched"), functions["update_attributes"]):
                id = make(["t"], stored)
                before = state(id)
                kind, value, seen = call(function, db, id, json.loads(json.dumps(held)), dict(new))
                after = state(id)
                sides[side] = {"kind": kind, "wrote": after.xmin != before.xmin, "earlier_committed": seen,
                               "attributes_before": before.attributes, "attributes": after.attributes,
                               "result": json.dumps(value, sort_keys=True) if kind == "returned" else value}
            results.append({"function": "update_attributes", "case": name, **sides,
                            "problems": compare(sides["alerta"], sides["patched"], "attributes", must_write)})
        # An id with no row: both sides must do the same.
        missing = str(uuid.uuid4())
        for function_name, args in (("update_tags", (["a"],)), ("update_attributes", ({}, {"k": "v"}))):
            a = call(functions[function_name][0], db, missing, *args)[:2]
            p = call(functions[function_name][1], db, missing, *args)[:2]
            results.append({"function": function_name, "case": "no such id",
                            "alerta": {"kind": a[0], "result": a[1]},
                            "patched": {"kind": p[0], "result": p[1]},
                            "problems": [] if a == p else [f"Alerta {a!r}, patched {p!r}"]})
        # Other ids that Alerta's LIKE matches: the documented difference.
        for name, id_of, other_of in (
                ("id begins with this id", lambda u: u, lambda u: u + "-more"),
                ("_ in this id is a wildcard", lambda u: u + "_w", lambda u: u + "Zw")):
            for sent, skip in ((["a"], True), (["b"], False)):
                sides = {}
                for side, function in zip(("alerta", "patched"), functions["update_tags"]):
                    u = str(uuid.uuid4())
                    id, other = make(["a"], id=id_of(u)), make(["other"], id=other_of(u))
                    kind, value, seen = call(function, db, id, list(sent))
                    sides[side] = {"kind": kind, "earlier_committed": seen,
                                   "tags": state(id).tags, "other_tags": state(other).tags}
                a, p = sides["alerta"], sides["patched"]
                wanted = ["other"] if skip else sent
                problems = [what for what, bad in (
                    ("Alerta's function did not change the other row", a["other_tags"] != sent),
                    (f"patched left the other row as {p['other_tags']}, wanted {wanted}",
                     p["other_tags"] != wanted),
                    ("this row differs", a["tags"] != p["tags"] or a["tags"] != sent),
                    ("a call raised", a["kind"] != "returned" or p["kind"] != "returned"),
                    ("an earlier statement was not committed",
                     not a["earlier_committed"] or not p["earlier_committed"])) if bad]
                results.append({"function": "update_tags", "problems": problems, **sides,
                                "case": f"{name}; {'same' if skip else 'other'} tags sent"})
    failed = sum(1 for r in results if r["problems"])
    Path(out).write_text(json.dumps({"ok": not failed, "cases": len(results), "failed": failed,
                                     "results": results}, indent=1, default=str))
    for r in results:
        if r["problems"]:
            print(f"FAILED {r['function']} / {r['case']}: {r['problems']}", file=sys.stderr)
    print(f"noecho differential check: {len(results) - failed} of {len(results)} cases agree")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
