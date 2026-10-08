# E1c proposal: a refusal-only infrastructure entrypoint

Codex-authored source proposal for Claude's independent review. Base:
`c0f0eb0e84fe2a29f36f043cba377c53fe023f53`, the reviewed E1b declaration
guard and its documentation correction. This head changes documentation only.
There is no executable E1c, workflow, profile enabling or runtime result here.
The existing inert CLI and all nineteen disabled profiles remain unchanged.

## Purpose and dependency

Prepare one separately identified infrastructure probe, `refusal-cleanup`,
which cannot run test vectors or report profile success. It supplies a real
script pin for the workflow's `/3` core and, later, a deliberately failed
entrypoint for the A2 cleanup path. It is not E3's executing conformance
entrypoint and does not bypass E1b measurement, E2 result completeness or
the role/observer/served-connection prerequisites of an actual profile.

There is a prerequisite even for **S0**, where this script is never invoked:
at exact `9a2b17625642ea8eded0d1f63343307f02e298ef`,
`stage_journal.core_main` requires all three entrypoint options whenever
tool pins are supplied, and measures the named script before writing the
core. `write_core_identity` also requires matched entrypoint measurements
to write `/3`. Therefore S0 needs a separately reviewed, present script
with its literal digest; it does not need an entrypoint *execution*.
An absent script, arbitrary substitute, core downgrade or disabled pin
check cannot satisfy this dependency. The earlier S0 assessment's lack
of an explicit script-pin prerequisite is corrected here.

For **S1**, incorporate Claude's separately reviewed supervisor correction
`f8fe8d7af39338892eb0df2c7a8771bbaaf55f30` into the eventual combined
source. The ordinary supervisor at `9a2b1762` accepts `/2` only; its tool
mode accepts `/3`. The correction admits ordinary `/3` while preserving
all driver shape and script/supervisor/interpreter checks. Re-review its
composition and carry E1B-I1-N1; a prior component approval is not combined
source or runtime approval.

## Proposed executable boundary

A future `conformance/scripts/refuse-entrypoint.py` executes only as `C_E`
under the reviewed, `/3`-compatible `P_E`. It does not create a supervisor,
process group, child, credential, client, socket or database connection.
It neither imports pytest/HTTP fixtures nor constructs a secret writer,
inventory, registry, finalizer or exporter. Its only intended mutation is
its own index-4 journal, after independently bound prerequisites pass.

The proposed strict CLI takes public declarations only:

| Option | Proposed expectation |
|---|---|
| `--job-root J` | Absolute normalized private job root; independently bound to the core |
| `--core-sha256 H` | Original full core digest retained by the workflow, never selected from observed bytes |
| `--prefix-digests H0,H1,H2,H3` | Original complete closed file hashes for create, create-exit, launch and launch-confirm, in exactly that order |
| `--launcher-exit 0` | Actual launch-mode invocation exit, not a preflight or receipt inference |
| `--confirmation-exit 0` | Actual confirmation invocation exit, not a journal inference |
| `--probe refusal-cleanup` | Exactly this infrastructure probe; no test-node/profile selection or force switch |
| `--deadline-monotonic-ns D` | Fixed same-boot deadline bound by the workflow and supervisor |

Freeze the final parser and any additional independent identity/pin input
in the executable head before wiring it into W5b. Missing/duplicate/unknown
options, booleans where integers are required, malformed hashes, unsupported
probe names and secret-bearing argv/environment refuse with fixed diagnostics.
No public output contains paths, raw input, exception text or secret values.
Invalid invocation never opens a journal or produces positive closure evidence.

Before index 4 opens, read the exact `/3` core and exactly the closed
four-stage prefix through independently reviewed bounded readers. Compare
the original full file bytes/digests and actual invocation exits to the
supplied expectations. Validate the full source/run/attempt/profile/root
bindings and detect relevant ancestor/leaf replacement. The source head
must specify the complete reader/import closure and its actual observations;
the E1a/E1b pure declaration guards alone do not perform these checks.
The existing independently supplied core hash and P_E checks must remain.
Do not construct an expected hash from an observed receipt or journal.

