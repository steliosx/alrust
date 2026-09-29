# Collaboration: claude-alrust ↔ codex-alrust

Status: proposed (ADR-0003). The binding rules live outside the repo at
`/home/user/claude-alrust-mailbox/RULES.md`.

## Day-to-day flow

1. The author works on a branch named `<agent>/<topic>`, for example
   `claude/scaffold` or `codex/conformance-harness`, and keeps each PR small
   and to one concern.
2. The author opens the PR as draft, marks it ready, and sends a mailbox
   `note` with the PR number and anything the reviewer should focus on.
3. The reviewer reviews on GitHub. The verdict lives in the PR review
   (approve / request changes), not in the mailbox.
4. claude-alrust merges once the PR is approved and CI is green. A Codex PR
   is merged by claude-alrust after claude-alrust's own review, so both agents
   see every change.
5. Any disagreement on alert-lifecycle semantics or data loss goes to the
   owner, with both positions stated. Neither agent settles it alone.

## Mailbox

| | claude-alrust | codex-alrust |
|---|---|---|
| mailbox | `/home/user/claude-alrust-mailbox` | _pending_ |
| inbox (peer drops here) | `/home/user/claude-alrust-mailbox/inbox` | _pending_ |
| key ID | `BEE3EBB0B44C1B46` | _pending_ |

    # send
    python3 tooling/mailbox.py --root <own mailbox> send --kind note --subject S --body-file F --drop-dir <peer inbox>
    # receive + ack
    python3 tooling/mailbox.py --root <own mailbox> fetch --no-taildrop --from-dir <own inbox> --ack --drop-dir <peer inbox>

## Session resume

Every session starts by reading `docs/STATUS.md`, then fetching the mailbox.
