"""The cache suite of bench_compare.py: a short-lived response cache in front
of Alerta, with no change to Alerta's code.

Why: a production site has a NetScaler VPX in front of two Alerta VMs. Its
integrated cache can answer a repeated GET without Alerta. This suite checks
that idea with open-source parts: Varnish as the cache and HAProxy as the
front balancer. It shows what the approach gives and which rules keep it
safe. It does not measure a NetScaler, and it is a laboratory rule set for
this benchmark's two synthetic keys, not a production configuration.

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

The cache rules (vcl(), below). Only what the suite measures is eligible:
- GET only, no cookie;
- the two measured routes only: /alerts/count without a query, and the
  measured list URL, alone or with the ignored `_` parameter;
- exactly one credential header, and its value must be one of the two
  synthetic keys of the benchmark, whole: `Authorization: Key <key>` or
  `X-API-Key: <key>`. A request with both headers, with any other
  credential (also another valid key that starts with the same letters) or
  with the `api-key` query parameter goes to Alerta;
- the key is the host, the URL, the Authorization header and the X-API-Key
  header, so two credentials never share an answer;
- only a 200 answer without Set-Cookie is kept. An answer that says
  no-store, no-cache, private, must-revalidate or proxy-revalidate, or
  `Vary: *`, or a freshness of zero, is not kept. The expiry is N seconds or
  the origin's own freshness (max-age, s-maxage, Expires), whichever is
  shorter. Nothing is served after the expiry (no grace);
- everything else goes to Alerta unchanged.
Alerta 9.1.0 sends none of those response headers, so the origin-header
rules are written but not exercised by a run.

What a hit costs in authentication. A hit does not reach Alerta. So for up
to N seconds after an answer was stored, the same credential gets that
answer without Alerta's checks of the key (expiry, deletion) and of the
customer scope, and without Alerta's update of the key's use count and
last-used time. The CPU numbers of the cache arms include those avoided
checks and writes. The revocation check below observes the interval: it
takes a time before it deletes a key, and from that time it records, for
each cache and the front, the last 200 it saw and the first 401 it saw. The
real end of the stale answers is between those two observations. No 200
seen is recorded as not observed, not as zero. The first 401 must come
within N + 0.75 s. A site that cannot accept that interval needs
invalidation or revalidation, which this suite does not have.

Checks before any cell, for each expiry (probe()); a failure stops the suite:
- oracle: Alerta's direct answers equal the database's (totals, counts by
  severity and status, the 50 ids in order), for the admin key and for a
  customer's key;
- isolation, on each cache alone, with the same Host: the two identities
  in turn, in both header forms, on both routes (eight miss and hit pairs);
  each answer must equal Alerta's direct answer and each request must be
  the expected miss, hit or pass, read from that cache's backend-request
  counter. A third key that is valid in Alerta and differs from the two
  only after `bench-key` must be a pass: 200 twice, one backend request
  each time (a miss would make the second a hit);
- separation, through HAProxy: the same identities and forms, twice;
- staleness: both caches and the front hold the old count; after a write
  every observation must be the old or the new answer, each target must be
  new within N + 0.75 s, and must stay new;
- burst: 32 requests for one new URL at the same moment must reach Alerta
  as one request;
- revocation: as staleness, for a deleted key: 200 (old) or 401, the first
  401 within N + 0.75 s of the time taken before the deletion, then always
  401.

Before the cells of an arm (sane()): the list, the list with `_` and the
count, twice, must equal the database's answer and Alerta's direct answer;
on a cache arm the second round must come from the cache (no backend
request).

Cells: `count` and `list` repeat one URL with one key, the best case for a
cache. `list-unique` gives every request a new URL (the ignored `_`
parameter), the worst case: no hit, only the cost of the extra hop.
`write-dup` is the duplicate write of the compare suite; a cache passes it.
Real traffic is between the two read cases. A cell is valid with no error,
only 2xx answers, every process alive and, for writes, every counted write
saved. On a cache arm the counters of each cache are kept as read, before
and after; a missing counter stops the run and a counter that goes down
makes the cell invalid. The backend requests are checked as a number
first. For writes they must equal the client's requests exactly: the load
tool waits for its last answers, so nothing is in flight when the counters
are read. For new URLs they must also be equal: the suite does not see the
URLs that the load tool made, so it cannot explain a difference by a
repeated URL, and a cell with any difference is not scored. For a repeated
URL they must be one tenth of the requests or fewer. The hit ratio is 1
minus backend requests for each request, not clamped.

The gate (gate()) wants every check part of every expiry, every isolation
step on each cache with its expected result, a passed data check for every
arm and repetition, and every planned cell once, valid, with its per-cache
counters. gate_self_test() runs first: it removes each
required item from a complete record in turn and the gate must refuse each.

CPU is the process tree's, per completed request, for Alerta (or alrust),
the caches, HAProxy and PostgreSQL.
"""

import copy
import json
import os
import re
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
COUNT = "/alerts/count"
LIST = "/alerts?page-size=50&sort-by=lastReceiveTime"
# The eligible routes and credentials, as regular expressions of the VCL.
ROUTES_RE = r"^/alerts/count$|^/alerts\?page-size=50&sort-by=lastReceiveTime(&_=[0-9a-z]{1,16})?$"
KEY2 = "bench-key2-0123456789abcdef0123456789abcdef"
# Valid in Alerta in the checks, never eligible: the pass-path control.
KEY3 = "bench-key3-0123456789abcdef0123456789abcdef"
CUSTOMER = "acme"
CUSTOMER_ALERTS = 7
BURST = 32
STALE_MARGIN_S = 0.75
VSTAT = ("cache_hit", "cache_miss", "backend_req", "s_pass")
PROBE_PARTS = ("oracle", "isolation", "separation", "staleness", "burst", "revocation")
SANE_PATHS = (LIST, LIST + "&_=000000000000", COUNT)
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


