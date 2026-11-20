"""The cache suite of bench_compare.py: a short-lived response cache in front
of Alerta, with no change to Alerta's code.

Why: a production site has a NetScaler VPX in front of two Alerta VMs. Its
integrated cache can answer a repeated GET without Alerta. This suite checks
that idea with open-source parts: Varnish as the cache and HAProxy as the
front balancer. It shows what the approach gives and which rules keep it
safe. It does not measure a NetScaler.

Layout, all on one runner (the two "VMs" share its CPUs):

    client -> HAProxy :8090 -> Varnish A :6181 -+-> Alerta A :8081 -+
                            -> Varnish B :6182 -+-> Alerta B :8082 -+-> pgbouncer -> PostgreSQL

Each Alerta is gunicorn with half the runner's CPUs as workers, 4 threads,
pgbouncer's Unix socket and no forwarder plugin: the best arm of the tuning
suite, split in two. Each Varnish sends its misses to both Alertas in turn.
HAProxy picks the cache by a hash of the Authorization header, so one
credential always meets the same cache; without that, each cache holds its
own copy and the hit rate of a credential is lower.

Arms, every one behind the same HAProxy:
  alrust            one alrust process
  alerta            the two Alertas, no cache, round robin
  alerta-cache-Ns   the two caches, expiry N seconds (1, 2, 5)

The cache rules (vcl(), below):
- only GET and HEAD of /alerts... with a credential and no cookie;
- the key is the host, the URL, the Authorization header and the X-API-Key
  header, so two credentials never share an answer (the `api-key` query
  parameter is part of the URL);
- only a 200 answer without Set-Cookie is kept, for N seconds, never served
  after that (no grace);
- everything else goes to Alerta unchanged.

Checks before any cell, for each expiry (probe()); a failure stops the suite:
- separation: an admin key and a customer's key ask the same URL through
  the cache, in both header forms, with a wrong key and with no key, twice;
  every answer must equal the answer Alerta gives directly, and the cache
  must have served hits meanwhile (a request without the Authorization
  header meets the caches in turn, so only that form is sure to hit);
- staleness: after a write, the count through the cache must show it within
  the expiry plus 0.75 s; whether an old answer was seen is recorded;
- burst: 32 requests for one new URL at the same moment must reach Alerta
  as one request.

Cells: `count` and `list` repeat one URL with one key, the best case for a
cache. `list-unique` gives every request a new URL (the ignored `_`
parameter), the worst case: no hit, only the cost of the extra hop.
`write-dup` is the duplicate write of the compare suite; a cache passes it.
Real traffic is between the two read cases. A cell is valid with no error,
only 2xx answers, every process alive, the expected hit rate (0.9 or more
for a repeated URL, 0.01 or less for new URLs and for writes) and, for
writes, every counted write saved. The hit rate is 1 minus the caches'
backend requests for each request.

CPU is the process tree's, per completed request, for Alerta (or alrust),
the caches, HAProxy and PostgreSQL.
"""

import json
import os
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request

FRONT_PORT = 8090
# (Alerta port, Varnish port) of each stand-in for an Alerta VM.
STACKS = [(8081, 6181), (8082, 6182)]
TTLS = [1, 2, 5]
ARMS = ["alrust", "alerta", *[f"alerta-cache-{t}s" for t in TTLS]]
# The arms of the cases that do not depend on the expiry.
FEW_ARMS = ["alrust", "alerta", "alerta-cache-2s"]
LIST = "/alerts?page-size=50&sort-by=lastReceiveTime"
KEY2 = "bench-key2-0123456789abcdef0123456789abcdef"
CUSTOMER = "acme"
CUSTOMER_ALERTS = 7
BURST = 32
STALE_MARGIN_S = 0.75
VSTAT = ("cache_hit", "cache_miss", "backend_req", "s_pass")
_dirs = iter(range(10 ** 6))


def ttl_of(arm):
    return int(arm.rsplit("-", 1)[1][:-1]) if "-cache-" in arm else None


def concurrency(bc):
    return [4] if bc.QUICK else [8, 32]


