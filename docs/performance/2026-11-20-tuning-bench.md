# Deployment settings only: the tuning suite on a hosted runner, 20 November 2026

One full run of the `tuning` suite. It asks what an operator can change in Alerta's deployment with
no change to Alerta's code: remove `forwarder` from `PLUGINS`, put PgBouncer in front of
PostgreSQL over TCP with SCRAM, or put PgBouncer on a Unix socket with `trust`. The numbers are
diagnostic and relative to one hosted runner. They are not a capacity claim. The alrust arm is a
reference only. It is in the tables for scale, and the report is about Alerta.

## Answer in short

- **Removing `forwarder` from `PLUGINS` adds about 8 % to 11 % duplicate writes per second.** The
  gain shows in every repetition, at 1 and at 8 clients. Reads change by no more than about 1.5 %.
  The write path commits 5.00 times per request, not 7.00.
- **PgBouncer over TCP with SCRAM adds 18 % to 39 % requests per second** with forwarder in place.
  With forwarder removed as well, the gain is 21 % to 55 %. PgBouncer keeps the pool, so
  Alerta opens no new PostgreSQL session per request.
- **PgBouncer on a Unix socket with `trust` gives the largest gain, but the run changes two things
  at once.** Compared with the same pair over TCP and SCRAM, server CPU per request is about 50 %
  to 77 % lower. PostgreSQL CPU per request does not move. The run cannot say how much of this is
  the socket and how much is `trust`. `trust` on a Unix socket lets any local process that can open
  the socket file connect without a password. The run does not recommend it.
- **Limits:** removing `forwarder` does not change reads. The run covers one runner, one
  1000-alert data set, one duplicate alert with no tags, loopback only, no Patroni and no
  replication.

## The question

The `tuning` suite runs the same cases and gates as the other suites. Every arm has 4 gunicorn
workers of 4 threads (`4x4`). Only the named setting differs from the base (`alerta-4x4`):

| Arm | What differs from the base |
|---|---|
| `alrust` | reference only (not Alerta) |
| `alerta-4x4` | the base: direct connections, the default plugins |
| `alerta-4x4-nofwd` | `PLUGINS` is `remote_ip,reject,heartbeat,blackout`, without `forwarder` |
| `alerta-4x4-pgb` | PgBouncer on 127.0.0.1 over TCP with SCRAM-SHA-256, transaction pooling, 16 server connections |
| `alerta-4x4-pgb-nofwd` | both changes above |
| `alerta-4x4-pgbu-nofwd` | both changes, and PgBouncer on a Unix socket with `trust` |

No add-on file is used in this suite. Alerta 9.1.0 runs as released.

## How this report was made

- **Run:** GitHub Actions run 44764620681, attempt 1, workflow `bench compare`, mode `full`, suite
  `tuning`, on `main` at `5199aa385d08bb78ca669e207dfa4a6bdba21748`. Started 2026-11-20T15:34:25Z.
  Started by hand (workflow_dispatch) on `main` by the project's agent, after the owner approved one
  full run of this suite. The review of this report is the first independent check of the result.
- **Artifact:** `bench-compare-44764620681-1`, 4522207 bytes, digest
  `sha256:60ae1e18733f0b33a226fea92c720d3ce9721fa785c848a4db6f301d6492f313` as GitHub reports it.
  GitHub keeps the artifact until 2026-11-27T16:20:31Z. The results file here stays.
- **Machine:** one GitHub-hosted runner, 4 vCPUs, 15701 MiB of memory (`free.txt`), image
  `ubuntu24 20261116.133.1`. The servers, PostgreSQL and the load tool share the runner, over
  loopback.
- **Software:** PostgreSQL 18.7 (Ubuntu 18.7-1.pgdg24.04+2) on x86_64, built with gcc 13.3.0.
  Alerta 9.1.0 from the pinned `reference/alerta` submodule (`f4312727`), as released. Load tool
  oha 1.17.0. gunicorn 27.0.1 and psycopg2 2.9.11 (`alerta-pip-freeze.txt`). PgBouncer
  1.26.1 (`pgbouncer.log`), transaction pooling, 16 server connections.
