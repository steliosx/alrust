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

Correctness gates (Codex BENCH-R1, BENCH-R2; added after runs 43788943429
and 43792492613, whose numbers they do not cover):
  reads   every arm's 50 newest alerts must match the database field by
          field, the count's severity and status groups must match SQL, and
          every arm's list and count bodies must equal the first arm's, with
          only `href` origins normalized;
  writes  the duplicate alert is primed with one request and checked in the
          database; after the warm-up and after the timed run its
          duplicate_count must have grown by exactly the 2xx responses
          counted (oha -w waits for in-flight requests), with one row, the
          same id and unchanged identity fields.
A failed gate makes the check or the repetition invalid; it is never scored.

The "tuning" suite runs the same cases and gates on other arms: what an
operator can change in Alerta's deployment with no change to its code.
N = vCPUs workers in each, so only the named setting differs from the base:
  alrust                 the reference;
  alerta-Nx4             the base: direct connections, the default plugins;
  alerta-Nx4-nofwd       PLUGINS without `forwarder`. With no destination
                         set, that plugin forwards nothing, but its
                         post_receive returns the alert, so every receive
                         also rewrites the alert's tags and attributes;
  alerta-Nx4-pgb         behind pgbouncer over TCP with SCRAM;
  alerta-Nx4-pgb-nofwd   both;
  alerta-Nx4-pgbu-nofwd  both, with the pgbouncer hop on a Unix socket with
                         `trust`, which removes the client's per-request
                         SCRAM exchange with the pooler.
The write case sends an alert without tags, so the tag order that
`forwarder`'s rewrite changes is not exercised; the cross-arm body gate
covers the seeded alerts only.

The "addon" suite adds scripts/bench/alerta_fast.py: Alerta as released,
with a few functions replaced when the server starts (no fork). Its file
header describes each patch. Every arm is N workers without `forwarder`:
  alrust                      the reference;
  alerta-Nx4-nofwd            the base;
  alerta-Nx4-pgbu-nofwd       the best arm that changes no code;
  alerta-Nx4-reuse-nofwd      one kept connection per worker thread;
  alerta-Nx4-reuse-hot-nofwd  and the key-use and metrics writes collected
                              and written about once a second;
  alerta-Nx4-fast-nofwd       every patch.
Before the suite's first cell, a check of the add-on: after 50 requests
and a wait of three flush intervals, the key's use count and the timers'
count must each have grown by exactly 50. Then every session on the
database is terminated, as a failover would do it: the next 50 requests
must all be answered 200 on new connections, and both counts must reach
exactly 100, while running and after a clean stop. A failure stops the
run before anything is scored.

BENCH_MODE selects "quick" (plumbing: 1 repetition, c=1, 5 s; the default)
or "full". The workflow passes "quick" on pull requests and the dispatch
input otherwise (DECISIONS Y69).

BENCH_SUITE selects "compare" (the cases above; the default), "mixed" or
"both". The mixed suite is specified in
docs/performance/benchmark-methodology.md: 8 writers, each with its own
1,000 alert identities, and 16 readers of the 50 newest alerts, alone and
together, each cell on a fresh copy of the seeded database. Its arms are
one alrust binary with a pool of 16, 32 and 64 connections, the same binary
with a pool of 16 that writes key use on every request, and Alerta with one
worker per vCPU behind pgbouncer. Its clients are scripts/bench/mixed_client.py
(closed loops, one process each), not oha: oha sends one fixed body and
does not read the answers. Its gates:
  before  the read gate above, on the cell's seeded copy;
  during  every list answer is a 2xx that parses, has at most 50 alerts and
          is in descending lastReceiveTime order (the client checks it);
  after   with every client stopped and every request answered: a row for
          an identity exists exactly when it had a 2xx answer, with
          duplicate_count = its 2xx answers - 1 (warm-up, timed phase and
          the requests that were in flight at the stop, together); the
          table has the seeded rows plus those; then the list and count
          are compared with the arm's own final rows.
The final list check allows for equal lastReceiveTime values: each served
alert is compared with its own row, its served lastReceiveTime included
(mixed_checks.py states the time policy), and the served rows' times must
be the database's 50 newest times in order.
A mixed cell that fails any of these gates is not scored, and it makes the
suite's exit non-zero after the other cells have run and results.json is
written.

