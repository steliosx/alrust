import os
#!/usr/bin/env python3
"""Gunicorn sweep and Test 1 concurrent reads."""
import csv
import json
import time
from pathlib import Path

from common import (
    RAW,
    READ_CASES,
    READ_LEVELS,
    REPS,
    ROOT,
    SWEEP_CONFIGS,
    Sampler,
    alert_count,
    alert_fingerprint,
    cases_equal,
    config_db,
    configs,
    ensure_clones,
    ensure_server,
    fetch_case,
    file_done,
    log,
    median3,
    parse_oha,
    redact,
    remove_apps,
    rotate,
    run_oha,
    write_json,
)

SWEEP_DIR = RAW / "sweep"
SWEEP_WARM = RAW / "sweep-warmup"
TEST1_DIR = RAW / "test1"
TEST1_WARM = RAW / "test1-warmup"
LIST_PATH = READ_CASES[0][1]
CHOICE_PATH = RAW / "chosen-gunicorn.json"
PARITY_SWEEP = ROOT / "parity-sweep.json"
PARITY_TEST1 = ROOT / "parity-test1.json"

READ_FIELDS = [
    "suite", "config", "case", "concurrency", "rep", "order_pos", "first_config",
    "rps", "p50_ms", "p99_ms", "requests", "duration_s",
    "status_2xx", "non_2xx", "error_count", "success_rate",
    "peak_total_backends", "peak_active_backends", "peak_client_backends",
    "db_sample_count", "db_sample_interval_s", "db_sample_interval_actual_s",
    "status_codes", "errors", "raw_file",
]


def read_groups():
    groups = []
    for level in READ_LEVELS:
        for case, path in READ_CASES:
            for rep in REPS:
                groups.append((case, path, level, rep))
    return groups


def sweep_groups():
    return [("list", LIST_PATH, 32, rep) for rep in REPS]


def order_for(group_list, configs_order, key):
    index = group_list.index(key)
    return rotate(configs_order, index)


def sweep_timed(config_id, rep):
    return SWEEP_DIR / f"{config_id}_list_c32_r{rep}.json"


def test1_timed(config_id, case, level, rep):
    return TEST1_DIR / f"{config_id}_{case}_c{level}_r{rep}.json"


def db_fields(stats):
    if not stats:
        return {
            "peak_total_backends": None,
            "peak_active_backends": None,
            "peak_client_backends": None,
            "db_sample_count": 0,
            "db_sample_interval_s": None,
            "db_sample_interval_actual_s": None,
        }
    return {
        "peak_total_backends": stats.get("peak_total_backends"),
        "peak_active_backends": stats.get("peak_active_backends"),
        "peak_client_backends": stats.get("peak_client_backends"),
        "db_sample_count": stats.get("sample_count"),
        "db_sample_interval_s": stats.get("sample_interval_s"),
        "db_sample_interval_actual_s": stats.get("sample_interval_actual_s"),
    }


