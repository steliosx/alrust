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
Lifecycle, customer/auth, query, heartbeat, timeout and stateful shrinking
coverage await the source-cited spec. Empty Rust crates and offline harness
tests are not evidence of Alerta compatibility.

Use an isolated disposable server/database for every HTTP run. All generated
alerts carry an `alrust-synthetic-` resource and `SYNTHETIC` tag. Credentials
are optional environment inputs; they never enter reports or fixtures. Real
payloads, alertad.conf and signing certificates are unnecessary for these cases.

```sh
python3.12 -m venv /tmp/alrust-harness-env
/tmp/alrust-harness-env/bin/python -m pip install -r conformance/requirements.lock
# Offline infrastructure verification, without a service or compatibility claim:
/tmp/alrust-harness-env/bin/python -m pytest -c conformance/pytest.ini conformance/tests/unit
# Black-box HTTP cases; absent ALRUST_TARGET fails rather than silently skipping:
ALRUST_TARGET=http://127.0.0.1:8080 /tmp/alrust-harness-env/bin/python -m pytest -c conformance/pytest.ini conformance/tests/http
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
contract, independently awaiting Claude review; server authentication behavior
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
requires a fresh independent verdict before execution. `--comparison
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

Current validation labels: offline tests LOCAL_PASS. Released window03 passed
the two initial reference smoke cases locally at frozen9d70dc2/f431272 on
Python3.12.14/PostgreSQL16. This new correction has HTTP NOT_RUN pending fresh
exact-head review and a new signed window; withdrawn acknowledgments are not
reused. Two-server execution NOT_RUN; hosted Phase0 CI_BLOCKED_BILLING.
Never substitute a local result for CI or claim Rust parity from reference smoke.
