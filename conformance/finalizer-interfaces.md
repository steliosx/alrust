# Finalizer inputs (proposed interface)

Status: **proposed**. Nothing writes these records yet. They define what
the finalizer (contract section 9) will consume, so that the stages that
produce them (the launcher wiring, the database stage, the credential
registry stage and Codex's executing entrypoint) can be written against
one shape.

## One source of truth

The finalizer reads its inputs **only from stage journal records**, and
only after `stage_journal.aggregate()` has returned `VALID`: every
journal complete, chained and every stage `COMPLETE`. It never accepts
these values as command-line arguments or caller-written JSON.

Each record below is the `payload` of a stage journal record of that
`kind`. All paths are absolute, normalized and inside the job root; `dev`
and `ino` are those observed when the resource was created.

| kind | payload | written by |
|---|---|---|
| `owned_resource` | `{"path", "dev", "ino", "type": "file" or "dir"}` | the stage that created the resource |
| `credential_registry` | `{"path", "dev", "ino", "sha256"}` | the registry stage, before the database drop |
| `export_candidate` | `{"path", "dev", "ino", "max_bytes"}` | the stage that produced a file meant for export |
| `obtained_secret_count` | `{"count"}`: a count only, never values | Codex's entrypoint |
| `stop_verdict` | `{"verdict", "reason"}`: the stop helper's own output | the stop stage |
| `database_drop` | `{"verdict"}` | the database drop stage |

## Credential registry

- Path: `job_root/credential-registry.json`, 0600, written exclusively.
- Content: `{"schema": "alrust-credential-registry/1", "secrets": [...]}`.
  It holds every actual secret of the job: every `keys.key` value read from
  the job database, the provisioner's role keys, `SECRET_KEY`, the database
  password and URL, and every secret Codex's entrypoint reports.
- Every secret must be at least 16 UTF-8 bytes. A weak value is refused,
  not scanned, because a short common string would match unrelated bytes.
  **Consequence for the executing workflow:** the database password must be
  random per job, like `SECRET_KEY`. The current scaffold's `postgres`
  password must not be used with the finalizer.
- The finalizer verifies the file's sha256 against the
  `credential_registry` record before using it.

## Scanner

Each export file's final bytes are searched for every registry secret, raw
and JSON-escaped, and for fixed generic patterns:

- `SYNTHETIC-` followed by 48 lowercase hex characters (the synthetic key
  format);
- a PostgreSQL URL with a password;
- an `Authorization:` header;
- `Bearer` followed by a token;
- `-----BEGIN`.

A finding names the file and the kind of match, never the matched bytes. An
unreadable file is a finding, never a clean result.

## Guarded removal

An `owned_resource` is removed only if its path is inside the job root, has
no symlinked parent, has the recorded type and owner, and has the recorded
`dev` and `ino`. Directories are removed with the symlink-safe `rmtree`.
Absence is verified afterwards. Anything else is left untouched and
reported `UNPROVEN`. A resource that is already absent is reported
`ABSENT`, which does not count as a proven removal.

## Not in this head

- The orchestrator: the quiescence gate, the export staging directory, the
  finalizer's own journal, the terminal cleanup receipt, the manifest and
  the seal.
- The failure-only record (contract section 9, step 1a).
- Redaction. Candidates are copied byte for byte, and the scan decides; no
  redaction rules are defined yet.
