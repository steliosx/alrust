# Search v2: what makes alert reads and `q=` fast, and what it costs

Status: **measured design for review**, 15 November 2026. Owner request,
the same day: "Search quality and performance would be a big improvement
if you can redesign/rethink it." This document covers performance. Search
quality is the opt-in mode in `spec/search-lucene-mode.md` (its own PR).
It follows [`search-design.md`](search-design.md) (Codex-approved as a
proposal in #228), which it does not edit:
- step 1 of that design (`q=` compatibility) is #245;
- question Q-S1 there (refuse `q` until it works) is moot.

**Every number here comes from one hosted GitHub runner** (PostgreSQL
18.6 from PGDG, synthetic data). Read the numbers as relative and the
plans as shapes. They are not a performance claim for any deployment.
The source is the `search measurement` workflow of #247, runs `43714683590`
and `43715199114`, and the raw plans are in those runs' artifacts.

## What was measured

`crates/alrust-store-pg/tests/search_measure.rs`:
- **Data.** 10,000 and 100,000 synthetic alerts. Every value is a fixed
  function of the row number (no randomness). `text` is three words from a
  16-word vocabulary; `resource` is `host-<n/8>` with 8 events per host;
  there are 3 environments, 6 severities, 5 statuses, 4 services and a
  `region` attribute. The seed ends with `VACUUM (ANALYZE)`.
- **Reads.** These run through the store's own statements (`find_alerts`
  page 1 of 50 and its grouped counts), so the measured SQL is the served
  SQL:
  - the dashboard's hot path: all alerts; `status=open&environment=Production`;
    `resource=host-42`;
  - every `q=` form of the reference corpus, including the unselective ones
    (`di`, `Dis*`, `resource:host*`).
- **Writes.** 200 duplicates per candidate through `Store::receive`, the
  receive path's duplicate update (it rewrites `text` and
  `last_receive_time`). It records the time and the WAL bytes per update.
- **Candidates.** One at a time: none; B-tree `(last_receive_time DESC,
  id)`; B-tree `(status, environment, last_receive_time DESC, id)`; B-tree
  `(severity, status)`; B-tree `(environment, resource)`; `pg_trgm` GIN and
  GiST on `text`; `pg_trgm` GIN on `resource` and on `event`.
- **Invariant.** Every candidate selected exactly what the unindexed
  baseline selected: the total, both count maps and the first page's ids,
  for every read, in both runs. Result: no difference in any case.

## Results (100,000 alerts; median of 5, milliseconds; custom plans)

| Read | none | B-tree `last_receive_time` | B-tree `status, environment, …` | `pg_trgm` GIN `text` |
|---|---|---|---|---|
| all alerts (list + counts) | 43 | 12 | 42 | 43 |
| `status=open&environment=Production` | 20 | 9.5 | 8.7 | 20 |
| `q=disk` | 100 | 47 | 100 | 27 |
| `q="disk full"` | 108 | 109 | 111 | 18 |
| `q=disk AND full AND server` | 100 | 104 | 100 | 4 |
| `q=Dis*` (matches nearly everything) | 97 | 38 | 96 | 96 |
| `q=di` (two letters) | 98 | 47 | 97 | 97 |

At 10,000 alerts, every read takes 3 to 21 ms with no index. The B-tree
on `last_receive_time` brings the list to 3 ms, and trigram GIN brings
`q=disk` to 4 ms.

Figures from run 2 (run 1 had no custom-plan column; its sizes and WAL
figures agree within a few percent).

| Candidate | Size at 100k | WAL per duplicate update | Time per update |
|---|---|---|---|
| none | — | 709 bytes | 0.55 ms |
| B-tree `last_receive_time` | 3.1 MB | 928 (+31 %) | 0.52 ms |
| B-tree `status, environment, last_receive_time` | 5.3 MB | 1,008 (+42 %) | 0.51 ms |
| B-tree `severity, status` | 0.7 MB | 789 (+11 %) | 0.52 ms |
| `pg_trgm` GIN `text` | 5.3 MB | 1,418 (+100 %) | 0.59 ms |
| `pg_trgm` GiST `text` | 15.4 MB | 1,078 (+52 %) | 0.60 ms |

