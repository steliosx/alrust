# Captured API-key flow construction

`make_captured_api_key_session` is a standalone construction function. No
fixture, profile, workflow or executor selects it yet. It creates a fresh
budget, concrete private raw sink, bounded client, API-key capture and session
from one actual already-entered writer/stage and explicit job/core pins.
It does not accept separately constructed interchangeable components.

The caller supplies byte/count limits and a future monotonic deadline.
Constructor checks refuse invalid bindings before new session material or
HTTP. A supplied authentication key is durably remembered before putting it
in the HTTP client; it is not added to the session-generated set. An omitted
key remains omitted. Credentials must satisfy the existing private inventory
codec bounds. No environment credential, implicit key or fallback is read.

The ordinary session still owns its exact endpoint/status/entity assertions,
UUID whitelist and cleanup. Its generated key is captured before first use;
returned keys are captured before oracle assertions. The concrete client
durably closes each bounded raw response before parsing it. Streaming
transports are required: pre-read `httpx.Response(json=...)` test doubles
refuse. Tests use streaming `MockTransport` and synthetic credentials only.

Failure poisons the writer and retains private partial evidence. Constructor
cleanup closes allocated client/sink handles without HTTP or unlinking.
Secondary cleanup failures cannot replace a primary interrupt; ordinary
constructor errors become a fixed diagnostic. The caller must propagate
failure. Successful session close closes the client and sink; writer finish
and stage closure are separate explicit actions. A second construction on
the same writer refuses rather than replacing/resetting its sink or budget.

This is accident-resistant construction, not a hostile same-process/UID
boundary. It does not change the independently reviewed session's general
manual-injection interface or claim manual arbitrary assembly is safe.
Future executor activation must require this exact reviewed construction,
prove all four provisioned roles and every historical discovery were saved,
and bind the deadline to its supervised credential-holder generation. It
must prove successful tests/capture/role completeness, explicit sink/writer
closure, positive entrypoint exit and whole-tree quiescence before registry,
database cleanup or finalization. R-EP runtime, failed-entrypoint handling,
unaccounted root entries, live HTTP/SQL, full Phase 0 and Rust parity remain
separate prerequisites. Offline construction tests qualify none of them.
