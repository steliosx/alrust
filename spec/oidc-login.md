# OIDC operator login (proposed design)

Status: **PROPOSED; nothing here is implemented. One owner choice is
decided: Q-L1's verification requirement and failure rule (DECISIONS
Y52, section 7). Everything else is open.**
This page proposes how alrust's login stage (L) would turn an operator's
OpenID Connect sign-in into the HS256 Bearer token that
`spec/oidc-caller-facts.md` verifies (R, sections 4 to 6, implemented by
PRs #192, #194 and #196). It keeps two kinds of statement apart:

- **Settled constraints**, which L must meet and which this page does not
  reopen: KICKOFF's auth target, DECISIONS Y9, Y44 A1 and A2, Y50 and Y51,
  SF-MGMT item 3, and the prerequisites P1 to P5 of
  `spec/oidc-caller-facts.md` section 7.
- **Owner choices**, each with the reference behavior, options and a
  recommendation. Q-L1's verification requirement and failure rule are
  decided (DECISIONS Y52); every other choice and detail remains a proposal,
  not adopted. Those that change the auth contract go to the owner one at a
  time; the others are settled in a later reviewed design.

It reads no provider, configuration, key, secret or mapping, and it names no
deployment's provider: every value below is a parameter or fictional.

Sources, all read-only: the reference server `reference/alerta` at v9.1.0
(`f4312727`), cited `alerta/<path>:<line>`; the pinned web UI
`reference/alerta-webui` (`2687e4af`; `src/services/auth.ts` blob
`bccf6657`, `src/services/config.ts` blob `4da5a5a4`); and its login
library, `alerta/vue-authenticate` at
`ff6de656aa1929f242d8fa3ab4afae72c808fffa` (the commit
`package-lock.json:73` resolves), of which `src/oauth/oauth2.js` (sha256
`1361f8bc0c63a4e37481536a4fce4dd6569e3d6adc5da5cf8fc3dcdc6eda810e`) and
`src/oauth/popup.js` (sha256
`2e543b87d7c02505945063a11a381d07f0a54592283852a6fa42681407f85c2d`) were
read from that exact commit and are cited `vue-authenticate:<path>:<line>`.

## 1. Scope

In this slice: `POST /auth/openid` and the provider aliases the reference
serves with the same handler (section 3, open choice Q-L5); the provider
exchange; the claims of the issued token; and what L stores. Outside this
slice, not decided against: SAML, LDAP, Basic, GitHub, CAS and PingFederate
logins, signup and password flows, `/auth/logout`, `/userinfo`, and the
`/config` route (section 3, gap G3). Y28 set only the depth of the existing
auth-route spec; it adopts none of these.

## 2. Settled constraints

L's design must show, with evidence and synthetic tests, that every token
it issues:

- passes R: HS256 with the effective `SECRET_KEY` (Y9), the configured
  audience (`OAUTH2_CLIENT_ID`, else `SAML2_ENTITY_ID`, else
  `ALRUST_BASE_URL`), an `exp` (Y50 O3b), and the decoding profile (Y51):
  canonical base64url, UTF-8 JSON, integer time claims;
- carries, for hook facts, `sub` = the provider subject from a verified ID
  token, equal to the userinfo subject when userinfo supplies one (P1, A1);
  `preferred_username` = the provider's `preferred_username`, else its
  `email`, each a non-empty string from verified provider data (P2, A1);
  `provider` = the configured provider name (P3, A1); and
  `alrust_hook_customers` = the provider's customers claim, or its groups
  mapped to customers by alrust configuration, only when that can be
  applied (P4, A2);
- carries `alrust_hook_customers` only for a login bound to the request
  that started it (P5);
- never applies hook customers to global filtering, customer assignment or
  route permissions (A2; KICKOFF: no customer views, no per-customer RBAC).
  Global permissions come only from `scope` (section 6).

## 3. Consumer inventory: the pinned web UI

The web UI's login is the consumer L must serve (KICKOFF: the vendored web
UI's login works). What it does, from source:

| Step | Who | What | Source |
|---|---|---|---|
| Configuration | browser | fetches build-time environment values, then its own `config.json`, then the server's `GET <endpoint>/config`, and alerts and stops if the last request fails; but the effective value of each key gives priority the other way: environment over `config.json` over the server (`mergeConfig` spreads server, then local, then environment). So `provider`, `client_id` and the provider URL settings (`oidc_auth_url`, `keycloak_url` and `keycloak_realm`, `azure_tenant`, `gitlab_url`, `cognito_domain` and `aws_region`) can come from the browser deployment as well as from the server's `/config` | `alerta-webui/src/services/config.ts:15-35`, `:65-77`, `:80-86`; `auth.ts:14-121`; `alerta/views/config.py:25-40` |
| Initiation | browser | generates `state` as `Math.random().toString(36).substr(2)`, stores it in `localStorage` under `<provider name>_state`, opens a popup at the provider's authorization endpoint with `response_type=code`, `client_id`, `redirect_uri`, `scope=openid+profile+email` and `state`; no `nonce`, no PKCE | `auth.ts` provider blocks; `vue-authenticate:src/oauth/oauth2.js:39-50`, `:121-153` |
| Callback | browser | reads the popup's query and fragment at `redirect_uri` (`window.location.origin` plus the base path); rejects on `error`; rejects only when a returned `state` differs from the stored one, so a callback without `state` passes | `vue-authenticate:src/oauth/popup.js:50-70`; `oauth2.js:57-59` |
| Exchange | browser to alrust | `POST <provider url>` with JSON `{code, clientId, redirectUri}` and `state` when the callback had one | `oauth2.js:77-112` |
| Token | browser | takes `token` from the JSON response, stores it, then sends `Authorization: Bearer <token>` | `auth.ts:17-24` (`tokenPath`, `tokenName`, `tokenPrefix`) |

Routes the UI calls: `openid` and `cognito` use `/auth/openid`; `azure`,
`gitlab`, `google` and `keycloak` use `/auth/<name>`; `github` and
`pingfederate` use routes outside this slice (the reference server has no
`/auth/pingfederate`).

Compatibility gaps this inventory shows:

- **G1, flow binding.** The browser alone creates, stores and checks
  `state`; the reference server never sees it except as an optional
  echoed field and ignores it; no `nonce` is sent, so the ID token carries
  none; PKCE is not used. So with the pinned UI unchanged the server has no
  value of its own to bind the code to, and P5 cannot be met server-side.
  See Q-L9.
- **G2, untrusted fields.** `clientId` and `redirectUri` come from the
  request body; the reference sends them on to the provider as given
  (`oidc.py:72-118`). See section 5.
- **G3, `/config`.** alrust serves no `GET /config` today, so the UI cannot
  learn the provider or client id from alrust. Serving it is a separate
  slice (its spec entry exists); L depends on it for the UI flow.

## 4. Flow ownership (proposal)

| Element | Created by | Stored by | Sent to | Validated by | Proposal |
|---|---|---|---|---|---|
| Authorization request | browser | — | provider | provider | unchanged |
| `state` | browser | browser | provider, then alrust | browser only (today) | Q-L9 |
| `nonce` | nobody (today) | — | — | — | Q-L9 |
| PKCE verifier | nobody (today) | — | — | — | Q-L9 |
| `code` | provider | — | browser, then alrust | provider, at the token endpoint | one use, provider-enforced |
| `redirectUri` | browser (its origin and base path) | — | alrust, then provider | alrust (proposed) and provider | must equal a configured allowed value |
| `clientId` | effective browser configuration (environment, `config.json` or server `/config`) | — | alrust | alrust (proposed) | must equal the configured client id |
| client secret | configuration | server | provider token endpoint | provider | never in a response or log |
| ID token, access token | provider | not stored | — | alrust (section 5) | discarded after the login |
| alrust token | alrust | browser | alrust on each request | R | section 6 |

## 5. Provider exchange (Y52 requirement decided; other details proposed)

- **Request fields.** `code`: a non-empty string, passed on once and never
  logged. `clientId`: must equal the configured client id, else 400.
  `redirectUri`: must equal one configured allowed redirect URI, else 400.
  `state`: see Q-L9. Unknown fields ignored.
- **Discovery.** From the issuer URL the configured provider selects (as
  the reference: a provider-specific rule for `AUTH_PROVIDER` = `azure`,
  `cognito`, `gitlab`, `google` or `keycloak`, else `OIDC_ISSUER_URL`,
  `oidc.py:20-32`), whichever of the five routes was called (Q-L5): fetch `/.well-known/openid-configuration` over
  HTTPS; require `issuer` to equal the configured issuer exactly; take
  `token_endpoint`, `userinfo_endpoint` and `jwks_uri` from it. Cache with a
  bounded lifetime. The reference fetches on every login with a 2-second
  timeout and returns 503 on failure (`oidc.py:20-58`).
- **Keys.** Fetch `jwks_uri`; select the key by `kid`; on an unknown `kid`
  refetch once (rotation), then refuse. Bound response sizes, timeouts and
  refetch rate.
- **Token endpoint.** Client authentication `client_secret_basic` or
  `client_secret_post`, chosen from the provider's advertised methods; the
  reference also offers `client_secret_jwt` (`oidc.py:84-118`).
- **ID token (Q-L1).** Decided (Y52): the ID token is always verified,
  with no switch to turn verification off, and a missing or invalid ID
  token refuses the login even if userinfo is available. The check covers
  the issuer, the signature with a trusted compatible key, a configured
  allowed algorithm (never the token's header), the intended client
  (`aud`) and the validity times (`exp`, `iat`); `nonce` per Q-L9. Still
  proposals, not decided: the key model and discovery described here, the
  algorithm list, key rotation and time tolerances. The reference verifies nothing by default and takes the
  algorithm from the header when it does (AUTH-SEC-5, `oidc.py:121-142`).
- **Userinfo.** Fetch with the access token. If it returns `sub`, it must
  equal the verified ID token's `sub` (P1, settled); else refuse.
- **Failures.** Provider unreachable, timed out or malformed: 503 with a
  fixed message that names no URL, token or secret. A provider error
  response: 400 with a fixed alrust message chosen from the provider's
  `error` only through this mapping: `invalid_grant` gives `Login code was
  rejected by the identity provider`; `invalid_client` and
  `unauthorized_client` give `alrust is not authorised at the identity
  provider`; every other value, including a missing, non-string, empty,
  oversized (over 64 bytes) or unknown one, gives `Login failed at the
  identity provider`. The provider's `error_description`, `error_uri` and
  any other field are never echoed or logged.

## 6. The issued token's claims

| Claim | Source | Type | Trust | Purpose |
|---|---|---|---|---|
| `sub` | verified ID token `sub` (P1) | non-empty string | verified | hook user id (A1) |
| `preferred_username` | userinfo, else ID token, `preferred_username`; else `email` (P2) | non-empty string | verified provider data | global login and hook login (A1); Q-L8 if neither |
| `provider` | configured provider name (P3) | string | configuration | `bearer:<provider>` (A1) |
| `scope` | Q-L3 | space-separated string | alrust policy | global route permissions only |
| `alrust_hook_customers` | provider customers claim, or groups mapped by configuration (P4); only with P5 binding | array of non-empty strings | verified provider data plus configuration | hook actor checks only (A2) |
| `aud` | the effective audience (Y9) | string | configuration | R's audience check |
| `iss` | `ALRUST_BASE_URL` | string | configuration | informational (R does not check it) |
| `iat`, `nbf` | login time | integer | server clock | R's time checks |
| `exp` | login time plus lifetime (Q-L7) | integer | server clock | required by Y50 O3b |
| `jti` | random | string | server | uniqueness |

Not issued: `customers` (the reference's global customer claim;
`spec/oidc-caller-facts.md` section 6 and A2), `oid` (account linking, Q-L2), `name`,
`picture` and other display claims unless a consumer needs them. Every
issued token must round-trip through R: a synthetic issued token verified by
`verify_bearer` gives the expected `Caller` and hook facts.

## 7. Login choices and decision status

Each lists the reference behavior, options and a recommendation. Q-L1's
verification requirement and failure rule are decided (Y52); the remaining
choices, and Q-L1's open details, are proposals, not adopted.

- **Q-L1, ID-token verification. Decided (DECISIONS Y52), in part.** The
  owner chose "Always check; refuse failure": verification is mandatory,
  there is no off switch, and a missing or invalid ID token refuses the
  login even if userinfo is available, so no token is issued without a
  verified ID token. This changes the reference's failure handling, which
  can ignore a failed check and continue on userinfo (AUTH-SEC-5). Still
  open: the exact key model, algorithm list, discovery, key rotation and
  time tolerances (section 5's proposals), and whether a given provider is
  compatible, which has not been proved.
- **Q-L2, account linking.** Reference: by default links a provider
  identity to an existing local account with the same e-mail
  (`OIDC_LINK_USER_EMAIL`, AUTH-SEC-6, `oidc.py:171-187`). A1 fixes only the
  hook user id. Recommendation: no linking; the provider subject is the
  identity.
- **Q-L3, scopes.** Reference: `Permission.lookup` over the role claim,
  stored user roles and `ADMIN_USERS` (`oidc.py:183-197`). alrust's alpha
  has no permissions administration. Options: a configured role-to-scope
  map; the reference's permissions and users tables; a fixed operator scope
  set. Recommendation: a configured role-to-scope map with a default of
  read-only scopes, `admin` only by explicit mapping.
- **Q-L4, user records.** Reference: creates or updates a `users` row at
  each login and then uses the pre-update row (`oidc.py:171-181`). In alrust
  the reference-layout migration creates the `users` table, but no route
  reads or writes it (no `/users`, `/userinfo` or logout routes).
  Storage mechanics alone are not an owner question; what matters is the
  login outcomes the stored data drives, set out in Q-L6. Recommendation:
  no row creation or update, no e-mail linking and no stored roles, but a
  read-only status check by subject (Q-L6), so that a user marked inactive
  in an adopted reference database stays refused.
- **Q-L5, routes and providers.** Reference: one handler serves
  `/auth/openid`, `/auth/azure`, `/auth/gitlab`, `/auth/google` and
  `/auth/keycloak` (`oidc.py:61-66`); the issuer comes from the configured
  `AUTH_PROVIDER`, not from the route called (`oidc.py:20-32`). So a request
  to `/auth/google` on a server configured for `keycloak` uses the Keycloak
  issuer. The UI calls the route of its effective `provider` (section 3).
  Recommendation (parity, no contract change): the same five routes, one
  handler, issuer selected by the configured provider, the route name
  ignored. Alternative (a contract change, needing owner disposition):
  refuse with 400 a route that does not match the configured provider.
- **Q-L6, login eligibility.** Reference outcomes and the proposal, rule by
  rule (`oidc.py:171-197`; `auth/utils.py:36-39`):

  | Rule | Reference | Fully stateless | Proposal | Contract change? |
  |---|---|---|---|---|
  | User status | a `users` row whose `status` is not `active` gives 403 (`oidc.py:189-190`) | no rows, so no check: an inactive user of an adopted database could log in | read-only lookup by subject; 403 when a row exists and is not `active`, whatever `AUTH_REQUIRED` is (the reference checks status unconditionally); no write | no, for rows keyed by subject; yes for e-mail-linked rows (Q-L2) |
  | E-mail domain | with `AUTH_REQUIRED`, `ALLOWED_EMAIL_DOMAINS` checked against the stored user's domain, pre-update (`oidc.py:192`) | from the verified `email` claim | with `AUTH_REQUIRED` on: the verified `email` claim's domain against the setting unless it is `*`, else 403; with `AUTH_REQUIRED` off: no check, as in the reference | yes (source of the domain): owner; the `AUTH_REQUIRED` condition is kept, no change |
  | Roles or groups | with `AUTH_REQUIRED`, `ALLOWED_OIDC_ROLES` against claim roles plus stored roles; ineffective from a configuration file (AUTH-SEC-7) | claim roles only | with `AUTH_REQUIRED` on: claim roles or groups only against the setting from alrust configuration, effective however set, unless it is `*`, else 403; with `AUTH_REQUIRED` off: no check, as in the reference | yes (stored roles dropped; the setting made effective): owner; the `AUTH_REQUIRED` condition is kept, no change |
  | Account association | e-mail linking by default (Q-L2) | none | none (Q-L2) | yes: owner (Q-L2) |
  | Stored roles to scopes | stored roles join claim roles for `Permission.lookup` (`oidc.py:183-196`) | none | claim roles through the Q-L3 map | yes: owner (Q-L3) |

  The status lookup is the only state the proposal reads, and it writes
  none.
- **Q-L7, lifetime.** `exp` is required (settled). Reference lifetime:
  `TOKEN_EXPIRE_DAYS`, default 14 days (`auth/utils.py:60`,
  `settings.py:174`), already a reviewed public setting in SF-MGMT item 1.
  Options: the reference's 14-day default; a shorter default such as 1 day.
  Recommendation: keep the setting name and the 14-day default for parity,
  accept whole days from 1 to 90, and refuse start-up outside that range.
  Each token's lifetime is fixed at issue; revocation before `exp` would
  need state (Q-L4) and is outside this slice.
- **Q-L8, no login claim.** Reference: falls back to `nickname`, else 400
  (`oidc.py:167-169`). P2 already says such a login never carries hook
  customers. Recommendation: refuse with 400, as without `nickname`.
- **Q-L9, flow binding with the pinned UI (gap G1).** Operator capture
  through the hooks is already required (Y35; Y44 A1 and A2, which set
  aside leaving customers undefined because Y35 would then be unmet). It
  needs `alrust_hook_customers`, which P5 allows only for a bound login. The
  options, with flows outlined in section 7a:
  - (a) **Staging only, deliberately incomplete.** Keep the pinned UI's
    browser-only `state`; issue tokens without `alrust_hook_customers`.
    Cost: no operator is ever a hook actor, so operator capture stays
    unfinished, and a reviewed binding and web UI change (b) are still
    required to finish it. Whether alrust should admit such global-only
    logins in the meantime is an open policy proposal, not adopted here.
  - (b) **Server-issued `state` and `nonce`**, with a web UI change. Meets
    P5.
  - (c) **PKCE alone**, with a web UI change. Does not by itself meet P5
    (section 7a); it supplements (b).
  Recommendation: (b) with PKCE as the target that completes operator
  capture; (a) only as an interim step if the owner admits global-only
  logins before the UI change.

## 7a. Flow outlines for Q-L9 (proposals)

- **(a) Staging.** Initiation, `state` creation, storage and checking stay
  in the browser (section 3); the server receives `code`, `clientId`,
  `redirectUri` and an optional echoed `state` it cannot check; no `nonce`
  in the ID token. The provider enforces single use of `code`. Nothing binds
  the code to the browser that started the login, so a login-forgery attack
  (a victim's browser completing an attacker's code) is not prevented
  server-side. Tokens carry no `alrust_hook_customers`. Does not meet P5.
- **(b) Server-issued `state` and `nonce`, bound to the initiating
  browser.** One concrete possibility, not an adopted architecture:
  - *Initiation.* A new alrust route (for example
    `POST /auth/openid/start`) creates a login transaction: random `state`,
    `nonce` and browser-binding secret (at least 128 bits each), and, with
    PKCE (c), a `code_verifier`. It stores a server-side row keyed by
    `state` holding the `nonce`, the configured provider, client id and
    allowed `redirectUri`, the verifier, a hash of the binding secret, an
    expiry (for example 10 minutes) and a consumed flag. It sets the binding
    secret in an HttpOnly, Secure cookie scoped to the login route (with
    `SameSite=None` and credentialed CORS when the web UI is served from
    another origin than the API) and returns the authorization URL with
    `state`, `nonce` and the S256 challenge. The web UI opens that URL
    instead of building its own.
  - *Completion.* `POST /auth/openid` requires both the echoed `state` and
    the binding cookie; the row must exist, be unexpired and unconsumed,
    and the cookie's secret must hash to the stored value (constant-time).
    The row is consumed atomically, in one statement that succeeds only if
    it was still unconsumed, before the code is redeemed; so of concurrent
    completions exactly one proceeds, and a second completion with a fresh
    `code` from the same authorization URL is refused. alrust then redeems
    the `code` with the stored verifier, and requires the verified ID
    token's `nonce` to equal the stored one.
  - *Meets P5* in this form. Costs: the web UI change, the CORS and cookie
    requirements above, and new state (a transaction table, which Q-L4's
    read-only proposal does not otherwise need), with expiry clean-up.
  - A stateless variant (the transaction in a signed cookie, no row) is
    left unresolved: clearing a cookie does not invalidate copied bytes,
    so its one-use property is not defined here.
- **(c) PKCE.** The initiator creates a `code_verifier`, sends its S256
  `code_challenge` with the authorization request and the verifier with the
  token exchange; the provider then refuses a code redeemed without that
  verifier. If the browser creates the verifier, PKCE binds the code to the
  holder of the verifier but not to a request alrust started: an attacker
  can complete a login with their own code and verifier pair, so it does
  not meet P5 alone. If alrust creates and records the verifier as in (b),
  PKCE adds protection against intercepted codes. So PKCE supplements (b).

## 8. Missing inputs

Needed later, from the owner or deployment configuration, not to be
inferred: which identity provider and alias a deployment uses; the client
id, allowed redirect URIs and issuer URL (configuration values); where the
client secret and the group-to-customer mapping are kept (configuration
only). None is needed to review this design.

## 9. Synthetic test plan (not an execution grant)

When an implementation is proposed: an in-process fake issuer with
synthetic keys and recorded discovery, JWKS, token and userinfo responses;
cases for each section 5 refusal, each claim rule of section 6, each Q-L6
outcome under both `AUTH_REQUIRED` settings, Q-L9's behavior (for (b): a
transferred `code` and `state` without the initiating browser's cookie,
two concurrent completions of one transaction, reuse of a transaction with
a fresh `code`, an expired transaction, a `nonce` mismatch), issued-token round-trips through R, provider
error bodies whose `error` and `error_description` carry synthetic code,
token, secret and URL markers, non-string and oversized values, and that
no secret, code,
provider token or key appears in a response, log or `Debug` output. Running
listeners, network, databases or builds for these is execution and keeps
its own gates.
