# Refusal entrypoint source boundary

Codex's first E1c executable component, on reviewed design
`93b0dee5e707b39cf9bd1767f98d92660d82951e`. The wrapper is
`scripts/refuse-entrypoint.py`; its module is
`alrust_conformance/refusal_entrypoint.py`. No existing entrypoint, profile,
workflow, implementation or project STATUS/DECISIONS file changes.

## Invocation and expectations

Run only as C_E under the reviewed, /3-compatible P_E. It accepts exactly
one occurrence of each pair:

```
--job-root /absolute/normalized/private/job
--core-sha256 ORIGINAL_FULL_SHA256
--prefix-digests ORIGINAL_CREATE_SHA,ORIGINAL_CREATE_EXIT_SHA,ORIGINAL_LAUNCH_SHA,ORIGINAL_CONFIRM_SHA
--launcher-exit 0
--confirmation-exit 0
--probe refusal-cleanup
--deadline-monotonic-ns FIXED_D
```

The workflow retains the original core and complete closed prefix digests
from each producing step's outputs, never selects new expectations from later
bytes. The two exits are **declared**, never observed by this script and never
A2 proof. Only PATH/LANG/LC_ALL environment names are allowed. Arguments are
public; no credentials, vectors or test-node selectors are accepted or printed.

The original core digest binds its whole source/run/attempt/profile/root/pin
identity; `validate_core` performs its existing exact schema/type/uid/root checks.
This probe additionally requires /3 and the current boot. It checks the exact
four-stage prefix with the existing pure journal parser, full original hashes,
COMPLETE results, monotonic ordering, core and previous-stage chains.

The workflow and P_E must enforce the **same** fixed D, the original bound J
from the producer's deadline-file digest, and the E1a budget/reserve bound
`D <= min(start + budget, J - R - B)`. This component checks actual same-boot
monotonic freshness repeatedly against D; it does not discover or renew J,
authenticate a caller's declarations, or provide a resource admission.

## Reads and sole write

`HeldReader` opens every absolute root ancestor from `/` with no-follow directory
descriptors held. Root and journal directory must be caller-owned0700. It reads
only the core and stages0–4: caller-owned regular0600, one hard link, bounded to
EOF, with descriptor metadata stable before/after reading. Ancestor identities
and leaf identity/size/mode/owner/link-count/mtime/ctime are rechecked throughout.
The pre-open journal set must be exactly stages0–3. No neighboring job, receipt,
secret/config, socket, database, reference process or protected VM is inspected.
These are accident checks; same UID is not a security boundary or filesystem lease.

The sole write is index4 `entrypoint` via the actual existing `StageJournal` and
`PrivateJournal` exclusive/fsynced mechanism. It records fixed public probe
metadata, sets `infrastructure_refusal`, and raises an internal intentional
exception to produce stage_complete FAILED. It then reads the actual closed
file and validates the original core/previous links, exactly three records and
the fixed FAILED terminal payload. An incomplete close cannot masquerade as F1,
even if StageJournal propagated the intentional exception after a write failure.

Ordinary probe output is exactly an execution FAIL / journal FAILED_VALID report
with fixed reason infrastructure_refusal and exit2. It proves only failed journal
bytes, **never** process exit, cleanup, finalization, reference serving or normal
conformance success. Unknown/write/close errors report UNPROVEN/exit75, policy
refusal UNPROVEN/exit65, interrupt UNPROVEN/exit130; no raw exception/input/path
is printed. SystemExit remains an interruption rather than deliberate refusal.

## Import/call classification and controls

The policy module imports only hashlib/json/os/re/stat/time definitions. The
real FileRuntime lazily imports stage_journal -> security_evidence; their
module definitions use stdlib only. Production calls reach bounded fd reads,
root/boot validation, monotonic/time capture and StageJournal/PrivateJournal
exclusive writes/fsync. They do not call stage_journal.core_main/_git_head,
fork/exec/signal/HTTP/SQL/Docker/SSH/VM operations. Wrapper import/path setup
does not qualify actual Python/module binding; dependency digests must be
checked as literals in the separately reviewed eventual combined workflow.

Only the named `test_refusal_entrypoint_pure.py` ran. It uses MemoryRuntime,
MemoryJournal and the **actual pure journal parser**. Core/root/pin validation
is explicitly stubbed. Additional HeldReader controls replace its whole os
global with MemoryOS before construction; only flag constants are forwarded,
no real OS function. Main controls replace the whole FileRuntime constructor
with MemoryRuntime and use StringIO. No actual FileRuntime/PrivateJournal,
clock, filesystem adapter, process, socket, SQL, HTTP, VM or neighboring tests
were executed. Loader reads and evidence-file writes are outside these adapters.

Controls cover strict malformed/public CLI, each changed prefix, original bad
journal syntax, extra/index4 files, wrong core/schema/boot/environment, deadline
edges, ancestor changes before/during/after opening, write/close/interrupt and
missing terminal failures, public error redaction, one-byte short reads to EOF,
ancestor/leaf replacement, in-place metadata changes, nonprivate/hardlinked/
oversize/nonregular files, and descriptor cleanup on constructor failure.

Parent dependency differs from the current W head only in StageJournal's W3
exposed core_identity_sha256/previous_stage attributes and their initialization;
this component does not consume them. validate_core, read_bounded, boot_id,
_parse_journal, strict_json and their pure validation helpers are identical.
Actual composition on the W head and f8 supervisor correction still need review;
component approval is not combined-source approval.

Claude independently reviews this exact executable and the combined workflow.
Publication/merge and S0/S1 dispatch require their separate signed admissions.
S0 only pins this real script and never runs it; S1 must assert this exact failed
probe result and independently prove P_E/exit/stop/A2/drop/finalizer outcomes.
An unexpected stage_failed fault must not be accepted as the intended probe.
S2/E2/E3/E4 and all19 profile execution remain separate. F3CD hold, owner gates,
cooperative heavy exclusion and ARM64 deferral remain unchanged.

## E1C-S-N1 diagnostic correction

Refusal exit65 stays FAIL/UNPROVEN. Its reason now preserves only exact strings
from the fixed PUBLIC_REFUSAL_CODES vocabulary; unknown arguments, multiple
arguments, non-string objects and subclasses collapse to probe_refused without
stringification. This distinguishes a measured core_changed before index4
opens from refusal_journal_unproven after a closed journal fails validation.
The identifier is diagnostic only: journal bytes and P_E/A2 still decide the
evidence state and process closure. Close failures still override diagnostics
to runtime_error/interrupted; nothing acquires COMPLETE or conformance credit.

Named memory controls cover both phases and every public identifier, plus
private text, multiple/empty arguments and an object that forbids __str__.
No actual FileRuntime/OS/journalwriter/CLI/process call is newly exercised.
Any module/dependency literal in W5b must use this changed exact head, after
fresh independent review. The wrapper remains unchanged.
