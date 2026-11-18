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
same privileges are specified in SF-GROUPS, SF-PERMS and SF-CUSTOMERS under
the same principle: every edit that can widen a caller's or another user's
scopes or customers is held to the caller's own.

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

## SF-GROUPS: group names that grant privileges are held to the caller's scopes

Status: Proposed. Covers G-SEC-1, GR-R1 and G-DEF-1.

Define `group_grants(name)` as `Permission.lookup(login='', roles=[name])`:
the scopes a group of that name gives its members at login. Like any role,
that is `ADMIN_SCOPES` for a name in `ADMIN_ROLES`, the default scopes for
the built-in user and guest roles, or the stored permission rows matching the
name (`database/backends/postgres/base.py:1277-1293`).

**Reference.**
1. G-SEC-1: at password login and sign-up, a user's group names are passed to
   the role lookup together with the user's roles (`auth/basic.py:52-53`,
   `:93-94`). No group endpoint applies a role ceiling: `POST /group`
   (`views/groups.py:17-38`), `PUT /group/{group_id}` (`:111-131`) and
   `PUT /group/{group_id}/user/{user_id}` (`:134-153`) need only
   `admin:groups`. So a holder of `admin:groups` can create a group named
   `admin` (in the default `ADMIN_ROLES`), add any user, including
   themselves, and that user gets `ADMIN_SCOPES` at the next password login.
2. GR-R1: `PUT /group/{group_id}` with `{"name": ""}` writes the empty name,
   because the store writes any non-null `name` (`postgres/base.py:1170-1185`),
   and then fails while rebuilding the model, whose constructor rejects an
   empty name (`models/group.py:72-73`). The response is a 500 after the
   write (source-derived; NOT_RUN).
3. G-DEF-1: in the auth-proxy branch, the customer lookup receives `Group`
   objects instead of group names (`auth/decorators.py:116`, against the
   names used at password login, `auth/basic.py:52-54`). With
   `CUSTOMER_VIEWS` on, for a login not in `ADMIN_USERS` and a user in at
   least one group, this is expected to fail when the objects are bound as SQL
   parameters (inferred; NOT_RUN).

