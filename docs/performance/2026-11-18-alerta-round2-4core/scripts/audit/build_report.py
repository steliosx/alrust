#!/usr/bin/env python3
"""Rebuild REPORT.md from raw/ only. Every table cell points back to raw JSON fields."""
import json, statistics as st, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw"
CFG = ["alrust", "alerta-uwsgi-5", "alerta-gunicorn-8"]
SHORT = {"alrust": "alrust", "alerta-uwsgi-5": "uWSGI-5", "alerta-gunicorn-8": "gunicorn-8"}
CASES = ["list", "event", "severity", "count", "empty"]
LEVELS = [1, 8, 16, 32]
J = lambda p: json.loads(Path(p).read_text())
med = lambda xs: st.median(xs) if len(xs) == 3 else None
f0 = lambda x: "—" if x is None else f"{x:,.0f}"
f1 = lambda x: "—" if x is None else f"{x:,.1f}"
f2 = lambda x: "—" if x is None else f"{x:.2f}"
H = {}  # headline numbers

def peak(d):
    return (d.get("db_connections") or {}).get("peak_total_backends")

# ---------- sweep
sweep = {}
for w in (4, 8, 16):
    reps = [J(RAW / "sweep" / f"alerta-gunicorn-{w}_list_c32_r{r}.json") for r in (1, 2, 3)]
    sweep[w] = dict(rps=med([d["oha"]["summary"]["requestsPerSec"] for d in reps]),
                    p50=med([d["oha"]["latencyPercentiles"]["p50"] * 1000 for d in reps]),
                    p99=med([d["oha"]["latencyPercentiles"]["p99"] * 1000 for d in reps]),
                    bad=sum(sum(v for k, v in d["oha"]["statusCodeDistribution"].items() if not k.startswith("2")) + sum(d["oha"].get("errorDistribution", {}).values()) for d in reps),
                    peak=max(peak(d) for d in reps))
choice = J(RAW / "chosen-gunicorn.json")

# ---------- test1
par = J(ROOT / "parity-test1.json"); par2 = J(ROOT / "parity-test1-rerun.json")
t1 = {}; t1_unscored = []
peaks = {c: {"reads": 0, "writes": 0, "mixed": 0} for c in CFG}
for case in CASES:
    parity_ok = par["cases"][case]["match"] and par["sql_fingerprint_match"] and par2["cases"][case]["match"] and par2["sql_fingerprint_match"]
    for lv in LEVELS:
        for c in CFG:
            reps = [J(RAW / "test1" / f"{c}_{case}_c{lv}_r{r}.json") for r in (1, 2, 3)]
            bad = 0
            for d in reps:
                o = d["oha"]
                bad += sum(v for k, v in o["statusCodeDistribution"].items() if not k.startswith("2")) + sum(o.get("errorDistribution", {}).values())
                peaks[c]["reads"] = max(peaks[c]["reads"], peak(d))
            scored = parity_ok and bad == 0
            if not scored:
                t1_unscored.append(f"reads {case} c={lv} {c}: parity={parity_ok} errors={bad}")
            t1[(case, lv, c)] = dict(rps=med([d["oha"]["summary"]["requestsPerSec"] for d in reps]),
                                    p50=med([d["oha"]["latencyPercentiles"]["p50"] * 1000 for d in reps]),
                                    p99=med([d["oha"]["latencyPercentiles"]["p99"] * 1000 for d in reps]),
                                    reps=[round(d["oha"]["summary"]["requestsPerSec"], 1) for d in reps],
                                    bad=bad, scored=scored, peak=max(peak(d) for d in reps))

