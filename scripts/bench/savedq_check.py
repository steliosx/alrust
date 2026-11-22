#!/usr/bin/env python3
"""Check of alerta_fast.py's `savedq` patch against Alerta as released. Run
with the reference Alerta environment's Python and DATABASE_URL set to an
empty database (Alerta makes its tables; the check makes its own alerts and
keys):

    <venv>/bin/python -I scripts/bench/savedq_check.py <result.json> [tests/test_queryparser.py]

The second argument is Alerta's own parser test file (default: the one of
the reference submodule). It is read as text only: the search expressions
of its PostgreSQL test class are taken from it and nothing in it runs.

Parser. For each expression (Alerta's own, more of the same kind, and
texts that the parser refuses) and each default field, Alerta's parser and
the patched parser are called with equal arguments. Then:
  - a first call, a second call and a call after the caller changed the
    dict it got must each give what Alerta's parser gives: the same string
    and an equal dict of exact `str` values, or the same exception type
    and message;
  - two calls never get the same dict object;
  - a refused text is not kept, and the numbers of `savedq_stats` move as
    the patch's text says.
Not kept, and still equal to Alerta: a text over the size limit (counted
in bytes), a text or field that is not exactly `str`, a subclass of the
parser, a replaced grammar object. The store never has more entries than
its limit and drops the one that was used longest ago. Eight threads call
at the same time and every answer is compared.

HTTP, in this process with Flask's test client and a real database. The
same request is sent with Alerta's parser and with the patched parser put
in turn on the same application. Two customers' keys and an admin key send
the same saved search: each must see only its own alerts, with equal
totals, counts and ordered ids on both sides. A new alert and a removed
alert must show in the next answer of a saved search. `q.df` must change
the answer. A refused search must give the same 400 each time. A request
without `q` must not touch the store. A removed key and an expired key
must get 401 for a saved search.

Start-up, each in a new process: the patch is off unless named; with
another queryparser.py the server does not start; with a limit of 0 it
does not start.

Times. A few in-process times are written to the result (the parser alone,
and whole requests through the test client). They are relative to this
runner, with no network, no gunicorn and one thread: not a capacity claim.

Exit status 1 on any failure. No key and no search text of the HTTP part
is written to the result.
"""

import ast
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ALERTA_FAST"] = "savedq"
os.environ["AUTH_REQUIRED"] = "True"
for name in ("ALERTA_FAST_SAVEDQ_ENTRIES", "ALERTA_FAST_SAVEDQ_BYTES"):
    os.environ.pop(name, None)  # the defaults are what is checked

import alerta_fast as af  # noqa: E402
from alerta.database.backends.postgres import queryparser as qp  # noqa: E402

MORE = [
    "severity:(critical OR major) AND status:open",
    "resource:web* AND NOT environment:Development",
    "status:open AND severity:critical AND group:bench AND NOT tags:muted",
    "tags:bench", "attributes.region:eu", "_.region:eu", 'text:"disk full"',
    "id:[a TO f]", "text:καλημέρα", "text:O'Brien", "%(customers)s", "text:%(x)s",
    "__default_field__:x", "100%", "a%%b", "x AND y OR z", "+must -mustnot maybe",
    "value:>10", "value:[1 TO 9]", "resource:/web[0-9]+/", "(a OR b) AND (c OR d)",
]
REFUSED_KIND = ["status:(open", '"abc', "AND", "(", "", " ", "status:", "a AND", ")", "[1 TO"]
FIELDS = (None, "", "text", "resource", "tags", "attributes.region")


def upstream_expressions(path):
    """The `string = '...'` values of Alerta's PostgreSQL parser tests."""
    source = path.read_bytes()
    found = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == "PostgresQueryTestCase":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name) and sub.targets[0].id == "string"
                        and isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str)):
                    found.append(sub.value.value)
    return list(dict.fromkeys(found)), hashlib.sha256(source).hexdigest()


def outcome(function, parser, query, field):
    try:
        sql, params = function(parser, query, field)
    except Exception as e:  # compared by type and message with the other side
        return ("raised", type(e).__name__, str(e))
    return ("returned", sql, params)


def stock(query, field=None, parser=None):
    return outcome(af._orig_parse, parser or qp.QueryParser(), query, field)


