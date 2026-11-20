#!/usr/bin/env python3
"""Build REPORT.md and scripts/audit/headline.json for round 2 from raw/ only.
The comparison section reads the previous run's headline.json
(/workspace/alerta-bench-tuned/scripts/audit/headline.json), which that run built from its raw/.
Missing raw files show as "missing"; no number is filled in by hand."""
import json
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw"
PREV = Path("/workspace/alerta-bench-tuned/scripts/audit/headline.json")
CFG = ["alrust", "alerta-uwsgi-5", "alerta-gunicorn-8"]
SHORT = {"alrust": "alrust", "alerta-uwsgi-5": "uWSGI-5", "alerta-gunicorn-8": "gunicorn-8",
         "alrust-p32": "alrust-p32", "alrust-p64": "alrust-p64"}
CASES = ["list", "event", "severity", "count", "empty"]
LEVELS = [1, 8, 16, 32]
WLEVELS = [1, 8, 16, 32]
ARMS = ["alrust", "alrust-p32", "alrust-p64", "alerta-uwsgi-5", "alerta-gunicorn-8"]
CELLS = ["writers-alone", "readers-alone", "mixed"]
J = lambda p: json.loads(Path(p).read_text())
f0 = lambda x: "missing" if x is None else f"{x:,.0f}"
f1 = lambda x: "missing" if x is None else f"{x:,.1f}"
f2 = lambda x: "missing" if x is None else f"{x:.2f}"


def med(xs):
    return st.median(xs) if len(xs) == 3 and None not in xs else None


def load(p):
    return J(p) if Path(p).exists() else None


def oha_bad(o):
    return sum(v for k, v in o.get("statusCodeDistribution", {}).items() if not k.startswith("2")) + sum(o.get("errorDistribution", {}).values())


H = {}
meta = J(ROOT / "meta.json")
L = []
A = L.append

# ---------------- reads
def read_block(sub, configs, levels, parity):
    out, unscored = {}, []
    for case in CASES:
        pok = parity["cases"][case]["match"] and parity["sql_fingerprint_match"]
        for lv in levels:
            for c in configs:
                reps = [load(RAW / sub / f"{c}_{case}_c{lv}_r{r}.json") for r in (1, 2, 3)]
                if None in reps:
                    out[(case, lv, c)] = dict(rps=None, p50=None, p99=None, reps=[], bad=None, scored=False, peak=None)
                    unscored.append(f"{sub} {case} c={lv} {c}: {sum(x is None for x in reps)} rep file(s) missing")
                    continue
                bad = sum(oha_bad(d["oha"]) for d in reps)
                scored = pok and bad == 0
                if not scored:
                    unscored.append(f"{sub} {case} c={lv} {c}: parity={pok} errors/non-2xx={bad}")
                out[(case, lv, c)] = dict(
                    rps=med([d["oha"]["summary"]["requestsPerSec"] for d in reps]),
                    p50=med([d["oha"]["latencyPercentiles"]["p50"] * 1000 for d in reps]),
                    p99=med([d["oha"]["latencyPercentiles"]["p99"] * 1000 for d in reps]),
                    reps=[round(d["oha"]["summary"]["requestsPerSec"], 1) for d in reps],
                    bad=bad, scored=scored,
                    peak=max((d.get("db_connections") or {}).get("peak_total_backends") or 0 for d in reps))
    return out, unscored


par1 = J(ROOT / "parity-test1.json")
par1b = load(ROOT / "parity-test1-rerun.json")
parc = {"sql_fingerprint_match": par1["sql_fingerprint_match"] and (par1b is None or par1b["sql_fingerprint_match"]),
        "cases": {c: {"match": par1["cases"][c]["match"] and (par1b is None or par1b["cases"][c]["match"])} for c in CASES}}
t1, t1_un = read_block("test1", CFG, LEVELS, parc)
parp = load(ROOT / "parity-pool.json")
pool, pool_un = (read_block("pool-reads", ["alrust", "alrust-p32", "alrust-p64"], [32], parp) if parp else ({}, ["pool parity missing"]))

