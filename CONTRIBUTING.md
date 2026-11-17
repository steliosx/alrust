# Contributing

Alrust is an early Rust API alpha. Changes should preserve the consumed Alerta API and alert lifecycle, and make incomplete behavior explicit. The current evaluation target is Linux x86_64 with PostgreSQL 18.7; ARM64, the web UI and desktop delivery are not qualified. See [README](README.md), [the alpha guide](docs/alpha.md) and [STATUS](docs/STATUS.md) for scope and known gaps.

## Propose a change

Open a focused pull request with the problem, resulting behavior and validation performed. For a bug, include the source revision, platform, expected and actual results, and a minimal synthetic reproduction. For a behavior change, cite the relevant specification or upstream behavior and describe any compatibility cost. Maintainers review deliberate lifecycle, authentication and data-retention differences before accepting them.

Keep unrelated changes separate. Include a regression test when it demonstrates the bug or meaningful behavior; do not weaken existing checks to obtain a pass. Changes to generated files should explain the generator and the exact source used. Do not edit the pinned `reference/` trees or change their pins as part of an unrelated change.

Use synthetic alerts and test credentials. Remove tokens, connection strings, customer data and private logs from issues, pull requests and artifacts. Report a suspected vulnerability privately, as [SECURITY.md](SECURITY.md) describes, through GitHub's private vulnerability reporting; do not publish its sensitive details in a normal issue. Ordinary public issues are not a private channel.

## Set up a checkout

Use the Rust toolchain pinned in `rust-toolchain.toml`. Initialize the read-only upstream sources required by some store tests:

```sh
git submodule update --init
```

Development and CI use Rust 1.100.0. The declared Rust 1.98 minimum is
unchanged; a toolchain upgrade alone does not requalify that minimum.
If your shell overrides `RUSTUP_TOOLCHAIN`, use `cargo +1.100.0` for these
checks so the override does not silently select another compiler.
Cargo's unused-dependency lints reject unused normal/build dependencies.
They do not cover dev-dependencies, and feature-only dependencies still need
manual review before removal.

New dependency resolution uses a seven-day publication cooldown from
`.cargo/config.toml`. Keep `Cargo.lock` committed and use `--locked` for checks;
the cooldown does not recheck already locked versions, protect registries
without publication timestamps, or govern `cargo install`. For an urgent
reviewed security update, use a command-scoped
`CARGO_RESOLVER_INCOMPATIBLE_PUBLISH_AGE=allow cargo update -p <crate> --precise <version>`
exception and document the reason and resulting lockfile diff in the PR.
Use the pinned Cargo version for dependency updates; older Cargo versions
do not enforce this cooldown. Dependency or toolchain changes also require
the notice-corpus review described in `third_party/rust/README.md`.

From the repository root, the Rust checks are:

```sh
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked --workspace
```

Database-dependent tests require a disposable PostgreSQL 18.7 instance and a suitable `DATABASE_URL`. Store tests create and drop their own test databases. Do not point them at a production or shared database. Without `DATABASE_URL`, some database-dependent tests return without executing outside CI; a successful command alone is not evidence that those cases ran. State which checks executed, skipped or could not run.

The Python harness currently locks wheels for CPython 3.12 on Linux x86_64 with glibc 2.17 or newer. To run its unit controls in a separate environment:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r conformance/requirements.lock
PYTHONPATH=conformance:conformance/tests/unit \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -c conformance/pytest.ini \
  -p no:cacheprovider -q conformance/tests/unit
```

These unit controls use synthetic subprocesses and loopback fixtures. They are not a full HTTP compatibility run. HTTP and differential tests need separately prepared disposable targets and their documented prerequisites; do not run the whole HTTP directory against an arbitrary server. The alpha smoke script also writes test data, and its `--disposable-target` option runs global housekeeping that can delete other alerts. See [the alpha guide](docs/alpha.md) and [conformance documentation](conformance/README.md) before using those tools.

## Branch rules

Changes reach `main` only through pull requests. `main` is protected: the ten required checks must pass, the branch must be up to date with `main`, and review conversations must be resolved; force pushes and deletion are refused. Workflows may use only GitHub-owned actions and `dtolnay/rust-toolchain` and `Swatinem/rust-cache`, each pinned by its full commit SHA; adding another third-party action needs the owner's approval. Workflows from a fork's pull request run only after a maintainer approves them. The `forgehold-*` workflows are the Forgehold lane: they run only on manual dispatch, on the owner's self-hosted `forgehold` runner, and never on pull requests. Development and CI use Rust 1.100.0 (`rust-toolchain.toml`).

## Review and evidence

A pull request may be submitted with unavailable checks clearly identified. Include the exact commands, source revision and relevant outcome; retain a failed result when a later attempt succeeds. A green harness unit test, reference-only smoke or disabled-profile preflight does not establish full Alerta parity. All 19 executing profiles remain `NOT_RUN` in the current qualification record.

Maintainers independently review the final change and its applicable CI before merging. Private lab execution is a separate maintainer-controlled process; public contributions and successful hosted jobs do not authorize it. Do not include private lab configuration, credentials or raw runtime evidence in a contribution.

alrust is licensed under the [Apache License, Version 2.0](LICENSE); see [Licensing](README.md#licensing) and the applicable third-party notices. Under section 5 of that licence, a contribution you submit for inclusion is licensed under the same terms unless you state otherwise. These instructions do not change upstream licence terms.
