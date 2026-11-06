# Provisioner and entrypoint secret handoff (Q1)

Interface and private snapshot contract for the source-only Q1 integration.
Codex owns the codec, discovery writer and capture components; Claude owns
the registry reader, cleanup and entrypoint supervisor implementation.
These libraries have source and offline controls. No runtime provisioner,
profile, executor or workflow is enabled by this integration.

This implements the next source step identified in database-stage design
`87653d31ad094da9ca721e46040353c560e72c1d`, sections 5 and 9 (Q1), and uses
the frozen-core/closed-stage bindings in observer plan
`71dc4763c83354d9c536bc00dd07f6d511fe9ae5`. The existing launch contract
sections 8–9 and finalizer record shapes remain unchanged. Reference source
and customer filtering remain unchanged; this proposes no auth divergence.

## What must be retained

The final `SELECT key FROM keys` cannot recover a key revoked during a test.
The entrypoint must retain every distinct secret it generates or obtains:
all provisioned role keys, test-created keys, tokens and synthetic user
passwords, including secrets associated with failed or deleted operations.
Never use a current database query, an HTTP success count or the last value
of a credential variable as a substitute for this history.

The registry independently adds the database URL/password, signing key and
all remaining stored API keys. Inventory completeness and the database
query are separate requirements; overlap is allowed and the resulting
registry is the set union. The complete union must fit the existing
registry limits; overflow fails, never truncates.

## Private files and writer obligations

The executing entrypoint is the only writer. It owns two fixed files under
the already validated 0700 job root:

| File | Purpose |
|---|---|
| `entrypoint-secret-events.jsonl` | Private append-only discovery evidence, fsynced after each distinct secret is generated/obtained. It is never an export candidate. |
| `entrypoint-secrets.json` | Exclusive 0600 frozen snapshot, written and fsynced (including parent directory) only after discoveries and test writers have stopped. |

Both are created exclusively without following links, recorded immediately
as `owned_resource` with actual path/device/inode/type, retained on failure
and removed only by reviewed identity-checked cleanup. The later writer
must operate through the held validated root, not a caller-selected path.
Partial allocation or failed journal registration prevents COMPLETE; do not
overwrite, adopt or delete an occupied path to recover from such a failure.
Read-only modes protect against mistakes, not another process with the same
UID. This interface is not a hostile-writer security boundary.

Register a generated secret durably before supplying it to a command or
HTTP request. Register a returned secret before a later request, candidate
summary or test assertion can expose it. A capture failure fails the stage
and prevents export, even if the API call itself succeeded. Raw HTTP and CLI
output always remains private. An interrupted or failed stage does not
publish a COMPLETE snapshot; its partial evidence remains diagnostic only.

No child writer may survive entrypoint closure. Any future pytest bridge
must either run as the same sole writer or have a separately reviewed
explicit serialization/lifetime protocol. Neither an environment variable
nor adding a pytest fixture silently establishes that protocol. Every
enabled test module's secret-generation and response paths must be audited;
unintegrated paths block that module's execution qualification.

## Frozen snapshot and closed-stage record

The exact JSON object has three fields:

```text
schema: "alrust-entrypoint-secrets/1"
core_identity_sha256: SHA-256 of the independently validated frozen core bytes
secrets: ordered list of distinct private UTF-8 strings
```

No example credential bytes are published. Limits match the existing secret
values: at most 256 distinct entries, 16–4096 UTF-8 bytes per entry; the
complete snapshot is at most 1 MiB. This additional byte bound is an
interface refusal limit, not a guarantee that the eventual registry fits.
Empty is permitted only as an explicit reviewed no-discovery profile, never
as the default for a missing, malformed or incomplete inventory. A
four-role profile requires all four actual provisioned keys; metadata
declarations alone do not prove successful stored-key provisioning.

The proposed new entrypoint journal kind `private_secret_inventory` has
exact payload `{path, dev, ino, sha256, count}`. The path is fixed to the
snapshot above; identities are actual exact integers, count is the number
of distinct discoveries and sha256 hashes the final closed bytes. Secrets
and individual secret digests never enter stage journals. The entrypoint
records exactly one such payload before closing COMPLETE. The snapshot's
core binding and that journal's core binding must both match the expected
validated frozen core. A digest merely binds bytes; it does not prove
that all discovery paths have reported their secrets.

