# PostgreSQL evaluation for Alrust

Checked against upstream documentation on 14 November 2026. This is an
evaluation plan, not a version-pin adoption or a measured speedup.

## Version choice

PostgreSQL18.7 was released on12 November2026;19 RC2 is still a release
candidate. Prefer an18.7 validation candidate. Keep19 as a later experimental
arm rather than making an unsupported major version the baseline.
[Release notes](https://www.postgresql.org/docs/release/18.7/),
[19 development documentation](https://www.postgresql.org/docs/19/release-19.html).

18.7 is a fixes release, not a new feature release. Its notes include composite
type/concurrent-DDL protections, planner corrections and index-corruption fixes.
This matters to Alrust's composite history type, adoption tests and proposed
search indexes. No dump/restore is required within18.x, but review the specific
REINDEX guidance and incremental-backup warning before any real upgrade. C
extensions need an inventory and rebuild assessment. Never disable the new
composite-type checks to make a test pass. No installed cluster is changed by
this proposal. [18.7 migration and changes](https://www.postgresql.org/docs/release/18.7/).

The current project decision Y32 pins18.6. The owner's request to investigate
new PostgreSQL authorizes this proposal; it is not silently recorded as a
decision to replace that pin. Prepare the exact18.7 diff and results for review.
Historical18.6 results retain their original version and evidence.

## Features worth measuring

These are18.x features already available to the18.6 baseline, not benefits
introduced by18.7. Their relevance below is an Alrust-specific hypothesis.

| Feature | Candidate use | Guardrail |
|---|---|---|
| Asynchronous I/O | Larger scans, bitmap access and maintenance | Compare recorded I/O settings and warm/cold workload shapes; a warmed1-client test may show little benefit |
| Multicolumn B-tree skip scan | Existing filter combinations | Inspect actual plans/selectivity before adding or removing indexes |
| OLD/NEW in RETURNING | Explore avoiding a genuinely redundant read after an update | Preserve locked-row, trigger and hook visibility semantics; prove equivalence first |
| UUIDv7 | Possibly future internal append-heavy tables | Do not change existing alert IDs, reference layout or external contracts |

[PostgreSQL18 features](https://www.postgresql.org/docs/18/release-18.html).

`io_method` defaults to `worker`; `io_uring` requires a suitably built server
and working guest support. Record SHOW values first. Change one variable per
experiment and retain `worker` as a control; no host-kernel reconfiguration.
[I/O settings](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-IO-METHOD).

19's development notes describe REPACK, plan-advice extensions and further I/O
and autovacuum changes. These may be relevant to future churn/bloat studies,
but do not justify a release-candidate production dependency or new extensions
now. [Development release notes](https://www.postgresql.org/docs/19/release-19.html).

## Required experiment

1. Bind one Alrust head, reference head/environment, integration versions,
   dataset, image/package digests and resource envelope. Inventory all18.6
   pins without editing historical records. Resolve18.7 package/image contents
   independently; do not guess digest substitutions.
2. Prepare a separate18.7 candidate and disposable DB. Run fresh catalog,
   reference adoption, migration/refusal and composite-history tests. Catalog
   differences must be explained, not accepted by regenerating a golden file
   blindly. Preserve applied0002 and strict adoption checks.
3. Compare18.6 against18.7 with identical application code and data, alternating
   order and recording query plans, buffers, waits, WAL, CPU and allocations.
   Keep durability, auth and hook semantics identical. Then test I/O/index
   alternatives separately; never combine a database upgrade and application
   optimization into a single claimed cause.
4. Require matched correctness before ratios. Test integration capture latency
   separately from worker/provider completion. Keep synthetic and real-plugin
   results distinct. Report unsupported cases and missing gates as NOT_RUN.
5. Submit the exact diff, actual results and rollout/rollback note. No production
   upgrade, reference-oracle promotion, new paid CI, public release or automatic
   rerun is admitted by this document.
