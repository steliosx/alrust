# Stored-state assertions

This offline component implements exact snapshot comparison for anonymous
SF-AN1/SF-CRON refusals and restoration after owned cleanup. It follows the
common no-side-effect definition in `spec/security-fixes.md` and the approved
[fixture plan](security-fix-fixtures.md). The prior response helper checks the
401 envelope; this component checks a separate stored-state predicate.

`StateScope` binds a case, unique attempt id, server side, fixture-attestation digest, purpose and
nonempty collection inventory. The future reviewed vector/collector supplies
that inventory and trusted digest independently of the observations. The digest
must identify the effective profile and lifecycle actually used by the case;
the helper does not authenticate it or refresh the fixture attestation.
Allocate a new attempt id before each execution, even when repeating a case in
the same fixture lifecycle; never reuse a previous observation as a new attempt.

`StoredSnapshot` freezes a complete `collection -> record identity -> row`
mapping when constructed. Every named collection must be present, even if empty;
unknown collections are rejected. Row identities must be unique in the mapping;
the future collector must detect duplicates before constructing a dictionary.
It must include all relevant records and fields through privileged observation,
not filtered reads with the identity under test. Missing or unreadable data must
stay missing, never be replaced by an empty collection. An explicit empty table
is valid only when the trusted collector has actually observed it as empty.

The collector must use a reviewed lossless JSON encoding of database types.
Datetime, bytes, tuples, non-string object keys, custom objects, nonfinite floats
and cyclic values are rejected rather than silently stringified or coerced.
Object/record ordering is immaterial; arrays, null versus absent, and JSON scalar
types remain significant. Capture immediately: changing the collector's original
mutable object later cannot rewrite a snapshot. Stored values remain private;
the object representation and assertion failures do not expose them.

For `ANONYMOUS_REFUSAL`, capture the baseline after fixture setup and before the
request; capture the observation after it completes. No field is projected away,
including timestamps, audit-related fields or API-key usage. Credentialed refusal
cases are **not covered**: the allowed reference pre-refusal key-usage write
requires its own exact, reviewed predicate. Do not omit key rows or fields to
force equality here.

For `OWNED_CLEANUP`, capture a separate baseline **before fixture setup**, then
observe after teardown. Use a distinct cleanup scope; a refusal baseline cannot
pass as a cleanup baseline. Exact restoration of the named inventory is only
one cleanup predicate: resource/process/port cleanup, collector completeness,
and cleanup of objects outside that inventory remain unimplemented.

Equal snapshots prove only equality of observed final state, not absence of a
write followed by a compensating write. Audit/confirmation observers and any
necessary write observation are still required by the approved contract. This
component supplies no collector, executor, admission gate, live database access,
automatic evidence transition or comparison between different servers. Caller
code must record each checked stage through `CaseEvidence`; state equality alone
does not earn a case pass. A cleanup failure remains FAIL and blocks reuse under
the fixture plan.

Validation is offline synthetic unit testing only. Security HTTP, shared-VM
execution, pending reference qualification and candidate behavior remain
**NOT_RUN**. Exact-source peer approval is still needed before adoption.
