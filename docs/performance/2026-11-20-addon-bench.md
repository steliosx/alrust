# The alerta_fast add-on's first patches: the addon suite, 20 November 2026

One full run of the `addon` suite. It measures Alerta as released, with no fork, and the start-up
patches of the add-on `scripts/bench/alerta_fast.py` as they were at that time: `reuse`; `safe`
(`reuse` with `nolog`, `onecount` and `nosentry`); and `safe-hot` (`safe` with the opt-in `hot`). The numbers are diagnostic and relative to one hosted runner. They are not a capacity claim.

## Answer in short

- **`reuse` gives the largest step.** Keeping one PostgreSQL connection per worker thread gave the
  largest step of the three add-on arms in all six pairs of case and client count. Duplicate
  writes at 8 clients went from 263.4 to 915.6 requests a second (median).
- **`safe` adds a smaller step on top of `reuse`:** count at 8 clients went from 1333.2 to 1631.8,
  and list at 1 client from 195.8 to 219.6. Its three patches were not run one at a time, so this
  run does not show each one's share.
- **`safe-hot` adds more on the read and write cases.** Count at 1 client went from 603.5 to 859.4,
  and duplicate writes at 8 clients from 999.5 to 1260.8. `hot` writes key-use counts and metrics
  about once a second, so its counts are best effort. It is not for an installation that needs
  exact counts.
- **The best arm that changes no code is `pgbu-nofwd`.** It is below `reuse` in all six pairs, and
  it reaches part of the gain: count at 8 clients went from 311.5 to 1000.3 requests a second.
- **Limits.** Every add-on arm was faster than the base in every repetition. But the run used one runner, 1000 synthetic alerts and one fixed duplicate alert, on loopback only, with
  no Patroni and no replication. The benchmark's requests did not hold an application context
  over several requests, and the harness sets no `SENTRY_DSN`, so those cases were not tested.

## How this report was made

- **Run:** GitHub Actions run 44818483781, attempt 1, workflow `bench compare`, mode `full`, suite
  `addon`, on `main` at `9b59d75acd9caf8772f315515013afdee82a5cbc`. Started by hand
  (workflow_dispatch) on `main` by the project's agent, after the owner approved one full run of
  this suite. The review of this report is the first independent check of the result. The run
  started at 2026-11-20T21:14:02Z.
- **Artifact:** `bench-compare-44818483781-1`, 2006778 bytes, digest
  `sha256:327714733e7f4155cf82bd42f8c980e410194ce7878a5da8c8174df9bee67423` as GitHub reports it.
  GitHub keeps the artifact until 2026-11-27T22:00:38Z. The results file here stays.
- **Results file:** [2026-11-20-addon-bench-results.json](2026-11-20-addon-bench-results.json), the
  run's `results.json`, unchanged.
- **Machine:** one GitHub-hosted runner, 4 vCPUs, 15701 MiB of memory, image
  `ubuntu24 20261116.133.1`. Servers, PostgreSQL and the load tool share it, over loopback.
- **Software:** PostgreSQL 18.7 (PGDG package `18.7-1.pgdg24.04+2`), PgBouncer 1.26.1, Alerta
  9.1.0 from the pinned `reference/alerta` submodule (`f4312727`), gunicorn 27.0.1 with the
  gthread worker, load tool oha 1.17.0.
- **Add-on:** `scripts/bench/alerta_fast.py` at git blob `89bb052de2d846dab413ebfff3554bf6e6f71c75`
  (see the version note below).
- **Shape:** 6 arms, 3 cases, 1 and 8 clients, 3 repetitions: 108 cells. Warm-up 5 s, timed 15 s.
  The tables show the median of the three repetitions. 1000 seeded alerts.

Every arm is Alerta with 4 worker processes of 4 threads, and no `forwarder` plugin. The docstring
of the benchmark script at commit `9b59d75a` says why: with no destination set, `forwarder` forwards
nothing, but its `post_receive` returns the alert, so every receive also rewrites the alert's tags
and attributes. Removing it takes those writes out.

| Arm | What it is |
|---|---|
| `alrust` | the reference only, not a comparison target for the add-on |
| `alerta-4x4-nofwd` | the base: Alerta with `forwarder` removed, direct connections |
| `alerta-4x4-pgbu-nofwd` | the base behind pgbouncer on a Unix socket with `trust`; the best arm that changes no code |
| `alerta-4x4-reuse-nofwd` | the add-on with `reuse`: one kept connection per worker thread |
| `alerta-4x4-safe-nofwd` | the add-on's default `safe`: `reuse`, `nolog`, `onecount`, `nosentry` |
| `alerta-4x4-safe-hot-nofwd` | `safe` and `hot`, the opt-in patch: key-use and metrics writes about once a second |

