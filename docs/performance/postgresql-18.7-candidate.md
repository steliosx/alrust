# PostgreSQL 18.7: pins and checklist

Status: **adopted by the owner's decision on 2026-11-15** ("Yes, move to 18.7
(Recommended)", in Claude's session), which replaces Y32's 18.6 pin when this
PR merges. The DECISIONS row is Y67, which is in the record PR #227 and not
yet on `main` at this head. This branch first existed so that hosted CI ran
the catalog, adoption, migration and history tests on 18.7 while 18.6 was the
pin. Background and the experiment plan:
[PostgreSQL evaluation](postgresql-evaluation-2026-11-14.md) (Codex). 18.7
was released on 2026-11-12; it is a fixes release. 19 RC2 is not a
candidate.

## What this branch changes

The pins were resolved on 2026-11-14 from the official sources. The index
hashes and image provenance below are as I recorded them; the registry bytes
were not attached for review. CI verified the Compose digest by pulling it.

| Where | Before (18.6) | This branch (18.7) | How it was resolved |
|---|---|---|---|
| `.github/scripts/start-postgres18.sh` (hosted CI, Ubuntu 24.04) | `18.6-1.pgdg24.04+2` | `18.7-1.pgdg24.04+2` | `apt.postgresql.org` `noble-pgdg` `Packages.gz`: `postgresql-18` SHA256 `902e12bad8a7527199712b9f502b5ce14054289a8e138d1df9e85e86cecdaf1e`, `postgresql-client-18` SHA256 `2c102e1d6ab4ae0abf5bb1e55240feb2a2b7d81a2d0f78b29614611d88ed746e`. The index was read over HTTPS; its `InRelease` signature was not checked by me. CI's `apt-get` checks it with the pinned PGDG key |
| `docker-compose.yml` | `postgres:18.6@sha256:74935e72…4336` (2026-10-07) | `postgres:18.7@sha256:9f4fd4aa918f52ae3d6f3b687f1de90d8f867cd4d520021545a9a50c120eae3b` | Docker Hub index digest; the saved index bytes hash to it. linux/amd64 manifest `sha256:c7fedce47eff6a78cb35dad6b627f0d684107f75ab15c0870e40c093766ba452`, config `sha256:3cccc725163a95947178f4da5e9bf16add75cb286170ac5a2cd2f6513534955d` with `PG_VERSION=18.7-1.pgdg13+2`, created 2026-11-12T20:53:30Z. ECR Public serves the same index digest |
| `.github/scripts/compose-smoke.sh` | guards the Compose file's SHA256 `daab8986…` | `3dc8accf534388af4b3a29ba9bcf612fdc21bc7e8fe36c9f049dfc8d397db363` | The guard refuses any other Compose file, so it moves with the image pin |
| `.github/workflows/ci.yml` | job and step names say 18.6 | names say **PostgreSQL 18**, with no minor version | Names only; no logic change. The two required checks become "alpha server smoke (PostgreSQL 18)" and "store catalog generation (PostgreSQL 18, draft PRs only)" |
| `docs/alpha.md`, `CONTRIBUTING.md`, `README.md` | 18.6 | 18.7 | The current instructions name the adopted version |

The repository's list of required checks names the old job names. It is a
settings change, made by the settings owner (claude-chief-0a) at merge time
with a readback, under an owner approval given in that session (relayed by
claude-chief-0a; not witnessed by Claude or Codex). It is not part of this
diff.

Not changed here, and why:

- `.github/workflows/executing.yml` (`public.ecr.aws/…/postgres:18.6@sha256:74935e72…`):
  the dispatch-only conformance custody workflow is Codex's. It stays on
  18.6 on purpose and gains nothing from this change. The same 18.7 digest is
  on ECR Public if Codex wants it.
- `docs/adr/0005`, the comments in `crates/alrust-store-pg/src/catalog.rs`,
  and test fixtures that name 18.6: they record what was measured on 18.6.
- Historical records (STATUS, DECISIONS, measured results, the applied
  migration `0002`): never rewritten.

Note: the `postgres:18.6` tag now resolves to `sha256:e3d24f69…` (a rebuilt
image), not the pinned `74935e72…`. Pinning by digest is why `main` is
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
| Compose smoke | `compose-smoke` job | Pulls and runs the pinned 18.7 image. The job runs only when the repository is `steliosx/alrust` and its metadata says public (its `if:` in `ci.yml`); draft status does not matter. Only store catalog generation is limited to draft PRs |

**First result, head `677d2880`, hosted run `43553977417` (2026-11-14):** the
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
not changed. The Compose smoke was skipped by its own condition (repository
identity and public metadata), not because the PR was a draft, so that run
did not exercise the 18.7 image.

