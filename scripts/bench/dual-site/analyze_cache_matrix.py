"""Verify and summarize an uploaded two-site hosted benchmark artifact offline."""

import argparse
from collections import Counter, defaultdict
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
import subprocess

from cache_replay_evidence import verify
from cache_replay_model import percentiles, digest
from run_cache_replay import validate_duplicates
from hosted_matrix import schedule
from summarize_cache_replay import pair_cells


def source_check(repo, commit, manifest):
    base = Path("scripts/bench")
    required = {
        "dual_site_quorum.py",
        *(
            f"dual-site/{name}"
            for name in (
                "run_cache_replay.py",
                "cache_replay_model.py",
                "run_lab.py",
                "cache_replay.vcl",
                "cache_replay_panels.py",
                "cache_replay_journey.py",
                "cache_replay_mutations.py",
                "dashboard_routes.py",
                "hosted_matrix.py",
                "hosted_lab.py",
                "hosted-netem.sh",
                "hosted-setup.sh",
                "haproxy.cfg",
                "haproxy-affinity.cfg",
                "fixture/alerta_fast.py",
                "patroni-extra.lock",
            )
        ),
    }
    if not required <= manifest.keys():
        raise ValueError("Benchmark source manifest omits required components")
    for rel, expected in manifest.items():
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError("Invalid source path")
        content = subprocess.check_output(["git", "-C", str(repo), "show", f"{commit}:{base / rel}"])
        if sha256(content).hexdigest() != expected:
            raise ValueError(f"Source hash mismatch for {rel}")


def middle(values):
    return round(median(values), 2)


def observed(rows, field):
    values = [r[field] for r in rows]
    return {"median": middle(values), "min": min(values), "max": max(values)}


def extras(cell):
    groups = {}
    for site in ("a", "b"):
        reads = [r for r in cell["arrivals"] if r["site"] == site and r["kind"] == "dashboard"]
        if not reads:
            raise ValueError("Qualified pair missing dashboard observations")
        envs = [r for r in reads if "environments" in r]
        groups[site] = {
            "dashboards": len(reads),
            "wire_bytes": sum(r["bytes"] for r in reads),
            "companion_wire_bytes": sum(r["environments"]["wire_bytes"] for r in envs),
            "companion_slower": sum(r["environments"]["finished_s"] > r["list_finished_s"] for r in envs),
            "companions": len(envs),
            "list_only_ms": percentiles(
                [1000 * (r.get("list_finished_s", r["finished_s"]) - r["due_s"]) for r in reads]
            ),
        }
    usage = cell["scope_cpu_delta"]["usage_usec"] / 1000
    memory = cell["scope_memory"]
    caps = cell["scope_limits"]
    quota, period = map(int, caps["cpu.max"].split())
    if quota != 2 * period or caps["memory.swap.max"] != "0" or int(caps["memory.max"]) != 4 * 1024**3:
        raise ValueError("Unexpected applied resource limits")
    if memory["oom_kill_count"] or memory["peak_bytes"] > int(caps["memory.max"]):
        raise ValueError("Cgroup memory evidence outside scope")
    return {
        "site": groups,
        "cpu_ms": round(usage, 2),
        "cpu_ms_per_completed_arrival": round(usage / len(cell["arrivals"]), 3),
        "scope_peak_bytes": memory["peak_bytes"],
    }


