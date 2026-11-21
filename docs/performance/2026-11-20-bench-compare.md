# alrust and Alerta: a diagnostic benchmark on a laptop (20 November 2026)

One full compare run of the harness on a laptop. The numbers are diagnostic and relative to that
laptop. They are not a capacity claim and they do not transfer to the hosted CI runner or to any
other machine. The mixed run from the same laptop is [2026-11-20-mixed-bench.md](2026-11-20-mixed-bench.md).

## How this report was made

- **Harness:** `scripts/bench/bench_compare.py` of this repository, the same program that the
  hosted workflow `bench compare` runs.
- **Harness revision:** the run did not record the revision of the harness files. The run script
  started the harness from a checkout of this repository and recorded that checkout's `HEAD` as
  the alrust commit, `38c671382e8aa0860d616e4931a0a82dfeafc9de`. A check after the runs found the
  checkout at that commit with no local change, and `bench_compare.py` there is Git blob
  `dbc6d1a553618ed181e8f986bee12e9c6107b8ba`, last written the day before the runs. That is an
  inference from a later check, not a record of the run. The harness of later revisions also
  writes a `cell_gate` field; this results file has none. Here it ran on a laptop, in mode `full`, suite `compare`.
- **Machine:** one laptop with 8 logical CPUs (a four-core laptop processor from 2014) and 16 GB
  of memory, on mains power. The host name, the kernel version and local paths are not in this
  report; they are not needed to read it.
- **Other work on the machine:** the laptop was in use. The one-minute load average at the start
  was 1.7, below the CPU count.
- **Software:** PostgreSQL 18.7 and PgBouncer 1.26.1, both built from source (the hosted
  workflow uses the PGDG packages of the same versions). Alerta 9.1.0 from the pinned
  `reference/alerta` submodule (`f4312727`), served by gunicorn 27.0.1 with the gthread worker.
  alrust is the release binary of commit `38c671382e8aa0860d616e4931a0a82dfeafc9de`. Load tool
  oha 1.17.0.
- **Network:** everything on the host's loopback interface. The servers, PostgreSQL, PgBouncer
  and the load clients share the laptop's CPUs.
- **Results file:** [2026-11-20-bench-compare-results.json](2026-11-20-bench-compare-results.json) is the harness's `results.json` of this run with one field
  changed: `env.runner`, which held the host name and the kernel version. Every number is as the
  harness wrote it.
- **Tables:** recomputed from the results file with the harness's rules (medians of the valid
  repetitions) and compared with the summary that the harness wrote for the run.
- **Earlier attempts the same day:** a quick run checked the plumbing first. Their
  results are not in this report.

## This run

| | |
|---|---|
| Label | compare-1, mode `full`, suite `compare` |
| alrust | `38c671382e8aa0860d616e4931a0a82dfeafc9de` |
| Alerta commit | `f4312727783dad64198b62ed778bbca98a5144a3` |
| Database | `PostgreSQL 18.7 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 16.2.1 20260810, 64-bit` |
| Pooler | `PgBouncer 1.26.1` |
| Load tool | `oha 1.17.0` |
| Shape | `3` repetitions; warm-up `5` s; timed `15` s; concurrency `[1, 8]`; `1000` seeded alerts |
| Arms | `alrust, alerta-1x4, alerta-1x4-pgb, alerta-8x4-pgb` |
| Network | `mode B: host loopback` |

## Method

The arms are the four names in the results file. From the harness that wrote
it: `alrust` is the release binary with its own pool; `alerta-1x4` is gunicorn, gthread,
with the worker count and the thread count carried in that arm name; `alerta-1x4-pgb` is
that one worker behind PgBouncer; `alerta-8x4-pgb` is one gunicorn worker per logical
CPU, behind PgBouncer. The `8` in that arm name is the `vcpus` value in this
results file. The hosted 15 November report names its fourth arm `alerta-4x4-pgb`. This
run's fourth arm is the name in the results file.

Cases are `GET /alerts?page-size=50&sort-by=lastReceiveTime`, `GET /alerts/count`, and `POST /alert` with one fixed body, so the write case is the
duplicate path. A repetition is scored only with no error, real responses,
the server alive at its end and, for writes, every counted write saved in
the database.

## Results

Medians of the valid repetitions. A dash would mark a group with no valid
repetition. This run has none of those.

