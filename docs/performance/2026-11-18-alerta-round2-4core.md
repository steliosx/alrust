# Round 2: alrust `main` vs Alerta 9.1.0, tuned 4-core setup (PostgreSQL 18.7)

Log span 2026-11-17T14:35:24-0500 to 2026-11-18T18:06:49-0500 (America/Nassau, UTC-5). Built from `raw/` by `scripts/audit/build_report2.py`; the only outside input is the previous tuned run's `headline.json`, used in the comparison section.

## Setup

- alrust: `main` at `d12dc7705541747c8a90a4387405560f60665f73` built in a separate worktree as image `alrust:main-d12dc77` (binary sha256 `8ae548595488659783f201d5a0165905e37afb7f27cddfcf7fe395fa6d475eca`). Arms: `alrust` = default pool (ALRUST_DB_MAX_CONNECTIONS unset, 16); `alrust-p32`/`alrust-p64` set it to 32/64. Key use written about once a second (default, #260).
- Alerta 9.1.0 `f4312727`: uWSGI-5 (`raw/uwsgi-cmd.json`) and gunicorn-8 (8 gthread workers × 4 threads; worker count carried over from the previous tuned sweep, `raw/chosen-gunicorn.json`).
- Cores: app server `--cpus=4 --cpuset-cpus=0-3`; PostgreSQL 18.7 `--cpus=2 --cpuset-cpus=4-5`; load and samplers on cores 6-7. Verified inside each container (`raw/auth-probe.json`, `environment.txt`).
- PostgreSQL `postgres:18.7` (`sha256:2ea8630df176…`), image defaults, one disposable container `r2-pg`; every cell uses `CREATE DATABASE … TEMPLATE` clones of `base_empty` / `base_seed` (1,000 alerts from `seed.py`).
- Reads: oha 1.17.0, keep-alive, 5 s warm-up + 30 s timed, 3 reps, rotated order. Writes: closed-loop Python client, same sequence for all servers, sized from a 5 s pilot on the slowest server (`raw/test2/size_*.json`). Mixed: PR #263 method (below). Medians = middle of 3 reps; p50/p99 = median of per-rep values.

## Box reset during the run

- box reset at about 15:56 EST 2026-11-17 (uptime restarted; /var/lib/docker, /tmp tools and the r2-pg container were gone; docker CLI absent). Last finished cell before it: alerta-uwsgi-5_count_c16_r1 (finished 15:55:58); alerta-gunicorn-8_count_c16_r1 warm-up interrupted, no timed file.
- Docker: reinstalled from download.docker.com static docker-29.10.0.tgz (was 29.9.3); dockerd --iptables=false --bridge=none (all containers use --network host as before); apt mirror returned HTTP 500. PostgreSQL: postgres:18.7 re-pulled; id sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc (same digest).
- Alerta image rebuilt: sha256:b6c8b4733b38… → sha256:a5e18b9aad59… (same Dockerfile and hash locks (/workspace/alerta-bench/alerta-image), src = git archive of Alerta f4312727783dad64198b62ed778bbca98a5144a3, base python@sha256:3c3ba8ad...); alerta-pip-freeze-rebuild.txt is byte-identical to the pip freeze recorded for the same recipe in /workspace/alerta-bench-full/environment.txt (the b6c8b473 image's own freeze was not recorded). uWSGI image sha256:bc186302eb23… → sha256:98da59bf0538…, uWSGI 2.0.28.
- alrust image rebuilt from `d12dc770`: sha256:1f8f987c08f8… → sha256:6d198e294382…; binary sha256 unchanged (`8ae548595488659783f201d5a0165905e37afb7f27cddfcf7fe395fa6d475eca`).
- Decision: binary identical and Alerta packages identical apart from image ids: finished test1 cells kept; base_empty/base_seed rebuilt with the same seed.py (RERUN_REBUILD=1), parity re-checked on the new clones in parity-test1-rerun.json. Test 1 cells finished before the reset ran on the first clone set (`parity-test1.json`), later cells on the rebuilt one (`parity-test1-rerun.json`); a read cell is scored only if parity held on both. Pool reads, writes and mixed ran entirely after the reset.

## Reads (median req/s; p50 / p99 ms) — `raw/test1/`

Parity before load (`parity-test1.json`): ordered ids of every list case and the full `/alerts/count` payload matched across the three servers: list=True, event=True, severity=True, count=True, empty=True; SQL fingerprint match True. After the reset (`parity-test1-rerun.json`): list=True, event=True, severity=True, count=True, empty=True; SQL fingerprint match True.

### list

| clients | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 449 | 96 | 100 | 2.2 / 3.3 | 10.1 / 14.7 | 9.4 / 22.3 | yes |
| 8 | 1,585 | 299 | 315 | 4.8 / 10.0 | 26.4 / 43.6 | 25.0 / 41.2 | yes |
| 16 | 1,777 | 299 | 322 | 8.5 / 17.4 | 51.6 / 92.2 | 43.2 / 98.3 | yes |
| 32 | 1,506 | 273 | 294 | 20.6 / 33.1 | 115.8 / 157.6 | 108.3 / 223.1 | yes |

### event

| clients | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 639 | 101 | 106 | 1.5 / 2.5 | 9.6 / 14.9 | 8.9 / 19.6 | yes |
| 8 | 2,829 | 331 | 351 | 2.7 / 5.1 | 23.7 / 46.1 | 22.3 / 39.0 | yes |
| 16 | 3,188 | 331 | 352 | 4.9 / 8.2 | 47.5 / 73.9 | 43.5 / 72.0 | yes |
| 32 | 2,009 | 262 | 276 | 15.6 / 23.8 | 120.8 / 149.6 | 85.5 / 251.1 | yes |

### severity

| clients | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 623 | 103 | 108 | 1.5 / 2.6 | 9.4 / 17.0 | 8.9 / 13.1 | yes |
| 8 | 2,719 | 335 | 353 | 2.8 / 5.4 | 23.6 / 33.9 | 22.4 / 33.5 | yes |
| 16 | 3,127 | 340 | 354 | 5.0 / 8.5 | 46.4 / 63.8 | 43.9 / 81.8 | yes |
| 32 | 2,229 | 270 | 305 | 14.0 / 21.7 | 117.3 / 144.7 | 99.0 / 200.8 | yes |

### count

| clients | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 2,837 | 167 | 176 | 0.3 / 0.8 | 5.8 / 8.8 | 5.4 / 9.3 | yes |
| 8 | 8,085 | 411 | 423 | 0.9 / 2.3 | 19.1 / 34.7 | 18.3 / 38.6 | yes |
| 16 | 7,111 | 388 | 379 | 2.1 / 4.7 | 40.9 / 52.1 | 40.9 / 69.9 | yes |
| 32 | 6,617 | 358 | 339 | 4.7 / 8.4 | 86.8 / 118.7 | 77.9 / 223.6 | yes |

### empty

| clients | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 1,969 | 150 | 160 | 0.5 / 1.1 | 6.4 / 9.2 | 5.9 / 9.0 | yes |
| 8 | 5,733 | 382 | 393 | 1.3 / 2.7 | 20.6 / 30.7 | 19.8 / 34.0 | yes |
| 16 | 4,823 | 370 | 369 | 3.2 / 5.6 | 42.9 / 55.7 | 40.3 / 78.7 | yes |
| 32 | 4,247 | 308 | 305 | 7.3 / 11.9 | 102.6 / 131.1 | 74.4 / 211.4 | yes |

## Pool size, reads at 32 clients (median req/s; p50 / p99 ms) — `raw/pool-reads/`

Parity across the three alrust arms (`parity-pool.json`): all_match=True. The default arm was rerun in this interleaved schedule.

| case | alrust (16) | alrust-p32 | alrust-p64 | p50/p99 (16) | p50/p99 (32) | p50/p99 (64) | per-rep req/s (16 / 32 / 64) |
|---|---:|---:|---:|---|---|---|---|
| list | 1,594 | 1,659 | 1,372 | 19.4 / 32.5 | 18.1 / 40.0 | 20.9 / 65.7 | [1497.2, 1594.1, 1597.5] / [1621.2, 1717.5, 1659.4] / [1316.0, 1372.2, 1406.8] |
| event | 2,855 | 2,608 | 2,639 | 10.9 / 16.6 | 11.9 / 21.1 | 11.6 / 26.1 | [2836.9, 2855.4, 2984.8] / [2417.5, 2752.4, 2608.2] / [2384.5, 2639.0, 2812.7] |
| severity | 2,847 | 2,669 | 2,561 | 10.9 / 17.7 | 11.6 / 21.9 | 11.8 / 28.4 | [2739.4, 2847.1, 2891.6] / [2470.2, 2669.0, 2711.6] / [2364.9, 2560.6, 2627.1] |
| count | 6,679 | 6,066 | 6,260 | 4.6 / 7.8 | 5.0 / 10.9 | 4.3 / 15.9 | [6706.2, 5981.5, 6678.8] / [5375.6, 6066.1, 6500.6] / [6061.5, 6259.9, 6738.3] |
| empty | 4,646 | 4,628 | 4,563 | 6.7 / 10.1 | 6.7 / 12.2 | 6.3 / 17.3 | [4692.9, 4153.9, 4646.1] / [4447.7, 4628.1, 4869.6] / [4571.0, 4563.2, 4328.9] |

## Writes (median alerts/s; p50 / p99 ms) — `raw/test2/`

| DB | writers | alerts/rep | alrust | uWSGI-5 | gunicorn-8 | alrust p50/p99 | uWSGI-5 p50/p99 | gunicorn-8 p50/p99 | scored |
|---|---:|---:|---:|---:|---:|---|---|---|---|
| empty | 1 | 1,555 | 822 | 37 | 85 | 0.9 / 3.3 | 11.4 / 72.1 | 9.7 / 23.9 | yes |
| empty | 8 | 7,912 | 3,690 | 236 | 242 | 1.8 / 6.6 | 20.8 / 83.7 | 31.3 / 62.1 | yes |
| empty | 16 | 8,224 | 5,122 | 241 | 228 | 2.8 / 7.6 | 56.3 / 119.9 | 68.6 / 105.8 | yes |
| empty | 32 | 6,720 | 6,499 | 255 | 224 | 4.5 / 12.9 | 120.5 / 191.8 | 105.6 / 340.0 | yes |
| seeded | 1 | 1,428 | 853 | 37 | 83 | 0.9 / 3.2 | 12.2 / 72.2 | 10.3 / 25.0 | yes |
| seeded | 8 | 7,440 | 3,964 | 212 | 229 | 1.7 / 5.3 | 24.6 / 92.0 | 32.8 / 58.7 | yes |
| seeded | 16 | 7,616 | 5,264 | 226 | 228 | 2.6 / 7.5 | 61.9 / 128.0 | 68.5 / 108.5 | yes |
| seeded | 32 | 6,624 | 6,684 | 233 | 215 | 4.5 / 8.7 | 133.2 / 196.1 | 112.0 / 295.9 | yes |

Per-rep alerts/s (r1, r2, r3) and alrust timed seconds:

- empty c=1: alrust [845.1, 785.6, 822.2]; uWSGI-5 [36.9, 37.3, 37.8]; gunicorn-8 [78.5, 89.7, 85.0]; alrust elapsed s [1.84, 1.98, 1.89]
- empty c=8: alrust [3268.9, 4122.0, 3689.8]; uWSGI-5 [227.1, 236.0, 237.4]; gunicorn-8 [242.3, 243.3, 236.6]; alrust elapsed s [2.42, 1.92, 2.14]
- empty c=16: alrust [5157.2, 5122.0, 4973.7]; uWSGI-5 [241.1, 241.2, 253.8]; gunicorn-8 [230.8, 226.7, 227.9]; alrust elapsed s [1.59, 1.61, 1.65]
- empty c=32: alrust [6770.3, 6182.5, 6498.7]; uWSGI-5 [225.4, 255.5, 257.0]; gunicorn-8 [210.2, 223.6, 235.7]; alrust elapsed s [0.99, 1.09, 1.03]
- seeded c=1: alrust [877.2, 768.9, 853.0]; uWSGI-5 [37.3, 36.6, 36.3]; gunicorn-8 [82.8, 82.9, 80.1]; alrust elapsed s [1.63, 1.86, 1.67]
- seeded c=8: alrust [3964.5, 3601.4, 4204.6]; uWSGI-5 [211.9, 218.6, 207.8]; gunicorn-8 [229.5, 231.9, 221.1]; alrust elapsed s [1.88, 2.07, 1.77]
- seeded c=16: alrust [4984.5, 5659.3, 5264.5]; uWSGI-5 [215.4, 226.0, 237.2]; gunicorn-8 [227.5, 230.8, 223.5]; alrust elapsed s [1.53, 1.35, 1.45]
- seeded c=32: alrust [6683.9, 6356.3, 7160.6]; uWSGI-5 [233.0, 238.1, 232.1]; gunicorn-8 [210.9, 214.8, 229.8]; alrust elapsed s [0.99, 1.04, 0.93]

Scoring rule: after each rep, `alert_rows`, `SUM(duplicate_count)` and `cardinality(history)` must be equal on all three servers, and every rep must offer the full sequence. Because the sequence is sized for the slowest server (~30 s), alrust finishes it in a few seconds; its write rates are therefore measured over short windows.

## Mixed load per alrust PR #263 — `raw/mixed263/`

Method (from `docs/performance/benchmark-methodology.md` on `main`): 8 writers, each with its own 1,000 identities (distinct `resource`, fixed event/environment/severity), sent in order and wrapped, so the table grows from 1,000 to at most 9,000 rows; 16 readers of `/alerts?sort-by=lastReceiveTime&page-size=50`. Cells: writers alone, readers alone, mixed. Fresh `base_seed` clone per cell and rep; 5 s warm-up, 15 s timed; rates count 2xx answers that ended in the timed phase. A rep is valid only with no error, non-2xx or list-check problem in any phase (2xx, parses, ≤50 alerts, non-increasing `lastReceiveTime`), a live server at the end, every identity's row present iff it had a 2xx with `duplicate_count` = 2xx − 1, and the final `/alerts/count` (total and severity counts) and newest-50 list equal to SQL on the final rows (ids may differ only inside groups of equal millisecond `last_receive_time`). Writers and readers are separate Python processes pinned to cores 6 and 7. CPU = cgroup `usage_usec` delta over the timed window.

| cell | arm | valid | writes/s | write p50/p99 ms | reads/s | read p50/p99 ms | server CPU s/s | PG CPU s/s | server ms/req | PG ms/req | sessions opened | final rows | client CPU (max proc, s/s) |
|---|---|---:|---:|---|---:|---|---:|---:|---:|---:|---|---|---|
| writers-alone | alrust | 3/3 | 7,691.9 | 0.9 / 2.2 | - | - | 2.09 | 1.64 | 0.28 | 0.22 | [14, 14, 13] | [9000, 9000, 9000] | [0.4, 0.44, 0.41] |
| writers-alone | alrust-p32 | 3/3 | 7,724.5 | 0.9 / 2.4 | - | - | 2.12 | 1.64 | 0.28 | 0.21 | [14, 13, 13] | [9000, 9000, 9000] | [0.5, 0.49, 0.41] |
| writers-alone | alrust-p64 | 3/3 | 8,041.8 | 0.9 / 2.0 | - | - | 2.13 | 1.66 | 0.27 | 0.21 | [14, 14, 13] | [9000, 9000, 9000] | [0.43, 0.41, 0.45] |
| writers-alone | uWSGI-5 | 3/3 | 237.7 | 33.1 / 49.3 | - | - | 0.82 | 1.81 | 3.45 | 7.59 | [4826, 4888, 5167] | [5826, 5888, 6167] | [0.04, 0.04, 0.04] |
| writers-alone | gunicorn-8 | 3/3 | 238.8 | 32.7 / 51.1 | - | - | 0.98 | 1.90 | 4.04 | 7.96 | [4824, 4811, 5070] | [5824, 5811, 6070] | [0.05, 0.05, 0.05] |
| readers-alone | alrust | 3/3 | - | - | 1,642.1 | 9.1 / 19.1 | 1.68 | 1.82 | 1.02 | 1.10 | [14, 14, 14] | [1000, 1000, 1000] | [0.4, 0.39, 0.36] |
| readers-alone | alrust-p32 | 3/3 | - | - | 1,551.6 | 9.3 / 24.8 | 1.72 | 1.86 | 1.12 | 1.20 | [30, 30, 30] | [1000, 1000, 1000] | [0.38, 0.42, 0.4] |
| readers-alone | alrust-p64 | 3/3 | - | - | 1,516.4 | 9.5 / 25.3 | 1.71 | 1.86 | 1.14 | 1.21 | [43, 44, 43] | [1000, 1000, 1000] | [0.4, 0.41, 0.39] |
| readers-alone | uWSGI-5 | 3/3 | - | - | 266.5 | 59.7 / 83.5 | 1.16 | 1.66 | 4.35 | 6.22 | [5323, 5295, 5301] | [1000, 1000, 1000] | [0.11, 0.1, 0.1] |
| readers-alone | gunicorn-8 | 3/3 | - | - | 286.3 | 54.5 / 89.3 | 1.46 | 1.94 | 4.99 | 6.77 | [6021, 5275, 5718] | [1000, 1000, 1000] | [0.11, 0.1, 0.11] |
| mixed | alrust | 3/3 | 270.2 | 28.1 / 63.1 | 390.3 | 37.5 / 88.3 | 0.58 | 1.93 | 0.89 | 2.91 | [14, 14, 14] | [7252, 7289, 7378] | [0.16, 0.16, 0.17] |
| mixed | alrust-p32 | 3/3 | 292.6 | 24.4 / 84.1 | 404.3 | 31.7 / 120.6 | 0.62 | 1.91 | 0.91 | 2.77 | [30, 30, 30] | [7377, 7403, 7527] | [0.17, 0.18, 0.17] |
| mixed | alrust-p64 | 3/3 | 325.3 | 21.3 / 83.5 | 396.4 | 34.0 / 123.3 | 0.61 | 1.91 | 0.85 | 2.64 | [62, 62, 62] | [7991, 7673, 7898] | [0.15, 0.16, 0.16] |
| mixed | uWSGI-5 | 3/3 | 100.2 | 76.9 / 125.8 | 130.4 | 119.7 / 171.7 | 0.98 | 1.81 | 4.33 | 7.77 | [4720, 4776, 4605] | [3069, 3082, 2998] | [0.06, 0.07, 0.06] |
| mixed | gunicorn-8 | 3/3 | 77.8 | 91.1 / 199.3 | 152.9 | 93.0 / 197.4 | 1.18 | 1.94 | 5.11 | 8.39 | [4887, 4738, 4742] | [2578, 2667, 2575] | [0.07, 0.07, 0.07] |

Per-rep rates:

- writers-alone alrust: writes/s [7115.4, 7691.9, 8159.5]; reads/s -
- writers-alone alrust-p32: writes/s [7655.5, 7816.4, 7724.5]; reads/s -
- writers-alone alrust-p64: writes/s [8054.1, 7443.0, 8041.8]; reads/s -
- writers-alone uWSGI-5: writes/s [237.4, 237.7, 252.3]; reads/s -
- writers-alone gunicorn-8: writes/s [235.9, 238.8, 243.1]; reads/s -
- readers-alone alrust: writes/s -; reads/s [1580.4, 1642.1, 1693.9]
- readers-alone alrust-p32: writes/s -; reads/s [1563.9, 1511.1, 1551.6]
- readers-alone alrust-p64: writes/s -; reads/s [1571.7, 1437.2, 1516.4]
- readers-alone uWSGI-5: writes/s -; reads/s [266.9, 265.0, 266.5]
- readers-alone gunicorn-8: writes/s -; reads/s [299.1, 261.9, 286.3]
- mixed alrust: writes/s [267.9, 270.8, 270.2]; reads/s [392.4, 390.3, 378.8]
- mixed alrust-p32: writes/s [280.3, 292.6, 293.1]; reads/s [404.3, 412.9, 394.2]
- mixed alrust-p64: writes/s [327.9, 307.5, 325.3]; reads/s [391.5, 406.0, 396.4]
- mixed uWSGI-5: writes/s [99.0, 100.2, 101.5]; reads/s [128.1, 130.4, 131.1]
- mixed gunicorn-8: writes/s [77.8, 81.5, 77.7]; reads/s [159.5, 146.7, 152.9]

The mixed cells are not compared across servers on identical row histories: each server's writers reach a different number of identities (final rows column), so the readers see tables of different sizes, as in the #263 hosted run.

## Unscored cells and why

- None.

## Freeze audit

`scripts/audit/freeze_audit2.py` over every timed file. Flags: logged span > measured + allowance + 20 s; DB sampler > 0.26 s/sample; any error/non-2xx or invalid mixed rep; rate > 12% from its rep median; realtime vs monotonic gap > 1 s (mixed). Final pass: 342 records, 0 flagged. Rounds: pass1: 33 flagged records / 24 groups (72 files) -> raw/_superseded/clean-pass-1-2026-11-18; pass2: 3 groups (9 files) -> raw/_superseded/clean-pass-2-2026-11-18; pass3: 0 flagged.


## Compared with the previous tuned run (alrust `11aede4b`, 2026-11-15/16)

Same box, caps, images for Alerta, and harness for reads and writes. alrust binary and the mixed method differ; the previous mixed case was a fixed write sequence (new/duplicate/correlate mix) beside 16 readers, so mixed numbers are not like for like.

| metric | prev alrust | now alrust | prev uWSGI-5 | now uWSGI-5 | prev gunicorn-8 | now gunicorn-8 |
|---|---:|---:|---:|---:|---:|---:|
| reads list @32 req/s | 1,574 | 1,506 | 279 | 273 | 310 | 294 |
| reads event @32 req/s | 2,586 | 2,009 | 314 | 262 | 318 | 276 |
| reads severity @32 req/s | 2,437 | 2,229 | 319 | 270 | 324 | 305 |
| reads count @32 req/s | 4,798 | 6,617 | 398 | 358 | 397 | 339 |
| reads empty @32 req/s | 3,653 | 4,247 | 342 | 308 | 354 | 305 |
| writes empty @1 alerts/s | 902 | 822 | 39 | 37 | 92 | 85 |
| writes empty @8 alerts/s | 3,450 | 3,690 | 240 | 236 | 262 | 242 |
| writes empty @32 alerts/s | 4,170 | 6,499 | 242 | 255 | 234 | 224 |
| writes seeded @1 alerts/s | 829 | 853 | 38 | 37 | 86 | 83 |
| writes seeded @8 alerts/s | 3,366 | 3,964 | 221 | 212 | 248 | 229 |
| writes seeded @32 alerts/s | 4,296 | 6,684 | 242 | 233 | 221 | 215 |
| mixed write alerts/s (prev method → #263 method, default pool) | 304 | 270 | 84 | 100 | 67 | 78 |
| mixed read p50 ms (prev → #263) | 29.4 | 37.5 | 120.1 | 119.7 | 100.1 | 93.0 |

## Files

- `raw/progress.txt` log; `raw/test1/`, `raw/pool-reads/`, `raw/test2/`, `raw/mixed263/` (+ `identities/`) timed results; `raw/_superseded/` flagged results moved aside (never deleted); `raw/_smoke/` harness smoke tests before the run (not scored).
- `parity-test1.json`, `parity-pool.json`, `freeze-audit.json`, `teardown.json`, `meta.json`, `environment.txt`, `alrust-build.log`.
- Nothing was committed or pushed.