def run_timed_read(config_id, case, path, level, rep, db, dest, warm_dest, order):
    if file_done(dest):
        return
    cfg = configs()[config_id]
    url = f"http://127.0.0.1:{cfg['port']}{path}"
    expected = 1000
    ensure_server(config_id, db, expected)
    stem = dest.stem
    log(f"warmup {stem} first={order[0]}")
    t0 = time.perf_counter()
    run_oha(url, level, "5s", warm_dest, overwrite=True)
    log(f"  warmup done {time.perf_counter() - t0:.1f}s")
    oha_tmp = dest.with_name(dest.name + ".oha")
    if oha_tmp.exists():
        oha_tmp.unlink()
    sampler = Sampler(db).start()
    try:
        log(f"timed {stem}")
        t0 = time.perf_counter()
        oha_doc = run_oha(url, level, "30s", oha_tmp, overwrite=True)
        stats = sampler.stop()
        parsed = parse_oha(oha_doc)
    except Exception:
        sampler.stop()
        if oha_tmp.exists():
            oha_tmp.unlink()
        raise
    if oha_tmp.exists():
        oha_tmp.unlink()
    doc = {
        "suite": dest.parent.name,
        "config": config_id,
        "case": case,
        "concurrency": level,
        "rep": rep,
        "order": order,
        "order_pos": order.index(config_id) + 1,
        "first_config": order[0],
        "path": path,
        "database": db,
        "oha": oha_doc,
        "db_connections": stats,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_json(dest, doc)
    log(
        f"  rps={parsed['rps']:.4f} p50_ms={parsed['p50_ms']:.4f} "
        f"p99_ms={parsed['p99_ms']:.4f} req={parsed['requests']} "
        f"non2xx={parsed['non_2xx']} err={parsed['error_count']} "
        f"peak_db={stats['peak_total_backends']} peak_active={stats['peak_active_backends']} "
        f"wall={time.perf_counter() - t0:.1f}s"
    )


def record_failure(path, message):
    fail = path.with_suffix(".fail.json")
    write_json(fail, {"error": redact(message), "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cell": path.name})
    log(f"FAIL {path.name} {redact(message)}")


def load_read_row(path, suite):
    doc = json.loads(path.read_text(encoding="utf-8"))
    parsed = parse_oha(doc["oha"])
    stats = doc.get("db_connections") or {}
    return {
        "suite": suite,
        "config": doc["config"],
        "case": doc["case"],
        "concurrency": doc["concurrency"],
        "rep": doc["rep"],
        "order_pos": doc["order_pos"],
        "first_config": doc["first_config"],
        "raw_file": str(path.relative_to(ROOT)),
        **parsed,
        **db_fields(stats),
    }


def write_table(rows, csv_path, json_path, summary):
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=READ_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["status_codes"] = json.dumps(out["status_codes"], separators=(",", ":"))
            out["errors"] = json.dumps(out["errors"], separators=(",", ":"))
            writer.writerow(out)
    write_json(json_path, {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "rows": rows,
        "summary": summary,
    })


def point_from_rows(rows):
    rows = sorted(rows, key=lambda row: row["rep"])
    rps = [row["rps"] for row in rows]
    p50 = [row["p50_ms"] for row in rows]
    p99 = [row["p99_ms"] for row in rows]
    return {
        "n_reps": len(rows),
        "reps": [row["rep"] for row in rows],
        "rps": rps,
        "p50_ms": p50,
        "p99_ms": p99,
        "requests": [row["requests"] for row in rows],
        "duration_s": [row["duration_s"] for row in rows],
        "error_count": [row["error_count"] for row in rows],
        "non_2xx": [row["non_2xx"] for row in rows],
        "peak_total_backends": [row["peak_total_backends"] for row in rows],
        "peak_active_backends": [row["peak_active_backends"] for row in rows],
        "median_rps": median3(rps),
        "median_p50_ms": median3(p50),
        "median_p99_ms": median3(p99),
        "error_count_sum": sum(row["error_count"] for row in rows),
        "non_2xx_sum": sum(row["non_2xx"] for row in rows),
        "requests_sum": sum(row["requests"] for row in rows),
    }


def run_parity(config_ids, purpose, cases, dest):
    if file_done(dest):
        log(f"parity exists {dest.name}")
        return json.loads(dest.read_text(encoding="utf-8"))
    got = {case: {} for case, _path in cases}
    fingerprints = {}
    for config_id in config_ids:
        db = config_db(purpose, config_id)
        ensure_server(config_id, db, 1000)
        fingerprints[config_id] = {
            "database": db,
            "fingerprint": alert_fingerprint(db),
            "alert_count": alert_count(db),
        }
        for case, path in cases:
            got[case][config_id] = fetch_case(config_id, path)
            log(f"parity fetch {config_id} {case} http={got[case][config_id]['http']} total={got[case][config_id].get('total')}")
    remove_apps()
    out = {
        "compared_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "configs": list(config_ids),
        "sql_fingerprint": fingerprints,
        "cases": {},
    }
    fingers = [item["fingerprint"] for item in fingerprints.values()]
    out["sql_fingerprint_match"] = len(set(fingers)) == 1
    all_ok = out["sql_fingerprint_match"]
    for case, _path in cases:
        pairs = {}
        ids = config_ids
        match = True
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                same = cases_equal(case, got[case][left], got[case][right])
                pairs[f"{left}__{right}"] = same
                match = match and same
        out["cases"][case] = {"match": match, "pairs": pairs, "observed": got[case]}
        all_ok = all_ok and match
        log(f"parity {case}: match={match}")
    out["all_match"] = all_ok
    write_json(dest, out)
    return out


def sweep_complete():
    return all(file_done(sweep_timed(config_id, rep)) for config_id in SWEEP_CONFIGS for rep in REPS)


def sweep_fail_path(config_id, rep):
    return sweep_timed(config_id, rep).with_suffix(".fail.json")


def sweep_settled():
    """True when every sweep cell succeeded or has a recorded failure."""
    for config_id in SWEEP_CONFIGS:
        for rep in REPS:
            if file_done(sweep_timed(config_id, rep)):
                continue
            if not file_done(sweep_fail_path(config_id, rep)):
                return False
    return True


def any_test1_cells():
    folder = TEST1_DIR
    if not folder.exists():
        return False
    return any(
        file_done(path)
        for path in folder.glob("*.json")
        if not path.name.endswith(".fail.json")
    )


def load_sweep_rows():
    rows = []
    for key in sweep_groups():
        case, _path, level, rep = key
        order = order_for(sweep_groups(), SWEEP_CONFIGS, key)
        for config_id in order:
            path = sweep_timed(config_id, rep)
            if file_done(path):
                rows.append(load_read_row(path, "sweep"))
    return rows


def summarize_sweep(rows, parity):
    parity_ok = bool(parity and parity.get("cases", {}).get("list", {}).get("match") and parity.get("sql_fingerprint_match"))
    points = {}
    for config_id in SWEEP_CONFIGS:
        workers = configs()[config_id]["workers"]
        subset = [row for row in rows if row["config"] == config_id and row["concurrency"] == 32 and row["case"] == "list"]
        cell = point_from_rows(subset)
        cell["config"] = config_id
        cell["workers"] = workers
        cell["threads"] = 4
        errors = cell["n_reps"] == 3 and cell["error_count_sum"] == 0 and cell["non_2xx_sum"] == 0
        cell["scored"] = bool(parity_ok and cell["n_reps"] == 3 and errors and cell["median_rps"] is not None)
        if not parity_ok:
            cell["unscored_reason"] = "parity mismatch or missing parity"
        elif cell["n_reps"] != 3:
            cell["unscored_reason"] = "fewer than 3 timed repetitions"
        elif not errors:
            cell["unscored_reason"] = "a repetition has transport errors or non-2xx responses"
        elif cell["median_rps"] is None:
            cell["unscored_reason"] = "median rps missing"
        points[str(workers)] = cell
    return {
        "summary_rule": (
            "Gunicorn sweep is newest-50 at 32 clients, three 30s timed repetitions after a discarded 5s warmup. "
            "median_rps, median_p50_ms, and median_p99_ms are statistics.median of the three timed values "
            "(the middle value). A point is scored only when list parity matches across workers 4, 8, and 16, "
            "all three repetitions exist, error_count is 0, and non_2xx is 0. "
            "The chosen N is the scored point with the highest median_rps. An exact rps tie picks the fewer workers."
        ),
        "parity_match": parity_ok,
        "threads": 4,
        "points": points,
    }


def choose_from_summary(summary):
    scored = []
    for workers, cell in summary["points"].items():
        if cell.get("scored"):
            scored.append((cell["median_rps"], int(workers)))
    if not scored:
        return {
            "chosen_workers": None,
            "chosen_config": None,
            "reason": "no scored sweep point; full Gunicorn workload is not run",
            "scored_workers": [],
        }
    best_rps = max(item[0] for item in scored)
    tied = sorted(workers for rps, workers in scored if rps == best_rps)
    chosen = tied[0]
    reason = f"highest median timed rps on list concurrency 32 among scored points"
    if len(tied) > 1:
        reason += f"; workers {tied} tied on median rps; picked the fewer workers ({chosen})"
    return {
        "chosen_workers": chosen,
        "chosen_config": f"alerta-gunicorn-{chosen}",
        "reason": reason,
        "best_median_rps": best_rps,
        "tied_workers": tied,
        "scored_workers": sorted(workers for _rps, workers in scored),
    }


def refresh_sweep():
    rows = load_sweep_rows()
    parity = json.loads(PARITY_SWEEP.read_text(encoding="utf-8")) if file_done(PARITY_SWEEP) else None
    summary = summarize_sweep(rows, parity)
    write_table(rows, ROOT / "sweep.csv", ROOT / "sweep.json", summary)
    return summary


def run_sweep():
    SWEEP_DIR.mkdir(parents=True, exist_ok=True)
    SWEEP_WARM.mkdir(parents=True, exist_ok=True)
    ensure_clones("sweep", SWEEP_CONFIGS, "base_seed", 1000)
    if not file_done(PARITY_SWEEP):
        run_parity(SWEEP_CONFIGS, "sweep", [("list", LIST_PATH)], PARITY_SWEEP)
    groups = sweep_groups()
    for key in groups:
        case, path, level, rep = key
        order = order_for(groups, SWEEP_CONFIGS, key)
        for config_id in order:
            dest = sweep_timed(config_id, rep)
            if file_done(dest):
                continue
            try:
                run_timed_read(
                    config_id, case, path, level, rep,
                    config_db("sweep", config_id),
                    dest,
                    SWEEP_WARM / dest.name,
                    order,
                )
            except Exception as exc:
                record_failure(dest, str(exc))
            refresh_sweep()
    summary = refresh_sweep()
    choice = choose_from_summary(summary)
    choice["summary_points"] = {
        workers: {
            "median_rps": cell["median_rps"],
            "scored": cell["scored"],
            "unscored_reason": cell.get("unscored_reason"),
        }
        for workers, cell in summary["points"].items()
    }
    if file_done(CHOICE_PATH) and any_test1_cells():
        log("keeping existing gunicorn choice because Test 1 cells exist")
    else:
        write_json(CHOICE_PATH, choice)
        log(f"gunicorn choice {choice['chosen_config']} {choice['reason']}")
    remove_apps()
    return choice


def load_choice():
    if not file_done(CHOICE_PATH):
        return None
    return json.loads(CHOICE_PATH.read_text(encoding="utf-8"))


def final_configs():
    choice = load_choice()
    configs_order = ["alrust", "alerta-uwsgi-5"]
    if choice and choice.get("chosen_config"):
        configs_order.append(choice["chosen_config"])
    return configs_order


def test1_expected():
    return [
        (config_id, case, level, rep)
        for case, _path, level, rep in read_groups()
        for config_id in final_configs()
    ]


def test1_complete():
    if not file_done(CHOICE_PATH):
        return False
    return all(file_done(test1_timed(*cell)) for cell in test1_expected())


def load_test1_rows():
    rows = []
    configs_order = final_configs()
    groups = read_groups()
    for key in groups:
        case, _path, level, rep = key
        order = order_for(groups, configs_order, key)
        for config_id in order:
            path = test1_timed(config_id, case, level, rep)
            if file_done(path):
                rows.append(load_read_row(path, "test1"))
    return rows


def summarize_test1(rows, parity):
    configs_order = final_configs()
    out = {
        "summary_rule": (
            "For each configuration, case, and concurrency, median_rps, median_p50_ms, and median_p99_ms "
            "are statistics.median of the three timed repetitions (the middle value). "
            "rps is oha summary.requestsPerSec. p50_ms and p99_ms are oha latencyPercentiles seconds times 1000. "
            "error_count is the sum of errorDistribution values, or 0 when that object is empty. "
            "non_2xx is the sum of status codes outside 200-299. "
            "A case is scored only when parity matched across every final configuration and each configuration "
            "has three timed repetitions. Transport errors do not by themselves unscore Test 1; they are reported."
        ),
        "configs": configs_order,
        "cases": {},
    }
    for case, _path in READ_CASES:
        match = bool((parity or {}).get("cases", {}).get(case, {}).get("match")) and bool((parity or {}).get("sql_fingerprint_match"))
        case_doc = {"parity_match": match, "levels": {}}
        for level in READ_LEVELS:
            cell = {"scored": False}
            for config_id in configs_order:
                subset = [
                    row for row in rows
                    if row["config"] == config_id and row["case"] == case and row["concurrency"] == level
                ]
                cell[config_id] = point_from_rows(subset)
            complete = all(cell[config_id]["n_reps"] == 3 for config_id in configs_order)
            if match and complete:
                cell["scored"] = True
                alrust_rps = cell["alrust"]["median_rps"]
                cell["ratios_over_alrust"] = {}
                if alrust_rps not in (None, 0):
                    for config_id in configs_order:
                        if config_id == "alrust":
                            continue
                        rps = cell[config_id]["median_rps"]
                        if rps is not None:
                            cell["ratios_over_alrust"][config_id] = rps / alrust_rps
            elif not match:
                cell["unscored_reason"] = "parity mismatch"
            else:
                cell["unscored_reason"] = "fewer than 3 timed repetitions for a configuration"
            case_doc["levels"][str(level)] = cell
        out["cases"][case] = case_doc
    return out


def refresh_test1():
    rows = load_test1_rows()
    parity = json.loads(PARITY_TEST1.read_text(encoding="utf-8")) if file_done(PARITY_TEST1) else None
    summary = summarize_test1(rows, parity)
    write_table(rows, ROOT / "test1-reads.csv", ROOT / "test1-reads.json", summary)
    return summary


def run_test1():
    configs_order = final_configs()
    if len(configs_order) < 2:
        raise RuntimeError("final configs are not known")
    TEST1_DIR.mkdir(parents=True, exist_ok=True)
    TEST1_WARM.mkdir(parents=True, exist_ok=True)
    ensure_clones("t1", configs_order, "base_seed", 1000)
    if not file_done(PARITY_TEST1):
        run_parity(configs_order, "t1", READ_CASES, PARITY_TEST1)
    if os.environ.get("RERUN_REBUILD") == "1":
        run_parity(configs_order, "t1", READ_CASES, ROOT / "parity-test1-rerun.json")
    groups = read_groups()
    done = sum(1 for cell in test1_expected() if file_done(test1_timed(*cell)))
    log(f"test1 cells already done {done}/{len(test1_expected())}")
    for key in groups:
        case, path, level, rep = key
        order = order_for(groups, configs_order, key)
        for config_id in order:
            dest = test1_timed(config_id, case, level, rep)
            if file_done(dest):
                continue
            try:
                run_timed_read(
                    config_id, case, path, level, rep,
                    config_db("t1", config_id),
                    dest,
                    TEST1_WARM / dest.name,
                    order,
                )
            except Exception as exc:
                record_failure(dest, str(exc))
            refresh_test1()
    summary = refresh_test1()
    remove_apps()
    log(f"test1 loop finished complete={test1_complete()}")
    return summary


def order_self_check():
    groups = read_groups()
    sample = ["alrust", "alerta-uwsgi-5", "alerta-gunicorn-4"]
    first = {name: 0 for name in sample}
    for key in groups:
        first[order_for(groups, sample, key)[0]] += 1
    if set(first.values()) != {20}:
        raise RuntimeError(f"read order not balanced: {first}")
    sweeps = sweep_groups()
    sfirst = {name: 0 for name in SWEEP_CONFIGS}
    for key in sweeps:
        sfirst[order_for(sweeps, SWEEP_CONFIGS, key)[0]] += 1
    if set(sfirst.values()) != {1}:
        raise RuntimeError(f"sweep order not balanced: {sfirst}")
