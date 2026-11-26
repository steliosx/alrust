# Alerta 9.1.0 vs alrust concurrent benchmark

This is a new full run on an 8 vCPU uncapped machine. It is not a continuation of the incomplete concurrency attempt, and it is not comparable with the 13 November run that capped the process at 25% of one CPU or directly with the 15 November single-client warm-read medians.

## Subjects and setup

Alerta image `alerta-bench:9.1.0` id `sha256:b6c8b4733b3805177c8932598a1aa408bc675b6720224e1b3c1d4406da521271` (digests `['alerta-bench@sha256:b6c8b4733b3805177c8932598a1aa408bc675b6720224e1b3c1d4406da521271']`). This id is a rebuild from Alerta commit `f4312727783dad64198b62ed778bbca98a5144a3` and `/workspace/alerta-bench/alerta-image` (Dockerfile and lock files). It is not the 15 November image `sha256:79c31334627638a63002fe78fcbe2d4f8d933bbcd9e817d664bd853631fcacec`. The image has no git metadata. Before timing, `VERSION`, `alerta/version.py`, `alerta/settings.py`, and `wsgi.py` in the image hashed equal to `/workspace/alerta` at that commit. Image command: `['gunicorn', '--workers', '1', '--worker-class', 'gthread', '--threads', '4', '--bind', '127.0.0.1:8081', 'wsgi:app']`.

alrust image `alrust:alpha` id `sha256:fc2ce3a4791939af4551ddf7691cac270dc19dc64e6a0a5d4eb603fc5cc97655` digests `['alrust@sha256:fc2ce3a4791939af4551ddf7691cac270dc19dc64e6a0a5d4eb603fc5cc97655']`. The image digest may differ from the 15 November image `sha256:0ca93dc8fd75eadcb87cc60a0448985042247cf1532cb83c978762acf1445ab4`. Binary SHA256 of `/usr/local/bin/alrust` is `739102855cd68102adce1dd2e89a0e48a3f2ce368990b62e7cd447b30085968a`, the required `739102855cd68102adce1dd2e89a0e48a3f2ce368990b62e7cd447b30085968a`, from commit `11aede4b3d04a46d4246b5566c453b9746ae2540`. Entrypoint is `/usr/local/bin/alrust` with no extra arguments.

PostgreSQL image id `sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc` digests `['postgres@sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc']`. The required digest `sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc` matched. No custom `postgresql.conf`. Containers used `docker run` on the host network, not Compose.

Load tool for reads: `oha 1.17.0` at `/tmp/oha`. Docker: `client 29.9.3 / server 29.9.3`. Host facts (nproc, memory, uname, PostgreSQL `SHOW` settings, pip freeze, auth probe, and the redacted command lines) are in `environment.txt`.

Server config, unchanged from the previous benches: Alerta gunicorn, 1 worker, worker class gthread, 4 threads, bind `127.0.0.1:8081` (the image CMD). alrust defaults plus `ALRUST_BIND=127.0.0.1:8082`, `ALRUST_HOUSEKEEPING_INTERVAL=0`, and `ALRUST_ADMIN_KEY` set to the same disposable admin key. No pool size, worker count, or Postgres parameter was tuned. This is untuned Alerta. Container memory and CPU limits were left unset. The box is 8 vCPU and was not given a cgroup CPU cap for this run. At capture, `environment.txt` records nproc 8 and free memory total 15Gi, used 4.2Gi, free 5.8Gi, shared 66Mi, buff/cache 5.9Gi, available 11Gi.

Auth: one disposable admin API key was inserted into Alerta's `keys` table, the database was cloned, and both servers were sent `Authorization: Key` on every timed request. The raw key is not in this report. Alerta `AUTH_REQUIRED` was left at the image default `False` in `settings.py`. alrust `ALRUST_AUTH_REQUIRED` was left unset (documented default `true`). The live probe results are in `environment.txt`. On startup, alrust ensures `ALRUST_ADMIN_KEY` under its bootstrap key id, which rewrites that key's id on the alrust database only. Alert rows are not rewritten by that step.

Redacted command lines:

```
docker run -d --name fullbench-pg --network host -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=<redacted> -e POSTGRES_DB=postgres postgres:18.7
docker run -d --name fullbench-alerta --network host -e DATABASE_URL=postgresql://postgres:<redacted>@127.0.0.1:5432/<db> alerta-bench:9.1.0
gunicorn --workers 1 --worker-class gthread --threads 4 --bind 127.0.0.1:8081 wsgi:app
docker run -d --name fullbench-alrust --network host -e DATABASE_URL=postgresql://postgres:<redacted>@127.0.0.1:5432/<db> -e ALRUST_BIND=127.0.0.1:8082 -e ALRUST_ADMIN_KEY=<redacted> -e ALRUST_HOUSEKEEPING_INTERVAL=0 alrust:alpha
```

## Method

Test 1 seeded 1,000 synthetic alerts through the Alerta API with `/workspace/alerta-bench/seed.py`, then `CREATE DATABASE alrust_t1 TEMPLATE alerta_t1`. Both servers read that PostgreSQL 18.7 data. No alert writes were issued during Test 1 timing. Authenticated reads can still update the `keys` use counter; that is part of the authenticated GET path and was not disabled.

Before scoring, each read case was fetched once from both servers. List cases compared HTTP status, `total`, and ordered ids. The count case compared HTTP status, `total`, `severityCounts`, and `statusCounts`. A mismatch was not scored. SQL `COUNT(*)`, `SUM(duplicate_count)`, and `md5` of ordered ids were also recorded.

Read cases: newest 50 (`/alerts?sort-by=lastReceiveTime&page-size=50`), `event=BackupFailed` with the same sort and page size, `severity=critical` with the same sort and page size, `/alerts/count`, and empty `event=NoSuchEventBench` with the same sort and page size. Concurrency 1, 8, 16, and 32. Each server, case, level, and repetition ran a discarded 5 second oha warmup (saved under `raw/test1-warmup/`) and then a 30 second timed oha run (`-z 30s -w --http-version 1.1`, keep-alive left on, request timeout 60s). Three repetitions. Which server ran first follows `(rep + case_index + level_index) % 2`, which splits the 60 pairs evenly.

Read metrics come from oha JSON: requests/sec is `summary.requestsPerSec`; p50 and p99 are `latencyPercentiles` seconds times 1000, not the rounded `metrics.latency_ms` fields; duration is `summary.total`; request count is the sum of `statusCodeDistribution`; error count is the sum of `errorDistribution` (0 when that object is empty); non-2xx is the sum of status codes outside 200-299. The summary of a server, case, and concurrency is the median of the three timed values. Throughput ratio is median Alerta rps / median alrust rps. Below 1 means alrust had higher throughput.

Test 2 used fresh databases, not the Test 1 databases. `base_empty` is schema plus the admin key. `base_seed` is that database plus the same 1,000-alert seed. Before every probe, sizing pilot, and repetition, the server's working database was dropped and recreated from the template for that starting condition.

Writes are a closed-loop HTTP/1.1 keep-alive client, one connection per worker, not oha. oha cannot replay one identical POST sequence to two servers with different throughput. Each worker's index `n` uses a 4/3/3 mix: `n % 10` in 0-3 is a new alert (`wNN-new-{n}`), 4-6 is a duplicate with identical fields on one of three stable resources, 7-9 is a correlate on one of three stable resources whose severity cycles major, critical, minor, warning and whose value changes. Resource names include the worker id. Indexes are sent in order, so duplicate and correlate updates do not race across connections. The first hit of a duplicate or correlate slot creates the row.

A 5 second probe on each server, after a template reset, identified the slower server. A fresh 5 second pilot on that server sized the shared sequence: `n_warm = round_up(half_up(rps * 5), concurrency)` and `n_timed = round_up(half_up(rps * 30), concurrency)`, where `half_up` is `floor(x + 0.5)` and `round_up` is the next multiple of the concurrency. Both servers then received that same warmup prefix and the same timed suffix. Connections were opened before the timer. alerts/sec is HTTP 201 count divided by the timed-phase wall seconds. p50 and p99 are nearest-rank percentiles of those 201 latencies (`rank = ceil(p/100 * N)`, 1-based). A safety deadline (120 seconds for warmup, 240 seconds for the timed phase) stops a hung phase; unsent requests are not offered. Concurrency 1, 8, and 32, three repetitions, empty and seeded. Odd and even pair indexes split which server runs first 9/9.

