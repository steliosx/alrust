# Alerta vs alrust concurrency and write benches — incomplete

Status: **incomplete**. Grok Build CLI started this experiment on 15 November 2026 and collected Test 1 results through most concurrency-32 cases. The grok process died mid Test 1 (after `alerta` severity c32 rep 1; `alrust` severity c32 rep 1 was left as an empty file). Around 16:01 America/Nassau the box restarted. After the restart, Docker is not installed (no `docker`/`dockerd` binary, no `/var/run/docker.sock`, no images or containers), `/tmp/oha` is gone, and the disposable PostgreSQL databases were lost with the containers. Test 2 (writes and mixed load) was not executed. This file was filled in from the raw artifacts that survived under `/workspace/alerta-bench-concurrency`. No figure below is invented; every scored number was checked against the oha JSON in `raw/test1/`.

This is not a production capacity, reliability, or tuning claim. It is also not comparable with the 13 November 25%-of-one-CPU cap, and not a drop-in substitute for the 15 November single-client warm-read medians.

## Subjects and environment

Copied from `environment.txt`, captured Sun Nov 15 14:50:48 EST 2026 (America/Nassau), before timing:

- 8 vCPU uncapped box, about 15 GiB RAM, no swap (`nproc` 8).
- Docker 29.9.3 (client and server) at the time of the run; started with `docker run` and `--network host`, not Compose.
- PostgreSQL 18.7 (Debian 18.7-1.pgdg13+2), image `postgres@sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc`. Image defaults were recorded, including `shared_buffers=128MB`, `fsync=on`, `synchronous_commit=on`, `full_page_writes=on`. No custom `postgresql.conf` was applied.
- Alerta 9.1.0 at commit `f4312727783dad64198b62ed778bbca98a5144a3`, image `sha256:79c31334627638a63002fe78fcbe2d4f8d933bbcd9e817d664bd853631fcacec`. Server config identical to the 15 November warm-read bench: gunicorn 27.0.1, `--workers 1 --worker-class gthread --threads 4 --bind 127.0.0.1:8081`. **This is untuned Alerta.**
- alrust at commit `11aede4b3d04a46d4246b5566c453b9746ae2540`, image `sha256:0ca93dc8fd75eadcb87cc60a0448985042247cf1532cb83c978762acf1445ab4`, binary SHA256 `739102855cd68102adce1dd2e89a0e48a3f2ce368990b62e7cd447b30085968a`, bind `127.0.0.1:8082`, defaults, `ALRUST_HOUSEKEEPING_INTERVAL=0`.
- Load tool: **oha 1.17.0** (GitHub release `oha-linux-amd64`, not the pgo build), previously at `/tmp/oha`.
- One A→B clone: 1,000 alerts seeded through the Alerta API, then `CREATE DATABASE … TEMPLATE` for alrust. Both servers read that PostgreSQL 18.7 data during Test 1. No writes during Test 1 timing.
- Auth: a disposable admin API key was sent on every request. The environment capture notes that Alerta's `AUTH_REQUIRED` was left at the image default `false` (the key was still sent and Alerta verifies it when present); alrust `ALRUST_AUTH_REQUIRED` stayed at its default `true`.

## Method (Test 1, what actually ran)

Cases (same paths as the 15 November bench / `bench.py`):

- `list`: `/alerts?sort-by=lastReceiveTime&page-size=50`
- `event`: `/alerts?event=BackupFailed&sort-by=lastReceiveTime&page-size=50`
- `severity`: `/alerts?severity=critical&sort-by=lastReceiveTime&page-size=50`
- `count`: `/alerts/count`
- `empty`: `/alerts?event=NoSuchEventBench&sort-by=lastReceiveTime&page-size=50`

Concurrency 1, 8, 16, and 32. Separate 5 s oha warmup (discarded) then 30 s timed (`oha -z 30s -w`, HTTP/1.1 keep-alive), three repetitions, servers alternated. Before timing, ordered ids and counts were compared; `parity-test1.json` records `all_match: true` (list/event/severity totals 1000/100/143 with 50 returned objects; count total 1000 and matching severity/status counts; empty total 0).

Summary rule, from `test1-reads.json`: for each server, case, and concurrency, the median of the three timed-run `summary.requestsPerSec` values, the median of the three p50 values, and the median of the three p99 values. p50 and p99 are oha `latencyPercentiles` (seconds) multiplied by 1000, not the rounded `metrics.latency_ms` fields. Throughput ratio is median Alerta rps / median alrust rps. A ratio below 1 means alrust had higher throughput. `test1-reads.json` sets `"partial": true`.

