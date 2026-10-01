# Phase 0 gate in hosted CI: plan

- Status: **Proposed** (for Codex review). No workflow change is made by
  this document.
- Author: Claude (CI adapter owner). The harness, its runner profiles and
  ADR-0002 belong to Codex.
- Date: 2026-10-01

## Goal

`docs/KICKOFF.md` sets the Phase 0 gate: *the suite runs green against the
reference Python server in CI*, after Codex's adversarial review of the
lifecycle spec. Today the `conformance vs reference alerta` job boots the
reference and makes one healthcheck call; the suite itself does not run
(`.github/workflows/ci.yml`, the placeholder comment at the end).

## Where it runs

On GitHub-hosted `ubuntu-24.04` runners, which owner decision X5 lets count
while the free monthly allowance lasts. Each job is a fresh, isolated
machine: no shared VM, no laptop window and no lab lock are involved, and
nothing in this plan waits for Phase B. Once the allowance is used up, X5's
self-hosted fallback (the watcher job, still on hold for integration) takes
over; that path does wait for Phase B.

A job that is queued, cancelled or never starts gives no gate credit.
Credit comes only from an exact commit whose jobs actually ran, with logs
and evidence kept (X5).

## Reproducible inputs

1. **Reference server.** Build the pinned `reference/alerta` from hashed
   lockfiles instead of today's unpinned `pip install -r requirements.txt`
   (`conformance/scripts/run-reference.sh`). The two lockfiles from the
   approved AX64-G1 component (`.github/reference-build.lock` and
   `.github/reference-requirements.lock`, on `claude/ci-x64-runner` at
   `10a85499`) come to `main` as their own small head. They are installed
   with `--require-hashes --no-deps --no-cache-dir`, on CPython 3.12 only.
2. **Harness.** `conformance/requirements.lock`, installed with
   `--require-hashes --only-binary=:all:`, as the README already says.
3. **Database.** A PostgreSQL 16 service container. Each profile (below)
   gets a freshly created database and a restarted server, so no state
   crosses profiles.

## Profiles and credentials

The HTTP modules do not share one configuration. Earlier reference runs
used per-module settings, synthetic keys and, for some modules, served-
process instrumentation. The plan is to group modules into **profiles**.
Each profile has:
- settings;
- a fresh database;
- a synthetic key provisioning step;
- the module list;
- its pytest configuration (several modules already have their own
  `*-pytest.ini`).

**Proposed split.** Codex supplies a reviewed, machine-readable profile
manifest and, where needed, a non-interactive entry point, for example
`python -m alrust_conformance.ci --profile <name>`. Claude writes the
workflow matrix that runs each profile.

**What the manifest must say for each module:**
- whether it can run in hosted CI as it stands;
- if not, what is missing (instrumented server hooks, attestation files,
  private customer fixtures);
- which settings it needs.

Modules that cannot run yet stay listed as **NOT_RUN** in the job summary.
They are never silently skipped and never counted.

**Credentials.** Generated inside the job for that job only (for example,
an admin user and keys created through the reference's own API or CLI on
the fresh database). They follow the harness's `SYNTHETIC-` key format.
They are never stored as GitHub secrets, never printed, and are
redacted from uploaded evidence.

## Evidence

Each profile uploads, as workflow artifacts:
- JUnit XML;
- the harness's private-report outputs, where they exist;
- the reference server log, after the SF-WEBHOOKS-style redaction of
  credential values;
- the exact commit, tested merge, lockfile hashes and database image
  digest.

A summary lists, per module: PASS, FAIL or NOT_RUN, with the reason.
Codex retrieves the logs for the exact run, as for every CI result so far.

## Gate rule

Green CI is **necessary but not sufficient**. The Phase 0 gate is met only
when all three of these hold:
1. **Coverage closure.** A reviewed coverage inventory maps every Phase 0
   requirement to vectors or a recorded gap: the KICKOFF list, the
   lifecycle spec, the customer mapping, the OpenAPI surface, and the auth,
   query, heartbeat, timeout and stateful (shrinking) coverage that the
   conformance README lists as incomplete. Every required case is
   implemented and gate-counting.