# ---------------- writes
t2, t2_un = {}, []
for cond in ("empty", "seeded"):
    for lv in WLEVELS:
        size = load(RAW / "test2" / f"size_{cond}_c{lv}.json") or {}
        docs = {c: [load(RAW / "test2" / f"{cond}_{c}_c{lv}_r{r}.json") for r in (1, 2, 3)] for c in CFG}
        if any(None in v for v in docs.values()):
            t2_un.append(dict(cond=cond, c=lv, reason="rep file(s) missing"))
            for c in CFG:
                t2[(cond, lv, c)] = dict(rps=None, p50=None, p99=None, reps=[], scored=False, n_timed=size.get("n_timed"), peak=None)
            continue
        mism = []
        for r in range(3):
            aggs = {c: tuple(docs[c][r]["agg"].values()) for c in CFG}
            if len(set(aggs.values())) != 1:
                mism.append((r + 1, aggs))
        complete = all(d["timed"]["offered"] == d["n_timed"] and d["warmup"]["offered"] == d["n_warm"]
                       and not d["timed"].get("truncated") for c in CFG for d in docs[c])
        errs = sum(d["timed"]["error_count"] + d["warmup"]["error_count"] for c in CFG for d in docs[c])
        scored = not mism and complete
        if not scored:
            t2_un.append(dict(cond=cond, c=lv, mismatches=mism, complete=complete, errors=errs))
        for c in CFG:
            ds = docs[c]
            t2[(cond, lv, c)] = dict(rps=med([d["timed"]["rps"] for d in ds]), p50=med([d["timed"]["p50_ms"] for d in ds]),
                                    p99=med([d["timed"]["p99_ms"] for d in ds]), reps=[round(d["timed"]["rps"], 1) for d in ds],
                                    err=[d["timed"]["error_count"] + d["warmup"]["error_count"] for d in ds],
                                    elapsed=[round(d["timed"]["elapsed_s"], 2) for d in ds],
                                    n_timed=ds[0]["n_timed"], scored=scored,
                                    peak=max((d.get("db_connections") or {}).get("peak_total_backends") or 0 for d in ds),
                                    sizing=size.get("sizing_config"))

# ---------------- mixed263
mx, mx_un = {}, []
for cell in CELLS:
    for arm in ARMS:
        ds = [load(RAW / "mixed263" / f"{cell}_{arm}_r{r}.json") for r in (1, 2, 3)]
        present = [d for d in ds if d]
        valid = [d for d in present if d["valid"]]
        ok = len(valid) == 3
        if not ok:
            mx_un.append(f"{cell} {arm}: {len(valid)}/3 valid reps; reasons: " +
                         "; ".join(f"r{d['rep']}: {d['invalid_reasons']}" for d in present if not d["valid"]) +
                         ("" if len(present) == 3 else f"; {3 - len(present)} rep file(s) missing"))
        def m(fn):
            return med([fn(d) for d in valid]) if ok else None
        w = lambda d: d["writers"]
        r_ = lambda d: d["readers"]
        mx[(cell, arm)] = dict(
            valid=len(valid),
            wrps=m(lambda d: w(d)["timed_rate"]) if cell != "readers-alone" else None,
            wp50=m(lambda d: w(d)["timed_p50_ms"]) if cell != "readers-alone" else None,
            wp99=m(lambda d: w(d)["timed_p99_ms"]) if cell != "readers-alone" else None,
            rrps=m(lambda d: r_(d)["timed_rate"]) if cell != "writers-alone" else None,
            rp50=m(lambda d: r_(d)["timed_p50_ms"]) if cell != "writers-alone" else None,
            rp99=m(lambda d: r_(d)["timed_p99_ms"]) if cell != "writers-alone" else None,
            scpu=m(lambda d: d["cpu"].get("server_cpu_s_per_s")),
            pcpu=m(lambda d: d["cpu"].get("postgres_cpu_s_per_s")),
            scpu_req=m(lambda d: d["cpu"].get("server_cpu_ms_per_timed_ok_request")),
            pcpu_req=m(lambda d: d["cpu"].get("postgres_cpu_ms_per_timed_ok_request")),
            sessions=[d["pg_stat_database_delta"]["sessions"] for d in present],
            rows=[d["checks"].get("count_check", {}).get("sql_total") for d in present],
            wreps=[round(w(d)["timed_rate"], 1) for d in present] if cell != "readers-alone" else [],
            rreps=[round(r_(d)["timed_rate"], 1) for d in present] if cell != "writers-alone" else [],
            client_cpu=[round(max((x or {}).get("client_process_cpu_s", 0) / max((x or {}).get("client_wall_s", 1), 1e-9)
                                  for x in (d["writers"], d["readers"]) if x), 2) for d in present],
        )

