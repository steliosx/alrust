"""alrust and Alerta 9.1.0, side by side on one hosted runner (bench-compare.yml).

A diagnostic benchmark, not a scoreboard. Four arms separate the two causes
of the gap #255 measured:
  alrust          alrust as built in CI (release), its own 16-connection pool;
  alerta-1x4      gunicorn, 1 gthread worker x 4 threads (#255's setup);
  alerta-1x4-pgb  the same behind pgbouncer (transaction pooling, 16 server
                  connections), which removes the backend fork and server-side
                  authentication of Alerta's per-request connection (a lower
                  bound: the client still connects to pgbouncer each time);
  alerta-Nx4-pgb  N = vCPUs workers behind pgbouncer, which also removes the
                  one-core limit of a single Python process.
Cases: GET /alerts (50 newest), GET /alerts/count, POST /alert (one fixed
body: the duplicate path). Every request sends the same API key and
Accept-Encoding: identity (Alerta enables Flask-Compress; alrust does not
compress). Results are relative to this runner; see the workflow's summary.

Standard library only; run with `python3 -I`. Inputs come from the
environment that scripts/bench/setup.sh writes.
"""

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ["BENCH_WORK"])
PG_BIN = Path(os.environ["BENCH_PG_BIN"])
VENV = Path(os.environ["BENCH_ALERTA_VENV"])
PGBOUNCER = os.environ["BENCH_PGBOUNCER"]
ALRUST = os.environ["ALRUST_BIN"]
OHA = os.environ["OHA_BIN"]
OUT = Path(sys.argv[1])
MODE = (ROOT / "scripts/bench/MODE").read_text().strip()
QUICK = MODE == "quick"

KEY = "bench-key-0123456789abcdef0123456789abcdef"
HEADERS = {
    "Authorization": f"Key {KEY}",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}
PG = {"host": "127.0.0.1", "port": "5432", "user": "postgres", "password": "postgres"}
PGB_PORT = 6432
VCPUS = os.cpu_count() or 4
ARMS = ["alrust", "alerta-1x4", "alerta-1x4-pgb", f"alerta-{VCPUS}x4-pgb"]
REPS = 1 if QUICK else 3
WARM_S = 2 if QUICK else 5
TIMED_S = 5 if QUICK else 15
CONCURRENCY = [1] if QUICK else [1, 8]
SEED_ALERTS = 1000
EVENTS = ["BackupFailed", "DiskFull", "HighCPU", "NodeDown", "Latency",
          "QueueDepth", "CertExpiry", "MemoryLow", "ServiceDown", "PacketLoss"]
SEVERITIES = ["critical", "major", "minor", "warning", "normal"]
DUP_BODY = json.dumps({"resource": "bench-dup", "event": "Dup", "environment": "Production",
                       "severity": "major", "service": ["web"], "text": "duplicate path"})
CASES = {
    "list": ("GET", "/alerts?page-size=50&sort-by=lastReceiveTime", None),
    "count": ("GET", "/alerts/count", None),
    "write-dup": ("POST", "/alert", DUP_BODY),
}


def log(msg):
    print(f"[bench {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def psql(db, sql, capture=True):
    env = dict(os.environ, PGPASSWORD=PG["password"])
    try:
        r = subprocess.run([str(PG_BIN / "psql"), "-h", PG["host"], "-p", PG["port"], "-U", PG["user"],
                            "-d", db, "-v", "ON_ERROR_STOP=1", "-tA", "-c", sql],
                           env=env, capture_output=capture, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"psql failed on {db}: {sql[:120]}\n{e.stderr}", file=sys.stderr, flush=True)
        raise
    return r.stdout.strip() if capture else ""


def http(method, url, body=None):
    data = body.encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(HEADERS))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, raw, r.headers.get("Content-Encoding")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Encoding")


