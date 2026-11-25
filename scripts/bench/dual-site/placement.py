#!/usr/bin/env python3
"""Two data sites, one PostgreSQL primary: where must Alerta run, and what
does the add-on's `noecho` patch give there?

A diagnostic on one GitHub-hosted runner, synthetic data only. The numbers
are relative to that runner and to the emulated links; they are not a
capacity claim. The lab is run_lab.py's (imported unchanged): three etcd
members (site C has only that), Patroni with PostgreSQL at sites A and B
(A is the primary, B an asynchronous standby), and at each data site
HAProxy, PgBouncer and two Alerta processes. hosted-netem.sh gives the
links between the sites their delay, jitter, loss and rate.

Arms. All three use scripts/bench/alerta_fast.py (the blob in ADDON_BLOB)
and Alerta's default plugin list, `forwarder` included, with no forwarder
destination:

  L  each site's users go to the Alerta of their own site; site B's Alerta
     reaches the primary across the SQL link. ALERTA_FAST=safe.
  P  each site's users go to their own site's HAProxy, which sends the
     request to the site of the current primary (haproxy-affinity.cfg);
     site B's requests cross the HTTP link. ALERTA_FAST=safe.
  N  P's route. ALERTA_FAST=safe,noecho.

L and P differ only in the route. P and N differ only in `noecho`.

Stages (the design is Codex's, mail 20261121T182255Z-codex-alrust-c3594b37):

  preflight   On copies of the seeded database: (1) fastpath_check.py, the
              differential check of `noecho` (38 cases); (2) for Alerta as
              released, `safe` and `safe,noecho`: 1 create and 20
              duplicates over HTTP, and the row updates of `alerts` that
              they cause (62, 62 and 20 wanted); (3) for the same three:
              an attribute PUT with the stored value, with a new value and
              with null (removes the key; another key stays), and an `ack`
              action. The three must agree on every HTTP status and on the
              stored result. A failure stops the run before any timing.
  placement   Blocks of L, P and N, the order rotated (L/P/N, P/N/L,
              N/L/P). Each arm of a block gets a new copy of the database
              and the same requests at the same offsets: per 50 s, 160
              dashboard reads and 40 writes (36 duplicates of stored
              alerts, 4 new alerts), half from each site.
  fresh       Blocks of P and N in both orders. The same reads; of the 40
              writes 20 new alerts, 12 duplicates and 8 changes (two
              alerts get, in this order: a new attribute value, the same
              value again, null for that key, an `ack` action).

A dashboard read is one request of a tenant's key: GET /alerts (newest
first, one page) or GET /alerts/count, in turn. Every key belongs to one
customer (CUSTOMER_VIEWS). Requests are sent at fixed times, 4 per second;
a late answer does not delay the next request. The writes of one tenant
are sent one after the other (a write waits for the tenant's earlier
write), so the order of that tenant's changes is known. No request is sent
a second time.

Checks on every arm run (a failed check makes the run, and its block,
rejected; rejected blocks stay in the results, apart):
  - every answer has the wanted status and shape, and a read has only the
    tenant's alerts;
  - every read agrees with the tenant's committed alerts. The states that
    a read can see are those between the writes that ended before the
    read began and the writes that began before it ended. With `safe`
    (`onecount`) one statement gives the counts by severity and by
    status, so the two must be the counts of ONE state (the severities
    are those sent; the status is `open`, or `ack` after that change).
    GET /alerts/count has no other condition: its counts must be a
    state's counts, with every alert. GET /alerts has the condition
    `last_receive_time <= <the time the read arrived>` in its count
    statement and in its page statement, which runs later. So the check
    looks for one position of that time in the tenant's writes, one state
    for the count statement and the same or a later state for the page;
    only the alerts that a write received after that time are absent
    from a statement that sees that write. Such a read shows neither the
    old nor the new alert. It is accepted and counted
    (`reads_missing_an_alert`), because Alerta as released does this; the
    count is a result, and the ledger keeps its page and counts;
  - after the last request the model's order is the database's order;
  - every write with an answer is stored: each alert has the wanted
    number of duplicates, severity and status, and there is no other
    alert (`persisted`);
  - before the run's database is removed, site B has replayed the
    primary's last flushed WAL position and has the same alerts;
  - P and N: every answer came from an Alerta of site A, by the proxy of
    the user's site; L: from an Alerta of the user's site (`app_site` is
    the site of the HAProxy in front of the Alerta that answered,
    `proxy_site` the site that the user's HAProxy sent the request to);
  - the four Alerta processes got the wanted PLUGINS and ALERTA_FAST
    (read from /proc/<pid>/environ);
  - the use counts of the keys and Alerta's timers `received`, `queries`
    and `counts` grew by the number of requests answered;
  - the changed alerts have the wanted attributes, status and history;
  - the arms of a block end with equal alerts (customer, resource,
    severity, status, duplicate count, sorted tags, attributes as jsonb
    text, history types).

Not measured here: failover and recovery, a response cache, a larger
table. Limit of the checks: the commit of an earlier statement of the same
request is checked by fastpath_check.py, not over HTTP.

Credentials. All are made for one run and end with it. Patroni gets the
PostgreSQL passwords in its environment, PgBouncer a file with the role's
SCRAM verifier (for its owner only, in the lab's private directory), psql
the tenant keys on standard input.

Output. The run writes into a private directory beside --output (mode
0700, not uploaded, removed at the end). --output starts with a notice
that the run did not finish. At the end every file is read once, searched
for the credentials, and those same bytes are written to --output; a file
with a credential is not copied (a notice takes its place) and the run is
not ok (`credential_scan`). A packed file that cannot be unpacked to its
end, a file that cannot be read and a link are not copied either.
results.json and summary.md are searched as the bytes that are published:
a credential in them is replaced by `[REDACTED]`, the errors and problems
stay, and the run is not ok. A run that is stopped before this leaves only
the notice. The searched values are the password, the keys, and the
role's SCRAM verifier with its parts (known from the moment the lab reads
it, before PgBouncer starts). The message of an exception is not
published and not printed: only its type and the lines of this
directory's code that it passed.

    placement.py --self-test
    placement.py --mode quick|full --output DIR --etcd F --haproxy F
                 --pgbouncer F --pg-bin DIR --venv DIR
"""

import argparse
import asyncio
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
import gzip
import hashlib
from http.client import HTTPConnection, HTTPException
import itertools
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hosted_lab  # noqa: E402
import run_lab  # noqa: E402
from run_lab import HOST, SITES, percentile, wait_until  # noqa: E402

ADDON = HERE.parent / "alerta_fast.py"
ADDON_BLOB = "a8ea47300222215a40b17b8b9b44f796e5b9affb"
PLUGINS = "remote_ip,reject,heartbeat,blackout,forwarder"
# arm: (route, run_lab feature, ALERTA_FAST the processes must have)
ARMS = {"L": ("local", "safe", "safe"),
        "P": ("primary", "safe", "safe"),
        "N": ("primary", "safe+noecho", "safe,noecho")}
PREFLIGHT_ARMS = (("stock", "stock", None), ("safe", "safe", "safe"),
                  ("noecho", "safe+noecho", "safe,noecho"))
PLACEMENT_ORDERS = (("L", "P", "N"), ("P", "N", "L"), ("N", "L", "P"))
FRESH_ORDERS = (("P", "N"), ("N", "P"))
# mode: (seconds of a block, placement blocks, fresh blocks)
MODES = {"quick": (20, 1, 1), "full": (50, 3, 2)}
TENANTS = tuple(f"tenant-{i}" for i in range(4))
STABLE = 6            # stored alerts of each tenant that get duplicates
BACKGROUND = 22       # more stored alerts of each tenant
TARGETS = 2           # stored alerts of each tenant for the changes
RATE = 4              # requests per second
PAGE = 25
SCHEMA = 2  # 2: finish_offset_s; credential_scan of publish()
LIST = f"/alerts?page-size={PAGE}&sort-by=lastReceiveTime"
COUNT = "/alerts/count"
READS = ("list", "count")
POSTS = ("dup", "new", "create")
CHANGES = ("changed", "same", "null", "ack")
CLASS = {"list": "list", "count": "count", "dup": "duplicate", "new": "create",
         "create": "create", **{kind: "change" for kind in CHANGES}}
TIMERS = {"received": POSTS, "queries": ("list",), "counts": ("count",)}
SENTINEL = 20
TIMEOUT = 8
IDLE = 20
LOCAL = threading.local()


def alert_body(resource):
    index = int(resource.rsplit("-", 1)[1]) if resource.startswith("repeat-") else 1
    return {"resource": resource, "event": "LabAlert", "environment": "Production",
            "severity": ("major", "minor", "warning")[index % 3], "service": ["lab"],
            "tags": ["synthetic"], "attributes": {"zone": "lab", "keep": "1"}}


def seed_resources():
    return ([f"repeat-{i}" for i in range(STABLE)] + [f"mutate-{i}" for i in range(TARGETS)]
            + [f"seed-{i:02d}" for i in range(BACKGROUND)])


