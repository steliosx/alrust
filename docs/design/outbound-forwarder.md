# Outbound webhook forwarder: design proposal

Status: **proposal for review, 14 November 2026.** No code. KICKOFF Phase 4
asks for "a generic outbound webhook forwarder for notifications" with
"per-customer routing rules"; Codex's comparison report lists it with the
Zabbix receiver and the generic inbound mapper as the essential integration
set. This page proposes the shape and lists what needs a decision before
any code.

## What Alerta does today

Alerta 9.1.0's core ships one outbound sender, the `forwarder` plugin
(`reference/alerta/alerta/plugins/forwarder.py`), for federating Alerta
servers: in `post_receive` it sends the alert body (without history, with
`id` set to `lastReceiveId`) to each `FWD_DESTINATIONS` entry whose action
list includes `alerts`, from the request thread, through Alerta's API
client. A failure is logged and skipped, with no retry, and a crash after
commit loses the send. Loops are prevented with an `X-Alerta-Loop` header
listing the servers already passed; an incoming alert that names this
server is refused (`ForwardingLoop`). Other notifications (Slack, email and
so on) are external plugins. alrust runs no Python plugins (KICKOFF), so
its forwarder is its own and does not need to copy the synchronous,
lose-on-failure delivery; it should keep the loop header if it is to
federate with Alerta servers.

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
4. **Body.** An envelope, so nothing collides with the alert's own
   `event` field:
   `{"kind": "alrust.alert.<new|severity|status|deleted>", "deliveryId":
   "<uuid>", "sentAt": "<time>", "previous": {"severity", "status"},
   "alert": <the alert's normal API JSON>}`. The alert JSON is the
   `GET /alert/{id}` serialiser, so the restricted attribute is already
   absent (spec 4.2). No `history` by default: it can carry signed
   acknowledgement markers. **Federation** destinations (item 7) do not
   use the envelope: they send the reference forwarder's body (the alert
   body without history, `id` set to `lastReceiveId`) to the remote
   `POST /alert`, as `forwarder.py` does.
5. **Delivery: a durable outbox, written in the same transaction as the
   alert change.** A row per (event, destination) is inserted in the
   alert's own transaction. No network call ever runs inside a database
   transaction (the same rule as the integration hooks, spec section 3).
   Each row moves through a fenced lease:
   - **Claim** (one short transaction): pick due rows with
     `FOR UPDATE SKIP LOCKED`, set `state = sending`, `lease_owner = <worker
     id>`, `lease_token = <new uuid>`, `lease_until = now() + <lease>`,
     increment `attempts`, commit. The row lock ends at commit; the lease
     is what keeps other workers away.
   - **Send** outside any transaction, with a timeout shorter than the
     lease.
   - **Complete** (one short transaction): `UPDATE … SET state = sent | retry
     | failed … WHERE id = $1 AND lease_token = $2`. Zero rows updated means
     the lease expired and another worker re-claimed the row: this worker's
     result is discarded (the fence), never written over the newer claim.
   - **Recover:** a row in `sending` whose `lease_until` has passed is due
     again. So a crash after send and before completion sends again:
     delivery is at least once, and `deliveryId` lets receivers drop
     repeats.
6. **Where the outbox lives.** In an alrust-owned schema (for example
   `alrust`), not in `public`: ADR-0005 decision 2 excludes other schemas
   from the reference catalog check, so the reference layout and its
   adoption stay exact. Its own migrations, separate from `0002`.
7. **Loops.** Each send carries `X-Alerta-Loop` (the reference's header,
   so mixed alrust and Alerta federations interoperate); a destination
   already named in an incoming alert's header is skipped, and an incoming
   alert naming this server is refused, as the reference does.
8. **Bounds are policy choices, and each one limits "at least once".**
   - **Retry exhaustion:** after the retry limit a row becomes `failed`
     and is not sent again unless an operator re-queues it. That event is
     then delivered zero times.
   - **Capacity and age:** either refuse new events when the outbox is
     full or too old (the alert write still succeeds; the event is lost and
     counted), or let the outbox grow without limit (no loss, unbounded
     storage). Blocking alert writes is never an option.
   Which to use, and the limits, is decision OUT-7. Metrics: queued,
   sending, sent, retried, failed, dropped, per destination (the Phase 5
   metrics endpoint), so every loss is visible.

## Decisions needed

| ID | Question | Recommendation | Who |
|---|---|---|---|
| OUT-1 | At-least-once outbox (item 5) or best-effort in-memory sends | Outbox: alerts are the thing that must not be lost | Codex review, owner |
| OUT-2 | An alrust-owned schema for alrust's own tables (item 6) | Yes; it also gives the search side tables (search design option (a)) a home | Codex (ADR-0005), owner |
| OUT-3 | Where routing rules and destination secrets come from: environment and files, or a table managed by an admin API | Files first (no new admin surface, secrets stay out of the database); an API later | Owner (key custody, as X1) |
| OUT-4 | Which events send by default, and whether duplicates may | Item 1; duplicates off | Owner |
| OUT-6 | Federation with Alerta servers (the reference `forwarder`: same body and loop header) in addition to generic webhooks | Yes, as one destination kind | Owner |
| OUT-7 | Retry limit, and on capacity or age: drop new events (counted) or grow without limit | A retry limit with a dead-letter state; grow without limit with an alarm at a size, until there is data | Owner |
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
