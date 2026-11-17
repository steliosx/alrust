# alrust alpha: quick start

The first runnable alrust: the Rust API server on PostgreSQL 18.7, speaking
the core of the Alerta v9.1.0 API. It is an **early alpha**: built to be
tried, not yet to replace the reference server. Behaviour follows
`spec/lifecycle.md`; the gaps are listed below.

## Run it

You need Docker with the compose plugin on an x86_64 (amd64) host. The image
build refuses other hosts; ARM64 is deferred.

```sh
export ALRUST_ADMIN_KEY=$(openssl rand -hex 20)   # your admin API key
docker compose up --build -d
curl http://localhost:8080/management/gtg          # -> OK (bound to 127.0.0.1; set ALRUST_LISTEN=0.0.0.0 to expose)
```

Send an alert and read it back:

```sh
curl -s -X POST http://localhost:8080/alert \
  -H "Authorization: Key $ALRUST_ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"resource":"web01","event":"HttpError","environment":"Production",
       "service":["Web"],"severity":"major","text":"500 on /login"}'

curl -s -H "Authorization: Key $ALRUST_ADMIN_KEY" http://localhost:8080/alerts
```

Run the end-to-end check (needs only Python 3):

```sh
python3 scripts/alpha-smoke.py http://localhost:8080 "$ALRUST_ADMIN_KEY"
```

Add `--disposable-target` only against a server and database you are about
to throw away: it also runs the housekeeping checks, and housekeeping is
global (it can expire or delete any alert).

Compose uses the same `ALRUST_DB_PASSWORD` value for PostgreSQL's initial
password and SQLx's `PGPASSWORD`; it is passed separately from the connection
URL, so reserved URL characters do not need encoding. The default `alrust`
password is for this disposable local trial. Compose does not publish the
database port to the host. Changing the environment variable is not a password
rotation for an already initialized volume.

Local `.env` and `.env.*` files are excluded from Git and the Docker build
context. Keep real credentials out of issues, logs and build inputs.

Stop with `docker compose down`; add `-v` to delete the database volume.

### Without Docker

Use a disposable PostgreSQL 18.7 development database. Set its connection
URL privately; do not commit real connection strings or credentials. The native
server defaults to listening on all interfaces, so bind it to loopback explicitly
for a local trial. Authentication is enabled by default; bootstrap a local admin
key before using the authenticated examples above:

```sh
export DATABASE_URL='<your disposable PostgreSQL 18.7 connection URL>'
export ALRUST_ADMIN_KEY="$(openssl rand -hex 20)"
export ALRUST_BIND=127.0.0.1:8080
cargo build --locked --release --bin alrust
target/release/alrust
```

These are setup instructions, not a recorded successful native or Compose run.
Start with a disposable database rather than an existing deployment.

### The database

alrust uses the reference Alerta's own PostgreSQL schema, exactly
(ADR-0005), so Alerta integrations that work in the database can attach to
it. At start-up it decides what it was pointed at:

- **An empty database:** creates the reference schema.
- **An alrust alpha database** (created before this change): converts it in
  place, in one transaction, checking every copied value. On any problem it
  stops with the reason and changes nothing. A database with other objects
  attached to its tables (views, triggers, foreign keys, grants) is refused.
- **An existing Alerta 9.1.0 database:** accepts it at the schema-adoption
  step if its schema matches the reference exactly, otherwise stops and lists
  the differences. An older Alerta database must first be upgraded by starting
  Alerta 9.1.0. Schema adoption does not make server startup or operation
  read-only: a nonempty `ALRUST_ADMIN_KEY` inserts a bootstrap key or updates
  the matching key's scopes and expiry. Authenticated key use records usage;
  API writes and enabled housekeeping can change or delete data. API keys
  with no expiry or no scopes cannot authenticate until edited.
- **Anything else:** stops with nothing written.

## What works

