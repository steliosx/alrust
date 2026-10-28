# ADR-0005: alrust's store adopts the reference PostgreSQL layout

- Status: **Accepted in design; implementation under review (PR #150).**
  Direction set by the owner on 2026-10-13 (DECISIONS Y35): "1) yes. Alrust
  needs to fully support zabalack and voalack." The `value` rule (decision
  8) was decided on 2026-10-28: option (b), DECISIONS Y38. Named extra
  indexes are allowed in principle (Y40); the list is proposed in decision 9
  and the strict rule stays in force until Codex has reviewed it.
- Date: 2026-10-13
- Revision 5. It answers Codex's design prerequisites (afaeaa46), findings
  ADR5-CAT-1, ADR5-ADOPT-1, ADR5-VALUE-1 and ADR5-RANGE-1 (e06bdc32),
  ADR5-NULLAUTH-1 (f0abfc75), and ADR5-COPY-1 and ADR5-DEPS-1 (8b013c32).

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
  `format_type`, `attnotnull`, **collation**, the default (`pg_get_expr`)
  and identity/generated status;
- the table state that changes behaviour: persistence, row-level security
  (enabled and forced) and policies, rules, inheritance parents and
  children, partitioning, storage options, tablespace, replica identity;
- the composite type's attributes in order;
- the constraints, by name, `contype` and `pg_get_constraintdef`;
- the indexes, by name and `pg_get_indexdef`, with `indisvalid`,
  `indisready` and `indislive` (a failed `CREATE INDEX CONCURRENTLY`
  leaves the same definition but enforces nothing).

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

**CI also proves repeat starts:** a second start on databases B, C and D
applies nothing, adopts nothing and passes the catalog check. A start
against a database where `0002` failed (a fixture violating each preflight,
and an injected copy mismatch) leaves the alpha catalog and the row
checksums unchanged.

**At every start**, after the path in decision 4, alrust compares the live
catalog with the embedded file and refuses to serve on any mismatch,
listing each difference.

### 3. `0002_reference_layout.sql`: alpha to reference, in one transaction

SQLx runs a migration in one transaction. Any failure, including every
`RAISE` below, rolls back all of it: the alpha schema and its rows stay
exactly as they were, `0002` is not recorded, and alrust exits with the
reason. `0001` is never edited, so its applied checksum stays valid.

0. **Lock and dependency preflight (ADR5-DEPS-1), before any change:**
   - `SET LOCAL lock_timeout = '30s'`, then `LOCK TABLE alerts, heartbeats,
     keys IN ACCESS EXCLUSIVE MODE`. Writers that are not alrust block until
     commit, or the migration fails on the timeout and nothing changes.
     alrust instances are already serialised by the start-up lock
     (decision 4).
   - Raise, listing each object, if any of these exist:
     - for `alerts`, `heartbeats` or `keys`: a foreign key from another
       table; a view, materialized view or rule depending on them
       (`pg_depend`/`pg_rewrite`); a non-internal trigger; row-level
       security enabled or a policy; publication membership; inheritance or
       partitioning; extended statistics;
     - for `alerts` or `heartbeats`: a sequence owned by one of their
       columns (`pg_depend` deptype `a` or `i`), with or without a column
       default. PostgreSQL drops an owned sequence, and its state, together
       with the old table, and the row copy checks cannot see that
       (ADR5-OWNED-SEQ-1, Codex 2026-10-28). An unowned sequence in
       `public` is untouched and survives;
     - a table or column ACL other than the default (`relacl`/`attacl` not
       NULL);
     - an owner other than the migrating role.
   - An alpha database created by alrust has none of these. Anything else
     is an unsupported layout, refused rather than carried over. **Nothing
     is dropped with CASCADE**, so no external object can be silently
     removed. The new tables get the migrating role as owner and default
     privileges, the same as the alpha tables had.