After each timed write repetition, SQL on each working database computed `COUNT(*)`, `SUM(duplicate_count)`, and, as a non-gating extra, `SUM(cardinality(history))`. A repetition is scored only when the API sample agreed, both servers were offered the full warmup and timed sequences, both accepted at least one HTTP 201, and row counts and `duplicate_count` sums matched. The level is scored only when all three repetitions meet that bar. If only HTTP errors differ and the aggregates still match, the level stays scored and the error counts are reported.

The API sample, saved in `raw/test2/api-samples.json`, posted worker-0 indexes 0, 4, 4, 7, and 17 to each server on an empty database. Scoring requires the same HTTP status, resource, event, severity, value, alert status, duplicateCount, and the same SQL row and duplicate sums. A difference in the `repeat` field is recorded and is not by itself a scoring failure.

The mixed run is 8 writers and 16 readers, three repetitions, starting from `base_seed`. Writers send the seeded concurrency-8 sequence. Readers loop on the newest-50 list for the warmup phase and again for the timed phase. Reported read p50, p99, and errors are from the timed phase only. Odd repetitions run Alerta first; the even repetition runs alrust first.

Resumability: each warmup and timed cell is written to its own JSON file as soon as that command finishes, via a temporary file and `os.replace`. A later process skips any timed file that exists, is non-empty, and parses as JSON. Finished timed files are not overwritten. CSV and JSON summaries are rebuilt from those files after each new timed cell. `scripts/run_all.py` drives the whole matrix.

Test 1 timed cells complete: True. Test 1 summary partial: False.
Test 2 write cells complete: True. Write summary partial: False.
Test 2 mixed cells complete: True. Mixed summary partial: False.

## Test 1 — concurrent reads

Parity `all_match` is True. SQL fingerprint match is True.

Display rounding: requests/sec use Python format with 2 decimal places; the throughput ratio uses 3 decimal places; milliseconds use 3 decimal places. Python format rounds ties to even. The median of three values is the middle raw value. Error and non-2xx columns are the sums of the three repetitions, so 0 means each repetition recorded 0. Exact inputs and medians follow the table.

| case | c | alerta rps | alrust rps | ratio | alerta p50 ms | alrust p50 ms | alerta p99 ms | alrust p99 ms | alerta errors | alrust errors | alerta non-2xx | alrust non-2xx | scored |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| list | 1 | 81.17 | 354.95 | 0.229 | 12.111 | 2.741 | 16.007 | 4.165 | 0 | 0 | 0 | 0 | yes |
| list | 8 | 196.55 | 2101.15 | 0.094 | 40.176 | 3.655 | 56.322 | 6.625 | 0 | 0 | 0 | 0 | yes |
| list | 16 | 190.19 | 2681.65 | 0.071 | 83.130 | 5.735 | 109.417 | 10.842 | 0 | 0 | 0 | 0 | yes |
| list | 32 | 199.24 | 2730.69 | 0.073 | 159.041 | 11.487 | 187.105 | 17.316 | 0 | 0 | 0 | 0 | yes |
| event | 1 | 88.78 | 442.94 | 0.200 | 11.177 | 2.165 | 14.815 | 3.495 | 0 | 0 | 0 | 0 | yes |
| event | 8 | 211.66 | 2652.82 | 0.080 | 37.254 | 2.893 | 51.291 | 5.214 | 0 | 0 | 0 | 0 | yes |
| event | 16 | 195.92 | 3107.89 | 0.063 | 80.690 | 4.934 | 108.263 | 9.428 | 0 | 0 | 0 | 0 | yes |
| event | 32 | 201.61 | 3416.22 | 0.059 | 157.862 | 9.105 | 187.296 | 14.693 | 0 | 0 | 0 | 0 | yes |
| severity | 1 | 88.28 | 485.45 | 0.182 | 11.189 | 1.991 | 14.504 | 3.292 | 0 | 0 | 0 | 0 | yes |
| severity | 8 | 211.00 | 2925.05 | 0.072 | 37.495 | 2.616 | 49.857 | 4.763 | 0 | 0 | 0 | 0 | yes |
| severity | 16 | 199.49 | 3526.49 | 0.057 | 79.368 | 4.322 | 101.076 | 8.829 | 0 | 0 | 0 | 0 | yes |
| severity | 32 | 200.30 | 3581.64 | 0.056 | 157.779 | 8.666 | 186.586 | 14.409 | 0 | 0 | 0 | 0 | yes |
| count | 1 | 137.48 | 1595.62 | 0.086 | 7.106 | 0.558 | 9.945 | 1.425 | 0 | 0 | 0 | 0 | yes |
| count | 8 | 561.10 | 9024.87 | 0.062 | 14.124 | 0.784 | 18.001 | 2.775 | 0 | 0 | 0 | 0 | yes |
| count | 16 | 527.90 | 8274.51 | 0.064 | 30.009 | 1.567 | 36.438 | 6.710 | 0 | 0 | 0 | 0 | yes |
| count | 32 | 531.43 | 8586.60 | 0.062 | 59.566 | 3.289 | 74.695 | 8.937 | 0 | 0 | 0 | 0 | yes |
| empty | 1 | 123.42 | 1066.39 | 0.116 | 7.987 | 0.869 | 10.624 | 1.925 | 0 | 0 | 0 | 0 | yes |
| empty | 8 | 459.97 | 6453.38 | 0.071 | 17.105 | 1.118 | 22.272 | 3.033 | 0 | 0 | 0 | 0 | yes |
| empty | 16 | 480.26 | 6863.64 | 0.070 | 33.064 | 2.078 | 39.203 | 6.345 | 0 | 0 | 0 | 0 | yes |
| empty | 32 | 476.10 | 7001.38 | 0.068 | 66.611 | 4.172 | 87.491 | 9.564 | 0 | 0 | 0 | 0 | yes |

Exact Test 1 summary values (from `test1-reads.json`, rebuilt from `raw/test1/*.json`):

