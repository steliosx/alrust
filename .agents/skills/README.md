# Agent skills

The three Rust skills here are BeautifulRust's `rust-best-practices`,
`rust-review` and `rust-port`, copied from
https://github.com/morganlinton/BeautifulRust at commit
`f92961282b83e11757f076e787b4725b38d07b56` (MIT, see `LICENSE-BeautifulRust`).
The owner chose to install them in the repository on 2026-10-28 (DECISIONS
Y37); both agents follow them for Rust work. The lint set they recommend is
already the workspace policy (`Cargo.toml` `[workspace.lints]`, `clippy.toml`;
DECISIONS Y36), and CI's `cargo clippy --workspace --all-targets -- -D warnings`
is the bar that counts where a skill's own command line differs.

Layout: Codex and most agents read `.agents/skills/`; Claude Code reads
`.claude/skills/`, which holds symbolic links to these folders. The upstream
`evals/` directories are not copied. Updating means copying the upstream
folders again and recording the new commit here, as a reviewed PR.

## Alrust scope adapter (read before applying a skill)

The skills are engineering guidance, subordinate to the owner's charter, the
owner's current direct decisions, the maintained communication protocol
(`docs/COLLABORATION.md`), the project specs and ADRs, and the assigned lane
ownership. They grant no execution, publication, access or timer authority.

- A reviewer stays read-only unless separately assigned authorized writes.
  Run `cargo fmt`, builds and tests only within the applicable authority and
  resource rules: a toolchain being available is not permission. Routine
  developer smoke runs keep the current limits (Minimal process, the 80 °C
  CPU guard, each run reported; DECISIONS Y41); held, heavy, reference and
  VM scopes keep their gates. Report an unexecuted check as NOT_RUN, never as
  passed.
- alrust's consumed API and lifecycle specification (`spec/`) and the
  required conformance and differential checks override a skill's generic
  porting shortcuts. Do not accept Unicode, whitespace, integer or other
  behavioural drift merely by adding porting notes or assuming inputs are
  rare; a deliberate divergence goes through an ADR with Codex's concurrence
  and, where it is the owner's call, an owner decision.
- Investigate and fix a flaky test without weakening or removing required
  conformance coverage, disabling required checks or manufacturing a green
  result.
- `reference/` stays read-only, private evidence stays in its custody, and
  the signed peer-review process stands. CI's exact lint command remains
  the bar, as stated above.
