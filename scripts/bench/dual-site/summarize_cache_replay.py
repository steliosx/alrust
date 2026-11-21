"""Verify and summarize existing two-site cache replays; never starts a benchmark."""

from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import median

from cache_replay_model import SITES, digest, percentiles, plan
import cache_replay_mutations as mutations
import cache_replay_panels as panels
from run_cache_replay import validate_timed
from run_lab import WRITE_IDENTITIES


def disqualified_cells(report):
    """Distinguish confirmed responses from persisted, unconfirmed writes when SQL was captured."""
    evidence = []
    for cell in report.get("cells", []):
        rows = cell["arrivals"]
        failed = [r for r in rows if not r["ok"]]
        item = {
            "arm": cell["arm"],
            "rep": cell["rep"],
            "recorded_arrivals": len(rows),
            "accepted_responses": sum(r["ok"] for r in rows),
            "failed_responses": [
                {
                    "sequence": r["sequence"],
                    "kind": r["kind"],
                    "site": r["site"],
                    "error": r.get("error"),
                    "http_status": r.get("http_status"),
                }
                for r in failed
            ],
        }
        if "ledger_before" in cell and "ledger_after" in cell:
            before = {r[1]: r[5] + 1 for r in cell["ledger_before"]["alerts"]}
            after = {r[1]: r[5] + 1 for r in cell["ledger_after"]["alerts"]}
            item["failed_write_resources"] = {}
            for r in failed:
                if r["kind"] != "write":
                    continue
                name = r.get("resource") or WRITE_IDENTITIES[r["index"]]
                changes = after.get(name, 0) - before.get(name, 0)
                confirmed = sum(w["ok"] and w["kind"] == "write" and w["resource"] == name for w in rows)
                item["failed_write_resources"][name] = {
                    "persisted_increments": changes,
                    "confirmed": confirmed,
                    "persisted_without_confirmation": changes - confirmed,
                }
        evidence.append(item)
    return evidence


def phase_contrasts(groups):
    """Align the two cadences by seed and check that only refresh phase changed."""
    contrasts = []
    for key, aligned in groups.items():
        (
            profile,
            cadence,
            features,
            rate,
            seconds,
            mbit,
            bulk,
            quota,
            clamp,
            newest,
            feedback,
            write_route,
            environments,
        ) = key
        if not profile.endswith("-5s") or cadence != "aligned" or newest:
            continue
        spread = groups.get((profile, "spread", *key[2:]), [])

        def by_seed(runs):
            found = {}
            for _, pairs in runs:
                for pair in pairs:
                    if pair["seed"] in found:
                        raise ValueError("Ambiguous cadence control for one seed")
                    found[pair["seed"]] = pair
            return found

        synchronized, staggered = by_seed(aligned), by_seed(spread)
        matched = []
        for seed in sorted(synchronized.keys() & staggered.keys()):

            def traffic(phase):
                events = plan(profile, rate, seconds, phase, seed)
                return Counter(
                    digest({k: v for k, v in event.items() if k not in ("sequence", "due_s")})
                    for event in events
                )

            if traffic("aligned") != traffic("spread"):
                raise ValueError("Cadence contrast changes panel identities, writes or filters")
            first, second = synchronized[seed], staggered[seed]
            delta = {}
            for g, baseline in first["rows"].items():
                delta[g] = {}
                for arm in ("off", "on"):
                    a, b = baseline[arm + "_p95_ms"], second["rows"][g][arm + "_p95_ms"]
                    delta[g][arm] = {
                        "aligned_p95_ms": a,
                        "spread_p95_ms": b,
                        "diff_percent": round(100 * (b / a - 1), 2),
                    }
            matched.append({"seed": seed, "groups": delta})
        if matched:
            contrasts.append(
                {
                    "profile": profile,
                    "features": features,
                    "rate": rate,
                    "seconds": seconds,
                    "mbit": mbit,
                    "bulk": bulk,
                    "quota": quota,
                    "clamp": clamp,
                    "feedback": feedback,
                    "write_route": write_route,
                    "environment_list": environments,
                    "pairs": matched,
                }
            )
    return contrasts