VCL = """vcl 4.1;
import directors;
@BACKENDS@
sub vcl_init {
    new both = directors.round_robin();
@ADDS@}
sub vcl_recv {
    set req.backend_hint = both.backend();
    if (req.method != "GET") { return (pass); }
    if (req.http.Cookie) { return (pass); }
    if (req.url !~ "@ROUTES@") { return (pass); }
    if (req.http.Authorization && req.http.X-API-Key) { return (pass); }
    if (req.http.Authorization ~ "^Key (@KEY@)$" || req.http.X-API-Key ~ "^(@KEY@)$") {
        return (hash);
    }
    return (pass);
}
sub vcl_hash {
    hash_data(req.http.host);
    hash_data(req.url);
    hash_data("|a|");
    hash_data(req.http.Authorization);
    hash_data("|x|");
    hash_data(req.http.X-API-Key);
    return (lookup);
}
sub vcl_backend_response {
    set beresp.grace = 0s;
    set beresp.keep = 0s;
    # beresp.ttl is the origin's freshness here, or the default (-t), which
    # is the expiry, when the origin gives none.
    if (beresp.status != 200 || beresp.http.Set-Cookie || beresp.http.Vary == "*"
        || beresp.http.Cache-Control ~ "(?i)no-store|no-cache|private|must-revalidate|proxy-revalidate"
        || beresp.http.Pragma ~ "(?i)no-cache" || beresp.ttl <= 0s) {
        set beresp.ttl = 1s;
        set beresp.uncacheable = true;
        return (deliver);
    }
    if (beresp.ttl > @TTL@s) { set beresp.ttl = @TTL@s; }
    return (deliver);
}
"""


def key_re(keys):
    """The eligible keys, whole, as one alternation for the VCL."""
    for key in keys:
        if not re.fullmatch(r"[0-9a-z-]+", key):
            raise RuntimeError("a benchmark key has a character that the VCL pattern would read as syntax")
    return "|".join(keys)


def vcl(ttl, keys):
    backends = "".join(f'backend alerta{i} {{ .host = "127.0.0.1"; .port = "{port}"; }}\n'
                       for i, (port, _) in enumerate(STACKS))
    adds = "".join(f"    both.add_backend(alerta{i});\n" for i in range(len(STACKS)))
    return (VCL.replace("@BACKENDS@", backends).replace("@ADDS@", adds)
            .replace("@ROUTES@", ROUTES_RE).replace("@KEY@", key_re(keys)).replace("@TTL@", str(ttl)))


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
        self.nodes = []  # each cache's own base URL
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
        """Each cache's counters as varnishstat gives them: a list with one
        dict for each cache. A counter that is not in the output is an error."""
        nodes = []
        for d in self.cache_dirs:
            cmd = [os.environ["BENCH_VARNISHSTAT"], "-n", str(d), "-1"]
            for name in VSTAT:
                cmd += ["-f", f"MAIN.{name}"]
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            got = {}
            for line in out.splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[0].startswith("MAIN.") and parts[0][5:] in VSTAT:
                    got[parts[0][5:]] = int(parts[1])
            missing = [name for name in VSTAT if name not in got]
            if missing:
                raise RuntimeError(f"varnishstat for {d.name} gave no counter {missing}: {out!r}")
            nodes.append(got)
        return nodes

    def fetched(self):
        """Backend requests of each cache so far."""
        return [n["backend_req"] for n in self.stats()]

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
                (d / "cache.vcl").write_text(vcl(ttl, (bc.KEY, KEY2)))
                # -t: the freshness of an answer without one of its own.
                # http_gzip_support=off: Accept-Encoding goes to Alerta as the
                # client sent it, as in the arms without a cache.
                st.caches.append(subprocess.Popen(
                    [os.environ["BENCH_VARNISHD"], "-F", "-n", str(d), "-a", f"127.0.0.1:{vport}",
                     "-f", str(d / "cache.vcl"), "-s", "malloc,128m", "-t", str(ttl),
                     "-p", "http_gzip_support=off"],
                    stdout=logfile(f"varnish{i}"), stderr=subprocess.STDOUT))
                st.cache_dirs.append(d)
                st.nodes.append(f"http://127.0.0.1:{vport}")
            ports = [vport for _, vport in STACKS]
        cfg = bc.WORK / f"haproxy-{next(_dirs)}.cfg"
        cfg.write_text(haproxy_cfg(ports, by_credential=bool(ttl)))
        st.front = subprocess.Popen([os.environ["BENCH_HAPROXY"], "-db", "-f", str(cfg)],
                                    stdout=logfile("haproxy"), stderr=subprocess.STDOUT)
        # Readiness through the whole chain; a cache passes this request.
        bc.wait_ready(st.base, st.front)
        for node in st.nodes:
            bc.wait_ready(node, st.front)
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


def view(answer):
    """What a check compares: the status and, for a 200, the total with the
    ids in order (a list) or the counts by severity and status (a count)."""
    status, raw = answer
    if status != 200:
        return [status]
    try:
        body = json.loads(raw)
    except ValueError:
        return [status, "not JSON"]
    if "alerts" in body:
        return [status, body.get("total"), [a.get("id") for a in body["alerts"]]]
    return [status, body.get("total"), body.get("severityCounts"), body.get("statusCounts")]


def sql_view(bc, db, path, customer=None):
    """The database's own answer for a measured route, in view()'s form."""
    where = f" WHERE customer = '{customer}'" if customer else ""
    total = int(bc.psql(db, f"SELECT count(*) FROM alerts{where}"))
    if path.startswith(COUNT):
        groups = lambda col: bc.sql_json(
            db, f"SELECT json_object_agg({col}, n) FROM (SELECT {col}, count(*) AS n "
                f"FROM alerts{where} GROUP BY {col}) g") or {}
        return [200, total, groups("severity"), groups("status")]
    ids = bc.psql(db, f"SELECT id FROM alerts{where} ORDER BY last_receive_time DESC, id LIMIT 50")
    return [200, total, ids.split("\n") if ids else []]


def auth(key):
    return {"Authorization": f"Key {key}"}


def xkey(key):
    return {"X-API-Key": key}


# One Host for every request of a check, also when it goes to one cache
# directly: the host is part of the cache key.
HOST = {"Host": f"127.0.0.1:{FRONT_PORT}"}


# ---------------------------------------------------------------- the checks

