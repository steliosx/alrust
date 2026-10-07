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

- **Inert on publication.** *Proposed:* a new job, separate from the four
  existing scaffold jobs, triggered only by `workflow_dispatch` (no
  `pull_request` or `push` trigger), so publishing it starts nothing. Any
  first execution needs its own admission (section 8). The existing
  `hosted profiles (inert preflight, all NOT_RUN)` job is unchanged.
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
bind `stage_index`. The canonical names and their order are
`finalizer.FAILURE_STAGES`. *Proposed:* the workflow imports that
constant rather than restating it.

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

The job's 30-minute budget (contract section 10). *Proposed:* one early
step computes the absolute `CLOCK_MONOTONIC` deadlines `D` for the
entrypoint and each tool run from the step start and the budgets below,
passes them as `--deadline-monotonic-ns`, and sets each step's
`timeout-minutes` above `D + B` (B = 16 s, `entrypoint_exit.TEARDOWN_BUDGET_NS`)
plus a margin, so an outside kill lands in UNPROVEN rather than racing
teardown. Each tool `D` fits inside `database_stage.STAGE_BUDGET_NS`
(60 s) with room for its exit stage (tool-exit-contract section 8).

## 6. Stop recorder (missing; W1)

`stop-reference.py` prints one JSON line `{"verdict", "reason"}` and exits
0/1/2. *Proposed* `scripts/record-stop.py`: opens the `stop` journal at its
index after `entrypoint-exit`, runs `stop-reference.py RECEIPT SHA256
JOB_ROOT` once (bounded), validates the line strictly (`(verdict, reason)
in finalizer.STOP_VERDICTS`, Q-P3d-2: imported, not restated), computes
the launch and terminal receipt digests from the fixed paths itself (not
from the line), and records one `stop_verdict` of the exact shape
`_stop_evidence` and `failure_record` require. Any other output closes the
stage FAILED with a fixed code; it never retries.

## 7. Entrypoint for the first run (Q-W-1)

Codex's executing entrypoint (contract section 12 item 6) does not exist.
Two options, for Codex's decision: (a) the first hosted run waits for it;
(b) a minimal reviewed stand-in run as `C_E`, which opens the
`entrypoint` journal, writes an empty Q1 inventory and closes COMPLETE,
and touches no server or credential. (b) would exercise the database,
launch, stop, registry, drop and finalizer stages end to end with zero
vectors; it would prove nothing about conformance. No default is assumed.

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
5. **W5** the workflow job itself (`workflow_dispatch` only, inert on
   publication), after W1–W4 are approved; its first execution is a
   separate admission.

Questions: Q-W-1 (section 7); Q-W-2: agree that the workflow imports
`FAILURE_STAGES` and `STOP_VERDICTS` rather than restating them; Q-W-3:
agree that pins are workflow-file literals (section 3).
