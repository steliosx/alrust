# Per-ID metadata mutation vectors

These three cases are **SOURCE_DERIVED / HTTP NOT_RUN** at this new head.
They follow the reviewed OpenAPI heartbeat/single-alert source slice
`791bf3fe0e26a47d5fbec8883dfe6bbfa41a0337` and its OAA1–3 observations,
against reference `f4312727783dad64198b62ed778bbca98a5144a3`.
They describe the current reference without choosing an intentional divergence.

| Vector | Source expectation | Evidence limit |
|---|---|---|
| Missing and empty tag input | Both per-ID tag and untag answer 500 with `must supply 'tags' as json list`; the owned alert remains exactly unchanged after each request | Views `alerts.py:200-245`, `ApiError` default code in `exceptions.py:10-19,64-65`; no HTTP executed at this head |
| Remove every tag | Untag answers 200; GET serializes tags as `[]`, with all other fields unchanged | SQL `array_agg` in `postgres/base.py:321-328` and model hydration `alert.py:62,216`; API observation cannot prove direct database NULL storage |
| Remove sole attribute | A nonempty request maps the sole attribute to null; the update is observable via GET despite a 500 `failed to update attributes` response; all other fields remain unchanged | `views/alerts.py:254-273`, backend `base.py:339-349,1568-1576` commits before returning the empty map; planned HTTP observes state, not direct transaction tracing |

Prerequisites match the previously reviewed isolated metadata fixture:
Python 3.12; a disposable PostgreSQL/reference server; `AUTH_REQUIRED=False`,
`CUSTOMER_VIEWS=False`, DEBUG off, ALERTA alarm model, default five plugins,
no forwarding destinations, no concurrent cron or housekeeping, explicit
synthetic admin/key metadata and the private fixture loader. The fixture's
normal receive path removes its synthetic null attribute and deduplicates
the stored tags, leaving one attribute and two tags as asserted before use.
No production inputs or credentials are needed.

Only the existing `metadata_session` fixture is imported from
`test_metadata_edges.py`; importing it does not run its two tests. Each case
owns one UUID alert. The existing fixture records successful create IDs before
subsequent assertions and deletes that owned alert on teardown. Its client
closes in a `finally` block. Requests consume original responses; evidence
export alone redacts known synthetic keys. No identifier, client time, server
time, history, unknown field or list order is normalized. Comparisons exclude
only the field intentionally mutated in the corresponding vector.

The frozen previous suite
`60d2e021eeb9c1040cfa3bdb373cfe949e9b5db0` passed its **four heartbeat**
input/query cases in 0.42s in window
`alrust-heartbeat-input-query-reference-20260930-01`, RELEASE `ef919f45`,
independent peer result/cleanup confirmation `312fdb6f`. That historical scope
does not run or approve these new metadata cases. The source head, runner and
fresh signed window each need independent review before their first HTTP run.
Hosted CI remains **CI_BLOCKED_BILLING**; no local result satisfies the full
Phase 0 hosted gate or establishes Rust/two-server parity.
