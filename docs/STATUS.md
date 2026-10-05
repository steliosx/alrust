# Status

_Last updated: 2026-10-03T16:27Z by claude-alrust, the sole writer of this
file and DECISIONS.md. Codex sends corrections through the mailbox._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Lifecycle (receive, actions, housekeeping, heartbeats) and customer-mapping specs source-derived and reviewed. Separate scoped reference HTTP runs: smoke 2, customer 4, operator 5, metadata 2, heartbeat 3, heartbeat input/query 4, metadata mutations 3, alert queries 3, HF7 scaffold smoke 2, alert notes 2, blackouts 2, API keys 1, config 1, groups 1, permissions 1, management health 1, management gtg 1, root debug 1, root index 1 (each HTTP LOCAL_PASS at its own head and window; the first metadata-mutations attempt FAILED on a harness precondition, HF9, and is preserved). OpenAPI: heartbeat, single-alert, alert-query, `q=`, notes, blackouts, API keys, users, groups, permissions, customers, `/config`, management, the root routes `/` and `/_`, and the generic webhook dispatcher and the prometheus provider mapping (reviewed). Both root routes now have one reference HTTP run each (`GET /_` and `GET /`). Remaining inventory: the other eleven provider webhook payload mappings and auth, outstanding source work (priorities from the web UI consumer inventory; auth contract changes stay with the owner) | **Open.** These are separate scoped runs, not a full-suite pass. Hosted CI now executes, at scaffold scope only (fmt/clippy with no Rust tests, the reference guard, one healthcheck); no full conformance suite has run on it. OpenAPI extraction and broader vectors are outstanding |
| 1 Domain/engine/store | blocked on 0 | — |
| 2 HTTP API | blocked on 1 | — |
| 3 Auth | blocked on 2 | — |
| 4 Receivers | blocked on 3 (see PIN inputs) | — |
| 5 Ops | blocked on 4 (see PIN inputs) | — |
| 6 Tauri desktop | blocked on 2 (see PIN inputs) | — |

## Merged into `main` (2026-10-01, owner instruction, Y1)

Each PR was merged with a merge commit pinned to its approved head, after a
retarget to `main` whose diff matched the reviewed diff (patch-id). Branches
are kept.

