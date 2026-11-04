# OIDC operator caller facts (design)

Status: **reviewed design (PR #189, Codex approval `83fc8774`); its owner
choices are decided (DECISIONS Y50, Y51). The verifier and conversions of
sections 4 to 6 are implemented as pure code (`crates/alrust-domain/src/bearer.rs`,
PR #192), and the request path of sections 3, 5 and 6 (`authenticate`,
`Caller::from_bearer`, `facts()` in `crates/alrust-api/src/lib.rs`, PR #194)
behind `ApiConfig::bearer`. That setting is `None` unless configured, and the
server configures nothing yet, so section 3's credential order is not in
effect: with `None` a Bearer header is ignored as before. Configuring the
verifier from the server's settings is a separate, later slice.** This page
designs the one place where a verified operator credential becomes the
`CallerFacts` that the integration hooks receive
(`crates/alrust-store-pg/src/hooks.rs`; `spec/integration-hooks.md`,
sections 2 and 4.1.1). It builds on settled decisions: KICKOFF's auth target
(API keys for sources, OIDC for operators, no customer views, no
per-customer RBAC), DECISIONS Y9 and `spec/security-fixes.md` SF-MGMT (the
token signing key and HS256 Bearer tokens), Y28 (auth-route spec depth
only) and Y44 A1 and A2 (what the hooks capture and where operator
customers come from). Where the design answers differently from the
reference, the owner decided the point (DECISIONS Y50 and Y51, owner
choices O3a, O3b and O3c, section 8); it adopts no other auth contract.
Section 7 separates what the later login design must
guarantee for these facts to be true from proposals that are that design's
own to make. It reads no provider, configuration, key or credential; the
examples use synthetic values.

Citations: `reference/alerta/<path>:<line>` at v9.1.0 for the server,
`reference/alerta-webui/<path>:<line>` for the web UI, PyJWT 2.12.1 (the
reference's pinned version) as `jwt/<file>:<line>`, and alrust paths at
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
work with its own design and review (section 7).

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

## 3. Which credential a request uses

The order is the reference's and is covered by SF-MGMT item 3 (Codex's
review `f2ba85e5`; no new owner decision): an API key on any of its three
carriers, then a recognised Bearer token, then the auth-off caller (without
`AUTH_REQUIRED`) or 401 `Missing authorization API Key or Bearer Token`
(`decorators.py:30-54`, `:69-91`, `:155-175`). Hawk, auth proxy and Basic
are not part of KICKOFF's target and stay unread.

- **API keys keep today's extraction and selection exactly**, including
  the empty carriers: an `Authorization` value received as `Key` and a
  space with only spaces after it, an empty `X-API-Key` and an empty
  `?api-key=` each select the key path and give 401 `API key parameter ''
  is invalid`, as today. (If HTTP parsing removes the trailing spaces, the
  value alrust receives is `Key`, which is neither a key carrier nor a
  Bearer header, and the request goes on as the table's last row.) (The reference instead
  treats an empty carrier as no key and goes on to the Bearer check,
  `decorators.py:31-42`; that is an existing alrust difference, which this
  design does not change.)
- **A recognised Bearer header** is an `Authorization` value that matches
  the reference's `re.match(r'Bearer (\S+)', ...)`: it starts with exactly
  `Bearer` and one space, followed by at least one non-space character; the
  token is that first run of non-space characters, and anything after it
  is ignored (`decorators.py:70-72`). Only a recognised header is verified.
- **Anything else in `Authorization`** (absent, not valid text, `Bearer`
  alone, `Bearer` and a space with nothing after it, `Bearer` followed by
  two spaces, lower-case `bearer`, `Basic ...`, any other scheme) is not a
  Bearer token, and the request goes on exactly as today.
- **A recognised but invalid token is refused** with a 401 (section 4),
  with or without `AUTH_REQUIRED`, as in the reference. This is the one
  change that turns a request alrust accepts today into a refusal: without
  `AUTH_REQUIRED`, an invalid recognised token now gives a 401 instead of
  the auth-off caller. A valid recognised token, refused or treated as
  auth-off today, becomes the Bearer operator.

| `Authorization` | `X-API-Key` | `?api-key=` | Today | With this design |
|---|---|---|---|---|
| `Key <valid>` | any | any | key caller | same |
| `Key ` and only spaces, as received | any | any | 401 `API key parameter '' is invalid` | same |
| `Bearer <t>` | `<valid>` | any | key caller | same |
| `Bearer <t>` | empty | any | 401 `API key parameter '' is invalid` | same |
| `Bearer <t>` | absent | `<valid>` | key caller | same |
| `Bearer <t>` | absent | empty | 401 `API key parameter '' is invalid` | same |
| `Bearer <valid t>` | absent | absent | 401 Missing (`AUTH_REQUIRED`), else auth-off | the Bearer operator |
| `Bearer <invalid t>` | absent | absent | 401 Missing (`AUTH_REQUIRED`), else auth-off | 401 (section 4) in both settings |
| `Bearer <valid t> extra` | absent | absent | as the row above | the Bearer operator for `<valid t>` |
| `Bearer`, `Bearer `, `Bearer  <t>`, `bearer <t>`, `Basic <x>` | absent | absent | 401 Missing (`AUTH_REQUIRED`), else auth-off | same |

## 4. The trusted boundary

One function turns a recognised token into claims, and only its result can
produce human facts:

```text
verify_bearer(token, key, audience, now) -> Result<VerifiedToken, BearerError>
```

- `VerifiedToken` has private fields and no public constructor; tests build
  one only through `verify_bearer` with a synthetic key. No request body,
  query parameter or other header can add to it or override it: the only
  input is the recognised token.
- `verify_bearer` accepts **HS256 only**, fixed in code, never from the
  token's `alg` header. It checks the signature with the effective
  `SECRET_KEY` (Y9), the audience (`OAUTH2_CLIENT_ID`, else
  `SAML2_ENTITY_ID`, else the absolute base URL, as `models/token.py:50`),
  and `exp`, `nbf` and `iat` when present against the injected `now`, with
  no leeway (reference parity; Codex `f2ba85e5`, no owner decision). As in
  PyJWT 2.12.1 with zero leeway, a token is expired when `exp <= now`
  (`jwt/api_jwt.py:507`), not yet valid when `nbf > now` (`:491`) or
  `iat > now` (`:477`), with `now` in seconds and the claim converted by
  `int()`. The implementation's tests must prove these boundaries, not
  infer them from a library's defaults.
- It also checks the claim types that sections 5 and 6 depend on and that
  the reference already fails on: `sub`, if present, is a string; `scope`,
  if present, is a string; and `preferred_username`, if present, is a
  string or `null` (the last is not a reference failure; owner choice O3a
  part 2, section 8).
- The key is passed in; the function reads no file, environment or clock.

**Errors.** The reference catches three PyJWT errors and gives 401 with
these messages (`decorators.py:75-82`); alrust keeps them (SF-MGMT item 3):

| Failure | Reference | alrust |
|---|---|---|
| malformed token, bad signature, an `exp` or `nbf` string that is not an integer (`DecodeError`, `jwt/api_jwt.py:486-489`, `:500-505`), a `b64` header that is `false` (a detached payload, which the reference never supplies; `DecodeError`, `jwt/api_jws.py:244-248`) | 401 `Token is invalid` | same |
| `exp` in the past (`ExpiredSignatureError`) | 401 `Token has expired` | same |
| `aud` present but not the expected audience (`InvalidAudienceError`) | 401 `Invalid audience` | same |

Every other failure escapes the reference's handler as a generic 500. With
PyJWT 2.12.1 these are: an `alg` other than HS256, including `none`
(`InvalidAlgorithmError`, `jwt/api_jws.py:343-357`); a missing or empty
`aud` (`MissingRequiredClaimError`, `jwt/api_jwt.py:524-527`); an `nbf` or
`iat` in the future (`ImmatureSignatureError`, `:478`, `:492`); an `iat`
string that is not an integer (`InvalidIssuedAtError`, `:471-476`); an
`exp`, `nbf` or `iat` of a type `int()` does not take, such as `null` or an
array (`TypeError`, which the `except ValueError` clauses do not catch,
`:471-473`, `:486-488`, `:500-502`); a non-string `sub`
(`InvalidSubjectError`, `:444-445`); a present non-string `jti`
(`InvalidJTIError`; `verify_jti` is on by default, `:60`, `:459-463`); a
present non-string `kid` header, or a `crit` header that is not a non-empty
list whose every item is the string `b64` with a `b64` header present
(`InvalidTokenError`, `jwt/api_jws.py:242`, `:373-397`); and, after PyJWT,
a `scope` that is
present but not a string, including `null` (`AttributeError` on `.split`,
`models/token.py:71`). The reference already refuses these, as a 500. In
alrust each is 401 `Token is invalid`, except a missing or empty `aud`,
which is 401 `Invalid audience`; no `Caller` and no facts (**owner choice
O3a part 1**, section 8).

One more shape is **not** a reference failure: a `preferred_username` that
is a number, boolean, array or object is carried on as `g.login`
(`decorators.py:84`) with route-dependent results. alrust's
`Caller.login` is text. alrust refuses such a token with 401 `Token is
invalid`, no `Caller` and no facts (**owner choice O3a part 2**, section
8); it neither coerces nor drops the value.

A token with **no `exp`** is accepted by the reference, since PyJWT checks
`exp` only when present and the reference requires no claims
(`jwt/api_jwt.py:394`, `:404-405`). alrust requires `exp`: a token without
it is 401 `Token is invalid` (**owner choice O3b**, section 8).

**Order.** The checks run in PyJWT 2.12.1's order, and the first failure
decides the response: split the compact token (the last `.` ends the
payload, the first `.` ends the header, `jwt/api_jws.py:302-303`); decode
the three segments; the header as a JSON object; the `kid`, `crit` and
`b64` header checks; `alg`; the signature, computed over the original
encoded `header.payload` bytes, never over re-encoded data; the payload as a
JSON object; then the claims (`jwt/api_jwt.py:394-419`): `exp` present
(where PyJWT's `require` option runs), `iat`, `nbf`, `exp` not expired,
`aud`, `sub`, `jti`; then `scope` and `preferred_username`. So a token with
no `exp` and a wrong `aud` gets `Token is invalid`; an expired token with a
wrong `aud` gets `Token has expired`; a wrong `aud` with a non-string `sub`
gets `Invalid audience`.

**Decoding profile (owner choice O3c, section 8).** alrust decodes tokens
by this profile. Any token outside it is 401 `Token is invalid`, with no
`Caller` and no facts, even where the reference's Python stack accepts it.

- **Segments:** base64url with the URL-safe alphabet (`A-Z`, `a-z`, `0-9`,
  `-`, `_`) only. Two allowances are chosen to match PyJWT
  (`jwt/utils.py:25-33`), though JWS itself omits padding: canonical `=`
  padding may be present or absent, and unused low bits of the last
  character are ignored. Refused, though the reference's non-strict decoder
  accepts them: any other character, including `+`, `/`, whitespace and a
  further `.` inside the payload segment; non-canonical padding; data after
  padding.
- **JSON:** UTF-8 JSON as `serde_json` 1.0.154 parses it, with the
  workspace's features (no `arbitrary_precision`, no `preserve_order`); the
  header and the payload must each be a JSON object. Duplicate keys: the
  last wins, as in Python. Refused, though Python's `json.loads` accepts
  them: a byte-order mark; UTF-16 or UTF-32 text; the literals `NaN`,
  `Infinity` and `-Infinity`; unpaired surrogate escapes such as
  `"\ud800"`; numbers that overflow `f64`, including integers of more
  than about 308 digits (numbers that underflow, such as `1e-400`, are
  read as zero, not refused); nesting of 128 levels or more, counting the
  top-level object (`serde_json`'s recursion limit; 127 levels are
  accepted). Integers beyond 64 bits but inside the `f64` range are
  read as `f64`.
- **Time claims (`exp`, `nbf`, `iat`)** convert as Python's `int()` does in
  PyJWT's checks. A JSON number: an integer as it is, any other number
  truncated toward zero. A boolean: 1 or 0. A string: optional surrounding
  whitespace (the same set `int()` strips), an optional `+` or `-`, then
  ASCII digits `0-9` with single underscores allowed between digits, at
  most 4300 digits (Python 3.12's default `int()` limit, which the
  reference also refuses beyond). Values beyond the `i128` range saturate,
  which leaves every comparison with a real `now` unchanged. Refused,
  though Python's `int()` accepts them: non-ASCII decimal digits (Python
  accepts every Unicode 15.0 decimal digit). Every other string, `null`, an
  array or an object is refused as in the reference (section 4's lists).
- The profile constrains only how the token is encoded. It sets no rule for
  the text of logins, customers or other claim values.

Then two conversions read `VerifiedToken`, and nothing else does:

```text
Caller::from_bearer(&VerifiedToken)       -> Caller          (global semantics)
OperatorFacts::from_token(&VerifiedToken, provider) -> Option<OperatorFacts>  (hooks only)
```

`Caller` gains one field, `operator: Option<OperatorFacts>`. It is `None`
for every API-key and auth-off caller, so their `Caller` and `CallerFacts`
stay byte-for-byte what they are today. `facts()` reads `operator`; no
other code does.

## 5. Global semantics (`Caller::from_bearer`)

These are the reference's, for a deployment without customer views, which
is alrust's only kind. Each claim's outcome at each layer:

| Claim in a token that passed section 4 | `Caller` | Reference |
|---|---|---|
| `preferred_username` absent or `null` | `login` `None` | `g.login = None` (`decorators.py:84`) |
| `preferred_username` a string, including `""` | `login` `Some` of it, unchanged | `g.login` is the string |
| `preferred_username` a number, boolean, array or object | not reached: 401 `Token is invalid` (section 4; O3a part 2) | `g.login` is that value, with route-dependent results |
| `scope` absent | `scopes` `[""]` | `''.split(' ')` gives `['']` (`models/token.py:71`) |
| `scope` a string | `scopes` = the string split on every single space, **empty items kept** | `str.split(' ')`, same |
| `scope` `null`, a number, boolean, array or object | not reached: 401 `Token is invalid` (section 4; O3a part 1) | 500 (`AttributeError`) |
| any | `customer` **always `None`** | with `CUSTOMER_VIEWS` off the token has no `customers` (`token.py:111-112`) and `g.customers` is empty, so no filter applies |
| any | `auth_off` `false` | the route's scope check runs (`decorators.py:88-91`) |

Keeping empty items is observable and matches the reference: `POST /key`
reports the caller's scopes joined with commas in its 403
(`lib.rs:1047-1055`; reference `views/keys.py:38-41`, and `PUT /key/<id>`
at `:137-140`). With `scope`
`"write:keys  read"` (two spaces) and a requested scope `admin`, both give
403 `Requested scope 'admin' not in existing scopes: write:keys,,read`. An
empty item matches no route scope (`scope_allows`,
`crates/alrust-domain/src/lib.rs:584-604`, gives no type match for `""`).

So a Bearer operator sees and writes like an unfiltered caller with the
token's scopes, and `assign_customer` treats them as a caller with no
customer. **No customer list from the token ever reaches global filtering,
`assign_customer` or a route permission.** The reference's
`g.customers = jwt.customers` applies the token's customers globally when
`CUSTOMER_VIEWS` is on; alrust has no customer views (KICKOFF; Y44 A2) and
does not reproduce that.

The reference's `g.user_id = jwt.oid or jwt.subject` (`decorators.py:83`)
has no global counterpart in alrust's `Caller`, which carries no user id;
the user id exists only in the hook facts (section 6).

## 6. Hook facts (`OperatorFacts::from_token`)

`OperatorFacts` is `{ user_id, login, auth_type, customers }`. It is all or
nothing: `from_token` returns `Some` only when **every** check below
passes, and otherwise `None`, in which case `facts()` gives exactly today's
API-key-shaped facts (`user_id` and `auth_type` `None`; `customers` from
`Caller.customer`, which is `None`). A failed check here never refuses the
request; it only means no human actor, so the actor slice skips with
`skipped_actor_policy`, as today. (Type failures that section 4 refuses
never reach this step.)

1. **User id (A1):** `sub`, a non-empty string. Absent or `""` gives `None`
   (the request still proceeds as the section 5 `Caller`). The hook facts
   never read the `oid` claim, which the reference prefers for `g.user_id`
   (`decorators.py:83`): A1 makes the provider subject the user id. This
   rule is about hook facts only; it says nothing about whether L links
   accounts (section 7).
2. **Login (A1):** `preferred_username`, a non-empty string. Absent,
   `null` or `""` gives `None`. A1's "preferred username or e-mail" is
   applied at issuance (section 7, P2); at request time the token's
   `preferred_username` is the login, as in the reference.
3. **Auth type (A1):** `bearer:<provider>`, where `provider` is a non-empty
   string equal to the configured OIDC provider name. Any other value
   (absent, `null`, a non-string, `basic`, `ldap`, `saml2`, another
   provider) gives `None`: KICKOFF has no basic or LDAP operators, and A1
   set them aside.
4. **Customers (A2):** the `alrust_hook_customers` claim (the name is an
   engineering detail for the implementation's review; Codex `f2ba85e5`).
   It must be an array of non-empty strings; the result is sorted and
   de-duplicated. An empty array is allowed and means no customers (the
   actor slice then authorises nobody except an allowed admin). A missing
   claim, `null`, a non-array or any non-string or empty item gives `None`
   for the whole `OperatorFacts`. `None` is never "every customer".
   A separate claim, and no `customers` claim, keeps the web UI from
   showing a customer view alrust does not enforce: its `customers` getter
   shows `['ALL (*)']` for an empty `customers` array and otherwise the
   claim as it is (`alerta-webui/src/store/modules/auth.store.ts:110-111`),
   and the reference issues no `customers` claim without `CUSTOMER_VIEWS`
   (`models/token.py:111-112`).

`facts()` then sets `user_id`, `login`, `auth_type` and `customers` from
`OperatorFacts`, and the rest as today: `scopes` from `Caller.scopes`,
`auth_required` from configuration, `request_id` `None`, `from_request`
`true`.

Unchanged downstream (hook spec 4.1.1): the ingestion-login exclusion, the
exact `admin` and `admin:alerts` scope test, `admin_exceptions`, the
cohort's `allow_admin`, and the actor-customer check. These facts carry no
admin meaning of their own: an operator is an admin only by the token's
scopes, exactly as for global route permissions.

## 7. Login (L): prerequisites and proposals

R trusts a token because alrust signed it, so the facts in section 6 are
only as true as L's issuance. This section does not design, select or
implement L.

**Prerequisites.** These follow from A1 and A2 and from R's design. L's
own design must meet them, with evidence and synthetic tests, before any
token it issues can carry hook customers:

- **P1, subject.** The token's `sub` is the provider subject from a
  verified ID token. If userinfo also supplies a subject, it must equal the
  verified ID token's `sub`; otherwise no token is issued with it.
- **P2, login.** The token's `preferred_username` is the provider's
  `preferred_username` if that is a non-empty string, else its `email` if
  that is a non-empty string, both from verified provider data (A1).
  Whether L admits a login that has neither is L's question (the reference
  falls back to `nickname`, else 400, `oidc.py:167-169`); if it does, that
  token carries no `alrust_hook_customers` claim, so it never yields hook
  facts.
- **P3, provider.** The token's `provider` is the configured provider name
  (`oidc.py:203`).
- **P4, customers (A2).** `alrust_hook_customers` holds the provider's
  customers claim, or its groups mapped to customers by alrust
  configuration. A mapping that cannot be applied writes no claim, so the
  operator can log in but is never a hook actor.
- **P5, flow binding.** The login is bound to the request that started it
  (`state`, and `nonce` where the flow carries an ID token), so a code or
  ID token from another login is refused.

**Proposals for L's own design.** These are not adopted here. Each needs
L's design and review, and the owner where it changes the auth contract.
The AUTH-SEC observations are evidence for them, not authority:

- ID token verification: issuer equal to the configured issuer, the
  issuer's published keys, an algorithm allowlist from configuration and
  never the token's header, audience equal to the client id, `exp` and
  `iat` checked (the reference skips this by default and takes the
  algorithm from the header in verify mode, AUTH-SEC-5,
  `oidc.py:124-142`).
- Account linking by e-mail (AUTH-SEC-6, `oidc.py:171-187`): whether L
  keeps, restricts or drops it. Section 6 only ignores `oid` for hook
  facts.
- How an operator's scopes are chosen (the reference's `Permission.lookup`
  over roles and `ADMIN_USERS`, AUTH-SEC-6 and AUTH-SEC-7).

## 8. Owner choices (decided)

After Codex approved this page (`83fc8774`), the owner decided O3a and
O3b, and later O3c, in Claude's session, one at a time, each shown with the
reference behavior, the advisor's recommendation and Codex's; the owner
chose the recommended option each time (DECISIONS Y50, Codex recorded them
resolved in `88b2b4b4`; Y51, Codex recorded it resolved in `ca7605fe`).

- **O3a, token shapes the reference does not refuse cleanly** (section 4):
  "Refuse both, 401".
  - **Part 1, failures the reference refuses as a generic 500:** 401
    `Token is invalid`, except a missing or empty `aud`, which is 401
    `Invalid audience`; no `Caller` and no facts. The three settled
    SF-MGMT 401s are unchanged.
  - **Part 2, a `preferred_username` that is present and neither a string
    nor `null`** (the reference carries it on as `g.login`): 401 `Token is
    invalid`; no `Caller` and no facts.
- **O3b, a token with no `exp`** (the reference accepts it, and it never
  expires): "Require expiry, 401". It is refused with 401 `Token is
  invalid`. Tokens that L issues always carry `exp`
  (`alerta/auth/utils.py:60`), so this changes nothing for them; it
  refuses a token that is otherwise validly signed with the effective key
  but was made some other way, such as by hand or by an older tool, which
  the reference accepts.
- **O3c, loosely encoded tokens** (section 4, decoding profile): "Refuse,
  401". A token outside the profile, that is, loose base64, JSON that is
  not standard UTF-8 JSON as the profile states, or a time claim written as
  text with non-ASCII digits, is refused with 401 `Token is invalid`; no
  `Caller` and no facts. alrust's own login is designed never to make such
  tokens; a validly signed token made some other way, which the reference
  accepts, can be refused. The alternative set aside was a
  Python-compatible JSON parser and base64 decoder in the login check.

Settled without a new owner decision (Codex `f2ba85e5`): the credential
order and the refusal of an invalid recognised token (section 3, SF-MGMT
item 3); zero leeway (section 4); the claim name (section 6, an
engineering detail).

## 9. Tests the implementation will carry

Synthetic key, tokens minted in the test, fixed `now`; no provider, no real
key. Codex's synthetic cases (review input `98221a76`) map as follows.
Rows marked **O3a**, **O3b** or **O3c** follow those owner choices
(section 8). Each token is fully signed over its original encoded
segments; nothing is normalised before verification.

| Case | Expectation |
|---|---|
| Malformed token, bad signature | 401 `Token is invalid`; no `Caller`, no facts |
| `exp` in the past | 401 `Token has expired` |
| `aud` present and wrong | 401 `Invalid audience` |
| An `exp` or `nbf` string that is not an integer; a `b64` header `false` | 401 `Token is invalid` (reference parity) |
| `alg` `none` or `HS512`; `aud` missing or `""`; `nbf` or `iat` in the future; an `iat` string that is not an integer; `exp`, `nbf` or `iat` `null` or an array; `sub` a number, array or object; `jti` not a string; `kid` not a string; `crit` empty, not a list, or naming anything but a present `b64`; `scope` `null`, a number or an array | **O3a part 1**: 401 `Token is invalid`, except `aud` missing or `""`: 401 `Invalid audience`; no `Caller`, no facts |
| `preferred_username` a number, boolean, array or object | **O3a part 2**: 401 `Token is invalid`; no `Caller`, no facts |
| No `exp`, otherwise valid | **O3b**: 401 `Token is invalid`; no `Caller`, no facts |
| Outside the decoding profile, each in its own signed token: a `+`, `/`, `!` or non-ASCII byte in a segment; a further `.` in the payload segment; non-canonical padding; data after padding; a UTF-8 byte-order mark; UTF-16 or UTF-32 text; `NaN` or `Infinity`; an unpaired surrogate escape; a number such as `1e400` or a 401-digit integer; 128 levels of nesting; a time string with non-ASCII digits | **O3c**: 401 `Token is invalid`; no `Caller`, no facts |
| Inside the profile: segments with and without canonical padding; a last character with unused low bits set; duplicate keys (last wins); a 300-digit integer; 127 levels of nesting; time strings such as `" +1_800_000_001 "`; a time string of exactly 4300 digits | accepted where every other check passes; 4301 digits refused, as in the reference |
| Mixed failures: no `exp` and a wrong `aud`; an expired token with a wrong `aud`; a wrong `aud` with a non-string `sub` | `Token is invalid`; `Token has expired`; `Invalid audience` (section 4, order) |
| `now` exactly at `exp`, `nbf` and `iat`, and one second either side | expired at and after `exp`; not yet valid before `nbf` and before `iat`; valid otherwise (section 4) |
| `sub` absent or `""` | `Caller` as section 5; `OperatorFacts` `None`; the hook skips |
| `preferred_username` absent, `null` or `""` | `Caller.login` `None`, `None` or `Some("")`; `OperatorFacts` `None` |
| `scope` absent | `Caller.scopes` `[""]`; every scoped route 403 |
| `scope` `"write:keys  read"`, `POST /key` asking for `admin` | 403 `Requested scope 'admin' not in existing scopes: write:keys,,read` |
| `provider` absent, `null`, `basic`, `ldap` or another provider | `OperatorFacts` `None` |
| Hook-customers claim missing, `null`, a string, `[1]`, `[""]` | `OperatorFacts` `None` |
| Claim `[]` | `customers` `Some([])`; a non-admin actor is authorised for no alert |
| Claim `["b","a","a"]` | `customers` `Some(["a","b"])`; duplicates do not widen membership |
| Customer A operator acknowledges a customer B alert, or an alert with an empty customer | not authorised; skip |
| Admin scopes: exact `admin`, `admin:alerts`, near matches (`Admin`, `admin:alert`, `admin:alerts:x`) | as hook spec 4.1.1, unchanged |
| An ingestion-mapped login in `preferred_username` | not an actor |
| `user_id`, `customers`, `provider` or `login` sent in the body, query or other headers alongside a valid token | ignored; facts come only from the token |
| Every row of the section 3 table | as the table |
| An API key, an admin API key, a key whose user looks like a person, Basic credentials, no credential | `operator` `None`; facts byte-identical to today's |
| Every existing API-key route test, replayed unchanged | the same responses (global semantics unchanged) |
| A Bearer operator with a hook-customers claim reads alerts of any customer | allowed by scope; the claim does not filter (section 5) |

## 10. Out of scope

L itself (the `/auth/openid` routes, the provider exchange, discovery and
key fetching, user records, scope selection); SAML, LDAP, Basic, Hawk and
auth proxy; logout and `/userinfo`; any change to API keys; any
configuration, key file or provider access. The adapters' registration
stays with the hook spec and its holds.
