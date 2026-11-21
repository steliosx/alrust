"""Compare a one-second site-local Varnish cache with the same healthy two-site Alerta ingress."""

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from itertools import groupby, product
import json
import os
from pathlib import Path
import secrets
import subprocess
import sysconfig
import threading
import time

from cache_replay_model import SITES, TENANTS, VIEWS, check_view, digest, expected_views, percentiles, plan
from dashboard_routes import probe_routes
from hosted_lab import replay_lab
import run_lab
import cache_replay_mutations as mutations
import cache_replay_panels as panels
import cache_replay_journey as journey

HERE = Path(__file__).resolve().parent
ARMS = ("off", "one-second")
KEY_COUNT = 13  # Eight dashboard identities, four scoped writers, one disposable revocation identity.


class NonJsonResponse(ValueError):
    def __init__(self, status, details):
        self.status = status
        super().__init__(details)


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    tmp.replace(path)


def guard(lab):
    reading = lab.read_temperature()
    if reading is not None and reading >= 84:
        raise run_lab.ThermalLimit(f"CPU reached {reading:g} C during cache replay (84 C stop)")


def failure_reason(exc):
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(failure_reason(cause) for cause in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def snapshot(lab, db, site="a"):
    sql = """SELECT json_build_object(
      'alerts', (SELECT json_agg(json_build_array(id, resource, severity, status, customer,
       duplicate_count, last_receive_id) ORDER BY resource) FROM alerts),
      'uses', (SELECT json_agg(count ORDER BY split_part(id,'-',3)::int) FROM keys WHERE id LIKE 'bench-key-%'),
      'timers', (SELECT coalesce(json_object_agg(name,count),'{}') FROM metrics WHERE "group"='alerts'
       AND type='timer' AND name IN ('received','queries','counts')))"""
    return json.loads(lab.sql(db, sql, site))


def tenants(lab):
    # Scope every reader/writer to exactly one customer. The revocation probe uses only slot 12.
    for i in range(KEY_COUNT):
        tenant = TENANTS[i % 4]
        scopes = "ARRAY['read','write']" if 8 <= i < 12 else "ARRAY['read']"
        lab.sql(
            "bench_seed",
            f"""UPDATE keys SET "user"='lab-{i}',customer='{tenant}',scopes={scopes}
                                 WHERE id='bench-key-{i}'""",
        )
    names = "ARRAY['blue','gold','green','red']"
    lab.sql(
        "bench_seed",
        f"""WITH numbered AS (SELECT id,row_number() OVER(ORDER BY resource) AS n FROM alerts)
        UPDATE alerts a SET customer=({names})[1 + (CASE WHEN a.resource LIKE 'repeat-%'
        THEN split_part(a.resource,'-',2)::int ELSE numbered.n::int END % 4)] FROM numbered WHERE a.id=numbered.id""",
    )


def request(lab, target, slot, path, payload=None, gzip=False):
    port, host = target
    code, raw, route, worker, trace = run_lab.http(
        port,
        path,
        lab.client_keys[slot],
        payload,
        epoch=lab.epoch,
        host=host,
        trace=True,
        idle_limit=lab.args.client_idle,
        requested_encoding="gzip" if gzip else None,
    )
    if code is None:
        raise RuntimeError(f"transport failure in {trace.get('phase')}: {trace.get('error')}")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        # Retain the HTTP disposition without logging a response body or a credential.
        hint = next(
            (
                s
                for s in ("Backend fetch failed", "No server is available", "Service Unavailable")
                if s.encode() in raw
            ),
            "non-JSON response",
        )
        raise NonJsonResponse(
            code,
            f"HTTP {code} ({hint}, {len(raw)} bytes), route={route}, "
            f"worker={worker}, cache={trace.get('cache_result')}",
        ) from exc
    return code, body, route, worker, trace


def varnish_layout():
    stage = Path(os.environ.get("BENCH_VARNISH_PREFIX", "/usr"))
    candidates = (
        stage / "lib" / (sysconfig.get_config_var("MULTIARCH") or ""),
        stage / "lib",
        stage / "lib64",
    )
    lib = next((base for base in candidates if (base / "varnish/vmods").is_dir()), None)
    if lib is None or not (stage / "sbin/varnishd").is_file() or not (stage / "bin/varnishstat").is_file():
        raise ValueError(
            "BENCH_VARNISH_PREFIX must contain varnishd, varnishstat and matching Varnish modules"
        )
    return stage, lib


async def caches(lab, stack, db):
    stage, lib = varnish_layout()
    env = {**lab.env, "LD_LIBRARY_PATH": str(lib)}
    ports = run_lab.reserve(SITES)
    out, nodes = {}, {}
    for site in SITES:
        directory = lab.root / f"v-{db}-{site}"
        directory.mkdir(mode=0o700)
        vcl = directory / "cache.vcl"
        origin_port, host = lab.ingress[site]
        if host != "127.0.0.1":
            raise ValueError("Cache backend must be the assigned loopback site ingress")
        vcl.write_text((HERE / "cache_replay.vcl").read_text().replace("@PORT@", str(origin_port)))
        proc = await lab.launch(
            stack,
            site,
            [
                stage / "sbin/varnishd",
                "-j",
                "none",
                "-F",
                "-n",
                directory,
                "-a",
                f"127.0.0.1:{ports[site]}",
                "-T",
                "none",
                "-f",
                vcl,
                "-s",
                "malloc,12m",
                "-t",
                "1",
                "-p",
                "thread_pools=1",
                "-p",
                "thread_pool_min=8",
                "-p",
                "thread_pool_max=48",
                "-p",
                "http_gzip_support=off",
                "-p",
                "vsl_space=4m",
                "-p",
                "timeout_idle=30",
                "-p",
                f"vmod_path={lib}/varnish/vmods",
                "-p",
                "cc_command=exec /usr/bin/cc -fPIC -shared %w -o %o %s",
            ],
            env,
            f"varnish-{db}-{site}.log",
        )
        nodes[site] = (directory, proc)
        out[site] = (ports[site], "127.0.0.1")

    async def ready(site):
        for _ in range(90):
            if nodes[site][1].poll() is not None:
                raise RuntimeError(f"Varnish {site} exited during startup")
            try:
                if (await asyncio.to_thread(run_lab.http, ports[site], "/management/gtg"))[0] == 200:
                    return
            except OSError:
                pass
            await asyncio.sleep(0.2)
        raise RuntimeError("Varnish startup timed out")

    await asyncio.gather(*(ready(s) for s in SITES))
    return out, nodes


def counters(nodes):
    result = {}
    if nodes:
        stage, lib = varnish_layout()
        for site, (directory, proc) in nodes.items():
            if proc.poll() is not None:
                raise RuntimeError("Varnish exited")
            data = json.loads(
                subprocess.check_output(
                    [stage / "bin/varnishstat", "-n", directory, "-1", "-j"],
                    env={**os.environ, "LD_LIBRARY_PATH": str(lib)},
                    timeout=3,
                )
            )
            names = (
                "backend_req",
                "cache_hit",
                "cache_miss",
                "backend_fail",
                "backend_retry",
                "fetch_failed",
                "backend_unhealthy",
                "backend_recycle",
            )
            result[site] = {
                name: int(data["counters"][f"MAIN.{name}"]["value"])
                for name in names
                if f"MAIN.{name}" in data["counters"]
            }
    return result


def single_action(lab, arm, endpoints, event, views, t0):
    start = time.monotonic()
    row = {**event, "ok": False, "started_s": start - t0}
    slot = event["key"]
    bypass = event["kind"] == "write" and lab.write_route == "direct"
    row["via_cache_proxy"] = arm == "one-second" and not bypass
    try:
        if event["kind"] == "dashboard":
            code, body, route, worker, trace = request(
                lab, endpoints[event["site"]], slot, event["path"], gzip=True
            )
            row["http_status"] = code
            if code != 200:
                raise ValueError(f"Dashboard status {code}")
            if event.get("newest"):
                row["page_signature"] = mutations.observed(body, TENANTS[slot % 4])
            else:
                check_view(body, views[(TENANTS[slot % 4], event["view"])])
        else:
            i = event["index"]
            fresh = event.get("fresh", False)
            resource = f"replay-new-{event['sequence']:06d}" if fresh else run_lab.WRITE_IDENTITIES[i]
            row["resource"] = resource
            endpoint = (lab.ingress if bypass else endpoints)[event["site"]]
            code, body, route, worker, trace = request(
                lab, endpoint, slot, "/alert", run_lab.alert_for(resource, i)
            )
            row["http_status"] = code
            alert = body.get("alert", {})
            if (
                code not in (200, 201)
                or alert.get("resource") != resource
                or alert.get("customer") != TENANTS[slot % 4]
                or alert.get("repeat") is not (not fresh)
                or type(alert.get("duplicateCount")) is not int
            ):
                raise ValueError(f"Unexpected duplicate confirmation ({code}, {body.get('message', '')})")
            if "fresh" in event:
                row["write_row"] = [
                    alert[k]
                    for k in (
                        "id",
                        "resource",
                        "severity",
                        "status",
                        "customer",
                        "duplicateCount",
                        "lastReceiveId",
                    )
                ]
            row.update(
                alert_id=alert["id"],
                ordinal=alert["duplicateCount"],
                receipt=alert["lastReceiveId"],
                resource=resource,
            )
        if route != "a" or worker not in ("vm1", "vm2"):
            raise ValueError(f"Unverified writer-adjacent app route: {route}/{worker}")
        result = trace.get("cache_result")
        if row["via_cache_proxy"] and result not in ("origin", "hit"):
            raise ValueError("Missing cache disposition")
        if not row["via_cache_proxy"] and result is not None:
            raise ValueError("Unexpected cache in off arm")
        if event["kind"] == "write" and result == "hit":
            raise ValueError("Write served from cache")
        row.update(
            ok=True, hit=result == "hit", bytes=trace["encoded_body_bytes"], route=route, worker=worker
        )
    except (KeyError, ValueError, TypeError, OSError, RuntimeError) as exc:
        if isinstance(exc, NonJsonResponse):
            row["http_status"] = exc.status
        row["error"] = str(exc)[:220]
    row["finished_s"] = time.monotonic() - t0
    row["latency_ms"] = 1000 * (row["finished_s"] - event["due_s"])
    return row


def action(lab, arm, endpoints, event, views, t0):
    if event["kind"] != "dashboard" or lab.environment_workers is None:
        return single_action(lab, arm, endpoints, event, views, t0)

    # The UI starts the environments request and the list independently. A
    # separate HTTP lane prevents one from occupying the other's keepalive socket.
    env = lab.environment_workers.submit(
        journey.environments,
        request,
        lab,
        endpoints[event["site"]],
        event,
        views[TENANTS[event["key"] % 4], "all"],
        t0,
        arm == "one-second",
    )
    row = single_action(lab, arm, endpoints, event, views, t0)
    row["list_finished_s"] = row["finished_s"]
    try:
        row["environments"] = env.result()
        row["finished_s"] = max(row["list_finished_s"], row["environments"]["finished_s"])
        row["started_s"] = min(row["started_s"], row["environments"]["started_s"])
    except (ValueError, TypeError, OSError, KeyError, RuntimeError) as exc:
        row.update(ok=False, environments_error=str(exc)[:220], finished_s=time.monotonic() - t0)
        row.setdefault("error", "Environments request failed")
    row["latency_ms"] = 1000 * (row["finished_s"] - event["due_s"])
    return row


async def replay(lab, arm, endpoints, trace, views, cell, feedback=False):
    # Create the fixed client lanes before the first synchronized refresh.
    # No request reaches the application during this client-only warmup.
    barrier = threading.Barrier(lab.args.client_inflight, timeout=10)
    await asyncio.gather(*(asyncio.to_thread(barrier.wait) for _ in range(lab.args.client_inflight)))
    if feedback:

        def process(event, baseline):
            return action(lab, arm, endpoints, event, views, baseline)

        return await panels.run(trace, cell, lab.args.seconds, process, lambda: guard(lab))
    t0 = time.monotonic()
    tasks = set()
    rows = cell["arrivals"]
    try:
        for due, wave in groupby(trace, key=lambda event: event["due_s"]):
            while (remaining := t0 + due - time.monotonic()) > 0:
                await asyncio.sleep(min(remaining, 0.25))
                guard(lab)
            guard(lab)
            done = {t for t in tasks if t.done()}
            rows.extend(t.result() for t in done)
            tasks -= done
            for event in wave:
                if len(tasks) >= lab.args.client_inflight:
                    rows.append(dict(event, ok=False, error="generator_limit"))
                else:
                    tasks.add(
                        asyncio.create_task(asyncio.to_thread(action, lab, arm, endpoints, event, views, t0))
                    )
        while tasks:
            done, tasks = await asyncio.wait(tasks, timeout=0.2, return_when=asyncio.FIRST_COMPLETED)
            rows.extend(t.result() for t in done)
            guard(lab)
    finally:
        if tasks:
            rows.extend(await asyncio.gather(*tasks))
        rows.sort(key=lambda r: r["sequence"])
    return trace


def validate_timed(before, after, standby, rows, stat0, stat1, planned, mutating=False):
    if (
        after != standby
        or len(rows) != len(planned)
        or [r["sequence"] for r in rows] != [e["sequence"] for e in planned]
    ):
        raise ValueError("Standby, schedule, or request inventory mismatch")
    if not all(r["ok"] for r in rows):
        failures = Counter(r.get("error", "unknown") for r in rows if not r["ok"])
        raise ValueError(f"Failed arrivals: {dict(failures)}")
    uses = Counter(r["key"] for r in rows if not r["hit"])
    timers = Counter("queries" if r["kind"] == "dashboard" else "received" for r in rows if not r["hit"])
    companions = [r for r in rows if "environments" in r]
    uses.update(r["key"] for r in companions)
    timers["queries"] += len(companions)
    if [b - a for a, b in zip(before["uses"], after["uses"])] != [
        uses[k] for k in range(len(before["uses"]))
    ] or Counter(after["timers"]) != Counter(before["timers"]) + timers:
        raise ValueError("Origin API-key usage or timers differ from measured cache misses and writes")
    if not mutating:
        validate_duplicates(before, after, rows)
    for site in stat0:
        origin = sum(r["site"] == site and not r["hit"] and r.get("via_cache_proxy", True) for r in rows)
        origin += sum(r["site"] == site for r in companions)
        delta = stat1[site]["backend_req"] - stat0[site]["backend_req"]
        if delta != origin:
            raise ValueError(f"{site}: {delta} backend requests, expected {origin}")


def validate_duplicates(before, after, rows):
    writes = {}
    receipts = set()
    for r in rows:
        if r["kind"] == "write":
            writes.setdefault(r["alert_id"], []).append(r)
            if not r["receipt"] or r["receipt"] in receipts:
                raise ValueError("Missing or duplicate write receipt")
            receipts.add(r["receipt"])
    if len(before["alerts"]) != len(after["alerts"]):
        raise ValueError("Unexpected alert cardinality")
    for old, new in zip(before["alerts"], after["alerts"]):
        group = sorted(writes.pop(old[0], []), key=lambda r: r["ordinal"])
        if (
            old[:5] != new[:5]
            or not group
            and old != new
            or group
            and (
                [r["ordinal"] for r in group] != list(range(old[5] + 1, old[5] + 1 + len(group)))
                or new[5:] != [group[-1]["ordinal"], group[-1]["receipt"]]
            )
        ):
            raise ValueError("Duplicate confirmations and durable database state disagree")
    if writes:
        raise ValueError("Unknown changed alert")


def aggregate(rows):
    groups = {}
    for site, kind in product(SITES, ("dashboard", "write")):
        group = [r for r in rows if (r["site"], r["kind"]) == (site, kind)]
        groups[f"{site}-{kind}"] = {
            "n": len(group),
            "hits": sum(r["hit"] for r in group),
            "arrival_latency_ms": percentiles([r["latency_ms"] for r in group]),
            "service_ms": percentiles([1000 * (r["finished_s"] - r["started_s"]) for r in group]),
            "start_delay_ms": percentiles([1000 * (r["started_s"] - r["due_s"]) for r in group]),
            "wire_bytes": sum(r["bytes"] for r in group),
        }
    return groups


async def coherence(lab, arm, endpoints, db, views):
    out = {}

    async def count(site, slot):
        code, body, _, _, trace = await asyncio.to_thread(
            request, lab, endpoints[site], slot, "/alerts/count"
        )
        return [code, body.get("total") if code == 200 else None, trace.get("cache_result")]

    # Different answers prevent an accidental identity-key collision from looking correct.
    # Validate list IDs too where two tenants happen to have the same count.
    for site in SITES:
        for slot in range(4):
            _, body, _, _, _ = await asyncio.to_thread(
                request,
                lab,
                endpoints[site],
                slot,
                "/alerts?sort-by=resource&page=1&page-size=100&environment=Production",
                None,
                True,
            )
            check_view(body, views[TENANTS[slot], "all"])
    for part, slot in (("new_alert", 0), ("key_revocation", 12)):
        total = views["blue", "all"]["total"] + (1 if part == "key_revocation" else 0)
        await asyncio.sleep(1.15)
        warm = await asyncio.gather(*(count(s, slot) for s in SITES))
        if any(x[:2] != [200, total] for x in warm):
            raise ValueError("Count probe failed before mutation")
        again = await asyncio.gather(*(count(s, slot) for s in SITES))
        if any(x[:2] != [200, total] or (arm == "one-second" and x[2] != "hit") for x in again):
            raise ValueError("Both site counts must be warm and correct before mutation")
        if part == "new_alert":
            payload = run_lab.alert_for("replay-freshness-marker", 0)
            writer = (lab.ingress if lab.write_route == "direct" else endpoints)["a"]
            code, body, _, _, _ = await asyncio.to_thread(request, lab, writer, 8, "/alert", payload)
            if code not in (200, 201) or body.get("alert", {}).get("customer") != "blue":
                raise ValueError("Marker acknowledgement failed")
            wanted = [200, total + 1]
        else:
            removed = await asyncio.to_thread(
                lab.sql, db, "DELETE FROM keys WHERE id='bench-key-12' RETURNING id"
            )
            if removed != "bench-key-12":
                raise ValueError("Disposable probe key removal failed")
            wanted = [401, None]
        acknowledged = time.monotonic()

        async def observe(site):
            samples = []
            first = None
            while time.monotonic() - acknowledged < 2.3 or len(samples) < 2:
                guard(lab)
                start = time.monotonic() - acknowledged
                observed = (await count(site, slot))[:2]
                finish = time.monotonic() - acknowledged
                samples.append(
                    {
                        "start_ms": round(start * 1000, 2),
                        "end_ms": round(finish * 1000, 2),
                        "answer": observed,
                    }
                )
                if observed == wanted:
                    if first is None:
                        first = finish
                elif observed != [200, total] or first is not None or arm == "off":
                    raise ValueError(f"{part}/{site}: unexpected response after acknowledgement: {observed}")
                await asyncio.sleep(0.06)
            if first is None or (arm == "one-second" and first > 1.75):
                raise ValueError(f"{part}/{site}: first correct result exceeded the lab bound: {first}")
            return {
                "last_old_ms": max([x["end_ms"] for x in samples if x["answer"] == [200, total]] or [0]),
                "first_fresh_ms": round(first * 1000, 2),
                "samples": samples,
            }

        out[part] = dict(zip(SITES, await asyncio.gather(*(observe(s) for s in SITES))))
    return out


async def dashboard_coherence(lab, arm, endpoints, db, views):
    """Observe a just-acknowledged new alert and key deletion on the actual dashboard page."""
    from cache_replay_model import BASE_PATH

    guard(lab)
    prior = views["blue", "all"]

    async def read(site, slot):
        code, body, _, _, trace = await asyncio.to_thread(
            request, lab, endpoints[site], slot, BASE_PATH, None, True
        )
        return code, body, trace.get("cache_result")

    measured = {}
    for part, slot in (("new_alert", 0), ("key_revocation", 12)):
        await asyncio.sleep(1.1)
        for turn in range(2):
            warm = await asyncio.gather(*(read(site, slot) for site in SITES))
            for code, body, how in warm:
                if code != 200 or turn == 1 and arm == "one-second" and how != "hit":
                    raise ValueError("Dashboard probe requires a verified warm page on each site")
                check_view(body, prior)
        if part == "new_alert":
            payload = run_lab.alert_for("000-replay-dashboard-marker", 0)
            writer = (lab.ingress if lab.write_route == "direct" else endpoints)["a"]
            code, body, _, _, _ = await asyncio.to_thread(request, lab, writer, 8, "/alert", payload)
            if code not in (200, 201):
                raise ValueError("Dashboard visibility marker was not acknowledged")
            a = body["alert"]
            row = [a[k] for k in ("id", "resource", "severity", "status", "customer")]
            if (
                row[1] != "000-replay-dashboard-marker"
                or row[4] != "blue"
                or not row[1] < prior["rows"][0][1]
            ):
                raise ValueError("Dashboard marker is outside the first page or intended customer")
            updated = {
                "rows": ([row] + prior["rows"])[:100],
                "total": prior["total"] + 1,
                "sev": dict(Counter(prior["sev"]) + Counter([row[2]])),
                "status": dict(Counter(prior["status"]) + Counter([row[3]])),
            }
        else:
            removed = await asyncio.to_thread(
                lab.sql, db, "DELETE FROM keys WHERE id='bench-key-12' RETURNING id"
            )
            if removed != "bench-key-12":
                raise ValueError("Missing disposable dashboard probe key")
        ack = time.monotonic()
        before_sha = digest(prior)
        after_sha = digest(updated) if part == "new_alert" else None

        async def observe(site):
            samples = []
            first = None
            while time.monotonic() - ack < 2.4 or len(samples) < 2:
                guard(lab)
                sent = time.monotonic()
                code, body, how = await read(site, slot)
                received = time.monotonic()
                if part == "new_alert" and code == 200:
                    fresh = body.get("total") == updated["total"]
                    check_view(body, updated if fresh else prior)
                elif part == "key_revocation" and code == 401:
                    fresh = True
                elif part == "key_revocation" and code == 200:
                    fresh = False
                    check_view(body, prior)
                else:
                    raise ValueError(f"{part}/{site}: invalid dashboard response {code}")
                page_sha = None
                if code == 200:
                    page_sha = digest(
                        {
                            "rows": [
                                [alert[k] for k in ("id", "resource", "severity", "status", "customer")]
                                for alert in body["alerts"]
                            ],
                            "total": body["total"],
                            "sev": body["severityCounts"],
                            "status": body["statusCounts"],
                        }
                    )
                if fresh and first is None:
                    first = received - ack
                if not fresh and (arm == "off" or first is not None):
                    raise ValueError(
                        "Old dashboard observed after a fresh observation or on the direct writer path"
                    )
                samples.append(
                    {
                        "start_ms": round((sent - ack) * 1000, 2),
                        "end_ms": round((received - ack) * 1000, 2),
                        "fresh": fresh,
                        "http_status": code,
                        "cache_result": how,
                        "page_sha256": page_sha,
                    }
                )
                await asyncio.sleep(0.08)
            if first is None:
                raise ValueError("Fresh dashboard was never observed")
            return {
                "last_old_ms": max([s["end_ms"] for s in samples if not s["fresh"]] or [0]),
                "first_fresh_ms": round(first * 1000, 2),
                "samples": samples,
                "old_page_sha256": before_sha,
                "fresh_page_sha256": after_sha,
            }

        measured[part] = dict(zip(SITES, await asyncio.gather(*(observe(site) for site in SITES))))
        if part == "new_alert":
            prior = updated
    return measured


async def main(args):
    profile = os.environ.get("REPLAY_PROFILE", "mixed")
    cadence = os.environ.get("REPLAY_CADENCE", "aligned")
    seed = int(os.environ.get("REPLAY_SEED", "0"))
    if (
        len(args.rates) != 1
        or args.key_count != KEY_COUNT
        or args.ingress != "primary-aware"
        or args.fault
        or args.dashboard_encoding != "gzip"
        or args.features not in ("stock", "reuse+onecount", "reuse+onecount+nolog+nosentry")
    ):
        raise ValueError("Use one rate, 13 keys, gzip and the healthy primary-aware topology")
    worker_pool = ThreadPoolExecutor(max_workers=args.client_inflight)
    asyncio.get_running_loop().set_default_executor(worker_pool)
    report = {
        "valid": False,
        "profile": profile,
        "cadence": cadence,
        "seed": seed,
        "rate": args.rates[0],
        "seconds": args.seconds,
        "repeats": args.repeats,
        "features": args.features,
        "probes_enabled": os.environ.get("REPLAY_PROBES", "1") == "1",
        "cells": [],
    }
    report["newest"] = os.environ.get("REPLAY_NEWEST", "0") == "1"
    report["feedback"] = os.environ.get("REPLAY_FEEDBACK", "0") == "1"
    report["write_route"] = os.environ.get("REPLAY_WRITE_ROUTE", "proxy")
    report["environment_list"] = os.environ.get("REPLAY_ENVIRONMENTS", "0") == "1"
    if report["environment_list"] and (report["newest"] or not profile.endswith("-5s")):
        raise ValueError("The environments oracle needs a stable, tenant-scoped five-second fixture")
    if report["write_route"] not in ("direct", "proxy"):
        raise ValueError("Unknown POST route")
    if report["feedback"] and (not profile.endswith("-5s") or args.client_inflight < 20):
        raise ValueError(
            "Completion-driven refresh requires a panel profile and at least twenty client lanes"
        )
    path = args.output / "cache-replay.json"
    save(path, report)
    try:
        with run_lab.lab_directory(args.output) as root:
            lab = replay_lab(args, root, args.output)
            report["thermal_telemetry"] = lab.thermal_telemetry
            report["netem_fixed_seed"] = os.environ.get("ALRUST_NETEM_FIXED_SEED", "1") == "1"
            lab.write_route = report["write_route"]
            lab.environment_workers = None
            lab.client_keys = ["bench-replay-" + secrets.token_hex(20) for _ in range(KEY_COUNT)]
            async with AsyncExitStack() as services:
                if report["environment_list"]:
                    lab.environment_workers = services.enter_context(ThreadPoolExecutor(max_workers=16))
                await lab.cool()
                await lab.start(services)
                await lab.cool()
                await lab.seed()
                await asyncio.to_thread(tenants, lab)
                lab.env.update(CUSTOMER_VIEWS="True", ADMIN_USERS="bench")
                for rep in range(args.repeats):
                    events = plan(
                        profile, args.rates[0], args.seconds, cadence, seed + rep, newest=report["newest"]
                    )
                    pair_seed = None
                    for pos, arm in enumerate(ARMS if (seed + rep) % 2 == 0 else ARMS[::-1]):
                        db = lab.epoch = f"replay_{rep}_{pos}"
                        cell = {
                            "arm": arm,
                            "rep": rep,
                            "valid": False,
                            "arrivals": [],
                            "plan_sha256": digest(events),
                        }
                        report["cells"].append(cell)
                        save(path, report)
                        await lab.cool()
                        await lab.clone_fixture(db)
                        async with AsyncExitStack() as apps:
                            for site, vm in product(SITES, ("vm1", "vm2")):
                                await lab.start_app(apps, site, vm, db, args.features, db)
                            await run_lab.wait_until(
                                lambda: all(
                                    run_lab.http(p, "/management/gtg", host=h)[0] == 200
                                    for p, h in lab.ingress.values()
                                ),
                                "both site applications",
                            )
                            await asyncio.sleep(2 * args.health_interval / 1000)
                            endpoints, nodes = (
                                await caches(lab, apps, db) if arm == "one-second" else (lab.ingress, {})
                            )
                            cell["routes_before"] = await asyncio.to_thread(probe_routes, lab, db)
                            before = await asyncio.to_thread(snapshot, lab, db)
                            views = expected_views(before["alerts"])
                            if any(not v["rows"] for v in views.values()):
                                raise ValueError("Empty synthetic tenant/filter view")
                            cell["seed_sha256"] = digest(before)
                            if report["newest"]:
                                cell["ordered_seed"] = await asyncio.to_thread(
                                    mutations.ordered_snapshot, lab, db, "a"
                                )
                            if pair_seed not in (None, cell["seed_sha256"]):
                                raise ValueError("Paired cells have different initial fixtures")
                            pair_seed = cell["seed_sha256"]
                            await lab.cool()
                            cell["scope_limits"] = run_lab.resource_caps()
                            cell["clock_ticks_per_s"] = os.sysconf("SC_CLK_TCK")
                            cell["cpu_before"] = await asyncio.to_thread(lab.cpu_sample)
                            stat0 = await asyncio.to_thread(counters, nodes)
                            lab.peak_cpu = None
                            cpu_start = run_lab.cgroup_cpu_stats()
                            try:
                                eligible = await replay(
                                    lab, arm, endpoints, events, views, cell, report["feedback"]
                                )
                            finally:
                                cell["peak_cpu_c"] = lab.peak_cpu
                            cell["scope_cpu_delta"] = {
                                k: value - cpu_start[k]
                                for k, value in run_lab.cgroup_cpu_stats().items()
                                if k in cpu_start
                            }
                            stat1 = await asyncio.to_thread(counters, nodes)
                            cell["cpu_after"] = await asyncio.to_thread(lab.cpu_sample)
                            cell["scope_memory"] = run_lab.cgroup_memory_stats()
                            after = await asyncio.to_thread(snapshot, lab, db)
                            lsn = await asyncio.to_thread(
                                lab.sql, "postgres", "SELECT pg_current_wal_flush_lsn()"
                            )
                            await lab.wait_for_replay(lsn, "standby to replay timed writes")
                            standby = await asyncio.to_thread(snapshot, lab, db, "b")
                            cell.update(
                                primary_sha256=digest(after),
                                standby_sha256=digest(standby),
                                counters_before=stat0,
                                counters_after=stat1,
                            )
                            # These contain only synthetic fixture IDs, counts and ordinals.
                            # Retain the primary records so a reader can audit the ledger offline.
                            cell["ledger_before"] = before
                            cell["ledger_after"] = after
                            save(path, report)
                            validate_timed(
                                before,
                                after,
                                standby,
                                cell["arrivals"],
                                stat0,
                                stat1,
                                eligible,
                                report["newest"],
                            )
                            if report["newest"]:
                                cell["ordered_final"] = await asyncio.to_thread(
                                    mutations.ordered_snapshot, lab, db, "a"
                                )
                                if cell["ordered_final"] != await asyncio.to_thread(
                                    mutations.ordered_snapshot, lab, db, "b"
                                ):
                                    raise ValueError("Standby newest order did not match primary")
                                obs = mutations.verify(
                                    cell["ordered_seed"], cell["ordered_final"], cell["arrivals"], arm
                                )
                                cell["page_consistency"] = obs
                                save(path, report)
                                if any(not o["valid"] for o in obs):
                                    raise ValueError(
                                        "A newest dashboard page and its counts have no common verified snapshot"
                                    )
                                views = expected_views(after["alerts"])
                            cell["groups"] = aggregate(cell["arrivals"])
                            cell["routes_after"] = await asyncio.to_thread(probe_routes, lab, db)
                            if report["probes_enabled"]:
                                if os.environ.get("REPLAY_PROBE_ROUTE", "count") == "dashboard":
                                    cell["dashboard_coherence"] = await dashboard_coherence(
                                        lab, arm, endpoints, db, views
                                    )
                                else:
                                    cell["coherence"] = await coherence(lab, arm, endpoints, db, views)
                            cell["valid"] = True
                            save(path, report)
                        await asyncio.to_thread(lab.sql, "postgres", f"DROP DATABASE {db} WITH (FORCE)")
                    print(f"Verified {profile}/{cadence} pair {rep + 1}", flush=True)
        report["valid"] = True
    except BaseException as exc:
        report["error"] = failure_reason(exc)
        raise
    finally:
        save(path, report)


if __name__ == "__main__":
    run_lab.main = main
    run_lab.cli()