**alrust.**
1. `POST /group`: every scope in `group_grants(name)` must pass `in_scope`
   against the caller's scopes, else 403 `Requested scope '<scope>' not in
   existing scopes: <scopes>` (the reference's ceiling message), with no side
   effect.
2. `PUT /group/{group_id}` with `name`:
   - `name` must be a non-empty string, else 400 `Missing mandatory value for
     name` (the model's own message), with no change (GR-R1);
   - every scope in `group_grants(<new name>)` must pass the ceiling, else
     403 as above, with no change. A rename re-labels every member's role,
     so it is held to the same ceiling as creating a group.
3. `PUT /group/{group_id}/user/{user_id}`: every scope in
   `group_grants(<the group's current name>)` must pass the ceiling, else 403
   as above, with no change.
4. G-DEF-1: the auth-proxy branch passes group names to the customer lookup,
   as password login does.
Group names still act as roles at login, as in the reference. This entry
only stops an `admin:groups` holder from granting through them more than
they hold. A token already issued keeps its scopes until it expires, as in
the reference; that is not changed here.

**Vectors.** Caller scopes and permission rows are fixtures.

| Case | Reference | alrust | Kind |
|---|---|---|---|
| caller with only `admin:groups`, `POST /group` named `admin` | 201 | 403, no group | divergence |
| same caller, `POST /group` named `ops` (no permission row, not a built-in role) | 201 | 201 | parity |
| caller with `admin`, `POST /group` named `admin` | 201 | 201 | parity |
| caller with only `admin:groups`, rename `ops` to `admin` | 200 | 403, no change | divergence |
| caller with only `admin:groups`, add a user to a group named `admin` | 200 | 403, no change | divergence |
| caller with only `admin:groups`, add a user to `ops` | 200 | 200 | parity |
| `PUT /group/{id}` with `{"name": ""}` | 500 after writing the empty name (reference pending) | 400 `Missing mandatory value for name`, no change | divergence |
| `PUT /group/{id}` with `text` only | 200 | 200 | parity |
| auth-proxy login, `CUSTOMER_VIEWS` on, user in a group mapped to a customer | 500 (reference pending) | customers resolved from group names | divergence |

## SF-PERMS: permission edits hold the same checks as permission creation

Status: Proposed. Covers P-SEC-1.

"Known scopes" means `Scope.find_all()` (`models/enums.py:104-105`), which
includes the built-in scopes and every `CUSTOM_SCOPES` entry registered at
start-up (`:90-91`; `settings.py:73`).

**Reference.**
- `POST /perm` (`views/permissions.py:16-49`): `Permission.parse` requires
  `scopes`, when present, to be a list (else 400 `scopes must be a list`) and
  wraps each element as a `Scope` string without checking it is known
  (`models/permission.py:22-31`). The handler then refuses the three reserved
  role names with 409 `<match> role already exists` (`views/permissions.py:26-31`),
  and holds each submitted scope to the caller's scopes with the ceiling
  message (`:33-36`). `in_scope` accepts `""` (`models/permission.py:96-97`)
  and any scope whose exact text or prefix before `:` the caller holds
  (`:98-110`), known or not. A missing `match` fails at insert as a caught
  500 (`spec/openapi.yaml`, PermInput).
- `PUT /perm/{perm_id}` (`:132-156`): an empty body is 400. Each element of
  `scopes` (default `[]`) must be a known scope, else 400 `'<s>' is not a
  valid Scope` (`:140-142`). There is no reserved-name check and no ceiling.
  The store writes `match` and `scopes` whenever the members are present, even
  when null (`postgres/base.py:1252-1267`).
So a holder of `admin:perms` can give any role any scope, including `admin`,
or rename a row onto another role name.

**alrust input contract** (both endpoints):
- `scopes`: on `POST`, absent means `[]`. On `PUT`, absent means
  "unchanged". When present, it must be a JSON array of strings: `null` or
  any other type is 400 `scopes must be a list` (`POST`'s existing message),
  and a non-string element is 400 `'<value>' is not a valid Scope`. An
  explicit `[]` is a valid value: no scopes.
- `match`: `POST` is unchanged from the reference. On `PUT`, absent means
  "unchanged"; when present it must be a non-empty string, else 400 `match
  must be a non-empty string`.

**alrust order of checks** (both endpoints), the first failure deciding the
response, with no side effect:
1. the input contract above;
2. every submitted scope must be known, else 400 `'<s>' is not a valid
   Scope`;
3. reserved names: on `POST`, the row's `match`; on `PUT`, the new `match`
   when present. 409 `<match> role already exists`;
4. the ceiling, applied to the scopes the row will grant after the request:
   on `POST`, the submitted scopes; on `PUT`, the new `scopes` when present,
   otherwise the row's existing scopes when `match` changes. Every one must
   pass `in_scope` against the caller's scopes, else 403 with the ceiling
   message. A `PUT` that changes neither `scopes` nor `match` needs no
   ceiling, and an explicit `[]` has nothing to check.
`POST` keeps its reserved-name and ceiling checks, now after validation.
`PUT` gains them.

**Vectors.** "Caller" is the authenticated key's scopes; all `POST` cases
use an otherwise valid, unused, non-reserved `match` unless stated.

| Case | Reference | alrust | Kind |
|---|---|---|---|
| `PUT` by a caller with only `admin:perms`, adding `admin` to a row's scopes | 200 | 403, no change | divergence |
| same caller, `PUT` renaming a row to `user` (valid scopes) | 200 | 409, no change | divergence |
| same caller, `PUT` renaming a row whose existing scopes include `admin`, `scopes` omitted | 200 | 403, no change | divergence |
| same caller, `PUT` renaming that row with `scopes: []` | 200 | 200, row grants nothing | parity |
| same caller, `PUT` with known scopes within its own | 200 | 200 | parity |
| `POST` with `scopes: [""]` | 201 | 400 `'' is not a valid Scope` | divergence |
| caller `['admin:perms', 'read']`, `POST` with `scopes: ['read:synthetic_unknown']` | 201 (the `read` prefix passes the ceiling) | 400 `'read:synthetic_unknown' is not a valid Scope` | divergence |
| caller `['admin:perms']`, `POST` with `scopes: ['read:synthetic_unknown']` | 403 (ceiling) | 400 (validation runs first) | divergence (both refuse) |
| caller with the scope, `POST` with a scope registered through `CUSTOM_SCOPES` | 201 | 201 | parity |
| `POST` above the ceiling with known scopes | 403 | 403 | parity |
| `POST` with `match: 'user'` and known scopes within the ceiling | 409 | 409 | parity |
| `POST` with `match: 'user'` and `scopes: ['']` | 409 (reserved name first) | 400 (validation first) | divergence (both refuse) |
| `POST` with `scopes: null` or `scopes: 'admin'` | 400 `scopes must be a list` | 400, same message | parity |
| `PUT` with `scopes: 'admin'` | 400 `'a' is not a valid Scope` (the string is iterated by character, `views/permissions.py:140-142`) | 400 `scopes must be a list` | divergence in the message only; status and envelope must match |
| `PUT` with `scopes: null` | 500 (iterating `null` raises outside any handler; reference pending) | 400 `scopes must be a list` | divergence |
| `PUT` with `match: null` or `match: ""` | reference pending (the store writes the member as given) | 400 `match must be a non-empty string` | divergence |
| `PUT` with an unknown scope name | 400 | 400 | parity |

## SF-CUSTOMERS: a customer-scoped caller can only map to its own customers

Status: Proposed. Covers C-SEC-1.

**Reference.** `POST /customer` (`views/customers.py:16-37`) and
`PUT /customer/{customer_id}` (`:89-109`) need only `admin:customers`, and
nothing compares the mapped customer with the caller's own customers. With
`CUSTOMER_VIEWS` on, a customer-scoped holder of `admin:customers` can map
a name that a later lookup will match for them (their login, a group name or
their e-mail domain) to another customer, or to `*`, which removes customer
filtering (`postgres/base.py:1351-1367`; `models/customer.py:85-87`). It
takes effect at the next lookup. The caller's customers are `g.customers`;
note that API-key authentication sets it from the key's customer even when
`CUSTOMER_VIEWS` is off (`auth/decorators.py:48`).

**alrust.** The rule applies only when `CUSTOMER_VIEWS` is on **and** the
caller's `g.customers` is a non-empty list (a customer-scoped caller).
`CUSTOMER_VIEWS` off takes precedence: then nothing changes, whatever
`g.customers` holds. A caller with an empty `g.customers` is unchanged,
whatever its scopes. For a customer-scoped caller, in this order, the first
failure deciding the response, with no side effect:
1. `PUT /customer/{customer_id}` (any body, including a `match`-only change):
   the existing mapping's customer must be one of the caller's customers,
   else 403 `not allowed to change a mapping for customer '<existing>'`.
2. `POST /customer`, and `PUT` when `customer` is present: the new
   `customer` must be one of the caller's customers and not `*`, else 400
   `not allowed to set customer to '<customer>'` (the reference's own message
   in `utils/api.py:20`).

**Vectors.** All scoped cases have `CUSTOMER_VIEWS=True` and a caller whose
key carries customer `A`, so `g.customers` is `[A]`.

| Case | Reference | alrust | Kind |
|---|---|---|---|
| scoped caller, `POST` mapping to `B` | 201 | 400, no mapping | divergence |
| scoped caller, `POST` mapping to `*` | 201 | 400, no mapping | divergence |
| scoped caller, `POST` mapping to `A` | 201 | 201 | parity |
| scoped caller, `PUT` changing its own mapping for `A` to `B` | 200 | 400, no change | divergence |
| scoped caller, `PUT` on a mapping for `B` | 200 | 403, no change | divergence |
| scoped caller, `PUT` on a mapping for `B` with `customer: C` | 200 | 403 (ownership is checked first), no change | divergence |
| scoped caller, `PUT` on its own mapping for `A`, `match` only | 200 | 200 | parity |
| scoped caller, `PUT` on a mapping for `B`, `match` only | 200 | 403, no change | divergence |
| caller with empty `g.customers` (for example an `admin` key with no customer), `POST` mapping to `B` | 201 | 201 | parity |
| `CUSTOMER_VIEWS=False`, key with customer `A`, `POST` mapping to `B` | 201 | 201 | parity |

## SF-KEYS: API-key expiry, scopes, secret exposure and customer

Status: Proposed. Covers the API-key items, C23, the use of a key's
public id as a credential (KEYS-R4) and the edit ceiling on a key's
existing scopes (owner decision Y7, 2026-10-01). The secret-exposure fix
follows owner decision Y6 (2026-10-01): the secret is shown only once, at
creation, and alrust's vendored web UI shows it once after creation.

**Fixtures.** Settings `API_KEY_EXPIRE_DAYS = 365` (`settings.py:175`, the
default, set explicitly), `USER_DEFAULT_SCOPES = ['read', 'write']` (`:69`),
`ADMIN_USERS = ['root@example.test']` (default `[]`, `:64`) and
`AUTH_REQUIRED = True`. Callers:
- `C1`: login `alice@example.test`, scopes `[read, write:keys]`, no
  customers.
- `C2`: login `bob@example.test`, scopes `[admin]`.
- `C3`: login `carol@example.test`, scopes `[read, write:keys]`, customers
  `[A]`.
- `C4`: login `dave@example.test`, scopes `[read, write:keys]`, customers
  `[A, B]`.

Key `K0` has scopes `[read]`, text `t0`, customer `NULL` and a stored expiry
`E` one day ahead. Key `KA` is the same but with customer `A`. Each key is
owned by the `C1`, `C3` or `C4` login that edits it in the vector; in rows
where `C2` acts, it is owned by `alice@example.test`. Unless a vector varies them, a `PUT` body carries
`text: 't1'` and `expireTime` equal to `E`, so each vector isolates one
change. State is read from the store by a privileged observer, not through
the API.

**Reference.**
1. Responses (`views/keys.py`):
   - `POST /key` returns 201 `{status, key: <secret>, data: <key object>}`
     (`:52`);
   - `GET /key/{key}` returns `{status, total: 1, key: <key object>}` (`:71`);
   - `GET /keys` returns `{status, page, pageSize, pages, more, keys: [<key
     object>…], total}`, plus `message` when empty (`:94-113`);
   - `PUT /key/{key}` returns `{status, key: <key object>}` (`:147`);
   - `DELETE /key/{key}` returns `{status}` only (`:166`).

   Every key object holds the secret as `key` and `href` =
   `absolute_url('/key/' + <secret>)` (`models/key.py:64-78`). Paths accept
   the id or the secret (`postgres/base.py:947-953`, `:996`, `:1013`).
   Authentication uses the same lookup. The `permission` decorator takes a
   token from `Authorization: Key <token>`, else `X-API-Key`, else the
   `api-key` query parameter (`auth/decorators.py:30-40`), and passes it to
   `ApiKey.verify_key` (`:42-43`). That calls `db.get_key(token)`, which
   matches `id` or `key` (`models/key.py:162-166`; `postgres/base.py:947-953`),
   and then counts the use with the same raw token (`update_key_last_used`,
   `:1002-1008`). A key's id is therefore a working credential with that
   key's scopes. A token that matches no key, or an expired key, gives 401
   `API key parameter '<token>' is invalid` (`auth/decorators.py:44-45`).
   In the store, `id` is the primary key and `key` is a separate unique
   column (`sql/schema.sql:123-133`).
2. Expiry on `PUT`: `ApiKey.update` replaces `expireTime` with
   `DateTime.parse(expireTime)` when present, else with `None`
   (`models/key.py:152`), and the store then writes it
   (`postgres/base.py:990-991`). `DateTime.parse` returns `None` for every
   non-string (`utils/format.py:55-57`), so an absent `expireTime`, `null`, a
   number, a boolean, an array and an object all store `NULL`. Every read then
   rebuilds a `NULL` expiry as the read time plus `API_KEY_EXPIRE_DAYS`
   (`models/key.py:31`), so `verify_key` (`:162-167`) never finds the key
   expired. A string that does not parse raises `ValueError` (`utils/format.py:58-61`)
   outside any handler, after the audit signal (`views/keys.py:142-145`): the
   generic 500. On `POST`, `ApiKey.parse` turns the same `ValueError` into
   400 with its message (`views/keys.py:23-26`), and a non-string gives the
   default expiry, which is stored.
3. Scopes: a key with an empty scope list reads back as `USER_DEFAULT_SCOPES`
   (`models/key.py:29`). `PUT` checks the ceiling only on the scopes in the
   body (`views/keys.py:137-140`): `scopes: []` checks nothing and stores
   `[]`, and a body without `scopes` does not recheck the stored scopes. On
   `POST`, a `type` member replaces the scopes after construction, mapped
   with the body's `user` member (`models/key.py:58-59`), before the handler
   sets the final user (`views/keys.py:28-31`): a body `user` in
   `ADMIN_USERS` gives `ADMIN_SCOPES`; `read-write` and `read-only` give their
   scopes; any other type gives `[]` (`utils/key.py:36-43`). The ceiling then
   checks the resulting scopes (`views/keys.py:38-41`), so `[]` passes and the
   key acts with `USER_DEFAULT_SCOPES`. `scopes: []` on `POST` without `type`
   becomes `USER_DEFAULT_SCOPES` at construction and is checked: no defect.
4. C23: `PUT` always sets `customer` from `assign_customer`
   (`views/keys.py:135`; `utils/api.py:14-28`). Without `customer` in the
   body, an unscoped caller gets `None` (the customer is cleared), a caller
   with one customer gets that customer, and a caller with several customers
   gets 400 `must define customer as more than one possibility`.

**alrust.**
1. Responses. Every envelope keeps the reference's members, with
   exactly these serialization changes:
   - **S1:** the `key` member inside a key object appears only in the
     `POST /key` 201 response, which keeps both the top-level `key` and
     `data.key`. It is absent from `GET /key/{id}` (`key.key`), from every
     `GET /keys` element (`keys[*].key`) and from `PUT` (`key.key`). The
     outer `key` member of the `GET` and `PUT` envelopes is still the key
     object.
   - **S2:** every key object's `href` is `absolute_url('/key/' + <id>)`, in
     `POST` (`data.href`), `GET`, `GET /keys` and `PUT`.
   - **S3:** `GET`, `PUT` and `DELETE /key/{key}` match on `id` only. A
     secret in the path is an unknown id: 404 `not found`, no side effect.
   - **S4:** authentication matches the token against the `key` (secret)
     column only, never against `id`. Carriers and their precedence are the
     reference's (`Authorization: Key`, then `X-API-Key`, then `api-key`).
     A token that equals a key's id but no key's secret is an unknown key:
     401 `API key parameter '<token>' is invalid`, the reference's message
     and envelope. That 401 has no side effect: no use is counted and the
     handler does not run. Credential resolution comes before the scope
     check and the handler, as in the reference. On success, the use is
     counted on the one resolved key's row, by its id. Expired keys are
     refused as in the reference.

   Management and authentication are therefore separate: paths take the id
   only (S3), and credentials take the secret only (S4). A response is in **operation parity** when it has the
   reference's status, envelope members and stored state, and its key
   objects equal the reference's after S1 and S2 are applied to them.
2. `PUT /key/{id}` checks in this order, each refusal without side effect:
   1. empty body: 400 `nothing to change` (reference);
   2. lookup by id: 404 `not found` (reference, with S3);
   3. `customer` present: the reference's `assign_customer` rules and
      refusals. `customer` absent: the stored customer is unchanged and
      `assign_customer` is not called (C23);
   4. `expireTime` present and not a string that parses with the reference
      format: 400 `dates must be ISO 8601 date format
      YYYY-MM-DDThh:mm:ss.sssZ`. This covers `null`, numbers, booleans,
      arrays, objects and malformed strings. `expireTime` absent: the stored
      expiry is unchanged;
   5. `scopes` present and an empty list: 400 `scopes must not be empty`;
   6. the ceiling on the key's resulting scopes: the scopes in the body when
      present, otherwise the stored scopes (owner decision Y7). Each scope is
      checked in order with `in_scope` against the caller's scopes; the first
      that fails gives 403 `Requested scope '<scope>' not in existing scopes:
      <caller scopes joined by ','>`, the reference message
      (`views/keys.py:139-140`). So a caller cannot change any field of a key
      whose permissions exceed the caller's own, including moving its expiry.
      The pinned web UI always sends `scopes` when it edits a key
      (`ApiKeyList.vue:503-511`), so for UI edits the reference already
      applies this check and the behavior is unchanged;
   7. the audit signal, then the update.
3. `POST /key` keeps the reference's order (parse, final user, customer,
   missing user, ceiling). Within parse, after the reference's own checks, a
   `type` member other than the strings `read-only` and `read-write` is
   400 `unknown key type`, whatever the body `user` is. A valid `type` is
   mapped exactly as in the reference, with the body's `user` member,
   including the `ADMIN_USERS` case. Expiry handling on `POST` is unchanged.
4. Stored expiry and scopes are read literally; nothing is rebuilt or
   substituted on read.
5. Web UI (Y6, amended 2026-10-01). Against the reference, the pinned UI
   shows a new key's secret only through the reloaded list: `createKey`
   discards the creation response and reloads the list
   (`store/modules/keys.store.ts:35-38`), which shows `key` from `GET /keys`
   (`ApiKeyList.vue:215-224`). Against the S1 responses it would never show
   the secret. alrust's vendored web UI
   therefore carries one change: after a successful create, it shows the
   secret from the 201 response once, with a copy control, and the list
   no longer has a key column. Update and delete already use `id`
   (`ApiKeyList.vue:491`, `:503-504`). This is a vendored-UI change under
   ADR-0004 and has its own review.

**Vectors.** Reference columns are source-derived expectations. Before
execution each is NOT_RUN; on the first authorized reference run, every
status, envelope and state predicate in them is asserted, and none is
skipped for lack of an earlier run. Only a cell marked **reference pending**
has an unresolved expectation: that predicate alone is an explicit skip
(`SKIPPED_REFERENCE_UNQUALIFIED`), while the known predicates of the same
row, including the alrust side and its no-side-effect checks, are still
asserted.

| # | Case | Reference | alrust | Kind |
|---|---|---|---|---|
| 1 | `C1` `PUT` `K0` with body `{text: 't1'}` (no `expireTime`) | 200; stored expiry `NULL`; each read shows read time + 365 days; `verify_key` accepts after `E` | 200; stored expiry `E` | divergence (expiry) + S1/S2 |
| 2-6 | `C1` `PUT` `K0` with `expireTime` = `null`, `5`, `false`, `[]`, `{}` | 200, as row 1 | 400 parse message, no change | divergence |
| 7 | `C1` `PUT` `K0` with `expireTime: 'tomorrow'` | 500 (body reference pending) | 400 parse message, no change | divergence |
| 8 | `C1` `PUT` `K0` with a valid `expireTime` `E2` | 200, expiry `E2` | same | operation parity |
| 9 | `C1` `PUT` `K0` with `scopes: []` | 200; stored `[]`; the response and later reads show `[read, write]` | 400 `scopes must not be empty`, no change | divergence |
| 10 | `C1` `PUT` `K0` with `scopes: [read]` | 200 | 200 | operation parity |
| 11 | `C1` `PUT` `K0` with `scopes: [write]` | 403 ceiling message | same | parity |
| 12 | `C2` creates `K0` for `alice@example.test` with scopes `[admin]`; then `C1` `PUT` it with `{text: 't1', expireTime: E2}` (`E2` later than `E`) | 200; stored scopes not rechecked; text and expiry changed | 403 `Requested scope 'admin' not in existing scopes: read,write:keys`, no change | divergence (Y7) |
| 12b | same key; `C2` `PUT` it with `{text: 't1', expireTime: E2}` | 200 | same | operation parity (Y7 positive control) |
| 12c | same key; `C1` `PUT` it with `{scopes: [admin], text: 't1', expireTime: E}` (as the web UI sends) | 403, same message | same | parity |
| 13 | `C1` `POST` `{type: 'bogus'}` | 201; stored scopes `[]`; the 201 object (built by `ApiKey.from_db`) and later reads show `[read, write]`; user `alice@example.test` | 400 `unknown key type`, no key | divergence |
| 14 | `C2` `POST` `{type: 'bogus'}` | 201; stored scopes `[]`; the 201 object and later reads show `[read, write]` | 400 `unknown key type`, no key | divergence |
| 15 | `C2` `POST` `{type: 'bogus', user: 'root@example.test'}` | 201; scopes `ADMIN_SCOPES` | 400 `unknown key type`, no key | divergence |
| 16 | `C2` `POST` `{type: 'read-only', user: 'root@example.test'}` | 201; scopes `ADMIN_SCOPES`; user `root@example.test` | same | operation parity (`ADMIN_USERS` positive control) |
| 17 | `C1` `POST` `{type: 'read-only', user: 'root@example.test'}` | 403 ceiling message (`ADMIN_SCOPES` from the body user; final user `alice@example.test`) | same | parity |
| 18 | `C1` `POST` `{type: 'read-only'}` | 201; scopes `[read]` | same | operation parity |
| 19 | `C1` `POST` `{scopes: []}` | 403 ceiling message (`write` from `USER_DEFAULT_SCOPES`) | same | parity |
| 20 | `C1` `POST` `{scopes: [read]}` | 201 with top-level `key` and `data.key` = secret; `data.href` uses the secret | 201 with top-level `key` and `data.key` = secret; `data.href` uses the id | divergence (S2 only) |
| 21 | `C1` `GET /key/{id}` of `K0` | 200 `{status, total, key: {…, key: <secret>, href: …/key/<secret>}}` | 200, same envelope; key object without `key`; `href` uses the id | divergence (S1, S2) |
| 22 | `C1` `GET /keys` | each element has `key` and a secret `href` | each element without `key`; `href` uses the id | divergence (S1, S2) |
| 23 | `C1` `GET` or `PUT /key/{secret}` of `K0`; `C2` `DELETE /key/{secret}` | 200, the key / `{status}` | 404 `not found`, no side effect | divergence (S3) |
| 24 | `C2` `DELETE /key/{id}` | 200 `{status: 'ok'}` | same | parity |
| 25 | `C1` `PUT` `KA` without `customer` | 200; customer cleared to `NULL` | 200; customer `A` kept | divergence (C23) + S1/S2 |
| 26 | `C3` `PUT` `KA` without `customer` | 200; customer `A` | same | operation parity |
| 27 | `C4` `PUT` `KA` without `customer` | 400 `must define customer as more than one possibility` | 200; customer `A` kept | divergence (C23) |
| 28a | `C3` `PUT` `KA` with `customer: 'A'` | 200; customer `A` | same | operation parity |
| 28b | `C3` `PUT` `KA` with `customer: 'B'` | 400 `not allowed to set customer to 'B'` | same | parity |
| 29-31 | `GET /key/{id of K0}` with the token = `K0`'s **id**, sent as `Authorization: Key`, as `X-API-Key`, and as `?api-key=` | 200, authenticated as `K0` (`alice@example.test`, scopes `[read]`); `K0` use count +1 | 401 `API key parameter '<id>' is invalid`; count and last-used time unchanged | divergence (S4) |
| 32-34 | same request with the token = `K0`'s **secret**, on each of the three carriers | 200; `K0` use count +1 | 200 (S1/S2 apply to the body); `K0` use count +1 | operation parity (positive control) |
| 35 | same request with an unknown token | 401 `API key parameter '<token>' is invalid` | same | parity |
| 36 | same request with the secret of a key stored with an expiry in the past | 401, as row 35 | same | parity |

## SF-NOTES: a note is changed only through its own alert

Status: Proposed. Covers C21.

**Reference.** `PUT` and `DELETE /alert/{alert_id}/note/{note_id}` look up
the alert with the caller's customer filter, but then look up the note by id
alone (`views/alerts.py:666-697`, `:701-723`; `postgres/base.py:1381-1386`)
and update or delete it (`:1411-1436`), without checking that the note
belongs to that alert. A caller who can write one alert can therefore change
or delete any note, including notes on another customer's alerts, by naming
an alert they can see and the other note's id.

**alrust.** The note must belong to the path's alert (the note's `alert` is
that alert's id), else the handler's own not-found refusal: 404 `not found`
for `PUT`, 404 `note not found` for `DELETE`. There is no side effect.

*The same id, an earlier alert.* A client can set an alert's id, and a
deleted alert leaves its notes in the table (the reference does the same;
alrust does not delete them). A later alert can therefore have the id of an
earlier one, also for another customer. Equal ids are then not enough: the
note must have been added to the alert that is stored now. alrust keeps
this in its own table, `alrust_note_bindings` (the note's id with its
alert's id; DECISIONS Y77, ADR-0005 decision 10). The row is written in the
transaction that stores the note. Triggers on `alerts` remove an alert's
rows when any program (the API, the housekeeping, Alerta, an integration)
deletes the alert, stores an alert with that id, changes an alert's id or
truncates the table. The check and the write are in one transaction that
locks the alert's row. The binding does not depend on the alert's history,
so a note stays changeable for as long as its alert is stored.

*A note with no binding is protected:* it is listed and it is not changed
or deleted (the same 404). This applies to a note that another program
wrote to the `notes` table, to a note stored before the bindings existed,
and to every note while the bindings are not in force (the table, the
function or a trigger is absent, or a trigger is disabled). An adopted
reference database has no bindings, because alrust writes no DDL there:
its notes are added and listed and never changed or deleted through
alrust. *Limit:* if a trigger was disabled and enabled again, or bypassed
with `session_replication_role = replica`, a binding can outlive its
alert; alrust cannot detect that. Storing an alert with that id removes it.

`GET /alert/{id}/notes` is not changed by this: it still lists by the
reference's expression (C21), earlier notes included.

**Vectors.**

| Case | Reference | alrust | Kind |
|---|---|---|---|
| `PUT` with an alert the caller can see and a note of another alert (same customer) | 200 | 404 `not found`, no change | divergence |
| same, the note's alert belongs to another customer | 200 | 404, no change | divergence |
| `DELETE` with a note of another alert | 200 | 404 `note not found`, nothing deleted | divergence |
| `PUT` or `DELETE` with the alert's own note | 200 | 200 | parity |
| an alert the caller cannot see | 404 | 404 | parity |
| `PUT` or `DELETE` with a note of a deleted alert, through a later alert with the same id (same or another customer) | 200 | 404, no change, no `dismiss` entry | divergence |
| the same for a note row that no stored alert received (left by an earlier deletion) | 200 | 404, no change | divergence |
| a note added to the later alert | 200 | 200 | parity |
| the alert's own note after its `note` entry left the history | 200 | 200 | parity |
| the alert deleted, its id changed or the table truncated by SQL, then an alert with the same id | 200 | 404, no change | divergence |
| a note with no binding (written by another program, or stored while the bindings were not in force) | 200 | 404, no change | divergence |

## SF-BLACKOUTS: an edit keeps the blackout's customer unless it sets one

Status: Proposed. Covers C22.

**Reference.** `PUT /blackout/{blackout_id}` always sets `customer` from
`assign_customer` (`views/blackouts.py`, `update_blackout`), and the store
writes it whenever present (`postgres/base.py:822-823`). When the body omits
`customer`, an unscoped caller clears it to `NULL`, which widens the
blackout's matching under `CUSTOMER_VIEWS` to every customer's alerts.

**alrust.** Without `customer` in the body, the stored customer is
unchanged. With `customer`, the reference's `assign_customer` rules apply.
The pinned web UI always sends `customer` when editing a blackout
(`BlackoutList.vue:728-731`), so its behavior is unchanged.

**Vectors.**

| Case | Reference | alrust | Kind |
|---|---|---|---|
| unscoped caller, `PUT` without `customer` on a blackout for customer `A` | 200, customer cleared | 200, customer `A` kept | divergence |
| scoped caller (`[A]`), `PUT` without `customer` on its blackout | 200, customer `A` | 200, customer `A` | parity |
| `PUT` with `customer` | reference rules | same | parity |

## SF-MGMT: no secrets from the management endpoint, no default signing key

Status: Proposed. Covers M-SEC-1. Owner decisions Y8 (properties: redacted
settings) and Y9 (an unset `SECRET_KEY` becomes a random key at each start),
both 2026-10-01.

**Reference.**
1. `GET /management/properties` requires `admin:management`
   (`management/views.py:82-84`) and returns `text/plain` with one
   `key: value` line for every entry of the request's WSGI environ, the
   process environment, the app object's attributes and the whole app config
   (`:87-99`). The dump therefore contains `SECRET_KEY`, the `DATABASE_URL`
   password, the OAuth, LDAP and SMTP secrets, connection strings such as
   `CELERY_RESULT_BACKEND` (`settings.py:56`), nested maps such as
   `LDAP_CONFIG` (`:137`), every environment variable, and the caller's own
   `Authorization` header (`HTTP_AUTHORIZATION` in the environ). With
   `AUTH_REQUIRED` false any caller can read it (`auth/decorators.py:155-161`).
2. `SECRET_KEY` defaults to `changeme` (`settings.py:15`). `get_config` takes
   the environment variable when it is present, even when empty, else the
   configuration file's value, else that default (`utils/config.py:29`,
   `:129-148`); nothing at startup checks it.
3. Bearer tokens are HS256 JWTs verified with `SECRET_KEY` and the audience
   `OAUTH2_CLIENT_ID`, else `SAML2_ENTITY_ID`, else `absolute_url()`
   (`models/token.py:43-51`; `utils/response.py:22-27`). Their `scope` claim
   becomes the caller's scopes (`models/token.py:71`;
   `auth/decorators.py:69-91`). Refusals are 401 `Token is invalid`, `Token
   has expired` and `Invalid audience` (`:77-82`). Anyone who knows the key,
   from the dump or because it is still `changeme`, can therefore create a
   token with any scopes, including `admin`.