def patched(query, field=None, parser=None):
    parser = parser or qp.QueryParser()
    return outcome(type(parser).parse, parser, query, field)


def stats():
    with af._savedq_lock:
        return dict(af.savedq_stats)


def moved(before):
    after = stats()
    return {k: after[k] - before[k] for k in after if after[k] != before[k]}


def key_of(query, field):
    return (query, field or qp.QueryParser.DEFAULT_FIELD)


def kept(query, field=None):
    with af._savedq_lock:
        return key_of(query, field) in af._savedq


def exact(result):
    return (result[0] != "returned"
            or (type(result[1]) is str and type(result[2]) is dict
                and all(type(k) is str and type(v) is str for k, v in result[2].items())))


# ----------------------------------------------------------------- parser

def parser_cases(expressions):
    results, raised = [], 0
    for query in expressions:
        for field in FIELDS:
            was_kept, before = kept(query, field), stats()
            s = stock(query, field)
            first = patched(query, field)
            second = patched(query, field)
            problems = [what for what, bad in (
                ("first call differs", first != s), ("second call differs", second != s),
                ("not exact str values", not exact(first) or not exact(second))) if bad]
            if s[0] == "returned":
                if first[0] == second[0] == "returned":
                    if first[2] is second[2]:
                        problems.append("two calls got the same dict")
                    second[2]["_qp_0"] = "changed by the caller"
                    second[2]["added by the caller"] = "x"
                third = patched(query, field)
                if third != stock(query, field):
                    problems.append("differs after the caller changed its dict")
                want = {"hit": 3} if was_kept else {"miss": 1, "hit": 2}
                if not kept(query, field):
                    problems.append("not kept")
            else:
                raised += 1
                third = patched(query, field)
                if third != s:
                    problems.append("third call differs")
                want = {"refused": 3}
                if kept(query, field):
                    problems.append("a refused text was kept")
            got = moved(before)
            got.pop("dropped", None)
            if got != want:
                problems.append(f"numbers moved {got}, wanted {want}")
            results.append({"query": query, "field": field, "kind": s[0], "problems": problems})
    return results, raised


def one(name, problems, **more):
    return {"case": name, "problems": [what for what, bad in problems if bad], **more}


def not_kept_cases():
    class Text(str):
        pass

    class Sub(qp.QueryParser):
        pass

    results = []
    limit = af.SAVEDQ_BYTES
    for name, query, field, parser, want_kept in (
            ("long text", "text:" + "a" * 3000, None, None, False),
            ("many bytes, few characters", "€" * 700, None, None, False),
            ("at the limit", "a" * (limit - len("text")), None, None, True),
            ("one byte over the limit", "a" * (limit - len("text") + 1), None, None, False),
            ("long field", "zzfield", "f" * (limit + 1), None, False),
            ("lone surrogate", "text:zz\ud800", None, None, None),
            ("str subclass as text", Text("zzsubclass"), None, None, False),
            ("str subclass as field", "zzsubfield", Text("resource"), None, False),
            ("bytes as text", b"zzbytes", None, None, False),
            ("number as text", 5, None, None, False),
            ("None as text", None, None, None, False),
            ("number as field", "zznumfield", 5, None, False),
            ("subclass of the parser", "zzparsersub", None, Sub(), False)):
        s = stock(query, field, parser)
        p1, p2 = patched(query, field, parser), patched(query, field, parser)
        try:
            is_kept = kept(query, field)
        except TypeError:
            is_kept = False
        results.append(one(name, (
            ("differs from Alerta", p1 != s or p2 != s),
            (f"kept is {is_kept}, wanted {want_kept}", want_kept is not None and is_kept != want_kept)),
            kind=s[0]))
    # A grammar object replaced after start-up: Alerta's parser reads the
    # module's name at each call, so the patched parser must call it.
    patched("zzgrammar")
    grammar = qp.query_expr
    qp.query_expr = grammar.copy()
    try:
        before = stats()
        s, p = stock("zzgrammar"), patched("zzgrammar")
        s2, p2 = stock("zzgrammar2"), patched("zzgrammar2")
        results.append(one("replaced grammar object", (
            ("differs from Alerta", p != s or p2 != s2),
            ("an answer came from the store", moved(before) != {"not_kept": 2}),
            ("kept", kept("zzgrammar2")))))
    finally:
        qp.query_expr = grammar
    before = stats()
    results.append(one("the grammar object is back", (
        ("differs from Alerta", patched("zzgrammar") != stock("zzgrammar")),
        ("no hit", moved(before) != {"hit": 1}))))
    return results


