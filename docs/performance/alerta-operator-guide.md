# Making stock Alerta faster: what the measurements support

A guide for operators of Alerta 9.1.0 on PostgreSQL, built only from the measured reports in this
directory. Alerta stays as released (no fork). Where code helps, it comes from one add-on file,
`scripts/bench/alerta_fast.py`, which replaces a few of Alerta's functions when the server starts.
Its file header describes each patch.

Every number below comes from GitHub-hosted runners with synthetic data, and some runs also had
emulated links between sites. The numbers are relative to those runs. They are not a capacity
claim. Follow the links to each report for its method, checks and limits.

## 1. Two data sites with one PostgreSQL primary: send requests to the primary's site

With a 50 ms link between the sites, a site B user waited much less when site B's HTTP requests
went to the Alerta at the primary's site (site A), instead of to site B's own Alerta. Site B's own
Alerta reaches the primary across the link for every SQL statement. The ranges are the medians
of each of three placement blocks, with site A primary, both sites healthy, and the add-on's
`safe` patches in both arms:

| Site B request | Site B's own Alerta | Alerta at the primary's site |
|---|---:|---:|
| Alert list | 459 to 464 ms | 103 to 104 ms |
| Alert count | 410 to 414 ms | 56 ms |
| Duplicate alert (write) | 971 to 977 ms | 61 to 63 ms |

Site A medians stayed close in all arms. In the lab, HAProxy at each site checks Patroni's primary role and the
readiness of each site's Alerta (`scripts/bench/dual-site/haproxy-affinity.cfg`). Failover,
fallback and recovery were not part of the run.
Report: [2026-11-22-two-site-placement.md](2026-11-22-two-site-placement.md).

## 2. Writes of duplicate alerts

- **Remove `forwarder` from `PLUGINS` if you do not use it.** Check three things first. Is
  `FWD_DESTINATIONS` empty? The plugin also forwards actions and deletes, which are configured
  apart from alerts. Does anything rely on its loop check? Even with no destination, it refuses a
  request whose `X-Alerta-Loop` header already names this server. Does an UPDATE trigger of yours
  on `alerts` depend on the writes it causes? With no destination, the plugin forwards nothing.
  But its `post_receive` returns the alert, so Alerta writes the alert's tags and attributes
  again on every receive. The other four default plugins return nothing there; a plugin of
  yours that returns the alert keeps those writes. Both runs below used the default plugins and
  sent one fixed duplicate alert; the gain applies to that load.
  On one site, with 8 clients and the add-on's default (`safe`) patches in both arms, removing it
  gave 39% to 45% more duplicate writes per second, in each repetition. On stock Alerta (no
  add-on), the 20 November tuning run measured about 8% to 11% more, at 1 and at 8 clients;
  reads changed by no more than about 1.5%.
- **If you need `forwarder`, the add-on's opt-in `noecho` leaves out the writes that change
  nothing.** On one site, with 8 clients sending one fixed duplicate, beside the `safe` patches: 21% to 31%
  more duplicate writes per second. At two sites, alert-row updates fell from 116 to 36 per run in the placement
  stage and from 92 to 20 in the fresh stage, and the primary wrote 6% to 14% less WAL in the
  four pairs that can be compared (one pair is left out). Requests were not faster at that load:
  the median site B duplicate write was 1.2 to 2.4 ms slower. Before you turn it on, read what
  the add-on's file header says changes when a write is left out: the stored tag order, a change
  that another request commits at the same time, other alerts whose id begins with this id (ids
  that a client chose), and your own UPDATE triggers on `alerts`. Tags and attributes are checked
  apart: each write is left out only when its own stored value matches. A changed value still
  takes Alerta's ordinary UPDATE.
- **On a large `alerts` table, test the ID prefix index on a copy first:**
  `CREATE INDEX alerts_id_pattern ON alerts (id text_pattern_ops)`. With `forwarder` in
  `PLUGINS`, Alerta's tag and attribute writes use `WHERE id=… OR id LIKE …`. On one hosted
  runner with 100,000 synthetic alerts in a database with collation `C.UTF-8`, the index gave
  193% to 282% more duplicate writes per second on stock Alerta and 379% to 520% more with the
  `safe` patches, in each of 12 pairs; reads showed no clear change. At 1,000 alerts it gave 3%
  to 5% at 1 client and nothing at 8 (the 21 November run, with the `safe` patches in both arms).
  Not measured: the cost of building it on a live table
  (`CREATE INDEX` blocks writes while it runs; `CREATE INDEX CONCURRENTLY` does not, and takes
  longer), its size, and other table sizes. A database with collation `C` may not need it. If
  you remove `forwarder` and no other plugin returns the alert (the other four default plugins
  do not), the two statements go away. With `noecho`, each of the two is left out only when its
  own stored value matches, as for the one fixed duplicate of the tested load.
  The run did not compare the index with those. The read-only checkup in
  [Measure your own Alerta, safely](measure-your-own-alerta.md) plans (EXPLAIN, not run) a
  SELECT with a similar predicate. A full scan there is only a clue: confirm the plan of the
  real UPDATE on a copy of your data, as part B of that guide says.
- **The opt-in `hot` patch** writes the key-use count and the timers about once a second instead
  of on each request. Use it only where those counts may be approximate.

