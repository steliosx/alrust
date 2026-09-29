# ADR-0004: Which alerta-webui source to vendor into `webui/`

- Status: **Proposed.** It is pending Codex's compatibility and evidence
  review and a build-evidence window. If the choice changes the
  `reference/alerta-webui` pin, that is an **owner decision** (ADR-0001).
- Date: 2026-09-29
- Deadline: before the Phase 2 dependency. Vendoring happens in Phase 2.

## Candidates

| | A: tag plus one fix | B: exact upstream commit |
|---|---|---|
| Source | `v8.7.1` (`2687e4af`, 2024-04-11) plus a cherry-pick of `02ff632` (#575) | `master` @ `e6eb99e90901a32e63ee96588ac4dd8804d6831a` (2026-04-13), not a release |
| Reference pin | Unchanged (ADR-0001) | Diverges from the pinned `reference/alerta-webui`. Either the owner approves a re-pin, or the vendored tree and the reference differ |
| Commits beyond the tag | 1 | 9 (`f81a02f` … `e6eb99e`) |

## What differs (from `git diff v8.7.1 e6eb99e`, 66 files, +18587/−40540)

1. **The consumed API surface is identical.** Every change under
   `src/services/api/*` is `let` → `const` (lint). No endpoint, method,
   parameter or payload changes. The Phase 0/2 API contract extracted from
   the webui is the same for A and B.
2. **Auth:** #575 changes only the **Keycloak** provider's authorization URL.
   It removes the legacy `/auth` prefix that Keycloak ≥ 17 dropped
   (`src/services/auth.ts`). The generic **`openid`** provider takes its
   endpoint from the server's `oidc_auth_url` config and is **identical in A
   and B**. So #575 matters only if the NOC's IdP is Keycloak used through
   the Keycloak provider. Which provider alrust uses is a Phase 3 input from
   the owner (the IdP), and it is not yet known.
3. **UI behavior (B only):**
   - environment, service, group and tag lists sort case-insensitively
     (`alerts.store.ts`, #602);
   - customers sort alphabetically (`customers.store.ts`, #601).

   These are presentation changes, not API changes.
4. **Toolchain and dependencies (B only), the largest difference:**
   - axios 0.28 → **1.14** (major);
   - `@vue/cli-service` 4.5 → **5.0**;
   - TypeScript 3.9 → **4.9**;
   - `node-sass` 7 → **`sass`** (dart-sass);
   - `devServer.disableHostCheck` → `allowedHosts: 'all'`;
   - Jest/Cypress test additions (#609) and a lockfile rewrite.
5. **Docs/CI only:** #608, #580, and `e6eb99e` (the BASE_URL docs).

## Open evidence (NOT_RUN; each needs a signed heavy-build window)

- **E1, buildability on the laptop toolchain** (Node 26.8.2). A uses
  `node-sass` 7, a native addon, and Vue CLI 4. These may fail to build on
  current Node without an older Node/Python toolchain. B's dart-sass and
  Vue CLI 5 are more likely to build. Verify with `npm ci && npm run build`
  for A and B, each in an isolated copy. This is also a prerequisite for the
  Phase 6 Tauri packaging.
- **E2, dependency advisories.** Run `npm audit --omit=dev` on each lockfile.
  Record the counts only; a report is not a decision.
- **E3, runtime equivalence against the reference.** Load both builds
  against the pinned reference server and check the network calls. The
  expectation is that they are identical (point 1).

## Recommendation (for review, not decided)

Prefer **A** if E1 shows it builds reproducibly on a supported toolchain.
It keeps the reference pin, and the extra change is one line that matters
only for the Keycloak provider. If A does not build without an unsupported
toolchain, **B** becomes the practical choice. That requires the owner to
approve re-pinning `reference/alerta-webui` to `e6eb99e`, or to accept a
recorded vendored-vs-reference divergence. In either case, KICKOFF's "no
code changes beyond config" gate for the vendored webui is measured against
the chosen source, and the chosen source is recorded here.

**An unreleased commit is not automatically preferred.** B is justified
only by E1/E2 evidence, not by recency.
