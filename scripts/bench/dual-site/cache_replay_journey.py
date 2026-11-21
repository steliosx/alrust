"""Validate the independent environments request made alongside an Alerta list refresh."""

import time


def environments(request, lab, endpoint, event, expected, t0, via_proxy):
    start = time.monotonic()
    code, body, route, worker, trace = request(lab, endpoint, event["key"], "/environments")
    expected_row = {
        "environment": "Production",
        "count": expected["total"],
        "severityCounts": expected["sev"],
        "statusCounts": expected["status"],
    }
    if (
        code != 200
        or route != "a"
        or worker not in ("vm1", "vm2")
        or trace.get("cache_result") != ("origin" if via_proxy else None)
        or body.get("status") != "ok"
        or body.get("total") != 1
        or body.get("page") != 1
        or body.get("pages") != 1
        or body.get("more") is not False
        or body.get("environments") != [expected_row]
    ):
        raise ValueError(f"Incorrect uncached environments response (HTTP {code}, route {route}/{worker})")
    return {
        "started_s": start - t0,
        "finished_s": time.monotonic() - t0,
        "http_status": code,
        "wire_bytes": trace["encoded_body_bytes"],
        "cache_result": trace.get("cache_result"),
    }
