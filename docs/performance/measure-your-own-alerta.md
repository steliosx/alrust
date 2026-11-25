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

Ask the person who runs your PostgreSQL for a role that can read the five columns the checkup
reads, and nothing else. Creating a role is itself a change to the cluster, so it is theirs to
make. These are the grants of the project's CI check (on its own throwaway database):

```sql
CREATE ROLE checkup LOGIN PASSWORD '<a password>';
GRANT CONNECT ON DATABASE <alerta database> TO checkup;
GRANT USAGE ON SCHEMA public TO checkup;
GRANT SELECT (id) ON alerts TO checkup;
GRANT SELECT ("group", name, type, count) ON metrics TO checkup;
```

Do not grant `SELECT` on all tables for this, and do not use a role that already has it: the
checkup needs no more than the above. The statistics and catalog views it reads are open to every
role.

**These grants alone do not prove what the role can do.** A role also has what `PUBLIC` has,
what the roles it is a member of have, and what was granted before. On PostgreSQL 14, and on a
database that was made on 14 or older and then upgraded or restored, every role can usually
create objects in the `public` schema
([PostgreSQL 15 release notes](https://www.postgresql.org/docs/15/release-15.html#RELEASE-15-MIGRATION)).
The CI check shows that the role is refused each write and each sensitive read on its own new
PostgreSQL 18.7 database; it shows nothing about yours.

Before the first run on production, ask the DBA to run
[`scripts/bench/alerta_checkup_privileges.sql`](../../scripts/bench/alerta_checkup_privileges.sql)
as the checkup login. It reads the login's effective privileges with PostgreSQL's own
`has_*_privilege` functions, in one `READ ONLY` transaction, and changes nothing:

```sh
psql "<connection string of the read-only role>" -X -f scripts/bench/alerta_checkup_privileges.sql
```

Expected for a login that can only do what the checkup needs:

1. no role that the login is a member of (or only roles the DBA knows to be harmless);
2. `f` for superuser, for creating in the database and for creating in the `public` schema;
3. for every table, `f` for writing and `f` for reading the whole table. The readable columns
   are exactly `id` for `alerts` and exactly `count, group, name, type` for `metrics`, and none
   for every other table (`keys` and `users` among them). The last column says `ok`, or names
   each column that is missing or unexpected.

If a value differs, do not run the checkup with this login; what to change is the DBA's decision.
The file looks at the `public` schema of one database only: if Alerta's tables are in another
schema, or the login can connect to other databases, the DBA checks those too.

### A2. Run the checkup

[`scripts/bench/alerta_checkup.sql`](../../scripts/bench/alerta_checkup.sql) needs PostgreSQL 14
or newer (it has run with psql and PostgreSQL 18.7 only). Run it against the primary:

```sh
psql "<connection string of the read-only role>" -X -v interval=300 -f scripts/bench/alerta_checkup.sql
```

It reads settings, catalog and statistics views and four columns of Alerta's own `metrics`
table. It plans one predicate with plain `EXPLAIN` (the predicate is not run). It takes two
snapshots of counters `interval` seconds apart (a whole number from 1 to 3600) and prints the
differences. Every query that reads the database runs in a `READ ONLY` transaction, and each
transaction sets its own limits (`SET LOCAL`): 5 s for a statement, 1 s for a lock. The limits
therefore also hold behind a pooler in transaction mode. The wait between the snapshots is a
fixed `sleep` on your own host, with no transaction open. The file never runs `EXPLAIN ANALYZE`,
never resets a statistic, never loads an extension, and runs no command taken from the
environment. Pick a time of ordinary traffic, and read the file before you run it.

The [`alerta checkup sql`](../../.github/workflows/alerta-checkup.yml) workflow runs this file as
the role above, on PostgreSQL 18.7 only. While the file waits, the job sends stock Alerta 20
duplicates of one alert and 10 alert-list requests; the file itself sends nothing. The job checks
that:

- the role can neither write nor read `keys`, `users` or an alert's text, and the privileges
  file of A1 reports exactly the expected values for it;
- the privileges file reports a login that may create in `public` and is a member of a role
  with `SELECT` on `keys`, a login whose only excess is `SELECT` on the `text` column of
  `alerts`, and a login that lacks one of the `metrics` columns;
- an interval that is not a number from 1 to 3600 reads nothing, and a command in its place does
  not run;
- the file reports 20 receives, 10 queries, about one PostgreSQL session per request and three
  row updates of `alerts` per duplicate;
- an idle interval reports zeros, and `undetermined` for each ratio.

The output has three parts:

1. **Server and database:** the PostgreSQL version, the database's collation and its provider,
   and three settings (`max_connections`, `shared_buffers`, `synchronous_commit`).
2. **The `alerts` table:** its size, its indexes, and the plan of the predicate that Alerta's tag
   and attribute writes use (`WHERE id=… OR id LIKE …`).
3. **Activity over the interval:** alert receives and alert queries (from Alerta's own timers),
   new PostgreSQL sessions per receive or query, row updates of `alerts` per receive, the share
   of all row updates of `alerts` that were HOT updates, and alerts inserted.

**These ratios are indicators, not measurements of one request.** The three sources count
different things: sessions are those of the whole database (every route and every other client
of it), row updates are those of the whole `alerts` table (actions, housekeeping and other
programs included), and receives and queries are only what Alerta's two timers counted. A ratio
near the lab's value is a reason to look further on a copy (part B); it does not prove the cause.
The `result` field says `undetermined`, with the reason, when the server is a standby, when the
two snapshots are not of the same running database (a restart, another server behind the same
address, or a statistics reset), or when a counter or the clock went back. An interval in which
nothing was counted is a valid sample: `result` is `ok`, the counts are 0, and each ratio says
`undetermined` because it has nothing to divide by.

The plan in part 2 is also only a clue. It is the plan of a `SELECT` with that predicate for an
id that does not exist. The plan and the cost of the real `UPDATE` can only be seen with your
data on a separate copy.

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
- **Nothing leaves the copy.** Configuration alone is not enough, because a plugin can still
  call out. Before you start Alerta or any plugin on the copy:
  - block the copy host's outbound network access to your production systems and to the
    internet (a firewall rule or an isolated network), and check that it is blocked;
  - check that `DATABASE_URL` in the copy's configuration names the copy's own PostgreSQL, not
    production. Binding Alerta to `127.0.0.1` limits who can call it; it does not limit what it
    calls;
  - empty `FWD_DESTINATIONS`, and remove every plugin and webhook that sends mail, chat messages
    or HTTP requests;
  - make sure no monitoring system sends alerts to it.
- **A key for the test only.** Create a new API key on the copy for the load, and do not use a
  production key in commands or scripts. The copy still holds the production keys: one more
  reason to protect it.
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

What a checkup observation may point to, and what the lab measured for it. The second column is
a hypothesis to check on the copy (part B), not a diagnosis: the checkup's ratios come from
counters of the whole database and of the whole table. Each lab number holds only under its
condition; follow the link for the method and the limits.

| You see | Hypothesis to check on the copy | Lab result (condition) | Report |
|---|---|---|---|
| `sessions_per_receive_or_query` about 1 or more, no pooler | Each request may open its own PostgreSQL session (other clients of the database add sessions too) | PgBouncer over TCP: +18% to +39% requests/s, stock Alerta. The add-on's `reuse` (one kept connection per worker thread), duplicate writes at 8 clients: 263.4 to 915.6 per second (`forwarder` removed in both) | [tuning](2026-11-20-tuning-bench.md), [add-on](2026-11-20-addon-bench.md) |
| `alert_row_updates_per_receive` near 3, with `forwarder` in `PLUGINS` and no `FWD_DESTINATIONS` | Two of three row updates of a duplicate may be `forwarder`'s echo (other writers of `alerts` add updates too) | Removing `forwarder`: +8% to +11% duplicate writes/s on stock Alerta, +39% to +45% with the add-on's `safe` patches (8 clients). With `forwarder` kept, `noecho`: +21% to +31% (8 clients, `safe`) | [tuning](2026-11-20-tuning-bench.md), [fastpath](2026-11-21-fastpath-bench.md) |
| `Seq Scan on alerts` in the plan of part 2, and a large table | The tag and attribute writes may read the whole table (a clue: the real `UPDATE`'s plan needs a copy; the collation shown is context only) | No result yet: the index is being measured on a large table ([#291](https://github.com/steliosx/alrust/pull/291)). At 1,000 alerts it gave 3% to 5% at 1 client and nothing at 8 | [fastpath](2026-11-21-fastpath-bench.md) |
| Many requests with the same few `q=` texts | The search text is parsed again on every request | `savedq`: server CPU per request −73% for one recurring search, −47% for 16 in turn; +2% to 3% when every text is new | [savedq](2026-11-22-savedq-load.md) |
| Long `q=` chains such as `a AND b AND c` | Alerta's parser keeps only the first two terms | Group them: `(a AND b) AND c` | [savedq](2026-11-22-savedq-load.md#a-finding-about-alertas-own-parser) |
| Two data sites, site B's users served by site B's Alerta | Every SQL statement crosses the link to the primary | 50 ms link: site B list 459–464 → 103–104 ms, duplicate write 971–977 → 61–63 ms when HAProxy sends the request to the primary's site | [two-site placement](2026-11-22-two-site-placement.md) |

The findings page, [Making Alerta faster: what we measured](README.md), has the pictures and the
caveats of each result.
