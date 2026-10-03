# Closed launch prefix reader

Read-only conformance component extending the approved binding plan. It
starts no process, executes no receipt content, signals nothing, imports no
reference packages and exports no evidence. Synthetic controls use files and
stage journals with fictitious process/interpreter identities.

Call `read_closed_launch` with independently authenticated invocation context,
the earlier `read_binding_inputs` result, actual launcher and confirmation
exit codes, and original per-stage digests for the complete closed prefix.
The prefix ends `launch`, `launch-confirm`. Each original name/digest is a
caller input; inspected journals cannot choose or authenticate those pins.
A `BindingInputs` object is data, not an authorization capability. The caller
must retain approved source identities and the real invocation outcomes.

The reader refuses either nonzero exit code even if COMPLETE bytes survived a
late fsync/close failure. It checks all original hashes, typed envelopes,
sequences, core identity, boot, monotonic ordering, previous-stage links and
COMPLETE results using the existing stage-journal parser. It then validates
exact launch receipt/readiness/handoff payloads, their required counts and
order, recorded P/C generations, interpreter declaration equality and
observation times within the recording intervals. Generic aggregate VALID
alone is insufficient. The launch-confirm journal must have exactly its
begin, handoff and completion records, as the approved producer writes it.

Call before creating the next stage journal: extra directory entries refuse.
All files are bounded owned 0600 regular files. Root ancestors are opened
without symlink following; root identity and owned 0700 reference/journal
directories are held and rechecked. Core/launch bytes must still equal the
previous independently pinned reader result. File handles and named entries
are rechecked before return. All handles receive close attempts; normal close
failure refuses and a secondary close failure preserves a primary exception.
These checks detect ordinary replacement; same-UID access is not a security
boundary and observations cannot prove unobserved history.

`deadline_ns` is the original absolute local monotonic deadline, strictly in
the future and at most five seconds away at entry. It is checked through
reading, parsing, final rechecks and normal close, never refreshed. This
component cannot interrupt arbitrary kernel/Python operations. Future marked
capture composition must pass the exact `ObservationWindow` deadline rather
than reconstructing a later budget.

The dependency helpers and journal schema are those already reviewed on
`90b77847a7f27c58963028bf4a6f0ac391e5b869`; unchanged byte-reader implementation
`4b50c29f709f35d81671baa2f9b10396ea68c47efbf3189624f79225d4cda257`.
Payloads match the independently reviewed launcher/confirmation source
`effe409933390c75d2062f3a2e1fef4aedd00703`; no producer schema is changed.

Returned identities and times remain historical declarations. This component
does not inspect current P/C, actual venv/interpreter, installed origins,
observer activation, marked HTTP capture or database connection. The later
held audits and executing composition need their own exact-source reviews.
Real reference execution, full Phase 0, hosted/shared-VM qualification and
Rust parity remain NOT_RUN. No workflow or permission changes are included.
