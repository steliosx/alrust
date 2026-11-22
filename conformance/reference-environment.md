# Reference environment security-refresh candidate — Y54 successor

The reference source remains Alerta 9.1.0 at
`f4312727783dad64198b62ed778bbca98a5144a3`. The proposed build/runtime locks
refresh dependencies without changing that source pin. This environment is
**not runtime-qualified**. All 19 executing profiles remain `NOT_RUN`, ARM64 is
deferred, and historical results describe their original exact environments.

## Provenance and static compatibility

The original lock resolution came from the reference's `requirements.txt`.
The original 517e90b4 proposal overrode nine pins. Certifi 2026.10.19 has
already landed on main in PR #201. This successor changes eight additional
pins against main a86aa445; certifi and its current hash stay unchanged. Five direct pins in the unchanged upstream file conflict:
cryptography, PyJWT, pymongo, requests and Werkzeug. This is not an unchanged
resolution of that file. The existing launcher installs the exact hashed locks
and then the reference with `--no-deps`; it does not re-resolve that file.

The reference's `setup.py` lower bounds and all 30 active declared dependency
edges are satisfied by the 37-package build/runtime candidate. Extras include
`sentry-sdk[flask]`. No additional transitive version change was required by this
finite metadata check. In the retained 2026-11-06 evidence, all nine candidate wheels were fetched from official
version-specific PyPI metadata and checked for hash, size, name, version,
Python requirement, non-yanked status and target tags: CPython 3.12, Linux
x86_64, glibc 2.34 or newer. psycopg2 remains its original hashed source
distribution; its build has not run. These checks do not establish installation,
ABI compatibility, runtime behavior or complete security.

| Package | Original reviewed environment | Y54 successor |
|---|---|---|
| brotli | 1.2.0 | 1.2.1 |
| certifi (already on main; unchanged here) | 2026.7.22 | 2026.10.19 |
| cryptography | 46.0.6 | 50.0.0 |
| dnspython | 2.8.0 | 2.9.0 |
| PyJWT | 2.12.1 | 2.15.0 |
| pymongo | 4.16.0 | 4.18.3 |
| requests | 2.32.5 | 2.33.0 |
| Werkzeug | 3.1.7 | 3.1.9 |
| setuptools (build lock) | 84.0.0 | 84.1.0 |

The exact candidates returned no advisory IDs in the recorded OSV queries.
That bounded result does not prove safety or absence of unreported issues.
Advisory prerequisites and aliases remain distinct from reachable flaws.
For example, the recorded Werkzeug/setuptools conditions are Windows-specific;
this environment targets Linux. Some PyJWT advisories concern APIs the
reference does not use. Updates do not fix every unsafe reference path.

## PyJWT oracle differences and the Alrust contract

`spec/oidc-caller-facts.md` retains its original PyJWT 2.12.1 comparison basis.
Alrust's settled Y50/Y51 rules remain normative; this proposal changes none of
them. Keep O3c's optional canonical padding and ignored unused low bits, O3b's
required expiry, the chosen 401 classifications and the existing login rules.

In the retained 2026-11-06 evidence, thirty bounded synthetic pure-library cases were run against each authenticated
wheel on CPython 3.12.16. They use fictional HMAC keys and signatures over the
original encoded segments, with networking and nested processes denied.
They are **not reference HTTP, provider, database or conformance-profile runs**.
The probe environment has no optional cryptography backend, so execution covers
the tested HMAC paths. RSA/DER and native-backend behavior remain source
observations or unexecuted cases.

| Case | PyJWT 2.12.1 | PyJWT 2.15.0 |
|---|---|---|
| Canonical HS256 token, ordinary synthetic key | Accepted | Accepted |
| Canonically padded header, payload or signature, valid original-byte signature | Accepted | `DecodeError` |
| Nonzero unused signature bits, identical decoded MAC | Accepted | `DecodeError` |
| Null/list/object `exp` or `nbf` | `TypeError` | `DecodeError` |
| Infinity `exp` or `nbf` | `OverflowError` | `DecodeError` |
| Null/list/object `iat` | `TypeError` | `InvalidIssuedAtError` |
| Infinity `iat` | `OverflowError` | `InvalidIssuedAtError` |
| Missing `exp` | Accepted | Accepted |
| Empty detached `b64=false` payload, no `crit:b64` | `DecodeError` | `InvalidTokenError` |
| Same detached case, `crit:[b64]` | `DecodeError` | `DecodeError` |
| Empty or JSON-`kty`-shaped fictional raw HMAC key | Accepted | `InvalidKeyError` |

The pinned reference decorator catches `DecodeError`, expiry and invalid-audience
exceptions. Its 401/500 consequences are source-derived here, not newly measured
HTTP results. Malformed `exp`/`nbf` cases move from a generic error to the
invalid-token response; the detached no-crit case moves in the other direction.
Alrust's chosen refusals remain unchanged. Reference missing-exp acceptance
still differs from Alrust's expiry requirement. Missing/wrong audience,
malformed integer controls and normal encode were also probed; no new policy
was selected.

Reference operator parsing uses fresh options and HS256 with a configured
string key. Effective keys or provider settings were not read. New key-form
refusals may affect a key with those forms; no deployment precondition was
established. The reference fetches JWKS manually and calls
`RSAAlgorithm.from_jwk` for every member. It does not use `PyJWKClient` or tolerant
`PyJWKSet`; one malformed member can still abort the manual parse. Its broad
ID-token failure fallback also remains. Alrust's fail-closed ID-token and
browser-binding requirements stay unchanged.

Before adoption, independently review the exact source and comparison statements,
then validate the changed environment through its applicable execution/CI route.
A smoke or metadata pass is not full parity. Keep original evidence bound to
its original source/environment; do not relabel it or weaken tests to copy the
new oracle. This document grants no execution, access or publication authority
and records no new owner auth decision.

