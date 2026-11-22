# Zabalack and Voalack: acknowledgement performance probes

Status: **proposal for review**, 14 November 2026. Nothing here is measured.
No adapter is registered at main `30783165`, so no request calls an
integration hook yet, and no integration speed claim can be made. The
[read pilot](2026-11-13-alerta-comparison.md) measured plain reads only.

Owner goal: Zabalack and Voalack should run faster on alrust than on Alerta.
This page names the hot paths, three ranked hypotheses, the fixtures, and a
probe skeleton, so that the first measurement compares like with like.

Sources: [`spec/integration-hooks.md`](../../spec/integration-hooks.md)
(pinned Zabalack `eaf2acf0abd919b6f3f0b3e6b146adb81f83e70c`, Voalack
`4d70e69c3b3e2ccf95dd7b196c2cfe7a1de9d982`) and alrust's own code
(`crates/alrust-store-pg/src/lib.rs` `take_action_with`, `hooks.rs`). No file
of either integration was read for this page. A newer integration commit is a
separate, reviewed arm, never a silent pin change.

## What is compared

| Arm | Server | Integration |
|---|---|---|
| Baseline | Alerta 9.1.0 (`f4312727`), the reference environment of the read pilot | The pinned integration's Python plugin and SQL, installed as its own docs say |
| Candidate | alrust at one exact head | An implemented adapter for the same integration, calling the **same** capture SQL (spec section 1) |

A case without an implemented adapter or fixture is `NOT_RUN`. A mock hook
is never scored against the real plugin.

Outcome equality comes first: for every request, the same HTTP status and
body class, the same alert row, history head, capture row, outbox row and
skip or refusal reason. Only then are times compared.

## Hot paths of one claimed acknowledgement

Statements alrust runs today for `PUT /alert/{id}/action` with `ack` on a
claimed alert (`take_action_with`). Each row is one database round trip
unless stated. The row lock is taken in step 4 and held through `COMMIT` (step 11).

| # | Step | alrust today | Notes for the probe |
|---|---|---|---|
| 1 | `BEGIN` | 1 round trip | |
| 2 | `SET TRANSACTION ISOLATION LEVEL READ COMMITTED` | 1 round trip | Required by spec section 2; could be part of `BEGIN` (hypothesis 3) |
| 3 | `pre_lookup` per registered integration | adapter-defined | Voalack under A3 option (a): no SQL |
| 4 | Lock the alert: `SELECT * … WHERE (id LIKE $1 OR last_receive_id LIKE $1) … LIMIT 1 FOR UPDATE` | 1 round trip | A prefix match with `OR`: likely a sequential scan as the table grows (hypothesis 1) |
| 5 | `claims` per integration | Zabalack: attribute test, no SQL. Voalack: binding registry read | |
| 6 | `gate` | Zabalack: policy checks, then the capture-health probe on a **separate** read-only connection (bounded, cached). Voalack: `lock_binding`, full reload, `source_mismatch`, `lock_grant` | Pool acquire wait while the row lock is held (hypothesis 2) |
| 7 | `stage` | Zabalack: `stage_generation`, marker HMAC in Rust (X1). Voalack: `stage_intent` | Signing cost is CPU, measured separately |
| 8 | Re-read the locked row's `attributes` | 1 round trip | Required by spec section 3; keep |
| 9 | `UPDATE alerts …` with the new history head | 1 round trip, fires the capture trigger | Trigger cost is the integration's SQL, identical in both arms |
| 10 | `pre_commit` | Zabalack: nothing. Voalack: `approve_intent`, then the commit-time verification (reload, `lock_grant`, database clock) | Several round trips; all are required invariants |
| 11 | `COMMIT` | 1 round trip, one WAL flush | |
| 12 | `commit_outcome` | only when `COMMIT` has no server verdict | Never a blind retry |

On Alerta, Zabalack's staging commits on its own before Alerta's update
(spec 4.1, `plugins.py:300-319`); in alrust it stays inside the one
transaction once Zabalack's session accepts Z1. That is a difference in
durability steps, so it is reported, not hidden in a ratio.

## Three ranked hypotheses

1. **The lock lookup scans.** Step 4 matches `LIKE 'prefix%'` on `id` or
   `last_receive_id`. Under the default collation a plain B-tree cannot serve
   `LIKE`, so each acknowledgement likely scans `alerts` while it takes a row
   lock. Cost grows with table size and with the number of concurrent
   acknowledgements. Check: `EXPLAIN (ANALYZE, BUFFERS)` of step 4 at 1,000,
   10,000 and 100,000 alerts. Candidate fix (my earlier item 6): an exact-id
   lookup first, then the prefix path only when no row matches, with the
   reference's matching rules kept. This is a general alrust change, so it
   also helps acknowledgements that no integration claims.
2. **Zabalack's health probe waits for a second connection.** The probe in
   step 6 uses a separate connection while the alert row is locked. With 16
   pool connections and many concurrent acknowledgements, an uncached probe
   can wait for a free connection, which lengthens the lock hold time of every
   waiting request. Check: probe count, cache hits, pool acquire wait and lock
   hold time in the hot repeated-ACK case. Candidate fix: a small reserved
   pool for the probe, with the same bounds and cache rules; never skip the
   probe.