| Route | Notes |
|---|---|
| `POST /alert` | Parsing and defaults (R0), the reject policy (R1.2), heartbeat diversion (HB3), duplicate / correlate / create with the reference state machine (R2–R6) |
| `GET /alert/{id}` | Full id, id prefix or `lastReceiveId` |
| `GET /alerts`, `GET /alerts/count` | Filters: `id`, `resource`, `event`, `environment`, `severity`, `status`, `service`, `group`, `origin`, `tag`, and the exclusions `id!`, `resource!`, `event!`, `environment!`, `severity!`, `status!`, `group!`, `origin!` (for example `event!=X`; repeated values exclude each); `page`, `page-size`, `show-history` |
| `PUT /alert/{id}/status` | Reference semantics, including the raw timeout write (A1) |
| `PUT /alert/{id}/action` | open, assign, ack, unack, shelve, unshelve, close, expired, timeout (A2–A5) |
| `DELETE /alert/{id}` | |
| `PUT /alert/{id}/tag`, `/untag`, `/attributes` | Add or remove tags; merge attributes (a `null` value removes the key). No history entry, `updateTime` unchanged, as in the reference. Divergences (DECISIONS Y34): `tags` must be a non-empty list of strings and `attributes` a non-empty object, else 400; an update that leaves no attributes is 200 (the reference stores it and answers 500); untagging every tag stores `[]` (the reference stores NULL; both read back as `[]`) |
| `POST /heartbeat`, `GET /heartbeats`, `GET`/`DELETE /heartbeat/{id}` | Upsert by origin and customer; derived latency, since and status |
| `POST /key`, `GET /keys`, `DELETE /key/{id}` | API keys (`Authorization: Key …`, `X-API-Key`, or `?api-key=`). A new key's scopes must be within the creator's; the secret is shown only in the creation response; lists and `href`s use the public id. As in the reference, `POST /key` may supply its own `key` and `id`: a Python-falsy value (`null`, `false`, `0`, `""`, `[]`, `{}`) generates one, a duplicate is refused (500), and a truthy non-string is rejected with 400, an intentional divergence by the owner's ruling (DECISIONS Y33; the reference passes such values unchanged to the database adapter, and type-specific outcomes have not been qualified). `href`s percent-encode the id as one path segment |
| `GET /environments`, `/services`, `/alerts/groups`, `/alerts/tags` | The web UI's grouped counts, with the `GET /alerts` filters and the customer scope. As in the reference, `/environments` and `/services` list the names from **all** alerts, also other customers' (customer-mapping C20); only the counts are filtered. `/environments` pages with `page` and `page-size`; a page after the last is 416. Differences: a stored NULL or empty severity or status counts as `normal` or `open` (as in `/alerts/count`; the reference can misread such a row as a total or a status count); each grouped count keeps the reference's limit of 1000 grouped SQL rows (not 1000 alerts or names), in a fixed name order, so with many names a cut can leave one name with some counts and a zero total and the names after it with none; lists are sorted |
| `GET /alerts/top10/count`, `/flapping`, `/standing` (and the same under `/alerts/topn/`) | The `GET /alerts` filters and customer scope; `page-size` limits the list and a page after the first is 416 (`page` below 1 or not an integer is 400, as for `/alerts`; the reference gives 416). As in the reference, `count` counts one row per alert and service, `flapping` and `standing` one per severity history entry, `standing` orders by the summed lifetimes, and an alert without a service counts nothing. `group-by` (default `event`) must be exactly one `alerts` column, else 400 `Grouping by '<value>' field not supported.` (SF-QUERY, where the reference turns the value into SQL); `group-by=attributes` is refused by the restricted-attribute guard. Group values are written as the reference writes them (timestamps with milliseconds and `Z`, `history` as lists of its ten fields). Differences: equal counts come in a fixed order. SF-QUERY vector 31 (`q.df` checked before `group-by`) waits for the `q=` parser |
| `GET /management/gtg`, `/management/healthcheck` | No key needed. `gtg` answers `OK`, or 503 `FAILED` when the database does not answer |
| `GET`/`POST /management/housekeeping` | Deletion and expiry (K1, K2) |
| `POST /webhooks/prometheus` | Alertmanager notifications, mapped as the reference's `prometheus.py` (DECISIONS Y27) with the fixes of `spec/security-fixes.md` SF-WEBHOOKS: every item is mapped and processed on its own (Y10), and a batch with a failed item answers with the first failure's status plus `results` (one entry per item: `index`, `inputId`, `resourceId`, `outcome`, `code`, `message`); `moreInfo` is the plain URL (Y11); placeholders follow Y12 (no Python format language, 65,536-byte cap). Scope `write:webhooks`. The request log keeps only the route template, the webhook name and the body length. A null label takes the reference constructor's value (`customer` none, `environment` empty and then refused, `job` `Misc`, `severity` normal when a description or summary gives the text). Not reproduced, so the item is invalid: a label used as an alert field whose value is a boolean, a float, a list or an object (the reference's outcome for these has not been established). A leftover label is the tag `k=v` with Python's `str()` of the value, also for a list or an object. A `timeout` label is read as Python's `int()` reads it; a negative one is refused with the item, and one beyond 64 bits makes the item invalid (never the default timeout). Residuals: integers beyond 64 bits and `NaN`/`Infinity` elsewhere in the body. Other webhook names answer 404 |

