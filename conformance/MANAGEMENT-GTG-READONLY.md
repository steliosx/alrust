# One healthy management GTG vector

This candidate follows `ALRUST-P0-MANAGEMENT-GTG-PLAN-01`, plan packet
`0abe8053`, independently approved **as a plan only** by Claude in `d205108d`.
The pinned reference is `f4312727783dad64198b62ed778bbca98a5144a3`.
`management/views.py:127–134` checks `db.is_alive`; the PostgreSQL property
at `database/backends/postgres/base.py:141–145` executes `SELECT true` and
returns the whole `fetchone()` row. A truthy row selects `OK`; a falsy row
selects `FAILED`/503. Exceptions propagate to the global JSON500 handler.
These are source-derived facts. The nine selected-AST synthetic plan probes
are not SQL, registered HTTP or database outage evidence.

The single candidate case calls `GET /management/gtg` once through existing
`Client.request_text`, requires **integer 200 and original text exactly `OK`**,
and keeps the body in memory. No JSON parsing, content-type assertion, body
export or timestamp normalization occurs (GN-1). Failed requests are not
retried. The fixed summary exports only the case name and completion state.
The client closes even when the case fails. Its labeled synthetic key header
is ignored by this undecorated route; this is not authorization evidence.

Before constructing the client, the fixture validates the existing private
factory-config record, synthetic credential file, and a new GTG connection
record with a complete-byte hash chain to served config and health-readiness
marker. Every GTG record is a bounded regular nonsymlink file with mode0600.
Task/full reference/target/new GTG window/strict integer PID must match.
Connection flags must be literal true and backend `postgresql`; provenance
binds observation after readiness to the same served connection (GN-2).
Config prerequisites remain source-bound and type-exact. No empty-heartbeats
record/count is requested or inferred.

**The files cannot prove their producer observed a connection.** A separately
reviewed runner must produce fresh records on the actual served PostgreSQL
connection, checking effective backend URI, database name, schema and live
destination against the owned instance, after `/management/healthcheck`
readiness. Readiness and any setup requests remain setup evidence (GN-3).
The explicit suite case is a separate GTG request. No GTG readiness request,
response capture, heartbeat query, table mutation or separate-factory
substitute is implicit in this candidate.

Execution uses `management-gtg-pytest.ini`; its settings keep failure output
fixed and disable assertion rewriting/cached pytest evidence. Source/offline
review, exact runner execution review and a **fresh** signed heavy-window
tuple ACK/START are required before its first HTTP run. The closed health
window and its acknowledgments do not authorize this vector. Prerequisite
files produced by an unreviewed runner or copied from that health run are
insufficient.

Current status: **HTTP/SQL NOT_RUN**, pending fresh exact-head harness review
and separately reviewed runner/window. Existing health/reference results
stay at their original source identities. No outage/disconnect, 503/500 HTTP
case, properties dump, switchboard/housekeeping, auth/anonymous/proxy,
production, Rust parity, two-server or full-suite gate is covered. Static UI
absence does not decide operator usage or authorize an endpoint drop.
Hosted **CI_BLOCKED_BILLING** remains separate from local evidence; Phase0's
full reference CI/adversarial gate stays open. Repository STATUS/DECISIONS,
root README, reference pins, workflow, dependencies and existing tests are
unchanged. Owner-only merge/release/production/auth-contract/behavioral dispute
and host/billing/protection/runner/bypass gates remain in force.