def plan(bc):
    """Every cell of the suite: (arm, case, concurrency, repetition)."""
    conc = concurrency(bc)
    cells = []
    for rep in range(bc.REPS):
        for arm in ARMS:
            cells += [(arm, case, c, rep) for case in ("count", "list") for c in conc]
            if arm in FEW_ARMS:
                cells += [(arm, "list-unique", conc[0], rep), (arm, "write-dup", conc[0], rep)]
    return cells


def databases(bc):
    """The copies of the seeded database the suite needs: key -> name."""
    dbs = {("cache", "read"): "bench_cache_read"}
    for ttl in TTLS:
        dbs[("cache", "probe", ttl)] = f"bench_cache_probe_{ttl}"
    for arm in FEW_ARMS:
        for rep in range(bc.REPS):
            dbs[("cache", "write", arm, rep)] = f"bench_cache_write_{arm}_r{rep}".replace("-", "_")
    return dbs


def vcl(ttl):
    backends = "".join(f'backend alerta{i} {{ .host = "127.0.0.1"; .port = "{port}"; }}\n'
                       for i, (port, _) in enumerate(STACKS))
    adds = "".join(f"    both.add_backend(alerta{i});\n" for i in range(len(STACKS)))
    return f"""vcl 4.1;
import directors;
{backends}
sub vcl_init {{
    new both = directors.round_robin();
{adds}}}
sub vcl_recv {{
    set req.backend_hint = both.backend();
    if (req.method != "GET" && req.method != "HEAD") {{ return (pass); }}
    if (req.http.Cookie) {{ return (pass); }}
    if (!req.http.Authorization && !req.http.X-API-Key) {{ return (pass); }}
    if (req.url !~ "^/alerts") {{ return (pass); }}
    return (hash);
}}
sub vcl_hash {{
    hash_data(req.http.host);
    hash_data(req.url);
    hash_data("|a|");
    hash_data(req.http.Authorization);
    hash_data("|x|");
    hash_data(req.http.X-API-Key);
    return (lookup);
}}
sub vcl_backend_response {{
    if (beresp.status == 200 && !beresp.http.Set-Cookie) {{
        set beresp.ttl = {ttl}s;
        set beresp.grace = 0s;
        set beresp.keep = 0s;
        return (deliver);
    }}
    set beresp.ttl = 1s;
    set beresp.grace = 0s;
    set beresp.uncacheable = true;
    return (deliver);
}}
"""


def haproxy_cfg(ports, by_credential):
    servers = "".join(f"    server s{i} 127.0.0.1:{p}\n" for i, p in enumerate(ports))
    balance = "hdr(Authorization)" if by_credential else "roundrobin"
    return f"""global
    maxconn 2000
    nbthread 2
defaults
    mode http
    timeout connect 5s
    timeout client 120s
    timeout server 120s
frontend front
    bind 127.0.0.1:{FRONT_PORT}
    default_backend servers
backend servers
    balance {balance}
{servers}"""


class Stack:
    """One arm's processes behind the front balancer."""

    def __init__(self):
        self.servers, self.caches, self.cache_dirs, self.logs = [], [], [], []
        self.front = None
        self.base = f"http://127.0.0.1:{FRONT_PORT}"
        self.direct = None

    def procs(self):
        return self.servers + self.caches + ([self.front] if self.front else [])

    def alive(self):
        return all(p.poll() is None for p in self.procs())

    def cpu(self, bc):
        return {"server": sum(bc.tree_cpu(p.pid) for p in self.servers),
                "cache": sum(bc.tree_cpu(p.pid) for p in self.caches),
                "front": bc.tree_cpu(self.front.pid),
                "postgres": bc.tree_cpu(bc.postmaster_pid())}

    def stats(self):
        total = dict.fromkeys(VSTAT, 0)
        for d in self.cache_dirs:
            cmd = [os.environ["BENCH_VARNISHSTAT"], "-n", str(d), "-1"]
            for name in VSTAT:
                cmd += ["-f", f"MAIN.{name}"]
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[0].startswith("MAIN.") and parts[0][5:] in total:
                    total[parts[0][5:]] += int(parts[1])
        return total

    def stop(self, bc):
        for p in reversed(self.procs()):
            bc.stop(p)
        for f in self.logs:
            f.close()