# ---------------- freeze audit record
aud = load(ROOT / "freeze-audit.json") or {}

# ---------------- headline json
H["reads"] = {f"{case}_c{lv}": {c: {k: t1[(case, lv, c)][k] for k in ("rps", "p50", "p99", "scored")} for c in CFG}
              for case in CASES for lv in LEVELS}
H["pool_reads_c32"] = {case: {a: {k: pool.get((case, 32, a), {}).get(k) for k in ("rps", "p50", "p99", "scored")}
                              for a in ("alrust", "alrust-p32", "alrust-p64")} for case in CASES}
H["writes"] = {f"{cond}_c{lv}": {**{c: t2[(cond, lv, c)]["rps"] for c in CFG}, "scored": t2[(cond, lv, "alrust")]["scored"]}
               for cond in ("empty", "seeded") for lv in WLEVELS}
H["mixed263"] = {f"{cell}": {arm: {k: mx[(cell, arm)][k] for k in ("valid", "wrps", "wp50", "wp99", "rrps", "rp50", "rp99")}
                             for arm in ARMS} for cell in CELLS}
H["unscored"] = {"reads": t1_un, "pool": pool_un, "writes": [str(u) for u in t2_un], "mixed": mx_un}
(ROOT / "scripts/audit/headline.json").write_text(json.dumps(H, indent=1, default=str) + "\n")

# ---------------- markdown
prog = (RAW / "progress.txt").read_text().splitlines()
first_ts = next((l.split()[0] for l in prog if l[:4].isdigit()), "?")
last_ts = next((l.split()[0] for l in reversed(prog) if l[:4].isdigit()), "?")
A("# Round 2: alrust `main` vs Alerta 9.1.0, tuned 4-core setup (PostgreSQL 18.7)\n")
A(f"Log span {first_ts} to {last_ts} (America/Nassau, UTC-5). Built from `raw/` by `scripts/audit/build_report2.py`; "
  "the only outside input is the previous tuned run's `headline.json`, used in the comparison section.\n")
A("## Setup\n")
A(f"- alrust: `main` at `{meta['alrust_commit']}` built in a separate worktree as image `alrust:main-d12dc77` "
  f"(binary sha256 `{meta['alrust_binary_sha256']}`). Arms: `alrust` = default pool (ALRUST_DB_MAX_CONNECTIONS unset, 16); "
  "`alrust-p32`/`alrust-p64` set it to 32/64. Key use written about once a second (default, #260).")
A(f"- Alerta 9.1.0 `{meta['alerta_commit'][:8]}`: uWSGI-5 (`raw/uwsgi-cmd.json`) and gunicorn-8 (8 gthread workers × 4 threads; "
  "worker count carried over from the previous tuned sweep, `raw/chosen-gunicorn.json`).")
A("- Cores: app server `--cpus=4 --cpuset-cpus=0-3`; PostgreSQL 18.7 `--cpus=2 --cpuset-cpus=4-5`; load and samplers on cores 6-7. "
  "Verified inside each container (`raw/auth-probe.json`, `environment.txt`).")
A(f"- PostgreSQL `postgres:18.7` (`{meta['postgres_digest_required'][:19]}…`), image defaults, one disposable container `r2-pg`; "
  "every cell uses `CREATE DATABASE … TEMPLATE` clones of `base_empty` / `base_seed` (1,000 alerts from `seed.py`).")
