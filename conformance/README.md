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

Comparison policy `strict-json-v1` normalizes only the selected target base URL
in alert/history `href` fields. It preserves unknown fields, nulls, IDs, customer,
history order, severity/status, duplicate counts and **all timestamps**. A clock
policy is deliberately pending source/spec and Claude review; independent server
clocks can therefore produce a retained FAIL. This is an unexplained divergence,
not permission to discard fields. Reports retain the full synthetic sequence,
responses (known credential echoes redacted after comparison), cleanup results,
target URLs, declared reference pin, source HEAD, dirty state and harness file
hashes. The declared pin does not attest to the live server's installed source.
Equal HTTP failures, empty runs, malformed successes or cleanup failures are FAIL.

Current validation labels: offline tests LOCAL_PASS; supported Python 3.12 /
PostgreSQL 16 reference HTTP run NOT_RUN pending the coordinated service window;
hosted Phase 0 gate CI_BLOCKED_BILLING. Never substitute a local result for CI.