```json
{
 "list": {
  "parity_match": true,
  "levels": {
   "1": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.22867833976126994,
    "ratio_inputs": {
     "alerta_median_rps": 81.17006265186191,
     "alrust_median_rps": 354.95299964395343
    },
    "alerta": {
     "rps": [
      85.45094434803053,
      81.17006265186191,
      80.15786741158557
     ],
     "p50_ms": [
      11.449768,
      12.111434000000001,
      12.275703
     ],
     "p99_ms": [
      15.94381,
      16.684864,
      16.006773
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      2564,
      2436,
      2405
     ],
     "duration_s": [
      30.00551977,
      30.011064676,
      30.003293222
     ],
     "median_rps": 81.17006265186191,
     "median_p50_ms": 12.111434000000001,
     "median_p99_ms": 16.006773,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      346.7522462977798,
      354.95299964395343,
      356.67515514520056
     ],
     "p50_ms": [
      2.776969,
      2.741202,
      2.737449
     ],
     "p99_ms": [
      4.216365,
      4.165101,
      4.053529
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      10404,
      10650,
      10702
     ],
     "duration_s": [
      30.004131512,
      30.003972387,
      30.004893376
     ],
     "median_rps": 354.95299964395343,
     "median_p50_ms": 2.741202,
     "median_p99_ms": 4.165101,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "8": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.09354630156010084,
    "ratio_inputs": {
     "alerta_median_rps": 196.5548090269484,
     "alrust_median_rps": 2101.1499733174114
    },
    "alerta": {
     "rps": [
      197.39857726849522,
      196.5548090269484,
      193.59524231021513
     ],
     "p50_ms": [
      40.098203,
      40.175774,
      40.660234
     ],
     "p99_ms": [
      55.334931,
      56.321806,
      62.336757
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      5928,
      5904,
      5816
     ],
     "duration_s": [
      30.030611578,
      30.037423298,
      30.042060593
     ],
     "median_rps": 196.5548090269484,
     "median_p50_ms": 40.175774,
     "median_p99_ms": 56.321806,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      2101.1499733174114,
      2038.6706196176137,
      2160.4182820073606
     ],
     "p50_ms": [
      3.654546,
      3.7636819999999997,
      3.568348
     ],
     "p99_ms": [
      6.624949,
      6.946128,
      6.292858
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      63048,
      61169,
      64823
     ],
     "duration_s": [
      30.006425434,
      30.004356472,
      30.004837739
     ],
     "median_rps": 2101.1499733174114,
     "median_p50_ms": 3.654546,
     "median_p99_ms": 6.624949,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "16": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.07092223656356483,
    "ratio_inputs": {
     "alerta_median_rps": 190.18885635947362,
     "alrust_median_rps": 2681.6533935589405
    },
    "alerta": {
     "rps": [
      189.6695632838607,
      190.18885635947362,
      195.81628352019237
     ],
     "p50_ms": [
      83.129667,
      83.129939,
      81.108265
     ],
     "p99_ms": [
      109.84952000000001,
      109.417288,
      102.197053
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      5706,
      5724,
      5889
     ],
     "duration_s": [
      30.083899078,
      30.096400544,
      30.0741077
     ],
     "median_rps": 190.18885635947362,
     "median_p50_ms": 83.129667,
     "median_p99_ms": 109.417288,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      2765.012989870623,
      2681.6533935589405,
      2594.248737293474
     ],
     "p50_ms": [
      5.570318,
      5.7349630000000005,
      5.919157
     ],
     "p99_ms": [
      10.540108,
      10.842051999999999,
      11.541423
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      82966,
      80465,
      77848
     ],
     "duration_s": [
      30.005645653,
      30.005742052,
      30.007916697
     ],
     "median_rps": 2681.6533935589405,
     "median_p50_ms": 5.7349630000000005,
     "median_p99_ms": 10.842051999999999,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "32": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.07296356434016134,
    "ratio_inputs": {
     "alerta_median_rps": 199.24116428786854,
     "alrust_median_rps": 2730.693957863572
    },
    "alerta": {
     "rps": [
      198.65692781724493,
      199.24116428786854,
      202.52149134036162
     ],
     "p50_ms": [
      159.6894,
      159.040977,
      156.529451
     ],
     "p99_ms": [
      187.10539200000002,
      187.669631,
      185.24755100000002
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      5990,
      6011,
      6106
     ],
     "duration_s": [
      30.152484818,
      30.16946835,
      30.14988661
     ],
     "median_rps": 199.24116428786854,
     "median_p50_ms": 159.040977,
     "median_p99_ms": 187.10539200000002,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      2691.8137441037757,
      2730.693957863572,
      2748.667558436031
     ],
     "p50_ms": [
      11.671126000000001,
      11.486675,
      11.378554000000001
     ],
     "p99_ms": [
      17.290223,
      17.315652,
      17.691258
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      80792,
      81949,
      82493
     ],
     "duration_s": [
      30.013963699,
      30.010320184,
      30.011996084
     ],
     "median_rps": 2730.693957863572,
     "median_p50_ms": 11.486675,
     "median_p99_ms": 17.315652,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   }
  }
 },
 "event": {
  "parity_match": true,
  "levels": {
   "1": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.20042584595023438,
    "ratio_inputs": {
     "alerta_median_rps": 88.77565824124582,
     "alrust_median_rps": 442.93518044219087
    },
    "alerta": {
     "rps": [
      85.24223869319226,
      88.77565824124582,
      88.9364646051758
     ],
     "p50_ms": [
      11.5479,
      11.176515,
      11.060637
     ],
     "p99_ms": [
      15.181577,
      14.120874,
      14.815050999999999
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      2558,
      2664,
      2669
     ],
     "duration_s": [
      30.008597137,
      30.008225822,
      30.010187743
     ],
     "median_rps": 88.77565824124582,
     "median_p50_ms": 11.176515,
     "median_p99_ms": 14.815050999999999,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      436.88356499932434,
      454.64539496638605,
      442.93518044219087
     ],
     "p50_ms": [
      2.165494,
      2.13025,
      2.173367
     ],
     "p99_ms": [
      3.495297,
      3.416011,
      3.52291
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      13108,
      13641,
      13289
     ],
     "duration_s": [
      30.003417501,
      30.00360314,
      30.002132562
     ],
     "median_rps": 442.93518044219087,
     "median_p50_ms": 2.165494,
     "median_p99_ms": 3.495297,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "8": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.07978762410401019,
    "ratio_inputs": {
     "alerta_median_rps": 211.66236038867493,
     "alrust_median_rps": 2652.821947834346
    },
    "alerta": {
     "rps": [
      212.47584857701915,
      211.66236038867493,
      207.65774407385555
     ],
     "p50_ms": [
      37.254027,
      37.242652,
      38.026208
     ],
     "p99_ms": [
      49.678748,
      51.290658,
      53.388758
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      6382,
      6357,
      6237
     ],
     "duration_s": [
      30.036354921,
      30.03368189,
      30.034998347
     ],
     "median_rps": 211.66236038867493,
     "median_p50_ms": 37.254027,
     "median_p99_ms": 51.290658,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      2632.625392420802,
      2652.821947834346,
      2657.724385010517
     ],
     "p50_ms": [
      2.9200060000000003,
      2.8930789999999997,
      2.868903
     ],
     "p99_ms": [
      5.214275,
      5.194444000000001,
      5.529921
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      78988,
      79594,
      79741
     ],
     "duration_s": [
      30.003509131,
      30.003521369,
      30.003487363
     ],
     "median_rps": 2652.821947834346,
     "median_p50_ms": 2.8930789999999997,
     "median_p99_ms": 5.214275,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "16": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.06304017270684552,
    "ratio_inputs": {
     "alerta_median_rps": 195.92169635536138,
     "alrust_median_rps": 3107.88641500797
    },
    "alerta": {
     "rps": [
      196.2504426401223,
      195.92169635536138,
      195.58072583690674
     ],
     "p50_ms": [
      80.59930999999999,
      80.689707,
      81.04191800000001
     ],
     "p99_ms": [
      103.962848,
      108.262985,
      109.017846
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      5903,
      5894,
      5882
     ],
     "duration_s": [
      30.078913049,
      30.083447161,
      30.074538147
     ],
     "median_rps": 195.92169635536138,
     "median_p50_ms": 80.689707,
     "median_p99_ms": 108.262985,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      3094.549789724782,
      3144.946070045825,
      3107.88641500797
     ],
     "p50_ms": [
      4.968024,
      4.887822,
      4.934347000000001
     ],
     "p99_ms": [
      9.461976,
      9.395917,
      9.428063
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      92855,
      94371,
      93261
     ],
     "duration_s": [
      30.005980291,
      30.007191824,
      30.007853424
     ],
     "median_rps": 3107.88641500797,
     "median_p50_ms": 4.934347000000001,
     "median_p99_ms": 9.428063,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "32": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.05901515746245257,
    "ratio_inputs": {
     "alerta_median_rps": 201.60888981729934,
     "alrust_median_rps": 3416.2221789473274
    },
    "alerta": {
     "rps": [
      201.60888981729934,
      205.74780436739388,
      196.46962604570135
     ],
     "p50_ms": [
      157.862475,
      154.269265,
      161.333159
     ],
     "p99_ms": [
      187.296314,
      181.969565,
      200.682614
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      6078,
      6207,
      5927
     ],
     "duration_s": [
      30.147480131,
      30.168001156,
      30.167513011
     ],
     "median_rps": 201.60888981729934,
     "median_p50_ms": 157.862475,
     "median_p99_ms": 187.296314,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      3233.6963075837825,
      3429.6535177349083,
      3416.2221789473274
     ],
     "p50_ms": [
      9.657903000000001,
      9.060063999999999,
      9.105371
     ],
     "p99_ms": [
      15.243504999999999,
      14.693014000000002,
      14.656331
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      97043,
      102926,
      102525
     ],
     "duration_s": [
      30.009930052,
      30.010611704,
      30.011221352
     ],
     "median_rps": 3416.2221789473274,
     "median_p50_ms": 9.105371,
     "median_p99_ms": 14.693014000000002,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   }
  }
 },
 "severity": {
  "parity_match": true,
  "levels": {
   "1": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.18184742249296634,
    "ratio_inputs": {
     "alerta_median_rps": 88.27810484952813,
     "alrust_median_rps": 485.4515045597781
    },
    "alerta": {
     "rps": [
      88.27810484952813,
      88.34180749994597,
      86.74721380348983
     ],
     "p50_ms": [
      11.230092,
      11.164154,
      11.188946999999999
     ],
     "p99_ms": [
      14.273258,
      14.504298,
      15.159341
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      2649,
      2651,
      2603
     ],
     "duration_s": [
      30.007440741,
      30.008441926,
      30.006727431
     ],
     "median_rps": 88.27810484952813,
     "median_p50_ms": 11.188946999999999,
     "median_p99_ms": 14.504298,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      485.4515045597781,
      482.4964117589959,
      485.7917417551851
     ],
     "p50_ms": [
      1.991493,
      2.000982,
      1.988035
     ],
     "p99_ms": [
      3.3120290000000003,
      3.287362,
      3.2915200000000002
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      14565,
      14476,
      14575
     ],
     "duration_s": [
      30.002996928,
      30.002295659,
      30.002568482
     ],
     "median_rps": 485.4515045597781,
     "median_p50_ms": 1.991493,
     "median_p99_ms": 3.2915200000000002,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "8": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.07213643526644042,
    "ratio_inputs": {
     "alerta_median_rps": 211.00279653090797,
     "alrust_median_rps": 2925.0516157550064
    },
    "alerta": {
     "rps": [
      205.36411810309065,
      215.8586960391681,
      211.00279653090797
     ],
     "p50_ms": [
      38.287396,
      36.706412,
      37.495424
     ],
     "p99_ms": [
      56.305802,
      47.151341,
      49.856506
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      6168,
      6483,
      6337
     ],
     "duration_s": [
      30.034458098,
      30.033536378,
      30.03277731
     ],
     "median_rps": 211.00279653090797,
     "median_p50_ms": 37.495424,
     "median_p99_ms": 49.856506,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      2947.733658738794,
      2925.0516157550064,
      2916.861367418462
     ],
     "p50_ms": [
      2.5935770000000002,
      2.615806,
      2.6163190000000003
     ],
     "p99_ms": [
      4.706952,
      4.783182,
      4.762525
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      88442,
      87763,
      87518
     ],
     "duration_s": [
      30.003389125,
      30.003914983,
      30.004168514
     ],
     "median_rps": 2925.0516157550064,
     "median_p50_ms": 2.615806,
     "median_p99_ms": 4.762525,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "16": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.05657020909356599,
    "ratio_inputs": {
     "alerta_median_rps": 199.49453426815097,
     "alrust_median_rps": 3526.4945536650043
    },
    "alerta": {
     "rps": [
      202.1724313382249,
      199.49453426815097,
      197.83254584293257
     ],
     "p50_ms": [
      78.320704,
      79.368392,
      80.13799300000001
     ],
     "p99_ms": [
      99.08256999999999,
      102.164924,
      101.076342
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      6080,
      6000,
      5949
     ],
     "duration_s": [
      30.073338683,
      30.076011967,
      30.070886338
     ],
     "median_rps": 199.49453426815097,
     "median_p50_ms": 79.368392,
     "median_p99_ms": 101.076342,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      3333.6659752981477,
      3567.394098726934,
      3526.4945536650043
     ],
     "p50_ms": [
      4.600928,
      4.280437,
      4.322125
     ],
     "p99_ms": [
      9.073349,
      8.631031,
      8.828892
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      100030,
      107036,
      105813
     ],
     "duration_s": [
      30.006005623,
      30.00397406,
      30.005150551
     ],
     "median_rps": 3526.4945536650043,
     "median_p50_ms": 4.322125,
     "median_p99_ms": 8.828892,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "32": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.05592393937504457,
    "ratio_inputs": {
     "alerta_median_rps": 200.29947191889863,
     "alrust_median_rps": 3581.64096015525
    },
    "alerta": {
     "rps": [
      204.43949408084669,
      200.29947191889863,
      200.08360708524907
     ],
     "p50_ms": [
      154.76275,
      157.77930600000002,
      158.45289300000002
     ],
     "p99_ms": [
      186.58596599999998,
      193.82403200000002,
      185.744986
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      6163,
      6040,
      6035
     ],
     "duration_s": [
      30.145838639,
      30.15484735,
      30.162391052
     ],
     "median_rps": 200.29947191889863,
     "median_p50_ms": 157.77930600000002,
     "median_p99_ms": 186.58596599999998,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      3659.16825581389,
      3581.64096015525,
      3376.170327377818
     ],
     "p50_ms": [
      8.513285,
      8.666229,
      9.203782
     ],
     "p99_ms": [
      13.521571,
      14.408541,
      15.111572
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      109807,
      107488,
      101318
     ],
     "duration_s": [
      30.008732128,
      30.010824981,
      30.00974186
     ],
     "median_rps": 3581.64096015525,
     "median_p50_ms": 8.666229,
     "median_p99_ms": 14.408541,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   }
  }
 },
 "count": {
  "parity_match": true,
  "levels": {
   "1": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.08615859733664792,
    "ratio_inputs": {
     "alerta_median_rps": 137.47637865739506,
     "alrust_median_rps": 1595.619971855309
    },
    "alerta": {
     "rps": [
      137.39748168174776,
      137.47637865739506,
      141.60365463751822
     ],
     "p50_ms": [
      7.142187000000001,
      7.105963,
      6.964982
     ],
     "p99_ms": [
      9.94521,
      10.429730999999999,
      9.470058
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      4123,
      4125,
      4249
     ],
     "duration_s": [
      30.007828015,
      30.005154633,
      30.006287697
     ],
     "median_rps": 137.47637865739506,
     "median_p50_ms": 7.105963,
     "median_p99_ms": 9.94521,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      1595.619971855309,
      1531.8949420974538,
      1679.254573381463
     ],
     "p50_ms": [
      0.55774,
      0.580724,
      0.527614
     ],
     "p99_ms": [
      1.4249779999999999,
      1.48038,
      1.390397
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      47873,
      45961,
      50383
     ],
     "duration_s": [
      30.002758078,
      30.002710197,
      30.003193559
     ],
     "median_rps": 1595.619971855309,
     "median_p50_ms": 0.55774,
     "median_p99_ms": 1.4249779999999999,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "8": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.06217248200129903,
    "ratio_inputs": {
     "alerta_median_rps": 561.0986145795857,
     "alrust_median_rps": 9024.870755004797
    },
    "alerta": {
     "rps": [
      557.3623671799256,
      565.7295935883627,
      561.0986145795857
     ],
     "p50_ms": [
      14.174495,
      13.897764,
      14.123778
     ],
     "p99_ms": [
      18.000964,
      19.461018,
      17.446351
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      16728,
      16979,
      16840
     ],
     "duration_s": [
      30.012790574,
      30.012571717,
      30.012549599
     ],
     "median_rps": 561.0986145795857,
     "median_p50_ms": 14.123778,
     "median_p99_ms": 18.000964,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      9024.870755004797,
      9059.289957158628,
      8740.439291804494
     ],
     "p50_ms": [
      0.783795,
      0.774209,
      0.797304
     ],
     "p99_ms": [
      2.705813,
      2.775203,
      2.888819
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      270777,
      271807,
      262248
     ],
     "duration_s": [
      30.003421362,
      30.003124007,
      30.003983924
     ],
     "median_rps": 9024.870755004797,
     "median_p50_ms": 0.783795,
     "median_p99_ms": 2.775203,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "16": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.06379865707303568,
    "ratio_inputs": {
     "alerta_median_rps": 527.9025467493905,
     "alrust_median_rps": 8274.508758782431
    },
    "alerta": {
     "rps": [
      527.9025467493905,
      541.4766527201244,
      526.8059621084276
     ],
     "p50_ms": [
      30.009415,
      29.264419999999998,
      30.019947000000002
     ],
     "p99_ms": [
      36.438429,
      35.28911,
      37.324765
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      15853,
      16261,
      15820
     ],
     "duration_s": [
      30.030163896,
      30.030842361,
      30.03003219
     ],
     "median_rps": 527.9025467493905,
     "median_p50_ms": 30.009415,
     "median_p99_ms": 36.438429,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      8120.7060171914445,
      8364.13066281741,
      8274.508758782431
     ],
     "p50_ms": [
      1.595555,
      1.548629,
      1.56653
     ],
     "p99_ms": [
      6.83405,
      6.631106,
      6.709645
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      243650,
      250954,
      248264
     ],
     "duration_s": [
      30.003548889,
      30.003596323,
      30.003472984
     ],
     "median_rps": 8274.508758782431,
     "median_p50_ms": 1.56653,
     "median_p99_ms": 6.709645,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "32": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.06189081515624719,
    "ratio_inputs": {
     "alerta_median_rps": 531.4319433237117,
     "alrust_median_rps": 8586.60436095532
    },
    "alerta": {
     "rps": [
      557.091029735543,
      531.4319433237117,
      513.179510179487
     ],
     "p50_ms": [
      56.854743,
      59.566295,
      60.865926
     ],
     "p99_ms": [
      69.410599,
      74.695123,
      85.676065
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      16745,
      15974,
      15426
     ],
     "duration_s": [
      30.0579243,
      30.058411431,
      30.059656892
     ],
     "median_rps": 531.4319433237117,
     "median_p50_ms": 59.566295,
     "median_p99_ms": 74.695123,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      8586.60436095532,
      8447.278676387863,
      8704.363202392035
     ],
     "p50_ms": [
      3.289067,
      3.360558,
      3.268212
     ],
     "p99_ms": [
      9.150673,
      8.937477,
      8.596288000000001
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      257639,
      253458,
      261174
     ],
     "duration_s": [
      30.004759643,
      30.004692601,
      30.004951991
     ],
     "median_rps": 8586.60436095532,
     "median_p50_ms": 3.289067,
     "median_p99_ms": 8.937477,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   }
  }
 },
 "empty": {
  "parity_match": true,
  "levels": {
   "1": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.11573916330592288,
    "ratio_inputs": {
     "alerta_median_rps": 123.4236248803697,
     "alrust_median_rps": 1066.39465289839
    },
    "alerta": {
     "rps": [
      123.4236248803697,
      123.24872739945629,
      126.38180788219714
     ],
     "p50_ms": [
      7.987070000000001,
      8.008809000000001,
      7.79321
     ],
     "p99_ms": [
      10.738819,
      10.62386,
      10.508134
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      3703,
      3698,
      3792
     ],
     "duration_s": [
      30.002359788,
      30.004366601,
      30.004318371
     ],
     "median_rps": 123.4236248803697,
     "median_p50_ms": 7.987070000000001,
     "median_p99_ms": 10.62386,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      1066.39465289839,
      1035.8800842527032,
      1084.43058758119
     ],
     "p50_ms": [
      0.869467,
      0.889601,
      0.85192
     ],
     "p99_ms": [
      1.919222,
      1.957473,
      1.924827
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      31995,
      31079,
      32537
     ],
     "duration_s": [
      30.002963643,
      30.002507503,
      30.003764531
     ],
     "median_rps": 1066.39465289839,
     "median_p50_ms": 0.869467,
     "median_p99_ms": 1.924827,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "8": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.07127603949115427,
    "ratio_inputs": {
     "alerta_median_rps": 459.9716650051901,
     "alrust_median_rps": 6453.384170739102
    },
    "alerta": {
     "rps": [
      481.5363968961771,
      458.5236415958569,
      459.9716650051901
     ],
     "p50_ms": [
      16.330288,
      17.144154,
      17.104952
     ],
     "p99_ms": [
      22.118005,
      22.272437,
      22.635124
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      14454,
      13764,
      13807
     ],
     "duration_s": [
      30.016422628,
      30.018081406,
      30.017066377
     ],
     "median_rps": 459.9716650051901,
     "median_p50_ms": 17.104952,
     "median_p99_ms": 22.272437,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      6453.384170739102,
      6549.2310346999075,
      6397.868326052467
     ],
     "p50_ms": [
      1.110871,
      1.117921,
      1.1610369999999999
     ],
     "p99_ms": [
      3.2082669999999998,
      3.033058,
      2.783167
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      193622,
      196500,
      191952
     ],
     "duration_s": [
      30.003172735,
      30.003522392,
      30.002493052
     ],
     "median_rps": 6453.384170739102,
     "median_p50_ms": 1.117921,
     "median_p99_ms": 3.033058,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "16": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.0699719046606175,
    "ratio_inputs": {
     "alerta_median_rps": 480.26168914214645,
     "alrust_median_rps": 6863.636076101463
    },
    "alerta": {
     "rps": [
      482.70328467565594,
      480.26168914214645,
      464.97450113198084
     ],
     "p50_ms": [
      32.900165,
      33.063849,
      33.803522
     ],
     "p99_ms": [
      39.203272999999996,
      39.136438000000005,
      48.577117
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      14496,
      14423,
      13965
     ],
     "duration_s": [
      30.030870848,
      30.031543898,
      30.033905012
     ],
     "median_rps": 480.26168914214645,
     "median_p50_ms": 33.063849,
     "median_p99_ms": 39.203272999999996,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      6634.674835350376,
      6863.636076101463,
      6871.726603204838
     ],
     "p50_ms": [
      2.083282,
      2.0621609999999997,
      2.0777170000000003
     ],
     "p99_ms": [
      7.156789,
      6.345269,
      6.245324
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      199069,
      205934,
      206175
     ],
     "duration_s": [
      30.004334039,
      30.003630396,
      30.00337643
     ],
     "median_rps": 6863.636076101463,
     "median_p50_ms": 2.0777170000000003,
     "median_p99_ms": 6.345269,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   },
   "32": {
    "scored": true,
    "unscored_reason": null,
    "ratio_alerta_over_alrust": 0.06800062791308464,
    "ratio_inputs": {
     "alerta_median_rps": 476.09844615415545,
     "alrust_median_rps": 7001.383086678011
    },
    "alerta": {
     "rps": [
      476.09844615415545,
      467.0082427967226,
      483.11831650157615
     ],
     "p50_ms": [
      66.611271,
      67.65701200000001,
      64.872371
     ],
     "p99_ms": [
      83.77812,
      87.49063699999999,
      93.648227
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      14315,
      14042,
      14524
     ],
     "duration_s": [
      30.067310901,
      30.067991768,
      30.063029084
     ],
     "median_rps": 476.09844615415545,
     "median_p50_ms": 66.611271,
     "median_p99_ms": 87.49063699999999,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    },
    "alrust": {
     "rps": [
      6962.380523195616,
      7070.918590756017,
      7001.383086678011
     ],
     "p50_ms": [
      4.167199,
      4.171862,
      4.186324
     ],
     "p99_ms": [
      9.634734,
      9.145511,
      9.563789
     ],
     "error_count": [
      0,
      0,
      0
     ],
     "non_2xx": [
      0,
      0,
      0
     ],
     "requests": [
      208899,
      212158,
      210085
     ],
     "duration_s": [
      30.003961907,
      30.004305279,
      30.006214115
     ],
     "median_rps": 7001.383086678011,
     "median_p50_ms": 4.171862,
     "median_p99_ms": 9.563789,
     "error_count_sum": 0,
     "non_2xx_sum": 0
    }
   }
  }
 }
}
```

