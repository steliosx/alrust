# Stage-tool exit proofs: the credential-holder contract (R-EP plan P2)

Status: plan agreed in `c35732da` (P1, then P2, P3, P4); design `409eabfa`
section 6. P1 (core /2 entrypoint pins) is approved at `27aafc3d`. P2a (this
contract, the tool-exit verifier and stage, and the gates' holder check in
`tool_exit.py`) is approved at `c593b5ea`, offline source only.

P2b-1 (core /3 tool pins, required by the verifier and the holder check)
builds on P2a. P2b-2 adds the supervisor's tool mode
(`entrypoint-supervisor.py --tool NAME --stage-index N`), the pinned
stage-tool driver (`scripts/stage-tool.py`) and `run_create_stage`. P2c
adds the paths-only server secrets (section 10). P3 wires
`holder_proofs()` into the gates; P4 adds the timeout stop helper.

## 1. Rule

Every process that holds the job role's password, or a value that goes into
the credential registry, is inside a supervised tree before it holds it.
Its exit is proven by `ECHILD` and an ended supervisor generation before any
gate that relies on that holder having exited. A gate refuses while any
holder it requires lacks a COMPLETE, freshly recomputed exit verdict.

## 2. Holder inventory

| Process | Secret held | How it gets it | Lifetime | Exit proof |
|---|---|---|---|---|
| database-create driver | job role password | generates it (`secrets.token_hex`) in `create_database` | from generation to process exit; also writes the 0600 URL file | P_T run, `database-create-exit` (this contract) |
| registry driver | role password, `SECRET_KEY`, every `keys.key` value, entrypoint inventory values | reads the 0600 URL file, the `SECRET_KEY` file, the database as the role, the inventory snapshot | from the first read to process exit | P_T run, `registry-exit` (this contract) |
| psql children of either driver | role password (passfile in the 0600 database directory; registry only) | started by the driver's `Psql` | bounded by `_bounded_run`; descendants of `C_T` | covered by the driver's `P_T` (`ECHILD` over the whole tree) |
| database-drop | none | connects only as the service superuser, whose password is public | n/a | not a holder; it stays one only while that is true |
| entrypoint `C_E` | its secrets, anything it generates | `P_E` at fork | `P_E` tree | `entrypoint-exit` (approved design, P1) |
| reference server `C` | `DATABASE_URL`, `SECRET_KEY` | today: `P` at fork; agreed: `C`'s own pre-exec reader (section 10) | `P` tree | stop verdict (approved) |
| reference supervisor `P` | today: its environment, copied for `C` (`reference-supervisor.py:231`); agreed: paths only | the launcher's `Popen` (inherited environment) | until stop | stop verdict: `P`'s tree, `ECHILD`, generation ended |
| launcher | today: `DATABASE_URL`, `SECRET_KEY` in its environment; agreed: paths only | the workflow step | until it exits after the ready line; `P` outlives it by design | section 10 |
| launcher's stop helper | today: the launcher's whole environment (`subprocess.run` with no `env`); agreed: paths only | the launcher, on a failed launch | waited by the launcher | its own exit only, which is not proof that no descendant holds a value; hence paths only |
| workflow shell | today: whatever it places in the launcher's environment; agreed: paths only | the workflow | the step | workflow review: no read, export, command substitution or logging of the values |
| finalizer failure record (`finalizer.failure_record(job_root, stages)`) | none (P3d): it takes no secret value or path | n/a | n/a | not a holder: its record admits only fixed codes and hashes (section 10, Q-P2c-1) |

The drivers' entry points: `run_create_stage` (P2b-2; journal index 0,
calling `create_database`) and `run_registry_stage`. Their only caller is
`scripts/stage-tool.py`, run as `C_T`. The workflow does not call either
yet, so no unsupervised holder is wired. The driver takes no secret value
in argv or the environment; the only password in argv is the public
service superuser's. It prints one JSON line with the result and, on
failure, the fixed code only, never a value or a traceback.

## 3. Supervision of a tool run (P2b)