def hit_timing(arrivals):
    """Classify client-observed overlap with a matching fill; do not infer cache-internal wait time."""
    fills = defaultdict(list)
    for r in arrivals:
        if r["kind"] == "dashboard" and not r["hit"]:
            fills[r["site"], r["key"], r["path"]].append(r)
    cohorts = defaultdict(list)
    for hit in arrivals:
        if hit["kind"] != "dashboard" or not hit["hit"]:
            continue
        ended = hit.get("list_finished_s", hit["finished_s"])
        candidates = [
            r
            for r in fills[hit["site"], hit["key"], hit["path"]]
            if r["started_s"] <= ended and r.get("list_finished_s", r["finished_s"]) >= hit["started_s"] - 1
        ]
        if len(candidates) != 1:
            label = "ambiguous"
        elif candidates[0].get("list_finished_s", candidates[0]["finished_s"]) >= hit["started_s"]:
            label = "overlapped_origin"
        else:
            label = "after_origin_completion"
        cohorts[hit["site"], label].append(1000 * (ended - hit["started_s"]))
    return {
        site: {
            label: {"n": len(times), "client_service_ms": percentiles(times)}
            for (s, label), times in sorted(cohorts.items())
            if s == site
        }
        for site in SITES
    }


def pair_cells(run):
    if run.get("valid") is not True:
        raise ValueError("Run is incomplete")
    planned_cells = {(rep, arm) for rep in range(run["repeats"]) for arm in ("off", "one-second")}
    cells = {}
    for c in run["cells"]:
        key = (c["rep"], c["arm"])
        if key in cells or key not in planned_cells or c.get("valid") is not True:
            raise ValueError("Unexpected, incomplete or duplicate cell")
        if c["primary_sha256"] != c["standby_sha256"] or c["routes_before"] != c["routes_after"]:
            raise ValueError("Replica or routing does not agree")
        rows = c["arrivals"]
        planned = plan(
            run["profile"],
            run["rate"],
            run["seconds"],
            run["cadence"],
            run["seed"] + c["rep"],
            run.get("newest", False),
        )
        if c["plan_sha256"] != digest(planned):
            raise ValueError("Unmatched planned arrival trace")
        if run.get("feedback"):
            planned = panels.effective_plan(planned, rows, run["seconds"])
        if "ledger_before" in c:
            before, after = c["ledger_before"], c["ledger_after"]
            if digest(before) != c["seed_sha256"] or digest(after) != c["primary_sha256"]:
                raise ValueError("Saved SQL ledger disagrees with the captured digest")
            validate_timed(
                before,
                after,
                after,
                rows,
                c["counters_before"],
                c["counters_after"],
                planned,
                run.get("newest", False),
            )
        if len(rows) != len(planned):
            raise ValueError("Trace hash or length differs from its planned arrivals")
        for e, r in zip(planned, rows):
            via = c["arm"] == "one-second" and (
                e["kind"] == "dashboard" or run.get("write_route", "proxy") == "proxy"
            )
            if run.get("environment_list") and e["kind"] == "dashboard":
                env = r["environments"]
                if (
                    env["http_status"] != 200
                    or env["cache_result"] != ("origin" if via else None)
                    or env["started_s"] < r["due_s"] - 0.001
                    or env["finished_s"] < env["started_s"]
                    or r["finished_s"] != max(r["list_finished_s"], env["finished_s"])
                ):
                    raise ValueError("Missing or inconsistent list and environments completion times")
            if (
                not all(r.get(k) == v for k, v in e.items())
                or r.get("ok") is not True
                or r.get("via_cache_proxy", via) != via
                or r["route"] != "a"
                or r["worker"] not in ("vm1", "vm2")
                or r["finished_s"] < r["started_s"]
                or r["started_s"] + 0.001 < r["due_s"]
                or abs(r["latency_ms"] - (r["finished_s"] - r["due_s"]) * 1000) > 0.001
                or (r["hit"] and (e["kind"] != "dashboard" or c["arm"] == "off"))
            ):
                raise ValueError("Raw arrival failed identity, service, route or timing checks")
        for site in SITES:
            group = [r for r in rows if r["site"] == site]
            if c["arm"] == "one-second":
                origin = sum(not r["hit"] and r.get("via_cache_proxy", True) for r in group)
                origin += sum("environments" in r for r in group)
                if (
                    c["counters_after"][site]["backend_req"] - c["counters_before"][site]["backend_req"]
                    != origin
                ):
                    raise ValueError("Site backend delta differs from misses and writes")
            for kind in ("dashboard", "write"):
                g = [r for r in group if r["kind"] == kind]
                rec = c["groups"][f"{site}-{kind}"]
                if (
                    rec["n"] != len(g)
                    or rec["hits"] != sum(r["hit"] for r in g)
                    or rec["arrival_latency_ms"] != percentiles([r["latency_ms"] for r in g])
                ):
                    raise ValueError("Group aggregate does not reproduce from arrivals")
        if run.get("newest"):
            obs = mutations.verify(c["ordered_seed"], c["ordered_final"], rows, c["arm"])
            if obs != c["page_consistency"] or any(not o["valid"] for o in obs):
                raise ValueError("Newest-page snapshot validation failed")
        if run.get("probes_enabled"):
            probes = c.get("dashboard_coherence", c.get("coherence", {}))
            if set(probes) != {"new_alert", "key_revocation"}:
                raise ValueError("Missing mutation probes")
            for part, observations in probes.items():
                if set(observations) != set(SITES):
                    raise ValueError("Mutation probe did not observe both sites")
                for obs in observations.values():
                    if (
                        not obs["samples"]
                        or obs["first_fresh_ms"] < 0
                        or obs["last_old_ms"] > obs["first_fresh_ms"]
                    ):
                        raise ValueError("Invalid freshness interval")
                    samples = obs["samples"]
                    if any(s["start_ms"] > s["end_ms"] for s in samples):
                        raise ValueError("Invalid observation timestamps")
                    if "dashboard_coherence" in c:
                        fresh = [s["fresh"] for s in samples]
                        if not any(fresh) or sorted(fresh) != fresh or c["arm"] == "off" and not all(fresh):
                            raise ValueError("Dashboard freshness went backwards")
                        if "old_page_sha256" in obs:
                            for s in samples:
                                want = obs["fresh_page_sha256"] if s["fresh"] else obs["old_page_sha256"]
                                if s["page_sha256"] != want or (s["http_status"] == 401) != (want is None):
                                    raise ValueError("Probe status and page fingerprint disagree")
                            if obs["old_page_sha256"] == obs["fresh_page_sha256"]:
                                raise ValueError("The warm and fresh dashboard fingerprints must differ")
                    else:
                        wanted = samples[-1]["answer"]
                        if wanted[0] != (200 if part == "new_alert" else 401):
                            raise ValueError("Count probe did not finish at the expected HTTP status")
                        fresh = [s["answer"] == wanted for s in samples]
                        if fresh != sorted(fresh) or (c["arm"] == "off" and not all(fresh)):
                            raise ValueError("Count freshness went backwards")
                    first = next(s["end_ms"] for s, correct in zip(samples, fresh) if correct)
                    last = max((s["end_ms"] for s, correct in zip(samples, fresh) if not correct), default=0)
                    if (first, last) != (obs["first_fresh_ms"], obs["last_old_ms"]):
                        raise ValueError(
                            "Recorded freshness intervals do not reproduce from timed observations"
                        )
        cells[key] = c
    if cells.keys() != planned_cells:
        raise ValueError("Missing paired cells")
    pairs = []
    for rep in range(run["repeats"]):
        a, b = cells[rep, "off"], cells[rep, "one-second"]
        if a["seed_sha256"] != b["seed_sha256"] or a["plan_sha256"] != b["plan_sha256"]:
            raise ValueError("Unmatched starting fixtures or schedules")
        sample = {"rep": rep, "seed": run["seed"] + rep, "plan": a["plan_sha256"], "rows": {}}
        for group in a["groups"]:
            x, y = a["groups"][group], b["groups"][group]
            bx, by = x["arrival_latency_ms"]["p95"], y["arrival_latency_ms"]["p95"]
            sample["rows"][group] = {
                "off_p95_ms": bx,
                "on_p95_ms": by,
                "diff_ms": round(by - bx, 2),
                "diff_percent": round(100 * (by / bx - 1), 2),
                "n_off": x["n"],
                "n_on": y["n"],
                "hits": y["hits"],
                "max_start_delay_p95_ms": max(x["start_delay_ms"]["p95"], y["start_delay_ms"]["p95"]),
            }
            site, kind = group.split("-")
            for label, cell in (("off", a), ("on", b)):
                selected = [r for r in cell["arrivals"] if (r["site"], r["kind"]) == (site, kind)]
                metrics = {}
                for response_path, subset in (
                    ("origin", [r for r in selected if not r["hit"]]),
                    ("hit", [r for r in selected if r["hit"]]),
                ):
                    if subset:
                        metrics[response_path] = {
                            "n": len(subset),
                            "arrival_ms": percentiles([r["latency_ms"] for r in subset]),
                            "service_ms": percentiles(
                                [1000 * (r["finished_s"] - r["started_s"]) for r in subset]
                            ),
                        }
                sample["rows"][group][label + "_response_path"] = metrics
        sample["backend_requests_avoided"] = sum(r["hit"] for r in b["arrivals"])
        if not run.get("environment_list"):
            sample["hit_timing"] = hit_timing(b["arrivals"])
        sample["total_off"] = len(a["arrivals"])
        sample["total_on"] = len(b["arrivals"])
        sample["origin_requests_off"] = len(a["arrivals"]) + sum("environments" in r for r in a["arrivals"])
        sample["origin_requests_on"] = (
            len(b["arrivals"])
            + sum("environments" in r for r in b["arrivals"])
            - sample["backend_requests_avoided"]
        )
        if run.get("newest"):
            sample["newest"] = {}
            for label, cell in (("off", a), ("one-second", b)):
                obs = cell["page_consistency"]
                sample["newest"][label] = {
                    "pages": len(obs),
                    "pages_behind_acknowledged_write": sum(r["acknowledged_changes_behind"] > 0 for r in obs),
                    "maximum_observed_lag_ms": max(r["known_lag_ms"] for r in obs),
                }
        if run.get("probes_enabled"):
            sample["probe_route"] = "dashboard" if "dashboard_coherence" in a else "count"
            sample["freshness"] = {
                arm: {
                    k: {
                        site: {"old_until_ms": r["last_old_ms"], "fresh_by_ms": r["first_fresh_ms"]}
                        for site, r in obs.items()
                    }
                    for k, obs in c.get("dashboard_coherence", c.get("coherence", {})).items()
                }
                for arm, c in (("off", a), ("one-second", b))
            }
        pairs.append(sample)
    return pairs


