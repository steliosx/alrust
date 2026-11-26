# Tuned like-for-like benchmark: Alerta 9.1.0 vs alrust (PostgreSQL 18.7)

Run 2026-11-15 20:24 to 2026-11-16 12:36 Nassau time (EST, UTC-5), with freeze reruns at 12:26–12:36. Rebuilt from the per-run raw JSON by `scripts/audit/build_report.py`. Paths such as `raw/...`, `scripts/...`, `meta.json`, and `environment.txt` refer to the benchmark working directory, which is not in this repository. Nothing here is copied from another run except the comparison section, which cites [the full concurrency run](2026-11-15-alerta-concurrency-full.md).

## Key findings

- Reads at 32 clients: alrust 1,574–4,798 req/s; Alerta uWSGI-5 279–398; gunicorn-8 310–397. gunicorn-8 is a few percent faster than uWSGI-5 but has a much longer tail (p99) at 32 clients.
- Writes at 32 writers: alrust 4,170 (empty) / 4,296 (seeded) alerts/s vs 221–242 for the two Alerta setups. Alerta writes stop scaling after 8 writers.
- Mixed load (8 writers + 16 readers): alrust 304 alerts/s with read p50 29.4 ms; uWSGI-5 84 / 120.1 ms; gunicorn-8 67 / 100.1 ms.
- Tuning Alerta helped filtered/list reads versus the earlier untuned uncapped run (~200 → ~280–320 req/s at 32) but did not help writes (285 → ~240 at 32), and `/alerts/count` and the empty-result query were lower than in that run (that run had no CPU caps, so this is not a clean before/after).
- One write cell (empty, 1 writer) is unscored because an Alerta HTTP 500 deadlock left gunicorn-8 one alert short in rep 2. All read cells and the mixed test are scored.

## Setup

- Host: 8-vCPU Intel Xeon VM, 15 GiB RAM, no swap (`environment.txt`).
- Core layout (`meta.json` core_map, checked inside each container by `check_pins`): app server `--cpus=4 --cpuset-cpus=0-3`; PostgreSQL 18.7 `--cpus=2 --cpuset-cpus=4-5`; load generator, write client, and DB sampler `taskset -c 6-7`; dockerd unpinned. One app server under load at a time.
- PostgreSQL `postgres:18.7` digest `sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc`, image defaults (max_connections 100, shared_buffers 128MB). Disposable databases only, in a throwaway container (`tuned-pg`) that was removed at the end (`teardown.json`).
- Alerta 9.1.0 commit `f4312727`, two servers:
  - **uWSGI-5** (documented setup: docs.alerta.io and docker-alerta default `processes = 5`): `uwsgi --http --http-keepalive --master --processes 5 ...` (`raw/uwsgi-cmd.json`, uWSGI 2.0.28).
  - **gunicorn-8**: gunicorn gthread, 8 workers × 4 threads, the best point of the sweep below.
- alrust commit `11aede4b` (binary sha256 `739102855cd68102…`), defaults; fixed DB pool of 16, no tuning knobs.
- Reads: oha 1.17.0, HTTP/1.1 keep-alive, 5 s discarded warmup + 30 s timed, 3 reps, rotation of which server goes first. Writes: closed-loop Python client, one keep-alive connection per writer, a shared sequence sized from a 5 s pilot on the slowest server (~30 s for it), the same sequence offered to all three. Every write rep starts from a fresh clone of `base_empty` or `base_seed` (1,000 alerts from `seed.py`).
- Medians are the middle of 3 reps. p50/p99 are the median of the three per-rep p50/p99 values. Peak DB connections are sampled every 0.2 s from `pg_stat_activity` for that database, excluding the sampler.

## Gunicorn sweep (list, 32 clients, `raw/sweep/`)

| workers × threads | median req/s | p50 ms | p99 ms | errors | peak DB conns |
|---|---:|---:|---:|---:|---:|
| 4 × 4 | 334 | 93.3 | 169.5 | 0 | 16 |
| 8 × 4 | 339 | 57.4 | 246.3 | 0 | 20 |
| 16 × 4 | 337 | 69.1 | 268.8 | 0 | 26 |

Chosen: `alerta-gunicorn-8` (`raw/chosen-gunicorn.json`, rule: highest median). The three points are within 4 req/s of each other, so the sweep shows a plateau, not a clear winner.