def plan(stage, block, seconds):
    """The requests of one block: the same list for each arm of the block."""
    slots = seconds * RATE
    writes = slots // 5
    rng = random.Random(f"{stage}/{block}")
    changes = []
    if stage == "placement":
        new = round(writes / 10)
        kinds = ["new"] * new + ["dup"] * (writes - new)
    else:
        sets = max(1, writes // 20)
        if sets > TARGETS:
            raise ValueError("more change sets than tenants with targets")
        changes = [(kind, target) for kind in CHANGES for target in range(sets)]
        creates = writes // 2
        kinds = ["create"] * creates + ["dup"] * (writes - creates - len(changes))
    rng.shuffle(kinds)
    # The changes of one alert keep their order: their slots go up.
    change_at = dict(zip(sorted(rng.sample(range(writes), len(changes))), changes))
    kinds = iter(kinds)
    ops, write = [], 0
    for slot in range(slots):
        pair, site = slot // 2, SITES[slot % 2]
        op = {"op": f"{stage}-{block}-{slot:03d}", "offset": slot / RATE, "site": site}
        if pair % 5 != 4:
            op.update(kind=READS[pair % 2], tenant=TENANTS[(pair + slot % 2) % len(TENANTS)], resource=None)
        else:
            if write in change_at:
                kind, target = change_at[write]
                # Target 0 is tenant 0's, target 1 is tenant 2's.
                op.update(kind=kind, tenant=TENANTS[2 * target], resource=f"mutate-{target}")
            else:
                kind = next(kinds)
                tenant = TENANTS[(write + write // len(TENANTS)) % len(TENANTS)]
                resource = (f"repeat-{(write // len(TENANTS)) % STABLE}" if kind == "dup"
                            else f"fresh-{stage}-{block}-{write}")
                op.update(kind=kind, tenant=tenant, resource=resource)
            write += 1
        ops.append(op)
    return ops


def change_request(kind, alert_id, value="moved"):
    if kind == "ack":
        return f"/alert/{alert_id}/action", {"action": "ack", "text": "benchmark"}
    return f"/alert/{alert_id}/attributes", {"attributes": {"zone": None if kind == "null" else value}}


def request(epoch, port, method, path, key, body=None):
    """One HTTP request on this thread's kept connection to `port`. Never
    sent a second time."""
    if getattr(LOCAL, "epoch", None) != epoch:
        for connection, _ in getattr(LOCAL, "connections", {}).values():
            connection.close()
        LOCAL.connections, LOCAL.epoch = {}, epoch
    connection, last = LOCAL.connections.pop(port, (None, 0))
    if connection is not None and time.monotonic() - last >= IDLE:
        connection.close()
        connection = None
    if connection is None:
        connection = HTTPConnection(HOST, port, timeout=TIMEOUT)
    answer = {"status": None, "raw": b"", "app_site": None, "proxy_site": None, "worker": None,
              "reused": connection.sock is not None, "error": None}
    headers = {"Accept": "application/json", "Authorization": f"Key {key}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body)
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        answer.update(raw=response.read(), status=response.status,
                      app_site=response.getheader("X-Alrust-Bench-App-Site"),
                      proxy_site=response.getheader("X-Alrust-Bench-Site"),
                      worker=response.getheader("X-Alrust-Bench-Worker"))
        LOCAL.connections[port] = (connection, time.monotonic())
    except (OSError, HTTPException) as exc:
        connection.close()
        answer["error"] = type(exc).__name__
    return answer


def sample(epoch, port, key, op, ids, phase, due):
    """Sends one planned request and reads its answer; the ledger's row.
    Times are time.monotonic() until the run ends."""
    kind, tenant, resource = op["kind"], op["tenant"], op["resource"]
    if kind in READS:
        method, path, body = "GET", LIST if kind == "list" else COUNT, None
    elif kind in POSTS:
        method, path, body = "POST", "/alert", alert_body(resource)
    else:
        method, (path, body) = "PUT", change_request(kind, ids[tenant, resource])
    started = time.monotonic()
    answer = request(epoch, port, method, path, key, body)
    ended = time.monotonic()
    row = {"op": op["op"], "phase": phase, "site": op["site"], "tenant": tenant, "kind": kind,
           "resource": resource, "status": answer["status"], "error": answer["error"], "ok": False,
           "app_site": answer["app_site"], "proxy_site": answer["proxy_site"], "worker": answer["worker"],
           "reused": answer["reused"],
           "arrival": due, "start": started, "end": ended}
    if answer["status"] != (201 if kind in POSTS else 200):
        return row
    try:
        data = json.loads(answer["raw"])
    except ValueError:
        row["error"] = "not_json"
        return row
    if not isinstance(data, dict) or data.get("status") != "ok":
        row["error"] = "not_ok_body"
    elif kind in READS:
        counts = [data.get("severityCounts"), data.get("statusCounts")]
        alerts = data.get("alerts") if kind == "list" else []
        if (type(data.get("total")) is not int or not all(isinstance(c, dict) for c in counts)
                or not all(type(n) is int for c in counts for n in c.values())
                or sum(counts[0].values()) != data["total"]):
            row["error"] = "wrong_totals"
        elif not isinstance(alerts, list) or any(a.get("customer") != tenant for a in alerts):
            row["error"] = "other_customer"
        else:
            row.update(ok=True, total=data["total"], severity_counts=counts[0], status_counts=counts[1])
            if kind == "list":
                row["page"] = [a["id"] for a in alerts]
    elif kind in POSTS:
        alert = data.get("alert")
        if (not isinstance(alert, dict) or alert.get("resource") != resource
                or alert.get("customer") != tenant or not isinstance(data.get("id"), str)):
            row["error"] = "wrong_alert"
        else:
            row.update(ok=True, alert_id=data["id"])
    else:
        row["ok"] = True
    return row


def histograms(view, acked, names):
    """The counts by severity and by status of these alerts."""
    return (dict(Counter(alert_body(names[alert])["severity"] for alert in view)),
            dict(Counter("ack" if alert in acked else "open" for alert in view)))


def reconcile(rows, orders, names):
    """Marks every read: `reconciled` True (it agrees with the tenant's
    committed alerts; see the file header), False, or None (a write of the
    tenant has no known result, so the states after it are not known),
    `witness` (the states that it agrees with) and `missed` (it agrees
    only with an alert absent that was received during the read).
    `orders` has each tenant's ids, newest first, before the first
    request; `names` the resource of each of those ids. Returns each
    tenant's last known order, or None."""
    last = {}
    for tenant, first in orders.items():
        mine = [row for row in rows if row["tenant"] == tenant]
        # One after the other (the tenant's lane), so this is their order.
        writes = sorted((row for row in mine if row["kind"] not in READS), key=lambda row: row["start"])
        names = dict(names)
        # After k writes: the ids, newest first; the acknowledged ids; and
        # the id that write k received (a POST), or None.
        states, acked, received = [list(first)], [frozenset()], [None]
        for write in writes:
            if not write["ok"]:
                break
            state, done, alert = list(states[-1]), acked[-1], None
            if write["kind"] in POSTS:
                alert = write["alert_id"]
                names[alert] = write["resource"]
                if alert in state:
                    state.remove(alert)
                state.insert(0, alert)
            elif write["kind"] == "ack":
                done = done | {a for a in state if names[a] == write["resource"]}
            states.append(state)
            acked.append(done)
            received.append(alert)
        known = len(states) - 1
        last[tenant] = states[-1] if known == len(writes) else None

        def view(cutoff, k):
            """State k for a statement with the time condition, when the
            read arrived after write `cutoff` was received."""
            late = {received[j] for j in range(cutoff + 1, k + 1)}
            return [alert for alert in states[k] if alert not in late]

        for read in (row for row in mine if row["kind"] in READS and row["ok"]):
            low = sum(1 for write in writes if write["end"] <= read["start"])
            high = sum(1 for write in writes if write["start"] < read["end"])
            read["states"] = [low, high]
            if high > known:
                read["reconciled"] = None
                continue
            got = tuple({name: n for name, n in read[field].items() if n}
                        for field in ("severity_counts", "status_counts"))
            span = range(low, high + 1)
            if read["kind"] == "count":
                found = [{"state": k, "absent": 0} for k in span
                         if histograms(states[k], acked[k], names) == got]
            else:
                found = [{"cutoff": c, "count_state": k1, "page_state": k2,
                          "absent": max(len(states[k]) - len(view(c, k)) for k in (k1, k2))}
                         # The lane sends a write after the answer to the one
                         # before it: of the writes received before the read's
                         # time, only the last can be not yet stored.
                         for c in span for k1 in range(max(low, c - 1), high + 1) for k2 in range(k1, high + 1)
                         if histograms(view(c, k1), acked[k1], names) == got
                         and view(c, k2)[:PAGE] == read["page"]]
            read["reconciled"] = bool(found)
            if found:
                read["witness"] = min(found, key=lambda witness: witness["absent"])
                read["missed"] = read["witness"]["absent"] > 0
    return last


def persisted_problems(state, seeded, rows):
    """Every write with an answer against the stored alerts. `state` is
    final_state()'s, `seeded` the (tenant, resource) of the fixture.
    Returns the wanted and the stored values of every alert that a write
    named, and the problems. An alert of a write without a known result
    is not judged (that write makes the run rejected)."""
    posts, acks, unknown = Counter(), set(), set()
    for row in rows:
        key = (row["tenant"], row["resource"])
        if row["kind"] in READS:
            continue
        if not row["ok"]:
            unknown.add(key)
        elif row["kind"] in POSTS:
            posts[key] += 1
        elif row["kind"] == "ack":
            acks.add(key)
    stored = {(row["customer"], row["resource"]): row for row in state}
    fields = ("duplicate_count", "severity", "status")
    table, problems = {}, []
    for key in sorted(set(seeded) | set(posts) | set(stored)):
        if key in unknown:
            continue
        got = stored.get(key)
        wanted = None
        if key in seeded or key in posts:
            wanted = {"duplicate_count": posts[key] - (key not in seeded),
                      "severity": alert_body(key[1])["severity"], "status": "ack" if key in acks else "open"}
        have = got and {field: got[field] for field in fields}
        if key in posts or key in acks or have != wanted:
            table["/".join(key)] = {"wanted": wanted, "stored": have}
        if have != wanted:
            problems.append(f"{key[0]} {key[1]}: stored {have}, wanted {wanted}")
    return table, problems


def groups(rows):
    """Per site and class of the timed requests: counts and times in ms.
    `ms` is from the planned time of the request to the end of the answer;
    `service_ms` from the send. Small classes keep every time."""
    result = {}
    by = defaultdict(list)
    for row in rows:
        if row["phase"] == "timed":
            by[row["site"], CLASS[row["kind"]]].append(row)
    for (site, name), group in sorted(by.items()):
        good = [row for row in group if row["ok"]]
        wall = sorted(round((row["end"] - row["arrival"]) * 1000, 2) for row in good)
        service = sorted(round((row["end"] - row["start"]) * 1000, 2) for row in good)
        delay = sorted(round((row["start"] - row["arrival"]) * 1000, 2) for row in group)
        result[f"{site}/{name}"] = {
            "offered": len(group), "ok": len(good),
            "p50_ms": percentile(wall, 0.50), "p95_ms": percentile(wall, 0.95),
            "max_ms": wall[-1] if wall else None,
            "service_p50_ms": percentile(service, 0.50), "service_p95_ms": percentile(service, 0.95),
            "send_delay_p95_ms": percentile(delay, 0.95),
            **({"all_ms": wall} if len(wall) <= 24 else {})}
    return result


def differences(runs, pairs):
    """For one block: second arm minus first arm, per site and class."""
    by_arm = {run["arm"]: run["groups"] for run in runs}
    result = {}
    for first, second in pairs:
        if first not in by_arm or second not in by_arm:
            continue
        result[f"{second}-{first}"] = {
            name: {field: round(by_arm[second][name][field] - by_arm[first][name][field], 2)
                   for field in ("p50_ms", "p95_ms")
                   if by_arm[first][name][field] is not None and by_arm[second][name].get(field) is not None}
            for name in by_arm[first] if name in by_arm[second]}
    return result


def blob_id(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sources():
    files = [HERE.parent / "dual_site_quorum.py", ADDON, HERE.parent / "fastpath_check.py",
             *sorted(p for p in HERE.iterdir() if p.is_file())]
    return {str(path.relative_to(HERE.parent)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def require_hosted():
    """hosted_lab.replay_lab's conditions: an unprivileged process on a
    GitHub-hosted runner, in a capped cgroup, with the packet delays on."""
    caps = run_lab.resource_caps()
    try:
        quota, period = map(int, caps["cpu.max"].split())
        capped = (0 < quota <= 2 * period and 0 < int(caps["memory.max"]) <= 4 * 1024 ** 3
                  and caps["memory.swap.max"] == "0")
    except (KeyError, ValueError):
        capped = False
    if not (os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("ALRUST_NETEM_ACTIVE") == "1"
            and os.environ.get("ALRUST_NETEM_RTTS") == "50,100,30"
            and os.getuid() != 0 and capped):
        sys.exit("This benchmark runs only on a GitHub-hosted runner, as an unprivileged user in a "
                 "capped cgroup, started by hosted-netem.sh (see .github/workflows/bench-two-site.yml).")
    return caps


def lab_args(tools, output):
    """run_lab.cli()'s values for this lab. cli() itself is not used: it
    starts run_lab's own experiment and knows no `safe` or `noecho`."""
    return argparse.Namespace(
        etcd=tools.etcd, haproxy=tools.haproxy, pgbouncer=tools.pgbouncer, pg_bin=tools.pg_bin,
        venv=tools.venv, psql=tools.pg_bin / "psql", addon=ADDON, output=output, pg_library_dir=None,
        rtt_ms=50, wal_rtt_ms=100, dcs_rtt_ms=30, netem=True,
        http_rate_mbit=int(os.environ.get("ALRUST_NETEM_HTTP_RATE_MBIT", 0)),
        dcs_ttl=20, patroni_loop=4, patroni_retry=4, failover_lag=1048576, ingress="primary-aware",
        pool_size=8, pool_idle=600, pool_login_retry=15, pool_connect_timeout=15, pool_query_wait=120,
        pool_mode="transaction", workers=1, threads=2, worker_class="gthread", key_count=1,
        page_size=PAGE, read_filter="all", forwarder=True, health_interval=500, health_timeout=2000,
        writer_idle=30, flush=1.0, max_age=300.0, fault=None, fault_target="site-a", rejoin=False,
        wal_outage=0, health_trace=False, bulk_alerts=0, search_index="stock", clone_strategy="wal-log",
        file_copy_method="copy", dashboard_encoding=None, cool_start_c=75,
        # Each site's HAProxy names its site in the answers of its Alerta.
        operator_route_trace=True)


class PlacementLab(hosted_lab.VirtualLab):
    def __init__(self, args, root, logdir):
        super().__init__(args, root, logdir)
        # Every Alerta process: each key sees only its customer's alerts.
        # Alerta does not start with customer views and no admin user; no
        # request of the benchmark is that user's.
        self.env = {**self.env, "CUSTOMER_VIEWS": "True", "ADMIN_USERS": "bench"}
        self.keys = {tenant: secrets.token_hex(20) for tenant in TENANTS}
        self.settings = defaultdict(list)

    async def start_app(self, stack, site, slot, db, feature, label):
        await super().start_app(stack, site, slot, db, feature, label)
        pid = self.app_processes[site][-1].pid
        env = dict(item.split("=", 1) for item in
                   Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0") if "=" in item)
        self.settings[db].append({"site": site, "slot": slot, "feature": feature,
                                  **{name: env.get(name) for name in
                                     ("PLUGINS", "ALERTA_FAST", "CUSTOMER_VIEWS", "FWD_DESTINATIONS",
                                      "ALERTA_FAST_CONN_MAX_AGE", "ALERTA_FAST_FLUSH_SECONDS")}})

    def port(self, site, route):
        return self.p(site, "http" if route == "local" else "affinity")

    def json_sql(self, db, sql, site="a"):
        return json.loads(self.sql(db, sql, site))

    def private_sql(self, db, sql, what):
        """SQL that has a credential: psql reads it from standard input, and
        nothing that psql writes is kept or shown."""
        done = subprocess.run(
            [self.args.psql, "-X", "-w", "-q", "-v", "ON_ERROR_STOP=1", "-h", HOST, "-p", str(self.p("a", "pg")),
             "-U", "postgres", "-d", db, "-f", "-"], input=sql, text=True, timeout=10,
            env={**self.pg_env, "PGPASSWORD": self.password, "PGCONNECT_TIMEOUT": "3"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if done.returncode:
            raise RuntimeError(f"{what} failed (psql status {done.returncode}; its output is not kept)")

    def private_values(self):
        """The credentials of this lab, for the search of the output."""
        # The verifier is SCRAM-SHA-256$<count>:<salt>$<stored key>:<server key>.
        parts = [part for part in self.verifier.replace("$", ":").split(":")[2:] if len(part) >= 16]
        return [self.password, self.key, self.verifier, *parts, *self.client_keys, *self.keys.values()]

    def private_files(self):
        modes = {"directory": self.root.stat().st_mode & 0o777,
                 "userlist": (self.root / "users.txt").stat().st_mode & 0o777}
        if modes != {"directory": 0o700, "userlist": 0o600}:
            raise RuntimeError(f"The lab's directory or PgBouncer's user file is not private: {modes}")
        return {name: oct(mode) for name, mode in modes.items()}

    async def seed(self):
        self.sql("postgres", "CREATE DATABASE bench_seed")
        async with AsyncExitStack() as app:
            await self.start_app(app, "a", "vm1", "bench_seed", "stock", "seed")
            self.private_sql("bench_seed", "".join(
                'INSERT INTO keys (id,key,"user",scopes,text,expire_time,count,customer) '
                f"VALUES ('bench-key-{index}','{key}','user-{index}',ARRAY['read','write'],"
                f"'benchmark',timezone('UTC',now())+interval '1 day',0,'{tenant}');\n"
                for index, (tenant, key) in enumerate(self.keys.items())), "The insert of the tenant keys")
            # host-0000 is the alert that fastpath_check.py copies.
            for resource, tenant in [*itertools.product(seed_resources(), TENANTS), ("host-0000", TENANTS[0])]:
                answer = await asyncio.to_thread(request, "seed", self.p("a", "vm1"), "POST", "/alert",
                                                 self.keys[tenant], alert_body(resource))
                if answer["status"] != 201:
                    raise RuntimeError(f"Seed failed at {tenant} {resource}: HTTP {answer['status']} "
                                       f"{answer['error']} {answer['raw'][:160]!r}")
                await asyncio.sleep(0.02)
        wanted = len(seed_resources())
        counts = self.json_sql("bench_seed", "SELECT json_object_agg(customer, n) FROM (SELECT customer, "
                               "count(*) n, count(DISTINCT last_receive_time) d FROM alerts "
                               "GROUP BY 1 HAVING count(*) = count(DISTINCT last_receive_time)) s")
        if counts != {tenant: wanted + (tenant == TENANTS[0]) for tenant in TENANTS}:
            raise RuntimeError(f"The seed does not have the wanted alerts with distinct times: {counts}")
        await self.wait_for_replay(self.sql("postgres", "SELECT pg_current_wal_flush_lsn()"),
                                   "the standby to replay the seed")
        self.sql("postgres", "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                             "WHERE datname='bench_seed'")

    def fixture(self, db):
        """(tenant, resource) -> id, and each tenant's ids, newest first."""
        ids, orders = {}, {tenant: [] for tenant in TENANTS}
        for line in self.sql(db, "SELECT customer, resource, id FROM alerts "
                                 "ORDER BY last_receive_time DESC").splitlines():
            tenant, resource, alert_id = line.split("|")
            ids[tenant, resource] = alert_id
            orders[tenant].append(alert_id)
        return ids, orders

    def row_updates(self, db):
        """Row updates of `alerts` so far. A backend writes its counts to
        the shared statistics when it ends, so the backends on the
        database (PgBouncer's server connections) are ended first."""
        self.sql("postgres", f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname='{db}'")
        for _ in range(100):
            if self.sql("postgres", f"SELECT count(*) FROM pg_stat_activity WHERE datname='{db}'") == "0":
                return int(self.sql(db, "SELECT n_tup_upd FROM pg_stat_user_tables WHERE relname='alerts'"))
            time.sleep(0.1)
        return None

    def key_uses(self, db):
        return self.json_sql(db, "SELECT json_object_agg(customer, count) FROM keys WHERE id LIKE 'bench-key-%'")

    def final_state(self, db, site="a"):
        return self.json_sql(db, """
            SELECT coalesce(json_agg(json_build_object(
                     'customer', customer, 'resource', resource, 'severity', severity, 'status', status,
                     'duplicate_count', duplicate_count,
                     'tags', (SELECT coalesce(json_agg(t ORDER BY t), '[]') FROM unnest(tags) t),
                     'attributes', attributes::text,
                     'history', (SELECT coalesce(json_agg(concat_ws('/', h.type, h.status, h.severity)
                                                          ORDER BY h.ordinality), '[]')
                                   FROM unnest(history) WITH ORDINALITY AS h))
                   ORDER BY customer, resource), '[]')
              FROM alerts""", site)

    def settings_problems(self, db, wanted_fast, processes=4):
        got = self.settings[db]
        bad = [s for s in got if s["PLUGINS"] != PLUGINS or s["ALERTA_FAST"] != wanted_fast
               or s["CUSTOMER_VIEWS"] != "True" or s["FWD_DESTINATIONS"] is not None]
        return [f"Alerta process settings are not the wanted ones: {bad or got}"] if bad or len(got) != processes else []

    async def apps(self, stack, db, feature, label=None):
        for site, slot in itertools.product(SITES, ("vm1", "vm2")):
            await self.start_app(stack, site, slot, db, feature, label or db)
        ports = [self.port(site, route) for site in SITES for route in ("local", "primary")]
        await wait_until(lambda: all(run_lab.http(port, "/management/gtg")[0] == 200 for port in ports),
                         "the local and the primary-site frontends of both sites")
        await asyncio.sleep(self.args.health_interval * 2 / 1000)

    async def links(self):
        """The links as measured now (run_lab's calibration)."""
        reading = {kind: await asyncio.wait_for(run_lab.calibrate(0, host), 10)
                   for kind, host in run_lab.PACKET_HOSTS.items()}
        reading["http"]["bulk_read"] = await run_lab.measure_http_link_rate()
        return reading

    async def preflight(self, out, tools):
        result = {"ok": False, "problems": []}
        problems = result["problems"]
        # (1) The differential check, on its own copy, straight to the primary.
        await self.clone_fixture("pre_diff")
        report = out / "fastpath-check.json"
        env = {"PATH": f"{tools.venv}/bin:/usr/bin:/bin", "HOME": str(self.root),
               "DATABASE_URL": f"postgresql://postgres:{self.password}@{HOST}:{self.p('a', 'pg')}/pre_diff",
               "AUTH_REQUIRED": "True", "SECRET_KEY": self.key, "LOG_LEVEL": "WARNING"}
        with (self.logs / "fastpath-check.log").open("wb") as log:
            done = await asyncio.to_thread(
                subprocess.run, [str(tools.venv / "bin/python"), "-I", str(HERE.parent / "fastpath_check.py"),
                                 str(report)], env=env, cwd=str(self.root), stdout=log, stderr=log, timeout=300)
        check = json.loads(report.read_text()) if report.exists() else {}
        result["differential"] = {"exit": done.returncode, "cases": check.get("cases"),
                                  "failed": check.get("failed")}
        if done.returncode or not check.get("ok") or check.get("cases") != 38:
            problems.append("the differential check did not pass with 38 cases")
        self.sql("postgres", "DROP DATABASE pre_diff WITH (FORCE)")
        # (2) and (3) over HTTP, from site B by the primary-site route.
        tenant = TENANTS[0]
        key, port = self.keys[tenant], self.port("b", "primary")
        result["http"] = {}
        for name, feature, wanted_fast in PREFLIGHT_ARMS:
            db = f"pre_{name}"
            await self.clone_fixture(db)
            ids, _ = self.fixture(db)
            target = ids[tenant, "mutate-0"]
            async with AsyncExitStack() as stack:
                await self.apps(stack, db, feature)
                statuses = [(await asyncio.to_thread(request, db, port, "POST", "/alert", key,
                                                     alert_body("sentinel-0")))["status"]
                            for _ in range(SENTINEL + 1)]
            # Read with Alerta stopped: HAProxy's health checks of a running
            # Alerta keep backends on the database.
            updates = await asyncio.to_thread(self.row_updates, db)
            async with AsyncExitStack() as stack:
                await self.apps(stack, db, feature, f"{db}-changes")
                changes = []
                for kind, value in (("same", "lab"), ("changed", "moved"), ("null", None), ("ack", None)):
                    path, body = change_request(kind, target, value)
                    answer = await asyncio.to_thread(request, db, port, "PUT", path, key, body)
                    changes.append([kind, answer["status"], answer["raw"].decode(errors="replace")[:200]])
            rows = {row["resource"]: row for row in self.final_state(db) if row["customer"] == tenant}
            result["http"][name] = {
                "post_statuses_not_201": sum(1 for status in statuses if status != 201),
                "alert_row_updates": updates, "changes": changes,
                "sentinel": rows.get("sentinel-0"), "target": rows.get("mutate-0"),
                "settings": self.settings[db]}
            problems += [f"{name}: {text}" for text in self.settings_problems(db, wanted_fast, 8)]
            self.sql("postgres", f"DROP DATABASE {db} WITH (FORCE)")
        stock, safe, noecho = (result["http"][name] for name, _, _ in PREFLIGHT_ARMS)
        echo = SENTINEL + 2 * (SENTINEL + 1)
        result["alert_row_updates_wanted"] = {"stock": echo, "safe": echo, "noecho": SENTINEL}
        for name, arm, wanted in (("stock", stock, echo), ("safe", safe, echo), ("noecho", noecho, SENTINEL)):
            if arm["post_statuses_not_201"] or arm["alert_row_updates"] != wanted:
                problems.append(f"{name}: 1 create and {SENTINEL} duplicates gave {arm['alert_row_updates']} "
                                f"row updates ({wanted} wanted) or a status that is not 201")
            if any(status != 200 for _, status, _ in arm["changes"]):
                problems.append(f"{name}: a change was not answered with 200")
            if any(arm[field] != stock[field] for field in ("changes", "sentinel", "target")):
                problems.append(f"{name}: statuses, answers or stored result differ from Alerta as released")
            problems += [f"{name}: {text}" for text in target_problems(arm["target"])]
            if not arm["sentinel"] or arm["sentinel"]["duplicate_count"] != SENTINEL:
                problems.append(f"{name}: the sentinel alert does not have {SENTINEL} duplicates")
        result["ok"] = not problems
        return result

    async def arm_run(self, name, arm, ops, block_start):
        route, feature, wanted_fast = ARMS[arm]
        clone = await self.clone_fixture(name)
        ids, orders = self.fixture(name)
        keys_before, timers_before = self.key_uses(name), self.alert_timers(name)
        tenant_reads = [{"op": f"{phase}-{site}-{tenant}-{kind}", "site": site, "tenant": tenant,
                         "kind": kind, "resource": None}
                        for phase in ("warm", "final") for site in SITES for tenant in TENANTS for kind in READS]
        rows = []
        async with AsyncExitStack() as stack:
            await self.apps(stack, name, feature)

            async def send(op, phase, due, lanes=None):
                call = (sample, name, self.port(op["site"], route), self.keys[op["tenant"]], op, ids, phase)
                if lanes is None or op["kind"] in READS:
                    return await asyncio.to_thread(*call, due or time.monotonic())
                async with lanes[op["tenant"]]:
                    return await asyncio.to_thread(*call, due)

            for op in tenant_reads[:len(tenant_reads) // 2]:
                rows.append(await send(op, "warm", None))
            pool_before, cpu_before = self.pool_stats(name), self.cpu_sample()
            scope_before = run_lab.cgroup_cpu_stats()
            wal_before = self.sql("postgres", "SELECT pg_current_wal_insert_lsn()")
            lanes = {tenant: asyncio.Lock() for tenant in TENANTS}
            start = time.monotonic() + 0.25
            tasks = []
            for op in ops:
                await asyncio.sleep(max(0, start + op["offset"] - time.monotonic()))
                tasks.append(asyncio.create_task(send(op, "timed", start + op["offset"], lanes)))
            rows += await asyncio.gather(*tasks)
            timed_end = time.monotonic()
            wal_bytes = int(self.sql("postgres", f"SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),'{wal_before}')"))
            scope_after = run_lab.cgroup_cpu_stats()
            pool_after, cpu_after = self.pool_stats(name), self.cpu_sample()
            for op in tenant_reads[len(tenant_reads) // 2:]:
                rows.append(await send(op, "final", None))
        updates = await asyncio.to_thread(self.row_updates, name)
        _, orders_after = self.fixture(name)
        state = self.final_state(name)
        keys_after, timers_after = self.key_uses(name), self.alert_timers(name)
        # No Alerta runs now. Site B must replay this position (wait_until's
        # 45 s at most) and then have the same alerts.
        flush = self.sql("postgres", "SELECT pg_current_wal_flush_lsn()")
        try:
            await self.wait_for_replay(flush, "the standby to replay the run")
            standby_state = self.final_state(name, "b")
        except RuntimeError:
            standby_state = None
        replay = self.sql("postgres", "SELECT pg_last_wal_replay_lsn()", "b")
        self.sql("postgres", f"DROP DATABASE {name} WITH (FORCE)")

        problems = self.settings_problems(name, wanted_fast)
        last = reconcile(rows, orders, {alert: resource for (_, resource), alert in ids.items()})
        persisted, not_persisted = persisted_problems(state, set(ids), rows)
        failed = [row["op"] for row in rows if not row["ok"]]
        unreconciled = [row["op"] for row in rows if row["kind"] in READS and row["ok"]
                        and row.get("reconciled") is not True]
        wrong_site = [row["op"] for row in rows if row["status"] is not None and (
            row["worker"] not in ("vm1", "vm2")
            or (row["app_site"], row["proxy_site"]) != (("a", "a") if route == "primary" else (row["site"], None)))]
        answered = Counter(row["tenant"] for row in rows if row["status"] is not None)
        keys_wanted = {tenant: keys_before[tenant] + answered[tenant] for tenant in TENANTS}
        good = Counter(row["kind"] for row in rows if row["ok"])
        timers_wanted = {timer: timers_before.get(timer, 0) + sum(good[kind] for kind in kinds)
                         for timer, kinds in TIMERS.items()}
        stored = {(row["customer"], row["resource"]): row for row in state}
        changed = sorted({(op["tenant"], op["resource"]) for op in ops if op["kind"] in CHANGES})
        for what, bad in (
                (f"{len(failed)} requests without the wanted answer: {failed[:8]}", failed),
                (f"{len(unreconciled)} reads agree with no possible state: {unreconciled[:8]}", unreconciled),
                (f"{len(wrong_site)} answers from another site or route than planned: {wrong_site[:8]}", wrong_site),
                ("the model's last order is not the database's order", last != orders_after),
                (f"{len(not_persisted)} alerts are not stored as the answered writes say: {not_persisted[:8]}",
                 not_persisted),
                ("site B did not replay the primary's last flushed position in time", standby_state is None),
                ("site B's alerts are not the primary's alerts",
                 standby_state is not None and standby_state != state),
                (f"key uses are {keys_after}, wanted {keys_wanted}", keys_after != keys_wanted),
                (f"timers are {timers_after}, wanted {timers_wanted}", timers_after != timers_wanted),
                ("the row updates of `alerts` could not be read", updates is None)):
            if bad:
                problems.append(what)
        for key in changed:
            problems += [f"{key[0]} {key[1]}: {text}" for text in target_problems(stored.get(key))]
        for row in rows:  # the ledger's times: seconds from the block's start
            for field in ("arrival", "start", "end"):
                row[field] = round(row[field] - block_start, 4)
            if row.get("reconciled") and not row.get("missed"):
                # The page and the counts of any other read stay as evidence.
                for field in ("page", "severity_counts", "status_counts"):
                    row.pop(field, None)
        numeric = lambda stats: {k: int(v) for k, v in stats.items() if v.lstrip("-").isdigit()}
        return {
            "run": name, "arm": arm, "route": route, "alerta_fast": wanted_fast,
            "valid": not problems, "problems": problems, **clone,
            "requests": Counter(f"{row['phase']}/{CLASS[row['kind']]}" for row in rows),
            "groups": groups(rows),
            "reads_missing_an_alert": Counter(row["site"] for row in rows if row.get("missed")),
            # The last answer against the planned end of the timed part: the
            # signed offset (negative: it came before that end) and the wait.
            "finish_offset_s": (offset := round(timed_end - start - (ops[-1]["offset"] + 1 / RATE), 3)),
            "drain_s": max(0.0, offset),
            "alert_row_updates": updates, "primary_wal_bytes": wal_bytes,
            "primary_flush_lsn": flush, "standby_replay_lsn": replay,
            "standby_has_the_primary_alerts": standby_state == state,
            "persisted": persisted,
            "answers_by_site": Counter(f"user {row['site']}: Alerta {row['app_site']}, proxy to {row['proxy_site']}"
                                       for row in rows if row["status"] is not None),
            "key_uses": keys_after, "key_uses_wanted": keys_wanted,
            "timers": timers_after, "timers_wanted": timers_wanted,
            "pgbouncer_delta": {site: {k: v - numeric(pool_before[site]).get(k, 0)
                                       for k, v in numeric(pool_after[site]).items()} for site in SITES},
            "cpu_ms": {k: round((cpu_after[k] - v) * 1000 / os.sysconf("SC_CLK_TCK"), 1)
                       for k, v in cpu_before.items()},
            "cgroup_cpu_delta": {k: v - scope_before[k] for k, v in scope_after.items() if k in scope_before},
            "transport_errors": Counter(row["error"] for row in rows if row["status"] is None),
            "changed_alerts": {f"{key[0]}/{key[1]}": stored.get(key) for key in changed},
            "final_state_sha256": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
            "settings": self.settings[name],
        }, rows

    async def block(self, stage, index, order, seconds, t0):
        ops = plan(stage, index, seconds)
        record = {"stage": stage, "block": index, "order": list(order), "seconds": seconds,
                  "offered": Counter(CLASS[op["kind"]] for op in ops),
                  "plan_sha256": hashlib.sha256(json.dumps(ops, sort_keys=True).encode()).hexdigest(),
                  "links": await self.links(), "runs": []}
        ledger = []
        for position, arm in enumerate(order):
            name = f"{stage[0]}{index}{arm.lower()}"
            started = time.monotonic()
            run, rows = await self.arm_run(name, arm, ops, t0)
            run.update(position=position, wall_s=round(time.monotonic() - started, 1))
            record["runs"].append(run)
            ledger += [{"stage": stage, "block": index, "arm": arm, "position": position, **row} for row in rows]
            print(f"{stage} block {index} arm {arm}: {'valid' if run['valid'] else run['problems']}", flush=True)
        equal = len({run["final_state_sha256"] for run in record["runs"]}) == 1
        record["final_states_equal"] = equal
        record["valid"] = equal and all(run["valid"] for run in record["runs"])
        pairs = (("L", "P"), ("P", "N"), ("L", "N")) if stage == "placement" else (("P", "N"),)
        record["differences"] = differences(record["runs"], pairs) if record["valid"] else {}
        return record, ledger


def versions(tools):
    """The programs and the Python packages as they are now (Patroni's
    install can change the packages that setup.sh recorded)."""
    def first_line(*command):
        try:
            done = subprocess.run([str(part) for part in command], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return type(exc).__name__
        return (done.stdout.strip().splitlines() or [""])[0][:200]
    result = {"patroni": first_line(tools.venv / "bin/patroni", "--version"),
              "postgres": first_line(tools.pg_bin / "postgres", "--version"),
              "pgbouncer": first_line(tools.pgbouncer, "--version"),
              "etcd": first_line(tools.etcd, "--version"), "haproxy": first_line(tools.haproxy, "-v")}
    try:
        result["python_packages"] = json.loads(subprocess.run(
            [str(tools.venv / "bin/python"), "-I", "-c",
             "import importlib.metadata as m, json; print(json.dumps(sorted("
             "f'{d.metadata[\"Name\"]}=={d.version}' for d in m.distributions())))"],
            stdout=subprocess.PIPE, text=True, timeout=30).stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["python_packages"] = type(exc).__name__
    return result


NOT_FINISHED = "The run did not finish. Nothing of it was published."
HAD_A_CREDENTIAL = (b"Not published: this file had a credential that the lab made for this run, "
                    b"or it could not be read to its end.\n")
NOT_VERIFIED = "The output could not be searched for the lab's credentials. Nothing more of it was published."
REDACTED = b"[REDACTED]"


def has_credential(data, values, unpack=False):
    """True also for a packed file that cannot be unpacked to its end."""
    if unpack:
        try:
            data += gzip.decompress(data)
        except (OSError, EOFError, ValueError):
            return True
    return any(value in data for value in values)


def safe_error(exc):
    """An exception without its message (the message can have a private
    value): the type, and where this directory's code raised or passed it."""
    here = str(Path(__file__).resolve().parent)
    lines = [f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
             for frame in traceback.extract_tb(exc.__traceback__) if frame.filename.startswith(here)]
    return {"type": type(exc).__name__, "at": lines[-8:]}


def notice_only(out, text):
    for name, data in (("results.json", json.dumps({"schema": SCHEMA, "ok": False, "error": text}) + "\n"),
                       ("summary.md", f"**{text}**\n")):
        (out / name).write_text(data)


def begin_output(out):
    """What --output has when the run is stopped before publish()."""
    out.mkdir(parents=True)
    notice_only(out, NOT_FINISHED)


def publish(stage, out, record, values):
    """Writes the run to `out` from the private directory `stage`. Every
    byte written to `out` was searched for `values` (the lab's
    credentials) before; see the file header. Changes `record` (`ok`,
    `credential_scan`) and returns the scan."""
    values = [value.encode() for value in values if value]
    scan = {"files": 0, "values": len(values), "replaced": [], "redacted": []}
    for path in sorted(p for p in stage.rglob("*") if p.is_symlink() or p.is_file()):
        scan["files"] += 1
        name = path.relative_to(stage)
        try:
            data = None if path.is_symlink() else path.read_bytes()
        except OSError:
            data = None
        if (data is None or has_credential(data, values, path.suffix == ".gz")
                or has_credential(str(name).encode(), values)):
            name = Path(f"not-published-{len(scan['replaced'])}.txt")
            scan["replaced"].append(str(name))
            data = HAD_A_CREDENTIAL
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)
    record["credential_scan"] = scan
    if scan["replaced"]:
        record["ok"] = False
    for attempt in range(2):
        files = {"results.json": (json.dumps(record, indent=1) + "\n").encode(),
                 "summary.md": summary(record).encode()}
        dirty = [name for name, data in files.items() if has_credential(data, values)]
        if not dirty:
            break
        # The same record without the values; the problems and errors stay.
        text = json.dumps(record).encode()
        for value in values:
            text = text.replace(value, REDACTED)
        try:
            clean = json.loads(text)
            if not isinstance(clean, dict):
                raise ValueError
        except ValueError:
            clean = {"schema": SCHEMA, "mode": record.get("mode")}
        scan["redacted"] = dirty
        clean.update(ok=False, credential_scan=scan)
        record.clear()
        record.update(clean)
    else:
        files = {"results.json": (json.dumps({"schema": SCHEMA, "ok": False, "error": HAD_A_CREDENTIAL.decode().strip(),
                                              "credential_scan": {**scan, "redacted": dirty}}) + "\n").encode(),
                 "summary.md": HAD_A_CREDENTIAL}
        record["ok"] = False
    for name, data in files.items():
        (out / name).write_bytes(data)
    return scan


def target_problems(row):
    """A changed alert after: new value, same value, null, ack."""
    if not row:
        return ["the changed alert is not stored"]
    attributes = json.loads(row["attributes"])
    return [text for text, bad in (
        ("attribute `zone` is still there", "zone" in attributes),
        ("attribute `keep` is not there", attributes.get("keep") != "1"),
        ("status is not ack", row["status"] != "ack"),
        ("no history entry of the action", not any(h.startswith("ack/") or h.startswith("action/")
                                                   for h in row["history"]))) if bad]


def summary(record):
    lines = ["## Two-site Alerta: local or primary-site route, and `noecho`", "",
             f"Mode **{record['mode']}**. One hosted runner, synthetic data, emulated links; relative numbers "
             "only. L: local Alerta, `safe`. P: Alerta at the primary's site, `safe`. N: P with `noecho`.", ""]
    if record.get("error"):
        lines += [f"**The run stopped: {json.dumps(record['error'])}**", ""]
    pre = record.get("preflight")
    if pre:
        lines += [f"Preflight: **{'passed' if pre['ok'] else 'FAILED'}**. Differential: "
                  f"{pre.get('differential')}. Row updates of `alerts` for 1 create and {SENTINEL} duplicates: "
                  + ", ".join(f"{name} {arm['alert_row_updates']}" for name, arm in pre.get("http", {}).items())
                  + f" (wanted {pre.get('alert_row_updates_wanted')}).", ""]
        lines += [f"- {problem}" for problem in pre["problems"]] + [""] * bool(pre["problems"])
    blocks = record.get("blocks", [])
    rejected = [block for block in blocks if not block["valid"]]
    lines += [f"Blocks: {len(blocks)}, rejected: {len(rejected)}.", ""]
    for block in rejected:
        lines += [f"- REJECTED {block['stage']} block {block['block']}: final states equal "
                  f"{block['final_states_equal']}; "
                  + "; ".join(f"{run['arm']}: {run['problems']}" for run in block["runs"] if run["problems"])]
    for stage in ("placement", "fresh"):
        chosen = [block for block in blocks if block["stage"] == stage and block["valid"]]
        if not chosen:
            continue
        lines += ["", f"### Stage {stage}: {len(chosen)} valid blocks", "",
                  "Time from the planned send to the end of the answer, ms. One row per block and arm.", "",
                  "| block | arm (position) | site/class | ok/offered | p50 | p95 | max |", "|---|---|---|---|---|---|---|"]
        for block in chosen:
            for run in block["runs"]:
                for name, g in run["groups"].items():
                    lines.append(f"| {block['block']} | {run['arm']} ({run['position'] + 1}) | {name} | "
                                 f"{g['ok']}/{g['offered']} | {g['p50_ms']} | {g['p95_ms']} | {g['max_ms']} |")
        lines += ["", "Differences inside each block (second arm minus first), p50 / p95 in ms.", "",
                  "| block | pair | site/class | p50 | p95 |", "|---|---|---|---|---|"]
        for block in chosen:
            for pair, names in block["differences"].items():
                for name, d in names.items():
                    lines.append(f"| {block['block']} | {pair} | {name} | {d.get('p50_ms')} | {d.get('p95_ms')} |")
        lines += ["", "`reads without an alert`: reads from site A / B that showed neither the old nor the new "
                  "state of an alert received during the read (Alerta as released does this; file header).", "",
                  "| block | arm | row updates of `alerts` | primary WAL bytes | app CPU ms A / B | PostgreSQL CPU ms "
                  "| reads without an alert A / B |", "|---|---|---|---|---|---|---|"]
        for block in chosen:
            for run in block["runs"]:
                cpu = run["cpu_ms"]
                lines.append(f"| {block['block']} | {run['arm']} | {run['alert_row_updates']} | "
                             f"{run['primary_wal_bytes']} | {cpu.get('app_a')} / {cpu.get('app_b')} | "
                             f"{cpu.get('postgres_a')} | {run['reads_missing_an_alert'].get('a', 0)} / "
                             f"{run['reads_missing_an_alert'].get('b', 0)} |")
    scan = record.get("credential_scan")
    if scan:
        lines += ["", f"Search of the output for the lab's credentials: {scan['files']} files, "
                  f"{len(scan['replaced'])} not published {scan['replaced'] or ''}".rstrip() + "."]
        if scan.get("redacted"):
            lines += [f"A credential was replaced by `[REDACTED]` in {scan['redacted']}."]
    lines += ["", f"Result: **{'ok' if record.get('ok') else 'NOT OK'}**"]
    return "\n".join(lines) + "\n"


async def run(tools):
    seconds, placement_blocks, fresh_blocks = MODES[tools.mode]
    published = tools.output
    begin_output(published)
    # Private; publish() copies from here what it searched.
    out = published.with_name(published.name + "-private")
    out.mkdir(mode=0o700)
    record = {"schema": SCHEMA, "mode": tools.mode, "ok": False, "block_seconds": seconds,
              "cgroup": require_hosted(), "sources": sources(), "addon_blob": blob_id(ADDON),
              "arms": {arm: {"route": route, "ALERTA_FAST": fast} for arm, (route, _, fast) in ARMS.items()},
              "plugins": PLUGINS, "tenants": len(TENANTS), "alerts_per_tenant": len(seed_resources()),
              "netem": {k: v for k, v in os.environ.items() if k.startswith("ALRUST_NETEM_")},
              "blocks": []}
    ledger, lab, tmp = [], None, None
    try:
        record["versions"] = versions(tools)
        if record["addon_blob"] != ADDON_BLOB:
            raise RuntimeError(f"scripts/bench/alerta_fast.py is blob {record['addon_blob']}, not {ADDON_BLOB}")
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=8))
        args = lab_args(tools, out / "lab")
        args.output.mkdir()
        with run_lab.lab_directory(args.output) as tmp:
            lab = PlacementLab(args, tmp, args.output)
            async with AsyncExitStack() as stack:
                await lab.start(stack)
                record["private_file_modes"] = lab.private_files()
                # Each site's PgBouncer: a SCRAM login with the generated password.
                # In a thread: site B's path to the primary is a link that
                # this process's event loop carries.
                record["pgbouncer_logins"] = {
                    site: await asyncio.to_thread(lab.sql, "postgres", "SELECT current_user", site, "pooler")
                    for site in SITES}
                if set(record["pgbouncer_logins"].values()) != {"postgres"}:
                    raise RuntimeError(f"PgBouncer logins: {record['pgbouncer_logins']}")
                await lab.seed()
                record["lab"] = json.loads((args.output / "topology.json").read_text())
                record["preflight"] = await lab.preflight(out, tools)
                print(f"preflight: {'passed' if record['preflight']['ok'] else record['preflight']['problems']}",
                      flush=True)
                if record["preflight"]["ok"]:
                    t0 = time.monotonic()
                    todo = ([("placement", i, PLACEMENT_ORDERS[i]) for i in range(placement_blocks)]
                            + [("fresh", i, FRESH_ORDERS[i]) for i in range(fresh_blocks)])
                    for stage, index, order in todo:
                        block, rows = await lab.block(stage, index, order, seconds, t0)
                        record["blocks"].append(block)
                        ledger += rows
                    record["ok"] = all(block["valid"] for block in record["blocks"])
    except Exception as exc:  # the record and the ledger so far are kept
        # Not raised again: Python would print the message to the job's log.
        record["ok"], record["error"] = False, safe_error(exc)
        print(f"the run stopped: {json.dumps(record['error'])}", file=sys.stderr, flush=True)
    finally:
        with gzip.open(out / "samples.jsonl.gz", "wt") as file:
            for row in ledger:
                file.write(json.dumps(row) + "\n")
        record["lab_directory_removed"] = tmp is not None and not tmp.exists()
        if not record["lab_directory_removed"]:
            record["ok"] = False
        try:
            publish(out, published, record, lab.private_values() if lab else [])
        except Exception as exc:
            record["ok"] = False
            notice_only(published, NOT_VERIFIED)
            print(f"publish stopped: {json.dumps(safe_error(exc))}", file=sys.stderr, flush=True)
        shutil.rmtree(out, ignore_errors=True)
    return 0 if record["ok"] else 1


def self_test():
    """No server: the plan, the read check and the summaries."""
    wanted_plans = {
        ("quick", "placement"): {"list": 32, "count": 32, "duplicate": 14, "create": 2},
        ("quick", "fresh"): {"list": 32, "count": 32, "create": 8, "duplicate": 4, "change": 4},
        ("full", "placement"): {"list": 80, "count": 80, "duplicate": 36, "create": 4},
        ("full", "fresh"): {"list": 80, "count": 80, "create": 20, "duplicate": 12, "change": 8}}
    for mode, (seconds, _, _) in MODES.items():
        for stage in ("placement", "fresh"):
            ops = plan(stage, 0, seconds)
            got = Counter(CLASS[op["kind"]] for op in ops)
            assert got == wanted_plans[mode, stage], (mode, stage, got)
            assert ops == plan(stage, 0, seconds) and ops != plan(stage, 1, seconds)
            for site in SITES:
                mine = Counter(CLASS[op["kind"]] in READS for op in ops if op["site"] == site)
                assert mine == {True: seconds * RATE * 2 // 5, False: seconds * RATE // 10}, (mode, stage, mine)
            assert len({op["op"] for op in ops}) == len(ops)
            assert [op["offset"] for op in ops] == [i / RATE for i in range(seconds * RATE)]
            for target in {op["resource"] for op in ops if op["kind"] in CHANGES}:
                kinds = [op["kind"] for op in ops if op["resource"] == target and op["kind"] in CHANGES]
                tenants = {op["tenant"] for op in ops if op["resource"] == target and op["kind"] in CHANGES}
                assert kinds == list(CHANGES) and len(tenants) == 1, (mode, stage, target, kinds)
            new = [(op["tenant"], op["resource"]) for op in ops if op["kind"] in ("new", "create")]
            assert len(set(new)) == len(new)
            assert {op["resource"] for op in ops if op["kind"] == "dup"} <= set(seed_resources())
    full = Counter(op["kind"] for op in plan("placement", 2, 50)), Counter(op["kind"] for op in plan("fresh", 1, 50))
    assert full[0] == {"list": 80, "count": 80, "dup": 36, "new": 4}, full[0]
    assert full[1] == {"list": 80, "count": 80, "create": 20, "dup": 12,
                       "changed": 2, "same": 2, "null": 2, "ack": 2}, full[1]

    # id7 is a `major` alert, id3 the alert that gets the `ack`; the others
    # and the new alert are `minor`.
    first = [f"id{i}" for i in range(30)]
    names = {alert: f"seed-{i:02d}" for i, alert in enumerate(first)}
    names.update(id7="repeat-0", id3="mutate-0", new="fresh-x-0-1")
    moved = ["id7", *(i for i in first if i != "id7")]
    added = ["new", *moved]
    no7 = [i for i in first if i != "id7"]

    def read(kind, start, end, counted, page=None, acked=(), tenant="t", **other):
        severity, status = histograms(counted, acked, names)
        return {"tenant": tenant, "kind": kind, "ok": True, "start": start, "end": end, "resource": None,
                "severity_counts": severity, "status_counts": status,
                **({"page": page[:PAGE]} if page is not None else {}), **other}

    def write(kind, start, end, resource, alert_id=None, ok=True):
        return {"tenant": "t", "kind": kind, "ok": ok, "start": start, "end": end, "resource": resource,
                "alert_id": alert_id}

    assert histograms(moved, {"id3"}, names) == ({"major": 1, "minor": 29}, {"open": 29, "ack": 1})
    cases = [  # the read, reconciled, missed
        (read("list", 0.1, 0.3, first, first), True, False),         # before every write
        (read("list", 0.9, 1.2, first, first), True, False),         # during the duplicate: the old state
        (read("list", 1.1, 1.5, moved, moved), True, False),         # during the duplicate: the new state
        (read("list", 0.9, 1.5, first, moved), True, False),         # the count before it, the page after it
        (read("list", 0.9, 1.5, no7, first), False, None),           # the page from an earlier state than the count
        (read("list", 1.5, 1.7, first, first), False, None),         # after it, the old order
        (read("count", 2.9, 3.6, added), True, False),
        (read("count", 2.9, 3.6, moved), True, False),
        (read("count", 3.6, 3.7, moved), False, None),               # after the create, the old counts
        (read("list", 6.0, 6.1, added, added, acked={"id3"}), True, False),
        (read("list", 6.0, 6.1, added, moved, acked={"id3"}), False, None),   # right counts, wrong page
        (read("list", 6.0, 6.1, added, added), False, None),         # the acknowledged alert counted as open
        (read("count", 0.0, 9.0, first, tenant="other"), True, False),
        # Received during a list read: absent from both statements, or from the page.
        (read("list", 0.9, 1.5, no7, no7), True, True),
        (read("list", 0.9, 1.5, first, no7), True, True),
        # /alerts/count has no time condition: every alert, and one state for both counts.
        (read("count", 0.9, 1.5, no7), False, None),
        ({**read("count", 0.9, 1.5, first), "status_counts": {"open": 29}}, False, None),
        ({**read("list", 0.9, 1.5, first, first), "status_counts": {"open": 29}}, False, None),
        (read("count", 2.9, 3.6, no7), False, None),                 # the create hides nothing stored
        (read("count", 1.5, 1.7, no7), False, None),                 # after the duplicate
        (read("list", 0.9, 1.5, no7, moved[1:PAGE + 1][::-1]), False, None),
        # One position of the read's time for both statements. The new alert
        # counted and then not on the page:
        (read("list", 2.9, 3.6, added, moved), False, None),
        # The reverse can be: received before the read's time, stored between the statements.
        (read("list", 2.9, 3.6, moved, added), True, False),
        (read("list", 2.9, 3.6, moved, moved), True, False),         # the read arrived before the create
        # During the duplicate and the create. The duplicate is after the
        # read's time, so the create is also: `new` cannot be there without id7.
        (read("list", 0.9, 3.6, ["new", *no7], ["new", *no7]), False, None),
        (read("list", 0.9, 3.6, no7, no7), True, True)]
    rows = [write("dup", 1.0, 1.4, "repeat-0", "id7"), write("create", 3.0, 3.5, "fresh-x-0-1", "new"),
            write("ack", 5.0, 5.2, "mutate-0"), *(case[0] for case in cases)]
    last = reconcile(rows, {"t": first, "other": first}, names)
    for row, reconciled, missed in cases:
        assert (row["reconciled"], row.get("missed")) == (reconciled, missed), row
    assert rows[3]["witness"] == {"cutoff": 0, "count_state": 0, "page_state": 0, "absent": 0}, rows[3]
    assert cases[13][0]["witness"] == {"cutoff": 0, "count_state": 1, "page_state": 1, "absent": 1}, cases[13]
    assert cases[7][0]["witness"] == {"state": 1, "absent": 0}, cases[7]
    assert last == {"t": added, "other": first}
    rows = [write("dup", 1.0, 1.4, "repeat-0", "id7"), write("create", 3.0, 3.5, "fresh-x-0-1", None, ok=False),
            write("dup", 4.0, 4.1, "seed-01", "id1"),
            read("count", 2.0, 2.1, moved), read("count", 2.9, 3.1, moved), read("count", 6.0, 6.1, added)]
    assert reconcile(rows, {"t": first}, names) == {"t": None}
    # Two creates of one lane, one after the other, and a list read during
    # both: the counts from before both with a page that has both cannot be
    # (the read's time is after the second was received, so the first was
    # stored before the count).
    names.update(new2="fresh-x-0-2")
    both = ["new2", *added]
    two = [write("create", 3.0, 3.5, "fresh-x-0-1", "new"), write("create", 3.6, 4.1, "fresh-x-0-2", "new2")]
    cases = [(read("list", 2.9, 4.2, moved, both), False, None),
             (read("list", 2.9, 4.2, added, both), True, False),     # the second one in flight at the count
             (read("list", 2.9, 4.2, moved, added), True, False),    # the first one in flight at the count
             (read("list", 2.9, 4.2, both, both), True, False),
             (read("list", 2.9, 4.2, moved, moved), True, False),
             (read("list", 2.9, 4.2, added, added), True, False)]
    reconcile([{**row, "tenant": "t"} for row in two] + [case[0] for case in cases], {"t": moved}, names)
    for row, reconciled, missed in cases:
        assert (row["reconciled"], row.get("missed")) == (reconciled, missed), row
    assert [row["reconciled"] for row in rows[3:]] == [True, None, None], rows[3:]

    def stored(resource, duplicates=0, status="open", severity=None):
        return {"customer": "t", "resource": resource, "duplicate_count": duplicates, "status": status,
                "severity": severity or alert_body(resource)["severity"]}

    seeded = {("t", "repeat-0"), ("t", "mutate-0"), ("t", "seed-01")}
    rows = [write("dup", 1.0, 1.4, "repeat-0", "id7"), write("dup", 2.0, 2.4, "repeat-0", "id7"),
            write("create", 3.0, 3.5, "fresh-x-0-1", "new"), write("ack", 5.0, 5.2, "mutate-0"),
            read("count", 0.0, 0.1, first)]
    good = [stored("repeat-0", 2), stored("mutate-0", status="ack"), stored("seed-01"), stored("fresh-x-0-1")]
    table, problems = persisted_problems(good, seeded, rows)
    assert problems == [] and sorted(table) == ["t/fresh-x-0-1", "t/mutate-0", "t/repeat-0"], (table, problems)
    assert table["t/repeat-0"]["wanted"] == table["t/repeat-0"]["stored"] == {
        "duplicate_count": 2, "severity": "major", "status": "open"}
    for bad, key in (([stored("repeat-0", 1), *good[1:]], "t/repeat-0"),            # a duplicate was lost
                     ([good[0], stored("mutate-0"), *good[2:]], "t/mutate-0"),       # the ack was lost
                     (good[:3], "t/fresh-x-0-1"),                                    # the new alert is not stored
                     ([*good[:2], stored("seed-01", 1), good[3]], "t/seed-01"),      # a duplicate that nobody sent
                     ([*good, stored("extra-1")], "t/extra-1"),                      # an alert that nobody sent
                     ([stored("repeat-0", 2, severity="minor"), *good[1:]], "t/repeat-0")):
        table, problems = persisted_problems(bad, seeded, rows)
        assert len(problems) == 1 and table[key]["wanted"] != table[key]["stored"], (key, problems)
    # A write without a known result: its alert is not judged.
    table, problems = persisted_problems(good[:3], seeded, [*rows[:2], {**rows[2], "ok": False}, *rows[3:]])
    assert problems == [] and "t/fresh-x-0-1" not in table

    import tempfile

    def published_bytes(out):
        """Every byte under `out`, a .gz file also unpacked."""
        data = b""
        for path in sorted(p for p in out.rglob("*") if p.is_file()):
            data += str(path.relative_to(out)).encode() + path.read_bytes()
            if path.suffix == ".gz":
                data += gzip.decompress(path.read_bytes())
        return data

    values = ["0123abcd", "feedf00d", "", "notthere"]

    def staged(tmp, log="", rows="", in_name=False):
        stage, out = tmp / "run-private", tmp / "run"
        (stage / "lab").mkdir(parents=True)
        (stage / "lab/a.log").write_text(f"line\nconnect {log} here\n")
        (stage / "lab/b.log").write_text("nothing\n")
        (stage / ("lab/key-feedf00d.log" if in_name else "lab/c.log")).write_text("named\n")
        with gzip.open(stage / "samples.jsonl.gz", "wt") as file:
            file.write("x" * 500 + rows + "y" * 500)
        begin_output(out)
        return stage, out

    for secret in (b"0123abcd", b"feedf00d"):
        assert has_credential(b"x" + secret, [secret]) and not has_credential(b"x", [secret])
    with tempfile.TemporaryDirectory() as tmp:
        # Stopped before the search: only the notice, nothing of the private directory.
        stage, out = staged(Path(tmp), log="0123abcd", rows="feedf00d")
        assert sorted(p.name for p in out.rglob("*")) == ["results.json", "summary.md"]
        assert json.loads((out / "results.json").read_text()) == {"schema": SCHEMA, "ok": False, "error": NOT_FINISHED}
        assert NOT_FINISHED in (out / "summary.md").read_text()
        assert not any(v.encode() in published_bytes(out) for v in values if v)
        # The search itself stops: a harmless notice, nothing of the record.
        notice_only(out, NOT_VERIFIED)
        assert NOT_VERIFIED in (out / "summary.md").read_text()
        assert not any(v.encode() in published_bytes(out) for v in values if v)
    with tempfile.TemporaryDirectory() as tmp:
        # Nothing to find: the files as they are, the record as it is.
        stage, out = staged(Path(tmp))
        record = {"schema": SCHEMA, "mode": "quick", "ok": True, "blocks": []}
        scan = publish(stage, out, record, values)
        assert scan == {"files": 4, "values": 3, "replaced": [], "redacted": []}, scan
        assert json.loads((out / "results.json").read_text()) == record and record["ok"] is True
        assert (out / "lab/a.log").read_bytes() == (stage / "lab/a.log").read_bytes()
        assert (out / "samples.jsonl.gz").read_bytes() == (stage / "samples.jsonl.gz").read_bytes()
        assert "Result: **ok**" in (out / "summary.md").read_text()
    with tempfile.TemporaryDirectory() as tmp:
        # A credential in a log, in the packed ledger, in a file name and in the record.
        stage, out = staged(Path(tmp), log="0123abcd", rows="feedf00d", in_name=True)
        record = {"schema": SCHEMA, "mode": "quick", "ok": True, "blocks": [],
                  "error": {"type": "RuntimeError", "at": []}, "note": "psql said 0123abcd is wrong",
                  "warnings": ["kept"]}
        scan = publish(stage, out, record, values)
        assert scan["files"] == 4 and len(scan["replaced"]) == 3, scan
        assert scan["redacted"] == ["results.json"], scan
        assert not any(v.encode() in published_bytes(out) for v in values if v), "a credential was published"
        result = json.loads((out / "results.json").read_text())
        assert result["ok"] is False and record["ok"] is False and result["warnings"] == ["kept"]
        assert result["note"] == "psql said [REDACTED] is wrong" and result["error"]["type"] == "RuntimeError", result
        assert result["credential_scan"] == scan
        text = (out / "summary.md").read_text()
        assert "NOT OK" in text and "RuntimeError" in text and "3 not published" in text
        assert (out / "lab/b.log").read_text() == "nothing\n" and not (out / "lab/a.log").exists()
        assert sorted(p.name for p in out.iterdir()) == [
            "lab", "not-published-0.txt", "not-published-1.txt", "not-published-2.txt", "results.json", "summary.md"]
    with tempfile.TemporaryDirectory() as tmp:
        # A packed ledger that stops before its end, and a link: not copied.
        stage, out = staged(Path(tmp))
        packed = (stage / "samples.jsonl.gz").read_bytes()
        (stage / "samples.jsonl.gz").write_bytes(packed[:len(packed) // 2])
        (stage / "lab/link.log").symlink_to(stage / "lab/b.log")
        record = {"schema": SCHEMA, "mode": "quick", "ok": True, "blocks": []}
        scan = publish(stage, out, record, values)
        assert scan["files"] == 5 and len(scan["replaced"]) == 2 and record["ok"] is False, scan
        assert not (out / "samples.jsonl.gz").exists() and not (out / "lab/link.log").exists()
        assert (out / "lab/b.log").read_text() == "nothing\n"
    try:
        raise RuntimeError("psql said 0123abcd is wrong")
    except RuntimeError as exc:
        error = safe_error(exc)
    assert error["type"] == "RuntimeError" and len(error["at"]) == 1 and "self_test" in error["at"][0], error
    assert "0123abcd" not in json.dumps(error)
    text = summary({"mode": "quick", "ok": False, "blocks": [], "error": error})
    assert "RuntimeError" in text and "0123abcd" not in text
    assert "not published ['x.log']" in summary({"mode": "quick", "ok": False, "blocks": [],
                                                "credential_scan": {"files": 2, "replaced": ["x.log"]}})

    def timed(site, kind, ms, ok=True):
        return {"phase": "timed", "site": site, "kind": kind, "ok": ok, "arrival": 0, "start": 0.001, "end": ms / 1000}

    slow = groups([timed("b", "list", ms) for ms in range(100, 300, 10)] + [timed("b", "dup", 400, ok=False),
                  {**timed("a", "list", 5), "phase": "warm"}])
    fast = groups([timed("b", "list", ms) for ms in range(50, 150, 5)] + [timed("b", "dup", 90)])
    assert slow["b/list"]["p50_ms"] == 190 and slow["b/list"]["p95_ms"] == 280 and slow["b/list"]["ok"] == 20
    assert slow["b/duplicate"] == {**slow["b/duplicate"], "offered": 1, "ok": 0, "p95_ms": None} and "a/list" not in slow
    delta = differences([{"arm": "L", "groups": slow}, {"arm": "P", "groups": fast}], (("L", "P"), ("P", "N")))
    assert delta == {"P-L": {"b/list": {"p50_ms": -95.0, "p95_ms": -140.0}, "b/duplicate": {}}}, delta
    assert target_problems({"attributes": '{"keep": "1", "ip": "x"}', "status": "ack", "history": ["ack/ack/minor"]}) == []
    assert len(target_problems({"attributes": '{"zone": "lab"}', "status": "open", "history": []})) == 4
    assert change_request("null", "x") == ("/alert/x/attributes", {"attributes": {"zone": None}})
    text = summary({"mode": "quick", "ok": False, "blocks": [], "error": "x"})
    assert "NOT OK" in text and "stopped" in text
    assert blob_id(ADDON) == ADDON_BLOB, "scripts/bench/alerta_fast.py is not the pinned blob"
    print("placement self-test: passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--mode", choices=MODES, default="quick")
    for name in ("etcd", "haproxy", "pgbouncer", "pg-bin", "venv", "output"):
        parser.add_argument(f"--{name}", type=lambda item: Path(item).resolve())
    tools = parser.parse_args()
    if tools.self_test:
        return self_test()
    if None in (tools.etcd, tools.haproxy, tools.pgbouncer, tools.pg_bin, tools.venv, tools.output):
        parser.error("the tool paths and --output are required")
    return asyncio.run(run(tools))


if __name__ == "__main__":
    sys.exit(main())