## Findings

1. **The dashboard list is the hot path, and a B-tree on
   `(last_receive_time DESC, id)` is the broadest gain.** The page
   statement (`ORDER BY last_receive_time DESC, id LIMIT 50`) stops sorting
   the whole table: the list is 3.5 times faster, and most `q=` searches
   are about 2 times faster, because their page uses the same index. The
   cost is 31 % more WAL per duplicate update, with no measurable time.
   The grouped counts still scan; the `(severity, status)` B-tree saves only
   about 13 % there (43 to 37 ms).
2. **Trigram GIN on `text` makes selective text search 4 to 25 times
   faster** (`q=disk`, phrases, `AND` chains), with identical results.
   - It costs twice the WAL per duplicate update, because every duplicate
     rewrites `text`.
   - It does nothing for terms under three characters, for the
     `\y.*\y` of an upper-case wildcard, or for `NOT`. Those stay scans
     whatever is built. The `servable by an index` column of the runs shows
     this, case by case.
   - GiST is three times larger and slower to read. It is the fallback
     only if GIN on PostgreSQL 18.6 is refused (the 18.7 GIN fixes are #226,
     held).
3. **Generic plans are the trap.** sqlx prepares every statement, and after
   five executions PostgreSQL may switch a prepared statement to a generic
   plan that ignores the bound pattern.
   - With an index present, unselective searches then took the index and
     became up to 2.3 times slower than with no index at all: with trigram
     GIN, `Dis*` took 155 ms and `di` 123 ms; with every index except the
     `last_receive_time` B-tree (29 ms), `resource:host*` took 113 to 169 ms.
   - The same statements on sessions with
     `plan_cache_mode=force_custom_plan` took 96, 97 and 74 ms, the
     unindexed figures, and kept every result. With GIN and the B-trees,
     custom plans cost at most about 1.3 times on a few reads. With GiST
     they lose selective reads, where the generic plan took the GiST index
     and won (`dis*` 38 ms generic against 87 ms custom, `disk` 69 against
     99): one more reason GiST stays the fallback.
   - **A GIN or B-tree index proposal must come with this session
     setting**, or it risks slowing down the searches it does not help.
4. **At about 10,000 alerts no index is urgent:** the unindexed reads
   take 3 to 21 ms there. *Assumption, for the owner to correct from real
   deployments:* that a deployment keeps about that many. Housekeeping
   deletes expired and closed alerts after `DELETE_EXPIRED_AFTER` (2 hours)
   and informational ones after `DELETE_INFO_AFTER` (12 hours), but open
   and acknowledged alerts stay as long as they are open.

## Proposal (owner decisions, then an ADR-0005 revision)

| # | Change | Gain | Cost | Gate |
|---|---|---|---|---|
| P1 | `plan_cache_mode=force_custom_plan` on the serving pool's sessions | Removes the generic-plan regressions; no index, no schema change | Planning on every execution (0.1 to 0.2 ms per statement in these plans) | Codex review; it goes with P2 or P3, since without an index it changes nothing measurable |
| P2 | B-tree `alerts (last_receive_time DESC, id)` as migration `0003` | List 3.5×, most searches 2× | 3 MB at 100k; +31 % WAL per duplicate | ADR-0005 revision (owner ruling 6 of 2026-10-28: named extra indexes) |
| P3 | `pg_trgm` and a GIN index on `alerts.text` | Selective text search 4–25× | An extension on adopted Alerta databases; +100 % WAL per duplicate; the 18.7 GIN fixes (#226) | Owner (Q-S3), then an ADR-0005 revision |
| P4 | Nothing on `attributes`, `raw_data`, `history` | — | — | Unchanged from `search-design.md` |

My recommendation: P1 and P2 first, together. They are cheap and broad,
and need no extension. Decide P3 together with #226, after the opt-in
mode is reviewed, because that mode's multi-field search changes which
columns would want a trigram index.

## Not established

Absolute latencies on real hardware, behaviour above 100,000 alerts,
concurrent load, autovacuum's effect on the visibility map under constant
duplicate updates, and every one of the 19 runtime profiles (still
`NOT_RUN`).
