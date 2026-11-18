#!/usr/bin/env python3
"""Mixed suite per alrust PR #263 (docs/performance/benchmark-methodology.md).

Cells: writers-alone (8 writers), readers-alone (16 readers of the newest-50
list, seeded DB), mixed (both at once). Every cell and rep starts from a fresh
CREATE DATABASE ... TEMPLATE base_seed (1,000 alerts). 5 s warm-up, 15 s timed.
Arms: alrust (pool default 16), alrust-p32, alrust-p64, alerta-uwsgi-5,
alerta-gunicorn-8. A rep is valid only with zero errors/non-2xx/list-check
problems in every phase, a live server at the end, every identity's row
present iff it had a 2xx with duplicate_count = 2xx - 1, and the final
/alerts/count and newest-50 list equal to the final rows in SQL.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from common import (
    API_KEY, ALRUST_NAME, PG_NAME, RAW, REPS, configs, container_state, docker, file_done,
    http_json, ids_of, inspect_container, log, pg_exec, pg_scalar, redact, remove_apps, rotate,
    write_json,
)
from writes import reset_work

MIX_DIR = RAW / "mixed263"
LIST_PATH = "/alerts?sort-by=lastReceiveTime&page-size=50"
CELLS = ["writers-alone", "readers-alone", "mixed"]
ARMS = ["alrust", "alrust-p32", "alrust-p64", "alerta-uwsgi-5", "alerta-gunicorn-8"]
WARM_S = 5.0
TIMED_S = 15.0
LEAD_S = 2.0
CLIENT = str(Path(__file__).with_name("mixed_client.py"))


def cell_path(cell, arm, rep):
    return MIX_DIR / f"{cell}_{arm}_r{rep}.json"


def cpu_usec(container):
    r = docker(["exec", container, "cat", "/sys/fs/cgroup/cpu.stat"], check=False, timeout=20)
    for line in (r.stdout or "").splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return None


def db_stats(db):
    row = pg_exec(
        "SELECT sessions, xact_commit, tup_inserted, tup_updated FROM pg_stat_database WHERE datname = %s",
        (db,),
    )[0]
    return {"sessions": int(row[0]), "xact_commit": int(row[1]), "tup_inserted": int(row[2]), "tup_updated": int(row[3])}


def spawn(role, n, port, start_at, out, core):
    spec = {"role": role, "n": n, "port": port, "start_at": start_at, "warm_s": WARM_S,
            "timed_s": TIMED_S, "path": LIST_PATH, "out": str(out)}
    env = dict(os.environ, BENCH_KEY=API_KEY)
    p = subprocess.Popen(["taskset", "-c", str(core), sys.executable, CLIENT], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=open(str(out) + ".err", "w"), text=True, env=env)
    p.stdin.write(json.dumps(spec))
    p.stdin.close()
    return p


def final_checks(port, db, writers_doc):
    out = {}
    # identity gate
    if writers_doc is not None:
        rows = dict(pg_exec("SELECT resource, duplicate_count FROM alerts WHERE resource LIKE 'mix-w%%'", db=db))
        sent = {k: sum(v) for k, v in writers_doc["per_identity"].items()}
        missing = [k for k in sent if sent[k] > 0 and k not in rows]
        extra = [k for k in rows if sent.get(k, 0) == 0]
        wrong = [k for k in sent if k in rows and rows[k] != sent[k] - 1]
        out["identity_gate"] = {
            "identities_with_2xx": len(sent), "rows": len(rows),
            "missing": len(missing), "extra": len(extra), "wrong_duplicate_count": len(wrong),
            "examples": (missing[:3] + extra[:3] + wrong[:3]),
            "sum_duplicate_count": sum(rows.values()), "sum_2xx": sum(sent.values()),
            "ok": not missing and not extra and not wrong
                  and sum(rows.values()) == sum(sent.values()) - len(rows),
        }
    total_sql = int(pg_scalar("SELECT COUNT(*) FROM alerts", db=db))
    sev_sql = dict(pg_exec("SELECT severity, COUNT(*)::int FROM alerts GROUP BY severity", db=db))
    st, doc, _ = http_json(port, "/alerts/count", timeout=60)
    api_sev = {k: v for k, v in (doc.get("severityCounts") or {}).items() if v}
    out["count_check"] = {"http": st, "api_total": doc.get("total"), "sql_total": total_sql,
                          "api_severity": api_sev, "sql_severity": sev_sql,
                          "ok": st == 200 and doc.get("total") == total_sql and api_sev == sev_sql}
    top = pg_exec("SELECT id, last_receive_time FROM alerts ORDER BY last_receive_time DESC LIMIT 50", db=db)
    ties = pg_exec("SELECT id, last_receive_time FROM alerts ORDER BY last_receive_time DESC FETCH FIRST 50 ROWS WITH TIES", db=db)
    st2, ldoc, _ = http_json(port, LIST_PATH, timeout=60)
    api_ids = ids_of(ldoc)
    sql_ids = [r[0] for r in top]
    same = api_ids == sql_ids
    tie_only = False
    if not same and len(api_ids) == len(sql_ids) and len(set(api_ids)) == len(api_ids):
        # the ids may differ only inside groups of equal last_receive_time
        # (the stored times have millisecond precision); the time sequence must match
        times = {r[0]: r[1] for r in ties}
        api_times = [times.get(i) for i in api_ids]
        tie_only = None not in api_times and api_times == [r[1] for r in top]
    out["list_check"] = {"http": st2, "api_total": ldoc.get("total"), "sql_total": total_sql,
                         "ids_equal": same, "differs_only_within_equal_times": tie_only,
                         "ok": st2 == 200 and ldoc.get("total") == total_sql and (same or tie_only)}
    return out


def run_cell(cell, arm, rep, order):
    dest = cell_path(cell, arm, rep)
    if file_done(dest):
        return
    cfg = configs()[arm]
    port = cfg["port"]
    log(f"mixed263 {cell} {arm} r{rep} first={order[0]}")
    db = reset_work(arm, "base_seed", 1000)
    before = db_stats(db)
    start_at = time.time() + LEAD_S
    procs = {}
    tmp = MIX_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    if cell in ("writers-alone", "mixed"):
        procs["writers"] = (spawn("writers", 8, port, start_at, tmp / f"{dest.stem}.w.json", 6), tmp / f"{dest.stem}.w.json")
    if cell in ("readers-alone", "mixed"):
        procs["readers"] = (spawn("readers", 16, port, start_at, tmp / f"{dest.stem}.r.json", 7), tmp / f"{dest.stem}.r.json")
    t1 = start_at + WARM_S
    t2 = t1 + TIMED_S
    time.sleep(max(0, t1 - time.time()))
    cpu0 = {"wall": time.time(), "mono": time.monotonic(), "server": cpu_usec(cfg["container"]), "postgres": cpu_usec(PG_NAME)}
    time.sleep(max(0, t2 - time.time()))
    cpu1 = {"wall": time.time(), "mono": time.monotonic(), "server": cpu_usec(cfg["container"]), "postgres": cpu_usec(PG_NAME)}
    results = {}
    for role, (p, out) in procs.items():
        p.wait(timeout=180)
        errf = Path(str(out) + ".err")
        se = errf.read_text() if errf.exists() else ""
        if p.returncode != 0:
            raise RuntimeError(f"{role} client rc={p.returncode} {redact(se[-800:])}")
        errf.unlink()
        results[role] = json.loads(out.read_text())
        out.unlink()
    after = db_stats(db)
    info = inspect_container(cfg["container"])
    alive = bool(info and info["State"]["Running"])
    checks = final_checks(port, db, results.get("writers")) if alive else {}
    cpu = {}
    span = cpu1["wall"] - cpu0["wall"]
    for k in ("server", "postgres"):
        if cpu0[k] is not None and cpu1[k] is not None and span > 0:
            cpu[f"{k}_cpu_s_per_s"] = (cpu1[k] - cpu0[k]) / 1e6 / span
    w = results.get("writers")
    r = results.get("readers")
    timed_requests = (w["ok"]["timed"] if w else 0) + (r["ok"]["timed"] if r else 0)
    if timed_requests:
        for k in ("server", "postgres"):
            if f"{k}_cpu_s_per_s" in cpu:
                cpu[f"{k}_cpu_ms_per_timed_ok_request"] = cpu[f"{k}_cpu_s_per_s"] * span * 1000 / timed_requests
    cpu["window_s"] = span
    cpu["window_monotonic_s"] = cpu1["mono"] - cpu0["mono"]
    clock_gap = span - (cpu1["mono"] - cpu0["mono"])
    cpu["denominator"] = "2xx requests of both classes that ended in the timed phase"
    errors = (w["errors_all_phases"] if w else 0) + (r["errors_all_phases"] if r else 0)
    reasons = []
    if errors:
        reasons.append(f"{errors} errors/non-2xx/list-check problems across phases")
    if not alive:
        reasons.append("server not running at the end")
    for name, c in checks.items():
        if not c.get("ok"):
            reasons.append(f"{name} failed")
    if abs(clock_gap) > 1.0:
        reasons.append(f"realtime and monotonic clocks differ by {clock_gap:.2f}s over the timed window (freeze)")
    for role, d in results.items():
        if d["late_start_s"] > 0.5:
            reasons.append(f"{role} connected {d['late_start_s']:.2f}s after start_at")
    if w is not None:
        w_slim = {k: v for k, v in w.items() if k != "per_identity"}
        w_slim["identities_written"] = len(w["per_identity"])
    else:
        w_slim = None
    doc = {
        "suite": "mixed263", "cell": cell, "config": arm, "rep": rep, "order": order,
        "order_pos": order.index(arm) + 1, "database": db, "pool": cfg.get("pool"),
        "warm_s": WARM_S, "timed_s": TIMED_S, "list_path": LIST_PATH,
        "writers": w_slim, "readers": r,
        "pg_stat_database_delta": {k: after[k] - before[k] for k in after},
        "cpu": cpu, "server_alive": alive, "checks": checks,
        "valid": not reasons, "invalid_reasons": reasons,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if w is not None:
        write_json(MIX_DIR / "identities" / f"{dest.stem}.json", w["per_identity"])
    write_json(dest, doc)
    log(
        f"  valid={doc['valid']} writes/s={w['timed_rate'] if w else '-'} wp50={w['timed_p50_ms'] if w else '-'} "
        f"reads/s={r['timed_rate'] if r else '-'} rp50={r['timed_p50_ms'] if r else '-'} "
        f"err={errors} rows={checks.get('count_check', {}).get('sql_total')} reasons={reasons}"
    )


def mixed263_expected():
    return [(c, a, r) for r in REPS for c in CELLS for a in ARMS]


def mixed263_complete():
    return all(file_done(cell_path(*x)) for x in mixed263_expected())


def run_mixed263():
    MIX_DIR.mkdir(parents=True, exist_ok=True)
    idx = 0
    for rep in REPS:
        for cell in CELLS:
            order = rotate(ARMS, idx)
            idx += 1
            for arm in order:
                if file_done(cell_path(cell, arm, rep)):
                    continue
                try:
                    run_cell(cell, arm, rep, order)
                except Exception as exc:
                    write_json(cell_path(cell, arm, rep).with_suffix(".fail.json"),
                               {"error": redact(str(exc)), "when": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                    log(f"FAIL mixed263 {cell} {arm} r{rep} {redact(str(exc))}")
    remove_apps()
    log(f"mixed263 finished complete={mixed263_complete()}")
