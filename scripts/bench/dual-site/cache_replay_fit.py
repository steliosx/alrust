"""Estimate one-second cache reuse from a locally anonymized, chronological GET trace."""

import argparse
from collections import Counter, defaultdict, deque
import json
import math
from pathlib import Path
from random import Random
from statistics import median


FIELDS = {"time_ms", "site", "identity", "representation", "eligible"}


def estimate(records, ttl_ms=1000, fill_ms=0):
    """An empty cache per site. A successful fill is shareable after fill_ms."""
    if not all(math.isfinite(n) and n >= 0 for n in (ttl_ms, fill_ms)) or ttl_ms <= 0:
        raise ValueError("Use a positive TTL and a nonnegative fill time")
    totals = defaultdict(Counter)
    objects = {}
    expiry_order = deque()
    previous = -1
    for n, row in enumerate(records, 1):
        if (
            not isinstance(row, dict)
            or row.keys() != FIELDS
            or any(type(row[k]) is not int or row[k] < 0 for k in ("site", "identity", "representation"))
            or type(row["eligible"]) is not bool
            or type(row["time_ms"]) not in (int, float)
            or not math.isfinite(row["time_ms"])
            or row["time_ms"] < previous
            or row["time_ms"] < 0
        ):
            raise ValueError(
                f"Invalid or unsorted row {n}; use only the documented five fields and numeric labels"
            )
        now = previous = row["time_ms"]
        # All fills have the same lifetime and arrive in chronological order.
        # Forget expired identities even when a long trace contains only unique URLs.
        while expiry_order and expiry_order[0][0] <= now:
            _, expired_key = expiry_order.popleft()
            del objects[expired_key]
        site = row["site"]
        count = totals[site]
        count["reads"] += 1
        if not row["eligible"]:
            count["bypass"] += 1
            continue
        key = (site, row["identity"], row["representation"])
        ready, expires = objects.get(key, (-1, -1))
        if now < ready:
            count["coalesced"] += 1
        elif now < expires:
            count["warm"] += 1
        else:
            count["fills"] += 1
            ready = now + fill_ms
            expires = ready + ttl_ms
            objects[key] = (ready, expires)
            expiry_order.append((expires, key))

    def summary(counter):
        output = {k: counter[k] for k in ("reads", "bypass", "coalesced", "warm", "fills")}
        output["modeled_origin_reads"] = output["bypass"] + output["fills"]
        output["modeled_origin_reads_avoided"] = output["warm"] + output["coalesced"]
        return output

    return {
        "ttl_ms": ttl_ms,
        "assumed_fill_ms": fill_ms,
        "total": summary(sum(totals.values(), Counter())),
        "sites": {str(site): summary(c) for site, c in sorted(totals.items())},
    }


def example_grid():
    """Sixteen fixed-phase panels at two sites, with an empty cache at the start."""
    results = []
    for shared_by in (1, 2, 4, 8):
        for cadence in ("aligned", "even", "independent"):
            fractions = []
            for seed in range(48 if cadence == "independent" else 1):
                rng = Random(seed)
                events = []
                for site in (0, 1):
                    for panel in range(8):
                        phase = {
                            "aligned": 0,
                            "even": panel % shared_by * 5000 / shared_by,
                            "independent": rng.random() * 5000,
                        }[cadence]
                        for period in range(12):
                            events.append(
                                {
                                    "time_ms": 5000 * period + phase,
                                    "site": site,
                                    "identity": panel // shared_by,
                                    "representation": 0,
                                    "eligible": True,
                                }
                            )
                counts = estimate(sorted(events, key=lambda e: e["time_ms"]))["total"]
                fractions.append(100 * counts["modeled_origin_reads_avoided"] / counts["reads"])
            results.append(
                {
                    "readers_per_identity_per_site": shared_by,
                    "phases": cadence,
                    "seeds": len(fractions),
                    "median_avoided_percent": round(median(fractions), 2),
                    "range_percent": [round(min(fractions), 2), round(max(fractions), 2)],
                }
            )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", nargs="?", type=Path, help="Anonymized five-field JSON Lines file")
    parser.add_argument("--example-grid", action="store_true", help="Model fixed-phase five-second panels")
    parser.add_argument("--ttl-ms", type=float, default=1000)
    parser.add_argument("--fill-ms", type=float, default=0)
    args = parser.parse_args()
    if args.example_grid:
        if args.trace or args.ttl_ms != 1000 or args.fill_ms:
            parser.error("The example grid uses only a one-second TTL and instant fills")
        result = example_grid()
    elif args.trace:
        with args.trace.open() as stream:
            result = estimate(
                (json.loads(line) for line in stream if line.strip()), args.ttl_ms, args.fill_ms
            )
    else:
        parser.error("Supply a trace file or --example-grid")
    print(json.dumps(result, indent=2))
