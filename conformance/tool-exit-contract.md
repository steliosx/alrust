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
- Secrets never appear in `C_T`'s argv or environment. Only file paths, the
  port and the public superuser password are passed in.
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
