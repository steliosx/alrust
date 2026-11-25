# The ID prefix index on a table of 100,000 alerts: the 25 November 2026 run

One full run of the `pgtune` suite, step 1. It measures one PostgreSQL change and nothing else:
`CREATE INDEX alerts_id_pattern ON alerts (id text_pattern_ops)`, on Alerta 9.1.0 as released
(no fork), with Alerta's default plugins, on a table of 100,000 alerts. The numbers are
diagnostic and relative to one hosted runner. They are not a capacity claim, and nothing here is
about more than one site.

## The cost this run is about

With Alerta's default `PLUGINS`, the `forwarder` plugin returns the alert from `post_receive`,
and Alerta then writes the alert's tags and attributes again on every receive: two `UPDATE`
statements with `WHERE id=... OR id LIKE ...`. This run did not capture the plans of those
`UPDATE` statements. Its plan probes ran simplified statements with the same predicates on this
fixture, whose database collation is `C.UTF-8`: without the index, each predicate got a
sequential scan (see the checks below). The explanation that this supports, and that the
end-to-end results fit, is that each of the two statements reads the whole table, because the
primary key's index does not serve the `LIKE` half in this setup.
The [21 November run](2026-11-21-fastpath-bench.md) measured the index at 1,000 alerts, where a
whole-table read is cheap: 3% to 5% at 1 client and no gain at 8. This run asks the same question
where the table is 100 times larger.

## How this report was made

- **Run:** GitHub Actions run 45548068348, attempt 1, workflow `bench compare`, started by hand
  (`mode=full`, `suite=pgtune`, `pgtune_rows=100000`, `occupy_5433=false`) on branch `claude/pgtune-suite` at
  `acd963188ac7818fcbb21ad12c8c20e302ac081d` (pull request #291). Artifact
  `bench-compare-45548068348-1`, 1017268 bytes, SHA-256
  `d8905fb9fc5694b5472b35fc12f116aad1862f876c3b4e24314276837a021775` as GitHub reports it.
- **Machine:** one GitHub-hosted runner, 4 vCPUs, about 16 GB of memory, image
  `ubuntu24 20261116.133.1`. Servers, PostgreSQL, PgBouncer and the load tool share it, over
  loopback.
- **Software:** PostgreSQL 18.7 (PGDG package), PgBouncer 1.26.1 in transaction mode, Alerta
  9.1.0 from the pinned `reference/alerta` submodule (`f4312727`), gunicorn with the gthread
  worker, load tool oha 1.17.0. The add-on is `scripts/bench/alerta_fast.py` at that commit,
  blob `99adfccf560c6b162ad7e86b1796817d19602ba5`; the file on `main` is newer.
- **Data:** a template of 100,000 alerts (76,054,528 bytes of `alerts`), made from the 1,000
  seeded alerts in SQL with new UUID ids, then `VACUUM (ANALYZE)` once. Database collation
  `C.UTF-8` (provider `c`), `alerts.id` with the default collation. The 50 newest alerts are
  the seeded ones. Each of the 24 write cells used its own new copy of the template. The 48
  read cells shared four copies, one per arm.
- **Shape:** 4 arms, 3 cases, 1 and 8 clients, 3 repetitions: 72 cells. Warm-up 5 s, timed
  15 s. The tables of medians show the median of the three repetitions.
- **Results file:** [2026-11-25-pgtune-index-bench-results.json](2026-11-25-pgtune-index-bench-results.json),
  the run's `results.json`, unchanged (SHA-256
  `01594ead969466e370431a80cb8c099e6dc7882606690670b2903569267c8bb2`).

Every arm is Alerta with 4 worker processes of 4 threads and the default plugins (`forwarder`
included), behind PgBouncer:

| Arm | Alerta | The index |
|---|---|---|
| `stock` | as released | no |
| `stock-idx` | as released | yes |
| `safe` | with the add-on's default patches (`ALERTA_FAST=safe`) | no |
| `safe-idx` | with the add-on's default patches | yes |

`stock` and `safe` are two separate baselines. Each index arm is compared only with its own
control, in the same repetition. In the results file the arms are `alerta-4x4-pgb-pgt`,
`alerta-4x4-pgb-idx-pgt`, `alerta-4x4-pgb-safe-pgt` and `alerta-4x4-pgb-safe-idx-pgt`.

**Order.** r0: stock, stock-idx, safe-idx, safe. r1: safe, safe-idx, stock-idx, stock. r2: as r0.
With three repetitions the split is 2 to 1: the stock index arm ran after its control twice and
before it once; the `safe` index arm ran before its control twice and after it once.