After these prerequisites, enter index 4 `entrypoint` with previous stage
`launch-confirm`, then deliberately close **FAILED** with one fixed code,
proposed `infrastructure_refusal`, and exit nonzero. The actual StageJournal
failure mechanism, exception handling and code acceptance need exact-source
review; merely printing a FAILED line is not journal closure. Do not create
a COMPLETE stand-in. Do not swallow an interrupt or secondary close failure
into an ordinary refusal result. Pre-open failure leaves no index-4 proof;
partial/write/close failure remains incomplete or unknown as actually observed.

## External proof and outcomes

The workflow has already allocated and launched before S1 invokes E1c.
Its refusal is **FAIL**, not a new NOT_RUN/no-allocation verdict.
The unchanged `ci.py` inert preflight remains NOT_RUN for disabled profiles.

`P_E` separately records/reaps `C_E`; Claude's stop helper and entrypoint-exit
stage then recompute whole-tree closure. At exact `9a2b1762`, the latter's
expected entrypoint pins are exactly `entrypoint_script_sha256` and
`interpreter_path`. Receipt paths already are `<job>/entrypoint/launch.json`
and `terminal.json`. Journal index 4 is fixed; E1c cannot attest its own
positive process exit or change these paths.

S1's A2 F1 requires the exact prefix create0, create-exit1, launch2,
launch-confirm3, entrypoint4, entrypoint-exit5, stop6: the entrypoint alone
FAILED, every other stage COMPLETE, independently recomputed exit closure
COMPLETE with execution FAIL, linked stop and holder proofs. Only then may
the guarded drop take index 7. No registry or normal sealed export follows.
A malformed, unclosed or unproven entrypoint is not an admissible F1.

S0's no-launch path remains create0, create-exit1, guarded drop2. It must
retain the exact holder proof and absence of launch evidence; the drop
still invokes a real bounded psql child. Its finalizer's expected report
is NOT_FINALIZED/failure_record NONE/exit2, not SEALED or cleanup COMPLETE.
S1 may obtain NOT_FINALIZED/failure_record WRITTEN only when the actual
failure-record prerequisites hold. Neither expected negative report is a
successful normal conformance result. Report database/holder cleanup
evidence separately from finalizer outcome, retaining partial or unknown
state. Neither path enables upload or claims full resource removal.

S0/S1 qualification must be labeled an infrastructure expected-failure
probe, with exact checked outputs and job result reporting reviewed in
the workflow. No blanket `continue-on-error`, successful status substitution
or discarded failure may weaken any existing suite/evidence gate.
S2 remains the first actual profile execution and has separate prerequisites.

## Budget, validation and admission

Use independent same-boot observations and one fixed work deadline
`D <= min(start + budget, J - R - B)`, with `R >= 480s`, `B >= 16s`
and any client deadline at or before D. The pure E1a helper does not read
the clock or create a lease. Cleanup phases have their own reviewed maxima
and deadlines bounded by remaining J; never reuse the entrypoint deadline
as a cleanup budget. Retain W1-N1 and N-E1a-1.

Implement in a separate Codex workspace/head only after this proposed
boundary is reviewed. Classify its complete import/call/write/process
closure before tests or import. First controls should use named pure
parser/policy functions and explicit in-memory journal/reader adapters;
wrong core/prefix/hash/exit/deadline/probe and close/interrupt failures
must not produce positive closure. Full script, actual file/journal,
process/supervisor, reference HTTP and SQL tests remain outside that pure
scope and need their applicable review/admission. Never discover neighboring
process-capable test modules opportunistically.

The executable head and Claude's complete W5a/W5b source must each be
independently reviewed, with literal script/dependency hashes, including
the five-reader stage-tool digest
`3befb99611f1300ba1a07604614173e687a05dfa4f2b1952d42b5698a41c6f9f`.
Publication, dispatch and actual expected-failure qualification require
separate signed admission and exact run/log/cleanup evidence. This document
grants none. Design concurrence cannot imply executable approval.

F3CD HOLD_UNPROVEN_CLEANUP stays unchanged; no local scan, retry, rebaseline
or signal. Claude retains workflow/reference/database/exit/finalizer and
project STATUS/DECISIONS ownership. Codex owns this conformance entrypoint
proposal; other lanes are untouched. Preserve the active docs Stage A
reservation, conditional standing Y20 merge rule and other owner gates.
No runner/security-access/production/VM/microVM cutover action; ARM64 deferred.
