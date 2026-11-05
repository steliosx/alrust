# spec

Specs extracted from `reference/alerta` in Phase 0. Every behavioral claim
cites a source line (`reference/alerta/<path>:<line>` @ v9.1.0).

Planned:
- `openapi.yaml`: the API as implemented, not as documented.
- `lifecycle.md`: dedup, correlate, severity/status transitions, timeout,
  heartbeats, as prose plus a state machine.
- `customer-mapping.md`: how API-key metadata maps inbound alerts to `customer`.
- `security-fixes.md`: where alrust deliberately fixes a reference defect
  instead of reproducing it (owner decision Y3), with the conformance impact
  of each fix.
- `integration-hooks.md`: the hook contract through which alrust runs the
  Zabalack and Voalack acknowledgement capture (Y35, Y41); a draft until
  reviewed.
- `oidc-caller-facts.md`: how a verified OIDC operator's Bearer token becomes
  the caller facts the integration hooks receive (Y44 A1, A2; Y50, Y51); a
  reviewed design, implemented and active except the login stage (PRs #192,
  #194, #196).
- `oidc-login.md`: a proposed design for the OIDC login stage that issues
  operator Bearer tokens; two owner choices decided in part (Y52, Y53), the
  rest open; not implemented.
