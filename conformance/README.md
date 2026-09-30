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
prior-head local runtime result is recorded below. New operator action cases
remain NOT_RUN. Lifecycle, broader auth, query, heartbeat, timeout
and stateful shrinking coverage remain incomplete. Empty Rust crates and offline harness
tests are not evidence of Alerta compatibility.

Use an isolated disposable server/database for every HTTP run. All generated
alerts carry an `alrust-synthetic-` resource and `SYNTHETIC` tag. Credentials
are synthetic environment inputs or private fixture files outside Git; they
never enter shared reports or repository fixtures. Real
payloads, alertad.conf and signing certificates are unnecessary for these cases.

```sh
python3.12 -m venv /tmp/alrust-harness-env
/tmp/alrust-harness-env/bin/python -m pip install -r conformance/requirements.lock
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

Comparison policy `strict-json-v1` normalizes only the selected target base URL
in alert/history `href` fields. It preserves unknown fields, nulls, IDs, customer,
history order, severity/status, duplicate counts and **all timestamps**. A clock
policy is deliberately pending source/spec and Claude review. Independent server
timestamps ordinarily make even the first real response differ; this strict
foundation cannot yet discriminate meaningful two-server parity. Keep that FAIL
visible until a separately reviewed policy exists. Reports retain the full synthetic sequence,
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

Current validation labels: offline tests LOCAL_PASS. The two initial smoke
cases at frozen `9d70dc2e4e09c95901d1f8be2096e6ec2effa2bc` passed locally on
Python 3.12.14/PostgreSQL 16 against reference
`f4312727783dad64198b62ed778bbca98a5144a3` in signed window
`alrust-ref-smoke-20260929-03`; cleanup was confirmed. Customer cases passed at
frozen `a7426411` in the separately recorded customer window below; new operator
cases remain NOT_RUN and await independent review and a separate signed window.
Hosted Phase 0 gate remains CI_BLOCKED_BILLING. Never substitute local results
for CI or claim Rust parity from a reference-only smoke.

See [customer fixtures](customer-fixtures.md) for prerequisites and the private
synthetic credential schema. Running the whole HTTP directory now requires
that fixture; missing prerequisites fail before customer HTTP requests.

Five new [operator status/action vectors](operator-actions.md) are
SOURCE_DERIVED / NOT_RUN and require fresh exact-head peer review and a new
signed execution window. The released customer-only window did not run them.
The four customer vectors passed locally at frozen
`a7426411181f4dd3cc859581cc203ee4067c07b4` against reference
`f4312727783dad64198b62ed778bbca98a5144a3` in signed window
`alrust-customer-reference-20260930-01`, with stored-record attestation and
confirmed cleanup. That narrow prior-head evidence is separate from this new
head's unexecuted operator tests and the blocked hosted gate.
