# Standalone bounded captured-flow client

Codex-owned conformance component, based on the approved standalone hook
`bfbc2bd68884ff678bdee5f2f49c0b31bc5b1758` and Claude scope concurrence
`e06dc383`. That concurrence is a design-scope answer, not a source verdict.
The new exact source needs Claude independent review. No fixture, profile,
workflow, ordinary Client, security_request, reference or server code changes.

`BoundedCaptureClient` inherits the ordinary persistent HTTP client: Key
identity, fixed synthetic request-ID admission, JSON request encoding,
cookies, target/path rules, disabled redirects/environment trust and content
media-type/charset behavior are retained. Captured flows alone advertise
`Accept-Encoding: identity` and refuse nonidentity Content-Encoding before
body iteration. This is a harness transport difference: gzip/deflate/br/zstd
and compressed reference responses are NOT exercised or qualified here.

Explicit per-response byte cap and one shared `RawBudget` bound retained raw
body bytes across responses, and response count bounds empty allocations.
Actual streamed bytes count, irrespective of absent or false Content-Length.
Only the bounded prefix is retained on overflow; overflow fails, never returns
a truncated success. Byte charges precede sink append and are never refunded
on errors. Client and shared budget are permanently poisoned on any failure;
later requests refuse before HTTP. Budget is one process/thread owner. The
executor must independently bind its one job budget; constructing another
budget is not a recovery path. No limit is selected from environment/defaults.

The `PrivateResponseSink` abstract interface is a required explicit dependency,
NOT an implemented private-file sink or proof of custody. The future concrete
sink must bind the same job/root/core/actual open entrypoint stage and writer,
validate process/thread ownership and placement, allocate exclusive fixed
0600 regular single-link files through held private root identity, record
owned_resource before the first data write, fsync/close final bytes and poison
writer/entrypoint on any failure. Its raw bytes must never be export candidates;
failed raw bytes stay private with cleanup UNPROVEN. No automatic callback,
file-path fallback or no-op sink may execute a profile. Offline tests use only
an explicit in-memory sink and streaming stub; they do not qualify those
filesystem obligations. This standalone head implements just the streaming
boundary so its correctness can be reviewed independently of custody.

Bounded raw bytes are durably handed to sink.finish BEFORE JSON/text parsing.
Malformed JSON, coding refusal, stream/persistence/deadline failures use fixed
diagnostics and poison completion. Primary BaseException interrupts retain
identity. Raw responses/exceptions/tracebacks and response headers/cookies remain
private, and arbitrary header/unknown secret discovery is not claimed.

The explicit monotonic deadline is checked before HTTP, on headers/chunks and
before/after parsing. These are cooperative checks. They cannot interrupt a
blocked read/header/decoder; inherited 10s HTTP timeouts are per I/O. Claude's
reviewed supervisor absolute deadline and bounded teardown remain a hard
prerequisite, including process custody before secrets. Body limits are not
claims about total RSS, header/parser allocation, arbitrary caller transports
or request payload memory. No live use or full-job boundedness is approved.

Offline controls cover JSON/+json/plain/charset and raw-text behavior, ordinary
cookies, unchanged authorization/JSON requests, identity/no decoder, exact and
over-limit bodies (single/partial/large chunks), lying Content-Length,
per-job exhaustion/empty-response count, permanent shared poison, sink failure
before parsing, malformed JSON raw retention, response closure/interrupts and
expired deadline before HTTP. Still NOT_RUN: concrete filesystem sink, joined
capture/writer/session integration, plugin/executor requirement, hard deadline,
actual HTTP/SQL, all profiles, full Phase 0, Rust parity and compression. ARM64
remains deferred. No heavy window or publication implied by this component.
