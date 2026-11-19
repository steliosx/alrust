# Alrust and Alerta benchmark review, 19 November 2026

This is a review of the published benchmark evidence and a proposal for
the next test. It is not release qualification or approval to run a
benchmark or change deployment settings.

## What the runs show

[Round 2 (PR #270)](https://github.com/steliosx/alrust/pull/270)
pinned four CPUs for the app, two for PostgreSQL 18.7, and two for load
generation and sampling. The report verifies CPU-set assignments, not
physical-core or SMT sibling isolation. Each reported number is a median of three
repetitions. Alrust used its 16-connection default.

| Workload | Alrust | Alerta uWSGI (5) | Alerta Gunicorn (8 × 4) |
| --- | ---: | ---: | ---: |
| List, 16 clients (requests/s) | 1,777 | 299 | 322 |
| Count, 8 clients (requests/s) | 8,085 | 411 | 423 |
| Mixed, writes/s | 270.2 | 100.2 | 77.8 |
| Mixed, reads/s | 390.3 | 130.4 | 152.9 |
| Mixed, write p99 (ms) | 63.1 | 125.8 | 199.3 |
| Mixed, read p99 (ms) | 88.3 | 171.7 | 197.4 |

The mixed cell (eight writers, 16 readers, 15 seconds timed) is the
more relevant comparison for an alert console seeing live traffic.
Alrust delivered 2.7× the writes and 2.6× the reads of the best Alerta
arm for each metric. The separate 6,499 alerts/s result at 32 writers
used a fixed sequence Alrust finished in about one second. It is a burst
rate, not sustained throughput. Mixed arms reached different numbers of
alert identities and thus different final table sizes. The report says
read parity was checked before and after a host reset; the timed raw logs
are not in the public PR, so Round 2 figures here are reported, not
independently replayed.

The [17 November hosted run](2026-11-17-mixed-bench.md) used a different
setup: one shared 4-vCPU runner and an Alerta Gunicorn 4 × 4 arm behind
PgBouncer. Recomputing from the [public JSON](2026-11-17-mixed-bench-results.json)
gave 343.84 vs 92.73 writes/s and 418.76 vs 174.20 reads/s for Alrust
pool 16 vs Alerta. All 45 cells were marked valid. Do not compare
absolute rates across the two environments. The
[15 November diagnostic](2026-11-15-bench-compare.md) also shows why
Alerta pooling is a useful control: it reduced connection churn and
database CPU, but did not remove the app-side CPU gap.

## Tuning implications

- **Keep 16 as the default for now.** Round 2 increased mixed writes by
  about 20% at pool 64 (270.2 to 325.3/s), but read p99 rose from 88.3 to
  123.3 ms. PostgreSQL averaged 1.93 of its two allocated CPUs at pool
  16, while the app averaged 0.58 of four. This points to database work
  as the next thing to measure, not proof of where requests waited.
- **Measure the full list endpoint before adding a recent-alert index.**
  The page is only part of the list cost; the endpoint also computes
  counts. In a separate synthetic PostgreSQL 17.12 SQL-only probe, a
  candidate B-tree on customer, environment, status and descending last
  receive time reduced top-50 query execution from 0.610 to 0.110 ms.
  Updating 1,000 timestamps increased median execution from 7.73 to
  13.49 ms and WAL volume by 27%. This is neither an application result
  nor a PostgreSQL 18.7 result. Alerta's
  [repeat-alert update](https://github.com/alerta/alerta/blob/v9.1.0/alerta/database/backends/postgres/base.py#L216-L261)
  changes that timestamp, so retain both the read and write arms.

## Proposed next benchmark

1. Recover the exact seed generator noted as missing in PR #270. The
   [archived launcher](https://github.com/steliosx/alrust/blob/60411a2ebbd80de38e9afe06adcaa9e953cc902d/docs/performance/2026-11-18-alerta-round2-4core/scripts/run_all.py#L83-L88)
   links to the copy on the original benchmark machine. If that copy
   cannot be recovered, the [main-branch seeder](https://github.com/steliosx/alrust/blob/38c671382e8aa0860d616e4931a0a82dfeafc9de/scripts/bench/bench_compare.py#L328-L348)
   can start a new baseline; its equivalence to Round 2 is unproven.
   Verify a fresh setup from an empty database. Pin the benchmark and
   server revisions, DB image, seed checksum, client schedule and
   resource limits. Record physical-core and SMT topology.
2. Use identical seeded data and a fixed, predeclared offered rate.
   Start with a repeat-heavy fixed-identity cell to hold cardinality
   constant, then add a separate, controlled new-alert mix. Compare
   both at offered rates the slower arm can sustain before testing
   saturation. Verify response parity, persisted rows, duplicate counts
   and error rates. Report offered and achieved rates separately.
3. Time the full list endpoint, its counts and page query separately.
   Capture read and write p95/p99, database CPU, pool checkout and lock
   waits, WAL bytes per successful write, and the share of heap-only
   updates. Test the index against the unmodified schema with equal
   durations; keep app/pool settings unchanged for that A/B.
4. Use the pinned 4/2/2 layout for direct Round 2 comparisons. Label any
   smaller host or managed-database run as diagnostic, and report CPU
   allocation and thermal limits. Do not run against production.

No benchmark was dispatched for this review.
