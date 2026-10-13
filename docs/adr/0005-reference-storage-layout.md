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
4. **Three database cases, handled separately at start-up.** The reference
   runs its idempotent `schema.sql` on every start (`postgres/base.py:76`);
   alrust embeds the pinned file byte-for-byte and does the same.
   - **Fresh database** (no `alerts`, no `_sqlx_migrations`): run the
     embedded `schema.sql`. No alpha migration runs.
   - **Alpha database** (`_sqlx_migrations` holds version 1 with alrust's
     `0001_initial.sql` checksum): run the SQLx migrator, which applies the
     new `0002_reference_layout.sql` once, then the embedded `schema.sql`.
     `0001` is never edited, so applied history and checksums are preserved.
     SQLx runs `0002` in one transaction: any refusal below rolls everything
     back and alrust exits with the reason, leaving the alpha schema intact.
   - **Existing reference database** (`alerts` and type `history` present,
     no `_sqlx_migrations`): run the embedded `schema.sql` only. Its
     statements are `IF NOT EXISTS` / guarded `ALTER`s, so existing tables
     and every record (alerts, notes, blackouts, customers, keys, users,
     groups, perms, metrics, heartbeats) are untouched, exactly as when the
     reference itself starts.
   - Anything else (for example `alerts` without type `history`, or an
     unknown `_sqlx_migrations` state) is refused at start-up, with no change.
   After start-up, alrust verifies the catalog it needs (the same comparison
   as decision 2, at run time) and refuses to serve on a mismatch.
5. **`0002`: the alpha-to-reference conversion, in order, in one transaction.**
   1. Preflight, raising an exception that lists the offending ids (no
      narrowing, truncation or merge):
      - `alerts.timeout`, `alerts.duplicate_count` and every history
        element's `timeout` outside PostgreSQL `integer`;
      - groups of alerts sharing (environment, resource, event,
        COALESCE(customer, '')), which would violate `env_res_evt_cust_key`;
      - heartbeats sharing (origin, COALESCE(customer, '')) are already
        unique in `0001`;
      - any alert `value` or history `value` that rule 7 cannot convert.
   2. Create type `history` exactly as `schema.sql` does.
   3. `alerts.history` jsonb → `history[]`:
      `ARRAY(SELECT ROW(e->>'id', e->>'event', e->>'severity', e->>'status',
      <value rule 7>, e->>'text', e->>'type',
      ((e->>'updateTime')::timestamptz AT TIME ZONE 'UTC'), e->>'user',
      (e->>'timeout')::integer)::history FROM jsonb_array_elements(history)
      WITH ORDINALITY AS t(e, n) ORDER BY n)`. The array keeps its exact
      element order (newest first), including entries with equal
      timestamps, and its length (the history limit is untouched). JSON
      `null` and an absent key become SQL NULL; an empty string stays an
      empty string.
   4. `timestamptz` columns → `timestamp` with `AT TIME ZONE 'UTC'`, which
      does not depend on the session's TimeZone. Microsecond precision is
      kept (alrust writes milliseconds).
   5. `bigint` → `integer` (safe after the preflight); `value jsonb` →
      `text` by rule 7.
   6. Drop alrust's `NOT NULL` constraints and defaults that the reference
      does not have; add reference columns alrust lacks; create the tables
      alrust lacks by running the same definitions as `schema.sql`; replace
      alrust's identity index with `env_res_evt_cust_key` and its heartbeat
      index with `org_cust_key`.
6. **Composite mapping in code.** `HistoryEntry` ↔ `history`, fields in the
   type's positional order (id, event, severity, status, value, text, type,
   update_time, user, timeout), via `#[derive(sqlx::Type)]
   #[sqlx(type_name = "history")]` plus `PgHasArrayType` for `history[]`.
   The API's camelCase names (`type`, `updateTime`, `user`, `timeout`) are
   unchanged. `timeout` becomes `Option<i32>`, `update_time`
   `NaiveDateTime` read and written as UTC.
7. **`value` rule (needs your review; possibly the owner's).** The reference
   column and history field are `text`. The reference stores a string as
   is, an integer or boolean as Python `str()` ("5", "True",
   `models/alert.py:35-36` applies to `int`, which includes `bool`), and
   passes anything else to the database adapter. alrust today stores any
   JSON and returns it unchanged.
   - Proposed for new writes: a string is stored as is; an integer as its
     decimal string; a boolean as "True"/"False"; `null` as SQL NULL; a
     float, array or object is **400**, a documented divergence. The API
     then returns `value` as the stored string, as the reference does. That
     is an API-visible change for numbers (5 is returned as "5").
   - For alpha rows in `0002`: the same rule applied to stored jsonb. A
     stored float, array or object is a preflight refusal, not a rewrite.
   - This is a contract change, so I am flagging it for an explicit
     decision rather than choosing silently.
8. **NULL decoding.** Every reference-nullable column decodes as optional.
   - In the API: NULL `correlate`, `service` and `tags` read as `[]`, as the
     reference's constructor does (`or list()`).
   - NULL `attributes` reads as `{}`. The reference would fail here
     (`dict(None)` in `from_record`); alrust is deliberately tolerant.
   - NULL scalars read as JSON `null`, except where the lifecycle spec
     already fixes a read default (timeout NULL reads as `ALERT_TIMEOUT`,
     spec A1.3a).
   - Keys: a NULL `expire_time` reads as now + `API_KEY_EXPIRE_DAYS`, as
     the reference does (`models/key.py:31`). NULL `scopes` read as the
     default user scopes. The key's secret exposure and scope rules (Y6, Y7,
     Y33) are unchanged.
9. **Uniqueness.** `env_res_evt_cust_key` sits under the existing
   receive-path advisory lock, so it can only turn a race into a refusal. A
   unique violation is reported as a retryable 500, never as a silent merge.
   Heartbeat upserts keep `org_cust_key` semantics. The alpha limitation
   "multiple matching rows (R2.3)" goes away.
10. **Required objects** (the catalog check pins all of them): type
   `history` (10 attributes); tables `alerts` (28 columns incl.
   `update_time`), `notes`, `blackouts` (incl. `user`, `create_time`,
   `text`, `origin`), `customers`, `heartbeats` (incl. `attributes`),
   `keys`, `metrics` (bigint `count`, `total_time`), `perms`, `users`
   (incl. `login` UNIQUE NOT NULL), `groups`; their primary keys and UNIQUE
   constraints; indexes `env_res_evt_cust_key` and `org_cust_key`. alrust
   creates none of its own tables.
11. **First change's scope:** start-up handling of the three cases, `0002`,
   the catalog check (CI) and the store's type changes. The existing smoke
   and unit tests pass, except value assertions that change under rule 7,
   which the change will list. No new endpoints.

## Open questions

1. Rule 7 (`value` as text): adopt as proposed, choose another rule, or treat
   it as an owner contract decision.
2. Ownership and roles for the integrations (Voalack requires a runtime
   login distinct from its roles and owning none of their objects,
   `boundary.py:851-866`): a later deployment change.
3. Schema equality does not prove integration compatibility. Hook, auth and
   permission contracts, transaction behaviour and the integration tests are
   later ADRs. Nothing in another project changes now.

## Consequences

- alrust's database becomes interchangeable with the reference's at the
  schema level, which is the precondition for the integrations' SQL and for
  a future data migration from a real Alerta install.
- The alpha's own storage choices (jsonb history, timestamptz) are given up.
- Later ADRs: the ack hook for the integrations' in-process layer (owner
  choice A or B pending), user logins and customer views.
