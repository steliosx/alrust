# Mixed load on a hosted runner, again: the 20 November 2026 run

A second full run of the mixed suite on a GitHub-hosted runner, three days after the
[17 November run](2026-11-17-mixed-bench.md). The harness was the same, and alrust's code only gained lines (no
existing line changed). So the run is mostly a check of how well the 17 November rates repeat. The suite is specified in [the benchmark methodology](benchmark-methodology.md).

These are relative numbers from one shared 4-vCPU hosted runner. They are not a capacity claim,
and they do not transfer to another machine or workload.

## Answer in short

- **The 17 November rates repeat.** For alrust, every median rate is within 3% of the 17 November
  median for the same cell (−2.8% to +0.5%). For Alerta, within 4% (−1.0% to +3.7%). Latency
  medians moved more for Alerta (see below).
- **Pool size: the same picture.** In the mixed case, alrust's writes rose from 345.5/s with a pool
  of 16 to 375.9/s with 32; a pool of 64 gave 372.3/s. The repetitions of 16 and 32 do not overlap.
  With writers alone or readers alone the pool size made no difference that this run can see.
- **Key use written on every request costs writes.** Writers alone at a pool of 16: 3992.0/s against
  4641.0/s when key use is written about once a second.
- **Alerta's mixed writes varied the most:** 77.8, 110.8 and 92.7 writes/s in the three repetitions.
  Its median (92.7/s) equals the 17 November median, but a single repetition of this cell is not
  a reliable measure.

## The run

| | |
|---|---|
| Workflow run | `44744034066`, attempt 1, workflow `bench compare`, mode full, suite mixed |
| Started | 2026-11-20T13:24:27Z, by hand (`workflow_dispatch`) |
| Commit | `main` `38c671382e8aa0860d616e4931a0a82dfeafc9de` |
| Conclusion | success; 45 of 45 cells valid |
| Machine | one GitHub-hosted runner, 4 vCPUs, 15701 MiB of memory, image `ubuntu24 20261116.133.1` |
| alrust | built from that commit; pool and key-use mode per arm, below |
| Alerta | 9.1.0 (`f4312727`), 4 workers of 4 threads behind PgBouncer 1.26.1 |
| PostgreSQL | 18.7 (`18.7-1.pgdg24.04+2`) |
| Shape | 3 repetitions; 5 s warm-up, 15 s timed; medians; 8 writers with 1,000 alert identities each, 16 readers of the 50 newest alerts |
| Artifact | `bench-compare-44744034066-1`, 14031833 bytes, digest `sha256:7f4f73973595ad09ad80e995ece954e2705bf8732cc5180a4c6ef3b7128110c1` as GitHub reports it, kept by GitHub until 2026-11-27T13:46:33Z |
| Results file | [2026-11-20-mixed-bench-hosted-results.json](2026-11-20-mixed-bench-hosted-results.json), the run's `results.json`, unchanged; it stays after the artifact expires |

Codex checked this run on 20 November, before this report existed: its checkpoint of that day
(`benchmark-window-20261120T124851Z/CHECKPOINT.md`, cited in its signed review `cfc43820`) records
this run and 45 of 45 cells and checks passing. This page adds the report in the repository, the
results file, and a second check of the artifact.

**What changed since the 17 November run** (`d9550949` to `38c67138`): nothing under
`scripts/bench/` or in the workflow. In `crates/`, 9 files gained 2234 lines and lost none: new
routes (`/config`, top-N lists, `/environments`, `/services`, `/alerts/groups`, `/alerts/tags`),
their handlers, store queries and tests, and one new settings field (an OAuth2 client id for
`/config`). The mixed workload sends `POST /alert` and the alert list, and calls none of the new
routes.

**Arms:** `alrust-p16`, `alrust-p32` and `alrust-p64` set `ALRUST_DB_MAX_CONNECTIONS` to 16, 32
and 64, with key use written about once a second. `alrust-p16-immediate` has a pool of 16 and
writes key use on every request (`ALRUST_KEY_USAGE_IMMEDIATE=true`). `alerta-4x4-pgb` is the
reference.

