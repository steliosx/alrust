# Heartbeat input and query follow-up

Four SOURCE_DERIVED / HTTP NOT_RUN cases at immutable reference
`f4312727783dad64198b62ed778bbca98a5144a3`. These extend the separately reviewed
integration `061d2a969d8fc4bd876f54885758727c5643c896`; a new head needs a new
Claude verdict. This candidate changes no reference or authentication behavior.

- Present `tags: null` and `attributes: null` each produce a parser ValueError,
  mapped to 400, with no stored heartbeat for the test's unique origin.
  Absent containers use defaults; a present null does not. Source:
  `alerta/models/heartbeat.py:74-79`, `alerta/views/heartbeats.py:22-25`.
- An unknown-only status filter adds no SQL status predicate, while the customer
  constraint remains. Unscoped and customer-A admin reads must retain their
  respective two-row and one-row sets. Source:
  `alerta/database/backends/postgres/utils.py:291-307`,
  `alerta/database/backends/postgres/base.py:890-908`.
- A nonempty filtered page reports the total and paging computed before status
  selection. Two rows share the unique origin, but have different customers:
  A is safely slow (client time 2000); NULL is safely ok (client time 2100,
  negative latency). With page size 1 and `status=slow`, page 1 has one slow
  heartbeat but total 2, pages 2 and more true. Page 2 is empty and reports
  total 0 while retaining pages 2. Source: `alerta/views/heartbeats.py:62-87`,
  `alerta/utils/paging.py:9-33`,
  `alerta/database/backends/postgres/base.py:886-927`.

These are expectations, not executed behavior. The exact source-body probes
used to review OpenAPI ran only with mocked app/clock/query/storage globals.
The earlier three HTTP heartbeat vectors passed at frozen `9548f878` in
`alrust-heartbeat-vectors-reference-20260930-01`, runner `6bc8742d`, with both
agents confirming release. They did not run these four cases or this new head.

The existing reviewed heartbeat fixture supplies unique synthetic origins,
generated full UUID IDs and private admin keys. It registers created IDs before
later assertions and attempts all owned cleanup despite failures, then closes
clients. A surprising 201 in either invalid-input case is recorded for cleanup
before the expected-status assertion fails. All responses are exported only
after known-key redaction; assertions see raw responses. No timestamp, order,
null or field normalization is introduced. No short or wildcard IDs are used.

Any future run requires disposable reference/Postgres and the same attested
four-role private synthetic fixture: AUTH_REQUIRED False, CUSTOMER_VIEWS False,
HEARTBEAT_TIMEOUT 86400, HEARTBEAT_MAX_LATENCY 2000. The server clock must be
well before 2100; unexpected nonnegative future latency or expiry is a failure,
not normalized away. Positive large timeout and extreme client dates keep
these cases away from status boundaries. Only unscoped/A admin keys are used.
An exact reviewed runner and fresh signed full-tuple ACK/START window must name
only this file; no old heartbeat ACK authorizes execution.

Not covered: arbitrary-value persistence, SQL error/cast edge cases, expiry
and boundary races, short/prefix/wildcard multirow deletion, healthcheck after
heartbeats, plugin conversion, alternate auth, Rust/two-server parity or the
full Phase 0 hosted gate. No production payload/config/certificates are needed.
