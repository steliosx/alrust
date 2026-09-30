# conformance

Black-box HTTP conformance suite plus a differential fuzzer. The suite runs
against either the reference Python Alerta server or alrust. The target is
chosen only by `ALRUST_TARGET=<base url>`, and the suite never imports code
from either implementation.

- `scripts/run-reference.sh` boots the pinned reference server
  (`reference/alerta`) against PostgreSQL and waits for it to become healthy.
- Python 3.12 + pytest + httpx + Hypothesis (ADR-0002, owned by Codex).

Phase 0 gate: the suite is green against the reference server in CI.

This initial foundation is **not the full Phase 0 suite**. It includes target
selection, synthetic create/dedup/detail/cleanup cases, strict JSON comparison
and a seeded differential replay runner. It imports neither implementation.
Four customer-mapping cases were authored from the reviewed source spec; their
prior-head local runtime result is recorded below. Operator cases also have a
separate prior-head reference result; metadata cases also have a scoped frozen-head
reference result. Three frozen-head heartbeat cases also have a separate reference result. Four
new [heartbeat input/query cases](heartbeat-input-query.md) have a separate frozen-head
reference result below. Three new [metadata mutation cases](metadata-mutations.md)
are SOURCE_DERIVED / HTTP NOT_RUN at this head.
Lifecycle, broader auth, query, heartbeat, timeout
and stateful shrinking coverage remain incomplete. Empty Rust crates and offline harness
tests are not evidence of Alerta compatibility.

The harness lock records SHA-256 hashes of exact supported wheels while
preserving all 14 dependency versions. Its platform is Linux x86_64,
CPython 3.12 and glibc 2.17 or newer: pure Python wheels where available,
plus the matching Hypothesis binary wheel. [Wheel provenance](wheels.manifest.json)
records each filename, size, fixed-version PyPI metadata URL and published
digest, independently checked against the downloaded bytes. Install with
`--require-hashes --only-binary=:all:`. This rejects altered archives and
unlisted source distributions; it does not attest to upstream package safety
or lock the reference server's separate dependencies.

Use an isolated disposable server/database for every HTTP run. All generated
alerts carry an `alrust-synthetic-` resource and `SYNTHETIC` tag. Credentials
are synthetic environment inputs or private fixture files outside Git; they
never enter shared reports or repository fixtures. Real
payloads, alertad.conf and signing certificates are unnecessary for these cases.

```sh
python3.12 -m venv /tmp/alrust-harness-env
/tmp/alrust-harness-env/bin/python -m pip install --require-hashes --only-binary=:all: -r conformance/requirements.lock
# Offline infrastructure verification, without a service or compatibility claim:
/tmp/alrust-harness-env/bin/python -m pytest -c conformance/pytest.ini conformance/tests/unit
# Initial smoke only; absent ALRUST_TARGET fails rather than silently skipping:
ALRUST_TARGET=http://127.0.0.1:8080 /tmp/alrust-harness-env/bin/python -m pytest -c conformance/pytest.ini conformance/tests/http/test_scaffold.py
# Distinct disposable instances; use a fresh private report path each attempt:
PYTHONPATH=conformance /tmp/alrust-harness-env/bin/python -m alrust_conformance.differential \
  --reference http://127.0.0.1:8080 --candidate http://127.0.0.1:8081 \
  --seed 42 --steps 20 --report /tmp/alrust-review/attempt-01.json
```

`ALRUST_API_KEY` is the target/candidate key; `ALRUST_REFERENCE_API_KEY` is the
reference key. Use only synthetic test credentials. One heavy build/service test
runs at a time under the existing laptop coordination window.

For differential CLI reports, each key input must be absent or match
`SYNTHETIC-` followed by 48 lowercase hexadecimal characters. An empty, short
or differently formatted input produces a private `NOT_RUN` report before any
client is created. Only the rejected environment field names are recorded;
their values never enter the report. This is the runner's synthetic-fixture
contract, reviewed at frozen `e91dcabe`; this integration needs its own review. Server authentication behavior
and the general Client's negative-auth test inputs are unchanged.

The runner captures validated inputs once and uses those same values for
requests and export redaction. A short key cannot erase source hashes,
namespaces or timestamp evidence. Credential echoes are still redacted only
after raw comparison. Deliberate short-key authentication probes belong in
separate cases using Client, with private evidence and explicit echo handling.

The CLI's `receive-clock-v1` validates receive retention/refresh semantics
before projecting exactly `alert.receiveTime`, `lastReceiveTime` and
`updateTime`; selected alert/history href bases are also normalized. Client
createTime, history timestamps/order, customer, unknown fields and all other
semantic data stay strict. The controlled fixture uses ordered client times
and 50 ms request spacing. [The exact clock policy](receive-clock-policy.md)
documents scope, source citations and failure behavior. Its implementation
was reviewed at frozen `db06ce44`; this integration needs a fresh verdict before execution. `--comparison
strict-json-v1` preserves every timestamp for unsupported vectors/diagnostics.
Reports retain the full synthetic sequence,
responses (known credential echoes redacted after comparison), cleanup results,
target URLs, declared reference pin, source HEAD, dirty state and harness file
hashes. The declared pin does not attest to the live server's installed source.
Equal HTTP failures, empty runs, malformed successes or cleanup failures are FAIL.