**The same laptop day.** A laptop run of the same suite at the same commit is reported in
[2026-11-20-mixed-bench.md](2026-11-20-mixed-bench.md). That laptop was in use and is a different
machine; this page does not compare the two.

## Validity

All 45 cells (5 arms, 3 cases, 3 repetitions) are valid: no error and no non-2xx answer in any
phase, every list answer in order, every server alive at the end, every 2xx write found in the
database with the right `duplicate_count`, and the final list and count equal to the final rows.
Before each cell, the read check passed: the total and the 50 newest ids, every listed alert's
fields against the database, the count's groups against SQL, and the same bodies as the first
arm (`href` origins aside).

As in the 17 November report, the columns do not share one denominator. Rates and latencies are
of 2xx answers that ended in the timed phase. CPU is of both classes together over the timed
interval. Commits and key updates per request cover every phase.

## Results

Medians of three repetitions, from the run's `summary.md`:

| case | arm | valid reps | writes/s | write p50 ms | write p99 ms | reads/s | read p50 ms | read p99 ms | errors+non-2xx (all reps) | server CPU s/s | postgres CPU s/s | server CPU ms/req | postgres CPU ms/req | commits/req | key updates/req | final rows |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| writers-alone | alrust-p16 | 3/3 | 4641.0 | 1.70 | 2.64 | - | - | - | 0 | 1.32 | 1.32 | 0.284 | 0.287 | 1.93 | 0.0006 | 9000 |
| writers-alone | alrust-p32 | 3/3 | 4643.4 | 1.70 | 2.63 | - | - | - | 0 | 1.32 | 1.32 | 0.284 | 0.284 | 1.93 | 0.0006 | 9000 |
| writers-alone | alrust-p64 | 3/3 | 4645.0 | 1.70 | 2.62 | - | - | - | 0 | 1.31 | 1.32 | 0.282 | 0.285 | 1.92 | 0.0006 | 9000 |
| writers-alone | alrust-p16-immediate | 3/3 | 3992.0 | 1.96 | 3.14 | - | - | - | 0 | 1.20 | 1.45 | 0.300 | 0.365 | 1.91 | 0.9563 | 9000 |
| writers-alone | alerta-4x4-pgb | 3/3 | 304.3 | 26.26 | 37.20 | - | - | - | 0 | 2.46 | 0.83 | 8.077 | 2.727 | 6.74 | 0.9634 | 7192 |
| readers-alone | alrust-p16 | 3/3 | - | - | - | 1065.2 | 14.03 | 24.91 | 0 | 1.26 | 1.63 | 1.180 | 1.544 | 2.88 | 0.0025 | 1000 |
| readers-alone | alrust-p32 | 3/3 | - | - | - | 1059.0 | 13.99 | 25.09 | 0 | 1.26 | 1.64 | 1.185 | 1.557 | 2.90 | 0.0028 | 1000 |
| readers-alone | alrust-p64 | 3/3 | - | - | - | 1041.9 | 14.23 | 25.19 | 0 | 1.24 | 1.65 | 1.191 | 1.583 | 2.89 | 0.0023 | 1000 |
| readers-alone | alrust-p16-immediate | 3/3 | - | - | - | 1030.4 | 14.45 | 24.78 | 0 | 1.23 | 1.68 | 1.192 | 1.626 | 2.86 | 0.9521 | 1000 |
| readers-alone | alerta-4x4-pgb | 3/3 | - | - | - | 268.2 | 55.42 | 108.47 | 0 | 2.65 | 0.74 | 9.845 | 2.747 | 3.90 | 0.9746 | 1000 |
| mixed | alrust-p16 | 3/3 | 345.5 | 22.95 | 42.09 | 417.8 | 36.40 | 63.62 | 0 | 0.54 | 2.91 | 0.703 | 3.814 | 2.50 | 0.0031 | 8724 |
| mixed | alrust-p32 | 3/3 | 375.9 | 21.02 | 47.38 | 419.2 | 35.33 | 72.96 | 0 | 0.54 | 2.90 | 0.680 | 3.629 | 2.50 | 0.0028 | 8973 |
| mixed | alrust-p64 | 3/3 | 372.3 | 21.30 | 47.83 | 420.2 | 35.56 | 72.67 | 0 | 0.55 | 2.90 | 0.691 | 3.651 | 2.50 | 0.0030 | 8968 |
| mixed | alrust-p16-immediate | 3/3 | 330.1 | 23.93 | 42.90 | 418.5 | 36.33 | 62.74 | 0 | 0.55 | 2.87 | 0.728 | 3.818 | 2.51 | 0.9730 | 8383 |
| mixed | alerta-4x4-pgb | 3/3 | 92.7 | 74.54 | 201.02 | 177.6 | 69.24 | 194.73 | 0 | 2.50 | 0.99 | 9.400 | 3.728 | 4.91 | 0.9752 | 2884 |