3. **Round trips inside the lock.** Steps 1 and 2 can be one statement, and
   the Voalack commit-time checks may batch reads that do not depend on each
   other. Check: statements per acknowledgement and lock hold time per case.
   Candidate fix: only where the spec's order and every invariant stay
   exactly the same; every such change needs Codex's and the integration
   session's review.

Not candidates: removing intent signing, generation fences, row locks,
per-customer grants, health bounds, final binding checks, trigger behaviour
or uncertain-commit suspension; external provider I/O inside the transaction;
retrying `COMMIT`.

## Cases

Each case runs on both arms with the same synthetic data. Missing adapter,
fixture or owner decision: `NOT_RUN` with the reason.

| Case | What it isolates | Blocked by |
|---|---|---|
| Unclaimed alert | Hook overhead when no integration claims | Nothing on alrust; the baseline needs the plugin installed |
| Claimed, successful ACK | Full path, steps 1 to 11 | Adapter; A1 and A2 (human actor, customer list); X1 (keys) |
| Policy refusal | Gate cost of a refusal | Adapter |
| Hot repeated ACK | Lock contention on one alert, N clients | Adapter |
| Independent alerts | Throughput across different rows | Adapter |
| Grant or configuration change during load | Re-read and refusal paths | Adapter; Voalack grant fixture |
| Capture-health outage | Zabalack probe failure path (503) | Adapter; a health fixture that can fail |
| Stage failure | Rollback cost | Adapter |
| Uncertain `COMMIT` | `commit_outcome` path | Adapter; a fault-injection proxy that drops the connection at `COMMIT` |

Warm and cold caches, and one shared key against one key per source, are
separate rows, never averaged.

## Measurements

Per case and arm: request-to-durable-capture latency (until the capture row
is committed), separate from outbox drain and from provider completion;
throughput; statements per request; lock wait and lock hold time; WAL bytes
and WAL syncs (`pg_stat_wal`); allocations on the alrust side. p50 needs at
least 100 samples per case, p95 at least 1,000, p99 at least 10,000; with
fewer, report the median and the range only. Server order alternates, as in
the read pilot.

The provider side uses one synthetic HTTP fixture for both arms with a fixed
delay (for example 50 ms) and fixed responses, so provider time is identical
and excluded from capture latency.

## Fixtures

- Synthetic alerts: Zabbix-sourced alerts (`attributes.source_system =
  'zabbix'`) for Zabalack; bound alerts (an active `voalack.bindings` row) for
  Voalack; plain alerts for the unclaimed case.
- Synthetic keys only, generated per run and destroyed after it; never a
  production or evidence key. Signing vectors from each integration's session
  (spec section 7) check the Rust signing byte for byte first.
- One disposable PostgreSQL 18.6 database per arm and run, seeded from the
  same dump.
- The provider fixture above; no live provider call.

## Probe skeleton (not yet compiled)

A Rust integration test in `crates/alrust-store-pg/tests/`, ignored by
default, using the synthetic `AckIntegration` already in `ack_hooks.rs`. It
measures the alrust side only and is the place a real adapter plugs in later.

```rust
/// Probe: hooked acknowledgements per case. Ignored by default; run with
/// `cargo test -p alrust-store-pg --test ack_probe -- --ignored --nocapture`
/// against a disposable DATABASE_URL.
#[tokio::test]
#[ignore = "probe, not a test: prints timings"]
async fn probe_claimed_ack_lock_and_round_trips() {
    let Some(db) = test_db().await else { return };
    let store = setup(&db, vec![hook(Fake::claiming())]).await;
    seed_alerts(&db.pool, 10_000).await; // same generator as the read pilot
    let mut samples = Vec::with_capacity(1_000);
    for i in 0..1_000 {
        let id = format!("probe-{i}");
        let started = std::time::Instant::now();
        store
            .take_action_with(&id, None, "ack", "probe", None, &caller())
            .await
            .expect("ack");
        samples.push(started.elapsed());
        store
            .take_action_with(&id, None, "unack", "probe", None, &caller())
            .await
            .expect("unack");
    }
    report("claimed_ack", &samples); // median, range, count; p95 only if n >= 1000
}
```

The statement count per request comes from `pg_stat_statements` in the
disposable database when it is available, else from PostgreSQL's statement
log for the probe's session; neither is enabled on a shared database.

## Who does what

- alrust (Claude): this page; hypothesis 1 as a normal alrust change with
  tests; the probe test once reviewed; adapters after the owner decides A1 to
  A3 and X1.
- Codex: review; any change to the shared conformance harness (proposed to
  Codex, not edited here).
- Zabalack's and Voalack's sessions: Z1, Z3, V2, signing vectors and their
  own tests against alrust (spec section 7). Nothing is assigned to them by
  this page.

## Not established

No measurement, no adapter, no owner decision on A1 to A3 or X1. All 19
runtime profiles stay `NOT_RUN`.