def start_stack(bc, arm, db, tag, extra_env=None):
    st = Stack()

    def logfile(name):
        f = open(bc.OUT / f"cache-{tag}-{name}.log", "w")
        st.logs.append(f)
        return f

    try:
        if arm == "alrust":
            proc, st.direct = bc.start("alrust", db, logfile("alrust"))
            st.servers.append(proc)
            ports = [8080]
        else:
            workers = max(1, bc.VCPUS // len(STACKS))
            for i, (port, _) in enumerate(STACKS):
                proc, base = bc.start(f"alerta-{workers}x4-pgbu-nofwd", db, logfile(f"alerta{i}"),
                                      extra_env, port=port)
                st.servers.append(proc)
                st.direct = st.direct or base
            ports = [port for port, _ in STACKS]
        ttl = ttl_of(arm)
        if ttl:
            for i, (_, vport) in enumerate(STACKS):
                d = bc.WORK / f"v{next(_dirs)}"
                d.mkdir()
                (d / "cache.vcl").write_text(vcl(ttl))
                # http_gzip_support=off: Accept-Encoding goes to Alerta as the
                # client sent it, as in the arms without a cache.
                st.caches.append(subprocess.Popen(
                    [os.environ["BENCH_VARNISHD"], "-F", "-n", str(d), "-a", f"127.0.0.1:{vport}",
                     "-f", str(d / "cache.vcl"), "-s", "malloc,128m", "-p", "http_gzip_support=off"],
                    stdout=logfile(f"varnish{i}"), stderr=subprocess.STDOUT))
                st.cache_dirs.append(d)
            ports = [vport for _, vport in STACKS]
        cfg = bc.WORK / f"haproxy-{next(_dirs)}.cfg"
        cfg.write_text(haproxy_cfg(ports, by_credential=bool(ttl)))
        st.front = subprocess.Popen([os.environ["BENCH_HAPROXY"], "-db", "-f", str(cfg)],
                                    stdout=logfile("haproxy"), stderr=subprocess.STDOUT)
        # Readiness through the whole chain; a cache passes this request.
        bc.wait_ready(st.base, st.front)
        if not st.alive():
            raise RuntimeError(f"{arm}: a process of the stack exited at start")
    except BaseException:
        st.stop(bc)
        raise
    return st


def get(url, headers):
    """(status, body) of one GET on a new connection."""
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Accept-Encoding": "identity", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def total_of(answer):
    status, raw = answer
    try:
        return status, json.loads(raw).get("total") if status == 200 else None
    except ValueError:
        return status, "not JSON"


# ---------------------------------------------------------------- the checks

def probe(bc, ttl, db, rec):
    """Separation, staleness and burst for one expiry. True when all pass."""
    arm = f"alerta-cache-{ttl}s"
    p = {"ttl_s": ttl, "ok": False, "problems": []}
    rec["probes"].append(p)
    bc.psql(db, "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, customer) "
                f"VALUES ('bench-key2', '{KEY2}', 'bench2', ARRAY['read'], 'benchmark customer', "
                f"timezone('UTC', now()) + interval '2 days', 0, '{CUSTOMER}')")
    bc.psql(db, f"UPDATE alerts SET customer = '{CUSTOMER}' WHERE resource < 'host-{CUSTOMER_ALERTS:04d}'")
    admin, customer = {"Authorization": f"Key {bc.KEY}"}, {"Authorization": f"Key {KEY2}"}
    st = start_stack(bc, arm, db, f"probe-{ttl}s",
                     {"CUSTOMER_VIEWS": "True", "ADMIN_USERS": "bench"})
    try:
        # Separation.
        asks = [("admin, Authorization", "/alerts/count", admin),
                ("customer, Authorization", "/alerts/count", customer),
                ("admin, X-API-Key", "/alerts/count", {"X-API-Key": bc.KEY}),
                ("customer, X-API-Key", "/alerts/count", {"X-API-Key": KEY2}),
                ("customer, api-key parameter", f"/alerts/count?api-key={KEY2}", {}),
                ("wrong key", "/alerts/count", {"Authorization": "Key wrong-key"}),
                ("no key", "/alerts/count", {})]
        direct = [total_of(get(st.direct + path, h)) for _, path, h in asks]
        seed = bc.SEED_ALERTS
        wanted = [(200, seed), (200, CUSTOMER_ALERTS), (200, seed), (200, CUSTOMER_ALERTS),
                  (200, CUSTOMER_ALERTS), (401, None), (401, None)]
        if direct != wanted:
            p["problems"].append(f"Alerta answers directly {direct}, {wanted} expected")
        rounds = [[total_of(get(st.base + path, h)) for _, path, h in asks]]
        before = st.stats()
        rounds.append([total_of(get(st.base + path, h)) for _, path, h in asks])
        hits = len(asks) - (st.stats()["backend_req"] - before["backend_req"])
        for n, answers in enumerate(rounds):
            for (name, _, _), got, want in zip(asks, answers, direct):
                if got != want:
                    p["problems"].append(f"separation, round {n + 1}, {name}: {got} through the "
                                         f"cache, {want} directly")
        # Only the two Authorization forms with a good key are sure to meet
        # the same cache twice: HAProxy sends a request without that header
        # to the caches in turn.
        if hits < 2:
            p["problems"].append(f"separation: {hits} answers of the second round came from "
                                 "the cache, 2 or more expected")
        p["separation"] = {"asks": [name for name, _, _ in asks], "direct": direct,
                           "through_cache": rounds, "cache_hits": hits}

        # Staleness: a fresh entry, then a write, then how long the old count lives.
        count = lambda: total_of(get(st.base + "/alerts/count", admin))
        time.sleep(ttl + 0.3)
        first = count()
        alert = {"resource": "cache-probe", "event": "Stale", "environment": "Production",
                 "severity": "minor", "service": ["web"], "text": "staleness probe"}
        status, _, _ = bc.http("POST", st.base + "/alert", json.dumps(alert))
        t0 = time.monotonic()
        seen_old, fresh_after = False, None
        while time.monotonic() - t0 < ttl + 5:
            now = count()
            if now == (200, seed + 1):
                fresh_after = time.monotonic() - t0
                break
            seen_old = seen_old or now == first
            time.sleep(0.05)
        p["staleness"] = {"write_status": status, "before": first, "old_answer_seen": seen_old,
                          "fresh_after_s": fresh_after, "limit_s": ttl + STALE_MARGIN_S}
        if status != 201 or first != (200, seed):
            p["problems"].append(f"staleness: count {first} before the write, write answered {status}")
        if fresh_after is None or fresh_after > ttl + STALE_MARGIN_S:
            p["problems"].append(f"staleness: the new count came after {fresh_after!r} s, "
                                 f"limit {ttl + STALE_MARGIN_S} s")

        # Burst: one new URL, BURST requests at the same moment.
        url, answers = st.base + LIST + f"&_=burst{ttl}", [None] * BURST
        gate = threading.Barrier(BURST)

        def one(i):
            gate.wait()
            answers[i] = get(url, admin)

        before = st.stats()
        threads = [threading.Thread(target=one, args=(i,)) for i in range(BURST)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        fetched = st.stats()["backend_req"] - before["backend_req"]
        same = len({a for a in answers}) == 1 and answers[0] is not None and answers[0][0] == 200
        p["burst"] = {"requests": BURST, "backend_requests": fetched, "answers_equal_200": same}
        if fetched != 1 or not same:
            p["problems"].append(f"burst: {BURST} requests gave {fetched} backend requests, "
                                 f"equal 200 answers: {same}")
        if not st.alive():
            p["problems"].append("a process of the stack exited")
    finally:
        st.stop(bc)
    p["ok"] = not p["problems"]
    bc.log(f"cache probe {ttl}s: {'ok' if p['ok'] else 'FAILED ' + str(p['problems'])}; "
           f"fresh after {p.get('staleness', {}).get('fresh_after_s')!r} s, burst "
           f"{p.get('burst', {}).get('backend_requests')!r} backend request(s)")
    return p["ok"]


def sane(bc, st, arm, rec):
    """The arm serves the seeded data, also in the new-URL form, twice."""
    problems = []
    h = {"Authorization": f"Key {bc.KEY}"}
    for n in range(2):
        for path in (LIST, LIST + "&_=000000000000", "/alerts/count"):
            got = total_of(get(st.base + path, h))
            if got != (200, bc.SEED_ALERTS):
                problems.append(f"{path} (request {n + 1}): {got}, (200, {bc.SEED_ALERTS}) expected")
    rec["checks"].append({"arm": arm, "ok": not problems, "problems": problems})
    if problems:
        bc.log(f"{arm}: CACHE SUITE CHECK FAILED: {problems}")
    return not problems


# ----------------------------------------------------------------- the cells

def oha(bc, st, case, conc, seconds, out_file, json_flag):
    cmd = [bc.OHA, "-z", f"{seconds}s", "-c", str(conc), "--no-tui", "--http-version", "1.1", "-w"]
    cmd += json_flag
    for k, v in bc.HEADERS.items():
        cmd += ["-H", f"{k}: {v}"]
    if case == "write-dup":
        cmd += ["-m", "POST", "-H", "Content-Type: application/json", "-d", bc.DUP_BODY,
                st.base + "/alert"]
    elif case == "list-unique":
        # oha makes each URL from this pattern; it takes the dots as written.
        cmd += ["-m", "GET", "--rand-regex-url",
                st.base + LIST.replace("?", "\\?") + "&_=[0-9]{12}"]
    else:
        cmd += ["-m", "GET", st.base + {"list": LIST, "count": "/alerts/count"}[case]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out_file.write_text(r.stdout)
    if r.returncode != 0:
        raise RuntimeError(f"oha exited {r.returncode}: {r.stderr[-500:]}")
    return json.loads(r.stdout)


def cell(bc, st, arm, case, conc, rep, db, json_flag, rec):
    raw = bc.OUT / "raw"
    write, problems = case == "write-dup", []
    if write:
        prime_id, problems = bc.prime_dup(st.base, db)
    warm = bc.summarize_oha(oha(bc, st, case, conc, bc.WARM_S,
                                raw / f"cache-warm-{arm}-{case}-c{conc}-r{rep}.json", json_flag))
    time.sleep(1.5)
    if write:
        rows = bc.dup_rows(db)
        before = rows[0]["duplicate_count"] if len(rows) == 1 else None
        if before != warm["ok"]:
            problems.append(f"after the warm-up duplicate_count is {before!r}, {warm['ok']} counted")
    cpu0, v0 = st.cpu(bc), st.stats()
    t0 = time.monotonic()
    j = oha(bc, st, case, conc, bc.TIMED_S, raw / f"cache-timed-{arm}-{case}-c{conc}-r{rep}.json",
            json_flag)
    wall = time.monotonic() - t0
    cpu1, v1 = st.cpu(bc), st.stats()
    alive = st.alive()
    time.sleep(1.5)
    s = bc.summarize_oha(j)
    s["warm"] = warm
    if write:
        rows = bc.dup_rows(db)
        after = rows[0]["duplicate_count"] if len(rows) == 1 else None
        if before is None or after is None or after - before != s["ok"]:
            problems.append(f"timed: duplicate_count {before!r} -> {after!r}, {s['ok']} counted")
        if len(rows) != 1 or not bc.dup_identity_ok(rows[0], prime_id):
            problems.append(f"after the timed run the database has {rows!r}")
        total = int(bc.psql(db, "SELECT count(*) FROM alerts"))
        if total != bc.SEED_ALERTS + 1:
            problems.append(f"{total} alerts, {bc.SEED_ALERTS + 1} expected")
    req = s["requests"] or 1
    hz = os.sysconf("SC_CLK_TCK")
    s.update({"arm": arm, "case": case, "c": conc, "rep": rep, "db": db, "wall_s": wall,
              "alive": alive, "cache": {k: v1[k] - v0[k] for k in VSTAT}})
    for part in cpu0:
        s[f"{part}_cpu_ms_per_request"] = 1000 * (cpu1[part] - cpu0[part]) / hz / req
    if ttl_of(arm):
        # From backend_req, which Varnish counts at once; its hit, miss and
        # pass counters come later, from each worker thread (run 44805872744).
        s["hit_ratio"] = max(0.0, 1 - s["cache"]["backend_req"] / req)
        if case in ("count", "list") and s["hit_ratio"] < 0.9:
            problems.append(f"hit ratio {s['hit_ratio']!r} for a repeated URL, 0.9 or more expected")
        if case in ("list-unique", "write-dup") and s["hit_ratio"] > 0.01:
            problems.append(f"hit ratio {s['hit_ratio']!r}, 0.01 or less expected: "
                            "every such request must reach a server")
    s["problems"] = problems
    s["valid"] = (alive and s["errors"] == 0 and s["non_2xx"] == 0 and s["ok"] > 0
                  and warm["errors"] == 0 and warm["non_2xx"] == 0 and not problems)
    rec["cells"].append(s)
    ratio = "" if s.get("hit_ratio") is None else f", hit ratio {s['hit_ratio']:.4f}"
    bc.log(f"cache {arm:16} {case:11} c={conc} r{rep}: {'' if s['valid'] else 'INVALID '}"
           f"{s['rps']:.1f} rps, p50 {s['p50_ms']:.2f} ms, p99 {s['p99_ms']:.2f} ms, errors "
           f"{s['errors']}, non-2xx {s['non_2xx']}{ratio}"
           + (f", problems {problems}" if problems else ""))
    return alive


def run(bc, record, dbs):
    """Checks, then cells. True when every check passed and every planned
    cell exists and is valid; results are in record["cache"]."""
    def version(name, flag):
        r = subprocess.run([os.environ[name], flag], capture_output=True, text=True)
        return " ".join((r.stdout + r.stderr).strip().split("\n")[0].split())

    rec = record["cache"] = {
        "arms": ARMS, "few_arms": FEW_ARMS, "ttls_s": TTLS, "concurrency": concurrency(bc),
        "alerta_per_stack": f"{max(1, bc.VCPUS // len(STACKS))}x4-pgbu-nofwd",
        "varnish": version("BENCH_VARNISHD", "-V"), "haproxy": version("BENCH_HAPROXY", "-v"),
        "probes": [], "checks": [], "cells": []}
    ok = True
    for ttl in TTLS:
        if not probe(bc, ttl, dbs[("cache", "probe", ttl)], rec):
            ok = False
    json_flag = bc.oha_json_flag()
    planned = plan(bc)
    for rep in range(bc.REPS if ok else 0):
        order = ARMS[rep % len(ARMS):] + ARMS[:rep % len(ARMS)]
        for arm in order:
            st = start_stack(bc, arm, dbs[("cache", "read")], f"{arm}-r{rep}-read")
            try:
                if not sane(bc, st, arm, rec):
                    ok = False
                    continue
                alive = True
                for a, case, conc, r in planned:
                    if alive and (a, r) == (arm, rep) and case != "write-dup":
                        alive = cell(bc, st, arm, case, conc, rep, dbs[("cache", "read")],
                                     json_flag, rec)
            finally:
                st.stop(bc)
            for a, case, conc, r in planned:
                if (a, r) == (arm, rep) and case == "write-dup":
                    db = dbs[("cache", "write", arm, rep)]
                    st = start_stack(bc, arm, db, f"{arm}-r{rep}-write")
                    try:
                        cell(bc, st, arm, case, conc, rep, db, json_flag, rec)
                    finally:
                        st.stop(bc)
    got = {(c["arm"], c["case"], c["c"], c["rep"]): c["valid"] for c in rec["cells"]}
    rec["gate"] = {"expected": len(planned),
                   "missing": [list(k) for k in planned if k not in got],
                   "invalid": [list(k) for k in planned if got.get(k) is False],
                   "checks_ok": ok}
    rec["gate"]["ok"] = ok and not rec["gate"]["missing"] and not rec["gate"]["invalid"]
    return rec["gate"]["ok"]


# --------------------------------------------------------------- the summary

def summary(record):
    rec = record["cache"]
    g = rec.get("gate", {})
    lines = ["", "## Response cache in front of Alerta (Varnish; a stand-in, not a NetScaler)", "",
             f"{rec['varnish']}; {rec['haproxy']}.",
             f"Every arm is behind one HAProxy. The Alerta arms are two servers, each "
             f"{rec['alerta_per_stack']}; the cache arms add one Varnish for each server and "
             "HAProxy picks the cache by the Authorization header. `count` and `list` repeat one "
             "URL with one key (best case for a cache); `list-unique` never repeats a URL (worst "
             "case). Real traffic is between the two. All processes and the load generator share "
             "the runner's CPUs.", ""]
    if g:
        lines += [f"Cells: {g['expected']} planned, {len(g['missing'])} missing, "
                  f"{len(g['invalid'])} invalid; checks {'passed' if g['checks_ok'] else 'FAILED'}.", ""]
    for p in rec["probes"]:
        if not p["ok"]:
            lines.append(f"- FAILED check, expiry {p['ttl_s']} s: {p['problems']}")
            continue
        s, b, sep = p["staleness"], p["burst"], p["separation"]
        lines.append(f"- expiry {p['ttl_s']} s: {len(sep['asks'])} credential forms answered as "
                     f"Alerta answers directly, twice ({sep['cache_hits']} from the cache); after a "
                     f"write the new count came after {s['fresh_after_s']:.2f} s (limit "
                     f"{s['limit_s']} s; an old answer was {'seen' if s['old_answer_seen'] else 'not seen'}); "
                     f"{b['requests']} requests at once gave {b['backend_requests']} backend request.")
    for c in rec["checks"]:
        if not c["ok"]:
            lines.append(f"- FAILED data check, {c['arm']}: {c['problems']}")
    rows = {}
    for c in rec["cells"]:
        rows.setdefault((c["case"], c["c"], c["arm"]), []).append(c)
    if rows:
        lines += ["", "| case | c | arm | valid reps | rps | p50 ms | p99 ms | hit ratio | backend requests | "
                      "server CPU ms/req | cache CPU ms/req | HAProxy CPU ms/req | postgres CPU ms/req |",
                  "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for case in ("count", "list", "list-unique", "write-dup"):
        for conc in rec["concurrency"]:
            for arm in rec["arms"]:
                allc = rows.get((case, conc, arm))
                if not allc:
                    continue
                cs = [x for x in allc if x["valid"]]
                if not cs:
                    lines.append(f"| {case} | {conc} | {arm} | 0 of {len(allc)} | not scored "
                                 "| | | | | | | | |")
                    continue
                med = lambda k: statistics.median(x[k] for x in cs)
                cached = "hit_ratio" in cs[0]
                ratio = f"{med('hit_ratio'):.4f}" if cached else ""
                fetched = (f"{statistics.median(x['cache']['backend_req'] for x in cs):.0f}"
                           if cached else "")
                lines.append(
                    f"| {case} | {conc} | {arm} | {len(cs)} of {len(allc)} | {med('rps'):.0f} | "
                    f"{med('p50_ms'):.2f} | {med('p99_ms'):.2f} | {ratio} | {fetched} | "
                    f"{med('server_cpu_ms_per_request'):.3f} | {med('cache_cpu_ms_per_request'):.3f} | "
                    f"{med('front_cpu_ms_per_request'):.3f} | {med('postgres_cpu_ms_per_request'):.3f} |")
    for c in rec["cells"]:
        if not c["valid"]:
            lines.append(f"- INVALID {c['arm']} {c['case']} c={c['c']} r{c['rep']}: errors "
                         f"{c['errors']}, non-2xx {c['non_2xx']}, alive {c['alive']}, {c['problems']}")
    return lines