4. `SECRET_KEY` also signs e-mail confirmation and password-reset tokens
   (`auth/utils.py:106-112`) and is the HMAC key used to derive new API-key
   secrets from random bytes (`utils/key.py:20-28`).

**alrust.**
1. `GET /management/properties` keeps its path, method, scope and
   credential rules, and returns 200 `text/plain`. The body has exactly one
   line per alrust setting, sorted by name: `NAME: VALUE`. Names come only
   from alrust's fixed settings schema (`[A-Z0-9_]+`). It never includes the
   process environment, request data (headers, cookies, query) or internal
   objects. `VALUE` is either the bare token `[REDACTED]` or a JSON value
   (strings quoted, with newlines and control characters escaped), so no
   value can start a new line. Each setting has one class in the schema
   (Y8):
   - **public:** the value is shown, subject to the secret-value check
     below. Only booleans, numbers, strings and lists of strings may be
     public, and no setting whose upper-cased name contains `SECRET`,
     `PASSWORD`, `PASSWD`, `TOKEN`, `CREDENTIAL` or `PRIVATE`, or ends with
     `_KEY` or `_KEYS`, may be public. The one reviewed exception is
     `TOKEN_EXPIRE_DAYS`. The public list is part of the schema and is
     reviewed with the implementation;
   - **connection:** every setting that holds a URL or connection string,
     including every name ending in `_URL` or `_URI`, `CELERY_BROKER_URL` and
     `CELERY_RESULT_BACKEND`. The value is parsed with RFC 3986 generic
     syntax. With a scheme and a non-empty host, it is shown as
     `scheme://` + (`[REDACTED]@` if there is user information) + host
     (IPv6 in brackets) + (`:port` if present) + (`/[REDACTED]` if there is
     a path other than empty or `/`, a query or a fragment). Without a
     scheme or host, or when parsing fails, it is `[REDACTED]`. An unset
     (null) value is `null`;
   - **secret:** every other setting, including every map (such as
     `LDAP_CONFIG`) and every list that is not a list of strings, is
     `[REDACTED]`. Nested names are not inspected. A setting the schema does
     not classify is secret.
   **Secret-value check.** The secret values are:
   - every non-empty string held by a secret setting (each list element and
     map key and value, recursively);
   - every non-empty user-information string and password of a connection
     setting, both as written (percent-encoded) and percent-decoded.

   They are taken from every configured source of that setting (environment
   variable, configuration file, and for `SECRET_KEY` the generated key), not
   only the effective one. The check runs on decoded string values **before**
   serialization. Every string that would be emitted is compared as raw text
   with each secret value, by substring:
   - each public string;
   - each element of a public list;
   - the textual form of a public number;
   - for a connection setting, its scheme, host (as written and
     percent-decoded) and port.

   On any hit, the whole value of that setting is `[REDACTED]`. Only then is
   the remaining value serialized to JSON. Empty strings are never secret
   values. Only configured settings are sources; the process environment is
   never read for this purpose.
2. Signing key at startup (Y9). The effective `SECRET_KEY` follows the
   reference precedence: the environment variable if present (even empty),
   else the configuration-file value. If there is no effective value, or it
   is empty, or it equals the reference default `changeme`, alrust generates
   a random key of at least 256 bits from the operating system's secure
   random source at each start. It logs a warning that sessions, e-mail
   confirmation and password-reset links will not survive a restart and
   that every instance behind one address needs the same configured key.
   The generated key is never logged or shown. An effective key shorter than
   32 characters is used, with a startup warning. Any other effective key is
   used unchanged.
3. HS256 Bearer tokens signed with the effective key keep the reference
   behavior: the `scope` claim becomes the caller's scopes, with the same
   audience and expiry rules and the same three 401 messages in the
   reference error envelope.
4. alrust generates API-key secrets from the secure random source directly;
   they do not depend on `SECRET_KEY`. Their length, entropy and encoding are
   fixed in the implementation's own review.

