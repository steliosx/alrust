# Security fixes: where alrust deliberately differs from the reference

Owner decision Y3 (2026-10-01, `docs/DECISIONS.md`): alrust **fixes** the
security and behavior defects found in the pinned reference
(`reference/alerta` v9.1.0, `f4312727`) instead of reproducing them. This
file specifies each fix. For every entry:

- **Reference** states what the pinned source does, with line citations
  (`reference/alerta/alerta/<path>:<line>`). These are source-derived; none of
  the defects was exploited or executed.
- **alrust** states the required behavior. It is normative for Phases 2 and 3.
- **Conformance** states how the suite and the differential fuzzer treat the
  difference: each entry is an intended divergence, so a vector that exercises
  it must expect the alrust behavior from alrust and must not report the
  reference's behavior as a regression.

Entries keep their identifiers from `docs/STATUS.md` and `spec/openapi.yaml`.
The status of each entry is **Proposed** until Codex approves it at an exact
commit.

## SF-AN1: read-only anonymous access must still enforce scopes

Status: Proposed.

**Reference.** With `AUTH_REQUIRED` and `ALLOW_READONLY` both true
(`settings.py:78-79`; defaults `False` and `['read']`), a request that
carries no credential reaches precedence branch 4 of `permission()`
(`auth/decorators.py:163-169`). The branch sets `g.scopes` to
`READONLY_SCOPES` and calls the view **without checking the route's
required scope**. Every authenticated branch does check it
(`:51-52`, `:88-89`, `:119-120`, `:150-151`). So an anonymous caller reaches
write and delete routes such as `POST /alert`, `DELETE /alert/{id}`,
`PUT /user/{id}` and `DELETE /key/{key}`.

**alrust.** In that configuration, an anonymous request is admitted only if
the route's required scope is satisfied by `READONLY_SCOPES`, using the same
scope check as the authenticated branches. Otherwise alrust responds exactly
as the reference does when no branch admits a request: HTTP 401 with
`Missing authorization API Key or Bearer Token` (`auth/decorators.py:175`).
Admitted anonymous requests keep the reference's context: no user id, no
login, no customers, and `READONLY_SCOPES` as scopes. The other branches
and `AUTH_REQUIRED` false are unchanged.

**Conformance.** The intended divergence is only in the configuration
`AUTH_REQUIRED=True, ALLOW_READONLY=True`, for an anonymous request to a
route whose scope `READONLY_SCOPES` does not satisfy. The reference
dispatches; alrust returns 401 and must cause no side effect. Vectors for
read routes in that configuration must match the reference.

## SF-CRON: no unauthenticated App Engine cron branch

Status: Proposed.

**Reference.** With `AUTH_REQUIRED` true, `ALLOW_READONLY` false and no
credential, a request whose `X-Appengine-Cron` header is non-empty and whose
`X-Forwarded-For` is exactly `0.1.0.1` is dispatched **with no scope check
and no auth context set** (`auth/decorators.py:171-173`). Both headers are
ordinary client-controlled headers unless a proxy strips them.

**alrust.** No such branch exists. The request falls through to the 401
above. Scheduled housekeeping is triggered with an ordinary credential that
carries the required scope.

**Conformance.** An intended divergence in that configuration only: the
reference dispatches, alrust returns 401 and causes no side effect.

## SF-USERS: one role ceiling for every way of setting roles

Status: Proposed.

**Reference.**

1. `PUT /user/{user_id}` runs the role ceiling only when the body's `roles`
   member is truthy (`views/users.py:181-186`). The model also accepts a
   legacy singular `role` and turns it into `roles: [role]`
   (`models/user.py:180-181`), so `{"role": "admin"}` grants any role
   without the ceiling check. A holder of `admin:users` can therefore create
   full administrators under the default role mapping.
2. `POST /user` hashes a missing password as the empty string
   (`models/user.py:67`, `generate_password_hash(json.get('password', ''))`).
3. `POST /user` builds `ApiError(str(e), 500)` when the insert fails but never
   raises it (`views/users.py:45-48`), so a failed insert still returns 201
   with the unsaved user (`:57-58`).

**alrust.**

1. The effective new roles of `PUT /user/{user_id}` are `[role]` when the
   body has a `role` member, otherwise `roles` when present. If the body has
   both members, the request is rejected with 400 as ambiguous. Whenever the
   effective roles are present and non-empty, the same ceiling as `roles`
   applies: every scope those roles grant (`Permission.lookup`) must be within
   the caller's scopes, else 403 with the reference's message
   (`Requested scope '<scope>' not in existing scopes: <scopes>`).
   An empty list removes roles and needs no ceiling, as in the reference.
2. `POST /user` with the basic auth provider requires a non-empty `password`;
   a missing or empty password is rejected with 400 before anything is
   stored.
3. A failed user insert returns 500 and creates nothing. Nothing that
   follows the insert (confirmation e-mail, audit event, 201 response) runs.

**Conformance.** Intended divergences:
- `PUT /user/{id}` with `role` (alone) above the caller's scopes: reference
  200, alrust 403 with no change;
- `PUT /user/{id}` with both `role` and `roles`: reference applies `role`,
  alrust 400 with no change;
- `POST /user` without a password: reference 201 with an empty-password
  hash, alrust 400;
- a failing user insert: reference 201, alrust 500.

All other `PUT /user` and `POST /user` vectors must match the reference.

## Not yet specified

The other Y3 items get entries in this file before their phases need them:
the string-formatted SQL constructions; G-SEC-1, P-SEC-1, C-SEC-1, GR-R1 and
G-DEF-1; M-SEC-1; WUI-SEC-1 and WP-SEC-1; W-SEC-1 and W-DL-1; the API-key
items; C21–C23; and the `q=` search. Until an item has an entry here, its
reference behavior in `spec/openapi.yaml` stands as the description of the
defect, not as a requirement for alrust.
