# Alerta and Alrust comparison: 15 November 2026

Status: one completed laboratory read. On all five scored cases, both recorded Alerta/alrust ratios are greater than 1. This is a client-visible latency comparison of warm authenticated reads. It is not a production capacity, reliability, full-text search, or integration claim.

This run is a separate experiment from the [13 November comparison](2026-11-13-alerta-comparison.md). Do not treat a ratio in this file as comparable with a ratio in that file.

## Exact subjects and environment

The environment capture below is timestamped Sun Nov 15 08:58:54 EST 2026. It is copied here in full so the identifiers stay the capture text.

```
date: Sun Nov 15 08:58:54 EST 2026
uname: Linux grok-bot-vm-413051033 6.12.113+ #1 SMP PREEMPT_DYNAMIC Thu Nov  5 16:04:51 UTC 2026 x86_64 GNU/Linux
nproc: 8
               total        used        free      shared  buff/cache   available
Mem:           16002        6802        1095         464        8899        9199
Swap:              0           0           0

docker client 29.9.3 / server 29.9.3
pg version(): PostgreSQL 18.7 (Debian 18.7-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit
pg image: postgres@sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc
alerta commit: f4312727783dad64198b62ed778bbca98a5144a3
alrust commit: 11aede4b3d04a46d4246b5566c453b9746ae2540
alrust image: sha256:0ca93dc8fd75eadcb87cc60a0448985042247cf1532cb83c978762acf1445ab4
alrust binary sha256: 739102855cd68102adce1dd2e89a0e48a3f2ce368990b62e7cd447b30085968a  /usr/local/bin/alrust
alerta image: sha256:79c31334627638a63002fe78fcbe2d4f8d933bbcd9e817d664bd853631fcacec
--- alerta pip freeze
alerta-server @ file:///src
backports.zstd==1.7.0
bcrypt==5.0.0
blinker==1.9.0
brotli==1.2.0
certifi==2026.10.19
cffi==2.1.1
charset-normalizer==3.5.2
click==8.5.0
cryptography==46.0.6
defusedxml==0.7.1
dnspython==2.8.0
Flask==3.1.3
Flask-Compress==1.23
flask-cors==6.0.2
gunicorn==27.0.1
idna==3.20
itsdangerous==2.2.0
Jinja2==3.1.6
MarkupSafe==3.0.3
mohawk==1.1.0
packaging==26.3
pip==25.0.1
psycopg2==2.9.11
pycparser==3.0
PyJWT==2.12.1
pymongo==4.16.0
pyparsing==3.3.2
python-dateutil==2.9.0.post0
pytz==2026.1.post1
PyYAML==6.0.3
requests==2.32.5
requests-hawk==1.2.1
sentry-sdk==2.56.0
setuptools==84.0.0
six==1.17.0
StrEnum==0.4.15
urllib3==2.8.0
Werkzeug==3.1.7
wheel==0.48.0
```

- Upstream Alerta 9.1.0 at commit `f4312727783dad64198b62ed778bbca98a5144a3`.
- alrust main at commit `11aede4b3d04a46d4246b5566c453b9746ae2540`.
- alrust image `sha256:0ca93dc8fd75eadcb87cc60a0448985042247cf1532cb83c978762acf1445ab4`.
- alrust binary SHA256 `739102855cd68102adce1dd2e89a0e48a3f2ce368990b62e7cd447b30085968a` for `/usr/local/bin/alrust`.
- Alerta image `sha256:79c31334627638a63002fe78fcbe2d4f8d933bbcd9e817d664bd853631fcacec`.
- PostgreSQL version string: `PostgreSQL 18.7 (Debian 18.7-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`.
- PostgreSQL image: `postgres@sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc`.
- Docker: docker client 29.9.3 / server 29.9.3.
- Host: `Linux grok-bot-vm-413051033 6.12.113+ #1 SMP PREEMPT_DYNAMIC Thu Nov  5 16:04:51 UTC 2026 x86_64 GNU/Linux`.
- `nproc` 8. The free(1) line prints Mem total 16002 and Swap 0, with no unit suffix. This note reads that line as about 16 GiB of RAM and no swap. No CPU quota is recorded in the capture.

The Alerta image's pip freeze is the block in the capture, including `alerta-server @ file:///src`, `gunicorn==27.0.1`, `Flask==3.1.3`, and `psycopg2==2.9.11`. The capture does not record a CPython version, Gunicorn worker class or thread count, the alrust pool size, `shared_buffers`, `fsync`, `synchronous_commit`, or `full_page_writes`.

`docs/alpha.md` still describes the alpha quick start on PostgreSQL 18.6. This measurement used the PostgreSQL 18.7 string above. It does not retitle that quick start.

