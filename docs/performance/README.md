# Making Alerta faster: what we measured

This folder holds the measured reports of the project. Most of them answer one question: **how
fast can Alerta 9.1.0 as released (no fork) be made on PostgreSQL**, at one site and at two data
sites with a witness, using configuration, PostgreSQL itself and one add-on file. This page
collects what the reports found, in one place, with pictures. Each finding links to its report,
where the method, the checks and the raw results are.

**Read every number with its conditions.** The three charted reports below used GitHub-hosted
runners (4 vCPUs) with synthetic alerts, and the two-site run used emulated links between the
sites on one machine. Some reports in the index were made on other machines (a laptop); each
report names its machine.
The numbers are diagnostic and relative to those runs. They are not a capacity claim for your
installation.

- **Operator steps, in short:** [Making stock Alerta faster](alerta-operator-guide.md) says
  what to change. This page says what was found and why.
- **The add-on:** [`scripts/bench/alerta_fast.py`](../../scripts/bench/alerta_fast.py)
  replaces a few of Alerta's functions when gunicorn starts. Its default (`safe`) keeps every
  write of Alerta as it is. The other patches are opt-in. Its file header lists, for each patch,
  what changes.
- **Alrust compared with Alerta:** see [the end of this page](#alrust-compared-with-alerta).
- **Your own installation:** [Measure your own Alerta, safely](measure-your-own-alerta.md) has
  a read-only checkup that shows which findings may apply to you.

## Start here, if you are new

[Alerta](https://github.com/alerta/alerta) is an open-source server that collects alerts from
monitoring systems such as Prometheus or Zabbix. It stores them in a database (PostgreSQL in
all of these tests) and shows them in a web console. This project did not change Alerta. It
measured Alerta 9.1.0 as released, found where a request spends its time, and tested changes
that an operator can make without a fork.

A request to Alerta is one of two kinds:

- **A write:** a monitoring system sends an alert. Most of the write tests here send a
  *duplicate*, because monitoring systems often send the same alert again while the problem is
  still open.
- **A read:** a person or a dashboard asks for a list of alerts or for a count.

The findings are in three groups, and each has a section below:

1. **Where requests go** when there are two sites and one database primary (①).
2. **What a duplicate write costs** in PostgreSQL, and four ways to make it cheaper (②).
3. **What a search costs** in the server's CPU (③).

**Words used on this page**

| Word | Meaning |
|---|---|
| Duplicate | An alert that Alerta already has, with the same resource, event, environment and severity. Alerta updates the stored alert (count and last receive time); it does not make a new one |
| Stock Alerta | Alerta 9.1.0 as released, with its default settings and plugins |
| Plugin | Code that Alerta runs for each alert that it receives. `PLUGINS` in Alerta's configuration lists the active ones |
| `forwarder` | One of Alerta's default plugins. It sends alerts to another Alerta. It is active even when no destination is set |
| The add-on | One file of this project, [`alerta_fast.py`](../../scripts/bench/alerta_fast.py). It replaces a few of Alerta's functions when the server starts. Alerta's own files stay as released |
| `safe`, `noecho`, `hot`, `savedq` | Names of the add-on's patches, selected with the environment variable `ALERTA_FAST`. `safe` is the default set and keeps every write of Alerta as it is. The others are opt-in |
| Primary | The one PostgreSQL server that accepts writes. With two sites, one site has the primary and the other has a copy (a standby) |
| Patroni, etcd, HAProxy | Patroni decides which PostgreSQL server is the primary and uses etcd to agree on it. HAProxy sends each request to the right server |
| PgBouncer | A connection pooler: it keeps connections to PostgreSQL open, so that each request does not open a new one |
| WAL | PostgreSQL's write-ahead log. Every change is written there first, and standbys receive it. Less WAL means less to write and to send |
| Index | A structure that lets PostgreSQL find rows without reading the whole table |
| p50 | The median time: half of the requests were faster |
| Client | One sender in the load tool. "8 clients" means 8 requests in progress at a time |

## At a glance

| If you run… | Do this | Measured change | Where it does not help | Report |
|---|---|---|---|---|
| Two data sites, one PostgreSQL primary | Send each site's HTTP requests to the Alerta at the **primary's** site (HAProxy checks Patroni's role) | Site B, 50 ms link: list 459–464 → 103–104 ms; duplicate write 971–977 → 61–63 ms | Failover, fallback and recovery were not tested | [two-site placement](2026-11-22-two-site-placement.md) |
| Alerta's default `PLUGINS`, and you do not forward alerts | Remove `forwarder` from `PLUGINS` | +39 to +45% duplicate writes per second (8 clients, with the add-on's `safe` patches); +8 to +11% on stock Alerta | Nothing, if you need forwarding | [fastpath](2026-11-21-fastpath-bench.md), [tuning](2026-11-20-tuning-bench.md) |
| You need `forwarder` | `ALERTA_FAST=safe,noecho` | +21 to +31% duplicate writes per second (8 clients); 6 to 14% less WAL at two sites | Site B's duplicate write was 1.2 to 2.4 ms slower at low load; alerts whose tags change on every receive | [fastpath](2026-11-21-fastpath-bench.md), [two-site](2026-11-22-two-site-placement.md) |
| Default `PLUGINS` with `forwarder`, and a large `alerts` table | Hypothesis to test on a copy first: `CREATE INDEX alerts_id_pattern ON alerts (id text_pattern_ops)` | 100,000 synthetic alerts, a `C.UTF-8` database: duplicate writes +193 to +282% per second on stock Alerta, +379 to +520% with `safe` | At 1,000 alerts: 3 to 5% at 1 client, nothing at 8. Building the index on a live table was not measured. A database with collation `C` may not need it | [index, 100,000 alerts](2026-11-25-pgtune-index-bench.md) |
| Dashboards and saved views that repeat searches | `ALERTA_FAST=safe,savedq` | Server CPU per request −73% (one recurring search), −47% (16 in turn) | +2 to 3% CPU when every search text is new | [savedq](2026-11-22-savedq-load.md) |
| Searches such as `a AND b AND c` | Group them: `(a AND b) AND c` | Alerta's stock parser keeps only the first two terms of an ungrouped chain | — (a correctness finding, not a speed-up) | [savedq](2026-11-22-savedq-load.md#a-finding-about-alertas-own-parser) |

## The lab, as measured

Each number on the map is one of the findings below.

```mermaid
flowchart LR
  subgraph A["Site A: PostgreSQL primary"]
    UA(["Users at site A"]) --> HA["HAProxy A"]
    HA --> AA["Alerta 9.1.0 + alerta_fast<br/>gunicorn, PgBouncer<br/>② writes · ③ searches"]
    AA --> DBA[("PostgreSQL primary<br/>Patroni · etcd a")]
  end
  subgraph B["Site B: standby"]
    UB(["Users at site B"]) --> HB["HAProxy B"]
    AB["Alerta 9.1.0 + alerta_fast"]
    DBB[("PostgreSQL standby<br/>Patroni · etcd b")]
  end
  subgraph C["Site C: witness"]
    EC[("etcd c")]
  end
  HB == "① P: HTTP to the primary's site" ==> AA
  HB -. "L: site B's own Alerta" .-> AB
  AB -. "every SQL statement crosses the link" .-> DBA
  DBA -- "WAL stream" --> DBB
```

In the two-site lab, each data site has HAProxy, PgBouncer in transaction mode and two Alerta
processes with at least the add-on's `safe` patches (among them, one kept PostgreSQL connection
per worker thread). The runs of ② and ③ used one site on one runner. ④, the lab's own limits,
is at the end.

## ① Two sites: send requests to the primary's site

**The cost.** When site B's Alerta serves site B's users, it reaches the PostgreSQL primary at
site A across the link for its SQL. When site B's HAProxy sends the HTTP request to the Alerta at
site A instead, the request crosses the link and its SQL runs at site A.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/two-site-placement-dark.svg">
  <img alt="Site B median time per request with a 50 ms link. Site B's own Alerta: list 459 to 464 ms, count 410 to 414 ms, duplicate write 971 to 977 ms. Alerta at the primary's site: list 103 to 104 ms, count 56 ms, duplicate write 61 to 63 ms." src="assets/two-site-placement-light.svg" width="860">
</picture>

| Site B request (median, range of three blocks) | L: site B's own Alerta | P: Alerta at the primary's site |
|---|---:|---:|
| Alert list | 459 to 464 ms | 103 to 104 ms |
| Alert count | 410 to 414 ms | 56 ms |
| Duplicate alert (write) | 971 to 977 ms | 61 to 63 ms |

In L, a site B count took about 8 times and a duplicate write about 19 times the link's 50 ms
round trip. Site A's times did not change with site B's route. The lab's HAProxy configuration
([`haproxy-affinity.cfg`](../../scripts/bench/dual-site/haproxy-affinity.cfg)) checks
Patroni's primary role and each Alerta's readiness, and falls back to the other site's Alerta.

**Not tested:** promotion, fallback, recovery, a real network, synchronous replication.
Report: [2026-11-22-two-site-placement.md](2026-11-22-two-site-placement.md).

## ② Duplicate alerts: two of three writes are an echo

**The cost.** Monitoring systems often send an alert again while it is still open, and Alerta
stores it as a duplicate. With Alerta's default `PLUGINS`, the `forwarder` plugin returns the alert from `post_receive`, even
with no destination. Alerta then writes the alert's tags and its attributes again: two more
`UPDATE` statements, each with its own commit. Each has `WHERE id=… OR id LIKE …`. On the test
runners (database collation `C.UTF-8`), plan probes of that predicate got a sequential scan,
that is, a read of the whole table. The plans of Alerta's own statements were not captured;
the results below fit that explanation.

```mermaid
sequenceDiagram
  autonumber
  participant M as Monitoring
  participant A as Alerta 9.1.0
  participant P as PostgreSQL
  M->>A: POST /alert (a duplicate)
  A->>P: UPDATE alerts … (duplicate count, last receive time)
  A->>P: COMMIT
  Note over A: forwarder's post_receive returns the alert
  A->>P: UPDATE alerts SET tags … WHERE id=… OR id LIKE …
  A->>P: COMMIT
  A->>P: UPDATE alerts SET attributes … WHERE id=… OR id LIKE …
  A->>P: COMMIT
  A-->>M: 201
  Note over A,P: noecho reads the row by its primary key first,<br/>and leaves out each UPDATE that would change nothing
```

The diagram leaves out the other statements of a request (the duplicate check, the API key's
use count, the timers).

With one new alert and 20 duplicates, Alerta made 62 row updates of `alerts` with the default
plugins, and 20 with `noecho`.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/duplicate-writes-dark.svg">
  <img alt="Duplicate writes per second at one site, median of three repetitions. 8 clients: default plugins 738, forwarder removed 1048, noecho 931, noecho and hot 1108, id prefix index 731. 1 client: default 309, forwarder removed 394, noecho 341, noecho and hot 415, id prefix index 319." src="assets/duplicate-writes-light.svg" width="860">
</picture>

| Arm (every arm has the `safe` patches) | 1 client, writes/s | 8 clients, writes/s | Change at 8 clients, each repetition |
|---|---:|---:|---|
| Default plugins (baseline) | 309.3 | 737.7 | |
| `forwarder` removed | 393.7 | 1047.6 | +44.9%, +38.8%, +42.0% |
| `noecho` | 341.4 | 930.9 | +27.8%, +30.5%, +21.4% |
| `noecho` and `hot` | 415.1 | 1108.4 | +25.5%, +49.2%, +50.6% |
| Index `alerts (id text_pattern_ops)` | 319.3 | 731.0 | +0.4%, −0.0%, −3.1% |

What this says:

- **If you do not forward alerts, remove `forwarder`.** It is the largest gain from one change,
  and it needs no code.
- **`noecho` gets part of the way.** The two reads and the two commits stay, so it does not reach
  `forwarder` removed. PostgreSQL CPU per request fell from 2.155 to 1.577 ms at 8 clients. At two
  sites, `noecho` cut alert-row updates from 116 to 36 per run and the primary's WAL by 6 to
  14%, but did not make requests faster at that low load.
- **`hot`** writes the API key's use count and the timers about once a second in place of on
  every request. Use it only where those counts may be approximate.
- **The index** had little effect on 1,000 alerts, where a full scan is cheap. On 100,000
  alerts its effect was large: see the next part.
- **Before you turn on `noecho`,** read the add-on's file header: the stored tag order, a
  change that another request commits at the same time, alerts whose id begins with this id, and
  your own UPDATE triggers on `alerts`.

Reports: [2026-11-21-fastpath-bench.md](2026-11-21-fastpath-bench.md),
[2026-11-22-two-site-placement.md](2026-11-22-two-site-placement.md).

### The same index on 100,000 alerts

One full run on 25 November measured the index alone,
`CREATE INDEX alerts_id_pattern ON alerts (id text_pattern_ops)`, on a table of 100,000
synthetic alerts, with Alerta's default plugins (so with `forwarder`). Each arm ran with and
without the index, in three repetitions. The table shows the medians.

| Arm | Clients | Without the index, writes/s | With the index, writes/s | p50 without | p50 with |
|---|---:|---:|---:|---:|---:|
| Stock Alerta | 1 | 35.1 | 126.2 | 29.74 ms | 8.26 ms |
| Stock Alerta | 8 | 99.4 | 311.8 | 77.45 ms | 25.38 ms |
| Add-on, `safe` | 1 | 44.7 | 243.4 | 20.64 ms | 3.91 ms |
| Add-on, `safe` | 8 | 117.7 | 601.1 | 65.01 ms | 13.21 ms |

- **Duplicate writes:** +193% to +282% per second on stock Alerta and +379% to +520% with
  `safe`, in each of the 12 pairs, whichever member ran first.
- **PostgreSQL CPU per duplicate** fell from about 19 to 22 ms to about 1.4 to 2.3 ms.
- **Reads** (alert list and count) showed no clear change: −6.7% to +8.2%, in both directions.
- **Without the index, the table size sets the rate:** `safe` wrote 737.7 duplicates per
  second at 8 clients on 1,000 alerts in the 21 November run, and 117.7 here on 100,000. Those
  runs differ in more than the table (PgBouncer is in the path here), so this is context, not
  a measured comparison.

**Limits.** One hosted runner; synthetic alerts; a database with collation `C.UTF-8`. The cost
of building the index on a live table was not measured (`CREATE INDEX` blocks writes while it
runs; `CREATE INDEX CONCURRENTLY` does not, and takes longer), and neither was the index's
size. A database with collation `C` may already answer the predicate from the primary key.
Removing `forwarder`, or `noecho`, takes away the two statements themselves; this run did not
compare the index with either. Test it on a copy of your data first:
[Measure your own Alerta, safely](measure-your-own-alerta.md).

Report: [2026-11-25-pgtune-index-bench.md](2026-11-25-pgtune-index-bench.md).

## ③ Searches: keep the parse, and group your terms

**The cost.** Alerta parses the `q=` search text of every request into SQL, again for each
request. The opt-in `savedq` patch keeps, in
each worker, the SQL condition and values that Alerta's parser made for its most recent search
texts (256 by default). It never keeps results: every request still reads the database with its
caller's customer scope. With one recurring complex search, the server's CPU per request fell by
about 7.5 ms. Less parsing agrees with that; the run did not time the parser on its own.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/savedq-cpu-dark.svg">
  <img alt="Server CPU per request at 40 requests a second. One recurring search: 10.29 ms without savedq, 2.76 ms with it. Sixteen searches in turn: 3.84 and 2.09 ms. A new search every time: 11.16 and 11.41 ms. No search: 3.33 and 3.24 ms." src="assets/savedq-cpu-light.svg" width="860">
</picture>

| Search shape | CPU per request, 40 requests/s | p95, 40 requests/s | Requests/s, 8 clients at full speed |
|---|---|---|---|
| One search, again and again | 10.29 → 2.76 ms | 28.78 → 5.51 ms | 218.4 → 696.7 |
| Sixteen searches in turn | 3.84 → 2.09 ms | 6.35 → 4.45 ms | 550.8 → 972.0 |
| A new search text every time | 11.16 → 11.41 ms | 30.86 → 31.59 ms | 196.9 → 195.1 |
| No search (control) | 3.33 → 3.24 ms | 5.56 → 5.34 ms | 574.8 → 569.1 |

**A finding about Alerta itself.** Alerta 9.1.0's PostgreSQL search parser keeps only the first
two terms of an ungrouped chain, and shows no error:

| Search text | What the stock SQL tests |
|---|---|
| `severity:(major OR critical) AND service:web AND NOT group:disk` | severity and service only |
| `(severity:(major OR critical) AND service:web) AND NOT group:disk` | all three |

Put parentheses around the first two terms. This happens with or without the add-on.
Report: [2026-11-22-savedq-load.md](2026-11-22-savedq-load.md).

## ④ What the lab does not show yet

- **etcd round trips for Patroni.** A lab finding of 23 November (recorded in
  [STATUS](../STATUS.md)): in the two-site lab, Patroni's etcd client traffic goes to local ports
  with no added delay; only etcd peer traffic uses the emulated link. The two-site results so far
  measure Patroni with a near-zero round trip to etcd. A change that sends Patroni's etcd
  traffic through the emulated links is in preparation.
- **Large tables.** One run used 100,000 alerts (the index run of ②). Every other finished run
  used about 1,000 alerts or fewer.
- **One machine.** Servers, PostgreSQL and the load tool share one runner. There is no real
  network.

## Merged, with no full run yet

The code of these changes is on `main`. They have no full run and no report, so this page
gives no numbers for them.

- **A response cache at each site in the two-site lab**
  ([#290](https://github.com/steliosx/alrust/pull/290)): an eligible read enters the site's
  Varnish first, whose backend is the site's primary-aware HAProxy; writes bypass Varnish.
- **Building alert pages with less CPU**, the opt-in `alertlinks` and `fastdates` patches, with
  a byte-for-byte check against Alerta's own answers
  ([#288](https://github.com/steliosx/alrust/pull/288)).

## Alrust compared with Alerta

Alrust is this project's Rust server for Alerta's API and database layout. The reports below
compare it with Alerta 9.1.0; the [project README](../../README.md#performance) has the summary.
These runs measure a different question from the rest of this page.

## All reports

**Making stock Alerta faster**

| Date | Report | Question |
|---|---|---|
| 2026-11-20 | [Tuning suite](2026-11-20-tuning-bench.md) | Deployment settings only: what do removing `forwarder` and PgBouncer (TCP or Unix socket) change? |
| 2026-11-20 | [Add-on, first patches](2026-11-20-addon-bench.md) | What do `reuse`, `safe` and `hot` change, before `noecho` and `savedq` existed? |
| 2026-11-21 | [Response cache at one site](2026-11-21-cache-bench.md) | What does a short-lived Varnish cache in front of Alerta give? |
| 2026-11-21 | [PostgreSQL 19.0 against 18.7](2026-11-21-pg19-bench.md) | Does the newer PostgreSQL change Alerta's rates? |
| 2026-11-21 | [Fastpath suite](2026-11-21-fastpath-bench.md) | What do removing `forwarder`, `noecho`, `hot` and the id index change for duplicate writes? |
| 2026-11-22 | [Saved searches under gunicorn](2026-11-22-savedq-load.md) | What does `savedq` change for whole HTTP requests? |
| 2026-11-22 | [Alerta at two sites](2026-11-22-two-site-placement.md) | Where must a site B user's request go, and what does `noecho` change there? |
| 2026-11-25 | [The ID prefix index on 100,000 alerts](2026-11-25-pgtune-index-bench.md) | What does the index alone change on a large table? |
| — | [Operator guide](alerta-operator-guide.md) | What to change, from the reports above |
| — | [Measure your own Alerta, safely](measure-your-own-alerta.md) | Which findings may apply to your installation? A read-only checkup, then a comparison on a copy |

**Alrust compared with Alerta**

| Date | Report |
|---|---|
| 2026-11-13 | [Alerta and alrust comparison](2026-11-13-alerta-comparison.md) |
| 2026-11-15 | [A diagnostic benchmark](2026-11-15-bench-compare.md) |
| 2026-11-17 | [Mixed load, pool size and key use](2026-11-17-mixed-bench.md) |
| 2026-11-20 | [The diagnostic benchmark on a laptop](2026-11-20-bench-compare.md) |
| 2026-11-20 | [Mixed load on a laptop](2026-11-20-mixed-bench.md) |
| 2026-11-20 | [Mixed load on a hosted runner, again](2026-11-20-mixed-bench-hosted.md) |

**Method and PostgreSQL**

- [Benchmark methodology: mixed load and the connection pool](benchmark-methodology.md)
- [PostgreSQL evaluation](postgresql-evaluation-2026-11-14.md) and
  [PostgreSQL 18.7: pins and checklist](postgresql-18.7-candidate.md)

## Run it yourself

The harnesses are in [`scripts/bench/`](../../scripts/bench/) and run as GitHub Actions
workflows: `bench compare` (suites such as `fastpath`), `bench two-site` and
`addon savedq load`. Each report names its run, its commit and its artifact. The charts on this
page are drawn by [`scripts/gen-perf-figures.py`](../../scripts/gen-perf-figures.py) from
numbers quoted in the reports; `python3 scripts/gen-perf-figures.py --check` confirms that each
bar's value is in its report and that the SVG files are current.
