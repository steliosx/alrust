#!/usr/bin/env python3
"""Closed-loop HTTP/1.1 client for identical POST /alert sequences.

One keep-alive connection per worker. Payload index n on worker w never shares
resource, event, and environment with another worker. Each worker sends its
indexes in order, so duplicate and correlate matching does not race.

Mix for each worker, by n % 10 (4 new / 3 duplicate / 3 correlate):
  0-3  new alert, unique resource wNN-new-{n}
  4-6  duplicate-class, stable resource wNN-dup-{0,1,2}, identical fields
  7-9  correlate-class, stable resource wNN-cor-{0,1,2}
       severity cycles major, critical, minor, warning once per 10 indexes
       value changes with n

The first hit of a duplicate or correlate slot creates a row. Later duplicate
slots repeat every field. Later correlate slots change severity and value.
"""
import asyncio
import json
import math
import socket
import time

SEVERITIES = ["major", "critical", "minor", "warning"]


def payload(worker, n):
    kind = n % 10
    if kind < 4:
        return {
            "resource": f"w{worker:02d}-new-{n}",
            "event": "HttpError",
            "environment": "Production",
            "service": ["Web"],
            "group": "BenchWrite",
            "severity": "major",
            "text": "new",
            "value": str(n),
            "origin": "bench-write",
            "tags": ["bench", "new"],
            "attributes": {"worker": worker, "n": n, "kind": "new"},
        }
    if kind < 7:
        slot = kind - 4
        return {
            "resource": f"w{worker:02d}-dup-{slot}",
            "event": "DiskFull",
            "environment": "Production",
            "service": ["Storage"],
            "group": "BenchWrite",
            "severity": "major",
            "text": "duplicate",
            "value": "1",
            "origin": "bench-write",
            "tags": ["bench", "dup"],
            "attributes": {"worker": worker, "slot": slot, "kind": "dup"},
        }
    slot = kind - 7
    return {
        "resource": f"w{worker:02d}-cor-{slot}",
        "event": "CpuHigh",
        "environment": "Production",
        "service": ["Web"],
        "group": "BenchWrite",
        "severity": SEVERITIES[(n // 10) % 4],
        "text": "correlate",
        "value": str(n),
        "origin": "bench-write",
        "tags": ["bench", "cor"],
        "attributes": {"worker": worker, "slot": slot, "kind": "cor", "n": n},
    }


def encode_payloads(worker, start, count):
    bodies = []
    for n in range(start, start + count):
        bodies.append(json.dumps(payload(worker, n), separators=(",", ":")).encode())
    return bodies


def percentile(values, p):
    """Nearest-rank percentile. rank = ceil(p/100 * N), 1-based, then clamp."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((p / 100.0) * len(ordered))
    idx = min(max(rank, 1), len(ordered)) - 1
    return ordered[idx]


def split_count(total, concurrency):
    base, rem = divmod(total, concurrency)
    return [base + (1 if i < rem else 0) for i in range(concurrency)]


def worker_ranges(n_warm, n_timed, concurrency):
    warm_counts = split_count(n_warm, concurrency)
    timed_counts = split_count(n_timed, concurrency)
    warm = [(0, warm_counts[i]) for i in range(concurrency)]
    timed = [(warm_counts[i], timed_counts[i]) for i in range(concurrency)]
    return warm, timed


async def open_conn(host, port):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return reader, writer


async def read_response(reader):
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("eof before status")
    parts = status_line.split()
    if len(parts) < 2:
        raise ConnectionError("bad status line")
    status = int(parts[1])
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n"):
            break
        if not line:
            raise ConnectionError("eof in headers")
        text = line.decode("iso-8859-1")
        key, value = text.split(":", 1)
        headers[key.lower().strip()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        chunks = []
        while True:
            size_line = await reader.readline()
            if not size_line:
                raise ConnectionError("eof in chunk size")
            size = int(size_line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                while True:
                    trailer = await reader.readline()
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                body = b"".join(chunks)
                break
            chunks.append(await reader.readexactly(size))
            delim = await reader.readexactly(2)
            if delim != b"\r\n":
                raise ConnectionError("bad chunk delimiter")
    else:
        length = int(headers.get("content-length", "0"))
        body = await reader.readexactly(length) if length else b""
    return status, headers, body


def request_bytes(method, path, host, port, api_key, body=None):
    headers = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}:{port}",
        f"Authorization: Key {api_key}",
        "Accept: application/json",
        "Connection: keep-alive",
    ]
    data = b""
    if body is not None:
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(body)}")
        data = body
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + data


async def one_request(reader, writer, raw, timeout):
    writer.write(raw)
    await writer.drain()
    status, _headers, body = await asyncio.wait_for(read_response(reader), timeout)
    return status, body


async def send_all(reader, writer, raws, timeout, record, deadline, reconnect):
    for raw in raws:
        if time.perf_counter() >= deadline:
            return
        started = time.perf_counter()
        try:
            status, body = await one_request(reader, writer, raw, timeout)
            elapsed = time.perf_counter() - started
            err = "" if status == 201 else body[:240].decode("utf-8", "replace")
            record.append((status == 201, elapsed, status, err))
        except Exception as exc:
            elapsed = time.perf_counter() - started
            message = type(exc).__name__ + ": " + str(exc)[:200]
            record.append((False, elapsed, 0, message))
            try:
                reader, writer = await reconnect()
            except Exception as exc2:
                record[-1] = (False, elapsed, 0, message + "; reconnect " + type(exc2).__name__)
                return


def summarize_records(records, accept_status):
    latencies = [rec[1] for rec in records if rec[2] == accept_status]
    statuses = {}
    errors = {}
    for _ok, _dt, status, err in records:
        if status:
            statuses[str(status)] = statuses.get(str(status), 0) + 1
        if err:
            errors[err] = errors.get(err, 0) + 1
    offered = len(records)
    accepted = len(latencies)
    if len(errors) > 8:
        items = sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        errors = dict(items)
        errors_truncated = True
    else:
        errors_truncated = False
    return {
        "offered": offered,
        "accepted": accepted,
        "p50_ms": None if not latencies else percentile(latencies, 50) * 1000.0,
        "p99_ms": None if not latencies else percentile(latencies, 99) * 1000.0,
        "status_codes": statuses,
        "error_samples": errors,
        "error_samples_truncated": errors_truncated,
        "error_count": offered - accepted,
    }


async def _connect_all(host, port, concurrency):
    pairs = []
    for _i in range(concurrency):
        pairs.append(await open_conn(host, port))
    return pairs


async def run_posts(host, port, api_key, per_worker_ranges, timeout=60.0, deadline_s=240.0):
    """Send a fixed sequence. Connections open before the timer.

    elapsed_s covers the closed-loop sends only. A worker stops taking new
    requests at deadline_s after the timer starts. Unsent requests are not
    counted as offered.
    """
    prepared = []
    for worker, (start, count) in enumerate(per_worker_ranges):
        bodies = encode_payloads(worker, start, count)
        raws = [request_bytes("POST", "/alert", host, port, api_key, body) for body in bodies]
        prepared.append(raws)
    pairs = await _connect_all(host, port, len(prepared))
    records = [[] for _ in prepared]

    async def go_one(index, raws):
        reader, writer = pairs[index]

        async def reconnect():
            nonlocal reader, writer
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            reader, writer = await open_conn(host, port)
            return reader, writer

        try:
            await send_all(reader, writer, raws, timeout, records[index], deadline, reconnect)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    t0 = time.perf_counter()
    deadline = t0 + deadline_s
    await asyncio.gather(*[go_one(i, raws) for i, raws in enumerate(prepared)])
    elapsed = time.perf_counter() - t0
    flat = [rec for bucket in records for rec in bucket]
    summary = summarize_records(flat, 201)
    planned = sum(len(raws) for raws in prepared)
    summary["elapsed_s"] = elapsed
    summary["rps"] = (summary["accepted"] / elapsed) if elapsed else None
    summary["planned"] = planned
    summary["truncated"] = summary["offered"] != planned
    summary["per_worker_offered"] = [len(bucket) for bucket in records]
    return summary


async def run_posts_for_seconds(host, port, api_key, concurrency, seconds, timeout=30.0):
    """Closed loop for about `seconds` after connections are open. Sizing only."""
    pairs = await _connect_all(host, port, concurrency)
    counters = [0 for _ in range(concurrency)]
    records = [[] for _ in range(concurrency)]
    stop = time.perf_counter() + seconds

    async def worker(wid):
        reader, writer = pairs[wid]
        try:
            while time.perf_counter() < stop:
                n = counters[wid]
                counters[wid] = n + 1
                body = json.dumps(payload(wid, n), separators=(",", ":")).encode()
                raw = request_bytes("POST", "/alert", host, port, api_key, body)
                started = time.perf_counter()
                try:
                    status, resp = await one_request(reader, writer, raw, timeout)
                    elapsed = time.perf_counter() - started
                    err = "" if status == 201 else resp[:180].decode("utf-8", "replace")
                    records[wid].append((status == 201, elapsed, status, err))
                except Exception as exc:
                    records[wid].append((False, time.perf_counter() - started, 0, type(exc).__name__))
                    try:
                        writer.close()
                        reader, writer = await open_conn(host, port)
                    except Exception:
                        return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    t0 = time.perf_counter()
    await asyncio.gather(*[worker(i) for i in range(concurrency)])
    elapsed = time.perf_counter() - t0
    flat = [rec for bucket in records for rec in bucket]
    summary = summarize_records(flat, 201)
    summary["elapsed_s"] = elapsed
    summary["rps"] = (summary["accepted"] / elapsed) if elapsed else None
    summary["per_worker_completed"] = [len(bucket) for bucket in records]
    summary["truncated"] = False
    return summary


async def run_gets(host, port, api_key, concurrency, path, stop_event, ready_event, timeout=60.0):
    records = [[] for _ in range(concurrency)]
    raw = request_bytes("GET", path, host, port, api_key)
    connected = 0
    connect_lock = asyncio.Lock()

    async def worker(wid):
        nonlocal connected
        try:
            reader, writer = await open_conn(host, port)
        except Exception as exc:
            records[wid].append((False, 0.0, 0, "connect " + type(exc).__name__))
            return
        async with connect_lock:
            connected += 1
            if connected == concurrency:
                ready_event.set()
        try:
            while not stop_event.is_set():
                started = time.perf_counter()
                try:
                    status, body = await one_request(reader, writer, raw, timeout)
                    elapsed = time.perf_counter() - started
                    err = "" if status == 200 else body[:180].decode("utf-8", "replace")
                    records[wid].append((status == 200, elapsed, status, err))
                except Exception as exc:
                    records[wid].append((False, time.perf_counter() - started, 0, type(exc).__name__))
                    if stop_event.is_set():
                        return
                    try:
                        writer.close()
                        reader, writer = await open_conn(host, port)
                    except Exception:
                        return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    await asyncio.gather(*[worker(i) for i in range(concurrency)])
    flat = [rec for bucket in records for rec in bucket]
    summary = summarize_records(flat, 200)
    summary["truncated"] = False
    return summary


def self_check():
    assert payload(0, 4) == payload(0, 14)
    assert payload(0, 5) == payload(0, 15)
    assert payload(0, 6) == payload(0, 16)
    first = payload(0, 7)
    later = payload(0, 17)
    assert first["resource"] == later["resource"]
    assert first["event"] == later["event"]
    assert first["environment"] == later["environment"]
    assert first["severity"] != later["severity"]
    assert first["value"] != later["value"]
    news = [payload(1, n)["resource"] for n in range(4)]
    assert len(set(news)) == 4
    assert payload(0, 0)["resource"] != payload(1, 0)["resource"]
    kinds = []
    for n in range(10):
        resource = payload(3, n)["resource"]
        if "-new-" in resource:
            kinds.append("new")
        elif "-dup-" in resource:
            kinds.append("dup")
        else:
            kinds.append("cor")
    assert kinds == ["new"] * 4 + ["dup"] * 3 + ["cor"] * 3
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile([5], 99) == 5
    warm, timed = worker_ranges(8, 16, 8)
    assert warm == [(0, 1)] * 8
    assert timed == [(1, 2)] * 8
    print("write_client self-check ok")


if __name__ == "__main__":
    self_check()
