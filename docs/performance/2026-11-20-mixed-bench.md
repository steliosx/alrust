# Mixed load on a laptop: the 20 November 2026 run

One full mixed run of the harness on a laptop. The numbers are diagnostic and relative to that
laptop. They are not a capacity claim and they do not transfer to the hosted CI runner or to any
other machine. The compare run from the same laptop is [2026-11-20-bench-compare.md](2026-11-20-bench-compare.md).
The methodology of the mixed suite is [benchmark-methodology.md](benchmark-methodology.md). The hosted run of that suite is
[2026-11-17-mixed-bench.md](2026-11-17-mixed-bench.md), on a different machine; this page does not treat the two as one series.

## How this report was made

- **Harness:** `scripts/bench/bench_compare.py` of this repository, the same program that the
  hosted workflow `bench compare` runs. Here it ran on a laptop, in mode `full`, suite `mixed`.
- **Machine:** one laptop with 8 logical CPUs (a four-core laptop processor from 2014) and 16 GB
  of memory, on mains power. The host name, the kernel version and local paths are not in this
  report; they are not needed to read it.
- **Other work on the machine:** the laptop was in use. The one-minute load average at the start
  was 14.5, above the CPU count of 8, so this run competed for CPU.
- **Software:** PostgreSQL 18.7 and PgBouncer 1.26.1, both built from source (the hosted
  workflow uses the PGDG packages of the same versions). Alerta 9.1.0 from the pinned
  `reference/alerta` submodule (`f4312727`), served by gunicorn 27.0.1 with the gthread worker.
  alrust is the release binary of commit `38c671382e8aa0860d616e4931a0a82dfeafc9de`. Load tool
  oha 1.17.0.
- **Network:** everything on the host's loopback interface. The servers, PostgreSQL, PgBouncer
  and the load clients share the laptop's CPUs.
- **Results file:** [2026-11-20-mixed-bench-results.json](2026-11-20-mixed-bench-results.json) is the harness's `results.json` of this run with one field
  changed: `env.runner`, which held the host name and the kernel version. Every number is as the
  harness wrote it.
- **Tables:** recomputed from the results file with the harness's rules (medians of the valid
  repetitions) and compared with the summary that the harness wrote for the run.
- **Earlier attempts the same day:** a quick run checked the plumbing first. A first full mixed run did not finish and was started again. Their
  results are not in this report.

## This run

| | |
|---|---|
| Label | mixed-2, mode `full`, suite `mixed` |
| alrust | `38c671382e8aa0860d616e4931a0a82dfeafc9de` |
| Alerta commit | `f4312727783dad64198b62ed778bbca98a5144a3` |
| Database | `PostgreSQL 18.7 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 16.2.1 20260810, 64-bit` |
| Pooler | `PgBouncer 1.26.1` |
| Shape | `3` repetitions; warm-up `5` s; timed `15` s; `8` writers; `16` readers; `1000` identities per writer; `1000` seeded alerts |
| Arms | `alrust-p16, alrust-p32, alrust-p64, alrust-p16-immediate, alerta-8x4-pgb` |
| Network | `mode B: host loopback` |

Pool and key-use settings recorded in `mixed.arm_env`:

```
{
  "alrust-p16": {
    "ALRUST_DB_MAX_CONNECTIONS": "16",
    "ALRUST_KEY_USAGE_IMMEDIATE": "false"
  },
  "alrust-p32": {
    "ALRUST_DB_MAX_CONNECTIONS": "32",
    "ALRUST_KEY_USAGE_IMMEDIATE": "false"
  },
  "alrust-p64": {
    "ALRUST_DB_MAX_CONNECTIONS": "64",
    "ALRUST_KEY_USAGE_IMMEDIATE": "false"
  },
  "alrust-p16-immediate": {
    "ALRUST_DB_MAX_CONNECTIONS": "16",
    "ALRUST_KEY_USAGE_IMMEDIATE": "true"
  }
}
```

The start-up probe records `refused_zero` true and exit 1. Its error line is:

```
2026-11-20T19:34:42.168016Z ERROR alrust: alrust stopped error=ALRUST_DB_MAX_CONNECTIONS must be between 1 and 1024, not 0
```

## Method

8 writers, each with its own 1000 alert identities in order and again; 16 readers of the 50 newest alerts; closed loops, one connection each; every cell on a fresh copy of the seeded database. Rates and latencies are of 2xx answers that ended in the timed phase. CPU is of the server's process tree and of PostgreSQL, for both classes together, per second and per completed request. The clients are Python processes on the same runner and take part of its CPU. commits/req and key updates/req cover every phase; key updates are row updates of the `keys` table from PostgreSQL's statistics, and show the key-use mode.

A repetition is scored only with no error or non-2xx answer in any phase, every list answer in order, the server alive, every 2xx write found in the database with the right duplicate_count, and the final list and count equal to the final rows.