- One supervisor generation `P_T` per tool run, using the approved
  entrypoint supervisor in a tool mode (`--tool NAME --stage-index N`).
  Its algorithm (subreaper, fork, teardown to `ECHILD`, receipts, deadline)
  is unchanged. `C_T` is the whole driver process:
  `INTERPRETER -I [-S] [-B] [-u] DRIVER NAME ...` (the P1 invocation
  grammar). So the password is generated or read only inside `C_T`, after
  `P_T` exists, and never before.
- Secrets never appear in `C_T`'s argv. Only file paths, the port and the
  public superuser password are passed in.
- **Caller prerequisite (P2B2-SCOPE-N1).** `C_T` inherits the caller's
  environment through `P_T`'s `execv`. The caller must start `P_T` with a
  minimal environment holding no secret values: the local windows use
  `env -i`, and the workflow's paths-only wiring is P2c. The driver
  refuses to start if `DATABASE_URL`, `SECRET_KEY` or `PGPASSWORD` is
  present. That is a check for the known names only; this component does
  not make an arbitrary inherited environment safe.
- Before the fork, tool mode also requires: `NAME` is one of the two tools;
  `N` is 0..30 (canonical decimal); and the receipt, terminal and journal
  paths are exactly the fixed ones under the core's `job_root`. It then
  creates the run directory `tools/<NN>-<NAME>` exclusively, so a second
  run for the same stage refuses.
- Pins, checked pre-fork: the supervisor file and the interpreter as for the
  entrypoint (`entrypoint_pins`), plus the driver from core /3
  `tool_pins[NAME].driver` (`{path, sha256}`, equal to the measured file
  when the core is written, or no core is written). A /1 or /2 core, or a
  driver mismatch, refuses in tool mode before a child exists. The
  entrypoint mode is unchanged and still accepts /2.
- Core /3 = the /2 keys plus `tool_pins` with exactly the keys
  `database-create` and `registry`. The observer binding reader gets /3
  expectations; a /2 expectation never matches a /3 core, and vice versa.

## 4. Receipts

Schemas `alrust-tool-launch/1` and `alrust-tool-terminal/1`. They have
exactly the entrypoint receipt fields, except that `entrypoint_journal` is
named `stage_journal`, and both add `tool: {name, stage_index}`.
- Paths: `<job_root>/tools/<NN>-<NAME>/launch.json` and `terminal.json`,
  created exclusively (`O_EXCL`; an existing receipt refuses pre-fork).
  `stage_journal` is the journal path of stage `NN`, `NAME`.
- Identity: each receipt binds the core sha256 (so a run or job identity),
  the boot, the supervisor and child generations, the deadline, the tool
  name and stage index, and the driver and interpreter. A receipt from
  another run fails the core check. One from another tool or stage index
  fails `tool_mismatch` or the path check. An entrypoint receipt fails the
  schema check.

## 5. Sequencing and journal ownership

1. The workflow starts `P_T` for stage `NN`. `C_T` opens stage `NN` (its
   `StageJournal`), writes its records (for database-create, every attempt
   record before its CREATE, DBC023-R2), and closes it COMPLETE or FAILED.
   Then it exits.
2. `P_T` tears down to `ECHILD`, reads the closed journal and writes the
   terminal receipt, then exits.
3. Only then does the workflow open stage `NN+1`, named `<NAME>-exit`. The
   verifier appends exactly one `tool_exit_verdict`. The stage closes
   COMPLETE only for verdict COMPLETE, otherwise FAILED with
   `tool_exit_<verdict>`.

**Deviation from design 409eabfa section 6.** That design put the verdict
"in the stage that ran it". It cannot go there. `C_T` owns and closes that
journal before `P_T` can observe `ECHILD`, and writing after closure is the
backfill Codex ruled out. A separate exit stage, mirroring
`entrypoint-exit`, keeps a single writer per journal. The verdict is also
produced after `ECHILD` and the ended supervisor generation, and before the
stage that records it closes. The proof depends only on receipts and closed
journals, never on the stage that records it, so it is not cyclic.