Each report is reserved exclusively with mode `0600` before HTTP effects, even
inside an existing parent directory with a permissive umask. The initial
preflight record survives an interrupted run. Missing Git provenance produces
a retained `NOT_RUN` with error types and sends no requests; client setup
failures retain `FAIL` evidence and close any already-created client. Existing
report paths are refused so every attempt keeps its own evidence.

The executing record is flushed as FAIL before effects, then updated at
request/response/cleanup boundaries. Late metadata failure retains the HTTP
outcome, partial responses and cleanup, but makes overall verification FAIL.
An interruption after effects is FAIL rather than NOT_RUN.

Current validation labels: offline tests LOCAL_PASS. The two initial smoke
cases at frozen `9d70dc2e4e09c95901d1f8be2096e6ec2effa2bc` passed locally on
Python 3.12.14/PostgreSQL 16 against reference
`f4312727783dad64198b62ed778bbca98a5144a3` in signed window
`alrust-ref-smoke-20260929-03`; cleanup was confirmed. Customer cases passed at
frozen `a7426411` in the separately recorded customer window below. Operator
cases passed at frozen `a4d75716`; metadata cases passed at frozen `678f1996`.
The three heartbeat cases were reviewed at frozen `9548f878`; runner `6bc8742d`
was separately reviewed. Those three cases passed at frozen `9548f878` in
`alrust-heartbeat-vectors-reference-20260930-01`, released `1ba053be` and
independently confirmed `4dbd2043`, in 0.56 seconds. Neither source approval
nor that window applies to this new follow-up head. Historical runs do not
approve this changed head.
Hosted Phase 0 gate remains CI_BLOCKED_BILLING. Never substitute local results
for CI or claim Rust parity from a reference-only smoke.

See [customer fixtures](customer-fixtures.md) for prerequisites and the private
synthetic credential schema. Running the whole HTTP directory now requires
that fixture; missing prerequisites fail before customer HTTP requests.

Five [operator status/action vectors](operator-actions.md) have a separate
frozen-head runtime result recorded below. The customer-only window did not
run them; their execution used its own reviewed runner and signed window.
The four customer vectors passed locally at frozen
`a7426411181f4dd3cc859581cc203ee4067c07b4` against reference
`f4312727783dad64198b62ed778bbca98a5144a3` in signed window
`alrust-customer-reference-20260930-01`, with stored-record attestation and
confirmed cleanup. That narrow prior-head evidence is separate from this new
operator result and the blocked hosted gate.

The five operator cases later passed only at frozen
`a4d75716755b89b7a1ab9acfac122092ee7b736e` in signed window
`alrust-operator-actions-reference-20260930-01`, with effective-config and
stored-key attestation and confirmed cleanup. The two
[metadata/suppression vectors](metadata-edges.md) separately passed at frozen
`678f19961565dbbebffb4a14f4f38315c789fdd6` against the same immutable reference,
in signed window `alrust-metadata-edges-reference-20260930-01`, with runner
`1b0cf1a1`, effective-config and stored-key attestations. Owned cleanup was
independently confirmed by both agents (`0f72c944` / `8e0db6f8`). The prior
operator window did not run them. Raw tag order is retained, including where
assertions can only check tag content/count because reference ordering is undefined.

Three [heartbeat vectors](heartbeats.md) passed only at the frozen head and
window above. Four [input/query follow-up cases](heartbeat-input-query.md)
passed only at frozen `60d2e021eeb9c1040cfa3bdb373cfe949e9b5db0` in window
`alrust-heartbeat-input-query-reference-20260930-01`, in 0.42s with runner
`48e3f654`, RELEASE `ef919f45`, independently confirmed `312fdb6f`.
That scope does not execute or approve this new head. Three new
[metadata mutation cases](metadata-mutations.md) remain HTTP NOT_RUN, needing
a fresh source verdict and reviewed runner/window. No released window authorizes them.
This changed head has no runtime pass or inherited review approval.

The reviewed base integration `061d2a969d8fc4bd876f54885758727c5643c896`
(Claude source/offline APPROVE `3d0e68fd`, 50 tests and 16 collected) combines guard `e839ceaf`, hash/clock/key
harness `130145ea` and HTTP vectors `9548f878` without changing the Python
code, locks, guard or pinned reference files. Its README resolved the branch
overlap. The previous `60d2e021` follow-up added four heartbeat HTTP cases without changing
that integration. This follow-up adds three metadata cases; existing fixture/tests/
library/lock/guard/reference are unchanged. It needs a fresh review; integration
HTTP and this head's HTTP are NOT_RUN. The frozen four-case result above is separate. Hosted billing and the full Phase0 gate
remain separate.