def check_oracle(bc, st, db):
    """Alerta's direct answers against the database."""
    part = {"problems": [], "answers": []}
    for name, path, headers, customer in (
            ("admin count", COUNT, auth(bc.KEY), None), ("admin list", LIST, auth(bc.KEY), None),
            ("customer count", COUNT, auth(KEY2), CUSTOMER),
            ("customer list", LIST, auth(KEY2), CUSTOMER),
            ("customer count, X-API-Key", COUNT, xkey(KEY2), CUSTOMER)):
        got, want = view(get(st.direct + path, headers)), sql_view(bc, db, path, customer)
        part["answers"].append({"ask": name, "total": got[1] if len(got) > 1 else None,
                                "equals_database": got == want})
        if got != want:
            part["problems"].append(f"{name}: Alerta directly {str(got)[:200]}, database {str(want)[:200]}")
    for name, headers in (("wrong key", auth("wrong-key")), ("no key", {})):
        got = view(get(st.direct + COUNT, headers))
        if got != [401]:
            part["problems"].append(f"{name}: Alerta directly {got}, [401] expected")
    totals = [a["total"] for a in part["answers"]]
    if totals != [bc.SEED_ALERTS, bc.SEED_ALERTS, CUSTOMER_ALERTS, CUSTOMER_ALERTS, CUSTOMER_ALERTS]:
        part["problems"].append(f"totals {totals}")
    part["ok"] = not part["problems"]
    return part


def isolation_steps(a):
    """(name, path, headers, expected) in order, for the admin key `a`.
    `miss` and `pass` are one backend request; `hit` is none. The
    identities alternate, so a cache that mixed them would give one the
    other's answer. A `pass` is asked twice: a miss would make the second
    request a hit. That shows a pass only for a 200 answer, so the third
    key, which Alerta accepts, is the control of the credential rule."""
    c = KEY2
    steps = [("admin, Authorization, count", COUNT, auth(a), "miss"),
             ("customer, Authorization, count", COUNT, auth(c), "miss"),
             ("admin, Authorization, count", COUNT, auth(a), "hit"),
             ("customer, Authorization, count", COUNT, auth(c), "hit"),
             ("admin, X-API-Key, count", COUNT, xkey(a), "miss"),
             ("customer, X-API-Key, count", COUNT, xkey(c), "miss"),
             ("customer, X-API-Key, count", COUNT, xkey(c), "hit"),
             ("admin, X-API-Key, count", COUNT, xkey(a), "hit"),
             ("customer, Authorization, list", LIST, auth(c), "miss"),
             ("admin, Authorization, list", LIST, auth(a), "miss"),
             ("customer, Authorization, list", LIST, auth(c), "hit"),
             ("admin, Authorization, list", LIST, auth(a), "hit"),
             ("admin, X-API-Key, list", LIST, xkey(a), "miss"),
             ("customer, X-API-Key, list", LIST, xkey(c), "miss"),
             ("admin, X-API-Key, list", LIST, xkey(a), "hit"),
             ("customer, X-API-Key, list", LIST, xkey(c), "hit")]
    for name, path, headers in (
            (THIRD + ", Authorization, count", COUNT, auth(KEY3)),
            (THIRD + ", X-API-Key, count", COUNT, xkey(KEY3)),
            (THIRD + ", Authorization, list", LIST, auth(KEY3)),
            ("customer, api-key parameter", f"{COUNT}?api-key={c}", {}),
            ("admin Authorization with customer X-API-Key", COUNT, {**auth(a), **xkey(c)}),
            ("admin, Authorization, with a cookie", COUNT, {**auth(a), "Cookie": "a=b"}),
            ("admin, Authorization, a route not measured", "/alerts?page-size=5", auth(a)),
            ("wrong key", COUNT, auth("wrong-key")),
            ("no key", COUNT, {})):
        steps += [(name, path, headers, "pass")] * 2
    return steps


THIRD = "third valid key"


def required_steps():
    """What the gate wants of each cache: (ask, expected) in order."""
    return [[name, expect] for name, _, _, expect in isolation_steps("-")]


def check_isolation(bc, st, ttl):
    """Each cache alone: every answer equals Alerta's direct answer and
    every request is the expected miss, hit or pass."""
    part = {"problems": [], "nodes": []}
    steps = isolation_steps(bc.KEY)
    direct = {}
    for name, path, headers, _ in steps:
        key = (path, json.dumps(headers, sort_keys=True))
        if key not in direct:
            direct[key] = view(get(st.direct + path, headers))
    for i, node in enumerate(st.nodes):
        for attempt in range(1, 4):
            rows, problems, late, primed = [], [], False, {}
            for name, path, headers, expect in steps:
                key = (path, json.dumps(headers, sort_keys=True))
                before = st.fetched()[i]
                asked = time.monotonic()
                got = view(get(node + path, {**HOST, **headers}))
                fetched = st.fetched()[i] - before
                if expect == "miss":
                    primed[key] = asked
                elif expect == "hit" and time.monotonic() - primed[key] > ttl - 0.2:
                    late = True
                same = got == direct[key]
                rows.append({"ask": name, "expected": expect, "backend_requests": fetched,
                             "status": got[0], "equals_direct": same})
                if not same:
                    problems.append(f"cache {i}, {name} ({expect}): {str(got)[:160]} from the cache, "
                                    f"{str(direct[key])[:160]} directly")
                if fetched != (0 if expect == "hit" else 1):
                    problems.append(f"cache {i}, {name}: {fetched} backend request(s) for an "
                                    f"expected {expect}")
                if name.startswith(THIRD) and got[0] != 200:
                    problems.append(f"cache {i}, {name}: status {got[0]}; this control needs a "
                                    "200 to tell a pass from a miss")
            if not late:
                break
            # The runner was too slow for this expiry; start again with empty entries.
            time.sleep(ttl + 0.3)
        if late:
            problems.append(f"cache {i}: a repeat came later than the expiry in 3 attempts")
        part["nodes"].append({"cache": i, "attempts": attempt, "steps": rows,
                              "hits": sum(r["backend_requests"] == 0 for r in rows)})
        part["problems"] += problems
    part["ok"] = not part["problems"] and len(part["nodes"]) == len(STACKS)
    return part