def wait_ready(base, proc, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with {proc.returncode}")
        try:
            status, _, _ = http("GET", base + "/management/gtg")
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{base} not ready in {timeout}s")


def db_url(db, port=5432):
    return f"postgresql://postgres:postgres@127.0.0.1:{port}/{db}"


def start(arm, db, logf):
    """Start one arm's server on database `db`; returns (process, base url)."""
    if arm == "alrust":
        env = dict(os.environ, DATABASE_URL=db_url(db), ALRUST_BIND="127.0.0.1:8080",
                   ALRUST_AUTH_REQUIRED="true", RUST_LOG="warn")
        proc = subprocess.Popen([ALRUST], env=env, stdout=logf, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:8080"
    else:
        workers = int(arm.split("-")[1].split("x")[0])
        port = PGB_PORT if arm.endswith("-pgb") else 5432
        env = {"PATH": f"{VENV}/bin:/usr/bin:/bin", "HOME": str(WORK),
               "DATABASE_URL": db_url(db, port), "AUTH_REQUIRED": "True",
               "SECRET_KEY": "bench-not-secret", "LOG_LEVEL": "WARNING"}
        proc = subprocess.Popen([str(VENV / "bin/gunicorn"), "--workers", str(workers),
                                 "--worker-class", "gthread", "--threads", "4",
                                 "--bind", "127.0.0.1:8081", "alerta.app:create_app()"],
                                env=env, cwd=str(WORK), stdout=logf, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:8081"
    wait_ready(base, proc)
    return proc, base


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ------------------------------------------------------------ CPU and stats

def _stat(pid):
    """(ppid, utime+stime, cutime+cstime) in clock ticks, or None."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = raw[raw.rindex(")") + 2:].split()
    return int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[13]) + int(fields[14])


def tree_cpu(root_pid):
    """CPU ticks of a process tree: live members, plus the root's reaped children."""
    stats = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            s = _stat(int(d))
            if s:
                stats[int(d)] = s
    if root_pid not in stats:
        return 0
    members, frontier = {root_pid}, [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, _, _) in stats.items():
            if ppid == parent and pid not in members:
                members.add(pid)
                frontier.append(pid)
    return sum(stats[p][1] for p in members) + stats[root_pid][2]


def postmaster_pid():
    return int((WORK / "data/postmaster.pid").read_text().split()[0])


def pg_db_stats(db):
    psql("postgres", "SELECT pg_stat_clear_snapshot()")
    row = psql("postgres", "SELECT sessions, xact_commit, numbackends FROM pg_stat_database "
                           f"WHERE datname = '{db}'")
    s, c, n = (int(x) for x in row.split("|"))
    return {"sessions": s, "xact_commit": c, "numbackends": n}


# ------------------------------------------------------------------- oha

def oha_json_flag():
    help_text = subprocess.run([OHA, "--help"], capture_output=True, text=True).stdout
    if "--output-format" in help_text:
        return ["--output-format", "json"]
    if "--json" in help_text:
        return ["--json"]
    raise RuntimeError("oha has neither --output-format nor --json")


def oha(base, case, conc, seconds, out_file, json_flag):
    method, path, body = CASES[case]
    cmd = [OHA, "-z", f"{seconds}s", "-c", str(conc), "--no-tui", "--http-version", "1.1",
           "-w", "-m", method] + json_flag
    for k, v in HEADERS.items():
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", body]
    cmd.append(base + path)
    r = subprocess.run(cmd, capture_output=True, text=True)
    out_file.write_text(r.stdout)
    if r.returncode != 0:
        raise RuntimeError(f"oha exited {r.returncode}: {r.stderr[-500:]}")
    return json.loads(r.stdout)


def summarize_oha(j):
    summary = j.get("summary", {})
    pct = j.get("latencyPercentiles", {})
    status = j.get("statusCodeDistribution", {}) or {}
    errors = j.get("errorDistribution", {}) or {}
    ok = sum(int(v) for k, v in status.items() if 200 <= int(k) < 300)
    return {
        "rps": summary.get("requestsPerSec") or 0.0,
        "p50_ms": (pct.get("p50") or 0.0) * 1000,
        "p99_ms": (pct.get("p99") or 0.0) * 1000,
        "requests": sum(int(v) for v in status.values()),
        "non_2xx": sum(int(v) for k, v in status.items() if not 200 <= int(k) < 300),
        "ok": ok,
        "errors": sum(int(v) for v in errors.values()),
        "size_per_request": summary.get("sizePerRequest"),
    }


# ------------------------------------------------------------ the run

def seed(record):
    log("seeding through Alerta (alerta-1x4, direct)")
    psql("postgres", "CREATE DATABASE bench_seed")
    with open(OUT / "server-seed.log", "w") as logf:
        proc, base = start("alerta-1x4", "bench_seed", logf)
        try:
            psql("bench_seed", "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count) "
                               f"VALUES ('bench-key', '{KEY}', 'bench', ARRAY['admin','read','write'], "
                               "'benchmark', timezone('UTC', now()) + interval '2 days', 0)")
            for i in range(SEED_ALERTS):
                alert = {"resource": f"host-{i:04d}", "event": EVENTS[i % len(EVENTS)],
                         "environment": "Production", "severity": SEVERITIES[i % len(SEVERITIES)],
                         "service": ["web"], "group": "bench", "value": str(i % 97),
                         "text": f"bench alert {i}", "tags": ["bench"],
                         "attributes": {"region": ["eu", "us", "ap"][i % 3]}}
                status, raw, _ = http("POST", base + "/alert", json.dumps(alert))
                if status != 201:
                    raise RuntimeError(f"seed alert {i}: {status} {raw[:200]!r}")
        finally:
            stop(proc)
    record["seed"] = {"alerts": int(psql("bench_seed", "SELECT count(*) FROM alerts"))}


def expected(db):
    ids = psql(db, "SELECT id FROM alerts ORDER BY last_receive_time DESC, id LIMIT 50").split("\n")
    total = int(psql(db, "SELECT count(*) FROM alerts"))
    return ids, total


def check(arm, base, db, record):
    """Same answer on every arm before anything is scored."""
    ids, total = expected(db)
    status, raw, enc = http("GET", base + CASES["list"][1])
    body = json.loads(raw)
    got = [a["id"] for a in body.get("alerts", [])]
    status_c, raw_c, enc_c = http("GET", base + CASES["count"][1])
    count = json.loads(raw_c)
    ok = status == 200 and body.get("total") == total and got == ids and status_c == 200 \
        and count.get("total") == total
    record["checks"].append({"arm": arm, "db": db, "ok": ok, "list_bytes": len(raw),
                             "list_content_encoding": enc, "count_bytes": len(raw_c),
                             "count_content_encoding": enc_c,
                             "severityCounts": count.get("severityCounts"),
                             "statusCounts": count.get("statusCounts")})
    if not ok:
        log(f"CHECK FAILED for {arm}: status {status}, total {body.get('total')} vs {total}")
    return ok


def cell(arm, proc, base, case, conc, rep, db, json_flag, record):
    raw_dir = OUT / "raw"
    oha(base, case, conc, WARM_S, raw_dir / f"warm-{arm}-{case}-c{conc}-r{rep}.json", json_flag)
    time.sleep(1.5)
    pg0, srv0, pm0 = pg_db_stats(db), tree_cpu(proc.pid), tree_cpu(postmaster_pid())
    t0 = time.monotonic()
    j = oha(base, case, conc, TIMED_S, raw_dir / f"timed-{arm}-{case}-c{conc}-r{rep}.json", json_flag)
    wall = time.monotonic() - t0
    srv1, pm1 = tree_cpu(proc.pid), tree_cpu(postmaster_pid())
    time.sleep(1.5)
    pg1 = pg_db_stats(db)
    s = summarize_oha(j)
    hz = os.sysconf("SC_CLK_TCK")
    s.update({"arm": arm, "case": case, "c": conc, "rep": rep, "db": db, "wall_s": wall,
              "server_cpu_s": (srv1 - srv0) / hz, "postgres_cpu_s": (pm1 - pm0) / hz,
              "pg_sessions": pg1["sessions"] - pg0["sessions"],
              "pg_commits": pg1["xact_commit"] - pg0["xact_commit"]})
    req = s["requests"] or 1
    s["sessions_per_request"] = s["pg_sessions"] / req
    s["commits_per_request"] = s["pg_commits"] / req
    s["server_cpu_ms_per_request"] = 1000 * s["server_cpu_s"] / req
    s["postgres_cpu_ms_per_request"] = 1000 * s["postgres_cpu_s"] / req
    record["cells"].append(s)
    log(f"{arm:16} {case:9} c={conc} r{rep}: {s['rps']:.1f} rps, p50 {s['p50_ms']:.2f} ms, "
        f"p99 {s['p99_ms']:.2f} ms, errors {s['errors']}, non-2xx {s['non_2xx']}, "
        f"sessions/req {s['sessions_per_request']:.3f}, server CPU {s['server_cpu_ms_per_request']:.3f} ms/req")


def start_pgbouncer():
    d = WORK / "pgb"
    d.mkdir(exist_ok=True)
    (d / "userlist.txt").write_text('"postgres" "postgres"\n')
    (d / "pgbouncer.ini").write_text(f"""[databases]
* = host=127.0.0.1 port=5432
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {PGB_PORT}
unix_socket_dir =
auth_type = scram-sha-256
auth_file = {d}/userlist.txt
pool_mode = transaction
default_pool_size = 16
max_client_conn = 400
ignore_startup_parameters = extra_float_digits,options
logfile = {d}/pgbouncer.log
pidfile = {d}/pgbouncer.pid
""")
    proc = subprocess.Popen([PGBOUNCER, str(d / "pgbouncer.ini")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    if proc.poll() is not None:
        raise RuntimeError("pgbouncer did not start; see pgb/pgbouncer.log")
    return proc


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "raw").mkdir(exist_ok=True)
    record = {"mode": MODE, "arms": ARMS, "reps": REPS, "warm_s": WARM_S, "timed_s": TIMED_S,
              "concurrency": CONCURRENCY, "vcpus": VCPUS, "checks": [], "cells": [],
              "env": {"postgres": psql("postgres", "SELECT version()"),
                      "pgbouncer": subprocess.run([PGBOUNCER, "--version"], capture_output=True,
                                                  text=True).stdout.strip(),
                      "oha": subprocess.run([OHA, "--version"], capture_output=True,
                                            text=True).stdout.strip(),
                      "alrust_sha256": subprocess.run(["sha256sum", ALRUST], capture_output=True,
                                                      text=True).stdout.split()[0],
                      "alrust_commit": os.environ.get("GITHUB_SHA", "unknown"),
                      "alerta_commit": subprocess.run(["git", "-C", str(ROOT / "reference/alerta"),
                                                       "rev-parse", "HEAD"], capture_output=True,
                                                      text=True).stdout.strip(),
                      "runner": f"{os.environ.get('RUNNER_OS', '?')} {os.environ.get('ImageOS', '?')} "
                                f"{os.environ.get('ImageVersion', '?')}",
                      "headers": {k: ("Key <bench key>" if k == "Authorization" else v)
                                  for k, v in HEADERS.items()}}}
    json_flag = oha_json_flag()
    seed(record)
    # Every copy is made now: a template must have no connection open, and
    # pgbouncer keeps server connections once it starts.
    dbs = {}
    for arm in ARMS:
        safe = arm.replace("-", "_")
        dbs[(arm, "read")] = f"bench_read_{safe}"
        for rep in range(REPS):
            for conc in CONCURRENCY:
                dbs[(arm, "write", rep, conc)] = f"bench_write_{safe}_r{rep}_c{conc}"
    for name in dbs.values():
        # The seeding server's backends may still be exiting: a template with
        # any connection open cannot be copied, so retry briefly.
        for attempt in range(10):
            try:
                psql("postgres", f"CREATE DATABASE {name} TEMPLATE bench_seed")
                break
            except subprocess.CalledProcessError:
                if attempt == 9:
                    raise
                time.sleep(1)
    log(f"{len(dbs)} database copies created")
    pgb = start_pgbouncer()
    all_ok = True
    try:
        for rep in range(REPS):
            order = ARMS[rep % len(ARMS):] + ARMS[:rep % len(ARMS)]
            for arm in order:
                db = dbs[(arm, "read")]
                with open(OUT / f"server-{arm}-r{rep}-read.log", "w") as logf:
                    proc, base = start(arm, db, logf)
                    try:
                        if not check(arm, base, db, record):
                            all_ok = False
                            continue
                        for case in ("list", "count"):
                            for conc in CONCURRENCY:
                                cell(arm, proc, base, case, conc, rep, db, json_flag, record)
                    finally:
                        stop(proc)
                for conc in CONCURRENCY:
                    db = dbs[(arm, "write", rep, conc)]
                    with open(OUT / f"server-{arm}-r{rep}-write-c{conc}.log", "w") as logf:
                        proc, base = start(arm, db, logf)
                        try:
                            cell(arm, proc, base, "write-dup", conc, rep, db, json_flag, record)
                        finally:
                            stop(proc)
    finally:
        stop(pgb)
        (OUT / "results.json").write_text(json.dumps(record, indent=1))
        try:
            write_summary(record)
        except Exception as e:  # a partial run still keeps results.json
            print(f"summary failed: {e!r}", file=sys.stderr)
    if not all_ok:
        sys.exit("a correctness check failed; see results.json")


def write_summary(record):
    rows = {}
    for c in record["cells"]:
        rows.setdefault((c["case"], c["c"], c["arm"]), []).append(c)
    lines = [f"# alrust and Alerta: diagnostic benchmark ({record['mode']} mode)", "",
             f"{record['env']['postgres']}", "",
             f"Runner vCPUs {record['vcpus']}; {record['reps']} repetition(s); "
             f"warm-up {record['warm_s']} s, timed {record['timed_s']} s; medians across repetitions.",
             "Relative numbers on one hosted runner; not a capacity claim.", "",
             "| case | c | arm | rps | p50 ms | p99 ms | errors+non-2xx | sessions/req | "
             "commits/req | server CPU ms/req | postgres CPU ms/req |",
             "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for case in CASES:
        for conc in record["concurrency"]:
            for arm in record["arms"]:
                cs = rows.get((case, conc, arm))
                if not cs:
                    continue
                med = lambda k: statistics.median(x[k] for x in cs)
                bad = sum(x["errors"] + x["non_2xx"] for x in cs)
                lines.append(f"| {case} | {conc} | {arm} | {med('rps'):.1f} | {med('p50_ms'):.2f} | "
                             f"{med('p99_ms'):.2f} | {bad} | {med('sessions_per_request'):.3f} | "
                             f"{med('commits_per_request'):.2f} | {med('server_cpu_ms_per_request'):.3f} | "
                             f"{med('postgres_cpu_ms_per_request'):.3f} |")
    lines += ["", "Checks before scoring (same total, the same 50 newest ids, the count's total):", ""]
    for ch in record["checks"]:
        lines.append(f"- {ch['arm']} on {ch['db']}: {'ok' if ch['ok'] else 'FAILED'}; list "
                     f"{ch['list_bytes']} bytes, Content-Encoding {ch['list_content_encoding']}")
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