def eviction_case():
    limit = af.SAVEDQ_ENTRIES
    with af._savedq_lock:
        af._savedq.clear()
    before = stats()
    for i in range(limit + 40):
        patched(f"text:evict{i}")
    with af._savedq_lock:
        size = len(af._savedq)
    first = moved(before)
    oldest_gone = not any(kept(f"text:evict{i}") for i in range(40))
    rest_kept = all(kept(f"text:evict{i}") for i in range(40, limit + 40))
    patched("text:evict40")        # used again: no longer the oldest
    patched("text:evictlast")      # one more: the oldest is dropped
    with af._savedq_lock:
        size2 = len(af._savedq)
    return one("limit of entries", (
        (f"{size} entries, limit {limit}", size != limit or size2 != limit),
        (f"numbers moved {first}", first != {"miss": limit + 40, "dropped": 40}),
        ("the oldest 40 were not the ones dropped", not oldest_gone or not rest_kept),
        ("the entry used again was dropped", not kept("text:evict40")),
        ("the oldest entry was not dropped", kept("text:evict41"))), entries=limit)


def thread_case(expressions, refused):
    texts = expressions[:20] + refused[:4]
    wanted = {q: stock(q) for q in texts}
    with af._savedq_lock:
        af._savedq.clear()
    before, wrong, calls = stats(), [], 300

    def work(n):
        for i in range(calls):
            q = texts[(i * (n + 1) + n) % len(texts)]
            got = patched(q)
            if got != wanted[q] or not exact(got):
                wrong.append(q)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = moved(before)
    return one("eight threads", (
        (f"{len(wrong)} answers differ", bool(wrong)),
        (f"numbers {got} do not add up to {8 * calls}", sum(got.values()) != 8 * calls),
        ("an entry was dropped", "dropped" in got or "not_kept" in got)), calls=8 * calls, numbers=got)


# ------------------------------------------------------------------- HTTP

class Http:
    def __init__(self):
        self.client = af.app.test_client()
        self.second = psycopg2.connect(os.environ["DATABASE_URL"])
        self.second.autocommit = True
        self.run = uuid.uuid4().hex[:8]
        self.keys = {}
        for who, customer, scopes in (("admin", None, ["admin", "read", "write"]),
                                      ("a", f"cust-a-{self.run}", ["read"]),
                                      ("b", f"cust-b-{self.run}", ["read"]),
                                      ("expires", f"cust-a-{self.run}", ["read"])):
            self.keys[who] = uuid.uuid4().hex + uuid.uuid4().hex[:8]
            self.sql('INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, customer) '
                     "VALUES (%s, %s, %s, %s, 'savedq check', timezone('UTC', now()) + interval '1 day', 0, %s)",
                     (str(uuid.uuid4()), self.keys[who], f"savedq-{who}", scopes, customer))
        self.ids = {"a": [], "b": [], "none": []}
        for i, owner in enumerate(("a", "a", "a", "b", "b", "none")):
            self.add(owner, i)

    def sql(self, statement, values=()):
        cursor = self.second.cursor()
        cursor.execute(statement, values)
        return cursor.rowcount

    def add(self, owner, i):
        body = {"resource": f"sqres{self.run}n{i}", "event": "savedq", "environment": "Production",
                "severity": "major", "service": ["web"], "text": f"sqprobe{self.run} alert {i}"}
        r = self.client.post("/alert", json=body, headers={"Authorization": f"Key {self.keys['admin']}"})
        if r.status_code != 201:
            raise SystemExit(f"the check could not make its alert: HTTP {r.status_code}")
        id = r.get_json()["id"]
        if owner != "none":
            self.sql("UPDATE alerts SET customer=%s WHERE id=%s", (f"cust-{owner}-{self.run}", id))
        self.ids[owner].append(id)
        return id

    def get(self, who, path, query, alerta=False):
        """(status, what is compared). With `alerta`, Alerta's own parser
        is on the application for this request."""
        if alerta:
            qp.QueryParser.parse = af._orig_parse
        try:
            r = self.client.get(path, query_string=query, headers={"Authorization": f"Key {self.keys[who]}"})
        finally:
            qp.QueryParser.parse = af._savedq_parse
        body = r.get_json(silent=True) or {}
        return r.status_code, {"status": body.get("status"), "message": body.get("message"),
                               "total": body.get("total"), "severityCounts": body.get("severityCounts"),
                               "statusCounts": body.get("statusCounts"),
                               "ids": [a.get("id") for a in body.get("alerts") or []]}

    def both(self, who, query, path="/alerts"):
        """Alerta's parser, then the patched parser twice."""
        return (self.get(who, path, query, alerta=True), self.get(who, path, query),
                self.get(who, path, query))

    def close(self):
        self.second.close()


