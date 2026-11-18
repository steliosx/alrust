#!/usr/bin/env python3
"""Closed-loop writes and mixed read/write load for the final configurations."""
import asyncio
import csv
import json
import math
import time

from common import (
    API_KEY,
    HOST,
    RAW,
    REPS,
    ROOT,
    WRITE_LEVELS,
    Sampler,
    aggregates,
    alert_count,
    config_db,
    configs,
    db_exists,
    ensure_server,
    file_done,
    http_json,
    log,
    median3,
    median_any,
    recreate_db,
    redact,
    remove_apps,
    rotate,
    stop_server,
    write_json,
)
from reads import final_configs
from write_client import payload, run_gets, run_posts, run_posts_for_seconds, worker_ranges

WRITE_RAW = RAW / "test2"
MIX_RAW = RAW / "test2-mixed"
PILOT_RAW = WRITE_RAW / "pilots"
READ_PATH = "/alerts?sort-by=lastReceiveTime&page-size=50"
CONDITIONS = ("empty", "seeded")

WRITE_FIELDS = [
    "condition", "config", "concurrency", "rep", "order_pos", "first_config",
    "n_warm", "n_timed", "offered", "accepted", "rps", "p50_ms", "p99_ms",
    "elapsed_s", "error_count", "warmup_offered", "warmup_accepted",
    "alert_rows", "duplicate_count_sum", "history_entries",
    "peak_total_backends", "peak_active_backends", "peak_client_backends",
    "db_sample_count", "db_sample_interval_actual_s",
    "offered_complete", "status_codes", "truncated",
]


def half_up(value):
    return int(math.floor(value + 0.5))