## Deviations from the 13 November comparison

The 13 November document and this document are different experiments. The differences include at least the following.

- PostgreSQL 18.7, package string `18.7-1.pgdg13+2`, in this run. The 13 November comparison used PostgreSQL 18.6, PGDG Debian package `18.6-1.pgdg12+2`.
- This run used `docker run`. The 13 November comparison used Compose.
- This run set `page-size=50` on the list queries.
- This run's empty case is `event=NoSuchEventBench`, with the same sort and page-size. The seed script never assigns that event name. The matched signature records `total` 0. The case is distinct from a bare unfiltered `/alerts`.
- This run used one A→B database clone: 1,000 synthetic alerts were seeded through the Alerta API, then that database was cloned for alrust. Both servers read that PostgreSQL 18.7 data.
- This run's host is an 8 vCPU uncapped box: `nproc` 8, about 16 GiB RAM, no swap, and no CPU quota in the capture. The 13 November comparison used a 1 vCPU / 4 GiB guest under a host quota of 25% of one CPU.
- This run's matched list signature records `total` 1000. The 13 November main table is 10,000 alerts. The 13 November candidate table is 1,000 alerts on that capped guest, under the other differences above.

A ratio from this file is not a before-and-after reading of a ratio from the 13 November file.

## Method

The timed workload is the bench script's warm authenticated read. The client is Python `http.client` on one persistent keep-alive HTTP/1.1 connection per case, target, and repetition. Each sample is `time.perf_counter()` from the request send through the full body read, reported in milliseconds. The connection timeout in the script is 30 seconds. Alerta was dialed at `127.0.0.1:8081` and alrust at `127.0.0.1:8082`. Every request sent `Authorization: Key` and `Accept: application/json`. `docs/alpha.md` states that authenticated key use records usage. Each recorded sample is the client-visible round trip.

The seed script posts synthetic alerts to Alerta `POST /alert` and exits if a response is not HTTP 201. For index `i` it sends `event` `BackupFailed` when `i % 10 == 0`, otherwise a cycle of `HttpError`, `DiskFull`, `CpuHigh`, `NodeDown`, and `MemoryLow`. It sends `severity` `critical` when `i % 7 == 0`, otherwise `major`, `minor`, or `warning` from `i % 3`. Resource, environment, service, group, text, value, origin, tags, and attributes follow that script. This run seeded 1,000 alerts, then made one clone of that database for alrust.

Five cases, in script order:

- `list`: `GET /alerts?sort-by=lastReceiveTime&page-size=50`
- `event`: `GET /alerts?event=BackupFailed&sort-by=lastReceiveTime&page-size=50`
- `severity`: `GET /alerts?severity=critical&sort-by=lastReceiveTime&page-size=50`
- `count`: `GET /alerts/count`
- `empty`: `GET /alerts?event=NoSuchEventBench&sort-by=lastReceiveTime&page-size=50`

Four repetitions. Repetitions 1 and 3 run Alerta, then alrust. Repetitions 2 and 4 run alrust, then Alerta. Within a server position the five cases run in the order above. Each case on that connection has five warmup requests, then 25 timed requests. Warmup samples are excluded from `summary.json`. Each arm and case records `n_timed` 100.

Before the repetitions, one response per target per case is stored as the correctness signature. List signatures compare `total`, `status`, and the response-order tuples `id`, `resource`, `event`, `environment`, `severity`, `status`, `service`, `group`. The count signature compares `total`, `severityCounts`, and `statusCounts`. During warmups and timed requests, a response counts as a failure when its HTTP status is not 200 or its signature differs from the Alerta pre-timing signature. `scored` is true only when the pre-timing signatures match and `response_failures_or_mismatches` is 0. All five cases met that bar. The speed figures below are those scored aggregates.

`summary.json` holds two different median reductions and two different ratios. This document prints both. The 13 November document displayed the first pair only: the median of the four per-run medians, and the median of the four paired Alerta/alrust ratios.

- `per_rep_medians_ms`: the median of the 25 timed samples in that repetition. Array order is repetition 1, 2, 3, 4.
- `median_of_rep_medians_ms`: the median of those four per-repetition medians.
- `median_all_100_ms`: the median of all 100 timed samples for that arm and case.
- `paired_rep_ratios`: each repetition's Alerta `per_rep_medians_ms` divided by that repetition's alrust `per_rep_medians_ms`.
- `median_paired_ratio`: the median of those four paired ratios.
- `ratio_of_all100_medians`: Alerta `median_all_100_ms` divided by alrust `median_all_100_ms`.
- `ratio_range`: the minimum and the maximum of `paired_rep_ratios`, in that order.

