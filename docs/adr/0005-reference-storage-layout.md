# ADR-0005: alrust's store adopts the reference PostgreSQL layout

- Status: **Proposed** (design; no code yet). Direction set by the owner on
  2026-10-13 (DECISIONS Y35, to be recorded with this ADR): "1) yes. Alrust
  needs to fully support zabalack and voalack."
- Date: 2026-10-13

## Why

Zabalack (zab-al-ack) and Voalack attach to Alerta's own database: an
`AFTER UPDATE` trigger on `public.alerts` that reads `(NEW.history[1])` as
Alerta's composite `history` type, `NEW.customer` and `NEW.status`; Zabalack
writes `alerts.attributes`; Voalack writes `public.notes` with the 9-column
layout. Zabalack's installer refuses to run unless `public.alerts` and the
type `public.history` exist (`sql/migrations/001_core.sql:8`). alrust's
alpha store (`crates/alrust-store-pg/migrations/0001_initial.sql`) keeps the
same columns but with alrust's own types (history as `jsonb`, `timestamptz`,
`value jsonb`, `bigint` counters, `NOT NULL` defaults), so neither
integration's database part can attach. Their in-Alerta Python plugin layer
is a separate problem (ADR to follow); this ADR is only the store.

## Decision

1. **The target is the reference schema itself**, `reference/alerta/alerta/
   sql/schema.sql` at the pinned `f4312727` (198 lines): the `history`
   composite type; `alerts`, `notes`, `blackouts`, `customers`,
   `heartbeats`, `keys`, `metrics`, `perms`, `users`, `groups`; the unique
   indexes `env_res_evt_cust_key` and `org_cust_key`. Types, nullability,
   column order and index definitions as the reference creates them, in the
   `public` schema.
2. **Exactness is tested mechanically, not by reading.** A CI check creates
   one database from the reference `schema.sql` and one from alrust's
   migrations, then compares their catalogs: every table's columns (name,
   position, type, nullability, default), the `history` type's attributes,
   and every index definition. Any difference fails the job.
3. **alrust's code adapts to the reference types:**
   - `history history[]`, newest first, trimmed to `HISTORY_LIMIT`. This is
     the reference's own order (`(%(change)s || history)[1:limit]`,
     `postgres/base.py:228-306`) and alrust's existing jsonb order, so only
     the storage type changes. sqlx maps the composite with
     `#[derive(sqlx::Type)] #[sqlx(type_name = "history")]` and an array
     impl.
   - `timestamp without time zone` columns hold UTC; alrust converts at the
     boundary (the API already serialises UTC with millisecond precision).
   - `timeout integer`, `duplicate_count integer`, `history.timeout
     integer`: alrust's values become `i32`. Out-of-range input follows the
     reference's behaviour; the lifecycle spec's timeout rules apply.
   - **Every column the reference leaves nullable is read as nullable.** Rows
     may be written by integrations or by the reference's own code paths, so
     alrust must not assume its own `NOT NULL` defaults.
   - `value text`: see open question 1.
4. **Migration from the alpha schema** is a new migration (`0002`) that
   converts in place:
   - creates the `history` type;
   - converts `alerts.history` jsonb → `history[]` with a SQL function that
     maps each element in order;
   - converts the `timestamptz` columns to UTC `timestamp`;
   - converts `bigint` columns to `integer`, refusing (not truncating) any
     out-of-range row;
   - drops alrust-only `NOT NULL` and defaults;
   - creates the tables alrust did not have;
   - creates the unique indexes. It refuses if existing rows violate
     `env_res_evt_cust_key`, listing them, rather than merging.
   A database freshly created by alrust runs `0001` then `0002`, and the
   catalog check (decision 2) runs on that result. Alpha data is synthetic so
   far, but the migration does not assume it is disposable.
5. **Scope of this ADR's first change:** the schema, the migration, the
   catalog check and the store code's type changes, with the existing smoke
   (115 checks) and unit tests passing unchanged. No new API behaviour.
   Notes, logins, customer views and the ack hook are later changes.

## Open questions

1. **`value`.** The reference column is `text`; the reference model turns an
   integer `value` into a string (`models/alert.py:35-36`, spec R0.3) and
   passes any other JSON value to the database adapter unchanged. alrust
   accepts any JSON `value` and stores it as `jsonb`. Under the reference
   layout alrust must store text. Options: (a) store strings as-is,
   integers as their decimal string (the reference rule), and reject any
   other non-string value with 400 (as Y33 did for keys); (b) store the
   JSON text of any value. Proposed: (a); it is an API-visible behaviour
   change, so it needs Codex's review and, if it is an auth- or
   contract-level change, the owner.
2. **Unique identity index.** `env_res_evt_cust_key` makes the reference's
   duplicate/correlate identity unique at the database level. alrust's
   receive path already serialises on that identity (advisory lock), so the
   index should only turn a race into a constraint error, never fire in
   normal operation. The alpha limitation "multiple matching rows (R2.3)"
   goes away.
3. **Ownership and roles.** Both integrations require the server's database
   login to be distinct from their own roles and not to own their objects
   (Voalack `boundary.py:851-866`). alrust's deployment docs will need a
   role layout like the reference's; that is a later, deployment change.

## Consequences

- alrust's database becomes interchangeable with the reference's at the
  schema level, which is the precondition for the integrations' SQL and for
  a future data migration from a real Alerta install.
- The alpha's own storage choices (jsonb history, timestamptz) are given up.
- Later ADRs: the ack hook for the integrations' in-process layer (owner
  choice A or B pending), user logins and customer views.