## Checks

- 72 of 72 cells valid; none missing, none left out.
- 104,693 timed requests; 0 errors; 0 answers outside 2xx.
- 24 of 24 duplicate-write repetitions saved exactly the writes that were counted (70,956 timed
  duplicate writes).
- Read checks passed for every arm, and every arm's list and count bodies equal the first arm's.
- No seed problem; the template has 100,000 alerts with UUID ids.
- No write cell has a problem flag, and no checkpoint completed inside a timed window.
- Plan probes (statements with the three `WHERE` clauses, on a throwaway copy; they are not
  Alerta's statements): without the index, the tag and the attribute predicates each get a
  sequential scan; with it, each gets a bitmap scan that uses only `alerts_id_pattern`. The
  duplicate predicate uses `env_res_evt_cust_key` in both.

## Duplicate writes

| Clients | Arm | Requests/s | p50 ms | p99 ms | Commits per request | Server CPU ms per request | PostgreSQL CPU ms per request |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | `stock` | 35.1 | 29.74 | 32.60 | 6.59 | 5.863 | 21.366 |
| 1 | `stock-idx` | 126.2 | 8.26 | 9.26 | 6.54 | 5.438 | 1.558 |
| 1 | `safe` | 44.7 | 20.64 | 28.90 | 4.77 | 2.191 | 18.748 |
| 1 | `safe-idx` | 243.4 | 3.91 | 5.73 | 4.75 | 1.826 | 1.441 |
| 8 | `stock` | 99.4 | 77.45 | 142.22 | 6.97 | 8.025 | 21.574 |
| 8 | `stock-idx` | 311.8 | 25.38 | 38.87 | 7.05 | 8.139 | 2.202 |
| 8 | `safe` | 117.7 | 65.01 | 127.66 | 5.01 | 2.820 | 19.757 |
| 8 | `safe-idx` | 601.1 | 13.21 | 20.37 | 4.98 | 2.504 | 2.263 |

The index arm against its control, repetition by repetition:

| Pair | Clients | Repetition | Index arm ran first | Control /s | Index /s | Change | p50 change | p99 change |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| stock | 1 | 0 | no | 33.0 | 126.2 | +282.4% | -73.1% | -72.7% |
| stock | 1 | 1 | yes | 35.1 | 126.1 | +259.1% | -71.9% | -70.6% |
| stock | 1 | 2 | no | 35.1 | 126.2 | +259.7% | -72.2% | -72.4% |
| stock | 8 | 0 | no | 93.9 | 311.8 | +232.2% | -70.1% | -73.1% |
| stock | 8 | 1 | yes | 99.4 | 303.0 | +204.6% | -66.6% | -69.8% |
| stock | 8 | 2 | no | 106.6 | 312.7 | +193.5% | -65.3% | -68.9% |
| safe | 1 | 0 | yes | 37.0 | 229.3 | +519.5% | -83.4% | -79.4% |
| safe | 1 | 1 | no | 44.7 | 243.4 | +444.7% | -81.1% | -80.2% |
| safe | 1 | 2 | yes | 51.2 | 245.2 | +378.7% | -78.9% | -79.8% |
| safe | 8 | 0 | yes | 107.5 | 601.1 | +459.4% | -81.2% | -85.5% |
| safe | 8 | 1 | no | 117.7 | 589.5 | +400.7% | -79.3% | -83.0% |
| safe | 8 | 2 | yes | 121.8 | 627.1 | +414.9% | -80.7% | -83.5% |

- **Stock Alerta:** +259% to +282% requests per second at 1 client and +193% to +232% at 8.
  p50 latency fell by 65% to 73%.
- **With the add-on's `safe` patches:** +379% to +520% at 1 client and +401% to +459% at 8.
  p50 latency fell by 79% to 83%.
- The gain has the same sign and a similar size in all 12 pairs, whichever member ran first.
- PostgreSQL CPU per duplicate fell from about 19 to 22 ms to about 1.4 to 2.3 ms. Commits per
  request stayed similar (the PostgreSQL counters vary from cell to cell). The experiment
  changed only the index; Alerta's statements were the same in both members of a pair.
- The control's rate rose from repetition to repetition in three of the four groups (for
  example `safe` at 1 client: 37.0, 44.7, 51.2 per second, +38% from first to last). The index
  arms moved much less (`safe-idx` at 1 client: 229.3, 243.4, 245.2, +7%). So the percentage
  for one repetition depends on which repetition it is; the ranges above show that.