def check_separation(bc, st):
    """Through HAProxy, as a client meets the caches."""
    part = {"problems": []}
    asks = [("admin, Authorization", COUNT, auth(bc.KEY)),
            ("customer, Authorization", COUNT, auth(KEY2)),
            ("admin, X-API-Key", COUNT, xkey(bc.KEY)),
            ("customer, X-API-Key", COUNT, xkey(KEY2)),
            ("customer, api-key parameter", f"{COUNT}?api-key={KEY2}", {}),
            ("wrong key", COUNT, auth("wrong-key")),
            ("no key", COUNT, {})]
    direct = [view(get(st.direct + path, h)) for _, path, h in asks]
    rounds = [[view(get(st.base + path, h)) for _, path, h in asks]]
    before = st.fetched()
    rounds.append([view(get(st.base + path, h)) for _, path, h in asks])
    hits = len(asks) - (sum(st.fetched()) - sum(before))
    for n, answers in enumerate(rounds):
        for (name, _, _), got, want in zip(asks, answers, direct):
            if got != want:
                part["problems"].append(f"round {n + 1}, {name}: {str(got)[:160]} through the "
                                        f"cache, {str(want)[:160]} directly")
    # Only the two Authorization forms with a good key are sure to meet the
    # same cache twice: HAProxy sends a request without that header to the
    # caches in turn.
    if hits < 2:
        part["problems"].append(f"{hits} answers of the second round came from the cache, "
                                "2 or more expected")
    part.update({"asks": [name for name, _, _ in asks], "totals_direct": [d[1:2] for d in direct],
                 "cache_hits": hits, "ok": not part["problems"]})
    return part


def settle(targets, headers, path, old, new, ttl, t0):
    """Ask every target in turn until all gave `new` and then for one more
    expiry. Every answer must be `old` or `new`; a target must give `new`
    within the limit and never `old` after it."""
    limit = ttl + STALE_MARGIN_S
    res = {name: {"observations": 0, "old_answers": 0, "last_old_s": None, "new_after_s": None}
           for name in targets}
    problems, stop_at = [], None
    while True:
        for name, base in targets.items():
            got = view(get(base + path, {**HOST, **headers}))
            t, r = time.monotonic() - t0, res[name]
            r["observations"] += 1
            if got == new:
                if r["new_after_s"] is None:
                    r["new_after_s"] = t
            elif got == old:
                r["old_answers"] += 1
                r["last_old_s"] = t
                if r["new_after_s"] is not None:
                    problems.append(f"{name}: the old answer at {t:.2f} s, after the new one "
                                    f"at {r['new_after_s']:.2f} s")
            else:
                problems.append(f"{name}: at {t:.2f} s an answer that is not the old or the "
                                f"new one: {str(got)[:160]}")
        now = time.monotonic()
        if stop_at is None and all(r["new_after_s"] is not None for r in res.values()):
            stop_at = now + ttl + 1
        if (stop_at is not None and now >= stop_at) or now - t0 > 2 * ttl + 10:
            break
        time.sleep(0.05)
    for name, r in res.items():
        if r["new_after_s"] is None or r["new_after_s"] > limit:
            problems.append(f"{name}: the new answer came after {r['new_after_s']!r} s, limit {limit} s")
    return res, problems[:12]


def targets_of(st):
    return {**{f"cache {i}": node for i, node in enumerate(st.nodes)}, "front": st.base}


def check_staleness(bc, st, ttl, db):
    """Both caches and the front hold the count; then a write."""
    part = {"problems": [], "limit_s": ttl + STALE_MARGIN_S}
    headers, targets = auth(bc.KEY), targets_of(st)
    old = sql_view(bc, db, COUNT)
    for name, base in targets.items():
        got = view(get(base + COUNT, {**HOST, **headers}))
        if got != old:
            part["problems"].append(f"{name} before the write: {got}, database {old}")
    alert = {"resource": "cache-probe", "event": "Stale", "environment": "Production",
             "severity": "minor", "service": ["web"], "text": "staleness probe"}
    t0 = time.monotonic()
    status, _, _ = bc.http("POST", st.base + "/alert", json.dumps(alert))
    new = sql_view(bc, db, COUNT)
    if status != 201 or new[1] != old[1] + 1:
        part["problems"].append(f"the write answered {status}; total {old[1]} -> {new[1]}")
    part["targets"], problems = settle(targets, headers, COUNT, old, new, ttl, t0)
    part["problems"] += problems
    part.update({"write_status": status, "total_before": old[1], "total_after": new[1],
                 "ok": not part["problems"]})
    return part