| case | c | arm | valid reps | rps | p50 ms | p99 ms | errors+non-2xx (all reps) | sessions/req | commits/req | server CPU ms/req | postgres CPU ms/req |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| list | 1 | alrust | 3/3 | 208.2 | 3.43 | 12.66 | 0 | 0.000 | 2.96 | 2.292 | 2.510 |
| list | 1 | alerta-1x4 | 3/3 | 28.9 | 33.81 | 42.14 | 0 | 1.000 | 4.00 | 10.922 | 8.881 |
| list | 1 | alerta-1x4-pgb | 3/3 | 29.3 | 33.38 | 44.64 | 0 | 0.000 | 3.78 | 11.955 | 3.932 |
| list | 1 | alerta-8x4-pgb | 3/3 | 29.2 | 33.30 | 49.78 | 0 | 0.000 | 3.80 | 12.276 | 4.064 |
| list | 8 | alrust | 3/3 | 1132.5 | 6.84 | 11.98 | 0 | 0.000 | 3.01 | 2.365 | 2.553 |
| list | 8 | alerta-1x4 | 3/3 | 59.7 | 133.38 | 154.48 | 0 | 1.000 | 4.00 | 11.031 | 8.795 |
| list | 8 | alerta-1x4-pgb | 3/3 | 58.6 | 135.17 | 174.82 | 0 | 0.000 | 3.87 | 11.811 | 3.895 |
| list | 8 | alerta-8x4-pgb | 3/3 | 59.3 | 117.62 | 347.71 | 0 | 0.000 | 4.10 | 12.297 | 4.278 |
| count | 1 | alrust | 3/3 | 781.1 | 0.82 | 3.47 | 0 | 0.000 | 2.04 | 0.399 | 0.832 |
| count | 1 | alerta-1x4 | 3/3 | 33.1 | 29.64 | 37.30 | 0 | 1.000 | 4.01 | 8.134 | 7.319 |
| count | 1 | alerta-1x4-pgb | 3/3 | 34.0 | 28.65 | 39.82 | 0 | 0.000 | 4.35 | 8.865 | 2.622 |
| count | 1 | alerta-8x4-pgb | 3/3 | 35.0 | 27.93 | 37.83 | 0 | 0.000 | 4.19 | 8.542 | 2.414 |
| count | 8 | alrust | 3/3 | 5034.9 | 1.49 | 3.09 | 0 | 0.000 | 2.00 | 0.269 | 0.716 |
| count | 8 | alerta-1x4 | 3/3 | 60.2 | 131.15 | 151.86 | 0 | 1.000 | 4.00 | 8.459 | 7.253 |
| count | 8 | alerta-1x4-pgb | 3/3 | 59.1 | 135.41 | 217.64 | 0 | 0.000 | 3.83 | 9.017 | 2.665 |
| count | 8 | alerta-8x4-pgb | 3/3 | 59.1 | 117.48 | 363.36 | 0 | 0.000 | 4.12 | 9.277 | 3.073 |
| write-dup | 1 | alrust | 3/3 | 94.3 | 9.83 | 17.64 | 0 | 0.000 | 1.94 | 0.874 | 1.236 |
| write-dup | 1 | alerta-1x4 | 3/3 | 16.5 | 59.90 | 73.26 | 0 | 1.000 | 7.00 | 10.442 | 11.040 |
| write-dup | 1 | alerta-1x4-pgb | 3/3 | 16.5 | 59.70 | 75.69 | 0 | 0.000 | 6.58 | 11.129 | 5.363 |
| write-dup | 1 | alerta-8x4-pgb | 3/3 | 16.6 | 59.07 | 73.47 | 0 | 0.000 | 6.59 | 11.084 | 5.178 |
| write-dup | 8 | alrust | 3/3 | 103.3 | 75.58 | 103.30 | 0 | 0.003 | 2.06 | 0.880 | 1.305 |
| write-dup | 8 | alerta-1x4 | 3/3 | 26.3 | 302.35 | 357.73 | 0 | 1.000 | 7.00 | 10.499 | 11.721 |
| write-dup | 8 | alerta-1x4-pgb | 3/3 | 25.0 | 316.25 | 375.39 | 0 | 0.000 | 6.71 | 11.560 | 6.267 |
| write-dup | 8 | alerta-8x4-pgb | 3/3 | 26.3 | 287.58 | 614.51 | 0 | 0.000 | 7.12 | 12.375 | 6.899 |

Valid cells: 72 of 72. Checks before scoring: 12 of 12 ok. The summary line
is: Write checks: 24 of 24 duplicate-write repetitions saved exactly the
writes counted.

Single-client median p50, an Alerta arm divided by alrust, using the table's
rounded milliseconds. Read these ratios on this laptop only.

| case | alerta-1x4 p50 / alrust p50 | alerta-8x4-pgb p50 / alrust p50 |
|---|---:|---:|
| list | 9.9× | 9.7× |
| count | 36.1× | 34.1× |
| write-dup | 6.1× | 6.0× |

## What this does not establish

No capacity for this laptop or for any other host. The load average above
is part of the record, and other work was active later in the afternoon on
the mixed run. No comparison that carries the hosted 15 November rates onto
this machine: the CPU count, the Alerta worker arm and the PostgreSQL build
differ, and the harness summary's "hosted runner"
sentence does not describe this run. No confidence interval beyond the
repetitions in the results file. The duplicate-write case is the duplicate
path only. Nothing here qualifies a release.