## Test 2 — writes

API samples compatible: True. create, duplicate, and correlate HTTP status and counted fields matched.

Sizing empty c=1: slower probe server `alerta`, sizing rps 105.1236406893497, probe rps {"alrust": 837.3777385112601, "alerta": 106.22238807299863}, n_warm 526, n_timed 3154.
Sizing empty c=8: slower probe server `alerta`, sizing rps 301.7885107649384, probe rps {"alerta": 299.5687911674051, "alrust": 4430.247962749887}, n_warm 1512, n_timed 9056.
Sizing empty c=32: slower probe server `alerta`, sizing rps 309.14564074280645, probe rps {"alrust": 5613.281577890599, "alerta": 314.585656782415}, n_warm 1568, n_timed 9280.
Sizing seeded c=1: slower probe server `alerta`, sizing rps 97.3633053280279, probe rps {"alerta": 96.93965502596124, "alrust": 698.1154788797128}, n_warm 487, n_timed 2921.
Sizing seeded c=8: slower probe server `alerta`, sizing rps 292.47497657218383, probe rps {"alrust": 4322.189016405145, "alerta": 300.5772052656779}, n_warm 1464, n_timed 8776.
Sizing seeded c=32: slower probe server `alerta`, sizing rps 319.59110677550194, probe rps {"alerta": 320.531874973533, "alrust": 5540.411257847603}, n_warm 1600, n_timed 9600.

