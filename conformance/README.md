# conformance

Black-box HTTP conformance suite plus a differential fuzzer. The suite runs
against either the reference Python Alerta server or alrust. The target is
chosen only by `ALRUST_TARGET=<base url>`, and the suite never imports code
from either implementation.

- `scripts/run-reference.sh` boots the pinned reference server
  (`reference/alerta`) against PostgreSQL and waits for it to become healthy.
- The suite language is not decided yet (ADR-0002, owned by Codex).

Phase 0 gate: the suite is green against the reference server in CI.
