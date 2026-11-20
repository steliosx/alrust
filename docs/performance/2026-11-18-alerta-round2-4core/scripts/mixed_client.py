#!/usr/bin/env python3
"""One load process for the PR #263 mixed suite: writers OR readers.

Reads a JSON spec on stdin. The API key comes from the env var BENCH_KEY.
All processes of a cell share one CLOCK_REALTIME start (start_at).
Phases: warm-up [start_at, start_at+warm_s), timed [.., +timed_s); a request is
counted in the phase in which it ENDED. No new request starts at or after the
timed end; requests in flight then finish and are counted as "drain".

Writers: writer w owns identities i=0..999 (resource mix-w{w}-{i:04d}); it sends
them in order and wraps. Every field but resource is fixed, so the first 2xx
for an identity inserts a row and every later one is a duplicate.
Readers: GET the list path; each answer must be 200, parse as JSON, hold at
most 50 alerts, in non-increasing lastReceiveTime order.
"""
import asyncio
import json
import os
import socket
import sys
import time

IDENTITIES = 1000


def body_for(w, i):
    return json.dumps({
        "resource": f"mix-w{w}-{i:04d}",
        "event": "BenchMixed",
        "environment": "Production",
        "service": ["Web"],
        "group": "BenchMixed",
        "severity": "major",
        "text": "mixed",
        "value": "1",
        "origin": "bench-mixed",
        "tags": ["bench"],
    }, separators=(",", ":")).encode()


def req(method, path, port, key, body=None):
    h = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Authorization: Key {key}",
         "Accept: application/json", "Connection: keep-alive"]
    if body is not None:
        h += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    return ("\r\n".join(h) + "\r\n\r\n").encode() + (body or b"")


async def read_resp(reader):
    line = await reader.readline()
    if not line:
        raise ConnectionError("eof")
    status = int(line.split()[1])
    headers = {}
    while True:
        l = await reader.readline()
        if l in (b"\r\n", b"\n"):
            break
        if not l:
            raise ConnectionError("eof in headers")
        k, v = l.decode("iso-8859-1").split(":", 1)
        headers[k.lower().strip()] = v.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        chunks = []
        while True:
            size = int((await reader.readline()).split(b";", 1)[0].strip(), 16)
            if size == 0:
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
                break
            chunks.append(await reader.readexactly(size))
            await reader.readexactly(2)
        return status, b"".join(chunks)
    n = int(headers.get("content-length", "0"))
    return status, (await reader.readexactly(n) if n else b"")


async def conn(port):
    r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
    s = w.get_extra_info("socket")
    if s is not None:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return r, w


def phase_of(t, t1, t2):
    if t < t1:
        return "warmup"
    if t < t2:
        return "timed"
    return "drain"


def check_list(body):
    try:
        doc = json.loads(body)
    except Exception:
        return "parse"
    alerts = doc.get("alerts")
    if not isinstance(alerts, list):
        return "no-alerts-array"
    if len(alerts) > 50:
        return "more-than-50"
    lrt = [a.get("lastReceiveTime") for a in alerts]
    if any(not isinstance(x, str) for x in lrt):
        return "lastReceiveTime-missing"
    for a, b in zip(lrt, lrt[1:]):
        if a < b:
            return "order"
    return None


async def main():
    spec = json.loads(sys.stdin.read())
    key = os.environ["BENCH_KEY"]
    port = spec["port"]
    role = spec["role"]
    n = spec["n"]
    start_at = spec["start_at"]
    t1 = start_at + spec["warm_s"]
    t2 = t1 + spec["timed_s"]
    conns = [await conn(port) for _ in range(n)]
    connected_at = time.time()
    lat = {"warmup": [], "timed": [], "drain": []}
    bad = {"warmup": {}, "timed": {}, "drain": {}}
    ok_n = {"warmup": 0, "timed": 0, "drain": 0}
    per_identity = [[[0, 0, 0] for _ in range(IDENTITIES)] for _ in range(n)] if role == "writers" else None
    list_problems = {}
    raw_get = req("GET", spec.get("path", "/"), port, key)

    def note_bad(ph, label):
        bad[ph][label] = bad[ph].get(label, 0) + 1

    async def worker(w):
        r, wr = conns[w]
        k = 0
        delay = start_at - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        while time.time() < t2:
            if role == "writers":
                i = k % IDENTITIES
                raw = req("POST", "/alert", port, key, body_for(w, i))
            else:
                raw = raw_get
            k += 1
            s0 = time.time()
            try:
                wr.write(raw)
                await wr.drain()
                status, body = await asyncio.wait_for(read_resp(r), 60)
                e = time.time()
                ph = phase_of(e, t1, t2)
                if 200 <= status < 300:
                    problem = None
                    if role == "readers":
                        problem = check_list(body)
                        if problem:
                            list_problems[problem] = list_problems.get(problem, 0) + 1
                            note_bad(ph, "list-check:" + problem)
                    else:
                        per_identity[w][i][{"warmup": 0, "timed": 1, "drain": 2}[ph]] += 1
                    if problem is None:
                        ok_n[ph] += 1
                        lat[ph].append(e - s0)
                else:
                    note_bad(ph, f"http {status}: {body[:120].decode('utf-8', 'replace')}")
            except Exception as exc:
                e = time.time()
                note_bad(phase_of(e, t1, t2), type(exc).__name__)
                try:
                    wr.close()
                    r, wr = await conn(port)
                except Exception:
                    note_bad(phase_of(time.time(), t1, t2), "reconnect-failed")
                    return
        wr.close()

    await asyncio.gather(*[worker(w) for w in range(n)])
    ended_at = time.time()

    def pct(v, p):
        if not v:
            return None
        v = sorted(v)
        import math
        return v[min(max(int(math.ceil(p / 100 * len(v))), 1), len(v)) - 1] * 1000.0

    out = {
        "role": role, "n": n, "start_at": start_at, "timed_start": t1, "timed_end": t2,
        "connected_at": connected_at, "ended_at": ended_at,
        "late_start_s": max(0.0, connected_at - start_at),
        "ok": ok_n, "bad": bad,
        "timed_rate": ok_n["timed"] / spec["timed_s"],
        "timed_p50_ms": pct(lat["timed"], 50), "timed_p99_ms": pct(lat["timed"], 99),
        "errors_all_phases": sum(sum(b.values()) for b in bad.values()),
        "client_process_cpu_s": time.process_time(),
        "client_wall_s": ended_at - connected_at,
    }
    if role == "writers":
        out["per_identity"] = {
            f"mix-w{w}-{i:04d}": per_identity[w][i]
            for w in range(n) for i in range(IDENTITIES) if sum(per_identity[w][i])
        }
    else:
        out["list_problems"] = list_problems
    with open(spec["out"], "w") as fh:
        json.dump(out, fh)


asyncio.run(main())
