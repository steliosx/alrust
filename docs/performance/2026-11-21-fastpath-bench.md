# Fastpath suite on a hosted runner: the 21 November 2026 run

One full run of the `fastpath` suite. It measures Alerta as released (no fork) with its default
plugin list, and what two additions change on the duplicate-write path: the opt-in `noecho` patch
of the add-on `scripts/bench/alerta_fast.py`, and one PostgreSQL index. The numbers are
diagnostic and relative to one hosted runner. They are not a capacity claim, and nothing here is
about more than one site.

## The cost this run is about

With Alerta's default `PLUGINS`, the `forwarder` plugin returns the alert from `post_receive`. Alerta
then writes the alert's tags and attributes again on every receive
(`reference/alerta/alerta/utils/api.py:88-90`): two more `UPDATE` statements, each with its own
commit, each with `WHERE id=... OR id LIKE ...` (`postgres/base.py:330-349`). For a duplicate
alert these writes change nothing.

- **`noecho`** reads the row by its primary key first. When the stored value is the value that
  would be written, it leaves the `UPDATE` out and commits as Alerta's function does. Otherwise
  Alerta's own function runs. The file header of the add-on lists what changes.
- **The index** is `CREATE INDEX alerts_id_pattern ON alerts (id text_pattern_ops)`. It lets
  PostgreSQL answer the `LIKE` half of that `WHERE` from an index. It changes no Alerta code.

## How this report was made

- **Run:** GitHub Actions run 44934944933, attempt 1, workflow `bench compare`, started by hand
  (`mode=full`, `suite=fastpath`) on `main` at `690222f906de586ce698b7a180473908d6e27390`.
  Artifact `bench-compare-44934944933-1`, 249694 bytes, SHA-256
  `6a4345cc49440011ebe2f79572b13754a4232cd1cee0251c44ba5465c0cef00f` as GitHub reports it.
- **Machine:** one GitHub-hosted runner, 4 vCPUs, about 16 GB of memory, image
  `ubuntu24 20261116.133.1`. Servers, PostgreSQL and the load tool share it, over loopback.
- **Software:** PostgreSQL 18.7 (PGDG package), Alerta 9.1.0 from the pinned `reference/alerta`
  submodule (`f4312727`), gunicorn with the gthread worker, load tool oha 1.17.0.
- **Shape:** 5 arms, 3 cases, 1 and 8 clients, 3 repetitions: 90 cells. Warm-up 5 s, timed 15 s.
  The tables show the median of the three repetitions. 1000 seeded alerts.
- **Results file:** [2026-11-21-fastpath-bench-results.json](2026-11-21-fastpath-bench-results.json),
  the run's `results.json`, unchanged.

Every arm is Alerta with 4 worker processes of 4 threads and the add-on's `safe` patches:

| Arm | Plugins | Addition |
|---|---|---|
| `safe-nofwd` | `forwarder` removed | none (the reference for "no second write") |
| `safe` | default | none (the baseline) |
| `safe-idx` | default | the index |
| `safe-noecho` | default | `noecho` |
| `safe-hot-noecho` | default | `noecho` and `hot` |

## Checks

- 90 of 90 cells valid; none missing, none left out.
- 1,020,847 timed requests; 0 errors; 0 answers outside 2xx.
- 30 of 30 duplicate-write repetitions saved exactly the writes that were counted (281,687 timed
  duplicate writes).
- Read checks passed for every arm, and every arm's list and count bodies equal the first arm's.
- `noecho` differential check: 38 of 38 cases agree with Alerta's own functions, on stored
  values, results, exceptions and the commit.
- Server check: one new alert and 20 duplicates made 62 row updates of `alerts` with the default
  plugins and 20 with `noecho`. These are row updates, not commits or WAL.
- Plan check: without the index the `UPDATE` is a sequential scan; with it the plan uses only
  `alerts_id_pattern`.

## Duplicate writes