A("- Reads: oha 1.17.0, keep-alive, 5 s warm-up + 30 s timed, 3 reps, rotated order. Writes: closed-loop Python client, "
  "same sequence for all servers, sized from a 5 s pilot on the slowest server (`raw/test2/size_*.json`). "
  "Mixed: PR #263 method (below). Medians = middle of 3 reps; p50/p99 = median of per-rep values.\n")

rb = load(RAW / "rebuild-after-reset.json")
if rb:
    A("## Box reset during the run\n")
    A(f"- {rb['event']}. Last finished cell before it: {rb['last_cell_before_reset']}.")
    A(f"- Docker: {rb['docker']}. PostgreSQL: {rb['postgres']}.")
    A(f"- Alerta image rebuilt: {rb['alerta']['old_id'][:19]}… → {rb['alerta']['new_id'][:19]}… ({rb['alerta']['how']}); {rb['alerta']['pip_freeze']}. "
      f"uWSGI image {rb['uwsgi']['old_id'][:19]}… → {rb['uwsgi']['new_id'][:19]}…, uWSGI {rb['uwsgi']['uwsgi_version']}.")
    A(f"- alrust image rebuilt from `{rb['alrust']['commit'][:8]}`: {rb['alrust']['old_id'][:19]}… → {rb['alrust']['new_id'][:19]}…; binary sha256 unchanged (`{rb['alrust']['binary_sha256_new']}`).")
    A(f"- Decision: {rb['decision']}. Test 1 cells finished before the reset ran on the first clone set (`parity-test1.json`), later cells on the rebuilt one (`parity-test1-rerun.json`); "
      "a read cell is scored only if parity held on both. Pool reads, writes and mixed ran entirely after the reset.\n")

A("## Reads (median req/s; p50 / p99 ms) — `raw/test1/`\n")
A(f"Parity before load (`parity-test1.json`): ordered ids of every list case and the full `/alerts/count` payload matched across the three servers: "
  f"{', '.join(f'{c}={par1['cases'][c]['match']}' for c in CASES)}; SQL fingerprint match {par1['sql_fingerprint_match']}. "
  f"After the reset (`parity-test1-rerun.json`): " + (", ".join(f"{c}={par1b['cases'][c]['match']}" for c in CASES) + f"; SQL fingerprint match {par1b['sql_fingerprint_match']}." if par1b else "missing") + "\n")
for case in CASES:
    A(f"### {case}\n")
    A("| clients | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |\n|---:|---:|---:|---:|---|---|---|---|")
    for lv in LEVELS:
        r = [t1[(case, lv, c)] for c in CFG]
        A(f"| {lv} | " + " | ".join(f0(x["rps"]) for x in r) + " | " +
          " | ".join(f"{f1(x['p50'])} / {f1(x['p99'])}" for x in r) + f" | {'yes' if all(x['scored'] for x in r) else '**NO**'} |")
    A("")

A("## Pool size, reads at 32 clients (median req/s; p50 / p99 ms) — `raw/pool-reads/`\n")
if parp:
    A(f"Parity across the three alrust arms (`parity-pool.json`): all_match={parp['all_match']}. The default arm was rerun in this interleaved schedule.\n")
A("| case | alrust (16) | alrust-p32 | alrust-p64 | p50/p99 (16) | p50/p99 (32) | p50/p99 (64) | per-rep req/s (16 / 32 / 64) |\n|---|---:|---:|---:|---|---|---|---|")
for case in CASES:
    r = [pool.get((case, 32, a), {}) for a in ("alrust", "alrust-p32", "alrust-p64")]
    A(f"| {case} | " + " | ".join(f0(x.get("rps")) for x in r) + " | " +
      " | ".join(f"{f1(x.get('p50'))} / {f1(x.get('p99'))}" for x in r) + " | " + " / ".join(str(x.get("reps")) for x in r) + " |")
A("")