All 103 finished timed rows in `test1-reads.csv` / `test1-reads.json` match the corresponding `raw/test1/*.json` files (rps, p50, p99, duration, 2xx, non-2xx, error count). The compact table below uses those file-summary medians. Exact values remain in `test1-reads.json`. Displayed table figures are rounded (rps and ratio to 2 and 4 decimals, latencies to 2 decimals); that rounding is display-only.

## Test 1 results — concurrent reads

Errors (HTTP non-2xx + oha error count) were 0 on every scored cell.

| Case | C | Alerta rps | alrust rps | Alerta/alrust rps | Alerta p50 ms | alrust p50 ms | Alerta p99 ms | alrust p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| newest 50 | 1 | 87.96 | 356.38 | 0.2468 | 11.22 | 2.75 | 14.68 | 3.79 |
| newest 50 | 8 | 216.44 | 2186.99 | 0.0990 | 36.32 | 3.44 | 60.42 | 6.41 |
| newest 50 | 16 | 212.98 | 2645.44 | 0.0805 | 74.30 | 5.84 | 102.54 | 11.01 |
| newest 50 | 32 | 214.73 | 2725.42 | 0.0788 | 146.63 | 11.43 | 176.70 | 18.36 |
| event=BackupFailed | 1 | 93.38 | 458.09 | 0.2039 | 10.70 | 2.10 | 13.06 | 3.26 |
| event=BackupFailed | 8 | 214.48 | 2746.76 | 0.0781 | 36.37 | 2.79 | 61.41 | 5.15 |
| event=BackupFailed | 16 | 219.72 | 3458.54 | 0.0635 | 72.12 | 4.39 | 92.45 | 8.93 |
| event=BackupFailed | 32 | 218.96 | 3534.99 | 0.0619 | 145.47 | 8.77 | 175.69 | 16.56 |
| severity=critical | 1 | 92.57 | 461.72 | 0.2005 | 10.48 | 2.11 | 19.13 | 3.16 |
| severity=critical | 8 | 207.23 | 2633.02 | 0.0787 | 37.79 | 2.90 | 64.60 | 5.24 |
| severity=critical | 16 | 218.21 | 3348.45 | 0.0652 | 72.95 | 4.58 | 95.25 | 9.02 |
| severity=critical | 32 | — | — | — | — | — | — | — |
| /alerts/count | 1 | 145.97 | 1641.53 | 0.0889 | 6.69 | 0.54 | 9.78 | 1.41 |
| /alerts/count | 8 | 510.59 | 8007.05 | 0.0638 | 15.34 | 0.90 | 20.28 | 2.47 |
| /alerts/count | 16 | 498.76 | 7604.05 | 0.0656 | 31.68 | 1.69 | 40.83 | 7.21 |
| /alerts/count | 32 | — | — | — | — | — | — | — |
| empty (NoSuchEventBench) | 1 | 132.68 | 1115.84 | 0.1189 | 7.45 | 0.79 | 9.56 | 1.86 |
| empty (NoSuchEventBench) | 8 | 436.65 | 6037.68 | 0.0723 | 17.93 | 1.23 | 30.10 | 2.75 |
| empty (NoSuchEventBench) | 16 | 442.36 | 6342.42 | 0.0697 | 35.49 | 2.18 | 55.81 | 6.98 |
| empty (NoSuchEventBench) | 32 | — | — | — | — | — | — | — |

Exact scored medians (from `test1-reads.json`):

