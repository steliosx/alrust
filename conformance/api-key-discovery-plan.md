# API-key discovery bridge: proposed execution contract

Codex-authored design for independent Claude review, based on exact writer
`adcd2c3dc4f343f701b3abad47be5269884b94c5`. That writer has explicit standalone
offline approval (`cdf00550`); this new design and any implementation require
their own verdict. No profile, fixture, workflow, provisioning or HTTP/SQL
execution is changed by this document. `api-keys` is a candidate for bounded
qualification, not a decision to enable it or all nineteen profiles.

## Source findings, not new runtime evidence

At the base above, `alrust_conformance/api_key_session.py:61–70` creates one
secret and persists it in `owned-api-key-material.json`. That private file
does not feed the Q1 writer and is not a durable history of discoveries.
`_request` at lines 72–87 receives a parsed response, appends fixed metadata,
and checks status before returning to response assertions. The oracle at
lines 89–111 expects the generated secret, but can first encounter a different
returned secret. Existing offline test
`tests/unit/test_api_key_privacy_cleanup.py:45–56` already models that
unexpected returned value and verifies it stays out of a safe summary. It
does not prove durable Q1 capture or registry inclusion.

`client.py:43–51` parses declared JSON before returning `Response`; malformed
JSON can therefore fail before a helper sees the body. The API-key fixture
at `tests/http/test_api_keys.py:12–30` loads role credentials, creates a
client/session, yields it, then closes owned keys. No executing writer is
passed to it. The manifest's `api-keys` entry remains explicitly disabled,
with a four-role credential profile and a test-generated fifth-key exclusion
prerequisite. These are source-derived gaps; no new HTTP request was run.

## Proposed ownership and call order

The executing entrypoint remains the sole process/thread writer. It opens
the actual entrypoint StageJournal and PrivateSecretWriter before provisioning
or test discovery. An explicitly registered pytest plugin instance passes
that same writer object through a required capture fixture into the selected
API-key session. No environment variable, automatic mailbox command, fallback
global or file path chooses a writer. Missing/wrong/closed/foreign-thread
capture refuses before credential use. Existing standalone local test use
must remain explicitly distinguished from the future executing adapter.

The reviewed provisioner records every generated role secret before supplying
it to the reference command, and every returned secret before using it. Its
private output and actual stored customer/scope proof remain separate Claude
implementation prerequisites. Credential loading must validate that the
supplied role values belong to this same provisioning generation; merely
reading and capturing a credential file cannot manufacture that proof.

The API-key helper records its generated value immediately after generation,
before local persistence or any HTTP request. For its known response shapes,
the proposed capture adapter records string secrets in create `key` and
`data.key`, and detail/update `key.key`, before status/shape/entity assertions
or fixed-summary production. A differing returned key must be retained even
when the oracle subsequently fails. Repeated values deduplicate in the writer;
deleted/revoked values remain in its history. The current oracle, customer,
scope and expiry expectations do not change.

Entity `href` is another supported secret-bearing field: create `data.href`
and detail/update `key.href`. After capturing the string `key` of each
entity, the capture adapter must require its `href` to equal exactly
`client.base_url + '/key/' + entity.key`, before any ordinary test assertion
or summary. This is a capture-time consistency check, not a new API oracle.
A missing, non-string or divergent href fails the execution stage immediately
and prevents COMPLETE and normal export; its private response is retained.
The adapter does not claim to capture an unknown secret from a divergent
href. A dedicated divergent-href control must prove stage failure before
assertions, no later request and no completed snapshot or normal export.

These declared fields are not a claim to discover arbitrary secrets in every
response. Unexpected shapes, unsupported secret-bearing fields, malformed
JSON, transport failures and oversize bodies fail the execution stage and
prevent normal completion/export. Raw response/exception output stays private.
An adapter cannot convert a failed test or failed capture into an empty or
COMPLETE inventory. Its bounded parsing/capture and failure-only exclusion
path need exact-source review before execution. Do not log credentials to
recover a missed capture.

Malformed declared JSON can contain a server-generated secret that the
adapter never sees. The requested key is already captured; a later stored-key
query may recover surviving keys, but cannot recover revoked history. This
residual never becomes completion evidence: the malformed response fails the
stage, keeps raw bytes private and blocks the normal inventory/export path.

All fixture cleanup and client closure must occur before writer.finish().
The executor requires a successful selected pytest result, no unfinished
capture work and independently qualified child/writer exit before finishing
the inventory and closing the entrypoint. A catch around pytest or a callback
must never suppress the failure state. Ctrl-C/SystemExit retain their original
identity under the reviewed writer. File existence and pytest fixture teardown
alone do not prove generation quiescence.

Profile completeness is an executor/writer obligation before finish and
entrypoint COMPLETE. The reviewed `hosted-profiles.json` declares `api-keys`
as `four-role-synthetic`: require all four distinct actual provisioned role
keys, with their independently verified stored customer/scope/validity and
generation binding, plus the captured session-generated key and all later
discoveries. Match actual required values in writer history, not merely a
minimum count or credential-file metadata. The reader may stay profile-agnostic
only when consuming that qualified closed-stage result; the combined executor
and writer/reader handoff still need their own review. An empty inventory is
not approved for this profile. No profile is enabled by this design.

Claude's separately reviewed reader then gates on stop/quiescence, closed
entrypoint and exact inventory identity/digest/core, checks actual value-set
inclusion in the bounded registry union, and authors the digest-linked count
in the registry stage. This document grants no query, destruction or export
authority and changes no finalizer contract.

## Bounded next verification

Start with a standalone capture adapter and explicitly injected stub client,
without enabling the HTTP fixture. Before import, classify actual dependencies
and hooks. Controls must demonstrate durable-before-request ordering,
unexpected returned keys retained before assertions, duplicate/revoked history,
capture/fsync failure preventing further requests and completion, malformed
response refusal, and original interrupts preserved through cleanup. Privacy
controls cover divergent href, missing required role/session values, reflected
errors and raw output; summary counts alone are
insufficient. A composed local writer/journal test must verify actual snapshot
bytes and absence of completion on failure.

Only after fresh adapter/plugin/executor and Claude reader review may a
source-bound window propose one real profile, with actual stored-role proof,
service-log exclusion, bounded runtime, cleanup and separately scanned evidence.
Do not use a scaffold PASS, mock response, or this design as that qualification.
Full Phase 0, Rust parity and shared-VM qualification remain separate gates;
ARM64 remains deferred. Missing production PIN inputs do not block this
independent synthetic source work.
