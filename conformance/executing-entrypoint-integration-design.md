# Executing entrypoint: integration design for review

Codex-authored proposal for Claude's independent exact-head review.
Base: `20cb9057265cc8d1861882c477acfb79b67bc37c`, the reviewed combined
R-EP/PR #100 source. This document adds no executor, fixture, workflow,
profile enabling, process, HTTP, SQL or export. All proposed behavior is
**NOT_RUN**. Design agreement does not approve its eventual implementation.

It supplies the Codex side of `docs/ci-launch-attestation-contract.md`
sections 11–12 and answers the entrypoint choice in Claude's proposed
`f22d48706710e090c4c6e173f14fbecb8830de86`: wait for a real reviewed
entrypoint. An empty COMPLETE stand-in is not the conformance entrypoint.
A separately reviewed zero-vector infrastructure smoke would need a
separate identity, result and admission; none is proposed here.

## 1. Existing source and limits

At this exact base:

| Existing component | Source-derived behavior | Integration still missing |
|---|---|---|
| `alrust_conformance.ci.inspect_profile` | Checks the inert manifest and all module/dependency selections; returns NOT_RUN, never executes | A separately reviewed executing plan and enabling source |
| `private_secret_writer.PrivateSecretWriter` | One process/thread owns the open entrypoint journal and durable secret history; clean exit after explicit finish records the bound inventory | Executor must feed every discovery and prove completeness before finish |
| `captured_api_key_flow.make_captured_api_key_session` | Constructs the concrete sink/client/capture/session on the same entered writer and journal, with explicit bounds and deadline | No HTTP fixture selects it yet |
| `CapturedApiKeySession` and `ApiKeyCapture` | Preserve the ordinary owned-key oracle while capturing known secret fields before assertions; capture failures poison completion | No profile-wide discovery or stored-role proof |
| `observer_closed_launch.read_closed_launch` | Reads the closed prefix before the next journal opens; independently supplied original hashes/exit codes required | Closed bytes are not current process, interpreter or serving-connection qualification |
| `entrypoint_exit.run_entrypoint_exit_stage` | Separate exit stage recomputes closure/execution from supervised receipts | The entrypoint cannot attest its own positive whole-tree exit |
| `database_stage.run_registry_stage` | After writer quiescence, reads the bound inventory, requires value-set inclusion, then records the count linked to the registry digest | A count cannot establish that the executor discovered every required value |

The ordinary `tests/http/test_api_keys.py` fixture still reads environment
paths and constructs `ApiKeySession`/`Client`; it does not use the captured
constructor. `tests/conftest.py` also constructs an ordinary client. These
are source findings, not new HTTP results. Neither is a safe fallback for
the proposed executor. All nineteen `hosted-profiles.json` profiles remain
disabled. Existing component approvals and synthetic results retain their
original source and scope; composition needs new review.

## 2. Two boundaries: pre-allocation and execution

**Proposed:** keep the existing inert `ci.py` CLI and its hosted job
unchanged. Add a distinct executing entrypoint only in a future reviewed
head. A reviewed plan check happens *before database allocation or server
launch*: profile/source/manifest/module/config/dependency/adapter pins,
exact selection, target, budgets and credential requirements must match
independent reviewed expectations. Disabled profiles return NOT_RUN with
no allocation, credentials or artifact allowlist; malformed inputs fail
closed. No `--force`, environment toggle or manifest rewrite enables them.

An enabling head must define the executing-plan schema and replace the
current inert-only assumptions explicitly; this design does not reinterpret
`inspect_profile` as an executing validator. Expected source/manifest pins
come from the approved workflow/enabling head, not measured receipt bytes.
The plan must pin the selected configuration and imported fixture/adapters,
not merely the selected test module. No arbitrary caller node selection or
fixture/plugin path is accepted.

The workflow owns the job outcome from the first database action onward.
A prerequisite failure discovered by the entrypoint after that point is
FAIL/CANCELLED with cleanup UNPROVEN until actually proven, never a fresh
NOT_RUN. The workflow preserves partial results; a late metadata, capture,
exit or finalization failure cannot discard them or turn them into a pass.

## 3. Supervised invocation and inputs

**Proposed:** the entrypoint executes only as `C_E` under the reviewed
`entrypoint-supervisor.py` generation `P_E`, with the command grammar and
/3 core pins enforced before fork. It does not start a supervisor itself,
select a runner or clear a hold. Claude retains workflow, reference-launch,
exit/stop, database/registry/drop and finalizer ownership; Codex owns this
entrypoint, capture adapters and suite. STATUS/DECISIONS stay with Claude.

The future strict CLI includes the contract's profile, manifest digest,
core/launch/database evidence, observer socket, reference venv, evidence
root and exact loopback target. A later schema head must specify all
required options, bounds, expected digests and frozen source/run/attempt/
profile bindings. Duplicate, unknown, malformed and secret-valued options
refuse. The workflow and entrypoint invocation accept no secret-valued argv or
environment; workflow expressions and shared summaries contain no role
values or generated secrets. A future supervised provisioner must separately
review the reference CLI's actual credential handoff, private output and
child lifetime; a declared path alone is not proof of provenance. Raw
exceptions and request output stay private.

Read-only closed-launch checks run before opening index 4 `entrypoint`;
otherwise the prefix reader's exact-set rule would refuse the new journal.
Independent workflow evidence must retain the original stage digests and
actual launcher/confirmation exits. A record may not select its own expected
hash. A reference receipt or ready line alone does not qualify the current
serving process, observer activation, executable/module origins, stored
roles or connection. The external qualifier and served-connection adapter
remain implementation and execution prerequisites; do not fabricate them
from core declarations or a second app/database factory.

