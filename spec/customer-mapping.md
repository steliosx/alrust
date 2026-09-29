# Customer mapping (Phase 0, spec item 3)

How the reference server decides an alert's `customer`, and where `customer`
changes behavior afterwards.

- Source: `reference/alerta` @ v9.1.0 (`f4312727783dad64198b62ed778bbca98a5144a3`).
  Citations are `path:line` inside that tree.
- Evidence level: **source-derived only.** No claim below has yet been checked
  against a running reference server. Each rule marked **[V]** is a candidate
  conformance vector. codex-alrust owns the suite and decides which ones to
  adopt and in what form.
- Scope: PostgreSQL backend only. MongoDB code paths are ignored.

## 1. Request identity → `g.customers`

`permission()` (`alerta/auth/decorators.py`) tries each credential in a fixed
order and sets `g.customers` from the first one that is present:

| Order | Credential | `g.customers` | `g.scopes` | Cite |
|---|---|---|---|---|
| 1 | API key: `Authorization: Key <k>`, else `X-API-Key: <k>`, else `?api-key=<k>` | `[key.customer]` if the key has a customer, else `[]` | `key.scopes` | `:31-40`, `:48-49` |
| 2 | Hawk HMAC | `[]` | `ADMIN_SCOPES` = `admin, read, write` | `:56-66`, `models/enums.py:144` |
| 3 | Bearer JWT | `jwt.customers` | `jwt.scopes` | `:85-86` |
| 4 | Auth proxy header | `get_customers(login, groups)` | role lookup | `:116` |
| 5 | Basic auth | `get_customers(email, [domain])` | role lookup | `:147` |
| 6 | No credential, `AUTH_REQUIRED=False` | `[]` | `[]` | `:156-161` |
| 7 | No credential, `AUTH_REQUIRED` and `ALLOW_READONLY` | `[]` | `READONLY_SCOPES` | `:164-169` |

Rules:

- **C1 [V]** An API key is checked **before** `AUTH_REQUIRED`. An unknown or
  expired key gives `401` with the message `API key parameter '<key>' is
  invalid`, even when auth is disabled (`:42-45`). The message echoes the
  submitted key string.
- **C2 [V]** A key is valid when a row matches `id=<k> OR key=<k>`
  (`database/backends/postgres/base.py:947-953`) **and**
  `expire_time > utcnow()`. A successful check updates the key's last-used
  time (`models/key.py:162-167`). Presenting the key **id** instead of the
  secret also authenticates.
- **C3** For users, `get_customers()` returns `[]` unless `CUSTOMER_VIEWS` is
  on (`auth/utils.py:42-49`), and a JWT carries a `customers` claim only when
  `CUSTOMER_VIEWS` is on (`models/token.py:111-112`). **For API keys there is
  no such guard**: the key's customer reaches `g.customers` whatever
  `CUSTOMER_VIEWS` says (`auth/decorators.py:48`).
- **C4** `CUSTOMER_VIEWS=True` with `AUTH_REQUIRED=False` is a startup error
  (`utils/config.py:117-118`). The default is `CUSTOMER_VIEWS=False`
  (`settings.py:76`).

## 2. Assigning `customer` on write: `assign_customer(wanted, permission)`

`alerta/utils/api.py:14-28`. `wanted` is the `customer` field of the submitted
object. `permission` defaults to `admin:alerts`.

1. If `wanted` is set:
   - if `'admin'` **or** `permission` is in `g.scopes` (an exact list
     membership test, see C6), the result is `wanted`;
   - else, if `wanted` is not in `g.customers`, the request fails with **400**
     `not allowed to set customer to '<wanted>'`;
   - else the result is `wanted`.
2. Otherwise, if `g.customers` has exactly one entry, the result is that entry.
3. Otherwise, if `g.customers` has more than one entry, the request fails with
   **400** `must define customer as more than one possibility`.
4. Otherwise the result is `null`.

Rules:

- **C5 [V]** A `customer` of `""` is rejected with 400 `customer must not be an
  empty string` at parse time, before assignment (`models/alert.py:93-94`).
  Heartbeats have the same check (`models/heartbeat.py:80-81`).
- **C6 [V]** The scope test is `Scope.admin in g.scopes or permission in
  g.scopes`. It is **not** `Permission.is_in_scope()`
  (`models/permission.py:90-110`). A key scoped `admin:alerts` may override
  the customer, and so may a key scoped `admin`. A key scoped `write:alerts`
  may not, and neither may a key whose only scope is `write`, even though
  `write` satisfies `write:alerts` for the endpoint itself.
- **C7 [V]** A key with customer `X` and no admin scope:
  - an alert that omits `customer` is stored with `X`;
  - an alert with `customer: "X"` is stored with `X`;
  - an alert with `customer: "Y"` is rejected with 400.
