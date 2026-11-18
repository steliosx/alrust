#!/usr/bin/env python3
"""Shared helpers for the tuned Alerta vs alrust bench.

Secrets stay in /tmp. Result numbers are produced only by this run.
"""
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
OHA = "/tmp/oha"
SEED_PY = ROOT / "seed.py"

PG_NAME = "r2-pg"
ALERTA_NAME = "r2-alerta"
ALRUST_NAME = "r2-alrust"
PG_IMAGE = "postgres:18.7"
ALERTA_IMAGE = "alerta-bench:9.1.0"
UWSGI_IMAGE = "alerta-bench-uwsgi:9.1.0"
ALRUST_IMAGE = "alrust:main-d12dc77"

PG_DIGEST = "sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc"
ALERTA_ID = "sha256:a5e18b9aad5934f8e1259927a9996afe49be5e8d8554295860917a6716ec84e5"  # rebuilt 16:10 after box reset; was b6c8b473
ALRUST_ID = "sha256:6d198e2943820024ab2a2c6c1e7cc56a632f88d480a00060eca6122ae20f25e3"  # rebuilt after box reset; was 1f8f987c, same binary sha
ALRUST_BINARY_SHA = "8ae548595488659783f201d5a0165905e37afb7f27cddfcf7fe395fa6d475eca"
ALERTA_COMMIT = "f4312727783dad64198b62ed778bbca98a5144a3"
ALRUST_COMMIT = "d12dc7705541747c8a90a4387405560f60665f73"
UWSGI_VERSION = "2.0.28"

HOST = "127.0.0.1"
APP_NANO = 4_000_000_000
PG_NANO = 2_000_000_000
APP_CPUSET = "0-3"
PG_CPUSET = "4-5"
LOAD_CPUS = "6-7"
SAMPLE_INTERVAL_S = 0.2

READ_CASES = [
    ("list", "/alerts?sort-by=lastReceiveTime&page-size=50"),
    ("event", "/alerts?event=BackupFailed&sort-by=lastReceiveTime&page-size=50"),
    ("severity", "/alerts?severity=critical&sort-by=lastReceiveTime&page-size=50"),
    ("count", "/alerts/count"),
    ("empty", "/alerts?event=NoSuchEventBench&sort-by=lastReceiveTime&page-size=50"),
]
READ_LEVELS = [1, 8, 16, 32]
WRITE_LEVELS = [1, 8, 16, 32]
REPS = [1, 2, 3]
GUNICORN_WORKERS = [4, 8, 16]
SWEEP_CONFIGS = [f"alerta-gunicorn-{n}" for n in GUNICORN_WORKERS]

SECRET_KEY_PATH = Path("/tmp/r2-bench-api-key")
SECRET_PG_PATH = Path("/tmp/r2-bench-pg-pass")
UWSGI_CMD_PATH = RAW / "uwsgi-cmd.json"

IDENT_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")


def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + msg
    print(line, flush=True)
    progress = RAW / "progress.txt"
    progress.parent.mkdir(parents=True, exist_ok=True)
    with progress.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_secret(path, nbytes):
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    import secrets
    value = secrets.token_hex(nbytes)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return value


API_KEY = load_secret(SECRET_KEY_PATH, 20)
PG_PASSWORD = load_secret(SECRET_PG_PATH, 24)


def redact(text):
    if text is None:
        return ""
    out = str(text)
    if API_KEY:
        out = out.replace(API_KEY, "<redacted>")
    if PG_PASSWORD:
        out = out.replace(PG_PASSWORD, "<redacted>")
    return out


def assert_no_secrets(text, label):
    if API_KEY and API_KEY in text:
        raise RuntimeError(f"{label} contains the API key")
    if PG_PASSWORD and PG_PASSWORD in text:
        raise RuntimeError(f"{label} contains the database password")


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    return str(obj)


def dump_json(obj):
    text = json.dumps(sanitize(obj), indent=1, allow_nan=False) + "\n"
    assert_no_secrets(text, "json")
    return text


def write_json(path, obj):
    atomic_write(path, dump_json(obj))


