"""One client of the mixed benchmark (docs/performance/benchmark-methodology.md).

A closed loop over one keep-alive connection: the next request starts when
the last one is answered. It runs from the start time until SIGTERM; the
request in flight is finished first.

  write  POST /alert with the writer's own alert identities, in order,
         starting again at the first after the last. Only `resource`
         differs between identities (`mix-w<writer>-<index>`).
  read   GET the 50 newest alerts. Each answer is held to what stays true
         while rows change: a 2xx status, a body that parses, at most 50
         alerts, in descending `lastReceiveTime` order.

It then writes a gzip JSON file: every request as
[identity index (write) or 1 for a failed body check, else 0 (read),
 HTTP status (0 = no answer), start, end], times in microseconds after the
start time, and the first few body problems.

Standard library only; run with `python3 -I`.
Arguments: role host port client-number identities start-at(time.time) out-file.
The API key comes from BENCH_KEY in the environment.
"""

import gzip
import http.client
import json
import os
import signal
import socket
import sys
import time

READ_PATH = "/alerts?page-size=50&sort-by=lastReceiveTime"


def resource(writer, index):
    return f"mix-w{writer}-{index:04d}"


def write_body(writer, index):
    return json.dumps({"resource": resource(writer, index), "event": "Mixed",
                       "environment": "Production", "severity": "major",
                       "service": ["web"], "text": "mixed workload"}).encode()


def read_problem(status, raw):
    """What is wrong with one list answer, or None."""
    if not 200 <= status < 300:
        return None  # counted by its status
    try:
        alerts = json.loads(raw)["alerts"]
        times = [a["lastReceiveTime"] for a in alerts]
    except (ValueError, KeyError, TypeError) as e:
        return f"the body does not parse as a list of alerts: {e!r}"
    if len(alerts) > 50:
        return f"{len(alerts)} alerts in one page"
    if any(not isinstance(t, str) or len(t) != len(times[0]) for t in times):
        return "lastReceiveTime values are not strings of one length"
    if any(a < b for a, b in zip(times, times[1:])):
        return "the alerts are not in descending lastReceiveTime order"
    return None


def connect(host, port):
    """A connection that sends each request at once (no Nagle delay)."""
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.connect()
    conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return conn


def main():
    role, host, port, number, identities, start_at, out = sys.argv[1:8]
    if role not in ("write", "read"):
        sys.exit(f"role must be write or read, not {role!r}")
    port, number, identities, start_at = int(port), int(number), int(identities), float(start_at)
    headers = {"Authorization": f"Key {os.environ['BENCH_KEY']}", "Accept": "application/json",
               "Accept-Encoding": "identity"}
    bodies = []
    if role == "write":
        headers["Content-Type"] = "application/json"
        bodies = [write_body(number, i) for i in range(identities)]
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    conn = connect(host, port)
    requests, problems = [], []
    time.sleep(max(0.0, start_at - time.time()))
    i = 0
    while not stopping:
        t0 = time.time()
        first = i if role == "write" else 0
        try:
            if role == "write":
                conn.request("POST", "/alert", body=bodies[i], headers=headers)
            else:
                conn.request("GET", READ_PATH, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except (http.client.HTTPException, OSError):
            # No answer: for a write the outcome is unknown. The cell is not scored.
            status = 0
            conn.close()
            try:
                conn = connect(host, port)
            except OSError:
                # The server is gone: a short pause, so the loop does not spin.
                conn = http.client.HTTPConnection(host, port, timeout=30)
                time.sleep(0.05)
        t1 = time.time()
        if role == "read" and status:
            problem = read_problem(status, raw)
            if problem:
                first = 1
                if len(problems) < 5:
                    problems.append(problem)
        requests.append([first, status, round((t0 - start_at) * 1e6), round((t1 - start_at) * 1e6)])
        if role == "write":
            i = (i + 1) % identities
    conn.close()
    with gzip.open(out, "wt") as f:
        json.dump({"role": role, "number": number, "start_at": start_at,
                   "requests": requests, "problems": problems}, f)


if __name__ == "__main__":
    main()
