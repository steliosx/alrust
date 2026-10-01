# alrust — Kickoff Prompt (Primary Agent: Opus 5.5)

You (Opus 5.5) are the primary agent on this project. Codex Sol6.1 is the
secondary agent; use your existing collaboration protocol with it throughout.
Recommended split, adjust as your protocol allows: you own architecture, specs,
phase gates, and merges; Codex owns adversarial review of every PR, the
conformance/differential suite, and second-opinion passes on the alert
lifecycle semantics. Neither agent merges its own unreviewed work.

## Project

**alrust** — a Rust rewrite of alerta-server for internal MSP NOC use.

## Context and fixed decisions (do not relitigate)

- We are an MSP. alrust receives alerts from multiple monitoring systems
  (Zabbix per-tenant instances, Veeam ONE, more later) and is used **only by
  our internal NOC** — no customer ever accesses it. `customer` is a
  grouping/routing attribute, not a security boundary.
- Rust backend: **axum + tokio + sqlx**. Database: **PostgreSQL only**. No
  MongoDB, ever.
- Frontend: the existing **alerta-webui (Vue)** is vendored and kept, lightly
  patched — no frontend rewrite. Desktop delivery via **Tauri** (Windows +
  macOS) wrapping that same webui, with native notifications driven by a new
  SSE push endpoint.
- API compatibility target: the Alerta API **as actually consumed by
  alerta-webui and our scripts**, not full documented parity. Endpoints and
  query operators nothing consumes may be dropped; record each drop in an ADR.
- Auth: API keys for inbound sources (one key per tenant monitoring instance,
  which is how alerts get their `customer` attribute) + OIDC for NOC
  operators. No customer-views subsystem, no per-customer RBAC.
- Integrations: native Zabbix webhook receiver; a **generic configurable
  inbound webhook mapper** (declarative field-extraction config → Alert
  fields), which is how Veeam ONE and future sources connect; generic outbound
  webhook forwarder for notifications. No Python plugin compatibility.
- Licensing: Alerta is Apache-2.0; retain LICENSE/NOTICE in vendored code.

## Version pins

- **Alerta (reference server + webui):** latest stable release at project
  start. Resolve the exact tags on day one, record them in
  `docs/DECISIONS.md`, and pin the `reference/` submodules to them.
- **Zabbix:** target **7.0.25 and up** (7.0 LTS line). Build the webhook
  receiver and severity/OK-event mapping against 7.0 LTS documentation and
  payloads; do not support older majors.
- **Veeam ONE:** latest stable version's documentation for its webhook/alarm
  notification payloads. Record the exact version consulted in
  `docs/DECISIONS.md`.

## Repo setup (your first actions)

Monorepo layout:

```
alrust/
  crates/          # workspace: alrust-domain, alrust-engine, alrust-store-pg,
                   #            alrust-api, alrust-receivers
  webui/           # vendored alerta-webui (Vue), lightly patched
  desktop/         # Tauri shell (Windows + macOS)
  conformance/     # black-box HTTP suite + differential runner
  spec/            # extracted API + lifecycle + customer-mapping specs
  reference/       # pinned submodules: alerta, alerta-webui (READ-ONLY)
  docs/            # ADRs (docs/adr/), DECISIONS.md, STATUS.md, phase gates
```

`reference/` is read-only: never edit it; CI must fail any diff touching it.
Set up CI from day one: fmt, clippy (deny warnings), tests, and the
conformance suite against a Postgres service container.

## Phase plan and gates

Work proceeds in phases; a phase is done only when its gate passes, and you do
not start dependent work on an ungated phase. Record gate results in `docs/`.

### Phase 0 — Spec extraction + conformance harness
From `reference/alerta`, extract into `spec/`:
1. An OpenAPI description of the API **as implemented** (code, not docs).
2. A precise prose + state-machine spec of alert lifecycle semantics — dedup
   keys, `correlate`, severity transitions, status transitions
   (open/ack/shelve/close/expired), `timeout`, heartbeats — every edge case
   cited to the Python source line.
