#!/usr/bin/env python3
"""Check of two default patches of alerta_fast.py, `onecount` and
`nosentry`, for an installation that runs its own Python code with
Alerta. Run with the reference Alerta environment's Python and
DATABASE_URL set to an empty database (Alerta makes its tables):

    <venv>/bin/python -I scripts/bench/defaults_check.py <result.json>

Each case runs in a new process.

onecount, with ALERTA_FAST=safe and Flask's test client on a real
database. The patch's grouped query and Alerta's two count functions are
counted.
  - One application context held open over several requests, as own code
    can do: GET /alerts, a new alert, GET /alerts again. The second answer
    must count the new alert (total, severity and status counts), and each
    request must make one grouped query and call neither of Alerta's two
    count functions.
    This case passes before the fix too: Alerta's /alerts gives each
    request its own time cut-off (its query time is in the query's
    values), so the cache key on `g` never repeated across these
    requests. It checks that the fix keeps one grouped query per request.
  - A request with no outer context: the same.
  - An application context and no request (a tool): the counts must come
    from Alerta's two functions, one call each, and see an alert made
    between two calls. This is the case that fails before the fix (the
    same query object, so the same key, on the same `g`).

nosentry. Before alerta_fast is imported, the child puts a recorder in
place of sentry_sdk.init: it records who called it and returns a marker,
so no SDK starts and nothing is sent anywhere.
  - No SENTRY_DSN, `nosentry` on (the default): Alerta's own call at the
    import of alerta.app does not reach the recorder; after the start,
    sentry_sdk.init is the recorder again and a call of own code (as a
    plugin's) reaches it and gets the marker.
  - SENTRY_DSN set: Alerta's own call reaches the recorder.
  - `nosentry` not named: the same.
  - The import of alerta.app fails: sentry_sdk.init is the recorder
    again.
Not covered: a call of own code while alerta.app is being imported.

With DEFAULTS_ADDON_DIR set, the alerta_fast.py of that directory is
checked in place of the one beside this file (the workflow uses it for the
add-on before these fixes, where three cases must fail).

Exit status 1 on any failure.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDON = Path(os.environ.get("DEFAULTS_ADDON_DIR") or HERE)

ONECOUNT = r"""
import json
import sys
import uuid
sys.path.insert(0, sys.argv[1])
import alerta_fast as af
from alerta.database.backends.postgres.base import Backend
from alerta.database.backends.postgres.utils import Alerts
from alerta.models.alert import Alert
from werkzeug.datastructures import MultiDict

calls = {"severity": 0, "status": 0, "grouped": 0}
for which in ("severity", "status"):
    def counted(self, query=None, _alerta=af._orig_by[which], _which=which):
        calls[_which] += 1
        return _alerta(self, query)
    af._orig_by[which] = counted
alerta_fetchall = Backend._fetchall


def fetchall(self, query, vars, *args, **kwargs):
    if "GROUP BY severity, status" in query:
        calls["grouped"] += 1
    return alerta_fetchall(self, query, vars, *args, **kwargs)


Backend._fetchall = fetchall
client = af.app.test_client()
group = "dfc" + uuid.uuid4().hex[:12]
problems = []


def post(resource, severity):
    r = client.post("/alert", json={"resource": group + resource, "event": "DefaultsCheck", "group": group,
                                    "environment": "Production", "severity": severity, "service": ["check"]})
    if r.status_code != 201:
        problems.append(f"POST /alert gave {r.status_code}")


def page(name, total, severity, status):
    before = dict(calls)
    r = client.get("/alerts", query_string={"group": group})
    body = r.get_json(silent=True) or {}
    got = (r.status_code, body.get("total"), body.get("severityCounts"), body.get("statusCounts"))
    wanted = (200, total, severity, status)
    delta = {k: calls[k] - before[k] for k in calls}
    if got != wanted:
        problems.append(f"{name}: got {got}, wanted {wanted}")
    if delta != {"severity": 0, "status": 0, "grouped": 1}:
        problems.append(f"{name}: queries {delta}, wanted one grouped query")


with af.app.app_context():  # one context over several requests
    post("-1", "major")
    page("held context, first request", 1, {"major": 1}, {"open": 1})
    post("-2", "minor")
    page("held context, after a new alert", 2, {"major": 1, "minor": 1}, {"open": 2})
