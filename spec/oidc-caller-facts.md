# OIDC operator caller facts (design)

Status: **design for review; nothing here is implemented.** This page
designs the one place where a verified operator credential becomes the
`CallerFacts` that the integration hooks receive
(`crates/alrust-store-pg/src/hooks.rs`; `spec/integration-hooks.md`,
sections 2 and 4.1.1). It works inside settled decisions only: KICKOFF's
auth target (API keys for sources, OIDC for operators, no customer views,
no per-customer RBAC), DECISIONS Y9 and `spec/security-fixes.md` SF-MGMT
(the token signing key and HS256 Bearer tokens), Y28 (auth-route spec depth
only) and Y44 A1 and A2 (what the hooks capture and where operator
customers come from). It adopts no new auth contract. Every point that
would change what a caller can observe is listed in section 7 as an open
question, with the reference behavior and a recommendation, for Codex's
review and then the owner. It reads no provider, configuration, key or
credential; the examples use synthetic values.

Citations: `reference/alerta/<path>:<line>` at v9.1.0 for the server,
`reference/alerta-webui/<path>:<line>` for the web UI, and alrust paths at
`main` `41a465b4` (`crates/alrust-api/src/lib.rs`, blob `d154febd`).

## 1. Where this sits

The reference has two stages, and so does this design:

- **Login (L).** The web UI sends an authorization code to
  `POST /auth/openid` (or an alias). The server exchanges it at the
  provider, reads the ID token and userinfo, and returns **its own** HS256
  token, signed with `SECRET_KEY`, carrying `sub`, `preferred_username`,
  `provider`, `scope` and, with `CUSTOMER_VIEWS`, `customers`
  (`alerta/auth/oidc.py:61-206`; `alerta/auth/utils.py:52-73`;
  `alerta/models/token.py:78-115`).
- **Request (R).** Every later request carries `Authorization: Bearer
  <token>`. The server checks it with `SECRET_KEY` and the audience and sets
  `g.user_id`, `g.login`, `g.customers` and `g.scopes` from its claims
  (`alerta/auth/decorators.py:69-91`; `alerta/models/token.py:42-76`).

alrust already specifies R's verification: SF-MGMT item 3 keeps HS256
Bearer tokens signed with the effective key, the `scope` claim as the
caller's scopes, the reference audience and expiry rules, and the three 401
messages; Y9 fixes how the key is chosen. **This page designs R's output:
the conversion from a verified token to alrust's `Caller` and
`CallerFacts`.** L, the provider exchange that issues the token, is Phase 3
work with its own design and review; section 6 lists only what L must
guarantee for R's facts to be true.

## 2. Today

alrust authenticates API keys only (`authenticate`, `lib.rs:229-261`). A key
comes from `Authorization: Key`, else `X-API-Key`, else `?api-key=`
(`key_from_request`, `:214-227`). An `Authorization: Bearer` header is not
read: with `AUTH_REQUIRED` it gives 401 `Missing authorization API Key or
Bearer Token`, and without it the caller is the auth-off caller.
`Caller::facts` (`:164-177`) sets `user_id` and `auth_type` to `None` for
every caller, so the actor slice (`hooks::zabalack::human_actor`, hook spec
4.1.1) finds no human actor and every acknowledgement is
`skipped_actor_policy`.

`Caller.customer` has two uses: it filters reads and writes globally
(`get_alert`, `list_heartbeats`, `list_keys` and others take
`caller.customers()`), and `assign_customer` (C5-C11) uses it for submitted
objects. `facts()` passes the same value to the hooks.

## 3. The trusted boundary

One function turns a token into claims, and only its result can produce
human facts:

```text
verify_bearer(token, key, audience, now) -> Result<VerifiedToken, BearerError>
```

- `VerifiedToken` has private fields and no public constructor; tests build
  one only through `verify_bearer` with a synthetic key. No request body,
  query parameter or other header can add to it or override it: the only
  input is the `Bearer` header's token.