**Result at head `12704f69`, hosted run `43789827612` (2026-11-15):** all
ten jobs succeeded. The server reported `PostgreSQL 18.7 (Ubuntu
18.7-1.pgdg24.04+2)`, and the store suite reported 28 passed and 2 ignored.
The Compose smoke ran: its `pull-db` stage and every other stage exited 0,
the smoke reported 154 passing cases, and the job ended with "PASS (alpha
Compose smoke and scoped cleanup only; 19 profiles NOT_RUN; ARM64
deferred)". This is compatibility evidence only, not a speed result and not
a formal profile qualification.

## What the 18.7 release notes ask, and what that means here

Quoted from the
[18.7 release notes](https://www.postgresql.org/docs/release/18.7/),
section E.1.1, "Migration to Version 18.7":

> A dump/restore is not required for those running 18.X.
>
> However, several different bugs have been discovered that could result in
> index corruption. Examine the changelog entries below that mention
> `REINDEX`, and reindex any indexes you have that might be affected.
>
> Also, existing incremental backup files should not be trusted, thanks to
> the discovery of several ways that they could become corrupt. It's
> recommended that users of incremental backups take a new full backup after
> updating and use it as the basis for subsequent incremental backups.

The changelog entries that mention `REINDEX` concern GIN indexes, B-tree
indexes on `bpchar` columns, `gist_trgm_ops` indexes, and TOAST-table
indexes. On the last one, the notes say: "`REINDEX CONCURRENTLY` can change
these fields, but the relation cache was not expecting that, allowing index
updates to be missed. … Fortunately, these only represent index corruption;
the TOAST table itself is fine, so a `REINDEX` will suffice to recover."

What this means for alrust:

- alrust's declared schema (the Alerta 9.1.0 layout, embedded verbatim in
  `crates/alrust-store-pg/migrations/0002_reference_layout.sql`, with
  `0001_initial.sql`) has no GIN index, no index on a `bpchar` column and no
  trigram index. alrust never runs `REINDEX`.
- PostgreSQL gives a TOAST table, and so a TOAST index, to every table with
  a column of a toastable type. Every table in that schema has a `text`,
  `jsonb` or array column. Whether one of those is affected depends on
  the database's own history (for example, a `REINDEX CONCURRENTLY` run by
  an operator), which alrust cannot see.
- So the declared schema alone does **not** qualify an existing data
  directory. An operator who updates an existing database to 18.7 follows
  the release notes: examine the `REINDEX` entries against that database,
  and replace incremental backups with a new full backup. CI's databases
  are created fresh in each run, so these steps do not apply to them.
- **C extensions and `composite_type_mismatch`:** the notes say "For full
  protection, this change requires rebuilding C-language extensions … If
  necessary, the new `composite_type_mismatch` server parameter can be used
  to disable these checks, but doing so carries security risks." alrust
  installs no extension. The parameter stays at its default; disabling it is
  never a way to make a test pass.

## Rollback

Nothing below is tested. It is a list of what a revert must include, not a
qualified procedure.

- **A pin-only revert** (back to 18.6, keeping the version-free job names):
  revert `start-postgres18.sh`, `docker-compose.yml` and the guard digest in
  `compose-smoke.sh` **together**. A Compose file reverted without its guard
  digest is refused by the guard. Revert the version lines in `docs/alpha.md`,
  `CONTRIBUTING.md` and `README.md`. The job names say "PostgreSQL 18", so
  they stay correct, and the required-check settings do not change.
- **A full revert of this PR**, including the job names: everything above,
  plus `ci.yml`'s names. The settings owner must switch the two required
  checks back to the old names at the same moment, under its own authority,
  with a readback; otherwise protected merges wait on checks that no longer
  run.
- **A running server's data directory:** going from 18.7 back to the 18.6
  binaries on the same data directory has not been tested here, and this
  document does not offer it as a procedure. An operator relies on the
  PostgreSQL release notes and their own backups.

## Experiments after CI

Same alrust head, same data, alternating order, 18.6 against 18.7, with
plans, buffers, waits, WAL and CPU recorded; then I/O and index settings one
at a time (Codex's plan, steps 3 and 4). No speed claim for 18.7 without that
run. The alrust and Alerta benchmark (#258) used 18.7 for both servers; it
compares the servers, not the PostgreSQL versions.

## What adoption needs

Done:
- the owner's decision (Y67; its row is in #227);
- the current instructions (`docs/alpha.md`, `CONTRIBUTING.md`, `README.md`);
- a run with the Compose smoke on 18.7 (run `43789827612`).

Still needed before the merge:
- Codex's review of this exact head;
- the settings owner's switch of the two required checks to the new names,
  with a readback and the rollback above, at merge time (the owner approval
  for it is relayed by claude-chief-0a, not witnessed by Claude or Codex).

`executing.yml` stays on 18.6 unless Codex decides otherwise.
