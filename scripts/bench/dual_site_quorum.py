#!/usr/bin/env python3
"""Exercise a disposable three-member etcd quorum through delayed loopback links."""

import argparse
import asyncio
import base64
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


MEMBERS = ("site-a", "site-b", "witness")
HOST = "127.0.0.1"
KEY = base64.b64encode(b"/dual-site-bench/probe").decode()
HTTP = build_opener(ProxyHandler({}))


async def relay(reader, writer, delay):
    # Delay TCP stream chunks in each direction. The guest lab uses netem for
    # packet-level delay, retransmission and loss.
    while data := await reader.read(65536):
        await asyncio.sleep(delay)
        writer.write(data)
        await writer.drain()
    writer.write_eof()


async def proxy(source, client, target, delay):
    try:
        backend, upstream = await asyncio.open_connection(HOST, target)
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(relay(source, upstream, delay))
                tasks.create_task(relay(backend, client, delay))
        finally:
            upstream.close()
    except* OSError:
        pass
    finally:
        client.close()


def port(server):
    return server.sockets[0].getsockname()[1]


@asynccontextmanager
async def delayed_link(target, delay, listen_host=HOST):
    connections = set()

    def connect(reader, writer):
        task = asyncio.create_task(proxy(reader, writer, target, delay))
        connections.add(task)
        task.add_done_callback(connections.discard)

    server = await asyncio.start_server(connect, listen_host, 0)
    try:
        yield port(server)
    finally:
        server.close()
        active = tuple(connections)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        await server.wait_closed()


async def calibrate(delay, listen_host=HOST):
    async def echo(reader, writer):
        try:
            while data := await reader.read(1):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async with await asyncio.start_server(echo, HOST, 0) as backend:
        async with delayed_link(port(backend), delay, listen_host) as proxy_port:
            reader, writer = await asyncio.open_connection(listen_host, proxy_port)
            samples = []
            try:
                for _ in range(12):
                    start = time.monotonic()
                    writer.write(b"x")
                    await writer.drain()
                    if await reader.readexactly(1) != b"x":
                        raise RuntimeError("Calibration echo returned the wrong byte")
                    samples.append((time.monotonic() - start) * 1000)
            finally:
                writer.close()
                await writer.wait_closed()
    return {"median_rtt_ms": round(statistics.median(samples), 2),
            "min_rtt_ms": round(min(samples), 2), "max_rtt_ms": round(max(samples), 2),
            "samples": len(samples)}


def client_call(endpoint, operation, value=None):
    payload = {"key": KEY}
    if value is not None:
        payload["value"] = value
    request = Request(f"{endpoint}/v3/kv/{operation}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    with HTTP.open(request, timeout=1.2) as response:
        return json.load(response)


async def confirm_write(survivors, seconds=12):
    writer, reader = survivors
    value = base64.b64encode(uuid4().bytes).decode()
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        try:
            await asyncio.to_thread(client_call, writer, "put", value)
            outcome = await asyncio.to_thread(client_call, reader, "range")
            if [row["value"] for row in outcome.get("kvs", [])] == [value]:
                return round((time.monotonic() - start) * 1000, 1)
        except (URLError, TimeoutError):
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("A write could not be confirmed through a second survivor")


async def stop(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()


async def case(binary, delay, lost_member):
    # Reserve all addresses while choosing ports to avoid duplicates. Release
    # immediately before starting the child processes that bind them.
    reservations = [socket.socket() for _ in range(6)]
    async with AsyncExitStack() as links:
        directory = links.enter_context(tempfile.TemporaryDirectory(prefix="alrust-etcd-"))
        root = Path(directory)
        processes = {}
        try:
            for sock in reservations:
                sock.bind((HOST, 0))
            peers, clients = (
                dict(zip(MEMBERS, (s.getsockname()[1] for s in group), strict=True))
                for group in (reservations[:3], reservations[3:])
            )
            advertised = {}
            for name, target in peers.items():
                proxy_port = await links.enter_async_context(delayed_link(target, delay))
                advertised[name] = f"http://{HOST}:{proxy_port}"
            members = ",".join(f"{name}={url}" for name, url in advertised.items())
            endpoint = {name: f"http://{HOST}:{number}" for name, number in clients.items()}
            token = f"alrust-bench-{uuid4()}"
            for sock in reservations:
                sock.close()
            for name in MEMBERS:
                with (root / f"{name}.log").open("wb") as log:
                    processes[name] = await asyncio.create_subprocess_exec(
                        binary, "--name", name, "--data-dir", str(root / name),
                        "--listen-peer-urls", f"http://{HOST}:{peers[name]}",
                        "--initial-advertise-peer-urls", advertised[name],
                        "--listen-client-urls", endpoint[name],
                        "--advertise-client-urls", endpoint[name],
                        "--initial-cluster", members, "--initial-cluster-token", token,
                        "--log-level", "warn",
                        env={**{k: v for k, v in os.environ.items() if not k.startswith("ETCD_")},
                             "GOMAXPROCS": "1"},
                        stdout=log, stderr=log,
                    )
            # Establish a usable cluster via every member before injecting a loss.
            await confirm_write((endpoint["site-a"], endpoint["site-b"]))
            await confirm_write((endpoint["site-b"], endpoint["witness"]))
            survivors = [name for name in MEMBERS if name != lost_member]
            started = time.monotonic()
            await stop(processes[lost_member])
            await confirm_write([endpoint[name] for name in survivors])
            surviving_write_ms = round((time.monotonic() - started) * 1000, 1)

            first, last = survivors
            await stop(processes[first])
            try:
                await asyncio.to_thread(client_call, endpoint[last], "put", "bm8=")
            except (URLError, TimeoutError):
                if processes[last].returncode is not None:
                    raise RuntimeError("The final member exited during the quorum check")
            else:
                raise RuntimeError("A write returned success with only one voter")
            return {"stopped_member": lost_member, "survivors": survivors,
                    "write_confirmed_ms": surviving_write_ms, "one_voter_write": "unavailable"}
        except Exception as exc:
            logs = {file.name: file.read_text(errors="replace")[-1800:]
                    for file in root.glob("*.log")}
            raise RuntimeError(f"{lost_member}: {exc}\n{json.dumps(logs)}") from exc
        finally:
            for sock in reservations:
                sock.close()
            await asyncio.gather(*(stop(process) for process in processes.values()))


async def run(binary, delay_ms):
    result = {"one_way_delay_ms": delay_ms,
              "calibration": await asyncio.wait_for(calibrate(delay_ms / 1000), 10)}
    if binary is not None:
        version = subprocess.check_output([binary, "--version"], text=True, timeout=5)
        with binary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result["etcd_version"] = version.splitlines()[0]
        result["etcd_sha256"] = digest
        result["cases"] = [await case(binary, delay_ms / 1000, member) for member in MEMBERS]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--etcd", type=lambda path: Path(path).resolve(),
                        help="path to an independently verified etcd binary")
    parser.add_argument("--one-way-ms", type=int, default=25)
    args = parser.parse_args()
    if not 0 <= args.one_way_ms <= 100:
        parser.error("--one-way-ms must be between 0 and 100 for this rehearsal")
    print(json.dumps(asyncio.run(run(args.etcd, args.one_way_ms)), indent=2))