def panel_cycles(cell):
    """Time until the complete wall has refreshed, with the scheduling delay included."""
    cycles = defaultdict(list)
    for row in cell["arrivals"]:
        if row["kind"] == "dashboard":
            cycles[(row["site"], int(row["due_s"] // 5))].append(row)
    samples = defaultdict(list)
    for (site, period), rows in sorted(cycles.items()):
        if len({row["panel"] for row in rows}) != 8 or len(rows) != 8:
            raise ValueError("Fixed wallboard cycle must contain eight distinct panels per data site")
        samples[site].append(round(1000 * (max(row["finished_s"] for row in rows) - 5 * period), 2))
    return {
        site: {"n": len(values), "completion_ms": percentiles(values), "cycles_ms": values}
        for site, values in samples.items()
    }


def aggregate(pairs):
    groups = {}
    for key in ("a-dashboard", "b-dashboard", "a-write", "b-write"):
        rows = [p["rows"][key] for p in pairs]
        groups[key] = {
            "off_p95_ms": observed(rows, "off_p95_ms"),
            "on_p95_ms": observed(rows, "on_p95_ms"),
            "paired_pct": observed(rows, "diff_percent"),
            "faster_pairs": sum(r["diff_ms"] < 0 for r in rows),
            "hits": sum(r["hits"] for r in rows),
            "n_off": sum(r["n_off"] for r in rows),
            "n_on": sum(r["n_on"] for r in rows),
        }
    freshness = {}
    for site in ("a", "b"):
        freshness[site] = {}
        for part in ("new_alert", "key_revocation"):
            b = [p["freshness"]["one-second"][part][site] for p in pairs]
            freshness[site][part] = {
                "first_correct_ms": observed(b, "fresh_by_ms"),
                "last_old_ms": observed(b, "old_until_ms"),
            }
    return {
        "pairs": len(pairs),
        "groups": groups,
        "freshness": freshness,
        "origin_off": sum(p["origin_requests_off"] for p in pairs),
        "origin_on": sum(p["origin_requests_on"] for p in pairs),
        "completed_on": sum(p["total_on"] for p in pairs),
        "completed_off": sum(p["total_off"] for p in pairs),
        "hits": sum(p["backend_requests_avoided"] for p in pairs),
    }


def analyze(root, repo):
    meta = json.loads((root / "matrix.json").read_bytes())
    raw = gzip.decompress((root / "samples.json.gz").read_bytes())
    checked = verify(raw)
    if checked != meta["verified"]:
        raise ValueError("Published job counts disagree with offline verification")
    if repo:
        source_check(repo, meta["source"], meta["manifest"])
    plan = {f"{c[0]}-{seed}": (seed, c) for seed, c in schedule()}
    rows = meta["cases"]
    if len(rows) != len({r["name"] for r in rows}) or list(plan)[: len(rows)] != [r["name"] for r in rows]:
        raise ValueError("Case order differs from the planned experiment")
    entries = {r["name"]: r for r in json.loads(raw)["runs"]}
    if set(entries) != {r["name"] for r in rows} or meta["planned"] != len(plan):
        raise ValueError("Archive inventory differs from the job and plan")
    pairs = {}
    exclusions = []
    write_outcomes = defaultdict(Counter)
    for status in rows:
        name = status["name"]
        seed, case = plan[name]
        e = entries[name]
        c, run = e["control"], e["result"]
        if (
            c["manifest"] != meta["manifest"]
            or c["runtime_empty"] != status["clean"]
            or c["exit_code"] != status["exit"]
            or c["args"]["seed"] != seed
            or c["addon_sha256"] != meta["manifest"]["dual-site/fixture/alerta_fast.py"]
        ):
            raise ValueError(f"{name} controls do not match the matrix")
        valid = bool(run and run["valid"] and c["runtime_empty"] and not c["exit_code"])
        if status["valid"] != valid:
            raise ValueError(f"{name} validity differs from job status")
        for cell in (run or {}).get("cells", []):
            for row in cell["arrivals"]:
                if row["kind"] == "write":
                    route = "via_varnish" if row["via_cache_proxy"] else "direct_haproxy"
                    write_outcomes[route]["acknowledged" if row["ok"] else "unsuccessful"] += 1
        if not valid:
            ledger_checks = {}
            failed = Counter()
            for cell in (run or {}).get("cells", []):
                for a in cell["arrivals"]:
                    if not a["ok"]:
                        failed[
                            (a["site"], a["kind"], str(a.get("http_status")), a.get("error", "unspecified"))
                        ] += 1
                if (
                    not cell.get("valid")
                    and not run.get("newest")
                    and "ledger_after" in cell
                    and "ledger_before" in cell
                ):
                    accepted = [r for r in cell["arrivals"] if r["ok"]]
                    before, after = cell["ledger_before"], cell["ledger_after"]
                    if not (
                        digest(before) == cell["seed_sha256"]
                        and digest(after) == cell["primary_sha256"] == cell["standby_sha256"]
                    ):
                        raise ValueError("Failed trial ledger hash mismatch")
                    try:
                        validate_duplicates(before, after, accepted)
                    except ValueError:
                        matches = False
                    else:
                        matches = True
                    ledger_checks[cell["arm"]] = {
                        "acknowledged_duplicates_match_durable_rows": matches,
                        "successful_write_receipts": sum(r["kind"] == "write" for r in accepted),
                        "received_timer_delta": after["timers"].get("received", 0)
                        - before["timers"].get("received", 0),
                        "fetch_failed_delta": {
                            site: end["fetch_failed"] - cell["counters_before"][site]["fetch_failed"]
                            for site, end in cell["counters_after"].items()
                        },
                    }
            exclusions.append(
                {
                    "name": name,
                    "exit": c["exit_code"],
                    "clean": c["runtime_empty"],
                    "reason": (run or {}).get("error", "no completed measurement"),
                    "ledger_checks": ledger_checks,
                    "failed_arrivals": [[*k, v] for k, v in failed.items()],
                }
            )
            continue
        if (
            run["profile"],
            run["cadence"],
            run["feedback"],
            run["environment_list"],
            run["newest"],
            run["write_route"],
        ) != case[1:-1]:
            raise ValueError("Workload does not match its label")
        if (run["seconds"], run["rate"], run["seed"], run["repeats"]) != (50, 4, seed, 1):
            raise ValueError("Trace dimensions differ from the planned run")
        (pair,) = pair_cells(run)
        pair["resources"] = {cell["arm"]: extras(cell) for cell in run["cells"]}
        if run["profile"].endswith("-5s") and not run["feedback"]:
            pair["fixed_wall_cycles"] = {cell["arm"]: panel_cycles(cell) for cell in run["cells"]}
        pair["thermal_telemetry"] = run["thermal_telemetry"]
        pair["netem_fixed_seed"] = run["netem_fixed_seed"]
        pair["calibration"] = e["topology"]["link_calibration"]
        pairs[name] = pair
    grouped = defaultdict(list)
    for name, p in pairs.items():
        grouped[plan[name][1][0]].append(p)
    # Compare four refresh schedules for identical fixed-arrival users and writes,
    # at the same seed. This distinguishes phase effects from adding the cache.
    contrasts = {}
    for other in ("spread", "pairs-0", "pairs-500", "pairs-750"):
        changes = []
        for seed in (12, 13, 14, 15):
            a = pairs.get(f"aligned-{seed}")
            b = pairs.get(f"{other}-{seed}")
            if not a or not b:
                continue
            changes.append(
                {
                    "seed": seed,
                    "off_to_off_ms": round(
                        b["rows"]["b-dashboard"]["off_p95_ms"] - a["rows"]["b-dashboard"]["off_p95_ms"], 2
                    ),
                    "off_to_on_ms": round(
                        b["rows"]["b-dashboard"]["on_p95_ms"] - a["rows"]["b-dashboard"]["off_p95_ms"], 2
                    ),
                }
            )
        if changes:
            contrasts[other] = changes
    return {
        "run_id": meta["run_id"],
        "source": meta["source"],
        "versions": meta["versions"],
        "verified": checked,
        "planned": len(plan),
        "finished": meta.get("finished"),
        "end_reason": meta.get("end_reason"),
        "not_started": [n for n in plan if n not in entries],
        "exclusions": exclusions,
        "all_recorded_write_outcomes": dict(write_outcomes),
        "cases": {k: aggregate(v) for k, v in grouped.items()},
        "phase_contrasts": contrasts,
        "pairs": pairs,
    }


if __name__ == "__main__":
    arg = argparse.ArgumentParser(description=__doc__)
    arg.add_argument("artifact", type=Path)
    arg.add_argument("output", type=Path)
    arg.add_argument("--repo", type=Path, help="also verify source hashes in the exact Git commit")
    args = arg.parse_args()
    result = analyze(args.artifact, args.repo)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["verified"], sort_keys=True))
