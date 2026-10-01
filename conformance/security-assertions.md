# Security response assertions

`security_assertions.check_auth_refusal` implements the response predicates for
the exact SF-AN1/SF-CRON 401 and their limited-key 403 control in
`spec/security-fixes.md` (approved at `9acc9b41`, unchanged through base
`3a60dbee`). The reference envelope is pinned `exceptions.py:98–107`;
`BaseError` defaults `errors` to null. The trace is a nonempty string generated
or forwarded by `utils/tracing.py:19–25`.

The expected refusal must come from the reviewed vector and attested settings,
not from the HTTP response. Reference dispatch is not necessarily HTTP success:
the handler still needs its own qualified response and state assertions. Do not
use this helper to turn a dispatch case into an expected refusal. Every executed
side is checked independently; equal unexpected failures cannot pass parity.

An empty tuple means only that the response predicates match. The helper neither
starts a case nor records an overall pass. The existing evidence state still
requires state, audit, cleanup and comparison assertions. This helper does not
check content type or trace-header correspondence because `Response` retains
only status and body. It does not replace those future executor checks.

Failure labels contain no response data. Preserve raw synthetic evidence in the
private execution record and compare before export redaction. Request ids vary
between servers; they must be present but are not compared across responses.

Validation is offline only. No executor, fixture collector, admission gate, HTTP
run or automated integration was added. Shared exclusion, execution permission,
effective fixture attestation and bounded cleanup remain prerequisites for any
later run. Live security cases and absent candidate outcomes remain **NOT_RUN**.