A("## Writes (median alerts/s; p50 / p99 ms) — `raw/test2/`\n")
A("| DB | writers | alerts/rep | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |\n|---|---:|---:|---:|---:|---:|---|---|---|---|")
for cond in ("empty", "seeded"):
    for lv in WLEVELS:
        r = [t2[(cond, lv, c)] for c in CFG]
        A(f"| {cond} | {lv} | {f0(r[0]['n_timed'])} | " + " | ".join(f0(x["rps"]) for x in r) + " | " +
          " | ".join(f"{f1(x['p50'])} / {f1(x['p99'])}" for x in r) + f" | {'yes' if r[0]['scored'] else '**NO**'} |")
A("\nPer-rep alerts/s (r1, r2, r3) and alrust timed seconds:\n")
for cond in ("empty", "seeded"):
    for lv in WLEVELS:
        A(f"- {cond} c={lv}: " + "; ".join(f"{SHORT[c]} {t2[(cond, lv, c)]['reps']}" for c in CFG) +
          f"; alrust elapsed s {t2[(cond, lv, 'alrust')].get('elapsed')}")
A("\nScoring rule: after each rep, `alert_rows`, `SUM(duplicate_count)` and `cardinality(history)` must be equal on all three servers, "
  "and every rep must offer the full sequence. Because the sequence is sized for the slowest server (~30 s), alrust finishes it in a few "
  "seconds; its write rates are therefore measured over short windows.\n")

A("## Mixed load per alrust PR #263 — `raw/mixed263/`\n")
A("Method (from `docs/performance/benchmark-methodology.md` on `main`): 8 writers, each with its own 1,000 identities (distinct `resource`, "
  "fixed event/environment/severity), sent in order and wrapped, so the table grows from 1,000 to at most 9,000 rows; 16 readers of "
  "`/alerts?sort-by=lastReceiveTime&page-size=50`. Cells: writers alone, readers alone, mixed. Fresh `base_seed` clone per cell and rep; "
  "5 s warm-up, 15 s timed; rates count 2xx answers that ended in the timed phase. A rep is valid only with no error, non-2xx or "
  "list-check problem in any phase (2xx, parses, ≤50 alerts, non-increasing `lastReceiveTime`), a live server at the end, every "
  "identity's row present iff it had a 2xx with `duplicate_count` = 2xx − 1, and the final `/alerts/count` (total and severity counts) "
  "and newest-50 list equal to SQL on the final rows (ids may differ only inside groups of equal millisecond `last_receive_time`). "
  "Writers and readers are separate Python processes pinned to cores 6 and 7. CPU = cgroup `usage_usec` delta over the timed window.\n")
A("| cell | arm | valid | writes/s | write p50/p99 ms | reads/s | read p50/p99 ms | server CPU s/s | PG CPU s/s | server ms/req | PG ms/req | sessions opened | final rows | client CPU (max proc, s/s) |\n"
  "|---|---|---:|---:|---|---:|---|---:|---:|---:|---:|---|---|---|")
for cell in CELLS:
    for arm in ARMS:
        x = mx[(cell, arm)]
        A(f"| {cell} | {SHORT[arm]} | {x['valid']}/3 | {f1(x['wrps']) if cell != 'readers-alone' else '-'} | "
          f"{(f1(x['wp50']) + ' / ' + f1(x['wp99'])) if cell != 'readers-alone' else '-'} | "
          f"{f1(x['rrps']) if cell != 'writers-alone' else '-'} | "
          f"{(f1(x['rp50']) + ' / ' + f1(x['rp99'])) if cell != 'writers-alone' else '-'} | "
          f"{f2(x['scpu'])} | {f2(x['pcpu'])} | {f2(x['scpu_req'])} | {f2(x['pcpu_req'])} | {x['sessions']} | {x['rows']} | {x['client_cpu']} |")
A("\nPer-rep rates:\n")
for cell in CELLS:
    for arm in ARMS:
        x = mx[(cell, arm)]
        A(f"- {cell} {SHORT[arm]}: writes/s {x['wreps'] or '-'}; reads/s {x['rreps'] or '-'}")
