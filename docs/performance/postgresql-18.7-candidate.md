# PostgreSQL 18.7: pins and checklist

Status: **adopted by the owner's decision on 2026-11-15** ("Yes, move to 18.7
(Recommended)", in Claude's session), which replaces Y32's 18.6 pin; the
DECISIONS record is Y67. This branch first existed so that hosted CI ran the
catalog, adoption, migration and history tests on 18.7 while 18.6 was the
pin. Background and the
experiment plan: [PostgreSQL evaluation](postgresql-evaluation-2026-11-14.md)
(Codex). 18.7 was released on 2026-11-12; it is a fixes release. 19 RC2 is
not a candidate.

## Pins changed on this branch

Resolved independently on 2026-11-14 from the official sources.

| Where | 18.6 (main) | 18.7 (this branch) | How it was resolved |
|---|---|---|---|
| `.github/scripts/start-postgres18.sh` (hosted CI, Ubuntu 24.04) | `18.6-1.pgdg24.04+2` | `18.7-1.pgdg24.04+2` | `apt.postgresql.org` `noble-pgdg` `Packages.gz`: `postgresql-18` SHA256 `902e12bad8a7527199712b9f502b5ce14054289a8e138d1df9e85e86cecdaf1e`, `postgresql-client-18` SHA256 `2c102e1d6ab4ae0abf5bb1e55240feb2a2b7d81a2d0f78b29614611d88ed746e`. The index was read over HTTPS; its `InRelease` signature was not checked by me. CI's `apt-get` checks it with the pinned PGDG key |
| `docker-compose.yml` | `postgres:18.6@sha256:74935e72…4336` (2026-10-07) | `postgres:18.7@sha256:9f4fd4aa918f52ae3d6f3b687f1de90d8f867cd4d520021545a9a50c120eae3b` | Docker Hub index digest; the saved index bytes hash to it. linux/amd64 manifest `sha256:c7fedce47eff6a78cb35dad6b627f0d684107f75ab15c0870e40c093766ba452`, config `sha256:3cccc725163a95947178f4da5e9bf16add75cb286170ac5a2cd2f6513534955d` with `PG_VERSION=18.7-1.pgdg13+2`, created 2026-11-12T20:53:30Z. ECR Public serves the same index digest |
| `.github/workflows/ci.yml` | step and job names say 18.6 | say 18.7 | Names only; no logic change |

Not changed here, and why:

- `.github/workflows/executing.yml` (`public.ecr.aws/…/postgres:18.6@sha256:74935e72…`):
  the conformance custody workflow is Codex's; the same 18.7 digest is
  available on ECR Public if Codex wants it.
- `docs/alpha.md`, `CONTRIBUTING.md`, `README`, `docs/adr/0005`,
  `crates/alrust-store-pg/src/catalog.rs` comments: they describe the adopted
  version and change only with an adoption.
- Historical records (STATUS, DECISIONS, measured results, the applied
  migration `0002`): never rewritten.

Note: the `postgres:18.6` tag now resolves to `sha256:e3d24f69…` (a rebuilt
image), not the pinned `74935e72…`. Pinning by digest is why main is
unaffected.

## Compatibility checklist (what CI on this branch shows)

Each line is checked from the hosted run on this branch's exact head, not
assumed. Any failure is explained, never hidden by regenerating a file.

| Check | Where | Why it matters for 18.7 |
|---|---|---|
| Fresh database gets the reference layout; catalog equals the committed `reference-catalog.json` | `reference_layout.rs` `fresh_database_gets_the_reference_layout…`, catalog generation job | 18.7 adds composite-type versioning and tighter `ALTER COLUMN TYPE` rules; the catalog must not change |
| Alpha upgrade (0002) converts and verifies every field; refusals change nothing | `alpha_upgrade_*` tests | Uses the composite `history` type during conversion |
| Adoption of a reference database writes nothing; a different catalog is refused | `adoption_*` tests | |
| The composite-history `ALTER` measurement test | `reference_layout.rs` (measured on 18.6, run 38458334716) | 18.7's composite checks could change its outcome; a change is a finding, not a test edit |
| Acknowledgement hooks, receive, actions | `ack_hooks.rs`, unit tests, alpha smoke | Planner fixes must not change results |
| Compose smoke | skipped on draft PRs | The new image is **not** exercised until a non-draft run |

**Result at head `677d2880`, hosted run `43553977417` (2026-11-14):** the
installed packages were `postgresql-18` and `postgresql-client-18`
`18.7-1.pgdg24.04+2` in both the test job and the alpha smoke job. Passed
on 18.7: `reference_schema_matches_the_committed_catalog`,
`fresh_database_gets_the_reference_layout_and_repeat_start_is_a_no_op`,
`alpha_upgrade_converts_every_field_as_declared`,
`alpha_upgrade_refusals_change_nothing`,
`copy_verification_detects_any_changed_field`, the five `adoption_*` tests,
`owned_sequences_are_refused_and_unowned_ones_survive`,
`unrecognised_states_are_refused_without_writing`,
`non_string_values_become_jsonb_text_the_same_way_everywhere`,
`times_round_trip_exactly_whatever_the_session_time_zone`,
`alter_type_history_does_not_wait_for_the_alerts_lock` (the characterisation
measured on 18.6 still holds), the hook tests, and the alpha smoke. No test
failed; two catalog generators stay ignored, as on main. The catalog
generation job ran and uploaded its output; the committed catalog file was
not changed. The Compose smoke was skipped (draft), so the 18.7 image is
still not exercised. This is compatibility evidence only, not a speed
result.

Release-note items checked against alrust (from the
[18.7 notes](https://www.postgresql.org/docs/release/18.7/)):

- **No dump or restore** within 18.x.
- **REINDEX:** the corruption fixes concern GIN indexes, TOAST-table indexes
  after `REINDEX CONCURRENTLY`, B-tree on unconstrained `bpchar`, parallel GIN
  builds and `pg_trgm` GiST. The reference layout has no GIN, `bpchar` or
  `pg_trgm` index; any future search index of those kinds is built on 18.7.
- **Incremental backups** taken before the update should be replaced by a new
  full backup. alrust takes no backups itself; this is an operator note.
- **C extensions** need rebuilding for full composite protection. alrust
  installs no extension.
- **`composite_type_mismatch`** must stay at its default; disabling it is a
  security risk and is never a way to make a test pass.

## Rollback

18.7 and 18.6 share the on-disk format, so a server can go back to the 18.6
binaries with the same data directory. Before going back, reindex any index
built on 18.7 only if it is one of the kinds above (none today). For CI and
Compose, rollback is reverting this branch's three files.

## Experiments after CI

Same alrust head, same data, alternating order, 18.6 against 18.7, with
plans, buffers, waits, WAL and CPU recorded; then I/O and index settings one
at a time (Codex's plan, steps 3 and 4). No speed claim for 18.7 without that
run. These need a resource window; the 13 November thermal exception has
expired, and local builds on this host reach 96 to 99 °C.

## What adoption needs

The owner's decision to replace Y32's 18.6 pin, recorded in DECISIONS; then
the documentation lines listed above, Codex's decision on `executing.yml`,
and a non-draft run with the Compose smoke.
