# Heartbeat vectors

The three cases passed only at frozen `9548f8781c999e43da0382be69a6d5b390e7d0e4`
in signed reference window `alrust-heartbeat-vectors-reference-20260930-01`,
runner `6bc8742d`: LOCAL_PASS 3 in 0.56 seconds, with config/key attestations
and both agents confirming release (`1ba053be` / `4dbd2043`). New heads do not
inherit that runtime result or approval. These cases target immutable reference
`f4312727783dad64198b62ed778bbca98a5144a3` and reviewed lifecycle
`84b2ed5de5b15d0baac5b24a423f8f1c23cb4ad1` (HB0/HB2/HB4), plus customer
mapping `207f1bcac270afa40b8efc1bb2464e1886e48857` (C19).

1. The identity is `(origin, customer)`: customer A, B and NULL have separate
   IDs for the same origin. A repeat in A keeps its stored ID despite a new
   requested ID, and replaces tags/attributes/client createTime/timeout and
   server receiveTime. `postgres/base.py:851-861`, `views/heartbeats.py:27`.
2. Listing filters the customer-scoped admin to A; an unscoped admin sees all
   three. Fixed old client timestamps make these safely `slow`, far from the
   2000ms boundary. The `ok` filter returns an empty list and `total: 0`, even
   though the query has three records before status filtering.
   `views/heartbeats.py:62-87`, `postgres/base.py:886-918`.
3. Per-ID GET and DELETE have no listing customer filter: scoped admin A can
   read and delete its test's owned B heartbeat, while listing still hides B.
   `views/heartbeats.py:48,96`, `auth/decorators.py:48` and
   `postgres/base.py:863-872`. This preserves the observed reference contract;
   it proposes no authentication divergence or removal of alert filtering.

Configuration for that frozen run and any proposed future run: disposable reference/Postgres, default
`AUTH_REQUIRED=False`, `CUSTOMER_VIEWS=False`, `HEARTBEAT_TIMEOUT=86400`,
`HEARTBEAT_MAX_LATENCY=2000`, and the existing private four-role actual-record
synthetic fixture. Only unscoped and A admin roles are used: `write:alerts`
roles are not assumed to have `write:heartbeats`. A new runner must attest
heartbeat settings explicitly and retain `pytest -rP` evidence, then obtain
its own exact-source review and fresh signed window. No current or released
metadata window authorizes heartbeat execution.

Requests/responses are printed only after known-key redaction; assertions use
original responses. Raw tags/nulls/timestamps/order and derived fields remain
in private captured evidence. Dynamic `since` is validated as a nonnegative
integer; fixed old createTime and positive large timeout keep these cases
away from expiry or latency boundaries. The repeated receiveTime must advance;
there is no two-server clock normalization. UUIDs are recorded immediately on
successful creation, all remaining owned IDs are deleted in teardown, and
all owned cleanup attempts continue after a failed deletion, and clients are
closed even on setup/cleanup failures. The deliberate deletion
case removes only a heartbeat created by that test.

NOT_RUN beyond these three: expiry/status-boundary races, nonempty filtered
page totals, ID-prefix collisions, heartbeat-as-alert plugin conversion,
healthcheck staleness/day wrapping, actual invalid-input/scope cases, other
auth configurations, Rust/two-server parity or full Phase0 hosted gate.
