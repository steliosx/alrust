# Security fixes: where alrust deliberately differs from the reference

Owner decision Y3 (2026-10-01, `docs/DECISIONS.md`): alrust **fixes** the
security and behavior defects found in the pinned reference
(`reference/alerta` v9.1.0, `f4312727`) instead of reproducing them. This
file specifies each fix. For every entry:

- **Reference** states what the pinned source does, with line citations
  (`reference/alerta/alerta/<path>:<line>`). These are source-derived; none of
  the defects was exploited or executed.
- **alrust** states the required behavior. It is normative for Phases 2 and 3.
- **Vectors** list the cases the conformance suite and the differential fuzzer
  use. A **parity** vector expects alrust to behave exactly as the reference.
  A **divergence** vector states the predicate under which the difference is
  intended and what each side must do; outside that predicate the reference
  behavior stands, so a difference there is a regression.

Entries keep their identifiers from `docs/STATUS.md` and `spec/openapi.yaml`.
Each entry is **Proposed** until Codex approves it at an exact commit. The
fixtures here are not executed by this spec; reference HTTP runs stay gated
by the heavy-window rules.

## Common definitions

- **Error envelope.** Every refusal below uses the reference's `ApiError`
  envelope (`exceptions.py:98-107`): HTTP status `code`, JSON
  `{"status": "error", "message": <text>, "code": <code>, "errors": <list or
  null>, "requestId": <id>}`. Where a message text is given, it is exact.
- **No side effect.** A refused request writes nothing: no row created,
  updated or deleted; no audit event (`admin_audit_trail`); no confirmation
  e-mail; and no API-key usage update beyond what the reference itself performs
  for the same credential before the refusal.
- **Scope check.** `in_scope(want, have)` is `Permission.is_in_scope`
  (`models/permission.py:90-110`): `want` empty or `None` is always
  satisfied; otherwise `want` or its prefix before `:` must be in `have`, with
  inheritance `read` → `write` → `admin`, and `delete` → `admin` when listed in
  `DELETE_SCOPES` (default `[]`, `settings.py:75`), otherwise `delete` →
  `write`.
- **Settings.** `AUTH_REQUIRED` (`settings.py:62`, default `False`),
  `ALLOW_READONLY` (`:78`, default `False`), `READONLY_SCOPES` (`:79`, default
  `['read']`), `EMAIL_VERIFICATION` (`:215`, default `False`).

## SF-AN1: read-only anonymous access must still enforce scopes

Status: Proposed.

**Reference.** The `permission(scope)` decorator (`auth/decorators.py:25`)
tries credentials in order. Four branches check the route's scope with
`in_scope` before dispatch: API key (`:30-54`), Bearer token (`:69-91`), auth
proxy (`:93-122`) and HTTP Basic (`:124-153`). The Hawk branch (`:55-67`)
sets `ADMIN_SCOPES` and dispatches without a scope check; it needs a valid
HMAC signature and is not changed here. With `AUTH_REQUIRED` and
`ALLOW_READONLY` both true, a request with no credential reaches the
read-only branch (`:163-169`), which sets `READONLY_SCOPES` and dispatches
**without** the scope check, so an anonymous caller reaches write and delete
routes.