## Reads

The index arm against its control:

| Pair | Case | Clients | Requests/s change, lowest to highest | p50 change, lowest to highest |
|---|---|---:|---:|---:|
| stock | list | 1 | -1.1% to +4.7% | -4.6% to +1.6% |
| stock | list | 8 | +0.6% to +2.6% | -2.6% to -0.5% |
| stock | count | 1 | -4.5% to +4.8% | -3.7% to +6.5% |
| stock | count | 8 | -2.5% to +8.2% | -7.2% to +4.5% |
| safe | list | 1 | -6.7% to +4.6% | -4.2% to +7.2% |
| safe | list | 8 | -5.0% to +1.5% | -1.5% to +8.0% |
| safe | count | 1 | -5.3% to +3.2% | -3.6% to +4.0% |
| safe | count | 8 | -5.9% to +0.5% | +0.7% to +6.5% |

The changes go from −6.7% to +8.2%, with both signs, and one group is positive in all three
repetitions (stock list at 8 clients, +0.6% to +2.6%). The run has no control-against-control
pair, so it cannot separate an effect of the index on reads from the variation between
repetitions. It shows no clear change. Whether the list or the count query uses the index was
not probed. Medians:

### Alert list

| Clients | Arm | Requests/s | p50 ms | p99 ms | Commits per request | Server CPU ms per request | PostgreSQL CPU ms per request |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | `stock` | 14.6 | 68.62 | 74.54 | 15.89 | 7.671 | 169.726 |
| 1 | `stock-idx` | 14.7 | 67.89 | 71.37 | 15.88 | 7.534 | 168.959 |
| 1 | `safe` | 19.4 | 51.54 | 53.99 | 9.95 | 3.677 | 134.708 |
| 1 | `safe-idx` | 19.1 | 52.30 | 54.56 | 9.97 | 3.798 | 136.411 |
| 8 | `stock` | 21.9 | 346.43 | 597.26 | 10.65 | 10.871 | 169.700 |
| 8 | `stock-idx` | 22.4 | 337.57 | 597.35 | 10.78 | 10.676 | 166.206 |
| 8 | `safe` | 27.9 | 260.80 | 497.12 | 6.46 | 5.083 | 136.903 |
| 8 | `safe-idx` | 27.6 | 267.99 | 509.41 | 6.39 | 5.178 | 138.138 |

### Alert count

| Clients | Arm | Requests/s | p50 ms | p99 ms | Commits per request | Server CPU ms per request | PostgreSQL CPU ms per request |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | `stock` | 22.8 | 44.00 | 46.54 | 4.16 | 5.409 | 37.661 |
| 1 | `stock-idx` | 22.5 | 44.65 | 47.04 | 4.14 | 5.473 | 38.077 |
| 1 | `safe` | 67.8 | 14.61 | 16.42 | 6.03 | 1.212 | 35.894 |
| 1 | `safe-idx` | 65.1 | 15.20 | 17.20 | 6.03 | 1.279 | 37.533 |
| 8 | `stock` | 88.3 | 90.99 | 134.00 | 4.07 | 7.128 | 36.338 |
| 8 | `stock-idx` | 86.6 | 91.91 | 132.02 | 4.07 | 7.144 | 35.804 |
| 8 | `safe` | 114.8 | 66.90 | 114.00 | 4.35 | 1.644 | 32.488 |
| 8 | `safe-idx` | 109.0 | 70.77 | 118.43 | 4.34 | 1.745 | 34.271 |

## WAL and HOT updates in the write cells

Timed WAL is the WAL of the cluster during the timed window, divided by the duplicates saved in
that window. Lifecycle WAL is from the server's start to its stop, over all saved writes; it
includes the first full-page images after the checkpoint before the cell, so a cell with fewer
writes shows more of it per write.