The columns do not share one denominator. Rates and latencies are of 2xx
answers that ended in the timed phase. CPU is of both classes together over
the timed interval. Commits and key updates per request cover every phase.

## Results

Valid mixed cells: 45 of 45. Checks before scoring: 45 of 45 ok. The medians
are the harness medians.

| case | arm | valid reps | writes/s | write p50 ms | write p99 ms | reads/s | read p50 ms | read p99 ms | errors+non-2xx (all reps) | server CPU s/s | postgres CPU s/s | server CPU ms/req | postgres CPU ms/req | commits/req | key updates/req | final rows |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| writers-alone | alrust-p16 | 3/3 | 428.8 | 17.99 | 27.25 | - | - | - | 0 | 0.39 | 0.46 | 0.906 | 1.065 | 1.93 | 0.0047 | 9000 |
| writers-alone | alrust-p32 | 3/3 | 430.9 | 17.76 | 26.87 | - | - | - | 0 | 0.40 | 0.46 | 0.905 | 1.053 | 1.94 | 0.0047 | 9000 |
| writers-alone | alrust-p64 | 3/3 | 430.5 | 17.99 | 25.70 | - | - | - | 0 | 0.40 | 0.48 | 0.946 | 1.118 | 1.93 | 0.0046 | 9000 |
| writers-alone | alrust-p16-immediate | 3/3 | 74.4 | 89.16 | 331.56 | - | - | - | 0 | 0.08 | 0.16 | 1.023 | 2.201 | 1.97 | 0.9808 | 2507 |
| writers-alone | alerta-8x4-pgb | 3/3 | 58.4 | 122.38 | 298.33 | - | - | - | 0 | 0.66 | 0.28 | 11.278 | 4.695 | 6.81 | 0.9737 | 2164 |
| readers-alone | alrust-p16 | 3/3 | - | - | - | 863.1 | 14.36 | 24.81 | 0 | 2.37 | 2.58 | 2.723 | 2.989 | 2.98 | 0.0035 | 1000 |
| readers-alone | alrust-p32 | 3/3 | - | - | - | 895.5 | 14.02 | 24.16 | 0 | 2.39 | 2.66 | 2.635 | 2.909 | 2.98 | 0.0032 | 1000 |
| readers-alone | alrust-p64 | 3/3 | - | - | - | 901.7 | 13.95 | 24.34 | 0 | 2.35 | 2.62 | 2.598 | 2.900 | 2.98 | 0.0031 | 1000 |
| readers-alone | alrust-p16-immediate | 3/3 | - | - | - | 112.3 | 102.50 | 601.58 | 0 | 0.19 | 0.38 | 1.738 | 3.424 | 2.99 | 0.9783 | 1000 |
| readers-alone | alerta-8x4-pgb | 3/3 | - | - | - | 59.5 | 225.06 | 856.83 | 0 | 0.73 | 0.30 | 12.322 | 5.022 | 3.94 | 0.9815 | 1000 |
| mixed | alrust-p16 | 3/3 | 282.3 | 27.98 | 44.42 | 413.5 | 33.08 | 61.91 | 0 | 1.27 | 4.68 | 1.829 | 6.733 | 2.57 | 0.0033 | 6907 |
| mixed | alrust-p32 | 3/3 | 319.3 | 24.05 | 46.62 | 414.4 | 32.07 | 71.57 | 0 | 1.23 | 4.74 | 1.679 | 6.592 | 2.56 | 0.0030 | 7507 |
| mixed | alrust-p64 | 3/3 | 311.8 | 24.60 | 46.78 | 424.6 | 31.14 | 73.13 | 0 | 1.24 | 4.79 | 1.683 | 6.536 | 2.59 | 0.0030 | 7432 |
| mixed | alrust-p16-immediate | 3/3 | 31.4 | 205.12 | 811.56 | 56.8 | 235.38 | 836.51 | 0 | 0.13 | 0.33 | 1.460 | 3.766 | 2.65 | 0.9821 | 1642 |
| mixed | alerta-8x4-pgb | 3/3 | 18.1 | 392.63 | 1252.80 | 44.4 | 306.29 | 1068.63 | 0 | 0.85 | 0.36 | 13.430 | 5.667 | 4.77 | 0.9774 | 1373 |

## What this does not establish

No capacity for this laptop or for any other host. Other work was active:
the one-minute load average at the start is given above, and it is above
the logical CPU count. The hosted 17 November mixed report
is a different runner, a different alrust commit, and an `alerta-4x4-pgb` arm. This page
does not divide one run's rates by the other's. The results file records no
pool-wait, session-peak, or lock-wait samples. `pg_sessions` and the key-update
figures mean what the methodology says they mean, including the lag in
PostgreSQL's statistics. It is one run. Nothing here qualifies a release or
changes the default pool size.
