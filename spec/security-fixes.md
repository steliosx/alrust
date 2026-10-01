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

## Not yet specified

The other Y3 items get entries in this file before their phases need them:
the string-formatted SQL constructions; M-SEC-1; WUI-SEC-1 and WP-SEC-1; W-SEC-1 and W-DL-1; the API-key
items; C21–C23; and the `q=` search. Until an item has an entry here, its
reference behavior in `spec/openapi.yaml` stands as the description of the
defect, not as a requirement for alrust.