**Fixtures.**
- Properties profile `MP`: `AUTH_REQUIRED = True`; distinct synthetic
  sentinels (each at least 16 characters, unique, and not a substring of
  any other setting) for `SECRET_KEY`, `OAUTH2_CLIENT_SECRET`,
  `LDAP_BIND_PASSWORD` and `SMTP_PASSWORD`; `DATABASE_URL` is the
  authorized disposable database's URL, whose role password is the
  synthetic `<db-sentinel>`; its scheme, host, port and database name are
  taken from the fixture attestation, not assumed. `CELERY_BROKER_URL` is
  unset, so no background task runs; the reference reads the Celery
  settings only inside the bulk handlers (`views/bulk.py:24`, `:76`;
  `app.py:102-110`), which these rows never call.
  `CELERY_RESULT_BACKEND = redis://:<celery-sentinel>@cache.test:6379/0`
  is therefore display-only. The rows below treat `TOKEN_EXPIRE_DAYS`,
  `AUTH_REQUIRED`, `ALLOWED_ENVIRONMENTS`, `DEFAULT_ENVIRONMENT` and
  `CLIPBOARD_TEMPLATE` (`settings.py:240`; no startup invariant) as public;
  `LDAP_CONFIG = {'BindPassWord': <ldap-map-sentinel>, 'Uri':
  'ldap://u:<ldap-url-sentinel>@ldap.test'}`; process environment variable
  `SYNTHETIC_ENV_SECRET = <env-sentinel>` (not a setting). Callers: `M1`,
  an API key with scopes `[admin:management]`; `M2`, an API key with
  `[read]`.
- Token profile `TP`: `AUTH_REQUIRED = True`, `ALLOW_READONLY = False`,
  `AUTH_PROVIDER = 'basic'`, `AUTH_PROXY = False`,
  `EMAIL_VERIFICATION = False`, `OAUTH2_CLIENT_ID = ''`,
  `SAML2_ENTITY_ID = None`, `BASE_URL = 'http://alrust.test'` (so the
  effective audience is `http://alrust.test`),
  `ADMIN_USERS = ['admin@example.test']`, `TOKEN_EXPIRE_DAYS = 14`. A
  seeded active user `admin@example.test` with a synthetic password and
  `email_verified = true`. Keys `K40` and `K40b` are distinct 40-character
  synthetic values; `K10` is a 10-character value.
- Request `RQ`: `GET /management/properties` with `Authorization: Bearer
  <token>`. Accepted means 200 `text/plain`; refusals use the reference
  error envelope.
- Minted token `MT(key, overrides)`: HS256 over the claims `iss
  'http://alrust.test/'`, `typ 'Bearer'`, `sub 'synthetic-sub'`, `aud`
  the effective audience, `iat` and `nbf` now, `exp` now + 3600 s, `jti` a
  fresh UUID, `preferred_username 'mint@example.test'`, `scope 'admin'`,
  `customers []`, with the overrides applied.
- Restart procedure `RS`: log in with `POST /auth/login` `{username:
  'admin@example.test', password}`; confirm `RQ` with that token is
  accepted; stop the server and start it again with the same database,
  configuration file, environment and `BASE_URL`, changing only what the
  row varies; send `RQ` with the same token. The token's expiry (14 days)
  is beyond both observations.

**Vectors.** Reference cells are source-derived. Before execution they are
NOT_RUN, and their known predicates are asserted on the first authorized
run. Only cells marked **reference pending** skip, and only that predicate.
alrust cells are NOT_RUN until an implementation exists.

| # | Case | Reference | alrust | Kind |
|---|---|---|---|---|
| 1 | `MP`, `M1` `GET /management/properties` | 200 `text/plain` containing every sentinel, `SYNTHETIC_ENV_SECRET` and `M1`'s key | 200 `text/plain`; no sentinel, environment variable or request header anywhere in the body; one line per setting, sorted; `SECRET_KEY: [REDACTED]` | divergence (Y8) |
| 2 | same; the connection lines | full URLs with passwords | `DATABASE_URL: "<attested scheme>://[REDACTED]@<attested host>:<attested port>/[REDACTED]"`; `CELERY_RESULT_BACKEND: "redis://[REDACTED]@cache.test:6379/[REDACTED]"` | divergence (Y8) |
| 3 | same; `LDAP_CONFIG` (mixed-case nested secret name and nested URL) | the map, with both sentinels | `LDAP_CONFIG: [REDACTED]` | divergence (Y8) |
| 4 | `MP` with `CELERY_RESULT_BACKEND = 'not a url'`; then `'redis://'` (no host) | shown as given | `CELERY_RESULT_BACKEND: [REDACTED]` both times | divergence (Y8) |
| 5 | `MP` with `ALLOWED_ENVIRONMENTS = ['Production', <smtp-sentinel>]` and `DEFAULT_ENVIRONMENT = <smtp-sentinel>` (the startup check at `utils/config.py:123-124` passes) | both shown | `ALLOWED_ENVIRONMENTS: [REDACTED]`; `DEFAULT_ENVIRONMENT: [REDACTED]` | divergence (Y8, secret-value check) |
| 5b | `MP` with `SMTP_PASSWORD = 'SYNTHETIC"pass\\word\tX'` (Python string-literal notation: one double quote, one backslash and one tab character) and `CLIPBOARD_TEMPLATE` containing that exact string | both shown | `CLIPBOARD_TEMPLATE: [REDACTED]` (compared before JSON escaping) | divergence (Y8) |
| 5c | `MP` with `CELERY_RESULT_BACKEND = 'redis://:p%40ss-SYNTHETIC-0001@cache.test:6379/0'` and `CLIPBOARD_TEMPLATE` containing `p@ss-SYNTHETIC-0001` | both shown | `CLIPBOARD_TEMPLATE: [REDACTED]` (decoded alias) | divergence (Y8) |
| 5d | `MP` with `SMTP_PASSWORD = 'secret-host-SYNTHETIC.test'` and `CELERY_RESULT_BACKEND = 'redis://secret-host-SYNTHETIC.test:6379/0'` | both shown | `CELERY_RESULT_BACKEND: [REDACTED]` (host equals a secret) | divergence (Y8) |
| 6 | `MP` with `CLIPBOARD_TEMPLATE = "x\nSECRET_KEY: leaked"` | two lines | one line: `CLIPBOARD_TEMPLATE: "x\nSECRET_KEY: leaked"` with the newline escaped | divergence (Y8, escaping) |
| 7 | `MP` with `SMTP_PASSWORD = ''` | — | other values unaffected; `SMTP_PASSWORD: [REDACTED]` | alrust control (empty is not a secret value) |
| 8 | `MP`; positive controls | `TOKEN_EXPIRE_DAYS: 14`, `ALLOWED_ENVIRONMENTS` listed | `TOKEN_EXPIRE_DAYS: 14`; `ALLOWED_ENVIRONMENTS: ["Production","Development"]`; `AUTH_REQUIRED: true` | parity of values (format differs) |
| 9 | `MP`, `M2` `GET /management/properties` | 403 `Missing required scope: admin:management` | same | parity |
| 10 | `MP` with `AUTH_REQUIRED = False`, no credentials | 200, the full dump | 200, redacted as in rows 1-3 | divergence (Y8) |
| 11 | `TP`, `SECRET_KEY` in neither environment nor file; `RQ` with `MT(changeme)` | accepted | 401 `Token is invalid`; handler not run | divergence (Y9) |
| 12 | `TP`, file `SECRET_KEY = 'changeme'`, no environment variable; `RQ` with `MT(changeme)` | accepted | 401 `Token is invalid` | divergence (Y9) |
| 13 | `TP`, environment `SECRET_KEY=` (empty), file `K40`; `RQ` with `MT(K40)` | 401 `Token is invalid` (effective key empty); reference pending | 401 `Token is invalid` (random key) | divergence (Y9) |
| 14 | `TP`, environment `SECRET_KEY=` (empty), no file value; `RQ` with `MT('')` | reference pending | 401 `Token is invalid` | divergence (Y9) |
| 15 | `TP`, file `K40`, no environment variable; `RQ` with `MT(K40)` | accepted, scopes `[admin]` | same | parity (HS256 claim trust kept) |
| 16 | `TP`, environment `K40`, file `K40b`; `RQ` with `MT(K40)`; then with `MT(K40b)` | accepted; 401 `Token is invalid` | same | parity (precedence) |
| 17 | `TP`, file `K40`; `RQ` with `MT(K40b)` | 401 `Token is invalid` | same | parity (wrong key) |
| 18 | `TP`, file `K40`; `RQ` with `MT(K40, exp = now - 60 s)` | 401 `Token has expired` | same | parity |
| 19 | `TP`, file `K40`; `RQ` with `MT(K40, aud = 'http://other.test')` | 401 `Invalid audience` | same | parity |
| 20 | `TP`, no `SECRET_KEY`; `RS` | the token stays accepted after the restart | accepted before; 401 `Token is invalid` after | divergence (Y9) |
| 21 | `TP`, file `K40`; `RS` | accepted before and after | same | parity |
| 22 | `TP`, file `K10`; `RQ` with `MT(K10)` | accepted | accepted; a startup warning was logged | parity (warning only) |

Tokens with other algorithms are not covered by these vectors; their
refusal is left to the implementation review.

## SF-QUERY: no request or stored text in SQL, and `q=` keeps every operand

Status: Proposed. Covers the four string-formatted SQL constructions and the
`q=` search dropping operands (both under Y3).

**Principle.** alrust never places request text or stored text into SQL
text. Values are bound parameters. Identifiers come only from fixed lists:
the reference's sort columns, the queried table's columns (for `group-by`,
`q.df` and `q=` search fields) and its `jsonb` columns (for attribute
paths). They are emitted quoted.

**Reference.**
1. `sort-by=attributes.<k>` (and `-attributes.<k>`): the second
   dot-separated segment is taken (`database/backends/postgres/utils.py:38-39`).
   If it is non-empty, it is formatted into the ORDER BY clause as
   `attributes->'<k>'`; if it is empty (`attributes.`), the whole
   `attributes` column is used (`:46-49`). Every list using
   `sort_by_columns` with an `attributes` sort entry is affected.
2. `group-by` on the six top-N endpoints (`/alerts/top10/…` and
   `/alerts/topn/…` for `count`, `flapping` and `standing`,
   `views/alerts.py:405-483`): the first value, default `event`, is
   formatted unvalidated into the SELECT and GROUP BY clauses
   (`base.py:556-570`, `:582-596`, `:608-623`). The response key is the raw
   value (`:577`, `:603`, `:630`). Because it is unquoted, `group-by=group`
   (a reserved word) is a SQL error, as is any non-column. `count` joins
   `UNNEST(service)`; `flapping` and `standing` also join `UNNEST(history)`
   filtered to `type = 'severity'`, so every alert counts once per service
   and per severity-history entry. `standing` orders by the summed
   `last_receive_time - create_time`.
3. `q.df`: when `q` is non-empty, the default-field placeholder is replaced
   by plain string replacement inside a double-quoted identifier
   (`queryparser.py:284-289`; alerts `utils.py:145-151`, blackouts
   `:231-237`). A value containing `"` ends the identifier and the rest
   becomes SQL text. With an empty `q`, `q.df` is ignored.
4. `q=` search fields: a field name is a grammar word that cannot contain
   `"` (`queryparser.py:237-238`, `:244`) and is emitted as a quoted
   identifier without any column check (`:153-155`, `:173-175`, `:213`), so
   an unknown field reaches SQL (`unknownfield:disk` becomes `"unknownfield"
   ILIKE …`). For an attribute path `p.k`, the key `k` is bound
   (`:147-151`, `:209-211`). The prefix `p` is a quoted identifier: in a term,
   every `_` is replaced by `attributes` (`:146`, `:166`); in a subquery,
   `_` alone becomes `attributes` and any other prefix is used as written
   (`:208`).
5. `GET /environments`: the stored environment names of the page are
   formatted into `environment in ('<name>', …)` (`base.py:646`). A stored
   name containing `'` becomes SQL text; an alert received with `status:
   blackout` can store an arbitrary environment (lifecycle R1 step 1).
6. `q=` flat chains: `BinaryOperation` keeps only the first two operands of
   a same-level chain (`queryparser.py:20-26`). So `a AND b AND c` means
   `a AND b`, and `a b c` (adjacency is OR) means `a OR b`; this also holds
   inside subqueries. Codex's pure-source probe (`01c183e3`), replayed
   byte-identically by Claude, shows the translations used below.

**alrust.**
1. `sort-by`: the reference's selection is kept. With a non-empty second
   segment, the key is a bound parameter (`attributes -> $n`). With an
   empty segment, or with `attributes` alone, the whole `attributes`
   column is used, as in the reference. Directions and every other
   `sort-by` rule are unchanged.
2. `group-by` (top-N endpoints only; elsewhere it stays ignored): the value
   must exactly equal (case-sensitive) one of the `alerts` columns: `id`,
   `resource`, `event`, `environment`, `severity`, `correlate`, `status`,
   `service`, `group`, `value`, `text`, `tags`, `attributes`, `origin`,
   `type`, `create_time`, `timeout`, `raw_data`, `customer`,
   `duplicate_count`, `repeat`, `previous_severity`, `trend_indication`,
   `receive_time`, `last_receive_id`, `last_receive_time`, `history`,
   `update_time` (`sql/schema.sql:32-62`). It is emitted quoted, and the
   response key is that name. Otherwise: 400 `Grouping by '<value>' field
   not supported.` The default stays `event`. The joins, counting and
   ordering above are unchanged.