| PR | Branch | Approved head | Merge commit |
|---|---|---|---|
| #1 | `claude/scaffold` | `e839ceaf` | `dc5e09c6` |
| #2 | `claude/docs-landing` | `fcc06d73` | `bae71b7b` (local merge; README conflict resolved to PR #2's reviewed README, blob `9b87a366`) |
| #3 | `claude/spec-lifecycle-receive` | `47e59dd8` | `507c4dab` |
| #7 | `claude/spec-customer-mapping` | `decbb78a` | `4fbccd59` |
| #8 | `claude/spec-openapi` | `56dd7c5e` | `6a2e6c03` |
| #9 | `claude/charter-sync` | `77c44f9f` | `cb740883` |
| #4 | `claude/spec-lifecycle-actions` | `cedfcdff` | `ce48b5ba` |
| #5 | `claude/spec-lifecycle-housekeeping` | `c6aecf66` | `b793b4db` |
| #6 | `claude/spec-lifecycle-heartbeats` | `84b2ed5d` | `e0b0d412` |
| #10 | `claude/adr-0004-webui` | `96268854` | `bcd60ea3` |
| #14 | `codex/harness-integration` | `2811c700` (merge of `main`, ADR-0002 reconciled; approved `69c16b55`) | `cbc0953a` |
| #15 | `codex/heartbeat-input-query` | `60d2e021` | `f0a5893a` |
| #17 | `codex/metadata-mutations-correction` | `a5ce0e12` (brings #16 `70a40be3` and its HF9 correction together; cumulative patch-id equal to the reviewed chain) | `9bfd50c5` |
| #21 | `codex/alert-query-conformance` | `9e33dc2a` | `97fa52ba` |
| #24 | `codex/scaffold-clock-correction` | `4f808c9d` | `e5374d3d` |
| #25 | `codex/alert-note-conformance` | `24594379` | `d14911b5` |
| #26 | `codex/blackout-conformance` | `0f2566d6` | `4e9e9bf2` |
| #28 | `codex/api-key-conformance` | `b1439810` | `4663acc1` |
| #29 | `codex/config-conformance` | `0c83a5a3` | `70e22433` |
| #30 | `codex/groups-conformance` | `6266212b` | `185ea9a1` |
| #31 | `codex/permissions-conformance` | `49c2ec36` | `46a72c7b` |
| #32 | `codex/management-health-conformance` | `de7e0cbc` | `efdd4d3c` |
| #33 | `codex/management-gtg-conformance` | `6f30b824` | `4b12480a` |
| #34 | `codex/root-debug-conformance` | `0dab8866` | `1efb0393` |
| #35 | `codex/root-index-conformance` | `6e2c1e4b` | `622bf03d` |
| #36 | `codex/webhook-unknown-conformance` | `83bb4058` | `51721391` |
| #37 | `claude/charter-sync` (STATUS and DECISIONS) | `310c4916` (approved `f3df2a62`) | `c8198d13` |
| #38 | `claude/charter-sync` (DECISIONS Y5) | `19ed06c2` (approved `7bc0c6bd`) | `4da8e286` |
| #39 | `claude/charter-sync` (STATUS) | `95b7fd66` (approved `8f1f5925`) | `66bbb0df` |
| #40 | `claude/spec-security-fixes` (Y3: SF-AN1, SF-CRON, SF-USERS) | `9acc9b41` (approved `654ab8a5`) | `b4cd5aca` |
| #41 | `codex/security-fixture-plan` (Codex's fixture plan for the security fixes; reviewed by Claude) | `a9479718` (approved `8089916e`) | `5e13cdce` |
| #42 | `claude/spec-security-fixes-gpc` (Y3: SF-GROUPS, SF-PERMS, SF-CUSTOMERS) | `4d7b5508` (approved `262c80de`) | `2eda00bb` |
| #43 | `codex/security-evidence-state` (Codex's offline fixture-check and evidence component; reviewed by Claude) | `4e1285b7` (approved `d497ad4f`) | `bd5b47f1` |
| #44 | `claude/charter-sync` (STATUS, DECISIONS) | `2ccdbdb8` (approved `dc31a4ab`) | `6f6f57fc` |
| #45 | `codex/security-transport` (Codex's offline security transport; reviewed by Claude) | `c36eba87` (approved `d497ad4f`) | `b5012d5c` |
| #46 | `claude/charter-sync` (DECISIONS Y6) | `23ad33ae` (approved `acd04922`) | `0fde1e86` |
| #47 | `codex/security-journal-durability` (journal directory fsync, Claude's note N4; reviewed by Claude) | `ed1b515d` (approved `0b3cf13c`) | `9a36a19e` |
| #48 | `codex/security-path-boundary` (transport dot-segment refusal, Claude's note N5; reviewed by Claude) | `06b7c404` (approved `0b3cf13c`) | `f4fe5335` |
| #49 | `claude/charter-sync` (DECISIONS Y6 amendment; STATUS) | `89ab81e0` (approved `26020446`) | `8b62d7bb` |
| #50 | `claude/spec-security-fixes-keys` (Y3: SF-KEYS, SF-NOTES, SF-BLACKOUTS) | `018ea7b7` (approved `0111ce30`) | `6fe2954b` |
| #51 | `claude/spec-keys-retained-scopes` (Y3: SF-KEYS edit ceiling, Y7) | `0b39ece7` (approved `79f4413c`) | `e048c01a` |
| #52 | `claude/charter-sync` (DECISIONS Y7; STATUS) | `135b7787` (approved `79f4413c`) | `56f303e3` |
| #53 | `claude/charter-sync` (DECISIONS Y8, Y9; STATUS) | `72473188` (approved `0d2c692f`) | `3a60dbee` |
| #54 | `claude/spec-security-fixes-mgmt` (Y3: SF-MGMT, M-SEC-1; Y8, Y9) | `655f1958` (approved `99fe8f17`) | `cc346e5f` |
| #55 | `codex/security-refusal-assertions` (Codex's offline refusal predicates for SF-AN1/SF-CRON; reviewed by Claude) | `b43001d3` (approved `8d092494`) | `6ee6f364` |
| #56 | `claude/charter-sync` (STATUS) | `41718b7e` (approved `01c183e3`) | `00438236` |
| #57 | `chatgpt/convergence-brand-20261001` (owner's PR: Convergence branding and README illustrations; owner instruction to merge after Codex review) | `077bcad5` (approved `f85ea3a8`) | `e4040810` |
| #58 | `claude/charter-sync` (STATUS) | `26abf88b` (approved `f0c40881`) | `60ab0210` |
| #59 | `claude/spec-security-fixes-query` (Y3: SF-QUERY) | `9b07f71a` (approved `39e98779`) | `01bc294c` |
| #60 | `claude/charter-sync` (DECISIONS Y10-Y12; STATUS) | `e44e9b20` (approved `98825e74`) | `3aded5a7` |
| #61 | `claude/charter-sync` (DECISIONS Y10 clarified; STATUS) | `9de6095e` (approved `a534124f`) | `1ece49da` |
| #62 | `claude/spec-security-fixes-webhooks` (Y3: SF-WEBHOOKS, SF-WEBUI; the last Y3 entries) | `41df7b84` (approved `590fab93`) | `3211f855` |
| #63 | `claude/readme-sync` (README: X5 gate, Y5 merges) | `beb25b86` (approved `590fab93`) | `22630d7d` |
| #64 | `claude/spec-openapi-webhooks-1` (OpenAPI: the eleven remaining provider webhooks) | `abfcc823` (approved `46a69454`) | `213f661c` |
| #65 | `claude/charter-sync` (DECISIONS Y13; STATUS) | `0eaef294` (approved `46a69454`) | `867a24c9` |
| #66 | `codex/security-state-assertions` (Codex's offline stored-state assertions; reviewed by Claude) | `53b257d2` (approved `09982f31`) | `8425271b` |
| #67 | `claude/charter-sync` (STATUS) | `e71d37ef` (approved `23d295cc`) | `b046c6b0` |
| #69 | `claude/spec-security-fixes-providers` (SF-PROVIDERS) | `dcee6bd4` (approved `b122d7c2`) | `424df8a5` |
| #72 | `claude/ci-phase0-plan` (Phase 0 hosted-CI plan) | `32475cb5` (approved `1adb7d49`) | `7dc202fd` |
| #73 | `codex/hosted-profile-inventory` (Codex's inert 19-module hosted profile inventory; reviewed by Claude) | `1d0313c0` (approved `d34b2af7`) | `dcc04b9f` |
| #74 | `claude/charter-sync` (DECISIONS Y14-Y16; STATUS) | `48b16d72` (approved `6f9638dc`) | `bbe0b256` |
| #68 | `claude/ci-reference-hashed-build` (Phase 0 CI step 1: hash-locked reference build, owned-process launcher) | `49630122` (approved `b503bbf8`) | `ecf7487c` |
| #75 | `claude/charter-sync` (STATUS) | `1e89b39d` (approved `da75f11a`) | `41828c09` |
| #76 | `codex/hosted-entrypoint-preflight` (Codex's inert offline profile preflight; reviewed by Claude) | `f0aefb7c` (approved by Claude `2ee1ad54`) | `f6ba1450` |
| #77 | `claude/ci-hosted-profiles-inert` (Phase 0 CI: inert preflight job, all 19 profiles NOT_RUN) | `b096801c` (approved `1022caef`) | `1186f705` |
| #78 | `codex/hosted-entrypoint-root-docs` (entrypoint root documentation, Claude's note N11; reviewed by Claude) | `3acfb167` (approved by Claude `ab35cb81`) | `d87e25cc` |
| #80 | `claude/charter-sync` (STATUS) | `5549fe45` (approved `9803c385`) | `fce66071` |
| #79 | `chatgpt/readme-evidence-20261001` (owner's PR: README checkpoint at `d87e25cc`; first under Y17) | `346baa4e` (approved `35399ea3`; Claude also approved) | `83d31ffa` |
| #81 | `claude/ci-launch-attestation-contract` (Phase 0 CI executing-stage design) | `1d840e71` (approved `35399ea3`) | `9b18cad9` |
| #82 | `claude/charter-sync` (DECISIONS Y17; STATUS) | `d2b577e0` (approved `c4fa69c3`) | `509d2a0e` |
| #83 | `claude/ci-reference-supervisor` (Phase 0 CI: subreaper supervisor and pidfd stop helper, standalone) | `43dd73c2` (approved `eb2a251f`) | `7c4c1984` |
| #84 | `claude/ci-stop-pid-range` (stop helper integer bounds, SS-R5) | `003b3ff6` (approved `5e779e8d`) | `27b9b136` |
| #85 | `claude/ci-reference-launcher` (supervised launcher, receipt custody, pre-build preflight; SS-I1 component) | `82c6b06c` (approved `0474710a`) | `85442a1f` |
| #86 | `claude/ci-observer-wiring` (observer installation and receipt wiring) | `ebf97f48` (approved `1fb61c03`) | `0e2a3228` |
| #87 | `claude/charter-sync` (STATUS) | `377e8d54` (approved `1fb61c03`) | `6e96c632` |
| #88 | `claude/ci-hosted-supervised-scaffold` (scaffold reference job in supervised mode with a proven stop) | `a5ee4c22` (approved `1fb61c03`) | `b121a31b` |
| #89 | `claude/ci-observer-fileset` (observer pinned file set and install manifest) | `04b7f2e2` (approved `4d7f167b`) | `3ee9cc45` |
| #90 | `claude/charter-sync` (STATUS) | `f9903e76` (approved `4d7f167b`) | `b109277d` |
| #91 | `claude/ci-stage-journals` (frozen core identity, stage journals, aggregation) | `a014eb43` (approved `363f0674`) | `bb6428bc` |
| #92 | `claude/ci-finalizer` (finalizer building blocks, F1) | `0d5ca967` (approved `6bf96960`) | `38a2dfbb` |
| #93 | `claude/charter-sync` (STATUS) | `cffbea38` (approved `6bf96960`) | `792db300` |
| #94 | `claude/ci-observer-activation-wiring` (`.pth` calls the explicit activation helper) | `55a839d0` (approved `061c33e5`) | `94ac035d` |
| #95 | `claude/ci-finalizer` (finalizer orchestration, upload gate, failure-only record) | `bb9c6832` (approved `5b37cb7a`) | `9730201a` |
| #96 | `claude/status-2026-10-02c` (STATUS) | `0938df1f` (approved `64047270`) | `bdbd6511` |
| #97 | `chatgpt/readme-evidence-20261002` (owner's README checkpoint, Y17) | `207af942` (approved `bdd7f4bf`) | `b8dd6271` |
| #98 | `claude/ci-launch-stage` (launch stage journal, readiness record, launch-confirm) | `effe4099` (approved `6cb8c11c`) | `0a117f6c` |
| #99 | `codex/observer-launch-interoperability` (Codex's binding reader, venv audit and closed-launch consumer) | `583cd721` (approved `3930aaef`) | `def59f18` |

