# One root-debug read vector

This candidate follows `ALRUST-P0-ROOT-DEBUG-READONLY-PLAN-01`, signed plan
packet `84b1a2ae`, independently approved **as a plan only** by Claude in
`4685e805`. It requires a fresh exact-head source/offline verdict; the plan
and parent GTG approvals do not approve this harness, a runner or a window.

Pinned reference `f4312727783dad64198b62ed778bbca98a5144a3` registers
`GET /_` at `alerta/views/__init__.py:44–46`; its handler returns literal
`OK` without a handler-local database or config call. The API blueprint's
JSON guard at lines17–20 covers POST/PUT. Factory initialization still
loads database/config/plugins/CORS (`alerta/app.py:49–91`), and global
tracing/logging hooks still apply. These are source-derived facts, not
whole-stack database independence, auth, CORS or outage evidence.

The single explicit case calls `GET /_` once through existing
`Client.request_text`, requires **integer 200 and original string exactly
`OK`**, and retains the body only in memory. RDN-1 excludes HEAD, OPTIONS,
CORS/header assertions and auth claims. There is no JSON parsing, content-type
oracle, body export, stripping, timestamp normalization or retry. The fixed
summary exports only project/case/completion/captured-record count. Client
cleanup uses fixed failure messages even after a case failure. The labeled
synthetic key header is ignored by this undecorated handler; this is not
proof about global authorization.

Before constructing a client, the fixture checks the existing baseline
factory-config record, a new root-debug connection/provenance/readiness
chain, and synthetic credentials. Chain files must be bounded regular
nonsymlink mode0600 files with exact field sets (12 connection fields,
12 provenance fields, 5 readiness fields). Project/SYNTHETIC task/full
reference/target/new root-debug window/strict integer PID must match.
Backend must be postgresql and identity flags literal true. Provenance
hashes bind the exact complete connection, served-config and marker bytes;
comments/newlines are not reconstructed. GTG task/window records are rejected
even with internally updated hashes. No heartbeat dataset or count is
required or inferred.

**Records are accident guards, not evidence that their producer observed a
connection.** A separately reviewed runner must create fresh private records
from the actual served connection after healthcheck readiness, checking
effective backend URI, database name, schema and live destination against
the owned instance. Its producer must recheck baseline config and its own
request's cached connection identity on each later request, including `/_`,
before the original handler. Teardown may replace the setup connection;
this does not claim the same object is reused across requests. Setup and
health-readiness requests are distinct from the one explicit root-debug
case. No hook SQL/count/query, table mutation, response capture or separate
factory substitute is part of this harness.

Use `root-debug-pytest.ini` only after fresh exact harness and runner reviews,
a fresh signed heavy-window request/ACK and matching START. Closed GTG and
health windows do not authorize this case; copied old records and unreviewed
producer records are insufficient. The reviewed inert START renderer
`7eb51452350467a143a534c860838a6541e7be0f875574637f4561ff021f4bb6`
helps prepare intent, but caller verification and signed delivery remain
necessary.

Status at authorship: **reference HTTP/SQL NOT_RUN**. Offline tests use
synthetic stubs and do not count as registered HTTP or Rust parity. No
root-index, auth/anonymous/proxy, properties dump, switchboard/housekeeping,
503/500, outage, production, two-server or full-suite gate is covered. A
negative literal search in pinned UI source is not proof of non-use by
operators/scripts/dynamic callers; no endpoint drop or compatibility decision
is taken. Hosted **CI_BLOCKED_BILLING** is separate from local results; the
Phase0 full reference CI/adversarial gate remains open.

Existing tests, dependencies, workflow, reference pins, root README and
repository STATUS/DECISIONS are unchanged. Claude remains sole writer of
STATUS/DECISIONS. Owner-only main merge/release/production/auth-contract/
behavioral dispute, billing/protection/visibility/runner-registration/
privileged-host/bypass gates remain in force.
