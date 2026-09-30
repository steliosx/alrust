# Ordinary owned API-key conformance

Source: read-only Alerta `f4312727783dad64198b62ed778bbca98a5144a3`.
Codex is this harness's author; Claude independently reviews each exact head.
Source-only plan `8751435b` (SHA-256
`12f8c0e911467a325ae9b4edbb916fdf1f050debe05e7d0eab652cb5a177282f`)
received APPROVE_WITH_COMMENTS `103be36b`; KP-C4 was withdrawn in `71bfe389`.
Harness/runner/window approvals remain separate. HTTP is **NOT_RUN** here.

One ordinary UUID-owned create/detail/update/delete case uses the existing
private, valid synthetic unscoped admin fixture under AUTH_REQUIRED=False and
CUSTOMER_VIEWS=False. The generated key is never used as a credential. It has
explicit user `alrust-synthetic-admin`, non-empty `read:alerts` scopes, null
customer and expiry `2099-01-01T00:00:00.000Z`. The test requires a clock before
that expiry before effects. Create expects active/read-only/count0/null
lastUsedTime and exact original fields. Details use the UUID, not a secret path.
Update repeats scopes, null customer and explicit expiry, changing only text;
delete returns status-only, and the later owned UUID detail gives404.

Citations: models/key.py:24-77,99-124,151-159; utils/key.py:30-34;
views/keys.py:18-73,116-168; postgres/base.py:939-953,979-1000,1010-1016.
Source-derived expectations are distinct from runtime evidence. No field,
timestamp, null, scope, count or response value is normalized. Original bodies
are compared in memory. No production input or private signing key is needed.

The requested UUID enters the owned cleanup set before POST, including when
response JSON parsing or transport fails after a committed create. Cleanup
attempts every owned UUID and closes the client even after an earlier failure.
It accepts404 as absence; only own freshly supplied UUIDs may be deleted.
Failed ownership/cleanup remains FAIL, never an automatic retry or green result.

## Evidence containment

KP-C1: each proposed shared artifact must pass both known-key exclusion
(the four private fixture keys plus this generated key) and the generic
`SYNTHETIC-[0-9a-f]{48}` pattern check. This includes summaries, release notes
and diagnostics, not just a four-key HTTP-log check. The helper emits fixed
failure messages without rejected bytes. The future runner must actually apply
these checks; a helper's presence does not prove runner enforcement.

The key is generated inside the test process and persisted only in
`owned-api-key-material.json`,0600 under the runner-created resolved0700
`ALRUST_PRIVATE_EVIDENCE_DIR`, outside Git/mailboxes. Existing files are never
overwritten. Git metadata files/symlinks/populated directories are rejected;
an empty sandbox `.git` placeholder alone is not a checkout (confirmed with
`git rev-parse` for the own evidence root and `/tmp`). This is an accident
guard, not a same-UID security boundary. The key is absent from shared fixtures
and environment variables.
Material, raw server/audit/config/CLI/HTTP logs stay ineligible for sharing.
No response bodies, payloads, URLs, actual paths, headers or exception text are
printed. Only fixed case/method/endpoint-template/status metadata can be
exported through `safe_summary`; it is checked before returning. Its existence
alone is not a runtime result, and no runtime summary is produced here.

KP-C2: the future exact runner must use `api-keys-pytest.ini`, with
`--assert=plain -p no:cacheprovider --tb=line`, no `-rP` or `--showlocals`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and no inherited PYTEST_ADDOPTS/plugins.
Fixed explicit assertion messages avoid secret-bearing value repr. The offline
nested-test check proves a deliberately failed secret comparison's report is
credential-free under these actual settings; expected nested exit1 is a
privacy test, not an HTTP result. No general pytest reporting behavior is changed.

## Scope boundaries

No list endpoint, created-key authentication, anonymous/other-user/customer
access, NULL/empty-scope/type override, omitted expiry, reserved `key`/`self`
update, SQL/query/sort injection, security-defect reproduction or behavior
selection. AK/C23 auth/customer/expiry/secret-exposure decisions remain owner
decisions. Phase0 full suite/hosted CI/Rust parity are not satisfied by this case.
Each new execution needs exact source/harness/runner approvals and a fresh
signed one-heavy-job window ACK/START; old or withdrawn ACKs never carry forward.
