# ADR-0003: Agent collaboration channel

- Status: **Accepted.** The charter was owner-authorized on 2026-09-29. Both
  agents recorded the agreed rules SHA-256
  `abad4f687dace3fb12782a0bb23e7befa337c191e0e91c5af951b11dc84d55a3`, and the
  signed round trip `ALRUST-BOOTSTRAP-ROUNDTRIP` completed at 2026-09-29T21:13Z.
  The peer keys, `claude-alrust` `BEE3EBB0B44C1B46` and `codex-alrust`
  `21A3D921B09AC096`, were each pinned from the owner's relay.
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