Display rounding matches Test 1 for alerts/sec (2 decimal places), the ratio (3 decimal places), and milliseconds (3 decimal places). Wall seconds use 3 decimal places. alerts/sec is accepted HTTP 201s divided by timed-phase wall time. error_count is offered minus accepted in the timed phase and is 0 when every offered POST returned 201. The error columns are the three repetition counts.

| condition | c | alerta alerts/s | alrust alerts/s | ratio | alerta p50 ms | alrust p50 ms | alerta p99 ms | alrust p99 ms | alerta errors | alrust errors | alerta wall s | alrust wall s | scored |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---|
| empty | 1 | 84.34 | 756.73 | 0.111 | 9.956 | 1.045 | 21.089 | 3.210 | [0, 0, 0] | [0, 0, 0] | 37.395 | 4.168 | yes |
| empty | 8 | 224.66 | 4533.39 | 0.050 | 30.288 | 1.482 | 68.140 | 4.370 | [0, 0, 0] | [0, 0, 0] | 40.309 | 1.998 | yes |
| empty | 32 | 285.27 | 6227.57 | 0.046 | 102.321 | 4.691 | 172.227 | 11.358 | [0, 0, 0] | [0, 0, 0] | 32.531 | 1.490 | yes |
| seeded | 1 | 83.21 | 743.31 | 0.112 | 10.120 | 1.058 | 22.916 | 3.345 | [0, 0, 0] | [0, 0, 0] | 35.105 | 3.930 | yes |
| seeded | 8 | 225.81 | 4691.72 | 0.048 | 31.704 | 1.430 | 71.513 | 4.500 | [0, 0, 0] | [0, 0, 0] | 38.865 | 1.871 | yes |
| seeded | 32 | 273.27 | 5633.84 | 0.049 | 107.574 | 4.955 | 184.547 | 13.371 | [0, 0, 0] | [0, 0, 0] | 35.130 | 1.704 | yes |

