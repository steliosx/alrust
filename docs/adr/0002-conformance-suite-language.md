# ADR-0002: Conformance suite language

- Status: **Decided by Codex** (owner of `conformance/`, review `ed33e33c`):
  Python 3.12 + pytest + httpx + Hypothesis.
- Date: 2026-09-29

## Context

`conformance/` must be a black-box HTTP suite that runs against either the
Python reference server or alrust, plus a differential fuzzer that replays
identical randomized request sequences against both and diffs normalized JSON.
It must not import code from either implementation.

## Proposal

Python 3.12 + pytest + httpx + hypothesis (stateful testing for the
differential fuzzer):

- The reference server already needs a Python toolchain in CI.
- Hypothesis `RuleBasedStateMachine` fits "randomized request sequences" and
  shrinks failing sequences to minimal reproductions.
- JSON normalization (timestamps, ids, hrefs, ordering) is simple to write in
  Python.

The target is selected only by `ALRUST_TARGET=<base url>`, so the suite stays
black-box.

Alternative: a Rust test crate using reqwest + proptest. This keeps a single
toolchain, but it tempts sharing types with the implementation under test,
which weakens the black-box property.

## Decision

Codex, the owner of `conformance/`, selected Python 3.12 + pytest + httpx +
Hypothesis (review `ed33e33c`). The Proposal above is kept as the original
option analysis.

The harness keeps implementation imports out and preserves semantic fields
during normalization. Implementation reviews and validation evidence are tracked
in `docs/STATUS.md`; this language decision does not close the Phase 0 gate.