def check_burst(bc, st, ttl):
    """One new URL, BURST requests at the same moment."""
    url, answers = st.base + LIST + f"&_=burst{ttl}", [None] * BURST
    gate_ = threading.Barrier(BURST)

    def one(i):
        gate_.wait()
        answers[i] = get(url, auth(bc.KEY))

    before = st.fetched()
    threads = [threading.Thread(target=one, args=(i,)) for i in range(BURST)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fetched = [b - a for a, b in zip(before, st.fetched())]
    same = len(set(answers)) == 1 and answers[0] is not None and answers[0][0] == 200
    part = {"requests": BURST, "backend_requests": sum(fetched), "backend_requests_by_cache": fetched,
            "answers_equal_200": same, "problems": []}
    if sum(fetched) != 1 or not same:
        part["problems"].append(f"{BURST} requests gave {fetched} backend requests, "
                                f"equal 200 answers: {same}")
    part["ok"] = not part["problems"]
    return part


def check_revocation(bc, st, ttl, db):
    """A deleted key and its cached answer: for each target the last 200
    and the first 401 that the check saw, from a time taken before the
    deletion. Last: it removes the key."""
    part = {"problems": [], "limit_s": ttl + STALE_MARGIN_S}
    headers, targets = auth(KEY2), targets_of(st)
    old = sql_view(bc, db, COUNT, CUSTOMER)
    for name, base in targets.items():
        got = view(get(base + COUNT, {**HOST, **headers}))
        if got != old:
            part["problems"].append(f"{name} before the deletion: {got}, database {old}")
    t0 = time.monotonic()
    bc.psql(db, "DELETE FROM keys WHERE id = 'bench-key2'")
    direct = view(get(st.direct + COUNT, headers))
    if direct != [401]:
        part["problems"].append(f"after the deletion Alerta answers directly {direct}, [401] expected")
    part["targets"], problems = settle(targets, headers, COUNT, old, [401], ttl, t0)
    part["problems"] += problems
    # Both are times after t0, which is before the deletion. The stale
    # answers of a target ended between its two observations.
    part["observed"] = {name: {"last_200_s": r["last_old_s"], "first_401_s": r["new_after_s"]}
                        for name, r in part["targets"].items()}
    part["ok"] = not part["problems"]
    return part


def revocation_text(part):
    """`cache 0: last 200 at 0.96 s, first 401 at 1.04 s; ...`"""
    sec = lambda v: "not observed" if v is None else f"at {v:.3f} s"
    return "; ".join(f"{name}: last 200 {sec(o['last_200_s'])}, first 401 {sec(o['first_401_s'])}"
                     for name, o in part["observed"].items())


def probe(bc, ttl, db, rec):
    """Every check of one expiry. True when all pass."""
    arm = f"alerta-cache-{ttl}s"
    p = {"ttl_s": ttl, "ok": False, "problems": []}
    rec["probes"].append(p)
    bc.psql(db, "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, customer) "
                f"VALUES ('bench-key2', '{KEY2}', 'bench2', ARRAY['read'], 'benchmark customer', "
                f"timezone('UTC', now()) + interval '2 days', 0, '{CUSTOMER}'), "
                f"('bench-key3', '{KEY3}', 'bench3', ARRAY['read'], 'benchmark third key', "
                f"timezone('UTC', now()) + interval '2 days', 0, '{CUSTOMER}')")
    bc.psql(db, f"UPDATE alerts SET customer = '{CUSTOMER}' WHERE resource < 'host-{CUSTOMER_ALERTS:04d}'")
    st = start_stack(bc, arm, db, f"probe-{ttl}s",
                     {"CUSTOMER_VIEWS": "True", "ADMIN_USERS": "bench"})
    try:
        p["oracle"] = check_oracle(bc, st, db)
        p["isolation"] = check_isolation(bc, st, ttl)
        p["separation"] = check_separation(bc, st)
        p["staleness"] = check_staleness(bc, st, ttl, db)
        p["burst"] = check_burst(bc, st, ttl)
        p["revocation"] = check_revocation(bc, st, ttl, db)
        if not st.alive():
            p["problems"].append("a process of the stack exited")
    finally:
        st.stop(bc)
    for part in PROBE_PARTS:
        p["problems"] += [f"{part}: {x}" for x in p.get(part, {}).get("problems", ["not run"])]
    p["ok"] = not p["problems"]
    news = [r["new_after_s"] for r in p["staleness"]["targets"].values()]
    bc.log(f"cache probe {ttl}s: {'ok' if p['ok'] else 'FAILED ' + str(p['problems'])}; "
           f"new count after {news!r} s; deleted key, {revocation_text(p['revocation'])}; "
           f"burst {p['burst']['backend_requests']!r} backend request(s)")
    return p["ok"]


def sane(bc, st, arm, rep, db, rec):
    """The arm serves the database's data on the measured read routes,
    twice; on a cache arm the second round is from the cache."""
    problems, h, cached = [], auth(bc.KEY), bool(ttl_of(arm))
    wanted = {path: sql_view(bc, db, path) for path in SANE_PATHS}
    direct = {path: view(get(st.direct + path, h)) for path in SANE_PATHS}
    for path in SANE_PATHS:
        if direct[path] != wanted[path]:
            problems.append(f"{path}: the server directly differs from the database")
    known_hits = None
    for attempt in range(1, 4):
        late, t0 = False, time.monotonic()
        first = {path: view(get(st.base + path, h)) for path in SANE_PATHS}
        before = st.fetched()
        second = {path: view(get(st.base + path, h)) for path in SANE_PATHS}
        if cached:
            late = time.monotonic() - t0 > ttl_of(arm) - 0.2
            known_hits = len(SANE_PATHS) - (sum(st.fetched()) - sum(before))
        if not late:
            break
        time.sleep(ttl_of(arm) + 0.3)
    for n, answers in enumerate((first, second)):
        for path in SANE_PATHS:
            if answers[path] != wanted[path]:
                problems.append(f"{path} (request {n + 1}): total {answers[path][1:2]}, differs "
                                f"from the database (total {wanted[path][1]})")
    if cached and known_hits != len(SANE_PATHS):
        problems.append(f"{known_hits} of {len(SANE_PATHS)} repeated requests came from the cache")
    rec["checks"].append({"arm": arm, "rep": rep, "ok": not problems, "problems": problems,
                          "known_hits": known_hits, "attempts": attempt})
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
        cmd += ["-m", "GET", st.base + {"list": LIST, "count": COUNT}[case]]
    if out_file.exists():
        raise RuntimeError(f"{out_file.name} exists: two cells would share one raw sample")
    r = subprocess.run(cmd, capture_output=True, text=True)
    out_file.write_text(r.stdout)
    if r.returncode != 0:
        raise RuntimeError(f"oha exited {r.returncode}: {r.stderr[-500:]}")
    return json.loads(r.stdout)


QUIET_READS, QUIET_LIMIT, QUIET_WAIT_S = 10, 50, 0.1


def settled(read, wait, quiet=QUIET_READS, limit=QUIET_LIMIT):
    """Reads the counters at one boundary of a timed run, with no load.
    Varnish adds a worker thread's numbers to the totals when the thread
    ends a task and the lock is free (its `thread_stats_rate` text), so a
    read just after the load tool stops can miss the last requests (run
    44903568987, attempt 1: 1932 of 1933), and two equal reads do not show
    that no more is to come. So the counters must rest: `quiet` reads in a
    row equal, within `limit` reads. Returns (the last read, history,
    resolved). The history has the first read and every changed read, each
    with the number of its read. A change later than the rest interval is
    not seen; the exact rule then fails the cell, or passes it wrongly by
    the size of that change, and the saved history is the evidence."""
    last = read()
    history, same = [{"read": 1, "counters": last}], 1
    for n in range(2, limit + 1):
        if same >= quiet:
            break
        wait()
        now = read()
        if now == last:
            same += 1
        else:
            last, same = now, 1
            history.append({"read": n, "counters": now})
    return last, history, same >= quiet


def counter_problems(case, requests, before, after):
    """The raw counters of each cache for one timed run, checked as numbers
    before any ratio. Returns (per-cache deltas, backend requests, problems)."""
    problems = []
    delta = [{k: b[k] - a[k] for k in VSTAT} for a, b in zip(before, after)]
    if len(delta) != len(STACKS):
        problems.append(f"counters of {len(delta)} caches, {len(STACKS)} expected")
    for i, d in enumerate(delta):
        down = {k: v for k, v in d.items() if v < 0}
        if down:
            problems.append(f"cache {i}: counters went down: {down}")
    fetched = sum(d["backend_req"] for d in delta)
    if case in ("count", "list"):
        if not 0 <= fetched <= 0.1 * requests:
            problems.append(f"{fetched} backend requests for {requests} requests of one URL; "
                            "one tenth or fewer expected")
    elif fetched != requests:
        why = ("the suite does not see the URLs, so it cannot say that a repeated URL explains it"
               if case == "list-unique" else "the load tool waits for its last answers")
        problems.append(f"{fetched} backend requests for {requests} requests that a cache must "
                        f"not answer; equal numbers expected ({why})")
    return delta, fetched, problems


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
    # Both boundaries of the timed run are read the same way. A late count
    # of the warm-up must not fall into the timed run's difference.
    pause = lambda: time.sleep(QUIET_WAIT_S)
    v0, history0, resolved0 = settled(st.stats, pause)
    cpu0 = st.cpu(bc)
    t0 = time.monotonic()
    j = oha(bc, st, case, conc, bc.TIMED_S, raw / f"cache-timed-{arm}-{case}-c{conc}-r{rep}.json",
            json_flag)
    wall = time.monotonic() - t0
    cpu1 = st.cpu(bc)
    # No request is under way now: the load tool waited for its last answers.
    v1, history1, resolved1 = settled(st.stats, pause)
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
              "alive": alive})
    for part in cpu0:
        s[f"{part}_cpu_ms_per_request"] = 1000 * (cpu1[part] - cpu0[part]) / hz / req
    if ttl_of(arm):
        # Only backend_req is in a gate.
        delta, fetched, found = counter_problems(case, s["requests"], v0, v1)
        problems += found
        for name, resolved in (("before", resolved0), ("after", resolved1)):
            if not resolved:
                problems.append(f"unresolved: the caches' counters did not rest for {QUIET_READS} reads "
                                f"within {QUIET_LIMIT} reads {name} the timed run")
        s["cache_nodes"] = {"before": v0, "after": v1, "delta": delta,
                            "before_first": history0[0]["counters"], "after_first": history1[0]["counters"],
                            "before_history": history0, "after_history": history1,
                            "resolved": [resolved0, resolved1],
                            "rest": {"reads": QUIET_READS, "limit": QUIET_LIMIT, "wait_s": QUIET_WAIT_S}}
        s["backend_requests"] = fetched
        s["hit_ratio"] = 1 - fetched / req
    s["problems"] = problems
    s["valid"] = (alive and s["errors"] == 0 and s["non_2xx"] == 0 and s["ok"] > 0
                  and warm["errors"] == 0 and warm["non_2xx"] == 0 and not problems)
    rec["cells"].append(s)
    ratio = "" if s.get("hit_ratio") is None else (f", hit ratio {s['hit_ratio']:.4f}, backend "
                                                   f"requests {s['backend_requests']}")
    bc.log(f"cache {arm:16} {case:11} c={conc} r{rep}: {'' if s['valid'] else 'INVALID '}"
           f"{s['rps']:.1f} rps, p50 {s['p50_ms']:.2f} ms, p99 {s['p99_ms']:.2f} ms, errors "
           f"{s['errors']}, non-2xx {s['non_2xx']}{ratio}"
           + (f", problems {problems}" if problems else ""))
    return alive