Rates in each repetition (`-` where the cell has no such class):

| case | arm | writes/s r0 | writes/s r1 | writes/s r2 | reads/s r0 | reads/s r1 | reads/s r2 |
|---|---|---:|---:|---:|---:|---:|---:|
| writers-alone | alrust-p16 | 4641.0 | 4607.9 | 4650.6 | - | - | - |
| writers-alone | alrust-p32 | 4641.8 | 4643.4 | 4656.0 | - | - | - |
| writers-alone | alrust-p64 | 4697.8 | 4638.4 | 4645.0 | - | - | - |
| writers-alone | alrust-p16-immediate | 4007.3 | 3970.2 | 3992.0 | - | - | - |
| writers-alone | alerta-4x4-pgb | 310.7 | 302.4 | 304.3 | - | - | - |
| readers-alone | alrust-p16 | - | - | - | 1065.2 | 1035.0 | 1067.6 |
| readers-alone | alrust-p32 | - | - | - | 1068.2 | 1037.2 | 1059.0 |
| readers-alone | alrust-p64 | - | - | - | 1063.3 | 1041.9 | 1027.0 |
| readers-alone | alrust-p16-immediate | - | - | - | 1038.2 | 1014.2 | 1030.4 |
| readers-alone | alerta-4x4-pgb | - | - | - | 270.3 | 261.9 | 268.2 |
| mixed | alrust-p16 | 346.8 | 339.7 | 345.5 | 418.2 | 416.8 | 417.8 |
| mixed | alrust-p32 | 375.9 | 371.2 | 383.6 | 424.1 | 419.2 | 418.9 |
| mixed | alrust-p64 | 374.2 | 372.3 | 369.3 | 420.2 | 413.0 | 421.3 |
| mixed | alrust-p16-immediate | 334.2 | 323.5 | 330.1 | 418.5 | 418.1 | 420.6 |
| mixed | alerta-4x4-pgb | 77.8 | 110.8 | 92.7 | 190.2 | 152.7 | 177.6 |

## Against the 17 November run

Medians of each run, and the change from 17 to 20 November. The 17 November medians are those of
[its report](2026-11-17-mixed-bench.md). Both runs used the same harness on runners of the same
type, not the same machine. Every cross-date percentage change is computed from the unrounded
medians of the two results files and then rounded to one decimal, so a few differ by 0.1 from what
the rounded table values give.