- **Shape:** 6 arms, 3 cases (`list`, `count`, `write-dup`), 1 and 8 clients, 3 repetitions:
  108 cells. Warm-up 5 s, timed 15 s. The tables show the median of the three repetitions. The data
  set has 1000 seeded alerts.
- **Results file:** [2026-11-20-tuning-bench-results.json](2026-11-20-tuning-bench-results.json), the
  run's `results.json`, unchanged.

## Checks

From the run's `summary.md`:

- 108 of 108 cells valid. None missing, none invalid.
- 36 of 36 duplicate-write repetitions saved exactly the writes that were counted.
- Read checks for each arm, in each of the 3 repetitions (18 lines): the total and the 50 newest
  ids, every listed alert's fields against the database, the count's groups against SQL, and the
  same bodies as the first arm, `href` origins aside. The list bodies are 41148 bytes for `alrust`
  and 44230 bytes for the Alerta arms. The run's files do not say why the sizes differ.

## Removing `forwarder`

The write case sends one fixed duplicate alert with no tags. The docstring says `forwarder`
rewrites the alert's tags and attributes on every receive, so the tag order that changes is not
exercised by this case.

Requests per second in each repetition, and the change against the base in the same repetition
(from `perrep-tuning.md`):

| Clients | Arm | Repetition 0 | Repetition 1 | Repetition 2 | Median | Change against the base, each repetition |
|---:|---|---:|---:|---:|---:|---|
| 1 | `alerta-4x4` | 101.3 | 99.3 | 102.1 | 101.3 | base |
| 1 | `alerta-4x4-nofwd` | 111.5 | 110.3 | 110.3 | 110.3 | +10.1%, +11.0%, +8.0% |
| 8 | `alerta-4x4` | 246.8 | 245.1 | 240.0 | 245.1 | base |
| 8 | `alerta-4x4-nofwd` | 268.4 | 270.6 | 266.2 | 268.4 | +8.8%, +10.4%, +10.9% |

Reads, in the same form. Reads do not change: the change is within about 1.5 % in every repetition.

| Case | Clients | Arm | Repetition 0 | Repetition 1 | Repetition 2 | Median | Change against the base, each repetition |
|---|---:|---|---:|---:|---:|---:|---|
| list | 1 | `alerta-4x4` | 93.4 | 91.7 | 93.7 | 93.4 | base |
| list | 1 | `alerta-4x4-nofwd` | 93.1 | 93.0 | 93.4 | 93.1 | -0.4%, +1.5%, -0.3% |
| list | 8 | `alerta-4x4` | 226.8 | 222.1 | 227.4 | 226.8 | base |
| list | 8 | `alerta-4x4-nofwd` | 223.3 | 223.5 | 226.1 | 223.5 | -1.5%, +0.7%, -0.6% |
| count | 1 | `alerta-4x4` | 130.9 | 128.6 | 128.9 | 128.9 | base |
| count | 1 | `alerta-4x4-nofwd` | 130.0 | 129.6 | 129.9 | 129.9 | -0.7%, +0.8%, +0.8% |
| count | 8 | `alerta-4x4` | 315.7 | 308.4 | 311.5 | 311.5 | base |
| count | 8 | `alerta-4x4-nofwd` | 313.9 | 311.5 | 311.3 | 311.5 | -0.6%, +1.0%, -0.1% |

The later fastpath run ([2026-11-21-fastpath-bench.md](2026-11-21-fastpath-bench.md)) measured the
same change, removing `forwarder`, with the add-on's `safe` patches on. It found a larger gain
there.

## PgBouncer on a Unix socket with `trust`

The `pgbu` arm changes two things at once, compared with `alerta-4x4-pgb-nofwd`:

1. the connection from Alerta to PgBouncer uses a Unix socket, not TCP on 127.0.0.1;
2. the rule for that socket is `trust`, not SCRAM. The bench's `hba.conf` has
   `local all all trust` for the socket and `host all all 127.0.0.1/32 scram-sha-256` for TCP.

This run does not separate the two. A fifth arm would be needed: the socket with SCRAM, or TCP with
`trust`.