A ratio above 1 means that Alerta figure is larger than the corresponding alrust figure. `median_paired_ratio` is not `ratio_of_all100_medians`, and it is not the quotient of the two `median_of_rep_medians_ms` values in the same row. `median_of_rep_medians_ms` is not `median_all_100_ms`. No results figure below is rounded. Each displayed statistic is the named `summary.json` field, copied so the decimal text matches that file, including values such as Alerta list `median_of_rep_medians_ms` `14.267050000000001`. Individual raw samples are not re-aggregated in this document.

## Results

`scored` and `response_failures_or_mismatches` from `summary.json`:

| Case | `scored` | `response_failures_or_mismatches` |
|---|---|---:|
| list (newest 50) | true | 0 |
| event=BackupFailed | true | 0 |
| severity=critical | true | 0 |
| count | true | 0 |
| empty (event=NoSuchEventBench) | true | 0 |

### Median of the four per-repetition medians

These columns are `median_of_rep_medians_ms`, `median_paired_ratio`, and `ratio_range`. This is the reduction the 13 November document displayed. It is not the all-100-sample reduction in the next table.

| Case | Alerta `median_of_rep_medians_ms` | alrust `median_of_rep_medians_ms` | `median_paired_ratio` | `ratio_range` |
|---|---:|---:|---:|---:|
| list (newest 50) | 14.267050000000001 | 2.8424745 | 4.940647428499159 | 4.849812831211849 to 5.11065749861843 |
| event=BackupFailed | 13.77701 | 2.229337 | 6.201308896066273 | 5.995177322337688 to 6.56418543385248 |
| severity=critical | 13.4382255 | 2.0927559999999996 | 6.374065119513119 | 6.0244738829672375 to 7.166433928747656 |
| count | 10.6899085 | 0.772778 | 14.559821660298773 | 12.452013216202856 to 20.948128314558538 |
| empty (event=NoSuchEventBench) | 11.2524 | 0.9519765 | 11.583799599758796 | 10.688391356936307 to 15.639261602704735 |

### Median of all 100 timed samples

These columns are `median_all_100_ms` and `ratio_of_all100_medians`. `n_timed` is 100 for each arm. This ratio is the quotient of the two `median_all_100_ms` values. It is not `median_paired_ratio`.

| Case | Alerta `median_all_100_ms` | alrust `median_all_100_ms` | `ratio_of_all100_medians` | `n_timed` (each arm) |
|---|---:|---:|---:|---:|
| list (newest 50) | 14.1750465 | 2.8035755 | 5.056060198842514 | 100 |
| event=BackupFailed | 13.89717 | 2.216049 | 6.27114743401432 | 100 |
| severity=critical | 13.586643500000001 | 2.1242555000000003 | 6.395955430031839 | 100 |
| count | 10.6680665 | 0.7555084999999999 | 14.12037918832151 | 100 |
| empty (event=NoSuchEventBench) | 11.1715075 | 0.957465 | 11.667797256296575 | 100 |

### Per-repetition medians

`per_rep_medians_ms` in repetition order, with both summary medians repeated so each field stays attached to its name.

| Case | Arm | Rep 1 `per_rep_medians_ms` | Rep 2 `per_rep_medians_ms` | Rep 3 `per_rep_medians_ms` | Rep 4 `per_rep_medians_ms` | `median_of_rep_medians_ms` | `median_all_100_ms` | `n_timed` |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| list (newest 50) | alerta | 13.558161 | 14.297279 | 14.236821 | 14.363366 | 14.267050000000001 | 14.1750465 | 100 |
| list (newest 50) | alrust | 2.795605 | 2.797542 | 2.901442 | 2.887407 | 2.8424745 | 2.8035755 | 100 |
| event=BackupFailed | alerta | 13.700241 | 13.313838 | 13.853779 | 14.476617 | 13.77701 | 13.89717 | 100 |
| event=BackupFailed | alrust | 2.237916 | 2.220758 | 2.11051 | 2.304921 | 2.229337 | 2.216049 | 100 |
| severity=critical | alerta | 13.42803 | 13.448421 | 13.236541 | 14.523495 | 13.4382255 | 13.586643500000001 | 100 |
| severity=critical | alrust | 2.149053 | 2.232298 | 2.036459 | 2.0266 | 2.0927559999999996 | 2.1242555000000003 | 100 |
| count | alerta | 11.328664 | 9.978903 | 10.051153 | 12.561992 | 10.6899085 | 10.6680665 | 100 |
| count | alrust | 0.540796 | 0.759442 | 0.807191 | 0.786114 | 0.772778 | 0.7555084999999999 | 100 |
| empty (event=NoSuchEventBench) | alerta | 11.423482 | 10.597347 | 11.081318 | 11.80489 | 11.2524 | 11.1715075 | 100 |
| empty (event=NoSuchEventBench) | alrust | 0.928304 | 0.975649 | 1.036762 | 0.754824 | 0.9519765 | 0.957465 | 100 |

