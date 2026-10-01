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
| Remove all attributes | A nonempty request maps both exactly attested stored attributes to null; the empty map is observable via GET despite a 500 `failed to update attributes` response; all other fields remain unchanged | `views/alerts.py:254-273`, backend `base.py:339-349,1568-1576` commits before returning the empty map; planned HTTP observes state, not direct transaction tracing |

Prerequisites match the previously reviewed isolated metadata fixture:
Python 3.12; a disposable PostgreSQL/reference server; `AUTH_REQUIRED=False`,
`CUSTOMER_VIEWS=False`, DEBUG off, ALERTA alarm model, default five plugins,
no forwarding destinations, no concurrent cron or housekeeping, explicit
synthetic admin/key metadata and the private fixture loader. The fixture's
normal receive path removes its synthetic null attribute and deduplicates
the stored tags. The default `remote_ip` pre-receive plugin also adds
`ip: "127.0.0.1"` on the reviewed loopback target
(`alerta/plugins/remote_ip.py:16-19`). The removal case requires exactly
the key set `{"SYNTHETIC-keep", "ip"}`, the retained synthetic value and
a nonempty string `ip`; it removes every attested key. The plugin derives
`ip` from the request access route, falling back to the remote address;
the client sends no `X-Forwarded-For` and disables environment proxies.
The observed loopback value is not a universal IP-value oracle. Unknown
attributes fail the prerequisite before mutation. No plugin is disabled.
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

HF9 correction: the preserved first attempt at frozen
`70a40be307aceeb6719e80f1bdc162a24c9c1017`, window
`alrust-metadata-mutations-reference-20260930-01`, START `d9737019`,
RELEASE `b317ee1c`, finished **FAIL: 1 failed, 2 passed in 0.46s**.
Tag/untag empty-input and untag-all cases passed. The removal case's
incorrect sole-attribute precondition failed before its PUT: it omitted
the default plugin's `ip`. The removal 500 and persisted empty map were
therefore **NOT_RUN**, with no inferred runtime success. Private evidence
and the original failed report are retained. This head corrects that
precondition and payload; it requires fresh independent review and a new
signed window before any retry. No prior ACK or approval carries forward.
