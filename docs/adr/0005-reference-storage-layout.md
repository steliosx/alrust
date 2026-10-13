# ADR-0005: alrust's store adopts the reference PostgreSQL layout

- Status: **Proposed** (design only; no code). Direction set by the owner on
  2026-10-13 (to be recorded as DECISIONS Y35): "1) yes. Alrust needs to
  fully support zabalack and voalack." **One part is pending an owner
  decision: the `value` rule (decision 8).**
- Date: 2026-10-13
- Revision 3. It answers Codex's design prerequisites (afaeaa46) and findings
  ADR5-CAT-1, ADR5-ADOPT-1, ADR5-VALUE-1 and ADR5-RANGE-1 (e06bdc32).

## Why

Zabalack (zab-al-ack) and Voalack attach to Alerta's own database:
- an `AFTER UPDATE` trigger on `public.alerts` reads `(NEW.history[1])` as
  Alerta's composite `history` type, and reads `NEW.customer` and
  `NEW.status`;
- Zabalack writes `alerts.attributes`;
- Voalack writes `public.notes` in the reference's layout;
- Zabalack's installer refuses to run unless `public.alerts` and the type
  `public.history` exist (`sql/migrations/001_core.sql:8`).

alrust's alpha store (`crates/alrust-store-pg/migrations/0001_initial.sql`)
has the reference's column names but its own types: history as `jsonb`,
`timestamptz`, `value jsonb`, `bigint` counters, `NOT NULL` defaults. It also
has its own column order and indexes. Neither integration's database part
can attach to that.

This ADR covers the store only. Schema equality is necessary for the
integrations but does not prove they work. Their in-process layer, logins,
customer views, permissions and roles are later ADRs, and nothing in another
project changes now.

## Decision

### 1. Target: the reference catalog, exactly, including physical order

The target is what the pinned reference `schema.sql` (`f4312727`, 198 lines)
creates on PostgreSQL 18.6 in schema `public`:
- type `history`, with 10 attributes in this order: id, event, severity,
  status, value, text, type, update_time, user, timeout;
- the tables `alerts`, `notes`, `blackouts`, `customers`, `heartbeats`,
  `keys`, `metrics`, `perms`, `users` and `groups`;
- their primary-key and UNIQUE constraints, with the reference's names
  (`alerts_pkey`, `keys_key_key`, `perms_match_key`, `users_email_key`,
  `users_login_key`, `groups_name_key`, …);
- the unique indexes `env_res_evt_cust_key` and `org_cust_key`.

**Exact means physical, not only semantic:**
- the same columns in the same order (`alerts` ends `…, history,
  update_time`; `heartbeats` ends `…, customer, attributes`);
- the same types, nullability and defaults (the reference has no defaults);
- the same constraint and index definitions and names;
- **no other index, constraint or default on those tables.** alrust's four
  alpha-only indexes (`alerts_identity_idx`, `alerts_last_receive_time_idx`,
  `alerts_status_idx`, `heartbeats_origin_customer_idx`) are removed. Any
  later performance index would be a separate, explicit decision.

The reference's own code reads by column name, so order matters only for
`SELECT *` consumers and for the composite type, where position is
meaningful. Exact order is still the target, because it costs only the
rebuild in decision 3 and removes the question entirely.

### 2. The catalog check, and what it excludes

`reference-catalog.json` is a canonical description of the target. CI
generates it by applying the pinned `schema.sql` to an empty PostgreSQL
18.6 database, and fails if the committed file differs. For each of the ten
tables and the type it records:
- the columns in `attnum` order, skipping dropped columns, with name,
  `format_type`, `attnotnull`, the default (`pg_get_expr`) and
  identity/generated status;
- the composite type's attributes in order;
- the constraints, by name, `contype` and `pg_get_constraintdef`;
- the indexes, by name and `pg_get_indexdef`.

