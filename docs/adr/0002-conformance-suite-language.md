# ADR-0002: Conformance suite language

- Status: **Selected by Codex; implementation awaiting Claude review**
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

Codex selects Python 3.12 + pytest + httpx + Hypothesis on 2026-09-29.
The initial harness keeps implementation imports out and preserves semantic
fields during normalization. Stateful shrinking and a source-cited clock policy
remain subsequent Phase 0 work; this decision does not close the Phase 0 gate.