- `verify_bearer` accepts **HS256 only**, from configuration, never from the
  token's `alg` header (a token whose header names another algorithm,
  including `none`, is invalid). It checks the signature with the effective
  `SECRET_KEY` (Y9), the audience (`OAUTH2_CLIENT_ID`, else
  `SAML2_ENTITY_ID`, else the absolute base URL, as `models/token.py:50`),
  and `exp`, `nbf` and `iat` against the injected `now`, with no leeway
  (O4). Whether `exp` must be present is O3.
- Its errors are the three SF-MGMT 401s: `Token is invalid`, `Token has
  expired`, `Invalid audience` (`decorators.py:77-82`). In the reference,
  only PyJWT's `DecodeError` (which includes a bad signature),
  `ExpiredSignatureError` and `InvalidAudienceError` are caught there; other
  failures escape as a generic 500. With the reference's PyJWT 2.12.1
  these include a disallowed `alg` (`InvalidAlgorithmError`,
  `jwt/api_jws.py:345-357`), a missing or empty `aud`
  (`MissingRequiredClaimError`, `jwt/api_jwt.py:524-527`), an `nbf` or
  `iat` in the future (`ImmatureSignatureError`, `:478`, `:492`) and a
  non-string `sub` (`InvalidSubjectError`, `:444-445`). A token with no
  `exp` is accepted, since PyJWT checks `exp` only when present and the
  reference requires no claims (`:394`, `:404-405`). What alrust returns for
  these is O3.
- The key is passed in; the function reads no file, environment or clock.

Then two conversions read `VerifiedToken`, and nothing else does:

```text
Caller::from_bearer(&VerifiedToken)       -> Caller          (global semantics)
OperatorFacts::from_token(&VerifiedToken, provider) -> Option<OperatorFacts>  (hooks only)
```

`Caller` gains one field, `operator: Option<OperatorFacts>`. It is `None`
for every API-key and auth-off caller, so their `Caller` and `CallerFacts`
stay byte-for-byte what they are today. `facts()` reads `operator`; no
other code does.

## 4. Global semantics (`Caller::from_bearer`)

These are the reference's, for a deployment without customer views, which
is alrust's only kind:

| `Caller` field | From | Reference |
|---|---|---|
| `login` | `preferred_username` if a non-empty string, else `None` | `g.login = jwt.preferred_username` (`decorators.py:84`) |
| `scopes` | `scope` split on single spaces, empty items dropped | `models/token.py:71` |
| `customer` | **always `None`** | with `CUSTOMER_VIEWS` off the token has no `customers` (`token.py:111-112`) and `g.customers` is empty, so no filter applies |
| `auth_off` | `false` | the route's scope check runs (`decorators.py:88-91`) |

So a Bearer operator sees and writes like an unfiltered caller with the
token's scopes, and `assign_customer` treats them as a caller with no
customer. **No customer list from the token ever reaches global filtering,
`assign_customer` or a route permission.** The reference's
`g.customers = jwt.customers` applies the token's customers globally when
`CUSTOMER_VIEWS` is on; alrust has no customer views (KICKOFF; Y44 A2) and
does not reproduce that.

Dropping the empty scope item differs from the reference only in a case no
route can observe: `''.split(' ')` gives `['']`, which matches no scope.

## 5. Hook facts (`OperatorFacts::from_token`)

`OperatorFacts` is `{ user_id, login, auth_type, customers }`. It is all or
nothing: `from_token` returns `Some` only when **every** check below
passes, and otherwise `None`, in which case `facts()` gives exactly today's
API-key-shaped facts (`user_id` and `auth_type` `None`; `customers` from
`Caller.customer`, which is `None`). A failed check never refuses the
request; it only means no human actor, so the actor slice skips with
`skipped_actor_policy`, as today.

