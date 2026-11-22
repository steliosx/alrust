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

Shapes of the search (GET /alerts, sort-by=resource, page-size=50: each
seeded alert has its own resource, so the page is the same each time;
Accept-Encoding: identity):
  one       the same `q` each time;
  sixteen   sixteen different `q`, in turn;
  unique    a `q` that no request had before (each one is a miss, and
            fills the store: the cost side of the patch);
  none      no `q` (control: the patch has no part in it).

Loads, each with eight client threads and one kept connection a thread.
All threads start at the same time.
  offered   requests are due at a fixed rate (RATE a second, the same for
            both arms). A thread takes the next due request, waits for its
            time and sends it. `late` is how many were sent more than
            100 ms after they were due, and `max lag` the most;
  closed    each thread sends its next request as soon as it has checked
            the last answer. Its requests a second are those of this
            client: eight threads on the same machine that check every
            answer. Not the server's capacity.

Times, both taken when the last byte of the answer was read, before the
client checks it:
  due to body    from when the request was due (offered only; it has the
                 time that a request waited for a free client thread);
  send to body   from when the request was sent.
The table's p50 / p95 / p99 / max are due to body for offered and send to
body for closed; results.json has both for each cell.

Order. A repetition runs every shape and load on both arms. The arm order
of a pair is A B, B A, B A, A B over four repetitions (full mode), and the
other way round for every second shape, so each arm is first as often as
second in each shape. Each cell has a warm-up that is not counted.

Gates. A cell, warm-up and timed, is valid only when:
  - no client thread stopped (an exception in a thread is caught and
    counted; it is not lost in the thread);
  - every request that was taken was answered and checked;
  - offered: exactly RATE x seconds requests were sent;
  - every answer is HTTP 200 and equal to what Alerta without the add-on
    gave for that key and search: the page, its size, the number of
    pages, `more`, the total, the status and severity counts and each
    alert of the page in order with all its fields, `href` left out (its
    port is the server's). There is no second try of a request.
The expected answers are also checked against the seed, not only against
each other: for each customer key every alert of every expected page has
that key's customer and is one of that customer's seeded alerts; with no
`q` the page of each key is its first 50 seeded alerts in resource order,
with its total. The three keys' answers to one search must differ. The
`unique` texts differ only in a term that is true for every alert, so the
expected answer is the one of their common part; after the timed cells
the first, the last and 20 more of the texts that were sent are asked of
Alerta without the add-on again, and must give that answer. One failed
gate makes the run not ok, and the cell is not scored.

Numbers for a cell: requests, requests a second, the times above (ms), and
the CPU time of the server's process tree for each request (ms; gunicorn
master and workers, from /proc). The summary gives the median over the
repetitions and each repetition's value. With a few hundred requests in a
cell, p99 rests on few requests.

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
import math
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
PAGE = 50
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
TIMES = ("p50_ms", "p95_ms", "p99_ms", "max_ms")


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


def page_of(status, body):
    """What is compared of an answer, or None when it is not a page."""
    if status != 200:
        return None
    try:
        data = json.loads(body)
        alerts = [{k: v for k, v in alert.items() if k != "href"} for alert in data["alerts"]]
        # An empty page's lastTime is the server's clock, not data.
        top = {k: v for k, v in data.items() if k != "alerts" and not (k == "lastTime" and not alerts)}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return {"top": top, "alerts": alerts}


def digest(status, body):
    """What is compared of an answer, as one short value."""
    if status != 200:
        return f"http {status}"
    page = page_of(status, body)
    if page is None:
        return "not an alert list"
    return hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()[:24]


def path_of(q):
    query = {} if q is None else {"q": q}
    query.update({"sort-by": "resource", "page-size": str(PAGE)})
    return "/alerts?" + urllib.parse.urlencode(query)


class Client:
    """One thread's kept connection. No second try: a request that fails
    is a failed request."""

    def __init__(self, port):
        self.port, self.connection = port, None

    def get(self, path, key):
        """(status or None, body or the error's type, sent, ended)."""
        if self.connection is None:
            self.connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = time.monotonic()
        try:
            self.connection.request("GET", path, headers={"Authorization": f"Key {key}",
                                                            "Accept-Encoding": "identity"})
            response = self.connection.getresponse()
            body = response.read()
            return response.status, body, sent, time.monotonic()
        except (OSError, http.client.HTTPException) as exc:
            self.connection.close()
            self.connection = None
            return None, type(exc).__name__.encode(), sent, time.monotonic()

    def close(self):
        if self.connection is not None:
            self.connection.close()