The prefix becomes: `database-create`, `database-create-exit`, `launch`,
`launch-confirm`, `entrypoint`, `entrypoint-exit`, `stop`, `registry`,
`registry-exit`, `database-drop`, ... (P3 updates the gates' order rules).

## 6. Verdict

`tool_exit_verdict` has exactly these keys: `verdict`, `reason`, `tool`,
`stage_index`, `launch_receipt_sha256`, `terminal_receipt_sha256`,
`stage_journal_sha256`, `stage_state`, `execution`, `trigger`. The semantics
are those of `entrypoint_exit_verdict`:
- COMPLETE means `tree_empty` with every binding valid, the journal
  observation equal to the current bytes, and the supervisor generation
  positively ended;
- FAILED means `tree_not_empty`;
- anything else is UNPROVEN.

`execution` is derived, never copied: PASS only for an own exit with status
0, no signal, and a closed COMPLETE stage journal. A COMPLETE verdict with
`execution` FAIL is still a holder exit proof. The tool stage's own result
is judged from its journal by the gates, as today.

## 7. Gate use (P3)

`holder_proofs(job_root, aggregation, records, required, expected)`:
- `required_holders` derives the holders from the stage names alone. Each
  required tool must be present; every holder present, required or not,
  appears once and is immediately followed by its exit stage. An exit stage
  without its tool, or repeated, refuses.
- For each holder, there must be exactly one `tool_exit_verdict`, in its
  exit stage, and none anywhere else. The offered record must equal, as a
  whole, a verdict recomputed now from the receipts and the current bytes,
  using the gate's independently supplied pins (`expected`, one entry per
  holder tool, from the workflow head). The recomputed verdict must be
  COMPLETE.

Required sets, proposed:
- the registry stage's writer gate: (`database-create`);
- database-drop on the launched path: (`database-create`, `registry`);
- `failed_entrypoint_quiescence` (A2) and `no_launch_quiescence`:
  (`database-create`);
- `finalize()`: both.

Each gate evaluates the holder check before any mutable read, SQL, removal
or export. Every gate passes at least (`database-create`); the empty
request exists only for the pure controls.

The aggregation must be structurally complete: VALID, or FAILED on the
failed-entrypoint path, never INCOMPLETE. Each holder's exit stage must have
closed COMPLETE in it, and the recomputed `stage_journal_sha256` must equal
the aggregation's digest of the tool stage. So neither the aggregation's
label nor the offered record is trusted alone.

**Pins and legacy cores (P2b-1).** `verify_tool_exit`, and so
`holder_proofs`, requires a /3 core. A /1 or /2 core gives UNPROVEN
`legacy_core`. The caller's `expected[tool]` must equal the core's
`tool_pins[tool].driver.sha256` and its `entrypoint_pins.interpreter.path`,
otherwise UNPROVEN `tool_pins_mismatch`. Both values come from the workflow
head: the core's when it is written (checked equal to the measured files),
and the caller's when the gate runs. Neither alone selects the expectation.
The producer side, P2b-2's supervisor tool mode, refuses a legacy core or a
driver mismatch pre-fork.

**As implemented (P3a).** `finalizer.gate_exit_proofs` is shared by the
writer gate and `finalize()`:
- **One core snapshot.** `aggregate()` reads and hashes the core for its
  own checks. The gate then reads it exactly once more (`_gate_core`) and
  passes those bytes to every later check; nothing rereads it (counted in
  the controls, P3A-SNAPSHOT-N1). Its sha256 must equal the
  aggregation's `core_identity_sha256` (`gate_core_changed`), it must be
  /3 (`gate_legacy_core`), and the caller's pins (`{"entrypoint", "tools"}`)
  must equal the core's entrypoint pins (`gate_pins_mismatch`).
  `verify_entrypoint_exit` and `holder_proofs` take that snapshot.