1. **User id (A1):** `sub`, a non-empty string. The reference's `oid`
   claim (`decorators.py:83`, set when an account was linked by e-mail,
   AUTH-SEC-6) is not read: A1 makes the subject the user id.
2. **Login (A1):** `preferred_username`, a non-empty string. A1's "preferred
   username or e-mail" is applied at issuance (section 6, L3); at request
   time the token's `preferred_username` is the login, as in the
   reference. The reference's `nickname` fallback is not part of A1.
3. **Auth type (A1):** `bearer:<provider>`, where `provider` is a non-empty
   string equal to the configured OIDC provider name. Any other provider
   value (absent, `basic`, `ldap`, `saml2`, another provider) gives `None`:
   KICKOFF has no basic or LDAP operators, and A1 set them aside.
4. **Customers (A2):** the hook-customers claim (open question O2 names
   it). It must be an array of non-empty strings; the result is sorted and
   de-duplicated. An empty array is allowed and means no customers (the
   actor slice then authorises nobody except an allowed admin). A missing
   claim, `null`, a non-array or any non-string or empty item gives
   `None` for the whole `OperatorFacts`. `None` is never "every customer".

`facts()` then sets `user_id`, `login`, `auth_type` and `customers` from
`OperatorFacts`, and the rest as today: `scopes` from `Caller.scopes`,
`auth_required` from configuration, `request_id` `None`, `from_request`
`true`.

Unchanged downstream (hook spec 4.1.1): the ingestion-login exclusion, the
exact `admin` and `admin:alerts` scope test, `admin_exceptions`, the
cohort's `allow_admin`, and the actor-customer check. These facts carry no
admin meaning of their own: an operator is an admin only by the token's
scopes, exactly as for global route permissions.

## 6. What login (L) must guarantee

R trusts a token because alrust signed it. The facts in section 5 are only
as true as L's issuance, so L's own design must show, with evidence and
synthetic tests, at least:

- **L1, ID token verification.** Issuer equal to the configured issuer;
  signature checked against the issuer's published keys, with the
  algorithm from an allowlist in configuration, never from the token's
  header; audience equal to the client id; `exp` and `iat` checked; a
  `nonce` bound to the login if the flow allows it. Any failure refuses the
  login. The reference does none of this by default and takes the
  algorithm from the header in verify mode (AUTH-SEC-5, `oidc.py:124-142`);
  alrust does not reproduce it.
- **L2, subject.** `sub` is the verified provider subject. No account
  linking by e-mail (AUTH-SEC-6, `oidc.py:171-187`), so no `oid`.
- **L3, login.** `preferred_username`, else `email` (A1), copied into the
  token's `preferred_username`; neither present refuses the login, as the
  reference's 400 does without `nickname` (`oidc.py:167-169`).
- **L4, provider.** The token's `provider` is the configured provider name
  (`oidc.py:203`).
- **L5, customers (A2).** The provider's customers claim, or its groups
  mapped to customers by alrust configuration, written into the
  hook-customers claim (O2). A mapping that cannot be applied writes no
  claim, so the operator can log in but is never a hook actor.
- **L6, scopes.** How an operator's scopes are chosen (the reference's
  `Permission.lookup` over roles and `ADMIN_USERS`, AUTH-SEC-6 and
  AUTH-SEC-7) is L's design; R only reads the result.

## 7. Open questions

Each is for Codex's review first; any that changes observable auth
behavior then goes to the owner one at a time.

- **O1, precedence and fallback.** Reference order: an API key on any of
  its three carriers, then `Bearer`, then (absent both) the auth-off
  caller; Hawk, auth proxy and Basic are not part of KICKOFF's target and
  stay unread. So a request with both a key and a token is an API-key
  request, and an **invalid** token is a 401 even when `AUTH_REQUIRED` is
  false (`decorators.py:30-91`, `:155-161`). Today alrust ignores a Bearer
  header, so with `AUTH_REQUIRED` false an invalid token changes from the
  auth-off caller to 401. SF-MGMT item 3 says "keep the reference
  behavior"; whether that sentence already settles the order and the
  fallback, or whether they need the owner, is for Codex to judge.
  Recommendation: the reference order and the 401.