3. `q.df`: when `q` is non-empty, `q.df` (if given) must exactly equal a
   column of the queried table, whether or not the query uses the default
   field. Otherwise: 400 `Default field '<value>' not supported.` The
   columns are the list above for alert queries, or for blackout queries
   `id`, `priority`, `environment`, `service`, `resource`, `event`,
   `group`, `tags`, `customer`, `start_time`, `end_time`, `duration`,
   `user`, `create_time`, `text`, `origin` (`sql/schema.sql:78-97`,
   including the columns added by `ALTER TABLE`). With an empty `q`,
   `q.df` stays ignored.
4. `q=` search fields:
   - a plain field must exactly equal a column of the queried table, or be
     `_exists_` (unchanged meaning);
   - for an attribute path `p.k`, `p` is first resolved exactly as the
     reference does for that form, and the result must be a `jsonb` column
     of the queried table (alerts: `attributes`; blackouts: none). The key
     `k` stays bound.

   Otherwise: 400 `Search field '<field as written>' not supported.` This
   deliberately replaces the reference's unknown-column SQL error. For a
   listed column whose type does not support the generated operator, this
   entry does not change the outcome. The parser's other behaviors (the `+`
   and `-` modifiers, ignored trailing input, term translations) are
   unchanged.
5. `GET /environments`: the page's names are one bound array parameter
   (`environment = ANY($n)`). Every name, including one containing `'`, is
   listed and counted like any other.
6. `q=` chains: a same-precedence chain of n ≥ 2 operands is folded to the
   left, `((x1 op x2) op x3) … op xn`. Each binary step uses the reference's
   translation for a pair, including its rule that `OR` with a `NOT` right
   operand becomes `AND` (`queryparser.py:54-57`). With two operands the
   result is exactly the reference's.