A("\nThe mixed cells are not compared across servers on identical row histories: each server's writers reach a different number of "
  "identities (final rows column), so the readers see tables of different sizes, as in the #263 hosted run.\n")

A("## Unscored cells and why\n")
any_un = False
for u in t1_un + pool_un + mx_un:
    A(f"- {u}"); any_un = True
for u in t2_un:
    any_un = True
    if u.get("mismatches"):
        A(f"- writes {u['cond']} c={u['c']}: counts differ across servers (per the rule, not scored): " +
          "; ".join(f"r{r}: " + ", ".join(f"{SHORT[c]} rows/dup/hist={a}" for c, a in aggs.items()) for r, aggs in u["mismatches"]) +
          f"; errors in those 9 files: {u.get('errors')}")
    else:
        A(f"- writes {u['cond']} c={u['c']}: {u}")
if not any_un:
    A("- None.")
A("")

A("## Freeze audit\n")
if aud:
    rounds = aud.get("passes") or []
    rounds_txt = "; ".join(rounds) if rounds else "none"
    A(f"`scripts/audit/freeze_audit2.py` over every timed file. Flags: logged span > measured + allowance + 20 s; DB sampler > 0.26 s/sample; "
      "any error/non-2xx or invalid mixed rep; rate > 12% from its rep median; realtime vs monotonic gap > 1 s (mixed). "
      f"Final pass: {aud.get('records')} records, {aud.get('flagged')} flagged. Rounds: {rounds_txt}.\n")
    for line in aud.get("notes", []):
        A(f"- {line}")
    A("")

prev = load(PREV)
if prev:
    A("## Compared with the previous tuned run (alrust `11aede4b`, 2026-11-15/16)\n")
    A("Same box, caps, images for Alerta, and harness for reads and writes. alrust binary and the mixed method differ; the previous mixed "
      "case was a fixed write sequence (new/duplicate/correlate mix) beside 16 readers, so mixed numbers are not like for like.\n")
    A("| metric | prev alrust | now alrust | prev uWSGI-5 | now uWSGI-5 | prev gunicorn-8 | now gunicorn-8 |\n|---|---:|---:|---:|---:|---:|---:|")
    for case in CASES:
        p = prev["reads_c32"][case]
        A(f"| reads {case} @32 req/s | " + " | ".join(f"{f0(p[c]['rps'])} | {f0(t1[(case, 32, c)]['rps'])}" for c in CFG) + " |")
    for cond in ("empty", "seeded"):
        for lv in (1, 8, 32):
            p = prev["writes"].get(f"{cond}_c{lv}", {})
            A(f"| writes {cond} @{lv} alerts/s | " + " | ".join(f"{f0(p.get(c))} | {f0(t2[(cond, lv, c)]['rps'])}" for c in CFG) + " |")
    pm = prev["mixed"]
    A("| mixed write alerts/s (prev method → #263 method, default pool) | " +
      " | ".join(f"{f0(pm[c]['write_rps'])} | {f0(mx[('mixed', c)]['wrps'])}" for c in CFG) + " |")
    A("| mixed read p50 ms (prev → #263) | " +
      " | ".join(f"{f1(pm[c]['read_p50'])} | {f1(mx[('mixed', c)]['rp50'])}" for c in CFG) + " |")
    A("")

A("## Files\n")
A("- `raw/progress.txt` log; `raw/test1/`, `raw/pool-reads/`, `raw/test2/`, `raw/mixed263/` (+ `identities/`) timed results; "
  "`raw/_superseded/` flagged results moved aside (never deleted); `raw/_smoke/` harness smoke tests before the run (not scored).")
A("- `parity-test1.json`, `parity-pool.json`, `freeze-audit.json`, `teardown.json`, `meta.json`, `environment.txt`, `alrust-build.log`.")
A("- Nothing was committed or pushed.")
(ROOT / "REPORT.md").write_text("\n".join(L) + "\n")
print("wrote REPORT.md", len(L), "lines")
