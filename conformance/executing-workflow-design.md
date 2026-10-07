# Executing workflow: design (proposed)

Status: **design for review, docs only.** No workflow file, stage CLI or
job changes in this head. Every statement either cites an existing module,
function or constant at the base commit, or is marked *proposed*. Nothing
here has run; every stage below is NOT_RUN until its own admitted
execution.

Base: the approved combined R-EP head `20cb9057` (P1–P4 and the PR #100
fix; Codex `6c63592e`). Contract: `docs/ci-launch-attestation-contract.md`
sections 3–11 (journals, settings, launch, database, inventory,
finalization order, deadlines, entrypoint interface). R-EP design
`409eabfa` section 8 item 4 leaves "workflow wiring" to this head.

## 1. Scope and inertness

The executing workflow is one job that runs the stages below on one fresh
machine for one profile (contract section 1). This design proposes:

- **Inert on publication (EWD-R1).** Triggers belong to a workflow file,
  not a job. *Proposed:* a **separate new workflow file**,
  `.github/workflows/executing.yml`, whose only trigger is
  `workflow_dispatch` (no `pull_request`, `push` or `schedule`), so
  publishing it starts nothing. The existing `ci.yml`, with its
  `push: main` / `pull_request` triggers and its four scaffold jobs,
  including `hosted profiles (inert preflight, all NOT_RUN)`, is left
  untouched. Publishing the file is not a dispatch; any first execution
  needs its own admission (section 8).