## Current owner decision and exact refresh provenance

On 2026-11-08 the owner chose the refreshed environment as the **future**
authoritative reference baseline, conditional on exact source review, a reviewed
behaviour-difference list and qualification (DECISIONS Y54, PR #212). The current
environment remains the reference until those conditions are met. This candidate
is proposed source for independent Claude review, not fulfillment of qualification.
The owner manually requested this source review; automation remains stopped.

This successor starts from the original four-file reference patch from
517e90b44961ba548e823f6419290ed7e01e9f87 onto current main
a86aa445486d0cdbac02335236b616ae6b076e4d, then updates this provenance and the
explicit comparison list. The executing workflow changes only its two literal reference-lock SHA256 pins
to match these exact lock bytes (Y54-PIN-1). No Rust files, Cargo.lock, notice
corpus, submodule, Y50/Y51 requirements, DECISIONS, STATUS or qualification
implementation changes. This pin repair does not dispatch the workflow.
Original artifacts and evidence retain their exact hashes and dates. No fresh
package query, resolver, installation, native build, HTTP/provider/DB/VM/profile
execution or advisory-risk acceptance was performed by this source refresh.

### Exhaustive retained pure-library comparison controls

The following are the 30 recorded, fictional-HMAC controls on CPython 3.12.16,
not newly executed HTTP results. Each original evidence row met its expected
exception or success in each wheel. Exceptions are library class names; wire
mapping depends on the pinned reference catch surface and prior failure order.

| Recorded case | PyJWT 2.12.1 | PyJWT 2.15.0 |
|---|---|---|
| `canonical` | `OK` | `OK` |
| `header_canonical_padding` | `OK` | `DecodeError` |
| `payload_canonical_padding` | `OK` | `DecodeError` |
| `signature_canonical_padding` | `OK` | `DecodeError` |
| `signature_unused_bits` | `OK` | `DecodeError` |
| `exp_null` | `TypeError` | `DecodeError` |
| `exp_list` | `TypeError` | `DecodeError` |
| `exp_object` | `TypeError` | `DecodeError` |
| `exp_infinity` | `OverflowError` | `DecodeError` |
| `exp_bad_integer` | `DecodeError` | `DecodeError` |
| `nbf_null` | `TypeError` | `DecodeError` |
| `nbf_list` | `TypeError` | `DecodeError` |
| `nbf_object` | `TypeError` | `DecodeError` |
| `nbf_infinity` | `OverflowError` | `DecodeError` |
| `nbf_bad_integer` | `DecodeError` | `DecodeError` |
| `iat_null` | `TypeError` | `InvalidIssuedAtError` |
| `iat_list` | `TypeError` | `InvalidIssuedAtError` |
| `iat_object` | `TypeError` | `InvalidIssuedAtError` |
| `iat_infinity` | `OverflowError` | `InvalidIssuedAtError` |
| `iat_bad_integer` | `InvalidIssuedAtError` | `InvalidIssuedAtError` |
| `exp_null_before_wrong_audience` | `TypeError` | `DecodeError` |
| `missing_exp` | `OK` | `OK` |
| `missing_aud` | `MissingRequiredClaimError` | `MissingRequiredClaimError` |
| `wrong_aud` | `InvalidAudienceError` | `InvalidAudienceError` |
| `b64_false_empty_without_crit` | `DecodeError` | `InvalidTokenError` |
| `b64_false_empty_with_crit` | `DecodeError` | `DecodeError` |
| `empty_key` | `OK` | `InvalidKeyError` |
| `json_kty_key` | `OK` | `InvalidKeyError` |
| `encode_normal_key` | `OK` | `OK` |
| `encode_empty_key` | `OK` | `InvalidKeyError` |

Additional source-only differences, not covered by those 30 executed controls:

- Deep header or payload JSON recursion can move from uncaught RecursionError
  to DecodeError at the reference operator-token catch surface (inferred 500 to
  401). Nested JSON limits and HTTP responses require qualification, not a new
  decoder acceptance rule for alrust.
- The newer loader rejects a nonempty encoded payload with `b64:false` before
  decoding it, and its key guards change treatment of some PEM/DER/JWK-shaped
  inputs. Those native/key paths were not qualified. The reference's token
  parser falls back on its configured secret for a falsy key; the pure empty-key
  controls do not prove a deployment uses an empty secret.
- Alerta creates fresh decode options and does not use PyJWKClient/PyJWKSet.
  Fixes to reused-option mutation or those high-level APIs are library controls,
  not proof that the reference's manual discovery/JWKS/fallback paths are fixed.
- The other seven changed dependency pins also need affected install/native,
  reference HTTP and applicable profile qualification. Finite dependency-edge
  metadata checks do not enumerate or rule out all downstream behaviour changes.

Qualification must demonstrate the unchanged canonical controls, intentional
Y50/Y51 exceptions (including padding and unused bits), changed exception-to-wire
mappings and precedence, configured-key assumptions without exporting secrets,
reference source/lock/interpreter/platform identity and exact covered profiles.
Retain old environment results; do not relabel them or make changed oracle
behaviour a reason to weaken alrust's settled assertions. All 19 runtime profiles
remain NOT_RUN, ARM64 deferred. Public release and PR #124 stay separate.

Y54-PIN-1 correction: d76a5677 was held because executing.yml retained its old
reference lock hashes. This successor repairs exactly those two literal pins
and rebases the proposal onto a86aa445, retaining the earlier candidate and
review unchanged. Source review and the Y54 qualification conditions still
precede adoption; no run or publication occurred while preparing this correction.