The three cases are `list` (`GET /alerts`, 50 newest), `count` (`GET /alerts/count`) and
`write-dup` (`POST /alert`, one fixed duplicate body).

## What each patch changes

- **`reuse` (against the base):** the base opens a new PostgreSQL connection for each request. With
  `reuse`, each worker thread keeps one connection. A request ends with a ROLLBACK, and a connection
  is replaced, not reused, when the server closed it, reports a standby or read-only session, a
  statement failed on it, the ROLLBACK failed, or it is older than 300 seconds. Sessions per request
  fall from 1.000 (base) to 0.000 in every case, as the summary's sessions per request show.
- **`safe` (against `reuse`):** three more patches. `nolog` skips building SQL debug text unless
  DEBUG is on. `onecount` reads the severity and status totals of the list and the count with
  one grouped query in place of two scans of the same rows. `nosentry` does not start the Sentry SDK when no `SENTRY_DSN` is set.
  Together they are one arm, so this run does not split their effects.
- **`safe-hot` (against `safe`):** `hot` is opt-in and best effort. The API key's use count and
  last-used time, and the `metrics` timers and counters, are collected in the process and written
  about once a second (`ALERTA_FAST_FLUSH_SECONDS`, default 1.0). Key verification is not cached.
  What changes, from the add-on's header:
  - late: `GET /keys` and `/management/metrics` are behind by up to the interval;
  - lost: a worker that is killed, or stops while the database cannot be written, loses what it
    had not written;
  - too high: a batch that committed but was reported as failed is written again.

  Do not use `hot` where key-use counts or metrics must be exact. The summary's commits per request
  for `safe-hot` are 0.00 or 0.01 for `list` and `count`, and 0.96 or 1.00 for `write-dup`.

## Checks

- 108 of 108 cells valid. None missing, none invalid.
- No cell had an error or a non-2xx answer.
- 36 of 36 duplicate-write repetitions saved exactly the writes that were counted.
- Read checks passed for all six arms: the total, the 50 newest ids, every listed alert's fields
  against the database, and the count's severity and status groups against SQL. Every arm's list
  and count bodies equal the first arm's, with `href` origins aside. The list body was 41098 bytes
  for `alrust` and 44180 bytes for the Alerta arms; no body was compressed.
- Add-on probes passed before the first cell (see below).

## Add-on probes

Before the first cell the run checks the add-on in three ways. A failure would have stopped the
run before anything was scored. The results are in the run's `addon_probe` block.

- **Counts:** 50 requests on the `safe-hot` arm. The key's use count and the timers' count each
  grew by exactly 50 (`[50, 50]`). One start alone added `[0, 0]`. No request answered other than
  200.
- **Stop:** with a one-hour flush interval, 50 requests left the database unchanged (`[0, 0]`), so
  the batch was known to wait. The worker then stopped without a kill (`stopped_without_kill`
  true), and the database grew by `[50, 50]`, which was the wanted value.
- **Connections:** on one worker with one thread (`alerta-1x1-safe-nofwd`, without `hot`):
  - three requests shared one backend (16166);
  - that backend was terminated (the call answered true), and the next request was answered (200)
    on a new backend (16173);
  - new sessions were made read-only, and the backend was terminated again. One alert write
    answered 500 and saved nothing;
  - after the read-only setting was reset, the next request answered 200 on another new backend
    (16189). The failed write was still absent, so nothing replayed it. A new write answered 201
    and saved exactly one alert.

## Results

Medians of the three repetitions. Server CPU is the gunicorn master and workers, from `/proc`.
Commits and sessions are PostgreSQL's counts per request.

