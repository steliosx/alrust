# Security fixture checks and evidence states

This is the first offline component of the [approved fixture plan](security-fix-fixtures.md).
It implements no HTTP executor, runner admission, attestation collector or case
oracle. **Security HTTP, shared-VM execution and reference qualification: NOT_RUN.**
Existing clients, comparisons, HTTP vectors and execution gates are unchanged.

`alrust_conformance.security_evidence.check_fixture` compares an effective
fixture observation with a complete reviewed expectation. Both name the profile,
server commit, server lifecycle, disposable database and workspace. Required
settings include auth/readonly/proxy policy, role names and default scopes,
admin users, email domains and verification. Additional settings are compared
exactly as supplied; each case must include all settings affecting its behavior.
Credentials contain synthetic key fingerprints and effective scopes/roles,
never raw keys. Anonymous profiles explicitly name an empty credential map.
Missing, stale, future, malformed or contradictory observations return NOT_RUN
without echoing rejected values. Exact matches yield VALIDATED_OFFLINE and a
canonical observation digest. They never grant execution permission.

The future trusted fixture collector must attest what is actually installed,
including credentials, rather than copying a requested profile. Its origin and
lifecycle binding still need review. No file loader or environment fallback is
provided. An unreadable observation must reach this checker as missing data,
not a cached success. The caller must recheck freshness immediately before use
and enforce execution permission, shared exclusion, reviewed runner identity,
bounded lifetime, independent fixture stores and cleanup. A JSON digest alone
proves none of those prerequisites.

`CaseEvidence` tracks each exact vector separately. Each side starts NOT_RUN;
starting it makes it INCOMPLETE. Response, stored-state, audit/confirmation and
cleanup assertions must all pass before a qualified comparison can earn
LOCAL_PASS. Equal unexpected errors cannot pass through response equality
alone: the future executor must assert each side's expected response first.
Assertion/comparison results cannot be overwritten. Any recorded failure,
including cleanup, keeps the case FAIL. Pending reference oracles always have
SKIPPED_REFERENCE_UNQUALIFIED differential status unless an assertion fails;
per-side NOT_RUN/INCOMPLETE states remain visible and no pass is awarded.
Known assertions for partially qualified outcomes must still be evaluated.

`PrivateJournal` reserves a new 0600 JSONL file in an existing caller-owned
private directory and fsyncs every incremental record. Existing paths and
symlinks are refused. Records are monotonically numbered. An I/O error closes
the journal; retained partial evidence cannot be followed by a success record.
A later reader must reject partial trailing records and incomplete sequences.
This component has no reader or automatic gate aggregation yet. Callers must
write initial NOT_RUN evidence and provenance before effects, then all state
transitions. Journal inputs are secret-free metadata and assertion summaries;
raw response comparison/redaction belongs to the later executor.

Next components remain separate reviews: explicit anonymous transport and
bounded cron headers; reviewed profile/vector catalog including AN1's
AUTH_REQUIRED=False control; exact 401 ApiError assertions; privileged state
and audit observers; owned cleanup; authenticated collector/runner integration;
and fresh-window reference qualification. These helpers cannot substitute for
those components or justify execution. Offline unit tests are harness evidence
only, including tests that exercise the LOCAL_PASS state with synthetic inputs.
