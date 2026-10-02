# Hosted profile launch, attestation and cleanup contract

- Status: **Proposed**, revision 3. It answers Codex `6e32ba91` (LC-R1 to
  LC-R5) and `04ec376b` (LC-R2, R3 and R5 closed at design level; LC-R1
  original-group lifetime and LC-R4 finalization order remained). A source design for review. It changes no workflow, starts
  nothing, and grants no execution permission or producer qualification.
- Scope: the executing stage of `docs/ci-phase0-conformance-plan.md`. It
  covers the interface between Claude's workflow and launcher (machine,
  database, server, cleanup, finalization) and Codex's executing entrypoint
  and adapters (provisioning, served-process observations, pytest, scans).
- A profile runs only after its own reviewed enabling head. Until then the
  merged inert job (PR #77) reports it NOT_RUN.
- Every component named here as new (the launch receipt, the observer hook
  wiring, the supervisor and `stop-reference.py`, the stage journals, the finalizer) gets its
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
| `external_config` | `lstat('/etc/alertad.conf')`, which the pinned `Config` loads before `ALERTA_SVR_CONF_FILE`. It must be ENOENT; anything else, including an unreadable result, is a prerequisite failure (NOT_RUN). Absence is observed, never inferred from a fresh machine |
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
  `VENV`, `REFERENCE_PYTHON`, `READY_TIMEOUT` and the required `JOB_ROOT`.
  The two observer variables (`ALRUST_OBSERVER_SOCKET`,
  `ALRUST_OBSERVER_PARENT`) are **not** in this set: the supervisor adds
  them to C's environment only (section 5). No Sentry, Flask, proxy
  or other inherited variable passes.
- **Variables that `get_config` reads.** Of these, only `DATABASE_URL`,
  `SECRET_KEY` and `AUTH_REQUIRED` are set, because the launcher always
  exports them, and the environment overrides the file. Every other
  `get_config` variable is **unset**, so the file's typed value applies.
  The three are handled so that the file stays the single source:
  - `DATABASE_URL` and `SECRET_KEY` are secrets. The file is rendered
    **without** them, and the renderer refuses a manifest that sets them.
  - `AUTH_REQUIRED` stays in the file (the manifest baseline sets it). The
    workflow **derives** its environment value from the file's typed value:
    the bool `True` or `False` becomes the string `True` or `False`, which
    `get_config` maps back exactly. A non-bool value fails the job before
    launch.
- **No list or None values through the environment.** No list or None
  value is ever passed through the environment: `get_config` splits on
  `,`, so `''` would become `['']`, `'[]'` would become `['[]']`, and
  `'None'` would become a string (Codex's probes, `6e32ba91`).
- **One serving process.** `alertad run` is Flask's `run` command
  (`alerta/commands.py:30`, `FlaskGroup`; pinned Flask 3.1.3). Its reloader
  would fork a second serving process, which breaks the rule that C is the
  served process. The launcher therefore passes `--no-reload
  --no-debugger` explicitly; `FLASK_DEBUG` is outside the allowlist; and
  the renderer refuses `DEBUG=True` (the baseline sets `DEBUG: false`).
  Threads stay inside C.
- **Signing key.** `SECRET_KEY` is 32 random bytes, hex-encoded, and
  masked.
- **Database URL.** `DATABASE_URL` stays private.
- **Effective values.** The effective typed values are observed only
  through the served-observer channel (section 6). The file is the input,
  never the attestation.

## 5. Supervised launch and owned lifetime (LC-R1)

Process-group numbers do not identify a group's lifetime: Codex's
counterexample (`04ec376b`) shows that a reused group number can hold a
foreign group. This contract therefore never signals a group and never
proves cleanup from group state. Ownership comes from a supervisor that
only ever signals its own unreaped children.

**Supervisor.** `conformance/scripts/reference-supervisor.py` is Claude's
component, reviewed with the launcher head. The launcher (`run-reference.sh`,
in a new reviewed head) starts it as P, the leader of its own process
group:
- **No observer activation in P.** P runs on the job's CPython 3.12 with
  `-I -S -B`, so no `.pth` file is processed in it.
- **Subreaper.** It calls `prctl(PR_SET_CHILD_SUBREAPER, 1)` and records
  the `PR_GET_CHILD_SUBREAPER` read-back. Every orphaned descendant is
  therefore reparented to P while P lives. Descendants that change process
  group or session stay descendants.
- **Child C.** It forks C, which `exec`s the venv's `alertad`. Only C's
  environment adds `ALRUST_OBSERVER_SOCKET` and
  `ALRUST_OBSERVER_PARENT=<P>` (section 6).
- **Signals.** It signals only PIDs that are its own unreaped children: C,
  and any adopted orphan, which it finds by scanning `/proc` for
  `ppid == P`. A child's PID cannot be reused before P reaps it, and P is
  single-threaded, so each signal reaches the process it was meant for.
- **Teardown.** Teardown begins on `SIGTERM` to P or on C's exit:
  1. TERM to every child, then reap, for up to 10 s.
  2. KILL to every child, then reap, repeated until `waitpid(-1)` reports
     `ECHILD`, for up to 5 s.

  `ECHILD` means P has no children. With the subreaper set, that means P
  has no living descendants at all.
- **Terminal receipt.** P then writes the terminal stop receipt
  exclusively and exits: `tree_empty`, or `tree_not_empty` if the bounds
  expire. The receipt carries P and its `start_ticks`.

**Launch receipt**, written in two parts:
- **At launch, by P**, with values that are stable across `exec`:
  - P, G (`P == G`), P's `start_ticks`, `boot_id` and `uid`, and the
    subreaper read-back;
  - C's PID and `start_ticks`, read by P after the fork. C is P's
    unreaped child, so its PID is stable;
  - the `VENV` path, device and inode;
  - the observer module's and `.pth` activator's paths and sha256;
  - `launched_utc` and the monotonic time.
- **At readiness:**
  - C's resolved interpreter identity: the device and inode of
    `/proc/C/exe`, against the resolved `$VENV/bin/python3`. It is not
    read earlier, because C may not have `exec`ed yet.
  - The listener on 18081 must belong to C or to a descendant of P. This
    replaces the old process-group test for listener ownership.

The receipt path is under the job root. It is created exclusively and
never reused. **P writes the receipt itself**, so it is an expected binding,
not an attestation. Before printing the ready line, the launcher
cross-checks it against its own observations: `$!` must equal P, and
`/proc/$!/stat` must give the same `start_ticks`. A mismatch fails the
launch. The ready line adds the receipt's sha256.

**Stop helper** `stop-reference.py <receipt> <sha256>` (new, its own head):
1. It re-verifies the receipt hash.
2. If no terminal receipt exists yet, it calls `pidfd_open(P)` first, then reads
   `/proc/P/stat` and checks P's `start_ticks`, `uid` and `boot_id` through
   that stable handle.
   **Why this is race-free.** The pidfd refers to whichever process held
   P at the moment it was opened. `/proc/P/stat` is read by number
   afterwards. A matching `start_ticks` (with the same `boot_id`) means
   the receipt's generation still held P at that read. P's number cannot
   change hands while that process lives, so the pidfd opened earlier
   refers to it. A mismatch or an unreadable value means no signal.
3. It signals only through `pidfd_send_signal`. It never calls `kill`,
   never signals a group, and never signals a PID other than the verified
   P.

| Observed | Action | Verdict |
|---|---|---|
| a terminal receipt `tree_empty` from the receipt's P generation | none | COMPLETE |
| P live with a matching generation (via pidfd) | TERM via pidfd; poll the pidfd for exit, bounded at 20 s; then read the terminal receipt | COMPLETE only on `tree_empty`, else UNPROVEN |
| a terminal receipt `tree_not_empty` | none | FAILED |
| P absent, reused or unreadable, with no terminal receipt | none | UNPROVEN |

- **No group-number evidence.** In none of these cases does a group number,
  session id or uid count as attribution. The kernel group state of G may be
  recorded as information only, labelled as the *current* group. It is never
  evidence that the original group is empty.
- **Codex's counterexample.** The original leader is absent and a foreign
  group reuses the number. This lands in the last row: no signal, UNPROVEN.
- **P killed from outside.** If something outside kills P, its orphans
  leave our supervision. That is the same row: UNPROVEN, never relabelled.

## 6. Served-observer channel

The reference stays vanilla: no edits, no altered HTTP semantics, no second
app factory, and no values derived from responses.

**Wiring (Claude's launcher).** When given `--observer <reviewed module
path> <module sha256> <socket dir>`, the launcher first checks the module's
sha256 against the value pinned in the reviewed workflow (refusing on a
mismatch), then installs the reviewed observer module and
a one-line `.pth` activator into the **fresh** job venv before launch.
This is the only code in the serving process that does not come from the
hash-locked install, so both files' sha256 go into the launch receipt. The
activator imports the module only when `ALRUST_OBSERVER_SOCKET` is set. The
launcher does not add the observer variables to its allowlist; the
supervisor sets them in C's environment only. The socket
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

**Activation scope.**
- The module starts its server only if `ALRUST_OBSERVER_SOCKET` is set
  **and** `os.getppid()` equals `ALRUST_OBSERVER_PARENT`. Only the
  supervisor's child C satisfies both.
- Build, pip, provisioning and reference-CLI interpreters run without the
  variables, so they never become observer servers.
- Each activation is recorded with the module's sha256.

**Client (Codex's adapter).** It verifies that the socket peer
(`SO_PEERCRED`) is C with C's receipt generation before trusting any
answer.

The exact protocol and schemas are agreed in Codex's observer head.
Nothing is collected before readiness.

## 7. Database binding

- **Name.** `alrust_<profile>_<run_id>_<attempt>`, with `-` mapped to `_`.
  The profile id is at most 31 characters (the longest today has 21), the
  run id at most 20 digits, the attempt at most 3, so the name is at most
  63 bytes. A longer name is still **refused**, never truncated. Two
  profile ids that differ only by `-` and `_` are refused by the renderer,
  so the mapping cannot collide.
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
| reference server tree and observer (in-process) | the supervisor and stop helper, section 5 |
| job database | the guarded drop, section 7 |
| fresh venv and launcher temporary directories | removed only if device and inode match the receipt; verified absent |
| settings file, `HOME`, observer socket directory | as above |
| synthetic credential files (Codex's provisioner) and the credential registry | read into the finalizer's memory, then removed and verified before the terminal receipt (section 9) |
| capture journals, stage journals and raw logs | aggregated read-only, candidates copied, then removed and verified before the terminal receipt |
| launcher temporaries, `.reference.pid` and receipts | all under the job root (the new launcher head takes a required `JOB_ROOT` and refuses to write elsewhere); removed and verified like the above |
| export staging directory and the finalizer journal | **deliberately retained**: the sealed export, in custody until upload. Not counted as cleaned |

Rules:
- No glob deletion.
- No path outside the job root is touched.
- A pre-existing or unowned path is never removed.
- Discarding the machine is never counted as proof.
- Codex's per-case cleanup proof, recorded in its own journal, stays
  separate from this job teardown.

## 9. Acyclic finalization order (LC-R4)

Every proof is written after the action it attests. The final bytes are
scanned after every proof has been written. Steps:

1. **Writer quiescence, positively proven.** This is a **prerequisite**
   for steps 2 to 5. All of these must hold:
   - the entrypoint has exited and its journal is closed with
     `stage_complete`;
   - the stop helper's verdict is COMPLETE from a terminal receipt
     `tree_empty` of the receipt's P generation. P writes that receipt
     only after `ECHILD`, so every process that could write the server
     log or serve the observer has exited and its file descriptors are
     closed. The helper has also seen P itself exit through the pidfd;
   - each stage journal is closed with `stage_complete`.

   **Merely running the stop helper proves nothing.** A missing,
   `tree_not_empty` or unreadable receipt does **not** imply that the
   writers have stopped. P may have been killed from outside while
   orphans are still writing. In that branch, steps 2 to 5 do not run:
   - no mutable log, journal or credential snapshot is read, removed or
     exported;
   - no orphan or other unattributed process is signalled or guessed at;
   - the profile is FAIL with cleanup UNPROVEN, and cleanup is never
     claimed COMPLETE.

   The only output is a bounded failure-only record (section 9.1a).

   **1a. Failure-only record.** The summary step may upload one record,
   assembled only from inputs that were independently closed and are
   immutable:
   - the core identity;
   - closed stage journals that end in `stage_complete`;
   - the launch receipt, and the stop helper's own verdict.

   It holds the outcome, the stop verdict and those inputs' hashes. It
   names no log content. Before upload it is scanned completely against
   the generic patterns and against `SECRET_KEY` and the database URL,
   which the workflow holds; the registry is never built in this branch.
   If anything is unavailable, nothing is uploaded.
2. **Credential registry frozen, before the database is dropped.** A
   bounded step, with its own stage journal, writes a private registry
   (0600, exclusive) that contains:
   - every `keys.key` value in the job database
     (`alerta/sql/schema.sql:123-125`, `key text UNIQUE NOT NULL`), which
     covers the API keys
     that tests created;
   - the provisioner's role keys;
   - `SECRET_KEY`;
   - the database URL and password.

   - every other secret that Codex's entrypoint reports it obtained, for
     example a token from `/auth` or a user password.

   A registry that cannot be completed fails the profile.
3. **Database drop and verification** (section 7), in its own stage
   journal.
4. **Finalizer.** One process (Claude's component), with its own
   exclusive journal, kept distinct from the capture writers. In order:
   1. It loads the registry into memory.
   2. It validates the aggregation of every closed journal (section 3),
      read-only. The result and every stage hash stay in memory.
   3. It copies the allowlisted, bounded candidate files into the export
      staging directory (0700) and applies the entrypoint's reviewed
      redactions there. Source files are never modified.
   4. It removes, then verifies the absence of, every owned resource in
      the section 8 inventory except the staging directory and its own
      journal. This includes the registry file, the credential files, the
      raw logs and the journals already aggregated.
   5. It writes the **terminal cleanup receipt** into staging, after
      those actions. The receipt holds:
      - every removal and its verification;
      - the stop verdict and the database verdict;
      - the aggregation result with all prior stage hashes;
      - the overall cleanup verdict.

      It then appends a final record and **closes its own journal**,
      which goes into staging. The journal does not contain its own
      hash.
   6. It scans the **final bytes of every staging file**, including the
      receipt and its own closed journal, against the in-memory registry
      and the generic patterns.
   7. It builds the manifest (names, sizes, sha256) in memory, scans the
      manifest bytes, and writes it. It then marks the directory 0500 and
      the files 0400. The manifest is written **only on a clean scan**,
      and it records the scan verdict and the terminal receipt's hash. On a
      failed scan, nothing is written. That marking is a custody convention, not a security
      boundary for the same uid. Nothing writes to staging after this.
   8. It exits. The registry existed only in its memory and in the file
      it removed.
5. **Upload.** The upload step re-hashes every file against the manifest
   immediately before uploading, and uploads only the manifest's files.
   Any mismatch means no upload and FAIL.
6. **Summary.** The summary step derives the outcome only from the
   terminal receipt and the manifest. A missing manifest, which is what a
   scan failure leaves, means FAIL with no upload.

**Failures.** A failure of step 1 (no positive quiescence) or of any of
steps 2 to 5 keeps the profile FAIL, or UNPROVEN for cleanup. That includes a failed removal, scan, aggregation or
terminal write. No unscanned byte is exported. Nothing is stamped COMPLETE
before its action. No source log or journal is writable after it has been
read for a candidate.

**Before finalization**, entrypoint success is **provisional**.

**PASS requires all of these:**
- the selected vectors actually ran, non-empty and not skipped, and
  passed;
- the attestations are current;
- the terminal receipt's cleanup verdict is COMPLETE;
- the aggregation is valid;
- the final scan is clean;
- the manifest has been sealed and rechecked at upload.

**Not proven by the job's own records.** The job's identity, source and
permissions are still checked by the reviewers from GitHub's run
metadata. Caller-written JSON never authenticates them.

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
| supervisor teardown | TERM 10 s, then KILL 5 s |
| stop helper | pidfd exit poll 20 s |
| credential registry, finalizer | 60 s each, inside the 8-minute reserve |

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
2. The launcher head (Claude): the supervisor, the launch receipt and its
   cross-check, `JOB_ROOT`-only paths, the observer wiring and
   `--no-reload --no-debugger`. Offline controls cover:
   - a receipt that disagrees with `$!`;
   - a child that exits before readiness;
   - the subreaper read-back failing;
   - observer activation outside C;
   - interrupted receipt writes.
3. The supervisor and the `stop-reference.py` stop helper (Claude), with
   offline controls for:
   - the caller and an unauthorized input;
   - original-leader absence with a foreign reused group (Codex's trace);
   - unreadable inspection;
   - surviving owned descendants, including ones that moved to another
     group;
   - members that ignore TERM;
   - an outside KILL of the supervisor;
   - interrupted receipt or output writes.
4. The observer module and adapters (Codex).
5. The stage journals, finalizer and executing workflow (Claude).
6. The executing entrypoint (Codex).
7. Per-profile enabling heads.

Coverage closure and the lifecycle review stay separate gate conditions.
The shared VM waits for Phase B (Y15).
