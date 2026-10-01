# Status

_Last updated: 2026-10-01T01:55Z by claude-alrust, the sole writer of this
file and DECISIONS.md. Codex sends corrections through the mailbox._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Lifecycle (receive, actions, housekeeping, heartbeats) and customer-mapping specs source-derived and reviewed. Separate scoped reference HTTP runs: smoke 2, customer 4, operator 5, metadata 2, heartbeat 3, heartbeat input/query 4, metadata mutations 3, alert queries 3, HF7 scaffold smoke 2, alert notes 2, blackouts 2, API keys 1, config 1, groups 1, permissions 1, management health 1, management gtg 1, root debug 1, root index 1 (each HTTP LOCAL_PASS at its own head and window; the first metadata-mutations attempt FAILED on a harness precondition, HF9, and is preserved). OpenAPI: heartbeat, single-alert, alert-query, `q=`, notes, blackouts, API keys, users, groups, permissions, customers, `/config`, management, the root routes `/` and `/_`, and the generic webhook dispatcher and the prometheus provider mapping (reviewed). Both root routes now have one reference HTTP run each (`GET /_` and `GET /`). Remaining inventory: the other eleven provider webhook payload mappings and auth, outstanding source work (priorities from the web UI consumer inventory; auth contract changes stay with the owner) | **Open.** These are separate scoped runs, not a full-suite pass. Hosted CI has never executed. OpenAPI extraction and broader vectors are outstanding |
| 1 Domain/engine/store | blocked on 0 | — |
| 2 HTTP API | blocked on 1 | — |
| 3 Auth | blocked on 2 | — |
| 4 Receivers | blocked on 3 (see PIN inputs) | — |
| 5 Ops | blocked on 4 (see PIN inputs) | — |
| 6 Tauri desktop | blocked on 2 (see PIN inputs) | — |

## Work in flight

Each disposition is bound to the exact head listed. A new head needs its own
review.

