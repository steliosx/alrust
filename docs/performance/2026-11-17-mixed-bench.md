# Mixed load, pool size and key use: the 17 November 2026 run

One full run of the mixed suite, as
[the benchmark methodology](benchmark-methodology.md) specifies it. The
owner approved this one run (DECISIONS Y71). Every number here is from the
run's `results.json`, kept beside this file as
[2026-11-17-mixed-bench-results.json](2026-11-17-mixed-bench-results.json)
(95,259 bytes, SHA256
`92144117aaf77faaaf6e1f4c557f5ddd857db11fcb7f5bfba806dc3b686ba7eb`).
The medians in the table were recomputed from that file and equal the
run's own summary.

These are relative numbers from one shared 4-vCPU hosted runner. They are
not a capacity claim, and they do not transfer to another machine or
workload.

## The run

| | |
|---|---|
| Workflow run | `44118467373`, attempt 1, started by hand (`workflow_dispatch`), mode full, suite mixed |
| Commit | `main` `d9550949a46508e2b62d91ec524040e08fe06402` |
| Time | 2026-11-17, 10:48 to 11:12 EST |
| Conclusion | success; no step failed |
| alrust | built from that commit; pool and key-use mode per arm, below |
| Alerta | 9.1.0 (`f4312727`), 4 workers of 4 threads behind PgBouncer 1.26.1 |
| PostgreSQL | 18.7 (`18.7-1.pgdg24.04+2`) |
| Shape | 3 repetitions; 5 s warm-up, 15 s timed; medians; 8 writers, 16 readers |

A subagent started the run once, watched it and downloaded the files, from
a written brief. It interpreted nothing. Claude checked the numbers against
the raw file.

Arms: `alrust-p16`, `alrust-p32` and `alrust-p64` set
`ALRUST_DB_MAX_CONNECTIONS` to 16, 32 and 64 with key use written about
once a second; `alrust-p16-immediate` has a pool of 16 and writes key use
on every request (`ALRUST_KEY_USAGE_IMMEDIATE=true`); `alerta-4x4-pgb`
is the reference.

## Validity

All 45 cells (5 arms, 3 cases, 3 repetitions) are valid: no error and no
non-2xx answer in any phase, no list-body problem, every server alive at
the end, every 2xx write found in the database with the right
`duplicate_count`, and the final list and count equal to the final rows.
The checks are those on `main` at the commit above; their limits are in
the last section.

The columns do not share one denominator. Rates and latencies are of 2xx
answers that ended in the timed phase. CPU is of both classes together
over the timed interval, per second and per completed request. Commits
and key updates per request cover every phase. One must not be divided by
another.

## Results

| case | arm | valid reps | writes/s | write p50 ms | write p99 ms | reads/s | read p50 ms | read p99 ms | errors+non-2xx (all reps) | server CPU s/s | postgres CPU s/s | server CPU ms/req | postgres CPU ms/req | commits/req | key updates/req | final rows |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| writers-alone | alrust-p16 | 3/3 | 4700.4 | 1.68 | 2.60 | - | - | - | 0 | 1.31 | 1.31 | 0.279 | 0.280 | 1.92 | 0.0006 | 9000 |
| writers-alone | alrust-p32 | 3/3 | 4681.9 | 1.68 | 2.61 | - | - | - | 0 | 1.32 | 1.32 | 0.282 | 0.281 | 1.94 | 0.0007 | 9000 |
| writers-alone | alrust-p64 | 3/3 | 4694.3 | 1.68 | 2.60 | - | - | - | 0 | 1.32 | 1.32 | 0.280 | 0.280 | 1.92 | 0.0006 | 9000 |
| writers-alone | alrust-p16-immediate | 3/3 | 4052.7 | 1.93 | 3.08 | - | - | - | 0 | 1.20 | 1.44 | 0.297 | 0.356 | 1.91 | 0.9567 | 9000 |
| writers-alone | alerta-4x4-pgb | 3/3 | 307.3 | 26.09 | 36.43 | - | - | - | 0 | 2.45 | 0.83 | 7.974 | 2.705 | 6.75 | 0.9648 | 7257 |
| readers-alone | alrust-p16 | 3/3 | - | - | - | 1068.9 | 13.97 | 24.71 | 0 | 1.25 | 1.63 | 1.172 | 1.535 | 2.88 | 0.0027 | 1000 |
| readers-alone | alrust-p32 | 3/3 | - | - | - | 1058.5 | 14.07 | 24.64 | 0 | 1.25 | 1.64 | 1.180 | 1.557 | 2.89 | 0.0023 | 1000 |
| readers-alone | alrust-p64 | 3/3 | - | - | - | 1067.8 | 13.93 | 24.80 | 0 | 1.25 | 1.65 | 1.170 | 1.550 | 2.89 | 0.0025 | 1000 |
| readers-alone | alrust-p16-immediate | 3/3 | - | - | - | 1042.2 | 14.31 | 24.43 | 0 | 1.23 | 1.68 | 1.172 | 1.611 | 2.86 | 0.9525 | 1000 |
| readers-alone | alerta-4x4-pgb | 3/3 | - | - | - | 258.6 | 45.21 | 120.14 | 0 | 2.55 | 0.70 | 9.842 | 2.701 | 3.89 | 0.9707 | 1000 |
| mixed | alrust-p16 | 3/3 | 343.8 | 23.16 | 42.36 | 418.8 | 36.24 | 65.44 | 0 | 0.54 | 2.90 | 0.704 | 3.806 | 2.51 | 0.0031 | 8703 |
| mixed | alrust-p32 | 3/3 | 380.2 | 20.99 | 46.60 | 422.7 | 35.22 | 72.90 | 0 | 0.55 | 2.91 | 0.683 | 3.633 | 2.49 | 0.0028 | 8970 |
| mixed | alrust-p64 | 3/3 | 383.2 | 20.57 | 47.23 | 420.1 | 35.51 | 72.26 | 0 | 0.54 | 2.92 | 0.676 | 3.645 | 2.50 | 0.0027 | 9000 |
| mixed | alrust-p16-immediate | 3/3 | 329.2 | 24.00 | 42.79 | 418.7 | 36.37 | 62.39 | 0 | 0.54 | 2.85 | 0.725 | 3.816 | 2.51 | 0.9731 | 8342 |
| mixed | alerta-4x4-pgb | 3/3 | 92.7 | 77.85 | 190.16 | 174.2 | 73.83 | 179.55 | 0 | 2.49 | 0.98 | 9.369 | 3.668 | 4.90 | 0.9728 | 2877 |