| Case | Clients | Arm | Requests/s | p50 ms | p99 ms | Sessions per request | Commits per request | Server CPU ms per request | PostgreSQL CPU ms per request |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|
| list | 1 | `alrust` | 509.8 | 1.93 | 2.46 | 0.000 | 2.88 | 0.897 | 1.101 |
| list | 1 | `alerta-4x4-nofwd` | 92.5 | 10.77 | 11.40 | 1.000 | 4.00 | 6.525 | 5.443 |
| list | 1 | `alerta-4x4-pgbu-nofwd` | 182.7 | 5.30 | 6.29 | 0.000 | 3.75 | 3.247 | 1.777 |
| list | 1 | `alerta-4x4-reuse-nofwd` | 195.8 | 5.10 | 5.93 | 0.000 | 1.88 | 3.197 | 1.776 |
| list | 1 | `alerta-4x4-safe-nofwd` | 219.6 | 4.51 | 5.36 | 0.000 | 1.88 | 2.853 | 1.537 |
| list | 1 | `alerta-4x4-safe-hot-nofwd` | 245.8 | 4.02 | 4.71 | 0.000 | 0.00 | 2.738 | 1.272 |
| list | 8 | `alrust` | 1332.5 | 5.89 | 10.18 | 0.000 | 3.00 | 1.146 | 1.480 |
| list | 8 | `alerta-4x4-nofwd` | 218.4 | 34.50 | 61.63 | 1.000 | 4.00 | 10.018 | 7.354 |
| list | 8 | `alerta-4x4-pgbu-nofwd` | 414.5 | 18.54 | 33.01 | 0.000 | 4.01 | 4.456 | 2.629 |
| list | 8 | `alerta-4x4-reuse-nofwd` | 493.7 | 11.92 | 40.47 | 0.000 | 2.01 | 4.461 | 2.572 |
| list | 8 | `alerta-4x4-safe-nofwd` | 522.2 | 14.77 | 30.09 | 0.000 | 2.01 | 3.886 | 2.216 |
| list | 8 | `alerta-4x4-safe-hot-nofwd` | 634.8 | 10.79 | 30.39 | 0.000 | 0.01 | 3.898 | 1.833 |
| count | 1 | `alrust` | 2670.1 | 0.37 | 0.49 | 0.000 | 2.00 | 0.123 | 0.259 |
| count | 1 | `alerta-4x4-nofwd` | 129.1 | 7.75 | 8.10 | 1.000 | 4.00 | 4.638 | 4.329 |
| count | 1 | `alerta-4x4-pgbu-nofwd` | 385.2 | 2.46 | 3.08 | 0.000 | 4.22 | 1.364 | 0.865 |
| count | 1 | `alerta-4x4-reuse-nofwd` | 479.4 | 2.06 | 2.66 | 0.000 | 2.07 | 1.217 | 0.770 |
| count | 1 | `alerta-4x4-safe-nofwd` | 603.5 | 1.64 | 2.16 | 0.000 | 2.06 | 0.925 | 0.639 |
| count | 1 | `alerta-4x4-safe-hot-nofwd` | 859.4 | 1.15 | 1.37 | 0.000 | 0.00 | 0.795 | 0.376 |
| count | 8 | `alrust` | 7356.5 | 1.06 | 1.97 | 0.000 | 1.99 | 0.119 | 0.333 |
| count | 8 | `alerta-4x4-nofwd` | 311.5 | 25.25 | 42.21 | 1.000 | 4.00 | 7.031 | 5.525 |
| count | 8 | `alerta-4x4-pgbu-nofwd` | 1000.3 | 7.47 | 11.97 | 0.000 | 4.00 | 1.698 | 1.210 |
| count | 8 | `alerta-4x4-reuse-nofwd` | 1333.2 | 5.85 | 10.26 | 0.000 | 2.01 | 1.535 | 1.136 |
| count | 8 | `alerta-4x4-safe-nofwd` | 1631.8 | 4.79 | 8.58 | 0.000 | 2.01 | 1.174 | 0.901 |
| count | 8 | `alerta-4x4-safe-hot-nofwd` | 2428.7 | 2.48 | 8.64 | 0.000 | 0.00 | 0.945 | 0.491 |
| write-dup | 1 | `alrust` | 1844.6 | 0.54 | 0.70 | 0.000 | 1.89 | 0.278 | 0.246 |
| write-dup | 1 | `alerta-4x4-nofwd` | 110.9 | 8.98 | 9.57 | 1.000 | 5.00 | 5.134 | 4.916 |
| write-dup | 1 | `alerta-4x4-pgbu-nofwd` | 280.4 | 3.42 | 4.33 | 0.000 | 4.68 | 2.018 | 1.061 |
| write-dup | 1 | `alerta-4x4-reuse-nofwd` | 349.5 | 2.86 | 3.56 | 0.000 | 2.86 | 1.744 | 0.902 |
| write-dup | 1 | `alerta-4x4-safe-nofwd` | 383.0 | 2.63 | 3.37 | 0.000 | 2.82 | 1.458 | 0.917 |
| write-dup | 1 | `alerta-4x4-safe-hot-nofwd` | 494.6 | 1.95 | 2.71 | 0.000 | 0.96 | 1.289 | 0.628 |
| write-dup | 8 | `alrust` | 2985.0 | 2.67 | 3.00 | 0.000 | 1.98 | 0.308 | 0.308 |
| write-dup | 8 | `alerta-4x4-nofwd` | 263.4 | 28.23 | 53.89 | 1.000 | 5.00 | 8.035 | 6.650 |
| write-dup | 8 | `alerta-4x4-pgbu-nofwd` | 714.5 | 10.62 | 16.29 | 0.000 | 4.97 | 2.630 | 1.487 |
| write-dup | 8 | `alerta-4x4-reuse-nofwd` | 915.6 | 8.63 | 15.20 | 0.000 | 2.99 | 2.481 | 1.401 |
| write-dup | 8 | `alerta-4x4-safe-nofwd` | 999.5 | 6.55 | 16.14 | 0.000 | 2.98 | 2.056 | 1.355 |
| write-dup | 8 | `alerta-4x4-safe-hot-nofwd` | 1260.8 | 6.30 | 10.94 | 0.000 | 1.00 | 1.840 | 0.983 |

