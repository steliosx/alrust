# Repository protection: recommendation (owner decision)

Status: **recommendation only**. Nothing here is configured. Billing, the
repository plan, and protection settings are owner decisions.

## Observed (2026-09-29, exact responses)

| API | HTTP | Body `message` | Request id | Date |
|---|---|---|---|---|
| `GET /repos/steliosx/alrust/branches/main/protection` | 403 | `Upgrade to GitHub Pro or make this repository public to enable this feature.` | `F787:19B45B:A4BD99:22267D7:6ABC3B89` | Tue, 29 Sep 2026 22:28:25 GMT |
| `GET /repos/steliosx/alrust/rulesets` | 403 | same | `8A0B:13F98F:ACDA93:23799DE:6ABC3B89` | Tue, 29 Sep 2026 22:28:25 GMT |

The `documentation_url` values were
`https://docs.github.com/rest/branches/branch-protection#get-branch-protection`
and `https://docs.github.com/rest/repos/rules#get-all-repository-rulesets`.

**Consequence:** no required checks, required reviews, or push restrictions
exist on `main`. The `reference-guard` job is **not enforced**. It is a
check that runs from the candidate's own workflow (ADR-0001), and today it
does not even run, because Actions is blocked by billing. CODEOWNERS has no
effect without protection.

## Recommendation (once the plan allows it)

A ruleset (or classic protection) on `main` should:

1. **Require pull requests.** No direct pushes and no force-pushes to `main`;
   block branch deletion.
2. **Require these status checks** from the `CI` workflow:
   `reference/ is read-only`, `fmt / clippy / test`,
   `conformance vs reference alerta`. Also require the branch to be up to
   date before merging.
3. **Dismiss stale approvals on new commits.** Approval must not carry
   forward to a new head. This mirrors the agents' charter.
4. **Require CODEOWNERS review** for `/reference/`, `/.gitmodules`,
   `/.github/scripts/`, and `/.github/workflows/`. Changes to the workflow or
   guard can disable or alter the guard itself, so they need the owner's
   review, not just a green check.
5. **Bypass list: empty.** Alternatively, the owner only, in which case
   record every use. Actions and apps get no bypass.
6. **Shared-login constraint:** both agents use the `steliosx` login. GitHub
   does not let an author approve their own PR, so a required-review rule
   cannot be satisfied by the agents' signed dispositions. Either the owner
   performs the GitHub approval, or a separate reviewer identity is created
   for codex-alrust (owner decision).

## Remaining gap even with protection

Under `pull_request`, the workflow runs from the PR's branch. A PR could
weaken the guard and pass its own weakened check. Rule 4 (owner review of
`/.github/`) closes this socially, not technically. A stronger option is to
move the guard into a workflow required by the ruleset, pinned to a ref on
`main`. That depends on the plan's features, and it is the owner's choice.
Until one of these is in place, the guard must not be described as enforced.