### Paired ratios

`paired_rep_ratios` in repetition order. `ratio_range` is the minimum and maximum of those four ratios.

| Case | Rep 1 `paired_rep_ratios` | Rep 2 `paired_rep_ratios` | Rep 3 `paired_rep_ratios` | Rep 4 `paired_rep_ratios` | `median_paired_ratio` | `ratio_of_all100_medians` | `ratio_range` |
|---|---:|---:|---:|---:|---:|---:|---:|
| list (newest 50) | 4.849812831211849 | 5.11065749861843 | 4.906808752337631 | 4.974486104660687 | 4.940647428499159 | 5.056060198842514 | 4.849812831211849 to 5.11065749861843 |
| event=BackupFailed | 6.121874547570151 | 5.995177322337688 | 6.56418543385248 | 6.280743244562394 | 6.201308896066273 | 6.27114743401432 | 5.995177322337688 to 6.56418543385248 |
| severity=critical | 6.248347527957663 | 6.0244738829672375 | 6.499782711068576 | 7.166433928747656 | 6.374065119513119 | 6.395955430031839 | 6.0244738829672375 to 7.166433928747656 |
| count | 20.948128314558538 | 13.139782893229505 | 12.452013216202856 | 15.979860427368042 | 14.559821660298773 | 14.12037918832151 | 12.452013216202856 to 20.948128314558538 |
| empty (event=NoSuchEventBench) | 12.305755442182733 | 10.86184375733486 | 10.688391356936307 | 15.639261602704735 | 11.583799599758796 | 11.667797256296575 | 10.688391356936307 to 15.639261602704735 |

## Validation

The pre-timing signatures matched (`match` true in `correctness.json`) for `list`, `event`, `severity`, `count`, and `empty`. The shared signature values recorded there:

- `list`: `total` 1000, `status` `ok`, 50 objects in the stored `alerts` array.
- `event`: `total` 100, `status` `ok`, 50 objects in the stored `alerts` array.
- `severity`: `total` 143, `status` `ok`, 50 objects in the stored `alerts` array.
- `count`: `total` 1000, `severityCounts` `critical` 143, `major` 286, `minor` 285, `warning` 286, `statusCounts` `open` 1000. The count signature has no `alerts` array.
- `empty`: `total` 0, `status` `ok`, 0 objects in the stored `alerts` array.

Alerta and alrust carried the same values on each of those fields. `summary.json` then records `response_failures_or_mismatches` 0 and `scored` true for all five cases, which is the bench script's rule that every warmup and every timed response was HTTP 200 and matched the Alerta pre-timing signature.

The artifacts for this run do not include a formatter run, a Clippy run, a unit-test count, a pilot wall-clock time, a cgroup check, a temperature reading, or a query plan. Those items in the 13 November note stay with that note.

## What this does not establish

This file does not revise the 13 November comparison, the PostgreSQL 18.6 pin recorded as Y32 in the [PostgreSQL evaluation](postgresql-evaluation-2026-11-14.md), or the PostgreSQL 18.6 wording in `docs/alpha.md`. Both arms here used PostgreSQL 18.7, so the ratios are not an 18.6-versus-18.7 result. They are also not a `docker run`-versus-Compose result, and not a result about the 13 November CPU cap.

The empty case scored `event=NoSuchEventBench`. It does not score a bare unfiltered `/alerts`, and it does not score search. `docs/alpha.md` lists `q=` queries as not yet implemented. The list queries set `page-size=50` and `sort-by=lastReceiveTime`. The stored list signatures contain 50 alert objects, with totals 1000, 100, and 143.

The harness is one client, loopback, keep-alive, five warmups, then 25 timed GETs, four repetitions, 100 timed samples per arm per case. `summary.json` contains the medians and ratios defined above and no other percentile. There is no open-loop load test, concurrency sweep, write-throughput comparison, ingest measurement, housekeeping run, fault-recovery run, or failover run in these artifacts. The seed writes happened before the clone and before timing, through Alerta, and are not a write benchmark.

No EXPLAIN plan, buffer counter, CPU profile, or allocation trace is in the capture, so the ratios do not identify a cause. Worker topology and pool size were not recorded. No external provider, webhook, or adapter comparison was part of the five GET cases. The host string is x86_64. This read does not qualify ARM64 or a public release. No temperature, cgroup, or wall-clock duration was captured, and the 13 November thermal note is not extended by this file.