Exact Test 2 write summary values (from `test2-writes.json`, rebuilt from `raw/test2/*.json`):

```json
{
 "empty": {
  "1": {
   "scored": true,
   "unscored_reason": null,
   "rep_matches": [
    true,
    true,
    true
   ],
   "ratio_alerta_over_alrust": 0.11145793376822388,
   "ratio_inputs": {
    "alerta_median_rps": 84.34351402753313,
    "alrust_median_rps": 756.7295676135983
   },
   "alerta": {
    "n_reps": 3,
    "rps": [
     81.9867158804702,
     86.23027634889718,
     84.34351402753313
    ],
    "p50_ms": [
     10.310528999980306,
     9.747842999786371,
     9.956395000699558
    ],
    "p99_ms": [
     22.532259999934467,
     20.818095999857178,
     21.089071000460535
    ],
    "elapsed_s": [
     38.46964677300002,
     36.57648025200069,
     37.394695210000464
    ],
    "accepted": [
     3154,
     3154,
     3154
    ],
    "offered": [
     3154,
     3154,
     3154
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     1478,
     1478,
     1478
    ],
    "duplicate_count_sum": [
     1101,
     1101,
     1101
    ],
    "history_entries": [
     1775,
     1775,
     1775
    ],
    "median_rps": 84.34351402753313,
    "median_p50_ms": 9.956395000699558,
    "median_p99_ms": 21.089071000460535,
    "median_elapsed_s": 37.394695210000464
   },
   "alrust": {
    "n_reps": 3,
    "rps": [
     756.7295676135983,
     764.559115193542,
     745.5792513530984
    ],
    "p50_ms": [
     1.0452540000187582,
     1.0374120001870324,
     1.100941999538918
    ],
    "p99_ms": [
     3.2788870003059856,
     3.161551000630425,
     3.2104429992614314
    ],
    "elapsed_s": [
     4.16793546199915,
     4.125253283000347,
     4.230267934999574
    ],
    "accepted": [
     3154,
     3154,
     3154
    ],
    "offered": [
     3154,
     3154,
     3154
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     1478,
     1478,
     1478
    ],
    "duplicate_count_sum": [
     1101,
     1101,
     1101
    ],
    "history_entries": [
     1775,
     1775,
     1775
    ],
    "median_rps": 756.7295676135983,
    "median_p50_ms": 1.0452540000187582,
    "median_p99_ms": 3.2104429992614314,
    "median_elapsed_s": 4.16793546199915
   }
  },
  "8": {
   "scored": true,
   "unscored_reason": null,
   "rep_matches": [
    true,
    true,
    true
   ],
   "ratio_alerta_over_alrust": 0.04955732563042541,
   "ratio_inputs": {
    "alerta_median_rps": 224.66282283337367,
    "alrust_median_rps": 4533.392792597414
   },
   "alerta": {
    "n_reps": 3,
    "rps": [
     224.32560519318773,
     224.66282283337367,
     229.55115121038287
    ],
    "p50_ms": [
     28.50173699971492,
     32.0515309995244,
     30.287608000435284
    ],
    "p99_ms": [
     74.06673500008765,
     68.14034600029117,
     67.92091399984201
    ],
    "elapsed_s": [
     40.369889974000216,
     40.30929499500053,
     39.45090212899959
    ],
    "accepted": [
     9056,
     9056,
     9056
    ],
    "offered": [
     9056,
     9056,
     9056
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     4280,
     4280,
     4280
    ],
    "duplicate_count_sum": [
     3144,
     3144,
     3144
    ],
    "history_entries": [
     6656,
     6656,
     6656
    ],
    "median_rps": 224.66282283337367,
    "median_p50_ms": 30.287608000435284,
    "median_p99_ms": 68.14034600029117,
    "median_elapsed_s": 40.30929499500053
   },
   "alrust": {
    "n_reps": 3,
    "rps": [
     4533.392792597414,
     4619.745790714801,
     4414.554816672825
    ],
    "p50_ms": [
     1.4440120003200718,
     1.482082999245904,
     1.5238129999488592
    ],
    "p99_ms": [
     4.370399000436009,
     4.197221999675094,
     4.501831000197853
    ],
    "elapsed_s": [
     1.9976208580001185,
     1.9602810219994353,
     2.0513959790005174
    ],
    "accepted": [
     9056,
     9056,
     9056
    ],
    "offered": [
     9056,
     9056,
     9056
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     4280,
     4280,
     4280
    ],
    "duplicate_count_sum": [
     3144,
     3144,
     3144
    ],
    "history_entries": [
     6656,
     6656,
     6656
    ],
    "median_rps": 4533.392792597414,
    "median_p50_ms": 1.482082999245904,
    "median_p99_ms": 4.370399000436009,
    "median_elapsed_s": 1.9976208580001185
   }
  },
  "32": {
   "scored": true,
   "unscored_reason": null,
   "rep_matches": [
    true,
    true,
    true
   ],
   "ratio_alerta_over_alrust": 0.04580699442192271,
   "ratio_inputs": {
    "alerta_median_rps": 285.2663702262671,
    "alrust_median_rps": 6227.572313492409
   },
   "alerta": {
    "n_reps": 3,
    "rps": [
     300.0570831214005,
     284.6795117735207,
     285.2663702262671
    ],
    "p50_ms": [
     96.99649899994256,
     103.45602999950643,
     102.32104500028072
    ],
    "p99_ms": [
     166.9587340002181,
     187.97206500039465,
     172.22727500029578
    ],
    "elapsed_s": [
     30.927448548999564,
     32.59806068299986,
     32.53099898399978
    ],
    "accepted": [
     9280,
     9280,
     9280
    ],
    "offered": [
     9280,
     9280,
     9280
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     4544,
     4544,
     4544
    ],
    "duplicate_count_sum": [
     3168,
     3168,
     3168
    ],
    "history_entries": [
     7680,
     7680,
     7680
    ],
    "median_rps": 285.2663702262671,
    "median_p50_ms": 102.32104500028072,
    "median_p99_ms": 172.22727500029578,
    "median_elapsed_s": 32.53099898399978
   },
   "alrust": {
    "n_reps": 3,
    "rps": [
     6011.945572598196,
     6227.572313492409,
     6288.1179672072
    ],
    "p50_ms": [
     4.923231999782729,
     4.598257999532507,
     4.691129000093497
    ],
    "p99_ms": [
     11.390292999749363,
     11.357791999216715,
     10.166695999942021
    ],
    "elapsed_s": [
     1.5435934819997783,
     1.4901472889996512,
     1.4757992849999937
    ],
    "accepted": [
     9280,
     9280,
     9280
    ],
    "offered": [
     9280,
     9280,
     9280
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     4544,
     4544,
     4544
    ],
    "duplicate_count_sum": [
     3168,
     3168,
     3168
    ],
    "history_entries": [
     7680,
     7680,
     7680
    ],
    "median_rps": 6227.572313492409,
    "median_p50_ms": 4.691129000093497,
    "median_p99_ms": 11.357791999216715,
    "median_elapsed_s": 1.4901472889996512
   }
  }
 },
 "seeded": {
  "1": {
   "scored": true,
   "unscored_reason": null,
   "rep_matches": [
    true,
    true,
    true
   ],
   "ratio_alerta_over_alrust": 0.11194361031408812,
   "ratio_inputs": {
    "alerta_median_rps": 83.20851481162816,
    "alrust_median_rps": 743.3074078829881
   },
   "alerta": {
    "n_reps": 3,
    "rps": [
     82.81258896554775,
     83.30534656800168,
     83.20851481162816
    ],
    "p50_ms": [
     10.162441000829858,
     10.02485400022124,
     10.120239000571019
    ],
    "p99_ms": [
     21.880410999983724,
     22.91630299987446,
     23.00277699941944
    ],
    "elapsed_s": [
     35.27241493699967,
     35.06377585999962,
     35.104580422000254
    ],
    "accepted": [
     2921,
     2921,
     2921
    ],
    "offered": [
     2921,
     2921,
     2921
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     2370,
     2370,
     2370
    ],
    "duplicate_count_sum": [
     1020,
     1020,
     1020
    ],
    "history_entries": [
     2667,
     2667,
     2667
    ],
    "median_rps": 83.20851481162816,
    "median_p50_ms": 10.120239000571019,
    "median_p99_ms": 22.91630299987446,
    "median_elapsed_s": 35.104580422000254
   },
   "alrust": {
    "n_reps": 3,
    "rps": [
     792.7284547850596,
     743.3074078829881,
     663.164394620186
    ],
    "p50_ms": [
     0.9401329998581787,
     1.0580099997241632,
     1.2067999996361323
    ],
    "p99_ms": [
     3.2286000005115056,
     3.344744000060018,
     3.513403999932052
    ],
    "elapsed_s": [
     3.6847422119999464,
     3.929733470999963,
     4.404639368000062
    ],
    "accepted": [
     2921,
     2921,
     2921
    ],
    "offered": [
     2921,
     2921,
     2921
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     2370,
     2370,
     2370
    ],
    "duplicate_count_sum": [
     1020,
     1020,
     1020
    ],
    "history_entries": [
     2667,
     2667,
     2667
    ],
    "median_rps": 743.3074078829881,
    "median_p50_ms": 1.0580099997241632,
    "median_p99_ms": 3.344744000060018,
    "median_elapsed_s": 3.929733470999963
   }
  },
  "8": {
   "scored": true,
   "unscored_reason": null,
   "rep_matches": [
    true,
    true,
    true
   ],
   "ratio_alerta_over_alrust": 0.048129301548778085,
   "ratio_inputs": {
    "alerta_median_rps": 225.8092122279685,
    "alrust_median_rps": 4691.720115637152
   },
   "alerta": {
    "n_reps": 3,
    "rps": [
     218.71840613043838,
     225.8092122279685,
     227.37441027678054
    ],
    "p50_ms": [
     31.704031999652216,
     33.88108399940393,
     30.813435000709433
    ],
    "p99_ms": [
     72.71201399998972,
     62.5854009995237,
     71.5126169998257
    ],
    "elapsed_s": [
     40.12465231100032,
     38.86466771399955,
     38.59713144199941
    ],
    "accepted": [
     8776,
     8776,
     8776
    ],
    "offered": [
     8776,
     8776,
     8776
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     5144,
     5144,
     5144
    ],
    "duplicate_count_sum": [
     3048,
     3048,
     3048
    ],
    "history_entries": [
     7520,
     7520,
     7520
    ],
    "median_rps": 225.8092122279685,
    "median_p50_ms": 31.704031999652216,
    "median_p99_ms": 71.5126169998257,
    "median_elapsed_s": 38.86466771399955
   },
   "alrust": {
    "n_reps": 3,
    "rps": [
     4459.034175737739,
     4796.214908514549,
     4691.720115637152
    ],
    "p50_ms": [
     1.5031539996925858,
     1.3848809994669864,
     1.4296869994723238
    ],
    "p99_ms": [
     4.49999000011303,
     4.277324999748089,
     4.506848999881186
    ],
    "elapsed_s": [
     1.9681392100001176,
     1.8297762230004082,
     1.8705293120001443
    ],
    "accepted": [
     8776,
     8776,
     8776
    ],
    "offered": [
     8776,
     8776,
     8776
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     5144,
     5144,
     5144
    ],
    "duplicate_count_sum": [
     3048,
     3048,
     3048
    ],
    "history_entries": [
     7520,
     7520,
     7520
    ],
    "median_rps": 4691.720115637152,
    "median_p50_ms": 1.4296869994723238,
    "median_p99_ms": 4.49999000011303,
    "median_elapsed_s": 1.8705293120001443
   }
  },
  "32": {
   "scored": true,
   "unscored_reason": null,
   "rep_matches": [
    true,
    true,
    true
   ],
   "ratio_alerta_over_alrust": 0.048504593311574,
   "ratio_inputs": {
    "alerta_median_rps": 273.2668788201916,
    "alrust_median_rps": 5633.835069285811
   },
   "alerta": {
    "n_reps": 3,
    "rps": [
     287.7917288699912,
     273.09599295631216,
     273.2668788201916
    ],
    "p50_ms": [
     101.44764999949984,
     107.57376499987004,
     107.66528500062122
    ],
    "p99_ms": [
     170.7468010008597,
     184.61881300027017,
     184.54667500009236
    ],
    "elapsed_s": [
     33.35745623299954,
     35.152474761999656,
     35.130492364999554
    ],
    "accepted": [
     9600,
     9600,
     9600
    ],
    "offered": [
     9600,
     9600,
     9600
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     5672,
     5672,
     5672
    ],
    "duplicate_count_sum": [
     3264,
     3264,
     3264
    ],
    "history_entries": [
     8936,
     8936,
     8936
    ],
    "median_rps": 273.2668788201916,
    "median_p50_ms": 107.57376499987004,
    "median_p99_ms": 184.54667500009236,
    "median_elapsed_s": 35.130492364999554
   },
   "alrust": {
    "n_reps": 3,
    "rps": [
     5553.055250653547,
     5633.835069285811,
     5765.307969147136
    ],
    "p50_ms": [
     4.954805999659584,
     5.21158799983823,
     4.9246480002693716
    ],
    "p99_ms": [
     13.37056499960454,
     11.425677000261203,
     14.851750999696378
    ],
    "elapsed_s": [
     1.7287780449996717,
     1.703990244999659,
     1.6651322100005927
    ],
    "accepted": [
     9600,
     9600,
     9600
    ],
    "offered": [
     9600,
     9600,
     9600
    ],
    "error_count": [
     0,
     0,
     0
    ],
    "alert_rows": [
     5672,
     5672,
     5672
    ],
    "duplicate_count_sum": [
     3264,
     3264,
     3264
    ],
    "history_entries": [
     8936,
     8936,
     8936
    ],
    "median_rps": 5633.835069285811,
    "median_p50_ms": 4.954805999659584,
    "median_p99_ms": 13.37056499960454,
    "median_elapsed_s": 1.703990244999659
   }
  }
 }
}
```