page("no outer context", 2, {"major": 1, "minor": 1}, {"open": 2})
with af.app.app_context():  # a tool: no request
    query = Alerts.from_params(MultiDict({"group": group}))
    for name, wanted in (("tool, first call", {"major": 1, "minor": 1}),
                         ("tool, after a new alert", {"major": 1, "minor": 2})):
        if name.endswith("new alert"):
            post("-3", "minor")
        before = dict(calls)
        got = (Alert.get_counts_by_severity(query), Alert.get_counts_by_status(query))
        delta = {k: calls[k] - before[k] for k in calls}
        if got != (wanted, {"open": sum(wanted.values())}):
            problems.append(f"{name}: got {got}")
        if delta != {"severity": 1, "status": 1, "grouped": 0}:
            problems.append(f"{name}: queries {delta}, wanted Alerta's two")
print("RESULT " + json.dumps({"problems": problems, "calls": calls}))
"""

NOSENTRY = r"""
import json
import sys
sys.path.insert(0, sys.argv[1])
mode = sys.argv[2]
import sentry_sdk

MARK = object()
calls = []


def recorder(*args, **kwargs):
    calls.append({"caller": sys._getframe(1).f_globals.get("__name__"), "kwargs": sorted(kwargs)})
    return MARK


sentry_sdk.init = recorder
if mode == "the import of alerta.app fails":
    sys.modules["alerta.app"] = None  # the next import of it raises ImportError
started, error = True, None
try:
    import alerta_fast  # noqa: F401
except ImportError as exc:
    started, error = False, type(exc).__name__
at_start = list(calls)
restored = sentry_sdk.init is recorder
own = sentry_sdk.init(dsn="http://own@127.0.0.1:9/1") if restored else None
print("RESULT " + json.dumps({"started": started, "error": error, "at_start": at_start,
                              "restored": restored, "own_call_reached": own is MARK}))
"""

STOCK_CALL = [{"caller": "alerta.app", "kwargs": ["integrations", "release"]}]
NOSENTRY_CASES = (
    # name, environment, started, calls at start-up
    ("no SENTRY_DSN, nosentry on", {"ALERTA_FAST": "safe"}, True, []),
    ("SENTRY_DSN set", {"ALERTA_FAST": "safe", "SENTRY_DSN": "http://stock@127.0.0.1:9/1"}, True, STOCK_CALL),
    ("nosentry not named", {"ALERTA_FAST": "reuse,nolog,onecount"}, True, STOCK_CALL),
    ("the import of alerta.app fails", {"ALERTA_FAST": "safe"}, False, []),
)


def child(script, env, *args):
    child_env = {k: v for k, v in os.environ.items() if not k.startswith(("ALERTA_FAST", "SENTRY"))}
    child_env.update(env)
    r = subprocess.run([sys.executable, "-I", "-c", script, str(ADDON), *args], env=child_env,
                       capture_output=True, text=True, timeout=120)
    lines = [line[7:] for line in r.stdout.splitlines() if line.startswith("RESULT ")]
    result = json.loads(lines[-1]) if lines else None
    error = r.stderr.strip().splitlines()[-1][:300] if r.returncode and r.stderr.strip() else None
    return r.returncode, result, error


def main(out):
    results = []
    code, result, error = child(ONECOUNT, {"ALERTA_FAST": "safe"})
    problems = ([] if code == 0 else [f"exit {code}"]) + ([] if result else ["no result"])
    results.append({"case": "onecount", "problems": problems + (result or {}).get("problems", []),
                    "calls": (result or {}).get("calls"), "error": error})
    for name, env, started, at_start in NOSENTRY_CASES:
        code, result, error = child(NOSENTRY, env, name)
        problems = [] if code == 0 else [f"exit {code}"]
        if not result:
            problems.append("no result")
        else:
            problems += [what for what, bad in (
                (f"started is {result['started']}", result["started"] != started),
                (f"calls at start-up {result['at_start']}", result["at_start"] != at_start),
                ("sentry_sdk.init is not the recorder after the start", not result["restored"]),
                ("a call of own code did not reach the recorder", not result["own_call_reached"])) if bad]
        results.append({"case": f"nosentry: {name}", "problems": problems, "result": result, "error": error})
    failed = sum(1 for r in results if r["problems"])
    Path(out).write_text(json.dumps({"ok": not failed, "cases": len(results), "failed": failed, "addon": str(ADDON),
                                     "results": results}, indent=1))
    for r in results:
        print(("FAILED " if r["problems"] else "ok     ") + r["case"] + (f": {r['problems']}" if r["problems"] else ""))
    print(f"defaults check: {len(results) - failed} of {len(results)} cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