3. How API-key metadata maps inbound alerts to `customer`.

Build `conformance/`: a language-agnostic black-box HTTP suite runnable
against either the Python server or alrust, plus a differential fuzzer that
replays identical randomized request sequences against both and diffs
normalized JSON.

**Gate:** suite runs green against the reference Python server in CI. Codex
reviews the lifecycle spec adversarially against the source before the gate
counts.

### Phase 1 — Domain + engine + Postgres store
`alrust-engine` is a pure library (no I/O) implementing the lifecycle spec,
property-tested; Zabbix 7.0 severity mapping and OK-event flows are
first-class fixtures. `alrust-store-pg` with sqlx migrations.

**Gate:** property tests + engine test vectors derived from the Phase 0 spec
all pass; Codex sign-off on engine semantics.

### Phase 2 — HTTP API
Full consumed surface including the `q=` query grammar (budget this
explicitly — it is the largest single item), pagination, history, counts and
top-N, plus a **new SSE endpoint** streaming alert changes (for webui + Tauri
notifications).

**Gate:** conformance suite green against alrust; differential fuzzer shows no
unexplained divergence over a long run; vendored webui works against alrust
with no code changes beyond config.

### Phase 3 — Auth
API keys (create/revoke, per-key customer attribute), OIDC for operators.

**Gate:** conformance auth tests green; webui login works.

### Phase 4 — Receivers
Zabbix webhook receiver matching the existing alerta-zabbix payload mapping
against Zabbix ≥ 7.0.25; generic inbound mapper (config-driven, with a
Veeam ONE sample config built from the latest stable documentation and real
captured payloads [PIN: request samples from operator]); outbound webhook
forwarder with per-customer routing rules.

**Gate:** replay of captured real payloads produces correct alerts end-to-end.

### Phase 5 — Ops
Housekeeping jobs (timeout/expiry), per-tenant heartbeat alerting ("tenant X's
Zabbix went quiet" raises an alert), Prometheus `/metrics`, env-var config
compatible with our current `alertad.conf` settings where sensible,
Dockerfile, shadow-run tooling (traffic mirror + response differ).

**Gate:** 1-week shadow run against production traffic with explained-diff
rate ~0.

### Phase 6 — Tauri desktop
Windows + macOS apps wrapping `webui/`, tray icon with severity badge, native
notifications from the SSE stream, auto-start, auto-update feed. Code signing
is human-dependent [PIN: certs to be provided]; build unsigned artifacts until
then.

**Gate:** both apps build in CI and receive live notifications against a
staging alrust.

## Working rules

- The conformance suite is the merge gate for all backend work: nothing merges
  red, and no test is weakened to get green — divergence from the Python
  behavior is either fixed or documented as an intentional ADR with Codex's
  concurrence.
- Small PRs, one concern each. Every PR gets a Codex review before merge;
  disagreements between the two agents touching alert-lifecycle semantics or
  data loss are escalated to the operator rather than settled by either agent
  alone.
- Keep `docs/DECISIONS.md` (ADR index) and `docs/STATUS.md` (phase, gate
  state, open questions) updated every session so any session can resume cold.
- Anything marked [PIN] is an operator input: ask once, park the dependent
  work, continue elsewhere — never guess these.
- Optimize for correctness of the state machine over speed of delivery; the
  whole value of this rewrite dies on silent lifecycle divergence.

## Start now

1. Repo scaffold + CI.
2. Resolve and pin the latest stable Alerta server/webui tags; add the
   `reference/` submodules.
3. Begin Phase 0.

## Operator inputs pending ([PIN] items)

- Captured real Veeam ONE (and ideally Zabbix 7.0 LTS) webhook payloads from
  production.
- Current production `alertad.conf` (for Phase 5 config compatibility).
- Code-signing certificates for Windows and macOS (Phase 6).