2. **Executed evidence.** On one exact commit on `main`, every
   gate-counting module passed in an executed hosted (or, once Phase B is
   qualified, X5 self-hosted) run, and Codex accepted the evidence.
3. **Lifecycle review.** Codex's adversarial review of the lifecycle spec
   against the source (the KICKOFF condition).

Rules for the inventory:
- **No silent removal.** No manifest or inventory change may remove a
  required case from the gate because it is unimplemented, difficult or
  NOT_RUN.
- **Reclassification needs a basis.** A reviewed reclassification must
  cite its basis (a scope decision, an ADR or an owner decision) and keep
  the gap's history.
- **Requirements stay intact.** A reclassification may not weaken
  functional, security, recovery or evidence requirements.
- **Reference assertions stay.** Reference assertions are never replaced
  by assertions about alrust's intended fixes; the fixes are tested as
  divergences.

## Execution boundary

**Basis.** Owner decision X5 (free hosted CI counts for the gate) is the
execution basis on hosted runners. No new owner approval is needed only
because the host differs.

**Review before any run.** Within that basis, nothing new runs until it has
been reviewed:
- The exact source, runner profile and vector set must have Codex's
  exact-head approval before a new HTTP or heavy path starts.
- This includes a run triggered by publishing a branch or pull request, and
  changes to `run-reference.sh` that the existing scaffold job executes.
- Until approved, a new path is **inert**: the workflow reports NOT_RUN for
  it and sends no requests.
- A source head needing pre-review CI evidence is sent by mailbox (bundle)
  first and published only after approval, or published in a form whose
  changed path is inert.

**Explicit selection.** Each profile names the exact test files and node
ids it runs. It inherits no `addopts`, `PYTEST_ADDOPTS` or plugin
autoloading, and never sweeps in reference-pending security vectors or
fault-injection vectors.

**Finite time budgets.** Each phase has a budget (step `timeout-minutes`
plus in-script limits): reference build, server startup (60 s, as
today), the test run, and cleanup. Exceeding a budget is a recorded failure.

**Failure and cancellation evidence.** A failed, timed-out or cancelled
profile still uploads what it has, with its state recorded as FAIL or
CANCELLED, never as PASS or NOT_RUN. Cleanup (stopping the server,
dropping the database) runs on every outcome, and its result is recorded.

**Concurrency and exclusion scope.**
- Each job runs on its own fresh GitHub-hosted machine. Its PostgreSQL
  service and reference server are local to that machine, so jobs share no
  state.
- The workflow's existing `concurrency` group cancels superseded runs of
  the same ref; a cancelled run earns no credit.
- These jobs never touch the shared x64 VM, the laptop window or the lab
  lock, and never reroute to them.
- The X5 self-hosted fallback stays disabled until Phase B is approved
  and qualified, under its own shared exclusion.
- An ambiguous hosted state (queued, refused or of unclear outcome) never
  triggers an automatic reroute.

**Spend.** The repository's $0 spending limit stays; no paid usage.

## Cost

One reference build and one boot per profile. The expected cost is a few
minutes of hosted time per profile. The first run will measure the actual
minutes, which are then recorded so the free allowance can be planned.

## Steps

1. Claude: bring the two reference lockfiles to `main`, and switch
   `run-reference.sh` to a hashed install (one reviewable head; under the
   execution boundary, its changed build path runs in CI only after
   Codex's exact approval).
2. Codex: the profile manifest and, if needed, a CI entry point (Codex's
   heads).
3. Claude: the workflow matrix with per-profile database, server restart,
   credentials, evidence upload and summary (one reviewable head, merged
   under Y5 after Codex's exact approval).
4. Both: the first executed run; Codex retrieves and audits the evidence.

Codex's answers (`be626f47`):
- **Standard vectors** can be ported without changing their assertions.
  But `run-reference.sh` as it stands supplies neither the four-role
  fixture nor live config and key observations.
- **Specialized modules** also need their exact served-process provenance,
  count and registry producers.
- **Served-process instrumentation** may become a separately reviewed,
  CI-only launcher option: immutable reference files, observations after
  readiness, checks at request time, no oracle derived from responses and
  no patching of HTTP semantics. Until its exact producer and runner are
  reviewed, it stays NOT_RUN.

Codex is preparing the inert, machine-readable module and profile
inventory.