What `trust` on a Unix socket means: PgBouncer does not ask for a password on that socket. The rule
`local all all trust` applies to any database and any user name. Any local process that can open
the socket file can connect. The socket file's permissions and its directory become the only
control. On this runner the directory belongs to the runner user and has mode 700 (the bench's own
comment says so). On an installation, a process of another user, or a group with access to that
directory, could connect. The run measures speed only. It does not test that access control.

Server CPU per request and PostgreSQL CPU per request, medians from `summary.md`:

| Case | Clients | Arm | Requests/s | p50 ms | Sessions per request | Commits per request | Server CPU ms per request | PostgreSQL CPU ms per request |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| list | 1 | `alerta-4x4-pgb-nofwd` | 113.3 | 8.58 | 0.000 | 3.75 | 6.341 | 1.841 |
| list | 1 | `alerta-4x4-pgbu-nofwd` | 183.7 | 5.27 | 0.000 | 3.75 | 3.204 | 1.763 |
| list | 8 | `alerta-4x4-pgb-nofwd` | 277.5 | 28.44 | 0.000 | 4.05 | 9.532 | 2.598 |
| list | 8 | `alerta-4x4-pgbu-nofwd` | 436.6 | 18.07 | 0.000 | 4.01 | 4.501 | 2.610 |
| count | 1 | `alerta-4x4-pgb-nofwd` | 171.6 | 5.67 | 0.000 | 4.28 | 4.479 | 0.863 |
| count | 1 | `alerta-4x4-pgbu-nofwd` | 389.4 | 2.43 | 0.000 | 4.24 | 1.349 | 0.844 |
| count | 8 | `alerta-4x4-pgb-nofwd` | 418.9 | 19.01 | 0.000 | 4.11 | 7.089 | 1.137 |
| count | 8 | `alerta-4x4-pgbu-nofwd` | 1029.4 | 7.47 | 0.000 | 4.01 | 1.669 | 1.186 |
| write-dup | 1 | `alerta-4x4-pgb-nofwd` | 145.4 | 7.12 | 0.000 | 4.68 | 5.103 | 1.100 |
| write-dup | 1 | `alerta-4x4-pgbu-nofwd` | 285.7 | 3.28 | 0.000 | 4.68 | 1.906 | 1.050 |
| write-dup | 8 | `alerta-4x4-pgb-nofwd` | 370.1 | 21.60 | 0.000 | 5.06 | 7.767 | 1.422 |
| write-dup | 8 | `alerta-4x4-pgbu-nofwd` | 693.3 | 10.38 | 0.000 | 4.98 | 2.599 | 1.454 |

The gain sits in the server's process tree, not in PostgreSQL. Compared with `alerta-4x4-pgb-nofwd`,
server CPU per request is about 50 % to 77 % lower in every case. PostgreSQL CPU per request stays
within about 5 %. The benchmark's docstring names the per-request SCRAM exchange with the pooler as what `trust`
removes. The run does not show how much of the gain comes from that and how much from the socket.

## Sessions and commits per request

- **Sessions per request.** The counter is the change in `pg_stat_database.sessions` over the timed
  part, divided by the requests. The direct arms show 1.000: each request opens one new PostgreSQL
  session. The PgBouncer arms show 0.000. PgBouncer keeps its own server connections (16 here, in
  transaction pooling). Alerta's per-request connection reaches PgBouncer, and PgBouncer starts no
  new PostgreSQL session. `alrust` shows 0.000 too.
- **Commits per request on writes.** For one duplicate write, the base arm commits 7.00 times and the
  arm without `forwarder` commits 5.00 times, at both client counts. The docstring says
  `forwarder` rewrites the tags and attributes on every receive. Two extra writes, each with its own
  commit, would give the two-commit difference. The counter counts commits, not statements, so this
  is consistent with the docstring but does not show the statements. In the PgBouncer arms the
  difference is 1.86 at 1 client and 1.93 at 8 clients. This run does not say why it is not
  exactly 2 there.
- **Commits per request on reads.** Both direct arms commit 4.00 times per list or count request.
  The run does not split these by statement.
- **Socket change.** The `pgbu` arm commits about the same as `pgb-nofwd`: within about 0.1 commits
  per request in every case (see the table). The socket changes the cost of each request, not the
  number of commits.

## Results

Medians of the three repetitions, copied from the run's `summary.md`:

| case | c | arm | valid reps | rps | p50 ms | p99 ms | errors+non-2xx (all reps) | sessions/req | commits/req | server CPU ms/req | postgres CPU ms/req |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| list | 1 | alrust | 3/3 | 509.7 | 1.93 | 2.46 | 0 | 0.000 | 2.92 | 0.897 | 1.098 |
| list | 1 | alerta-4x4 | 3/3 | 93.4 | 10.67 | 11.20 | 0 | 1.000 | 4.00 | 6.498 | 5.327 |
| list | 1 | alerta-4x4-nofwd | 3/3 | 93.1 | 10.71 | 11.32 | 0 | 1.000 | 4.00 | 6.474 | 5.360 |
| list | 1 | alerta-4x4-pgb | 3/3 | 113.4 | 8.56 | 9.94 | 0 | 0.000 | 3.75 | 6.363 | 1.827 |
| list | 1 | alerta-4x4-pgb-nofwd | 3/3 | 113.3 | 8.58 | 9.97 | 0 | 0.000 | 3.75 | 6.341 | 1.841 |
| list | 1 | alerta-4x4-pgbu-nofwd | 3/3 | 183.7 | 5.27 | 6.37 | 0 | 0.000 | 3.75 | 3.204 | 1.763 |
| list | 8 | alrust | 3/3 | 1349.5 | 5.84 | 9.96 | 0 | 0.000 | 3.00 | 1.140 | 1.463 |
| list | 8 | alerta-4x4 | 3/3 | 226.8 | 34.85 | 58.61 | 0 | 1.000 | 4.00 | 9.898 | 7.179 |
| list | 8 | alerta-4x4-nofwd | 3/3 | 223.5 | 34.42 | 59.11 | 0 | 1.000 | 4.00 | 9.982 | 7.233 |
| list | 8 | alerta-4x4-pgb | 3/3 | 281.3 | 28.24 | 44.04 | 0 | 0.000 | 4.03 | 9.500 | 2.580 |
| list | 8 | alerta-4x4-pgb-nofwd | 3/3 | 277.5 | 28.44 | 44.00 | 0 | 0.000 | 4.05 | 9.532 | 2.598 |
| list | 8 | alerta-4x4-pgbu-nofwd | 3/3 | 436.6 | 18.07 | 30.36 | 0 | 0.000 | 4.01 | 4.501 | 2.610 |
| count | 1 | alrust | 3/3 | 2669.9 | 0.37 | 0.49 | 0 | 0.000 | 2.00 | 0.123 | 0.258 |
| count | 1 | alerta-4x4 | 3/3 | 128.9 | 7.75 | 8.16 | 0 | 1.000 | 4.00 | 4.613 | 4.349 |
| count | 1 | alerta-4x4-nofwd | 3/3 | 129.9 | 7.69 | 8.01 | 0 | 1.000 | 4.00 | 4.615 | 4.272 |
| count | 1 | alerta-4x4-pgb | 3/3 | 172.5 | 5.64 | 6.48 | 0 | 0.000 | 4.31 | 4.467 | 0.835 |
| count | 1 | alerta-4x4-pgb-nofwd | 3/3 | 171.6 | 5.67 | 6.55 | 0 | 0.000 | 4.28 | 4.479 | 0.863 |
| count | 1 | alerta-4x4-pgbu-nofwd | 3/3 | 389.4 | 2.43 | 3.11 | 0 | 0.000 | 4.24 | 1.349 | 0.844 |
| count | 8 | alrust | 3/3 | 7418.7 | 1.05 | 1.97 | 0 | 0.000 | 2.00 | 0.118 | 0.330 |
| count | 8 | alerta-4x4 | 3/3 | 311.5 | 24.73 | 42.84 | 0 | 1.000 | 4.00 | 7.024 | 5.490 |
| count | 8 | alerta-4x4-nofwd | 3/3 | 311.5 | 24.99 | 42.53 | 0 | 1.000 | 4.00 | 7.047 | 5.462 |
| count | 8 | alerta-4x4-pgb | 3/3 | 423.6 | 18.89 | 30.29 | 0 | 0.000 | 4.07 | 7.013 | 1.138 |
| count | 8 | alerta-4x4-pgb-nofwd | 3/3 | 418.9 | 19.01 | 30.41 | 0 | 0.000 | 4.11 | 7.089 | 1.137 |
| count | 8 | alerta-4x4-pgbu-nofwd | 3/3 | 1029.4 | 7.47 | 11.75 | 0 | 0.000 | 4.01 | 1.669 | 1.186 |
| write-dup | 1 | alrust | 3/3 | 1839.9 | 0.54 | 0.70 | 0 | 0.000 | 1.93 | 0.282 | 0.242 |
| write-dup | 1 | alerta-4x4 | 3/3 | 101.3 | 9.82 | 10.49 | 0 | 1.000 | 7.00 | 5.349 | 5.480 |
| write-dup | 1 | alerta-4x4-nofwd | 3/3 | 110.3 | 9.04 | 9.60 | 0 | 1.000 | 5.00 | 5.103 | 4.985 |
| write-dup | 1 | alerta-4x4-pgb | 3/3 | 128.3 | 8.19 | 8.92 | 0 | 0.000 | 6.54 | 5.318 | 1.564 |
| write-dup | 1 | alerta-4x4-pgb-nofwd | 3/3 | 145.4 | 7.12 | 7.67 | 0 | 0.000 | 4.68 | 5.103 | 1.100 |
| write-dup | 1 | alerta-4x4-pgbu-nofwd | 3/3 | 285.7 | 3.28 | 4.16 | 0 | 0.000 | 4.68 | 1.906 | 1.050 |
| write-dup | 8 | alrust | 3/3 | 3019.8 | 2.64 | 2.92 | 0 | 0.000 | 1.98 | 0.304 | 0.306 |
| write-dup | 8 | alerta-4x4 | 3/3 | 245.1 | 31.98 | 50.08 | 0 | 1.000 | 7.00 | 8.210 | 7.465 |
| write-dup | 8 | alerta-4x4-nofwd | 3/3 | 268.4 | 29.30 | 47.52 | 0 | 1.000 | 5.00 | 7.958 | 6.500 |
| write-dup | 8 | alerta-4x4-pgb | 3/3 | 311.7 | 25.12 | 41.92 | 0 | 0.000 | 6.99 | 7.994 | 2.124 |
| write-dup | 8 | alerta-4x4-pgb-nofwd | 3/3 | 370.1 | 21.60 | 32.76 | 0 | 0.000 | 5.06 | 7.767 | 1.422 |
| write-dup | 8 | alerta-4x4-pgbu-nofwd | 3/3 | 693.3 | 10.38 | 21.20 | 0 | 0.000 | 4.98 | 2.599 | 1.454 |