7. Order of checks; the first failure wins, with no side effect:
   1. `q` syntax (the reference's 400 `Failed to parse query string.`);
   2. `q.df`;
   3. `q=` search fields, in order of appearance;
   4. the reference's other filter checks;
   5. `sort-by` (the reference's 400 for an unsupported name);
   6. `group-by`.

**Fixtures.** Each fixture is the only data in its own disposable store.
`AUTH_REQUIRED = True`, `CUSTOMER_VIEWS = False`; the caller is a synthetic
API key with scopes `[read]` and no customer. Requests use the default page
size, which exceeds every fixture's size. `T0` is a fixed fixture time at
least two hours before the first request: alert lists include only alerts
with `last_receive_time` at or before the request time
(`views/alerts.py:315-316`; `postgres/utils.py:165-175`). Every alert is
seeded directly and completely, with no ingestion defaults: a stable `id`
(given below), `severity 'major'`, `status 'open'`, `previous_severity
'indeterminate'`, `trend_indication 'moreSevere'`, `type 'exceptionAlert'`,
`origin 'fixture'`, `value NULL`, `repeat false`, `timeout 86400`,
`correlate []`, `tags []`, `last_receive_id` equal to its `id`,
`create_time = receive_time = T0`, `update_time = T0`, and
`last_receive_time` and `duplicate_count` as stated (otherwise `T0` and 0).
- **`QF`** (query store): six alerts with ids `q1` … `q6` in table order,
  all `environment = 'Production'`,
  `service = ['svc']`, `customer = NULL`, empty history, distinct events
  `ev-1` … `ev-6`:

  | Resource | Text | Attributes |
  |---|---|---|
  | `res-alpha` | `disk` | `{"region": "b"}` |
  | `res-bravo` | `full` | `{"region": "a"}` |
  | `res-charlie` | `server` | `{"reg'ion": "z"}` |
  | `res-delta` | `disk full` | `{}` |
  | `res-echo` | `disk full server` | `{}` |
  | `res-foxtrot` | `other` | `{}`, with `environment = 'Prod''uction'`, seeded directly |

  One blackout: `priority 1`, `environment 'Production'`, empty service and
  tags, no resource, event, group, customer or origin, `start_time` and
  `end_time` fixed one hour before and one day after the fixture time,
  `user 'admin@example.test'`, `create_time` the fixture time, `text 'bo'`.
- **`TN`** (top-N store): three alerts, `environment = 'Production'`,
  `service = ['svc']`, `customer = NULL`, `create_time = T0` (a fixed
  fixture time). Each history
  has the stated number of `type = 'severity'` entries plus one
  `type = 'status'` entry, which the filter excludes:

  | Id | Resource | Event | Group | `duplicate_count` | Severity entries | `last_receive_time` |
  |---|---|---|---|---|---|---|
  | `t1` | `res-1` | `ev-a` | `grp-x` | 2 | 1 | `T0 + 10 min` |
  | `t2` | `res-2` | `ev-a` | `grp-x` | 0 | 2 | `T0 + 60 min` |
  | `t3` | `res-3` | `ev-b` | `grp-y` | 5 | 1 | `T0 + 30 min` |

  Every top-N item has `environments ['Production']` and `services
  ['svc']`, and `resources` lists `{id, resource, href}` for its alerts.
  Items with equal sort values may appear in any order; the assertions
  compare those as sets.

**Vectors.** Reference cells are source-derived. Before execution they are
NOT_RUN, and their known predicates are asserted on the first authorized
run. Only cells marked **reference pending** skip, and only that predicate.
alrust cells are NOT_RUN until an implementation exists. Each vector has at
most one invalid input unless it says otherwise.

| # | Case (store) | Reference | alrust | Kind |
|---|---|---|---|---|
| 1 | `GET /alerts?sort-by=attributes.region` (QF) | first `res-bravo`, then `res-alpha`, then the rest | same | parity |
| 2 | `sort-by=-attributes.region` (QF) | the four without `region` first (NULLs first in DESC), then `res-alpha`, then `res-bravo` | same | parity |
| 3 | `sort-by=attributes.region.x` (QF) | as row 1 | same | parity |
| 4 | `sort-by=attributes.`; `sort-by=-attributes.`; `sort-by=attributes` (QF) | ordered by the whole `attributes` column, ASC, DESC, ASC | same | parity |
| 5 | `sort-by=attributes.reg'ion` (QF) | the key ends the SQL literal; reference pending | 200; `res-charlie` first, then the rest | divergence |
| 6 | `GET /alerts/top10/count` (TN) | `ev-a` (count 2, `duplicateCount` 2, resources `t1`, `t2`), then `ev-b` (1, 5, `t3`) | same | parity |
| 7 | `GET /alerts/top10/flapping` (TN) | `ev-a` (count 3, `duplicateCount` 2), then `ev-b` (1, 5) | same | parity |
| 8 | `GET /alerts/top10/standing` (TN) | `ev-a` (count 3, `duplicateCount` 2), then `ev-b` (1, 5). The order follows the summed lifetimes derived from the fixture (130 min and 30 min), which the response does not contain (`base.py:625-632`) | same | parity |
| 9 | `GET /alerts/topn/flapping?group-by=resource` (TN) | `res-2` (count 2) first, then `res-1` and `res-3` (count 1 each) in any order; key `resource` | same | parity |
| 10 | `GET /alerts/top10/count?group-by=group` (TN) | SQL error (`group` is reserved); reference pending | 200: `grp-x` (count 2), then `grp-y` (1); key `group` | divergence |
| 11 | `GET /alerts/top10/standing?group-by=event,resource` (TN) | the text becomes SQL; reference pending | 400 `Grouping by 'event,resource' field not supported.` | divergence |
| 12 | `GET /alerts/top10/count?group-by=Resource` (TN) | reference pending | 400 `Grouping by 'Resource' field not supported.` | divergence |
| 13 | `GET /alerts?group-by=bogus` (QF) | ignored; 200 | same | parity |
| 14 | `GET /alerts?q=alpha&q.df=resource` (QF) | `res-alpha` | same | parity |
| 15 | `q=disk&q.df=resource" IS NOT NULL OR "text` (QF) | the value becomes SQL text and widens the match; reference pending | 400 `Default field 'resource" IS NOT NULL OR "text' not supported.` | divergence |
| 16 | `q=alpha&q.df=Resource` (QF) | unknown column; reference pending | 400 `Default field 'Resource' not supported.` | divergence |
| 17 | `q=resource:alpha&q.df=bogus` (QF) | 200 `res-alpha` (`q.df` unused) | 400 `Default field 'bogus' not supported.` | divergence |
| 18 | `GET /alerts?q.df=anything` (no `q`) (QF) | `q.df` ignored | same | parity |
| 19 | `q=resource:alpha` (QF) | `res-alpha` | same | parity (explicit column) |
| 20 | `q=unknownfield:disk` (QF) | unknown-column SQL error; reference pending | 400 `Search field 'unknownfield' not supported.` | divergence |
| 21 | `q=_.region:a` (QF) | `res-bravo` (ILIKE `%a%` on the `region` value) | same | parity (attribute path) |
| 22 | `q=raw_data.x:y` (QF) | prefix becomes `rawattributesdata`, unknown column; reference pending | 400 `Search field 'raw_data.x' not supported.` | divergence |
| 23 | `q=_exists_:region` (QF) | `res-alpha`, `res-bravo` | same | parity |
| 24 | `GET /blackouts?q=Production&q.df=environment`; then `q.df=severity` (not a blackouts column) (QF) | 200 with the blackout; then an unknown-column error, reference pending | 200 with the blackout; then 400 `Default field 'severity' not supported.` | parity; divergence |
| 25 | `GET /environments` (QF) | `Prod'uction` ends the SQL literal; reference pending | 200; `Production` (5 alerts) and `Prod'uction` (1) listed with their counts | divergence |
| 26 | `q=disk full server` (QF) | `disk OR full`: `res-alpha`, `res-bravo`, `res-delta`, `res-echo` | those four plus `res-charlie` | divergence (chain) |
| 27 | `q=disk AND full AND server` (QF) | `disk AND full`: `res-delta`, `res-echo` | `res-echo` | divergence (chain) |
| 28 | `q=disk OR full OR NOT server` (QF) | `disk OR full`: as row 26's reference | `(disk OR full) AND NOT server`: `res-alpha`, `res-bravo`, `res-delta` | divergence (chain) |
| 29 | `q=resource:(alpha OR bravo OR charlie)` (QF) | `res-alpha`, `res-bravo` | `res-alpha`, `res-bravo`, `res-charlie` | divergence (chain) |
| 30 | `q=disk AND full`; `q=disk full`; `q=disk AND (full AND server)` (QF) | `res-delta`, `res-echo`; row 26's reference set; `res-echo` | same | parity |
| 31 | two invalid inputs: `GET /alerts/top10/count?q=alpha&q.df=bogus&group-by=bogus` (TN) | reference pending | 400 `Default field 'bogus' not supported.` (`q.df` precedes `group-by`) | divergence (order) |

Row 15's value is a fixed synthetic string, run only against the
disposable fixture; it reads and writes nothing. These vectors authorize no
provisioning and no SQL or HTTP execution.

## SF-WEBHOOKS: no credentials in logs, whole batches, plain placeholders

Status: Proposed. Covers W-SEC-1, W-DL-1 (owner decision Y10), WP-SEC-1
(owner decision Y12) and the Prometheus `moreInfo` part of WUI-SEC-1 (owner
decision Y11), all 2026-10-01.

**Reference.**
1. W-SEC-1, logs and audit:
   - The webhooks blueprint's `before_request` (`webhooks/__init__.py:17-24`)
     runs before authentication on every method. At INFO it logs the
     method, the full URL (including any `?api-key=`), all request headers
     (including `Authorization`, `X-API-Key` and `Cookie`) and the whole
     body.
   - Audit records (`utils/audit.py:79-118`) carry the full `request.url`,
     all query `args` and the JSON body (only a top-level `password` is
     redacted), and their `message` can be the alert text. They go to the
     application log (`AUDIT_LOG`) and to `AUDIT_URL`, for the categories in
     `AUDIT_TRAIL`, default `['admin']` (`settings.py:178-182`).
   - The request id is client-controlled (`X-Request-ID`,
     `utils/tracing.py:18-25`). It is returned as the `X-Request-ID`
     response header (`:28-31`), and in the JSON body only in error
     envelopes (`exceptions.py:98-107`).
   - An invalid API key is echoed in the 401 message
     (`auth/decorators.py:44-45`).
2. W-DL-1, batches:
   - The Prometheus webhook maps every item of `alerts` before any
     processing (`webhooks/prometheus.py:110-114`). One malformed item
     fails the whole request, and nothing is processed. A `KeyError`,
     `AttributeError` or `ValueError` from mapping gives 400 with the
     exception's text (`webhooks/custom.py:38-39`). A `TypeError` (for
     example `labels.timeout = []` reaching `int([])`,
     `webhooks/prometheus.py:60`, where only `ValueError` is caught) takes
     the dispatcher's legacy retry without `path` and ends in the generic
     500.
   - The envelope check tests only that `alerts` is present (`:112`). A
     missing `alerts` gives 400 `no alerts in Prometheus notification
     payload`. An empty list, `{}` or `''` maps to no alerts, and the
     request returns 201 `{status, ids: []}` (`custom.py:81-84`). A `null`
     or a number is not iterable: `TypeError`, so `custom.py:31-35` retries
     the call without its required `path` argument, which raises again, and
     the generic handler returns 500 `An internal error has occurred!`
     (`exceptions.py:120-136`). A non-empty string or object is iterated,
     and `parse_prometheus` fails at its first `alert.get` (`:18`) with
     `AttributeError`: 400 `'str' object has no attribute 'get'`. Codex's
     source-only probes (`3154abb3`, `a534124f`, both replayed
     byte-identically by Claude) show these mappings and exception
     classes.
   - `custom` then processes the mapped alerts one by one (`:41-84`).
     `process_alert` runs the pre-receive plugins, then stores the alert by
     deduplication, correlation or creation, then runs the post-receive
     plugins (`utils/api.py:40-92`). A post-receive failure therefore
     follows a durable store change.
   - The heartbeat plugin creates a heartbeat record before signalling
     (`plugins/heartbeat.py:20-33`), and the response carries that
     heartbeat's id (`custom.py:62-64`).
   - The first alert whose processing does not complete ends the request
     with that alert's response: rejected (403), rate-limited (429),
     heartbeat (202), blackout (202), forwarding loop (202), or an error
     (the `AlertaException` code, else 500). Alerts before it are processed;
     alerts after it are never processed.
   - When all complete, the response is 201 `{status, id, alert}` for one
     alert, or `{status, ids}` for several, with the ids of the stored
     alerts after deduplication (`:81-84`).
3. WP-SEC-1: each label value is run through `str.format(**labels)`, and
   each annotation through `str.format(**expanded labels)`; any exception
   keeps the original value (`webhooks/prometheus.py:20-34`). The full
   format language applies. A non-string value raises and is kept as is. A
   referenced non-string label is inserted as Python's `str()` of it: `5`,
   `True`, `None`, `[1]`, `{'k': 'v'}` (Codex's probe, replayed).
4. `moreInfo`: with a `generatorURL`, the mapping stores the HTML string
   `<a href="<generatorURL>" target="_blank">Prometheus Graph</a>` as an
   annotation, which becomes `attributes.moreInfo` (`:74-75`, `:82`).

**alrust.**
1. Sinks (W-SEC-1). The sinks are: the application log at every level,
   error and exception logs, debug output, and audit records in every
   destination (log and `AUDIT_URL`). Rules for all of them:
   - **No credential value**, anywhere in a sink. The protected values of
     a request are, whether valid or not:
     - the `Authorization` header value and the token after its `Key ` or
       `Bearer ` prefix;
     - the `X-API-Key` value;
     - the `api-key` query value;
     - the `Cookie` header value and each cookie value.

     Before any field is encoded for a sink (JSON, log line or URL), every
     occurrence of a non-empty protected value in every retained text
     field is replaced by `[REDACTED]`, in its raw, percent-encoded (upper-
     or lower-case hex) and `+`-for-space forms. The retained text fields
     are: the request id, user agent, path, URL, query names and values,
     audit data and message, and every log message. The name-based rules
     below apply as well. The 401 response itself keeps the reference's
     message (SF-KEYS S4).
   - **No query string or request body** in the application, error or
     debug logs. Webhook request logging records only the method, the route
     template (`/webhooks/<webhook>/<path>`), the webhook name if it is
     registered (else `<unknown>`), the body length and the request id.
   - **Audit records** keep the reference's event, category, user,
     resource, endpoint, method, remote IP and user-agent fields, with:
     - `request.url` without its query string;
     - `request.args` keeping names, with the value of `api-key` and of every
       name matching the SF-MGMT secret-name patterns replaced by
       `[REDACTED]`;
     - `request.data` null for webhook routes. Elsewhere, the JSON body with
       every member, at any depth, whose name matches those patterns
       replaced by `[REDACTED]`;
     - for webhook alert events, `message` is the fixed text `alert
       received via <webhook> webhook`, never the alert text.
   - **Logged identifiers are safe:** after the credential-value rule, the
     request id written to any sink has every character outside
     `[A-Za-z0-9._:-]` replaced by `_` and is cut to 128 characters.
     Responses keep the reference's request id (header, and error body).
2. Batches (Y10).
   - **Envelope (Prometheus).** A missing `alerts` gives 400 `no alerts in
     Prometheus notification payload`, as in the reference. An empty list
     gives 201 `{status, ids: []}`, as in the reference. Any other
     non-list `alerts` (`null`, a number, a string or an object, empty or
     not) gives the same 400, with nothing processed. Against the
     reference that is: a divergence for `{}` and `''` (201); a divergence
     for `null` and numbers (500); status parity with a different message
     for a non-empty string or object (400 `'str' object has no attribute
     'get'`).
   - **Items.** A malformed item (one whose mapping fails) becomes an
     `invalid` result for that item, and every other item is still mapped
     and processed. A batch of exactly one malformed item gives 400
     `Invalid alert at index 0 in webhook payload.` with no `results`. Its
     status matches the reference when the reference's mapping failure is a
     `KeyError`, `AttributeError` or `ValueError` (400 with the exception
     text, a deliberate message divergence). It diverges when the failure
     is a `TypeError` (the reference's 500). This takes precedence over the
     single-item parity rule below.
   - **Processing.** Every mapped alert is processed in order, each
     independently, with the reference's per-alert handling and audit
     events (sanitized as above). There is no rollback: a store change made
     before a later failure in the same alert's processing stays, as in the
     reference.
   - **`results`** is a list in batch order of `{index, inputId,
     resourceId, outcome, code, message}`:
     - `index` counts from 0;
     - `inputId` is the id of the mapped input alert (null for an `invalid`
       item);
     - `resourceId` is the id of what the processing produced: the stored
       alert after deduplication or correlation, the created heartbeat, or
       null when nothing was produced;
     - `outcome` is one of `processed`, `invalid`, `rejected`,
       `rate-limited`, `heartbeat`, `blackout`, `forwarding-loop` or
       `error`. An `error` whose failure came after the store step has its
       store change kept;
     - `code` is the status the reference would return for that alert alone
       (201 when processed, 400 when invalid);
     - `message` is null when processed, else that response's message (for
       `invalid`: `Invalid alert at index <index> in webhook payload.`).
   - **Response.**
     - If every item is `processed`, the response is the reference's (201,
       `{status, id, alert}` for one, `{status, ids}` with the
       `resourceId`s for several).
     - A batch of exactly one item that maps successfully gets the
       reference's response in every case.
     - Otherwise, the status and members are those of the reference's
       response for the **first** item whose outcome is not `processed`,
       plus `results`. This is the one stated exception to the common error
       envelope.
   - **Side effects.** The common "no side effect" rule does not apply to a
     batch that was admitted and partly processed. It still applies to
     every refusal before dispatch: authentication, scope, unknown webhook,
     malformed envelope.
3. Placeholders (Y12). Each label value is expanded against the original
   labels, and each annotation against the expanded labels, in the
   reference's order. The full Python format language is never invoked.
   1. A non-string label or annotation value is kept as is.
   2. The value is read left to right as literal text, `{{`, `}}` and brace
      groups `{…}` (from a `{` to the next `}`).
   3. The value is kept unchanged if it has any of: an unmatched `{` or a
      lone `}`; an empty or all-digit group; a plain-name group (one with
      none of `:`, `!`, `.`, `[`) whose name is missing from the map. This
      matches the reference, which keeps the value on error. The value is
      also kept unchanged when a plain-name group refers to a list or an
      object. That is a deliberate divergence: the reference inserts
      Python's representation of it.
   4. Otherwise the output is built left to right: literal text as is, `{{`
      as `{`, `}}` as `}`, a plain-name group as the referenced value (a
      string as is; an integer, boolean or null as Python's `str()`: for
      example `5`, `True`, `None`; a float as Python's `str()`), and every
      other group exactly as written, braces included.
   5. The output's size is counted in UTF-8 bytes, with overflow-safe
      arithmetic, **before** each append. If an append would make it larger
      than 65,536 bytes, building stops at once and the original value is
      kept. No longer string is ever allocated.
4. `moreInfo` (Y11): with a `generatorURL`, the Prometheus mapping stores the
   plain URL string as `attributes.moreInfo`. The rest of the mapping is
   unchanged.

**Fixtures.** Each vector runs on its own fresh store; each sub-case marked
"separately" gets a fresh store too. Settings:
- `PLUGINS` the default list (`settings.py:272`);
- `ALLOWED_ENVIRONMENTS = ['Production', 'Development']`,
  `DEFAULT_ENVIRONMENT = 'Production'`;
- `NOTIFICATION_BLACKOUT = False`, `FWD_DESTINATIONS = []`,
  `HEARTBEAT_EVENTS = ['Heartbeat']`;
- `AUTH_REQUIRED = True`, `CUSTOMER_VIEWS = False`;
- `AUDIT_TRAIL = ['admin', 'write', 'auth']`, `AUDIT_LOG = True`;
- the log level DEBUG for alrust and INFO for the reference.

The caller holds a synthetic key with `write:webhooks` and no customer.
Every sink is captured for the request: the application log, the error log
and the audit records.

Payloads are `POST /webhooks/prometheus` with `{"alerts": [...]}`. Each item
has:
- `status 'firing'` and fixed `startsAt` and `endsAt`;
- labels `alertname 'A-<row>-<n>'`, `instance 'h-<row>-<n>'` and
  `severity 'major'`, and no `service` label (the reference maps that to
  `['']`);
- annotation `summary 'S-<row>-<n>'`;
- no `generatorURL`, unless a row says otherwise.

Resources therefore differ across vectors. Where a row names a blackout, it
is `environment 'Production'`, the named resource only, from one hour
before to one hour after the request.

**Vectors.** Reference cells are source-derived. Before execution they are
NOT_RUN, and their known predicates are asserted on the first authorized
run. Only cells marked **reference pending** skip, and only that predicate.
alrust cells are NOT_RUN until an implementation exists. "Rows" means alert
rows in the store; "audit" means the events of the write category.

| # | Case | Reference | alrust | Kind |
|---|---|---|---|---|
| 1 | one item; `Authorization: Key <valid key>` plus `?api-key=<s2>` (the header wins); body `summary` set to sentinel `<s3>` | the request log contains the key, `<s2>` and `<s3>`; the audit record contains `<s2>` (in `url` and `args`) and `<s3>` (in `data`) | no sink contains the key, `<s2>` or `<s3>`; the alert's stored `text` is `<s3>` | divergence |
| 2 | `X-API-Key: <invalid sentinel s4>` | 401 echoing `<s4>`; the request log contains `<s4>` | 401 echoing `<s4>` (SF-KEYS S4); no sink contains `<s4>` | divergence (logs) |
| 3 | valid key, plus `Cookie: session=<s5>` and `X-Request-ID: id<TAB>x"y` | the request log contains `<s5>`; logged request ids hold the tab and the quote | no sink contains `<s5>`; every logged request id is `id_x_y`; the 201 response's `X-Request-ID` header is the value as sent (a success body has no `requestId`) | divergence |
| 3b | valid key whose token `<t>` is also sent as `X-Request-ID: <t>`, `User-Agent: <t>`, query `?note=<t>` and path `/webhooks/prometheus/<t>` | the request log contains `<t>`; the audit record contains `<t>` in `url`, `args` and `userAgent` | no sink contains `<t>` in raw or percent-encoded form; the `X-Request-ID` response header is `<t>` as sent | divergence |
| 4 | four items; `h-4-2` is blacked out | 202 `Suppressed alert during blackout period` (`plugins/blackout.py:33`); rows for `h-4-1` only; audit `webhook-received` (1), `alert-blackout` (1) | 202, same members plus `results`: processed, blackout, processed, processed; rows for `h-4-1`, `h-4-3`, `h-4-4`; audit `webhook-received` (3), `alert-blackout` (1) | divergence (Y10) |
| 5 | four items; `h-5-2` has label `environment 'Bogus'` | 403 with the reject message; rows for `h-5-1` only; audit `alert-rejected` (1) | 403, same envelope plus `results`: processed, rejected, processed, processed; rows for `h-5-1`, `h-5-3`, `h-5-4` | divergence (Y10) |
| 6 | three items; `h-6-2` has label `event 'Heartbeat'` | 202 with the heartbeat message and the **heartbeat's** id; one heartbeat record; rows for `h-6-1` only | 202, same members plus `results`; `h-6-2` has outcome `heartbeat`, its `inputId` is the mapped alert's id and its `resourceId` the heartbeat's id; rows for `h-6-1` and `h-6-3`; one heartbeat | divergence (Y10) |
| 7 | three items; the middle item lacks `labels` | 400 for the whole request; nothing processed; no rows | 400 `Invalid alert at index 1 in webhook payload.` plus `results`: processed, invalid, processed; rows for `h-7-1` and `h-7-3` | divergence (Y10) |
| 8 | payload `{"foo": 1}` (no `alerts`) | 400 `no alerts in Prometheus notification payload`; no rows | same; no rows | parity |
| 8b | `{"alerts": []}` | 201 `{status, ids: []}` | same | parity |
| 8c | `{"alerts": {}}`; separately `{"alerts": ""}` | 201 `{status, ids: []}` each | 400 `no alerts in Prometheus notification payload` each; no rows | divergence |
| 8d | `{"alerts": null}`; separately `{"alerts": 5}` | 500 `An internal error has occurred!` each | 400 `no alerts in Prometheus notification payload` each; no rows | divergence |
| 8d2 | `{"alerts": "ab"}`; separately `{"alerts": {"x": 1}}` | 400 `'str' object has no attribute 'get'` each; no rows | 400 `no alerts in Prometheus notification payload` each; no rows | parity of status; divergence of message |
| 8e | one item lacking `labels` | 400 with the exception text (`'labels'`); no rows | 400 `Invalid alert at index 0 in webhook payload.`, no `results`; no rows | divergence (message only) |
| 8f | one item with `labels.timeout = []` | 500 `An internal error has occurred!` (`TypeError` at `prometheus.py:60`, legacy retry); no rows | 400 `Invalid alert at index 0 in webhook payload.`, no `results`; no rows | divergence |
| 9 | three items, no blackout | 201 `{status, ids}`; three rows; audit `webhook-received` (3) | same; no `results` | parity |
| 9b | three items, `PLUGINS_RAISE_ON_ERROR = True`; a test-only post-receive plugin named `synthetic-fail` raises `RuntimeError('synthetic failure')` for item 2 only, after its store step (a reviewed fault-injection fixture is required; NOT_RUN until one exists). An `AlertaException` would instead be re-raised (`utils/api.py:77-78`) and is not this vector | 500 `Error while running post-receive plugin 'synthetic-fail': synthetic failure` (`utils/api.py:79-81`; `custom.py:72-73`); rows for `h-9b-1` and `h-9b-2`; `h-9b-3` never processed; audit `webhook-received` (1) | 500, same message plus `results`: processed, error (`resourceId` the stored `h-9b-2` alert), processed; rows for all three; audit `webhook-received` (2) | divergence (Y10) |
| 10 | one item, no blackout; separately, one item blacked out | 201 `{status, id, alert}`; separately, 202 with the blackout message | same, no `results` | parity |
| 11 | annotation `runbook 'https://wiki.test/{instance}/{alertname}'` | `attributes.runbook` `https://wiki.test/h-11-1/A-11-1` | same | parity |
| 12 | annotation `note '{{literal}} {alertname}'` | `{literal} A-12-1` | same | parity |
| 13 | annotation `note` set to `'{missing} {alertname}'`; separately `'{0}'`; separately `'{alertname'` | unchanged each time | same | parity |
| 14 | annotation `note 'x {alertname:>20} {instance}'` | `x`, then `A-14-1` right-aligned in 20 characters, then ` h-14-1` | `x {alertname:>20} h-14-1` | divergence (Y12) |
| 15 | annotation `note '{alertname.__class__}'` | `<class 'str'>` | `{alertname.__class__}` | divergence (Y12) |
| 16 | label `big` of 1,000 `a` characters; annotation `note` of 66 `{big}` groups | 66,000 characters | the original 330-character value (66,000 bytes exceed the cap) | divergence (Y12) |
| 17 | label `big` of 1,024 `a` characters; annotation `note` of 64 `{big}` groups (65,536 bytes); separately, the same plus one trailing `x` (65,537 bytes) | expanded both times | expanded; separately, the original value | parity; divergence (Y12) |
| 18 | label `u` of 30,000 `ü` (2 bytes each); annotation `note '{u}{u}'` (60,000 characters, 120,000 bytes) | expanded | the original value (bytes, not characters, are counted) | divergence (Y12) |
| 19 | label `n` the number `5`; annotation `note 'v={n}'`; separately, annotation `note` the number `7` | `v=5`; separately `7`, kept | same | parity |
| 19b | label `x` the list `[1]`, annotation `note '{x}'`; separately, label `x` the object `{"k": "v"}` | `[1]`; separately `{'k': 'v'}` | `{x}` both times | divergence (Y12) |
| 20 | `generatorURL 'http://prom.test/graph?g0.expr=up'` | `attributes.moreInfo` is the HTML anchor | `attributes.moreInfo` is `http://prom.test/graph?g0.expr=up` | divergence (Y11) |

Sentinels are synthetic. Captures stay private to the run. The largest
payload (row 18) is about 60 KB; no stress run is implied. These vectors
authorize no fault injection and no execution.

## SF-WEBUI: no raw HTML in the web UI

Status: Proposed. Covers WUI-SEC-1. Owner decision Y11 (2026-10-01). This is
a change to alrust's vendored web UI (ADR-0004) and is tested with the UI's
own component tests, not the HTTP conformance suite.

**Reference.** The pinned web UI (`2687e4af`) renders nine fields as raw
HTML with `v-html` and no sanitizer:
- alert `value`, `text` and custom attribute columns
  (`AlertList.vue:153`, `:162`, `:180`);
- history text (`AlertDetail.vue:582`);
- string attributes containing `http://` or `https://`
  (`AlertDetail.vue:728-730`);
- group member name and email, and name and login
  (`GroupList.vue:53-54`, `:84-85`).

Anyone who can write these fields controls HTML, including script, in
operators' browsers.

**alrust.** None of the nine places renders HTML. Each one shows its field
as text, through one shared rendering function:
- The text is escaped, so any HTML is shown literally.
- Every substring matching `https?://[^\s<>"']+`, minus any trailing `.`,
  `,`, `;`, `:`, `!`, `?` or `)`, becomes an anchor whose visible text is
  that substring, with `href` the same substring, `target="_blank"` and
  `rel="noopener noreferrer"`.
- No other scheme is linked.
- Non-string values are shown as their text form.

The renderer works on the original string. It splits the string into text
and URL tokens and builds DOM nodes: text nodes for text, and anchors whose
`href` and text are bound as attributes and text. It never builds an HTML
string. So `&` and quotes are neither interpreted nor escaped twice. No other
UI behavior changes.

**Vectors** (vendored UI component tests):

| # | Field content | Rendered |
|---|---|---|
| 1 | `<img src=x onerror=alert(1)>` in an alert's `text` | the literal characters; no `img` element; no handler runs |
| 2 | `see https://wiki.test/x.` in `value` | `see `, an anchor to `https://wiki.test/x` (the period stays text) with `target="_blank"` and `rel="noopener noreferrer"`, then `.` |
| 3 | `javascript:alert(1)` in a custom attribute | the literal text, no anchor |
| 4 | `<b>bold</b>` in history text | the literal characters |
| 5 | a group member email `a@b.test<script>x</script>` | the literal characters; no `script` element |
| 6 | an attribute value `http://prom.test/graph` (the alrust `moreInfo`) | one anchor to that URL |
| 7 | `a & b "q" 'x' <i>` in `text` | exactly those characters, once escaped by the DOM, never `&amp;amp;` |
| 8 | `https://a.test/?x=1&y=2` in `value` | one anchor whose `href` is exactly `https://a.test/?x=1&y=2` |
| 9 | `(see https://a.test/p), then stop!` in history text | `(see `, an anchor to `https://a.test/p`, then `), then stop!` |
| 10 | the number `5` in a custom attribute; `true`; `null` | `5`; `true`; the empty text |
| 11 | vector 1's content in each of the nine sites (alert value, text, a custom attribute column, history text, group member name, email, name and login); at the string-attribute site, `see https://x.test/ <img src=x onerror=alert(1)>`, which contains an http(s) substring and so reaches the former raw-HTML branch (`AlertDetail.vue:728-739`) | no element is created from the content at any site; at the string-attribute site, one anchor to `https://x.test/` and the rest as literal text |

## SF-PROVIDERS: provider webhook fixes

Status: Proposed. Covers the provider-webhook findings recorded in
`docs/STATUS.md` after the Y3 list (WG-SEC-1, WT-SEC-1, WS-AUTH-1, the
regex-prefix lookup, the Slack and Telegram behavior defects, PagerDuty
partial application, and the remaining stored HTML), under owner decisions
Y3, Y10, Y11 and Y13 (2026-10-01). The reference behavior of each provider
is in `spec/openapi.yaml` (`/webhooks/<provider>`). SF-WEBHOOKS (logging,
batches, placeholders) and SF-WEBUI (the shared plain-text renderer) still
apply.

**alrust.**
1. **Plain text instead of stored HTML (Y11).** Text the server builds
   holds no markup:
   - Grafana: `attributes.ruleUrl` and `attributes.imageUrl` are the plain
     `ruleUrl` and `imageUrl` values;
   - CloudWatch `SubscriptionConfirmation`: `text` is `<Message>
     <SubscribeURL>` (one space between them);
   - Stackdriver: `attributes.moreInfo` is the plain `url`;
   - PagerDuty: the history text uses `#<incident_number> (<html_url>)`
     where the reference has the anchor. For example: `Incident #42
     (https://pd.test/i/42) assigned to Ana`.
   These are the provider mappings. The SF-WEBUI renderer separately makes
   the URLs into links when it displays them.
2. **Normal scopes for webhook actions (Y13).** The outer `write:webhooks`
   admission and its credential precedence are unchanged. The added check
   uses the same effective context as that admission:
   - **Authenticated requests.** When the request was authenticated by any
     credential branch (API key, Bearer token, Hawk, auth proxy or Basic),
     with `AUTH_REQUIRED` true or false, its effective scopes must also
     satisfy the conditions below. Hawk's `ADMIN_SCOPES` satisfy them.
     Conditions:
     - `in_scope('write:alerts')` for Slack and Telegram `open`, `ack` and
       `close`, and for PagerDuty (once per request, before any message);
     - `in_scope('write:blackouts')` for Telegram `blackout`.
   - **Anonymous, authentication disabled.** A request that reached the
     credential-free fallback with `AUTH_REQUIRED` false
     (`auth/decorators.py:155-161`) gets no added check, as for every other
     scope.
   - **Anonymous read-only.** With `AUTH_REQUIRED` and `ALLOW_READONLY`
     true, an anonymous request admitted under SF-AN1 has
     `READONLY_SCOPES` as its effective scopes, and the same conditions
     apply to them. If `READONLY_SCOPES` lacks `write:webhooks`, the outer
     check already refuses. If it has `write:webhooks` but not the action's
     scope, the action is refused with SF-AN1's 401 `Missing authorization
     API Key or Bearer Token`. If it covers both (for example `['write']`),
     the action proceeds.
   - **Refusal.** For a credential branch: 403 `Missing required scope:
     write:alerts` or `…: write:blackouts`. For the anonymous read-only
     context: the 401 above. Both use the common error envelope and are
     raised outside the parsing-stage translation (`custom.py:38-39`), so
     their codes stay. Both come before any change, reply or action audit
     event.
   - `watch`, `unwatch` and unknown actions need only `write:webhooks`.
3. **Grafana `ok` (WG-SEC-1).** The `ruleId` lookup is limited to the
   caller's customers, exactly as alert lists are (no limit for an
   unscoped caller). The found alerts are not processed inside the parsing
   stage. They are returned to the dispatcher, which processes each one
   once (severity the alarm model's normal severity, status `closed`),
   with the SF-WEBHOOKS batch rules (Y10). The reference processes them
   twice; alrust does not, so counts that the second pass would change
   (such as `duplicateCount`) differ.
4. **Alert lookup by id (PagerDuty, Slack, Telegram).**
   - The key matches `id` or `last_receive_id` as a **literal**,
     case-insensitive prefix. No regular-expression or SQL wildcard
     character (`.`, `%`, `_`, …) has a special meaning.
   - The caller's customer filter is applied first. Then distinct alerts
     are counted, so an alert matching on both `id` and `last_receive_id`
     counts once.
   - Exactly one match is the alert. Zero or several is the provider's
     not-found result (items 5-7).
5. **Slack.** In order:
   1. parse;
   2. an action outside `open`, `ack`, `close`, `watch`, `unwatch` gives
      400 `Unsupported #slack action`, with no lookup;
   3. the scope check (item 2);
   4. the lookup. Not found gives 200 `{status: error, message: alert not
      found for #slack message}`, which the reference builds but does not
      return, and no change.
   5. Then the action: `watch` adds the tag `watch:<user name>`; `unwatch`
      removes it. This is the web UI's watch tag (`App.vue:782`,
      `AlertList.vue:652`). The reference instead untags `watch:<user>`
      and `unwatch:<user>` respectively, so `watch` never adds a tag.
   6. The reference's audit event and 201 response follow.
6. **Telegram.** In order:
   1. parse;
   2. the scope check for the action (item 2);
   3. the lookup. Not found gives 200 `{status: error, message: alert not
      found for Telegram message}` for **every** action, `blackout`
      included, with no reply, no action audit event and no change. The
      dispatcher's `webhook-received` audit event still occurs, as for any
      provider response.
   4. Then the action:
      - `open`, `ack`, `close` as in the reference;
      - `watch` and `unwatch` as for Slack, with `<first_name> <last_name>`;
      - `blackout` creates a blackout for the alert's environment,
        resource and event, whose customer is
        `assign_customer(wanted=None, permission='admin:blackouts')`, the
        rule of `POST /blackout` (`views/blackouts.py:32`). So a caller
        with one customer gets that customer and an unscoped caller none.
        A caller with several customers gets 400 `must define customer as
        more than one possibility`, with no blackout, no reply and no
        action audit event;
      - an unknown action changes nothing.
   5. For every found alert, the reference's reply attempt, `webhook-updated`
      audit event and 200 `{status: ok}` follow, unknown actions included,
      as in the reference (`telegram.py:97-104`).
   The reply transport must be a reviewed synthetic sink, or disabled, in
   any test run.
7. **PagerDuty messages (Y10).**
   - The scope check (item 2) runs once, before any message. A refusal is
     403 with no `results` and no change.
   - Every message is then processed in order, each independently, and
     none stops the others. Applied changes stay; nothing is rolled back.
   - `results` holds one entry per message, in order:
     `{index, incidentKey, alertId, outcome, code, message}`:
     - `index` counts from 0;
     - `incidentKey` is the key when parsing succeeded and the key is a
       non-empty string, else null. The checks per message run in this
       order: parse (any failure is `invalid`); a falsy key (`invalid`, the
       reference's text); a key that is not a string, such as `42` or
       `true` (`invalid`); then the lookup. The reference instead passes a
       truthy non-string key on to its lookup, which fails on `'^' + id`
       (`postgres/base.py:299`) inside the caught lookup branch, so a
       400;
     - `alertId` is the matched alert's id, or null.
   - The `outcome` values:

     | `outcome` | When | `code` | `message` |
     |---|---|---|---|
     | `applied` | the status update returned a truthy result | 200 | null |
     | `invalid` | parsing failed | 400 | `Invalid PagerDuty message at index <index>.` |
     | `invalid` | the key is empty or falsy | 400 | `no incident key in PagerDuty data payload` (the reference's text) |
     | `invalid` | the key is truthy but not a string | 400 | `Invalid PagerDuty message at index <index>.` |
     | `not-found` | zero or several matches | 400 | `not found` |
     | `failed` | the lookup raised (for example a database error); `alertId` null | 400 | `update PagerDuty incident status failed` |
     | `failed` | the update raised, or returned a falsy result | 400 | `update PagerDuty incident status failed` |

     A truthy update to the same status is `applied`. The reference's 404
     and 500 inside the handler reach the client as 400, and alrust keeps
     that 400.
   - **Response.** 200 `{status: ok}` when every message is `applied`.
     Otherwise the common error envelope with the first non-applied
     message's code and message, plus `results`. An empty `messages` list
     is 400 `update PagerDuty incident status failed`, as in the reference,
     with `results: []`.

**Fixtures.** A fresh store per vector. Settings:
- `AUTH_REQUIRED = True` and `CUSTOMER_VIEWS = True`, with `ADMIN_USERS`
  set so the startup check passes (`utils/config.py:117-121`), unless a
  row says otherwise;
- `ALARM_MODEL` ALERTA and the default `PLUGINS`.

Customers `A` and `B`. Synthetic API keys (one customer each, or none):
- `KW` (`write:webhooks`, customer `A`);
- `KWA` (`write:webhooks`, `write:alerts`, customer `A`);
- `KWB` (`write:webhooks`, `write:blackouts`, customer `A`);
- `KWB0` (`write:webhooks`, `write:blackouts`, no customer).

`TAB` is a Bearer token with scopes `write:webhooks` and `write:blackouts`
and customers `[A, B]`, minted as in SF-MGMT.

Seeded alerts, each with environment `Production`, severity `major`, status
`open`, no tags, and `last_receive_id` equal to its id:

| Alert | Id | Customer | Resource / event | `ruleId` |
|---|---|---|---|---|
| `a1` | `11aa0000-0000-4000-8000-000000000001` | `A` | `web` / `cpu` | `7` |
| `a2` | `11ab0000-0000-4000-8000-000000000002` | `A` | `db` / `disk` | none |
| `b1` | `11aa9999-0000-4000-8000-000000000003` | `B` | `web` / `cpu` | `7` |

Payload shapes are those of `spec/openapi.yaml`. Reads are made by an admin
observer. The Telegram reply goes to a synthetic sink.

**Vectors.** Reference cells are source-derived. Before execution they are
NOT_RUN, and their known predicates are asserted on the first authorized
run. Only cells marked **reference pending** skip, and only that predicate.
alrust cells are NOT_RUN until an implementation exists.

| # | Case | Reference | alrust | Kind |
|---|---|---|---|---|
| 1 | Grafana `alerting` with `ruleUrl` and `imageUrl`, key `KWA` | both attributes are HTML anchors | both are the plain URLs | divergence (Y11) |
| 2 | CloudWatch `SubscriptionConfirmation` (text body), key `KWA` | `text` holds an HTML anchor | `text` is `<Message> <SubscribeURL>` | divergence (Y11) |
| 3 | Stackdriver incident with `url`, key `KWA` | `moreInfo` is an HTML anchor | `moreInfo` is the plain `url` | divergence (Y11) |
| 4 | PagerDuty `incident.trigger` for `a1` (full id), key `KWA` | 200; history text holds the anchor | 200; history text `Incident #<n> (<html_url>) assigned to <user>` | divergence (Y11) |
| 5 | Slack `close` for `a1`, key `KW` | 201; `a1` closed | 403 `Missing required scope: write:alerts`; `a1` unchanged; no action audit event | divergence (Y13) |
| 6 | the same with key `KWA` | 201; `a1` closed | same | parity |
| 7 | `AUTH_REQUIRED = False`, `CUSTOMER_VIEWS = False`: Slack `close` for `a1` with key `KW`; separately with `KWA`; separately with no credential | 201 and `a1` closed, all three | 403 and no change; 201 and closed; 201 and closed (anonymous fallback) | divergence (Y13, key `KW`); parity; parity |
| 8 | Telegram `/ack <a1>`, key `KW`; separately with `KWA` | 200 and `a1` acknowledged, both | 403 and no change; 200 and acknowledged | divergence (Y13); parity |
| 9 | Telegram `/blackout <a1>`, key `KW` | 200; a blackout with no customer | 403 `Missing required scope: write:blackouts`; no blackout, reply or action audit event | divergence (Y13) |
| 10 | the same with `KWB` | 200; a blackout with no customer | 200; a blackout for `Production`/`web`/`cpu` with customer `A` | divergence (customer) |
| 11 | the same with `KWB0` (unscoped) | 200; no customer | 200; blackout with no customer | parity |
| 12 | Telegram `/blackout <a1>` with the Bearer token `TAB` (customers `A` and `B`) | 200; a blackout with no customer | 400 `must define customer as more than one possibility`; no blackout, reply or action audit event | divergence |
| 13 | `AUTH_REQUIRED = False`, `CUSTOMER_VIEWS = False`: Telegram `/blackout <a1>` with `KW`; separately with no credential | 200 and a blackout, both | 403 and no blackout; 200 and a blackout with no customer | divergence (Y13, key); parity |
| 13b | `AUTH_REQUIRED = True`, `ALLOW_READONLY = True`, `CUSTOMER_VIEWS = False`, no credential: Slack `close` for `a1` and Telegram `/blackout <a1>`, each with `READONLY_SCOPES` `['read']`; `['write:webhooks']`; `['write']`; `['write:webhooks', 'write:alerts']` | every action dispatched (SF-AN1's defect) | `['read']`: both 401 (outer check). `['write:webhooks']`: both 401 (action scope). `['write']`: close 201, blackout 200 with no customer. `['write:webhooks', 'write:alerts']`: close 201, blackout 401 | divergence (SF-AN1, Y13) except `['write']` (parity) |
| 14 | PagerDuty one message for `a1`, key `KW` | 200; `a1` updated | 403, no `results`; no change | divergence (Y13) |
| 15 | Grafana `ok` with `ruleId 7`, key `KWA` | first pass closes `a1` **and `b1`** (no customer filter); the dispatcher's second pass and the final response are reference pending | `a1` closed, processed once; `b1` unchanged | divergence (WG-SEC-1) |
| 16 | Slack `ack` with `callback_id` `11AA0000` (upper case), key `KWA` | `a1` acknowledged | same | parity (case-insensitive prefix) |
| 17 | Slack `ack` with `callback_id` `11a`, key `KWA` (matches `a1` and `a2` within `A`) | whichever the query returns first is acknowledged | 200 not-found JSON; no change | divergence (ambiguous) |
| 18 | Slack `ack` with `callback_id` `11aa`, key `KWA` (matches `a1`; `b1` is excluded by the customer filter) | `a1` acknowledged | same | parity (filter before counting) |
| 19 | Slack `ack` with `callback_id` `.`; separately `%`; separately `_`, key `KWA` | `.` acknowledges the first alert of `A` the query returns; `%` and `_` match nothing (literal in a regex), so 400 `'NoneType' object has no attribute 'from_action'` | 200 not-found JSON each; no change | divergence (`.`, and the not-found response) |
| 20 | Slack `ack` with the full `a1` id (matching `id` and `last_receive_id`), key `KWA` | `a1` acknowledged | same (one distinct match) | parity |
| 21 | Slack `ack` for an id matching nothing, key `KWA` | 400 `'NoneType' object has no attribute 'from_action'` | 200 not-found JSON | divergence |
| 22 | Slack `bogus` action for a missing id, key `KW` | 400 `Unsupported #slack action` | same; no lookup | parity |
| 23 | Slack `watch` for `a1` by `ana`, key `KW`; then `unwatch` | `a1` tags unchanged both times | `a1` gets `watch:ana`, then loses it | divergence |
| 24 | Telegram `/bogus <a1>`, key `KW` | `a1` unchanged; reply attempt; `webhook-updated` and `webhook-received` events; 200 `{status: ok}` | same | parity |
| 25 | Telegram `/bogus <missing>`, key `KW` | 200 `{status: error, message: alert not found for Telegram message}` | same | parity |
| 26 | Telegram `/blackout <missing>`, key `KWB` | 400 with Python's `UnboundLocalError` text | 200 Telegram not-found JSON; no blackout | divergence |
| 27 | PagerDuty three messages: `a1` trigger, key `zzzz-no-such-alert`, `a1` acknowledge; key `KWA` | 400 `not found` at message 2; message 1 applied; message 3 not applied | 400 `not found` with `results`: applied (`incidentKey` `11aa…0001`, `alertId` `a1`), not-found (`zzzz-no-such-alert`, null), applied; `a1` ends acknowledged | divergence (Y10) |
| 28 | PagerDuty one message without `incident_key`; separately with `incident_key ''` | 400 with the exception text (`'incident_key'`); separately 400 `no incident key in PagerDuty data payload` | 400 `Invalid PagerDuty message at index 0.`, `results` (invalid, `incidentKey` null); separately 400 `no incident key in PagerDuty data payload`, `results` (invalid, null) | divergence (message, `results`); divergence (`results`) |
| 28b | PagerDuty one message with `incident_key 42` (a number), key `KWA` | 400 from the caught lookup failure (`'^' + 42` raises) | 400 `Invalid PagerDuty message at index 0.` with `results` (invalid, `incidentKey` null); no lookup | divergence (message, `results`) |
| 28c | PagerDuty two messages, both for `a1`, where the lookup for message 1 raises (needs a reviewed fault-injection fixture; NOT_RUN until one exists) | 400 at message 1; message 2 not applied | 400 `update PagerDuty incident status failed` with `results`: failed (`alertId` null), applied | divergence (Y10) |
| 29 | PagerDuty two valid messages for `a1`, key `KWA` | 200 `{status: ok}` | same; no `results` | parity |
| 30 | PagerDuty three messages where message 2's update returns a falsy result and message 3 applies (needs a reviewed fault-injection fixture; NOT_RUN until one exists) | 200 (only the last result decides) | 400 `update PagerDuty incident status failed` with `results`: applied, failed, applied | divergence (Y10) |

These vectors authorize no execution. Payloads and credentials are
synthetic.

## Later findings

Every Y3 item now has an entry above. A defect found later gets its own
entry here before alrust relies on its fix; until then its reference
behavior in `spec/openapi.yaml` stands as the description of the defect, not
as a requirement for alrust.