def round_up(n, concurrency):
    n = max(int(n), concurrency)
    return ((n + concurrency - 1) // concurrency) * concurrency


def write_groups():
    groups = []
    for condition in CONDITIONS:
        for level in WRITE_LEVELS:
            for rep in REPS:
                groups.append((condition, level, rep))
    return groups


def order_for(condition, level, rep, config_ids):
    index = write_groups().index((condition, level, rep))
    return rotate(config_ids, index)


def template_for(condition):
    return "base_empty" if condition == "empty" else "base_seed"


def expected_for(condition):
    return 0 if condition == "empty" else 1000


def size_path(condition, level):
    return WRITE_RAW / f"size_{condition}_c{level}.json"


def cell_path(condition, config_id, level, rep):
    return WRITE_RAW / f"{condition}_{config_id}_c{level}_r{rep}.json"


def mix_path(config_id, rep):
    return MIX_RAW / f"{config_id}_r{rep}.json"


def scrub(obj):
    return json.loads(redact(json.dumps(obj, default=str)))


def write_order_self_check():
    sample = ["alrust", "alerta-uwsgi-5", "alerta-gunicorn-4"]
    first = {name: 0 for name in sample}
    for condition, level, rep in write_groups():
        first[order_for(condition, level, rep, sample)[0]] += 1
    if set(first.values()) != {8}:
        raise RuntimeError(f"write order not balanced: {first}")


def reset_work(config_id, template, expected):
    remove_apps()
    db = config_db("work", config_id)
    recreate_db(db, template)
    ensure_server(config_id, db, expected)
    return db


def sample_fields(step):
    response = step["response"]
    return {
        "http": response.get("http"),
        "top_status": response.get("top_status"),
        "resource": response.get("resource"),
        "event": response.get("event"),
        "severity": response.get("severity"),
        "alert_status": response.get("alert_status"),
        "duplicateCount": response.get("duplicateCount"),
        "value": response.get("value"),
    }


def post_sample(port, body):
    status, doc, _server = http_json(port, "/alert", method="POST", body=body, timeout=30)
    alert = doc.get("alert") or {}
    return {
        "http": status,
        "top_status": doc.get("status"),
        "top_keys": sorted(doc.keys()),
        "resource": alert.get("resource"),
        "event": alert.get("event"),
        "severity": alert.get("severity"),
        "alert_status": alert.get("status"),
        "duplicateCount": alert.get("duplicateCount"),
        "value": alert.get("value"),
        "repeat": alert.get("repeat"),
        "message": doc.get("message"),
    }


def api_samples_path():
    return WRITE_RAW / "api-samples.json"


def load_samples():
    path = api_samples_path()
    if not file_done(path):
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def api_compatible(samples, config_ids):
    if not samples:
        return False, "api samples missing"
    missing = [config_id for config_id in config_ids if config_id not in samples]
    if missing:
        return False, "api samples missing " + ",".join(missing)
    base = config_ids[0]
    base_steps = samples[base]["steps"]
    for config_id in config_ids[1:]:
        steps = samples[config_id]["steps"]
        if len(steps) != len(base_steps):
            return False, f"{config_id} sample step count differs"
        for index, (left, right) in enumerate(zip(base_steps, steps)):
            if sample_fields(left) != sample_fields(right):
                return False, f"{base} vs {config_id} step {index} differs"
        left_agg = samples[base]["agg"]
        right_agg = samples[config_id]["agg"]
        if left_agg["alert_rows"] != right_agg["alert_rows"] or left_agg["duplicate_count_sum"] != right_agg["duplicate_count_sum"]:
            return False, f"{base} vs {config_id} sample aggregates differ"
    return True, "create, duplicate, and correlate HTTP status and counted fields matched"


def run_api_samples(config_ids):
    path = api_samples_path()
    if file_done(path):
        log("api samples already recorded")
        return load_samples()
    sequence = [0, 4, 4, 7, 17]
    samples = {
        "sequence": sequence,
        "steps_note": "worker 0 indexes 0 (new), 4 (first dup), 4 (duplicate), 7 (first correlate), 17 (correlate severity/value change)",
        "configs": list(config_ids),
    }
    for config_id in config_ids:
        db = reset_work(config_id, "base_empty", 0)
        steps = []
        port = configs()[config_id]["port"]
        for n in sequence:
            observed = post_sample(port, payload(0, n))
            steps.append({"n": n, "response": observed})
            log(
                f"sample {config_id} n={n} http={observed['http']} "
                f"dup={observed['duplicateCount']} sev={observed['severity']} value={observed['value']}"
            )
        samples[config_id] = {"steps": steps, "agg": aggregates(db)}
    remove_apps()
    write_json(path, samples)
    ok, reason = api_compatible(samples, config_ids)
    log(f"api samples compatible={ok} {reason}")
    return samples


def recover_size(condition, level):
    path = size_path(condition, level)
    if file_done(path):
        return json.loads(path.read_text(encoding="utf-8"))
    for rep in REPS:
        for config_id in final_configs():
            cell = cell_path(condition, config_id, level, rep)
            if file_done(cell):
                doc = json.loads(cell.read_text(encoding="utf-8"))
                return {
                    "condition": condition,
                    "concurrency": level,
                    "n_warm": doc["n_warm"],
                    "n_timed": doc["n_timed"],
                    "recovered_from_cell": cell.name,
                }
    return None


async def pilot(config_id, level, seconds):
    port = configs()[config_id]["port"]
    summary = await run_posts_for_seconds(HOST, port, API_KEY, level, seconds)
    summary = scrub(summary)
    log(
        f"pilot {config_id} c={level} accepted={summary['accepted']} "
        f"elapsed={summary['elapsed_s']:.3f} rps={summary['rps']} status={summary['status_codes']}"
    )
    return summary


async def ensure_size(condition, level, config_ids):
    existing = recover_size(condition, level)
    if existing and existing.get("n_warm") and existing.get("n_timed"):
        log(f"size {condition} c={level} n_warm={existing['n_warm']} n_timed={existing['n_timed']}")
        return existing
    template = template_for(condition)
    expected = expected_for(condition)
    order = order_for(condition, level, 1, config_ids)
    probes = {}
    for config_id in order:
        probe_path = PILOT_RAW / f"{condition}_{config_id}_c{level}_probe.json"
        if file_done(probe_path):
            probes[config_id] = json.loads(probe_path.read_text(encoding="utf-8"))
            log(f"probe cached {probe_path.name} rps={probes[config_id].get('rps')}")
            continue
        log(f"probe {condition} c={level} {config_id}")
        reset_work(config_id, template, expected)
        probes[config_id] = await pilot(config_id, level, 5)
        write_json(probe_path, probes[config_id])
    ranked = []
    for config_id, summary in probes.items():
        rps = summary.get("rps")
        if rps is None or summary.get("accepted", 0) <= 0:
            write_json(WRITE_RAW / f"size_{condition}_c{level}.fail.json", {
                "error": f"{config_id} probe accepted no 201 responses",
                "probes": probes,
            })
            raise RuntimeError(f"{condition} c={level} probe failed on {config_id}")
        ranked.append((rps, config_id))
    ranked.sort()
    slowest_rps, slowest = ranked[0]
    tied = [config_id for rps, config_id in ranked if rps == slowest_rps]
    if len(tied) > 1:
        slowest = sorted(tied)[0]
    log(f"slowest {condition} c={level} is {slowest} probe_rps={slowest_rps}")
    sizing_path = PILOT_RAW / f"{condition}_{slowest}_c{level}_sizing.json"
    if file_done(sizing_path):
        sizing = json.loads(sizing_path.read_text(encoding="utf-8"))
    else:
        reset_work(slowest, template, expected)
        sizing = await pilot(slowest, level, 5)
        write_json(sizing_path, sizing)
    if not sizing.get("rps") or sizing.get("accepted", 0) <= 0:
        raise RuntimeError(f"{condition} c={level} sizing pilot failed")
    n_warm = round_up(half_up(sizing["rps"] * 5), level)
    n_timed = round_up(half_up(sizing["rps"] * 30), level)
    meta = {
        "condition": condition,
        "concurrency": level,
        "probe_order": order,
        "probe_rps": {config_id: probes[config_id].get("rps") for config_id in probes},
        "probe_accepted": {config_id: probes[config_id].get("accepted") for config_id in probes},
        "probe_elapsed_s": {config_id: probes[config_id].get("elapsed_s") for config_id in probes},
        "sizing_config": slowest,
        "sizing_rule_who": (
            "Every final configuration is probed for about 5 closed-loop seconds. "
            "The shared sequence is sized from a fresh 5 second pilot on the slowest. "
            "An exact rps tie picks the lexicographically first configuration id."
        ),
        "sizing_rps": sizing.get("rps"),
        "sizing_accepted": sizing.get("accepted"),
        "sizing_elapsed_s": sizing.get("elapsed_s"),
        "n_warm": n_warm,
        "n_timed": n_timed,
        "count_rule": (
            "n_warm = round_up(half_up(sizing_rps * 5), concurrency); "
            "n_timed = round_up(half_up(sizing_rps * 30), concurrency); "
            "half_up is floor(x+0.5); round_up is the next multiple of concurrency."
        ),
    }
    write_json(size_path(condition, level), meta)
    log(f"sized {condition} c={level} n_warm={n_warm} n_timed={n_timed} from {slowest}")
    return meta


def aggs_of(docs):
    rows = [doc["agg"]["alert_rows"] for doc in docs]
    dups = [doc["agg"]["duplicate_count_sum"] for doc in docs]
    hists = [doc["agg"]["history_entries"] for doc in docs]
    return len(set(rows)) == 1 and len(set(dups)) == 1 and len(set(hists)) == 1


def pair_docs(condition, level, rep, config_ids):
    docs = []
    for config_id in config_ids:
        path = cell_path(condition, config_id, level, rep)
        if not file_done(path):
            return None
        docs.append(json.loads(path.read_text(encoding="utf-8")))
    return docs


def row_from_write(doc, offered_complete):
    timed = doc["timed"]
    warm = doc["warmup"]
    stats = doc.get("db_connections") or {}
    return {
        "condition": doc["condition"],
        "config": doc["config"],
        "concurrency": doc["concurrency"],
        "rep": doc["rep"],
        "order_pos": doc["order_pos"],
        "first_config": doc["first_config"],
        "n_warm": doc["n_warm"],
        "n_timed": doc["n_timed"],
        "offered": timed["offered"],
        "accepted": timed["accepted"],
        "rps": timed["rps"],
        "p50_ms": timed["p50_ms"],
        "p99_ms": timed["p99_ms"],
        "elapsed_s": timed["elapsed_s"],
        "error_count": timed["error_count"],
        "warmup_offered": warm["offered"],
        "warmup_accepted": warm["accepted"],
        "alert_rows": doc["agg"]["alert_rows"],
        "duplicate_count_sum": doc["agg"]["duplicate_count_sum"],
        "history_entries": doc["agg"]["history_entries"],
        "peak_total_backends": stats.get("peak_total_backends"),
        "peak_active_backends": stats.get("peak_active_backends"),
        "peak_client_backends": stats.get("peak_client_backends"),
        "db_sample_count": stats.get("sample_count"),
        "db_sample_interval_actual_s": stats.get("sample_interval_actual_s"),
        "offered_complete": offered_complete,
        "status_codes": timed.get("status_codes") or {},
        "truncated": bool(timed.get("truncated") or warm.get("truncated")),
        "raw_file": str(cell_path(doc["condition"], doc["config"], doc["concurrency"], doc["rep"]).relative_to(ROOT)),
    }


def load_write_rows(config_ids):
    rows = []
    for condition, level, rep in write_groups():
        for config_id in order_for(condition, level, rep, config_ids):
            path = cell_path(condition, config_id, level, rep)
            if not file_done(path):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            timed = doc["timed"]
            warm = doc["warmup"]
            offered_complete = (
                timed["offered"] == doc["n_timed"]
                and warm["offered"] == doc["n_warm"]
                and not timed.get("truncated")
                and not warm.get("truncated")
            )
            rows.append(row_from_write(doc, offered_complete))
    return rows


def summarize_writes(rows, config_ids):
    samples = load_samples()
    compatible, reason = api_compatible(samples, config_ids)
    levels_meta = []
    for condition in CONDITIONS:
        for level in WRITE_LEVELS:
            meta = recover_size(condition, level) or {}
            levels_meta.append({
                "condition": condition,
                "concurrency": level,
                "n_warm": meta.get("n_warm"),
                "n_timed": meta.get("n_timed"),
                "sizing_config": meta.get("sizing_config"),
                "sizing_rps": meta.get("sizing_rps"),
                "probe_rps": meta.get("probe_rps"),
            })
    out = {
        "summary_rule": (
            "alerts/sec is the count of HTTP 201 POST /alert responses in the timed phase "
            "divided by that phase's wall-clock seconds. Connections open before the timer. "
            "p50 and p99 are nearest-rank percentiles of those 201 latencies, in milliseconds. "
            "The summary median is statistics.median of the three repetition values (the middle value). "
            "A level is scored only when API samples agreed, every repetition offered the full warmup "
            "and timed sequence on every configuration, every repetition accepted at least one timed POST, "
            "and alert_rows, duplicate_count_sum, and history_entries matched across configurations."
        ),
        "api_compatible": compatible,
        "api_compatible_reason": reason,
        "configs": list(config_ids),
        "levels_meta": levels_meta,
        "cells": {},
    }
    for condition in CONDITIONS:
        out["cells"][condition] = {}
        for level in WRITE_LEVELS:
            cell = {"scored": False}
            for config_id in config_ids:
                subset = [
                    row for row in rows
                    if row["condition"] == condition and row["config"] == config_id and row["concurrency"] == level
                ]
                subset.sort(key=lambda row: row["rep"])
                cell[config_id] = {
                    "n_reps": len(subset),
                    "rps": [row["rps"] for row in subset],
                    "p50_ms": [row["p50_ms"] for row in subset],
                    "p99_ms": [row["p99_ms"] for row in subset],
                    "elapsed_s": [row["elapsed_s"] for row in subset],
                    "accepted": [row["accepted"] for row in subset],
                    "offered": [row["offered"] for row in subset],
                    "error_count": [row["error_count"] for row in subset],
                    "alert_rows": [row["alert_rows"] for row in subset],
                    "duplicate_count_sum": [row["duplicate_count_sum"] for row in subset],
                    "history_entries": [row["history_entries"] for row in subset],
                    "peak_total_backends": [row["peak_total_backends"] for row in subset],
                    "offered_complete": [row["offered_complete"] for row in subset],
                    "median_rps": median3([row["rps"] for row in subset]),
                    "median_p50_ms": median3([row["p50_ms"] for row in subset]),
                    "median_p99_ms": median3([row["p99_ms"] for row in subset]),
                    "median_elapsed_s": median3([row["elapsed_s"] for row in subset]),
                }
            matches = []
            for rep in REPS:
                docs = pair_docs(condition, level, rep, config_ids)
                matches.append(aggs_of(docs) if docs else False)
            both = all(cell[config_id]["n_reps"] == 3 for config_id in config_ids)
            offered_ok = both and all(
                all(cell[config_id]["offered_complete"]) for config_id in config_ids
            )
            accepted_ok = both and all(
                value not in (None, 0)
                for config_id in config_ids
                for value in cell[config_id]["accepted"]
            )
            aggs_ok = both and len(matches) == 3 and all(matches)
            cell["rep_aggregate_match"] = matches
            cell["scored"] = bool(compatible and aggs_ok and offered_ok and accepted_ok)
            if not compatible:
                cell["unscored_reason"] = reason
            elif not both:
                cell["unscored_reason"] = "fewer than 3 timed repetitions for a configuration"
            elif not aggs_ok:
                cell["unscored_reason"] = "alert row count, duplicate_count sum, or history_entries mismatched"
            elif not offered_ok:
                cell["unscored_reason"] = "a configuration did not receive the full offered sequence"
            elif not accepted_ok:
                cell["unscored_reason"] = "a repetition accepted zero HTTP 201 responses"
            if cell["scored"] and cell["alrust"]["median_rps"] not in (None, 0):
                cell["ratios_over_alrust"] = {}
                for config_id in config_ids:
                    if config_id == "alrust":
                        continue
                    rps = cell[config_id]["median_rps"]
                    if rps is not None:
                        cell["ratios_over_alrust"][config_id] = rps / cell["alrust"]["median_rps"]
            out["cells"][condition][str(level)] = cell
    return out


def write_write_outputs(rows, config_ids):
    summary = summarize_writes(rows, config_ids)
    write_json(ROOT / "test2-writes.json", {
        "partial": not test2_write_complete(config_ids),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "rows": rows,
        "summary": summary,
    })
    with (ROOT / "test2-writes.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=WRITE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["status_codes"] = json.dumps(out.get("status_codes"), separators=(",", ":"))
            writer.writerow(out)
    return summary


def test2_write_complete(config_ids):
    return all(
        file_done(cell_path(condition, config_id, level, rep))
        for condition, level, rep in write_groups()
        for config_id in config_ids
    )


async def run_one_write(condition, level, rep, config_id, meta, order):
    path = cell_path(condition, config_id, level, rep)
    if file_done(path):
        return
    n_warm = meta["n_warm"]
    n_timed = meta["n_timed"]
    warm_ranges, timed_ranges = worker_ranges(n_warm, n_timed, level)
    port = configs()[config_id]["port"]
    log(f"write {condition} c={level} rep={rep} {config_id} first={order[0]} n_timed={n_timed}")
    db = reset_work(config_id, template_for(condition), expected_for(condition))
    t0 = time.perf_counter()
    warm = scrub(await run_posts(HOST, port, API_KEY, warm_ranges, timeout=60.0, deadline_s=180.0))
    sampler = Sampler(db).start()
    try:
        timed = scrub(await run_posts(HOST, port, API_KEY, timed_ranges, timeout=60.0, deadline_s=300.0))
    finally:
        stats = sampler.stop()
    agg = aggregates(db)
    doc = {
        "condition": condition,
        "config": config_id,
        "concurrency": level,
        "rep": rep,
        "order": order,
        "order_pos": order.index(config_id) + 1,
        "first_config": order[0],
        "n_warm": n_warm,
        "n_timed": n_timed,
        "database": db,
        "warmup": warm,
        "timed": timed,
        "agg": agg,
        "db_connections": stats,
        "wall_s": time.perf_counter() - t0,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_json(path, doc)
    log(
        f"  accepted={timed['accepted']}/{timed['offered']} rps={timed['rps']} "
        f"p50={timed['p50_ms']} p99={timed['p99_ms']} elapsed={timed['elapsed_s']} "
        f"err={timed['error_count']} rows={agg['alert_rows']} dup_sum={agg['duplicate_count_sum']} "
        f"hist={agg['history_entries']} peak_db={stats['peak_total_backends']}"
    )


async def write_suite(config_ids):
    WRITE_RAW.mkdir(parents=True, exist_ok=True)
    PILOT_RAW.mkdir(parents=True, exist_ok=True)
    for condition, level, rep in write_groups():
        if rep != 1:
            continue
        if recover_size(condition, level):
            continue
        try:
            await ensure_size(condition, level, config_ids)
        except Exception as exc:
            log(f"FAIL size {condition} c={level} {redact(str(exc))}")
            write_json(WRITE_RAW / f"size_{condition}_c{level}.fail.json", {
                "error": redact(str(exc)),
                "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
    for condition, level, rep in write_groups():
        meta = recover_size(condition, level)
        order = order_for(condition, level, rep, config_ids)
        if not meta or not meta.get("n_timed"):
            log(f"skip writes {condition} c={level} because sizing failed")
            write_write_outputs(load_write_rows(config_ids), config_ids)
            continue
        for config_id in order:
            if file_done(cell_path(condition, config_id, level, rep)):
                continue
            try:
                await run_one_write(condition, level, rep, config_id, meta, order)
            except Exception as exc:
                log(f"FAIL write {condition} {config_id} c={level} r={rep} {redact(str(exc))}")
                write_json(WRITE_RAW / f"{condition}_{config_id}_c{level}_r{rep}.fail.json", {
                    "error": redact(str(exc)),
                    "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
            write_write_outputs(load_write_rows(config_ids), config_ids)
    summary = write_write_outputs(load_write_rows(config_ids), config_ids)
    remove_apps()
    log(f"test2 write loop finished complete={test2_write_complete(config_ids)}")
    return summary


def mix_docs(rep, config_ids):
    docs = []
    for config_id in config_ids:
        path = mix_path(config_id, rep)
        if not file_done(path):
            return None
        docs.append(json.loads(path.read_text(encoding="utf-8")))
    return docs


def load_mixed_rows(config_ids):
    rows = []
    for rep in REPS:
        order = rotate(config_ids, rep - 1)
        for config_id in order:
            path = mix_path(config_id, rep)
            if not file_done(path):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            write = doc["write"]
            read = doc["read"]
            stats = doc.get("db_connections") or {}
            offered_complete = (
                write["offered"] == doc["n_timed"]
                and doc["warmup_write"]["offered"] == doc["n_warm"]
                and not write.get("truncated")
                and not doc["warmup_write"].get("truncated")
            )
            rows.append({
                "config": config_id,
                "rep": rep,
                "order_pos": order.index(config_id) + 1,
                "first_config": order[0],
                "writers": 8,
                "readers": 16,
                "n_warm": doc["n_warm"],
                "n_timed": doc["n_timed"],
                "write_offered": write["offered"],
                "write_accepted": write["accepted"],
                "write_rps": write["rps"],
                "write_p50_ms": write["p50_ms"],
                "write_p99_ms": write["p99_ms"],
                "write_elapsed_s": write["elapsed_s"],
                "write_error_count": write["error_count"],
                "write_status_codes": write.get("status_codes") or {},
                "read_offered": read["offered"],
                "read_accepted": read["accepted"],
                "read_p50_ms": read["p50_ms"],
                "read_p99_ms": read["p99_ms"],
                "read_error_count": read["error_count"],
                "read_status_codes": read.get("status_codes") or {},
                "alert_rows": doc["agg"]["alert_rows"],
                "duplicate_count_sum": doc["agg"]["duplicate_count_sum"],
                "history_entries": doc["agg"]["history_entries"],
                "peak_total_backends": stats.get("peak_total_backends"),
                "peak_active_backends": stats.get("peak_active_backends"),
                "peak_client_backends": stats.get("peak_client_backends"),
                "db_sample_count": stats.get("sample_count"),
                "offered_complete": offered_complete,
                "raw_file": str(path.relative_to(ROOT)),
            })
    return rows


def summarize_mixed(rows, config_ids):
    samples = load_samples()
    compatible, reason = api_compatible(samples, config_ids)
    cell = {
        "summary_rule": (
            "Eight writers send the seeded concurrency-8 sequence while 16 readers "
            "GET the newest-50 list on the same server. Write alerts/sec and write percentiles "
            "use the write-suite rules. Read p50 and p99 are nearest-rank percentiles of HTTP 200 "
            "latencies during the timed write. A rep is scored only when API samples agreed, "
            "the full write sequence was offered, and alert_rows, duplicate_count_sum, and "
            "history_entries matched across configurations."
        ),
        "api_compatible": compatible,
        "api_compatible_reason": reason,
        "configs": list(config_ids),
        "scored": False,
    }
    for config_id in config_ids:
        subset = [row for row in rows if row["config"] == config_id]
        subset.sort(key=lambda row: row["rep"])
        cell[config_id] = {
            "n_reps": len(subset),
            "write_rps": [row["write_rps"] for row in subset],
            "write_p50_ms": [row["write_p50_ms"] for row in subset],
            "write_p99_ms": [row["write_p99_ms"] for row in subset],
            "write_elapsed_s": [row["write_elapsed_s"] for row in subset],
            "write_error_count": [row["write_error_count"] for row in subset],
            "write_accepted": [row["write_accepted"] for row in subset],
            "read_p50_ms": [row["read_p50_ms"] for row in subset],
            "read_p99_ms": [row["read_p99_ms"] for row in subset],
            "read_error_count": [row["read_error_count"] for row in subset],
            "read_accepted": [row["read_accepted"] for row in subset],
            "alert_rows": [row["alert_rows"] for row in subset],
            "duplicate_count_sum": [row["duplicate_count_sum"] for row in subset],
            "history_entries": [row["history_entries"] for row in subset],
            "peak_total_backends": [row["peak_total_backends"] for row in subset],
            "offered_complete": [row["offered_complete"] for row in subset],
            "median_write_rps": median3([row["write_rps"] for row in subset]),
            "median_write_p50_ms": median3([row["write_p50_ms"] for row in subset]),
            "median_write_p99_ms": median3([row["write_p99_ms"] for row in subset]),
            "median_read_p50_ms": median3([row["read_p50_ms"] for row in subset]),
            "median_read_p99_ms": median3([row["read_p99_ms"] for row in subset]),
            "median_write_elapsed_s": median3([row["write_elapsed_s"] for row in subset]),
        }
    matches = []
    for rep in REPS:
        docs = mix_docs(rep, config_ids)
        matches.append(aggs_of(docs) if docs else False)
    both = all(cell[config_id]["n_reps"] == 3 for config_id in config_ids)
    offered_ok = both and all(all(cell[config_id]["offered_complete"]) for config_id in config_ids)
    accepted_ok = both and all(
        value not in (None, 0)
        for config_id in config_ids
        for value in cell[config_id]["write_accepted"]
    )
    aggs_ok = both and matches == [True, True, True]
    cell["rep_aggregate_match"] = matches
    cell["scored"] = bool(compatible and aggs_ok and offered_ok and accepted_ok)
    if not cell["scored"]:
        if not compatible:
            cell["unscored_reason"] = reason
        elif not both:
            cell["unscored_reason"] = "fewer than 3 mixed repetitions for a configuration"
        elif not aggs_ok:
            cell["unscored_reason"] = "alert row count, duplicate_count sum, or history_entries mismatched"
        elif not offered_ok:
            cell["unscored_reason"] = "a configuration did not receive the full offered write sequence"
        else:
            cell["unscored_reason"] = "a repetition accepted zero HTTP 201 writes"
    return cell


def write_mixed_outputs(rows, config_ids):
    summary = summarize_mixed(rows, config_ids)
    write_json(ROOT / "test2-mixed.json", {
        "partial": not test2_mixed_complete(config_ids),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "rows": rows,
        "summary": summary,
    })
    fields = [
        "config", "rep", "order_pos", "first_config", "writers", "readers",
        "n_warm", "n_timed", "write_offered", "write_accepted", "write_rps",
        "write_p50_ms", "write_p99_ms", "write_elapsed_s", "write_error_count",
        "read_offered", "read_accepted", "read_p50_ms", "read_p99_ms", "read_error_count",
        "alert_rows", "duplicate_count_sum", "history_entries",
        "peak_total_backends", "peak_active_backends", "peak_client_backends",
        "offered_complete", "raw_file",
    ]
    with (ROOT / "test2-mixed.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            for key in ("write_status_codes", "read_status_codes"):
                if key in out and not isinstance(out[key], str):
                    out[key] = json.dumps(out[key], separators=(",", ":"))
            writer.writerow(out)
    return summary


def test2_mixed_complete(config_ids):
    return all(file_done(mix_path(config_id, rep)) for rep in REPS for config_id in config_ids)


async def run_one_mixed(rep, meta, config_id, order):
    path = mix_path(config_id, rep)
    if file_done(path):
        return
    n_warm = meta["n_warm"]
    n_timed = meta["n_timed"]
    warm_ranges, timed_ranges = worker_ranges(n_warm, n_timed, 8)
    port = configs()[config_id]["port"]
    log(f"mixed rep={rep} {config_id} first={order[0]}")
    db = reset_work(config_id, "base_seed", 1000)

    async def phase(ranges, deadline_s, sample):
        stop = asyncio.Event()
        ready = asyncio.Event()
        readers = asyncio.create_task(run_gets(
            HOST, port, API_KEY, 16, READ_PATH, stop, ready, timeout=60.0,
        ))
        sampler = None
        try:
            await asyncio.wait_for(ready.wait(), 30)
            if sample:
                sampler = Sampler(db).start()
            writers = await run_posts(
                HOST, port, API_KEY, ranges, timeout=60.0, deadline_s=deadline_s,
            )
            stop.set()
            reads = await readers
            stats = sampler.stop() if sampler else None
            return scrub(writers), scrub(reads), stats
        except BaseException:
            stop.set()
            if sampler:
                sampler.stop()
            readers.cancel()
            try:
                await readers
            except BaseException:
                pass
            raise

    warm_w, warm_r, _warm_stats = await phase(warm_ranges, 180.0, sample=False)
    timed_w, timed_r, stats = await phase(timed_ranges, 300.0, sample=True)
    agg = aggregates(db)
    doc = {
        "config": config_id,
        "rep": rep,
        "order": order,
        "order_pos": order.index(config_id) + 1,
        "first_config": order[0],
        "writers": 8,
        "readers": 16,
        "n_warm": n_warm,
        "n_timed": n_timed,
        "read_path": READ_PATH,
        "database": db,
        "warmup_write": warm_w,
        "warmup_read": warm_r,
        "write": timed_w,
        "read": timed_r,
        "agg": agg,
        "db_connections": stats,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_json(path, doc)
    log(
        f"  write rps={timed_w['rps']} p50={timed_w['p50_ms']} p99={timed_w['p99_ms']} "
        f"err={timed_w['error_count']} read p50={timed_r['p50_ms']} p99={timed_r['p99_ms']} "
        f"read_err={timed_r['error_count']} read_n={timed_r['accepted']} "
        f"rows={agg['alert_rows']} dup_sum={agg['duplicate_count_sum']} hist={agg['history_entries']} "
        f"peak_db={None if not stats else stats['peak_total_backends']}"
    )


async def mixed_suite(config_ids):
    MIX_RAW.mkdir(parents=True, exist_ok=True)
    meta = recover_size("seeded", 8)
    if not meta or not meta.get("n_timed"):
        log("mixed has no seeded c=8 size; sizing now")
        meta = await ensure_size("seeded", 8, config_ids)
    for rep in REPS:
        order = rotate(config_ids, rep - 1)
        for config_id in order:
            if file_done(mix_path(config_id, rep)):
                continue
            try:
                await run_one_mixed(rep, meta, config_id, order)
            except Exception as exc:
                log(f"FAIL mixed {config_id} r={rep} {redact(str(exc))}")
                write_json(MIX_RAW / f"{config_id}_r{rep}.fail.json", {
                    "error": redact(str(exc)),
                    "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
            write_mixed_outputs(load_mixed_rows(config_ids), config_ids)
    summary = write_mixed_outputs(load_mixed_rows(config_ids), config_ids)
    remove_apps()
    log(f"mixed loop finished complete={test2_mixed_complete(config_ids)}")
    return summary


def templates_ok():
    if not (db_exists("base_empty") and db_exists("base_seed")):
        return False
    try:
        return alert_count("base_empty") == 0 and alert_count("base_seed") == 1000
    except Exception:
        return False


async def run_test2():
    config_ids = final_configs()
    if "alrust" not in config_ids or "alerta-uwsgi-5" not in config_ids:
        raise RuntimeError(f"unexpected final configs {config_ids}")
    if not templates_ok():
        raise RuntimeError("base_empty/base_seed are not intact for test 2")
    run_api_samples(config_ids)
    await write_suite(config_ids)
    # round 2: the mixed case is mixed263.py (PR #263 methodology), not the old mixed_suite