## Test 1: reads (median req/s, p50 / p99 ms) — `raw/test1/`

Parity: ordered ids (lists) and count payloads matched across all three servers for every case, both in the original clones (`parity-test1.json`) and in the rebuilt clones used for the rerun (`parity-test1-rerun.json`).

### list — `/alerts?sort-by=lastReceiveTime&page-size=50`

| clients | alrust req/s | uWSGI-5 req/s | gunicorn-8 req/s | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 387 | 93 | 99 | 2.5 / 3.9 | 10.4 / 15.9 | 9.7 / 13.9 | yes |
| 8 | 1,335 | 290 | 311 | 5.7 / 11.7 | 27.2 / 40.3 | 25.4 / 38.1 | yes |
| 16 | 1,512 | 293 | 312 | 10.1 / 20.0 | 53.9 / 72.1 | 49.7 / 89.8 | yes |
| 32 | 1,574 | 279 | 310 | 19.9 / 30.0 | 116.7 / 142.4 | 105.7 / 173.8 | yes |

### event — `/alerts?event=BackupFailed&sort-by=lastReceiveTime&page-size=50`

| clients | alrust req/s | uWSGI-5 req/s | gunicorn-8 req/s | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 526 | 98 | 103 | 1.8 / 3.5 | 9.8 / 15.9 | 9.5 / 14.3 | yes |
| 8 | 2,266 | 322 | 349 | 3.3 / 6.8 | 24.7 / 36.0 | 22.4 / 38.4 | yes |
| 16 | 2,654 | 323 | 351 | 5.8 / 11.5 | 48.9 / 75.1 | 42.2 / 80.7 | yes |
| 32 | 2,586 | 314 | 318 | 12.0 / 20.3 | 99.3 / 142.5 | 39.2 / 264.9 | yes |

### severity — `/alerts?severity=critical&sort-by=lastReceiveTime&page-size=50`

| clients | alrust req/s | uWSGI-5 req/s | gunicorn-8 req/s | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 493 | 99 | 99 | 1.9 / 3.2 | 9.7 / 13.7 | 9.8 / 16.2 | yes |
| 8 | 2,279 | 335 | 358 | 3.3 / 6.9 | 23.7 / 35.8 | 21.9 / 35.1 | yes |
| 16 | 2,573 | 323 | 362 | 5.9 / 12.4 | 48.4 / 69.9 | 37.5 / 82.2 | yes |
| 32 | 2,437 | 319 | 324 | 12.5 / 22.4 | 97.9 / 131.1 | 103.3 / 203.7 | yes |

### count — `/alerts/count`

| clients | alrust req/s | uWSGI-5 req/s | gunicorn-8 req/s | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 1,731 | 164 | 179 | 0.5 / 1.4 | 5.8 / 11.0 | 5.4 / 8.2 | yes |
| 8 | 5,126 | 419 | 420 | 1.4 / 4.3 | 18.9 / 29.4 | 18.5 / 31.8 | yes |
| 16 | 4,835 | 399 | 401 | 2.8 / 10.8 | 39.3 / 61.7 | 37.9 / 75.4 | yes |
| 32 | 4,798 | 398 | 397 | 6.0 / 15.2 | 79.5 / 108.7 | 57.1 / 206.6 | yes |

### empty — `/alerts?event=NoSuchEventBench&sort-by=lastReceiveTime&page-size=50`

| clients | alrust req/s | uWSGI-5 req/s | gunicorn-8 req/s | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 1,346 | 141 | 158 | 0.7 / 1.7 | 6.7 / 12.3 | 6.1 / 9.1 | yes |
| 8 | 4,035 | 379 | 393 | 1.8 / 4.7 | 20.9 / 30.4 | 20.0 / 31.9 | yes |
| 16 | 3,880 | 374 | 396 | 3.7 / 11.2 | 42.1 / 60.3 | 32.3 / 81.8 | yes |
| 32 | 3,653 | 342 | 354 | 7.9 / 18.0 | 89.4 / 138.4 | 47.9 / 194.5 | yes |

## Test 2: writes (median alerts/s, p50 / p99 ms) — `raw/test2/`

