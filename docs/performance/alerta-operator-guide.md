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
Alerta reaches the primary across the link for every SQL statement. Medians of three blocks, with
site A primary and both sites healthy:

| Site B request | Site B's own Alerta | Alerta at the primary's site |
|---|---:|---:|
| Alert list | 459 to 464 ms | 103 to 104 ms |
| Alert count | 410 to 414 ms | 56 ms |
| Duplicate alert (write) | 971 to 977 ms | 61 to 63 ms |

Site A did not change. In the lab, HAProxy at each site checks Patroni's primary role and the
readiness of each site's Alerta (`scripts/bench/dual-site/haproxy-affinity.cfg`). Failover,
fallback and recovery were not part of the run.
Report: [2026-11-22-two-site-placement.md](2026-11-22-two-site-placement.md).

## 2. Writes of duplicate alerts

- **Remove `forwarder` from `PLUGINS` if you do not forward alerts.** With no destination, the
  plugin forwards nothing. But its `post_receive` returns the alert, so Alerta writes the alert's
  tags and attributes again on every receive. On one site, with 8 clients, removing it gave 39% to
  45% more duplicate writes per second, in each repetition.
- **If you need `forwarder`, the add-on's opt-in `noecho` leaves out the writes that change
  nothing.** On one site, with 8 clients: 21% to 31% more duplicate writes per second. At two
  sites, alert-row updates fell from 116 to 36 per run and the primary wrote 6% to 14% less WAL,
  but requests were not faster at that load: the median site B duplicate write was 1.2 to 2.4 ms
  slower. If your own post-receive hooks or database triggers expect those writes, check them
  first.
- **The opt-in `hot` patch** writes the key-use count and the timers about once a second instead
  of on each request. Use it only where those counts may be approximate.

Reports: [2026-11-21-fastpath-bench.md](2026-11-21-fastpath-bench.md),
[2026-11-22-two-site-placement.md](2026-11-22-two-site-placement.md).

## 3. Searches

- **Put parentheses around chained terms.** Alerta 9.1.0's PostgreSQL search parser keeps only the
  first two terms of an ungrouped chain: `a AND b AND c` loses `c`, and no error is shown.
  `(a AND b) AND c` keeps all three. This is Alerta's own behaviour, with or without the add-on.
- **Repeated searches (dashboards, saved views):** the opt-in `savedq` patch keeps the SQL that
  Alerta's parser made for a search text, so the parse runs once per worker and text. With one
  recurring search, the server's CPU per request fell by 73% to 78%. When every search text is
  new, it cost 2% to 3% more CPU. It is in pull request #281 and is not yet on `main`.

Report: [2026-11-22-savedq-load.md](2026-11-22-savedq-load.md).

## 4. Not yet measured, or measured only in part

- A response cache in front of Alerta at two sites (Varnish and HAProxy): the next stage of the
  two-site lab.
- Building alert pages with less CPU (the opt-in `alertlinks` and `fastdates` patches, pull
  request #288): byte-identical answers and less CPU in-process so far; no whole-request
  measurement yet.
- Failover and recovery with Patroni and etcd at three sites, synchronous replication, real
  networks, larger tables.
