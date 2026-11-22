# Saved searches under gunicorn: the 22 November run

One full run of `scripts/bench/savedq_load.py` (PR #282) on 22 November 2026. It measures what the
opt-in `savedq` patch of the add-on `scripts/bench/alerta_fast.py` (PR #281) changes for whole HTTP
requests to Alerta as released (no fork).

The numbers come from one hosted runner, with the load client on the same machine. They are
diagnostic and relative to that runner. They are not a capacity claim, and they say nothing about
a network between client and server or about an installation's own parser code.

## Answer in short

- **A search that comes back often gets much cheaper.** With one complex search sent again and
  again, `savedq` cut the server's CPU per request by 73% to 78%. Under a fixed load of 40
  requests a second, the p95 fell from 28.8 to 5.5 ms. Eight clients that send as fast as they can
  completed 697 requests a second in place of 218.
- **Sixteen searches in turn also gain:** CPU per request 47% to 53% lower; p95 at 40 a second
  6.4 to 4.4 ms; eight clients 972 requests a second in place of 551.
- **A new search text on every request costs a little:** CPU per request 2% to 3% higher, and
  eight clients completed 1.5% fewer requests a second.
- **Without a search text, nothing changes** beyond the noise of the run (a few percent either
  way).

`savedq` keeps only what Alerta's parser returned for a search text (the SQL condition and its
values). It never keeps results: every request still reads the database, with the caller's
customer scope. An installation that changes Alerta's search grammar must leave `savedq` out, or
call `alerta_fast.savedq_disable()` before the first request (the add-on's file header).

## A finding about Alerta's own parser

Alerta 9.1.0's PostgreSQL search parser keeps only the first two terms of a chain such as
`a AND b AND c`. The third term is not in the SQL, and no error is raised. Codex found this while
checking this run, and recorded the parser's output for the run's texts. At the pinned source
(`f4312727`), `queryparser.py` lines 23 to 26 store the first two operands of a binary operation,
and lines 43 and 44 write those two for `AND`. Examples of the stock output:

| Search text | What the stock SQL tests |
|---|---|
| `severity:(major OR critical) AND service:web AND NOT group:disk` | severity and service only |
| `(severity:(major OR critical) AND service:web) AND NOT group:disk` | all three |

With parentheses around the first two terms, all terms are kept. `SearchOr` uses the same two
operands (lines 56 and 57), so `a OR b OR c` should lose `c` too; this is read from the source and
was not run. This is Alerta's behaviour with or without the add-on; `savedq` gives back exactly
what the parser gave.

## How this report was made

- **Run:** GitHub Actions run 45054378079, attempt 1, workflow `addon savedq load`, started by hand
  (`mode=full`) on branch `claude/alerta-fast-savedq-load` at
  `1203a8af60a663b9c4dc089b3df99cb1fc1fb279`. Artifact `addon-savedq-load`, 8366 bytes, SHA-256
  `ccfe778ae00d14e91a8f421f060265f68b4d8058de2502b18314681f174057d4` as GitHub reports it. The
  reviewer agreed to the run before it started, and later checked its result independently.
- **Machine:** one GitHub-hosted runner, 4 vCPUs, image `ubuntu-24.04 20261116.133.1`. Servers,
  PostgreSQL 18.7 and the client share it, over loopback.
- **Software:** Alerta 9.1.0 from the pinned `reference/alerta` submodule, gunicorn 27.0.1, 4
  workers of 4 threads, direct PostgreSQL connections. Add-on `alerta_fast.py` with SHA-256
  `306d3846…` (as in PR #281).
- **Arms:** `safe` (the add-on's default patches) and `safe,savedq`. Both servers run for the whole
  run on one database; only one gets requests at a time, so each worker's store keeps what earlier
  cells left.
- **Data:** 300 alerts: 150 of customer A, 100 of customer B and 50 of none. Three keys
  (customer A, customer B, admin) take turns. Nothing writes during the timed part.
- **Requests:** `GET /alerts`, sorted by resource, 50 a page.
- **Loads:** each with eight client threads. *Offered*: 40 requests a second at fixed times; a
  request's time is counted from when it was due. *Closed*: each thread sends its next request as
  soon as it has checked the last answer. Closed requests a second are those of this client, which
  checks every answer, on the same machine.
- **Order:** 4 repetitions; the arm order is balanced (each arm first as often as second in each
  shape). Each cell has 3 s of warm-up and 10 s of timed load.
- **Results file:** [2026-11-22-savedq-load-results.json](2026-11-22-savedq-load-results.json), the
  run's `results.json`, unchanged. The artifact (with the server logs) is kept by GitHub for 7 days;
  the results file here stays.

### The four shapes of search

| Shape | Search text | What the stock SQL tests |
|---|---|---|
| `one` | `severity:(major OR critical) AND service:web AND NOT group:disk`, every time | severity and service (the third term is dropped, see above) |
| `sixteen` | `severity:<one of 4> AND group:<one of 4>`, 16 texts in turn | both terms |
| `unique` | `severity:(major OR minor) AND NOT service:mail AND NOT resource:zz<run>n<number>`, a new number every time | severity and service only, so the same SQL and values every time |
| `none` | no `q` | (control) |

So `unique` is a new key for the store on every request, with the same SQL each time. It measures
the cost of a miss (parse, and store an entry that is not used again). It is not a load with
different results.

## Checks

- 64 of 64 cells valid: in every cell, warm-up and timed, every request taken was answered and
  checked, and no client thread stopped. 171,275 timed and 51,072 warm-up answers.
- Every offered cell sent 400 of its 400 due requests; none was sent more than 100 ms late (the
  most was 0.38 ms).
- Every answer was HTTP 200 and equal to the answer of Alerta without the add-on for that key and
  search: page, page size, number of pages, `more`, total, both counts and every alert in order with
  all its fields, the link included.
- The expected answers were checked against the seed itself: each customer key saw only its own
  alerts, and with no search its first 50.
- 24,549 `unique` texts were sent. 22 of them were asked again, for each key, of Alerta without the
  add-on after the timed part: 0 differences.

## Results

Medians of the four repetitions. CPU is the gunicorn master and workers, from `/proc`.

| Shape | Load | Arm | Requests/s | p50 ms | p95 ms | p99 ms | Server CPU ms per request |
|---|---|---|---:|---:|---:|---:|---:|
| `one` | offered | `safe` | 40.1 | 10.98 | 28.78 | 35.13 | 10.29 |
| `one` | offered | `safe,savedq` | 40.1 | 4.19 | 5.51 | 5.95 | 2.76 |
| `one` | closed | `safe` | 218.4 | 32.25 | 66.59 | 83.45 | 14.18 |
| `one` | closed | `safe,savedq` | 696.7 | 10.18 | 16.71 | 20.31 | 3.13 |
| `sixteen` | offered | `safe` | 40.1 | 5.20 | 6.35 | 7.21 | 3.84 |
| `sixteen` | offered | `safe,savedq` | 40.1 | 3.50 | 4.45 | 5.65 | 2.09 |
| `sixteen` | closed | `safe` | 550.8 | 12.57 | 26.38 | 39.63 | 4.69 |
| `sixteen` | closed | `safe,savedq` | 972.0 | 7.68 | 11.85 | 13.63 | 2.19 |
| `unique` | offered | `safe` | 40.1 | 11.66 | 30.86 | 36.34 | 11.16 |
| `unique` | offered | `safe,savedq` | 40.1 | 11.92 | 31.59 | 39.49 | 11.41 |
| `unique` | closed | `safe` | 196.9 | 36.15 | 72.35 | 89.19 | 15.64 |
| `unique` | closed | `safe,savedq` | 195.1 | 36.65 | 72.19 | 88.92 | 16.05 |
| `none` | offered | `safe` | 40.1 | 4.75 | 5.56 | 5.90 | 3.33 |
| `none` | offered | `safe,savedq` | 40.1 | 4.62 | 5.34 | 5.82 | 3.24 |
| `none` | closed | `safe` | 574.8 | 11.96 | 21.01 | 25.45 | 4.09 |
| `none` | closed | `safe,savedq` | 569.1 | 11.85 | 22.37 | 27.34 | 4.07 |

Change with `savedq` against `safe` in the same repetition: the median of the four, and the range.

| Shape | Offered: CPU per request | Offered: p95 | Closed: requests/s | Closed: CPU per request |
|---|---|---|---|---|
| `one` | -73.2% (-74.3 to -72.4) | -81.2% (-81.6 to -80.7) | +223.1% (+214.0 to +254.0) | -78.0% (-78.1 to -75.9) |
| `sixteen` | -46.9% (-50.0 to -45.5) | -30.5% (-35.4 to -27.7) | +76.0% (+63.5 to +85.7) | -53.4% (-54.0 to -51.7) |
| `unique` | +2.2% (-1.5 to +4.2) | +2.4% (+1.3 to +7.5) | -1.5% (-1.9 to +0.2) | +2.9% (+1.6 to +6.5) |
| `none` | -3.4% (-5.3 to 0.0) | -4.5% (-7.6 to -2.4) | -0.1% (-4.5 to +1.5) | -0.1% (-1.9 to +0.4) |

`none` has no search text, so `savedq` takes no part in it. Its few percent show the noise of this
run.

With a few hundred to a few thousand requests per cell, the p99 rests on few requests.

## What this means for an operator

- `savedq` is a good fit where the same searches come back often, as dashboards and saved views
  do. With the search text `one`, the server's CPU per request fell by about 7.5 ms: that is the
  parse, which `savedq` does once per worker and text.
- Where most searches are new (ad-hoc text), `savedq` gives nothing and costs a few percent of CPU.
- It is opt-in (`ALERTA_FAST=safe,savedq`), each worker keeps at most 256 searches by default
  (`ALERTA_FAST_SAVEDQ_ENTRIES`), and it does not start unless Alerta's parser file is the one of
  Alerta 9.1.0. Codex measured about 0.5 to 1 KB of Python objects per kept search for the texts of
  this run, so about 130 to 240 KB per worker when the store is full.
- Group long searches with parentheses, with or without the add-on: Alerta drops the third term
  of an ungrouped `AND` chain (see above).

## Not in this run

A network between client and server, more workers or more alerts, writes during the timed part, a
load with many different results, and the store's own numbers (they are in each worker's memory).