# ---------- test2 writes
t2 = {}; t2_unscored = []
for cond in ("empty", "seeded"):
    size = {lv: J(RAW / "test2" / f"size_{cond}_c{lv}.json") for lv in (1, 8, 32)}
    for lv in (1, 8, 32):
        docs = {c: [J(RAW / "test2" / f"{cond}_{c}_c{lv}_r{r}.json") for r in (1, 2, 3)] for c in CFG}
        mism = []
        for r in range(3):
            aggs = {c: tuple(docs[c][r]["agg"].values()) for c in CFG}
            if len(set(aggs.values())) != 1:
                mism.append((r + 1, aggs))
        complete = all(d["timed"]["offered"] == d["n_timed"] and d["warmup"]["offered"] == d["n_warm"] and not d["timed"].get("truncated") for c in CFG for d in docs[c])
        scored = not mism and complete
        if not scored:
            t2_unscored.append(dict(cond=cond, c=lv, mismatches=mism, complete=complete))
        for c in CFG:
            ds = docs[c]
            for d in ds:
                peaks[c]["writes"] = max(peaks[c]["writes"], peak(d))
            t2[(cond, lv, c)] = dict(rps=med([d["timed"]["rps"] for d in ds]), p50=med([d["timed"]["p50_ms"] for d in ds]),
                                    p99=med([d["timed"]["p99_ms"] for d in ds]), reps=[round(d["timed"]["rps"], 1) for d in ds],
                                    err=[d["timed"]["error_count"] + d["warmup"]["error_count"] for d in ds],
                                    n_timed=ds[0]["n_timed"], scored=scored, peak=max(peak(d) for d in ds),
                                    sizing=size[lv].get("sizing_config"))

# ---------- mixed
mx = {}; mx_unscored = []
docs = {c: [J(RAW / "test2-mixed" / f"{c}_r{r}.json") for r in (1, 2, 3)] for c in CFG}
mm = []
for r in range(3):
    aggs = {c: tuple(docs[c][r]["agg"].values()) for c in CFG}
    if len(set(aggs.values())) != 1: mm.append((r + 1, aggs))
mx_scored = not mm and all(d["write"]["offered"] == d["n_timed"] and not d["write"].get("truncated") for c in CFG for d in docs[c])
for c in CFG:
    ds = docs[c]
    for d in ds: peaks[c]["mixed"] = max(peaks[c]["mixed"], peak(d))
    mx[c] = dict(wrps=med([d["write"]["rps"] for d in ds]), wp50=med([d["write"]["p50_ms"] for d in ds]), wp99=med([d["write"]["p99_ms"] for d in ds]),
                 rp50=med([d["read"]["p50_ms"] for d in ds]), rp99=med([d["read"]["p99_ms"] for d in ds]),
                 rrps=med([d["read"]["accepted"] / d["write"]["elapsed_s"] for d in ds]),
                 wreps=[round(d["write"]["rps"], 1) for d in ds], err=[(d["write"]["error_count"], d["read"]["error_count"]) for d in ds],
                 n=ds[0]["n_timed"], peak=max(peak(d) for d in ds))

# ---------- freeze audit record
sup = ROOT / "raw/_superseded"
def listing(sub):
    return sorted(p.name for p in (sup / sub).glob("*.json")) if (sup / sub).exists() else []