## Test 2 — mixed

Readers used `/alerts?sort-by=lastReceiveTime&page-size=50` while 8 writers posted the seeded concurrency-8 sequence. Read latencies are under that write load, not a fixed 30 second oha window. Display rounding matches the write table: alerts/sec to 2 decimal places, milliseconds and wall seconds to 3, and the ratio below the table to 3.

| server | write alerts/s | write p50 ms | write p99 ms | write errors | write wall s | read p50 ms | read p99 ms | read errors | scored |
|---|---:|---:|---:|---|---:|---:|---:|---|---|
| alerta | 62.25 | 121.597 | 188.010 | [0, 0, 0] | 140.980 | 122.450 | 181.810 | [0, 0, 0] | yes |
| alrust | 926.27 | 8.123 | 16.674 | [0, 0, 0] | 9.475 | 12.632 | 22.204 | [0, 0, 0] | yes |

Write throughput ratio (median Alerta alerts/sec / median alrust alerts/sec): 0.067 (exact 0.06720488202393714).

Test 1 list newest-50 at concurrency 16: Alerta median p50 83.130 ms (exact 83.129667), p99 109.417 ms (exact 109.417288); alrust median p50 5.735 ms (exact 5.7349630000000005), p99 10.842 ms (exact 10.842051999999999); scored=True. Test 1 list newest-50 at concurrency 8: Alerta median p50 40.176 ms (exact 40.175774), p99 56.322 ms (exact 56.321806); alrust median p50 3.655 ms (exact 3.654546), p99 6.625 ms (exact 6.624949); scored=True.

Exact mixed summary (from `test2-mixed.json`):

