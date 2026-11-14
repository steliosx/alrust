# Outbound webhook forwarder: design proposal

Status: **proposal for review, 14 November 2026.** No code. KICKOFF Phase 4
asks for "a generic outbound webhook forwarder for notifications" with
"per-customer routing rules"; Codex's comparison report lists it with the
Zabbix receiver and the generic inbound mapper as the essential integration
set. This page proposes the shape and lists what needs a decision before
any code.

## What Alerta does today

Alerta 9.1.0 has no outbound webhook in its core: notifications are
plugins (`post_receive`, `status_change`, `take_action` hooks), each sending
from the request thread, so a slow or failing receiver slows or fails the
request path, and nothing is retried after a crash. alrust has no Python
plugins (KICKOFF), so the forwarder is alrust's own, and it does not need to
copy that delivery behaviour.

## Proposal

1. **Events.** An alert is new; its severity changed; its status changed
   (including an operator action); it was deleted. Duplicates without a
   severity or status change send nothing (they would flood receivers).
2. **Routing rules.** Configuration, one list per deployment, each rule:
   `customer` (exact, or any), `environment`, minimum severity, event types
   from item 1, optional `service`/`tags` match, and one destination name.
   A rule never matches across customers unless it says "any customer"
   explicitly. Rules are evaluated in order; every matching rule sends.
3. **Destinations.** Name, HTTPS URL, optional static headers, an optional
   HMAC-SHA256 signature header over the exact body bytes with a key read
   from a file (`hmac` and `sha2` are already dependencies), a timeout, and
   a retry limit.
4. **Body.** The alert's normal API JSON (the same serialiser as
   `GET /alert/{id}`, so the restricted attribute is already absent, spec
   4.2), plus `{"event": "<kind>", "previous": {severity, status}}`. No
   `history` text by default: it can carry signed acknowledgement markers.
5. **Delivery: a durable outbox, written in the same transaction as the
   alert change.** A row per (event, destination) is inserted in the
   alert's own transaction; a worker task in the server process claims
   rows with `FOR UPDATE SKIP LOCKED`, sends outside any transaction,
   and marks each row sent, retried with backoff, or failed after the retry
   limit. A crash between commit and send leaves the row to send later; a
   crash after send and before marking sends it again, so delivery is
   **at least once**, with a stable delivery id header for receivers to
   de-duplicate. No network call ever runs inside an alert transaction
   (the same rule as the integration hooks, spec section 3).
6. **Where the outbox lives.** In an alrust-owned schema (for example
   `alrust`), not in `public`: ADR-0005 decision 2 excludes other schemas
   from the reference catalog check, so the reference layout and its
   adoption stay exact. Its own migrations, separate from `0002`.
7. **Bounds.** A maximum outbox size and age; past them, new events are
   dropped and counted, never blocking alert writes. Metrics: queued,
   sent, retried, failed, dropped, per destination (the Phase 5 metrics
   endpoint).

## Decisions needed

| ID | Question | Recommendation | Who |
|---|---|---|---|
| OUT-1 | At-least-once outbox (item 5) or best-effort in-memory sends | Outbox: alerts are the thing that must not be lost | Codex review, owner |
| OUT-2 | An alrust-owned schema for alrust's own tables (item 6) | Yes; it also gives the search side tables (search design option (a)) a home | Codex (ADR-0005), owner |
| OUT-3 | Where routing rules and destination secrets come from: environment and files, or a table managed by an admin API | Files first (no new admin surface, secrets stay out of the database); an API later | Owner (key custody, as X1) |
| OUT-4 | Which events send by default, and whether duplicates may | Item 1; duplicates off | Owner |
| OUT-5 | Body format: alrust's API JSON, or a provider format (Slack, Teams, PagerDuty) | Generic JSON first; provider formats as later templates | Owner |

## Tests (when built)

Unit: rule matching (customer isolation first), body shape, signature
bytes against fixed vectors. Database: the outbox row commits and rolls
back with the alert change; `SKIP LOCKED` hands one row to one worker; a
restart resends an unmarked row. HTTP: a local receiver fixture with fixed
delays and failures; no external service. No live provider calls.

## Not in scope

Inbound webhooks (the generic mapper, draft #230; Zabbix), Alerta's twelve
provider webhook receivers (Y27 chose four, a separate port), and the
integration hooks (spec `integration-hooks.md`).
