#!/usr/bin/env python3
"""Build REPORT.md from this run's raw cell files."""
import json
from pathlib import Path

from common import (
    ALERTA_COMMIT,
    ALRUST_BINARY_SHA,
    ALRUST_COMMIT,
    PG_DIGEST,
    RAW,
    ROOT,
    file_done,
    median_any,
    redact,
    uwsgi_cmd,
)
from reads import (
    CHOICE_PATH,
    PARITY_SWEEP,
    PARITY_TEST1,
    READ_CASES,
    READ_LEVELS,
    load_choice,
    refresh_sweep,
    refresh_test1,
)


def _refresh_writes():
    from reads import final_configs
    from writes import load_write_rows, write_write_outputs
    config_ids = final_configs()
    if not file_done(CHOICE_PATH):
        return None
    return write_write_outputs(load_write_rows(config_ids), config_ids)


def _refresh_mixed():
    from reads import final_configs
    from writes import load_mixed_rows, write_mixed_outputs
    config_ids = final_configs()
    if not file_done(CHOICE_PATH):
        return None
    return write_mixed_outputs(load_mixed_rows(config_ids), config_ids)


def num(value):
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return json.dumps(value)
    return str(value)


def load_json(path):
    path = Path(path)
    if not file_done(path):
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect_peaks():
    found = []
    mapping = [
        (RAW / "sweep", "sweep"),
        (RAW / "test1", "test1"),
        (RAW / "test2", "test2"),
        (RAW / "test2-mixed", "mixed"),
    ]
    for folder, suite in mapping:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if path.name.endswith(".fail.json") or path.name.startswith("size_") or path.name.startswith("api-"):
                continue
            if not file_done(path):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            stats = doc.get("db_connections")
            if not isinstance(stats, dict) or "config" not in doc:
                continue
            found.append({
                "suite": suite,
                "config": doc["config"],
                "peak_total": stats.get("peak_total_backends"),
                "peak_active": stats.get("peak_active_backends"),
                "peak_client": stats.get("peak_client_backends"),
                "samples": stats.get("sample_count"),
                "file": str(path.relative_to(ROOT)),
            })
    return found