def seen(name, answers, ids, more=()):
    """One case: three answers that must be equal, 200, with these ids."""
    a, p1, p2 = answers
    return one(name, (
        ("the patched answers differ from Alerta's", p1 != a or p2 != a),
        (f"HTTP {a[0]}", a[0] != 200),
        (f"{len(a[1]['ids'])} alerts, wanted {len(ids)}", sorted(a[1]["ids"]) != sorted(ids)),
        ("total is not the number of alerts", a[1]["total"] != len(ids)),
        ("counts do not add up to the total",
         a[0] == 200 and (sum((a[1]["severityCounts"] or {}).values()) != len(ids)
                          or sum((a[1]["statusCounts"] or {}).values()) != len(ids))), *more),
        alerts=len(a[1]["ids"]))


def http_cases(refused_text):
    h = Http()
    results = []
    try:
        search = {"q": f"text:sqprobe{h.run}"}
        everyone = h.ids["a"] + h.ids["b"] + h.ids["none"]
        with af._savedq_lock:
            af._savedq.clear()
        before = stats()
        results.append(seen("customer A, saved search", h.both("a", search), h.ids["a"]))
        results.append(seen("customer B, the same search", h.both("b", search), h.ids["b"]))
        results.append(seen("admin key, the same search", h.both("admin", search), everyone))
        got = moved(before)
        results.append(one("one entry for the three keys", (
            (f"numbers moved {got}", got != {"miss": 1, "hit": 5}),)))

        new = h.add("a", 6)
        results.append(seen("a new alert of customer A shows", h.both("a", search), h.ids["a"]))
        results.append(seen("and not for customer B", h.both("b", search), h.ids["b"]))
        h.ids["a"].remove(new)
        gone = h.sql("DELETE FROM alerts WHERE id=%s", (new,))
        results.append(seen("a removed alert is gone", h.both("a", search), h.ids["a"],
                            (("the check did not remove its alert", gone != 1),)))
        moved_id = h.ids["a"].pop()
        h.sql("UPDATE alerts SET customer=%s WHERE id=%s", (f"cust-b-{h.run}", moved_id))
        h.ids["b"].append(moved_id)
        results.append(seen("an alert moved to customer B: A", h.both("a", search), h.ids["a"]))
        results.append(seen("an alert moved to customer B: B", h.both("b", search), h.ids["b"]))
        everyone = h.ids["a"] + h.ids["b"] + h.ids["none"]

        word = {"q": f"sqres{h.run}"}
        results.append(seen("a word, default field: no alert", h.both("admin", word), []))
        results.append(seen("the same word, q.df=resource: all", h.both("admin", dict(word, **{"q.df": "resource"})),
                            everyone))
        results.append(seen("the same word, default field again", h.both("admin", word), []))

        before = stats()
        a, p1, p2 = h.both("a", search, path="/alerts/count")
        results.append(one("/alerts/count, saved search", (
            ("the patched answers differ from Alerta's", p1 != a or p2 != a),
            (f"HTTP {a[0]}", a[0] != 200), (f"total {a[1]['total']}", a[1]["total"] != len(h.ids["a"])),
            ("not from the store", moved(before) != {"hit": 2}))))

        before = stats()
        a, p1, p2 = h.both("a", {"q": refused_text})
        results.append(one("a refused search", (
            ("the patched answers differ from Alerta's", p1 != a or p2 != a),
            (f"HTTP {a[0]}, wanted 400", a[0] != 400),
            (f"numbers moved {moved(before)}", moved(before) != {"refused": 2}))))

        before = stats()
        a, p1, p2 = h.both("a", {"resource": f"sqres{h.run}n0"})
        results.append(one("no q: the store is not used", (
            ("the patched answers differ from Alerta's", p1 != a or p2 != a),
            (f"HTTP {a[0]}", a[0] != 200), (f"{a[1]['total']} alerts, wanted 1", a[1]["total"] != 1),
            (f"numbers moved {moved(before)}", moved(before) != {}))))

        # Keys. The saved search is in the store before each change.
        ok_before = h.get("expires", "/alerts", search)
        h.sql("UPDATE keys SET expire_time = timezone('UTC', now()) - interval '1 minute' WHERE key=%s",
              (h.keys["expires"],))
        results.append(one("an expired key", (
            ("the key did not work before", ok_before[0] != 200),
            ("patched", h.get("expires", "/alerts", search)[0] != 401),
            ("Alerta", h.get("expires", "/alerts", search, alerta=True)[0] != 401))))
        removed = h.sql("DELETE FROM keys WHERE key=%s", (h.keys["a"],))
        results.append(one("a removed key", (
            ("the check did not remove its key", removed != 1),
            ("patched", h.get("a", "/alerts", search)[0] != 401),
            ("Alerta", h.get("a", "/alerts", search, alerta=True)[0] != 401))))
        results.append(seen("customer B's key still works", h.both("b", search), h.ids["b"]))
        timing = http_times(h)
    finally:
        h.close()
    return results, timing


