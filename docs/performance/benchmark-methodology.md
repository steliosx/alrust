# Benchmark methodology: mixed load and the connection pool

Status: **a plan, not a result** (16 November 2026). No number in this
document is new. It records what the next hosted benchmark measures, what it
leaves out, and why. The owner approved one full run for it on 2026-11-16,
to start after #260 merged (it has, at `c86fa2c0`). The harness is
`scripts/bench/` and `.github/workflows/bench-compare.yml`; the method of
the existing cases is in
[the 15 November diagnostic benchmark](2026-11-15-bench-compare.md) and is
not changed here. That document's limits stay as written: nothing here
gives its earlier results a later correctness check.

## What prompted it

A tuned 4-core comparison (in #255, not on `main` at this writing; alrust
`11aede4b`, Alerta `f4312727`, an 8-vCPU VM with CPU limits per component)
reports alrust at 3,366 alerts/s with 8 writers alone and at 304 alerts/s
when 16 readers of the newest-50 list ran beside them. alrust was still
ahead of both tuned Alerta servers (84 and 67 alerts/s), but its own write
rate fell about 11-fold. That document names the fixed pool of 16
connections as a hypothesis and says it was not measured. These numbers are
quoted from that report; its raw capture was not audited here.

## What the code says (read from source, not measured)

At `main` `e8d73099`, the commit before #260:

1. **The serving pool is fixed at 16 connections** (`alrust-server`
   `main.rs`, the `connect(&database_url, 16, …)` call). There is no setting
   for its size. The start-up pool (2 connections) is closed before the
   serving pool opens.
2. **A request checks a connection out of the pool more than once, one
   after another.** A list with an API key makes three checkouts: the key
   check (`use_key`), the count (`count_alerts`) and the page
   (`find_alerts`). A write with an API key makes two: the key check and the
   receive transaction. These are checkouts, not necessarily different
   physical connections. A checkout waits in the pool's queue only when
   every connection is busy.
3. **The key check is its own committed `UPDATE` of the key's row.**
   Requests that use the same key at the same moment can wait for that row;
   a request with no such neighbour does not.
4. **Every list counts all matching rows** before it reads its page, as
   Alerta does. If writers add rows during a run, each list costs more.

Since #260 (`c86fa2c0`), the key check is a `SELECT`: it is still a
checkout, but no longer a write. A background writer now updates the
counters about once a second, through the same pool. That work is part of
the server's database use and is counted with it (see Reported).

So with 24 clients on 16 connections, a short write can wait behind list
queries that take much longer. That fits the reported write latency. It
does not show the cause.

## What the run measures

One new case, added to the existing harness by its own reviewed PR. What
follows is the specification that PR must meet; nothing here is built yet.

### The write workload

The existing write case sends one fixed alert, so every write after the
first is a duplicate of one row. The report in #255 describes a sequence
of 7,920 alerts with new, duplicate and correlated shapes on a growing
table. Its harness is not in this repository, so this run does not
reproduce that sequence. It uses its own, stated here:

- Each of the 8 writers has its own 1,000 alert identities (a distinct
  `resource` per identity; the same `environment`, `event` and severity
  throughout, so there are no correlated alerts and no severity changes).
  No identity is shared between writers.
- A writer sends its identities in order and starts again at the first
  after the last. The first request for an identity inserts a row; every
  later one is a duplicate. The table grows from the 1,000 seeded rows to
  at most 9,000.
- The writer-alone control and the mixed case use this same workload, the
  same server build and settings, and a fresh copy of the same seeded
  database (`CREATE DATABASE … TEMPLATE`) for every cell and repetition.

This measures inserts followed by duplicates on separate rows, beside
readers of a growing table. It is neither the single-row duplicate
contention of the existing write case nor the workload of #255. Because
the table grows, the per-list count (point 4 above) is part of what the
mixed case measures; with at most 9,000 rows its size is bounded and the
same in every arm.

### Cells

| Cell | Load |
|---|---|
| writers alone | 8 writers, the workload above |
| readers alone | 16 readers of `GET /alerts?page-size=50&sort-by=lastReceiveTime`, on the seeded database |
| mixed | both at the same time |

### Reported

- Writes/s and reads/s, with p50 and p99 of each class, and errors.
- CPU of the server's process tree and of PostgreSQL as aggregates: CPU
  time per second of the timed phase, and CPU time per completed request of
  any class, with the denominator given. In the mixed cell this CPU is not
  split between reads and writes; the harness has no way to attribute it.
  It includes the server's background key-use writer.
- PostgreSQL sessions opened during the cell (the existing delta from
  `pg_stat_database`). A peak count of sessions is reported only if the
  harness PR adds a sampler, and then with its interval and the sessions
  it counts.

### Gates for the mixed cell

The existing BENCH-R1 and BENCH-R2 gates compare a fixed database with
responses read before the timed run. A list read while writers change the
table cannot be compared with a row snapshot from another moment, or with
another arm's list. So the mixed cell is checked at points where nothing
is being written:

