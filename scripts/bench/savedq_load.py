#!/usr/bin/env python3
"""What alerta_fast.py's `savedq` patch changes for whole HTTP requests:
stock Alerta 9.1.0 under gunicorn with the add-on's default patches
(`safe`), and the same with `savedq` (`safe,savedq`), on one hosted runner.
Run by .github/workflows/addon-savedq-load.yml after scripts/bench/setup.sh:

    python3 -I scripts/bench/savedq_load.py <output directory>

Relative numbers of one runner with the client on the same machine: not a
capacity claim. Synthetic data only.

Servers. Two gunicorn servers (N = vCPUs workers x 4 threads, direct
PostgreSQL connections, AUTH_REQUIRED) run for the whole of the run on one
database, one for each arm; only one gets requests at a time. A third,
Alerta with no add-on, gives the expected answers before the timed cells
and is then stopped. `savedq` keeps its store for each worker, so a cell
finds what earlier cells left: as a server in use does.

Data. 300 alerts made through Alerta, then given to two customers and to
none (150 / 100 / 50) in SQL. Three keys: customer A, customer B, admin.
Nothing writes an alert after that. Each request's key is the next of the
three, so the same search text comes from each of them in turn.

Shapes of the search (GET /alerts, 50 a page, Accept-Encoding: identity):
  one       the same `q` each time;
  sixteen   sixteen different `q`, in turn;
  unique    a `q` that no request had before (each one is a miss, and
            fills the store: the cost side of the patch);
  none      no `q` (control: the patch has no part in it).

Loads, each with eight client threads and one kept connection a thread:
  offered   requests are due at a fixed rate (RATE a second, the same for
            both arms). A thread takes the next due request, waits for its
            time and sends it. The time of a request is counted from when
            it was due, so waiting for a free thread is in it. `late` is
            how many started more than 100 ms after they were due;
  closed    each thread sends its next request as soon as it has an
            answer: the requests a second that eight clients get.

Order. A repetition runs every shape and load on both arms. The arm order
of a pair is A B, B A, B A, A B over four repetitions (full mode), and the
other way round for every second shape, so each arm is first as often as
second in each shape. Each cell has a warm-up that is not counted.

Gates. Every answer of every cell, warm-up and timed, must be HTTP 200
with exactly the total, the status and severity counts and the set of ids
that Alerta without the add-on gave for that key and that search. One
wrong answer makes the run not ok, and the cell is not scored. The
`unique` texts differ only in a term that is true for every alert, so the
expected answer is the one of their common part; after the timed cells
the first, the last and 20 more of the texts that were sent are asked of
Alerta without the add-on again, and must give that answer. The three
keys' answers to one search must differ (else a mixed-up answer could not
show).

Numbers for a cell: requests, requests a second, p50 / p95 / p99 / max of
the time (ms), and the CPU time of the server's process tree for each
request (ms; gunicorn master and workers, from /proc). The summary gives
the median over the repetitions and each repetition's value. With a few
hundred requests in a cell, p99 rests on few requests.

Not measured here: PostgreSQL's CPU, a network between client and server,
writes, and the numbers of the store (it is in each worker's memory; no
route shows it).

BENCH_MODE: "quick" (plumbing: one repetition, short cells; the default)
or "full" (DECISIONS Y69: by dispatch only).

No key is written to the output. The server logs are copied only if they
have none of the keys.
"""

import hashlib
import http.client
import json
import os
import secrets
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODE = os.environ.get("BENCH_MODE", "quick")
QUICK = MODE != "full"
VCPUS = os.cpu_count() or 4
THREADS = 4
CLIENTS = 8
RATE = 40          # offered requests a second
LATE_S = 0.1
REPS = 1 if QUICK else 4
WARM_S = 1 if QUICK else 3
TIMED_S = 3 if QUICK else 10
ARMS = {"safe": "safe", "safe+savedq": "safe,savedq"}
PORTS = {"safe": 8091, "safe+savedq": 8092, "stock": 8090}
SHAPES = ("one", "sixteen", "unique", "none")
LOADS = ("offered", "closed")
DB = "savedq_load"
OWNERS = (("a", 150), ("b", 100), (None, 50))
SEVERITIES = ("critical", "major", "minor", "warning")
SERVICES = ("web", "db", "mail")
GROUPS = ("net", "os", "app", "disk")

ONE = "severity:(major OR critical) AND service:web AND NOT group:disk"
SIXTEEN = [f"severity:{severity} AND group:{group}" for severity in SEVERITIES for group in GROUPS]
UNIQUE_PART = "severity:(major OR minor) AND NOT service:mail"