# ------------------------------------------------------------------ times

def per_call(function, texts, calls):
    start = time.perf_counter()
    for i in range(calls):
        function(texts[i % len(texts)])
    return round((time.perf_counter() - start) / calls * 1e6, 1)


def parser_times():
    """Microseconds for one call, the parser alone."""
    dashboard = "severity:(critical OR major) AND status:open AND NOT tags:muted"
    sixteen = [f"{dashboard} AND group:team{i}" for i in range(16)]
    calls = 400
    alerta = lambda q: af._orig_parse(qp.QueryParser(), q)  # noqa: E731
    saved = lambda q: qp.QueryParser().parse(q)  # noqa: E731
    out = {"calls": calls, "unit": "microseconds for one call"}
    for name, texts in (("one saved search", [dashboard]), ("sixteen saved searches", sixteen)):
        for q in texts:
            saved(q)
        out[name] = {"alerta": per_call(alerta, texts, calls), "savedq": per_call(saved, texts, calls)}
    different = [f"{dashboard} AND group:once{i}" for i in range(calls)]
    with af._savedq_lock:
        af._savedq.clear()
    out["each search different (nothing to reuse)"] = {
        "alerta": per_call(alerta, different, calls), "savedq": per_call(saved, different, calls)}
    return out


def http_times(h):
    """Median milliseconds for one request through the test client, in
    blocks that take turns."""
    out = {"unit": "median milliseconds for one request, Flask test client", "blocks": 5, "requests_in_block": 40}
    for name, query in (("saved search", {"q": f"severity:(critical OR major) AND status:open AND text:sqprobe{h.run}"}),
                        ("no q (control)", {"status": "open"})):
        times = {True: [], False: []}
        h.get("admin", "/alerts", query)
        for block in range(5):
            for alerta in ((True, False), (False, True))[block % 2]:
                for _ in range(40):
                    start = time.perf_counter()
                    status, _body = h.get("admin", "/alerts", query, alerta=alerta)
                    times[alerta].append((time.perf_counter() - start) * 1e3)
                    if status != 200:
                        raise SystemExit(f"timing request: HTTP {status}")
        out[name] = {"alerta": round(statistics.median(times[True]), 3),
                     "savedq": round(statistics.median(times[False]), 3)}
    return out


# --------------------------------------------------------------- start-up

CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from alerta.database.backends.postgres import queryparser as qp
alerta_parse = qp.QueryParser.parse
if len(sys.argv) > 2:
    with open(qp.__file__, "rb") as f:
        data = f.read()
    with open(sys.argv[2], "wb") as f:
        f.write(data + b"\n# one more line\n")
    qp.__file__ = sys.argv[2]