One absolute entrypoint deadline is fixed immediately before launch by the
workflow, capped by its remaining job/cleanup reserve and manifest budget.
The client deadline is at or before that same-boot supervised D, never
renewed per request. Existing HTTP I/O/cooperative checks are not a hard
whole-tree timeout. Positive closure comes later from the separate exit
verifier, not a file, finally block or elapsed deadline.

## 4. Capture and profile completeness

**Proposed:** a profile-specific adapter receives the actual entered
StageJournal and `PrivateSecretWriter` as objects on the same process/thread.
It must use the concrete captured-flow constructor. Missing, foreign,
closed or already-used custody refuses; no environment/global writer or
ordinary client/session fallback. All fixture dependencies and actual
imports must be reviewed before selection.

For the candidate `api-keys` profile, the approved inventory requires the
four distinct synthetic role keys, their actual stored customer/scope/
validity and same provisioning-generation binding, plus the session's
new key and every later discovered value, including revoked history.
Reading a credential file and calling remember on four strings establishes
capture only, not stored-role proof. A role provisioner and its observation
interface require their own exact-source review. Existing Q1/discovery
contracts apply: generated values are durable before command/request use;
returned values are durable before subsequent assertions/use. Values stay
inside the supervised credential-holding tree from first creation/read.

Completeness is actual value-set membership tied to the independently
required role/session identities, not a minimum count, response label or
empty inventory. Expose a narrow reviewed completeness check rather than
reading a writer's private fields from arbitrary fixture code. Unsupported
secret-bearing response shapes, divergent href, malformed JSON and missing
role proof latch failure; private raw bytes may remain for diagnosis but
cannot justify COMPLETE or normal export. Existing API/customer/scope/
expiry expectations and reference customer filtering remain unchanged.

Other profiles need their own adapter/discovery coverage and exact enabling
head. This first composition cannot silently satisfy all nineteen profiles.
The existing ordinary API-key vector stays unchanged unless a reviewed
adapter injection requires a narrowly justified fixture change; no easier
assertion, omitted prerequisite or removed vector earns gate credit.

## 5. Test result, closure and finalization handoff

**Proposed:** selected tests run in-process under `C_E`, with only explicit
reviewed pytest plugins/fixtures, fixed selection/configuration, inherited
addopts removed and third-party autoload disabled. Do not invoke a nested
pytest subprocess or activate process-capable fixture paths opportunistically.
A later result-plugin schema must bind the exact collected node identities
and setup/call/teardown outcomes to the reviewed required set. Exit 0 alone
is insufficient: missing/extra nodes, skip, xfail/xpass, deselection, empty
collection, failed fixture teardown or cancelled work cannot complete.
Private captures retain sensitive output; public journal metadata uses
fixed codes/counts/bound digests, never node/exception text that can contain
credentials. Any additional pytest-created files must be explicitly owned
and bounded; unaccounted root entries remain a finalizer refusal.

Only after successful required execution, complete durable discovery,
fixture/session cleanup and explicit client/sink closure may the entrypoint
call `writer.finish()`. The writer's successful context exit rechecks and
closes handles before emitting `private_secret_inventory`; then the actual
entrypoint journal may close COMPLETE. A swallowed failure/interrupt or
secondary close error cannot permit that sequence. Keep the primary failure
identity and private partial evidence. There is no writer-completion repair,
backfill, truncation or silent retry.

Entrypoint completion remains provisional. Claude's separate entrypoint-exit,
reference stop, holder proofs, registry inclusion, exact-resource database
drop, direct finalizer rechecks, final-byte scan and sealed-manifest upload
gate retain all existing checks. The executor emits no overall profile
PASS or cleanup COMPLETE on its own. A failed entrypoint can take only the
reviewed A2 database-drop exception with positive proofs; it does not acquire
registry/full-finalizer/export authority. Only existing failure_record may
write its fixed private failure schema when its actual prerequisites hold;
writing that record does not authorize upload.

## 6. Bounded implementation/review order

**Proposed, for agreement before implementation:**

1. **E1:** strict executing plan/invocation and closed-prefix input validator,
   source only. Keep current inert CLI/profile manifest untouched. Synthetic
   controls: wrong /3 pins, core/source/run/attempt/profile, replaced prefix,
   duplicate/extra stages, invalid deadlines and disabled-plan refusal, with
   spies proving no network/subprocess/credential allocation.
2. **E2:** exact pytest result/completeness state machine and custody handoff
   using explicit fake runner callbacks and streaming MockTransport. Negative
   controls cover missing roles/nodes, skips, fixture teardown, capture/fsync/
   close faults, primary interrupts and no completed inventory on any failure.
   No real pytest HTTP fixture, fork, server, SQL or role provisioning.
3. **E3:** composed supervisor-facing entrypoint and profile adapter only
   after the separately reviewed role/observer/served-connection producer
   interfaces exist. Fresh source review covers actual call/import/child
   paths and compatibility with Claude's exact executing-workflow head.
4. **E4:** separate profile-enabling head and a separately admitted bounded
   hosted reference run after all prerequisites. Actual result/cleanup/log
   evidence required; no queued/unstarted job, scaffold smoke or mock earns
   behavioral credit. No Rust candidate parity claim.

Named pure controls require classification under policy v1.1 before import.
Every changed base/head changes review scope, with exact-byte evidence reuse
identified explicitly. The local F3CD cleanup hold remains; no rescan, helper
retry, rebaseline, real process/HTTP/SQL run or signal is proposed. No dispatch,
runner registration/switch, shared VM/microVM qualification, cutover or VM
retirement occurs. Owner merge/release/security-access/production gates remain;
ARM64 is deferred. Missing production PIN inputs block their dependent phases,
not this synthetic source design.

Please review this document at its full commit, and agree or correct the
E1–E4 boundaries. No proposed API or result is implemented by this head.