**alrust.** In the read-only branch, alrust dispatches only if
`in_scope(route_scope, READONLY_SCOPES)`; otherwise it raises the
reference's fall-through refusal: 401, message `Missing authorization API Key
or Bearer Token` (`:175`), with no side effect. An admitted request keeps the
reference's context: no user id, no login, no customers, and
`READONLY_SCOPES` as scopes. Every other branch, and `AUTH_REQUIRED` false,
are unchanged.

**Vectors.** The divergence predicate is exactly: `AUTH_REQUIRED=True`,
`ALLOW_READONLY=True`, no credential, and `in_scope(route_scope,
READONLY_SCOPES)` false. The reference dispatches; alrust returns the 401
above with no side effect. Parity otherwise:

| Configuration | Request | Expected |
|---|---|---|
| default `READONLY_SCOPES=['read']` | anonymous `GET /alerts` (`read:alerts`) | parity: dispatched |
| default | anonymous `POST /alert` (`write:alerts`) | divergence: alrust 401, nothing stored |
| default | anonymous `DELETE /alert/{id}` (`delete:alerts`, `views/alerts.py:278`; with the default empty `DELETE_SCOPES` it needs `write`) | divergence: alrust 401, nothing deleted |
| `READONLY_SCOPES=[]` | anonymous `GET /alerts` | divergence: alrust 401 |
| `READONLY_SCOPES=['read:alerts']` | anonymous `GET /heartbeats` (`read:heartbeats`) | divergence: alrust 401 |
| `READONLY_SCOPES=['read:alerts']` | anonymous `GET /alerts` | parity: dispatched |
| `READONLY_SCOPES=['write']` | anonymous `GET /alerts` (`read` inherits from `write`) | parity: dispatched |
| default | anonymous `GET /user/me` (no required scope, `views/users.py:104`) | parity: dispatched |
| default | valid API key with `write:alerts`, `POST /alert` | parity: success |
| default | valid API key with `read`, `POST /alert` | parity: 403 `Missing required scope: write:alerts` |
| `AUTH_REQUIRED=False` | anonymous `POST /alert` | parity: dispatched |

## SF-CRON: no unauthenticated App Engine cron branch

Status: Proposed.

**Reference.** With `AUTH_REQUIRED` true, `ALLOW_READONLY` false and no
credential, a request whose `X-Appengine-Cron` header is non-empty and whose
`X-Forwarded-For` is exactly `0.1.0.1` is dispatched **with no scope check
and no auth context set** (`auth/decorators.py:171-173`). Both headers are
ordinary client-controlled headers unless a proxy strips them.

**alrust.** No such branch exists; the request falls through to the 401 of
SF-AN1, with no side effect. Scheduled housekeeping uses an ordinary
credential that carries the required scope.

**Vectors.** The divergence predicate is exactly: `AUTH_REQUIRED=True`,
`ALLOW_READONLY=False`, no credential, `X-Appengine-Cron` non-empty and
`X-Forwarded-For: 0.1.0.1`. The reference dispatches; alrust returns 401.
Parity otherwise, including:
- the same cron headers together with a valid credential: the credential's
  branch decides, as in the reference (credentials are tried first,
  `:30-152`);
- the same cron headers with `ALLOW_READONLY=True`: SF-AN1 applies, since that
  branch comes first (`:163-169`);
- the same cron headers with `AUTH_REQUIRED=False`: dispatched in both.

## SF-USERS: one role ceiling for every way of setting roles

Status: Proposed. The group, permission and customer-mapping paths to the
same privileges (G-SEC-1, P-SEC-1, C-SEC-1) get their own entries under the
same principle: every edit that can widen a caller's or another user's scopes
is held to the caller's own scopes. Until then they remain unresolved.

Create and update are specified separately: the reference treats them
differently, and this entry changes only what it names.

### Update: `PUT /user/{user_id}`

**Reference.** If the body's `roles` is truthy, every scope those roles grant
(`Permission.lookup(login='', roles=<roles>)`) must pass `in_scope` against
the caller's scopes, else 403 `Requested scope '<scope>' not in existing
scopes: <scopes>` (`views/users.py:181-186`). The update then runs, and the
model turns a legacy `role` member into `roles: [role]`
(`models/user.py:180-181`) **after** that check. So `{"role": "admin"}`
grants any role without the ceiling. With both members, the plural check runs
first on `roles`, then `role` replaces it.

**alrust.** Checks run in this order, the first failure deciding the
response, with no side effect:
1. `roles` must be absent, `null` (treated as absent) or a JSON array of
   strings, else 400 `roles must be a list of strings`.
2. `role` must be absent or a non-empty string, else 400 `role must be a
   non-empty string`.
3. `role` together with a non-null `roles`: 400 `role and roles are mutually
   exclusive`.
4. The effective new roles are `[role]` when `role` is present, else `roles`.
   When they are non-empty, the reference's ceiling applies to them with
   `login=''` and its 403 message. An empty array removes all roles and needs
   no ceiling, as in the reference.
Then the update runs as in the reference.

### Create: `POST /user`

**Reference.** A non-basic provider is refused with 400 before parsing
(`views/users.py:23-25`). `User.parse` (`models/user.py:62-74`) reads
`roles` only (a `null` value is kept as given) and ignores `role`. The
constructor replaces the roles with `ADMIN_ROLES` when the e-mail is in
`ADMIN_USERS` (`:39`). The ceiling runs after construction on the effective
privileges: `Permission.lookup(login=user.email, roles=user.roles)`
(`views/users.py:39-43`), which grants `ADMIN_SCOPES` for an `ADMIN_USERS`
login or an `ADMIN_ROLES` role (`database/backends/postgres/base.py:1277-1284`).
The password is hashed with bcrypt (`auth/utils.py:21-25`):
- absent: hashed as the empty string (`models/user.py:67`), then stored;
- `""`: hashed and stored;
- `null` or a non-string (for example `5`): the hash raises inside `parse`,
  which the handler turns into 400 with the exception text
  (`views/users.py:27-30`).
When the insert raises, the handler builds `ApiError(str(e), 500)` but never
raises it (`:45-48`). The confirmation e-mail (when `EMAIL_VERIFICATION` is on)
and the audit event then run (`:50-55`), and the response is 201 with the
unsaved user (`:57-58`).

**alrust.** Unchanged from the reference except:
1. With the basic provider, `password` must be a non-empty string. Absent,
   `null`, `""` or any other type gives 400 `Missing mandatory value for
   "password"`, with no side effect. This check belongs to parsing, so it
   follows the provider check and precedes the domain, uniqueness and
   ceiling checks.
2. A failed user insert returns 500 with the database error text, as an
   `ApiError(str(e), 500)` would, and nothing after the insert runs: no
   confirmation e-mail, no audit event, no 201. alrust claims only that no
   success response or post-insert side effect follows a refused insert; it
   makes no claim about a write whose commit outcome is unknown.
`role` stays ignored on create, `roles` is handled as given (including
`null`), and the create ceiling stays the reference's post-construction check
of effective privileges.

### Vectors

Caller scopes, roles and settings are fixtures. "Above the ceiling" means some
scope the roles grant fails `in_scope` against the caller's scopes.
"Reference pending" marks a reference outcome that is source-derived but not
yet qualified. Such a vector asserts the alrust expectation only, and the
differential comparison skips that exact vector. Nothing broader is
suppressed until it is qualified.

| Case | Reference | alrust | Kind |
|---|---|---|---|
| `PUT` with `role` (alone) above the ceiling | 200, role stored | 403, no change | divergence |
| `PUT` with `role` (alone) within the ceiling | 200 | 200 | parity |
| `PUT` with `roles` above the ceiling | 403 | 403 | parity |
| `PUT` with `roles` within the ceiling | 200 | 200 | parity |
| `PUT` with `roles: []` | 200, roles removed | 200, roles removed | parity |
| `PUT` without `role` or `roles` (for example `text` only) | 200 | 200 | parity |
| `PUT` with valid `roles` above the ceiling and a valid `role` | 403 (plural check first) | 400 mutually exclusive | divergence (both refuse) |
| `PUT` with valid `roles` within the ceiling or `[]`, and a valid `role` above it | 200, `role` stored | 400 mutually exclusive | divergence |
| `PUT` with `roles: "admin"` (with or without `role`) | reference pending | 400 `roles must be a list of strings` | divergence |
| `PUT` with `role: null` or `role: 5` (no `roles`) | reference pending (the rewrite stores `[null]` or `[5]`) | 400 `role must be a non-empty string` | divergence |
| `PUT` with `role: 5` and valid `roles: ["admin"]` | reference pending | 400 `role must be a non-empty string` (step 2 precedes step 3) | divergence |
| `POST`, basic provider, caller with `admin:users`, otherwise valid input that passes the domain, uniqueness and ceiling checks, `password` absent or `""` | 201 with the hash of the empty string | 400 `Missing mandatory value for "password"`, no user | divergence |
| same, `password: null` or `password: 5` | 400 with the exception text (reference pending) | 400 `Missing mandatory value for "password"` | divergence in the message only; status and envelope must match |
| same, non-empty string `password` | 201 | 201 | parity |
| `POST` with `role: "admin"` and no `roles`, otherwise valid | 201, `role` ignored | 201, `role` ignored | parity |
| `POST` with an e-mail in `ADMIN_USERS`, caller with only `admin:users` | 403 (effective `ADMIN_SCOPES`) | 403 | parity |
| `POST` with a non-basic provider | 400 before parsing | 400, same message | parity |
| `POST` whose insert fails before writing (fixture: an `id` equal to an existing user's id), `EMAIL_VERIFICATION=False`, default audit handlers | 201 with the unsaved user | 500, no new row, no audit event | divergence |

## Not yet specified

The other Y3 items get entries in this file before their phases need them:
the string-formatted SQL constructions; G-SEC-1, P-SEC-1, C-SEC-1, GR-R1 and
G-DEF-1; M-SEC-1; WUI-SEC-1 and WP-SEC-1; W-SEC-1 and W-DL-1; the API-key
items; C21–C23; and the `q=` search. Until an item has an entry here, its
reference behavior in `spec/openapi.yaml` stands as the description of the
defect, not as a requirement for alrust.
