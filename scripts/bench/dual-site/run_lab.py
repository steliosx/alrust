#!/usr/bin/env python3
"""Run a bounded dual-site Alerta diagnostic on disposable loopback processes."""

import argparse
import asyncio
import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
import csv
import gzip
import hashlib
from http.client import HTTPConnection, HTTPException
import io
import itertools
import json
import math
import os
from pathlib import Path
import random
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dual_site_quorum import calibrate, delayed_link  # noqa: E402


HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PACKET_HOSTS = {"sql": "127.0.0.2", "wal": "127.0.0.3",
                "dcs": "127.0.0.4", "http": "127.0.0.5"}
SITES = ("a", "b")
MEMBERS = (*SITES, "c")
EVENT = "LabAlert"
DEFAULT_PAGE_SIZE = 25
COUNT = "/alerts/count"
RESOURCE_FILTERS = {"resource": "lab-bulk-12", "resource-broad": "lab-bulk-"}
CLONE_STRATEGIES = {"wal-log": "WAL_LOG", "file-copy": "FILE_COPY"}
READ_FILTERS = {
    "all": ("", ""),
    "major": ("severity=major", " WHERE severity='major'"),
    **{name: (f"resource=~{term}", f" WHERE resource ILIKE '%{term}%'")
       for name, term in RESOURCE_FILTERS.items()},
}
NEWEST_INDEX = "CREATE INDEX lab_alerts_newest ON alerts (last_receive_time DESC)"
COUNT_COVER_INDEX = "CREATE INDEX lab_alerts_resource_counts ON alerts (severity, status, resource)"
SEARCH_INDEX_SQL = {
    "stock": (),
    "severity-time": ("CREATE INDEX lab_alerts_severity_time ON alerts (severity, last_receive_time DESC)",),
    "resource-trigram": ("CREATE EXTENSION pg_trgm",
                         "CREATE INDEX lab_alerts_resource_trgm ON alerts USING gin (resource gin_trgm_ops)"),
    "newest": (NEWEST_INDEX,),
    "resource-count-cover": (COUNT_COVER_INDEX,),
    "newest-and-count-cover": (NEWEST_INDEX, COUNT_COVER_INDEX),
}
FEATURES = ("stock", "reuse", "hot", "nolog", "onecount", "nosentry", "all")
WRITE_IDENTITIES = tuple(f"repeat-{i:02d}" for i in range(24))
KEEP = threading.local()


class ThermalLimit(RuntimeError):
    """The measurement reached the laptop's load-stop threshold."""


@contextmanager
def lab_directory(output):
    path = Path(tempfile.mkdtemp(prefix="alrust-lab-"))
    try:
        yield path
    finally:
        remaining = [str(path / f"pg-{site}") for site in SITES
                     if (path / f"pg-{site}/postmaster.pid").exists()]
        if remaining:
            (output / "cleanup.json").write_text(json.dumps({"retained_pg_data": remaining}) + "\n")
            raise RuntimeError("PostgreSQL cleanup could not be confirmed. The private lab data was retained.")
        shutil.rmtree(path)


def temperature():
    paths = [p for d in Path("/sys/class/hwmon").glob("hwmon*")
             if (d / "name").read_text().strip() == "coretemp"
             for p in d.glob("temp*_input")]
    if not paths:
        raise RuntimeError("No coretemp inputs for the laptop thermal limit")
    if any(int(p.with_name(p.name.replace("_input", "_crit_alarm")).read_text()) for p in paths):
        raise RuntimeError("CPU thermal alarm")
    return max(int(path.read_text()) / 1000 for path in paths)


def cgroup_root():
    for record in Path("/proc/self/cgroup").read_text().splitlines():
        if record.startswith("0::"):
            return Path("/sys/fs/cgroup") / record[3:].lstrip("/")
    return None


def resource_caps():
    root = cgroup_root()
    return {name: (root / name).read_text().strip()
            for name in ("cpu.max", "memory.max", "memory.swap.max")
            if root is not None and (root / name).is_file()}


def cgroup_memory_stats():
    root = cgroup_root()
    if root is None:
        return {}
    readings = {}
    for file, name in (("memory.peak", "peak_bytes"), ("memory.current", "current_bytes")):
        with suppress(OSError, ValueError):
            readings[name] = int((root / file).read_text().strip())
    with suppress(OSError, ValueError):
        events = dict(line.split() for line in (root / "memory.events").read_text().splitlines())
        if "oom_kill" in events:
            readings["oom_kill_count"] = int(events["oom_kill"])
    return readings


