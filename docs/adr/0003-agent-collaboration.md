# ADR-0003: Agent collaboration channel

- Status: **Charter owner-authorized 2026-09-29. Agreed rules hash pending
  both agents.**
- Date: 2026-09-29 (revised)

## Decision

- **Charter:** the owner's operating charter of 2026-09-29, reconciled with
  claude-alrust's first proposal. The snapshots live outside Git, in each
  agent's mailbox `bootstrap/` directory, together with the original, the
  diffs, and a hash record. Task delegation starts only after both agents have
  recorded the same agreed SHA-256.
- **Channel:** `agent-mailbox/1` (the Zabalack mechanics) with fresh alrust
  identities `claude-alrust` and `codex-alrust`, using local drop-directory
  transport. The Zabalack identities, keys, and workspaces are not used or
  modified.
- **Trust:** each peer key is trusted only after the owner has relayed its
  identity, key ID, full public key, and exact public-file SHA-256. A key a
  peer supplies about itself is not enough.
- **Review medium:** PRs on `steliosx/alrust`, backed by signed dispositions.
  `main` merges stay with the owner.

## Superseded

The first proposal, in which claude-alrust merged approved PRs, is superseded:
`main` merges are outside the agents' assignment.
