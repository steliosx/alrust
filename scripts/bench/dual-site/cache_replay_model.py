"""Deterministic synthetic dashboard arrivals. No production keys or customer traffic."""

from collections import Counter
from hashlib import sha256
import json
import math

SITES = ("a", "b")
TENANTS = ("blue", "gold", "green", "red")
VIEWS = ("all", "major")
PROFILES = ("wallboard", "mixed", "personal", "unique", "shared-5s", "personal-5s")
PAIR_GAPS = {f"paired-{ms}": ms / 1000 for ms in (0, 250, 500, 750, 1000, 1250)}
BASE_PATH = "/alerts?sort-by=resource&page=1&page-size=100&environment=Production"


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan(profile, rate, seconds, cadence, seed=0, newest=False):
    """A complete wave has one write and four dashboard reads per site."""
    if profile not in PROFILES or cadence not in ("aligned", "spread", *PAIR_GAPS):
        raise ValueError("Unknown profile or cadence")
    if cadence in PAIR_GAPS and not profile.endswith("-5s"):
        raise ValueError("A fixed gap between matching panels requires a five-second profile")
    if rate < 1 or seconds < 1 or rate * seconds % 10:
        raise ValueError("A whole number of ten-arrival waves is required")
    if profile.endswith("-5s"):
        return apply_order(five_second_panels(profile, rate, seconds, cadence, seed), newest, seed)
    events = []
    for wave in range(rate * seconds // 10):
        for lane in range(5):
            for position in range(2):
                site = SITES[(position + wave + seed) % 2]
                n = len(events)
                due = (wave * 10 + (n % 10 if cadence == "spread" else 0)) / rate
                if lane == 0:
                    index = (wave * 2 + position + seed) % 24
                    row = {"kind": "write", "index": index, "key": 8 + (index % 4)}
                else:
                    # Shared boards reuse a tenant credential and exact URL. Personal
                    # views use another credential and/or a different real filter.
                    tenant = (wave + seed) % 4
                    key, view, unique = tenant, "all", False
                    if profile == "mixed" and lane > 2:
                        key = (tenant + lane - 2) % 4 + (4 if lane == 4 else 0)
                        view = "major" if (wave + lane) % 2 else "all"
                    elif profile in ("personal", "unique"):
                        key = (tenant + lane + (4 if wave % 2 else 0)) % 8
                        view = VIEWS[(lane + wave) % 2]
                        unique = profile == "unique"
                    path = BASE_PATH + ("&severity=major" if view == "major" else "")
                    if unique:
                        path += f"&_={seed}-{n}"
                    row = {"kind": "dashboard", "key": key, "view": view, "path": path}
                events.append(dict(row, site=site, sequence=n, due_s=due))
    return apply_order(events, newest, seed)


def apply_order(events, newest, seed):
    if newest:
        write = 0
        for row in events:
            if row["kind"] == "dashboard":
                row["newest"] = True
                row["path"] = row["path"].replace("sort-by=resource", "sort-by=lastReceiveTime")
            else:
                # Run one fresh arrival per four writes. Advance which tenant is
                # fresh in successive blocks so the entire trace covers all four.
                row["fresh"] = write % 4 == (write // 4 + seed) % 4
                write += 1
    return events


def five_second_panels(profile, rate, seconds, cadence, seed):
    """Sixteen panels across the two sites, each refreshing once every five seconds.

    A panel's credential, filter and site persist across refreshes. Each site
    has two panels per tenant. Four interleaved writes per five seconds keep
    the offered load identical when panel identity or timing changes.
    """
    if rate != 4 or seconds % 5:
        raise ValueError("The five-second panel trace requires 4 arrivals/s and whole periods")
    events = []
    for period in range(seconds // 5):
        for number in range(16):
            site, panel = SITES[number % 2], number // 2
            tenant = (panel // 2 + seed) % 4
            key = tenant + (4 if profile == "personal-5s" and panel % 2 else 0)
            view = "major" if tenant == 3 else "all"
            # In the spread case, the two panels of a tenant at the same site
            # are half a refresh period apart. No nominal overlap within one TTL.
            phase_slot = (number % 2 + 2 * (panel // 2) + 8 * (panel % 2) + seed) % 16
            phase = phase_slot * 5 / 16 if cadence == "spread" else 0
            if cadence in PAIR_GAPS:
                group_phase = ((panel // 2 + seed) % 4) * 1.25 + (number % 2) * 5 / 32
                phase = (group_phase + (panel % 2) * PAIR_GAPS[cadence]) % 5
            path = BASE_PATH + ("&severity=major" if view == "major" else "")
            events.append(
                {
                    "kind": "dashboard",
                    "site": site,
                    "key": key,
                    "view": view,
                    "path": path,
                    "panel": number,
                    "due_s": period * 5 + phase,
                }
            )
        for w in range(4):
            index = (period * 4 + w + seed) % 24
            events.append(
                {
                    "kind": "write",
                    "site": SITES[(w + seed) % 2],
                    "key": 8 + index % 4,
                    "index": index,
                    "due_s": period * 5 + 0.15 + w * 1.25,
                }
            )
    events.sort(key=lambda row: row["due_s"])
    return [dict(row, sequence=n) for n, row in enumerate(events)]


def expected_views(alerts):
    views = {}
    for tenant in TENANTS:
        for view in VIEWS:
            rows = sorted(
                (r for r in alerts if r[4] == tenant and (view == "all" or r[2] == view)), key=lambda r: r[1]
            )
            views[(tenant, view)] = {
                "rows": [r[:5] for r in rows[:100]],
                "total": len(rows),
                "sev": dict(Counter(r[2] for r in rows)),
                "status": dict(Counter(r[3] for r in rows)),
            }
    return views


def check_view(body, expected):
    if (
        body.get("status") != "ok"
        or body.get("total") != expected["total"]
        or body.get("pageSize") != 100
        or body.get("page") != 1
        or body.get("pages") != math.ceil(expected["total"] / 100)
        or body.get("more") != (expected["total"] > 100)
        or body.get("severityCounts") != expected["sev"]
        or body.get("statusCounts") != expected["status"]
    ):
        raise ValueError("Dashboard pagination, severity or status counts disagree with SQL")
    rows = [[r[k] for k in ("id", "resource", "severity", "status", "customer")] for r in body["alerts"]]
    if rows != expected["rows"]:
        raise ValueError("Dashboard identity, tenant, display fields or order disagree with SQL")


def percentiles(values):
    if not values:
        return {}
    values = sorted(values)
    return {
        k: round(values[math.ceil(len(values) * p) - 1], 2)
        for k, p in (("p50", 0.5), ("p95", 0.95), ("max", 1))
    }