## Configuration

These are variables read by the server process, including native startup.
The Compose disposable trial above uses API keys; its service environment
does not forward `SECRET_KEY`, `OAUTH2_CLIENT_ID`, `SAML2_ENTITY_ID` or
`AUTH_PROVIDER`, so exporting them on the host alone does not configure the
container verifier or set up operator login. Persistent key/provider wiring
for a container remains separate reviewed configuration and an owner step.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | (required) | PostgreSQL connection URL |
| `ALRUST_ADMIN_KEY` | (unset) | Ensures an admin API key with this value. A changed value replaces the previous bootstrap key, which then stops working. A bootstrap key created before 2026-11-08 under another value stays valid until you delete it once by hand. Unsetting or emptying the variable revokes nothing. |
| `SECRET_KEY` | Random 256-bit key per start | HS256 Bearer verification key. Unset, empty or exactly `changeme` generates a new key; any other value is used unchanged as UTF-8 bytes, with a warning below 32 characters. |
| `OAUTH2_CLIENT_ID` | (unset) | First non-empty expected Bearer audience; takes precedence over `SAML2_ENTITY_ID`. |
| `SAML2_ENTITY_ID` | (unset) | Expected Bearer audience when `OAUTH2_CLIENT_ID` is unset or empty. If both audience variables are unset or empty, the effective `ALRUST_BASE_URL` (with trailing `/` removed) is used, never the request host. |
| `AUTH_PROVIDER` | Empty | Exact provider-claim match for optional hook-only operator facts; an empty or mismatching provider leaves those facts absent without rejecting an otherwise valid token. |
| `ALRUST_AUTH_REQUIRED` | `true` | `false` (the reference default) skips route permission checks for requests without a key; such requests hold no scopes, so they cannot name a customer or mint keys. Any value other than true/false/1/0/yes/no/on/off stops start-up |
| `ALRUST_BIND` | `0.0.0.0:8080` | Listen address |
| `ALRUST_BASE_URL` | `http://localhost:8080` | Used in `href` fields |
| `ALRUST_ALLOWED_ENVIRONMENTS` | `Production,Development` | Exact environment names accepted on receive |
| `ALRUST_ALERT_TIMEOUT` | `86400` | Seconds, at most 2147483647 (the reference's `integer` columns) |
| `ALRUST_ACK_TIMEOUT` / `ALRUST_SHELVE_TIMEOUT` | `0` / `7200` | Seconds, at most 2147483647 |
| `ALRUST_HISTORY_LIMIT` | `100` | History entries kept per alert |
| `ALRUST_REQUEST_TIMEOUT` | `30` | Seconds a request may run before it is answered 503 `request timed out`; `0` turns the limit off. After this 503 the outcome of a write is **unknown**: a commit already sent may have succeeded, and parts committed earlier stay (a long `/management/housekeeping` run keeps the batches it finished and continues at the next call). Check the alert before retrying. The database query itself is not cancelled; only `ALRUST_DB_STATEMENT_TIMEOUT` bounds it |
| `ALRUST_DB_ACQUIRE_TIMEOUT` | `10` | Seconds a request waits for a free database connection (pool of 16) before it fails with 500 |
| `ALRUST_DB_LOCK_TIMEOUT` / `ALRUST_DB_STATEMENT_TIMEOUT` | `10` / `0` (off) | PostgreSQL `lock_timeout` and `statement_timeout`, in seconds, on serving connections; a statement that exceeds one fails with 500. Start-up (migration, adoption) runs without them |
| `ALRUST_HOUSEKEEPING_INTERVAL` | `0` (off) | Seconds between automatic housekeeping runs. The reference never schedules it; with it on, closed and expired alerts older than 2 h and informational alerts older than 12 h are **deleted**. |
| `ALRUST_KEY_USAGE_IMMEDIATE` | `false` | `true` writes an API key's use count and last-used time on every request, as the reference does. By default a request checks its key with a read only, and one writer writes the uses about once a second, and once more at a graceful shutdown: `GET /keys` shows them about a second late. If the database cannot take a write (slow, locked or down), alrust retries for up to 30 seconds, then drops those counts and logs how many; requests never wait for it, and a use is never counted twice. Counts for a key deleted or changed meanwhile, and counts whose write had an unknown outcome, are also dropped and logged, each separately. A crash loses the counts not yet written, never an alert (DECISIONS Y68, Y70) |

### Bearer verification only

The server process installs a settings-backed HS256 Bearer verifier at startup
(including native startup). The four Bearer variables use their exact reference
names, without an `ALRUST_` prefix. Audience and provider values are not trimmed or case-normalized.
Arbitrary UTF-8 audience/provider strings are accepted at startup; this does
not validate a provider or prove that tokens will match. Non-UTF-8 values in
any of these four variables stop startup, as does failure to generate a needed
random key. `AUTH_PROVIDER` does not start discovery or a provider exchange.

API-key carriers take precedence: `Authorization: Key …`, then `X-API-Key`,
then the first `api-key` query parameter, including a selected empty value;
a selected invalid key does not fall back to Bearer. Only without an API-key
carrier is a recognized Bearer header verified. Recognition requires exactly
`Bearer` and one space followed immediately by a non-whitespace token; the
first non-whitespace run is used and trailing content is ignored. A recognized
invalid token returns 401 even with `ALRUST_AUTH_REQUIRED=false`. An
unrecognized scheme or header form supplies no Bearer credential; without an
API-key carrier it follows the no-credential path, anonymous only when
authentication is disabled.

Verification does not issue operator login tokens. The login stage (including
the OIDC exchange) and downstream hook adapters remain unimplemented; enabling
these settings establishes neither login nor integration qualification. A
random default key is regenerated on each start, so tokens verified with the
previous key will no longer verify. Persistent and multiple-instance key
custody remains an owner step; these instructions do not supply a token issuer
or a key-management procedure.

## Alpha limitations

- **History scope (R7.1):** the current and previous status come from the
  matched alert's own history. The reference reads matching entries across
  every alert with the same environment, resource and customer.
- **Timeouts above 2147483647** in alerts, status changes, actions and
  heartbeats are refused with 400; the database column is a 32-bit
  `integer`, as in the reference (ADR-0005 decision 6).
- **Tags:** the de-duplicated union keeps first-seen order; the reference may
  reorder tags.
- **Dot-segment ids:** an `href` for an id of exactly `.` or `..` is a dot
  segment, which a URI-normalizing client may remove; ids are not validated.
- **Query parameters:** an unknown filter parameter is ignored; the reference answers 400
  "Invalid filter parameter". `~` (regex) values are not supported.
- **Id lookup:** a full id is looked up exactly first (an index lookup);
  otherwise a case-sensitive prefix match on `id` or `lastReceiveId`. The
  reference reads with a case-insensitive regex anchored at the start, with
  the id not escaped, and updates by `id` or `id` prefix; it returns any one
  matching row, and an exact match is always one of them.
- **Not yet implemented:** blackouts, notes,
  customers, users, groups, permissions, auth logins (basic, LDAP, OIDC,
  SAML), the webhooks other than Prometheus, bulk routes, auto-unshelve and auto-unack
  in housekeeping, `q=` queries, the web UI and the desktop app.
- **Scopes:** `admin` grants everything; `write` and `write:<resource>` grant
  the matching reads. Customer assignment follows `assign_customer`
  (customer-mapping C5–C11). Roles, users and the reference's full
  permission model are not implemented.
- **Qualification:** the ten selected A3B cases at historical candidate
  `ff7bc55a` passed 9 of 10, versus 10 of 10 for the pinned reference. The
  `event!=` failure was fixed in #147, but the corrected pairing has not been
  re-run. This selected local evidence is not full parity, Compose or hosted
  qualification. All 19 runtime profiles remain `NOT_RUN`; see
  [STATUS](STATUS.md) for the exact scope and retained deviations.