Requests per second in each repetition, and the change against the base (`alerta-4x4-nofwd`) in
the same repetition. The base's own repetitions are given in its row.

| Case | Clients | Arm | Median requests/s | Change against the base, repetition 0 | Repetition 1 | Repetition 2 |
|---|---:|---|---:|---|---|---|
| count | 1 | `alrust` (reference) | 2670.1 | +1958.4% | +1973.2% | +1954.7% |
| count | 1 | `alerta-4x4-nofwd` (base; 129.7, 129.1, 127.1) | 129.1 | | | |
| count | 1 | `alerta-4x4-pgbu-nofwd` | 385.2 | +196.9% | +199.2% | +200.8% |
| count | 1 | `alerta-4x4-reuse-nofwd` | 479.4 | +269.0% | +271.5% | +278.5% |
| count | 1 | `alerta-4x4-safe-nofwd` | 603.5 | +365.3% | +364.2% | +375.0% |
| count | 1 | `alerta-4x4-safe-hot-nofwd` | 859.4 | +562.5% | +568.1% | +570.1% |
| count | 8 | `alrust` (reference) | 7356.5 | +2258.2% | +2288.1% | +2227.9% |
| count | 8 | `alerta-4x4-nofwd` (base; 312.0, 308.3, 311.5) | 311.5 | | | |
| count | 8 | `alerta-4x4-pgbu-nofwd` | 1000.3 | +241.2% | +224.4% | +218.7% |
| count | 8 | `alerta-4x4-reuse-nofwd` | 1333.2 | +331.2% | +332.4% | +312.1% |
| count | 8 | `alerta-4x4-safe-nofwd` | 1631.8 | +423.1% | +423.9% | +445.3% |
| count | 8 | `alerta-4x4-safe-hot-nofwd` | 2428.7 | +678.6% | +676.6% | +702.2% |
| list | 1 | `alrust` (reference) | 509.8 | +450.3% | +452.8% | +466.4% |
| list | 1 | `alerta-4x4-nofwd` (base; 92.6, 92.5, 88.3) | 92.5 | | | |
| list | 1 | `alerta-4x4-pgbu-nofwd` | 182.7 | +96.0% | +99.9% | +107.0% |
| list | 1 | `alerta-4x4-reuse-nofwd` | 195.8 | +111.3% | +107.5% | +124.6% |
| list | 1 | `alerta-4x4-safe-nofwd` | 219.6 | +138.8% | +137.5% | +148.1% |
| list | 1 | `alerta-4x4-safe-hot-nofwd` | 245.8 | +163.5% | +167.3% | +178.6% |
| list | 8 | `alrust` (reference) | 1332.5 | +487.5% | +516.6% | +512.6% |
| list | 8 | `alerta-4x4-nofwd` (base; 226.8, 218.4, 216.3) | 218.4 | | | |
| list | 8 | `alerta-4x4-pgbu-nofwd` | 414.5 | +82.8% | +97.4% | +85.0% |
| list | 8 | `alerta-4x4-reuse-nofwd` | 493.7 | +114.7% | +126.1% | +132.0% |
| list | 8 | `alerta-4x4-safe-nofwd` | 522.2 | +129.8% | +139.2% | +142.0% |
| list | 8 | `alerta-4x4-safe-hot-nofwd` | 634.8 | +184.3% | +176.8% | +193.5% |
| write-dup | 1 | `alrust` (reference) | 1844.6 | +1581.9% | +1567.9% | +1522.8% |
| write-dup | 1 | `alerta-4x4-nofwd` (base; 109.7, 110.9, 112.2) | 110.9 | | | |
| write-dup | 1 | `alerta-4x4-pgbu-nofwd` | 280.4 | +157.1% | +152.9% | +145.6% |
| write-dup | 1 | `alerta-4x4-reuse-nofwd` | 349.5 | +218.7% | +200.1% | +212.5% |
| write-dup | 1 | `alerta-4x4-safe-nofwd` | 383.0 | +251.9% | +245.5% | +231.3% |
| write-dup | 1 | `alerta-4x4-safe-hot-nofwd` | 494.6 | +347.8% | +346.1% | +341.7% |
| write-dup | 8 | `alrust` (reference) | 2985.0 | +1033.5% | +1064.3% | +1007.2% |
| write-dup | 8 | `alerta-4x4-nofwd` (base; 263.4, 259.1, 265.5) | 263.4 | | | |
| write-dup | 8 | `alerta-4x4-pgbu-nofwd` | 714.5 | +171.3% | +188.7% | +162.8% |
| write-dup | 8 | `alerta-4x4-reuse-nofwd` | 915.6 | +227.3% | +257.4% | +244.9% |
| write-dup | 8 | `alerta-4x4-safe-nofwd` | 999.5 | +243.9% | +285.7% | +278.2% |
| write-dup | 8 | `alerta-4x4-safe-hot-nofwd` | 1260.8 | +280.5% | +386.6% | +377.0% |