def unique_text(n, run):
    # No alert has this resource, so the second term is true for each one.
    return f"{UNIQUE_PART} AND NOT resource:zz{run}n{n}"


def percentile(sorted_values, q):
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def arm_order(rep, shape_index):
    first_a = (rep % 4 in (0, 3)) != (shape_index % 2 == 1)
    names = list(ARMS)
    return names if first_a else names[::-1]


def digest(status, body):
    """What is compared of an answer, as one short value."""
    if status != 200:
        return f"http {status}"
    try:
        data = json.loads(body)
        seen = [data["status"], data["total"], sorted(data["severityCounts"].items()),
                sorted(data["statusCounts"].items()), sorted(alert["id"] for alert in data["alerts"])]
    except (ValueError, KeyError, TypeError, AttributeError):
        return "not an alert list"
    return hashlib.sha256(json.dumps(seen).encode()).hexdigest()[:24]


def path_of(q):
    return "/alerts" if q is None else "/alerts?" + urllib.parse.urlencode({"q": q})


class Client:
    """One thread's kept connection."""

    def __init__(self, port):
        self.port, self.connection = port, None

    def get(self, path, key):
        for attempt in (0, 1):
            fresh = self.connection is None
            if fresh:
                self.connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            try:
                self.connection.request("GET", path, headers={"Authorization": f"Key {key}",
                                                                "Accept-Encoding": "identity"})
                response = self.connection.getresponse()
                return response.status, response.read()
            except (OSError, http.client.HTTPException) as exc:
                self.connection.close()
                self.connection = None
                # A kept connection that the server closed: once more, on a
                # new one. A GET changes nothing.
                if fresh or attempt:
                    return None, type(exc).__name__.encode()

    def close(self):
        if self.connection is not None:
            self.connection.close()


def run_load(port, load, seconds, request_of):
    """Sends requests for `seconds`. request_of(n) -> (path, key, wanted).
    Returns the samples [(time in s, right?)], the wrong answers' kinds,
    the late starts and the wall time."""
    samples, wrong, late = [], {}, [0]
    lock, counter = threading.Lock(), [0]
    start = time.monotonic() + 0.05
    stop = start + seconds

    def work():
        client = Client(port)
        try:
            while True:
                with lock:
                    n = counter[0]
                    due = start + n / RATE if load == "offered" else time.monotonic()
                    if due >= stop:
                        return
                    counter[0] += 1
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                began = time.monotonic()
                path, key, wanted = request_of(n)
                got = digest(*client.get(path, key))
                took = time.monotonic() - (due if load == "offered" else began)
                with lock:
                    samples.append(took)
                    if load == "offered" and began - due > LATE_S:
                        late[0] += 1
                    if got != wanted:
                        kind = got if got.startswith(("http", "not")) else "other answer"
                        wrong[kind] = wrong.get(kind, 0) + 1
        finally:
            client.close()

    threads = [threading.Thread(target=work) for _ in range(CLIENTS)]
    began = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return samples, wrong, late[0], time.monotonic() - began


def _stat(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = raw[raw.rindex(")") + 2:].split()
    return int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[13]) + int(fields[14])


def tree_cpu(root_pid):
    """CPU seconds of a process tree: live members, plus the root's reaped children."""
    stats = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            stat = _stat(int(name))
            if stat:
                stats[int(name)] = stat
    if root_pid not in stats:
        return 0.0
    members, frontier = {root_pid}, [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, _, _) in stats.items():
            if ppid == parent and pid not in members:
                members.add(pid)
                frontier.append(pid)
    return (sum(stats[p][1] for p in members) + stats[root_pid][2]) / os.sysconf("SC_CLK_TCK")