- list 1: 87.96377889087478 / 356.38332813129267, ratio 0.24682349579065793, p50 11.219577 / 2.74749, p99 14.675964 / 3.790346
- list 8: 216.4438304130254 / 2186.992382204318, ratio 0.09896871711773723, p50 36.321787 / 3.44439, p99 60.422045 / 6.41185
- list 16: 212.97641417171593 / 2645.4412156503804, ratio 0.08050695396735769, p50 74.303216 / 5.837313, p99 102.53760100000001 / 11.006654
- list 32: 214.72846889206573 / 2725.4166812094695, ratio 0.07878739070341889, p50 146.63331399999998 / 11.430671, p99 176.697004 / 18.360970000000002
- event 1: 93.38406572122588 / 458.0909421625244, ratio 0.203854861832423, p50 10.702674 / 2.1039209999999997, p99 13.063684 / 3.256362
- event 8: 214.48012934568527 / 2746.760369855511, ratio 0.07808476185236635, p50 36.373402 / 2.785761, p99 61.405454999999996 / 5.149815
- event 16: 219.71820090595875 / 3458.5366651767376, ratio 0.0635292385702468, p50 72.118978 / 4.39318, p99 92.446688 / 8.932179999999999
- event 32: 218.95596017406584 / 3534.9860327068727, ratio 0.06193969598414593, p50 145.471188 / 8.772866, p99 175.687986 / 16.564220000000002
- severity 1: 92.57019207724112 / 461.71703827206073, ratio 0.20049117620540427, p50 10.481142 / 2.107079, p99 19.131811 / 3.161357
- severity 8: 207.2257656503392 / 2633.0189910055547, ratio 0.07870272351176598, p50 37.787762 / 2.895084, p99 64.596175 / 5.235625
- severity 16: 218.2067790711794 / 3348.4450750306555, ratio 0.06516659947578256, p50 72.95085 / 4.576284, p99 95.253858 / 9.022835
- count 1: 145.9673821920279 / 1641.5277886652293, ratio 0.08892166383044782, p50 6.691313 / 0.5409600000000001, p99 9.784277 / 1.411795
- count 8: 510.59160808411013 / 8007.048008770732, ratio 0.06376777153388116, p50 15.335676 / 0.901384, p99 20.278847 / 2.470845
- count 16: 498.75777862314385 / 7604.053270575958, ratio 0.06559104215551691, p50 31.675165999999997 / 1.6908139999999998, p99 40.828120000000006 / 7.210512
- empty 1: 132.6795168388921 / 1115.8360425101455, ratio 0.11890592505008257, p50 7.454416999999999 / 0.793917, p99 9.563028 / 1.85529
- empty 8: 436.6487593884224 / 6037.684733506663, ratio 0.07232056304053135, p50 17.929985 / 1.2329459999999999, p99 30.100479999999997 / 2.748662
- empty 16: 442.3630706165081 / 6342.421175582332, ratio 0.06974671948932694, p50 35.487144 / 2.180996, p99 55.805876 / 6.9760789999999995

## Unscored cases

These were not scored, and no replacement numbers were filled in:

- `severity` at concurrency 32: only `alerta` rep 1 finished (raw row present; not a complete 3×2 set). `alrust` rep 1 was an empty file. Reps 2 and 3 were not started.
- `/alerts/count` at concurrency 32: no timed files.
- empty filter at concurrency 32: no timed files.

They are unscored because the grok process died mid-run, not because ids or counts differed. Pre-run parity for all five cases matched. Completing those three cells on a newly installed Docker and a newly seeded database would be a different experiment and was not done.

## Test 2 — writes and mixed load

Not run. Scripts `scripts/write_client.py` and `scripts/run_test2.py` were written before the crash, but there is no raw output, no CSV/JSON, and no correctness check for writes. After the box restart the disposable databases no longer exist.

## Deviations from the 15 November warm-read bench

- Concurrent clients and a 30 s duration (plus a discarded 5 s warmup), three reps, with oha 1.17.0, instead of a single keep-alive Python `http.client` connection, five warmups, and 25 timed requests, four reps.
- Test 2 (writes / mixed) was planned and not executed.
- PostgreSQL 18.7, `docker run`, `page-size=50`, empty filter as `event=NoSuchEventBench`, one A→B clone, 8 vCPU uncapped host — same family of deviations already noted on 15 November versus the 13 November document.
- Alerta `AUTH_REQUIRED` left at image default `false` (key still sent). This was recorded in `environment.txt`.
- The run is incomplete; the box restarted and Docker is no longer installed on this host.

## What this does not establish

No write-throughput result, no mixed read-under-write result, no complete c32 picture for severity/count/empty, no production claim, no gunicorn/alrust tuning study, no 100k-row dataset, no p99 SLO, and no comparison against a CPU-capped guest.

## Artifacts

- `environment.txt`, `parity-test1.json`
- `test1-reads.csv`, `test1-reads.json` (partial)
- `raw/test1/`, `raw/test1-warmup/`, `raw/smoke/`
- `scripts/` (including unrun Test 2 helpers)
- This `REPORT.md` (verification after the crash; Grok Build CLI did not finish it)
