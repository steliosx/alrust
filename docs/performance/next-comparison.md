# Next comparison: matched-size arms for the new changes

Status: **plan for a later lab window, 14 November 2026.** Nothing here has
run. It extends the 13 November read pilot
([report](2026-11-13-alerta-comparison.md)) so that the changes made since
can be measured against the same baseline, and fixes the two gaps that
report names: different dataset sizes per arm, and no matched three-arm run.
The lab, its harness and its raw evidence are Codex's; this page states what
to measure so the results answer a question.

## Arms

All arms on PostgreSQL 18.6 (`18.6-1.pgdg12+2`), the same VM shape and
resource caps as the pilot, one VM at a time, the same seeded database
cloned per arm.

| Arm | Source | Why |
|---|---|---|
| A | Alerta 9.1.0 `f4312727`, the pilot's environment | Baseline |
| M | alrust main `35b0380c` (Rust 1.100.0) | Current main |
| C | M + #224 (`bd3d674b`, counts-only) | Measures the removed row fetch |
| L | M + #229 (`29ede446`, exact-id lookup) | Measures the exact-id path |

C and L are each one change on top of M, so a difference between M and C
or M and L has one cause. If both merge before the window, add M2 (main
with both) and keep C and L as separate arms. A PostgreSQL 18.7 arm is a
separate experiment with identical application code (see the 18.7
candidate page), never mixed into these arms.

## Data

10,000 and 100,000 alerts from one generator and one seed, unique receive
timestamps, verified row counts. The 100,000 size is new: the lookup and
count changes matter more as the table grows. Same database bytes for every
arm at each size (clone, do not reseed).

## Cases

The pilot's five read cases (list, event, severity, count, empty) on every
arm, plus:

| Case | Request | Expected to move |
|---|---|---|
| Count, filtered | `GET /alerts/count?severity=critical` | C against M |
| Get by full id | `GET /alert/{full id}` | L against M |
| Get by short prefix | `GET /alert/{first 8 chars}` | **Possible regression** in L: #229 first runs an exact-id query that misses, then the unchanged prefix query |
| Get by lastReceiveId | `GET /alert/{a duplicate's id}` | **Possible regression** in L, for the same reason |
| Ack by full id | `PUT /alert/{id}/action` `ack`, then `unack` | L against M (shorter row-lock hold) |

The two prefix and `lastReceiveId` cases measure the cost #229 adds to
every lookup that is not a full id; they are not controls. The controls are
cases no arm changes: the pilot's list, event and severity cases, which
must agree between M, C and L. If a control moves, the environment is
noisy and the run is not scored.

## Rules (from the pilot, kept)

- Responses must match across arms before any time is compared: the same
  status, the same ids in the same order, the same counts. A mismatch
  stops scoring for that case.
- Alternate arm order across four repetitions; five warm-ups and 25 timed
  requests per case, arm and repetition; medians of per-run medians; ratio
  as the median of paired ratios.
- That gives 100 timed requests per case and arm (4 × 25). Per case and
  arm, report the
  median and range only. p95 needs at least 1,000 samples in one case and
  arm, p99 at least 10,000; this plan reaches neither, so it reports no
  percentile.
- Record `EXPLAIN (ANALYZE, BUFFERS)` once per case and arm after the
  timed runs, to show the plan change (an index scan on `alerts_pkey` for
  L's full-id cases; one statement instead of two for C's count).
- Host temperature, CPU quota and memory limit recorded as in the pilot;
  no thermal exception is assumed.

## What it would and would not show

It would show whether each change shortens the cases it targets, on equal
data, with matched answers. It would not show capacity, concurrency
behaviour, write throughput or integration speed: the acknowledgement probe
plan ([integration probes](integration-probes.md)) and an open-loop load
test are separate.