class Lab:
    def __init__(self, out):
        self.out = out
        self.work = Path(os.environ["BENCH_WORK"])
        self.pg_bin = Path(os.environ["BENCH_PG_BIN"])
        self.venv = Path(os.environ["BENCH_ALERTA_VENV"])
        self.run = secrets.token_hex(4)
        self.keys = {who: secrets.token_hex(20) for who in ("a", "b", "admin")}
        self.servers, self.logs = {}, {}
        self.unique_sent = 0

    def psql(self, sql, db=DB):
        # The throwaway cluster of setup.sh: its password is public text.
        return subprocess.run(
            [str(self.pg_bin / "psql"), "-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", db,
             "-v", "ON_ERROR_STOP=1", "-AXqt", "-c", sql],
            env={"PATH": "/usr/bin:/bin", "PGPASSWORD": "postgres"}, check=True, capture_output=True,
            text=True, timeout=60).stdout.strip()

    def start(self, name, patches):
        env = {"PATH": f"{self.venv}/bin:/usr/bin:/bin", "HOME": str(self.work),
               "DATABASE_URL": f"postgresql://postgres:postgres@127.0.0.1:5432/{DB}",
               "AUTH_REQUIRED": "True", "SECRET_KEY": "bench-not-secret", "LOG_LEVEL": "WARNING"}
        target = ["alerta.app:create_app()"]
        if patches:
            env["ALERTA_FAST"] = patches
            target = ["--pythonpath", str(HERE), "alerta_fast:app"]
        self.logs[name] = self.work / f"savedq-load-{name.replace('+', '-')}.log"
        with open(self.logs[name], "wb") as log:
            # --preload: Alerta's schema statements run once, before the fork.
            self.servers[name] = subprocess.Popen(
                [str(self.venv / "bin/gunicorn"), "--workers", str(VCPUS), "--worker-class", "gthread",
                 "--threads", str(THREADS), "--preload", "--bind", f"127.0.0.1:{PORTS[name]}", *target],
                env=env, cwd=str(self.work), stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.servers[name].poll() is not None:
                raise RuntimeError(f"the {name} server stopped at its start")
            client = Client(PORTS[name])
            status, _ = client.get("/management/gtg", "none")
            client.close()
            if status == 200:
                return
            time.sleep(0.25)
        raise RuntimeError(f"the {name} server was not ready in 90 s")

    def stop(self, name):
        process = self.servers.pop(name, None)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def seed(self):
        self.psql(f"CREATE DATABASE {DB}", db="postgres")
        self.start("stock", "")
        # The admin key first: Alerta made the tables when it started.
        for who, key in self.keys.items():
            customer = "NULL" if who == "admin" else f"'cust-{who}-{self.run}'"
            scopes = "'{admin,read,write}'" if who == "admin" else "'{read}'"
            self.psql('INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, customer) VALUES '
                      f"('{secrets.token_hex(16)}', '{key}', 'savedq-load-{who}', {scopes}, 'savedq load', "
                      f"timezone('UTC', now()) + interval '1 day', 0, {customer})")
        connection = http.client.HTTPConnection("127.0.0.1", PORTS["stock"], timeout=30)
        made = {}
        n = 0
        for owner, count in OWNERS:
            for _ in range(count):
                body = json.dumps({"resource": f"host{self.run}n{n:04d}", "event": "load",
                                   "environment": "Production", "severity": SEVERITIES[n % 4],
                                   "service": [SERVICES[n % 3]], "group": GROUPS[(n // 4) % 4],
                                   "text": f"savedq load alert {n}"})
                connection.request("POST", "/alert", body=body, headers={
                    "Authorization": f"Key {self.keys['admin']}", "Content-Type": "application/json"})
                response = connection.getresponse()
                answer = response.read()
                if response.status != 201:
                    raise RuntimeError(f"alert {n} was not made: HTTP {response.status}")
                made.setdefault(owner, []).append(json.loads(answer)["id"])
                n += 1
        connection.close()
        for owner, ids in made.items():
            if owner:
                quoted = ",".join(f"'{i}'" for i in ids)
                self.psql(f"UPDATE alerts SET customer='cust-{owner}-{self.run}' WHERE id IN ({quoted})")
        if int(self.psql("SELECT count(*) FROM alerts")) != sum(count for _, count in OWNERS):
            raise RuntimeError("the alerts table does not have the seeded number of rows")
        self.psql("ANALYZE alerts")

    def expected(self, texts):
        """{(who, q): digest} from Alerta without the add-on."""
        client, answers = Client(PORTS["stock"]), {}
        try:
            for q in texts:
                for who, key in self.keys.items():
                    answers[who, q] = digest(*client.get(path_of(q), key))
        finally:
            client.close()
        return answers

    def request_maker(self, shape, wanted):
        whos = list(self.keys)

        def request_of(n):
            who = whos[n % 3]
            if shape == "one":
                q = base = ONE
            elif shape == "sixteen":
                # n // 3: each text goes to each of the three keys.
                q = base = SIXTEEN[(n // 3) % 16]
            elif shape == "unique":
                with self.unique_lock:
                    number = self.unique_sent
                    self.unique_sent += 1
                q, base = unique_text(number, self.run), UNIQUE_PART
            else:
                q = base = None
            return path_of(q), self.keys[who], wanted[who, base]
        return request_of

    unique_lock = threading.Lock()

    def cell(self, arm, shape, load, rep, wanted):
        port, pid = PORTS[arm], self.servers[arm].pid
        request_of = self.request_maker(shape, wanted)
        _, warm_wrong, _, _ = run_load(port, load, WARM_S, request_of)
        cpu0 = tree_cpu(pid)
        samples, wrong, late, wall = run_load(port, load, TIMED_S, request_of)
        cpu = tree_cpu(pid) - cpu0
        for kind, count in warm_wrong.items():
            wrong[f"warm-up: {kind}"] = count
        samples.sort()
        row = {"arm": arm, "shape": shape, "load": load, "rep": rep, "requests": len(samples),
               "wrong": wrong, "valid": not wrong and bool(samples) and self.servers[arm].poll() is None,
               "late": late, "wall_s": round(wall, 3)}
        if row["valid"]:
            row.update(rps=round(len(samples) / wall, 1),
                       **{name: round(1000 * percentile(samples, q), 2)
                          for name, q in (("p50_ms", 0.5), ("p95_ms", 0.95), ("p99_ms", 0.99))},
                       max_ms=round(1000 * samples[-1], 2),
                       cpu_ms_per_request=round(1000 * cpu / len(samples), 3))
        return row


def summarize(record):
    cells = {}
    for row in record["cells"]:
        if row["valid"]:
            cells.setdefault((row["shape"], row["load"], row["arm"]), []).append(row)
    table = []
    for shape in SHAPES:
        for load in LOADS:
            entry = {"shape": shape, "load": load}
            for arm in ARMS:
                rows = cells.get((shape, load, arm), [])
                if len(rows) != record["reps"]:
                    entry[arm] = None
                    continue
                entry[arm] = {name: {"median": round(statistics.median(r[name] for r in rows), 3),
                                     "each": [r[name] for r in rows]}
                              for name in ("rps", "p50_ms", "p95_ms", "p99_ms", "cpu_ms_per_request")}
                entry[arm]["late"] = sum(r["late"] for r in rows)
                entry[arm]["requests"] = sum(r["requests"] for r in rows)
            table.append(entry)
    return table


def summary_text(record):
    lines = ["## `savedq` under gunicorn: whole requests", "",
             f"Mode **{record['mode']}**; {'**OK**' if record['ok'] else '**NOT OK**'}. "
             f"{record['workers']} workers x {THREADS} threads, {CLIENTS} client threads, "
             f"{record['reps']} repetition(s) of {TIMED_S} s after {WARM_S} s warm-up; the offered rate is "
             f"{RATE} a second. Relative to this runner, with the client on the same machine: "
             "not a capacity claim.", ""]
    if record.get("error"):
        lines += [f"**The run stopped: {json.dumps(record['error'])}**", ""]
    for problem in record.get("problems", []):
        lines.append(f"- problem: {problem}")
    lines += ["", "Medians over the repetitions (each repetition is in results.json).", "",
              "| search | load | arm | requests | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/request | late |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for entry in record.get("table", []):
        for arm in ARMS:
            got = entry[arm]
            if got is None:
                lines.append(f"| {entry['shape']} | {entry['load']} | {arm} | not scored | | | | | | |")
                continue
            lines.append(f"| {entry['shape']} | {entry['load']} | {arm} | {got['requests']} | "
                         + " | ".join(str(got[name]["median"]) for name in
                                      ("rps", "p50_ms", "p95_ms", "p99_ms", "cpu_ms_per_request"))
                         + f" | {got['late']} |")
    lines += ["", "`unique` is the cost side: each search text is new, so `savedq` parses it and "
              "keeps it for nothing. `none` is the control: no search text.", ""]
    return "\n".join(lines)


def main(out):
    out.mkdir(parents=True)
    record = {"schema": 1, "mode": "quick" if QUICK else "full", "ok": False, "workers": VCPUS,
              "threads": THREADS, "clients": CLIENTS, "rate": RATE, "reps": REPS, "warm_s": WARM_S,
              "timed_s": TIMED_S, "arms": ARMS, "cells": [], "problems": [],
              "searches": {"one": ONE, "sixteen": SIXTEEN, "unique": UNIQUE_PART + " AND NOT resource:zz<run>n<number>"},
              "addon_sha256": hashlib.sha256((HERE / "alerta_fast.py").read_bytes()).hexdigest()}
    lab = Lab(out)
    try:
        lab.seed()
        texts = [ONE, *SIXTEEN, UNIQUE_PART, None]
        wanted = lab.expected(texts)
        bad = sorted({str(q) for (who, q), value in wanted.items() if value.startswith(("http", "not"))})
        if bad:
            raise RuntimeError(f"Alerta without the add-on did not answer {len(bad)} of the searches")
        same = [str(q) for q in texts if len({wanted[who, q] for who in lab.keys}) != 3]
        if same:
            record["problems"].append(f"the three keys do not get three different answers for {same}")
        lab.stop("stock")
        for arm, patches in ARMS.items():
            lab.start(arm, patches)
        for rep in range(REPS):
            for shape_index, shape in enumerate(SHAPES):
                for load in LOADS:
                    for arm in arm_order(rep, shape_index):
                        row = lab.cell(arm, shape, load, rep, wanted)
                        record["cells"].append(row)
                        print(json.dumps(row), flush=True)
        for arm in ARMS:
            if lab.servers[arm].poll() is not None:
                record["problems"].append(f"the {arm} server stopped during the run")
            lab.stop(arm)
        # The unique texts that were sent, on Alerta without the add-on.
        lab.start("stock", "")
        last = lab.unique_sent - 1
        numbers = sorted({0, last, *range(0, last + 1, max(1, last // 20))})
        again = lab.expected([unique_text(n, lab.run) for n in numbers])
        differ = sum(1 for (who, q), value in again.items() if value != wanted[who, UNIQUE_PART])
        record["unique"] = {"sent": lab.unique_sent, "asked_again": len(numbers), "answers_that_differ": differ}
        if differ:
            record["problems"].append(f"{differ} unique texts do not have the answer of their common part")
        invalid = [row for row in record["cells"] if not row["valid"]]
        if invalid:
            record["problems"].append(f"{len(invalid)} of {len(record['cells'])} cells are not valid")
        record["table"] = summarize(record)
        record["ok"] = not record["problems"]
    except Exception as exc:
        # The message can have a key (a command line of psql): type only.
        record["error"] = {"type": type(exc).__name__, "line": exc.__traceback__.tb_lineno}
    finally:
        for name in list(lab.servers):
            lab.stop(name)
        keys = [key.encode() for key in lab.keys.values()]
        text = json.dumps(record, indent=1) + "\n"
        summary = summary_text(record)
        record_ok = not any(key.decode() in text + summary for key in keys)
        logs = []
        for name, path in sorted(lab.logs.items()):
            data = path.read_bytes() if path.exists() else b""
            if not any(key in data for key in keys):
                (out / path.name).write_bytes(data)
                logs.append(path.name)
        if record_ok:
            (out / "results.json").write_text(text)
            (out / "summary.md").write_text(summary)
            print(summary)
        else:
            (out / "summary.md").write_text("**Not published: the result had a key of the run.**\n")
        print(f"logs copied: {logs}", flush=True)
    return 0 if record["ok"] and record_ok else 1


def self_test():
    assert percentile([], 0.5) is None and percentile([1, 2, 3, 4], 0.5) == 3 and percentile([5], 0.99) == 5
    firsts = {shape: [arm_order(rep, index)[0] for rep in range(4)] for index, shape in enumerate(SHAPES)}
    assert all(order.count("safe") == 2 for order in firsts.values()), firsts
    assert firsts["one"] != firsts["sixteen"]
    body = json.dumps({"status": "ok", "total": 2, "severityCounts": {"major": 2}, "statusCounts": {"open": 2},
                       "alerts": [{"id": "b"}, {"id": "a"}]}).encode()
    other = body.replace(b'"b"', b'"c"')
    assert digest(200, body) == digest(200, body) != digest(200, other)
    assert digest(401, body) == "http 401" and digest(200, b"{}") == "not an alert list"
    assert digest(None, b"x") == "http None"
    assert len(set(SIXTEEN)) == 16 and unique_text(1, "r") != unique_text(2, "r")
    assert path_of(None) == "/alerts" and path_of("a b") == "/alerts?q=a+b"
    rows = [{"arm": arm, "shape": shape, "load": load, "rep": rep, "valid": True, "late": 0, "requests": 10,
             "rps": 1.0 + rep, "p50_ms": 1, "p95_ms": 2, "p99_ms": 3, "cpu_ms_per_request": 4}
            for arm in ARMS for shape in SHAPES for load in LOADS for rep in range(2)]
    rows[0]["valid"] = False
    record = {"mode": "quick", "ok": False, "workers": 4, "reps": 2, "cells": rows, "problems": ["one"]}
    record["table"] = summarize(record)
    assert record["table"][0]["safe"] is None and record["table"][1]["safe"]["rps"]["median"] == 1.5
    text = summary_text(record)
    assert "NOT OK" in text and "not scored" in text and "problem: one" in text
    print("savedq_load self-test: passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    sys.exit(main(Path(sys.argv[1])))