# ------------------------------------------------------------------ the gate

def gate(rec, planned):
    """What a complete, passed suite has. Anything absent is named."""
    g = {"expected": len(planned), "missing": [], "invalid": [], "duplicate": [],
         "missing_checks": []}
    for ttl in TTLS:
        probes = [p for p in rec.get("probes", []) if p.get("ttl_s") == ttl]
        if len(probes) != 1:
            g["missing_checks"].append(f"expiry {ttl} s: {len(probes)} check records, 1 expected")
            continue
        p = probes[0]
        for part in PROBE_PARTS:
            if not isinstance(p.get(part), dict) or p[part].get("ok") is not True:
                g["missing_checks"].append(f"expiry {ttl} s: {part} absent or not passed")
        nodes = (p.get("isolation") or {}).get("nodes") or []
        if len(nodes) != len(STACKS) or not all(n.get("hits", 0) > 0 for n in nodes):
            g["missing_checks"].append(f"expiry {ttl} s: isolation not shown on each cache")
        for n in nodes:
            rows = n.get("steps") or []
            if [[r.get("ask"), r.get("expected")] for r in rows] != required_steps():
                g["missing_checks"].append(f"expiry {ttl} s, cache {n.get('cache')}: the isolation "
                                           "steps are not the required ones")
            elif not all(r.get("equals_direct") is True
                         and r.get("backend_requests") == (0 if r["expected"] == "hit" else 1)
                         and (r.get("status") == 200 or not r["ask"].startswith(THIRD))
                         for r in rows):
                g["missing_checks"].append(f"expiry {ttl} s, cache {n.get('cache')}: an isolation "
                                           "step without its expected result")
        seen = (p.get("revocation") or {}).get("observed") or {}
        if len(seen) != len(STACKS) + 1 or not all(
                isinstance(o.get("first_401_s"), float) and "last_200_s" in o for o in seen.values()):
            g["missing_checks"].append(f"expiry {ttl} s: no first 401 recorded for each cache and "
                                       "the front")
        for part in ("staleness", "revocation"):
            if len((p.get(part) or {}).get("targets") or {}) != len(STACKS) + 1:
                g["missing_checks"].append(f"expiry {ttl} s: {part} not observed on each cache "
                                           "and the front")
        if p.get("ok") is not True:
            g["missing_checks"].append(f"expiry {ttl} s: the checks did not pass")
    for arm, rep in sorted({(a, r) for a, _, _, r in planned}):
        checks = [c for c in rec.get("checks", []) if (c.get("arm"), c.get("rep")) == (arm, rep)]
        if len(checks) != 1 or checks[0].get("ok") is not True:
            g["missing_checks"].append(f"data check of {arm}, repetition {rep}, absent or not passed")
        elif ttl_of(arm) and checks[0].get("known_hits") != len(SANE_PATHS):
            g["missing_checks"].append(f"data check of {arm}, repetition {rep}: no known cache hit")
    cells = {}
    for c in rec.get("cells", []):
        key = (c["arm"], c["case"], c["c"], c["rep"])
        if key in cells:
            g["duplicate"].append(list(key))
        cells[key] = c
    for key in planned:
        c = cells.get(key)
        if c is None:
            g["missing"].append(list(key))
        elif c.get("valid") is not True:
            g["invalid"].append(list(key))
        elif ttl_of(key[0]):
            nodes = c.get("cache_nodes") or {}
            if any(len(nodes.get(k) or []) != len(STACKS) for k in ("before", "after", "delta")) \
                    or "backend_requests" not in c:
                g["missing_checks"].append(f"cell {list(key)}: no counters of each cache")
    g["ok"] = not (g["missing"] or g["invalid"] or g["duplicate"] or g["missing_checks"])
    return g