| Arm | Clients | Repetition | Timed WAL bytes per write | Checkpoint in window | Lifecycle WAL bytes per write | HOT share of `alerts` updates | Row updates of `alerts` |
|---|---:|---:|---:|---|---:|---:|---:|
| `stock` | 1 | 0 | 1042.9 | no | 2075.4 | 0.962 | 1979 |
| `stock` | 1 | 1 | 791.0 | no | 1835.0 | 0.963 | 2081 |
| `stock` | 1 | 2 | 833.6 | no | 1873.6 | 0.963 | 2066 |
| `stock` | 8 | 0 | 1112.9 | no | 1646.8 | 0.894 | 5681 |
| `stock` | 8 | 1 | 1282.6 | no | 1646.9 | 0.893 | 5966 |
| `stock` | 8 | 2 | 1129.6 | no | 1538.6 | 0.898 | 6326 |
| `stock-idx` | 1 | 0 | 788.9 | no | 1083.4 | 0.99 | 7511 |
| `stock-idx` | 1 | 1 | 788.8 | no | 1081.3 | 0.99 | 7586 |
| `stock-idx` | 1 | 2 | 788.9 | no | 1078.3 | 0.99 | 7670 |
| `stock-idx` | 8 | 0 | 1094.3 | no | 1212.1 | 0.949 | 18935 |
| `stock-idx` | 8 | 1 | 979.1 | no | 1131.9 | 0.956 | 18515 |
| `stock-idx` | 8 | 2 | 1069.5 | no | 1190.0 | 0.952 | 18941 |
| `safe` | 1 | 0 | 791.2 | no | 1779.9 | 0.966 | 2216 |
| `safe` | 1 | 1 | 861.1 | no | 1689.4 | 0.97 | 2576 |
| `safe` | 1 | 2 | 790.5 | no | 1517.5 | 0.975 | 2984 |
| `safe` | 8 | 0 | 1252.7 | no | 1589.7 | 0.895 | 6353 |
| `safe` | 8 | 1 | 1139.5 | no | 1506.8 | 0.898 | 7001 |
| `safe` | 8 | 2 | 1168.1 | no | 1477.3 | 0.907 | 7220 |
| `safe-idx` | 1 | 0 | 788.5 | no | 948.2 | 0.995 | 13865 |
| `safe-idx` | 1 | 1 | 788.4 | no | 938.6 | 0.995 | 14741 |
| `safe-idx` | 1 | 2 | 788.4 | no | 961.9 | 0.995 | 14576 |
| `safe-idx` | 8 | 0 | 1068.4 | no | 1152.8 | 0.939 | 36404 |
| `safe-idx` | 8 | 1 | 1114.6 | no | 1193.7 | 0.937 | 35894 |
| `safe-idx` | 8 | 2 | 1106.0 | no | 1182.5 | 0.928 | 37538 |

- **No WAL change per write can be assigned to the index.** Timed WAL per write was lower in
  the index arm in all 12 pairs, by 0.3% to 24.4%, but both figures have the same bias: the
  control saves far fewer writes in its window, so any fixed WAL in the window weighs more per
  write. At 1 client the index cells are 788.4 to 788.9 bytes per write and the lowest control
  cells are 790.5 to 791.2: where the control has little extra, the two are the same.
- The HOT share of `alerts` updates was 0.89 to 0.98 without the index and 0.93 to 0.995 with
  it. The index is on `id`, which no update changes, so it does not stop HOT updates.

## What this supports, and what it does not

- **Supported:** on this runner, with Alerta's default plugins and 100,000 alerts in a
  database with collation `C.UTF-8`, the index made duplicate writes about 3 to 4 times faster on stock
  Alerta and about 5 to 6 times faster with the add-on's `safe` patches. Reads showed no clear
  change.
- **Why the two baselines differ (a likely reason, not measured):** `safe` removes other costs
  of a request (a new database connection, logging), so the table scan is a larger share of
  what is left.
- **Compare with 1,000 alerts:** in the 21 November run `safe` wrote 309.3 duplicates per second
  at 1 client and 737.7 at 8 on a table of 1,000. Here, at 100,000, `safe` wrote 44.7 and 117.7
  without the index and 243.4 and 601.1 with it. Those runs differ in more than the table
  (PgBouncer is in the path here), so this is context, not a measured comparison.
- **Not measured:** the cost of building the index on a live table (`CREATE INDEX` takes a lock
  that blocks writes; `CREATE INDEX CONCURRENTLY` does not, and takes longer); the index's size;
  other table sizes; new alerts and other write routes; a database with collation `C`, where
  the primary key may already serve the predicate; more than one site; real alert data.
- **Removing `forwarder`** from `PLUGINS` when no destination is set, or the add-on's `noecho`
  patch, takes away the two statements themselves
  ([operator guide](alerta-operator-guide.md), section 2). Neither was an arm of this run, so
  this run does not say how the index compares with them or adds to them.
- To see whether an installation has this plan, plan (do not run) a `SELECT` with that
  predicate for an id that does not exist: `EXPLAIN SELECT id FROM alerts WHERE id = '<id>' OR
  id LIKE '<id>%'`. A `Seq Scan on alerts` is the clue. Pull request #295 has a read-only
  checkup file that does this.
