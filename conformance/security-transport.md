# Isolated security-case transport

`alrust_conformance.security_transport.security_request` is the transport
component of the [approved fixture plan](security-fix-fixtures.md). It has no
CLI, case executor, credential collector or runner admission logic. All tests
use `httpx.MockTransport`; **reference HTTP and alrust HTTP remain NOT_RUN**.

Every call requires an explicit identity: `Identity.ANONYMOUS` or a
`SyntheticKey` with the existing harness format (`SYNTHETIC-` plus 48 lowercase
hex digits). A missing key, `None`, an empty string or the word `anonymous`
is rejected before transport construction. This prevents missing fixture
credentials from silently selecting anonymous access. A key's format does not
prove its installed identity, scopes, lifetime or authorization; the future
runner must attest those independently. Keys are omitted from object reprs.

A fresh httpx client per call uses `trust_env=False`, no redirect following,
a ten-second timeout and no inherited sessions/cookies. Response cookies are
discarded with that client. Environment API keys, netrc and proxy credentials
are not used. Response parsing retains the existing `Response` representation:
JSON for declared JSON media types, original text otherwise. JSON and transport
errors propagate; neither becomes a successful observation. The client closes
even when parsing or transport fails. Raw response bodies remain unredacted
for assertions and comparison; later evidence export must redact them.

The only header switch is the boolean `cron_headers=True`, sending exactly
`X-Appengine-Cron: true` and `X-Forwarded-For: 0.1.0.1`, the synthetic SF-CRON
pair. Explicit synthetic authorization can accompany it for precedence controls.
There is no arbitrary-header, cookie, auth callback or shared-session parameter.
The existing shared `Client` and its unknown-webhook request-id contract are
unchanged. A supplied mock transport is owned and closed by that call; callers
must not pass a transport carrying another session's authentication policy.

The future executor must enforce reviewed prerequisites, effective profile and
credential attestation, shared exclusion, execution permission, bounded total
lifetime, private incremental evidence and owned cleanup before any live call.
This function is not an admission gate. It does not assert a status/envelope,
state change or absence of writes. The reviewed AN1 auth-disabled control and
exact 401 ApiError assertions remain required in the forthcoming case vectors.
Pending reference predicates must remain explicitly unqualified; transport
unit tests do not qualify them or any full conformance gate.
