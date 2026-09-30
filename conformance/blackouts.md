# Ordinary owned blackout vectors

These two cases are **SOURCE_DERIVED / HTTP NOT_RUN** against immutable
reference `f4312727783dad64198b62ed778bbca98a5144a3`. They use only the
existing synthetic configuration and validated unscoped admin key. No current
OpenAPI review establishes runtime behavior for this changed head.

| Case | Source-derived expectation | Reference source |
|---|---|---|
| Owned create/detail/list/duration/time update/delete | Explicit endTime determines duration, ignoring input duration. Create returns integer remaining equal to duration. Reads of the expired period return numeric remaining serialized as a decimal string. A duration-only update leaves the displayed interval unchanged in this fixture; an endTime update changes displayed duration. Exact-resource list finds the owned row; delete removes it | models/blackout.py:25-61,92-137,190-210,236-246; views/blackouts.py:17-144; postgres/base.py:745-852; utils/format.py:39-42 |
| Origin override and priority chain | Origin makes priority 8. Removing it exposes service priority 3, clearing service exposes group priority 5, clearing group exposes resource+event priority 6. API reads recompute priority; all other raw fields remain equal | models/blackout.py:63-80,190-210; postgres/base.py:803-839 |

Every record has a fresh UUID and unique synthetic resource. Successful create
Requested UUIDs are tracked on a successful 201 before response-shape assertions.
Finally attempts deletion of every owned ID, reports failures and closes the
client even after a malformed response or a failed deletion. Three offline
fault-injection tests exercise those failures and invalid fixture setup. The period is explicitly in 2000 and expired; these cases
do not create active blackouts or run alert matching/housekeeping. List queries
use exact resource, never the ignored `id` query parameter, `q`, arbitrary sort,
or unspecified list ordering. Tests make no direct database access.

The reference serializer converts `Decimal` to a string. PostgreSQL EXTRACT
produces numeric remaining for reads/updates, whereas create returns stored
integer duration as remaining. This is a source/driver-derived expectation;
actual API representations remain HTTP NOT_RUN. PostgreSQL 16 documents
[EXTRACT as returning numeric](https://www.postgresql.org/docs/16/functions-datetime.html#FUNCTIONS-DATETIME-EXTRACT).
Pure probes of the actual constructor, driver numeric caster and serializer
confirm the priority chain and Decimal-to-string conversion without an
application or SQL connection. Expired read remaining is
validated as a string with numeric value zero, while the original string is
retained in captured evidence and subsequent whole-record comparisons. No
normalizer changes timestamps, IDs, nulls, priorities or remaining values.

Customer is already null under the unscoped fixture and login stays the same
synthetic admin. The duration-only assertion is restricted to that prerequisite.
Every PUT still assigns customer and user. This does not resolve C22's possible
customer clearing/matching expansion under CUSTOMER_VIEWS, change customer
filtering, or approve an Alrust behavioral/data-loss divergence.

These cases exclude malformed times/bodies, reserved id/self update keys,
arbitrary customer metadata, anonymous scope bypasses, cross-customer access,
negative durations, priority/duration SQL sorting, session-clock comparisons
and plugin/alert-matching effects. Those require separate review and execution
scope; any contract change is the owner's. No credentials or production inputs
belong in Git or messages.

Parent note source `24594379d6b8c494e0f89936212380f79290cbc3` has source/offline
approval `463582c1`; its separate runner is pending review and HTTP NOT_RUN.
Historical HF7 corrected smoke passed at `4f808c9d` for two cases; its original
aggregate cleanup FAIL is preserved separately from later confirmed cleanup.
Neither approval nor runtime evidence carries forward to this blackout head.
It needs Claude's fresh exact-source review and its own reviewed runner/window.
No full-suite, Rust parity, two-server or hosted gate result is implied.