def gate_self_test(planned):
    """The gate must pass a complete record and refuse it when any required
    item is removed. Returns the cases; raises when the gate is wrong."""
    node = {"hits": 1, "cache": 0,
            "steps": [{"ask": ask, "expected": expect, "equals_direct": True, "status": 200,
                       "backend_requests": 0 if expect == "hit" else 1}
                      for ask, expect in required_steps()]}
    xlist = [i for i, (ask, _) in enumerate(required_steps()) if ask.endswith("X-API-Key, list")]
    third = next(i for i, (ask, _) in enumerate(required_steps()) if ask.startswith(THIRD))
    seen = {name: {"last_200_s": None, "first_401_s": 1.0} for name in ("cache 0", "cache 1", "front")}
    counters = [dict.fromkeys(VSTAT, 0) for _ in STACKS]
    targets = {name: {} for name in ("cache 0", "cache 1", "front")}
    full = {"probes": [{"ttl_s": ttl, "ok": True,
                        **{part: {"ok": True} for part in PROBE_PARTS},
                        "isolation": {"ok": True, "nodes": [copy.deepcopy(node) for _ in STACKS]},
                        "staleness": {"ok": True, "targets": dict(targets)},
                        "revocation": {"ok": True, "targets": dict(targets),
                                       "observed": copy.deepcopy(seen)}} for ttl in TTLS],
            "checks": [{"arm": a, "rep": r, "ok": True, "known_hits": len(SANE_PATHS)}
                       for a, r in sorted({(a, r) for a, _, _, r in planned})],
            "cells": [{"arm": a, "case": case, "c": c, "rep": r, "valid": True,
                       "backend_requests": 0,
                       "cache_nodes": {"before": counters, "after": counters, "delta": counters}}
                      for a, case, c, r in planned]}
    cached = next(i for i, c in enumerate(full["cells"]) if ttl_of(c["arm"]))
    cached_check = next(i for i, c in enumerate(full["checks"]) if ttl_of(c["arm"]))

    def without(path):
        def change(rec):
            target = rec
            for step in path[:-1]:
                target = target[step]
            del target[path[-1]]
        return change

    def put(path, value):
        def change(rec):
            target = rec
            for step in path[:-1]:
                target = target[step]
            target[path[-1]] = value
        return change

    mutations = [("an expiry's check record removed", without(["probes", 0])),
                 ("an expiry's checks not passed", put(["probes", 1, "ok"], False)),
                 *[(f"{part} removed", without(["probes", 2, part])) for part in PROBE_PARTS],
                 *[(f"{part} not passed", put(["probes", 0, part, "ok"], False))
                   for part in PROBE_PARTS],
                 ("isolation on one cache only", without(["probes", 1, "isolation", "nodes", 1])),
                 ("isolation with no hit on a cache", put(["probes", 1, "isolation", "nodes", 0, "hits"], 0)),
                 ("the X-API-Key list steps removed on one cache", lambda rec: [
                     rec["probes"][0]["isolation"]["nodes"][1]["steps"].pop(i) for i in reversed(xlist)]),
                 ("one X-API-Key list step removed", without(
                     ["probes", 2, "isolation", "nodes", 0, "steps", xlist[0]])),
                 ("the third key's step removed", without(
                     ["probes", 1, "isolation", "nodes", 0, "steps", third])),
                 ("the third key answered from the cache", put(
                     ["probes", 1, "isolation", "nodes", 1, "steps", third + 1, "backend_requests"], 0)),
                 ("the third key refused by Alerta", put(
                     ["probes", 1, "isolation", "nodes", 1, "steps", third, "status"], 401)),
                 ("a hit that reached Alerta", put(
                     ["probes", 0, "isolation", "nodes", 0, "steps", 2, "backend_requests"], 1)),
                 ("an answer that differs from Alerta's", put(
                     ["probes", 0, "isolation", "nodes", 0, "steps", 0, "equals_direct"], False)),
                 ("revocation without its observations", without(["probes", 0, "revocation", "observed"])),
                 ("revocation with no first 401 on a target", put(
                     ["probes", 2, "revocation", "observed", "cache 1", "first_401_s"], None)),
                 ("staleness without a target", without(["probes", 1, "staleness", "targets", "cache 1"])),
                 ("revocation without a target", without(["probes", 1, "revocation", "targets", "front"])),
                 ("a data check removed", without(["checks", 0])),
                 ("a data check not passed", put(["checks", 0, "ok"], False)),
                 ("a data check without a known hit", put(["checks", cached_check, "known_hits"], 0)),
                 ("a cell removed", without(["cells", 0])),
                 ("a cell not valid", put(["cells", 0, "valid"], False)),
                 ("a cell twice", lambda rec: rec["cells"].append(dict(rec["cells"][0]))),
                 ("a cell without counters", without(["cells", cached, "cache_nodes"])),
                 ("a cell with one cache's counters",
                  put(["cells", cached, "cache_nodes", "delta"], counters[:1])),
                 ("a cell without its backend requests", without(["cells", cached, "backend_requests"]))]
    cases = [{"case": "complete record", "gate_must_pass": True, "gate_passed": gate(full, planned)["ok"]}]
    for name, change in mutations:
        rec = copy.deepcopy(full)
        change(rec)
        cases.append({"case": name, "gate_must_pass": False, "gate_passed": gate(rec, planned)["ok"]})
    # The counter rule of a cell, on numbers.
    zero = [dict.fromkeys(VSTAT, 0) for _ in STACKS]
    def after(n):
        return [dict(zero[0], backend_req=n), *zero[1:]]
    for name, case, requests, fetched, good in (
            ("counters: writes all passed", "write-dup", 100, 100, True),
            ("counters: one write not passed", "write-dup", 100, 99, False),
            ("counters: one backend request more than writes", "write-dup", 100, 101, False),
            ("counters: new URLs all fetched", "list-unique", 100, 100, True),
            ("counters: one new URL not fetched", "list-unique", 100, 99, False),
            ("counters: a repeated URL from the cache", "count", 100, 3, True),
            ("counters: a repeated URL fetched too often", "count", 100, 11, False),
            ("counters: a counter that went down", "count", 100, -1, False)):
        cases.append({"case": name, "gate_must_pass": good,
                      "gate_passed": not counter_problems(case, requests, zero, after(fetched))[2]})
    # The two boundaries of a cell with 1933 requests that no cache may
    # answer, on made-up reads: `start` and `end` are the backend_req
    # values of the reads in order (the last one repeats). The rule is the
    # cell's: both boundaries resolved, and the exact counter rule.
    def reads_of(values):
        left = list(values)
        return lambda: after(left.pop(0) if len(left) > 1 else left[0])
    def boundary_rule(start, end):
        v0, _, r0 = settled(reads_of(start), lambda: None)
        v1, h1, r1 = settled(reads_of(end), lambda: None)
        return r0 and r1 and not counter_problems("write-dup", 1933, v0, v1)[2], h1
    for name, start, end, good in (
            ("rest: flat at once", (0,), (1933,), True),
            ("rest: flat, then the late count arrives", (0,), (1932, 1932, 1933), True),
            ("rest: a shortage that stays", (0,), (1932,), False),
            ("rest: flat at the right number, then an excess", (0,), (1933, 1933, 1934), False),
            ("rest: an excess after nine equal reads", (0,), (1933,) * 9 + (1934,), False),
            ("rest: a late count of the warm-up is not the timed run's", (99, 99, 100), (2033,), True),
            ("rest: a late count of the warm-up, and a shortage", (99, 99, 100), (2032,), False),
            ("rest: counters that never rest at the end", (0,), tuple(range(1900, 1960)), False),
            ("rest: counters that never rest at the start", tuple(range(60)), (1993,), False)):
        cases.append({"case": name, "gate_must_pass": good, "gate_passed": boundary_rule(start, end)[0]})
    history = boundary_rule((0,), (1932, 1932, 1933))[1]
    cases.append({"case": "rest: the history keeps the first read and each change",
                  "gate_must_pass": True,
                  "gate_passed": [(h["read"], h["counters"][0]["backend_req"]) for h in history]
                  == [(1, 1932), (3, 1933)]})
    wrong = [c["case"] for c in cases if c["gate_passed"] != c["gate_must_pass"]]
    if wrong:
        raise RuntimeError(f"the cache suite's gate is wrong for: {wrong}")
    return cases