def peak_table(records):
    lines = [
        "| suite | config | runs | max peak total | median peak total | max peak active | median peak active | max peak client |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    keys = sorted({(rec["suite"], rec["config"]) for rec in records})
    if not keys:
        return "No timed cells with connection samples were found.\n"
    for suite, config in keys:
        subset = [rec for rec in records if rec["suite"] == suite and rec["config"] == config]
        totals = [rec["peak_total"] for rec in subset]
        actives = [rec["peak_active"] for rec in subset]
        clients = [rec["peak_client"] for rec in subset]
        lines.append(
            "| {suite} | {config} | {n} | {mx} | {med} | {amx} | {amed} | {cmx} |".format(
                suite=suite,
                config=config,
                n=len(subset),
                mx=num(max(totals) if totals else None),
                med=num(median_any(totals)),
                amx=num(max(actives) if actives else None),
                amed=num(median_any(actives)),
                cmx=num(max(clients) if clients else None),
            )
        )
    lines.append("")
    lines.append(
        "Median of an odd number of per-run peaks is the middle value. "
        "Median of an even number is the mean of the two middle values (`statistics.median`). "
        "Those means are exact JSON numbers, not display rounding."
    )
    return "\n".join(lines) + "\n"


def unscored_lines(sweep, test1, writes, mixed):
    lines = []
    if sweep:
        for workers, cell in sorted(sweep["points"].items(), key=lambda kv: int(kv[0])):
            if not cell.get("scored"):
                lines.append(
                    f"- Sweep gunicorn workers={workers}: {cell.get('unscored_reason', 'unscored')}."
                )
    if test1:
        for case, case_doc in test1["cases"].items():
            for level, cell in case_doc["levels"].items():
                if not cell.get("scored"):
                    lines.append(f"- Test 1 {case} concurrency {level}: {cell.get('unscored_reason', 'unscored')}.")
    if writes:
        for condition, levels in writes["cells"].items():
            for level, cell in levels.items():
                if not cell.get("scored"):
                    lines.append(f"- Test 2 writes {condition} concurrency {level}: {cell.get('unscored_reason', 'unscored')}.")
    if mixed and not mixed.get("scored"):
        lines.append(f"- Mixed: {mixed.get('unscored_reason', 'unscored')}.")
    if not lines:
        return "Every scored table cell met its scoring rule. There is no unscored case.\n"
    return "\n".join(lines) + "\n"


def sweep_table(summary, choice):
    chosen = None if not choice else choice.get("chosen_workers")
    lines = [
        "| workers | threads | median rps | median p50 ms | median p99 ms | error_count sum | non_2xx sum | reps | scored | chosen |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for workers in ("4", "8", "16"):
        cell = summary["points"][workers]
        lines.append(
            "| {w} | 4 | {rps} | {p50} | {p99} | {err} | {non} | {reps} | {scored} | {chosen} |".format(
                w=workers,
                rps=num(cell["median_rps"]),
                p50=num(cell["median_p50_ms"]),
                p99=num(cell["median_p99_ms"]),
                err=num(cell["error_count_sum"]),
                non=num(cell["non_2xx_sum"]),
                reps=cell["n_reps"],
                scored=num(cell["scored"]),
                chosen="yes" if chosen == int(workers) else "no",
            )
        )
    return "\n".join(lines) + "\n"


def test1_tables(summary):
    chunks = []
    configs_order = summary["configs"]
    for case, _path in READ_CASES:
        case_doc = summary["cases"][case]
        lines = [
            f"### {case}",
            "",
            f"Parity match: {num(case_doc['parity_match'])}.",
            "",
            "| config | c | median rps | median p50 ms | median p99 ms | error_count sum | non_2xx sum | scored |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for level in READ_LEVELS:
            cell = case_doc["levels"][str(level)]
            for config_id in configs_order:
                point = cell[config_id]
                lines.append(
                    "| {cfg} | {level} | {rps} | {p50} | {p99} | {err} | {non} | {scored} |".format(
                        cfg=config_id,
                        level=level,
                        rps=num(point["median_rps"]),
                        p50=num(point["median_p50_ms"]),
                        p99=num(point["median_p99_ms"]),
                        err=num(point["error_count_sum"]),
                        non=num(point["non_2xx_sum"]),
                        scored=num(cell["scored"]),
                    )
                )
        if case == "list":
            lines.append("")
            lines.append("Repetition rps values (exact) for list, used by the medians above:")
            for level in READ_LEVELS:
                cell = case_doc["levels"][str(level)]
                for config_id in configs_order:
                    lines.append(
                        f"- {config_id} c={level} rps={json.dumps(cell[config_id]['rps'])} "
                        f"p50_ms={json.dumps(cell[config_id]['p50_ms'])} "
                        f"p99_ms={json.dumps(cell[config_id]['p99_ms'])}"
                    )
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks) + "\n"


def write_tables(summary):
    chunks = []
    configs_order = summary["configs"]
    for condition in ("empty", "seeded"):
        lines = [
            f"### {condition}",
            "",
            "| config | c | median alerts/sec | median p50 ms | median p99 ms | median elapsed s | error counts | scored |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for level in ("1", "8", "32"):
            cell = summary["cells"][condition][level]
            for config_id in configs_order:
                point = cell[config_id]
                lines.append(
                    "| {cfg} | {level} | {rps} | {p50} | {p99} | {elapsed} | {err} | {scored} |".format(
                        cfg=config_id,
                        level=level,
                        rps=num(point["median_rps"]),
                        p50=num(point["median_p50_ms"]),
                        p99=num(point["median_p99_ms"]),
                        elapsed=num(point["median_elapsed_s"]),
                        err=json.dumps(point["error_count"]),
                        scored=num(cell["scored"]),
                    )
                )
            meta = next(
                (
                    item for item in summary["levels_meta"]
                    if item["condition"] == condition and str(item["concurrency"]) == level
                ),
                {},
            )
            lines.append("")
            lines.append(
                f"Sizing for {condition} c={level}: n_warm={num(meta.get('n_warm'))} "
                f"n_timed={num(meta.get('n_timed'))} sizing_config={meta.get('sizing_config')} "
                f"sizing_rps={num(meta.get('sizing_rps'))} probe_rps={json.dumps(meta.get('probe_rps'))}."
            )
            lines.append(
                f"Scored: {num(cell['scored'])}. "
                + (cell.get("unscored_reason") or "Scoring rule passed.")
            )
            lines.append("")
        chunks.append("\n".join(lines))
    return "\n".join(chunks) + "\n"


def mixed_table(summary, test1):
    configs_order = summary["configs"]
    lines = [
        "| config | median write alerts/sec | write p50 ms | write p99 ms | write errors | read p50 ms | read p99 ms | read errors | scored |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for config_id in configs_order:
        point = summary[config_id]
        lines.append(
            "| {cfg} | {rps} | {p50} | {p99} | {werr} | {rp50} | {rp99} | {rerr} | {scored} |".format(
                cfg=config_id,
                rps=num(point["median_write_rps"]),
                p50=num(point["median_write_p50_ms"]),
                p99=num(point["median_write_p99_ms"]),
                werr=json.dumps(point["write_error_count"]),
                rp50=num(point["median_read_p50_ms"]),
                rp99=num(point["median_read_p99_ms"]),
                rerr=json.dumps(point["read_error_count"]),
                scored=num(summary["scored"]),
            )
        )
    lines.append("")
    lines.append(f"Scored: {num(summary['scored'])}. " + (summary.get("unscored_reason") or "Scoring rule passed."))
    if test1 and "list" in test1.get("cases", {}):
        lines.append("")
        lines.append("Read latencies under this write load, compared with Test 1 list (newest 50) medians:")
        for level in ("8", "16"):
            cell = test1["cases"]["list"]["levels"][level]
            for config_id in configs_order:
                idle = cell[config_id]["median_p50_ms"]
                busy = summary[config_id]["median_read_p50_ms"]
                idle99 = cell[config_id]["median_p99_ms"]
                busy99 = summary[config_id]["median_read_p99_ms"]
                lines.append(
                    f"- {config_id}: Test 1 list c={level} p50={num(idle)} ms p99={num(idle99)} ms; "
                    f"mixed 16 readers p50={num(busy)} ms p99={num(busy99)} ms."
                )
    return "\n".join(lines) + "\n"


def build(meta, partial, teardown):
    choice = load_choice()
    sweep = refresh_sweep() if file_done(PARITY_SWEEP) or any((RAW / "sweep").glob("*.json")) else None
    test1 = refresh_test1() if file_done(CHOICE_PATH) else None
    writes = _refresh_writes() if file_done(CHOICE_PATH) else None
    mixed = _refresh_mixed() if file_done(CHOICE_PATH) else None
    peaks = collect_peaks()
    images = (meta or {}).get("images") or {}
    alerta = images.get("alerta-bench:9.1.0") or {}
    alrust = images.get("alrust:alpha") or {}
    pg = images.get("postgres:18.7") or {}
    uwsgi = images.get("alerta-bench-uwsgi:9.1.0") or {}
    chosen = None if not choice else choice.get("chosen_config")
    lines = []
    title = "TUNED like-for-like benchmark" if not partial else "TUNED like-for-like benchmark (partial)"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        "This is the tuned like-for-like laboratory run of upstream Alerta 9.1.0 against alrust. "
        "Each application server had 4 cores (cpuset 0-3). PostgreSQL had its own 2 cores (cpuset 4-5). "
        "The load generator and the connection sampler ran on the remaining cores (taskset 6-7). "
        "One application server was under load at a time."
    )
    lines.append("")
    lines.append("## Subjects")
    lines.append("")
    lines.append(f"- Alerta image `alerta-bench:9.1.0` id `{alerta.get('id')}`, commit `{ALERTA_COMMIT}`. Image command is gunicorn with 1 gthread worker and 4 threads; the sweep and the chosen run override that command. The base image was not modified.")
    lines.append(f"- Derived image `alerta-bench-uwsgi:9.1.0` id `{uwsgi.get('id')}`, parent `{uwsgi.get('parent')}`. It adds uWSGI {((meta or {}).get('uwsgi_version'))} and nothing else. Command: `{redact(' '.join(uwsgi_cmd()))}`.")
    lines.append(f"- alrust image `alrust:alpha` id `{alrust.get('id')}`, commit `{ALRUST_COMMIT}`. Binary SHA256 `{ALRUST_BINARY_SHA}`. Default config only. There is no pool-size knob; the serving pool in this binary is 16. `ALRUST_HOUSEKEEPING_INTERVAL=0`. `ALRUST_AUTH_REQUIRED` was left at its default true.")
    lines.append(f"- PostgreSQL image `postgres:18.7` digest `{PG_DIGEST}` id `{pg.get('id')}`. `docker run`, image defaults, no custom `postgresql.conf`.")
    lines.append(f"- Load tool: oha `{(meta or {}).get('oha_version')}` at `/tmp/oha`. Host nproc `{(meta or {}).get('nproc')}`. Docker `{(meta or {}).get('docker_version')}`.")
    lines.append("")
    if choice:
        lines.append(
            f"Gunicorn sweep workers were 4, 8, and 16, each with `--worker-class gthread --threads 4`. "
            f"Chosen configuration: `{chosen}`. {choice.get('reason')}."
        )
    else:
        lines.append("The Gunicorn sweep has not chosen a worker count.")
    lines.append("")
    lines.append(
        "alrust was not tuned. Tokio ran on the 4 cores given to the container. "
        "No pool-size variable was set."
    )
    lines.append("")
    lines.append(
        "These figures are a separate result from the 13 November 25%-of-one-CPU cap, "
        "the 15 November single-client medians, and the later uncapped 8-vCPU full run."
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "Disposable databases were created with `docker run` on the host network. "
        "A schema-and-key template (`base_empty`) was cloned to `base_seed`, which received "
        "1,000 synthetic alerts through the Alerta API (`seed.py`). Every read configuration "
        "got its own `CREATE DATABASE ... TEMPLATE base_seed` clone. Test 1 did not write alerts. "
        "Test 2 recreated a working database from `base_empty` or `base_seed` before every pilot and every repetition. "
        "Test 1 databases were not reused for writes."
    )
    lines.append("")
    lines.append(
        "Core map, checked with `docker inspect` (`NanoCpus`, `CpusetCpus`) and, inside each container, "
        "`nproc`, `/sys/fs/cgroup/cpuset.cpus.effective`, and `cpu.max`: application `--cpus=4 --cpuset-cpus=0-3`; "
        "PostgreSQL `--cpus=2 --cpuset-cpus=4-5`; oha and the in-process write client `taskset -c 6-7`. "
        "The runner re-execs itself under that taskset. oha also sets `--worker-threads 2`. "
        "Dockerd and other host daemons stayed unpinned."
    )
    lines.append("")
    lines.append(
        "Each timed cell is written as soon as it finishes. A later start skips a cell whose timed JSON "
        "exists, is non-empty, and parses. Filenames include the configuration id. "
        "Summaries are rebuilt from those files after each timed cell. "
        "The rotation of which configuration runs first is balanced across the matrix "
        "(each of three configurations is first on 20 of the 60 Test 1 groups, and on 6 of the 18 write groups). "
        "Because the idle server is removed, a rotation step starts the next server, runs a discarded 5 second warmup, then the timed measurement."
    )
    lines.append("")
    lines.append(
        "Read load is oha HTTP/1.1 with keep-alive (`--disable-keepalive` was not set), "
        "`-z 30s -w`, request timeout 60s, authenticated GET. "
        "Requests/sec is `summary.requestsPerSec`. p50 and p99 are `latencyPercentiles` seconds times 1000. "
        "error_count is 0 when `errorDistribution` is empty. "
        "The median of three repetitions is the middle value."
    )
    lines.append("")
    lines.append(
        "Writes use the copied closed-loop client: one keep-alive connection per worker, HTTP/1.1, "
        "indexes isolated by worker. Each 10 indexes are 4 new, 3 duplicate, and 3 correlate. "
        "A shared sequence is sized from a fresh 5 second pilot on the slowest of the final configurations "
        "so that configuration's timed phase is about 30 seconds. The same counts are then offered to every configuration. "
        "alerts/sec counts HTTP 201 responses divided by the timed wall clock. "
        "p50 and p99 are nearest-rank percentiles of those 201 latencies. "
        "Safety deadlines are 180 seconds for warmup and 300 seconds for the timed sequence; a phase that hits the deadline is truncated and is not scored."
    )
    lines.append("")
    lines.append(
        "During every timed run, a thread on cores 6-7 opens a short-lived database connection about every 0.2 seconds. "
        "The count excludes `pg_backend_pid()`. Peak total is the maximum number of other backends on that database. "
        "Peak active is the maximum with `state = 'active'`. Peak client counts `backend_type = 'client backend'` and is reported beside the required totals. "
        "The sampler connection is not part of the peak. "
        "Natural models, which were not capped: uWSGI 5 processes; Gunicorn N workers times 4 threads; alrust serving pool 16. "
        "Alerta opens one Postgres connection per request and closes it when the request ends."
    )
    lines.append("")
    lines.append(
        "Correctness: before Test 1 scoring, every pair of final configurations was compared on each query "
        "(ordered ids for lists, count payload for `/alerts/count`). A mismatch unscores that case. "
        "The sweep applied the same check to the newest-50 query across workers 4, 8, and 16. "
        "After each write repetition, `alert_rows`, `SUM(duplicate_count)`, and `cardinality(history)` had to match. "
        "A mismatch unscores that run. API samples of new, duplicate, and correlate responses are a gate for write scoring."
    )
    lines.append("")
    lines.append("## Gunicorn sweep")
    lines.append("")
    lines.append("Case `/alerts?sort-by=lastReceiveTime&page-size=50`, 32 clients, 3 repetitions, 5 second warmup, 30 second timed run.")
    lines.append("")
    if sweep:
        lines.append(sweep_table(sweep, choice))
        lines.append("")
        lines.append(sweep.get("summary_rule", ""))
        lines.append("")
        for workers, cell in sweep["points"].items():
            lines.append(
                f"Workers {workers} repetition rps={json.dumps(cell['rps'])} "
                f"p50_ms={json.dumps(cell['p50_ms'])} p99_ms={json.dumps(cell['p99_ms'])} "
                f"error_count={json.dumps(cell['error_count'])} non_2xx={json.dumps(cell['non_2xx'])}."
            )
    else:
        lines.append("The sweep did not produce a summary.")
    lines.append("")
    lines.append("## Test 1 reads")
    lines.append("")
    lines.append(
        "Medians below are the middle of the three timed repetitions. "
        "The same numbers are in `test1-reads.json` under `summary`. "
        "No display rounding is applied; each median is printed with `json.dumps`."
    )
    lines.append("")
    if test1:
        lines.append(test1_tables(test1))
    else:
        lines.append("Test 1 did not produce a summary.")
    lines.append("")
    lines.append("## Test 2 writes")
    lines.append("")
    if writes:
        lines.append(f"API compatible: {num(writes['api_compatible'])}. {writes.get('api_compatible_reason')}")
        lines.append("")
        lines.append(write_tables(writes))
    else:
        lines.append("Test 2 writes did not produce a summary.")
    lines.append("")
    lines.append("## Mixed load")
    lines.append("")
    lines.append("8 writers and 16 readers. Readers use `/alerts?sort-by=lastReceiveTime&page-size=50`. Databases start from `base_seed`. The write sequence is the seeded concurrency-8 sequence.")
    lines.append("")
    if mixed:
        lines.append(mixed_table(mixed, test1))
    else:
        lines.append("The mixed test did not produce a summary.")
    lines.append("")
    lines.append("## Peak database connections")
    lines.append("")
    lines.append(peak_table(peaks))
    lines.append("")
    lines.append("## Unscored cases")
    lines.append("")
    lines.append(unscored_lines(sweep, test1, writes, mixed))
    lines.append("")
    lines.append("## Deviations")
    lines.append("")
    lines.append("- Dockerd and other host daemons were unpinned. Only the application, PostgreSQL, and the load generator were pinned.")
    lines.append("- One application container was running during a timed cell. The idle peer was removed. Each rotation step therefore includes a process start, then the 5 second warmup.")
    lines.append("- uWSGI was reached directly on `127.0.0.1:8081` at `/alerts`. nginx and the `/api` prefix from the Alerta tutorial and from docker-alerta were not in the path.")
    lines.append("- `UWSGI_MAX_WORKER_LIFETIME` was not set.")
    lines.append("- uWSGI listens with `--http` (its HTTP router) plus `--http-keepalive`, not `--http-socket`. A 5 second probe of `--http-socket` with the same processes=5 and the same oha keep-alive client produced connection errors at concurrency 1 and 32 (`connection closed before message completed`, `operation was canceled`). The HTTP router completed those probes with zero errors. `--need-app` is set. `--enable-threads` is the stub uWSGI 2.0.28 already enables and is not `--threads`. `--py-call-uwsgi-fork-hooks` is set because sentry_sdk warns that prefork without it can crash workers. `--lazy-apps` is present only if `raw/uwsgi-cmd.json` records that the first smoke failed without it.")
    lines.append("- The uWSGI image is a child of `alerta-bench:9.1.0` that only installs uWSGI 2.0.28. Application code and the base image were not changed.")
    lines.append("- oha `--worker-threads 2` matches the two load-generator cores. The rest of the oha invocation matches the full-run flags (`--http-version 1.1`, `-z`, `-c`, `-w`, `-t 60s`, keep-alive, JSON output).")
    lines.append("- Alerta `AUTH_REQUIRED` remains the image default false. Every timed request still sends the admin key. alrust authentication remains the default true. The same disposable key is in both.")
    lines.append("- PostgreSQL `max_connections` and the other image defaults were left alone. Alerta was not capped at 16 connections.")
    lines.append("- Schema creation and the 1,000-alert seed used the image's default command, one gthread worker. Several workers at once race `CREATE TYPE history` on an empty database. Timed cells did not use that one-worker command.")
    lines.append("")
    lines.append("## What this does not establish")
    lines.append("")
    lines.append(
        "It does not establish a production deployment result. There is no nginx, no TLS, no multi-host network, "
        "and no long-lived soak. Three repetitions on one 8-vCPU machine do not provide a confidence interval. "
        "It does not establish a tuned alrust, because alrust has no pool or worker knob in this binary and none was added. "
        "It does not establish the losing Gunicorn worker counts on the full workload; only the sweep case was run for those. "
        "It does not establish behavior under a different alert mix, a larger database, or a custom PostgreSQL configuration. "
        "Parity covers the five read queries and the write aggregates that were checked, and nothing beyond those checks. "
        "Unpinned dockerd can still run on the application cores."
    )
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append("- `environment.txt` records the host, image ids, command lines with secrets removed, pins, and the sampler method.")
    lines.append("- `sweep.json`, `sweep.csv`, `raw/sweep/`, `parity-sweep.json`, `raw/chosen-gunicorn.json`.")
    lines.append("- `test1-reads.json`, `test1-reads.csv`, `raw/test1/`, `raw/test1-warmup/`, `parity-test1.json`.")
    lines.append("- `test2-writes.json`, `test2-writes.csv`, `raw/test2/`, `test2-mixed.json`, `test2-mixed.csv`, `raw/test2-mixed/`.")
    lines.append("- `raw/progress.txt` is the run log.")
    if teardown:
        lines.append(
            f"- Teardown removed `{teardown.get('removed')}`, remaining `{teardown.get('remaining')}`. "
            f"Docker names still present: `{teardown.get('docker_ps_names')}`. Git was not invoked."
        )
    else:
        lines.append("- Teardown has not run. Git was not invoked.")
    if partial:
        lines.append("")
        lines.append("This report is partial. Missing cells were not backfilled.")
    text = "\n".join(lines) + "\n"
    if "<redacted>" in text:
        pass
    (ROOT / "REPORT.md").write_text(text, encoding="utf-8")
    return ROOT / "REPORT.md"


def scrub_workspace_secrets():
    from common import API_KEY, PG_PASSWORD
    changed = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".py", ".log"}:
            continue
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        new = text
        if API_KEY:
            new = new.replace(API_KEY, "<redacted>")
        if PG_PASSWORD:
            new = new.replace(PG_PASSWORD, "<redacted>")
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(str(path.relative_to(ROOT)))
    return changed