The three repetitions of each cell are close. For example, mixed writes/s
per repetition: `alrust-p16` 344, 345, 344; `alrust-p32` 380, 373, 381;
`alrust-p64` 388, 376, 383; `alrust-p16-immediate` 327, 332, 329
(rounded; the file has the exact values).

## What the run shows, on this workload and this runner

**Pool size.** With writers alone or readers alone, 16, 32 and 64 give the
same rates (within about 1%). In the mixed case, writes rose from 343.8/s
at 16 to 380.2/s at 32 (about 11% more) and 383.2/s at 64; reads did not
change (418.8, 422.7, 420.1/s). The repetitions of 16 and 32 do not
overlap. Write p50 fell from 23.2 ms to 21.0 ms; write p99 did not fall
(42.4, 46.6, 47.2 ms). So a larger pool helped mixed writes a little, and
going past 32 added nothing that this run can see.

**Increasing the configured pool size did not remove the mixed-write
slowdown in this run.** Writers alone reach
about 4,700/s; beside 16 readers they reach 344 to 383/s at every pool
size. In the mixed case PostgreSQL uses about 2.9 of the runner's 4 CPUs
and the alrust server about 0.5; PostgreSQL's CPU per completed request is
about 3.7 ms, against 0.28 ms for writers alone and 1.5 ms for readers
alone on the 1,000-row table. That fits the cost named in the methodology
(every list counts all matching rows, and the table grows to 9,000), but
this run does not show the cause: it records no wait times, no pool
occupancy and no lock waits, and the Python clients share the same 4 CPUs.

**Key use.** Writing key use on every request instead of about once a
second cost, at a pool of 16: writers alone 4,052.7/s against 4,700.4/s
(the once-a-second mode is about 16% faster); readers alone 1,042.2/s
against 1,068.9/s (about 3%); mixed writes 329.2/s against 343.8/s (about
4%), mixed reads equal. The key-updates column confirms the mode: about
0.96 row updates per request when immediate, under 0.004 otherwise.

**Alerta, same job.** Writers alone 307.3/s, readers alone 258.6/s, mixed
92.7 writes/s and 174.2 reads/s. alrust at its default pool was about 15
times faster on writes alone, about 4 times on reads alone, and in the
mixed case about 3.7 times on writes and 2.4 times on reads. One caution:
Alerta's writers did not reach all 8,000 identities in a cell (final rows
7,257 alone and 2,877 mixed, against 9,000 and 8,703 for alrust), so its
share of inserts to duplicates, and the table its readers saw, differ from
alrust's. The ratios compare the two servers under the same clients, not
on identical row histories.

## What it does not show

- No capacity for any production host, and nothing about another runner.
- Not where requests wait. A sampler of pool waits, sessions and locks
  would be needed for that.
- Not that the pool was fully used: `pg_sessions` counts sessions opened
  in a cell (16 at p16, 32 at p32, 39 to 41 at p64 in the mixed case), not
  a peak, and the start-up probe only shows that a size of 0 is refused.
- The key-update figures come from PostgreSQL's statistics, which lag.
- Clients start at a common time with no readiness barrier.
- No test runs the real `mixed_cell` or `main` failure path against a
  database; the gates were reviewed in source and by fixtures (#266).
- It is one run. It is not compared with the 15 November run or with #255:
  the workload, the commit and the checks differ.

## What follows

The default pool size stays 16; whether to raise it to 32 is the owner's
choice, with these numbers. Reading the count and the page of a list on
one connection, and the cost of the per-list count, are separate later
measurements.