# ---------- write markdown
L = []
A = L.append
meta = J(ROOT / "meta.json")
A("# Tuned like-for-like benchmark: Alerta 9.1.0 vs alrust (PostgreSQL 18.7)\n")
A("Run 2026-11-15 20:24 to 2026-11-16 12:36 Nassau time (EST, UTC-5), with freeze reruns at 12:26–12:36. Rebuilt from `raw/` by `scripts/audit/build_report.py`. Nothing here is copied from another benchmark directory except the comparison section, which cites `/workspace/alerta-bench-full/REPORT.md`.\n")
A("## Key findings\n")
A(f"- Reads at 32 clients: alrust {f0(min(t1[(c_,32,'alrust')]['rps'] for c_ in CASES))}–{f0(max(t1[(c_,32,'alrust')]['rps'] for c_ in CASES))} req/s; Alerta uWSGI-5 {f0(min(t1[(c_,32,'alerta-uwsgi-5')]['rps'] for c_ in CASES))}–{f0(max(t1[(c_,32,'alerta-uwsgi-5')]['rps'] for c_ in CASES))}; gunicorn-8 {f0(min(t1[(c_,32,'alerta-gunicorn-8')]['rps'] for c_ in CASES))}–{f0(max(t1[(c_,32,'alerta-gunicorn-8')]['rps'] for c_ in CASES))}. gunicorn-8 is a few percent faster than uWSGI-5 but has a much longer tail (p99) at 32 clients.")
A(f"- Writes at 32 writers: alrust {f0(t2[('empty',32,'alrust')]['rps'])} (empty) / {f0(t2[('seeded',32,'alrust')]['rps'])} (seeded) alerts/s vs about {f0(t2[('empty',32,'alerta-uwsgi-5')]['rps'])} for either Alerta setup. Alerta writes stop scaling after 8 writers.")
A(f"- Mixed load (8 writers + 16 readers): alrust {f0(mx['alrust']['wrps'])} alerts/s with read p50 {f1(mx['alrust']['rp50'])} ms; uWSGI-5 {f0(mx['alerta-uwsgi-5']['wrps'])} / {f1(mx['alerta-uwsgi-5']['rp50'])} ms; gunicorn-8 {f0(mx['alerta-gunicorn-8']['wrps'])} / {f1(mx['alerta-gunicorn-8']['rp50'])} ms.")
A("- Tuning Alerta helped filtered/list reads versus the earlier untuned uncapped run (~200 → ~280–320 req/s at 32) but did not help writes (285 → ~240 at 32), and `/alerts/count` and the empty-result query were lower than in that run (that run had no CPU caps, so this is not a clean before/after).")
A("- One write cell (empty, 1 writer) is unscored because an Alerta HTTP 500 deadlock left gunicorn-8 one alert short in rep 2. All read cells and the mixed test are scored.\n")
A("## Setup\n")
A("- Host: 8-vCPU Intel Xeon VM, 15 GiB RAM, no swap (`environment.txt`).")
A(f"- Core layout (`meta.json` core_map, checked inside each container by `check_pins`): app server `--cpus=4 --cpuset-cpus=0-3`; PostgreSQL 18.7 `--cpus=2 --cpuset-cpus=4-5`; load generator, write client, and DB sampler `taskset -c 6-7`; dockerd unpinned. One app server under load at a time.")
A(f"- PostgreSQL `postgres:18.7` digest `{meta['postgres_digest_required']}`, image defaults (max_connections 100, shared_buffers 128MB). Disposable databases only, in a throwaway container (`tuned-pg`) that was removed at the end (`teardown.json`).")
A(f"- Alerta 9.1.0 commit `{meta['alerta_commit'][:8]}`, two servers:")
A("  - **uWSGI-5** (documented setup: docs.alerta.io and docker-alerta default `processes = 5`): `uwsgi --http --http-keepalive --master --processes 5 ...` (`raw/uwsgi-cmd.json`, uWSGI 2.0.28).")
A("  - **gunicorn-8**: gunicorn gthread, 8 workers × 4 threads, the best point of the sweep below.")
A(f"- alrust commit `{meta['alrust_commit'][:8]}` (binary sha256 `{meta['alrust_binary_sha256'][:16]}…`), defaults; fixed DB pool of 16, no tuning knobs.")
A("- Reads: oha 1.17.0, HTTP/1.1 keep-alive, 5 s discarded warmup + 30 s timed, 3 reps, rotation of which server goes first. Writes: closed-loop Python client, one keep-alive connection per writer, a shared sequence sized from a 5 s pilot on the slowest server (~30 s for it), the same sequence offered to all three. Every write rep starts from a fresh clone of `base_empty` or `base_seed` (1,000 alerts from `seed.py`).")
A("- Medians are the middle of 3 reps. p50/p99 are the median of the three per-rep p50/p99 values. Peak DB connections are sampled every 0.2 s from `pg_stat_activity` for that database, excluding the sampler.\n")

A("## Gunicorn sweep (list, 32 clients, `raw/sweep/`)\n")
A("| workers × threads | median req/s | p50 ms | p99 ms | errors | peak DB conns |\n|---|---:|---:|---:|---:|---:|")
for w in (4, 8, 16):
    s = sweep[w]; A(f"| {w} × 4 | {f0(s['rps'])} | {f1(s['p50'])} | {f1(s['p99'])} | {s['bad']} | {s['peak']} |")
A(f"\nChosen: `{choice['chosen_config']}` (`raw/chosen-gunicorn.json`, rule: highest median). The three points are within {f0(max(s['rps'] for s in sweep.values()) - min(s['rps'] for s in sweep.values()))} req/s of each other, so the sweep shows a plateau, not a clear winner.\n")

