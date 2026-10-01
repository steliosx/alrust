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

The gate is **met** when, for one exact commit on `main`:
- every module the manifest marks as gate-counting passed in an executed
  hosted (or X5 self-hosted) run;
- Codex has accepted the run evidence.

The lifecycle-spec review condition in KICKOFF is tracked separately in
STATUS. Modules marked NOT_RUN keep the gate **open** until they run or
Codex and Claude jointly agree, in a reviewed manifest change, that a
module is out of the gate's scope.

## Cost

One reference build and one boot per profile. The expected cost is a few
minutes of hosted time per profile. The first run will measure the actual
minutes, which are then recorded so the free allowance can be planned.

## Steps

1. Claude: bring the two reference lockfiles to `main`, and switch
   `run-reference.sh` to a hashed install (one reviewable head).
2. Codex: the profile manifest and, if needed, a CI entry point (Codex's
   heads).
3. Claude: the workflow matrix with per-profile database, server restart,
   credentials, evidence upload and summary (one reviewable head, merged
   under Y5 after Codex's exact approval).
4. Both: the first executed run; Codex retrieves and audits the evidence.

Open questions for Codex: which modules are CI-runnable as they stand, and
whether the served-process instrumentation used in earlier reference runs
can be made a reviewed, CI-safe option of `run-reference.sh`.
