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
of each of three placement blocks, with site A primary and both sites healthy:

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

- **Remove `forwarder` from `PLUGINS` if you do not forward alerts.** With no destination, the
  plugin forwards nothing. But its `post_receive` returns the alert, so Alerta writes the alert's
  tags and attributes again on every receive. On one site, with 8 clients and the add-on's default (`safe`) patches in
  both arms, removing it gave 39% to 45% more duplicate writes per second, in each repetition.
  That run did not measure the removal on Alerta without the add-on.
- **If you need `forwarder`, the add-on's opt-in `noecho` leaves out the writes that change
  nothing.** On one site, with 8 clients sending one fixed duplicate, beside the `safe` patches: 21% to 31%
  more duplicate writes per second. At two sites, alert-row updates fell from 116 to 36 per run in the placement
  stage and from 92 to 20 in the fresh stage, and the primary wrote 6% to 14% less WAL in the
  four pairs that can be compared (one pair is left out). Requests were not faster at that load:
  the median site B duplicate write was 1.2 to 2.4 ms slower. Before you turn it on, read what
  the add-on's file header says changes when a write is left out: the stored tag order, a change
  that another request commits at the same time, other alerts whose id begins with this id (ids
  that a client chose), and your own UPDATE triggers on `alerts`.
- **The opt-in `hot` patch** writes the key-use count and the timers about once a second instead
  of on each request. Use it only where those counts may be approximate.

Reports: [2026-11-21-fastpath-bench.md](2026-11-21-fastpath-bench.md),
[2026-11-22-two-site-placement.md](2026-11-22-two-site-placement.md).

## 3. Searches

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

## 4. Not covered by the reports above

- A response cache in front of Alerta at two sites (Varnish and HAProxy) on the 22 November
  placement lab. An earlier two-site replay (20 and 21 November) measured Varnish with
  authenticated reads; it is not one of the reports here.
- Building alert pages with less CPU (the opt-in `alertlinks` and `fastdates` patches, pull
  request #288): byte-identical answers so far, and less CPU for Flask test-client requests in
  one process; gunicorn and network latency and throughput are not measured.
- Failover and recovery with Patroni and etcd on the 22 November placement lab. Earlier
  asynchronous promotion trials measured acknowledged loss at some lag thresholds; they are not
  one of the reports here.
- Synchronous replication, real networks, larger tables.