- **A1.** There is exactly one `entrypoint_exit_verdict`, in
  `entrypoint-exit`, which must be closed COMPLETE immediately after the
  entrypoint. The record is typed and equal as a whole (canonical JSON) to
  the verdict recomputed now; it must be COMPLETE, with its journal digest
  equal to the aggregation's. On the normal path, execution must be PASS,
  and the terminal receipt (read, then hashed to the verdict's digest)
  must show `other_descendant_signals == 0`.
- **Holders.** `holder_proofs` with the gate's required set.
- **Order.** `database-create-exit`, `launch-confirm` and `entrypoint-exit`
  each come immediately after their stage, and the order is
  database-create < launch < entrypoint < stop (`gate_stages_invalid`).
  `finalize()` also requires `registry-exit` immediately after `registry`.

All of this runs before any mutable read, SQL, removal or export: before
`load_registry`, the candidate checks and the export directory in
`finalize()`, and before the registry or drop journal opens in the stage
runners.
- `run_registry_stage(..., expected=...)` requires (`database-create`).
- `run_drop_stage(..., launched, expected=...)` takes its path from the
  evidence: `launched` must equal `"launch" in prefix`, or
  `drop_path_mismatch`.
- The launched path uses the writer gate with both holders, then
  `registry_frozen`. The no-launch path uses `no_launch_quiescence(job_root,
  [database-create, database-create-exit], expected)`: the holder proof,
  even when the create stage closed FAILED, and still no launch evidence.
- The finalizer removes the `entrypoint/` and `tools/` receipt directories
  as proof inputs, as it already does for `reference/`.
- The stage-tool driver passes the expected pins from a JSON file
  (`--expected-pins`): a regular file (checked on the opened descriptor,
  P3A-INPUT-R1), opened without following a final link, bounded; anything
  else is NOT_RUN with no stage opened.

**As implemented (P3b): A2, `failed_entrypoint_quiescence(job_root,
stages, expected)`.** It is the gate for database-drop alone after a failed
entrypoint (designs `d6bc7e80` section 4 and `409eabfa` section 5). Its
prefix is exactly database-create, database-create-exit, launch,
launch-confirm, entrypoint, entrypoint-exit, stop (P3B-PREFIX-R1): no
registry or registry-exit anywhere, in any order. It has
the same order, core snapshot, pins, stop proof and launch hand-off as the
writer gate, plus the database-create holder. It admits exactly:
- **F1:** the entrypoint journal closed FAILED; the aggregation FAILED
  with the entrypoint its only non-COMPLETE stage; the entrypoint-exit
  verdict COMPLETE with execution FAIL;
- **F2:** the entrypoint journal closed COMPLETE; the aggregation VALID;
  the verdict COMPLETE with execution FAIL, recomputed from the terminal
  receipt (its trigger), never invented, with no journal or aggregation
  changed.

It refuses:
- a passing entrypoint (`entrypoint_not_failed`, so it stays disjoint from
  the normal gate);
- any other FAILED stage (`failed_gate_other_stage_not_complete`);
- an INCOMPLETE stage (`failed_gate_incomplete`);
- an unproven or stale exit, or a stale holder;
- a registry in the prefix (`failed_gate_prefix_invalid`).

`run_drop_stage` selects it from the evidence (launched, no registry in
the prefix) and records `drop_gate {gate: "failed_entrypoint", case}`
before the drop. On every drop path (normal, A2, no-launch), the
allocation bytes the drop consumes are read only after the gate passed,
and must hash to the gate's aggregation digest of `database-create`, which
the create holder's proof also binds. Otherwise the result is
`drop_allocation_changed`, with no SQL (P3B-ALLOCATION-R2). That is a separate record: the approved `database_drop`
record is unchanged, so the `d6bc7e80` "gate field" is carried this way.
The registry gate and `finalize()` refuse both cases. Profile FAIL and job
cleanup UNPROVEN are unchanged.

Not in P3a or P3b: the job-role binding before the first read (P3c) and
the paths-only failure record (P3d).

## 8. Deadlines

Each tool run gets its own `D` and `B` = 16 s in its launch receipt, as for
the entrypoint. `D` must fit inside the stage budget (`STAGE_BUDGET_NS`)
with room for the exit stage. `D` too close before the fork means no fork
and no receipt. Deadline expiry gives `execution` FAIL, while closure can
still be COMPLETE.

## 9. Stop, timeout and unknown state

An outside kill of `P_T` leaves no terminal receipt: UNPROVEN, and the gates
refuse. Stopping a live `P_T` uses only a held, generation-checked pidfd
(P4's reviewed helper), never a bare PID. Unknown state is FAIL or HOLD.

## 10. Server secrets: paths only (Q-P2-1 and Q-P2-2, agreed in `c860b652`)

The workflow shell, the launcher, the launcher's stop helper and the
reference supervisor `P` never hold `DATABASE_URL` or `SECRET_KEY` values.
- They receive and pass only the fixed paths: the job's 0600 URL file
  (`<job_root>/database/url`) and the 0600 `SECRET_KEY` file.
- The shell does no read, export, command substitution or logging of
  either value.
- The launcher's allowlist replaces the two value variables with path
  variables. It refuses if a value variable is present.
- **First reader:** `C`, after `P` has forked it and written its launch
  receipt, so after supervision exists. A small reviewed pre-exec step in
  `C` opens each fixed path without following links, checks that it is a
  private regular single-link file owned by the job uid, reads it bounded,
  validates the URL against the job's database name and port, and places
  the values in its own environment. It then execs `alertad` exactly as
  today. The application sees the same values, so behaviour is unchanged.
- **Every future holder:** `C` (the reference server) and its descendants,
  all inside `P`'s tree and covered by the stop verdict. Nothing outside a
  supervised tree reads a value. The database-create driver (the URL file's
  writer) and the registry driver (a reader) are the `P_T` runs above.
- This changes no authentication, data or behaviour contract; it only moves
  where the unchanged values are first read. It touches the approved
  launcher and supervisor, so it is a separate reviewed head (P2c), before
  the database stage is wired into the workflow.

**As implemented (P2c).**
- `launch-reference.py` no longer allows `DATABASE_URL` or `SECRET_KEY` in
  its environment. It requires `--secret-key-file K` (absolute, normalized,
  under the job root) and `--database-port D`, and passes `P` only
  `--database-url-file <J>/database/url --secret-key-file K
  --database-port D` (`supervisor_argv`). There is no value mode, no mixed
  mode and no fallback.
- `reference-supervisor.py` (`P`) refuses to start if `DATABASE_URL`,
  `SECRET_KEY` or `PGPASSWORD` is in its environment, never opens either
  file, and requires the three options. The barrier:
  - `P` creates a pipe before the fork;
  - `C` closes the write end and blocks on a one-byte read;
  - `P` writes the byte only after `write_exclusive` has written and synced
    the launch receipt; if the receipt fails, it closes the pipe unwritten,
    and `C` exits 126 having opened nothing.
- After the byte, `C` reads both files: each path component opened from
  `/` with `O_NOFOLLOW` (directories `O_DIRECTORY`); the leaf must be a
  regular file of this uid with no group or other bits, one link, and
  bounded size. The URL must match the job role's
  `postgresql://NAME:<64 hex>@127.0.0.1:D/NAME`, with the database name
  equal to the role and `D` the given port. The key must be 16..4096 bytes
  of printable UTF-8. A refusal exits 125 with a fixed code only.
- `C` then execs `COMMAND` with the two values added to its own
  environment. The launch receipt is unchanged.
- Residual at P2c, closed by P3c below: the URL's role was not yet bound to
  the core's `database_name(profile, run_id, attempt)`.

**Declared consequence.** With values no longer accepted, today's hosted
scaffold launch path (`run-reference.sh` exporting `DATABASE_URL` and a
`SECRET_KEY` default, and `ci.yml` passing the service superuser URL)
cannot launch at this head. That URL would be refused anyway, since it is
not a job-role URL. Wiring the workflow to the database stage, with paths
only, is the workflow head's change (design section 8, item 4). This head
is stacked and unpublished, so `main` is unaffected.

**As implemented (P3c): the job-role binding before the first secret read.**

What `database_created` does and does not bind: the record precedes the
URL write and binds no URL bytes (there is no URL digest). No secret-file
byte binding is inferred from it. The binding is instead:
- the fixed private path `<J>/database/url`;
- inside the directory object (`dev`, `ino`) that database-create recorded
  as its `owned_resource` when it created `<J>/database`;
- naming the expected role, `database_name(core profile, run_id,
  run_attempt)`, which `database_created` must also name (both `name` and
  `role`);
- and the database port `D`.

The expectation is fixed before any read; each reader compares the URL to it
after reading and before using it.

- **Launch gate** (`database_stage.launch_prerequisites`), run by the
  launcher before its journal opens and before `P` exists:
  - the prefix is exactly `[database-create, database-create-exit]`, closed
    VALID (a failed allocation never launches);
  - one core snapshot bound to the aggregation, `/3`, with the caller's pins
    (`--expected-pins`, the same bounded regular-file reader as the driver);
  - the database-create holder's exit proof (`holder_proofs`) on that
    snapshot;
  - exactly one `database_created` naming the expected role, and exactly one
    `owned_resource` of type `dir` for `<J>/database`.
  It reads only proof inputs, never the URL or the key. Refusals carry fixed
  codes: `launch_prefix_invalid`, `launch_prefix_not_complete`, the gate and
  holder codes, `database_role_unexpected`, `database_dir_unrecorded`.
- **Launcher.** Only stage mode launches: index 2 after
  `database-create-exit`, with `--expected-pins`. A launch without the stage
  context is refused right after argument parsing, before any check, journal
  or process; there is no fallback. `--preflight` starts nothing and reads
  no secret, so it still works without stage arguments. The launcher passes
  `P` `--expected-role ROLE --database-dir-identity DEV:INO`.
- **`P` and `C`.** Both options are required (usage exit 64 otherwise, or if
  malformed). `C`'s private reader `fstat`s the held parent directory of the
  URL file and requires that exact (`dev`, `ino`) (`secret_dir_not_bound`).
  After the read and before exec, the URL's role must equal
  `--expected-role` (`database_role_unexpected`), in addition to the P2c
  checks. A refusal still exits 125 with a fixed code only.
- **Registry.** `run_registry_stage` derives the same binding from the
  writer gate's evidence (its aggregation's records and core snapshot) and
  adds the gate's core digest. `build_registry` requires it, and before
  `Psql` is constructed:
  - the URL is read through one held directory (`_read_bound_url`,
    P3C-DIR-R1). The job root is opened component by component without
    following links and bound to that core (`_open_bound_root`, refusing
    with `inventory_root_changed`). `database` is opened relative to it
    with `O_DIRECTORY|O_NOFOLLOW`, and the held object must be the
    uid-owned 0700 directory with the recorded (`dev`, `ino`)
    (`database_dir_not_bound`). `url` is opened relative to that same
    descriptor (`url_invalid` if absent or a link). A rename or symlink
    swap after the check reads the bound bytes or refuses; it never
    substitutes others;
  - the URL's role must be the expected one (`database_role_unexpected`);
  - `Psql`'s passfile is written by path, because `psql` opens
    `PGPASSFILE` by name. The path `<J>/database` is therefore rechecked
    against the recorded object after the read (`database_dir_not_bound`).
- **Residuals** (declared, not addressed in P3c):
  - `create_database` derives the name from its own `--profile`,
    `--run-id` and `--attempt` arguments, not from the core. A mismatch is
    now refused at the launch and registry gates, never used. A later head
    may take those three from the validated core.
  - The secret key file's parent (the job root) is not identity-bound: no
    stage records the job root as an owned resource, so `C` reads the key
    with no parent identity. The P2c checks on the key file still apply.
  - `Psql`'s passfile (registry, and as before create and drop) is written
    by path in `<J>/database`. A swap after the registry's recheck could
    place the bound password's passfile in another directory; it cannot
    make a substituted password be used. An fd-held passfile is a later
    change.
  - The launch journal does not record the expectation the launcher handed
    `P` (role, directory identity, gate aggregation digest), so a later
    gate cannot yet recheck what `P` was told.
  - The launcher reads the core three times: in `check_stage`, in the
    gate's `_gate_core`, and when its `StageJournal` begins (recording that
    read's digest). From the source: every journal in an aggregation is
    validated against one core digest, so a launch journal begun under a
    different core is refused by every later gate. The three reads are not
    one snapshot.

**Declared consequence (P3c).** This reopens `launch-reference.py` and
`reference-supervisor.py`. The P2c consequence stands: the hosted scaffold
cannot launch at this head. In addition, no launch is possible without a
closed database-create and database-create-exit and their holder receipts.
The launcher process tests (`test_reference_launcher.py`) now prepare
those (synthetic ended `P_T`, `exit_fixtures`) and launch at index 2;
`confirm-launch.py` moves to index 3. Codex's
`observer_launch_interoperability.py`, which launches at index 1 after
`database-create` with no pins file, needs a further adaptation (hers).

**Q-P2c-1, the finalizer's failure record.** At P2c, `failure_record`
took the two values from its caller in order to scan its output for them,
so the caller was a holder outside a supervised tree.

**As implemented (P3d): not a holder; the core schema is /3, unchanged.**
`failure_record(job_root, stages)` takes no secret value and no secret
path. The value scan is replaced by a record schema that cannot carry a
secret value:
- the stop (verdict, reason) must be one of the exact pairs
  `stop-reference.py` emits (`finalizer.STOP_VERDICTS`, kept equal to that
  script's `verdict(...)` literals by a pure control). Before P3d any
  `[a-z][a-z0-9_]{0,63}` reason was admitted, which a 64-hex password
  whose first character is a letter (a-f) satisfies;
- the aggregation must be (`VALID`, `complete`) or (`FAILED`,
  `stage_failed`), and every stage result `COMPLETE` or `FAILED`;
- every stage name must be one of `finalizer.FAILURE_STAGES`, the ten
  pipeline names; otherwise nothing is written (`failure_stages_invalid`).
  Order and repeats are left to the aggregation, since a fixed name
  carries no secret whatever its position. At e3867203 this read
  "come from the caller's fixed list and are at most 32 characters", which
  closed nothing: Codex's P3D-STAGE-R1 exported a 23-character synthetic
  key through an extra, validly chained stage name. Indices are integers;
  every other field is a sha256 or a fixed string.
The generic patterns are still scanned, and any match writes nothing.

Why not a supervised tool run:
- nothing calls `finalize()` or `failure_record()` yet (no script and no
  workflow), so there is no supervised step to host an in-process call;
- a third holder tool cannot be added inside /3: `TOOL_PIN_NAMES` is part
  of what an approved /3 core means, and changing it would invalidate
  every /3 core already written;
- a /4 core would reopen every /3-requiring site (46 references in 12
  files) for a terminal step that no gate consumes.
If the value scan itself is wanted back, /4 with a `failure-record` holder
tool (paths only, through the P3c bound reader) is the fallback.

What this does not change: the record still requires the launch receipt
and exactly one linked stop verdict, so a job that fails before launch
still gets no failure record (unchanged since F2FE89-R4).

## 11. Controls in this head (pure)

`tests/unit/test_tool_exit.py` uses a real core, real stage journals and
synthetic receipts. Generations are modelled with absent PIDs, and this
process stands in for a live one. It covers:
- COMPLETE and PASS; a closed FAILED tool stage with closure COMPLETE and
  execution FAIL; `tree_not_empty`;
- refusals: receipts for another tool or stage index (copied or replayed);
  malformed or missing tool binding; entrypoint receipts or field names at
  the tool path; core, boot, journal and receipt-path replays; driver,
  interpreter and deadline mismatches; a missing terminal receipt; a
  terminal link mismatch; an execution contradiction; a changed journal
  observation; a live generation; bad pins; an unknown tool or index;
- the exit stage: one verdict, closing COMPLETE or FAILED, with the tool
  journal untouched; prefix refusals;
- `required_holders`: positives and every refusal;
- `holder_proofs`: the positive case; forged, missing, duplicate and
  misplaced verdicts; receipts changed after the verdict; a now-live
  generation; an exit stage closed FAILED; a non-COMPLETE verdict under a
  forged COMPLETE stage label; an INCOMPLETE aggregation; a tool journal
  digest that differs from the aggregation's; pin refusals and an
  independent pin mismatch; a required holder that is absent.
- a missing launch receipt; a tool binding index beyond the last possible
  tool index;
- P2A-SOURCE-R1: an offered verdict written through the actual
  StageJournal into a VALID chained aggregation. Exact int `stage_index` is
  accepted; `false`, `0.0`, `"0"` and other mistyped fields refuse as
  `holder_verdict_malformed`. The comparison with the recomputed verdict is
  type-sensitive (canonical JSON).

Mutants run on this head: dropping the receipt's tool-binding check,
trusting the offered verdict instead of recomputing it, and dropping the
adjacency rule each fail the controls.
