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
