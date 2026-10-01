# Conditional management health reference case

Task `ALRUST-P0-MANAGEMENT-HEALTH-READONLY-01` implements the independently
reviewed MGH-S1–S4 plan. Plan approval does not approve this new harness, a
runner or a window. HTTP at this new head is NOT_RUN.

At reference `f4312727783dad64198b62ed778bbca98a5144a3`, one explicit
`GET /management/healthcheck` on an attested empty heartbeat table must return
status 200 and the exact text `OK`. This is a separate conformance request
after completed readiness and setup attestation. Previous readiness requests
are not its result (MGH-N4). The HTTP suite performs no count SQL.

The runner must observe `SELECT COUNT(1) FROM heartbeats` as integer zero on
the actual served app's `db.get_db()` connection after readiness, checking
the actual backend URI, database name, schema and connection destination.
Its private eight-field record carries project, fixture label, task, full
reference head, target, window, owned PID and count. The separately reviewed
runner must bind its exact bytes to the served-configuration/readiness/PID/
window provenance chain before Client construction. Shape validation cannot
prove those observations. Do not reuse old permissions/groups/config records.

Use `Client.request_text` to compare the original text without JSON parsing,
whitespace stripping, response export or content-type assertions (MGH-N3).
For example, JSON-encoded `"OK"`, a trailing newline and an error body fail
the exact text comparison. Set the attempt flag before the request; a failed
request is not retried. The client closes in the fixture's `finally`. Failure
messages and summaries contain fixed metadata only.

Sources at the pinned server: `management/views.py:137-153`,
`management/__init__.py:1-5`, `app.py:90-91`, `models/heartbeat.py:166-167`,
`database/backends/postgres/base.py:874-884` and
`database/backends/postgres/utils.py:15-16`. This endpoint has no permission
decorator; this case is not authentication or authorization evidence.
The existing labeled synthetic unscoped-admin fixture is retained.

With nonempty storage, the reference checks at most the first 1000 heartbeats
with no defined order (MGH-N1). Its strict comparison uses
`timedelta.seconds > int(timeout) * 4`: whole days and negative deltas have
remainder behavior (MGH-N2). Those source facts are outside this empty-table
HTTP case. Do not change time or write heartbeats to exercise them here, and
do not infer a behavioral repair or divergence. The plan's nine selected-AST
offline cases use synthetic model, clock and logger stubs; they are not
Flask, database or HTTP results.

From `conformance/`, collect the explicit candidate without sending HTTP:

```sh
python -m pytest -c management-health-pytest.ini tests/http/test_management_health.py --collect-only -q
```

Execution requires fresh exact-head harness and runner reviews, a new matching
signed window ACK and START, and private prerequisites. A separately reviewed
runner owns `ALRUST_TARGET`, `ALRUST_CONFIG_ATTESTATION_FILE`,
`ALRUST_HEALTH_ATTESTATION_FILE`, `ALRUST_HEALTH_WINDOW_ID`,
`ALRUST_HEALTH_SERVER_PID` and `ALRUST_SYNTHETIC_KEYS_FILE`. Keep actual keys
and raw evidence outside Git and mailbox. The permissions window is closed
on both sides; its ACK cannot authorize this case.

No heartbeat mutations, time advance, stale-case HTTP, properties dump,
housekeeping/switchboard call, auth/anonymous/proxy/security/production probe,
manifest/build/status claim, timestamp normalization, Rust parity or
two-server comparison is included. Local evidence and CI_BLOCKED_BILLING
remain separate; owner merge/release/production/auth-contract/protection gates
still apply.