| Clients | Arm | Requests/s | p50 ms | p99 ms | Commits per request | PostgreSQL CPU ms per request |
|---:|---|---:|---:|---:|---:|---:|
| 1 | `safe-nofwd` | 393.7 | 2.53 | 3.26 | 2.87 | 0.886 |
| 1 | `safe` | 309.3 | 3.23 | 4.14 | 5.01 | 1.290 |
| 1 | `safe-idx` | 319.3 | 3.14 | 3.99 | 4.69 | 1.200 |
| 1 | `safe-noecho` | 341.4 | 2.89 | 3.84 | 4.69 | 1.080 |
| 1 | `safe-hot-noecho` | 415.1 | 2.37 | 3.17 | 2.82 | 0.803 |
| 8 | `safe-nofwd` | 1047.6 | 7.30 | 13.28 | 2.98 | 1.366 |
| 8 | `safe` | 737.7 | 10.61 | 17.13 | 4.97 | 2.155 |
| 8 | `safe-idx` | 731.0 | 10.68 | 18.68 | 4.95 | 2.039 |
| 8 | `safe-noecho` | 930.9 | 8.13 | 13.97 | 4.97 | 1.577 |
| 8 | `safe-hot-noecho` | 1108.4 | 5.29 | 16.55 | 3.00 | 1.168 |

Requests per second in each repetition, and the change against `safe` in the same repetition:

| Clients | Arm | Repetition 1 | Repetition 2 | Repetition 3 | Change against `safe` |
|---:|---|---:|---:|---:|---|
| 1 | `safe-nofwd` | 393.7 | 393.2 | 395.0 | +27.3 %, +26.9 %, +30.6 % |
| 1 | `safe` | 309.3 | 309.9 | 302.5 | |
| 1 | `safe-idx` | 320.5 | 319.3 | 316.5 | +3.6 %, +3.0 %, +4.6 % |
| 1 | `safe-noecho` | 340.5 | 342.1 | 341.4 | +10.1 %, +10.4 %, +12.9 % |
| 1 | `safe-hot-noecho` | 391.3 | 418.2 | 415.1 | +26.5 %, +34.9 %, +37.2 % |
| 8 | `safe-nofwd` | 1055.5 | 1031.5 | 1047.6 | +44.9 %, +38.8 %, +42.0 % |
| 8 | `safe` | 728.3 | 743.0 | 737.7 | |
| 8 | `safe-idx` | 731.0 | 742.8 | 715.2 | +0.4 %, -0.0 %, -3.1 % |
| 8 | `safe-noecho` | 930.9 | 969.6 | 895.7 | +27.8 %, +30.5 %, +21.4 % |
| 8 | `safe-hot-noecho` | 914.3 | 1108.4 | 1110.7 | +25.5 %, +49.2 %, +50.6 % |

What the run shows:

- **`noecho`:** 21 to 31 % more duplicate writes per second at 8 clients, and 10 to 13 % at 1
  client, in every repetition. The number of commits per request does not change, because a write
  that is left out still commits. PostgreSQL CPU per request is lower (1.577 against 2.155 ms at 8
  clients).
- **`noecho` does not reach `safe-nofwd`.** The two reads and the two commits stay.
- **`noecho` with `hot`:** the median is above `safe-nofwd` (1108.4 against 1047.6 at 8 clients).
  One of the three repetitions at 8 clients was much lower (914.3).
- **The index:** 3 to 5 % at 1 client and no gain at 8 clients. The table has 1001 rows, so a
  sequential scan is cheap here. This run does not say what the index does on a large table.

## Reads

`noecho` and the index do not touch the read path, and the read numbers of `safe-nofwd`, `safe`,
`safe-idx` and `safe-noecho` are within a few percent of each other. `safe-hot-noecho` is faster
on reads because of `hot` (the key's use count and the timers are written about once a second).

| Case | Clients | `safe-nofwd` | `safe` | `safe-idx` | `safe-noecho` | `safe-hot-noecho` |
|---|---:|---:|---:|---:|---:|---:|
| list, requests/s | 1 | 222.1 | 218.8 | 220.2 | 221.9 | 248.8 |
| list, requests/s | 8 | 549.9 | 539.3 | 539.1 | 552.5 | 647.1 |
| count, requests/s | 1 | 607.2 | 602.1 | 612.0 | 607.9 | 882.8 |
| count, requests/s | 8 | 1594.9 | 1704.1 | 1661.2 | 1626.8 | 2550.3 |

## Limits

- One runner and three repetitions. The order of the arms moves by one place in each repetition
  (the first arm of a repetition is the second arm of the one before), so each arm had three
  of the five positions, not all five.
- The write case sends one fixed duplicate alert with no tags and one attribute. An alert whose
  tags or attributes change on each receive gets no gain from `noecho`, and pays for one more
  read; this run did not measure that case.
- 1000 seeded alerts. No large-table arm.
- One site, loopback only. No Patroni, no replication, no network delay.
- `hot` and `noecho` are opt-in. Read the add-on's file header before use: `noecho` changes what
  happens to rows whose id only begins with the alert's id, to row triggers on `UPDATE`, and to a
  concurrent change of the same alert.
