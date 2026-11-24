# Measure your own Alerta, safely

This guide is for operators of Alerta 9.1.0 on PostgreSQL who want to know which of the findings
in this folder may apply to their installation, and how much they may gain, before they change
anything. It has three parts:

- **A. Look at production, read-only.** One SQL file and a few configuration checks. The file
  runs about a dozen short queries on one connection; it writes nothing, creates nothing, and
  needs no restart.
- **B. Compare on a copy.** Stock Alerta and Alerta with the add-on, side by side, on a restored
  copy of your database on another host. Every comparison under load belongs here, never on
  production.
- **C. Read the result.** What each observation points to, and what the lab measured for it.

**What this guide does not do.** It does not predict a speed-up for your installation. The lab
numbers come from one kind of hosted runner with synthetic alerts; your hardware, data and traffic
differ. **The add-on (`scripts/bench/alerta_fast.py`) has run only in this project's labs.** Whether
to try it in production is your decision, after part B, and after reading its file header, which
lists what each patch changes.

## A. Look at production, read-only

### A1. A role that can only read

Ask the person who runs your PostgreSQL for a role that can read Alerta's tables and the
statistics views, and nothing else. Creating a role is itself a change to the cluster, so it is
theirs to make. This is what the project's CI check uses (on its own throwaway database):

```sql
CREATE ROLE checkup LOGIN PASSWORD '<a password>';
GRANT CONNECT ON DATABASE <alerta database> TO checkup;
GRANT USAGE ON SCHEMA public TO checkup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO checkup;
GRANT pg_read_all_stats TO checkup;  -- only needed to see other sessions' statements
```

An existing monitoring role with the same or fewer rights also works.

### A2. Run the checkup

[`scripts/bench/alerta_checkup.sql`](../../scripts/bench/alerta_checkup.sql) needs PostgreSQL 14
or newer (it has run with psql and PostgreSQL 18.7 only):

```sh
psql "<connection string of the read-only role>" -X -v interval=300 -f scripts/bench/alerta_checkup.sql
```

It reads settings, catalog and statistics views and Alerta's own `metrics` table. It plans one
predicate with plain `EXPLAIN` (the predicate is not run). It takes two snapshots of counters
`interval` seconds apart and prints the differences. Every query runs in a `READ ONLY`
transaction with a 5 s statement timeout and a 1 s lock timeout, and the wait between the
snapshots holds no transaction open. It never runs `EXPLAIN ANALYZE`, never resets a statistic and
never loads an extension. Pick a time of ordinary traffic. Read the file before you run it, and
leave the `CHECKUP_WAIT` environment variable unset (it is for the CI check).

The [`alerta checkup sql`](../../.github/workflows/alerta-checkup.yml) workflow runs this file as
a role like the one above, on PostgreSQL 18.7 only, while stock Alerta receives 20 duplicates of
one alert and answers 10 alert lists. It checks that the role cannot write, and that the file
reports 20 receives, 10 queries, about one PostgreSQL session per request and three row updates
of `alerts` per duplicate. A second run takes the default path, with a plain wait.

The output has three parts:

1. **Server and database:** the PostgreSQL version, the database's collation and its provider,
   and four settings.
2. **The `alerts` table:** its size, its indexes, and the plan of the predicate that Alerta's tag
   and attribute writes use (`WHERE id=… OR id LIKE …`).