## What the run shows

- **`reuse` is the largest step of the add-on.** Against the base, its median count is 3.7 times
  higher at 1 client and 4.3 times at 8 clients (479.4 against 129.1, 1333.2 against 311.5). Its
  duplicate writes are 3.2 times higher at 1 client (349.5 against 110.9).
- **`safe` is a smaller step on top of `reuse`:** count at 8 clients 1333.2 to 1631.8; duplicate
  writes at 8 clients 915.6 to 999.5.
- **`hot` is a further step on top of `safe`:** count at 1 client 603.5 to 859.4; list at 8 clients
  522.2 to 634.8. Its commits per request fall to 0.00 for the reads, which is the one-write-a-second
  effect.
- **`pgbu-nofwd` gets part of the gain with no code change.** It is below `reuse` in all six pairs.
- **The base's gap to `alrust` remains** (reference only). Count at 8 clients: 311.5 against 7356.5.

## Limits

- One runner, three repetitions, 5 s warm-up and 15 s timed. The medians are of three values.
- 1000 seeded alerts. No large-table arm.
- The write case sends one fixed duplicate alert. It has no tags, so the tag rewrite that
  `forwarder` causes is not exercised by it.
- One site, loopback only. No Patroni, no replication, no network delay.
- `hot` is opt-in and best effort. Its counts are not for an installation that needs exact counts.
- The probes check the add-on's behaviour at one worker and one thread (connections) and at four
  workers (counts and stop). Several workers with a failover were not tested.

## Version note

This run used the add-on at blob `89bb052de2d846dab413ebfff3554bf6e6f71c75`. It predates the
opt-in `noecho` and `savedq` patches, which are not in this file. Two later changes to the default
patches came after it, in PR #283:

- `onecount` keeps its counts per request, instead of on Flask's `g` as in this file;
- `nosentry` leaves out only Alerta's own Sentry start, not more.

The benchmark's requests did not hold an application context over several requests, and the
harness sets no `SENTRY_DSN`. This run did not test the later `onecount` and `nosentry` behaviour, nor
requests that share an application context, nor a Sentry DSN.

## See also

- [The fastpath suite, 21 November 2026](2026-11-21-fastpath-bench.md), the run of the opt-in
  `noecho` patch and the index.
- [Saved searches under gunicorn, 22 November 2026](2026-11-22-savedq-load.md), the run of the
  opt-in `savedq` patch.