def file_done(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return True


def run_cmd(cmd, check=True, timeout=None):
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(redact(result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"))
    return result


def docker(args, check=True, timeout=None):
    return run_cmd(["docker", *args], check=check, timeout=timeout)


def ident(name):
    if not IDENT_RE.fullmatch(name):
        raise RuntimeError(f"refusing database name {name!r}")
    return name


def conninfo(db):
    ident(db)
    return (
        f"host={HOST} port=5432 dbname={db} user=postgres "
        f"password={PG_PASSWORD} connect_timeout=5"
    )


def pg_connect(db="postgres"):
    return psycopg.connect(conninfo(db), autocommit=True)


def pg_exec(sql, params=None, db="postgres"):
    with pg_connect(db) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return None
            return cur.fetchall()


def pg_scalar(sql, params=None, db="postgres"):
    rows = pg_exec(sql, params, db)
    if not rows:
        return None
    return rows[0][0]


def container_state(name):
    result = docker(["inspect", "-f", "{{.State.Running}}", name], check=False)
    if result.returncode != 0:
        return "missing"
    return "running" if result.stdout.strip() == "true" else "exited"


def inspect_container(name):
    result = docker(["inspect", name], check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)[0]


def port_open(port):
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


def database_url(db):
    ident(db)
    return f"postgresql://postgres:{PG_PASSWORD}@{HOST}:5432/{db}"


def config_db(purpose, config_id):
    short = {
        "alrust": "alrust",
        "alerta-uwsgi-5": "uwsgi",
        "alerta-gunicorn-4": "g4",
        "alerta-gunicorn-8": "g8",
        "alerta-gunicorn-16": "g16",
        "alrust-p32": "ap32",
        "alrust-p64": "ap64",
    }[config_id]
    return ident(f"{purpose}_{short}")


def gunicorn_cmd(workers):
    return [
        "gunicorn",
        "--workers", str(workers),
        "--worker-class", "gthread",
        "--threads", "4",
        "--bind", "127.0.0.1:8081",
        "wsgi:app",
    ]


def default_uwsgi_cmd():
    # --http is uWSGI's HTTP router. --http-socket plus --http-keepalive closed
    # connections under oha's HTTP/1.1 keep-alive client (errors at c=1 and c=32).
    # --enable-threads is a stub that uWSGI 2.0.28 already treats as true; it
    # does not add request threads. --py-call-uwsgi-fork-hooks refreshes the
    # CPython interpreter after prefork. --threads is absent: concurrency stays
    # 5 processes. There is no nginx and no /api mount.
    return [
        "uwsgi",
        "--http", "127.0.0.1:8081",
        "--http-keepalive",
        "--master",
        "--processes", "5",
        "--enable-threads",
        "--py-call-uwsgi-fork-hooks",
        "--die-on-term",
        "--need-app",
        "--module", "wsgi:app",
    ]


def uwsgi_cmd():
    if file_done(UWSGI_CMD_PATH):
        cmd = json.loads(UWSGI_CMD_PATH.read_text(encoding="utf-8"))["cmd"]
        if not isinstance(cmd, list) or not cmd or cmd[0] != "uwsgi":
            raise RuntimeError("uwsgi-cmd.json is not a uwsgi argv")
        return cmd
    return default_uwsgi_cmd()


def save_uwsgi_cmd(cmd, reason):
    write_json(UWSGI_CMD_PATH, {"cmd": cmd, "reason": reason, "uwsgi_version": UWSGI_VERSION})


def configs():
    found = {
        "alrust": {
            "id": "alrust",
            "kind": "alrust",
            "port": 8082,
            "image": ALRUST_IMAGE,
            "container": ALRUST_NAME,
            "pool": None,
        },
        "alrust-p32": {
            "id": "alrust-p32", "kind": "alrust", "port": 8082,
            "image": ALRUST_IMAGE, "container": ALRUST_NAME, "pool": 32,
        },
        "alrust-p64": {
            "id": "alrust-p64", "kind": "alrust", "port": 8082,
            "image": ALRUST_IMAGE, "container": ALRUST_NAME, "pool": 64,
        },
        "alerta-uwsgi-5": {
            "id": "alerta-uwsgi-5",
            "kind": "uwsgi",
            "port": 8081,
            "image": UWSGI_IMAGE,
            "container": ALERTA_NAME,
            "processes": 5,
        },
    }
    for workers in GUNICORN_WORKERS:
        cid = f"alerta-gunicorn-{workers}"
        found[cid] = {
            "id": cid,
            "kind": "gunicorn",
            "port": 8081,
            "image": ALERTA_IMAGE,
            "container": ALERTA_NAME,
            "workers": workers,
            "threads": 4,
        }
    return found


def argv_for(config_id):
    cfg = configs()[config_id]
    if cfg["kind"] == "gunicorn":
        return gunicorn_cmd(cfg["workers"])
    if cfg["kind"] == "uwsgi":
        return uwsgi_cmd()
    return ["/usr/local/bin/alrust"]


def redact_argv(argv):
    return redact(" ".join(argv))


def assert_pins(info, nano, cpuset, label):
    host = info["HostConfig"]
    got_nano = int(host.get("NanoCpus") or 0)
    got_set = host.get("CpusetCpus") or ""
    if got_nano != nano or got_set != cpuset:
        raise RuntimeError(
            f"{label} pins NanoCpus={got_nano} CpusetCpus={got_set!r}; "
            f"expected NanoCpus={nano} CpusetCpus={cpuset}"
        )


def inside_pin_text(name):
    nproc = docker(["exec", name, "nproc"], timeout=20).stdout.strip()
    cpuset = docker(["exec", name, "cat", "/sys/fs/cgroup/cpuset.cpus.effective"], timeout=20).stdout.strip()
    cpu_max = docker(["exec", name, "cat", "/sys/fs/cgroup/cpu.max"], timeout=20).stdout.strip()
    return {"nproc": nproc, "cpuset_cpus_effective": cpuset, "cpu_max": cpu_max}


def db_exists(name):
    ident(name)
    found = pg_scalar("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    return found == 1


def terminate_db(name):
    ident(name)
    pg_exec(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (name,),
    )


def recreate_db(name, template):
    ident(name)
    ident(template)
    if name == template:
        raise RuntimeError("refusing to clone a database onto itself")
    last = None
    for _attempt in range(12):
        terminate_db(name)
        terminate_db(template)
        try:
            pg_exec(f"DROP DATABASE IF EXISTS {name}")
            pg_exec(f"CREATE DATABASE {name} TEMPLATE {template}")
            return
        except Exception as exc:
            last = exc
            time.sleep(0.4)
    raise RuntimeError(f"could not recreate {name} from {template}: {redact(last)}")


def alert_count(db):
    return int(pg_scalar("SELECT COUNT(*) FROM alerts", db=db))


def alert_fingerprint(db):
    return pg_scalar(
        "SELECT COUNT(*)::text || '|' || "
        "COALESCE(SUM(duplicate_count),0)::text || '|' || "
        "md5(COALESCE(string_agg(id::text, ',' ORDER BY id::text), '')) "
        "FROM alerts",
        db=db,
    )


def aggregates(db):
    row = pg_exec(
        """
        SELECT COUNT(*)::bigint,
               COALESCE(SUM(duplicate_count), 0)::bigint,
               COALESCE(SUM(COALESCE(cardinality(history), 0)), 0)::bigint
        FROM alerts
        """,
        db=db,
    )[0]
    return {
        "alert_rows": int(row[0]),
        "duplicate_count_sum": int(row[1]),
        "history_entries": int(row[2]),
    }


def auth_headers():
    return {"Authorization": "Key " + API_KEY, "Accept": "application/json"}


def http_json(port, path, method="GET", body=None, timeout=60, headers=None):
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    hdrs = auth_headers() if headers is None else dict(headers)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://{HOST}:{port}{path}",
        data=data,
        method=method,
        headers=hdrs,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            server = resp.headers.get("Server")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        server = exc.headers.get("Server") if exc.headers else None
    doc = json.loads(raw.decode()) if raw else {}
    return status, doc, server


def wait_total(port, expected, timeout=120, headers=None):
    """headers=None sends the admin key. Pass an explicit dict to omit it.

    An unknown key is rejected with 401 even when AUTH_REQUIRED is false,
    so schema bootstrap must poll before the key row exists.
    """
    deadline = time.time() + timeout
    last = "none"
    while time.time() < deadline:
        try:
            status, doc, _server = http_json(port, "/alerts/count", timeout=5, headers=headers)
            last = f"http {status} total {doc.get('total')}"
            if status == 200 and doc.get("total") == expected:
                return doc["total"]
        except Exception as exc:
            last = redact(f"{type(exc).__name__}: {exc}")
        time.sleep(0.4)
    raise TimeoutError(f"port {port} expected total {expected}; last {last}")


def wait_root(port, timeout=120):
    deadline = time.time() + timeout
    last = "none"
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"http://{HOST}:{port}/")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status < 500:
                    return resp.status
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return exc.code
            last = f"http {exc.code}"
        except Exception as exc:
            last = redact(f"{type(exc).__name__}: {exc}")
        time.sleep(0.4)
    raise TimeoutError(f"port {port} root not ready; last {last}")


def insert_key(db):
    if not API_KEY or any(ch not in "0123456789abcdef" for ch in API_KEY):
        raise RuntimeError("api key is not hex")
    last = None
    for _attempt in range(30):
        try:
            existing = pg_scalar("SELECT COUNT(*) FROM keys WHERE key = %s", (API_KEY,), db)
            if int(existing) != 0:
                return
            pg_exec(
                "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count) "
                "VALUES (%s, %s, 'bench', ARRAY['admin','read','write'], "
                "'disposable bench key', TIMESTAMP '2099-01-01 00:00:00', 0)",
                ("bench-admin-key", API_KEY),
                db,
            )
            return
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"could not insert admin key: {redact(last)}")


def remove_apps():
    for name in (ALERTA_NAME, ALRUST_NAME):
        if container_state(name) != "missing":
            docker(["rm", "-f", name], timeout=60)


def start_postgres():
    state = container_state(PG_NAME)
    if state == "missing":
        if port_open(5432):
            raise RuntimeError("port 5432 is open and r2-pg is not running")
        log("starting postgres")
        docker([
            "run", "-d", "--name", PG_NAME, "--network", "host",
            "--cpus", "2", "--cpuset-cpus", PG_CPUSET,
            "-e", "POSTGRES_USER=postgres",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", "POSTGRES_DB=postgres",
            PG_IMAGE,
        ], timeout=60)
    elif state == "exited":
        log("starting existing postgres container")
        docker(["start", PG_NAME], timeout=60)
    info = inspect_container(PG_NAME)
    assert_pins(info, PG_NANO, PG_CPUSET, PG_NAME)
    deadline = time.time() + 90
    last = "none"
    while time.time() < deadline:
        try:
            if pg_scalar("SELECT 1") == 1:
                version = pg_scalar("SELECT version()")
                if "18.7" not in str(version):
                    raise RuntimeError(f"PostgreSQL version is not 18.7: {version}")
                log("postgres ready")
                return version
        except Exception as exc:
            last = redact(exc)
        time.sleep(1)
    raise TimeoutError(f"postgres did not become ready: {last}")


def _docker_run_app(config_id, db):
    cfg = configs()[config_id]
    name = cfg["container"]
    if container_state(name) != "missing":
        docker(["rm", "-f", name], timeout=60)
    cmd = [
        "run", "-d", "--name", name, "--network", "host",
        "--cpus", "4", "--cpuset-cpus", APP_CPUSET,
        "--label", f"tuned.config={config_id}",
        "--label", f"tuned.db={db}",
        "-e", f"DATABASE_URL={database_url(db)}",
    ]
    if cfg["kind"] == "alrust":
        cmd.extend([
            "-e", "ALRUST_BIND=127.0.0.1:8082",
            "-e", f"ALRUST_ADMIN_KEY={API_KEY}",
            "-e", "ALRUST_HOUSEKEEPING_INTERVAL=0",
        ])
        if cfg.get("pool"):
            cmd.extend(["-e", f"ALRUST_DB_MAX_CONNECTIONS={cfg['pool']}"])
        cmd.extend([
            ALRUST_IMAGE,
        ])
    else:
        cmd.append(cfg["image"])
        cmd.extend(argv_for(config_id))
    docker(cmd, timeout=60)


def container_matches(config_id, db):
    cfg = configs()[config_id]
    info = inspect_container(cfg["container"])
    if not info or not info["State"]["Running"]:
        return False
    labels = info["Config"].get("Labels") or {}
    if labels.get("tuned.config") != config_id or labels.get("tuned.db") != db:
        return False
    try:
        assert_pins(info, APP_NANO, APP_CPUSET, cfg["container"])
    except RuntimeError:
        return False
    return True


def wait_port_closed(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(port):
            return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} is still open")


def gunicorn_ready_gate(config_id, settle_s=8.0, probes=160):
    """Rerun fix: gunicorn (no --preload) boots every worker with its own schema DDL.
    Under write load that DDL deadlocked with INSERTs, a worker exited with code 3
    (Worker failed to boot) and the master halted. Wait until all workers are
    booted and settled, then touch the server with fresh connections, before load."""
    cfg = configs()[config_id]
    if cfg["kind"] != "gunicorn":
        return
    name = cfg["container"]
    deadline = time.time() + 90
    while time.time() < deadline:
        logs = docker(["logs", name], check=False)
        text = (logs.stdout or "") + (logs.stderr or "")
        if text.count("Booting worker") >= cfg["workers"] and "Control socket listening" in text:
            break
        time.sleep(0.5)
    else:
        raise TimeoutError(f"{config_id} workers did not all boot")
    time.sleep(settle_s)
    for _ in range(probes):
        http_json(cfg["port"], "/alerts/count", timeout=10)
    info = inspect_container(name)
    if not info or not info["State"]["Running"]:
        raise RuntimeError(f"{name} stopped during ready gate")
    log(f"gunicorn ready gate passed for {config_id}")


def ensure_server(config_id, db, expected_total):
    """Run exactly one application container, pinned, on this database."""
    cfg = configs()[config_id]
    last = None
    for attempt in (1, 2):
        try:
            for name in (ALERTA_NAME, ALRUST_NAME):
                if name != cfg["container"] and container_state(name) != "missing":
                    docker(["rm", "-f", name], timeout=60)
            if not container_matches(config_id, db):
                # Stop the current server before waiting. The same container
                # name is reused across Alerta configs, so the port stays open
                # until that container is removed.
                if container_state(cfg["container"]) != "missing":
                    docker(["rm", "-f", cfg["container"]], timeout=60)
                wait_port_closed(cfg["port"])
                log(f"start {config_id} db={db}")
                _docker_run_app(config_id, db)
            info = inspect_container(cfg["container"])
            if not info or not info["State"]["Running"]:
                logs = docker(["logs", "--tail", "40", cfg["container"]], check=False)
                raise RuntimeError(redact((logs.stdout or "") + (logs.stderr or "") or f"{cfg['container']} exited"))
            assert_pins(info, APP_NANO, APP_CPUSET, cfg["container"])
            if expected_total is not None:
                wait_total(cfg["port"], expected_total)
            gunicorn_ready_gate(config_id)
            return
        except (TimeoutError, RuntimeError) as exc:
            last = exc
            logs = docker(["logs", "--tail", "40", cfg["container"]], check=False)
            detail = redact(((logs.stdout or "") + (logs.stderr or ""))[-1500:])
            log(f"server start attempt {attempt} failed: {redact(exc)} {detail}")
            stop_server(config_id)
            if attempt == 2:
                raise RuntimeError(redact(f"{last}; logs: {detail}")) from exc


def stop_server(config_id):
    name = configs()[config_id]["container"]
    if container_state(name) != "missing":
        docker(["rm", "-f", name], timeout=60)


def rotate(items, index):
    shift = index % len(items)
    return list(items[shift:]) + list(items[:shift])


class Sampler:
    """Short-lived samples of pg_stat_activity. The sampler backend is excluded."""

    def __init__(self, db):
        ident(db)
        self.db = db
        self.peak_total = 0
        self.peak_active = 0
        self.peak_client = 0
        self.samples = 0
        self.errors = 0
        self.started = None
        self.stopped = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.started = time.perf_counter()
        self._thread = threading.Thread(target=self._run, name="pg-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.stopped = time.perf_counter()
        return self.result()

    def result(self):
        end = self.stopped if self.stopped is not None else time.perf_counter()
        begin = self.started if self.started is not None else end
        window = end - begin
        return {
            "database": self.db,
            "peak_total_backends": self.peak_total,
            "peak_active_backends": self.peak_active,
            "peak_client_backends": self.peak_client,
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "sample_interval_actual_s": (window / self.samples) if self.samples else None,
            "sample_count": self.samples,
            "sample_errors": self.errors,
            "sample_window_s": window,
            "excluded": "each sample opens a new connection; pg_backend_pid() is excluded from both counts",
        }

    def _run(self):
        sql = """
            SELECT
              COUNT(*) FILTER (WHERE pid <> pg_backend_pid())::int,
              COUNT(*) FILTER (WHERE pid <> pg_backend_pid() AND state = 'active')::int,
              COUNT(*) FILTER (
                WHERE pid <> pg_backend_pid() AND backend_type = 'client backend'
              )::int
            FROM pg_stat_activity
            WHERE datname = current_database()
        """
        next_t = time.perf_counter()
        while not self._stop.is_set():
            try:
                with pg_connect(self.db) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET statement_timeout = '2s'")
                        cur.execute(sql)
                        total, active, client = cur.fetchone()
                total = int(total)
                active = int(active)
                client = int(client)
                if total > self.peak_total:
                    self.peak_total = total
                if active > self.peak_active:
                    self.peak_active = active
                if client > self.peak_client:
                    self.peak_client = client
                self.samples += 1
            except Exception:
                self.errors += 1
            next_t += SAMPLE_INTERVAL_S
            delay = next_t - time.perf_counter()
            if delay > 0:
                if self._stop.wait(delay):
                    return
            else:
                next_t = time.perf_counter()


def assert_sampler_excludes_self():
    with pg_connect("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)::int,
                       COUNT(*) FILTER (WHERE pid <> pg_backend_pid())::int
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
            all_n, excluded = cur.fetchone()
    if int(all_n) != int(excluded) + 1:
        raise RuntimeError(
            f"sampler self-check failed: all={all_n} excluded_self={excluded}"
        )
    log(f"sampler self-check ok all={all_n} excluding_self={excluded}")


def image_inspect(name):
    result = docker(["image", "inspect", name])
    info = json.loads(result.stdout)[0]
    return info


def verify_prerequisites():
    problems = []
    oha = run_cmd([OHA, "--version"], check=False)
    oha_version = (oha.stdout or oha.stderr).strip()
    if oha.returncode != 0 or oha_version != "oha 1.17.0":
        problems.append(f"oha version is {oha_version!r}, need 'oha 1.17.0'")
    nproc = run_cmd(["nproc", "--all"], check=False)
    nproc_n = (nproc.stdout or "").strip()
    if nproc_n != "8":
        problems.append(f"nproc --all is {nproc_n!r}, need 8")
    affinity = set(os.sched_getaffinity(0))
    if affinity != {6, 7}:
        problems.append(f"runner affinity is {sorted(affinity)}, need cores 6 and 7")
    docker_ver = docker(["version", "--format", "client {{.Client.Version}} server {{.Server.Version}}"], check=False)
    if docker_ver.returncode != 0:
        problems.append("docker is not usable: " + redact(docker_ver.stderr.strip() or docker_ver.stdout.strip()))
    images = {}
    for name in (PG_IMAGE, ALERTA_IMAGE, ALRUST_IMAGE, UWSGI_IMAGE):
        try:
            images[name] = image_inspect(name)
        except Exception as exc:
            problems.append(f"image {name} inspect failed: {exc}")
    pg = images.get(PG_IMAGE) or {}
    pg_id = pg.get("Id") or ""
    pg_digests = " ".join(pg.get("RepoDigests") or [])
    if PG_DIGEST not in pg_id and PG_DIGEST not in pg_digests:
        problems.append(f"postgres digest/id does not match {PG_DIGEST}; id={pg_id} digests={pg.get('RepoDigests')}")
    alerta = images.get(ALERTA_IMAGE) or {}
    if alerta.get("Id") != ALERTA_ID:
        problems.append(f"alerta image id is {alerta.get('Id')}, expected {ALERTA_ID}")
    expected_cmd = gunicorn_cmd(1)
    if (alerta.get("Config") or {}).get("Cmd") != expected_cmd:
        problems.append(f"alerta CMD is {(alerta.get('Config') or {}).get('Cmd')}, expected {expected_cmd}")
    alrust = images.get(ALRUST_IMAGE) or {}
    if alrust.get("Id") != ALRUST_ID:
        problems.append(f"alrust image id is {alrust.get('Id')}, expected {ALRUST_ID}")
    uwsgi = images.get(UWSGI_IMAGE) or {}
    parent = uwsgi.get("Parent")
    if parent != ALERTA_ID:
        problems.append(f"uwsgi image parent is {parent}, expected base {ALERTA_ID}")
    hashed = docker(
        ["run", "--rm", "--network", "host", "--entrypoint", "sha256sum", ALRUST_IMAGE, "/usr/local/bin/alrust"],
        check=False,
        timeout=60,
    )
    binary = (hashed.stdout or "").strip()
    if hashed.returncode != 0 or not binary.startswith(ALRUST_BINARY_SHA + " "):
        problems.append(f"alrust binary sha256 mismatch; got {redact(binary)} stderr {redact(hashed.stderr)}")
    uwsgi_ver = docker(
        ["run", "--rm", "--entrypoint", "uwsgi", UWSGI_IMAGE, "--version"],
        check=False,
        timeout=60,
    )
    got_uwsgi = (uwsgi_ver.stdout or uwsgi_ver.stderr or "").strip()
    if uwsgi_ver.returncode != 0 or got_uwsgi != UWSGI_VERSION:
        problems.append(f"uwsgi version is {got_uwsgi!r}, expected {UWSGI_VERSION}")
    meta = {
        "oha_version": oha_version,
        "nproc": nproc_n,
        "docker_version": (docker_ver.stdout or "").strip(),
        "images": {
            name: {
                "id": (info.get("Id")),
                "parent": info.get("Parent"),
                "repo_digests": info.get("RepoDigests"),
                "created": info.get("Created"),
                "cmd": (info.get("Config") or {}).get("Cmd"),
                "entrypoint": (info.get("Config") or {}).get("Entrypoint"),
            }
            for name, info in images.items()
        },
        "alrust_binary_sha256": binary.split()[0] if binary else None,
        "uwsgi_version": got_uwsgi,
        "alerta_commit": ALERTA_COMMIT,
        "alrust_commit": ALRUST_COMMIT,
        "postgres_digest_required": PG_DIGEST,
        "core_map": {
            "app": {"cpus": 4, "cpuset": APP_CPUSET, "nano_cpus": APP_NANO},
            "postgres": {"cpus": 2, "cpuset": PG_CPUSET, "nano_cpus": PG_NANO},
            "load_generator": {"taskset": LOAD_CPUS},
            "dockerd": "unpinned",
        },
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return problems, meta


def seed_alerts(port, count=1000):
    proc = subprocess.run(
        [sys.executable, str(SEED_PY), f"http://{HOST}:{port}", API_KEY, str(count)],
        text=True,
        capture_output=True,
        timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(redact((proc.stdout or "") + (proc.stderr or "") or f"seed exit {proc.returncode}"))
    log(redact((proc.stdout or "").strip() or f"seeded {count}"))


def append_loadavg(label):
    path = RAW / "loadavg.log"
    line = time.strftime("%Y-%m-%dT%H:%M:%S%z") + f" {label} " + Path("/proc/loadavg").read_text(encoding="utf-8").strip()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def any_timed_success():
    for folder in (RAW / "sweep", RAW / "test1", RAW / "test2", RAW / "test2-mixed", RAW / "mixed263", RAW / "pool-reads"):
        if not folder.exists():
            continue
        for path in folder.glob("*.json"):
            if path.name.startswith("size_") or path.name.startswith("api-"):
                continue
            if file_done(path):
                return True
    return False


def drop_db(name):
    if db_exists(name):
        terminate_db(name)
        pg_exec(f"DROP DATABASE IF EXISTS {ident(name)}")


def start_image_default(db, expected_total):
    """Image CMD: one gthread worker. Used only to create the schema and seed.

    Several workers at once race CREATE TYPE history on an empty database.
    """
    remove_apps()
    wait_port_closed(8081)
    log(f"bootstrap alerta image default db={db}")
    docker([
        "run", "-d", "--name", ALERTA_NAME, "--network", "host",
        "--cpus", "4", "--cpuset-cpus", APP_CPUSET,
        "--label", "tuned.config=alerta-bootstrap",
        "--label", f"tuned.db={db}",
        "-e", f"DATABASE_URL={database_url(db)}",
        ALERTA_IMAGE,
    ], timeout=60)
    info = inspect_container(ALERTA_NAME)
    if not info or not info["State"]["Running"]:
        logs = docker(["logs", "--tail", "50", ALERTA_NAME], check=False)
        raise RuntimeError(redact((logs.stdout or "") + (logs.stderr or "") or "bootstrap exited"))
    assert_pins(info, APP_NANO, APP_CPUSET, ALERTA_NAME)
    try:
        wait_total(8081, expected_total, headers={"Accept": "application/json"})
    except TimeoutError as exc:
        logs = docker(["logs", "--tail", "50", ALERTA_NAME], check=False)
        detail = redact(((logs.stdout or "") + (logs.stderr or ""))[-2000:])
        raise TimeoutError(f"bootstrap on {db} did not reach total {expected_total}; logs: {detail}") from exc


def ensure_templates():
    """base_empty is schema plus the admin key. base_seed is that plus 1,000 alerts."""
    start_postgres()
    ok = (
        db_exists("base_empty")
        and db_exists("base_seed")
        and alert_count("base_empty") == 0
        and alert_count("base_seed") == 1000
    )
    if ok:
        log("templates base_empty and base_seed already present")
        return
    if any_timed_success() and os.environ.get("RERUN_REBUILD") != "1":
        raise RuntimeError("timed results exist but base_empty/base_seed are not intact; refusing to reseed")
    if any_timed_success():
        log("RERUN_REBUILD=1: postgres was torn down; rebuilding base_empty/base_seed with the same seed.py for flagged-cell reruns")
    log("building base_empty and base_seed")
    remove_apps()
    for name in ("base_empty", "base_seed", "smoke_db"):
        if db_exists(name):
            drop_db(name)
    pg_exec("CREATE DATABASE base_empty")
    start_image_default("base_empty", 0)
    insert_key("base_empty")
    stop_server("alerta-gunicorn-4")
    terminate_db("base_empty")
    recreate_db("base_seed", "base_empty")
    start_image_default("base_seed", 0)
    seed_alerts(8081, 1000)
    wait_total(8081, 1000)
    if alert_count("base_seed") != 1000:
        raise RuntimeError(f"base_seed count is {alert_count('base_seed')}")
    stop_server("alerta-gunicorn-4")
    terminate_db("base_seed")
    terminate_db("base_empty")
    log(f"base_seed fingerprint {alert_fingerprint('base_seed')}")


def ensure_clones(purpose, config_ids, template, expected_count):
    """Fresh clones. Refuses to rebuild them once timed cells exist for that purpose."""
    names = [config_db(purpose, cid) for cid in config_ids]
    ready = all(db_exists(name) and alert_count(name) == expected_count for name in names)
    if ready:
        finger = alert_fingerprint(template)
        if all(alert_fingerprint(name) == finger for name in names):
            log(f"clones ready purpose={purpose} count={expected_count}")
            return names
    timed_root = {"sweep": RAW / "sweep", "t1": RAW / "test1"}.get(purpose)
    if timed_root and timed_root.exists() and any(file_done(p) for p in timed_root.glob("*.json")) and os.environ.get("RERUN_REBUILD") != "1":
        raise RuntimeError(f"{purpose} timed results exist but clones are not an intact copy of {template}")
    remove_apps()
    terminate_db(template)
    finger = alert_fingerprint(template)
    for name in names:
        log(f"clone {name} from {template}")
        recreate_db(name, template)
        if alert_count(name) != expected_count or alert_fingerprint(name) != finger:
            raise RuntimeError(f"clone {name} does not match {template}")
    return names


def probe_auth(config_id, db, expected_total):
    ensure_server(config_id, db, expected_total)
    cfg = configs()[config_id]
    port = cfg["port"]
    rows = []
    for kind, headers in (
        ("no-authorization", {"Accept": "application/json"}),
        ("invalid-key", {"Accept": "application/json", "Authorization": "Key invalid"}),
        ("valid-key", auth_headers()),
    ):
        status, doc, server = http_json(port, "/alerts/count", headers=headers, timeout=20)
        rows.append({
            "kind": kind,
            "status": status,
            "total": doc.get("total"),
            "server_header": server,
        })
    pins = inside_pin_text(cfg["container"])
    info = inspect_container(cfg["container"])
    stop_server(config_id)
    return {
        "config": config_id,
        "argv": argv_for(config_id),
        "image": cfg["image"],
        "port": port,
        "auth": rows,
        "inside": pins,
        "nano_cpus": info["HostConfig"]["NanoCpus"],
        "cpuset_cpus": info["HostConfig"]["CpusetCpus"],
    }


def smoke_and_auth():
    """Prove each server kind answers, and record the uWSGI argv that works."""
    path = RAW / "auth-probe.json"
    if file_done(path) and file_done(UWSGI_CMD_PATH):
        log("auth probe already recorded")
        return json.loads(path.read_text(encoding="utf-8"))
    remove_apps()
    if db_exists("smoke_db"):
        drop_db("smoke_db")
    recreate_db("smoke_db", "base_empty")
    probes = []
    probes.append(probe_auth("alerta-gunicorn-4", "smoke_db", 0))
    save_uwsgi_cmd(
        default_uwsgi_cmd(),
        "--http router because --http-socket with --http-keepalive closed connections under "
        "oha HTTP/1.1 keep-alive (5s probes at concurrency 1 and 32 had connection errors; "
        "the same probes with --http had zero errors); --http-keepalive is still set; "
        "need-app so a missing WSGI app exits; --py-call-uwsgi-fork-hooks because sentry_sdk "
        "warns that prefork without it can crash workers; --enable-threads is the stub uWSGI "
        "already defaults on and is not --threads",
    )
    try:
        probes.append(probe_auth("alerta-uwsgi-5", "smoke_db", 0))
    except Exception as first:
        log(f"uwsgi default command failed: {redact(first)}; retrying with --lazy-apps")
        cmd = default_uwsgi_cmd()
        cmd.insert(1, "--lazy-apps")
        save_uwsgi_cmd(cmd, "added --lazy-apps after the first smoke failed to serve /alerts/count")
        remove_apps()
        probes.append(probe_auth("alerta-uwsgi-5", "smoke_db", 0))
    probes.append(probe_auth("alrust", "smoke_db", 0))
    remove_apps()
    drop_db("smoke_db")
    pg_pins = inside_pin_text(PG_NAME)
    pg_info = inspect_container(PG_NAME)
    doc = {
        "probes": probes,
        "postgres_inside": pg_pins,
        "postgres_nano_cpus": pg_info["HostConfig"]["NanoCpus"],
        "postgres_cpuset_cpus": pg_info["HostConfig"]["CpusetCpus"],
        "uwsgi_cmd": uwsgi_cmd(),
    }
    write_json(path, doc)
    log("auth probe recorded")
    return doc


def show_setting(name):
    return str(pg_scalar(f"SHOW {ident_setting(name)}"))


def ident_setting(name):
    if not re.fullmatch(r"[a-z_]+", name):
        raise RuntimeError(f"bad setting {name}")
    return name


def capture_environment(meta, auth_doc):
    lines = []
    lines.append("date: " + run_cmd(["date"]).stdout.strip())
    lines.append("uname: " + run_cmd(["uname", "-a"]).stdout.strip())
    lines.append("nproc --all: " + run_cmd(["nproc", "--all"]).stdout.strip())
    lines.append("nproc (this process, after taskset): " + run_cmd(["nproc"]).stdout.strip())
    cpu = ""
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lower().startswith("model name"):
            cpu = line
            break
    lines.append("cpu: " + (cpu or "model name not found"))
    lscpu = run_cmd(["lscpu"], check=False)
    for line in (lscpu.stdout or "").splitlines():
        if line.startswith(("CPU(s):", "Thread(s) per core:", "Core(s) per socket:", "Socket(s):", "NUMA", "On-line")):
            lines.append("lscpu: " + line.strip())
    lines.append("free:")
    lines.append(run_cmd(["free", "-h"]).stdout.rstrip())
    lines.append("loadavg: " + Path("/proc/loadavg").read_text(encoding="utf-8").strip())
    lines.append("runner affinity: " + ",".join(str(c) for c in sorted(os.sched_getaffinity(0))))
    lines.append("")
    lines.append("docker " + meta.get("docker_version", ""))
    lines.append("pg version(): " + str(pg_scalar("SELECT version()")))
    lines.append("pg image digest required: " + PG_DIGEST)
    lines.append("postgres settings (image defaults, no custom postgresql.conf):")
    for name in [
        "server_version", "shared_buffers", "max_connections", "work_mem",
        "maintenance_work_mem", "effective_cache_size", "synchronous_commit",
        "fsync", "full_page_writes", "listen_addresses", "max_wal_size",
        "checkpoint_timeout", "random_page_cost", "effective_io_concurrency",
    ]:
        lines.append(f"  {name} = {show_setting(name)}")
    lines.append("")
    for image, info in meta["images"].items():
        lines.append(
            f"image {image} id={info.get('id')} parent={info.get('parent')} "
            f"digests={info.get('repo_digests')} created={info.get('created')}"
        )
        lines.append(f"  entrypoint={info.get('entrypoint')} cmd={info.get('cmd')}")
    lines.append("")
    lines.append(f"alerta commit: {ALERTA_COMMIT}")
    lines.append(f"alrust commit: {ALRUST_COMMIT}")
    lines.append(f"alrust binary sha256: {meta.get('alrust_binary_sha256')}  /usr/local/bin/alrust")
    lines.append(f"uwsgi version: {meta.get('uwsgi_version')}")
    lines.append(f"oha: {meta.get('oha_version')} path {OHA}")
    lines.append("load tool flags: taskset -c 6-7 oha --no-tui --no-color --http-version 1.1 -z <5s|30s> -c <n> -w -t 60s --worker-threads 2 -H 'Authorization: Key <redacted>' -H 'Accept: application/json' --output-format json")
    lines.append("keepalive: HTTP/1.1 keep-alive; --disable-keepalive was not set")
    lines.append("")
    lines.append("core map (verified):")
    lines.append("  application server: --cpus=4 --cpuset-cpus=0-3")
    lines.append("  postgres: --cpus=2 --cpuset-cpus=4-5")
    lines.append("  load generator and sampler thread: taskset -c 6-7")
    lines.append("  dockerd and other host daemons: unpinned")
    if auth_doc:
        lines.append(f"  postgres inside: {auth_doc.get('postgres_inside')} nano={auth_doc.get('postgres_nano_cpus')} cpuset={auth_doc.get('postgres_cpuset_cpus')}")
        for probe in auth_doc.get("probes") or []:
            lines.append(
                f"  {probe['config']} inside={probe['inside']} nano={probe['nano_cpus']} "
                f"cpuset={probe['cpuset_cpus']} image={probe['image']}"
            )
            lines.append("    argv: " + redact_argv(probe["argv"]))
            for row in probe["auth"]:
                lines.append(
                    f"    auth {row['kind']}: status={row['status']} total={row['total']} server={row['server_header']}"
                )
    lines.append("")
    lines.append("AUTH_REQUIRED:")
    lines.append("  Alerta image settings.py default AUTH_REQUIRED = False. The key is still sent on every timed request.")
    lines.append("  alrust ALRUST_AUTH_REQUIRED was not set. The binary default is true.")
    lines.append("  ALRUST_HOUSEKEEPING_INTERVAL=0. alrust/alrust-p16 do not set ALRUST_DB_MAX_CONNECTIONS (binary default 16); alrust-p32/alrust-p64 set it to 32/64. ALRUST_KEY_USAGE_IMMEDIATE not set (default false: key use written about once a second).")
    lines.append("")
    lines.append("command lines (secrets redacted):")
    lines.append(
        "docker run -d --name r2-pg --network host --cpus=2 --cpuset-cpus=4-5 "
        "-e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=<redacted> -e POSTGRES_DB=postgres postgres:18.7"
    )
    lines.append(
        "docker run -d --name r2-alerta --network host --cpus=4 --cpuset-cpus=0-3 "
        "-e DATABASE_URL=postgresql://postgres:<redacted>@127.0.0.1:5432/<db> "
        "alerta-bench:9.1.0 " + redact_argv(gunicorn_cmd("N"))
    )
    lines.append(
        "docker run -d --name r2-alerta --network host --cpus=4 --cpuset-cpus=0-3 "
        "-e DATABASE_URL=postgresql://postgres:<redacted>@127.0.0.1:5432/<db> "
        "alerta-bench-uwsgi:9.1.0 " + redact_argv(uwsgi_cmd())
    )
    lines.append(
        "docker run -d --name r2-alrust --network host --cpus=4 --cpuset-cpus=0-3 "
        "-e DATABASE_URL=postgresql://postgres:<redacted>@127.0.0.1:5432/<db> "
        "-e ALRUST_BIND=127.0.0.1:8082 -e ALRUST_ADMIN_KEY=<redacted> "
        "-e ALRUST_HOUSEKEEPING_INTERVAL=0 [-e ALRUST_DB_MAX_CONNECTIONS=32|64] alrust:main-d12dc77"
    )
    lines.append("alrust entrypoint: /usr/local/bin/alrust with no extra arguments.")
    lines.append("")
    lines.append("peak connection method:")
    lines.append(
        "  During each timed run a thread on the load-generator cores opens a short-lived "
        "psycopg connection to that run's database every 0.2s. The count query excludes "
        "pg_backend_pid(). peak_total_backends is the max count of other backends whose "
        "datname is that database. peak_active_backends is the max of those with state active. "
        "sample_count and the actual mean interval are stored on the cell. The sampler connection "
        "is not included in the peak."
    )
    lines.append("")
    lines.append("natural connection model (not a cap):")
    lines.append("  alrust serving pool defaults to 16 connections (ALRUST_DB_MAX_CONNECTIONS) and is closed only on shutdown. A startup pool of 2 is closed before serving.")
    lines.append("  Alerta's Postgres backend opens one connection per request and closes it on teardown. It was not given a pool cap.")
    lines.append("  uWSGI runs 5 worker processes. Gunicorn runs N gthread workers with 4 threads each.")
    text = "\n".join(lines) + "\n"
    assert_no_secrets(text, "environment.txt")
    atomic_write(ROOT / "environment.txt", text)
    log(f"wrote environment.txt ({len(text)} bytes)")


def error_count_of(doc):
    errors = doc.get("errorDistribution")
    if not errors:
        return 0, {}
    if isinstance(errors, dict):
        total = 0
        clean = {}
        for key, value in errors.items():
            if isinstance(value, bool):
                count = int(value)
            elif isinstance(value, (int, float)):
                count = int(value)
            else:
                count = 1
            total += count
            clean[str(key)] = count
        return total, clean
    return 1, {"_raw": str(errors)[:500]}


def parse_oha(doc):
    if "summary" not in doc and "oha" in doc:
        doc = doc["oha"]
    statuses = {}
    for key, value in (doc.get("statusCodeDistribution") or {}).items():
        statuses[str(key)] = int(value)
    error_count, errors = error_count_of(doc)
    status_2xx = sum(count for code, count in statuses.items() if 200 <= int(code) <= 299)
    non_2xx = sum(count for code, count in statuses.items() if int(code) < 200 or int(code) > 299)
    pct = doc["latencyPercentiles"]
    summary = doc["summary"]
    return {
        "rps": summary["requestsPerSec"],
        "p50_ms": pct["p50"] * 1000.0,
        "p99_ms": pct["p99"] * 1000.0,
        "requests": sum(statuses.values()),
        "duration_s": summary["total"],
        "status_2xx": status_2xx,
        "non_2xx": non_2xx,
        "error_count": error_count,
        "success_rate": summary.get("successRate"),
        "status_codes": statuses,
        "errors": errors,
    }


def run_oha(url, concurrency, duration, dest, overwrite):
    dest = Path(dest)
    if file_done(dest) and not overwrite:
        raise RuntimeError(f"refusing to overwrite {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    if tmp.exists():
        tmp.unlink()
    cmd = [
        "taskset", "-c", LOAD_CPUS,
        OHA, "--no-tui", "--no-color",
        "--http-version", "1.1",
        "-z", duration,
        "-c", str(concurrency),
        "-w",
        "-t", "60s",
        "--worker-threads", "2",
        "-H", "Authorization: Key " + API_KEY,
        "-H", "Accept: application/json",
        "--output-format", "json",
        "-o", str(tmp),
        url,
    ]
    if duration.endswith("s") and duration[:-1].isdigit() and int(duration[:-1]) <= 5:
        timeout = 90
    elif duration.endswith("s") and duration[:-1].isdigit():
        timeout = int(duration[:-1]) + 120
    else:
        timeout = 180
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)
        proc.communicate()
        raise RuntimeError(f"oha timed out after {timeout}s for {dest.name}")
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        err_path = dest.with_suffix(".err.txt")
        err_path.write_text(redact((stdout or "") + "\n" + (stderr or "")), encoding="utf-8")
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"oha failed rc={proc.returncode} see {err_path.name}")
    text = tmp.read_text(encoding="utf-8")
    text = redact(text)
    assert_no_secrets(text, str(tmp))
    tmp.write_text(text, encoding="utf-8")
    doc = json.loads(text)
    if "requestsPerSec" not in doc.get("summary", {}):
        tmp.unlink()
        raise RuntimeError("oha json missing summary.requestsPerSec")
    if "p50" not in doc.get("latencyPercentiles", {}) or "p99" not in doc.get("latencyPercentiles", {}):
        tmp.unlink()
        raise RuntimeError("oha json missing latencyPercentiles p50/p99")
    os.replace(tmp, dest)
    return doc


def median3(values):
    import statistics
    if len(values) != 3 or any(value is None for value in values):
        return None
    return statistics.median(values)


def median_any(values):
    import statistics
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return statistics.median(clean)


def ids_of(doc):
    return [item.get("id") for item in (doc.get("alerts") or [])]


def fetch_case(config_id, path):
    port = configs()[config_id]["port"]
    status, doc, server = http_json(port, path, timeout=60)
    if path.endswith("/count") or path == "/alerts/count":
        return {
            "http": status,
            "total": doc.get("total"),
            "severityCounts": doc.get("severityCounts"),
            "statusCounts": doc.get("statusCounts"),
            "server": server,
        }
    return {
        "http": status,
        "total": doc.get("total"),
        "status": doc.get("status"),
        "n": len(doc.get("alerts") or []),
        "ids": ids_of(doc),
        "server": server,
    }


def cases_equal(name, left, right):
    if name == "count":
        a = {k: left[k] for k in ("http", "total", "severityCounts", "statusCounts")}
        b = {k: right[k] for k in ("http", "total", "severityCounts", "statusCounts")}
        return a == b
    return (
        left["http"] == 200
        and right["http"] == 200
        and left["total"] == right["total"]
        and left["ids"] == right["ids"]
    )