1. **Before any load:** the existing list and count checks against the
   database and across arms, on the seeded data.
2. **Counting:** each writer records its 2xx responses separately for the
   warm-up and for the timed phase, per identity.
3. **After both load streams have stopped and every request in flight has
   been answered:** for each identity, a row exists if and only if it had
   at least one 2xx response, and its `duplicate_count` equals its 2xx
   responses minus one. So new rows equal the identities written, and the
   sum of the duplicate counts equals all 2xx writes minus the new rows.
   Then the list and count checks run again against the final rows.
4. **During the timed phase**, reads are held only to what stays true while
   rows change: a 2xx status, a body that parses, at most 50 alerts, in
   descending `lastReceiveTime` order.

A repetition with an error, a failed check or a dead server is not scored.

### Variables

Each sweep changes one thing and holds everything else fixed:

| Sweep | Values | Held fixed |
|---|---|---|
| Pool size | 16, 32, 64, by a new `ALRUST_DB_MAX_CONNECTIONS` (default 16, so nothing changes unless it is set) | one binary, one key-use mode, the workload |
| Key use | written on every request, and written about once a second | one binary: `ALRUST_KEY_USAGE_IMMEDIATE=true` and `false` on the build under test; the pool at 16 |

Making a list use one checkout for its count and page is a possible later
change. It is not part of this run. If it is ever measured, it is its own
sweep and is not combined with the two above.

Rules carried over from the existing harness: hosted `ubuntu-24.04`
runners, PostgreSQL 18.7, three repetitions with medians, the order of the
arms rotated per repetition, and the same headers and key on every request.

A full run starts only by manual dispatch and only with the owner's
approval for that run. A subagent may start it, watch it and collect the
result files from a written brief; the numbers are checked against the raw
`results.json` before they are cited. This document does not start a run.

## What the run leaves out, and why

**No PgBouncer arm for alrust** (the owner's choice, 2026-11-16). The
document in #255 suggests one. It is left out for two reasons:

- *alrust already avoids the cost that the PgBouncer arms were built to
  show.* Alerta opens and closes a PostgreSQL connection on every request
  (1.000 sessions per request direct, 0 behind PgBouncer, in the
  15 November run), and PgBouncer removes part of that cost. alrust's own
  pool keeps its connections open: that run recorded 0 new sessions per
  alrust request. This does not show that a pooler could have no other
  effect on alrust, such as how it schedules or shares connections; it
  shows only that the measured session cost is already absent.
- *alrust behind PgBouncer is not a supported or tested setup today.*
  alrust sets its `search_path`, `TimeZone=UTC`, `lock_timeout` and
  `statement_timeout` once per connection, as start-up options, and its
  queries use prepared statements. PgBouncer's transaction mode can give a
  client a different server connection for each transaction. Whether those
  settings and statements then hold depends on PgBouncer's version and
  configuration. That is a concern to test, not an established failure. A
  number from an untested setup would not be evidence about alrust.

Where a pooler in front of PostgreSQL can still be the right tool: several
separate programs sharing one server (more than one alrust instance, or
alrust beside Alerta, Zabalack or Voalack during a migration). Each
program's pool cannot see the others, and a pooler limits the total. The
simpler choice, once the pool size is a setting, is to give each program a
small pool and add the sizes up against PostgreSQL's `max_connections`.
Running alrust behind PgBouncer would need its own tested change first.

**No capacity claim.** The runner has 4 vCPUs shared by the server,
PostgreSQL and the load generator. The run compares arms with each other on
one machine; it does not say what a production host will serve.

**No transfer to or from #255.** That run used an 8-vCPU VM with CPU limits
per component, another workload and an earlier alrust. A ratio from one
setup describes that setup; it is not an effect size for the other.

## How the result is read

The run can show whether a change helped this workload on this runner. It
cannot say where requests wait: it records no pool wait times, no pool
occupancy and no lock waits.

- **Mixed writes rise with the pool size:** this supports an effect of the
  pool size on this workload. The default size is then a question for the
  owner, with the numbers.
- **Mixed writes do not rise with the pool size:** a larger pool did not
  help this workload here. The cause stays open. Locks on the key's row or
  on alert rows, the server, the load generator, or several limits at once
  can all give that result, and PostgreSQL's CPU below its limit does not
  rule out waiting inside PostgreSQL. Finding the queue would need direct
  evidence of waits, which is a later change to the harness.
- **The key-use sweep differs:** on this workload and this binary, writing
  the counter on every request costs that much. No difference means only
  that none was seen here.

Each of these is stated with its run id and raw results, or not at all.

For Python Alerta deployments spanning two sites, the dashboard study records Varnish hits, origin load, response latency and revocation timing alongside the reproducible workload. [Two-site operator dashboard benchmark](2026-11-21-two-site-varnish.md)