After PR #10, `main` was at `bcd60ea3`; its hosted CI run [36859021523](https://github.com/steliosx/alrust/actions/runs/36859021523) completed successfully at scaffold scope (retrieved by Codex), and Codex independently verified every merge's parents and trees (`5bd706c0`). Checks then: README, STATUS, OpenAPI and the customer mapping are byte-identical to their reviewed versions; DECISIONS is charter-sync's plus only the ADR-0004 row; `spec/lifecycle.md` is the heartbeats version plus exactly the R1.2 clarification from PR #3's later approved head.

Codex's chain was merged next, in the order Codex proposed and Claude accepted (`5bd706c0`, `69c16b55`); #16 was not merged on its own, so its failed HF9 run stays a recorded failure and only its corrected form (#17) is on `main`. `main` is now at `51721391`, whose tree `5b322354` equals the final tree Codex simulated in advance; `conformance/` is byte-identical to the reviewed #36 head, and the only other change since `bcd60ea3` is the reviewed ADR-0002 wording. Codex independently audited all 16 merges: each second parent is the exact reviewed head and each tree equals its reviewed projection (`b638c8f2`). It then closed the stale draft README PR #11 and the superseded PRs #12, #13, #16, #18–#20, #22 and #23 and #27, keeping branches, reviews and the failed HF9 evidence. Hosted CI on `main` at `51721391`: [run 36862284211](https://github.com/steliosx/alrust/actions/runs/36862284211), all three jobs executed and succeeded (scaffold scope; the intermediate pushes were cancelled and give no credit). PR #14's own hosted run 36861192966 ran before its retarget, on a merge with the old base whose tree equals the approved one (`9eb398d5`). The merged harness and vectors are source and offline results; the shared-VM runs, the pending unknown-webhook reference HTTP run and the full conformance suite remain NOT_RUN.

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
| `claude/spec-lifecycle-receive` | claude | `47e59dd84b742e0acc543af3b233b7c00a8ae760` (pushed, PR #3) | Codex: **APPROVE** for its source-derived scope (receive slice only). HTTP beyond the 2 smoke cases is NOT_RUN. At `47e59dd8` (R1.2 states the reject-policy matching: `ORIGIN_BLACKLIST` uses start-anchored `re.match`, `ALLOWED_ENVIRONMENTS` uses `re.fullmatch`, `reject.py:26,30`): Codex **APPROVE_EXACT_SOURCE** (`37e5ed9b`); pushed 2026-10-01. Earlier: `a1de491c` APPROVE. The PR #3 bot finding asking for prefix matching is a false positive (both agents; Codex `1ca8f136`) |
| `claude/spec-lifecycle-actions` (on receive) | claude | `cedfcdff5afb8198bfb3588836156ca8510d076c` | Codex: **APPROVE** for its source-derived scope (A0–A6) |
| `claude/spec-lifecycle-housekeeping` (on actions) | claude | `c6aecf66d55d476c2ff936588e2f68d9a9ed9648` | Codex: **APPROVE** for its source-derived scope (K0–K5). Deletion semantics preserved |
| `claude/spec-lifecycle-heartbeats` (on housekeeping) | claude | `84b2ed5de5b15d0baac5b24a423f8f1c23cb4ad1` | Codex: **APPROVE** for the source-derived cumulative lifecycle (OA-1 through OA-6 closed). R1.3 and OA-5/A5.2 are corroborated at runtime by the metadata run (`678f1996`, window released `0f72c944`). The other exception, custom-plugin and receive-status-change edges are runtime NOT_RUN |
| `claude/charter-sync` | claude | this head | Latest Codex review: **APPROVE_EXACT_SOURCE** at `8a39beb0` (`9fe6ef35`), pushed to PR #9 on 2026-10-01; then `77c44f9f` APPROVE_EXACT_SOURCE (`860317a7`), pushed. PR #10, based on this branch, was rechecked by both agents after each base move: the three-dot diff is byte-identical (sha256 `a5f4bb02…`) and it merges cleanly (tree `0c03ca2b` at base `77c44f9f`). GitHub still reported PR #10's old base, so its earlier green run is not counted for the new merge. Earlier: `e4d93a84` APPROVE_EXACT_SOURCE (`e7f28f1e`; ST46-C1 closed). `bf335820` (sent `bd4ec196`) had no verdict and is superseded by this cumulative head, which also disposes the two PR #9 findings (`be317dc7`: ADR-0001/0002 status lines match DECISIONS; the clock policy leaves the open questions). Earlier: `4606baa0` REQUEST_CHANGES (`83fc788d`), `5b398dee` (`79eedab2`), `a83ae89d` (`73678f6a`), `6660315b` (`e325e836`), `812a3b8a` (`fd8f459b`), `0e74a247` (`2f2ae4cc`), `f70c9640` (`854f909c`), `38084c4a` (`14836275`), `999c1a5a` (`7adbbc6c`), `090233cf` (`a8e348a1`), `0313d437` (`20a101a0`), `48b11e3c` (`136ef825`), `10bed7e8` (`c7c629aa`; ST29-C1: checking production exposure is the owner's, under the production-access gate, not an agent task). This head needs its own review |
| `claude/spec-openapi` (on `e839ceaf`) | claude | `56dd7c5ea6ba38ae81f7e424973577f2ffe98c7b` (pushed 2026-10-01, PR #8; threads answered, title and description updated) | Codex **APPROVE_EXACT_SOURCE** at `56dd7c5e` (`c38069b5`; OA59-R1/R2 closed; the three PR #8 review-bot findings disposed). Earlier: Codex **APPROVE_EXACT_SOURCE** at `fe835479` (`e52a1df4`): the `/webhooks/prometheus` provider mapping, 66 paths and 88 operations, after REQUEST_CHANGES at `efa54a7c` (`79e4e4fb`, WP-R1..R3) and `b01ba677` (`3da7763e`, WP-R4: parsing versus processing stage and side-effect scope). `59e35995` received REQUEST_CHANGES (`37e5ed9b`, OA59-R1/R2), corrected in `56dd7c5e`. Earlier: Codex **APPROVE_EXACT_SOURCE** at `55057e9b` (`3e18a5b6`; UW-R1: every API-key-authenticated request, reads included, runs one `keys` usage UPDATE before the scope check), on `469fda22` (`84551f7c`; W-N2 closed; `x-runtime` for `/` and `/_` cite their reference runs), on `c5dcd979` (`1eaa191c`; W-C1 and W-N1 closed), after REQUEST_CHANGES at `c6bb61e9` (`74bfab71`, W-C1: the legacy retry reaches the registered error handlers). The dispatcher records W-SEC-1 and W-DL-1 (see Owner); the other eleven provider payload mappings are not specified yet. Root wording: `21a8c70d` (`1ad97bdf`; ROOT-C1..C3, then ROOT-C4 on `c6bb61e9`: links are absolute only with an empty or absolute `BASE_URL`, and the `/` rule's own href doubles its slash when the base already ends in `/`). Earlier: root routes `84c9cf96` (`721cd72a`), on `6066d413` (`6ade954c`), after REQUEST_CHANGES at `24aab639` (`c640040e`, MG-R1..R3). `/` returns the full route map without credentials (source-derived). Earlier: `/config` `773d5c27` (`efcd64f7`, CF-R1) and `f667fd4f` (`10e0fac5`, CF-R2); customers `d1fdb7a8` (`702587fc`, CU-R1..R3); groups and permissions `08d751a9`, `12631144`, `593f511e`. `/config` cites the config window and management health the health window, each for exactly what it checked. Records G-SEC-1, P-SEC-1, C-SEC-1, M-SEC-1, G-DEF-1 and GR-R1 (see Owner). SQL, other HTTP, auth and validator NOT_RUN |
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
| `claude/ci-x64-runner` (on `e839ceaf`, local, never pushed) | claude | `10a854990cf9a3c2754db9afb212adeac0f7b7a3` | Draft Linux x64 self-hosted routing and the job-side adapter for runner instance I3. Lineage and verdicts: `a50af152`; `0424cf6c` REQUEST_CHANGES (`6405ce47`: lock identity, cleanup containment, native PG isolation); `8edd85ef` REQUEST_CHANGES (`e8e39ba5`: E1 descendant escape, E2 unbound smoke readiness, reproduced offline); `4497c8e3` and `891d01cc` (E1/E2/E4 fixed; TERM-ignoring leader and zombie accounting); `891d01cc` REQUEST_CHANGES (`64ac6e1e`: E5, observation errors counted as quiet); `e7b1db46`: Codex closed E5 at exact source (`33af1858`; E1, E2, E4 closed at their tested scope). Then `90648afc` and `26378de1` (sent `b69b7a99`; Codex **HOLD_INTEGRATION**, `6a4622bb`, pending the broker), and `e6f80398` (sent `695c8f81`; Codex **APPROVE_COMPONENT_SOURCE_LOCAL** for the InvocationID comparison, `70368a9a`, with 4 independent real user-unit cases; the whole adapter stays HOLD_INTEGRATION): the job no longer takes any lock; it first checks, via the scheduler's read-only status socket, that its own unit is the active lifecycle (project alrust, phase running, quarantine false, and the scheduler's InvocationID equal to the manager's current one for this unit) and fails closed otherwise; this is defence in depth, never the admission authority; `scope-run.py` records `observation_error`. Evidence is offline or local only (real transient systemd user units, a fake scheduler socket, stub PostgreSQL binaries): 13 check cases and 6 end-to-end cases. **No job has run on the VM**. Then `2bb40a61` (X5 billing-check routing): Codex **REQUEST_CHANGES** (`2c252d7b`): X5-R1 (P1), the selector was itself a hosted job, so it could not run once the allowance was spent; X5-R2 (P2), malformed numbers (NaN, infinity, negatives) selected hosted and a huge integer raised (reproduced by Claude with Codex's probe: 2 pass, 10 fail). The owner then amended X5 (see DECISIONS). `10b57d27` (sent for review) is intended to work as follows: `ci.yml` keeps the hosted jobs with no selector and no secret; `ci-selfhosted.yml` starts with a job on the VM that checks the architecture and its shared-ci lifecycle, then watches the hosted run for the same commit and event with `GITHUB_TOKEN` (actions: read), and runs the self-hosted jobs unless that hosted run is seen executing a step. Refusal, errors, malformed or non-finite data, missing inputs and a 5-minute deadline are all meant to run the VM. The billing selector is removed, which retires X5-R2. Not verified: how GitHub reports a billing-refused run through this API (assumed: completed, no steps), whether mixed hosted and self-hosted workflows behave as expected, and any live run. Offline tests only: 15 cases against a fake Actions API on Python 3.14; on 3.12 the two workflow-shape cases skip for lack of PyYAML. Before Phase B the self-hosted workflow waits in the queue, which is not validation. `10b57d27`: Codex **REQUEST_CHANGES** (`445b4f8b`): X5-R1 closed at source (the selector is on the VM), X5-R2 retired by removal; new X5-R3 (P1, one started hosted step skipped all VM jobs), X5-R4 (P1, head sha and event did not bind the PR, base or tested merge), X5-R5 (P2, a valid prefix skipped validating the rest). Codex checked three real runs through the jobs API: the two billing-refused runs (36632329142, 36768253429) are completed failures with three jobs and no steps; run 36814564373 is a completed success. `79d3f4f9` (sent for review) is intended to close them: the hosted workflow's run name binds the event, tested commit, PR number and base, and the VM requires an exact match; each VM job is skipped only if its hosted counterpart executed, a hosted failure stays a failure and is not re-run; every run, job and step read is validated before any skip. Offline tests only (30 on Python 3.14; on 3.12 three workflow-shape cases skip for lack of PyYAML). Unverified: that both workflows of one event see the same `github.sha`, and any live run. `79d3f4f9`: Codex **REQUEST_CHANGES** (`4940687e`): X5-R4 closed at source (the run-name binding is accepted; live qualification NOT_RUN); X5-R3 open (a successful job without evidence that its own checks ran still skipped the VM); X5-R5 open (contradictory records were accepted). `adfa778c` (sent for review) is intended to close both: a successful hosted job counts only if every one of its own steps completed successfully with execution times, a failed one only if one of its own steps failed with execution times, and contradictory run, job or step records run every VM job. Offline: 32 tests; Codex's coverage probe 14 of 14. `adfa778c`: Codex **APPROVE_COMPONENT_SOURCE_OFFLINE** (`f3df2a62`; X5-R3 and R5 closed; X5-R4 closed since `79d3f4f9`; the whole adapter stays HOLD_INTEGRATION; the live same-event binding is NOT_RUN). `10a85499` closes AX64-G1 for the self-hosted path: each smoke run builds a fresh virtual environment (never reused, removed on exit, CPython 3.12 only) from hash locks (`.github/reference-build.lock`, 3 entries; `.github/reference-requirements.lock`, the 34-entry closure of the reference's requirements) with `--require-hashes --no-deps --no-cache-dir`; Codex **APPROVE_COMPONENT_SOURCE_OFFLINE**, CLOSE_G1_SOURCE (`9234ea57`), after checking all 37 locked files against PyPI; both agents ran offline refusal and cleanup tests (7 of 7 in Codex's probe, replayed by Claude). Not covered: the hosted `run-reference.sh` (Codex-owned follow-up), the VM's own interpreter, pip and native build identities (to be recorded at qualification), and the full smoke with the server (NOT_RUN). The whole adapter stays HOLD_INTEGRATION |
| Shared-CI scheduler broker (own repository, not in Git here) | claude | candidate 7 `9cbe8b49349d0b690766edb64182a776b6d08495`, bundle `5b688d42…` | A root service that starts one one-job (`--once`) runner unit at a time and observes it from outside: ActiveState, InvocationID, cgroup and `Runner.Worker`. Every unit is reconciled (inactive or failed, and quiet) before each start and on recovery; other units must stay idle and quiet on recovery and on every tick of a lifecycle; the lifecycle is written to disk before the start request; the InvocationID is rechecked while the unit is live; after each lifecycle it verifies quiet with the unit still idle. Observation and control failures, malformed output, partial cgroup walks, residue, unexpected states, stop timeouts and internal errors set a shared quarantine. The quarantine marker is read tri-state, and since c7 an inspection failure is latched in the state file (fail-stop if it cannot be saved) and becomes a QUARANTINE record once the marker is readable, so only the owner's clearance resumes work; while frozen, stops are re-sent every tick. It holds the shared and legacy locks permanently, with no lending, and checks both identities. The status socket is read-only with a total request deadline; pause, resume and clear are on a root-only socket, and clear needs an actor, evidence and a Codex disposition. Evidence: 59 offline tests (author-executed, unfiltered captures retained), passing on Python 3.14 and 3.12.14; the 6 new C5-R1 tests fail on c6, and Codex's own marker-transition probe stops on c7 at its first defect assertion (the defect no longer reproduces). This is offline evidence only; no claim that every failure path is now covered. Review history: c1 `1b26fd15` REQUEST_CHANGES (`6a4622bb`, C1-R1..R4); c2 `f6bf4ffa` superseded before review; c3 `1ac31b28` superseded before review; c4 `8d33644b` REQUEST_CHANGES_CODE (`e4e67b85`, C4-R1; the C1 cases closed offline); c5 `934a0878` REQUEST_CHANGES_CODE (`c38069b5`, C5-R1: a temporary marker error was not latched); c6 `1b1c1d75` REQUEST_CHANGES_CODE (`c100371e`; codex-omarchy's WP3-05-LOCAL-PORT closed, C5-R1 persisted). c7 `9cbe8b49`: Codex **APPROVE_CODE_SOURCE_OFFLINE** (`9fe6ef35`; C5-R1 closed; no outstanding reproduced source blocker at this scope; 59 supplied tests, a 22-case replay, 6 marker-transition and 5 latch-persistence cases, all with fake services and real files). Claude replayed Codex's corrected and new probes unmodified against c7: 11 of 11 PASS_OFFLINE (accepted, `860317a7`). codex-omarchy found c7 compatible from Zabalack's side (`bea84593`, disposition `bd988827`, relayed by the Zabalack session), so both reviewers are at c7 for code-offline scope. This is not deployment or shared-VM qualification. Open, not fixable in this process (C1-R4): broker death releases the locks before the runners finish stopping, so Phase B installs only after Zabalack's C6 retires the in-job flock and the legacy path is root-only; installed units, ownership, `--once`/`--disableupdate`, BindsTo/stop ordering and rollback are provisioning gates. Named in plan r3 (`9a45c1ff`) as the Phase B broker. **Not installed**; installing it is an owner decision |
| `claude/docs-landing` → [PR #2](https://github.com/steliosx/alrust/pull/2) (on `main`) | claude | `fcc06d7301a2048ab6fc4c73a80f5874dd3d08f4` | README only (ALRUST-DOCS-LANDING-01). Codex: **APPROVE** at `fcc06d73` (LD1–LD4 closed; `9409fde5` REQUEST_CHANGES is historical). **Marked ready for owner review**, not merged. Hosted checks for PR #2: NOT_RUN (no workflow on `main`). **The merge is the owner's** |
| Codex publication, `codex/*` PRs #12–#36 | codex | 25 branch heads (index in `87c554b3`) | Published 2026-10-01 under X8 (the owner's direct instruction to Codex), one atomic non-forced push of the exact reviewed heads, one PR per branch. 22 ready; 3 draft with stated limitations (#12 foundation, #16 metadata mutations with the retained failed HF9 run, #18 receive clock). #14 is the cumulative harness join against corrected `claude/scaffold`. README PR #11 untouched. Codex rechecked all 25 heads and bases later the same day: no change. Readiness rests on the existing exact-source verdicts, not on GitHub reviews; all 25 hosted runs passed at scaffold scope (see Checks). Merging is the owner's |

## Checks

Hosted checks, offline checks and live HTTP runs are recorded separately. A
local pass never satisfies a hosted gate. A reference-server smoke test is
not Rust behavioral parity.

| Check | Result |
|---|---|
| Hosted CI | **Now executing, at scaffold scope.** Successful hosted runs with every job executed on ubuntu-24.04 (retrieved by Codex with logs, `c38069b5`, `c100371e`): PR #3 head `47e59dd8`, tested merge `ec7f8742`, [run 36800742794](https://github.com/steliosx/alrust/actions/runs/36800742794); PR #10 head `96268854`, tested merge `3ba2a56a`, [run 36800745052](https://github.com/steliosx/alrust/actions/runs/36800745052); and all 25 Codex PRs #12–#36 (75 jobs; for example [run 36801863518](https://github.com/steliosx/alrust/actions/runs/36801863518) for `83bb4058`). Scope: fmt/clippy with no Rust tests, the reference guard (its regression step present in 18 of the 25 Codex runs), and one `curl` healthcheck; the job named conformance is only that curl, not the suite. No inference about billing settings or the remaining allowance. PR #8 head `56dd7c5e`, tested merge `6e81d247`, [run 36805205573](https://github.com/steliosx/alrust/actions/runs/36805205573): success, same scaffold scope (retrieved by Codex, `9fe6ef35`). PR #9 head `77c44f9f`, tested merge `4c5cc50a`, [run 36814564373](https://github.com/steliosx/alrust/actions/runs/36814564373): success, same scaffold scope (`13b7bc19`). Historical, kept as failures: published PR #1 `e839ceaf`: **CI_BLOCKED_BILLING** (run 36632329142; jobs never started). The approved `fe835479` push to PR #8: run 36768253429, all three jobs with zero steps, **CI_BLOCKED_BILLING**. In Claude's session the owner authorized pushing `claude/*` review branches (2026-09-30): eight branches pushed at their reviewed heads with stacked PRs #3-#10 (18:42Z). Their runs at that time were billing-refused too (**CI_BLOCKED_BILLING**, historical), not a code result either way. Unpushed heads: hosted **NOT_RUN** |
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

- **GitHub Actions billing:** jobs were refused before start until 2026-09-30; on 2026-10-01 hosted jobs ran successfully. X5 governs future use: the free monthly allowance first, then the self-hosted VM, with no paid usage. X5 was amended the same day: no billing token is needed; the VM watches the hosted run instead.
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
  - **Phase A installation peer-reported (2026-10-01T16:12Z):** the
    Zabalack session reports that the v7 install completed on the shared VM
    and that runner 22 runs under the gated shared-ci unit. Opus reports
    codex-omarchy's confirmation (`20261001T161937Z-codex-omarchy-cacbd979`,
    PHASE_A_V7_INSTALLATION_CONFIRMED); the original packet and evidence
    have not been independently verified by the Alrust reviewers. Still
    untested under Phase A: an operator or upgrade window, a CI job, a
    reboot and a live rollback. Opus relayed the binding hashes only
    (envelope `ea90116d…`, disposition `86f4da27…`); the original captures
    are private evidence under the owner's rule.
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
    self-hosted VM, with a job on the VM watching the hosted run (X5 as
    amended; no billing token) and no paid usage; beta is the Linux x64 server after Phase 5; the owner merges
    the x64 workflow after Codex approval; `codex/*` follows the `claude/*`
    cadence (the owner told Codex directly on 2026-10-01).
  - **r13 (2026-10-01):** ended in shared-CI HOLD `r13-phase-failed`
    (19:49Z), not a qualification. Cause: Opus's evaluator raised a false
    STOP on a bootstrap that had succeeded. Codex chose the reviewed
    recovery path, and runner 22 was stopped until the hold was cleared.
    Peer-reported by Opus.
  - **Runner 22 back in service (2026-10-01, peer-reported by Opus):** the
    owner cleared r13's hold at 22:19:08Z (exact hold only, custody
    complete). Opus's checks then passed, and GitHub showed runner 22
    online and idle at 22:22Z. No other x64 runner is registered; Alrust has
    none. r13's preserved files are untouched until r14. codex-omarchy's
    review of the outcome is pending. Claude has no VM access, and this is
    not Alrust validation.
  - **r14 (peer-reported by Opus, 2026-10-01 and 2026-10-02):** it ran after
    codex-omarchy accepted the request and state for C6, and the owner added
    the single permission rule.
    - Attempt 1 was stopped by Opus before any host mutation (R14-RUN-01: a
      launcher output line would have broken two parsers). No window was
      taken.
    - Attempt 2 **passed every phase** at C6 `3392d399`. The window was held
      01:52:58-02:06:25Z on 2026-10-02 and released. r13's preserved state
      was proven untouched, and runner 22 is back online.
    - **C6:** adoption, both canonical builds and the freeze `cd36053b` are
      complete and confirmed by codex-omarchy. The canonical wheel equals the
      r14-tested wheel. The C6 landing (push, PR and merge) has not happened.
    - **Phase B scope:** Opus's plan to codex-omarchy shows that Phase B is
      not on Zabalack's critical path (C6 admission works through Phase A).
      If codex-omarchy agrees, Phase B serves only Alrust's I3/Y15 path and
      is sequenced as Alrust's track. Alrust's Phase 0 runs on GitHub-hosted
      runners in the meantime.
    - Claude has no VM access; none of this is Alrust validation.
  - **Shared-VM order (owner-agreed, 2026-10-01):**
    1. **done (22:19Z):** clear r13's hold and return runner 22 to service. The owner clears
       this hold directly: the preserved r13 files fail Y14's clean-state
       condition, so the owner runs one reviewed script after
       codex-omarchy's acceptance and Opus's CLEARANCE_READY checks;
    2. r14;
    3. the C6 landing;
    4. the Phase B draft, with the Y14 hold-clear script and the Y15
       announcement record;
    5. the owner's final approval.
    The clearance package is transitions S3 and S4. At the owner's request,
    Claude reviewed S3 read-only (no VM access). Its four findings: one
    medium (exit 3 could misreport a state that had already changed), fixed
    in S4 as exit 8 "CLEARED-UNSYNCED"; ssh keepalive and the wording for
    an ambiguous exit 255, both adopted; recording the file mode, deferred
    to r14 or Phase B. codex-omarchy's acceptance is pending.
  - **Owner decisions Y14-Y16 (2026-10-01):** two-agent clearance of holds
    (quarantines stay with the owner); after Phase B, broker-gated
    shared-runner jobs announced beforehand, without per-run windows; and
    per-repository runners for now (no organization move).
  - **Phase 0 in hosted CI:** the plan is merged
    (`docs/ci-phase0-conformance-plan.md`, PR #72), and so is Codex's inert
    inventory of the 19 existing HTTP modules (PR #73; all disabled, 39
    tests collected offline).
    - **Step 1** (PR #68) builds the reference from the AX64-G1 hash locks
      and launches it as its own process group with owned readiness and
      bounded, kernel-verified cleanup. Fault cases were closed offline over
      four review rounds.
    - **Hosted run [36931315785](https://github.com/steliosx/alrust/actions/runs/36931315785)**
      (tested merge `4f701f2`): all jobs executed, **PASS_SCAFFOLD_ONLY**.
      The 37 pinned distributions installed and pip check passed;
      reference 9.1.0 came up as its own process group and passed one
      healthcheck. No conformance vector ran.
    - An earlier publication of step 1 as draft PR #68 ran the changed
      build path before review (scaffold only, no vectors). It is recorded
      as a boundary breach; CI-affecting heads went by bundle until
      approved.
    - **Inert stage merged.**
      - Codex's offline preflight (PR #76, `alrust_conformance.ci`)
        answers NOT_RUN or FAIL for a profile and never PASS. Its
        documentation note N11 is merged as PR #78.
      - The CI job `hosted profiles (inert preflight, all NOT_RUN)` (PR
        #77) runs it for the 19 profiles. The manifest digest and the
        profile list are pinned, and the environment is cleared. The job
        fails unless every profile answers NOT_RUN, so a profile can be
        enabled only by its own reviewed head.
      - Hosted [run 36938712971](https://github.com/steliosx/alrust/actions/runs/36938712971)
        (tested merge `9b02c1f4`): all 4 jobs succeeded; 19 rows `rc=2
        NOT_RUN`. Retrieved independently by Codex.
        **PASS_INERT_PREFLIGHT_AND_SCAFFOLD_ONLY**; no Phase 0 credit.
    - **Executing-stage contract merged (PR #81, design only):**
      `docs/ci-launch-attestation-contract.md`. Codex approved it at
      `1d840e71` (`35399ea3`) after four rounds, with LC-R1 to LC-R5
      closed at design level only:
      - revision 1 (`d4604c0a`, REQUEST_CHANGES `6e32ba91`);
      - revision 2 (`975dba60`): LC-R2, R3 and R5 closed; LC-R1 (a reused
        process-group number does not prove the original group's lifetime)
        and LC-R4 (finalization order) open (`04ec376b`);
      - revision 3 (`5588d77e`): LC-R1 closed by a subreaper supervisor and
        pidfd-only stop with no group signals; one failure branch open
        (`10fd1913`);
      - `1d840e71`: positive writer quiescence before finalization.
      The design alone grants no execution permission. The source and
      offline implementations listed below are not executing-workflow or
      runtime-qualified components.
    - **Executing-stage components merged** (evidence for these PRs is source
      and offline; the supervised scaffold integration that followed, PR
      #88, is recorded below; the observer, profiles and finalizer remain
      NOT_RUN):
      - **PR #83:** `reference-supervisor.py`, a subreaper that only ever
        signals its own unreaped children and ends at `ECHILD`, and
        `stop-reference.py`, which signals only through a generation-checked
        pidfd. Codex approved it after two rounds: SS-R1 (a UID mismatch is
        not an exit), SS-R2 (FIFO receipts blocked), SS-R3 (a malformed
        owner) and SS-R4 (deeply nested JSON).
      - **PR #84:** SS-R5, a PID above `pid_t` overflowed `pidfd_open`.
        Found by the review bot on #83 and confirmed by Codex.
      - **PR #85:** `launch-reference.py`, the supervised launcher. It
        cross-checks the supervisor's receipt against the generation
        observed at spawn, waits for owned readiness (every listener on the
        port belongs to the tree, one on 127.0.0.1), and prints the only
        trusted receipt digest. A `--preflight` refuses unsafe job roots,
        `HOME`, venv paths, environments and `/etc/alertad.conf` before the
        build. `run-reference.sh` gains a `JOB_ROOT` mode; its legacy mode
        stayed unchanged (CI moved to JOB_ROOT mode later, in #88). Codex approved it after
        SS-I1-HOME-1 (HOME could leave the job root). SS-I1's receipt
        custody is closed at component level; whole SS-I1 integration stays
        held.
      - Evidence: 103 offline controls with real processes, stub servers
        and a build-sentinel shim, plus mutation checks. All of it is local;
        the merged CI does not run these unit tests.
      - Incidents, disclosed in the mailbox: some mutation and old-code runs
        left test processes, which were stopped by exact PID; one cleanup
        command terminated its own shell. Old-code refusal checks once ran
        real pip downloads into temp folders. Controls now use a build
        sentinel and an automatic stop fixture.
      - **Codex's session stalled** from about 03:22Z to 11:40Z on
        2026-10-02, with no fetches. It resumed when the owner asked its
        thread to continue; Codex now reviews in lane `01a0eeab`.
      - At 17:51Z on 2026-10-02, Claude's tmux observation wrongly
        suggested that Codex's reviews might be stalled. They had already
        been delivered at 17:41Z (`061c33e5`). Codex sent a correction
        (`75b38bd2`); Claude now checks its inbox before reading anything
        into an idle pane.
    - **First hosted run of the supervised path:**
      - PR #88 moved the scaffold reference job to the merged supervised
        path: a private job root, `env -i`, a masked random signing key,
        port 18081, exactly one launcher ready line, and an always-run
        pidfd stop that must report COMPLETE with no listener left.
      - [Run 37009548848](https://github.com/steliosx/alrust/actions/runs/37009548848)
        (tested merge `9602c8a8`, same tree as main `b121a31b`): the fresh
        JOB_ROOT build, the single ready line, healthcheck OK, stop
        `tree_empty` COMPLETE, and listener inspection empty. The complete job log
        contains no orphan-termination message (this is not a host-wide
        process inventory).
      - Codex audited it independently (`f4da5fc1`):
        **PASS_SCAFFOLD_SUPERVISED**. One healthcheck only: no observer,
        collection, finalizer or vectors; no Phase 0 credit.
    - **Observer components** (source and offline; not wired into any
      workflow):
      - Claude's, merged: PR #86 (installation and receipt wiring), PR #89
        (pinned two-file set: producer plus codec, an install manifest,
        read-back before spawn) and PR #94 (the one-line `.pth` activator
        calls the helper's explicit `activate_from_environment()` only when
        the supervisor set the socket variable, which it does for the served
        child alone). #94 is synthetic wiring (stubs with the helper's shape),
        approved as `APPROVE_COMPONENT_SOURCE_OFFLINE_SYNTHETIC_PTH_WIRING`.
        AN-1 is covered by a control: site.py prints and skips an exception
        from a `.pth` line, so a failed activation leaves the server ready
        without an observer. Server readiness never proves an active
        observer; a missing or refused observer must fail qualification.
      - Codex's, merged as PR #99 (six commits on `bdbd6511`, merged at
        `def59f18`, tree `2ecff083`): the read-only binding reader
        (`10813fa4`, tests `5380e251`), the venv interpreter-alias audit
        (`2a3a98ee`, tests `90b77847`) and the closed-launch consumer
        (`62e381c9`), with `583cd721` fixing CL62-R1. That finding: the
        reader expected the observer socket under `reference/`, while the
        launcher puts it under `observer/`. It was found by Claude's
        producer/consumer probe; Claude's earlier approval of the reader
        had missed it. `583cd721` adds an opt-in real-launcher control,
        and CL62-T1 (the confirmation journal's shape) is closed.
        - Hosted runs, each under a signed window: PR run 37123056301
          (W1, HOSTED_PASS_SCAFFOLD_SUPERVISED_ONLY) and main-push run
          37132740751 (MAIN-PUSH-99-W2, success). Scaffold scope only.
      - Codex's, **approved by Claude as components but not yet
        published** (Codex publishes its own branches):
        - `02d4a084`: protocol codec;
        - `9422c4d4`: private Unix client (successor of `60de7e70`, OC-N1);
        - `0a616f05`: inert snapshot projections;
        - `3729ff7f`: prepared observation window;
        - `5119a2fb`: marked capture receiver and cache;
        - `1d31647c`: bounded socket worker;
        - `a099170d`: parent-gated explicit activation helper;
        - `1b6c3065`: read-only installed-file audit (file scope only, not
          process qualification);
        - `91e31660`: runtime origin declarations in the package frame
          (actual prefix, executable alias, bound receiver and codec
          origins, and an inventory of loaded `alrust_*` modules). These
          are private corroboration, never expected values or a trust root;
          tests-only `e2e230f2` closed RO91-N1 and N2;
        - `7f6050d8`: the held process pair and executable check (after
          PA53-R1 and T1);
        - `cb1de911`: the read-only original-deadline property on the
          observation window;
        - `71dc4763`: the binding plan (design document).
        Each was reviewed with synthetic controls only. No real reference
        run and no external process and interpreter qualifier exist yet.
        Ownership (agreed in the mailbox): Codex owns the producer and the
        external adapter; Claude owns the launcher, database and finalizer
        stages.
      - Database observation: the direction is agreed (a nonce-marked
        health request; a contained read-only identity query on the live
        serving connection, preserving its transaction with a savepoint).
        Not implemented; database qualification and profile enabling stay
        held.
    - **Stage journals and finalizer** (Claude, source and offline):
      - PR #91: the frozen core identity, one hash-chained journal per stage,
        and `aggregate()` (VALID only when every stage COMPLETED; FAILED for
        any failed stage; INCOMPLETE otherwise). Approved after three rounds
        (JR542-R1, R2, R2a, R2b).
      - PR #92, finalizer building blocks (credential registry, final-byte
        scanner, descriptor-anchored removal), approved after
        REQUEST_CHANGES on `46567876` (F1465-R1 to R3, including an
        ancestor-swap deletion escape that Codex reproduced on its own
        synthetic files).
      - PR #95, the finalizer orchestrator, approved at `bb9c6832` after
        three rounds:
        - `fe89e9d5` was refused (F2FE89-R1 to R5 and DOC1). The gate
          trusted the stop receipt's labels and sealed while the
          supervisor's PID was alive; a scanned file could change before
          hashing; the upload gate accepted an invented manifest;
        - `213eb1ab` was refused (R4a: a failure record with missing
          journals or no stop verdict; F2213-R6: disk errors escaped as
          exceptions);
        - at `bb9c6832` the gate positively observes that the supervisor
          generation ended (pidfd and `/proc` start_ticks, same boot, no
          signal) before any registry or candidate read. Each export file
          is read once and scanned and hashed from the same bytes.
          `verify_manifest` requires the sealed digest. The failure-only
          record needs every closed input, including exactly one linked
          stop verdict. Every outcome after the gate is a fixed report.
        Not run: actual stage writers, database drop receipt linkage, upload
        byte custody, the executing workflow, the reference observer, Rust
        parity and shared-VM qualification. No workflow calls the
        finalizer.
      - PR #98, the launch stage: an opt-in stage mode in
        `launch-reference.py` journals owned resources, the receipt
        digest and a readiness record (both generations, C's executable,
        the venv `python3`) before the ready line. The stage closes before
        the hand-off, and a close failure stops the tree (LB80-R1).
        Because COMPLETE bytes can survive a failed fsync or close
        (LB4E-R2), a COMPLETE launch journal is only a syntactic record:
        the new `launch-confirm` stage (`confirm-launch.py`) proves the
        hand-off by observing the receipt's exact generations alive.
        Approved at `effe4099` after `b80c831b` and `4ec44fc3` were
        refused. The evidence count is 35 x3 (the commit message's "38" is
        a miscount).
      - Database stage (Claude): design approved at `87653d31` (DBC023-R1
        and R2 closed at design scope; N3 and N4 for the code). The
        implementation is in review, bundle-only:
        - part A (create and drop state machines, SCRAM verifier, bounded
          psql);
        - part B (writer gate, no-launch gate, registry stage, frozen
          registry before a launched drop, the finalizer's database
          linkage).
        Earlier heads were refused for DB61-R1 (unbounded output), DBFE-R1
        (no entrypoint requirement), DBFE-R2 (drop before the registry was
        frozen) and DBA2-R2 (stdin written outside the deadline). All four
        are closed: Codex APPROVE_COMPONENT_SOURCE_OFFLINE at `3a0cb6db`
        (A3) and `d9ad6378` (B3) (`a276f9c1`; its own 47 and 205 focused
        tests, and the stdin control at about 0.20 s against a 200 ms
        deadline). Offline, with a stateful stub psql only: no real SQL,
        SCRAM, server, schema or log qualification, no hosted run of this
        code, and no approval of the future CLIs or workflow. Not yet
        published; publication needs its own integration check and a
        signed window.
    - **Next, in order:**
      1. Codex: the provisioner secret interface (Q1, candidate
         `6e2bfcc8` in review); then publishing the remaining approved
         lineages, each with its own merge-tree check and signed window.
      2. Claude: the stage CLIs and the executing workflow (create,
         launch, launch-confirm, entrypoint, stop, registry, drop,
         finalizer), with the first real PostgreSQL evidence from one
         hosted scaffold run under its own admission.
      3. Codex: the executing entrypoint, the provisioner secret interface
         (Q1) and the served-connection adapter (Q2, N8).
      4. Per-profile enabling heads.

      Phase 0 also needs the coverage closure and the lifecycle review.
  - **Still open for the owner:** the final Phase B approval; registering
    the runner (X3).
  - Codex's current review lane is `01a0eeab` (resumed 2026-10-02 at the
    owner's request). Check cadence: the owner set Codex's lane to a first
    check 20 minutes after resuming, then every 30 minutes. Claude runs an
    hourly mailbox check plus a 30-minute read-only tmux observation of
    Codex's session (owner, 2026-10-02). Both report only meaningful
    results.
  - Coordination on 2026-10-03:
    - Codex's lane stopped collecting its inbox from about 04:00Z to
      12:07Z, and again from about 12:33Z to 14:34Z. The owner prompted
      it both times; the mailbox, not the pane, showed the gap.
    - Hosted runs, including the main-push run a merge triggers, now each
      get a signed per-run window. Codex reads the owner's "no heavy job
      before agreed admission" instruction as covering them, and did not
      adopt a standing waiver. Claude's merges #92 to #98 had triggered
      main-push runs without one; this is recorded as coordination finding
      COORD-WINDOW-01, not retroactive admission.
    - Reported by Opus (Zabalack session) as owner decisions: runner 22
      stays held and will not be recovered; heavy laptop jobs are
      heat-gated, with no two at once until a machine-wide queue exists.
    - Shared CI security policy v1.1 adopted for Alrust, as the owner's
      first-hand decision reported by Claude (DECISIONS Y18). Codex has
      read and verified it and recorded a targeted classification; the
      adoption of its private controller supplement remains separate.
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

- **codex-alrust** (current task `01a0f3d9`): review this STATUS head. The
  broker is approved at c7 for source/offline scope; the next Alrust
  blockers, in Codex's order (`9fe6ef35`): (1) the complete Phase B package,
  with both Codex reviewers approving the same broker revision plus exact
  units, provisioning and rollback (C1-R4 open, including Zabalack's C6);
  (2) the X5 routing workflow; (3) the owner's final Phase B approval, then
  installed qualification and the both-agent submission and retrieval on the
  VM; (4) the pending unknown-webhook reference HTTP run under a fresh
  window. Since 2026-10-01 Codex pushes `codex/*` under the same cadence as
  `claude/*`, by the owner's direct instruction (X8).
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
  - repository protection: approved (Y2); applied when the owner makes the
    repository public after the beta has been tested (no paid upgrade); until
    then `main` is unprotected and the guard is not enforced;
  - production-exposure checks: deferred (Y4);
  - the security and behavior items below are DECIDED (Y3): alrust fixes
    each one. Specified and approved in `spec/security-fixes.md` so far:
    SF-AN1 (AN1), SF-CRON (App Engine cron), SF-USERS (users role ceiling
    and user creation), SF-GROUPS (G-SEC-1, GR-R1, G-DEF-1), SF-PERMS
    (P-SEC-1), SF-CUSTOMERS (C-SEC-1), SF-KEYS (the API-key items and C23; the
    secret shown only once, owner decision Y6 as amended; a key's public id
    no longer works as a credential), SF-NOTES (C21) and SF-BLACKOUTS (C22)
    (PR #50) with its edit ceiling on existing scopes (owner decision Y7,
    PR #51), SF-MGMT (M-SEC-1; owner decisions Y8 and Y9; PR #54) and
    SF-QUERY (the four string-formatted SQL constructions and the `q=`
    operand dropping; PR #59). Owner decisions Y10 (W-DL-1), Y11
    (WUI-SEC-1) and Y12 (WP-SEC-1) are recorded; SF-WEBHOOKS (W-SEC-1,
    W-DL-1, WP-SEC-1) and SF-WEBUI (WUI-SEC-1) are merged (PR #62). **Every
    Y3 item now has an approved specification.** The provider-webhook
    findings below come later under the same rule.
    Codex is building the
    harness cases from the approved entries (plan, PR #41); its offline
    evidence, transport and refusal-predicate components are merged
    (PRs #43, #45, #55); no security
    case has run.
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
  - **new, SECURITY and behavior (provider webhooks; source-level, found
    while mapping the remaining eleven providers in OpenAPI, nothing
    executed; awaiting Codex's review of that head):**
    - WG-SEC-1: Grafana `ok` notifications close up to 1,000 alerts by
      `attributes.ruleId` inside the parsing stage with **no customer
      filter**, so a customer-scoped caller can close other customers'
      alerts (`webhooks/grafana.py:114-133`);
    - WT-SEC-1: Telegram's `blackout` action creates a blackout with **no
      customer** using only `write:webhooks`, not `write:blackouts`
      (`webhooks/telegram.py:87-95`);
    - WS-AUTH-1: Slack and Telegram status actions (open, ack, close) change
      alert status with only `write:webhooks`, not `write:alerts`; PagerDuty
      does the same through its status updates. DECIDED (owner decision
      Y13): they require the normal scopes;
    - PagerDuty, Slack and Telegram find the alert by a **regular-expression
      id prefix**, so a key such as `.` matches whichever alert comes first
      within the caller's customers;
    - behavior defects: Slack drops its "not found" response and continues
      with no alert, so a recognized action fails with a 400
      `AttributeError` (an unrecognized one gives 400 `Unsupported #slack
      action` either way); `watch` and `unwatch` both untag in Slack and
      Telegram; Telegram `blackout` without an alert raises
      `UnboundLocalError`; PagerDuty applies messages partly (the W-DL-1
      pattern);
    - more stored HTML (the WUI-SEC-1 pattern, covered by Y11): Grafana
      `ruleUrl` and `imageUrl`, the CloudWatch subscription text, the
      PagerDuty history text, and Stackdriver `moreInfo`. Stackdriver also
      logs invalid documentation content whole (the W-SEC-1 pattern).
    The OpenAPI mappings are merged (PR #64). The fixes are specified in
    `spec/security-fixes.md` SF-PROVIDERS (PR #69); WS-AUTH-1 and the
    scope part of WT-SEC-1 follow owner decision Y13;
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
    DECIDED under Y3: alrust searches every operand (SF-QUERY, PR #59);
  - **App Engine cron auth branch (DECIDED under Y3, SF-CRON, PR #40):**
    alrust has no unauthenticated cron branch. In the reference, with `AUTH_REQUIRED` on, no credentials and
    `ALLOW_READONLY` off, a request with a truthy (non-empty)
    `X-Appengine-Cron` header and `X-Forwarded-For: 0.1.0.1` passes the
    decorator with no scope check (`auth/decorators.py:171-173`). The
    branch sets none of the usual auth fields (`g.login`, `g.scopes`,
    `g.customers`), so what the handlers then return is NOT established
    (source-derived, NOT_RUN).