def run(bc, record, dbs):
    """Checks, then cells. True when the gate passes; results are in
    record["cache"]."""
    def version(name, flag):
        r = subprocess.run([os.environ[name], flag], capture_output=True, text=True)
        return " ".join((r.stdout + r.stderr).strip().split("\n")[0].split())

    planned = plan(bc)
    rec = record["cache"] = {
        "arms": ARMS, "few_arms": FEW_ARMS, "ttls_s": TTLS, "concurrency": concurrency(bc),
        "alerta_per_stack": f"{max(1, bc.VCPUS // len(STACKS))}x4-pgbu-nofwd",
        "varnish": version("BENCH_VARNISHD", "-V"), "haproxy": version("BENCH_HAPROXY", "-v"),
        "routes": ROUTES_RE, "credentials": "the two synthetic keys, whole", "stale_margin_s": STALE_MARGIN_S,
        "gate_self_test": gate_self_test(planned), "probes": [], "checks": [], "cells": []}
    bc.log(f"cache suite: gate self-test, {len(rec['gate_self_test'])} cases, as expected")
    ok = True
    for ttl in TTLS:
        if not probe(bc, ttl, dbs[("cache", "probe", ttl)], rec):
            ok = False
    json_flag = bc.oha_json_flag()
    for rep in range(bc.REPS if ok else 0):
        order = ARMS[rep % len(ARMS):] + ARMS[:rep % len(ARMS)]
        for arm in order:
            st = start_stack(bc, arm, dbs[("cache", "read")], f"{arm}-r{rep}-read")
            try:
                if not sane(bc, st, arm, rep, dbs[("cache", "read")], rec):
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
    rec["gate"] = gate(rec, planned)
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
             "the runner's CPUs.", "",
             "The cache rules are a laboratory set: GET of the two measured routes, with one of "
             "the benchmark's two synthetic keys, whole, in one header. **A hit does not reach Alerta.** For "
             "up to the expiry, a stored answer is given without Alerta's check of the key "
             "(expiry, deletion) and of the customer scope, and without the update of the key's "
             "use count and last-used time; the CPU numbers of the cache arms include that "
             "avoided work. `deleted key` below gives, for each cache and the front, the last 200 "
             "and the first 401 that the check saw, from a time taken before the deletion; the "
             "stale answers ended between the two.", ""]
    if g:
        lines += [f"Cells: {g['expected']} planned, {len(g['missing'])} missing, "
                  f"{len(g['invalid'])} invalid, {len(g['duplicate'])} twice; "
                  f"{len(g['missing_checks'])} required checks absent or failed. Gate self-test: "
                  f"{len(rec['gate_self_test'])} cases as expected.", ""]
        lines += [f"- GATE: {m}" for m in g["missing_checks"][:30]]
    for p in rec["probes"]:
        if not p["ok"]:
            lines.append(f"- FAILED checks, expiry {p['ttl_s']} s: {p['problems'][:12]}")
            continue
        iso, s, b, v = p["isolation"], p["staleness"], p["burst"], p["revocation"]
        steps = sum(len(n["steps"]) for n in iso["nodes"])
        hits = sum(n["hits"] for n in iso["nodes"])
        new = max(t["new_after_s"] for t in s["targets"].values())
        old = sum(t["old_answers"] for t in s["targets"].values())
        lines.append(
            f"- expiry {p['ttl_s']} s: isolation, {steps} requests on the two caches one at a "
            f"time, each answer as Alerta's and each the expected miss, hit or pass ({hits} hits); "
            f"through HAProxy {len(p['separation']['asks'])} credential forms twice "
            f"({p['separation']['cache_hits']} from the cache); after a write every cache and the "
            f"front gave the new count within {new:.2f} s (limit {s['limit_s']} s; {old} old "
            f"answers before, none after); {b['requests']} requests at once gave "
            f"{b['backend_requests']} backend request; deleted key, {revocation_text(v)} "
            f"(limit for the first 401: {v['limit_s']} s).")
    for c in rec["checks"]:
        if not c["ok"]:
            lines.append(f"- FAILED data check, {c['arm']} r{c['rep']}: {c['problems']}")
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
                fetched = f"{med('backend_requests'):.0f}" if cached else ""
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