| Item | Author | Head | Disposition |
|---|---|---|---|
| PR #1 (`claude/scaffold`) | claude | `e839ceaf8cb43d6ed996c8fe12f884d0bb141743` | Codex: **guard correction APPROVE at `e839ceaf`**. The collaboration-doc reconciliation is a separate head (`claude/charter-sync`) pending its own review and integration. Historical: REQUEST_CHANGES at `a674546648c9ea6fd40e9915b23137b8cfaad150`. Hosted gate blocked. Draft; the `main` merge is the owner's |
| `codex/conformance-harness` | codex | `9d70dc2e4e09c95901d1f8be2096e6ec2effa2bc` | Claude: **APPROVE** for the reference-only HTTP smoke and offline scopes (the basis of window 03) |
| `codex/receive-clock-correction` | codex | `db06ce44f1fdbc3b0dbeb66b6f2952eb0d702071` | Claude: **APPROVE** for the offline `receive-clock-v1` scope (controlled fixture) and the reference-only HTTP smoke at this head. That smoke **completed** in window 04 (LOCAL_PASS). Any future heavy job needs a fresh signed window. No two-server run has been requested; there is no candidate server yet |
| `codex/synthetic-key-preflight` HF-5 (on `db06ce44`) | codex | `e91dcabea1291b6540bc4d97636e6eef8df2bef0` | Claude: **APPROVE** for the differential-CLI credential contract (`SYNTHETIC-` plus 48 hex) |
| `codex/hash-locked-harness` HF-4/HF-8 (on `e91dcabe`) | codex | `130145ea7b0f0b7502043c19b953e38da3e47a59` | Claude: **APPROVE**. Hash-locked wheels reproduced independently: offline install, tamper rejected, 40/40 tests. Citations corrected |
| `codex/customer-conformance` (on `9d70dc2e`) | codex | `a7426411181f4dd3cc859581cc203ee4067c07b4` | Claude: **APPROVE** for the 4 customer HTTP vectors as source-derived expectations. Runtime: **LOCAL_PASS** (4 tests plus stored-key attestation) at `a7426411`/`f4312727` with runner `4ba51ed9`, window released (`757bd477`); cleanup verified independently (`45696ef3`). Reference only |
| Customer runner (operations script, not in Git) | codex | `customer_reference_01.py` sha256 `4ba51ed9…8f16` | Claude: **APPROVE** for the four-vector execution scope (RF1 budget, RF2 deferred-stop cleanup). v2 `d937e69b` is superseded. Window `alrust-customer-reference-20260930-01`: ACK `7e0a2e84`, START `fdfd53eb`, **RELEASED** `757bd477` with LOCAL_PASS; cleanup verified by both agents |
| `claude/spec-customer-mapping` | claude | `decbb78aa03a55934d9b078b9f5a8c153f454870` | Codex **APPROVE** source-only (`0b27a3bc`) for C1–C23. C23: every API-key update rewrites customer; an unscoped caller clears it, removing customer filtering from alert reads the key's scopes otherwise permit. HTTP/SQL NOT_RUN |
| `claude/spec-lifecycle-receive` | claude | `47e59dd84b742e0acc543af3b233b7c00a8ae760` (pushed, PR #3) | Codex: **APPROVE** for its source-derived scope (receive slice only). HTTP beyond the 2 smoke cases is NOT_RUN. At `47e59dd8` (R1.2 states the reject-policy matching: R1.2 states the reject-policy matching: `ORIGIN_BLACKLIST` uses start-anchored `re.match`, `ALLOWED_ENVIRONMENTS` uses `re.fullmatch`, `reject.py:26,30`): Codex **APPROVE_EXACT_SOURCE** (`37e5ed9b`); pushed 2026-10-01. Earlier: `a1de491c` APPROVE. The PR #3 bot finding asking for prefix matching is a false positive (both agents; Codex `1ca8f136`) |
| `claude/spec-lifecycle-actions` (on receive) | claude | `cedfcdff5afb8198bfb3588836156ca8510d076c` | Codex: **APPROVE** for its source-derived scope (A0–A6) |
| `claude/spec-lifecycle-housekeeping` (on actions) | claude | `c6aecf66d55d476c2ff936588e2f68d9a9ed9648` | Codex: **APPROVE** for its source-derived scope (K0–K5). Deletion semantics preserved |
| `claude/spec-lifecycle-heartbeats` (on housekeeping) | claude | `84b2ed5de5b15d0baac5b24a423f8f1c23cb4ad1` | Codex: **APPROVE** for the source-derived cumulative lifecycle (OA-1 through OA-6 closed). R1.3 and OA-5/A5.2 are corroborated at runtime by the metadata run (`678f1996`, window released `0f72c944`). The other exception, custom-plugin and receive-status-change edges are runtime NOT_RUN |
| `claude/charter-sync` | claude | this head | Latest Codex review: **APPROVE_EXACT_SOURCE** at `e4d93a84` (`e7f28f1e`; ST46-C1 closed; pushed to PR #9, which stays there until a later head is approved). `bf335820` (sent `bd4ec196`) had no verdict and is superseded by this cumulative head, which also disposes the two PR #9 findings (`be317dc7`: ADR-0001/0002 status lines match DECISIONS; the clock policy leaves the open questions). Earlier: `4606baa0` REQUEST_CHANGES (`83fc788d`), `5b398dee` (`79eedab2`), `a83ae89d` (`73678f6a`), `6660315b` (`e325e836`), `812a3b8a` (`fd8f459b`), `0e74a247` (`2f2ae4cc`), `f70c9640` (`854f909c`), `38084c4a` (`14836275`), `999c1a5a` (`7adbbc6c`), `090233cf` (`a8e348a1`), `0313d437` (`20a101a0`), `48b11e3c` (`136ef825`), `10bed7e8` (`c7c629aa`; ST29-C1: checking production exposure is the owner's, under the production-access gate, not an agent task). This head needs its own review |
| `claude/spec-openapi` (on `e839ceaf`) | claude | `fe835479a38d14c0b9581a80fd16655fcb9029b7` (published, PR #8) | Codex **APPROVE_EXACT_SOURCE** at `fe835479` (`e52a1df4`): the `/webhooks/prometheus` provider mapping, 66 paths and 88 operations, after REQUEST_CHANGES at `efa54a7c` (`79e4e4fb`, WP-R1..R3) and `b01ba677` (`3da7763e`, WP-R4: parsing versus processing stage and side-effect scope). Successor `59e35995` disposes the three PR #8 findings (admin request bodies; per-resource filters and `sort-by`; the Basic, Hawk and auth-proxy schemes and bearer JWT) and adds 415 to every POST and PUT of the `api` blueprint (the management and webhooks blueprints have no JSON hook). Codex REQUEST_CHANGES (`37e5ed9b`, OA59-R1: `customer` must be nullable in the customer bodies; OA59-R2: a missing-match insert failure is a caught `ApiError(str(e), 500)` with the database text, not the generic 500); corrected successor `56dd7c5e` (local) needs its own review. Earlier: Codex **APPROVE_EXACT_SOURCE** at `55057e9b` (`3e18a5b6`; UW-R1: every API-key-authenticated request, reads included, runs one `keys` usage UPDATE before the scope check), on `469fda22` (`84551f7c`; W-N2 closed; `x-runtime` for `/` and `/_` cite their reference runs), on `c5dcd979` (`1eaa191c`; W-C1 and W-N1 closed), after REQUEST_CHANGES at `c6bb61e9` (`74bfab71`, W-C1: the legacy retry reaches the registered error handlers). The dispatcher records W-SEC-1 and W-DL-1 (see Owner); the other eleven provider payload mappings are not specified yet. Root wording: `21a8c70d` (`1ad97bdf`; ROOT-C1..C3, then ROOT-C4 on `c6bb61e9`: links are absolute only with an empty or absolute `BASE_URL`, and the `/` rule's own href doubles its slash when the base already ends in `/`). Earlier: root routes `84c9cf96` (`721cd72a`), on `6066d413` (`6ade954c`), after REQUEST_CHANGES at `24aab639` (`c640040e`, MG-R1..R3). `/` returns the full route map without credentials (source-derived). Earlier: `/config` `773d5c27` (`efcd64f7`, CF-R1) and `f667fd4f` (`10e0fac5`, CF-R2); customers `d1fdb7a8` (`702587fc`, CU-R1..R3); groups and permissions `08d751a9`, `12631144`, `593f511e`. `/config` cites the config window and management health the health window, each for exactly what it checked. Records G-SEC-1, P-SEC-1, C-SEC-1, M-SEC-1, G-DEF-1 and GR-R1 (see Owner). SQL, other HTTP, auth and validator NOT_RUN |
| `codex/harness-integration` (on `e839ceaf`) | codex | `061d2a969d8fc4bd876f54885758727c5643c896` | Claude **APPROVE** for the source/offline scope: provenance verified, guard PASS, 50 offline tests, 16 collected. HTTP NOT_RUN at this head |
| `codex/heartbeat-input-query` (on `061d2a96`) | codex | `60d2e021eeb9c1040cfa3bdb373cfe949e9b5db0` | Claude **APPROVE** for the source/offline scope (`ab47f0a4`): 4 vectors (present-null containers, unknown-only status, pre-filter total and paging). Runner `48e3f654` approved (`99738ba0`). Window `alrust-heartbeat-input-query-reference-20260930-01`: REQUEST `586679fb`, ACK `d8afb6df`, START `ff2b9fac`, **RELEASED** `ef919f45`. Runtime: **LOCAL_PASS**, 4 HTTP tests in 0.42 s plus config and stored-key attestations, frozen `60d2e021`. Cleanup and key-free capture verified by both agents (`312fdb6f`). Reference only |
| `codex/metadata-mutations` (on `60d2e021`) | codex | `70a40be307aceeb6719e80f1bdc162a24c9c1017` | Claude **APPROVE** source/offline (`f3d8fe45`); runner `94b53328` (`9deac923`). Window `…-metadata-mutations-reference-20260930-01`: ACK `317124e0`, START `d9737019`, RELEASED `b317ee1c`: **FAIL**, 2 passed and 1 failed at a harness precondition (HF9: the default `remote_ip` plugin adds `attributes.ip`). Cleanup verified by both (`fa615dfa`). Claude's review had wrongly called the precondition runtime-corroborated; corrected on record. Preserved, not rerun |
| `codex/metadata-mutations-correction` (on `70a40be3`) | codex | `a5ce0e122646f4c557ae46f7901ffc855adf6a95` | HF9 fix: Claude **APPROVE** source/offline (`c10b6b6e`); runner 02 `1b8c8721` (`43c17960`). Window `…-20260930-02`: ACK `4185564e`, START `fb38a738`, **RELEASED** `399b7344`. Runtime: **LOCAL_PASS**, 3 HTTP tests in 0.47 s (tag/untag missing input gives 500 with no change; untag-all gives `[]`; removing every attribute gives 500 while `{}` is saved). Cleanup and key-free capture verified by both (`89fe250b`). HF9 closed. Reference only |
| `codex/alert-query-conformance` (on `a5ce0e12`) | codex | `9e33dc2af0292381db1d4f4afebe25170ca68568` | Claude **APPROVE_WITH_COMMENTS** source/offline (`3e497d36`; QH-C1 withdrawn). Runner `1c9a9ef6` REQUEST_CHANGES (`35364490`, AQR-1: exact `UTC` zone gate); runner v2 `9efe0a41` APPROVE (`ac973a62`). Window `alrust-alert-query-reference-20260930-01`: ACK `302b7277`, START `bf6a054a`, **RELEASED** `e2cac6d5`. Runtime: **LOCAL_PASS**, 3 HTTP tests in 0.43 s; observed session zone `Etc/UTC` (metadata; the rejected exact gate would have failed). Cleanup and key-free capture verified by both (`590b22a8`). Reference only |
| `codex/scaffold-clock-correction` (on `9e33dc2a`) | codex | `4f808c9d1c1615d5fb347e4cbed3d597c8f566b5` | HF7 fix: Claude **APPROVE** source/offline (`93d30e4b`); runner `38442f96` (`48bda41a`). Window `…-scaffold-hf7-reference-20260930-01`: ACK `e0c5eea6`, START `9e5c7e44`, RELEASED `51804198`. Runtime: **HTTP LOCAL_PASS**, 2 tests in 0.22 s (HF7 closed). The original **aggregate result stays FAIL** (cleanup UNKNOWN: a strict bind during TCP TIME-WAIT); later cleanup confirmed by both (`ec9e9e8e`). RF3 runner `6cab03c0` (bounded port settle) approved (`e3557228`) |
| `codex/alert-note-conformance` (on `4f808c9d`) | codex | `24594379d6b8c494e0f89936212380f79290cbc3` | Claude **APPROVE** source/offline (`463582c1`); runner `d85660ff` (`7e6d761d`). Window `…-alert-note-reference-20260930-01`: ACK `b7e82bba`, START `2624a63a`, **RELEASED** `ca5f1be0`. Runtime: **LOCAL_PASS**, 2 HTTP tests in 0.64 s, aggregate PASS, RF3 port settle CONFIRMED. Cleanup verified by both (`600e3acf`). Reference only |
| `codex/blackout-conformance` (on `24594379`) | codex | `0f2566d63fe0d4a76687a64160d3158bf2e2a9e0` | Claude **APPROVE** source/offline (`aeacced6`, `3507bfe7`); runner `a058df0f` (`1febbdd1`). Window `…-blackout-reference-20260930-01`: ACK `b3ca545c`, START `66331864`, **RELEASED** `39dd3946`. Runtime: **LOCAL_PASS**, 2 HTTP tests in 0.43 s, aggregate PASS; `remaining` observed as the integer `120` on create and the string `"0"` on reads (OB-R4). Cleanup verified by both (`58aaa2f5`). Reference only |
| `codex/api-key-conformance` (on `0f2566d6`) | codex | `b1439810c16e90a701b2f136fc3e41482f7f0fa5` | Claude **APPROVE_WITH_COMMENTS** source/offline (`05d992f3`): one owned API-key create, read, update and delete case with no response bodies exported and a fixed-message, plain-assert pytest config; 66 offline. Runner `48f74f96` approved (`8a6c7c2f`): BASE_URL attested empty, five-key plus generic-pattern exclusion over every shareable artifact. Window `alrust-api-key-reference-20260930-01`: REQUEST `c0056601`, ACK `86edb30e`, START `38085eb5`, **RELEASED** `03bafb2c`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.12 s (one owned key: create 201, read, text-only update, delete, then 404; the new key was never authenticated), aggregate PASS. Cleanup and five-key plus generic exclusion verified by both (`7a323f9b`). Reference only; none of the key SECURITY items was exercised. UW-R1 (source-derived, not counted): its authenticated requests to permission-decorated routes each carried one API-key usage UPDATE on `keys`, so it is not database read-only; the result stands. |
| `codex/config-conformance` (on `b1439810`) | codex | `0c83a5a38b8d490b70263bf1058652564d4e518e` | Plan v2 `39480ddb` (v1 `b6170dc8` superseded: it expected the selector string as the model name): Claude **APPROVE_WITH_COMMENTS** (`e7a3881b`; CP-C1/C2 citations fixed in the harness, CP-N1: `/config` has no permission decorator, so a run is not authorization evidence). Harness: Claude **APPROVE** source/offline (`8a987c58`): 106 offline, 1 collected. Runner `f8811c59` (tests `574e02c7`) approved (`2d661ca9`): the pinned CLI is launched with one read-only first-request hook that attests the served app's debug, BASE_URL and ProxyFix state and binds its PID (CF-N1 to N3). Window `alrust-config-reference-20260930-01`: REQUEST `044882e3`, ACK `eb144153`, START `63776d76`, **RELEASED** `bb031d5b`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.04 s against the *instrumented* pinned reference (36 root keys, nested keysets, model name `Alerta 9.1.0`, severity/colors aliases, controlled flags and timeouts, exact endpoint), aggregate PASS. Cleanup, served PID binding and four-key plus generic exclusion verified by both (`5198b8d8`). Reference only; not authorization evidence |
| Web UI consumer inventory (source only, not Git) | codex | helper `8efc0f9e`, summary `2e212266`, correction `d47bb458` | Lexical inventory of webui `2687e4af` service calls. Claude **APPROVE_WITH_COMMENTS** (`0fd57eab`): 71 declared calls; 59 with a direct caller (corrected from 58 in `131b3b08`, WI-R1: a relative import of `getCounts` was missed); 24 of those are not yet in OpenAPI (groups 8, permissions 5 including `/scopes`, customers 4, management 3, `/config`, auth confirm/forgot/reset 3). WI-C1: `getUserAttributes(userId)` targets a GET that exists only at `/user/me/attributes`, and has no caller. Not a call graph and not a drop justification. Codex's static route inventory (`96083962`): 82 API view declarations, 50 in OpenAPI |
| `codex/groups-conformance` (on `0c83a5a3`) | codex | `6266212b413926e68b083c3fc9f36af179909292` | Plan GRP-S1..S5 (`039150fc`, identical resend `73f6ef60`): Claude **APPROVE** plan only (`ea14a636`; GRP-N1..N3). Harness: Claude **APPROVE** source/offline (`a64db9f9`): one `GET /groups?page=1&page-size=1` on an attested empty table; 40 focused, 146 offline, 1 collected. Runner `4579b607` (tests `d9624b48`) approved (`c3a055f6`): a read-only `COUNT(1) FROM groups` on the served app's own connection after readiness, bound to the served PID, window and config by a private hash chain (GH-N1..N3). Window `alrust-groups-reference-20260930-01`: REQUEST `9e4102fd`, ACK `73ab1e9e`, START `3001cb1e`, **RELEASED** `3b2690f6`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.05 s against the *instrumented* pinned reference (read-only before-request hook on every request; one count after readiness), with an attested count of 0; the empty page matched GRP-S2 type-exactly. Aggregate PASS. Cleanup, the count chain, 14 private hashes and four-key plus generic exclusion verified by both (`ad726311`). Reference only: no nonempty branch, `Group.count`, auth or UI-default paging claim. UW-R1 (source-derived, not counted): its authenticated requests to permission-decorated routes each carried one API-key usage UPDATE on `keys`, so it is not database read-only; the result stands. |
| Permissions read-only plan (source only) | codex | plan `97d1413b` | Claude **APPROVE** plan only (`6ca6658a`): one `GET /perms?page=1&page-size=1` on an attested empty perms table; seven keys, total 3, the three virtual built-ins with fresh uuid4 ids and exact hrefs (PN-1: the `urljoin` double-slash collapse was checked on Python 3.12.14) |
| `codex/permissions-conformance` (on `6266212b`) | codex | `49c2ec3682519dbe4f7ea50ebc5d6eed65574145` | Harness: Claude **APPROVE** source/offline (`2ca07db9`): 74 focused, 220 offline, 1 collected; PN-1..PN-4 addressed. Runner `a4822c91` (tests `116094f6`) approved (`5c870440`): the five served role settings and `COUNT(1) FROM perms` observed once after readiness on the served connection, chained to the PID, window and config (PRN-1..3). Window `alrust-permissions-reference-20260930-01`: REQUEST `a12a5435`, ACK `829d938b`, START `c86fe3d8`, **RELEASED** `08c6a471`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.05 s against the *instrumented* pinned reference (baseline hook on every request; role settings and count observed once after readiness), with an attested empty table and the five default role settings; seven root keys, total 3, the three virtual rows in order with exact scopes, canonical distinct uuid4 ids and exact hrefs (PERM-S1/S2). Aggregate PASS. Cleanup, the roles/count chain, 14 private hashes and four-key plus generic exclusion verified by both (`3258751a`). Reference only: no stored rows, UI-default paging, detail, `/scopes`, filter or authorization claim. UW-R1 (source-derived, not counted): its authenticated requests to permission-decorated routes each carried one API-key usage UPDATE on `keys`, so it is not database read-only; the result stands. |
| Management health read-only plan (source only) | codex | plan `233c2108` | Claude **APPROVE** plan only (`05c3489e`): one `GET /management/healthcheck` on an attested empty heartbeats table, expecting 200 and the text `OK` (MGH-S1..S4). Notes: at most the first 1000 heartbeats are examined, unordered (MGH-N1); a negative age can appear stale through `timedelta.seconds`, depending on the timeout and the within-day remainder (MGH-N2); readiness requests do not count (MGH-N4). Harness `de7e0cbc` (adds `Client.request_text`): Claude **APPROVE** source/offline (`fedb4cdd`; 48 focused, 268 offline, 1 collected). Runner `661e2576` (tests `106418b3`) approved (`82cc7ca6`): `COUNT(1) FROM heartbeats` once after readiness on the served connection, whole-record hash chain; readiness and setup health requests are separate from the one suite request (MHR-N1). Window `alrust-management-health-reference-20260930-01`: REQUEST `04393291`, ACK `40b64e36`, START `a33d7acb`, **RELEASED** `81e36c5a`. Runtime: **LOCAL_PASS**, 1 explicit HTTP test in 0.05 s (200 and the exact text `OK`, via `request_text`) against the *instrumented* pinned reference, after an attested empty heartbeats table on the served connection; readiness and setup health requests are separate. Aggregate PASS. Cleanup, the count chain, 14 private hashes and four-key plus generic exclusion verified by both (`e4eb3b75`, `69492097`). Reference only: no stale case, MGH-N1/N2 behavior or auth claim |
| Management gtg read-only plan (source only) | codex | plan `0abe8053` | Claude **APPROVE** plan only (`d205108d`): one `GET /management/gtg` on a healthy served connection, expecting 200 and `OK` (MGT-S1..S4); a falsy `SELECT true` row gives 503, while database exceptions give the JSON 500 (MG-R3). Harness `6f30b824`: Claude **APPROVE** source/offline (`ae3486b7`; 100 focused, 368 offline, 1 collected). Runner `288cd9e0` (tests `7f0a73cc`) approved for its code (`2238e59d`): one connection-identity observation after readiness, rechecked on every later request; GRM-R1: the first manifest misstated the container limits and interpreter; closed by manifest v3 (`a74de271`, `104863a8`). Window `alrust-management-gtg-reference-20260930-01`: REQUEST `d74e2ab6`, ACK `e385336e`, START `738ac813`, **RELEASED** `1fd1e0e9`. Runtime: **LOCAL_PASS**, 1 explicit HTTP test in 0.06 s (200 and the exact text `OK`) against the *instrumented* pinned reference, with the served connection identity attested after readiness and rechecked on the GTG request; readiness and setup requests separate. Aggregate PASS. Cleanup, the connection chain, 14 private hashes and four-key plus generic exclusion verified by both (`4856ada4`, `6b94fd4c`). Reference only: no 503 or 500 path, auth or production claim |
| Root `/_` read-only plan (source only) | codex | plan `84b1a2ae` | Claude **APPROVE** plan only (`4685e805`): one `GET /_` expecting 200 and `OK` (RD-S1..S4); HEAD and OPTIONS out of scope (RDN-1). Harness `0dab8866`: Claude **APPROVE** source/offline (`5eaa1dfd`; 103 focused, 471 offline, 1 collected). Runner `faa25dd1` (tests `4fc54f11`, manifest `a5813319`) approved (`824ff1a3`): one connection-identity observation after readiness, rechecked on every later request; the manifest's resources and interpreter match the code. Window `alrust-root-debug-reference-20260930-01`: REQUEST `2c9919af`, ACK `f4f23877`, START `3d934141`, **RELEASED** `54324196`. Runtime: **LOCAL_PASS**, 1 explicit HTTP test in 0.06 s (200 and the exact text `OK`, via `request_text`) against the *instrumented* pinned reference, with the served connection identity attested after readiness and rechecked on the `/_` request; readiness and setup requests separate. Aggregate PASS. Cleanup, the connection chain, 14 private hashes and four-key plus generic exclusion verified by both (Codex in `54324196`, Claude `59bffbc4`). Reference only: no HEAD, OPTIONS, CORS, auth or whole-stack claim |
| Unknown-webhook plan (source only) | codex | plan02 `435feaef` | Claude **APPROVE** plan only (`71ba9a83`; plan01 `f1c7bc3f` conditionally approved in `556a40e9`): one `GET /webhooks/alrust-synthetic-unregistered` with the synthetic key and a fixed `X-Request-ID`, expecting the exact 404 error envelope. Labeled as one expected key-usage write (UW-R1), stored keys attested before the case only; UW-C1 private server-log key scan, UW-C2 served logger attestation (`LOG_LEVEL` ERROR), UW-C3 a fixed request-ID header option. Components, each Claude **APPROVE_CODE_SOURCE_OFFLINE**: harness `83bb4058` (`208c2659`); observer `5b98c637` (`65d5ac12`, 16 tests; OBS-N2 app-level hook ordering); served hook `6750f254` (`b0aa7edd`, 18 tests; HK-N1..N3); hook02 `b26439ee` (tests `965df124`, `a59ab55f`, 20 tests: KEY_QUERY names its expiry column `active` for namedtuple cursors; HK-N2 closed). Runner, window and HTTP NOT_RUN |
| Root `/` index plan (source only) | codex | plan `4c0e0a10` | Claude **APPROVE** plan only (`693c9861`; RI-S1..S5); Codex accepted RIN-1..5 (`186ae8f5`): the expected route map is the actual served registry, including loaded built-in webhooks, frozen before the case by a separately reviewed producer, not the OpenAPI count; exact order and multiplicity; no authorization claim. Harness `6e2c1e4b`: Claude **APPROVE** source/offline (`803d39c2`; 175 focused, 646 offline, 1 collected). Inventory probe `44d540c3` (tests `d47dafad`), an inert helper that freezes the served route map once and rechecks it before the case: Claude **APPROVE** code source/offline (`8d1aa6b7`; 12 offline; RIP-C1 on freeze/recheck ordering goes to the runner review). Source finding RI-HF1: when the base ends in `/` (as with `BASE_URL` empty, where `url_root` is used, or a configured `BASE_URL` with a trailing slash), the literal `/` rule's own href ends in two slashes (`absolute_url('/')`), while the top-level `uri` has one; an absolute `BASE_URL` without a trailing slash gives one. Runtime-corroborated for the empty-`BASE_URL` case by the window below. Runner `476ff33a` (tests `32a34ed4`, manifest `2094d833`) approved (`32974092`): RIP-C1 closed in code (only the setup healthcheck freezes the route inventory; `GET /` only rechecks it before the original handler), the embedded helper is byte-identical to `44d540c3`, and resources and interpreter match the code. Window `alrust-root-index-reference-20260930-01`: REQUEST `dc89aed4`, ACK `66148b54`, START `e4ed7e4f`, **RELEASED** `70101d03`. Runtime: **LOCAL_PASS**, 1 explicit HTTP test in 0.06 s against the *instrumented* pinned reference: `GET /` compared exactly with the route inventory frozen from the served app during setup (120 rows: 108 url_map rules and all 12 built-in webhooks; static present), including the `/` row's double-slash href and the one-slash `uri`. Aggregate PASS, including the post-case chain. Cleanup, the 12/13/5/10 chain (and a rerun of the harness loader), 16 private hashes and four-key plus generic exclusion verified by both (Codex in `70101d03`, Claude `b9853d18`). Reference only: no auth, HEAD, OPTIONS, CORS, provider payload or whole-stack claim. Cleanup verifier `2e027d1c`: Claude **APPROVE** source (`a85cb2e8`, corrected `0abfe953`; RCV-N1: run without `-O`) |
| Coordination helper `render_start` (not in Git) | codex | helper `7eb51452`, tests `2f6f17f5` | Claude **APPROVE** (`4685e805`): a pure renderer that sets START tuples to `STARTED` (COORD-START-C1, after two START messages kept the request state); an accident guard, not authentication. First used for the root-debug START `3d934141`, which carries `STARTED`. Under the heavy-window protocol, START takes effect on the holder's source, ACK and preflight verification plus the matching signed START delivery; the peer's later verification corroborates it and is not a pre-execution gate (ST41-C1) |
| ALRUST-P0-CLOCK-01 policy | codex | design02 `67bb59f5…` | Claude: concurred, and approved as implemented in `db06ce44`. Resolved, not open. The two-server comparison it governs is **NOT_RUN** (no candidate server yet) |
| `claude/adr-0004-webui` (on `c9bbfaf5`) | claude | `96268854c644394a3cbd5e4b055e0b0a85538f6d` (pushed, PR #10) | Codex: **APPROVE** as a proposed assessment. No webui source is selected; E1–E3 are NOT_RUN; a re-pin would be the owner's call. At `96268854`, which disposes the two PR #10 findings (E2 adds a full-tree `npm audit` beside `--omit=dev`; either source choice needs E3): Codex **APPROVE_EXACT_SOURCE** (`37e5ed9b`); pushed 2026-10-01, threads answered. Earlier: `e5a23c46` APPROVE |
| `claude/ci-x64-runner` (on `e839ceaf`, local, never pushed) | claude | `26378de1e7d2a3a7c3a28fb14bec83c924644884` | Draft Linux x64 self-hosted routing and the job-side adapter for runner instance I3. Lineage and verdicts: `a50af152`; `0424cf6c` REQUEST_CHANGES (`6405ce47`: lock identity, cleanup containment, native PG isolation); `8edd85ef` REQUEST_CHANGES (`e8e39ba5`: E1 descendant escape, E2 unbound smoke readiness, reproduced offline); `4497c8e3` and `891d01cc` (E1/E2/E4 fixed; TERM-ignoring leader and zombie accounting); `891d01cc` REQUEST_CHANGES (`64ac6e1e`: E5, observation errors counted as quiet); `e7b1db46`: Codex closed E5 at exact source (`33af1858`; E1, E2, E4 closed at their tested scope). Then `90648afc` and `26378de1` (sent `b69b7a99`; Codex **HOLD_INTEGRATION**, `6a4622bb`, pending the broker), and `e6f80398` (sent `695c8f81`; Codex **APPROVE_COMPONENT_SOURCE_LOCAL** for the InvocationID comparison, `70368a9a`, with 4 independent real user-unit cases; the whole adapter stays HOLD_INTEGRATION): the job no longer takes any lock; it first checks, via the scheduler's read-only status socket, that its own unit is the active lifecycle (project alrust, phase running, quarantine false, and the scheduler's InvocationID equal to the manager's current one for this unit) and fails closed otherwise; this is defence in depth, never the admission authority; `scope-run.py` records `observation_error`. Evidence is offline or local only (real transient systemd user units, a fake scheduler socket, stub PostgreSQL binaries): 13 check cases and 6 end-to-end cases. **No job has run on the VM** |
| Shared-CI scheduler broker (own repository, not in Git here) | claude | candidate 6 `1b1c1d752668f0b87661e13f9bd6853bd5e8386f`, bundle `2c613631…` (c4 `8d33644b`, sent `bdb094f0`: Codex **REQUEST_CHANGES_CODE** `e4e67b85`, C4-R1, an unreadable quarantine marker was read as absent; the earlier C1 cases closed offline at c4. c5 `934a0878` (`d7c442f4`) reads the marker tri-state. c6 answers codex-omarchy's WP3-05-LOCAL-PORT, relayed by the Zabalack session: a listener whose local port is `*` was skipped as quiet and is now Unknown) | A root service that starts one one-job (`--once`) runner unit at a time and observes it from outside: ActiveState, InvocationID, cgroup and `Runner.Worker`. Every unit is reconciled (inactive or failed, and quiet) before each start and on recovery; other units must stay idle and quiet on recovery and on every tick of a lifecycle; the lifecycle is written to disk before the start request; the InvocationID is rechecked while the unit is live; after each lifecycle it verifies quiet (empty cgroup, `pgrep -u` exit 1, no declared listener) with the unit still idle. Any observation or control failure, malformed output, partial cgroup walk, residue, unexpected state, stop timeout or internal error sets a shared quarantine. Every inspection path found so far now fails closed in offline tests, including the quarantine marker itself since c5 (an unreadable marker blocks starts and is published as `quarantined: null`); this is offline evidence, not a proof that no other path exists. It holds the shared and legacy locks permanently, with no lending, and checks both identities. The status socket is read-only with a total request deadline; pause, resume and clear are on a root-only socket, and clear needs an actor, evidence and a Codex disposition. 53 offline tests (fake observer and clock, plus SystemObserver with crafted cgroup trees and command output; real sockets, SO_PEERCRED and flock), passing on Python 3.14 and 3.12.14; they fail against c1 and c2 (caveat: the cgroup-walk tests are fixture-incompatible with them). History: c1 `1b26fd15` (`b69b7a99`) got Codex **REQUEST_CHANGES** (`6a4622bb`, C1-R1 recovery, C1-R2 identity, C1-R3 real-observer failures, C1-R4 legacy barrier); c2 `f6bf4ffa` (`f29b26db`; four self-found defects) was superseded before review. Not fixable in-process: broker death releases the locks before the runners finish stopping, so Phase B must install only after Zabalack's C6 retires the in-job flock and the legacy path is root-only. Named in plan r3 (`9a45c1ff`) as the Phase B broker. **Not installed**; installing it is an owner decision |
| `claude/docs-landing` → [PR #2](https://github.com/steliosx/alrust/pull/2) (on `main`) | claude | `fcc06d7301a2048ab6fc4c73a80f5874dd3d08f4` | README only (ALRUST-DOCS-LANDING-01). Codex: **APPROVE** at `fcc06d73` (LD1–LD4 closed; `9409fde5` REQUEST_CHANGES is historical). **Marked ready for owner review**, not merged. Hosted checks for PR #2: NOT_RUN (no workflow on `main`). **The merge is the owner's** |

## Checks

Hosted checks, offline checks and live HTTP runs are recorded separately. A
local pass never satisfies a hosted gate. A reference-server smoke test is
not Rust behavioral parity.

| Check | Result |
|---|---|
| Hosted CI | Published PR #1 `e839ceaf`: **CI_BLOCKED_BILLING** (run 36632329142; jobs never started). The approved `fe835479` push to PR #8: run 36768253429, all three jobs with zero steps, **CI_BLOCKED_BILLING**. In Claude's session the owner authorized pushing `claude/*` review branches (2026-09-30): eight branches pushed at their reviewed heads with stacked PRs #3-#10 (18:42Z). `ci.yml` runs on every pull request, so their hosted runs are billing-refused too: **CI_BLOCKED_BILLING**, not a code result either way. Unpushed heads: hosted **NOT_RUN** |
| reference-guard regression, local | LOCAL_PASS 12/12 (both agents) |
| fmt / clippy / test, local | LOCAL_PASS. The crates are empty, so this proves no behavior |
| Harness offline tests | LOCAL_PASS, reproduced by Claude on Python 3.12.14: `9d70dc2e` 12/12; `db06ce44` 33/33; `e91dcabe` 40/40; `a7426411` 20/20. Offline infrastructure only |
| Harness findings | Closed: HF-1, HF-2 (controlled receive fixture only), HF-3, HF-4 (`130145ea`), HF-5 (differential CLI), HF-6, HF-7 (`4f808c9d`; HTTP LOCAL_PASS in window scaffold-hf7-01, whose original aggregate result stays FAIL), HF-8 (`130145ea`), HF-9 (`a5ce0e12`), CO-1. Open: none |
| Reference HTTP smoke (Python 3.12 / Postgres 16, one reference) | **LOCAL_PASS**: 2 cases (health; create/dedup/detail/delete), window `alrust-ref-smoke-20260929-03`, harness `9d70dc2e`, reference `f4312727`. Cleanup confirmed by Codex and independently verified by Claude. This is **reference behavior only**: not conformance of alrust, not Rust parity. Windows 01 and 02 were released NOT_RUN before start |
| API-key usage writes (UW-R1) | Source-derived (Codex audit `548f643b`, nothing re-run): a valid key on a `permission()`-decorated route runs one `UPDATE keys` (count, last_used_time) before the scope check (`models/key.py:162-166`, `postgres/base.py:1002-1008`, `auth/decorators.py:42-54`). Of the 19 audited reference windows (the earlier smoke windows were not in the audit), 14 sent the synthetic key to decorated routes and so are **not database read-only**: groups and permissions (read endpoints plus the usage write) and 12 mixed-scenario windows (alert notes, alert queries, API keys, blackouts, customer, heartbeat input/query, heartbeat vectors, metadata mutations 01 and 02, metadata edges, operator actions, scaffold HF7). The 5 others (`/config`, `/management/healthcheck`, `/management/gtg`, `/_`, `/`) hit undecorated routes. Usage counts NOT_RUN; every original result, including the FAILs, stands; the historical `READONLY` task names are identity labels |
| Two-server differential | **NOT_RUN**, and not yet approved |
| Self-hosted Linux x64 CI | **NOT_RUN.** Alrust has no runner; no Alrust job has been submitted, queued or run on the VM. The adapter and broker evidence above is offline or local only, and none of it is VM validation |
| Reference customer vectors (window `alrust-customer-reference-20260930-01`) | **LOCAL_PASS**: 4 HTTP tests plus the stored-key attestation, at suite `a7426411`, reference `f4312727`, runner `4ba51ed9`. Synthetic config (AUTH_REQUIRED False, CUSTOMER_VIEWS False). It corroborates C7–C10, C14 and C16–C17 at runtime; it does not cover C1–C19 in full or other auth modes. Cleanup was verified by both agents. Reference behavior only. UW-R1 (source-derived, not counted): its authenticated requests to permission-decorated routes each carried one API-key usage UPDATE on `keys`, so it is not database read-only; the result stands. |
| Operator status/action vectors (`codex/action-conformance` `a4d75716`) | Claude **APPROVE** as source-derived expectations (5 tests). Runner `4afb1260` approved (`8faec59d`). Window `alrust-operator-actions-reference-20260930-01`: ACK `d5fe3fc1`, START `82bcad86`, **RELEASED** `8e300281`. Runtime: **LOCAL_PASS**, 5 HTTP tests plus the effective-config and stored-key attestations, under the synthetic default config. Cleanup verified by both agents. Reference only |
| Metadata/suppression vectors (`codex/metadata-conformance` `678f1996`) | Claude **APPROVE** as source-derived expectations (2 tests). Runner `1b0cf1a1` approved (`53e52568`). Window `alrust-metadata-edges-reference-20260930-01`: ACK `10dbc36b`, START `c0aab9f7`, **RELEASED** `0f72c944`. Runtime: **LOCAL_PASS**, 2 HTTP tests plus the config and stored-key attestations, under the synthetic default config. This corroborates R1.3 and OA-5/A5.2 at runtime; the loaded-model detail is still source-only. Cleanup and a key-free capture were verified by both agents. Reference only |
| Heartbeat vectors (`codex/heartbeat-conformance` `9548f878`) | Claude **APPROVE** (3 tests). Runner `6bc8742d` approved. Window `alrust-heartbeat-vectors-reference-20260930-01`: ACK `1bd78063`, START `a833bb34`, **RELEASED** `1ba053be`. Runtime: **LOCAL_PASS**, 3 HTTP tests plus the config and key attestations. Corroborates HB0.1, HB4.1 (customer filter, empty-subset total 0) and C19. The non-empty total discrepancy, expiry and boundaries are NOT_RUN. Cleanup and a key-free capture were verified by both agents. Reference only |
| Lifecycle-transition and housekeeping HTTP vectors | **NOT_RUN** (planned) |
| Reference smoke at `db06ce44` (window `alrust-ref-smoke-20260929-04`) | **LOCAL_PASS**: 2 cases in 0.22 s (Python 3.12.14 / PostgreSQL 16.15), reference `f4312727`, runner `e0f30439`. Ack `bf39d294`, start `599ca26f`, release `32fdc13e`. Cleanup confirmed by Codex and independently verified by Claude. Reference behavior only |
| FAIL results on record | No **unexpected behavioral** failure in any executed run. Retained: the guard bypasses reproduced at `a674546` (a review FAIL, fixed at `e839ceaf`); corrected setup attempts (for example the first wheel-acquisition assumption in HF-4); expected negative probes (the tampered-wheel rejection), which are passes by design. Failed attempts are kept, not erased |

The informal smoke run of 2026-09-29, made before the charter on host Python
3.14, is superseded. It does not count as evidence.

## Owner items

- **GitHub Actions billing:** every GitHub-hosted job is refused before start.
- **Linux x64 beta (owner direction, 2026-09-30, given in Claude's session
  and directly to Codex, `a3286d1e`):** the Omarchy Ubuntu x64 runner VM
  (libvirt domain `zabalack-omarchy-x64`) is shared CI infrastructure; ARM64
  is deferred until after x64 beta.
  - Audit (read-only): `steliosx/alrust` has **no** self-hosted runner;
    `zab-al-ack`'s runners are repository-level on a personal account and
    cannot be shared. Self-hosted jobs do run despite the billing block
    (zab-al-ack run 36570231552). Claude has no VM access; the guest audit is
    the Zabalack session's (`handoffs/X64-SHARED-RUNNER-AUDIT-20260930.md`
    `22a71b3d…`, addenda `9556bc00…` and `c911190a…`).
  - VM outage: the VM was found off after an announced kernel reboot window
    and came back on kernel 6.8.0-142 with runner 22 online and idle
    (`b10fa57e`). The cause is **UNCONFIRMED** (RB01, `06501e82`).
  - Design, Zabalack session's plan r3 (`SHARED-CI-WINDOW-PLAN-20260930-r3.md`
    `9a45c1ff…`), superseding r2 after both Codex reviews. **Phase A:**
    Zabalack alone on runner 22, with no broker, no registration and no
    credential. **Phase B:** the root scheduler broker (above)
    plus one-job runner units, with Alrust as instance I3 (labels
    `self-hosted, Linux, X64, alrust`; ports 127.0.0.1:15532, 15533 and
    18082; evidence under `/var/lib/alrust-evidence/jobs`). The legacy lock
    is held by the broker with no lending, and Zabalack's C6 removes the
    in-job lock (the cutover). A job that is queued, never starts, or fails
    because the VM was busy is **not validation**.
  - Alrust's x64 beta path is blocked on Phase B: C6 and its r13 lab run,
    the Codex reviews of the broker and adapter, and the owner decisions
    below.
  - **Owner decisions (2026-10-01):** recorded as X1 to X8 in
    `docs/DECISIONS.md`. In short: Phase A approved after codex-omarchy's
    review of the exact scripts; Phase B approved in principle only, with a
    separate final approval of the complete package; the Alrust runner is
    registered only at the Phase B installation; the owner alone clears
    quarantines (clearance only; approved, reviewed scripts still run through
    `zabadmin`); the CI gate uses the free GitHub allowance and then the
    self-hosted VM, routed by an automatic read-only billing check, with no
    paid usage; beta is the Linux x64 server after Phase 5; the owner merges
    the x64 workflow after Codex approval; `codex/*` follows the `claude/*`
    cadence (the owner told Codex directly on 2026-10-01).
  - **Still open for the owner:** the final Phase B approval; creating the
    read-only billing token (X5); registering the runner (X3).
  - Codex's current task is the continuation thread `01a0f3d9` (`77acafe0`);
    the earlier `01a0eeab` lane is superseded. Both agents check hourly and
    message each other only when there is something meaningful (owner,
    2026-10-01).
- **Repository protection:** none. `branches/main` reports `protected: false`, and
  the protection and ruleset APIs return HTTP 403 (plan). The guard is
  **not enforced**. See `docs/repo-protection-recommendation.md`.

## PIN inputs and what each blocks

| Input | Blocks | Not blocked (synthetic or test substitutes allowed) |
|---|---|---|
| Real Veeam ONE (and Zabbix 7.0 LTS) webhook payloads | Phase 4 gate: replay of captured real payloads end to end; the Veeam ONE sample config | Receiver code, the mapper, synthetic labeled fixtures |
| Production `alertad.conf` | Phase 5 config-compatibility validation | The env-var config design and defaults from reference `settings.py` |
| Code-signing certificates (Windows, macOS) | Signed Phase 6 artifacts and auto-update trust | Unsigned CI builds, and test certificates |

## Open questions

1. webui: the v8.7.1 tag plus a specific fix, or an exact upstream commit?
   ADR-0004 will compare the two before the Phase 2 dependency.
2. Unknown severity gives 500 in the reference. It is reproduced as it is;
   any divergence goes to the owner (lifecycle L1).
3. The customer wording in the kickoff: preserve-plus-clarify is recorded in
   `spec/customer-mapping.md` Q1. KICKOFF is unedited. No divergence.

## Next actions

- **codex-alrust** (current task `01a0f3d9`): review this STATUS head,
  broker candidate 6 (C4-R1, WP3-05) and the PR #8 successor `56dd7c5e`
  (OA59-R1/R2). PR #3 `47e59dd8` and PR #10 `96268854` are approved
  (`37e5ed9b`) and pushed. Since 2026-10-01 Codex pushes `codex/*` under
  the same cadence as `claude/*`, by the owner's direct instruction (X8).
- **GitHub cadence (owner instruction given in Claude's session, applies to
  `claude/*` only):** push reviewed branches after every 4-5 mailbox cycles
  or right after an approving verdict; one stacked PR per branch, draft until
  its exact head is approved; no force-push or rebase of pushed branches; a
  base or retarget change needs its resulting diff and prerequisites
  rechecked (for example PR #10, which forks from an older `charter-sync`
  head); merging stays with the owner. The PR successors above are pushed
  only once approved, so every open PR stays at an approved head. PR review
  threads: #3 answered as a false positive, #7 declined (STATUS has one
  writer, on PR #9); the #8, #9 and #10 threads are answered when their
  successors are pushed.
- **claude-alrust:** answer the broker and adapter reviews; the unknown-webhook
  runner review when it arrives; qualify further windows as Codex's UW-R1
  inventory lists them; then the provider webhook mappings. The auth
  confirm/forgot/reset endpoints wait for the owner's auth-contract
  decision. Keep STATUS current.
- **Owner:**
  - PR #2 merge (approved, ready);
  - Actions billing;
  - the repository-protection plan;
  - **new, SECURITY (reference defects, source-level only, no exploit built
    or run; independently confirmed by Codex):** the pinned Alerta server
    builds SQL by string formatting from request or stored text in **four**
    places: `sort-by=attributes.<key>` (ORDER BY), `group-by` on the top-N
    endpoints (SELECT and GROUP BY, unvalidated), `q.df`, the default
    search field (replaced into a quoted identifier), and stored
    environment names on `/environments` (an IN list; arbitrary names can
    be stored via the `status: blackout` pre-receive bypass). The first
    three are reachable with `read:alerts`; the fourth needs
    `write:alerts`. Owner decisions:
    (a) what alrust does for these inputs, for example allow-listing
    `group-by` and quoting the attribute key, recorded in DECISIONS once
    decided; (b) whether production Alerta runs this version and is
    exposed, and whether to report upstream. Not blocking Phase 0 source
    work;
  - **new, AUTH (AN1):** with `AUTH_REQUIRED` and `ALLOW_READONLY` both on,
    a request **without credentials** is given read-only scopes but then
    dispatched **without any scope check**, so it reaches write and delete
    endpoints (`auth/decorators.py:164-169`; static source finding by
    Codex, confirmed by Claude; no request executed). Decide whether alrust
    keeps this, and check whether production Alerta runs with
    `ALLOW_READONLY`;
  - **new, customer scope (C21, C22, C23):** after the path alert is found
    with the usual customer filter, note update and delete look the note up
    by id with no alert or customer predicate (C21). A blackout update
    rewrites its customer; an unscoped caller clears it to NULL, which
    widens matching under `CUSTOMER_VIEWS` (C22). An API-key update does the
    same, so a customer-scoped key edited by an admin loses customer
    filtering on the alert reads its scopes otherwise permit (C23; clearing
    the customer does not grant `read:alerts`). Preserve or change in alrust? Recorded,
    not decided;
  - **new, SECURITY (users; source-level, nothing executed; urgent for
    production):** `PUT /user/<id>` checks the role ceiling only for a
    plural `roles` field, but the model also accepts a legacy singular
    `role`, so `{"role": "admin"}` gives any user the admin role without
    the check (`views/users.py:181-186`, `models/user.py:180-181`). A holder
    of `admin:users` can therefore create full admins under the default role
    mapping. Combined with AN1 (`ALLOW_READONLY`), a caller without
    credentials reaches that handler and could set a user's role and
    password, an inferred takeover chain. Also: a failed user insert can
    return 201 with unsaved data, and a missing password is hashed as `''`.
    Decide alrust's behavior and check production exposure;
  - **new, SECURITY (groups, permissions and customer mappings; source-level, nothing
    executed; confirmed by Codex as source traces; same class as the users
    item):**
    - G-SEC-1: at password login, group names are passed to the role lookup
      (`auth/basic.py:52-53`, `:93-94`), so a group whose name is in the
      configured `ADMIN_ROLES` (default `['admin']`) gives its members
      `ADMIN_SCOPES`. No group endpoint applies a role ceiling, so a holder
      of `admin:groups` can create that group, add any user (including
      themselves), and that user obtains full admin at their next
      successful password login (the usual password, account-status,
      domain and email-verification checks still apply). The token keeps
      those scopes until it expires (default 14 days), even after removal.
      Under `CUSTOMER_VIEWS`, group names also act as customer match keys;
    - P-SEC-1: `PUT /perm/<id>` applies neither the scope ceiling nor the
      reserved role names that `POST /perm` does
      (`views/permissions.py:132-156` against `:26-36`), so a holder of
      `admin:perms` can give any role, including the default `user` role,
      any scope such as `admin`. `POST` also lets empty-string scopes
      through its ceiling;
    - GR-R1 (availability, data integrity; inferred from source): `PUT
      /group/<id>` with `{"name": ""}` commits the empty name and then
      returns 500. After that the group can no longer be read, fixed or
      deleted through the API. A member's password login fails once it
      passes the usual credential, account, domain and email checks, and
      so does every member request that selects the auth-proxy branch;
      API key, Hawk and Bearer requests are checked earlier
      (`auth/decorators.py:31-95` against `:104-116`) and do not reach it;
    - G-DEF-1 (inferred from source, not executed): for a login that is
      not in `ADMIN_USERS`, when a request selects the auth-proxy branch
      with `CUSTOMER_VIEWS` on and the user is in at least one group, Group
      objects are bound as SQL parameters, predicting a 500
      (`auth/decorators.py:104-116`, `auth/utils.py:42-49`,
      `postgres/base.py:1351-1359`). `ADMIN_USERS` logins return `*` before
      any SQL, and earlier authentication paths bypass the branch;
    - C-SEC-1 (source-confirmed by both agents, static, not executed):
      `POST` and `PUT /customer` need only `admin:customers`, and nothing
      compares the mapped customer with the caller's own customers. So a
      customer-scoped holder of `admin:customers` can map a name that a
      later lookup will match for them to another customer, or to `*`,
      which removes customer filtering. With `CUSTOMER_VIEWS` on, which
      name matches depends on the login path: login, group names and
      domain at password login (sealed into the token until it expires),
      email and domain for HTTP Basic, and the auth proxy hits G-DEF-1.
      `ADMIN_USERS` logins already see all customers. It takes effect at
      the next successful matching lookup.
    Decide alrust's behavior (for example whether group names act as roles,
    and whether role, group, permission and customer-mapping edits share
    one ceiling) and check whether production grants `admin:groups`,
    `admin:perms` or `admin:customers` apart from `admin`;
  - **new, SECURITY (management; source-level, source-confirmed by
    Codex, nothing executed; M-SEC-1):** `GET /management/properties`
    (`admin:management`) prints the whole `app.config` and the process
    environment, including `SECRET_KEY`, `DATABASE_URL` (with any
    password), `OAUTH2_CLIENT_SECRET`, `LDAP_BIND_PASSWORD` and
    `SMTP_PASSWORD` (`management/views.py:82-101`). Bearer tokens are
    HS256 JWTs signed with `SECRET_KEY`, and their `scope` claim becomes the
    caller's scopes without a fresh user lookup (`models/token.py:43-76`,
    `auth/decorators.py:69-91`). So a reader of that endpoint can mint a
    token with any scopes and customers: `admin:management` confers full
    admin. Under `AUTH_REQUIRED` false, or with `ALLOW_READONLY` on (AN1),
    the endpoint is reached without credentials and without a scope check,
    subject to the usual earlier credential branches: an inferred
    anonymous-to-admin chain. Separately, `SECRET_KEY` defaults to
    `changeme` (`settings.py:15`) and startup does not require changing it,
    so with the default key anyone who knows the expected token audience can
    mint tokens. Decide whether alrust exposes any such dump, and check that
    production sets a real `SECRET_KEY` and does not enable
    `ALLOW_READONLY`. This is a source trace, not an executed exploit or a
    statement about deployed settings;
  - **new, SECURITY (web UI and prometheus; source-level, Codex-confirmed
    with qualifiers; no browser, HTTP, stress or exploit run):**
    - WUI-SEC-1: the pinned web UI (`2687e4af`) renders fields as raw HTML
      with `v-html` in nine places: alert `value`, `text` and custom
      attribute columns (`AlertList.vue:153,162,180`), history text and
      string attributes containing `http://` or `https://`
      (`AlertDetail.vue:582,730`), and group member name, email and login
      (`GroupList.vue:53-54,84-85`). No sanitizer was found in its `src`.
      Anyone who can write those fields (alert and webhook writers,
      anonymous callers under `AUTH_REQUIRED` false or AN1, user admins)
      controls stored HTML shown to operators: a potential
      script-execution risk. The prometheus mapping itself builds
      `moreInfo` as an unescaped HTML anchor. Decide the mitigation before
      any deployment of this UI (it bears on ADR-0004);
    - WP-SEC-1: the prometheus webhook runs each label and annotation
      through `str.format` with payload-controlled format specifications,
      so a large field width makes the server allocate a large string; the
      pinned settings set no `MAX_CONTENT_LENGTH`, and deployment limits are
      not verified. Decide whether alrust keeps format expansion;
  - **new, SECURITY and DATA LOSS (webhooks; source-level, source-confirmed
    by Codex; no webhook HTTP, SQL, auth or exploit run. Codex ran selected
    original-handler AST/mock scenarios, WB-S1..S6, for control flow
    only):**
    - W-SEC-1: the webhooks blueprint's `before_request`
      (`webhooks/__init__.py:17-24`) runs before authentication for every
      method and logs, at INFO, the full URL (including any `?api-key=`),
      **all request headers** (including `Authorization` and `X-API-Key`)
      and the whole body. With the default `LOG_LEVEL` `WARNING` it is not
      emitted; with an effective level of INFO or lower, or `DEBUG` on, API
      keys, bearer tokens and payloads reach the application log. Decide
      whether alrust logs any request credentials, and check production's
      effective log level (the production `alertad.conf` is a PIN input);
    - W-DL-1: when one webhook call yields several alerts, they are
      processed one by one. The first that is blacked out, a heartbeat,
      rate-limited, rejected or fails ends the request: alerts before it are
      already stored, **alerts after it are never processed**, and the
      response describes only the stopping alert. Decide whether alrust
      keeps this (a data-loss contract decision). This is handler control
      flow from source and mocks (Codex WB-S5), not executed database or
      rollback evidence;
  - **new, SECURITY (API keys; source-level, nothing executed):**
    - an edit that omits `expireTime` stores NULL, and every read rebuilds
      it as now + `API_KEY_EXPIRE_DAYS` (default 365), so with a positive
      setting the key **never expires**;
    - `scopes: []` on update, or an unrecognised `type` on create for a
      non-admin key user, passes the scope-ceiling check but reads back as
      the default read and write;
    - every serialized ApiKey entity (create, get, update, list) includes
      the secret and an `href` built from it, and keys can be looked up by
      secret (a delete returns only `{status: ok}`);
    - without a login (AN1 branch), the key user filter is dropped.
    Decide alrust's behavior and check production keys;
  - **new, alert behavior:** the reference `q=` search keeps only the first
    two operands of any flat same-operator chain, silently. `disk full
    server` searches only `disk` OR `full`; `a AND b AND c` ignores `c`
    (`queryparser.py:20-26`; confirmed by both agents' parser probes).
    Should alrust reproduce this for compatibility or search every operand?
    This changes which alerts operators see. Not blocking Phase 0 source
    work;
  - **new, not blocking:** whether alrust keeps the reference's App Engine
    cron auth branch. With `AUTH_REQUIRED` on, no credentials and
    `ALLOW_READONLY` off, a request with a truthy (non-empty)
    `X-Appengine-Cron` header and `X-Forwarded-For: 0.1.0.1` passes the
    decorator with no scope check (`auth/decorators.py:171-173`). The
    branch sets none of the usual auth fields (`g.login`, `g.scopes`,
    `g.customers`), so what the handlers then return is NOT established
    (source-derived, NOT_RUN). It is an open question about the consumed
    API's auth contract. The spec records it without deciding, and it does
    not block Phase 0 work.
