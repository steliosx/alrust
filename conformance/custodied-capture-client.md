# Standalone client with concrete response custody

This new component pairs bounded streaming with the independently reviewed
private file sink at `62b189ad58c1491f2ba8c255e57bd2cd9d3c52e7`. The earlier
client and sink remain separate approved historical sources; their verdicts
do not approve this changed head. No existing fixture, API-key capture/session,
oracle, ordinary client, executor or workflow is activated or changed.

`CustodiedCaptureClient` takes one exact actual `PrivateRawResponseSink` and
an explicit absolute monotonic deadline. The sink was explicitly constructed
with the actual writer, stage, frozen core, job root, per-response cap and fresh
per-job budget. It admits one exact client generation, refuses a second, and
checks that generation before requests. Its existing checks bind the current
actual writer/stage/core/process/thread/held root/file/budget on each action.
Executor composition must independently verify that the capture/session and
sink use that same actual writer/core and that all sources/roles are complete.
This standalone transport pairing does not establish that future composition.

The sink alone performs `begin()` response-count charging and
`reserve_and_append()` byte charging/writing. The client never reads or writes
budget counters or charges a second time. It slices each raw chunk using the
sink's read-only `remaining()`, retains only the bounded prefix before an
overflow failure, and buffers at most that actual per-response/per-job cap.
Content-Length is untrusted. Identity coding is advertised and any nonidentity
or malformed Content-Encoding is refused before body iteration. Compression
compatibility is not qualified by these captured flows.

Private ownership and directory durability precede HTTP. File fsync, identity
recheck and close precede JSON/text parsing and oracle return. Malformed JSON,
transport/close failures, deadline expiry and prerequisite failures poison the
actual sink, budget and writer; no completion or fallback follows. Raw body
files stay private; public journals carry only existing ownership metadata.
If body consumption/parsing already failed, a secondary stream-close failure
cannot replace the original exception or interrupt. Fixed diagnostics replace
ordinary exception details. Original non-Exception BaseExceptions propagate.

Ordinary persistent Key headers, cookie jar, fixed synthetic request ID,
media-type/charset decoding and redirect refusal come from unchanged `Client`.
Explicit client close shuts the HTTP client and clean idle sink before the
caller may explicitly finish the private writer. Context exit preserves the
body's primary failure even if close fails. No constructor obtains credentials
or selects a budget, writer or file path from the environment.

Monotonic checks are **cooperative**. The inherited ten-second HTTP I/O timeout
is not a whole-tree hard deadline. Before real execution, the approved
supervisor/execution plan must enforce a same-boot hard deadline D with the
client deadline at or before D, source-bound positive generation exit and all
applicable holder proofs. Missing proof never upgrades profile execution or
cleanup. Fixture/session discovery coverage, normal/failure finalizer, R-EP,
registry/database integration, live HTTP/SQL, all hosted profiles, full Phase 0
and Rust parity remain NOT_RUN for this source.

Offline controls use actual private temporary files and journals with labeled
synthetic bytes plus in-process httpx MockTransport. They exercise original
decoding/identity/cookies/redirects, durable close before parse, exactly-once
byte/count charging, cap overflow and exhausted budgets before another HTTP
invocation, refused coding, malformed JSON, interrupted streaming and secondary
close failures, changed prerequisites and one client per sink. There is no
socket, server, SQL, child generation, service, shared VM or microVM job.