| DB | writers | alerts/rep | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---|---:|---:|---:|---:|---:|---|---|---|---|
| empty | 1 | 1,592 | 902 | 39 | 92 | 0.9 / 2.8 | 10.6 / 71.9 | 9.3 / 21.5 | **NO** |
| empty | 8 | 8,224 | 3,450 | 240 | 262 | 2.0 / 5.9 | 20.2 / 80.2 | 28.8 / 54.3 | yes |
| empty | 32 | 9,120 | 4,170 | 242 | 234 | 6.9 / 18.2 | 122.8 / 206.5 | 90.6 / 254.4 | yes |
| seeded | 1 | 1,599 | 829 | 38 | 86 | 0.9 / 3.2 | 11.1 / 72.0 | 9.7 / 24.7 | yes |
| seeded | 8 | 7,920 | 3,366 | 221 | 248 | 2.1 / 6.4 | 22.7 / 88.2 | 30.5 / 56.7 | yes |
| seeded | 32 | 8,736 | 4,296 | 242 | 221 | 6.9 / 15.7 | 127.6 / 186.7 | 104.5 / 314.6 | yes |

Per-rep alerts/s (r1, r2, r3):

- empty c=1: alrust [902.0, 950.1, 831.3]; uWSGI-5 [39.4, 38.5, 39.2]; gunicorn-8 [90.4, 93.7, 92.1]
- empty c=8: alrust [3571.2, 3450.5, 3149.8]; uWSGI-5 [248.5, 239.5, 236.0]; gunicorn-8 [262.8, 262.3, 262.3]
- empty c=32: alrust [4035.1, 4170.0, 4335.6]; uWSGI-5 [241.7, 238.8, 251.1]; gunicorn-8 [235.2, 234.4, 228.4]
- seeded c=1: alrust [792.9, 875.2, 829.3]; uWSGI-5 [30.7, 37.6, 38.5]; gunicorn-8 [94.1, 85.7, 85.6]
- seeded c=8: alrust [3365.6, 3127.8, 3579.3]; uWSGI-5 [221.0, 219.7, 229.1]; gunicorn-8 [249.9, 244.6, 248.4]
- seeded c=32: alrust [4108.1, 4296.5, 4357.2]; uWSGI-5 [239.3, 248.2, 242.5]; gunicorn-8 [215.4, 221.4, 223.0]

## Test 2: mixed load — 8 writers + 16 readers of the newest-50 list, seeded DB — `raw/test2-mixed/`

Each rep offers the same 7,920-alert write sequence while 16 readers loop on `/alerts?sort-by=lastReceiveTime&page-size=50`.

| server | write alerts/s | write p50 / p99 ms | read p50 / p99 ms | reads/s during writes | per-rep write alerts/s | errors (write, read) per rep | peak DB conns |
|---|---:|---|---|---:|---|---|---:|
| alrust | 304 | 24.5 / 53.3 | 29.4 / 64.0 | 514 | [339.0, 297.0, 303.9] | [(0, 0), (0, 0), (0, 0)] | 18 |
| uWSGI-5 | 84 | 87.0 / 160.3 | 120.1 / 160.0 | 131 | [84.4, 78.4, 90.6] | [(0, 0), (0, 0), (0, 0)] | 6 |
| gunicorn-8 | 67 | 109.4 / 190.5 | 100.1 / 177.2 | 155 | [52.8, 68.7, 66.8] | [(0, 0), (0, 0), (0, 0)] | 23 |

Scored: yes (alert rows, duplicate_count sum, and history entries matched across servers in every rep). reads/s = successful reads ÷ timed write phase seconds.

## Peak database connections (max over all timed runs, `db_connections.peak_total_backends`)

| server | natural model | reads | writes | mixed |
|---|---|---:|---:|---:|
| alrust | sqlx pool fixed at 16 | 18 | 17 | 18 |
| uWSGI-5 | 1 connection per request, 5 processes | 5 | 5 | 6 |
| gunicorn-8 | 1 connection per request, 8 workers × 4 threads | 21 | 27 | 23 |

alrust's client-backend peak (`peak_client_backends`) never went above 17, one more than its pool of 16; the source of the extra short-lived connection was not identified. The total peaks of 18 also count non-client backends on that database (for example autovacuum). Alerta opens one connection per in-flight request, so uWSGI-5 stays at 5–6 and gunicorn-8 (32 threads) reached 27.

## Correctness checks and unscored cells

