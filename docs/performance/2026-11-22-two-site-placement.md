# Alerta at two sites: where requests go, and `noecho`

One full run of the two-site lab (`scripts/bench/dual-site/placement.py`) on 22 November 2026.
It answers two questions for Alerta as released (no fork). The setup has one PostgreSQL primary
at site A and an asynchronous standby at site B:

1. Must a site B user's request go to the Alerta at the primary's site, or can it go to the
   Alerta at site B?
2. What does the add-on's opt-in `noecho` patch change on that route?

The numbers come from one hosted runner and emulated links between the sites. They are
diagnostic and relative to that runner. They are not a capacity claim. They say nothing about
failover, a real network or synchronous replication.

## Answer in short

- **Send every request to an Alerta at the primary's site.** At site B, with a 50 ms link between
  the sites, a dashboard read took about 355 ms less this way, and a write about 910 ms less.
  This was so in all three blocks. Site A did not change.
- **`noecho` reduces write work, not response time.** Updates of alert rows fell from 116 to 36
  per run (placement stage) and from 92 to 20 (fresh stage). In the runs that can be compared,
  the primary wrote 6% to 14% fewer WAL bytes. The median time of a site B duplicate write was
  1.2 to 2.4 ms longer with `noecho`, in all five blocks. No PostgreSQL CPU saving was seen at
  this load.

## The three arms

| Arm | Route of a site B user's request | Add-on (`ALERTA_FAST`) |
|---|---|---|
| **L** | site B's Alerta, which reaches the primary at site A over the SQL link | `safe` |
| **P** | site B's HAProxy sends the request to an Alerta at the current primary's site, over the HTTP link | `safe` |
| **N** | as P | `safe,noecho` |

L and P differ only in the route; P and N differ only in `noecho`. Every arm uses Alerta's
default plugin list, `forwarder` included, with no forwarder destination. Each data site has
HAProxy, PgBouncer (transaction pooling, pool size 8) and two Alerta processes. Site C has only
the third etcd member. The add-on's file header describes `safe` and `noecho`. The fastpath
report of 21 November ([2026-11-21-fastpath-bench.md](2026-11-21-fastpath-bench.md)) measures
`noecho` at one site.

## How this report was made