Reports: [2026-11-21-fastpath-bench.md](2026-11-21-fastpath-bench.md),
[2026-11-22-two-site-placement.md](2026-11-22-two-site-placement.md),
[2026-11-20-tuning-bench.md](2026-11-20-tuning-bench.md),
[2026-11-25-pgtune-index-bench.md](2026-11-25-pgtune-index-bench.md).

## 3. Database connections

- **Put PgBouncer between Alerta and PostgreSQL.** In the run, each request of Alerta 9.1.0 opened a new PostgreSQL session
  (Alerta directly on PostgreSQL); PgBouncer keeps the sessions open. On one runner with 1,000 alerts and 4
  gunicorn workers of 4 threads, PgBouncer over TCP with SCRAM gave 18% to 39% more requests per
  second than Alerta directly on PostgreSQL, with `forwarder` in place in both arms. With
  `forwarder` removed as well, the gain was 21% to 55%, against the same direct baseline with
  `forwarder` in place. That figure is the two changes together, not PgBouncer alone.
- **A Unix socket with `trust` is not recommended.** It gave the largest gain in that run, but
  the run changed two things at once, and `trust` lets any local process that can open the
  socket file connect without a password.

Report: [2026-11-20-tuning-bench.md](2026-11-20-tuning-bench.md).

## 4. Searches

- **Put parentheses around chained terms.** Alerta 9.1.0's PostgreSQL search parser keeps only the
  first two terms of an ungrouped chain: `a AND b AND c` loses `c`, and no error is shown.
  `(a AND b) AND c` keeps all three. This is Alerta's own behaviour, with or without the add-on.
- **Repeated searches (dashboards, saved views):** the opt-in `savedq` patch is for Alerta's
  stock PostgreSQL search grammar. Each worker keeps, in a store of the searches it used last
  (256 by default, `ALERTA_FAST_SAVEDQ_ENTRIES`; the least recently used is dropped first), the
  SQL condition and values that Alerta's parser made for a search text. While a search stays in that store, its parse does not run again. Alert
  results and the customer scope are never kept: each request still reads the database for its
  caller. With one recurring search, the server's CPU per request fell by 73% to 78%. In the
  tested load where every search text was new, it cost 2% to 3% more CPU. If your Python code
  changes Alerta's search grammar, leave `savedq` out, or call `alerta_fast.savedq_disable()` in
  each worker before its first request. Turn it on with `ALERTA_FAST=safe,savedq`.

Report: [2026-11-22-savedq-load.md](2026-11-22-savedq-load.md).

## 5. Not covered by the reports above

- A response cache in front of Alerta at two sites (Varnish and HAProxy) on the 22 November
  placement lab. An earlier two-site replay (20 and 21 November) measured Varnish with
  authenticated reads; it is not one of the reports here.
- Building alert pages with less CPU (the opt-in `alertlinks` and `fastdates` patches, pull
  request #288): byte-identical answers so far, and less CPU for Flask test-client requests in
  one process; gunicorn and network latency and throughput are not measured.
- Failover and recovery with Patroni and etcd on the 22 November placement lab. Earlier
  asynchronous promotion trials measured acknowledged loss at some lag thresholds; they are not
  one of the reports here.
- Synchronous replication, real networks. Tables larger than 1,000 alerts were measured once,
  for the ID prefix index at 100,000 alerts (section 2); there is no curve against table size.

## 6. Day-to-day reporting: a mention, not a finding

Reports such as alerts by service, by severity or by week read a large part of the `alerts`
table. On the primary they compete with Alerta's own reads and writes. This project has measured
none of the options below for Alerta; the list only says where to look.

- **A streaming standby** (with Patroni, a replica that is already there) takes reporting
  queries off the primary with no new software. Answers are behind the primary by the
  replication lag.
- **[Mason](https://github.com/embeddable-hq/mason-data)** (Apache-2.0, self-hosted or as its
  maker's hosted service) is a read replica for analytics: it copies chosen PostgreSQL tables
  into DuckDB's columnar storage and answers PostgreSQL SQL from there, so tools such as Grafana
  or Metabase connect to it as to PostgreSQL. **Not tested by this project.** What follows is
  its maker's own account, from
  [the project's README](https://github.com/embeddable-hq/mason-data/blob/05fa41f6adb7bf1a99331a316f73dcb58d334474/README.md#speed)
  as read on 24 November 2026:
  - counts and a join over 10 million rows, at 1 and at 10 clients: 16 to 214 times faster
    than PostgreSQL;
  - lookups by primary key 2.7 to 6.2 times slower than PostgreSQL ("Mason is for scans and
    aggregates"), so it is for reports, and Alerta itself stays on PostgreSQL;
  - of 8,323 test queries it answers 94% exactly as PostgreSQL does, answers 1% differently
    and refuses 6% with an error;
  - it is pre-1.0, and a release may rename a command, a key or a setting;
  - continuous sync needs logical replication on the source (`wal_level = logical`, a
    replication user and a publication); from a standby that needs PostgreSQL 16 or newer.
    Polling mode reads each whole table again at every poll.

  With the hosted service the chosen tables are copied to the maker's machines, and the
  database's address and login go to that service
  ([its privacy policy](https://masondata.ai/policies/privacy-policy/)). Alerts can hold host names, customer names
  and message text: decide that with whoever owns the data.

A comparison of reporting queries on the primary, on a standby and on such a replica is a
possible later lab; no result exists.
