# Standalone API-key capture component

This Codex-authored component implements part of the reviewed bridge design
at `937c511b1b495faff6ee2ddd6060a69b5c36b933`. It needs its own independent
exact-head source review. It changes no HTTP fixture, manifest, workflow,
reference code or API oracle, and enables no profile.

`ApiKeyCapture` requires an explicitly supplied actual `PrivateSecretWriter`
and client object. It constructs neither. Before each call it checks writer
ownership, active entrypoint stage, collecting lifetime and current private
root/event custody. The added public writer methods `check_collecting()` and
`abort()` avoid adapter access to the writer's internal state. `abort()`
permanently refuses completion; raw failures and all discovery bytes remain
private. This is not a hostile same-UID security boundary.

The future caller must invoke `generated(value)` before persistence or use.
Create requests refuse unless their supplied key was registered through that
method. Known returned create `key`/`data.key` and detail/update `key.key`
values are retained before shape/status checks or the ordinary oracle.
Entity `href` must equal the client's exact base plus `/key/` and the same
captured entity key. Divergent, missing or non-string href fails before any
ordinary entity assertion, and does not claim to recover an unknown href
secret. Percent-encoded href also fails closed; that compatibility edge is
explicitly covered in offline controls.

Only declared create, entity, deleted, absent and cleanup shapes are admitted,
with explicit expected statuses and UUID-addressed key endpoints. A path
whose UUID equals any generated or response-captured secret is refused before
the injected client is called; UUID syntax alone cannot prove an identifier
is not a secret. Unknown credential ownership still belongs to the caller.
Absent responses require HTTP 404 and the same pinned five-field envelope
used by the existing security predicates: `status`, `message`, `code`,
`errors`, `requestId`. The API-key not-found path has `errors: null` and a
nonempty request ID; three-field synthetic responses are insufficient.
This is source-derived from pinned Alerta `f4312727783dad64198b62ed778bbca98a5144a3`
at `alerta/views/keys.py:71`, `:162`, `alerta/exceptions.py:98` and
`alerta/utils/tracing.py:18`; it is not a new reference HTTP run.

The future `ApiKeySession.close()` bridge must declare `shape="cleanup"`
and `expected=(200, 404)`. That shape validates the actual response status
and accepts only the successful DELETE body at 200 or the standard ApiError
not-found envelope at 404. It sends exactly one request, with no retry or
adapter bypass. Ordinary `delete()` retains the strict `deleted` shape and
200 expectation, followed by the `absent` GET and 404 expectation.
The current session/fixture is unchanged and is not connected yet.

The unchanged
oracle must execute inside `oracle_scope()` so an oracle failure poisons
completion even when caught outside the scope. Primary interrupts retain
their identity; secondary abort failures cannot replace them. Transport or
JSON-parse exceptions use fixed private diagnostics and block completion.
Original interrupt/test tracebacks are still private evidence, not safe
mailbox summaries.

The component does not enforce credential ownership or profile completeness
by itself. UUID ownership and all customer/scope/expiry predicates remain
with the unchanged API-key session oracle. The future executor must bind the
writer/client/session to one generation, register the session-generated key
before the existing constructor persists it, require all actual provisioned
role and session values with stored-metadata proof, close all test/client
writers, then separately qualify their exit before finishing the inventory.
The current HTTP fixture is deliberately not wired to this component.

Response-size enforcement before JSON decoding and retention of raw parse
failures require a separately reviewed bounded transport. An arbitrary
injected client does not establish either prerequisite. Failure after a
server committed a key cannot establish complete discovery or normal export;
stored-key queries cannot recover revoked history. No live-client permission
or new cleanup/destruction authority follows from these source controls.

Focused tests use an explicitly injected stub client, real private temporary
writer/journal files, synthetic keys, injected failures and one joined Python
thread. They check durable-before-request ordering, unchanged entity oracle,
unexpected/revoked history, divergent href, capture/interrupt refusals,
wrong writer custody and absence of COMPLETE after failure. They prove
standalone offline behavior only. Plugin/executor/provisioner integration,
the combined reader (`claude/ci-db-q1-reader`), real HTTP/SQL/service-log
exclusion and profile qualification remain `NOT_RUN` and need fresh review
and applicable signed resource admission. ARM64 remains deferred.