def summarize(root):
    groups = defaultdict(list)
    evidence = []
    for f in sorted(root.glob("*-control.json")):
        control = json.loads(f.read_text())
        name = control["args"]["label"]
        src = root / name / "cache-replay.json"
        item = {"name": name, "valid": False, "closed": control.get("runtime_empty") is True}
        evidence.append(item)
        thermal = root / (name + "-thermal.json")
        if thermal.exists():
            detail = json.loads(thermal.read_bytes())
            item["external_thermal_guard"] = {key: detail[key] for key in ("max_core_c", "trip", "exit")}
        cleanup = root / name / "cleanup-confirmation.json"
        if cleanup.exists():
            item["subsequent_cleanup"] = json.loads(cleanup.read_bytes())
        if not src.is_file():
            item["reason"] = "No result"
            continue
        b = src.read_bytes()
        rec = json.loads(b)
        item["report_sha256"] = sha256(b).hexdigest()
        if control.get("exit_code") != 0 or not item["closed"] or not rec.get("valid"):
            item["reason"] = rec.get("error", "Scope did not finish successfully")
            log = root / (name + ".log")
            if log.is_file():
                heat = re.findall(r"run_lab\.ThermalLimit: ([^\n]+)", log.read_text(errors="replace"))
                if heat:
                    item["recorded_thermal_stop"] = heat[-1]
            item["observations"] = disqualified_cells(rec)
            continue
        pairs = pair_cells(rec)
        cfg = control["args"]
        key = (
            rec["profile"],
            rec["cadence"],
            rec["features"],
            rec["rate"],
            rec["seconds"],
            cfg["mbit"],
            cfg["bulk"],
            cfg["quota"],
            cfg["clamp"],
            rec.get("newest", False),
            rec.get("feedback", False),
            rec.get("write_route", "proxy"),
            rec.get("environment_list", False),
        )
        groups[key].append((name, pairs))
        item.update(valid=True, pairs=pairs)
    summaries = []
    for key, runs in groups.items():
        pairs = [p for _, ps in runs for p in ps]
        stats = {}
        for g in pairs[0]["rows"]:
            rows = [p["rows"][g] for p in pairs]
            stats[g] = {
                "off_p95_median_ms": median(r["off_p95_ms"] for r in rows),
                "on_p95_median_ms": median(r["on_p95_ms"] for r in rows),
                "paired_diff_pct": median(r["diff_percent"] for r in rows),
                "paired_range_pct": [
                    min(r["diff_percent"] for r in rows),
                    max(r["diff_percent"] for r in rows),
                ],
                "hits": sum(r["hits"] for r in rows),
                "requests_off": sum(r["n_off"] for r in rows),
                "requests_on": sum(r["n_on"] for r in rows),
                "max_start_delay_p95_ms": max(r["max_start_delay_p95_ms"] for r in rows),
            }
        summaries.append(
            dict(
                zip(
                    (
                        "profile",
                        "cadence",
                        "features",
                        "rate",
                        "seconds",
                        "mbit",
                        "bulk",
                        "quota",
                        "clamp",
                        "newest",
                        "feedback",
                        "write_route",
                        "environment_list",
                    ),
                    key,
                ),
                runs=[name for name, _ in runs],
                pairs=len(pairs),
                groups=stats,
            )
        )
    return {"evidence": evidence, "groups": summaries, "cadence_contrasts": phase_contrasts(groups)}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    collected = summarize(args.root)
    args.output.write_text(json.dumps(collected, indent=2) + "\n")
    print(
        f"{sum(e['valid'] for e in collected['evidence'])} verified runs, {len(collected['groups'])} configurations"
    )