import alerta_fast
print("STARTED, Alerta's parser in place:", qp.QueryParser.parse is alerta_parse)
"""


def startup_cases():
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, env, other_file, want_start, want_text in (
                ("not named: Alerta's parser stays", {"ALERTA_FAST": "safe"}, False, True, "in place: True"),
                ("no ALERTA_FAST: Alerta's parser stays", {}, False, True, "in place: True"),
                ("named: patched", {"ALERTA_FAST": "savedq"}, False, True, "in place: False"),
                ("another queryparser.py: no start", {"ALERTA_FAST": "savedq"}, True, False, "is another file"),
                ("another queryparser.py, not named: starts", {"ALERTA_FAST": "safe"}, True, True, "in place: True"),
                ("limit of 0 entries: no start", {"ALERTA_FAST": "savedq", "ALERTA_FAST_SAVEDQ_ENTRIES": "0"},
                 False, False, "must be 1 or more"),
                ("limit of 0 bytes: no start", {"ALERTA_FAST": "savedq", "ALERTA_FAST_SAVEDQ_BYTES": "0"},
                 False, False, "must be 1 or more")):
            child_env = {k: v for k, v in os.environ.items() if not k.startswith("ALERTA_FAST")}
            child_env.update(env)
            args = [sys.executable, "-I", "-c", CHILD, str(HERE)]
            if other_file:
                args.append(str(Path(tmp) / "queryparser.py"))
            r = subprocess.run(args, env=child_env, capture_output=True, text=True, timeout=120)
            started = "STARTED" in r.stdout
            results.append(one(name, (
                (f"started is {started}, exit {r.returncode}", started != want_start
                 or (r.returncode == 0) != want_start),
                ("the expected text is missing", want_text not in r.stdout + r.stderr))))
    return results


def main(out, tests):
    upstream, tests_sha256 = upstream_expressions(tests)
    if len(upstream) < 30:
        raise SystemExit(f"only {len(upstream)} expressions found in {tests}")
    parser_results, raised = parser_cases(upstream + MORE + REFUSED_KIND)
    refused = [r["query"] for r in parser_results if r["kind"] == "raised" and r["field"] is None]
    parse_refused = [q for q in refused if q.strip() and stock(q)[1] == "ParseException"]
    others = not_kept_cases() + [eviction_case(), thread_case(upstream, refused)]
    if not parse_refused:
        raise SystemExit("no text of the list is refused by Alerta's parser with ParseException")
    http_results, http_timing = http_cases(parse_refused[0])
    timing = {"parser": parser_times(), "http": http_timing}
    startup = startup_cases()
    groups = {"parser": parser_results, "not_kept_and_limits": others, "http": http_results, "startup": startup}
    failed = {name: sum(1 for r in rows if r["problems"]) for name, rows in groups.items()}
    result = {"ok": not any(failed.values()),
              "cases": {name: len(rows) for name, rows in groups.items()}, "failed": failed,
              "expressions": {"alerta_tests": len(upstream), "alerta_tests_sha256": tests_sha256,
                              "more": len(MORE), "refused_kind": len(REFUSED_KIND),
                              "default_fields": len(FIELDS), "refused_by_the_parser": raised},
              "queryparser_blob": af._QP_BLOB, "limits": {"entries": af.SAVEDQ_ENTRIES, "bytes": af.SAVEDQ_BYTES},
              "times": timing,
              "results": {"parser": [r for r in parser_results if r["problems"]],
                          "not_kept_and_limits": others, "http": http_results, "startup": startup}}
    Path(out).write_text(json.dumps(result, indent=1, default=repr))
    for name, rows in groups.items():
        for r in rows:
            if r["problems"]:
                print(f"FAILED {name} / {r.get('case') or (r['query'], r['field'])!r}: {r['problems']}",
                      file=sys.stderr)
        print(f"savedq check, {name}: {len(rows) - failed[name]} of {len(rows)} cases agree")
    print(f"times (this runner only, in-process): {json.dumps(timing)}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2
                  else HERE.parents[1] / "reference/alerta/tests/test_queryparser.py"))
