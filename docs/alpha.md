# alrust alpha: quick start

The first runnable alrust: the Rust API server on PostgreSQL 18.6, speaking
the core of the Alerta v9.1.0 API. It is an **early alpha**: built to be
tried, not yet to replace the reference server. Behaviour follows
`spec/lifecycle.md`; the gaps are listed below.

## Run it

You need Docker with the compose plugin.

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

Stop with `docker compose down`; add `-v` to delete the database volume.

### Without Docker

Build with `cargo build --release --bin alrust`, point `DATABASE_URL` at a
PostgreSQL 18.6 database, and run `target/release/alrust`. Migrations run at
start-up.

## What works

| Route | Notes |
|---|---|
| `POST /alert` | Parsing and defaults (R0), the reject policy (R1.2), heartbeat diversion (HB3), duplicate / correlate / create with the reference state machine (R2–R6) |
| `GET /alert/{id}` | Full id, id prefix or `lastReceiveId` |
| `GET /alerts`, `GET /alerts/count` | Filters: `id`, `resource`, `event`, `environment`, `severity`, `status`, `service`, `group`, `origin`, `tag`; `page`, `page-size`, `show-history` |
| `PUT /alert/{id}/status` | Reference semantics, including the raw timeout write (A1) |
| `PUT /alert/{id}/action` | open, assign, ack, unack, shelve, unshelve, close, expired, timeout (A2–A5) |
| `DELETE /alert/{id}` | |
| `POST /heartbeat`, `GET /heartbeats`, `GET`/`DELETE /heartbeat/{id}` | Upsert by origin and customer; derived latency, since and status |
| `POST /key`, `GET /keys`, `DELETE /key/{id}` | API keys (`Authorization: Key …`, `X-API-Key`, or `?api-key=`). A new key's scopes must be within the creator's; the secret is shown only in the creation response; lists and `href`s use the public id. As in the reference, `POST /key` may supply its own `key` and `id`: a Python-falsy value (`null`, `false`, `0`, `""`, `[]`, `{}`) generates one, a duplicate is refused (500), and a truthy non-string is rejected with 400, an intentional divergence by the owner's ruling (DECISIONS Y33; the reference passes such values unchanged to the database adapter, and type-specific outcomes have not been qualified). `href`s percent-encode the id as one path segment |
| `GET /management/gtg`, `/management/healthcheck` | No key needed |
| `GET`/`POST /management/housekeeping` | Deletion and expiry (K1, K2) |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | (required) | PostgreSQL connection URL |
| `ALRUST_ADMIN_KEY` | (unset) | Ensures an admin API key with this value |
| `ALRUST_AUTH_REQUIRED` | `true` | `false` (the reference default) skips route permission checks for requests without a key; such requests hold no scopes, so they cannot name a customer or mint keys. Any value other than true/false/1/0/yes/no/on/off stops start-up |
| `ALRUST_BIND` | `0.0.0.0:8080` | Listen address |
| `ALRUST_BASE_URL` | `http://localhost:8080` | Used in `href` fields |
| `ALRUST_ALLOWED_ENVIRONMENTS` | `Production,Development` | Exact environment names accepted on receive |
| `ALRUST_ALERT_TIMEOUT` | `86400` | Seconds |
| `ALRUST_ACK_TIMEOUT` / `ALRUST_SHELVE_TIMEOUT` | `0` / `7200` | Seconds |
| `ALRUST_HISTORY_LIMIT` | `100` | History entries kept per alert |
| `ALRUST_HOUSEKEEPING_INTERVAL` | `0` (off) | Seconds between automatic housekeeping runs. The reference never schedules it; with it on, closed and expired alerts older than 2 h and informational alerts older than 12 h are **deleted**. |

## Alpha limitations

- **History scope (R7.1):** the current and previous status come from the
  matched alert's own history. The reference reads matching entries across
  every alert with the same environment, resource and customer.
- **Multiple matching rows (R2.3):** alrust locks and updates exactly one
  row; the reference's UPDATE can touch several.
- **Tags:** the de-duplicated union keeps first-seen order; the reference may
  reorder tags.
- **Dot-segment ids:** an `href` for an id of exactly `.` or `..` is a dot
  segment, which a URI-normalizing client may remove; ids are not validated.
- **Id lookup:** a prefix match on `id` or `lastReceiveId`; the reference
  uses a case-insensitive unanchored regex.
- **Not yet implemented:** blackouts, notes, tag/untag/attribute routes,
  customers, users, groups, permissions, auth logins (basic, LDAP, OIDC,
  SAML), webhooks, bulk routes, the top-N counts, auto-unshelve and auto-unack
  in housekeeping, `q=` queries, the web UI and the desktop app.
- **Scopes:** `admin` grants everything; `write` and `write:<resource>` grant
  the matching reads. Customer assignment follows `assign_customer`
  (customer-mapping C5–C11). Roles, users and the reference's full
  permission model are not implemented.
- The conformance suite has not yet been run against alrust; differences
  found there will be tracked as issues.