Standard library only; run with `python3 -I`. Inputs come from the
environment that scripts/bench/setup.sh writes.
"""

import gzip
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# `python3 -I` leaves the script's directory off the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mixed_checks import cell_valid, served_time_problems  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ["BENCH_WORK"])
PG_BIN = Path(os.environ["BENCH_PG_BIN"])
VENV = Path(os.environ["BENCH_ALERTA_VENV"])
PGBOUNCER = os.environ["BENCH_PGBOUNCER"]
ALRUST = os.environ["ALRUST_BIN"]
OHA = os.environ["OHA_BIN"]
OUT = Path(sys.argv[1])
MODE = os.environ.get("BENCH_MODE", "quick")
if MODE not in ("quick", "full"):
    sys.exit(f"BENCH_MODE must be quick or full, not {MODE!r}")
QUICK = MODE == "quick"
SUITE = os.environ.get("BENCH_SUITE", "compare")
if SUITE not in ("compare", "mixed", "both", "tuning", "addon", "all"):
    sys.exit(f"BENCH_SUITE must be compare, mixed, both, tuning, addon or all, not {SUITE!r}")

KEY = "bench-key-0123456789abcdef0123456789abcdef"
HEADERS = {
    "Authorization": f"Key {KEY}",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}
PG = {"host": "127.0.0.1", "port": "5432", "user": "postgres", "password": "postgres"}
PGB_PORT = 6432
VCPUS = os.cpu_count() or 4
COMPARE_ARMS = ["alrust", "alerta-1x4", "alerta-1x4-pgb", f"alerta-{VCPUS}x4-pgb"]
TUNE_ARMS = ["alrust", f"alerta-{VCPUS}x4", f"alerta-{VCPUS}x4-nofwd", f"alerta-{VCPUS}x4-pgb",
             f"alerta-{VCPUS}x4-pgb-nofwd", f"alerta-{VCPUS}x4-pgbu-nofwd"]
ADDON_ARMS = ["alrust", f"alerta-{VCPUS}x4-nofwd", f"alerta-{VCPUS}x4-pgbu-nofwd",
              f"alerta-{VCPUS}x4-reuse-nofwd", f"alerta-{VCPUS}x4-reuse-hot-nofwd",
              f"alerta-{VCPUS}x4-fast-nofwd"]
ADDON = SUITE in ("addon", "all")
# The arms of the compare cases in this run, each once, in order.
ARMS = list(dict.fromkeys((COMPARE_ARMS if SUITE in ("compare", "both", "all") else [])
                          + (TUNE_ARMS if SUITE in ("tuning", "all") else [])
                          + (ADDON_ARMS if ADDON else [])))
# Arm options that load scripts/bench/alerta_fast.py, and its ALERTA_FAST value.
ADDON_OPTIONS = {"reuse": "reuse", "hot": "hot", "fast": "all"}
# Alerta's default PLUGINS (settings.py) without `forwarder`.
PLUGINS_NOFWD = "remote_ip,reject,heartbeat,blackout"
REPS = 1 if QUICK else 3
WARM_S = 2 if QUICK else 5
TIMED_S = 5 if QUICK else 15
CONCURRENCY = [1] if QUICK else [1, 8]
SEED_ALERTS = 1000
EVENTS = ["BackupFailed", "DiskFull", "HighCPU", "NodeDown", "Latency",
          "QueueDepth", "CertExpiry", "MemoryLow", "ServiceDown", "PacketLoss"]
SEVERITIES = ["critical", "major", "minor", "warning", "normal"]
DUP_ALERT = {"resource": "bench-dup", "event": "Dup", "environment": "Production",
             "severity": "major", "service": ["web"], "text": "duplicate path"}
DUP_BODY = json.dumps(DUP_ALERT)
# Fields compared between each served alert and its database row (API name,
# column). Timestamps are compared across arms by the body comparison.
ALERT_FIELDS = [("id", "id"), ("resource", "resource"), ("event", "event"),
                ("environment", "environment"), ("severity", "severity"), ("status", "status"),
                ("service", "service"), ("group", "group"), ("value", "value"), ("text", "text"),
                ("tags", "tags"), ("attributes", "attributes"),
                ("duplicateCount", "duplicate_count"), ("customer", "customer")]
# The mixed suite (docs/performance/benchmark-methodology.md).
MIX_CLIENT = Path(__file__).resolve().parent / "mixed_client.py"
MIX_WRITERS = 8
MIX_READERS = 16
MIX_IDENTITIES = 1000
MIX_CELLS = {"writers-alone": (MIX_WRITERS, 0), "readers-alone": (0, MIX_READERS),
             "mixed": (MIX_WRITERS, MIX_READERS)}
# One variable per sweep: the pool size (16, 32, 64) with key use written
# about once a second, and the key-use mode with the pool at 16. Both
# settings are given in every alrust arm, so none depends on a default.
ARM_ENV = {
    "alrust-p16": {"ALRUST_DB_MAX_CONNECTIONS": "16", "ALRUST_KEY_USAGE_IMMEDIATE": "false"},
    "alrust-p32": {"ALRUST_DB_MAX_CONNECTIONS": "32", "ALRUST_KEY_USAGE_IMMEDIATE": "false"},
    "alrust-p64": {"ALRUST_DB_MAX_CONNECTIONS": "64", "ALRUST_KEY_USAGE_IMMEDIATE": "false"},
    "alrust-p16-immediate": {"ALRUST_DB_MAX_CONNECTIONS": "16",
                             "ALRUST_KEY_USAGE_IMMEDIATE": "true"},
}
MIX_ARMS = [*ARM_ENV, f"alerta-{VCPUS}x4-pgb"]
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
    if arm.startswith("alrust"):
        env = dict(os.environ, DATABASE_URL=db_url(db), ALRUST_BIND="127.0.0.1:8080",
                   ALRUST_AUTH_REQUIRED="true", RUST_LOG="warn", **ARM_ENV.get(arm, {}))
        proc = subprocess.Popen([ALRUST], env=env, stdout=logf, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:8080"
    else:
        workers = int(arm.split("-")[1].split("x")[0])
        options = arm.split("-")[2:]
        unknown = set(options) - {"pgb", "pgbu", "nofwd", *ADDON_OPTIONS}
        if unknown:
            raise ValueError(f"{arm}: unknown options {sorted(unknown)}")
        if "pgbu" in options:
            # pgbouncer's Unix socket; its hba file gives `trust` there.
            url = f"postgresql:///{db}?host={WORK / 'pgb'}&port={PGB_PORT}&user={PG['user']}"
        else:
            url = db_url(db, PGB_PORT if "pgb" in options else 5432)
        env = {"PATH": f"{VENV}/bin:/usr/bin:/bin", "HOME": str(WORK),
               "DATABASE_URL": url, "AUTH_REQUIRED": "True",
               "SECRET_KEY": "bench-not-secret", "LOG_LEVEL": "WARNING"}
        if "nofwd" in options:
            env["PLUGINS"] = PLUGINS_NOFWD
        patches = [ADDON_OPTIONS[o] for o in options if o in ADDON_OPTIONS]
        target = ["alerta.app:create_app()"]
        if patches:
            env["ALERTA_FAST"] = ",".join(patches)
            target = ["--pythonpath", str(Path(__file__).resolve().parent), "alerta_fast:app"]
        # Every Alerta worker runs sql/schema.sql at boot (create_engine), and
        # its threading.Lock is per call, so workers booting together can
        # deadlock on that DDL (run 43788943429, repetition 1). With several
        # workers, --preload runs it once in the master before the fork.
        preload = ["--preload"] if workers > 1 else []
        proc = subprocess.Popen([str(VENV / "bin/gunicorn"), "--workers", str(workers),
                                 "--worker-class", "gthread", "--threads", "4", *preload,
                                 "--bind", "127.0.0.1:8081", *target],
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


def sql_json(db, sql):
    out = psql(db, sql)
    return json.loads(out) if out else None


def expected_rows(db):
    cols = ", ".join(f'"{c}"' for _, c in ALERT_FIELDS)
    return sql_json(db, f"SELECT json_agg(row_to_json(t)) FROM (SELECT {cols} FROM alerts "
                        "ORDER BY last_receive_time DESC, id LIMIT 50) t") or []


def expected_groups(db, col):
    return sql_json(db, f"SELECT json_object_agg({col}, n) FROM "
                        f"(SELECT {col}, count(*) AS n FROM alerts GROUP BY {col}) g") or {}


def fields_match(served, rows):
    """Each served alert against its row; returns a list of differences."""
    diffs = []
    if len(served) != len(rows):
        return [f"{len(served)} alerts served, {len(rows)} expected"]
    for a, r in zip(served, rows):
        for api, col in ALERT_FIELDS:
            if a.get(api) != r.get(col):
                diffs.append(f"{r.get('id')}.{api}: served {a.get(api)!r}, database {r.get(col)!r}")
    return diffs


def normalized(value):
    """The body with only `href` origins removed (each server names its own base URL)."""
    if isinstance(value, dict):
        return {k: (urlsplit(v)._replace(scheme="", netloc="").geturl()
                    if k == "href" and isinstance(v, str) else normalized(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


FIRST_BODIES = {}  # the run's first checked arm: its normalized list and count bodies


def check(arm, base, db, record):
    """Same answer on every arm before anything is scored."""
    ids, total = expected(db)
    status, raw, enc = http("GET", base + CASES["list"][1])
    body = json.loads(raw)
    got = [a["id"] for a in body.get("alerts", [])]
    status_c, raw_c, enc_c = http("GET", base + CASES["count"][1])
    count = json.loads(raw_c)
    # The bodies, for comparing fields and formatting between servers.
    (OUT / "raw" / f"check-{arm}-list.json").write_bytes(raw)
    (OUT / "raw" / f"check-{arm}-count.json").write_bytes(raw_c)
    basic_ok = status == 200 and body.get("total") == total and got == ids and status_c == 200 \
        and count.get("total") == total
    field_diffs = fields_match(body.get("alerts", []), expected_rows(db))
    groups_ok = (count.get("severityCounts") == expected_groups(db, "severity")
                 and count.get("statusCounts") == expected_groups(db, "status"))
    bodies = {"list": normalized(body), "count": normalized(count)}
    reference = FIRST_BODIES.setdefault("arm", arm)
    FIRST_BODIES.setdefault("bodies", bodies)
    cross_arm_ok = bodies == FIRST_BODIES["bodies"]
    ok = basic_ok and not field_diffs and groups_ok and cross_arm_ok
    record["checks"].append({"arm": arm, "db": db, "ok": ok, "basic_ok": basic_ok,
                             "fields_ok": not field_diffs, "field_diffs": field_diffs[:20],
                             "groups_ok": groups_ok, "cross_arm_ok": cross_arm_ok,
                             "reference_arm": reference, "list_bytes": len(raw),
                             "list_content_encoding": enc, "count_bytes": len(raw_c),
                             "count_content_encoding": enc_c,
                             "severityCounts": count.get("severityCounts"),
                             "statusCounts": count.get("statusCounts")})
    if not ok:
        log(f"CHECK FAILED for {arm}: basic {basic_ok}, fields {not field_diffs} {field_diffs[:3]}, "
            f"groups {groups_ok}, same as {reference} {cross_arm_ok}")
    return ok


def dup_rows(db):
    return sql_json(db, "SELECT json_agg(row_to_json(t)) FROM (SELECT id, resource, event, "
                        "environment, severity, status, service, text, duplicate_count "
                        "FROM alerts WHERE resource = 'bench-dup') t") or []


def dup_identity_ok(row, prime_id):
    return (row["id"] == prime_id and row["status"] == "open"
            and all(row[k] == v for k, v in DUP_ALERT.items()))


def prime_dup(base, db):
    """One request creates the duplicate alert; returns (id or None, problems)."""
    problems = []
    if dup_rows(db):
        problems.append("a bench-dup alert existed before priming")
    status, raw, _ = http("POST", base + "/alert", DUP_BODY)
    alert = (json.loads(raw).get("alert") or {}) if status == 201 else {}
    if status != 201 or alert.get("duplicateCount") != 0 or alert.get("repeat") is not False:
        problems.append(f"priming answered {status}, duplicateCount {alert.get('duplicateCount')!r}, "
                        f"repeat {alert.get('repeat')!r}")
    rows = dup_rows(db)
    if len(rows) != 1 or not dup_identity_ok(rows[0], alert.get("id")) or rows[0]["duplicate_count"] != 0:
        problems.append(f"after priming the database has {rows!r}")
    return alert.get("id"), problems


def cell(arm, proc, base, case, conc, rep, db, json_flag, record):
    raw_dir = OUT / "raw"
    write = case == "write-dup"
    persisted = {}
    if write:
        prime_id, problems = prime_dup(base, db)
        persisted = {"prime_id": prime_id, "problems": problems}
    warm = summarize_oha(oha(base, case, conc, WARM_S,
                             raw_dir / f"warm-{arm}-{case}-c{conc}-r{rep}.json", json_flag))
    time.sleep(1.5)
    if write:
        rows = dup_rows(db)
        persisted["dup_before"] = rows[0]["duplicate_count"] if len(rows) == 1 else None
        if persisted["dup_before"] != warm["ok"]:
            persisted["problems"].append(f"after the warm-up duplicate_count is "
                                         f"{persisted['dup_before']!r}, {warm['ok']} counted")
    pg0, srv0, pm0 = pg_db_stats(db), tree_cpu(proc.pid), tree_cpu(postmaster_pid())
    t0 = time.monotonic()
    j = oha(base, case, conc, TIMED_S, raw_dir / f"timed-{arm}-{case}-c{conc}-r{rep}.json", json_flag)
    wall = time.monotonic() - t0
    srv1, pm1 = tree_cpu(proc.pid), tree_cpu(postmaster_pid())
    alive = proc.poll() is None
    time.sleep(1.5)
    pg1 = pg_db_stats(db)
    s = summarize_oha(j)
    s["warm"] = warm
    if write:
        rows = dup_rows(db)
        after = rows[0]["duplicate_count"] if len(rows) == 1 else None
        persisted["dup_after"] = after
        before = persisted["dup_before"]
        if before is None or after is None or after - before != s["ok"]:
            persisted["problems"].append(f"timed: duplicate_count {before!r} -> {after!r}, "
                                         f"{s['ok']} counted")
        if len(rows) != 1 or not dup_identity_ok(rows[0], persisted["prime_id"]):
            persisted["problems"].append(f"after the timed run the database has {rows!r}")
        total = int(psql(db, "SELECT count(*) FROM alerts"))
        if total != record["seed"]["alerts"] + 1:
            persisted["problems"].append(f"{total} alerts, {record['seed']['alerts'] + 1} expected")
        persisted["ok"] = not persisted["problems"]
        s["persisted"] = persisted
    hz = os.sysconf("SC_CLK_TCK")
    s.update({"arm": arm, "case": case, "c": conc, "rep": rep, "db": db, "wall_s": wall,
              "server_cpu_s": (srv1 - srv0) / hz, "postgres_cpu_s": (pm1 - pm0) / hz,
              "pg_sessions": pg1["sessions"] - pg0["sessions"],
              "pg_commits": pg1["xact_commit"] - pg0["xact_commit"]})
    # A repetition counts only with no error, real responses, the server
    # still running at the end and, for writes, every counted write saved;
    # otherwise it is kept but not scored.
    s["server_alive"] = alive
    s["valid"] = (alive and s["errors"] == 0 and s["non_2xx"] == 0 and s["ok"] > 0
                  and warm["errors"] == 0 and warm["non_2xx"] == 0
                  and (not write or persisted["ok"]))
    req = s["requests"] or 1
    s["sessions_per_request"] = s["pg_sessions"] / req
    s["commits_per_request"] = s["pg_commits"] / req
    s["server_cpu_ms_per_request"] = 1000 * s["server_cpu_s"] / req
    s["postgres_cpu_ms_per_request"] = 1000 * s["postgres_cpu_s"] / req
    record["cells"].append(s)
    if not alive:
        log(f"{arm}: the server exited during {case} c={conc} r{rep}; its remaining cells are skipped")
    if write and not persisted["ok"]:
        log(f"{arm}: WRITE CHECK FAILED in {case} c={conc} r{rep}: {persisted['problems']}")
    log(f"{arm:16} {case:9} c={conc} r{rep}: {'' if s['valid'] else 'INVALID '}{s['rps']:.1f} rps, p50 {s['p50_ms']:.2f} ms, "
        f"p99 {s['p99_ms']:.2f} ms, errors {s['errors']}, non-2xx {s['non_2xx']}, "
        f"sessions/req {s['sessions_per_request']:.3f}, server CPU {s['server_cpu_ms_per_request']:.3f} ms/req")
    return alive


# ------------------------------------------------------------ the mixed suite

def probe_pool_setting(record):
    """The binary must know ALRUST_DB_MAX_CONNECTIONS, or the pool sweep would
    silently run three equal arms: with the value 0 it must refuse to start."""
    env = dict(os.environ, DATABASE_URL=db_url("bench_probe"), ALRUST_BIND="127.0.0.1:8080",
               ALRUST_AUTH_REQUIRED="true", RUST_LOG="warn", ALRUST_DB_MAX_CONNECTIONS="0")
    proc = subprocess.Popen([ALRUST], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        out = "(still running after 20 s) " + out
    refused = proc.returncode not in (0, -9) and "ALRUST_DB_MAX_CONNECTIONS" in out
    record["pool_setting_probe"] = {"refused_zero": refused, "exit": proc.returncode,
                                    "output": out[-500:]}
    if not refused:
        sys.exit("this alrust binary does not refuse ALRUST_DB_MAX_CONNECTIONS=0, so it does "
                 f"not know the setting; the pool sweep would mean nothing: {out[-300:]!r}")
    log("the binary knows ALRUST_DB_MAX_CONNECTIONS (it refuses 0)")


def probe_addon(record):
    """The `hot` patch must lose and add nothing: see the module text."""
    db, n = "bench_addon_probe", 50
    counts = lambda: (int(psql(db, "SELECT count FROM keys WHERE id = 'bench-key'")),
                      int(psql(db, "SELECT COALESCE(SUM(count), 0) FROM metrics WHERE type = 'timer'")))
    with open(OUT / "server-addon-probe.log", "w") as logf:
        proc, base = start(f"alerta-{VCPUS}x4-fast-nofwd", db, logf)
        try:
            # Whatever the readiness requests counted is written before the
            # starting values are read.
            time.sleep(3.0)
            before = counts()
            statuses = [http("GET", base + "/alerts/count")[0] for _ in range(n)]
            time.sleep(3.0)
            first = counts()
            # What a failover does to kept connections: every session ends.
            killed = int(psql("postgres", "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                                          f"WHERE datname = '{db}' AND pid <> pg_backend_pid()"))
            time.sleep(0.5)
            statuses += [http("GET", base + "/alerts/count")[0] for _ in range(n)]
            time.sleep(3.0)
            running = counts()
        finally:
            stop(proc)
    stopped = counts()
    grew = lambda now: [now[0] - before[0], now[1] - before[1]]
    ok = (statuses == [200] * (2 * n) and killed > 0 and grew(first) == [n, n]
          and grew(running) == [2 * n, 2 * n] and grew(stopped) == [2 * n, 2 * n])
    record["addon_probe"] = {"ok": ok, "requests": 2 * n, "non_200": sum(1 for x in statuses if x != 200),
                             "sessions_terminated": killed,
                             "key_and_timer_growth_before_termination": grew(first),
                             "key_and_timer_growth_running": grew(running),
                             "key_and_timer_growth_stopped": grew(stopped)}
    if not ok:
        (OUT / "results.json").write_text(json.dumps(record, indent=1))
        sys.exit(f"the add-on's collected writes are wrong: {record['addon_probe']}")
    log(f"add-on check: {2 * n} requests gave {2 * n} key uses and timer counts, with {killed} "
        "sessions terminated half-way and no failed request")


def key_updates(db):
    """Row updates of the `keys` table so far (statistics; they lag by about
    a second). It shows the key-use mode: about one update per request when
    key use is written on every request, about one per second otherwise."""
    psql(db, "SELECT pg_stat_clear_snapshot()")
    return int(psql(db, "SELECT coalesce(sum(n_tup_upd), 0) FROM pg_stat_user_tables "
                        "WHERE relname = 'keys'"))


def percentile(sorted_values, q):
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def class_stats(requests, t0, t1):
    """One class's requests: all phases for the checks, the timed window
    [t0, t1) (seconds after the start time, by each request's end) for the
    rates. A request is [x, status, start us, end us]."""
    timed = [r for r in requests if t0 <= r[3] / 1e6 < t1]
    ok = [r for r in timed if 200 <= r[1] < 300]
    lat = sorted((r[3] - r[2]) / 1000 for r in ok)
    return {
        "requests_all_phases": len(requests),
        "errors": sum(1 for r in requests if r[1] == 0),
        "non_2xx": sum(1 for r in requests if r[1] != 0 and not 200 <= r[1] < 300),
        "ok_warm": sum(1 for r in requests if 200 <= r[1] < 300 and r[3] / 1e6 < t0),
        "ok": len(ok),
        "ok_closing": sum(1 for r in requests if 200 <= r[1] < 300 and r[3] / 1e6 >= t1),
        "requests": len(timed),
        "rps": len(ok) / (t1 - t0),
        "p50_ms": percentile(lat, 0.50),
        "p99_ms": percentile(lat, 0.99),
    }


def mix_persisted(db, per_identity, seed_alerts):
    """After the load: one row per identity with a 2xx answer, no other, and
    duplicate_count = its 2xx answers - 1. `per_identity` maps a resource to
    its 2xx answers over every phase."""
    rows = sql_json(db, "SELECT json_object_agg(resource, duplicate_count) FROM alerts "
                        "WHERE resource LIKE 'mix-w%'") or {}
    want = {res: n - 1 for res, n in per_identity.items() if n > 0}
    missing = sorted(set(want) - set(rows))
    extra = sorted(set(rows) - set(want))
    wrong = sorted(res for res in set(want) & set(rows) if rows[res] != want[res])
    total = int(psql(db, "SELECT count(*) FROM alerts"))
    problems = []
    if missing:
        problems.append(f"{len(missing)} identities with a 2xx answer have no row: {missing[:5]}")
    if extra:
        problems.append(f"{len(extra)} rows have no 2xx answer: {extra[:5]}")
    if wrong:
        problems.append(f"{len(wrong)} rows have another duplicate_count, such as "
                        f"{[(r, rows[r], want[r]) for r in wrong[:5]]} (resource, row, expected)")
    if total != seed_alerts + len(want):
        problems.append(f"{total} alerts, {seed_alerts + len(want)} expected")
    return {"identities_written": len(want), "rows": len(rows),
            "writes_2xx": sum(per_identity.values()),
            "duplicate_count_sum": sum(rows.values()), "alerts": total,
            "problems": problems, "ok": not problems}


def final_check(base, db):
    """The list and the count against this arm's own final rows, nothing
    being written. Several rows can share a lastReceiveTime, so the order
    among them is not compared: each served alert must equal its own row,
    and the served rows' times must be the database's newest times in order."""
    problems = []
    status, raw, _ = http("GET", base + CASES["list"][1])
    status_c, raw_c, _ = http("GET", base + CASES["count"][1])
    total = int(psql(db, "SELECT count(*) FROM alerts"))
    try:
        body, count = json.loads(raw), json.loads(raw_c)
        served = body["alerts"]
        ids = [a["id"] for a in served]
    except (ValueError, KeyError, TypeError) as e:
        return {"ok": False, "problems": [f"list {status} or count {status_c} does not parse: {e!r}"]}
    if status != 200 or body.get("total") != total:
        problems.append(f"list answered {status} with total {body.get('total')!r}, {total} rows")
    if status_c != 200 or count.get("total") != total:
        problems.append(f"count answered {status_c} with total {count.get('total')!r}, {total} rows")
    if (count.get("severityCounts") != expected_groups(db, "severity")
            or count.get("statusCounts") != expected_groups(db, "status")):
        problems.append("the count's groups differ from SQL")
    if len(ids) != min(50, total) or len(set(ids)) != len(ids):
        problems.append(f"{len(ids)} alerts served ({len(set(ids))} distinct), {min(50, total)} expected")
    if not all(isinstance(i, str) and re.fullmatch(r"[0-9A-Za-z-]{1,64}", i) for i in ids):
        problems.append("an alert id is not a plain identifier; the rows are not compared")
        return {"ok": False, "problems": problems}
    cols = ", ".join(f'"{c}"' for _, c in ALERT_FIELDS)
    id_list = ", ".join(f"'{i}'" for i in ids) or "NULL"
    rows = sql_json(db, f"SELECT json_agg(row_to_json(t)) FROM (SELECT {cols}, last_receive_time "
                        f"FROM alerts WHERE id IN ({id_list})) t") or []
    by_id = {r["id"]: r for r in rows}
    diffs = []
    for a in served:
        r = by_id.get(a["id"])
        if r is None:
            diffs.append(f"{a['id']}: served, not in the database")
            continue
        for api, col in ALERT_FIELDS:
            if a.get(api) != r.get(col):
                diffs.append(f"{a['id']}.{api}: served {a.get(api)!r}, database {r.get(col)!r}")
    if diffs:
        problems.append(f"{len(diffs)} field differences: {diffs[:5]}")
    time_problems = served_time_problems(served, by_id)
    if time_problems:
        problems.append(f"{len(time_problems)} time problems: {time_problems[:5]}")
    newest = sql_json(db, "SELECT json_agg(t.last_receive_time) FROM (SELECT last_receive_time "
                          "FROM alerts ORDER BY last_receive_time DESC LIMIT 50) t") or []
    if [by_id[i]["last_receive_time"] for i in ids if i in by_id] != newest:
        problems.append("the served rows' last_receive_time values are not the newest in order")
    return {"ok": not problems, "problems": problems, "list_bytes": len(raw)}


def score_cell(s, record, read_problems=()):
    """Sets the finished cell's validity, keeps its record and returns the
    validity, which the suite's exit depends on."""
    arm, name, rep = s["arm"], s["case"], s["rep"]
    writers, readers, problems = s["writers"], s["readers"], s["problems"]
    bad = sum(s[k]["errors"] + s[k]["non_2xx"] for k in ("writes", "reads"))
    s["valid"] = cell_valid(s, writers, readers)
    record["mixed_cells"].append(s)
    if not s["valid"]:
        log(f"{arm}: MIXED CELL INVALID {name} r{rep}: {problems} {s['persisted']['problems']} "
            f"{s['final_check']['problems']} {list(read_problems)[:2]}")
    log(f"{arm:20} {name:13} r{rep}: {'' if s['valid'] else 'INVALID '}"
        f"writes {s['writes']['rps']:.1f}/s p50 {s['writes']['p50_ms']:.2f} p99 {s['writes']['p99_ms']:.2f} ms; "
        f"reads {s['reads']['rps']:.1f}/s p50 {s['reads']['p50_ms']:.2f} p99 {s['reads']['p99_ms']:.2f} ms; "
        f"errors+non-2xx {bad}; rows {s['persisted']['alerts']}")
    return s["valid"]


def mixed_cell(arm, name, rep, db, record):
    """One cell of the mixed suite on its own database copy. Returns whether
    the cell is valid: False when the pre-load check or any later gate
    failed. The cell's record is kept either way."""
    writers, readers = MIX_CELLS[name]
    tag = f"{arm}-{name}-r{rep}"
    raw_dir = OUT / "raw"
    s = {"arm": arm, "case": name, "rep": rep, "db": db, "writers": writers, "readers": readers}
    with open(OUT / f"server-{tag}.log", "w") as logf:
        proc, base = start(arm, db, logf)
        try:
            if not check(arm, base, db, record):
                s.update({"valid": False, "problems": ["the pre-load check failed"]})
                record["mixed_cells"].append(s)
                return False
            url = urlsplit(base)
            pg0, keys0 = pg_db_stats(db), key_updates(db)
            start_at = time.time() + 2.0
            files, clients = [], []
            for role, n in (("write", writers), ("read", readers)):
                for number in range(n):
                    out = raw_dir / f"mixed-{tag}-{role}{number}.json.gz"
                    files.append((role, number, out))
                    clients.append(subprocess.Popen(
                        [sys.executable, "-I", str(MIX_CLIENT), role, url.hostname, str(url.port),
                         str(number), str(MIX_IDENTITIES), repr(start_at), str(out)],
                        env=dict(os.environ, BENCH_KEY=KEY)))
            time.sleep(max(0.0, start_at + WARM_S - time.time()))
            t0, srv0, pm0 = time.time(), tree_cpu(proc.pid), tree_cpu(postmaster_pid())
            time.sleep(max(0.0, t0 + TIMED_S - time.time()))
            t1, srv1, pm1 = time.time(), tree_cpu(proc.pid), tree_cpu(postmaster_pid())
            alive = proc.poll() is None
            # SIGTERM: each client finishes the request in flight, then exits.
            for c in clients:
                c.terminate()
            problems = []
            for c in clients:
                try:
                    if c.wait(timeout=60) != 0:
                        problems.append(f"a client exited with {c.returncode}")
                except subprocess.TimeoutExpired:
                    c.kill()
                    c.wait()
                    problems.append("a client did not stop in 60 s")
            time.sleep(1.5)
            pg1, keys1 = pg_db_stats(db), key_updates(db)
            by_role = {"write": [], "read": []}
            per_identity, read_problems = {}, []
            for role, number, out in files:
                try:
                    with gzip.open(out, "rt") as f:
                        data = json.load(f)
                except (OSError, ValueError) as e:
                    problems.append(f"{out.name} is not readable: {e!r}")
                    continue
                by_role[role] += data["requests"]
                read_problems += data["problems"]
                if role == "write":
                    for index, status, _, _ in data["requests"]:
                        if 200 <= status < 300:
                            res = f"mix-w{number}-{index:04d}"
                            per_identity[res] = per_identity.get(res, 0) + 1
            w0, w1 = t0 - start_at, t1 - start_at
            s["writes"] = class_stats(by_role["write"], w0, w1)
            s["reads"] = class_stats(by_role["read"], w0, w1)
            s["reads"]["body_problems"] = sum(1 for r in by_role["read"] if r[0] == 1)
            s["reads"]["body_problem_examples"] = read_problems[:5]
            s["persisted"] = mix_persisted(db, per_identity, record["seed"]["alerts"])
            still_alive = proc.poll() is None
            s["final_check"] = (final_check(base, db) if still_alive
                                else {"ok": False, "problems": ["the server is not running"]})
        finally:
            stop(proc)
    hz = os.sysconf("SC_CLK_TCK")
    wall = t1 - t0
    done = s["writes"]["ok"] + s["reads"]["ok"]
    all_requests = s["writes"]["requests_all_phases"] + s["reads"]["requests_all_phases"]
    s.update({"wall_s": wall, "server_cpu_s": (srv1 - srv0) / hz,
              "postgres_cpu_s": (pm1 - pm0) / hz,
              "server_cpu_per_s": (srv1 - srv0) / hz / wall,
              "postgres_cpu_per_s": (pm1 - pm0) / hz / wall,
              # Both classes together: the CPU is not split between reads and writes.
              "completed_requests": done,
              "server_cpu_ms_per_request": 1000 * (srv1 - srv0) / hz / (done or 1),
              "postgres_cpu_ms_per_request": 1000 * (pm1 - pm0) / hz / (done or 1),
              # From before the clients start until after they stop: every phase.
              "pg_sessions": pg1["sessions"] - pg0["sessions"],
              "pg_commits": pg1["xact_commit"] - pg0["xact_commit"],
              "requests_all_phases": all_requests,
              "commits_per_request": (pg1["xact_commit"] - pg0["xact_commit"]) / (all_requests or 1),
              "key_updates": keys1 - keys0,
              "key_updates_per_request": (keys1 - keys0) / (all_requests or 1),
              "server_alive": alive and still_alive, "problems": problems})
    return score_cell(s, record, read_problems)


def run_mixed(record, dbs):
    """Every cell runs, also after an invalid one. Returns False when any
    cell was invalid."""
    all_ok = True
    for rep in range(REPS):
        order = MIX_ARMS[rep % len(MIX_ARMS):] + MIX_ARMS[:rep % len(MIX_ARMS)]
        for arm in order:
            for name in MIX_CELLS:
                if not mixed_cell(arm, name, rep, dbs[(arm, "mixed", name, rep)], record):
                    all_ok = False
    return all_ok


def start_pgbouncer():
    d = WORK / "pgb"
    d.mkdir(exist_ok=True)
    (d / "userlist.txt").write_text('"postgres" "postgres"\n')
    # TCP clients authenticate with SCRAM, as before; the Unix socket (the
    # -pgbu arms) is `trust`. The directory is the runner user's, mode 700.
    (d / "hba.conf").write_text("local all all trust\n"
                                "host all all 127.0.0.1/32 scram-sha-256\n")
    (d / "pgbouncer.ini").write_text(f"""[databases]
* = host=127.0.0.1 port=5432
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {PGB_PORT}
unix_socket_dir = {d}
auth_type = hba
auth_hba_file = {d}/hba.conf
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
    compare, mixed = bool(ARMS), SUITE in ("mixed", "both", "all")
    record = {"mode": MODE, "suite": SUITE, "arms": ARMS if compare else [],
              "reps": REPS, "warm_s": WARM_S, "timed_s": TIMED_S,
              "concurrency": CONCURRENCY, "vcpus": VCPUS, "checks": [], "cells": [],
              "mixed_arms": MIX_ARMS if mixed else [], "mixed_cells": [],
              "mixed": {"writers": MIX_WRITERS, "readers": MIX_READERS,
                        "identities_per_writer": MIX_IDENTITIES, "arm_env": ARM_ENV},
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
    if mixed:
        dbs[("probe",)] = "bench_probe"
    if ADDON:
        dbs[("addon-probe",)] = "bench_addon_probe"
    for arm in ARMS if compare else []:
        safe = arm.replace("-", "_")
        dbs[(arm, "read")] = f"bench_read_{safe}"
        for rep in range(REPS):
            for conc in CONCURRENCY:
                dbs[(arm, "write", rep, conc)] = f"bench_write_{safe}_r{rep}_c{conc}"
    for arm in MIX_ARMS if mixed else []:
        for name in MIX_CELLS:
            for rep in range(REPS):
                dbs[(arm, "mixed", name, rep)] = (f"bench_mix_{arm}_{name}_r{rep}").replace("-", "_")
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
    if mixed:
        probe_pool_setting(record)
    if ADDON:
        probe_addon(record)
    pgb = start_pgbouncer()
    all_ok = True
    try:
        if mixed and not run_mixed(record, dbs):
            all_ok = False
        for rep in range(REPS if compare else 0):
            order = ARMS[rep % len(ARMS):] + ARMS[:rep % len(ARMS)]
            for arm in order:
                db = dbs[(arm, "read")]
                with open(OUT / f"server-{arm}-r{rep}-read.log", "w") as logf:
                    proc, base = start(arm, db, logf)
                    try:
                        if not check(arm, base, db, record):
                            all_ok = False
                            continue
                        alive = True
                        for case in ("list", "count"):
                            for conc in CONCURRENCY:
                                if alive:
                                    alive = cell(arm, proc, base, case, conc, rep, db, json_flag, record)
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
             "A repetition is scored only with no error, real responses, the server alive at its end "
             "and, for writes, every counted write saved in the database.",
             ""]
    if record["cells"]:
        lines += ["| case | c | arm | valid reps | rps | p50 ms | p99 ms | errors+non-2xx (all reps) | sessions/req | "
                  "commits/req | server CPU ms/req | postgres CPU ms/req |",
                  "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for case in CASES:
        for conc in record["concurrency"]:
            for arm in record["arms"]:
                allc = rows.get((case, conc, arm))
                if not allc:
                    continue
                bad = sum(x["errors"] + x["non_2xx"] for x in allc)
                cs = [x for x in allc if x["valid"]]
                if not cs:
                    lines.append(f"| {case} | {conc} | {arm} | 0/{len(allc)} | - | - | - | {bad} | - | - | - | - |")
                    continue
                med = lambda k: statistics.median(x[k] for x in cs)
                lines.append(f"| {case} | {conc} | {arm} | {len(cs)}/{len(allc)} | {med('rps'):.1f} | {med('p50_ms'):.2f} | "
                             f"{med('p99_ms'):.2f} | {bad} | {med('sessions_per_request'):.3f} | "
                             f"{med('commits_per_request'):.2f} | {med('server_cpu_ms_per_request'):.3f} | "
                             f"{med('postgres_cpu_ms_per_request'):.3f} |")
    lines += ["", "Checks before scoring (total and the 50 newest ids; every listed alert's fields against "
              "the database; the count's groups against SQL; the same bodies as the first arm, "
              "`href` origins aside):", ""]
    for ch in record["checks"]:
        lines.append(f"- {ch['arm']} on {ch['db']}: {'ok' if ch['ok'] else 'FAILED'} (basic "
                     f"{ch['basic_ok']}, fields {ch['fields_ok']}, groups {ch['groups_ok']}, same as "
                     f"{ch['reference_arm']} {ch['cross_arm_ok']}); list {ch['list_bytes']} bytes, "
                     f"Content-Encoding {ch['list_content_encoding']}")
    if "addon_probe" in record:
        a = record["addon_probe"]
        lines += ["", f"Add-on check: {a['requests']} requests gave key-use and timer growth "
                      f"{a['key_and_timer_growth_running']} while running and "
                      f"{a['key_and_timer_growth_stopped']} after the stop; "
                      f"{a['sessions_terminated']} sessions were terminated half-way and "
                      f"{a['non_200']} requests failed."]
    writes = [c for c in record["cells"] if "persisted" in c]
    if writes:
        bad = [c for c in writes if not c["persisted"]["ok"]]
        lines += ["", f"Write checks: {len(writes) - len(bad)} of {len(writes)} duplicate-write "
                      "repetitions saved exactly the writes counted."]
        for c in bad:
            lines.append(f"- FAILED {c['arm']} c={c['c']} r{c['rep']}: {c['persisted']['problems']}")
    if record["mixed_cells"]:
        lines += mixed_summary(record)
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")


def mixed_summary(record):
    m = record["mixed"]
    rows = {}
    for c in record["mixed_cells"]:
        rows.setdefault((c["case"], c["arm"]), []).append(c)
    lines = ["", "## Mixed load", "",
             f"{m['writers']} writers, each with its own {m['identities_per_writer']} alert identities "
             f"in order and again; {m['readers']} readers of the 50 newest alerts; closed loops, one "
             "connection each; every cell on a fresh copy of the seeded database. Rates and latencies "
             "are of 2xx answers that ended in the timed phase. CPU is of the server's process tree and "
             "of PostgreSQL, for both classes together, per second and per completed request. "
             "The clients are Python processes on the same runner and take part of its CPU. "
             "commits/req and key updates/req cover every phase; key updates are row updates of "
             "the `keys` table from PostgreSQL's statistics, and show the key-use mode.", "",
             "A repetition is scored only with no error or non-2xx answer in any phase, every list "
             "answer in order, the server alive, every 2xx write found in the database with the right "
             "duplicate_count, and the final list and count equal to the final rows.", "",
             "| case | arm | valid reps | writes/s | write p50 ms | write p99 ms | reads/s | read p50 ms | "
             "read p99 ms | errors+non-2xx (all reps) | server CPU s/s | postgres CPU s/s | "
             "server CPU ms/req | postgres CPU ms/req | commits/req | key updates/req | final rows |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for case in MIX_CELLS:
        for arm in record["mixed_arms"]:
            allc = rows.get((case, arm))
            if not allc:
                continue
            scored = [x for x in allc if "writes" in x]
            bad = sum(x[k]["errors"] + x[k]["non_2xx"] for x in scored for k in ("writes", "reads"))
            cs = [x for x in allc if x["valid"]]
            if not cs:
                lines.append(f"| {case} | {arm} | 0/{len(allc)} | - | - | - | - | - | - | {bad} "
                             "| - | - | - | - | - | - | - |")
                continue
            med = lambda f: statistics.median(f(x) for x in cs)
            cls = lambda k, on: (f"{med(lambda x: x[k]['rps']):.1f} | {med(lambda x: x[k]['p50_ms']):.2f} | "
                                 f"{med(lambda x: x[k]['p99_ms']):.2f}") if on else "- | - | -"
            lines.append(f"| {case} | {arm} | {len(cs)}/{len(allc)} | {cls('writes', cs[0]['writers'])} | "
                         f"{cls('reads', cs[0]['readers'])} | {bad} | "
                         f"{med(lambda x: x['server_cpu_per_s']):.2f} | "
                         f"{med(lambda x: x['postgres_cpu_per_s']):.2f} | "
                         f"{med(lambda x: x['server_cpu_ms_per_request']):.3f} | "
                         f"{med(lambda x: x['postgres_cpu_ms_per_request']):.3f} | "
                         f"{med(lambda x: x['commits_per_request']):.2f} | "
                         f"{med(lambda x: x['key_updates_per_request']):.4f} | "
                         f"{med(lambda x: x['persisted']['alerts']):.0f} |")
    invalid = [c for c in record["mixed_cells"] if not c["valid"]]
    for c in invalid:
        lines.append("")
        lines.append(f"- NOT SCORED {c['arm']} {c['case']} r{c['rep']}: {c.get('problems')} "
                     f"{c.get('persisted', {}).get('problems')} {c.get('final_check', {}).get('problems')}")
    return lines


if __name__ == "__main__":
    main()