def run_load(port, load, seconds, request_of, client_of=None):
    """Sends requests for `seconds` from one start for all threads.
    request_of(n) -> (path, key, wanted digest). Returns the counts and the
    samples [(due to body or None, send to body)] in seconds."""
    client_of = client_of or (lambda: Client(port))
    lock = threading.Lock()
    got = {"taken": 0, "done": 0, "late": 0, "max_lag": 0.0, "last": None}
    samples, wrong, failed = [], {}, {}
    start = time.monotonic() + 0.1
    stop = start + seconds

    def work():
        client = None
        try:
            client = client_of()
            wait = start - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            while True:
                with lock:
                    n = got["taken"]
                    due = start + n / RATE if load == "offered" else None
                    if (due if due is not None else time.monotonic()) >= stop:
                        return
                    got["taken"] += 1
                if due is not None:
                    wait = due - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                path, key, wanted = request_of(n)
                status, body, sent, ended = client.get(path, key)
                answer = digest(status, body)  # after the time was taken
                with lock:
                    got["done"] += 1
                    got["last"] = ended if got["last"] is None else max(got["last"], ended)
                    samples.append((None if due is None else ended - due, ended - sent))
                    if due is not None:
                        got["max_lag"] = max(got["max_lag"], sent - due)
                        got["late"] += sent - due > LATE_S
                    if answer != wanted:
                        kind = answer if answer.startswith(("http", "not")) else "other answer"
                        wrong[kind] = wrong.get(kind, 0) + 1
        except BaseException as exc:  # counted here: a thread's exception is lost at join()
            with lock:
                failed[type(exc).__name__] = failed.get(type(exc).__name__, 0) + 1
        finally:
            if client is not None:
                client.close()

    threads = [threading.Thread(target=work) for _ in range(CLIENTS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {"samples": samples, "wrong": wrong, "failed": failed, "taken": got["taken"], "done": got["done"],
            "slots": math.ceil(RATE * seconds - 1e-9) if load == "offered" else None,
            "late": got["late"], "max_lag_s": got["max_lag"],
            "wall_s": (got["last"] - start) if got["last"] is not None else None}


def load_problems(result, what):
    problems = []
    if result["failed"]:
        problems.append(f"{what}: a client thread stopped: {result['failed']}")
    if result["done"] != result["taken"]:
        problems.append(f"{what}: {result['taken']} requests taken, {result['done']} answered and checked")
    if result["slots"] is not None and result["taken"] != result["slots"]:
        problems.append(f"{what}: {result['taken']} of {result['slots']} due requests sent")
    if result["wrong"]:
        problems.append(f"{what}: wrong answers {result['wrong']}")
    if not result["done"]:
        problems.append(f"{what}: no request")
    return problems


def times_ms(values):
    values = sorted(values)
    return {name: round(1000 * percentile(values, q), 2)
            for name, q in (("p50_ms", 0.5), ("p95_ms", 0.95), ("p99_ms", 0.99), ("max_ms", 1.0))}


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
        self.made = {}  # owner -> ids, in resource order
        self.unique_sent = 0
        self.unique_lock = threading.Lock()

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
        with open(self.logs[name], "ab") as log:
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
            status = client.get("/management/gtg", "none")[0]
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
            customer = "NULL" if who == "admin" else f"'{self.customer(who)}'"
            scopes = "'{admin,read,write}'" if who == "admin" else "'{read}'"
            self.psql('INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, customer) VALUES '
                      f"('{secrets.token_hex(16)}', '{key}', 'savedq-load-{who}', {scopes}, 'savedq load', "
                      f"timezone('UTC', now()) + interval '1 day', 0, {customer})")
        connection = http.client.HTTPConnection("127.0.0.1", PORTS["stock"], timeout=30)
        n = 0
        for owner, count in OWNERS:
            for _ in range(count):
                # The resource sorts in the order of n.
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
                self.made.setdefault(owner, []).append(json.loads(answer)["id"])
                n += 1
        connection.close()
        for owner, ids in self.made.items():
            if owner:
                quoted = ",".join(f"'{i}'" for i in ids)
                self.psql(f"UPDATE alerts SET customer='{self.customer(owner)}' WHERE id IN ({quoted})")
        if int(self.psql("SELECT count(*) FROM alerts")) != sum(count for _, count in OWNERS):
            raise RuntimeError("the alerts table does not have the seeded number of rows")
        self.psql("ANALYZE alerts")

    def customer(self, who):
        return f"cust-{who}-{self.run}"

    def expected(self, texts):
        """{(who, q): (digest, page)} from Alerta without the add-on."""
        client, answers = Client(PORTS["stock"]), {}
        try:
            for q in texts:
                for who, key in self.keys.items():
                    status, body = client.get(path_of(q), key)[:2]
                    answers[who, q] = (digest(status, body), page_of(status, body))
        finally:
            client.close()
        return answers

    def seed_problems(self, expected):
        """The expected pages against the seed itself."""
        problems = []
        everyone = [i for owner, _ in OWNERS for i in self.made[owner]]
        for (who, q), (_, page) in expected.items():
            name = f"{who} / {'no q' if q is None else q}"
            if page is None:
                problems.append(f"{name}: Alerta without the add-on gave no page")
                continue
            ids = [alert["id"] for alert in page["alerts"]]
            if who != "admin":
                own = set(self.made[who])
                if any(alert.get("customer") != self.customer(who) or alert["id"] not in own
                       for alert in page["alerts"]):
                    problems.append(f"{name}: an alert of another customer")
            if q is None:
                seeded = self.made[who] if who != "admin" else everyone
                if ids != seeded[:PAGE] or page["top"].get("total") != len(seeded):
                    problems.append(f"{name}: not the first {PAGE} seeded alerts and their total")
            if len(ids) > PAGE or page["top"].get("pageSize") != PAGE:
                problems.append(f"{name}: the page size is not {PAGE}")
        return problems

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

    def cell(self, arm, shape, load, rep, wanted):
        port, pid = PORTS[arm], self.servers[arm].pid
        request_of = self.request_maker(shape, wanted)
        warm = run_load(port, load, WARM_S, request_of)
        cpu0 = tree_cpu(pid)
        timed = run_load(port, load, TIMED_S, request_of)
        cpu = tree_cpu(pid) - cpu0
        problems = load_problems(warm, "warm-up") + load_problems(timed, "timed")
        if self.servers[arm].poll() is not None:
            problems.append("the server stopped")
        row = {"arm": arm, "shape": shape, "load": load, "rep": rep, "requests": timed["done"],
               "taken": timed["taken"], "slots": timed["slots"], "warm_requests": warm["done"],
               "problems": problems, "valid": not problems, "late": timed["late"],
               "max_lag_ms": round(1000 * timed["max_lag_s"], 2) if load == "offered" else None,
               "wall_s": round(timed["wall_s"], 3) if timed["wall_s"] else None}
        if row["valid"]:
            send = times_ms(s for _, s in timed["samples"])
            due = times_ms(d for d, _ in timed["samples"]) if load == "offered" else None
            row.update(rps=round(timed["done"] / timed["wall_s"], 1), **(due or send),
                       send_to_body=send, due_to_body=due,
                       cpu_ms_per_request=round(1000 * cpu / timed["done"], 3))
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
                              for name in ("rps", *TIMES, "cpu_ms_per_request")}
                entry[arm]["late"] = sum(r["late"] for r in rows)
                entry[arm]["max_lag_ms"] = max((r["max_lag_ms"] or 0) for r in rows) if load == "offered" else None
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
    lines += ["", "Medians over the repetitions (each repetition is in results.json). Times end when the "
              "last byte of the answer was read. offered: from when the request was due (waiting for a free "
              "client thread is in it). closed: from when it was sent; its req/s is that of this co-located "
              "client of eight threads that checks every answer.", "",
              "| search | load | arm | requests | req/s | p50 ms | p95 ms | p99 ms | max ms "
              "| server CPU ms/request | late | max lag ms |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for entry in record.get("table", []):
        for arm in ARMS:
            got = entry[arm]
            if got is None:
                lines.append(f"| {entry['shape']} | {entry['load']} | {arm} | not scored | | | | | | | | |")
                continue
            lag = "" if got["max_lag_ms"] is None else got["max_lag_ms"]
            lines.append(f"| {entry['shape']} | {entry['load']} | {arm} | {got['requests']} | "
                         + " | ".join(str(got[name]["median"]) for name in ("rps", *TIMES, "cpu_ms_per_request"))
                         + f" | {got['late']} | {lag} |")
    lines += ["", "`unique` is the cost side: each search text is new, so `savedq` parses it and "
              "keeps it for nothing. `none` is the control: no search text.", ""]
    return "\n".join(lines)


def main(out):
    out.mkdir(parents=True)
    record = {"schema": 2, "mode": "quick" if QUICK else "full", "ok": False, "workers": VCPUS,
              "threads": THREADS, "clients": CLIENTS, "rate": RATE, "page_size": PAGE, "sort_by": "resource",
              "reps": REPS, "warm_s": WARM_S, "timed_s": TIMED_S, "arms": ARMS, "cells": [], "problems": [],
              "searches": {"one": ONE, "sixteen": SIXTEEN,
                           "unique": UNIQUE_PART + " AND NOT resource:zz<run>n<number>"},
              "sources_sha256": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                                 for name in ("alerta_fast.py", "savedq_load.py")}}
    lab = Lab(out)
    try:
        lab.seed()
        texts = [ONE, *SIXTEEN, UNIQUE_PART, None]
        expected = lab.expected(texts)
        record["problems"] += lab.seed_problems(expected)
        wanted = {k: d for k, (d, _) in expected.items()}
        same = [str(q) for q in texts if len({wanted[who, q] for who in lab.keys}) != 3]
        if same:
            record["problems"].append(f"the three keys do not get three different answers for {same}")
        if record["problems"]:
            raise RuntimeError("the expected answers failed a check; nothing was timed")
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
        differ = sum(1 for (who, q), (value, _) in again.items() if value != wanted[who, UNIQUE_PART])
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
        record["ok"] = False
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

    # The page: order, fields and paging count; href and an empty page's clock do not.
    def body(ids, **top):
        data = {"status": "ok", "page": 1, "pageSize": 50, "pages": 1, "more": False, "total": len(ids),
                "severityCounts": {"major": len(ids)}, "statusCounts": {"open": len(ids)},
                "lastTime": "2026-11-22T00:00:00.000Z", "autoRefresh": True,
                "alerts": [{"id": i, "resource": "r" + i, "customer": "c", "href": "http://127.0.0.1:1/alert/" + i}
                           for i in ids]}
        data.update(top)
        return json.dumps(data).encode()
    base = digest(200, body(["a", "b"]))
    assert base == digest(200, body(["a", "b"]).replace(b"127.0.0.1:1", b"127.0.0.1:2"))
    for changed in (body(["b", "a"]), body(["a", "c"]), body(["a", "b"], page=2), body(["a", "b"], more=True),
                    body(["a", "b"], pages=2), body(["a", "b"], total=3), body(["a", "b"], pageSize=20),
                    body(["a", "b"]).replace(b'"customer": "c"', b'"customer": "d"', 1),
                    body(["a", "b"]).replace(b'"resource": "ra"', b'"resource": "rx"')):
        assert digest(200, changed) != base, changed
    empty = digest(200, body([], lastTime="2026-11-22T00:00:01.000Z"))
    assert empty == digest(200, body([], lastTime="2026-11-22T00:00:09.000Z"))
    assert digest(401, b"{}") == "http 401" and digest(200, b"{}") == "not an alert list"
    assert digest(None, b"x") == "http None"
    assert len(set(SIXTEEN)) == 16 and unique_text(1, "r") != unique_text(2, "r")
    assert path_of(None) == "/alerts?sort-by=resource&page-size=50"
    assert path_of("a b") == "/alerts?q=a+b&sort-by=resource&page-size=50"

    # The load: every slot sent and checked; a thread that stops, a lost
    # answer and a wrong answer each make the cell invalid.
    good = body(["a"])

    class Fake:
        def __init__(self, answer=good):
            self.answer = answer

        def get(self, path, key):
            now = time.monotonic()
            return 200, self.answer, now, now

        def close(self):
            pass

    wanted = digest(200, good)
    seconds = 0.5
    result = run_load(0, "offered", seconds, lambda n: ("/", "k", wanted), Fake)
    assert result["taken"] == result["done"] == result["slots"] == 20 and not load_problems(result, "t"), result
    assert len(result["samples"]) == 20 and all(d >= s >= 0 for d, s in result["samples"])
    result = run_load(0, "closed", 0.2, lambda n: ("/", "k", wanted), Fake)
    assert result["taken"] == result["done"] > 20 and result["slots"] is None and not load_problems(result, "t")
    assert all(d is None for d, _ in result["samples"])

    def stops_at_3(n):
        if n == 3:
            raise RuntimeError("a client thread stops")
        return "/", "k", wanted
    result = run_load(0, "offered", seconds, stops_at_3, Fake)
    problems = load_problems(result, "t")
    assert result["failed"] == {"RuntimeError": 1} and result["done"] == result["taken"] - 1 == 19, result
    assert any("stopped" in p for p in problems) and any("answered and checked" in p for p in problems)
    result = run_load(0, "offered", seconds, lambda n: ("/", "k", wanted), lambda: Fake(body(["z"])))
    assert result["wrong"] == {"other answer": 20} and load_problems(result, "t")

    rows = [{"arm": arm, "shape": shape, "load": load, "rep": rep, "valid": True, "late": 0, "requests": 10,
             "max_lag_ms": 1.0 if load == "offered" else None, "rps": 1.0 + rep, "p50_ms": 1, "p95_ms": 2,
             "p99_ms": 3, "max_ms": 4, "cpu_ms_per_request": 4}
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
