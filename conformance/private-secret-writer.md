# Private discovery writer (Q1 component)

Codex-authored source for Claude's independent exact-head review. It implements
the file writer in the approved Q1 design/passive-codec head
`6e2bfcc82fc3415f52e5b2e5a6ea43903ab79651` (Claude `575786b6`). That earlier
approval does not carry to this new writer. No provisioner, pytest bridge,
registry reader, workflow or HTTP/SQL execution is enabled here.

`PrivateSecretWriter` runs inside an actual open `entrypoint` StageJournal,
on one process and thread, under a held 0700 UID-owned root. Before creating
files it validates the independently supplied frozen core digest, typed core
and boot, and the open stage's core link. The two fixed Q1 paths are exclusive
0600 private files, immediately recorded as owned resources. No caller-supplied
filename, overwrite, adoption, glob deletion or fallback path is used.

```python
with entrypoint_stage as journal:
    with PrivateSecretWriter(job_root, independently_verified_core_digest, journal) as capture:
        # Generated values are saved before any command/request uses them.
        capture.remember(generated_secret)
        # Returned values are saved before subsequent requests or assertions.
        capture.remember(obtained_secret)
        capture.finish()
```

The example is the component's call order, not a runnable provisioner. Future
adapters must call it at every discovery path; existing HTTP modules have not
been wired. Child processes receive no writer handle or independent writer
permission. Actual closure of every test/capture writer remains a separately
reviewed executor obligation, not evidence inferred from this object's exit.

Each distinct value is appended as a complete private JSONL discovery record
and fsynced before `remember` returns. A repeated value is already retained and
is not counted again. Deleted/revoked keys remain in the history. All values
and the prospective snapshot pass the approved codec's byte/count bounds.
The registry's complete union must still fit its stricter 64 KiB bound; this
component makes no fit guarantee and never truncates a union.

`finish()` exclusively writes and fsyncs the final private snapshot and parent
directory. It does **not** emit a completion record. Successful context exit
rechecks root, event identity, live core/stage link and final snapshot identity
and bytes, closes its owned handles, clears in-memory values, and only then
records `private_secret_inventory {path, dev, ino, sha256, count}`. No secret
bytes or individual secret hashes enter the stage journal. The object repr
and all reported errors are fixed and contain no credential bytes.

A captured error poisons the writer even if caught inside the context; it
cannot later finish normally. Missing explicit finish, a changed root/file,
invalid discovery, interrupted write, fsync/close error or journal-registration
failure cannot emit a COMPLETE inventory. Private partial files stay in place;
the component never removes them or claims cleanup. A closed/incomplete entrypoint
uses the existing failure-only path, not a completed inventory/normal export.
The executor must propagate writer failures to the stage context; catching and
suppressing the writer context itself is not an approved execution pattern.

The active-stage check currently uses `StageJournal`'s internal `_root`,
`_name`, `_journal._fd` fields because no public accessor exists. This dependency
is explicit, covered by wrong/closed/rebound stage controls, and must be changed
and reviewed if StageJournal's interface changes. The writer does not change
Claude's StageJournal implementation.

Focused local controls use only private temporary files, actual stage journals
and one joined Python thread. They cover ordinary/revoked/duplicate discoveries,
explicit zero discovery, no completion before handle closure, core/stage/root
rebinding, occupied regular/link/FIFO paths, file identity/permission/hardlink
changes, partial writes, input validation, fsync/close/record failures and a
closed or foreign writer. These are offline component results, not actual
reference authentication, stored-key proof, service-log exclusion, registry
inclusion, finalizer integration, hosted profiles, Phase 0 or Rust parity.

Same-UID substitution between checks remains outside a claimed security boundary.
The registry reader still must independently pass quiescence, verify closed stage
and inventory identities/hashes, require actual value-set inclusion, and emit the
registry-stage digest-linked count. A future composed executor and any heavy job
need their own exact-head review and fresh signed admission/exclusion.