| case | arm | writes/s 17 Nov | writes/s 20 Nov | change | reads/s 17 Nov | reads/s 20 Nov | change |
|---|---|---:|---:|---:|---:|---:|---:|
| writers-alone | alrust-p16 | 4700.4 | 4641.0 | -1.3% | - | - | - |
| writers-alone | alrust-p32 | 4681.9 | 4643.4 | -0.8% | - | - | - |
| writers-alone | alrust-p64 | 4694.3 | 4645.0 | -1.0% | - | - | - |
| writers-alone | alrust-p16-immediate | 4052.7 | 3992.0 | -1.5% | - | - | - |
| writers-alone | alerta-4x4-pgb | 307.3 | 304.3 | -1.0% | - | - | - |
| readers-alone | alrust-p16 | - | - | - | 1068.9 | 1065.2 | -0.3% |
| readers-alone | alrust-p32 | - | - | - | 1058.5 | 1059.0 | +0.1% |
| readers-alone | alrust-p64 | - | - | - | 1067.8 | 1041.9 | -2.4% |
| readers-alone | alrust-p16-immediate | - | - | - | 1042.2 | 1030.4 | -1.1% |
| readers-alone | alerta-4x4-pgb | - | - | - | 258.6 | 268.2 | +3.7% |
| mixed | alrust-p16 | 343.8 | 345.5 | +0.5% | 418.8 | 417.8 | -0.2% |
| mixed | alrust-p32 | 380.2 | 375.9 | -1.1% | 422.7 | 419.2 | -0.8% |
| mixed | alrust-p64 | 383.2 | 372.3 | -2.8% | 420.1 | 420.2 | +0.0% |
| mixed | alrust-p16-immediate | 329.2 | 330.1 | +0.3% | 418.7 | 418.5 | -0.0% |
| mixed | alerta-4x4-pgb | 92.7 | 92.7 | -0.0% | 174.2 | 177.6 | +2.0% |

## What the run shows, on this workload and this runner

- **Repeatability of rates.** Two runs three days apart, with the same harness, give median rates
  within 3% for every alrust cell and within 4% for every Alerta cell. The pool-size and key-use
  findings of the 17 November report hold in this run too.
- **Latency repeats less well for Alerta.** alrust's p50 and p99 medians moved by −2.8% to +3.6%
  (the largest: mixed write p50 at a pool of 64, 20.57 to 21.30 ms). Alerta's moved by −9.7% to
  +22.6%: readers-alone p50 45.21 to 55.42 ms while its rate rose 3.7%, and mixed read p99 179.55
  to 194.73 ms.
- **Pool size.** In the mixed case, 16 to 32 raised writes from 345.5/s to 375.9/s (the
  repetitions, 339.7 to 346.8 and 371.2 to 383.6, do not overlap); 64 gave 372.3/s, no more than 32.
  Reads stayed at 417.8 to 420.2/s. With writers alone or readers alone, the pool sizes are within
  about 2% of each other.
- **The mixed-write slowdown remains.** Writers alone reach about 4,600/s; beside 16 readers they
  reach 345.5 to 375.9/s. As on 17 November, PostgreSQL uses about 2.9 CPU seconds per second in
  the mixed case. This run, like that one, records no wait times, so it does not show the cause.
- **Key use.** Written on every request, it cost about 14% of writers-alone throughput (3992.0
  against 4641.0/s) and about 3% of readers-alone throughput (1030.4 against 1065.2/s); mixed
  writes 330.1 against 345.5/s; mixed reads equal.
- **Alerta.** Its writers again did not reach all 8,000 identities in a cell (final rows 7192 alone
  and 2884 mixed), so its row history differs from alrust's, as on 17 November. Its mixed writes
  varied from 77.8 to 110.8/s between repetitions: the largest is 42.4% above the smallest. No
  alrust cell has a relative spread above 3.6%. (In writes per second, alrust's writers-alone cell
  at a pool of 64 has the wider range, 4638.4 to 4697.8, which is 1.3%.)

## What it does not show

- No capacity for any production host, and nothing about another runner.
- Not where requests wait: no sampler of pool waits, sessions or locks.
- Not that the larger pools were fully used.
- The key-update figures come from PostgreSQL's statistics, which lag.
- Two runs are a small sample for a repeatability claim; they show that these medians moved by
  a few percent between two hosted runners, not the full spread of such runners.
