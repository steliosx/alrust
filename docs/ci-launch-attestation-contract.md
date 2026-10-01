# Hosted profile launch, attestation and cleanup contract

- Status: **Proposed** (a source design for Codex's review). It changes no
  workflow and starts nothing.
- Scope: the executing stage of `docs/ci-phase0-conformance-plan.md`. It
  covers the interface between Claude's workflow (machine, database, server,
  cleanup) and Codex's executing entrypoint and adapters (provisioning,
  served-process observations, pytest, scans). It realises Codex's
  refinements to the interface split and notes N8, N9 and N10.
- A profile runs only after its own reviewed enabling head. Until then the
  merged inert job (PR #77) reports it NOT_RUN.

## 1. One profile, one fresh machine

Each enabled profile is its own job on a fresh GitHub-hosted `ubuntu-24.04`
machine. The job uses:
- a PostgreSQL 16 service container;
- the hash-locked reference from `conformance/scripts/run-reference.sh`;
- the harness from `conformance/requirements.lock` (`--require-hashes
  --only-binary=:all:`).

Disabled profiles allocate no machine resources (Codex refinement 1). No
state crosses profiles.

## 2. Run identity

The workflow builds one identity record, `run-identity.json`, from what it
observes in the job. Nothing is copied from authored declarations:

| Field | Source |
|---|---|
| `repository`, `run_id`, `run_attempt`, `job` | `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_JOB` |
| `profile` | the matrix entry, checked against the manifest by the preflight |
| `event_head_sha` | the pull request head, or the push SHA |
| `checkout_sha` | `git rev-parse HEAD` in the workspace (for PRs, the tested merge) |
| `reference_sha` | `git -C reference/alerta rev-parse HEAD`; must equal the pin `f4312727…` |
| `lock_sha256` | sha256 of the two reference locks and `conformance/requirements.lock` |
| `manifest_sha256` | sha256 of `conformance/hosted-profiles.json`; must equal the workflow pin |
| `runner_image` | `ImageOS` and `ImageVersion` |
| `postgres_image` | `docker inspect` of the service container: image id and repo digest |
| `python` | `sys.version` of the harness interpreter |

The record is passed to the entrypoint as `--run-identity <path>`. It
replaces the historical window binding: no laptop ACK is fabricated, and no
shared-runner permission is implied.

## 3. Database

- **Creation.** Per job, as the service superuser, the workflow creates a
  database `alrust_<profile>_<run_id>_<attempt>` (identifier-safe; profile
  ids are `[a-z0-9-]`, with `-` mapped to `_`). It then observes and records
  the database's `oid` from `pg_database`, `current_database()`,
  `pg_postmaster_start_time()` and `version()`.
- **Connection URL.** `DATABASE_URL` points at that database. It is private:
  never echoed and never exported in evidence.
- **Served connection (N8).** Codex's adapters bind the served connection,
  meaning the database name and oid that the serving process actually uses,
  observed after readiness. The workflow's record is the expected side of
  that comparison, never proof by itself.

## 4. Settings (N9, N10)

- **Settings file.** The workflow writes a per-job reference config file
  from the manifest: `baseline_settings`, then `settings_overrides`, plus
  `ADMIN_USERS = ['alrust-synthetic-admin']` for four-role profiles (N9). It
  passes the file with `ALERTA_SVR_CONF_FILE`. Its sha256 and the names and
  values of all non-secret settings go into the identity record.
- **Environment precedence.** The reference reads some settings from the
  environment first (`utils/config.py`, `get_config`). The launcher exports
  `AUTH_REQUIRED` and `SECRET_KEY`. So the workflow sets every such
  environment variable explicitly to the file's value, and sets no other
  `get_config` variable. A mismatch between file and environment is a
  workflow defect that fails the job before launch.
- **Signing key.** `SECRET_KEY` is random per job (32 bytes from the OS) and
  masked in the log. It is never written to evidence.
- **Port (N10).** `PORT=18081`.
- **Observed values.** The effective served config is observed by Codex's
  served-config adapter. The file is the input, not the attestation.

## 5. Launch and server identity

`run-reference.sh` (merged, PR #68) launches the server as the leader of its
own process group. It prints exactly one line, `reference alerta <version>
up on :18081 (pid <P>, process group <G>)`, and hands off only after owned
readiness.
- The workflow parses `P` and `G` from that line in the same step, and
  requires `P == G`. They are passed between steps as step outputs, never
  through a file.
- It then records:
  - `launched_utc` and a monotonic timestamp;
  - `/proc/<P>/stat` (start time in ticks since boot, used as a
    PID-reuse guard);
  - `/proc/<P>/exe`, which must be the job venv's interpreter;
  - `/proc/<P>/cmdline`.
  Environment contents are never recorded.
- The entrypoint receives `--server-pid P --server-pgid G
  --server-starttime <ticks> --target http://127.0.0.1:18081`. Before every
  request phase, the adapters re-check that P exists with the same start
  time and still owns the listener (as `run-reference.sh` does).

## 6. Capture journal

The workflow writes one private, append-only JSONL journal per job, using
Codex's `PrivateJournal` (fsync per record, in a 0700 directory). It has
one record per event:
- `db_created`;
- `config_written`;
- `server_ready`;
- `entrypoint_started` and `entrypoint_finished`;
- `server_stopped`;
- `db_dropped`;
- `cleanup_verdict`.

Each record carries the UTC time, a monotonic time, the identity record's
sha256, P, G, the database oid, and the event's own result. Codex's
entrypoint keeps its own journal for provisioning, observations and pytest
(N8: the capture time and the collector identity per snapshot).

## 7. Order inside one profile job

1. Checkout (with the submodule), then `setup-python` 3.12.
2. Preflight. The job continues only if the profile is enabled in the
   reviewed manifest; otherwise it stops as NOT_RUN before step 3.
3. Database creation and identity record.
4. Config file and environment.
5. Launch (`run-reference.sh`) and the server identity record.
6. Codex's executing entrypoint with `--run-identity`, server identity,
   `--reference-venv` and a private `--evidence-dir`.
7. **Always**, even after a failure or cancellation (`if: always()`): stop
   the server's process group, then drop the database, then the cleanup
   verdict.
8. Upload only the files the entrypoint lists in its allowlist, after its
   scans. Then the summary row.

## 8. Cleanup contract

- **Server.** A reviewed `conformance/scripts/stop-reference.sh <G>` is
  needed (a new script, its own review). It receives G only from the step
  output and refuses if G is the job shell's own group. It uses the
  launcher's stop logic: TERM to the group, then KILL; kernel group state
  PRESENT, EMPTY or UNKNOWN; complete only on EMPTY, with fixed bounds and
  no blocking wait.
- **Database.** `DROP DATABASE … WITH (FORCE)`, then a check that the oid
  is absent from `pg_database`.
- **Verdict.** COMPLETE only when both are proven. Otherwise FAILED or
  UNPROVEN, which fails the job and is never relabelled.
- The service container and the machine are discarded by GitHub afterwards.
  That is not counted as proof of per-case cleanup (Codex's inventory rule).

## 9. Outcome semantics (Codex refinement 2)

| Outcome | When |
|---|---|
| NOT_RUN | the profile is disabled, or a prerequisite fails before step 3; keeps the gate open |
| PASS | the entrypoint exits 0 (real, selected, non-empty vectors; valid attestations; privacy scans clean) **and** the cleanup verdict is COMPLETE |
| FAIL | any failure from step 3 on, including a scan, attestation or cleanup failure |
| CANCELLED | cancelled after step 3; cleanup still runs and is recorded |

## 10. Budgets

| Scope | Budget |
|---|---|
| job | 30 min |
| build and launch | 15 min (readiness 60 s inside) |
| entrypoint | per profile, from the manifest; default 10 min |
| server stop | 30 s |
| database drop | 30 s |

Exceeding a budget is FAIL, or CANCELLED if GitHub's timeout fires; cleanup
still runs.

## 11. Not covered here

- Codex's executing entrypoint, provisioner and adapters, which are Codex's
  heads.
- `stop-reference.sh`, which needs its own head and offline fault controls,
  as the launcher had.
- Per-profile enabling.
- Coverage closure and the lifecycle review, which stay separate gate
  conditions.
- The shared VM, which waits for Phase B (Y15).
