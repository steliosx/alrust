# Held venv executable audit

This conformance component implements the executable-alias step of the
reviewed binding plan at `71dc4763c83354d9c536bc00dd07f6d511fe9ae5`.
It performs read-only Linux filesystem inspection. It neither executes an
interpreter nor reads its contents, imports a reference package, inspects a
process, captures HTTP/SQL, submits a job or changes a workflow.

`VenvExecutableWindow` takes independently authenticated private-root and
venv path/device/inode identities, a reviewed runtime alias, and an absolute
local monotonic deadline. The root, venv and `bin` ancestors must be real
directories. Root and venv are current-UID, mode 0700. The fixed aliases
`bin/python` (build) and `bin/python3` (launcher) must resolve to the same
actual executable device/inode. Their unresolved path strings need not match.
The reviewed runtime alias must be one of those two names; a served declaration
cannot supply or expand that choice.

Explicit link traversal holds each symlink with Linux `O_PATH|O_NOFOLLOW`,
reads its held link text, and holds every traversed directory and terminal
regular file. A venv executable may resolve outside the job root, including
through base-interpreter directory aliases. `..` in a target is traversed
after prior symlink resolution, rather than lexically normalized. Each alias
has at most 40 links and 256 path steps; link text is at most 4096 characters.
FIFOs, directories, non-executable regular files, missing paths, loops and
disagreeing targets refuse without reading or executing the target.

Entry, explicit `check()` and normal exit compare held and named entries.
Replacement of a link, directory or target, target metadata changes, or deadline
expiry invalidates the window. Directory identity/mode/owner are checked;
changes to unrelated directory contents do not invalidate an identity. The
deadline is supplied once, no more than five seconds away on entry, and never
refreshed. Future capture composition must preserve the original observation
deadline, not create another after the request.

All owned descriptors close on failures including unexpected exceptions.
An otherwise successful exit with an unsuccessful close is a failure. A
secondary cleanup error preserves an existing setup/check/caller error;
no cleanup-success claim is made. Same-UID mutation and executable/loaded-code
history between observations are outside this component's guarantees.

The returned `interpreter_identity` is an actual filesystem observation for
the independently expected venv layout. It is intended as input to the
separately reviewed process audit, not a process attestation or interpreter
trust pin. Installation/source pins, closed launch-stage binding, current
P/C, peer attribution, runtime origins, original capture clocks/deadline,
database qualification and export custody remain separate prerequisites.

All tests use labeled synthetic executable files or injected filesystem
observations. LOCAL_PASS proves those controls, not reference startup, a
composed qualifier, Alerta/Rust parity, hosted Phase 0 or shared-VM qualification.
The new exact source head requires Claude's independent review before use in
an executing reference qualifier and before any fresh signed test admission.
