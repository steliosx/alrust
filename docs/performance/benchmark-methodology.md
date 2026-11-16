# Benchmark methodology: mixed load and the connection pool

Status: **a plan, not a result** (16 November 2026). No number in this
document is new. It records what the next hosted benchmark measures, what it
leaves out, and why. The owner approved one full run for it on 2026-11-16,
to start after #260 merges. The harness is `scripts/bench/` and
`.github/workflows/bench-compare.yml`; the method of the existing cases is in
[the 15 November diagnostic benchmark](2026-11-15-bench-compare.md) and is
not changed here.

## What prompted it

The owner's tuned 4-core comparison (in #255, not on `main` at this
writing) measured alrust at 3,366 alerts/s with 8 writers alone and at
304 alerts/s when 16 readers of the newest-50 list ran beside them. alrust
was still ahead of both tuned Alerta servers (84 and 67 alerts/s), but its
own write rate fell about 11-fold. That document names the fixed pool of 16
connections as a hypothesis and says it was not measured. This plan
measures it.

## What the code says (read from source, not measured)

At `main` `e8d73099`:

1. **The serving pool is fixed at 16 connections** (`alrust-server`
   `main.rs`, the `connect(&database_url, 16, …)` call). There is no setting
   for its size. The start-up pool (2 connections) is closed before the
   serving pool opens.
2. **A request takes a pool connection more than once, one after another.**
   An authenticated list takes three: the key check, the count
   (`count_alerts`) and the page (`find_alerts`). An authenticated write
   takes two: the key check and the receive transaction. Each one waits in
   the pool's queue when all connections are busy.
3. **Before #260, the key check is a committed `UPDATE` of the key's row**
   (`use_key`). Every request with the same key waits for the same row lock.
   #260 makes the check a read; the connection is still taken.
4. **Every list counts all matching rows** before it reads its page, as
   Alerta does. When writers add rows during a run, each list costs more.

So with 24 clients on 16 connections, a short write can wait twice behind
list queries that take much longer. That fits the measured write latency
but does not prove the cause: PostgreSQL was limited to 2 cores in that
run, and a larger pool helps only when PostgreSQL has capacity to spare.

## What the run measures

One new case, added to the existing harness by a reviewed PR:

| | |
|---|---|
| Case | mixed: 8 writers (`POST /alert`) and 16 readers (`GET /alerts?page-size=50&sort-by=lastReceiveTime`) at the same time, on the seeded database |
| Reported | writes/s and reads/s, p50 and p99 of each, errors, peak PostgreSQL sessions, and CPU per request of the server and of PostgreSQL, as the existing cases report them |
| Controls in the same run | 8 writers alone and 16 readers alone, so the fall is measured inside one run on one runner |

Variables, one at a time:

| Variable | Values | Needs |
|---|---|---|
| Pool size | 16, 32, 64 | a new `ALRUST_DB_MAX_CONNECTIONS` setting (default 16, so nothing changes unless it is set) |
| Key use | the per-request write (before #260) and the coalesced write (#260) | two builds, or #260's immediate-write setting if the reviewed head keeps one |
| Connections per list | three (today) and two (count and page on one connection) | only if that change is merged before the run; otherwise it is left for a later run |

Rules carried over from the existing harness: hosted `ubuntu-24.04`
runners, PostgreSQL 18.7, three repetitions with medians, the order of the
arms rotated per repetition, the same headers and key on every request, and
the BENCH-R1 and BENCH-R2 gates. A repetition that fails a gate is not
scored. For the mixed case the write gate must also hold: the rows and
duplicate counts in the database must equal the 2xx write responses counted.

A full run starts only by manual dispatch and only with the owner's
approval for that run. A subagent may start it, watch it and collect the
result files from a written brief; the numbers are checked against the raw
`results.json` before they are cited.

## What the run leaves out, and why

**No PgBouncer arm for alrust.** The document in #255 suggests one. It is
left out for two reasons:

- *It would add nothing alrust lacks.* PgBouncer's job is to keep database
  connections open and share them. alrust does that itself: its pool opens
  connections once and reuses them, and the 15 November run measured 0 new
  PostgreSQL sessions per alrust request. The PgBouncer arms in that run
  exist for Alerta, which opens and closes a connection on every request
  (1.000 sessions per request direct, 0 behind PgBouncer).
- *alrust behind PgBouncer is not a supported setup today.* alrust sets its
  `search_path`, `TimeZone=UTC`, `lock_timeout` and `statement_timeout` once
  per connection, as start-up options, and its queries use prepared
  statements. PgBouncer's transaction mode gives a client a different server
  connection for each transaction, so whether those settings and statements
  hold depends on PgBouncer's version and configuration. This has not been
  tested. A number from an untested setup would not be evidence about
  alrust.

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

**No comparison of absolute numbers with #255.** That run used an 8-vCPU
VM with CPU limits per component. Only the direction and size of the change
between arms can be compared.

## How the result is read

- Mixed writes rise with the pool size and PostgreSQL's CPU is not at its
  limit: the pool queue is a cause, and the default size is then a question
  for the owner, with the numbers.
- Mixed writes do not rise with the pool size: the queue has moved into
  PostgreSQL, the pool was not the limit on this machine, and the per-list
  count and the number of connections per request are the next things to
  measure.
- #260 changes the mixed result with the pool held at 16: the per-request
  key write was part of the cost.

Each of these is stated with its run id and raw results, or not at all.
