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

**Vectors.**

| Case | Reference | alrust | Kind |
|---|---|---|---|
| `PUT` with an alert the caller can see and a note of another alert (same customer) | 200 | 404 `not found`, no change | divergence |
| same, the note's alert belongs to another customer | 200 | 404, no change | divergence |
| `DELETE` with a note of another alert | 200 | 404 `note not found`, nothing deleted | divergence |
| `PUT` or `DELETE` with the alert's own note | 200 | 200 | parity |
| an alert the caller cannot see | 404 | 404 | parity |

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
   **Secret-value check.** The secret values are every non-empty string held
   by a secret setting (each list element and map value, recursively) and
   every non-empty user-information string and password of a connection
   setting. They are taken from every configured source of that setting
   (environment variable, configuration file, and for `SECRET_KEY` the
   generated key), not only the effective one. A public value whose JSON
   rendering contains any secret value as a substring is shown as
   `[REDACTED]`. Empty strings are never secret values.
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
  `LDAP_BIND_PASSWORD` and `SMTP_PASSWORD`;
  `DATABASE_URL = postgres://u:<db-sentinel>@db.test:5432/alrust`;
  `CELERY_RESULT_BACKEND = redis://:<celery-sentinel>@cache.test:6379/0`;
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
| 2 | same; the connection lines | full URLs with passwords | `DATABASE_URL: "postgres://[REDACTED]@db.test:5432/[REDACTED]"`; `CELERY_RESULT_BACKEND: "redis://[REDACTED]@cache.test:6379/[REDACTED]"` | divergence (Y8) |
| 3 | same; `LDAP_CONFIG` (mixed-case nested secret name and nested URL) | the map, with both sentinels | `LDAP_CONFIG: [REDACTED]` | divergence (Y8) |
| 4 | `MP` with `DATABASE_URL = 'not a url'`; then `'postgres://'` | shown as given | `DATABASE_URL: [REDACTED]` both times | divergence (Y8) |
| 5 | `MP` with the public `DEFAULT_ENVIRONMENT` set to the `SMTP_PASSWORD` sentinel | shown | `DEFAULT_ENVIRONMENT: [REDACTED]` | divergence (Y8, secret-value check) |
| 6 | `MP` with a public string setting set to `"x\nSECRET_KEY: leaked"` | two lines | one line; the value is a JSON string with `\n` escaped | divergence (Y8, escaping) |
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

## Not yet specified

The other Y3 items get entries in this file before their phases need them:
the string-formatted SQL constructions; WUI-SEC-1 and WP-SEC-1; W-SEC-1 and W-DL-1;
and the `q=` search. Until an item has an entry here, its
reference behavior in `spec/openapi.yaml` stands as the description of the
defect, not as a requirement for alrust.
