# Finalizer inputs and outputs

Status: implemented in `alrust_conformance/finalizer.py`. This is offline
source only: no workflow runs it, and no stage writes these records yet.
The stages that will write them (launcher wiring, stop, registry, database
and Codex's executing entrypoint) must produce exactly these shapes.

## One source of truth

`finalize()` reads its inputs only from stage journal records, and only
after `stage_journal.aggregate()` has returned `VALID`: every journal
complete and chained, and every stage `COMPLETE`. Each record is re-read
from journal bytes whose sha256 equals the aggregation's value. Nothing is
taken from command-line arguments or caller-written JSON. A record of the
wrong type anywhere refuses, with a fixed code and nothing echoed.

## Record kinds (journal `payload` of that `kind`)

All paths are absolute and normalized, inside the job root. `dev` and `ino`
are those observed when the resource was created. Integers are exact ints
(not bool or float).

| kind | exact payload | count | meaning |
|---|---|---|---|
| `owned_resource` | `{"path", "dev", "ino", "type": "file" or "dir"}` | any | a resource the job created and the finalizer must remove |
| `credential_registry` | `{"path": "<job root>/credential-registry.json", "dev", "ino", "sha256"}` | exactly 1 | the frozen registry; written before the database drop |
| `obtained_secret_count` | `{"count", "registry_sha256"}` | exactly 1, in stage `registry` | how many distinct secrets the entrypoint obtained; must link the registry digest, and the registry must hold at least that many. Written by `run_registry_stage` only after every inventory value was found in the frozen registry (value-set inclusion; the count is only a lower bound) |
| `private_secret_inventory` | `{"path": "<job root>/entrypoint-secrets.json", "dev", "ino", "sha256", "count"}` | exactly 1, in stage `entrypoint` | the entrypoint's frozen private snapshot (provisioner-secret-interface.md); read by the registry stage after the writer gate, decoded against the gate's core digest and this count |
| `export_candidate` | `{"path", "dev", "ino", "name"}` | any, unique names | a file to publish; `name` must be in `EXPORT_ALLOWLIST`, whose fixed bound applies; the source stage is recorded in the receipt |
| `stop_verdict` | `{"verdict": "COMPLETE", "reason": "tree_empty", "launch_receipt_sha256", "terminal_receipt_sha256"}` | exactly 1 | the stop helper's verdict and the receipts it was based on |
| `database_created` | `{"name", "oid", "role", "role_oid", "system_identifier", "postmaster_start_time", "server_version_num", "version", "psql"}` | exactly 1, in stage `database-create` | the allocated database's identity (database_stage.py) |
| `launch_handoff` | `{"launch_journal_sha256", "receipt_sha256", "supervisor", "child", "observed_monotonic_ns"}` | exactly 1, in stage `launch-confirm` | the hand-off observed alive (confirm-launch.py) |
| `database_drop` | `{"verdict", "reason", "database", "oid", "role", "role_oid", "system_identifier", "postmaster_start_time"}` | exactly 1 | the guarded drop's result; only `COMPLETE` with an identity equal to `database_created` counts |

## Quiescence (section 9.1), all before any read of the registry or any write

- The named entrypoint stage is among the stages and `COMPLETE`.
- The `stop_verdict` record's two digests equal the sha256 of
  `reference/launch.json` and `reference/terminal.json`. The terminal is
  `tree_empty` for exactly the launch receipt's supervisor `pid` and
  `start_ticks` (exact ints), and both are on the core identity's boot.
- **Positive exit observation:** `generation_ended(pid, start_ticks, boot)`
  must observe that this supervisor generation has ended. That means the PID
  is absent, held by another `start_ticks`, or a zombie whose pidfd reports
  exit, on the same boot. A live generation or a failed inspection refuses.
  No signal is sent.
- **Launch hand-off:** exactly one `launch_handoff` in `launch-confirm`,
  naming the validated launch journal's sha256, the stop verdict's launch
  receipt and the same supervisor generation (a COMPLETE launch journal
  alone is a syntactic record, LB4E-R2).
- **Database linkage:** exactly one `database_created` in
  `database-create` and one `database_drop` with verdict `COMPLETE` whose
  `database`, `oid`, `role`, `role_oid`, `system_identifier` and
  `postmaster_start_time` equal it. `ROLE_ONLY_COMPLETE`, `NONE_ALLOCATED`
  and `UNPROVEN` refuse. This is offline evidence; no real PostgreSQL has
  been exercised yet.

## Writer gate (registry and database-drop)

`writer_quiescence(job_root, prefix)` is the first half of the gate above,
for the stages that run before the finalizer. PREFIX ends with the stage
just before the caller's (at least through `stop`) and must contain
`database-create`, then `launch` immediately followed by `launch-confirm`,
then the entrypoint stage, then `stop` (DBFE-R1). It requires aggregation over the prefix VALID (aggregate's
exact-set rule), the linked stop verdict with the supervisor generation
positively ended, and the launch hand-off. It reads only immutable proof
inputs (core, closed journals, launch and terminal receipts), never the
registry, a credential, a log or the database, and does not require
`database_drop`. The stage runners evaluate it BEFORE opening their own
journal; a refusal then opens the journal only to close it FAILED with the
gate's code. A failed pre-launch allocation uses `no_launch_quiescence`
instead (database_stage.py).

On the launched path, the `database-drop` runner also requires, after the
writer gate passed, a frozen registry (`registry_frozen`, DBFE-R2): a
COMPLETE `registry` stage after `stop` holding exactly one
`credential_registry` record whose path, identity and sha256 match the
registry file. Otherwise it sends no SQL and drops nothing, so the database
cannot be dropped before its secrets are captured. A positively proven
no-launch cleanup never needs a registry.
- The registry's identity, digest and completeness, and every candidate
  record, are checked before `export/` is created.

## Outputs

- `export/` (0700 while being written, then 0500, files 0400) holds:
  - `finalizer.jsonl`, the finalizer's own journal;
  - `terminal-cleanup.json`, written after every removal: each removal's
    outcome, the stop and database evidence, the aggregation with every
    stage hash, the copied candidates (name, size, sha256, source stage),
    the number of entries left in the job root other than `export/`
    (`unaccounted_root_entries`: a count, never names; `null` if the root
    could not be listed through a handle bound to its recorded identity),
    and the cleanup verdict (`COMPLETE` only if every removal is
    `REMOVED` and that count is 0);
  - the allowlisted candidates, copied byte for byte;
  - `manifest.json`, written only after every file above was read once,
    scanned and hashed from the same bytes, and only if nothing matched.
- `finalize()` returns `{"outcome", "cleanup", "reason"}`, plus
  `manifest_sha256` when sealed. `NOT_FINALIZED` means the gate failed and
  nothing mutable was read, removed or exported; the caller takes the
  failure-only path. After the gate, every outcome is a fixed report: an
  I/O failure creating `export/` (`export_unavailable`) or the finalizer
  journal (`finalizer_journal_unavailable`) is `FAILED` with cleanup
  `UNPROVEN` and every input still in place. A failure after the removals
  reports the actual cleanup verdict. A failure to close the journal never
  replaces an earlier report and can only withdraw a seal. Partial evidence
  stays private (0700), and no digest is returned.
- `verify_manifest(export, manifest_sha256)` is the upload gate. The
  manifest's sha256 must equal the sealed digest. It needs the exact schema;
  typed and unique entries; the mandatory `finalizer.jsonl` and
  `terminal-cleanup.json`; only allowlisted other names; the
  terminal-receipt link; and every file present and re-hashed, with nothing
  extra.
- `failure_record(job_root, stages, env)` (section 9.1a) is written only
  when every input is available and independently closed:
  - the core identity, validated for this job root;
  - the launch receipt;
  - a structurally complete set of closed stage journals: aggregation
    `VALID` or `FAILED` (a closed `FAILED` stage is failure evidence;
    `INCOMPLETE` means unavailable);
  - exactly one `stop_verdict` record, of the exact shape above, with a
    verdict of `COMPLETE`, `FAILED` or `UNPROVEN` and a fixed-code reason.
    Its launch digest must link the launch receipt; its terminal digest is
    null or must link the terminal receipt;
  - `SECRET_KEY` and `DATABASE_URL` (valid UTF-8 of at least 16 bytes) in
    the environment.

  If any input is missing, repeated or malformed, nothing is written; the
  record never says `"stop": null`. It holds the outcome, the stop verdict
  and reason, the aggregation outcome, and the inputs' hashes; it holds no
  payload, path or log content. It is scanned before writing, and it never
  builds the registry.

## Credential registry

`{"schema": "alrust-credential-registry/1", "secrets": [...]}`, 0600. It
holds every actual secret of the job: the `keys.key` values from the job
database, the role keys, `SECRET_KEY`, the database password and URL, and
every secret the entrypoint obtained. Each secret is 16 or more UTF-8 bytes,
so the executing workflow's database password must be random per job.

## Scanner

Each secret is searched raw, as ASCII-escaped JSON and as UTF-8 JSON, plus
fixed generic patterns (synthetic keys, a PostgreSQL URL with a password,
`Authorization:`, `Bearer`, `-----BEGIN`). Findings never contain bytes.
Base64 and URL encodings are not searched.

## Removal

`remove_owned(job_root, resource, root_identity)`:
- the root is opened `O_NOFOLLOW` and must match the core's dev and ino,
  the caller's uid and mode 0700;
- each ancestor is opened relative to the previous one with `O_NOFOLLOW`;
- unlink, or `rmtree` with `dir_fd`, then a verification of absence,
  relative to the held parent;
- a mismatch is left untouched and reported `UNPROVEN`.

Stated limitation: a same-uid process can substitute the final entry inside
the verified parent.

## Not yet

Redaction (candidates are copied byte for byte and the scan decides);
database receipt linkage; workflow wiring.