```json
{
 "summary_rule": "Eight writers send the seeded concurrency-8 sequence while 16 readers GET the newest-50 list. Write alerts/sec and write percentiles use the same rules as the write suite. Read p50 and p99 are nearest-rank percentiles of HTTP 200 latencies during the timed write phase. Compare those read latencies with Test 1 list at concurrency 16, and mention Test 1 list at concurrency 8. Scored only when API samples agreed, the full write sequence was offered, and alert aggregates matched.",
 "api_compatible": true,
 "api_compatible_reason": "create, duplicate, and correlate HTTP status and counted fields matched",
 "scored": true,
 "alerta": {
  "n_reps": 3,
  "write_rps": [
   65.57332555605892,
   60.22283897167524,
   62.2498378649613
  ],
  "write_p50_ms": [
   116.07952200029104,
   126.51473199912289,
   121.59711399908701
  ],
  "write_p99_ms": [
   177.79029399935098,
   202.95193699985248,
   188.0103869989398
  ],
  "write_elapsed_s": [
   133.8349081060005,
   145.7254448620006,
   140.9802868730003
  ],
  "write_error_count": [
   0,
   0,
   0
  ],
  "write_accepted": [
   8776,
   8776,
   8776
  ],
  "read_p50_ms": [
   116.50802500025748,
   126.94097300027352,
   122.45036999956938
  ],
  "read_p99_ms": [
   173.0396799994196,
   194.7405560003972,
   181.8099779993645
  ],
  "read_error_count": [
   0,
   0,
   0
  ],
  "read_accepted": [
   17723,
   17729,
   17703
  ],
  "alert_rows": [
   5144,
   5144,
   5144
  ],
  "duplicate_count_sum": [
   3048,
   3048,
   3048
  ],
  "median_write_rps": 62.2498378649613,
  "median_write_p50_ms": 121.59711399908701,
  "median_write_p99_ms": 188.0103869989398,
  "median_read_p50_ms": 122.45036999956938,
  "median_read_p99_ms": 181.8099779993645,
  "median_write_elapsed_s": 140.9802868730003
 },
 "alrust": {
  "n_reps": 3,
  "write_rps": [
   953.1506860010468,
   902.6729053881093,
   926.269580278246
  ],
  "write_p50_ms": [
   7.959818000017549,
   8.336975000020175,
   8.122502000333043
  ],
  "write_p99_ms": [
   16.673747999448096,
   17.303134000030695,
   16.630673000690877
  ],
  "write_elapsed_s": [
   9.20735842600061,
   9.722237089000373,
   9.474563547000798
  ],
  "write_error_count": [
   0,
   0,
   0
  ],
  "write_accepted": [
   8776,
   8776,
   8776
  ],
  "read_p50_ms": [
   12.258482999641274,
   12.713574000372319,
   12.63216200095485
  ],
  "read_p99_ms": [
   21.75677499963058,
   23.05951800008188,
   22.203969998372486
  ],
  "read_error_count": [
   0,
   0,
   0
  ],
  "read_accepted": [
   11718,
   11947,
   11779
  ],
  "alert_rows": [
   5144,
   5144,
   5144
  ],
  "duplicate_count_sum": [
   3048,
   3048,
   3048
  ],
  "median_write_rps": 926.269580278246,
  "median_write_p50_ms": 8.122502000333043,
  "median_write_p99_ms": 16.673747999448096,
  "median_read_p50_ms": 12.63216200095485,
  "median_read_p99_ms": 22.203969998372486,
  "median_write_elapsed_s": 9.474563547000798
 },
 "rep_matches": [
  true,
  true,
  true
 ],
 "ratio_alerta_over_alrust": 0.06720488202393714,
 "ratio_inputs": {
  "alerta_median_write_rps": 62.2498378649613,
  "alrust_median_write_rps": 926.269580278246
 }
}
```

API sample aggregate snapshot:

```json
{
 "alerta": {
  "agg": {
   "alert_rows": 3,
   "duplicate_count_sum": 1,
   "history_entries": 4
  },
  "steps": [
   {
    "n": 0,
    "http": 201,
    "duplicateCount": 0,
    "severity": "major",
    "value": "0",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   },
   {
    "n": 4,
    "http": 201,
    "duplicateCount": 0,
    "severity": "major",
    "value": "1",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   },
   {
    "n": 4,
    "http": 201,
    "duplicateCount": 1,
    "severity": "major",
    "value": "1",
    "alert_status": "open",
    "repeat": true,
    "top_status": "ok"
   },
   {
    "n": 7,
    "http": 201,
    "duplicateCount": 0,
    "severity": "major",
    "value": "7",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   },
   {
    "n": 17,
    "http": 201,
    "duplicateCount": 0,
    "severity": "critical",
    "value": "17",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   }
  ]
 },
 "alrust": {
  "agg": {
   "alert_rows": 3,
   "duplicate_count_sum": 1,
   "history_entries": 4
  },
  "steps": [
   {
    "n": 0,
    "http": 201,
    "duplicateCount": 0,
    "severity": "major",
    "value": "0",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   },
   {
    "n": 4,
    "http": 201,
    "duplicateCount": 0,
    "severity": "major",
    "value": "1",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   },
   {
    "n": 4,
    "http": 201,
    "duplicateCount": 1,
    "severity": "major",
    "value": "1",
    "alert_status": "open",
    "repeat": true,
    "top_status": "ok"
   },
   {
    "n": 7,
    "http": 201,
    "duplicateCount": 0,
    "severity": "major",
    "value": "7",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   },
   {
    "n": 17,
    "http": 201,
    "duplicateCount": 0,
    "severity": "critical",
    "value": "17",
    "alert_status": "open",
    "repeat": false,
    "top_status": "ok"
   }
  ]
 }
}
```

Supplemental history cardinality, not used for scoring:

- SUM(cardinality(history)) matched between the two databases on every write repetition (6 condition/concurrency cells) and every mixed repetition (3). That field was recorded and was not a scoring gate. Equal cardinality is not a full history-body comparison.

## Unscored cases

- No unscored case. Every planned read, write, and mixed cell met its scoring rule.

## Deviations

- This is a new full run. No result number was copied from the incomplete concurrency directory.
- The Alerta image was rebuilt. Its id is the one recorded above, not the 15 November id.
- The alrust image digest may differ from the 15 November digest. The binary hash was checked and matched.
- Reads used oha 1.17.0 with explicit HTTP/1.1, `-w`, keep-alive, and a 60 second request timeout.
- Writes and mixed reads used a Python asyncio HTTP/1.1 client so both servers received the same POST bytes. Write percentiles are nearest-rank, not oha percentiles.
- The write window is a fixed offered count sized from the slower server's pilot, not a hard 30 second time box. The faster server finishes sooner. Wall time is reported.
- Warmup and timed write phases open connections before the timer and stop at a 120 second or 240 second safety deadline if a phase hangs.
- Server order is balanced by the index formula above, rather than always running Alerta first.
- Alerta `AUTH_REQUIRED` stayed at the image default false. The key was still sent. alrust auth stayed at its default.
- `ALRUST_HOUSEKEEPING_INTERVAL=0` was set explicitly. No Alerta housekeeping process was started. `DELETE_EXPIRED_AFTER` stayed at the image default and does not apply to these fresh rows inside the run.
- Both servers stayed up during Test 1, so the idle server held its database connections while the other was measured.
- PostgreSQL ran with the image defaults, including `listen_addresses=*`, because the container uses the host network and no custom config was added.

## What this does not establish

These numbers are one co-located lab run: one 8 vCPU host, one PostgreSQL 18.7 container, untuned Alerta (one gthread worker, four threads) and default alrust, three repetitions, and a 1,000-alert read set. They are not a production capacity claim, not a tuning study, and not a result for a 100,000-alert dataset. They do not separate client overhead, key-counter updates, or Postgres checkpoints from server time. They do not provide a confidence interval beyond the median of three runs. They are not comparable with the 13 November 25%-of-one-CPU cap or directly with the 15 November single-connection warm-read medians. Correctness here is the Test 1 id and count parity plus the Test 2 row count and `duplicate_count` sum. It is not a full alert-body or history-equivalence proof. No soak, failover, or multi-host result is included.

## Artifacts

- `environment.txt` — host, images, PostgreSQL settings, redacted command lines, auth probe
- `meta.json` — image ids and the binary hash recorded before timing
- `parity-test1.json` — ordered ids and count comparison
- `test1-reads.csv`, `test1-reads.json`, `raw/test1/`, `raw/test1-warmup/`
- `test2-writes.csv`, `test2-writes.json`, `raw/test2/`
- `test2-mixed.csv`, `test2-mixed.json`, `raw/test2-mixed/`
- `raw/test2/api-samples.json` — create, duplicate, and correlate responses
- `seed.py` — symlink to `/workspace/alerta-bench/seed.py`

## Teardown

Disposable containers removed: ['fullbench-alerta', 'fullbench-alrust', 'fullbench-pg']. Still present from this bench: []. docker ps names after removal: []. git was not invoked. Nothing was committed or pushed.

git was not invoked by this benchmark. No commit and no push were made.
