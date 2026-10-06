# Private raw response custody component

This is a standalone offline component, not an activated profile or an HTTP
integration. Its parent is the separately reviewed bounded-client head
`9c376139e581e6c9187ad39ce99e13648f9eeef9`. That client's approval does not
cover this new source, its writer changes, or future client integration.

The constructor takes the same actual collecting `PrivateSecretWriter`, active
entrypoint `StageJournal`, absolute private job root, independently expected
core digest, fresh actual `RawBudget`, and explicit per-response byte cap.
`max_responses` must be between 1 and 999999. The execution plan must choose a
small response count and per-job byte cap that also bound journal/removal size;
the upper representation limit is not a recommended operational limit.

`reserve_and_append(bytes)` owns budget charging and writing together. Charges
precede bytes and are never refunded, including after partial writes. Direct
calls cannot bypass charging, and accidental external budget changes refuse.
`remaining()` returns the minimum per-response and per-job capacity; empty
bytes are valid at the exact cap. Only one response may be pending. The counter
comes from the bound budget, never directory contents.

Allocation uses fixed `entrypoint-raw-response-NNNNNN.bin` names at a held
0700 root with exclusive creation, NOFOLLOW and CLOEXEC. Before data, it checks
regular-file UID/0600/single-link identity, records existing `owned_resource`
metadata in the same actual journal, and fsyncs the root directory. Every
operation checks actual writer/stage/core/process/thread/budget binding and
held root/file identity and size. File finish fsyncs, revalidates and closes.
Raw files and their values/digests are never public journal evidence or export
candidates. Finished files remain private for the existing finalizer; this
component does not run or certify the finalizer.

An explicit clean `close()` (or clean context exit) is mandatory before
`writer.finish()`. The writer refuses a pending response or an unclosed sink
before snapshot allocation. This is stricter than idle alone, which would
leave a root descriptor open. One exact concrete sink may register with a
writer; arbitrary idle callbacks and replacement sinks refuse. The session or
executor must separately prove all captures/roles/discoveries and writer
generations complete. This check is not that proof or an R-EP substitute.

Any I/O, identity, binding, budget or lifetime failure permanently poisons the
sink, budget and writer. Original interrupts remain the primary exception;
secondary abort/close failures cannot replace them. Handles are attempted once
and ambiguous close failures are not retried. Failed files, including an empty
allocation whose ownership record failed, remain private; no failure unlinks
secret bytes or claims cleanup. HOOK8-N1 and reviewed failure-only cleanup
remain executor/finalizer prerequisites.

Git/mailbox placement checks refuse occupied `.git` metadata, gitfiles,
symlinks, bare repository metadata and mailbox-named ancestors. Empty sandbox
`.git` directories do not establish a checkout. This is accidental-placement
protection, not a boundary against a same-UID process changing metadata,
renaming paths or mutating Python objects. The executor must independently
bind the approved private job root and actual source/budget/client generation.

This class deliberately does **not** implement the old `PrivateResponseSink`
interface, so injecting it into the old client refuses before HTTP. A new
client head must move its response-count and byte transactions into the sink,
use `remaining()` and `reserve_and_append()`, and obtain fresh independent
review. There is no claim of combined client/capture/session execution,
deadline enforcement, SQL/HTTP qualification, full Phase 0 or Rust parity.

Offline controls use synthetic values and temporary private files only. They
exercise ownership and directory durability before bytes, short/no-progress
and interrupted writes, cap exhaustion/no reset, failed fsync/close/record,
occupied paths, file/root/core identity changes, journal identity, finish
ordering, constructor refusal and legacy-client rejection. No server, socket,
SQL, child process generation, service, shared VM or microVM is started.
