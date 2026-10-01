# Hosted profile launch, attestation and cleanup contract

- Status: **Proposed**, revision 2 (answers Codex `6e32ba91`, LC-R1 to
  LC-R5). A source design for review. It changes no workflow, starts
  nothing, and grants no execution permission or producer qualification.
- Scope: the executing stage of `docs/ci-phase0-conformance-plan.md`. It
  covers the interface between Claude's workflow and launcher (machine,
  database, server, cleanup, finalization) and Codex's executing entrypoint
  and adapters (provisioning, served-process observations, pytest, scans).
- A profile runs only after its own reviewed enabling head. Until then the
  merged inert job (PR #77) reports it NOT_RUN.
- Every component named here as new (the launch receipt, the observer hook
  wiring, `stop-reference.sh`, the stage journals, the finalizer) gets its
  own exact head and offline fault controls before any use.

## 1. One profile, one fresh machine

- Each enabled profile is its own job on a fresh GitHub-hosted
  `ubuntu-24.04` machine, with a PostgreSQL 16 service container.
- Disabled profiles allocate nothing.
- The job's private root is `$RUNNER_TEMP/alrust-job`. It is created
  exclusively with mode 0700, and its device and inode are recorded in
  stage 0. Every path the job creates lives under it, except the service
  container, which GitHub owns.

## 2. Frozen core identity (stage 0, before any allocation)

`core-identity.json` is written once and never rewritten. Its sha256 links
every later record. Fields:

| Field | Source |
|---|---|
| `repository`, `run_id`, `run_attempt`, `job`, `profile` | `GITHUB_*` and the matrix entry; profile checked by the preflight |
| `event_head_sha`, `checkout_sha` | the event payload, and `git rev-parse HEAD` in the workspace |
| `reference_sha` | `git -C reference/alerta rev-parse HEAD`; must equal the pin |
| `lock_sha256`, `manifest_sha256` | sha256 of the three lockfiles and the manifest; each must equal its workflow pin |
| `runner_image` | `ImageOS`, `ImageVersion` |
| `boot_id`, `uid` | `/proc/sys/kernel/random/boot_id`, `id -u` |
| `job_root` | path, device and inode of the private root |
| `schema` | `alrust-hosted-core/1` |

Nothing in it refers to a resource that does not exist yet.

## 3. Stage journals and stage receipts (LC-R3)

GitHub steps are separate processes, so no file descriptor survives across
them.
- Each stage writes **its own** exclusive journal,
  `journal/NN-<stage>.jsonl`, with Codex's `PrivateJournal`: created
  `O_CREAT|O_EXCL`, fsync per record, unchanged.
- **First record** of each stage: the core identity sha256, the previous
  stage journal's final sha256 (or null for stage 0), and the stage name.
- **Last record:** `stage_complete` with the stage's result.
- **Field values:** every record carries only values observed at its own
  capture. A resource that does not exist yet is an explicit `null`; no
  record is ever rewritten.
- **Capture fields:** UTC from `time.time_ns()`, `CLOCK_MONOTONIC`
  nanoseconds, `boot_id`, the capturing process's PID, and the record
  schema.
- **Aggregation.** A final aggregation step validates, without rewriting:
  - every expected stage file is present;
  - each line is a complete, newline-terminated JSON record;
  - sequence numbers are contiguous;
  - the chain of stage hashes holds;
  - each stage ends with `stage_complete`.

  A missing, truncated or failed journal is **INCOMPLETE**, never a
  success.

## 4. Typed settings and a cleared environment (LC-R2)

- **Settings file.** The reference settings file is the **single source**
  of settings values: `baseline_settings`, then `settings_overrides`, plus
  `ADMIN_USERS = ['alrust-synthetic-admin']` for four-role profiles (N9). It
  is rendered as Python literals, written exclusively in the job root and
  passed through `ALERTA_SVR_CONF_FILE`. Its sha256 and every non-secret
  value go into the config stage receipt.
- **Cleared environment.** The launcher runs under `env -i` with this
  allowlist and nothing else: `PATH` (fixed), `LANG=C.UTF-8`,
  `HOME=<job root>/home`, `DATABASE_URL`, `SECRET_KEY`, `AUTH_REQUIRED`,
  `ALERTA_SVR_CONF_FILE`, `PORT=18081` (N10) and the launcher's own
  `VENV`, `REFERENCE_PYTHON` and `READY_TIMEOUT`. No Sentry, Flask, proxy
  or other inherited variable passes.
- **Variables that `get_config` reads.** Of these, only `DATABASE_URL`,
  `SECRET_KEY` and `AUTH_REQUIRED` are set, because the launcher exports
  them. `AUTH_REQUIRED` is encoded as `True` or `False`, which `get_config`
  maps back exactly. Every other `get_config` variable is **unset**, so the
  file's typed value applies.
- **No list or None values through the environment.** No list or None
  value is ever passed through the environment: `get_config` splits on
  `,`, so `''` would become `['']`, `'[]'` would become `['[]']`, and
  `'None'` would become a string (Codex's probes, `6e32ba91`).
- **Signing key.** `SECRET_KEY` is 32 random bytes, hex-encoded, and
  masked.
- **Database URL.** `DATABASE_URL` stays private.
- **Effective values.** The effective typed values are observed only
  through the served-observer channel (section 6). The file is the input,
  never the attestation.

## 5. Launch receipt and owned generation (LC-R1)

The launcher (`run-reference.sh`, needing a new reviewed head) gains a
**launch receipt**. Immediately after `alertad` starts, before readiness,
it records into a receipt file:
- P and G (`P == G` required);
- the leader's `start_ticks` (field 22 of `/proc/P/stat`), `boot_id` and
  `uid`;
- the resolved interpreter identity: the realpath, device and inode of
  `/proc/P/exe`, compared with the realpath, device and inode of
  `$VENV/bin/python3`, never the literal path text;
- the `VENV` path, device and inode;
- `launched_utc` and the monotonic time.

The receipt file is created exclusively at a path given by the workflow and
is never reused. The single ready line adds the receipt's sha256. The
workflow passes only the receipt path and hash between steps.

**Generation check.** Before any signal, observation or stop:
- the current `boot_id` must equal the receipt's;
- a live PID P must have the same `start_ticks` and `uid`.

Any mismatch, or an unreadable value, gives **UNPROVEN** and no signal.

**`stop-reference.sh <receipt> <receipt sha256>`** (new, its own head)
first re-verifies the receipt hash, then applies the rules in this table:

| Observed | Meaning | Action |
|---|---|---|
| P alive, same generation, pgid G | our leader | TERM, then KILL, to group G, bounded |
| P absent (ESRCH), members with pgid G and the receipt uid | surviving descendants of our group. A PGID cannot be reused while any member holds it, so these are ours | TERM, then KILL, to G, bounded |
| P alive but with another `start_ticks` | the PID was reused after our group fully emptied | no signal; our group is proven EMPTY by that reuse |
| a member of G with another uid, or any unreadable value | not attributable | no signal; **UNPROVEN** |

Group state is read from the kernel as before (PRESENT, EMPTY or UNKNOWN).
The stop is complete only on EMPTY. There is no blocking wait, and no
signal is ever sent to the caller's group or to an unattributed process.

## 6. Served-observer channel

The reference stays vanilla: no edits, no altered HTTP semantics, no second
app factory, and no values derived from responses.

**Wiring (Claude's launcher).** When given `--observer <reviewed module
path> <socket dir>`, the launcher installs the reviewed observer module and
a one-line `.pth` activator into the **fresh** job venv before launch. The
activator imports the module only when `ALRUST_OBSERVER_SOCKET` is set. The
launcher also adds that one variable to the allowlist. The socket
directory is created in the job root with mode 0700.

**Observer module (Codex's component, its own head).** It runs inside the
serving process and serves a Unix socket (0600). It checks the peer's
credentials (`SO_PEERCRED`): the uid must be the job uid. It answers
newline-delimited JSON requests for:
- the effective `app.config` (typed values, secrets reported only as
  present or absent);
- the route and plugin registry;
- the serving database connection: database name, oid,
  `pg_backend_pid`, and the cluster system identifier;
- the reference package's import location, which must lie inside the
  receipt's `VENV`.

Every response carries its capture time, `boot_id`, PID and `start_ticks`.

**Client (Codex's adapter).** It verifies that the socket peer's PID equals
the receipt's P with the receipt's generation before trusting any answer.

The exact protocol and schemas are agreed in Codex's observer head.
Nothing is collected before readiness.

## 7. Database binding

- **Name.** `alrust_<profile>_<run_id>_<attempt>`, with `-` mapped to `_`.
  The profile id is at most 32 characters, the run id at most 20 digits,
  the attempt at most 3. A name over 63 bytes is **refused**, never
  truncated.
- **Database stage receipt.** Recorded after `CREATE DATABASE` as the
  service superuser:
  - the name;
  - the database `oid`;
  - the cluster identity: `system_identifier` from
    `pg_control_system()`, `pg_postmaster_start_time()` and `version()`;
  - the service container id;
  - the actual image id and `RepoDigests`, from `docker inspect` of the
    image the container runs, not of its tag;
  - the port mapping.
- **Served-connection check (N8).** The observer reports the served
  connection; the adapter requires its name, oid and system identifier to
  equal the receipt's.
- **Drop.** Before `DROP DATABASE … WITH (FORCE)`, the cleanup re-checks,
  on the same cluster (same system identifier and postmaster start time),
  that the name still has the receipt's oid. Any mismatch refuses the drop
  (**UNPROVEN**). After the drop, both the name and the original oid must
  be absent.

## 8. Owned-resource inventory and cleanup (LC-R4)

Every owned resource is recorded at creation, with its path, device and
inode or its identifier, in the stage receipt that created it:

| Resource | Removal and verification |
|---|---|
| reference server group and observer (in-process) | `stop-reference.sh`, section 5 |
| job database | the guarded drop, section 7 |
| fresh venv and launcher temporary directories | removed only if device and inode match the receipt; verified absent |
| settings file, `HOME`, observer socket directory | as above |
| synthetic credential files (Codex's provisioner) | kept private until finalization, then removed and verified |
| private journals and raw logs | kept until finalization, then removed after the export is sealed |

Rules:
- No glob deletion.
- No path outside the job root is touched.
- A pre-existing or unowned path is never removed.
- Discarding the machine is never counted as proof.
- Codex's per-case cleanup proof, recorded in its own journal, stays
  separate from this job teardown.

## 9. Finalization after all writers stop (LC-R4)

1. **Stop all writers.** After the stop (section 5) and the drop (section
   7), the server log, observer and journals no longer change.
2. **Scan the final bytes.** The finalizer (a new component) scans the
   final bytes of every candidate export file against **all** actual
   credentials and against generic patterns. The credentials are: the
   role keys, the keys created by tests (reported by Codex's entrypoint),
   `SECRET_KEY`, and the database password and URL.
3. **Seal.** It writes an immutable export directory (mode 0500) containing
   only allowlisted files, with a manifest of sha256 and sizes. Only that
   directory is uploaded.
4. **Provisional until then.** Until finalization succeeds, entrypoint
   success is **provisional**.
5. **PASS requires all of these:**
   - the selected vectors actually ran, non-empty and not skipped, and
     passed;
   - the attestations are current;
   - the privacy scans are clean;
   - the cleanup is COMPLETE;
   - the finalization is sealed;
   - the journal aggregation is valid.

## 10. Outcomes, deadlines and cancellation (LC-R5)

| Outcome | When |
|---|---|
| NOT_RUN | the profile is disabled, or a prerequisite fails before the database stage; keeps the gate open |
| PASS | all of section 9.5 |
| FAIL | any failure from the database stage on, including a scan, attestation, cleanup, finalization or journal failure |
| CANCELLED | cancelled or timed out after the database stage |

In every case where cleanup evidence is missing or not COMPLETE, the
result also carries `cleanup: UNPROVEN`, with no gate credit.

**Budgets.** The job has 30 minutes:

| Phase | Budget |
|---|---|
| setup and build | 15 min |
| reserved for cleanup, finalization and aggregation | 8 min |
| entrypoint | at most what is left, and its manifest budget |
| every SQL call | `statement_timeout` plus a client timeout of 30 s |
| producer and stop helpers | their own bounded limits |

`if: always()` only **schedules** cleanup. In-process traps and `finally`
blocks attempt cleanup on a handled interruption. If GitHub kills the job,
the runner dies, or outputs are missing, the result stays CANCELLED or FAIL
with cleanup UNPROVEN. No guarantee is claimed from a step that did not
execute.

## 11. Entrypoint interface (future; the merged CLI stays inert)

The interface keeps `--profile` and `--manifest-sha256`, and adds:
- `--core-identity <path>`;
- `--launch-receipt <path> --launch-receipt-sha256 <hex>`;
- `--db-receipt <path>`;
- `--observer-socket <path>`;
- `--reference-venv <path>`;
- `--evidence-dir <path>`;
- `--target http://127.0.0.1:18081`.

The records are expected bindings, not attestations or authorizations by
themselves. Codex's executing entrypoint, provisioner and adapters are its
own heads.

## 12. Order of the next heads

1. This contract (design).
2. The launcher receipt and observer wiring head (Claude), with offline
   controls: an unauthorized or caller group, generation reuse or
   mismatch, unreadable inspection, a leader gone with surviving
   descendants, TERM-ignoring members, interrupted output.
3. `stop-reference.sh` (Claude), with the same controls.
4. The observer module and adapters (Codex).
5. The stage journals, finalizer and executing workflow (Claude).
6. The executing entrypoint (Codex).
7. Per-profile enabling heads.

Coverage closure and the lifecycle review stay separate gate conditions.
The shared VM waits for Phase B (Y15).
