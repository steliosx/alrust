# ADR-0003: Agent collaboration channel

- Status: **Proposed. Pending owner approval and Codex concurrence**
- Date: 2026-09-29

## Decision (proposed)

- **Review medium:** GitHub PRs on `steliosx/alrust`. Every PR gets a Codex
  review before merge. claude-alrust merges once Codex approves and CI is
  green. Neither agent merges its own unreviewed work.
- **Coordination channel:** the signed file mailbox `agent-mailbox/1` used on
  the Zabalack project (Minisign-signed ZIP envelopes, inert reader,
  receipts). It has **new identities** (`claude-alrust`, `codex-alrust`) with
  fresh keys. The Zabalack keys and scope are not reused. Local drop-directory
  transport, because both agents run on the same machine.
- **Rules:** kept outside this repo at
  `/home/user/claude-alrust-mailbox/RULES.md` so that repo changes cannot
  amend them. They map KICKOFF's working rules onto the two roles: Codex's
  verdict is independent, and lifecycle or data-loss disputes are escalated to
  the owner.
- **Bootstrap:** each side's key ID, public key and public-key file SHA-256
  are relayed through the owner, not the mailbox.

See `docs/COLLABORATION.md` for the day-to-day flow.
