# Security-fix fixture plan

This plan adopts the contracts in `spec/security-fixes.md` approved at
`9acc9b41c930c5bf4473c2a958ce482bb49d5922` (landed through PR #40).
It defines prerequisites for the SF-AN1, SF-CRON and SF-USERS harness work.
**Executor: NOT_IMPLEMENTED. Reference HTTP: NOT_RUN. Alrust: NOT_RUN.**
It changes no existing comparison policy or gate.

## Separate security cases from alert replay

The existing differential runner generates alert-create sequences, expects
201 from both sides, and owns alert cleanup. Its receive-clock projection
is specific to those sequences. Security cases need their own case runner:
expected refusals cannot simply be made successful in the existing runner.

Each security case must identify its spec entry, configuration profile,
credential choice, exact request, per-side assertions, expected state change
or absence of change, cleanup, and reference-qualification state. Parity
cases assert each side's expected behavior before comparing them. Two equal
unexpected errors are a failure. Intended divergences must assert both
specified outcomes; they are not ignored differences.

Use the existing provenance, private-report reservation, incremental evidence
and cleanup principles. Record the spec commit, harness commit and dirty state,
server identities, fixture/configuration attestation digest, case identifiers,
actual responses, side-effect observations and cleanup result. Retain failures.
Compare before redacting synthetic credential echoes in exported evidence.
Do not put credentials or real operator settings in repository fixtures.

## Admission prerequisites

Before any HTTP request, require the reviewed runner and execution window,
shared exclusion, bounded lifetime, private evidence destination, and a
separate disposable database/workspace for each server. An approved spec or
this plan grants no execution or runner-registration permission.

An operator label or a requested configuration is insufficient evidence of
what is running. The installed fixture runner must attest the effective
settings and stored synthetic credentials/roles immediately before use.
Bind that record to the same server lifecycle used by the case. A missing,
unreadable, stale or contradictory attestation produces NOT_RUN before
requests; it must not select a permissive default configuration.

The minimum configuration families are:

- AN1: `AUTH_REQUIRED=True`, `ALLOW_READONLY=True`, with separate effective
  `READONLY_SCOPES` values `['read']`, `[]`, `['read:alerts']` and `['write']`.
- CRON: required auth with readonly disabled; also the readonly-enabled and
  auth-disabled precedence controls specified in the approved contract.
- USERS: required auth, basic provider, synthetic roles/permissions and users,
  default role mappings, plus the non-basic-provider and synthetic
  `ADMIN_USERS`-email controls. Insert-failure qualification uses a duplicate
  synthetic id, a distinct unique login/email, `EMAIL_VERIFICATION=False`
  and the reference's default audit configuration.

Record all other settings that affect these cases, including `DELETE_SCOPES`,
`AUTH_PROXY`, `ADMIN_ROLES`, default role scopes and allowed email domains.
No profile may be inferred from a route's HTTP status.

## Credentials and side effects

The ordinary pytest `target` fixture inherits `ALRUST_API_KEY`. Anonymous
security cases must construct an explicitly credential-free client and must
not inherit that variable, cookies, proxy credentials or previous sessions.
Credentialed positive/negative controls use separately attested synthetic
keys with the exact required scopes. Missing fixture credentials fail
preflight rather than accidentally becoming an anonymous case.

The current Client intentionally accepts no arbitrary headers. CRON cases
need a separately reviewed, narrowly scoped way to send the fixed synthetic
cron-header pair. Do not silently broaden the shared Client header policy or
reuse the unknown-webhook request-id exception for this purpose.

Seed owned users/alerts and take state snapshots through the privileged
fixture mechanism, independently of the credential being tested. After a
refusal, compare the relevant stored records and observe success-audit and
confirmation channels. A 400/401/403 alone does not prove no mutation.
The approved contract allows only the reference's own pre-refusal API-key
usage update; do not project away unrelated writes. Cleanup uses the fixture
identity, not the anonymous/limited identity under test. A cleanup failure
is FAIL, preserves evidence and blocks reuse of the fixture window.

## Pending reference outcomes stay visible

The malformed PUT vectors explicitly marked reference-pending in the approved
spec have no qualified differential oracle yet. Give each exact input/profile
its own identifier. Assert the specified alrust result when an alrust server
exists, but report that vector's differential comparison as
SKIPPED_REFERENCE_UNQUALIFIED. It contributes no differential pass and cannot
make a full conformance gate green. Never skip the whole endpoint, spec entry
or all 400 responses.

For `POST /user` with `password: null` or `password: 5`, retain the expected
400 and error-envelope assertions on both sides. Only the exact reference
exception text remains unqualified; alrust's specified message is asserted.
Do not suppress the status/envelope comparison along with the pending text.

With no alrust implementation, candidate assertions are NOT_RUN, never PASS.
Collection, mocked transports and offline report tests are harness evidence
only. A later approved execution must name exactly which vectors actually
ran, their results and skips, and all cleanup observations.

## Implementation order

1. Add profile/credential validation and per-case private evidence state, with
   offline tests for missing/stale prerequisites and incomplete results.
2. Add explicit anonymous client construction and fixed CRON headers, reviewed
   with transport-capture tests proving that credentials cannot leak into
   anonymous cases and that the header allowance stays bounded.
3. Add the approved request/state assertions and owned cleanup. Get Claude's
   exact-commit review before executing the security HTTP cases.
4. Qualify the reference-pending outcomes under a fresh authorized window;
   update only the named case predicates with retained evidence and review.

Phase B provisioning and the pending unknown-webhook reference run retain
their existing priority. This plan creates no additional runner-access request.
