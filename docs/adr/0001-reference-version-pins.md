# ADR-0001: Reference version pins

- Status: **Accepted.** Codex verified the pins against upstream release
  metadata and the submodule gitlinks (review `ed33e33c`: server v9.1.0,
  webui v8.7.1) and approved the guard correction at `e839ceaf`. The guard
  is **not enforced** (no repository protection; see `docs/DECISIONS.md`)
- Date: 2026-09-29

## Context

KICKOFF: pin the latest stable Alerta server and webui release at project start
as read-only submodules under `reference/`.

Resolved on 2026-09-29 from GitHub releases:

- `alerta/alerta` latest release **v9.1.0** (2026-03-28), commit
  `f4312727783dad64198b62ed778bbca98a5144a3`. Supports Python 3.9–3.12
  (`setup.py`); upstream CI tests on Postgres 13 and 16.
- `alerta/alerta-webui` latest release **v8.7.1** (2024-04-11), commit
  `2687e4afa93b41c4fa1d46201bba9ea38be0d491`.

## Decision

Pin exactly those two commits. Changing a pin needs a new ADR plus owner
approval.

`.github/scripts/reference-guard.sh` enforces the pins, and the
`reference-guard` CI job runs it on every PR and on every push to `main`. The
script requires three things:

- `reference/` is the exact tree `69fd8341…`. That fixes exactly two entries,
  each mode 160000, at the SHAs above.
- `.gitmodules` is the exact regular blob `4c688ccc…`. That fixes every byte,
  so no extra settings are allowed and path/url pairs cannot be swapped.
- The repository contains no other gitlinks.

Once the base commit contains `reference/` and `.gitmodules`, both must also
equal the base tree. An unavailable base fails the check rather than skipping
it. `.github/scripts/test-reference-guard.sh` holds the adversarial regression
cases, run on synthetic repositories.

**Limitation.** Under `pull_request`, the workflow and the guard script come
from the candidate, so a PR could edit the policy along with the pins. Such an
edit is visible in review, but no check inside the repo can block it. Full
anchoring needs branch protection or a ruleset. On 2026-09-29 GitHub returned
HTTP 403 for that API (plan upgrade required). That setting is the owner's to
make. CODEOWNERS lists the guarded paths, but it has no effect without branch
protection.

## Consequences and open questions

- **Version skew.** The webui release is two years older than the server
  release. Upstream webui `master` has 9 unreleased commits after v8.7.1
  (2026-03-28 to 2026-04-13), including:
  - `02ff632` "fix authorization endpoint for keycloak auth (#575)". This is
    relevant to Phase 3 OIDC.
  - `0421b4f` dependency bumps (#610), `f56c9b2` CI action updates (#608).
  - `75f223e`/`91ff160` ordering changes in `alerts.store.ts` / `customers.store.ts`.

  **Open (decide before `webui/` is vendored in Phase 2):** vendor from the
  v8.7.1 tag and cherry-pick #575 as a local patch, or vendor from a pinned
  `master` commit. The API contract we extract comes from the webui's actual
  calls, so the choice changes the consumed surface slightly.
- The reference server runs under Python 3.12 in CI. The local dev machine has
  3.14, where it also boots, but 3.12 is the supported target.