- **C8 [V]** A key with customer `X` and scope `admin:alerts`: an alert with
  `customer: "Y"` is stored with `Y`.
- **C9 [V]** A key with no customer and no admin scope: an alert that omits
  `customer` is stored with `null`; an alert with any non-empty `customer` is
  rejected with 400 (`g.customers == []`).
- **C10 [V]** With no credentials and `AUTH_REQUIRED=False`, `g.scopes == []`.
  An unauthenticated alert that names any `customer` is therefore **rejected
  with 400**. This is the configuration `conformance/scripts/run-reference.sh`
  uses today, and it has not been verified live.
- **C11** Other callers of `assign_customer`:
  - `POST /alert` (`views/alerts.py:49`) and `/webhooks/<name>`
    (`webhooks/custom.py:47`) use the default `admin:alerts`;
  - `POST /key` (`views/keys.py:33`) uses `admin:keys`;
  - heartbeats (`views/heartbeats.py:27`) use `admin:heartbeats`;
  - blackouts (`views/blackouts.py:32`, `:114`) use `admin:blackouts`.

  A key can therefore mint a child key only for its own customer unless it
  holds `admin` or `admin:keys`.
- **C12** Assignment runs **before** the plugin chain (`views/alerts.py:49`
  then `:56`; `webhooks/custom.py:47` then `:55`). Python plugins can rewrite
  `customer` after assignment. alrust has no Python plugins (KICKOFF), so it
  must pin down which default-plugin effects it reproduces. The default chain
  is `remote_ip, reject, heartbeat, blackout, forwarder` (`settings.py:272`).
- **C13 [V]** When a key with customer `X` sends an alert whose event is
  `Heartbeat`, the heartbeat plugin creates a heartbeat with customer `X`
  (`plugins/heartbeat.py:30`) and the request returns `202`. This matters for
  the per-tenant "Zabbix went quiet" alerting in Phase 5.

## 3. `customer` as part of alert identity

- **C14 [V]** `customer` is part of the dedup key and the correlation key.
  Every receive-time lookup and update adds `customer=%(customer)s` when the
  alert has a customer, and `customer IS NULL` otherwise:
  - `get_severity` and `get_status` (`database/backends/postgres/base.py:160-177`);
  - `is_duplicate` (`:179-188`) and `is_correlated` (`:190-198`);
  - `is_flapping` (`:200-214`);
  - `dedup_alert` (`:216-240`) and `correlate_alert` (`:242-262`).

  As a result, the same `environment/resource/event` under customers `A`,
  `B`, and `null` gives **three independent alerts**, each with its own
  duplicate count and severity history.
- **C15** Blackout matching compares customer only when `CUSTOMER_VIEWS` is on,
  and then as `customer IS NULL OR customer=<alert.customer>` (`:797-798`).
  With the default off, a blackout matches alerts of every customer.

## 4. `customer` as a read filter

- **C16 [V]** List, count, and top-N queries pass `customers=g.customers` into
  the query builder (`views/alerts.py:316`, `:364`, ...), which appends
  `AND customer=ANY(customers)` whenever the list is non-empty
  (`database/backends/postgres/utils.py:161-163`). Per-id reads and actions do
  the same through `get_alert` (`views/alerts.py:94-95`, `:117-118`, ...;
  `postgres/base.py:293-299`).
- **C17 [V]** Because of C3, a customer-scoped **API key** reads only alerts of
  its own customer, and never alerts with a `null` customer, even when
  `CUSTOMER_VIEWS=False`, and **even with `admin` scope**. For such a key, an
  alert of another customer answers `GET /alert/<id>` with 404. The same holds
  for status, action, tag, note, and delete.
- **C18** `get_alert` matches `id ~* '^<id>'` or `last_receive_id ~* '^<id>'`,
  a case-insensitive **regex prefix** on the raw path segment
  (`postgres/base.py:296-299`). This belongs to the API and lifecycle spec,
  not to customer mapping. It is recorded here because it decides which
  customer's alert a short id resolves to.

## Open questions (not decided here)

- **Q1: read filtering for customer-scoped keys (C17).** KICKOFF says
  `customer` is "a grouping/routing attribute, not a security boundary" and
  rules out per-customer RBAC. The reference nevertheless filters reads for
  customer-scoped API keys, even admin ones. Either alrust keeps this for
  compatibility, or it records an intentional divergence in an ADR. This
  changes the **authentication contract**, so it goes to the owner once Codex
  has reviewed the evidence. Knowing which consumers actually read with
  customer-scoped keys would settle it.
- **Q2 (C2):** does anything we run authenticate with a key **id** instead of
  the key secret? If not, that path is a candidate for a drop ADR.
- **Q3 (C10):** the black-box suite needs keys with known customers and
  scopes. That requires either `AUTH_REQUIRED=True` plus seeded keys, or
  creating keys through the API. The harness design is Codex's call.
