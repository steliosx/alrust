# Explicit API-key session capture hook

`CapturedApiKeySession` is a Codex-authored standalone source candidate. It
subclasses the ordinary `ApiKeySession`; it enables no HTTP fixture, profile,
manifest, workflow or executor. The legacy standalone fixture remains on
`ApiKeySession`. Executing profiles must explicitly require this hook after
their separate qualification, with no environment-selected writer or fallback.

The caller supplies the actual client, actual `ApiKeyCapture`, private job-root
string and independently pinned core SHA-256. Before construction, persistence,
ordinary operations and cleanup, public checked interfaces verify the same
client object/base and live writer/root/core custody. The writer rereads the
core and verifies its active entrypoint header. This couples the session to
the collecting writer generation; it does **not** prove child exit, server
generation, role coverage or hostile same-UID isolation.

Generated secret bytes are durably recorded before any request or other use.
Captured mode creates no `owned-api-key-material.json`: that legacy standalone
runner file would be an unrecorded secret-bearing resource. The writer's events
and snapshot files are the authoritative private discoveries and already have
owned-resource records for finalizer removal. The job root is already allocated
by the executor; the existing absolute/0700/outside-Git/mailbox placement checks
are retained through a shared validation helper, without creating a duplicate
file. Legacy `ApiKeySession` still creates its exclusive material file for its
separate standalone runner. The caller owns client closure if the
constructor fails, including interrupts. Raw values, files, responses, refusal
records and test tracebacks are private; summaries are not discovery evidence.

The base session gained only two delegating hooks: `_persist_material` and
`_send_request`. Its existing predicates and owned-ID whitelist are unchanged.
Create, read, update, delete and summary execute inside capture's oracle scope.
Any caught constructor, durable-capture, request or ordinary oracle failure latches
the writer failure and prevents COMPLETE. Declared shape/status selection is
made before the client runs: create/201, read and update/200, ordinary delete/200,
absence GET/404, cleanup DELETE/200-or-404. Response outcomes never select a
weaker shape. All original scope, customer, user, text, expiry, count and entity
predicates continue to apply.

`close()` checks custody even if no IDs remain. Unpoisoned close uses the
unchanged owned-ID cleanup oracle and closes the actual client. After poison or
failed binding it refuses HTTP teardown and still attempts to close the
originally injected client, preserving the primary refusal/interrupt if closure
also fails. There is no failure-only HTTP bypass. A later failure within normal
cleanup poisons the writer; subsequent requests stay refused. This is **not**
proof that remote API keys or the database were removed.

Signed design concurrence `20261005T203954Z-claude-alrust-9bed0f2e` selected
executor-owned cleanup (option B). It also disclosed that today's executor
requires an all-COMPLETE stage prefix and therefore refuses database drop after
a FAILED entrypoint. A separately reviewed failed-entrypoint drop design/source
and runtime evidence are prerequisites before executing this composition.
Do not count refused HTTP cleanup or container disposal as database-absence
proof; no registry, inventory completion or export may follow this failure.

Other prerequisites remain: reviewed bounded raw transport before JSON parsing;
actual four-role/session discovery and stored scope/customer/validity proof;
client/child/server generation qualification; writer finish and exit; combined
reader/finalizer/service-log exclusion. They are not supplied by this hook.
Offline controls use real temporary private files/journals and synthetic stubs.
Live HTTP/SQL, full profiles, Rust parity and these integrations remain NOT_RUN.
No resource window or hosted gate credit follows from local unit success.