def cgroup_cpu_stats():
    root = cgroup_root()
    if root is None or not (root / "cpu.stat").is_file():
        return {}
    fields = {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"}
    lines = (root / "cpu.stat").read_text().splitlines()
    return {field: int(value) for field, value in (line.split() for line in lines) if field in fields}


def reserve(names):
    sockets = {}
    try:
        for name in names:
            sock = sockets[name] = socket.socket()
            sock.bind((HOST, 0))
        return {name: sock.getsockname()[1] for name, sock in sockets.items()}
    finally:
        for sock in sockets.values():
            sock.close()


def addr(port, host=HOST):
    return f"{host}:{port}"


class ResponseEncodingError(HTTPException):
    """A synthetic response violated the requested representation contract."""


def response_entity(response, wire, requested_encoding):
    encoding = response.getheader("Content-Encoding", "identity").strip().lower()
    vary = {value.strip().lower() for value in response.getheader("Vary", "").split(",")}
    if response.status == 200 and requested_encoding is not None:
        if not ({"accept-encoding", "*"} & vary):
            raise ResponseEncodingError("missing_accept_encoding_vary")
        if encoding != requested_encoding:
            raise ResponseEncodingError("negotiated_encoding_mismatch")
    if encoding == "identity":
        body = wire
    elif encoding == "gzip" and requested_encoding == "gzip":
        try:
            body = gzip.decompress(wire)
        except (OSError, EOFError, zlib.error) as exc:
            raise ResponseEncodingError("invalid_gzip_body") from exc
    else:
        raise ResponseEncodingError("unexpected_content_encoding")
    return body, {"encoding": encoding, "encoded_body_bytes": len(wire), "decoded_body_bytes": len(body)}


def http(port, path, key=None, alert=None, epoch=None, host=HOST, trace=False, idle_limit=20,
         requested_encoding=None, trace_socket=False):
    connections = getattr(KEEP, "connections", None)
    if connections is None:
        connections = KEEP.connections = {}
    if key and getattr(KEEP, "epoch", None) != epoch:
        for previous in tuple(connections):
            if previous[1]:
                connections.pop(previous)[0].close()
        KEEP.epoch = epoch
    slot = (host, bool(key), port)
    connection, finished = connections.get(slot, (None, None))
    idle_age = time.monotonic() - finished if finished is not None else None
    expired = connection is not None and connection.sock is not None and idle_age >= idle_limit
    if expired:
        connection.close()
    if connection is None:
        connection = HTTPConnection(host, port, timeout=4)
    reused = connection.sock is not None
    headers = {"Accept": "application/json", "Accept-Encoding": requested_encoding or "identity"}
    if key:
        headers["Authorization"] = f"Key {key}"
    body = None
    if alert is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(alert)
    phase = "request"
    try:
        connection.request("POST" if alert else "GET", path, body=body, headers=headers)
        if trace_socket:
            # Linux SO_COOKIE identifies the actual connected socket even when
            # an HTTPConnection object, port number, or file descriptor is reused.
            cookie = int.from_bytes(connection.sock.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_COOKIE", 57), 8),
                                    sys.byteorder)
        phase = "response_headers"
        response = connection.getresponse()
        phase = "response_body"
        wire = response.read()
        raw, entity = response_entity(response, wire, requested_encoding)
        if trace and (cache_result := response.getheader("X-Bench-Cache")):
            entity["cache_result"] = cache_result
        if trace_socket:
            entity.update(socket_cookie=cookie, encoded_sha256=hashlib.sha256(wire).hexdigest())
            app_site = response.getheader("X-Alrust-Bench-App-Site")
            if app_site is not None:
                entity["app_site"] = app_site
        if trace:
            return (response.status, raw, response.getheader("X-Alrust-Bench-Site"),
                    response.getheader("X-Alrust-Bench-Worker"),
                    {"reused": reused, "idle_expired": expired, **entity})
        return response.status, raw
    except (OSError, HTTPException) as exc:
        connection.close()
        if trace:
            return (None, None, None, None,
                    {"reused": reused, "idle_expired": expired, "phase": phase,
                     "error": str(exc) if isinstance(exc, ResponseEncodingError) else type(exc).__name__})
        raise
    finally:
        connections[slot] = (connection, time.monotonic())


def alert_for(resource, i=0):
    return {"resource": resource, "event": EVENT, "environment": "Production",
            "severity": ("major", "minor", "warning")[i % 3], "service": ["lab"],
            "tags": ["synthetic"], "attributes": {"zone": "lab"}}


def read_contract_error(data, kind, totals, read_filter, page_size=DEFAULT_PAGE_SIZE):
    if not isinstance(data, dict) or data.get("status") != "ok":
        return "invalid_read_body"
    if type(data.get("total")) is not int or any(data.get(name) != value for name, value in totals.items()):
        return "wrong_read_totals"
    for group in ("severityCounts", "statusCounts"):
        counts = data[group]
        if not isinstance(counts, dict) or any(type(count) is not int for count in counts.values()):
            return "invalid_read_group_count"
    if kind == "count":
        return None
    pages = math.ceil(totals["total"] / page_size)
    if (type(data.get("page")) is not int or data["page"] != 1
            or type(data.get("pageSize")) is not int or data["pageSize"] != page_size
            or type(data.get("pages")) is not int or data["pages"] != pages
            or data.get("more") is not (pages > 1)):
        return "wrong_read_paging"
    alerts = data.get("alerts")
    if not isinstance(alerts, list) or len(alerts) != min(page_size, totals["total"]):
        return "wrong_read_page_size"
    if any(not isinstance(alert, dict) or not isinstance(alert.get("id"), str) for alert in alerts):
        return "invalid_read_alert"
    if len({alert["id"] for alert in alerts}) != len(alerts):
        return "duplicate_read_alert"
    if read_filter == "major" and any(alert.get("severity") != "major" for alert in alerts):
        return "wrong_read_severity"
    term = RESOURCE_FILTERS.get(read_filter)
    if term and any(not isinstance(alert.get("resource"), str) or
                    term not in alert["resource"].casefold() for alert in alerts):
        return "wrong_read_resource"
    times = [alert.get("lastReceiveTime") for alert in alerts]
    if any(not isinstance(value, str) for value in times) or times != sorted(times, reverse=True):
        return "wrong_read_order"
    return None


def offered_times(rate, seconds, burst_rate=None, burst_seconds=0):
    start = (seconds - burst_seconds) // 2
    for second in range(seconds):
        current_rate = burst_rate if burst_rate and start <= second < start + burst_seconds else rate
        for request in range(current_rate):
            yield second + request / current_rate


def cpu_ticks(roots):
    """Sample live CPU and reaped descendants of the selected lab processes."""
    processes = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            stat = path.read_text().rsplit(")", 1)[1].split()
            processes[int(path.parent.name)] = (int(stat[1]), sum(map(int, stat[11:15])))
        except (OSError, ValueError, IndexError):
            continue
    selected = set(roots) & processes.keys()
    frontier = list(selected)
    while frontier:
        parent = frontier.pop()
        children = {pid for pid, (ppid, _) in processes.items() if ppid == parent} - selected
        selected.update(children)
        frontier.extend(children)
    return sum(processes[pid][1] for pid in selected)


async def wait_until(check, description, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if await asyncio.to_thread(check):
                return
        except (OSError, HTTPException, subprocess.SubprocessError):
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}")


async def measure_http_link_rate():
    """Measure one 256 KiB response on the WAN HTTP packet class, before service start."""
    payload = bytes(256 * 1024)

    async def serve(reader, writer):
        try:
            await reader.readexactly(1)
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    host = PACKET_HOSTS["http"]
    async with await asyncio.start_server(serve, host, 0) as server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection(host, port, local_addr=(HOST, 0))
        try:
            start = time.monotonic()
            writer.write(b"?")
            await writer.drain()
            received = await asyncio.wait_for(reader.readexactly(len(payload)), 8)
            elapsed = time.monotonic() - start
            if received != payload:
                raise RuntimeError("The WAN HTTP rate calibration payload changed")
        finally:
            writer.close()
            await writer.wait_closed()
    return {"payload_bytes": len(payload), "elapsed_ms": round(elapsed * 1000, 2),
            "effective_mbit_s": round(len(payload) * 8 / elapsed / 1_000_000, 3)}


def stop_process(proc):
    if proc.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@asynccontextmanager
async def process(argv, env, log):
    with log.open("wb") as output:
        proc = subprocess.Popen(argv, env=env, stdout=output, stderr=output, start_new_session=True)
        try:
            yield proc
        finally:
            await asyncio.to_thread(stop_process, proc)


class Lab:
    thermal_telemetry = "coretemp"

    def __init__(self, args, root, logdir):
        self.args, self.root, self.logs = args, root, logdir
        self.pg = args.pg_bin
        self.venv = args.venv / "bin"
        self.env = {"PATH": f"{self.venv}:{self.pg}:/usr/bin:/bin",
                    "HOME": str(root), "LC_ALL": "C", "GOMAXPROCS": "1"}
        self.pg_env = self.env
        if args.pg_library_dir:
            self.pg_env = {**self.env, "LD_LIBRARY_PATH": str(args.pg_library_dir)}
        self.key = secrets.token_hex(20)
        self.client_keys = [secrets.token_hex(20) for _ in range(args.key_count)]
        self.password = secrets.token_hex(20)
        self.epoch = None
        self.site_processes = defaultdict(list)
        self.app_processes = defaultdict(list)
        self.wal_links = AsyncExitStack()
        self.dcs_client_links = AsyncExitStack()
        self.restart_specs = {}
        self.peak_cpu = 0.0
        self.paths = {"write": "/alert", "list": f"/alerts?page-size={args.page_size}&sort-by=lastReceiveTime",
                      "count": COUNT}
        query, self.filter_sql = READ_FILTERS[args.read_filter]
        if query:
            self.paths["list"] += "&" + query
            self.paths["count"] += "?" + query
        self.ports = reserve(
            [(site, role) for site in SITES
             for role in ("pg", "patroni", "pooler", "http", "writer", "vm1", "vm2", "affinity")]
            + [(site, role) for site in MEMBERS for role in ("peer", "dcs")]
        )
        self.ingress = {site: (self.p(site, "http"), HOST) for site in SITES}

    def p(self, site, role):
        return self.ports[site, role]

    def read_temperature(self):
        current = temperature()
        self.peak_cpu = current if self.peak_cpu is None else max(self.peak_cpu, current)
        return current

    def sql(self, db, sql, site="a", role="pg", headers=False):
        formatting = ["--csv", "-q"] if headers else ["-Atq"]
        return subprocess.check_output(
            [self.args.psql, "-X", "-w", *formatting, "-v", "ON_ERROR_STOP=1", "-h", HOST,
             "-p", str(self.p(site, role)), "-U", "postgres", "-d", db, "-c", sql],
            env={**self.pg_env, "PGPASSWORD": self.password, "PGCONNECT_TIMEOUT": "3"},
            timeout=10, text=True, stderr=subprocess.PIPE,
        ).strip()

    def stop_pg(self, site, mode="fast"):
        data = self.root / f"pg-{site}"
        if (data / "postmaster.pid").exists():
            subprocess.run([self.pg / "pg_ctl", "-D", data, "-m", mode, "-w", "-t", "10", "stop"],
                           env=self.pg_env, timeout=13, check=True, capture_output=True)

    async def launch(self, stack, site, command, env, filename):
        proc = await stack.enter_async_context(process(command, env, self.logs / filename))
        self.site_processes[site].append(proc)
        if self.args.rejoin and filename in ("etcd-a.log", "patroni-a.log"):
            self.restart_specs[filename] = (site, command, env)
        return proc

    async def link(self, stack, target, rtt, kind):
        host = PACKET_HOSTS[kind] if self.args.netem else HOST
        delay = 0 if self.args.netem else rtt / 2000
        return await stack.enter_async_context(delayed_link(target, delay, host)), host

    async def start(self, stack):
        wal_rtt = self.args.rtt_ms if self.args.wal_rtt_ms is None else self.args.wal_rtt_ms
        profiles = {"sql": self.args.rtt_ms, "wal": wal_rtt, "dcs": self.args.dcs_rtt_ms,
                    "http": self.args.rtt_ms}
        network = {}
        for kind, rtt in profiles.items():
            host = PACKET_HOSTS[kind] if self.args.netem else HOST
            delay = 0 if self.args.netem else rtt / 2000
            reading = await asyncio.wait_for(calibrate(delay, host), 10)
            jitter = int(os.environ.get("ALRUST_NETEM_JITTER_MS", "0")) if self.args.netem else 0
            if abs(reading["median_rtt_ms"] - rtt) > max(12, 2 * jitter + rtt / 4):
                raise RuntimeError(f"{kind} link RTT did not match its profile: {reading}")
            network[kind] = reading
        if self.args.http_rate_mbit:
            network["http"]["bulk_read"] = await measure_http_link_rate()
        dcs = ",".join(addr(self.p(site, "dcs")) for site in MEMBERS)
        peers = {}
        for site in MEMBERS:
            target = self.p(site, "peer")
            advertised = await self.link(stack, target, self.args.dcs_rtt_ms, "dcs"
                                         ) if self.args.dcs_rtt_ms else (target, HOST)
            peers[site] = f"http://{addr(*advertised)}"
        membership = ",".join(f"{site}={url}" for site, url in peers.items())
        for site in MEMBERS:
            endpoint = f"http://{addr(self.p(site, 'dcs'))}"
            await self.launch(stack, site,
                [self.args.etcd, "--name", site, "--data-dir", self.root / f"etcd-{site}",
                 "--listen-peer-urls", f"http://{addr(self.p(site, 'peer'))}",
                 "--initial-advertise-peer-urls", peers[site],
                 "--listen-client-urls", endpoint, "--advertise-client-urls", endpoint,
                 "--initial-cluster", membership, "--initial-cluster-token", self.key,
                 "--log-level", "warn"], self.env, f"etcd-{site}.log")
        await wait_until(lambda: all(http(self.p(s, "dcs"), "/health")[0] == 200 for s in MEMBERS),
                         "the three etcd members")
        dcs_settings = {site: {"hosts": dcs} for site in SITES}
        if self.args.fault_target == "dcs-client":
            await stack.enter_async_context(self.dcs_client_links)
            relays = [
                await self.link(self.dcs_client_links, self.p(member, "dcs"), self.args.dcs_rtt_ms, "dcs")
                for member in MEMBERS
            ]
            dcs_settings["a"] = {"hosts": ",".join(addr(*endpoint) for endpoint in relays), "use_proxies": True}

        # Separate remote SQL from WAL so one path can be varied independently.
        remote = {site: await self.link(stack, self.p(site, "pg"), self.args.rtt_ms, "sql")
                  for site in SITES}
        await stack.enter_async_context(self.wal_links)
        replication = {site: await self.link(self.wal_links, self.p(site, "pg"), wal_rtt, "wal")
                       for site in SITES}
        if self.args.ingress == "primary-aware":
            other_http = {site: await self.link(stack, self.p(site, "http"), self.args.rtt_ms, "http")
                          for site in SITES}
        for site in SITES:
            config = {
                "scope": "alerta-lab", "name": site, "etcd3": dcs_settings[site],
                "restapi": {"listen": addr(self.p(site, "patroni")),
                            "connect_address": addr(self.p(site, "patroni"))},
                "bootstrap": {
                    "dcs": {"ttl": self.args.dcs_ttl, "loop_wait": self.args.patroni_loop,
                            "retry_timeout": self.args.patroni_retry,
                            "maximum_lag_on_failover": self.args.failover_lag, "synchronous_mode": False,
                            "check_timeline": True,
                            "postgresql": {"use_pg_rewind": True, "use_slots": True,
                                           "parameters": {"shared_buffers": "48MB",
                                                          "max_connections": 80,
                                                          "log_min_messages": "warning"}}},
                    "initdb": ["data-checksums"],
                    "pg_hba": ["local all all trust",
                               "host replication replicator 127.0.0.1/32 scram-sha-256",
                               "host all all 127.0.0.1/32 scram-sha-256"],
                },
                "postgresql": {"listen": addr(self.p(site, "pg")),
                               "connect_address": addr(*replication[site]),
                               "bin_dir": str(self.pg), "data_dir": str(self.root / f"pg-{site}"),
                               "parameters": {"unix_socket_directories": str(self.root)},
                               "authentication": {"superuser": {"username": "postgres",
                                                                "password": self.password},
                                                  "replication": {"username": "replicator",
                                                                  "password": self.password}}},
                "watchdog": {"mode": "off"},
            }
            if self.args.file_copy_method == "clone":
                config["postgresql"]["parameters"]["file_copy_method"] = "clone"
            path = self.root / f"patroni-{site}.json"
            path.write_text(json.dumps(config))
            stack.push_async_callback(asyncio.to_thread, self.stop_pg, site)
            await self.launch(stack, site, [self.venv / "patroni", path], self.pg_env, f"patroni-{site}.log")
            endpoint = "/primary" if site == "a" else "/replica"
            await wait_until(lambda: http(self.p(site, "patroni"), endpoint)[0] == 200,
                             f"Patroni site {site} {endpoint}", seconds=75)

        for site in SITES:
            env = {**self.env, "ALERTA_BIND": addr(self.p(site, "http")),
                   "WRITER_BIND": addr(self.p(site, "writer")),
                   "ALERTA_CHECK_INTER": f"{self.args.health_interval}ms",
                   "ALERTA_CHECK_TIMEOUT": f"{self.args.health_timeout}ms",
                   "WRITER_IDLE_TIMEOUT": f"{self.args.writer_idle}s",
                   "ALERTA_VM_1": addr(self.p(site, "vm1")), "ALERTA_VM_2": addr(self.p(site, "vm2"))}
            if self.args.dashboard_encoding:
                env["ALERTA_LAB_DASHBOARD_ENCODING"] = "1"
            for db_site in SITES:
                env[f"POSTGRES_{db_site.upper()}"] = addr(self.p(db_site, "pg")) if site == db_site else addr(
                    *remote[db_site])
                env[f"PATRONI_PORT_{db_site.upper()}"] = str(self.p(db_site, "patroni"))
            if getattr(self.args, "operator_route_trace", False):
                env["ALERTA_OPERATOR_APP_SITE"] = site
                # Bind the actual writer-backend destination used by this site's HAProxy.
                if site == "a":
                    correct = env["POSTGRES_A"] == addr(self.p("a", "pg"))
                else:
                    correct = env["POSTGRES_A"] == addr(*remote["a"]) and remote["a"][1] == PACKET_HOSTS["sql"]
                if not correct:
                    raise ValueError("Operator writer route differs from its configured local or emulated SQL path")
            command = [self.args.haproxy, "-db", "-f", HERE / "haproxy.cfg"]
            if self.args.health_trace:
                trace_config = self.root / f"haproxy-trace-{site}.cfg"
                trace_config.write_text(f"global\n    stats socket {self.ha_socket(site)} mode 0600 level user\n")
                command += ["-f", trace_config]
            if self.args.ingress == "primary-aware":
                affinity = self.p(site, "affinity")
                env["AFFINITY_BIND"] = addr(affinity)
                for app_site in SITES:
                    env[f"SITE_{app_site.upper()}_HTTP"] = (
                        addr(self.p(app_site, "http")) if site == app_site else addr(*other_http[app_site])
                    )
                command += ["-f", HERE / "haproxy-affinity.cfg"]
                self.ingress[site] = (affinity, HOST)
            await self.launch(stack, site, command,
                        env, f"haproxy-{site}.log")
        if self.args.ingress == "primary":
            self.ingress["b"] = await self.link(stack, self.p("a", "http"), self.args.rtt_ms, "http")
        await self.start_pools(stack)
        pools = {site: self.pool_config(site) for site in SITES}
        copy_methods = {site: self.sql("postgres", "SELECT current_setting('file_copy_method', true)", site) or None
                        for site in SITES}
        if self.args.file_copy_method == "clone" and any(method != "clone" for method in copy_methods.values()):
            raise RuntimeError(f"Both PostgreSQL nodes must use file_copy_method=clone: {copy_methods}")
        with (self.logs / "topology.json").open("w") as record:
            json.dump({"postgres_a_role": self.sql("postgres", "SELECT pg_is_in_recovery()"),
                       "postgres_b_role": self.sql("postgres", "SELECT pg_is_in_recovery()", "b"),
                       "postgres_version": self.sql("postgres", "SHOW server_version"),
                       "postgres_io_method": self.sql("postgres", "SELECT current_setting('io_method', true)") or None,
                       "postgres_toast_compression": self.sql("postgres", "SHOW default_toast_compression"),
                       "postgres_wal_compression": self.sql("postgres", "SHOW wal_compression"),
                       "postgres_file_copy_methods": copy_methods,
                       "streaming": self.sql("postgres", "SELECT state FROM pg_stat_replication"),
                       "local_commit": self.sql("postgres", "SHOW synchronous_commit"),
                       "replication_sync_state": self.sql("postgres", "SELECT sync_state FROM pg_stat_replication"),
                       "link_calibration": network,
                       "pool_settings": pools,
                       "scope": "four Alerta processes on one host with emulated link profiles"}, record)

    def ha_socket(self, site):
        return self.root / f"haproxy-{site}.sock"

    def ha_states(self, site):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(self.ha_socket(site)))
            client.sendall(b"show stat\n")
            client.shutdown(socket.SHUT_WR)
            with client.makefile("r") as response:
                header = response.readline().removeprefix("# ")
                reports = csv.DictReader(itertools.chain((header,), response))
                if not {"pxname", "svname", "status"} <= set(reports.fieldnames or ()):
                    raise OSError("The lab HAProxy stats response has no server state columns")
                return {f"{row['pxname']}/{row['svname']}": row["status"]
                        for row in reports if row["svname"] not in ("FRONTEND", "BACKEND")}

    async def trace_health(self, start, deadline):
        transitions = []
        previous = {}
        while time.monotonic() < deadline:
            try:
                observed = await asyncio.to_thread(self.ha_states, "b")
                if observed != previous:
                    transitions.append({"seconds_from_fault": round(time.monotonic() - start, 3),
                                        "servers": observed})
                    previous = observed
            except OSError as exc:
                previous = {}
                error = type(exc).__name__
                if not transitions or transitions[-1].get("error") != error:
                    transitions.append({"seconds_from_fault": round(time.monotonic() - start, 3),
                                        "error": error})
            await asyncio.sleep(0.25)
        return transitions

    async def start_pools(self, stack):
        auth = self.root / "users.txt"
        auth.write_text(f'"postgres" "{self.password}"\n')
        for site in SITES:
            cfg = self.root / f"pgbouncer-{site}.ini"
            cfg.write_text(f"""[databases]
* = host={HOST} port={self.p(site, 'writer')}
[pgbouncer]
listen_addr = {HOST}
listen_port = {self.p(site, 'pooler')}
unix_socket_dir =
auth_type = scram-sha-256
auth_file = {auth}
admin_users = postgres
pool_mode = {self.args.pool_mode}
default_pool_size = {self.args.pool_size}
server_idle_timeout = {self.args.pool_idle}
server_login_retry = {self.args.pool_login_retry}
server_connect_timeout = {self.args.pool_connect_timeout}
query_wait_timeout = {self.args.pool_query_wait}
max_client_conn = 64
ignore_startup_parameters = extra_float_digits,options
""")
            await self.launch(stack, site, [self.args.pgbouncer, cfg], self.env, f"pgbouncer-{site}.log")
            await wait_until(lambda: self.sql("postgres", "SELECT 1", site, "pooler") == "1",
                             f"PgBouncer site {site}")

    def pool_config(self, site):
        expected = {
            "pool_mode": self.args.pool_mode, "default_pool_size": self.args.pool_size,
            "server_idle_timeout": self.args.pool_idle, "server_login_retry": self.args.pool_login_retry,
            "server_connect_timeout": self.args.pool_connect_timeout,
            "query_wait_timeout": self.args.pool_query_wait,
        }
        rows = self.sql("pgbouncer", "SHOW CONFIG", site, "pooler", headers=True)
        actual = {row["key"]: row["value"] for row in csv.DictReader(io.StringIO(rows))
                  if row["key"] in expected}
        for option, value in expected.items():
            observed = actual.get(option)
            if observed is None or (float(observed) if isinstance(value, int) else observed) != value:
                raise RuntimeError(f"PgBouncer site {site} uses {observed!r} for {option}; expected {value!r}")
        return actual

    async def start_app(self, stack, site, slot, db, feature, label):
        env = {**self.env, "DATABASE_URL": f"postgresql://postgres:{self.password}@"
                                           f"{addr(self.p(site, 'pooler'))}/{db}?connect_timeout=4",
               "AUTH_REQUIRED": "True", "SECRET_KEY": self.key, "LOG_LEVEL": "WARNING",
               "PLUGINS": "remote_ip,reject,heartbeat,blackout"}
        if self.args.forwarder:
            env["PLUGINS"] += ",forwarder"
        command = [self.venv / "gunicorn", "--preload", "--workers", str(self.args.workers),
                   "--worker-class", self.args.worker_class, "--threads", str(self.args.threads),
                   "--bind", addr(self.p(site, slot))]
        if feature == "stock":
            command += ["alerta.app:create_app()"]
        else:
            env.update(ALERTA_FAST=feature.replace("+", ","),
                       ALERTA_FAST_FLUSH_SECONDS=str(self.args.flush),
                       ALERTA_FAST_CONN_MAX_AGE=str(self.args.max_age))
            command += ["--pythonpath", str(self.args.addon.parent), "alerta_fast:app"]
        proc = await self.launch(stack, site, command, env, f"{label}-{site}-{slot}.log")
        self.app_processes[site].append(proc)
        await wait_until(lambda: http(self.p(site, slot), "/management/gtg")[0] == 200,
                         f"Alerta {label} {site} {slot}", seconds=20)

    async def seed(self):
        identities = (*WRITE_IDENTITIES,
                      *(f"seed-{i:04d}" for i in range(self.args.seed_alerts - len(WRITE_IDENTITIES))))
        self.sql("postgres", "CREATE DATABASE bench_seed")
        async with AsyncExitStack() as app:
            await self.start_app(app, "a", "vm1", "bench_seed", "stock", "seed")
            for slot, key in enumerate(self.client_keys):
                self.sql("bench_seed", 'INSERT INTO keys (id,key,"user",scopes,text,expire_time,count) '
                         f"VALUES ('bench-key-{slot}','{key}','bench',ARRAY['admin','read','write'],"
                         "'benchmark',timezone('UTC',now())+interval '1 day',0)")
            for i, resource in enumerate(identities):
                if i % 8 == 0 and (reading := self.read_temperature()) is not None and reading >= 80:
                    await self.cool()
                # Seeding talks directly to Gunicorn and may pause for cooling.
                # Retire its HTTP socket before Gunicorn's two-second idle timeout.
                status, body = await asyncio.to_thread(
                    http, self.p("a", "vm1"), "/alert", self.client_keys[0], alert_for(resource, i), idle_limit=1
                )
                if status != 201:
                    raise RuntimeError(f"Seed failed at {resource} with HTTP {status}: {body[:160]!r}")
                await asyncio.sleep(0.025)
        if self.args.bulk_alerts:
            await self.cool()
            await asyncio.to_thread(self.extend_seed)
        if self.args.search_index != "stock":
            await asyncio.to_thread(self.prepare_search_index, "bench_seed")
        expected_rows = len(identities) + self.args.bulk_alerts
        seed_lsn = self.sql("postgres", "SELECT pg_current_wal_flush_lsn()")
        await self.wait_for_replay(seed_lsn, "the standby to replay the completed seed")
        await wait_until(lambda: self.sql("bench_seed", "SELECT count(*) FROM alerts", "b")
                         == str(expected_rows), "the seed to reach the replica")
        self.sql("postgres", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                             "WHERE datname='bench_seed'")

    async def wait_for_replay(self, lsn, description):
        await wait_until(lambda: self.sql(
            "postgres", f"SELECT pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn", "b"
        ) == "t", description)

    async def clone_fixture(self, db):
        wal_before = None
        if self.args.bulk_alerts:
            wal_before = await asyncio.to_thread(self.sql, "postgres", "SELECT pg_current_wal_insert_lsn()")
        start = time.monotonic()
        strategy = CLONE_STRATEGIES[self.args.clone_strategy]
        await asyncio.to_thread(self.sql, "postgres", f"CREATE DATABASE {db} TEMPLATE bench_seed STRATEGY {strategy}")
        clone = {"fixture_clone_ms": round((time.monotonic() - start) * 1000, 3)}
        if wal_before is not None:
            wal, lsn = (await asyncio.to_thread(
                self.sql, "postgres",
                f"SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), '{wal_before}'), pg_current_wal_flush_lsn()"
            )).split("|")
            await self.wait_for_replay(lsn, f"the standby to replay fixture {db}")
            clone["fixture_clone_to_replay_ms"] = round((time.monotonic() - start) * 1000, 3)
            clone["fixture_clone_cluster_wal_bytes"] = int(wal)
        return clone

    def extend_seed(self):
        # Only the lab's additional background rows use SQL seeding. The
        # duplicate identities and each exemplar are generated by Alerta.
        self.sql("bench_seed", f"""
            WITH examples AS (
              SELECT DISTINCT ON (severity) a.* FROM alerts a WHERE resource LIKE 'seed-%'
              ORDER BY severity, resource
            ), ranked AS (
              SELECT *, row_number() OVER (ORDER BY severity) - 1 AS severity_slot FROM examples
            )
            INSERT INTO alerts
              (id, resource, event, environment, severity, correlate, status, service, "group",
               value, text, tags, attributes, origin, type, create_time, timeout, raw_data,
               customer, duplicate_count, repeat, previous_severity, trend_indication,
               receive_time, last_receive_id, last_receive_time, history, update_time)
            SELECT 'lab-bulk-' || i, 'lab-bulk-' || i, event, environment, severity, correlate,
                   status, service, "group", value, text, tags, attributes, origin, type, t,
                   timeout, raw_data, customer, duplicate_count, repeat, previous_severity,
                   trend_indication, t, 'lab-bulk-' || i, t, history, t
              FROM generate_series(1,{self.args.bulk_alerts}) i
              JOIN ranked ON severity_slot = i % 3
              CROSS JOIN LATERAL (SELECT timezone('UTC', statement_timestamp())
                                        - i * interval '1 second' AS t) stamp
        """)
        self.sql("bench_seed", "VACUUM (ANALYZE) alerts")

    def prepare_search_index(self, db):
        """Build once so each repetition clones an identical starting index."""
        for statement in SEARCH_INDEX_SQL[self.args.search_index]:
            self.sql(db, statement)

    def search_plans(self, db):
        queries = {
            "page": f"SELECT * FROM alerts{self.filter_sql} ORDER BY last_receive_time DESC LIMIT {self.args.page_size}",
            "groups": f"SELECT severity,status,count(*) FROM alerts{self.filter_sql} GROUP BY severity,status",
        }
        return {name: json.loads(self.sql(
            db, f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) {query}"
        ))[0] for name, query in queries.items()}

    def check_reads(self, db):
        """The fixture's unchanged row count and severity/status groups on both routes."""
        where = self.filter_sql
        expected = int(self.sql(db, f"SELECT count(*) FROM alerts{where}"))
        expected_ids = set(self.sql(
            db, f"SELECT id FROM alerts{where} ORDER BY last_receive_time DESC LIMIT {self.args.page_size}"
        ).splitlines())
        totals = {"total": expected}
        for column in ("severity", "status"):
            sql = (f"SELECT COALESCE(json_object_agg({column},n), '{{}}'::json) FROM "
                   f"(SELECT {column}, count(*) n FROM alerts{where} GROUP BY 1) a")
            totals[f"{column}Counts"] = json.loads(self.sql(db, sql))
        contract = (totals, self.args.read_filter, self.args.page_size)
        results = []
        for site, (port, host) in self.ingress.items():
            status, raw = http(port, self.paths["count"], self.client_keys[0], epoch=self.epoch, host=host)
            if status != 200:
                raise HTTPException(f"Count via site {site} returned HTTP {status}")
            counts = json.loads(raw)
            if error := read_contract_error(counts, "count", *contract):
                raise RuntimeError(f"Count mismatch via site {site}: {error}")
            # Site A supplies the identity control for the cross-route equality check.
            requested = "identity" if self.args.dashboard_encoding and site == "a" else self.args.dashboard_encoding
            status, raw = http(port, self.paths["list"], self.client_keys[0], epoch=self.epoch, host=host,
                               requested_encoding=requested)
            if status != 200:
                raise HTTPException(f"List via site {site} returned HTTP {status}")
            response = json.loads(raw)
            if error := read_contract_error(response, "list", *contract):
                raise RuntimeError(f"List mismatch via site {site}: {error}")
            alerts = response["alerts"]
            if {alert["id"] for alert in alerts} != expected_ids:
                raise RuntimeError(f"List page does not match the database via site {site}")
            results.append({alert["id"]: {k: v for k, v in alert.items() if k != "href"}
                            for alert in alerts})
        if results[0] != results[1]:
            raise RuntimeError("The site list responses disagree")
        return contract

    def duplicates(self, db):
        return json.loads(self.sql(db, "SELECT json_object_agg(resource,duplicate_count) "
                                  "FROM alerts WHERE resource LIKE 'repeat-%'"))

    def database_stats(self, db):
        return json.loads(self.sql("postgres", "SELECT row_to_json(s) FROM "
                                  "(SELECT sessions,xact_commit,xact_rollback,deadlocks "
                                  f"FROM pg_stat_database WHERE datname='{db}') s"))

    def alert_storage_stats(self, db):
        return json.loads(self.sql(
            db, "SELECT row_to_json(s) FROM (SELECT n_tup_upd,n_tup_hot_upd,"
                "pg_indexes_size('alerts') AS index_bytes FROM pg_stat_user_tables WHERE relname='alerts') s"
        ))

    async def confirm_replica_wait(self):
        if int(self.sql("postgres", "SHOW server_version_num")) < 190000:
            raise RuntimeError("The replica WAIT check requires PostgreSQL 19 or later")
        self.sql("postgres", "CREATE TABLE lab_wait (id int PRIMARY KEY)")
        await wait_until(lambda: self.sql("postgres", "SELECT to_regclass('lab_wait')", "b") == "lab_wait",
                         "the WAIT fixture table to reach the replica")
        self.sql("postgres", "SELECT pg_wal_replay_pause()", "b")
        try:
            await wait_until(lambda: self.sql("postgres", "SELECT pg_get_wal_replay_pause_state()", "b") == "paused",
                             "the test replica to pause")
            self.sql("postgres", "INSERT INTO lab_wait VALUES (1)")
            lsn = self.sql("postgres", "SELECT pg_current_wal_flush_lsn()")
            command = f"WAIT FOR LSN '{lsn}' WITH (MODE 'standby_replay', TIMEOUT '250ms', NO_THROW)"
            # Keep the same event loop available to relay WAL while SQL waits.
            deferred = await asyncio.to_thread(self.sql, "postgres", command, "b")
            invisible = self.sql("postgres", "SELECT count(*) FROM lab_wait", "b") == "0"
        finally:
            self.sql("postgres", "SELECT pg_wal_replay_resume()", "b")
        resume_at = time.monotonic()
        resumed = await asyncio.to_thread(self.sql, "postgres", command.replace("250ms", "5s"), "b")
        resume_wait_ms = round((time.monotonic() - resume_at) * 1000, 3)
        visible = self.sql("postgres", "SELECT count(*) FROM lab_wait", "b") == "1"
        record = {"while_paused": deferred, "row_absent_before_replay": invisible,
                  "after_resume": resumed, "row_present_after_replay": visible}
        (self.logs / "replica-wait.json").write_text(json.dumps(
            {**record, "resumed_wait_elapsed_ms": resume_wait_ms}
        ) + "\n")
        if record != {"while_paused": "timeout", "row_absent_before_replay": True,
                      "after_resume": "success", "row_present_after_replay": True}:
            raise RuntimeError("The replica WAIT check did not match its timeout and replay expectations")
        self.sql("postgres", "DROP TABLE lab_wait")

    def cpu_sample(self):
        reading = {f"app_{site}": cpu_ticks(p.pid for p in self.app_processes[site]
                                           if p.poll() is None) for site in SITES}
        reading["postgres_a"] = cpu_ticks([int(
            (self.root / "pg-a/postmaster.pid").read_text().splitlines()[0]
        )])
        return reading

    def pool_stats(self, db):
        result = {}
        for site in SITES:
            rows = self.sql("pgbouncer", "SHOW STATS", site, "pooler", headers=True)
            result[site] = next((r for r in csv.DictReader(io.StringIO(rows))
                                 if r["database"] == db), {})
        return result

    def interrupt_lab_components(self):
        target = self.args.fault_target
        if target == "site-a":
            processes = self.site_processes["a"]
        elif target == "witness":
            processes = self.site_processes["c"]
        else:
            processes = [proc for proc in self.app_processes["a"] if proc.poll() is None]
            if target == "one-app":
                processes = processes[:1]
        stopped = 0
        for proc in reversed(processes):
            if proc.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                    stopped += 1
        if not stopped:
            raise RuntimeError(f"No disposable processes were stopped for {target}")
        if target == "site-a":
            self.stop_pg("a", "immediate")
        return stopped

    async def trigger_fault(self):
        if self.args.fault_target == "dcs-client":
            await self.dcs_client_links.aclose()
            return 0
        return await asyncio.to_thread(self.interrupt_lab_components)

    def replica_gap(self):
        """Describe the replica's received and replayed LSNs just before the crash."""
        received, replayed = self.sql(
            "postgres", "SELECT pg_last_wal_receive_lsn(),pg_last_wal_replay_lsn()", "b"
        ).split("|", 1)
        if not received or not replayed:
            raise RuntimeError("The Site B standby has no observable replication positions")
        gap = json.loads(self.sql("postgres", f"""
            SELECT json_build_object(
              'unreceived_bytes', greatest(0,pg_wal_lsn_diff(pg_current_wal_lsn(),'{received}')),
              'unreplayed_bytes', greatest(0,pg_wal_lsn_diff(pg_current_wal_lsn(),'{replayed}'))
            )
        """))
        status_key = base64.b64encode(b"/service/alerta-lab/status").decode("ascii")
        status, raw = http(self.p("b", "dcs"), "/v3/kv/range", alert={"key": status_key})
        versions = json.loads(raw).get("kvs", []) if status == 200 else []
        if versions:
            dcs_state = json.loads(base64.b64decode(versions[0]["value"]))
            leader_lsn = int(dcs_state.get("optime", 0))
            if leader_lsn:
                left, right = received.split("/", 1)
                receiver_lsn = int(left, 16) * 2 ** 32 + int(right, 16)
                gap["dcs_reported_leader_lsn"] = leader_lsn
                gap["dcs_reported_gap_bytes"] = max(0, leader_lsn - receiver_lsn)
        return gap

    async def observe_primary(self, site, deadline):
        while time.monotonic() < deadline:
            try:
                status, _ = await asyncio.to_thread(http, self.p(site, "patroni"), "/primary")
                if status == 200:
                    return time.monotonic()
            except (OSError, HTTPException):
                pass
            await asyncio.sleep(0.2)
        return None

    async def rejoin_a(self, stack, db, expected):
        start = time.monotonic()
        site, command, env = self.restart_specs["etcd-a.log"]
        await self.launch(stack, site, command, env, "rejoin-etcd-a.log")
        await wait_until(lambda: http(self.p("a", "dcs"), "/health")[0] == 200,
                         "the recovered etcd member")
        site, command, env = self.restart_specs["patroni-a.log"]
        await self.launch(stack, site, command, env, "rejoin-patroni-a.log")
        await wait_until(lambda: http(self.p("a", "patroni"), "/replica")[0] == 200,
                         "the former primary to rejoin as a replica", seconds=75)
        if self.sql("postgres", "SELECT pg_is_in_recovery()", "a") != "t" or self.sql(
            "postgres", "SELECT pg_is_in_recovery()", "b"
        ) != "f":
            raise RuntimeError("Unexpected database roles after the former primary rejoined")
        await wait_until(lambda: set(self.sql(
            db, "SELECT resource FROM alerts WHERE resource LIKE 'fault-%'", "a"
        ).splitlines()) == expected, "the restored replica to replay the fault ledger", seconds=75)
        return {"elapsed_seconds": round(time.monotonic() - start, 3),
                "a_is_standby": True, "b_is_primary": True, "ledger_matches": True}

    async def failover(self, feature):
        """Exercise B's HTTP path during a disposable site or component outage."""
        db = "failover"
        self.epoch = db
        await asyncio.to_thread(self.sql, "postgres", f"CREATE DATABASE {db} TEMPLATE bench_seed")
        async with AsyncExitStack() as apps:
            for site, slot in itertools.product(SITES, ("vm1", "vm2")):
                await self.start_app(apps, site, slot, db, feature, db)
            port, host = self.ingress["b"] if self.args.ingress == "primary-aware" else (self.p("b", "http"), HOST)
            await wait_until(lambda: http(port, "/management/gtg", host=host)[0] == 200,
                             "the surviving site frontend")
            await self.cool()
            setup_peak_cpu = self.peak_cpu
            self.peak_cpu = 0.0
            start = time.monotonic()
            fault_at = None
            wal_cut_at = None
            gap = None
            promotion_expected = self.args.fault_target in ("site-a", "dcs-client")
            crash_index = 25 + 5 * self.args.wal_outage
            crash_due = start + crash_index / 5
            duration = 46 + self.args.wal_outage
            outage_tasks = []
            health_trace = None
            requests = []
            for i in range(5 * duration):
                due = start + i / 5
                await asyncio.sleep(max(0, due - time.monotonic()))
                if i % 2 == 0 and self.read_temperature() >= 84:
                    await asyncio.gather(*requests, *outage_tasks)
                    raise ThermalLimit("Temperature reached the 84 C load-stop limit")
                if i == 25 and self.args.wal_outage:
                    await self.wal_links.aclose()
                    wal_cut_at = time.monotonic()
                if i == crash_index:
                    if wal_cut_at is not None:
                        gap = await asyncio.to_thread(self.replica_gap)
                    fault_at = time.monotonic()
                    outage_tasks = [asyncio.create_task(self.trigger_fault())]
                    if promotion_expected:
                        outage_tasks.append(asyncio.create_task(self.observe_primary("b", start + duration)))
                    if self.args.health_trace:
                        health_trace = asyncio.create_task(self.trace_health(fault_at, start + duration))
                resource = f"fault-{i:04d}"
                requests.append(asyncio.create_task(asyncio.to_thread(
                    timed_request, port, self.client_keys[0], "b", "write", resource,
                    due, self.epoch, 0, host, idle_limit=self.args.client_idle
                )))
            outcomes = await asyncio.gather(*requests)
            completed_outages = await asyncio.gather(*outage_tasks)
            promotion_at = completed_outages[1] if promotion_expected else None
            expected_writer = "b" if promotion_expected else "a"
            final_writer = expected_writer if (
                http(self.p(expected_writer, "patroni"), "/primary")[0] == 200 and
                self.sql("postgres", "SELECT pg_is_in_recovery()", expected_writer) == "f"
            ) else None
            if final_writer:
                data = self.sql(db, "SELECT resource FROM alerts WHERE resource LIKE 'fault-%'", final_writer)
                stored = set(data.splitlines())
            acknowledged = {row["resource"] for row in outcomes if row["ok"]}
            success_after_fault = [row for row in outcomes if row["ok"] and
                                   row["completed_monotonic"] >= fault_at and
                                   row["scheduled_monotonic"] >= crash_due]
            first_success = min((row["completed_monotonic"] for row in success_after_fault), default=None)
            windows = [outcomes[i:i+10] for i in range(crash_index, len(outcomes) - 9)]
            sustained = min((max(row["completed_monotonic"] for row in window)
                             for window in windows if all(row["ok"] for row in window)), default=None)
            result = {
                "feature": feature, "offered_per_second": 5, "requests": outcomes,
                "peak_cpu_c": self.peak_cpu,
                "setup_peak_cpu_c": setup_peak_cpu,
                "site_a_database_stopped": not (self.root / "pg-a/postmaster.pid").exists(),
                "fault_target": self.args.fault_target,
                "stopped_process_groups": completed_outages[0],
                "verified_final_writer": final_writer,
                "b_is_primary": final_writer == "b", "acknowledged": len(acknowledged),
                "stored": len(stored) if final_writer else None,
                "missing_acknowledged": sorted(acknowledged - stored) if final_writer else None,
                "stored_without_success_response": sorted(stored - acknowledged) if final_writer else None,
                "fault_monotonic": fault_at,
                "fault_scheduled_monotonic": crash_due,
                "replica_gap_at_fault": gap,
                "wal_proxy_outage_before_fault_seconds":
                    round(fault_at - wal_cut_at, 3) if wal_cut_at is not None else None,
                "patroni_primary_seconds": round(promotion_at - fault_at, 3) if promotion_at else None,
                "observed_loss_window_seconds": round(max(0, fault_at - min(
                    row["completed_monotonic"] for row in outcomes
                    if row["ok"] and row["resource"] not in stored
                )), 3) if final_writer and acknowledged - stored else None,
                "first_success_seconds": round(first_success - fault_at, 3) if first_success else None,
                "ten_consecutive_success_seconds": round(sustained - fault_at, 3) if sustained else None,
            }
            if self.args.fault_target == "dcs-client":
                try:
                    a_in_recovery = self.sql("postgres", "SELECT pg_is_in_recovery()", "a") == "t"
                    result["a_sql_state_after_isolation"] = "replica" if a_in_recovery else "writable"
                except (OSError, subprocess.SubprocessError):
                    result["a_sql_state_after_isolation"] = "unreachable"
            if self.args.rejoin and final_writer == "b" and result["site_a_database_stopped"]:
                try:
                    result["rejoin"] = await self.rejoin_a(apps, db, stored)
                except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    result["rejoin"] = {"ledger_matches": False, "error": str(exc)}
            if health_trace:
                result["haproxy_b_health_after_fault"] = await health_trace
            return result

    async def cell(self, name, feature, rate, seconds):
        self.epoch = name
        self.peak_cpu = 0.0
        # Preserve the etcd and WAL relays while a disk-backed fixture clones.
        clone = await self.clone_fixture(name)
        key_use_start = self.key_counts(name)
        timer_start = self.alert_timers(name)
        plans = await asyncio.to_thread(self.search_plans, name) if self.args.bulk_alerts else None
        async with AsyncExitStack() as apps:
            for site, slot in itertools.product(SITES, ("vm1", "vm2")):
                await self.start_app(apps, site, slot, name, feature, name)
            await self.cool()
            await wait_until(lambda: all(http(port, "/management/gtg", host=host)[0] == 200
                                         for port, host in self.ingress.values()), "both Alerta frontends")
            await asyncio.sleep(self.args.health_interval * 2 / 1000)
            # The replay only writes existing duplicate identities, so totals and groups stay invariant.
            read_contract = await asyncio.to_thread(self.check_reads, name)
            batched = feature == "all" or "hot" in feature.split("+")
            if batched:
                await asyncio.sleep(self.args.flush + 0.2)
            before = self.duplicates(name)
            await self.cool()
            setup_peak_cpu = self.peak_cpu
            self.peak_cpu = 0.0
            warm = await self.load(rate, 2, read_contract=read_contract)
            if self.args.idle_gap:
                await asyncio.sleep(self.args.idle_gap)
                await self.cool()
            db_before = self.database_stats(name)
            storage_before = self.alert_storage_stats(name) if plans else None
            pool_before = self.pool_stats(name)
            cpu_before = self.cpu_sample()
            wal_before = self.sql(name, "SELECT pg_current_wal_insert_lsn()") if plans else None
            scope_before = cgroup_cpu_stats()
            rows = await self.load(rate, seconds, timed=True, read_contract=read_contract)
            scope_after = cgroup_cpu_stats()
            key_counts_at_end = self.key_counts(name)
            timers_at_end = self.alert_timers(name)
            cpu_after = self.cpu_sample()
            wal_bytes = int(self.sql(name, f"SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),'{wal_before}')"
                                     )) if plans else None
            db_after = self.database_stats(name)
            storage_after = self.alert_storage_stats(name) if plans else None
            pool_after = self.pool_stats(name)
            gate_errors = []
            try:
                await asyncio.to_thread(self.check_reads, name)
            except (RuntimeError, OSError, HTTPException, ValueError) as exc:
                gate_errors.append(str(exc))
            actual = self.duplicates(name)
            writes = Counter(row["resource"] for row in warm + rows
                             if row["case"] == "write" and row["ok"])
            if {resource: actual[resource] - count for resource, count in before.items()} != {
                resource: writes[resource] for resource in before
            }:
                gate_errors.append("Successful writes do not reconcile to stored duplicates")
            if batched:
                await asyncio.sleep(self.args.flush + 0.2)
        # Four untimed checks before and after, and one use per offered successful request.
        request_keys = Counter(row["key_slot"] for row in warm + rows if row["ok"])
        key_uses = self.key_counts(name)
        expected_at_end = [start + request_keys[slot] + (4 if slot == 0 else 0)
                           for slot, start in enumerate(key_use_start)]
        expected_key_uses = [value + (4 if slot == 0 else 0) for slot, value in enumerate(expected_at_end)]
        timer_names = {"write": "received", "list": "queries", "count": "counts"}
        success_by_case = Counter(row["case"] for row in warm + rows if row["ok"])
        expected_timers = {
            timer: timer_start.get(timer, 0) + success_by_case[operation] + (4 if operation != "write" else 0)
            for operation, timer in timer_names.items()
        }
        final_timers = self.alert_timers(name)
        if final_timers != expected_timers:
            gate_errors.append("Persisted alert timers do not reconcile to the verified HTTP operations")
        result = {"scenario": name, "feature": feature, "rate": rate, "seconds": seconds,
                  **clone,
                  "key_uses": key_uses, "expected_key_uses": expected_key_uses,
                  "key_uses_at_timed_end": key_counts_at_end, "expected_uses_at_timed_end": expected_at_end,
                  "alert_timers_at_timed_end": timers_at_end,
                  "alert_timers": final_timers, "expected_alert_timers": expected_timers,
                  "valid": not gate_errors and key_uses == expected_key_uses and
                           all(row["ok"] for row in warm + rows),
                  "gate_errors": gate_errors,
                  "peak_cpu_c": self.peak_cpu,
                  "setup_peak_cpu_c": setup_peak_cpu,
                  "cpu_ms": {k: round((cpu_after[k] - v) * 1000 / os.sysconf("SC_CLK_TCK"), 1)
                             for k, v in cpu_before.items()},
                  "cgroup_cpu_delta": {key: after - scope_before[key] for key, after in scope_after.items()
                                       if key in scope_before},
                  "pg_stats_delta": {k: db_after[k] - v for k, v in db_before.items()},
                  "pgbouncer_show_stats_before": pool_before, "pgbouncer_show_stats_after": pool_after,
                  "warmup_errors": [r for r in warm if not r["ok"]]}
        if plans:
            result["search_plans"] = plans
            result["alert_storage"] = {
                "updated_rows": storage_after["n_tup_upd"] - storage_before["n_tup_upd"],
                "heap_only_updates": storage_after["n_tup_hot_upd"] - storage_before["n_tup_hot_upd"],
                "index_bytes": storage_after["index_bytes"],
                "cluster_wal_insert_bytes": wal_bytes,
            }
        await asyncio.to_thread(self.sql, "postgres", f"DROP DATABASE {name} WITH (FORCE)")
        return {**result, "groups": summaries(rows), "requests": rows}

    def key_counts(self, db):
        counts = self.sql(db, "SELECT count FROM keys WHERE id LIKE 'bench-key-%' "
                              "ORDER BY split_part(id,'-',3)::int")
        return [int(value) for value in counts.splitlines()]

    def alert_timers(self, db):
        return json.loads(self.sql(db, "SELECT coalesce(json_object_agg(name,count),'{}') "
                                   "FROM metrics WHERE \"group\"='alerts' AND type='timer' "
                                   "AND name IN ('received','queries','counts')"))

    async def cool(self):
        target = self.args.cool_start_c
        if self.read_temperature() <= target:
            return
        for _ in range(60):
            await asyncio.sleep(2)
            if self.read_temperature() <= target:
                return
        raise RuntimeError(f"The CPU did not cool to {target} C within two minutes")

    async def load(self, rate, seconds, timed=False, read_contract=None):
        # Fixed-rate paired replay with a separate client concurrency budget.
        pending, rows = set(), []
        start = time.monotonic()
        next_sensor_read = start
        burst_rate = self.args.burst_rate if timed else None
        for i, offset in enumerate(offered_times(rate, seconds, burst_rate, self.args.burst_seconds)):
            due = start + offset
            await asyncio.sleep(max(0, due - time.monotonic()))
            if due >= next_sensor_read:
                next_sensor_read = due + 0.5
                if self.read_temperature() >= 84:
                    if pending:
                        await asyncio.gather(*pending)
                    raise ThermalLimit("Temperature reached the 84 C load-stop limit")
            done = {task for task in pending if task.done()}
            rows.extend(task.result() for task in done)
            pending -= done
            site = SITES[i % 2]
            kind = ("write", "list", "count")[i // 2 % 3]
            resource = WRITE_IDENTITIES[(i // 6 + (i % 2) * 12) % 24] if kind == "write" else None
            key_slot = (i // 6 + i % 2) % len(self.client_keys)
            if len(pending) == self.args.client_inflight:
                rows.append({"site": site, "case": kind, "resource": resource,
                             "ok": False, "status": "generator_limit", "scheduled_monotonic": due,
                             "generator_delay_ms": round((time.monotonic() - due) * 1000, 3)})
            else:
                port, host = self.ingress[site]
                pending.add(asyncio.create_task(asyncio.to_thread(
                    timed_request, port, self.client_keys[key_slot], site, kind,
                    resource, due, self.epoch, key_slot, host, self.paths[kind], self.args.client_idle,
                    read_contract=read_contract, dashboard_encoding=self.args.dashboard_encoding,
                    identity_check=getattr(self, "response_identity_check", None)
                )))
        rows.extend(await asyncio.gather(*pending))
        return rows


def timed_request(port, key, site, kind, resource, due, epoch, key_slot=0, host=HOST, path="/alert",
                  idle_limit=20, *, read_contract=None, dashboard_encoding=None, identity_check=None):
    started = time.monotonic()
    backend_site = None
    backend_worker = None
    transport = None
    status = None
    error = None
    identities_verified = None
    ok = False
    completed = None
    try:
        payload = alert_for(resource, int(resource.rsplit("-", 1)[1])) if resource else None
        status, raw, backend_site, backend_worker, transport = http(
            port, path, key, payload, epoch, host, trace=True, idle_limit=idle_limit,
            requested_encoding=dashboard_encoding if kind == "list" else None
        )
        completed = time.monotonic()
        error = transport.get("error")
        if status == (201 if kind == "write" else 200):
            data = json.loads(raw)
            field = {"list": "alerts", "count": "total", "write": "alert"}[kind]
            ok = isinstance(data, dict) and data.get("status") == "ok" and field in data
            if ok and kind == "write":
                returned = data["alert"]
                ok = isinstance(returned, dict) and returned.get("resource") == resource
            elif kind != "write" and read_contract is not None:
                error = read_contract_error(data, kind, *read_contract)
                ok = error is None
            if ok and identity_check is not None:
                identities_verified = identity_check(data, kind, resource)
                if identities_verified is None:
                    error, ok = "unexpected_fixture_response_id", False
    except (OSError, HTTPException, ValueError) as exc:
        error = type(exc).__name__
    ended = time.monotonic() if completed is None else completed
    return {"site": site, "case": kind, "resource": resource,
            **({"response_ids_verified": identities_verified} if identity_check is not None else {}),
            "status": status if status is not None else error, "ok": ok, "error": error,
            "key_slot": key_slot,
            "backend_site": backend_site,
            "backend_worker": backend_worker,
            "client_transport": transport,
            "elapsed_ms": round((ended - due) * 1000, 3),
            "generator_delay_ms": round((started - due) * 1000, 3),
            "scheduled_monotonic": due, "completed_monotonic": ended}


def percentile(values, fraction):
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summaries(rows):
    by_case = defaultdict(list)
    for row in rows:
        by_case[f"{row['site']}-{row['case']}"].append(row)
    aggregates = {}
    for label, group in sorted(by_case.items()):
        success = [row for row in group if row["ok"]]
        latency = sorted(row["elapsed_ms"] for row in success)
        generator = sorted(row["generator_delay_ms"] for row in group)
        service = sorted(round(row["elapsed_ms"] - row["generator_delay_ms"], 3) for row in success)
        aggregates[label] = {"offered": len(group), "success": len(latency),
                             "p50_ms": percentile(latency, 0.50), "p95_ms": percentile(latency, 0.95),
                             "p99_ms": percentile(latency, 0.99),
                             "generator_delay_p95_ms": percentile(generator, 0.95),
                             "client_service_p95_ms": percentile(service, 0.95)}
        entities = [row.get("client_transport") or {} for row in success]
        if entities and all("encoded_body_bytes" in entity for entity in entities):
            aggregates[label].update(
                encoded_body_bytes=sum(entity["encoded_body_bytes"] for entity in entities),
                decoded_body_bytes=sum(entity["decoded_body_bytes"] for entity in entities),
                encodings=dict(Counter(entity["encoding"] for entity in entities)),
            )
    return aggregates


async def main(args):
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=args.client_inflight))
    with lab_directory(args.output) as tmp:
        lab = Lab(args, tmp, args.output)
        async with AsyncExitStack() as stack:
            await lab.cool()
            await lab.start(stack)
            await lab.cool()
            if args.check_replica_wait:
                await lab.confirm_replica_wait()
            seed_start = time.monotonic()
            await lab.seed()
            (args.output / "fixture.json").write_text(json.dumps(
                {"seed_seconds": round(time.monotonic() - seed_start, 3)}
            ) + "\n")
            if args.fault:
                try:
                    result = await lab.failover(args.fault)
                except ThermalLimit as exc:
                    (args.output / "rejected.json").write_text(json.dumps(
                        {"reason": str(exc), "peak_cpu_c": lab.peak_cpu, "fault_target": args.fault_target}
                    ) + "\n")
                    raise
                (args.output / "failover.json").write_text(json.dumps(result) + "\n")
                print({key: value for key, value in result.items() if key != "requests"}, flush=True)
                return
            scenarios = [(feature, rate) for feature in args.features.split(",") for rate in args.rates]
            cutoff = None
            with (args.output / "cells.jsonl").open("x") as log:
                for rep in range(args.repeats):
                    order = scenarios.copy()
                    random.Random(rep).shuffle(order)
                    for index, (feature, rate) in enumerate(order):
                        name = f"r{rep}_cell{index}"
                        if cutoff is not None and rate >= cutoff:
                            continue
                        try:
                            cell = await lab.cell(name, feature, rate, args.seconds)
                        except ThermalLimit as exc:
                            cutoff = rate
                            cell = {"scenario": name, "feature": feature, "rate": rate,
                                    "valid": False, "gate_errors": [str(exc)],
                                    "peak_cpu_c": lab.peak_cpu, "key_uses": None,
                                    "expected_key_uses": None, "groups": {},
                                    "warmup_errors": [], "requests": []}
                        log.write(json.dumps(cell) + "\n")
                        log.flush()
                        print(cell["scenario"], feature, rate,
                              f"accounting {cell['key_uses']}/{cell['expected_key_uses']}", flush=True)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("etcd", "haproxy", "pgbouncer", "pg-bin", "venv", "addon", "output"):
        parser.add_argument(f"--{key}", required=True, type=lambda item: Path(item).resolve())
    parser.add_argument("--psql", default=shutil.which("psql"), type=lambda item: Path(item).resolve())
    parser.add_argument("--pg-library-dir", type=lambda item: Path(item).resolve(),
                        help="optional private package libraries for PostgreSQL tools and Patroni")
    parser.add_argument("--check-replica-wait", action="store_true",
                        help="exercise PostgreSQL 19 WAIT for a disposable committed row before the benchmark")
    parser.add_argument("--rtt-ms", type=int, default=50)
    parser.add_argument("--wal-rtt-ms", type=int, help="WAL stream RTT, defaulting to --rtt-ms")
    parser.add_argument("--dcs-rtt-ms", type=int, default=0,
                        help="added RTT shared by all three etcd peer paths")
    parser.add_argument("--dcs-ttl", type=int, default=20)
    parser.add_argument("--patroni-loop", type=int, default=4)
    parser.add_argument("--patroni-retry", type=int, default=4)
    parser.add_argument("--failover-lag", type=int, default=1048576, help="eligibility threshold in WAL bytes")
    parser.add_argument("--ingress", choices=("local", "primary", "primary-aware"), default="local",
                        help="route via local frontends, by static A affinity or by live Patroni role")
    parser.add_argument("--pool-size", type=int, default=8)
    parser.add_argument("--pool-idle", type=int, default=600)
    parser.add_argument("--pool-login-retry", type=int, default=15)
    parser.add_argument("--pool-connect-timeout", type=int, default=15)
    parser.add_argument("--pool-query-wait", type=int, default=120)
    parser.add_argument("--pool-mode", choices=("transaction", "session"), default="transaction")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--worker-class", choices=("gthread", "sync"), default="gthread")
    parser.add_argument("--key-count", type=int, default=1)
    parser.add_argument("--seed-alerts", type=int, default=120)
    parser.add_argument("--bulk-alerts", type=int, default=0,
                        help="additional synthetic rows for database search experiments")
    parser.add_argument("--search-index", choices=SEARCH_INDEX_SQL, default="stock")
    parser.add_argument("--clone-strategy", choices=CLONE_STRATEGIES, default="wal-log",
                        help="PostgreSQL strategy for cloning the fixture before each healthy cell")
    parser.add_argument("--file-copy-method", choices=("copy", "clone"), default="copy",
                        help="file copy implementation for a PostgreSQL 18 or newer FILE_COPY experiment")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                        help="number of alerts requested by dashboard list calls")
    parser.add_argument("--read-filter", choices=READ_FILTERS, default="all")
    parser.add_argument("--forwarder", action="store_true",
                        help="include the default forwarder with no external destinations")
    parser.add_argument("--health-interval", type=int, default=500, help="Alerta readiness interval in ms")
    parser.add_argument("--health-timeout", type=int, default=2000, help="Alerta readiness timeout in ms")
    parser.add_argument("--writer-idle", type=int, default=30, help="HAProxy writer idle timeout in seconds")
    parser.add_argument("--flush", type=float, default=1.0)
    parser.add_argument("--max-age", type=float, default=300.0)
    parser.add_argument("--features", default="stock,all")
    parser.add_argument("--fault", metavar="FEATURES", help="a single feature set for the fault drill")
    parser.add_argument("--fault-target", choices=("site-a", "one-app", "both-apps", "witness", "dcs-client"),
                        default="site-a", help="which disposable lab processes to stop in the fault drill")
    parser.add_argument("--rejoin", action="store_true", help="restore A as a standby after the crash")
    parser.add_argument("--wal-outage", type=int, default=0,
                        help="seconds to cut both WAL relay connections before the Site A crash")
    parser.add_argument("--health-trace", action="store_true",
                        help="record Site B HAProxy state transitions in a fault drill")
    parser.add_argument("--rates", type=int, nargs="+", default=[12])
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--burst-rate", type=int, help="optional higher offered rate midway through timed load")
    parser.add_argument("--burst-seconds", type=int, default=8)
    parser.add_argument("--cool-start-c", type=int, default=75,
                        help="starting thermal ceiling; the 84 C timed-load stop always applies")
    parser.add_argument("--idle-gap", type=int, default=0,
                        help="seconds of client inactivity between warmup and timed load")
    parser.add_argument("--client-idle", type=int, default=20,
                        help="maximum HTTP idle reuse in seconds, zero to open per request")
    parser.add_argument("--dashboard-encoding", choices=("identity", "gzip"),
                        help="enable the list-only compression backend; negotiate the selected timed representation")
    parser.add_argument("--client-inflight", type=int, default=16,
                        help="maximum simultaneous HTTP requests from the healthy replay")
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    args.netem = os.environ.get("ALRUST_NETEM_ACTIVE") == "1"
    args.http_rate_mbit = int(os.environ.get("ALRUST_NETEM_HTTP_RATE_MBIT", 0)) if args.netem else 0
    if args.http_rate_mbit and (not 1 <= args.http_rate_mbit <= 100 or args.ingress == "local" or args.fault):
        parser.error("HTTP link rate requires a healthy remote HTTP ingress and 1–100 Mbit/s")
    if args.dashboard_encoding and args.fault:
        parser.error("dashboard encoding experiments require a healthy replay")
    requested_features = args.features.split(",")
    if args.fault is not None:
        requested_features.append(args.fault)
    for feature in requested_features:
        parts = feature.split("+")
        if feature not in FEATURES and (
            len(set(parts)) != len(parts) or not set(parts) <= set(FEATURES[1:-1])
        ):
            parser.error(f"features must be comma-separated from {FEATURES}; use + to combine patches")
    for label, lo, hi in (("rtt_ms", 0, 100), ("pool_size", 1, 16), ("workers", 1, 2),
                          ("threads", 1, 4), ("flush", 0.2, 5), ("max_age", 1, 600),
                          ("health_interval", 500, 5000), ("health_timeout", 1000, 10000),
                          ("writer_idle", 10, 600),
                          ("pool_idle", 0, 600),
                          ("pool_login_retry", 1, 30), ("pool_query_wait", 1, 120),
                          ("pool_connect_timeout", 2, 30),
                          ("key_count", 1, 16), ("page_size", 1, 250), ("seed_alerts", 25, 10000), ("bulk_alerts", 0, 100000),
                          ("dcs_rtt_ms", 0, 100), ("dcs_ttl", 20, 30),
                          ("patroni_loop", 1, 10), ("patroni_retry", 3, 10),
                          ("failover_lag", 0, 16777216), ("wal_outage", 0, 25),
                          ("seconds", 4, 600), ("idle_gap", 0, 360), ("client_idle", 0, 60),
                          ("client_inflight", 4, 64),
                          ("repeats", 1, 12),
                          ("cool_start_c", 60, 75)):
        if not lo <= getattr(args, label) <= hi:
            parser.error(f"{label} must be in [{lo}, {hi}]")
    if args.wal_rtt_ms is not None and not 0 <= args.wal_rtt_ms <= 100:
        parser.error("wal_rtt_ms must be in [0, 100]")
    if args.patroni_loop + 2 * args.patroni_retry > args.dcs_ttl:
        parser.error("Patroni requires loop_wait + 2 * retry_timeout <= ttl")
    if args.netem:
        delays = (args.rtt_ms, args.rtt_ms if args.wal_rtt_ms is None else args.wal_rtt_ms,
                  args.dcs_rtt_ms)
        encoded = ",".join(map(str, delays))
        if encoded != os.environ.get("ALRUST_NETEM_RTTS"):
            parser.error("CLI RTT values must match the packet profile supplied to run_netem.sh")
    if not all(3 <= rate <= 64 for rate in args.rates):
        parser.error("offered rates must be in [3, 64]")
    if args.burst_rate is not None and (
        not max(args.rates) < args.burst_rate <= 64
        or not 1 <= args.burst_seconds <= min(30, args.seconds - 2)
        or args.fault
    ):
        parser.error("the burst needs a higher rate up to 64/s, a bounded duration and a healthy test")
    if args.addon.name != "alerta_fast.py":
        parser.error("the add-on path must point to alerta_fast.py")
    if args.rejoin and not args.fault:
        parser.error("--rejoin requires --fault")
    if args.fault and args.client_inflight != 16:
        parser.error("--client-inflight can only be changed for healthy replay")
    if args.fault and args.clone_strategy != "wal-log":
        parser.error("--clone-strategy can only be changed for healthy replay")
    if args.file_copy_method == "clone" and args.clone_strategy != "file-copy":
        parser.error("--file-copy-method clone requires a healthy --clone-strategy file-copy experiment")
    if args.health_trace and not args.fault:
        parser.error("--health-trace requires --fault")
    if args.fault_target != "site-a" and (
        not args.fault or args.rejoin or args.wal_outage or args.ingress != "primary-aware"
    ):
        parser.error("component loss requires primary-aware fault traffic with no WAL cut or rejoin")
    if args.wal_outage and (not args.fault or args.rejoin or args.wal_outage < 8):
        parser.error("a pre-crash WAL outage requires --fault, at least eight seconds and no --rejoin")
    if args.bulk_alerts and args.seed_alerts < len(WRITE_IDENTITIES) + 3:
        parser.error("bulk alerts require at least three API-seeded background exemplars")
    index_filters = {"severity-time": {"major"}, "resource-trigram": RESOURCE_FILTERS,
                     "newest": READ_FILTERS, "resource-count-cover": READ_FILTERS,
                     "newest-and-count-cover": READ_FILTERS}
    if args.search_index != "stock" and (
        not args.bulk_alerts or args.read_filter not in index_filters[args.search_index] or args.fault
    ):
        parser.error("a search index requires a matching healthy filter run with bulk alerts")
    if args.read_filter in RESOURCE_FILTERS and args.bulk_alerts < 120:
        parser.error("the resource filter needs at least 120 bulk alerts")
    if args.worker_class == "sync" and args.threads != 1:
        parser.error("the sync worker requires --threads 1")
    if args.psql is None:
        parser.error("psql must be available on PATH or passed via --psql")
    args.output.mkdir(mode=0o700)
    (args.output / "bindings.json").write_text(json.dumps(
        {"rtt_ms": args.rtt_ms, "pool_size": args.pool_size, "pool_mode": args.pool_mode,
         "workers": args.workers, "threads": args.threads, "max_age": args.max_age,
         "worker_class": args.worker_class,
         "flush": args.flush, "addon_sha256": hashlib.sha256(args.addon.read_bytes()).hexdigest(),
         "postgres_sha256": hashlib.sha256((args.pg_bin / "postgres").read_bytes()).hexdigest(),
         "postgres_wait_check": args.check_replica_wait,
         "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         "proxy_sha256": hashlib.sha256((HERE / "haproxy.cfg").read_bytes()).hexdigest(),
         "affinity_sha256": hashlib.sha256((HERE / "haproxy-affinity.cfg").read_bytes()).hexdigest()
                            if args.ingress == "primary-aware" else None,
         "ha_health_interval_ms": args.health_interval, "ha_health_timeout_ms": args.health_timeout,
         "ha_writer_idle_seconds": args.writer_idle,
         "ingress": args.ingress,
         "key_count": args.key_count, "page_size": args.page_size, "seed_alerts": args.seed_alerts, "read_filter": args.read_filter,
         "bulk_alerts": args.bulk_alerts, "search_index": args.search_index, "clone_strategy": args.clone_strategy,
         "file_copy_method": args.file_copy_method,
         "idle_gap_seconds": args.idle_gap,
         "client_idle_seconds": args.client_idle,
         "client_inflight_limit": args.client_inflight,
         "timed_seconds": args.seconds if not args.fault else None,
         "requested_repeats": args.repeats if not args.fault else None,
         "offered_rates": args.rates if not args.fault else None,
         "feature_arms": args.features.split(",") if not args.fault else [args.fault],
         "burst_rate": args.burst_rate, "burst_seconds": args.burst_seconds if args.burst_rate else None,
         "cool_start_c": args.cool_start_c,
         "pool_idle_seconds": args.pool_idle, "forwarder": args.forwarder,
         "pool_login_retry_seconds": args.pool_login_retry,
         "pool_connect_timeout_seconds": args.pool_connect_timeout,
         "pool_query_wait_seconds": args.pool_query_wait,
         "wal_rtt_ms": args.wal_rtt_ms, "dcs_rtt_ms": args.dcs_rtt_ms,
         "emulation": "netem" if args.netem else "tcp-stream",
         "dashboard_encoding": args.dashboard_encoding, "wan_http_rate_mbit": args.http_rate_mbit,
         "netem_jitter_ms": os.environ.get("ALRUST_NETEM_JITTER_MS") if args.netem else None,
         "netem_loss_percent": os.environ.get("ALRUST_NETEM_LOSS") if args.netem else None,
         "dcs_ttl": args.dcs_ttl, "patroni_loop": args.patroni_loop,
         "patroni_retry": args.patroni_retry, "failover_lag_bytes": args.failover_lag,
         "wal_outage_seconds": args.wal_outage,
         "fault_target": args.fault_target,
         "health_trace": args.health_trace,
         "cgroup_limits": resource_caps(),
         "cpu_affinity": sorted(os.sched_getaffinity(0))}, indent=2) + "\n")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    finally:
        if readings := cgroup_memory_stats():
            (args.output / "scope-memory.json").write_text(json.dumps(readings, indent=2) + "\n")


if __name__ == "__main__":
    cli()