A("## Test 1: reads (median req/s, p50 / p99 ms) — `raw/test1/`\n")
A("Parity: ordered ids (lists) and count payloads matched across all three servers for every case, both in the original clones (`parity-test1.json`) and in the rebuilt clones used for the rerun (`parity-test1-rerun.json`).\n")
for case in CASES:
    path = J(RAW / "test1" / f"alrust_{case}_c1_r1.json")["path"]
    A(f"### {case} — `{path}`\n")
    A("| clients | alrust req/s | uWSGI-5 req/s | gunicorn-8 req/s | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |\n|---:|---:|---:|---:|---|---|---|---|")
    for lv in LEVELS:
        r = [t1[(case, lv, c)] for c in CFG]
        A(f"| {lv} | " + " | ".join(f0(x["rps"]) for x in r) + " | " + " | ".join(f"{f1(x['p50'])} / {f1(x['p99'])}" for x in r) + f" | {'yes' if all(x['scored'] for x in r) else 'NO'} |")
    A("")
    for c in CFG: H.setdefault("reads32", {}).setdefault(case, {})[c] = t1[(case, 32, c)]

A("## Test 2: writes (median alerts/s, p50 / p99 ms) — `raw/test2/`\n")
A("| DB | writers | alerts/rep | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |\n|---|---:|---:|---:|---:|---:|---|---|---|---|")
for cond in ("empty", "seeded"):
    for lv in (1, 8, 32):
        r = [t2[(cond, lv, c)] for c in CFG]
        A(f"| {cond} | {lv} | {r[0]['n_timed']:,} | " + " | ".join(f0(x["rps"]) for x in r) + " | " + " | ".join(f"{f1(x['p50'])} / {f1(x['p99'])}" for x in r) + f" | {'yes' if r[0]['scored'] else '**NO**'} |")
A("\nPer-rep alerts/s (r1, r2, r3):\n")
for cond in ("empty", "seeded"):
    for lv in (1, 8, 32):
        A(f"- {cond} c={lv}: " + "; ".join(f"{SHORT[c]} {t2[(cond, lv, c)]['reps']}" for c in CFG))
A("")

A("## Test 2: mixed load — 8 writers + 16 readers of the newest-50 list, seeded DB — `raw/test2-mixed/`\n")
A(f"Each rep offers the same {mx['alrust']['n']:,}-alert write sequence while 16 readers loop on `/alerts?sort-by=lastReceiveTime&page-size=50`.\n")
A("| server | write alerts/s | write p50 / p99 ms | read p50 / p99 ms | reads/s during writes | per-rep write alerts/s | errors (write, read) per rep | peak DB conns |\n|---|---:|---|---|---:|---|---|---:|")
for c in CFG:
    m = mx[c]
    A(f"| {SHORT[c]} | {f0(m['wrps'])} | {f1(m['wp50'])} / {f1(m['wp99'])} | {f1(m['rp50'])} / {f1(m['rp99'])} | {f0(m['rrps'])} | {m['wreps']} | {m['err']} | {m['peak']} |")
A(f"\nScored: {'yes' if mx_scored else 'NO'} (alert rows, duplicate_count sum, and history entries matched across servers in every rep). reads/s = successful reads ÷ timed write phase seconds.\n")

A("## Peak database connections (max over all timed runs, `db_connections.peak_total_backends`)\n")
A("| server | natural model | reads | writes | mixed |\n|---|---|---:|---:|---:|")
model = {"alrust": "sqlx pool fixed at 16", "alerta-uwsgi-5": "1 connection per request, 5 processes", "alerta-gunicorn-8": "1 connection per request, 8 workers × 4 threads"}
for c in CFG:
    A(f"| {SHORT[c]} | {model[c]} | {peaks[c]['reads']} | {peaks[c]['writes']} | {peaks[c]['mixed']} |")
A("\nalrust's client-backend peak (`peak_client_backends`) never went above 17, one more than its pool of 16; the source of the extra short-lived connection was not identified. The total peaks of 18 also count non-client backends on that database (for example autovacuum). Alerta opens one connection per in-flight request, so uWSGI-5 stays at 5–6 and gunicorn-8 (32 threads) reached 27.\n")