1. **Value preflight.** Raise an exception listing every offending id;
   nothing is narrowed, truncated, merged or skipped:
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
   4. **Verify every field before dropping anything (ADR5-COPY-1).** One
      query does a `FULL JOIN` of `alerts_alpha` and `alerts` on `id`. It
      counts the rows where:
      - either side is missing; or
      - **any** of the 28 columns `IS DISTINCT FROM` the declared
        conversion of the alpha value. Unchanged columns are compared
        directly. Timestamps are compared both as `new = old AT TIME ZONE
        'UTC'` and back as `new AT TIME ZONE 'UTC' = old`. Integers are
        compared as `new::bigint = old`, and `value` by the decision-8 rule.

      A second query does a `FULL JOIN` on (id, ordinal) of
      `unnest(alerts.history) WITH ORDINALITY` and
      `jsonb_array_elements(alerts_alpha.history) WITH ORDINALITY`. It counts
      elements that are missing on either side, or whose 10 fields are
      distinct from the declared conversion (NULL-safe, field by field, not
      a whole-record comparison).

      A non-zero count raises, and the transaction rolls back. The
      conversion rules themselves are proved separately by CI fixtures that
      cover every field, NULLs, `""`, `[]`, equal timestamps and each
      `value` type (database C).
   5. Drop `alerts_alpha` and create `env_res_evt_cust_key` verbatim.

   Rows are dropped only after they are verified copied, inside the same
   transaction. This is a copy, not a destructive recreate.