- Reads: parity matched for all 5 cases (both clone sets). Error/non-2xx count across all 180 timed read files: 0.
- Writes: after each rep, `alert_rows`, `SUM(duplicate_count)`, and `cardinality(history)` must match across all three servers; API sample shapes for new/duplicate/correlate matched (`raw/test2/api-samples.json`).
- Mixed: same aggregate check, all three reps matched after the reruns.
- **Unscored: writes empty c=1.** Per the rule *if counts differ, do not score*, this cell is excluded from scoring. Mismatching reps: r2: alrust rows/dup/hist=(750, 555, 1047), uWSGI-5 rows/dup/hist=(750, 555, 1047), gunicorn-8 rows/dup/hist=(749, 555, 1046).
  Cause (`raw/test2/empty_alerta-gunicorn-8_c1_r2.json` warmup.error_samples): Alerta gunicorn-8 returned one HTTP 500 `deadlock detected` during the warmup of rep 2, so that database ended with 749 alerts instead of 750. This is an Alerta error, not a machine freeze, so it was not rerun; its per-rep values above are shown for information only.
- No read cell is unscored.

## Deviations

1. **Runner driven from the shell, not by grok.** The Grok Build CLI wrote the harness (`scripts/`) and started it, but its bwrap sandbox could not reach `/var/run/docker.sock` reliably and its tool calls were ending the long runner after ~18 min. From 2026-11-15 20:45 the same `scripts/run_all.py` was continued with `nohup` from the box shell (`raw/progress.txt` HANDOFF line). Resume skips finished cells, so nothing was overwritten.
2. **Machine freezes.** The benchmark VM was suspended several times (overnight and while idle). A suspended VM stops the clock the tests measure with, so a freeze shows up as a wall-clock gap in `raw/progress.txt` far longer than the test's measured time, sampler gaps, dropped connections, or low throughput.
   - First pass (09:06–09:17): 13 freeze-hit read cells were moved to `raw/_superseded/test1/` and rerun at 09:17–09:26, including `alrust_count_c16_r2` (25 min span, 5 non-2xx) and `alrust_empty_c16_r3` (≈5 min span, 12 non-2xx). In the same pass, the first write attempt was discarded: gunicorn-8 refused connections right after restart (`[Errno 111]` FAIL lines in `raw/progress.txt`, files `*.fail.json`) and the c=8/c=32 sizing pilots were too small (mixed reps had ~1,190 rows). A gunicorn ready gate was added, the c=8/c=32 sizes were re-piloted, and all c=8/c=32 write cells and all 9 mixed cells were rerun (09:25–11:16). Everything replaced is in `raw/_superseded/{test1,test2,test2-mixed,pilots}`.
   - Second pass (this audit, `scripts/audit/freeze_audit.py`): every one of the 243 timed files was checked for (a) progress-log span minus measured time minus normal startup > 20 s, (b) DB sampler interval > 0.26 s instead of 0.2 s, (c) any error/non-2xx, (d) rps more than 12% from its cell median. Eight cells were flagged and moved to `raw/_superseded/freeze-audit-1230/`:
     - `test1/alerta-uwsgi-5_empty_c16_r2.json`
     - `test2/empty_alerta-uwsgi-5_c32_r1.json`
     - `test2/seeded_alerta-gunicorn-8_c1_r1.json`
     - `test2/seeded_alerta-gunicorn-8_c8_r1.json`
     - `test2/seeded_alerta-uwsgi-5_c1_r3.json`
     - `test2-mixed/alerta-gunicorn-8_r2.json`
     - `test2-mixed/alerta-gunicorn-8_r3.json`
     - `test2-mixed/alerta-uwsgi-5_r3.json`
     All eight were rerun once at 12:26–12:36 and all reruns were clean (span ≈ measured time, sampler at 0.2 s, 0 errors). No second retry was needed.
   - Not flagged: `alrust_event_c32_r3` (2,270 req/s vs 2,586 median, −12%) had a normal span, normal sampler, and 0 errors, so it stays. `alerta-gunicorn-8` mixed r1 (52.8 alerts/s) also had a normal span and stays; it is lower than the clean reruns r2/r3 (68.7, 66.8).
3. **Templates rebuilt for the rerun.** The 11:16 teardown removed the Postgres container, so `base_empty`/`base_seed` were rebuilt with the same `seed.py` (1,000 deterministic alerts). Alert ids are server-generated UUIDs, so the id fingerprint differs from the first build (`90dc96…` → `cb0d11…`), but content and counts are identical. Parity was re-checked on the new clones and matched. To allow this, `scripts/common.py` and `scripts/reads.py` gained an opt-in `RERUN_REBUILD=1` path (originals in `scripts/audit/orig/`).
4. Within every write cell, all three servers received exactly the same sequence (`n_timed` in each file); c=1 cells kept their original sizing, c=8/c=32 were re-sized as above.
5. The sweep only tested gunicorn 4/8/16 workers with 4 threads each; uWSGI was run only at the documented 5 processes.