**Excluded, and stated as excluded:**
- `_sqlx_migrations`, which is alrust's own migration bookkeeping;
- other schemas;
- ownership and privileges, which belong to the later deployment ADR;
- **at run time only,** non-internal triggers on these tables, so that the
  integrations' triggers can attach. They are listed in the start-up log.

**CI compares four databases against that file. All four must match
exactly:**
- (A) the reference `schema.sql`, applied twice (it is idempotent);
- (B) alrust's migrations on an empty database;
- (C) alrust `0001`, then synthetic alpha rows, then `0002`;
- (D) database A after alrust has adopted it (decision 4).

**At every start**, after the path in decision 4, alrust compares the live
catalog with the embedded file and refuses to serve on any mismatch,
listing each difference.

### 3. `0002_reference_layout.sql`: alpha to reference, in one transaction

SQLx runs a migration in one transaction. Any failure, including every
`RAISE` below, rolls back all of it: the alpha schema and its rows stay
exactly as they were, `0002` is not recorded, and alrust exits with the
reason. `0001` is never edited, so its applied checksum stays valid.

1. **Preflight.** Raise an exception listing every offending id; nothing is
   narrowed, truncated, merged or skipped:
   - `alerts.timeout`, `alerts.duplicate_count`, `heartbeats.timeout`, and
     every history element's `timeout`, outside PostgreSQL `integer`;
   - alert groups sharing (environment, resource, event,
     COALESCE(customer, '')), which would violate `env_res_evt_cust_key`
     (heartbeats are already unique on `org_cust_key`'s expression);
   - any alert or history `value` that the decision-8 rule cannot convert.
     Under option (b) there are none.
2. `CREATE TYPE history`, with the reference's attributes in order.
3. **`alerts` is rebuilt by copy, because PostgreSQL cannot reorder
   columns:**
   1. Rename the alpha table and its primary-key index to `alerts_alpha` and
      `alerts_alpha_pkey`, and drop the three alpha-only `alerts_*` indexes.
   2. Create `alerts` with the reference's statements verbatim (`CREATE
      TABLE` with `history history[]`, then `ALTER TABLE … ADD COLUMN
      update_time`), so its order and names come out exactly as in the
      reference.
   3. `INSERT INTO alerts (<explicit column list>) SELECT <conversions> FROM
      alerts_alpha`, with these conversions:
      - **timestamps:** `col AT TIME ZONE 'UTC'` (`timestamptz` →
        `timestamp`, independent of the session TimeZone; microseconds are
        kept);
      - **counters:** `::integer`, which is safe after the preflight;
      - **`value`:** the decision-8 rule;
      - **`history`:**
        `ARRAY(SELECT ROW(e->>'id', e->>'event', e->>'severity',
        e->>'status', <value rule>, e->>'text', e->>'type',
        ((e->>'updateTime')::timestamptz AT TIME ZONE 'UTC'), e->>'user',
        (e->>'timeout')::integer)::history FROM jsonb_array_elements(h.history)
        WITH ORDINALITY AS t(e, n) ORDER BY n)`. This keeps the element order
        exactly, newest first, including entries with equal timestamps, and
        keeps the array length (the history limit is untouched). JSON `null`
        and absent keys become SQL NULL; `""` stays `""`; `[]` stays `[]`.
   4. **Verify before dropping anything:**
      - the row counts are equal;
      - the id sets are equal in both directions (`EXCEPT`);
      - for every id, `cardinality(history)` equals
        `jsonb_array_length` of the alpha history.

      Any mismatch raises.
   5. Drop `alerts_alpha` and create `env_res_evt_cust_key` verbatim.

   Rows are dropped only after they are verified copied, inside the same
   transaction. This is a copy, not a destructive recreate.
4. **`heartbeats`** is rebuilt the same way (the reference's `CREATE TABLE`,
   then `ADD COLUMN attributes`; copy; verify; drop). Then `org_cust_key`
   replaces `heartbeats_origin_customer_idx`.
5. **`keys`** already has the reference's column order and is altered in
   place:
   - `timestamptz` → `timestamp` (using `AT TIME ZONE 'UTC'`);
   - drop `NOT NULL` and the defaults the reference lacks;
   - `count` stays `bigint`, as in the reference.
6. **`notes`, `blackouts`, `customers`, `metrics`, `perms`, `users` and
   `groups`** are created by the reference's statements, copied verbatim and
   in the reference's order. That includes its later `ALTER`s and the `users`
   `login` DO block, so the positions and constraint names match.

### 4. Three database states, decided at start-up under the migration lock

1. **Fresh** (no `_sqlx_migrations`, no `public.alerts`, no type
   `public.history`): the SQLx migrator applies `0001` and then `0002`, the
   latter on empty tables. Migration history is then the same as on an
   upgraded alpha database.
2. **Applied alpha** (`_sqlx_migrations` holds version 1 with alrust's `0001`
   checksum): the migrator applies `0002` (decision 3). If the checksum is
   wrong or a version is unknown, SQLx's own refusal applies.
3. **Existing reference database** (no `_sqlx_migrations`, but
   `public.alerts` and type `public.history` exist). This is an adoption,
   not a migration. In one transaction:
   1. Compare the live catalog with `reference-catalog.json`, read-only.
   2. **On any difference, roll back and refuse, listing the differences.**
      Nothing is written, not even `_sqlx_migrations`. This includes a
      database created by an older Alerta, which is upgraded by starting
      that Alerta 9.1.0 once, outside alrust.
   3. If the catalog is exact, create `_sqlx_migrations` with SQLx's own
      definition and record a **baseline**: versions 1 and 2 with their
      embedded checksums, `success = true`, and a description beginning
      `baseline: adopted reference schema`.
   4. No table, column, constraint or row is touched. CI database D proves
      this: a checksum of every row of every reference table, taken before
      and after adoption, must be equal.
4. **Anything else is refused at start-up, with nothing written.** For
   example: `public.alerts` without the type, the type without
   `public.alerts`, `_sqlx_migrations` without `public.alerts`, or a
   partial set of reference tables.

### 5. Code adaptations and time handling

- `HistoryEntry` ↔ `history`, positional, via `#[derive(sqlx::Type)]
  #[sqlx(type_name = "history")]` and `PgHasArrayType` for `history[]`.
  The API's camelCase field names are unchanged.
- **Timestamps:** bound as `NaiveDateTime` from `DateTime<Utc>::naive_utc()`,
  and read back with `.and_utc()`.
  - SQL that compares with the current time uses `(now() AT TIME ZONE
    'UTC')` or a bound parameter, never a bare `now()` against a
    `timestamp` column, because that comparison depends on the session
    TimeZone.
  - As a second defence, every pool connection runs `SET TIME ZONE 'UTC'`.
  - Round trips stay exact: alrust writes milliseconds, and the column holds
    microseconds.
- **Widths:** the domain and API types keep their current `i64` widths. The
  store converts with `i32::try_from` at the boundary, never with `as`.

### 6. Ranges (ADR5-RANGE-1)

| Value | Today | Proposed | Contract change? |
|---|---|---|---|
| Incoming alert `timeout` (POST /alert, PUT /status) | any non-negative i64 | above 2147483647 → **400** "timeout out of range" | Yes, a narrowing. The reference's outcome at the database is unqualified, and the 400 is a documented divergence |
| Incoming heartbeat `timeout` | any non-negative i64 | same, **400** | Same |
| `ALERT_TIMEOUT`, `HEARTBEAT_TIMEOUT` settings | i64 | above 2147483647 → start-up refused | Configuration only |
| `duplicate_count` at 2147483647 | i64, effectively unbounded | the next duplicate's `UPDATE` raises "integer out of range"; **the whole receive transaction rolls back** and the client gets 500. The stored alert is unchanged; no wrap and no saturation | Yes, but only after 2.1 billion duplicates of one alert |
| History `timeout` | i64 | copied from the alert's bounded `timeout`; the migration preflight covers stored values | No |
| `keys.count` | bigint | bigint | No |

### 7. NULL decoding and uniqueness

**Every column the reference leaves nullable is read as optional.**
Integrations and the reference's own code may write such rows.
- NULL `correlate`, `service` and `tags` read as `[]`, as the reference's
  constructor does.
- NULL `attributes` reads as `{}`. The reference would fail here
  (`dict(None)`); alrust is deliberately tolerant.
- NULL scalars read as JSON `null`, except where the spec already fixes a
  read default: a NULL `timeout` reads as `ALERT_TIMEOUT` (A1.3a).
- **Keys:** a NULL `expire_time` reads as now + `API_KEY_EXPIRE_DAYS`
  (`models/key.py:31`), and NULL `scopes` read as the default user scopes.
  The secret-exposure and scope rules (Y6, Y7, Y33) are unchanged.

**Uniqueness:**
- `env_res_evt_cust_key` sits under the receive path's existing advisory
  lock, so it can only turn a race into a refusal: a retryable 500, never a
  merge.
- Heartbeat upserts keep their current semantics under `org_cust_key`.
- The alpha limitation "multiple matching rows (R2.3)" goes away.

### 8. `value`: OWNER DECISION PENDING (ADR5-VALUE-1)

The reference column, and the composite's `value` field, are `text`. alrust
already follows the reference for strings, integers, booleans and null
(spec R0.3): it stores strings as they are, integers as decimal strings,
`true`/`false` as `"True"`/`"False"`, and null as null. **What it still
stores as JSON is floats, arrays and objects.** The reference passes those
to its database adapter unconverted, and what it then stores or answers is
**unqualified** (not measured).

| Incoming or stored | Today (jsonb) | Option (a): reject | Option (b): JSON text |
|---|---|---|---|
| `"abc"` | `"abc"` | `"abc"` | `"abc"` |
| `5`, `true` | `"5"`, `"True"` (R0.3) | unchanged | unchanged |
| `null` / absent | `null` | `null` (SQL NULL) | `null` (SQL NULL) |
| `1.5` | `1.5` (a number) | **400** | stored `1.5`, returned as the string `"1.5"` |
| `[1,2]`, `{"a":1}` | returned as JSON | **400** | stored as compact JSON text, returned as the strings `"[1,2]"` and `"{\"a\":1}"` |
| Alpha rows holding a float, array or object (in `0002`) | — | **migration refused**, listing ids | converted as above |
| Reference rows (any text) | — | returned verbatim as a string, never parsed | the same |
| History `value` | jsonb | the same rule as the alert's value | the same rule |

- Neither option is reversible: the stored text does not record the original
  JSON type, which is inherent to a `text` column.
- (a) loses no data that was accepted, but **rejects alerts** that a sender
  sends today.
- (b) accepts everything and keeps the content, but turns those types into
  strings on read.
- **Recommended: (b).** A monitoring server should not drop alerts over the
  shape of an informational field, and integrations reading
  `history[1].value` see text either way.

**Implementation of `value` waits for the owner's choice.** The decisions
above do not depend on it, except its use in the `0002` preflight and the
conversion.

## Open questions

1. The `value` rule (decision 8): **owner**.
2. Ownership and roles for the integrations (Voalack requires a runtime
   login distinct from its roles and owning none of their objects,
   `boundary.py:851-866`): a later deployment ADR.
3. The integration contracts (hook, auth, permissions, transaction
   behaviour) and their tests: later ADRs.

## Consequences and first change's scope

- alrust's database becomes interchangeable with the reference's at the
  schema level. This is the precondition for the integrations' SQL and for
  adopting a real Alerta database.
- **The API-visible changes** are the `value` rule (decision 8) and the
  range refusals (decision 6). Everything else keeps the current API
  behaviour.
- **The first change** contains:
  - `0002`;
  - the start-up paths;
  - `reference-catalog.json`;
  - CI databases A–D;
  - the store's type changes;
  - the decision-6 refusals;
  - the decision-8 rule once chosen.

  It lists every smoke or unit assertion that changes.
