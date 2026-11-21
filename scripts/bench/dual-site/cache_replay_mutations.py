"""Reconstruct every possible ordered newest-page view from synthetic write acknowledgements."""

from collections import Counter
import math

from cache_replay_model import TENANTS, VIEWS, digest


def ordered_snapshot(lab, db, site):
    import json

    return json.loads(
        lab.sql(
            db,
            """SELECT json_agg(json_build_array(id, resource, severity, status, customer,
                    duplicate_count, last_receive_id) ORDER BY last_receive_time DESC,id) FROM alerts""",
            site,
        )
    )


def signature(rows, view):
    selected = [r for r in rows if view == "all" or r[2] == view]
    total = len(selected)
    page = [r for r in selected[:100]]
    counts = [total, dict(Counter(r[2] for r in selected)), dict(Counter(r[3] for r in selected))]
    return [digest(page), digest(counts), total]


def observed(body, tenant):
    total = body.get("total")
    if (
        body.get("status") != "ok"
        or type(total) is not int
        or total < 0
        or body.get("pageSize") != 100
        or body.get("page") != 1
        or body.get("pages") != math.ceil(total / 100)
        or body.get("more") is not (total > 100)
    ):
        raise ValueError("Invalid newest-page metadata")
    rows = [
        [
            r[k]
            for k in ("id", "resource", "severity", "status", "customer", "duplicateCount", "lastReceiveId")
        ]
        for r in body["alerts"]
    ]
    if (
        any(r[4] != tenant for r in rows)
        or len({r[0] for r in rows}) != len(rows)
        or not all(type(body[k]) is dict for k in ("severityCounts", "statusCounts"))
    ):
        raise ValueError("Invalid tenant, ID or distribution on newest page")
    return [digest(rows), digest([total, body["severityCounts"], body["statusCounts"]]), total]


def verify(seed, end, rows, arm):
    observations = []
    for tenant in TENANTS:
        state = [r for r in seed if r[4] == tenant]
        writes = sorted(
            (r for r in rows if r["kind"] == "write" and r["write_row"][4] == tenant),
            key=lambda r: r["started_s"],
        )
        # Reconstruction from the independent receipt ledger requires unambiguous order.
        if any(a["finished_s"] > b["started_s"] for a, b in zip(writes, writes[1:])):
            raise ValueError(f"Overlapping same-tenant writes prevent exact reconstruction ({tenant})")
        states = [{v: signature(state, v) for v in VIEWS}]
        for write in writes:
            new = write["write_row"]
            old = next((r for r in state if r[0] == new[0]), None)
            if write["fresh"]:
                if old or new[5] != 0 or any(r[1] == new[1] for r in state):
                    raise ValueError("A new-write receipt reused an ID or resource")
            elif old is None or old[:5] != new[:5] or new[5] != old[5] + 1 or new[6] == old[6]:
                raise ValueError("A duplicate receipt did not advance exactly one persisted ordinal")
            state = [new] + [r for r in state if r[0] != new[0]]
            states.append({v: signature(state, v) for v in VIEWS})
        if state != [r for r in end if r[4] == tenant]:
            raise ValueError("Reconstructed final newest-first ledger differs from PostgreSQL")
        for read in (r for r in rows if r["kind"] == "dashboard" and TENANTS[r["key"] % 4] == tenant):
            start, finish = read["started_s"], read["finished_s"]
            # A direct or origin-served query cannot predate a write acknowledged before
            # its start. A coalesced/cache hit may reuse an earlier origin response.
            # A cached object ages from the origin response, which itself can have taken
            # time; report observed lag rather than using TTL to infer a capture time.
            upper = sum(w["started_s"] <= finish for w in writes)
            lower = (
                0 if (arm == "one-second" and read["hit"]) else sum(w["finished_s"] < start for w in writes)
            )
            candidates = [
                k for k in range(lower, upper + 1) if states[k][read["view"]] == read["page_signature"]
            ]
            if not candidates:
                page_ok = any(
                    states[k][read["view"]][0] == read["page_signature"][0] for k in range(lower, upper + 1)
                )
                counts_ok = any(
                    states[k][read["view"]][1:] == read["page_signature"][1:] for k in range(lower, upper + 1)
                )
                observations.append(
                    {
                        "sequence": read["sequence"],
                        "site": read["site"],
                        "valid": False,
                        "page_seen": page_ok,
                        "counts_seen": counts_ok,
                        "hit": read["hit"],
                    }
                )
                continue
            # Some writes affect only another filter or an item beyond the page.
            # Use the newest compatible snapshot for the lower bound on visible lag.
            newest = max(candidates)
            stale = [w for w in writes[newest:] if w["finished_s"] < start]
            observations.append(
                {
                    "sequence": read["sequence"],
                    "site": read["site"],
                    "valid": True,
                    "hit": read["hit"],
                    "acknowledged_changes_behind": len(stale),
                    "known_lag_ms": round(1000 * (start - stale[0]["finished_s"]), 2) if stale else 0,
                }
            )
    return sorted(observations, key=lambda r: r["sequence"])