- **O2, the hook-customers claim.** The web UI's `customers` getter returns
  `['ALL (*)']` for an empty `customers` array and otherwise the claim as
  it is, so a token with no `customers` claim gives it `undefined`
  (`alerta-webui/src/store/modules/auth.store.ts:110-111`); the reference
  issues no `customers` claim without `CUSTOMER_VIEWS`
  (`models/token.py:111-112`) and applies the claim globally when it is
  there. Putting A2's hook-only list in `customers` would make the web UI
  show a customer view that alrust does not enforce. Recommendation: a
  separate claim, for example `alrust_hook_customers`, and no `customers`
  claim, as the reference issues without `CUSTOMER_VIEWS`.
- **O3, other token errors and a missing `exp`.** Recommendation: every
  verification failure other than expiry and audience is 401 `Token is
  invalid`, instead of the reference's generic 500 (section 3); a missing
  or empty `aud` is 401 `Invalid audience`. Separately, require `exp`, so
  a token without it is 401 `Token is invalid`; the reference accepts it.
  Tokens that login (L) issues always carry `exp`
  (`alerta/auth/utils.py:60`), so this tightening changes nothing for them;
  it refuses only tokens alrust did not issue through L.
- **O4, leeway.** The reference uses no clock leeway. Recommendation: none,
  to match; L's tokens live `TOKEN_EXPIRE_DAYS`.

## 8. Tests the implementation will carry

Synthetic key, tokens minted in the test, fixed `now`; no provider, no real
key. Codex's synthetic cases (review input `98221a76`) map as follows.

| Case | Expectation |
|---|---|
| Bad signature, wrong or missing audience, expired, `nbf` or `iat` in the future, `alg` `none` or `HS512`, a non-string `sub`, no `exp`, malformed token | 401 with the section 3 message as O3 settles it; no `Caller`, no facts |
| Valid token, missing or empty `sub` or `preferred_username`, or a non-string | the `Caller` as section 4 (login `None` where absent); `OperatorFacts` `None`; the hook skips |
| `provider` absent, `basic`, `ldap` or another provider | `OperatorFacts` `None` |
| Hook-customers claim missing, `null`, a string, `[1]`, `[""]` | `OperatorFacts` `None` |
| Claim `[]` | `customers` `Some([])`; a non-admin actor is authorised for no alert |
| Claim `["b","a","a"]` | `customers` `Some(["a","b"])`; duplicates do not widen membership |
| Customer A operator acknowledges a customer B alert, or an alert with an empty customer | not authorised; skip |
| Admin scopes: exact `admin`, `admin:alerts`, near matches (`Admin`, `admin:alert`, `admin:alerts:x`) | as hook spec 4.1.1, unchanged |
| An ingestion-mapped login in `preferred_username` | not an actor |
| `user_id`, `customers`, `provider` or `login` sent in the body, query or other headers alongside a valid token | ignored; facts come only from the token |
| A valid token and an API key together | the API-key caller; facts exactly as today |
| An API key, an admin API key, a key whose user looks like a person, Basic credentials, no credential | `operator` `None`; facts byte-identical to today's |
| Every existing API-key route test, replayed unchanged | the same responses (global semantics unchanged) |
| A Bearer operator with a hook-customers claim reads alerts of any customer | allowed by scope; the claim does not filter (section 4) |

## 9. Out of scope

L itself (the `/auth/openid` routes, the provider exchange, discovery and
key fetching, user records, scope selection); SAML, LDAP, Basic, Hawk and
auth proxy; logout and `/userinfo`; any change to API keys; any
configuration, key file or provider access. The adapters' registration
stays with the hook spec and its holds.
