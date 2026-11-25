#!/usr/bin/env python3
"""Check of alerta_fast.py's opt-in `alertlinks` and `fastdates` patches
against Alerta as released: the bytes of every answer must be the same.
Run with the reference Alerta environment's Python and DATABASE_URL set to
an empty database (Alerta makes its tables; the check makes its alerts):

    <venv>/bin/python -I scripts/bench/pages_check.py <result.json>

Each part runs in new processes.

Pages. One process with ALERTA_FAST=safe makes 120 alerts through Alerta
(with tags, nested attributes, null values, non-ASCII text, and three ids
of the caller's choice: `custom.id-1` and `custom~id2` are outside the
patch's plain-id pattern and go to Alerta's own helper, `Custom_ID_3` is
inside it and takes the patch's path) and changes the status of 20 of them,
so they have history. Then, for each
of two settings (BASE_URL unset; BASE_URL set with a path), a process with
ALERTA_FAST=safe and a process with ALERTA_FAST=safe,alertlinks,fastdates
send the same requests with Flask's test client, and every answer must be
byte-equal, with the same status and content type:
  - GET /alerts sorted by resource, 1, 25 and 100 a page, page 2 too;
  - GET /alerts with show-history;
  - GET /alerts with a search (q=);
  - GET /alerts/history, GET /alert/<id> (also for the caller's ids);
  - GET /alerts/count;
each from three base URLs (the default host, another host and port, and a
path below the host as a proxy gives it). The check fails unless every
requested answer is there once for each arm (78 pairs), and for a page that
differs it records which JSON fields differ.

Encoder. In a process with the two patches: app.json.dumps() of datetimes
(microseconds 0, 999 and 999999; years 999, 1000 and 9999; with a time
zone; a subclass of datetime), timedelta, Decimal, nested lists and dicts,
non-ASCII text, and with ensure_ascii, sort_keys and indent given, must
equal json.dumps(..., cls=CustomJSONEncoder) with the stock provider's
settings, or raise the same exception type. A copy of the provider with
other settings, a new provider and a subclass must give their own stock
answers too. The check fails unless all 224 cases (4 providers x 14 values
x 4 sets of arguments) are there, once each.

CPU. Four processes (safe, patched, patched, safe) each send 300 timed
requests for the list at 100 a page, after 20 not timed, and report the
process's CPU time per request (Python in one process: Alerta, Flask's
test client and the driver; not PostgreSQL, not gunicorn, not a network).
The answers are kept during the timed loop and checked after it: every
one must be HTTP 200 and byte-equal to Alerta's own answer for that page,
or the CPU numbers are not valid and the check fails.

Exit status 1 on any failure. With PAGES_ADDON_DIR set, the alerta_fast.py
of that directory is checked in place of the one beside this file (the
workflow uses it for two changed copies, where the check must fail).
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDON = Path(os.environ.get("PAGES_ADDON_DIR") or HERE)
FLAGS = "safe,alertlinks,fastdates"
BASES = ("http://localhost/", "http://alerta.internal:8443/", "http://proxy.example:8080/alerta/")
CUSTOM_IDS = ("custom.id-1", "custom~id2", "Custom_ID_3")
CONFIGS = {"no BASE_URL": {}, "BASE_URL with a path": {"BASE_URL": "https://alerta.example.org/api"}}
TIME_PATH, TIME_BASE, TIME_N = "/alerts?sort-by=resource&page-size=100", "http://localhost/", 300
ENCODER_CASES = 4 * 14 * 4


def diff_keys(a, b, key=None, out=None):
    """The names of the JSON fields whose values differ."""
    out = set() if out is None else out
    if type(a) is not type(b):
        out.add(str(key))
    elif isinstance(a, dict):
        for k in set(a) | set(b):
            if k not in a or k not in b:
                out.add(str(k))
            else:
                diff_keys(a[k], b[k], k, out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.add(str(key))
        else:
            for x, y in zip(a, b):
                diff_keys(x, y, key, out)
    elif a != b:
        out.add(str(key))
    return out


def child_env(flags, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALERTA_FAST", "BASE_URL", "PAGES_"))}
    env["ALERTA_FAST"] = flags
    env.update(extra or {})
    return env


def run_child(mode, flags, out, extra=None):
    r = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--child", mode, str(ADDON), out],
                       env=child_env(flags, extra), capture_output=True, text=True, timeout=300)
    tail = r.stderr.strip().splitlines()[-1][:300] if r.returncode and r.stderr.strip() else None
    return r.returncode, tail


# ------------------------------------------------------------- children

def child(mode, addon, out):
    sys.path.insert(0, addon)
    import alerta_fast as af
    client = af.app.test_client()
    if mode == "seed":
        ids = seed(client)
        Path(out).write_text(json.dumps(ids))
    elif mode == "read":
        ids = json.loads(Path(out + ".ids").read_text())
        from alerta.models import alert as alert_model
        from alerta.utils import format as fmt, response
        installed = {"alertlinks": alert_model.absolute_url is not response.absolute_url,
                     "fastdates": type(fmt.AlertaJsonProvider.__dict__["dumps"]).__name__ != "function"}
        Path(out).write_text(json.dumps({"installed": installed, "answers": read(client, ids)}))
    elif mode == "time":
        import hashlib
        import time
        from collections import Counter
        warm = [client.get(TIME_PATH, headers={"Accept-Encoding": "identity"}) for _ in range(20)]
        t0 = time.process_time()
        timed = [client.get(TIME_PATH, headers={"Accept-Encoding": "identity"}) for _ in range(TIME_N)]
        cpu = time.process_time() - t0
        # Checked after the timed loop, so that the check is not in the CPU time.
        answers = Counter(f"{r.status_code} {hashlib.sha256(r.get_data()).hexdigest()}" for r in warm + timed)
        Path(out).write_text(json.dumps({"us_per_request": round(1e6 * cpu / TIME_N, 1), "answers": answers}))
    elif mode == "encoder":
        Path(out).write_text(json.dumps(encoder_cases(af)))


def seed(client):
    ids = []
    for n in range(120):
        body = {"resource": f"res{n:03d}", "event": "PageCheck", "environment": "Production",
                "severity": ("critical", "major", "minor", "warning")[n % 4],
                "service": ["web", "db"][: 1 + n % 2], "group": "pages", "value": str(n),
                "text": f"alert {n} éè 中文" if n % 7 == 0 else f"alert {n}",
                "tags": [f"t{n % 5}", "pages"],
                "attributes": {"region": "eu" if n % 2 else None, "nested": {"n": n, "list": [1, "x", None]}}}
        if n < len(CUSTOM_IDS):
            body["id"] = CUSTOM_IDS[n]
        r = client.post("/alert", json=body)
        if r.status_code != 201:
            raise SystemExit(f"seed: POST /alert gave {r.status_code}")
        ids.append(r.get_json()["id"])
    for alert_id in ids[3:23]:
        r = client.put(f"/alert/{alert_id}/status", json={"status": "ack", "text": "page check"})
        if r.status_code != 200:
            raise SystemExit(f"seed: status change gave {r.status_code}")
    return ids


def requests(ids):
    paths = ["/alerts?sort-by=resource&page-size=1", "/alerts?sort-by=resource&page-size=25",
             "/alerts?sort-by=resource&page-size=100", "/alerts?sort-by=resource&page-size=100&page=2",
             "/alerts?sort-by=resource&page-size=25&show-history=true",
             "/alerts?sort-by=resource&page-size=50&q=severity:major",
             "/alerts/history?page-size=50", "/alerts/count",
             *(f"/alert/{i}" for i in (*ids[:3], ids[5], ids[60]))]
    return [(base, path) for base in BASES for path in paths]


def read(client, ids):
    answers = []
    for base, path in requests(ids):
        r = client.get(path, base_url=base, headers={"Accept-Encoding": "identity"})
        answers.append({"base": base, "path": path, "status": r.status_code, "type": r.content_type,
                        "body": base64.b64encode(r.get_data()).decode()})
    return answers


def encoder_cases(af):
    import copy
    import datetime as d
    import decimal
    from alerta.utils import format as fmt

    class Sub(d.datetime):
        pass

    values = {
        "naive, 0 us": d.datetime(2026, 11, 22, 21, 0, 0, 0),
        "naive, 999 us": d.datetime(2026, 11, 22, 21, 0, 0, 999),
        "naive, 999999 us": d.datetime(2026, 11, 22, 23, 59, 59, 999999),
        "year 999": d.datetime(999, 1, 2, 3, 4, 5, 678000),
        "year 1000": d.datetime(1000, 1, 2, 3, 4, 5, 678000),
        "year 9999": d.datetime(9999, 12, 31, 23, 59, 59, 999999),
        "UTC": d.datetime(2026, 11, 22, 21, 0, 0, 123456, tzinfo=d.timezone.utc),
        "+05:30": d.datetime(2026, 11, 22, 21, 0, 0, 1000, tzinfo=d.timezone(d.timedelta(hours=5, minutes=30))),
        "subclass": Sub(2026, 11, 22, 21, 0, 0, 500000),
        "timedelta": d.timedelta(seconds=90.5),
        "Decimal": decimal.Decimal("1.10"),
        "date (not datetime)": d.date(2026, 11, 22),
        "nested": {"b": [d.datetime(2026, 1, 1), {"a": d.datetime(2026, 1, 1, 0, 0, 0, 1)}], "a": "é 中"},
        "set (refused)": {1, 2},
    }
    kwargs_cases = {"defaults": {}, "ensure_ascii=False": {"ensure_ascii": False},
                    "sort_keys=False": {"sort_keys": False}, "indent=2": {"indent": 2}}

    def stock(provider, obj, **kw):
        kw.setdefault("ensure_ascii", provider.ensure_ascii)
        kw.setdefault("sort_keys", provider.sort_keys)
        return json.dumps(obj, **kw, cls=fmt.CustomJSONEncoder)

    def outcome(function, *args, **kw):
        try:
            return ["returned", function(*args, **kw)]
        except Exception as exc:  # compared by type
            return ["raised", type(exc).__name__]

    other = copy.copy(af.app.json)
    other.ensure_ascii, other.sort_keys = False, False

    class SubProvider(fmt.AlertaJsonProvider):
        pass

    providers = {"the app's provider": af.app.json, "a copy with other settings": other,
                 "a new provider": fmt.AlertaJsonProvider(af.app), "a subclass": SubProvider(af.app)}
    results = []
    for pname, provider in providers.items():
        for vname, value in values.items():
            for kname, kw in kwargs_cases.items():
                obj = {"z": value, "a": 1, "é": "x"}
                got = outcome(provider.dumps, obj, **kw)
                want = outcome(stock, provider, obj, **kw)
                results.append({"provider": pname, "value": vname, "kwargs": kname,
                                "problems": [] if got == want else [f"got {got!r}, wanted {want!r}"]})
    installed = type(fmt.AlertaJsonProvider.__dict__.get("dumps")).__name__
    return {"results": results, "class_dumps": installed}


# ---------------------------------------------------------------- parent

def main(out):
    import hashlib
    import tempfile
    results, problems, timing_problems, time_wanted = [], [], [], None
    with tempfile.TemporaryDirectory() as tmp:
        ids_file = str(Path(tmp) / "ids.json")
        code, error = run_child("seed", "safe", ids_file)
        if code:
            problems.append(f"seed: exit {code}: {error}")
        else:
            ids = json.loads(Path(ids_file).read_text())
            for cname, extra in CONFIGS.items():
                answers = {}
                for arm in ("safe", FLAGS):
                    f = str(Path(tmp) / f"{cname.replace(' ', '_')}-{arm}.json")
                    Path(f + ".ids").write_text(json.dumps(ids))
                    code, error = run_child("read", arm, f, extra)
                    if code:
                        problems.append(f"{cname}, {arm}: exit {code}: {error}")
                        break
                    got = json.loads(Path(f).read_text())
                    wanted = {"alertlinks": arm == FLAGS, "fastdates": arm == FLAGS}
                    if got["installed"] != wanted:
                        problems.append(f"{cname}, {arm}: patches installed {got['installed']}, wanted {wanted}")
                    answers[arm] = got["answers"]
                if len(answers) != 2:
                    continue
                inventory = requests(ids)
                by_key = {}
                for arm, rows in answers.items():
                    keys = [(r["base"], r["path"]) for r in rows]
                    if sorted(keys) != sorted(inventory) or len(set(keys)) != len(keys):
                        problems.append(f"{cname}, {arm}: {len(keys)} answers, not the {len(inventory)} requested")
                    by_key[arm] = {(r["base"], r["path"]): r for r in rows}
                for key in inventory:
                    a, p = by_key["safe"].get(key), by_key[FLAGS].get(key)
                    if a is None or p is None:
                        continue
                    same = all(a[k] == p[k] for k in ("status", "type", "body"))
                    bad = [] if same else [f"stock {a['status']} {len(a['body'])}, patched {p['status']} {len(p['body'])}"]
                    if a["status"] != 200:
                        bad.append(f"stock answered {a['status']}")
                    row = {"part": "pages", "config": cname, "base": a["base"], "path": a["path"],
                           "bytes": len(base64.b64decode(a["body"])), "problems": bad}
                    if not same:
                        try:
                            row["differs_in"] = sorted(diff_keys(json.loads(base64.b64decode(a["body"])),
                                                                 json.loads(base64.b64decode(p["body"]))))
                        except ValueError:
                            row["differs_in"] = ["not JSON"]
                    if cname == "no BASE_URL" and key == (TIME_BASE, TIME_PATH):
                        time_wanted = f"200 {hashlib.sha256(base64.b64decode(a['body'])).hexdigest()}"
                    results.append(row)
        f = str(Path(tmp) / "encoder.json")
        code, error = run_child("encoder", FLAGS, f)
        if code:
            problems.append(f"encoder: exit {code}: {error}")
        else:
            enc = json.loads(Path(f).read_text())
            if enc["class_dumps"] == "function":
                problems.append("fastdates is not installed (AlertaJsonProvider.dumps is the stock function)")
            cases = [(r["provider"], r["value"], r["kwargs"]) for r in enc["results"]]
            if len(cases) != ENCODER_CASES or len(set(cases)) != ENCODER_CASES:
                problems.append(f"encoder: {len(set(cases))} distinct of {len(cases)} cases, not {ENCODER_CASES}")
            for r in enc["results"]:
                results.append({"part": "encoder", **r})
        # In-process CPU per request, 100 alerts a page; relative to this runner.
        times = {"safe": [], FLAGS: []}
        if not problems and time_wanted:
            for arm in ("safe", FLAGS, FLAGS, "safe"):
                f = str(Path(tmp) / f"time-{len(times[arm])}-{arm}.json")
                code, error = run_child("time", arm, f)
                if code:
                    problems.append(f"time, {arm}: exit {code}: {error}")
                    break
                got = json.loads(Path(f).read_text())
                if got["answers"] != {time_wanted: TIME_N + 20}:
                    timing_problems.append(f"time, {arm}: answers {got['answers']}, wanted {TIME_N + 20} of "
                                           "Alerta's own page")
                times[arm].append(got["us_per_request"])
        elif not problems:
            problems.append("time: no answer of Alerta's own to check the timed answers against")
    failed = sum(1 for r in results if r["problems"]) + len(problems) + len(timing_problems)
    pages = [r for r in results if r["part"] == "pages"]
    summary = {"ok": not failed, "failed": failed, "problems": problems, "timing_problems": timing_problems,
               "timing_valid": not timing_problems and all(len(v) == 2 for v in times.values()),
               "addon": str(ADDON),
               "pages": len(pages), "page_bytes": sum(r["bytes"] for r in pages),
               "encoder_cases": sum(1 for r in results if r["part"] == "encoder"),
               "cpu_us_per_request_100_alerts": times, "results": results}
    Path(out).write_text(json.dumps(summary, indent=1))
    for problem in problems + timing_problems:
        print("FAILED", problem)
    for r in results:
        if r["problems"]:
            print("FAILED", r.get("config", r.get("provider")), r.get("path", r.get("value")), r["problems"][:1])
    print(f"pages check: {len(pages)} answers compared ({summary['page_bytes']} bytes), "
          f"{summary['encoder_cases']} encoder cases, {failed} failed; "
          f"CPU us per request (100 alerts, in-process, ABBA): {times}")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        sys.exit(main(sys.argv[1]))