## Tuning observation: Alerta

Tuning barely helped Alerta. All figures below are measured medians from this run and from [the full concurrency run](2026-11-15-alerta-concurrency-full.md), which used the image default (gunicorn 1 worker × 4 threads) with no CPU caps, so this is not a clean before/after.

- Reads at 32 clients: list, event, and severity went from about 200 req/s (199, 202, 200) to 279–324 req/s (uWSGI-5 279/314/319, gunicorn-8 310/318/324). `/alerts/count` and the empty-result query went down (531 → 398/397 and 476 → 342/354).
- Writes at 32 senders: 242 (empty) and 242 (seeded) alerts/s on uWSGI-5, and 234 and 221 on gunicorn-8, against 285 in the full run. Both worker models level off at about the same rate.
- The gunicorn sweep was a tie: 4, 8, and 16 workers (4 threads each) gave 334, 339, and 337 req/s on the 32-client list query.

## Tuning observation: database pool size

**Suggestion, not measured.** alrust's serving pool is fixed at 16 connections and there is no setting to change it (the only related variable is `ALRUST_DB_ACQUIRE_TIMEOUT`). In the mixed test above, 16 readers plus 8 writers share that pool, and alrust's write rate fell to 304 alerts/s, against 3,366 alerts/s for 8 writers alone on the same seeded database. The cause is not confirmed. Our hypothesis is pool contention: 24 concurrent clients queue for 16 connections, and writes wait behind reads.

Suggested follow-up:

1. Add an environment variable such as `ALRUST_DB_MAX_CONNECTIONS` (default 16, so current behaviour is unchanged).
2. Repeat the mixed test, and the 32-client read and write cells, with the pool at 16, 32, and 64 on the same 4-core layout. Record peak database connections next to throughput, as here.
3. As a separate variant, put PgBouncer in transaction mode between alrust and PostgreSQL.

This is a hypothesis to test; no numbers are predicted for other pool sizes. alrust has not been benchmarked with PgBouncer: a search of this repository (excluding `reference/`) and of the benchmark working directories found no PgBouncer configuration or results.

## Comparison with the earlier untuned full run

[2026-11-15-alerta-concurrency-full.md](2026-11-15-alerta-concurrency-full.md): no CPU caps (8 vCPU shared by everything), Alerta as the image default gunicorn 1 worker × 4 threads, alrust defaults. Not strictly comparable because of the caps, but directionally:

| metric | full run Alerta | full run alrust | tuned uWSGI-5 | tuned gunicorn-8 | tuned alrust |
|---|---:|---:|---:|---:|---:|
| reads list @32 req/s | 199 | 2,731 | 279 | 310 | 1,574 |
| reads event @32 req/s | 202 | 3,416 | 314 | 318 | 2,586 |
| reads severity @32 req/s | 200 | 3,582 | 319 | 324 | 2,437 |
| reads count @32 req/s | 531 | 8,587 | 398 | 397 | 4,798 |
| reads empty @32 req/s | 476 | 7,001 | 342 | 354 | 3,653 |
| writes empty @1 alerts/s | 84 | 757 | 39 | 92 | 902 |
| writes empty @8 alerts/s | 225 | 4,533 | 240 | 262 | 3,450 |
| writes empty @32 alerts/s | 285 | 6,228 | 242 | 234 | 4,170 |
| mixed write alerts/s | 62 | 926 | 84 | 67 | 304 |
| mixed read p50 ms | 122.5 | 12.6 | 120.1 | 100.1 | 29.4 |

(empty c=1 is an unscored cell in the tuned run; its medians are shown for information.)

## Files (benchmark working directory, not in this repository)

- `raw/progress.txt` full log; `raw/test1|test2|test2-mixed|sweep/*.json` per-run raw results; `raw/_superseded/` everything replaced, never deleted.
- `scripts/` harness; `scripts/audit/freeze_audit.py` freeze detector; `scripts/audit/build_report.py` this report; `scripts/audit/headline.json` the headline numbers.