`private_secret_inventory.py` implements only strict encoding/decoding of
these private snapshot bytes. Duplicate/extra fields, non-finite JSON,
invalid UTF-8, malformed core hashes, count/type mismatches, duplicate
values and all limit overflows refuse with fixed diagnostic codes. It does
not open files, import either server, query SQL or prove quiescence, source
identity, file identity, completeness, stored-key metadata or cleanup.

## Registry reader order and existing finalizer linkage

The registry reader contract requires:

1. Pass the existing writer-quiescence gate before opening either private
   inventory file or issuing a key query. No COMPLETE stop receipt, closed
   entrypoint or positive generation-exit observation means no read/query.
2. Read the independently validated closed COMPLETE entrypoint journal,
   require exactly one `private_secret_inventory`, validate its exact
   schema/types/fixed path, and open the snapshot through the held root.
   Require a UID-owned regular 0600 single-link file of the recorded device
   and inode, bound size, and exact final digest. Refuse symlinked ancestors,
   FIFO/device/socket, changed identity, unreadable or changed bytes.
3. Decode against the independent core digest and recorded count. Check
   profile-specific discovery prerequisites from the reviewed writer, then
   supply the private list as `other_secrets` to `build_registry`. Never
   derive the expected count or core from the snapshot being checked.
4. Require actual value-set inclusion in the frozen registry, then emit
   the existing `obtained_secret_count` shape `{count, registry_sha256}` in
   the **registry** journal, after the registry bytes/digest exist. Count
   means distinct entrypoint discoveries, not HTTP successes or total
   registry entries. Emit it exactly once; no record backfill into the
   already closed entrypoint journal.
5. Close the registry journal only after inclusion/digest linkage succeeds.
   The drop gate then independently re-evaluates quiescence and requires
   this valid frozen registry before any SQL destruction.

Step 4 avoids a cycle: the entrypoint cannot know a later registry digest.
The interface requires registry authorship of `obtained_secret_count`; a
count in any other stage must refuse. Claude's branch `claude/ci-db-q1-reader`
adds that finalizer stage check. Its corrected component head `a6b00f50`
was independently reviewed, then integrated at exact head
`30f79e273050662a7d038ec187c7385892733635` and merged into main as
`7970d25e9ed24ec7203201516b76cd996102cd1b`. This integration retains those
reader and finalizer bytes. Source/offline approval and the main scaffold
CI pass do not establish a composed executing profile. The finalizer's count
check is only a lower bound. Therefore the **reader's actual set-inclusion check**
is required; a same-sized registry of different secrets is not complete.
No finalizer schema change or weakened gate is proposed. Any required
reader/linkage adjustment is Claude-authored and independently reviewed.

Failed/incomplete entrypoint journals retain the existing failure-only
path: no mutable inventory read, no invented complete registry, no normal
export or upgraded cleanup verdict. The snapshot interface does not change
failure-only scanning or authorize a SQL/drop path.

## Focused verification and execution prerequisites

The codec has offline controls for zero/ordinary/revoked discoveries,
exact core/count bindings, UTF-8 byte bounds, byte/entry overflow,
duplicate/extra/non-finite JSON and fixed non-secret errors. The writer has
synthetic controls for durability, interruptions, occupied/replaced paths,
links and partial allocations. The reader has offline controls for gated
snapshot reads and inclusion, including a same-count different-secret
registry and failure before any query/drop.

The capture chain comes from exact reviewed component head
`7a25fef73bbd53b8565bc617683ef206b2b0b800`. The composed reader/capture/writer
source projection passed 743 targeted offline tests with no skips. The
integration retains all tested Python and test bytes; the interface wording
is updated separately. This is LOCAL_PASS component evidence only: each new
integration head still requires independent review, and no actual
writer-to-reader executing profile has been qualified.

The following remain NOT_RUN and require their own reviewed source and
execution evidence:

- provisioning through the pinned reference CLI, verifying all four actual
  role keys, their customer/scopes and completeness, and capturing
  secret-bearing stdout privately;
- each enabled test module's discovery bridge, successful test outcome,
  writer closure and positive supervised generation-exit evidence;
- actual service-log exclusion, composed observer/entrypoint execution,
  guarded database teardown and finalization at the new integration head.

The initial executing qualification remains one bounded reviewed profile,
not an automatic enablement of all 19. Existing hosted scaffold success
does not satisfy any of these prerequisites or Phase 0. Heavy jobs need
fresh signed admission/exclusion; shared VM and owner gates remain intact.