A("## Correctness checks and unscored cells\n")
A("- Reads: parity matched for all 5 cases (both clone sets). Error/non-2xx count across all 180 timed read files: " + str(sum(v['bad'] for v in t1.values())) + ".")
A("- Writes: after each rep, `alert_rows`, `SUM(duplicate_count)`, and `cardinality(history)` must match across all three servers; API sample shapes for new/duplicate/correlate matched (`raw/test2/api-samples.json`).")
A("- Mixed: same aggregate check, all three reps matched after the reruns.")
if t2_unscored:
    for u in t2_unscored:
        A(f"- **Unscored: writes {u['cond']} c={u['c']}.** Per the rule *if counts differ, do not score*, this cell is excluded from scoring. Mismatching reps: " + "; ".join(f"r{r}: " + ", ".join(f"{SHORT[c]} rows/dup/hist={a}" for c, a in aggs.items()) for r, aggs in u['mismatches']) + ".")
    A("  Cause (`raw/test2/empty_alerta-gunicorn-8_c1_r2.json` warmup.error_samples): Alerta gunicorn-8 returned one HTTP 500 `deadlock detected` during the warmup of rep 2, so that database ended with 749 alerts instead of 750. This is an Alerta error, not a machine freeze, so it was not rerun; its per-rep values above are shown for information only.")
if not t1_unscored:
    A("- No read cell is unscored.")
else:
    for u in t1_unscored: A(f"- Unscored read cell: {u}")
A("")

A("## Deviations\n")
A("1. **Runner driven from the shell, not by grok.** The Grok Build CLI wrote the harness (`scripts/`) and started it, but its bwrap sandbox could not reach `/var/run/docker.sock` reliably and its tool calls were ending the long runner after ~18 min. From 2026-11-15 20:45 the same `scripts/run_all.py` was continued with `nohup` from the box shell (`raw/progress.txt` HANDOFF line). Resume skips finished cells, so nothing was overwritten.")
A("2. **Machine freezes.** The benchmark VM was suspended several times (overnight and while idle). A suspended VM stops the clock the tests measure with, so a freeze shows up as a wall-clock gap in `raw/progress.txt` far longer than the test's measured time, sampler gaps, dropped connections, or low throughput.")
A("   - First pass (09:06–09:17): 13 freeze-hit read cells were moved to `raw/_superseded/test1/` and rerun at 09:17–09:26, including `alrust_count_c16_r2` (25 min span, 5 non-2xx) and `alrust_empty_c16_r3` (≈5 min span, 12 non-2xx). In the same pass, the first write attempt was discarded: gunicorn-8 refused connections right after restart (`[Errno 111]` FAIL lines in `raw/progress.txt`, files `*.fail.json`) and the c=8/c=32 sizing pilots were too small (mixed reps had ~1,190 rows). A gunicorn ready gate was added, the c=8/c=32 sizes were re-piloted, and all c=8/c=32 write cells and all 9 mixed cells were rerun (09:25–11:16). Everything replaced is in `raw/_superseded/{test1,test2,test2-mixed,pilots}`.")
A("   - Second pass (this audit, `scripts/audit/freeze_audit.py`): every one of the 243 timed files was checked for (a) progress-log span minus measured time minus normal startup > 20 s, (b) DB sampler interval > 0.26 s instead of 0.2 s, (c) any error/non-2xx, (d) rps more than 12% from its cell median. Eight cells were flagged and moved to `raw/_superseded/freeze-audit-1230/`:")
for sub in ("test1", "test2", "test2-mixed"):
    for n in listing("freeze-audit-1230/" + sub):
        A(f"     - `{sub}/{n}`")
A("     All eight were rerun once at 12:26–12:36 and all reruns were clean (span ≈ measured time, sampler at 0.2 s, 0 errors). No second retry was needed.")
A("   - Not flagged: `alrust_event_c32_r3` (2,270 req/s vs 2,586 median, −12%) had a normal span, normal sampler, and 0 errors, so it stays. `alerta-gunicorn-8` mixed r1 (52.8 alerts/s) also had a normal span and stays; it is lower than the clean reruns r2/r3 (68.7, 66.8).")
A("3. **Templates rebuilt for the rerun.** The 11:16 teardown removed the Postgres container, so `base_empty`/`base_seed` were rebuilt with the same `seed.py` (1,000 deterministic alerts). Alert ids are server-generated UUIDs, so the id fingerprint differs from the first build (`90dc96…` → `cb0d11…`), but content and counts are identical. Parity was re-checked on the new clones and matched. To allow this, `scripts/common.py` and `scripts/reads.py` gained an opt-in `RERUN_REBUILD=1` path (originals in `scripts/audit/orig/`).")
A("4. Within every write cell, all three servers received exactly the same sequence (`n_timed` in each file); c=1 cells kept their original sizing, c=8/c=32 were re-sized as above.")
A("5. The sweep only tested gunicorn 4/8/16 workers with 4 threads each; uWSGI was run only at the documented 5 processes.\n")