## What the run shows

- **`forwarder`:** removing it gives more duplicate writes per second in every repetition, at both
  client counts. It does not change reads.
- **PgBouncer over TCP with SCRAM:** more requests per second in every case, with or without
  `forwarder` removed. PostgreSQL CPU per request falls, and no new PostgreSQL session starts per
  request.
- **PgBouncer on a Unix socket with `trust`:** the largest gain in the table. Its server CPU per
  request is far lower, and its PostgreSQL CPU per request is not. The run does not say which of the
  two changes makes the difference.
- **Scale:** these are relative numbers, from one runner, over loopback.
  The CPU columns count only the server's process tree and PostgreSQL.

## Limits

- One runner, 4 vCPUs, and 3 repetitions. The results are medians of three.
- 1000 synthetic alerts. No large data set.
- The write case sends one fixed duplicate alert with no tags. An alert whose tags or attributes
  change on each receive would show a different effect, and this run did not measure it.
- Loopback only. No network delay, no Patroni, no replication.
- The `pgbu` arm changes two things at once. The run does not separate them.
- The `trust` rule was measured for speed. This run does not test access control.
- The read cases commit 4 times per request. This run does not say which statements they are.

## See also

- [Fastpath suite on a hosted runner](2026-11-21-fastpath-bench.md), the 21 November run of the
  add-on's `safe` patches, with `forwarder` removed in one arm.
- [The 15 November diagnostic benchmark](2026-11-15-bench-compare.md), which uses the same three
  cases and gates.