4. **`heartbeats`** is rebuilt the same way: the reference's `CREATE
   TABLE`, then `ADD COLUMN attributes`; copy; the same full per-field
   verification over all 9 columns; drop. Then `org_cust_key` replaces
   `heartbeats_origin_customer_idx`.
5. **`keys`** already has the reference's column order and is altered in
   place:
   - `timestamptz` → `timestamp` (using `AT TIME ZONE 'UTC'`);
   - drop `NOT NULL` and the defaults the reference lacks;
   - `count` stays `bigint`, as in the reference.
   - It is verified the same way: a temporary snapshot (`CREATE TEMP TABLE
     … ON COMMIT DROP AS SELECT * FROM keys`) taken before the `ALTER`s is
     compared field by field with the result afterwards.
6. **`notes`, `blackouts`, `customers`, `metrics`, `perms`, `users` and
   `groups`** are created by the reference's statements, copied verbatim and
   in the reference's order. That includes its later `ALTER`s and the `users`
   `login` DO block, so the positions and constraint names match.
   That block's `UPDATE users SET login = email` runs here only on the
   `users` table `0002` has just created, which is empty. alrust never runs
   the reference's `schema.sql` against an existing database (decision 4).

### 4. Three database states, decided at start-up under one lock

alrust holds SQLx's own migration lock on a dedicated connection for the
whole start-up decision. That covers state detection, then the migrator
(run with its own locking off on that connection) or the adoption, then
the run-time catalog check. Detection is read-only. A second alrust
instance, or any SQLx migrator on the database, waits.

**Every session is pinned** with `search_path=public,pg_temp` (PostgreSQL
then searches `pg_catalog` first implicitly), so built-ins cannot be
shadowed, unqualified DDL creates in `public`, no role schema is searched,
and temporary tables come last. Start-up refuses a connection whose
effective order (`current_schemas(true)`) is anything else.

**Adoption writes nothing** (amended after hosted run 38458334716 measured
that a standalone `ALTER TYPE history ADD ATTRIBUTE` does not wait for a
lock on `alerts`: the table locks available to alrust's role therefore could
not keep a recorded baseline in step with the type, and no other exclusion
is known to work without superuser). An existing reference database is never migrated and
never gets an SQLx baseline; every start just checks it read-only. The
earlier baseline existed only so the migrator could validate, and alrust
applies no migrations to a reference database, so it bought nothing and
cost the race. **Not solved, deferred:** if alrust ever has to change the
schema of a served reference database (a future reference re-pin, say),
that needs its own design.

1. **Fresh** (no `_sqlx_migrations`, no `public.alerts`, no type
   `public.history`): the SQLx migrator applies `0001` and then `0002`, the
   latter on empty tables. Migration history is then the same as on an
   upgraded alpha database.
2. **Applied alpha** (`_sqlx_migrations` holds version 1 with alrust's `0001`
   checksum): the migrator applies `0002` (decision 3). If the checksum is
   wrong or a version is unknown, SQLx's own refusal applies.
3. **Existing reference database** (no `_sqlx_migrations`, but
   `public.alerts` and type `public.history` exist). This is adoption, not a
   migration, and **it writes nothing**, on the first start or any later
   one:
   1. No migrator runs and no `_sqlx_migrations` is created.
   2. The common catalog check below compares the live catalog with
      `reference-catalog.json` under ACCESS SHARE locks on the reference
      tables. These conflict only with ACCESS EXCLUSIVE, so they keep the
      DDL that takes it (most `ALTER TABLE` forms, `DROP`, `TRUNCATE`) from
      landing mid-read, without blocking Alerta's writers, and need only
      SELECT (per the PostgreSQL 18 `LOCK` documentation, any stronger mode
      needs UPDATE, DELETE, TRUNCATE or MAINTAIN, which a split-role
      deployment's runtime login may lack). **They do not exclude all
      catalog DDL:** `CREATE INDEX` (plain or `CONCURRENTLY`), some `ALTER
      TABLE` forms and standalone `ALTER TYPE` take weaker or different
      locks and can still change the catalog during the check. The type's
      attributes are read by one statement, so that part is one consistent
      snapshot; the catalog is not frozen for the rest of the check or
      afterwards. A change that lands then is runtime drift, refused at the
      next start; in either case alrust has written nothing.
   3. **On any difference, alrust refuses to serve, listing the
      differences.** This includes a database created by an older Alerta,
      which is upgraded by starting that Alerta 9.1.0 once, outside alrust.
   4. Tests prove "nothing written" positively: statement-level triggers on
      all ten tables that reject any write, an event trigger that rejects
      any DDL, no `_sqlx_migrations` afterwards, and unchanged row
      checksums.
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
  - For example, key authentication's `expire_time > now()` becomes
    `expire_time > (now() AT TIME ZONE 'UTC')`.
  - As a second defence, every session is UTC: SQLx itself sends a
    `TimeZone=UTC` startup parameter, and `session_options` pins it too.
    The tests move their sessions to a far-from-UTC zone after connecting,
    to prove nothing depends on it.
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

**A NULL in a reference-nullable column never fails a read silently or
gets an invented value.** Integrations and the reference's own code may write
such rows. The reference's model (`Alert.__init__`, `models/alert.py:52-76`)
keeps some NULLs as `None` and replaces others with a constant through
Python's `or`, which replaces the empty string too. alrust follows it column
by column:

| Columns | NULL reads as | Why |
|---|---|---|
| alerts `value`, `raw_data`, `customer`, `update_time`, `duplicate_count`, `repeat`, `previous_severity`, `trend_indication`, `last_receive_id`, `last_receive_time` | JSON `null` | the model keeps `None`; `update_time` is NULL in rows written before the column existed. A duplicate leaves a NULL `duplicate_count` NULL (`duplicate_count + 1`); a NULL `previous_severity` is the engine's default |
| alerts `timeout` | `ALERT_TIMEOUT` in the API (A1.3a); stored NULL is kept | as today |
| alerts `environment`, `severity`, `status`, `group`, `text`, `type` | when NULL **or empty**: `""`, `normal`, `open`, `Misc`, `""`, `exceptionAlert` | the model's constants, through `or` |
| alerts `correlate`, `service`, `tags`; heartbeats `tags` | `[]` | the model's `or list()` |
| alerts and heartbeats `attributes` | `{}` | the reference would fail (`dict(None)`); alrust is deliberately tolerant |
| heartbeats `type` (NULL or empty), `timeout` (NULL) | `Heartbeat`, alrust's heartbeat default | the model's constants |
| alerts `origin`, `create_time`, `receive_time`; heartbeats `create_time`, `receive_time`; history `id`, `event`, `type`, `update_time` | **the row is unreadable**: the request fails with 500 and the log names the table, row id and column | **a deliberate divergence, for decision:** the reference always writes these itself, and for a NULL its model would show a moving `now()`, a host-specific origin, or (history) `null` fields that its own sorting and filtering then mishandle. alrust refuses rather than invent a value |
| history `severity`, `status`, `value`, `text`, `user`, `timeout` | JSON `null` | as today |
- **Keys: fail closed (ADR5-NULLAUTH-1).** alrust itself never creates a
  NULL `expire_time` or NULL `scopes`. (NULL `customer` and NULL
  `last_used_time` are legitimate, as today.) A NULL expiry or scopes can
  only come from an adopted reference database or another writer, and is
  handled separately for each use:

  | Stored NULL | Authentication | Display (`GET /keys`, `GET /key/{id}`) | Edit (`PUT /key/{id}`) |
  |---|---|---|---|
  | `expire_time` | **refused**: the key is inactive, as today, because `expire_time > now` is never true for NULL | `"expireTime": null`; never a computed or moving default | an omitted `expireTime` keeps NULL (still inactive), as SF-KEYS preserves an omitted expiry; a supplied one is validated as today |
  | `scopes` | **refused**: no default scopes are granted | `"scopes": []` | an omitted `scopes` keeps NULL (still refused); a supplied value is validated as today (no empty list, within the editor's ceiling) |
  | `text`, `count`, `last_used_time`, `customer` | unaffected (a NULL `customer` means no customer, as today) | `""`, `0`, `null`, `null` | as today |

  - `user` is `NOT NULL` in the reference too.
  - Authentication refuses in SQL (`AND scopes IS NOT NULL`), so a refused
    key's `count` and `last_used_time` do not change.
  - Such a key lists with `"status": "inactive"`, a status only alrust uses
    (the reference shows `active` or `expired`), whatever its expiry.
  - At start-up alrust logs the number of keys inactive for these reasons,
    never their secrets.
  - The secret-exposure, expiry and scope-ceiling rules (Y6, Y7, Y33,
    SF-KEYS) are unchanged.
  - A key becomes active only through an edit that supplies a valid expiry
    and valid scopes. The owner may later choose a different policy for
    imported keys; that would be a separate decision.

**Uniqueness:**
- `env_res_evt_cust_key` sits under the receive path's existing advisory
  lock, so it can only turn a race into a refusal: a retryable 500, never a
  merge.
- Heartbeat upserts keep their current semantics under `org_cust_key`.
- The alpha limitation "multiple matching rows (R2.3)" goes away.

### 8. `value`: option (b), JSON text (ADR5-VALUE-1; DECISIONS Y38)

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
| `[1,2]`, `{"a":1}` | returned as JSON | **400** | stored as PostgreSQL's `jsonb` text form, returned as the strings `"[1, 2]"` and `"{\"a\": 1}"` |
| Alpha rows holding a float, array or object (in `0002`) | — | **migration refused**, listing ids | converted as above |
| Reference rows (any text) | — | returned verbatim as a string, never parsed | the same |
| History `value` | jsonb | the same rule as the alert's value | the same rule |

- Under (b), one SQL expression is used both by the API's writes and by
  `0002`: `CASE jsonb_typeof(v) WHEN 'string' THEN v #>> '{}' WHEN 'null'
  THEN NULL ELSE v::text END`. That makes the stored text the same however
  it got there. No database function is created, so nothing is added to an
  adopted catalog.
- Neither option is reversible: the stored text does not record the original
  JSON type, which is inherent to a `text` column.
- (a) loses no data that was accepted, but **rejects alerts** that a sender
  sends today.
- (b) accepts everything and keeps the content, but turns those types into
  strings on read.
- **Decided: (b)** (owner, 2026-10-28, Y38). A monitoring server should not
  drop alerts over the shape of an informational field, and integrations
  reading `history[1].value` see text either way.

**Implementation.** The expression is `alrust_store_pg::VALUE_TEXT`; the
generator embeds the same text in `0002` for `alerts.value` and for every
history element's `value`, and the static migration test checks both. On
the API path the store converts a non-string `value` with one
`SELECT <expression>` on the receive transaction before any comparison or
write, so duplicate and correlation checks and the history entry all see
the stored text (a repeated `1.5` is a duplicate, not a value change). The
former "decision 8 pending" refusal in the `0002` preflight is gone.

### 9. Extra indexes: exact proposal (DECISIONS Y40; awaiting Codex review)

The owner allows named performance indexes on the adopted tables (Y40).
Codex's review of the first draft (`f274d1fa`) asked for exact,
schema-qualified definitions and the comparison behaviour, and noted that
column lists alone differ from the dropped alpha definitions. This is that
proposal. **Nothing here is implemented: the strict rule (no index beyond
the reference's) stays in the code, in `0002` and in the catalog check
until Codex has reviewed this decision and a separate affected change
carries it.**

**Withdrawn, redundant with the reference's own unique indexes** (`0001`
definitions shown as dropped by `0002`):
- `CREATE INDEX alerts_identity_idx ON alerts (environment, resource,
  COALESCE(customer, ''))`: the reference's `env_res_evt_cust_key` on
  `(environment, resource, event, (COALESCE(customer, ''::text)))` serves
  every lookup on the `(environment, resource)` prefix, including the
  receive path's duplicate and correlation queries.
- `CREATE UNIQUE INDEX heartbeats_origin_customer_idx ON heartbeats
  (origin, COALESCE(customer, ''))`: the same key as the reference's
  `org_cust_key`.

**Candidates, exact DDL, each to be added only with a measurement that
shows the reference layout without it is too slow for a realistic data set
(the pinned reference runs without either):**

| Name | Exact definition | Serves |
|---|---|---|
| `alerts_last_receive_time_idx` | `CREATE INDEX alerts_last_receive_time_idx ON public.alerts USING btree (last_receive_time DESC)` | the default list order `ORDER BY last_receive_time DESC, id` and housekeeping's `last_receive_time <` range deletes |
| `alerts_status_idx` | `CREATE INDEX alerts_status_idx ON public.alerts USING btree (status)` | status filters and the severity-by-status counts |

**Comparison behaviour, if a candidate is adopted:**
- `reference-catalog.json` keeps describing the reference exactly. A
  second committed file, `allowed-extra-indexes.json`, lists each adopted
  index by schema-qualified name, its full `pg_get_indexdef` text and the
  required `indisvalid`, `indisready` and `indislive` all true.
- The four-way CI comparison (decision 2, databases A to D) and the
  start-up check accept a live catalog only if it equals the reference
  catalog **plus exactly** the allowed list: an extra index not in the list,
  a listed index with a different definition, or a listed index that is
  missing, invalid, not ready or not live is a mismatch, reported by name.
  Databases A and D (the reference schema, and an adopted reference
  database) have none of the extra indexes and must still match, so the
  list is "allowed", not "required", on adopted databases; `0002` creates
  the adopted indexes on alrust-migrated databases (B and C) only.
- Zabalack's and Voalack's installers and triggers read tables and the
  `history` type, not alrust's indexes; an extra index changes no row,
  column, constraint or trigger. Their catalog checks, if any, are confirmed
  against the list in the hook-contract spec before adoption.
- `CREATE INDEX` runs inside `0002`'s transaction (not `CONCURRENTLY`,
  which cannot run in a transaction), under the same lock, so a failed
  creation rolls the whole migration back as every other step does.

**Order of work:** Codex review of this decision; the measurement on a
generated data set under the routine smoke rules (Y41) with the numbers
recorded here; then one affected change per adopted index (`0002`, the
allowed list, the catalog check, its tests), reviewed on its exact head.

## Open questions

1. The `value` rule (decision 8): decided, (b).
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