- **Hosted only at first.** GitHub-hosted `ubuntu-24.04`, the same
  `postgres:16` service shape as the existing `conformance-reference`
  job: `ports: ["5432:5432"]`, which publishes `0.0.0.0:5432`, matching
  the PR #100 endpoint check (`database_stage.container_identity(...,
  port=5432)`, `ENDPOINT_HOSTS`). The service container id is
  `${{ job.services.postgres.id }}`. No self-hosted runner, shared VM or
  microVM (Y15, policy v1.1).
- **No new Rust or candidate server.** Reference only; two-server
  conformance stays NOT_RUN (no candidate).

## 2. Stage table

Journal filenames are `NN-<stage>.jsonl` (`stage_journal.journal_path`),
so indices are part of each stage's identity, and the exit verifiers
bind `stage_index`. The stage lists below (normal, A2, no-launch) are
**explicit workflow proposals**. `finalizer.FAILURE_STAGES` is the
imported allowed-name vocabulary (`failure_record` checks membership
only); it does not enforce order (EWD-N1). The actual order is enforced by
the named gate checks: `finalizer._require_order`, the exact A2 prefix in
`failed_entrypoint_quiescence`, and `database_stage.no_launch_quiescence`.
*Proposed:* the workflow imports `FAILURE_STAGES` and `STOP_VERDICTS`
rather than restating them (Q-W-2, agreed in `0297abc0`).

| Index | Stage | Actor | Writer of the journal | Existing source | Missing (this design's heads) |
|---|---|---|---|---|---|
| — | core identity | workflow step, `python -m alrust_conformance.stage_journal core ...` | n/a (writes `core-identity.json`, /3 with pins) | `stage_journal.core_main` | the pins literals (section 3) |
| 0 | `database-create` | `P_T` (`entrypoint-supervisor.py --tool database-create --stage-index 0`) running `stage-tool.py database-create` | the tool child | `database_stage.run_create_stage`, `stage-tool.py` | none (W3 wires it) |
| 1 | `database-create-exit` | workflow step | verifier | `tool_exit.run_tool_exit_stage` | a CLI (W2); `stop-supervisor.py --tool database-create --stage-index 0` before it |
| 2 | `launch` | workflow step | `launch-reference.py` | P3c launch gate, `launch_prerequisites` | none |
| 3 | `launch-confirm` | workflow step | `confirm-launch.py` | exists | none |
| 4 | `entrypoint` | `P_E` (`entrypoint-supervisor.py`) running the entrypoint as `C_E` | `C_E` | supervisor exists; the entrypoint is Codex's (item 6) | Q-W-1 |
| 5 | `entrypoint-exit` | workflow step | verifier | `entrypoint_exit.run_entrypoint_exit_stage` | a CLI (W2); `stop-supervisor.py --entrypoint` before it |
| 6 | `stop` | workflow step | the stop recorder | `stop-reference.py` (prints one JSON line, exit 0/1/2) | the stop recorder (W1) |
| 7 | `registry` | `P_T` running `stage-tool.py registry` | the tool child | `run_registry_stage` (writer gate, P3c binding) | none (W3) |
| 8 | `registry-exit` | workflow step | verifier | `run_tool_exit_stage` | CLI (W2), `stop-supervisor.py --tool registry --stage-index 7` |
| 9 | `database-drop` | workflow step | drop runner | `database_stage.run_drop_stage` | a CLI (W2); `launched` comes from the evidence, never a caller flag |
| — | finalizer | workflow step | the finalizer's own journal | `finalizer.finalize`, `failure_record` | a CLI (W4) |

Paths (indices are positions in the stage list passed to each gate):

- **Normal:** 0–9 as above, then the finalizer.
- **A2, failed entrypoint** (`finalizer.failed_entrypoint_quiescence`): the
  exact prefix `[database-create, database-create-exit, launch,
  launch-confirm, entrypoint, entrypoint-exit, stop]`, then
  `database-drop` at index **7** (no registry). The profile is FAIL; no
  registry, finalization or export.
- **No launch** (`database_stage.no_launch_quiescence`): `[database-create,
  database-create-exit]`, then `database-drop` at index 2.
- **Failure record** (`failure_record(job_root, stages)`, P3d): any of
  the lists above whose names are all in `FAILURE_STAGES`; it needs the
  launch receipt and one linked stop verdict, so the no-launch path gets
  none (unchanged).

## 3. Pins provenance (c35732da: "pins must originate in exact approved workflow head")

*Proposed:* the expected pins are **literals in the committed workflow
file**, so they are fixed by the workflow head's reviewed commit, never
read from a receipt, journal or the runner:

- `--supervisor-pin conformance/scripts/entrypoint-supervisor.py=<sha256>`,
  `--entrypoint-script-pin <path>=<sha256>`, `--interpreter <abs path>`,
  `--tool-pin database-create=conformance/scripts/stage-tool.py=<sha256>`,
  `--tool-pin registry=conformance/scripts/stage-tool.py=<sha256>` to the
  core CLI. `stage_journal.core_main` measures each file itself and
  `write_core_identity` refuses a digest that differs from its pin
  (`entrypoint_not_pinned`, `tool_not_pinned`), so the check happens
  before any fork.
- the same literals, written by one workflow step into
  `$JOB_ROOT/expected-pins.json` (the `exit_fixtures.EXPECTED` shape:
  `{"entrypoint": {entrypoint_script_sha256, interpreter_path}, "tools":
  {tool: {driver_sha256, interpreter_path}}}`), passed as
  `--expected-pins` to the launcher and the exit/gate CLIs. The gates
  compare it to the core (`finalizer._gate_core`, `tool_exit.holder_proofs`).
- The interpreter is pinned by path, with its resolved path and digest
  measured into the core (not "hash of a path").

Residual: `stop-supervisor.py` (P4) and `stop-reference.py` are not in the
core's pins; a later core schema could add them.

## 4. Secrets, paths only

- `SECRET_KEY`: *proposed* generated in a workflow step directly into
  `$JOB_ROOT/secret-key` (0600, exclusive) by the reviewed private writer;
  never echoed, exported or placed in an environment. The launcher takes
  `--secret-key-file` (P2c).
- The job role's URL: written only by `create_database` to
  `$JOB_ROOT/database/url` (0600); read only by `C`'s private reader
  (P2c, bound by P3c to the recorded directory and role) and by the
  registry (`_read_bound_url`, P3C-DIR-R1).
- The service superuser password `postgres` is public by construction
  (the service definition) and appears only as an argument to the create
  and drop tools.
- The entrypoint's Q1 inventory `entrypoint-secrets.json` is required by
  `run_registry_stage` (`private_secret_inventory`); its writer is the
  entrypoint (Codex).
- Every tool and verifier runs under `env -i` with an explicit `PATH`
  and `LANG`, as the existing scaffold steps do.

## 5. Deadlines

The job's 30-minute budget (contract section 10). *Proposed* (EWD-R2;
the cleanup-phase cap per Codex `afa16a6a`):

- **Once:** an early step fixes the global job deadline `J` (absolute
  `CLOCK_MONOTONIC`, same boot as the core) and writes it to a private
  file in the job root. It is never recomputed or extended.
- **Two phases.** The *work phase* holds `database-create` (with its exit
  stage), `launch`, `launch-confirm` and `entrypoint`. The *cleanup phase*
  holds `entrypoint-exit`, `stop`, `registry`, `registry-exit`,
  `database-drop` and the finalizer. The cleanup reserve `R` is fixed and
  covers the whole cleanup phase at its maxima. Each line is an existing
  bound or a proposed step limit:
  the entrypoint's teardown `B` (16 s, `entrypoint_exit.TEARDOWN_BUDGET_NS`);
  `stop-supervisor.py` plus `entrypoint-exit` (20 s wait plus 60 s step);
  the reference stop (20 s exit wait, `stop-reference.py`, plus a margin);
  `registry` (60 s, `STAGE_BUDGET_NS`, plus `B`); `stop-supervisor.py` plus
  `registry-exit` (20 s plus 60 s); `database-drop` (60 s); the finalizer
  (60 s, contract section 10). That sums to about 402 s (under 7 minutes)
  before margins, inside the
  contract's 8-minute reserve; *proposed* `R` = 8 minutes. The work cutoff
  is `C = J − R`.
- **Work-phase D**, frozen once, immediately before each launch (the
  database-create tool, then the entrypoint):
  `D = min(now + stage budget, C − exit reserve of that run)`, where the
  stage budget is `STAGE_BUDGET_NS` for a tool or the manifest budget for
  the entrypoint. The entrypoint's own teardown and exit are inside `R`,
  so its exit reserve is 0 here; database-create's exit reserve is `B`
  plus its exit stage. No work-phase run may start after `C`.
- **Cleanup-phase D**, frozen once, immediately before the registry
  launch: `D = min(now + STAGE_BUDGET_NS, J − remaining(registry))`, where
  `remaining(registry)` is the reserve still needed after the registry
  run: its teardown `B`, `stop-supervisor.py` and `registry-exit`, the
  drop and the finalizer, plus margins. The registry is never capped by
  `C`. With the work phase confined to `C` and `R` sized as above, the
  registry has its full budget at the boundary (Codex's counterexample:
  the entrypoint passes just before its D at `C`, its exit and stop use
  their allowed reserve, and the registry still starts within `R` with
  `remaining(registry)` left).
- **Refusal, not extension.** If any launch's `D < now + B`, the
  supervisor refuses before the fork (no fork, no receipt). The workflow
  records that refusal with a fixed code; it never retries with a fresh
  `D`. A cleanup-phase refusal leaves the profile FAIL with cleanup
  UNPROVEN.
- **Plan fit before allocation.** *Proposed:* before `database-create`
  allocates anything, a pure check confirms that the whole plan (the work
  budgets plus `R`) fits before `J`, and refuses before allocation if not.
  W-heads add a pure near-boundary control: with the entrypoint ending
  exactly at `C`, every cleanup step still fits; with `R` shortened by one
  step's maximum, the plan refuses before allocation.
- **Outer step timeout:** `timeout-minutes` is relative, so each step
  derives it from the same start: at least
  `ceil((D − now + B + margin) / 60 s)` minutes, the margin covering the
  terminal receipt. An outside kill by that timeout or by the job leaves
  no valid terminal receipt: UNPROVEN, and the gates refuse. It never
  proves cleanup.

## 6. Stop recorder (missing; W1)

`stop-reference.py` prints one JSON line and exits 0, 1 or 2
(`EXIT = {"COMPLETE": 0, "FAILED": 1, "UNPROVEN": 2}`). The line is
`{"verdict", "reason"}`, plus an integer `errno` key only for
`pidfd_open_failed` and `signal_failed` (`stop-reference.py:228, 254`).
*Proposed* `scripts/record-stop.py` (EWD-R3):
- opens the `stop` journal at its index after `entrypoint-exit` and runs
  `stop-reference.py RECEIPT SHA256 JOB_ROOT` exactly once, bounded;
- parses the bounded stdout with strict JSON (duplicate keys, non-finite
  numbers and invalid UTF-8 refuse). The key set must be exactly
  `{verdict, reason}`, or `{verdict, reason, errno}` with a non-bool int
  `errno` and only for those two reasons. `(verdict, reason)` must be in
  `finalizer.STOP_VERDICTS` (imported, Q-P3d-2), and the exit code must
  equal `EXIT[verdict]`;
- projects only the fixed `verdict` and `reason` into the record; `errno`
  is never serialized as text into the journal;
- computes the receipt digests itself from the fixed paths, not from the
  line. `launch_receipt_sha256` comes from the launch receipt. For
  `terminal_receipt_sha256`: COMPLETE requires the terminal receipt and
  both actual digests (and the existing gates still apply); on an
  UNPROVEN path with no terminal receipt it is `null`, which `failure_record`
  accepts, so the failure evidence is kept, not erased;
- records exactly one `stop_verdict` of the shape `_stop_evidence` and
  `failure_record` require. It keeps the raw helper output privately in
  the job root (never exported, removed by the finalizer as an owned
  resource). Malformed or unavailable output closes the stage FAILED with
  a fixed code; it never retries and never invents COMPLETE.

## 7. Entrypoint for the first run (Q-W-1, decided)

Codex chose (a) in `0297abc0`: the first run waits for her independently
reviewed executing entrypoint (design `b03cebda`, heads E1–E4). No empty
COMPLETE stand-in is proposed or enabled.

## 8. Admission classification (policy v1.1)

New code paths compared with the scaffold jobs, all NOT_RUN today:
real `CREATE ROLE` / `CREATE DATABASE` and the `DROP`s on the service
container; a job-role login by the reference server; the supervised
entrypoint and tool runs; the stop recorder; the finalizer's removals and
export. The scaffold already exercises: the hosted postgres service, the
supervised reference boot and the pidfd stop. *Proposed:* hosted-only, the
first execution under its own signed admission and window, existing
hosted minutes (no new or paid CI), no artifact upload until the
finalizer's scan and manifest are themselves reviewed for upload.

## 9. Carried residuals and where they close

| Residual (source) | Proposed close |
|---|---|
| `create_database` takes `--profile/--run-id/--attempt` from argv, not the core (P3c) | W3: `stage-tool.py database-create` reads them from the validated core |
| `Psql` passfile written by path (P3c) | later: an fd-held passfile (`PGPASSFILE=/proc/self/fd/N` with `pass_fds`) |
| the key file's parent is not identity-bound (P3c) | later: record the job root as an owned resource at the core step |
| the launch expectation is not journalled (P3c) | W3: the launcher records `{role, database_dir, gate aggregation sha}` |
| `stop-supervisor.py` / `stop-reference.py` unpinned (P4) | later core schema, or workflow-literal digests checked pre-run |
| `observer_launch_interoperability.py` adaptation (Codex) | Codex's own head |
| a published port mapping is not live-peer proof (PR #100) | stays declared |

## 10. Proposed heads, for agreement

1. **W1** stop recorder + pure controls (the line grammar, `STOP_VERDICTS`
   import, receipt digests computed independently, fixed-code refusals).
2. **W2** CLIs for the exit stages and the drop runner (thin wrappers over
   `run_tool_exit_stage`, `run_entrypoint_exit_stage`, `run_drop_stage`;
   `launched` from evidence), with pure controls.
3. **W3** `stage-tool.py database-create` names from the core; the launch
   expectation record.
4. **W4** finalizer CLI (`finalize`, else `failure_record`), pure controls.
5. **W5** the separate workflow file `executing.yml` (`workflow_dispatch`
   only, inert on publication; `ci.yml` untouched), after W1–W4 and
   Codex's E-heads it depends on are approved; its first execution is a
   separate admission.

Answers (`0297abc0`): Q-W-1 (a), wait for the real entrypoint; Q-W-2
import the constants, with the membership/order distinction above; Q-W-3
pins as approved workflow literals, and the stop helpers' literal digests
checked before any eventual execution. W1–W5 order agreed in principle;
every implementation head is reviewed separately, and the first execution
is admitted separately.

## 11. Execution slices (proposed, 2026-10-07; for agreement)

The smallest complete executions, in order. Each is a separate W5 head,
reviewed at its exact commit, published inert (`workflow_dispatch` only),
and executed only under its own signed admission on GitHub-hosted runners
within the free allowance. Source/offline approval of a head is never
runtime qualification of it.

| Slice | Stages (indices) | Terminal outcome when everything works | What it qualifies | Not established |
|---|---|---|---|---|
| S0, no launch | core; `database-create` (0) under `P_T`; `database-create-exit` (1); `database-drop` (2, `no_launch_quiescence`); `finalize-job.py` | tool exit `COMPLETE` (`ECHILD`); drop `COMPLETE`; finalize-job line exactly `{"cleanup": "UNPROVEN", "failure_record": "NONE", "outcome": "NOT_FINALIZED", "reason": "entrypoint_stage_missing"}` (no launch receipt, so no failure record) | the core with pins as literals; `P_T` and `stage-tool.py` against the real PostgreSQL service; `stop-supervisor.py --tool`; the W2 exit and drop CLIs; W3's core-derived names; the deadlines module | any reference launch; the entrypoint; the finalizer's sealing path (S0 never reaches it); removal of the job root (runner discard only) |
| S1, A2 | S0's 0–1; `launch` (2); `launch-confirm` (3); `entrypoint` (4) under `P_E` with a pinned refusal entrypoint; `entrypoint-exit` (5); `stop` (6); `database-drop` (7, `failed_entrypoint_quiescence`); `finalize-job.py` | entrypoint-exit `COMPLETE` with execution `FAIL` (F1 or F2); stop `COMPLETE/tree_empty`; drop `COMPLETE`; finalize-job `NOT_FINALIZED` with failure record `WRITTEN` | the whole reference launch, confirmation, stop and cleanup chain with real journals and receipts; the A2 gate; the failure record | any conformance result (the entrypoint runs no test) |
| S2, one profile | S1 with the composed entrypoint (E3) and that profile enabled (E4); `registry` (7); `registry-exit` (8); `database-drop` (9); finalizer to `SEALED` | profile result from the entrypoint; finalize `SEALED` with cleanup `COMPLETE` | actual reference conformance for that profile | the other profiles; any Alrust server |

The S0 success criterion is that exact set of verdicts and line, not a
zero exit: `finalize-job.py` exits 2 for `NOT_FINALIZED` by design (W4).

**Pin dependency (corrects Claude a9adb562, item 2).** Even S0 depends on
an entrypoint script: tool mode needs a /3 core, `CORE_KEYS_V3` includes
`entrypoint_pins`, and `stage_journal.core_main` refuses `--tool-pin`
without `--supervisor-pin`, `--entrypoint-script-pin` and `--interpreter`
(usage), measuring the pinned script file. The core must pin the reviewed
script S1 will run, never an unrelated file as a stand-in. So the first
item on the critical path for both S0 and S1 is Codex's refusal entrypoint
(E1c): a C_E script that validates its E1a plan, opens journal index 4,
records the plan's `NOT_RUN` refusal, closes, and exits non-zero.

**Deadlines.** `alrust_conformance.deadlines` (Claude, on the W-head
stack) implements section 5 as pure functions of `now` and `J`: R = 480 s
over the cleanup steps' 402 s maxima; C = J − R; a tool's
D = min(now + 60 s, C − 96 s), refused at or after C or below B; the
child's deadline D − 5 s, so it closes its journal before its supervisor
tears down at D; step timeouts ceil((D − now + B + 30 s) / 60 s); the plan
fit before allocation; and J fixed once, after the core, as now + 25
minutes (the 30-minute job less 5 minutes for the earlier steps), bound to
the core's sha256 and boot. E1a's helper stays the entrypoint's
work-phase deadline only.