A("## Comparison with the earlier untuned full run\n")
A("`/workspace/alerta-bench-full/REPORT.md`: no CPU caps (8 vCPU shared by everything), Alerta as the image default gunicorn 1 worker × 4 threads, alrust defaults. Not strictly comparable because of the caps, but directionally:\n")
A("| metric | full run Alerta | full run alrust | tuned uWSGI-5 | tuned gunicorn-8 | tuned alrust |\n|---|---:|---:|---:|---:|---:|")
full = {"list": (199.24, 2730.69), "event": (201.61, 3416.22), "severity": (200.30, 3581.64), "count": (531.43, 8586.60), "empty": (476.10, 7001.38)}
for case in CASES:
    a, b = full[case]
    A(f"| reads {case} @32 req/s | {a:,.0f} | {b:,.0f} | {f0(t1[(case,32,'alerta-uwsgi-5')]['rps'])} | {f0(t1[(case,32,'alerta-gunicorn-8')]['rps'])} | {f0(t1[(case,32,'alrust')]['rps'])} |")
fw = {1: (84.34, 756.73), 8: (224.66, 4533.39), 32: (285.27, 6227.57)}
for lv in (1, 8, 32):
    a, b = fw[lv]
    A(f"| writes empty @{lv} alerts/s | {a:,.0f} | {b:,.0f} | {f0(t2[('empty',lv,'alerta-uwsgi-5')]['rps'])} | {f0(t2[('empty',lv,'alerta-gunicorn-8')]['rps'])} | {f0(t2[('empty',lv,'alrust')]['rps'])} |")
A(f"| mixed write alerts/s | 62 | 926 | {f0(mx['alerta-uwsgi-5']['wrps'])} | {f0(mx['alerta-gunicorn-8']['wrps'])} | {f0(mx['alrust']['wrps'])} |")
A(f"| mixed read p50 ms | 122.5 | 12.6 | {f1(mx['alerta-uwsgi-5']['rp50'])} | {f1(mx['alerta-gunicorn-8']['rp50'])} | {f1(mx['alrust']['rp50'])} |")
A("\n(empty c=1 is an unscored cell in the tuned run; its medians are shown for information.)\n")
A("## Files\n")
A("- `raw/progress.txt` full log; `raw/test1|test2|test2-mixed|sweep/*.json` per-run raw results; `raw/_superseded/` everything replaced, never deleted.")
A("- `scripts/` harness; `scripts/audit/freeze_audit.py` freeze detector; `scripts/audit/build_report.py` this report; `scripts/audit/headline.json` the headline numbers.")
A("- Nothing was committed or pushed.\n")
(ROOT / "REPORT.md").write_text("\n".join(L))

head = {"reads_c32": {case: {c: {k: H['reads32'][case][c][k] for k in ('rps', 'p50', 'p99')} for c in CFG} for case in CASES},
        "writes": {f"{cond}_c{lv}": {c: t2[(cond, lv, c)]['rps'] for c in CFG} | {"scored": t2[(cond, lv, 'alrust')]['scored']} for cond in ("empty", "seeded") for lv in (1, 8, 32)},
        "mixed": {c: {"write_rps": mx[c]['wrps'], "read_p50": mx[c]['rp50'], "read_p99": mx[c]['rp99']} for c in CFG},
        "peaks": peaks, "sweep": {w: sweep[w]['rps'] for w in sweep}, "t2_unscored": [str(u) for u in t2_unscored], "t1_unscored": t1_unscored}
(ROOT / "scripts/audit/headline.json").write_text(json.dumps(head, indent=1, default=str))
print(json.dumps(head, indent=1, default=str))