- **Run:** GitHub Actions run 45037764779, attempt 1, workflow `bench two-site`, started by hand
  (`mode=full`) on branch `claude/alerta-two-site-placement` at
  `0fc6497737890eb16cd42358636a92da09491ab7`. That commit was merged to `main` afterwards
  without a change (PR #280). Artifact `bench-two-site-45037764779-1`, 140742 bytes, SHA-256
  `28073f0cd704464fa02a294459f4243c4d7f136a68ddec3cd38d67406d664991` as GitHub reports it.
  The run was agreed by the reviewer before it started, and the reviewer checked its result
  on its own afterwards.
- **Machine:** one GitHub-hosted runner, 4 vCPUs, about 16 GB of memory, image
  `ubuntu-24.04 20261116.133.1`. Every process of the lab runs on it. The emulated links are on
  loopback.
- **Links:** set round trips 50 ms (SQL, site to site), 100 ms (WAL stream), 30 ms (etcd), and
  50 ms for HTTP between the sites; jitter 4 ms, loss 0.1%, HTTP rate 4 Mbit/s. Measured
  medians per block: SQL 48.8 to 50.8 ms, WAL 98.0 to 102.1 ms, etcd 25.9 to 32.3 ms, HTTP 50.0
  to 51.6 ms.
- **Software:** PostgreSQL 18.7 (PGDG package), Patroni 4.1.5, etcd 3.6.15, HAProxy 3.4.7,
  PgBouncer 1.26.1. Alerta 9.1.0 from the pinned `reference/alerta` submodule (`f4312727`),
  under gunicorn. Add-on `scripts/bench/alerta_fast.py`, git blob `c858e280`.
- **Load:** 4 tenants, 30 alerts each. Each block gives each arm a new copy of the database and
  the same requests at the same times. In 50 s that is 160 dashboard reads (`GET /alerts` and
  `GET /alerts/count`, in turn) and 40 writes, half from each site. Placement stage: 36
  duplicates of stored alerts and 4 new alerts. Fresh stage: 20 new alerts, 12 duplicates and
  8 changes. Requests are sent at fixed times: a late answer does not delay the next request.
- **Order:** placement blocks L/P/N, P/N/L and N/L/P; fresh blocks P/N and N/P.
- **Times:** from the planned send time to the end of the answer.
- **Results file:** [2026-11-22-two-site-placement-results.json](2026-11-22-two-site-placement-results.json),
  the run's `results.json`, unchanged. The per-request samples are in the artifact only.

## Checks

- Preflight passed: `noecho`'s differential check agreed in 38 of 38 cases. One new alert and 20
  duplicates made 62 row updates of `alerts` for Alerta as released and for `safe`, and 20 for
  `safe,noecho`. An attribute PUT with the same value, a new value and null, and an `ack`,
  gave the same status and stored result in all three.
- 5 blocks, none rejected; 13 of 13 arm runs valid; 3,016 requests; no transport error; no CPU
  throttling.
- Every read agreed with a state of its tenant's committed alerts, apart from the eight reads in
  the next item, which the lab accepts as Alerta's own behavior. Every written alert was stored
  as planned, and no other alert was. In every block, all arms ended with the same alerts. In all
  13 runs, the standby replayed to the primary's flush position and held the same alerts.
- Eight site B list reads in L (3, 2 and 3 in the three blocks) each showed neither the old nor
  the new state of one alert that was written while the read ran. Alerta's list has a time
  cut-off (alerts received up to the query time). These alerts were received before the cut-off
  but committed after the read had started. In L a site B write takes about a second, so this
  window is wide. P and N had none.
- The output was searched for the lab's 11 generated credentials before it was published: 90
  files, none found.

## Placement: site B

Median / p95 in ms, one row per block and arm. Creates are few (number in brackets), so their
p95 is close to the maximum.

| Block | Order | Arm | List | Count | Duplicate | Create |
|---:|---|---|---:|---:|---:|---:|
| 0 | L/P/N | L | 464.0 / 577.2 | 413.7 / 425.5 | 976.9 / 1230.1 | 1002.3 / 1197.7 (2) |
| 0 | L/P/N | P | 104.4 / 157.5 | 56.4 / 61.0 | 61.3 / 66.4 | 63.8 / 68.2 (2) |
| 0 | L/P/N | N | 102.2 / 108.9 | 55.9 / 63.9 | 62.5 / 325.1 | 62.4 / 67.1 (2) |
| 1 | P/N/L | L | 460.5 / 484.4 | 410.2 / 423.4 | 972.9 / 1025.3 | 973.6 / 1000.2 (3) |
| 1 | P/N/L | P | 103.4 / 157.9 | 56.5 / 63.9 | 62.8 / 67.8 | 63.4 / 66.0 (3) |
| 1 | P/N/L | N | 103.3 / 110.7 | 56.2 / 61.2 | 64.6 / 71.3 | 65.1 / 65.1 (3) |
| 2 | N/L/P | L | 459.3 / 477.8 | 409.8 / 426.4 | 971.1 / 1233.7 | 975.0 / 975.0 (1) |
| 2 | N/L/P | P | 103.1 / 108.4 | 56.1 / 61.1 | 62.5 / 69.0 | 56.7 / 56.7 (1) |
| 2 | N/L/P | N | 103.8 / 160.7 | 57.3 / 61.5 | 63.9 / 68.4 | 68.1 / 68.1 (1) |

P against L in the same block, site B medians: list 356 to 360 ms less, count 354 to 357 ms
less, duplicate 909 to 916 ms less, create 910 to 939 ms less. A site B count in L took about
8 link round trips of 50 ms (410 to 414 ms), and a duplicate write about 19 (971 to 977 ms). In
P the request crosses the link once (about 56 ms for a count). This is an inference from the
medians; the lab does not count round trips.

The p95 of the site B list was 108 to 161 ms in P and N; with 40 reads, a p95 rests on the two
slowest. The p95 of the 18 duplicates in N in block 0 (325 ms) is the slowest of them.

Site A, medians over the three blocks: list 6.2 to 6.7 ms, count 4.4 to 4.8 ms, duplicate 7.6
to 8.0 ms, create 6.6 to 8.3 ms, in all three arms. The route of site B's requests does not
change site A.

## Fresh ingest: site B, P and N

Median / p95 in ms (number of requests in brackets for the writes).

| Block | Order | Arm | List | Count | Create | Duplicate | Change |
|---:|---|---|---:|---:|---:|---:|---:|
| 0 | P/N | P | 102.4 / 108.4 | 57.0 / 62.1 | 64.1 / 112.1 (9) | 62.3 / 69.1 (7) | 56.2 / 60.9 (4) |
| 0 | P/N | N | 103.8 / 108.4 | 57.5 / 60.9 | 65.3 / 111.0 (9) | 64.7 / 120.5 (7) | 64.7 / 109.6 (4) |
| 1 | N/P | P | 102.5 / 107.5 | 55.8 / 61.8 | 62.9 / 68.4 (9) | 61.8 / 63.6 (6) | 58.8 / 63.1 (5) |
| 1 | N/P | N | 102.0 / 107.7 | 55.5 / 62.3 | 62.3 / 67.2 (9) | 63.3 / 66.2 (6) | 58.5 / 62.7 (5) |

With 4 to 9 writes of each kind per arm, these are descriptions, not a measured difference.

## Work done per arm run

| Stage | Block | Arm | Alert row updates | Primary WAL bytes | PostgreSQL CPU ms | Alerta CPU ms, site A / B |
|---|---:|---|---:|---:|---:|---|
| placement | 0 | L | 116 | 441,232 | 640 | 540 / 670 |
| placement | 0 | P | 116 | 316,656 | 480 | 810 / 290 |
| placement | 0 | N | 36 | 74,560 | 480 | 820 / 270 |
| placement | 1 | L | 116 | 86,888 | 620 | 560 / 640 |
| placement | 1 | P | 116 | 86,376 | 480 | 820 / 270 |
| placement | 1 | N | 36 | 74,392 | 520 | 850 / 280 |
| placement | 2 | L | 116 | 86,760 | 590 | 570 / 640 |
| placement | 2 | P | 116 | 86,424 | 470 | 810 / 280 |
| placement | 2 | N | 36 | 81,232 | 510 | 810 / 280 |
| fresh | 0 | P | 92 | 96,656 | 450 | 790 / 290 |
| fresh | 0 | N | 20 | 85,208 | 470 | 780 / 260 |
| fresh | 1 | P | 92 | 96,528 | 440 | 740 / 260 |
| fresh | 1 | N | 20 | 85,200 | 460 | 750 / 250 |

- The WAL of block 0 is not comparable: the first two runs of the run wrote much more. The cause
  is not known; one possible cause is full-page images after the database copy's checkpoint.
  In blocks 1 and 2 and in the fresh stage, N wrote 6% to 14% fewer WAL bytes than P.
- CPU is read in 10 ms steps over about 50 s; differences of a few tens of ms are noise.
- In L, site B's Alerta does site B's work, so the Alerta CPU is split between the sites. In P
  and N, site A's Alerta does almost all of it.

## What this means for an operator

- With Patroni at two sites, route Alerta's HTTP requests to the site of the current primary,
  for reads and writes. In this lab, HAProxy at each site follows Patroni's `/primary` health
  check (`scripts/bench/dual-site/haproxy-affinity.cfg`). A user then pays for one HTTP round trip
  between the sites, instead of a round trip for each SQL statement of the request.
- `noecho` is worth it where write load on the primary matters: fewer row updates, less WAL and
  less for the standby to replay. It does not make requests faster at this load, and it adds
  1 to 2 ms to a duplicate write. At one site and higher load, the fastpath report found more
  requests per second with `noecho`.

## Not in this run

Failover and recovery, the cache stage, a larger table, more load, real networks, synchronous
replication. Each needs its own run.