3. **Activity over the interval:** alert receives and alert queries (from Alerta's own timers),
   new PostgreSQL sessions per receive or query, row updates of `alerts` per receive, the share
   of those that were HOT updates, and alerts inserted. Other routes (keys, heartbeats,
   blackouts, the web UI's other calls) also open sessions and some update rows, so read these
   ratios as upper bounds.

If your server already loads `pg_stat_statements`, its view shows which statements take the most
time; do not load it just for this.

### A3. Configuration, by reading it

Without changing anything, note:

- `PLUGINS` in your Alerta configuration (Alerta's default is
  `['remote_ip', 'reject', 'heartbeat', 'blackout', 'forwarder']`), and whether
  `FWD_DESTINATIONS` lists anything. With `forwarder` in the list and no destination, every
  receive still writes the alert's tags and attributes again.
- Whether Alerta reaches PostgreSQL through a pooler such as PgBouncer, and how
  (`DATABASE_URL`: host and port, or a Unix socket).
- The gunicorn (or other server) workers and threads, and the number of Alerta servers.
- At two data sites: which site's Alerta serves each site's users, and where the PostgreSQL
  primary is.

### A4. Searches, from your access logs

If your web server or proxy already logs requests, count, for a typical hour: the share of
`GET /alerts` and `GET /alerts/count` requests with a `q=` parameter, and how many different `q=`
texts there are. Dashboards and saved views send the same few texts again and again. Do not turn
on new logging on production for this.

## B. Compare on a copy

### B1. The copy

- **A host that is not production,** with PostgreSQL of the same major version and Alerta 9.1.0
  installed as on production.
- **The data:** restore your newest backup there, or a dump from a replica. A `pg_dump` of the
  primary is a long read; if that is the only way, run it at a quiet time.
- **Nothing leaves the copy.** Before you start its Alerta: empty `FWD_DESTINATIONS`; remove every
  plugin and webhook that sends mail, chat messages or HTTP requests; make sure no monitoring
  system sends alerts to it.
- **Protect the copy like production.** The dump holds your alerts, API keys and user records.
  Keep it and the results inside your network, and delete the copy when you are done.
- **The same configuration as production otherwise:** plugins, pooler, workers and threads.
- **The server.** The project's runs used gunicorn only. If production uses another server, run
  both arms under gunicorn; the result then does not describe your server.

### B2. Two servers to compare

Stock Alerta, as the project's benchmark starts it (`scripts/bench/bench_compare.py`):

```sh
gunicorn --workers <N> --worker-class gthread --threads <T> --preload --bind 127.0.0.1:<port> 'alerta.app:create_app()'
```

Alerta with the add-on's default patches, the same way:

```sh
ALERTA_FAST=safe gunicorn --workers <N> --worker-class gthread --threads <T> --preload \
  --bind 127.0.0.1:<port> --pythonpath <directory of alerta_fast.py> alerta_fast:app
```

`--preload` runs Alerta's schema statements once, in the master, before the workers start. The
add-on checks at start that each name it replaces is where Alerta 9.1.0 has it, and stops with a
message if one has moved (`savedq` also stays off unless Alerta's parser file is the one of
9.1.0). Try the opt-in patches (`hot`, `noecho`, `savedq`) one at a time, each as
its own arm, after reading what the file header says about each.

### B3. Same answers first

Before any timing, send a few of your real requests (your dashboards' alert lists and counts,
with their `q=` texts) to both servers with the same key, and compare the JSON answers. They must
be the same apart from the host and port in each `href` and the per-request time `lastTime`. If they differ, stop and do
not use that patch.

### B4. The same load on each

- Send the same requests, at the same rate or with the same number of clients, to one server at
  a time; the other is idle. Repeat several times and change the order (stock first, then add-on
  first), and compare medians.
- The project's runs used `oha` in closed loops, for example
  `oha -z 15s -c 8 --no-tui --http-version 1.1 -m GET -H 'Authorization: Key <key>' <url>`, after a
  warm-up. A fixed rate shows latency at your real load better; use your load tool's rate option.
- **Writes change the copy.** For duplicate writes, send one alert that exists only on the copy,
  and restore the copy before each comparison that writes.
- Record requests per second, p50 and p99, and the CPU time of the gunicorn processes and of
  PostgreSQL per request. Part 3 of the checkup, run against the copy around a load run, shows
  sessions and row updates per request for each server.

## C. Read the result

What a checkup observation points to, and what the lab measured for it. Each lab number holds
only under its condition; follow the link for the method and the limits.

| You see | It points to | Lab result (condition) | Report |
|---|---|---|---|
| `sessions_per_receive_or_query` about 1 or more, no pooler | Each request opens a PostgreSQL session | PgBouncer over TCP: +18% to +39% requests/s, stock Alerta. The add-on's `reuse` (one kept connection per worker thread), duplicate writes at 8 clients: 263.4 to 915.6 per second (`forwarder` removed in both) | [tuning](2026-11-20-tuning-bench.md), [add-on](2026-11-20-addon-bench.md) |
| `alert_row_updates_per_receive` near 3, with `forwarder` in `PLUGINS` and no `FWD_DESTINATIONS` | Two of three row updates of a duplicate are an echo | Removing `forwarder`: +8% to +11% duplicate writes/s on stock Alerta, +39% to +45% with the add-on's `safe` patches (8 clients). With `forwarder` kept, `noecho`: +21% to +31% (8 clients, `safe`) | [tuning](2026-11-20-tuning-bench.md), [fastpath](2026-11-21-fastpath-bench.md) |
| `Seq Scan on alerts` in the plan of part 2, and a large table | The tag and attribute writes read the whole table (the plan decides; the collation shown is context only) | No result yet: the index is being measured on a large table ([#291](https://github.com/steliosx/alrust/pull/291)). At 1,000 alerts it gave 3% to 5% at 1 client and nothing at 8 | [fastpath](2026-11-21-fastpath-bench.md) |
| Many requests with the same few `q=` texts | The search text is parsed again on every request | `savedq`: server CPU per request −73% for one recurring search, −47% for 16 in turn; +2% to 3% when every text is new | [savedq](2026-11-22-savedq-load.md) |
| Long `q=` chains such as `a AND b AND c` | Alerta's parser keeps only the first two terms | Group them: `(a AND b) AND c` | [savedq](2026-11-22-savedq-load.md#a-finding-about-alertas-own-parser) |
| Two data sites, site B's users served by site B's Alerta | Every SQL statement crosses the link to the primary | 50 ms link: site B list 459–464 → 103–104 ms, duplicate write 971–977 → 61–63 ms when HAProxy sends the request to the primary's site | [two-site placement](2026-11-22-two-site-placement.md) |

The findings page, [Making Alerta faster: what we measured](README.md), has the pictures and the
caveats of each result.
