# Zabbix inbound: what "the existing alerta-zabbix mapping" is

Status: **analysis and pin proposal, 14 November 2026.** No code. KICKOFF
Phase 4 asks for a "Zabbix webhook receiver matching the existing
alerta-zabbix payload mapping against Zabbix ≥ 7.0.25". That mapping is not
in `reference/` (Alerta 9.1.0 has no Zabbix code; its twelve webhook
providers do not include Zabbix), so it must be pinned before a receiver can
match it.

## The upstream integration

`alerta/zabbix-alerta` on GitHub (MIT), default branch `master`, head
`b6ed6bc76e36bb88600689314de4b5fcbbae55c8` (2021-06-14, "Merge pull request
#46"), version `4.0.0`; no newer commit since 2021. Read through the GitHub
API on 2026-11-14; `zabbix_alerta.py` sha256
`894a50580f9d6880d06850bc30f451a06ce2aee61f20a76db94f0ad161e37655`.

It is **not a webhook receiver in Alerta**. It is a script that Zabbix runs
as an alert script (`zabbix-alerta <sendto> <subject> <message>`) on the
Zabbix side. It parses the message, which the Zabbix action fills from
macros as `key=value` lines, and sends the result with the Python Alerta
client's `send_alert`, that is, an ordinary `POST /alert`.

`parse_zabbix(subject, message)` (lines 34-85):

| Input line | Result |
|---|---|
| any `key=value` line (split at the first `=`) | `alert[key] = value`; lines without `=` are skipped |
| `service=a,b` and `tags=a,b` | split on `,` into lists |
| `severity=X!!` | `X` used as is (the action sends Alerta names); without `!!`, Zabbix names map: Disaster → critical, High → major, Average → minor, Warning → warning, Information → informational, Not classified → indeterminate, anything else → indeterminate |
| `timeout=N` | integer |
| `attributes.k=v` | attribute `k` |
| `environment={$ENVIRONMENT}` (macro undefined) | `Production` |
| `status=OK` | severity `ok` (with `!!`) or `normal`; `status` is not sent |
| `ack=Yes` and status not `OK` | status `ack` |
| always | `origin = zabbix/<hostname of the Zabbix server>`; `rawData = subject + "\n\n" + message` |

The documented action templates (lines 103-157) send `resource`,
`event={ITEM.KEY1}`, `group=Zabbix`, `type=zabbixAlert`, `value`, `text`,
`tags={EVENT.TAGS}`, attributes `eventId`, `triggerId`, `ip`,
`thresholdInfo`, `moreInfo`, and `dateTime`.

## What this means for alrust

- **If production uses this script, it talks to alrust through
  `POST /alert`,** which alrust serves today, so no new receiver is needed
  for that path. That is not proof that the production path works: the
  body the script sends is built by the Python Alerta client on the Zabbix
  host, whose version is not pinned, from keyword arguments such as
  `dateTime` and `rawData` whose handling depends on that version. alrust's
  own `POST /alert` tests do not cover that client. Proof needs the pinned
  client (or captured production requests, the PIN input) replayed against
  alrust.
- **A native receiver** (Zabbix 7.0 webhook media type posting to alrust
  directly, without the Python client on the Zabbix host) would accept the
  same `key=value` message, or JSON built from the same macros, and apply
  `parse_zabbix`'s rules. It is small, and the generic mapper (draft #230)
  could carry most of it as a built-in mapping plus the `!!` and `OK`
  rules.
- **Zabalack** claims alerts with `attributes.source_system = 'zabbix'`
  (`spec/integration-hooks.md` section 3). The script above does not set
  `source_system`, so alerts that Zabalack must claim arrive by another
  path or with an extended action template. That is for the Zabalack
  session to state.

## Decisions and inputs needed

| ID | Question | Who |
|---|---|---|
| ZBX-1 | Which path production uses today: this script (which version of the Alerta client on the Zabbix host), a Zabbix webhook media type, or Zabalack's own sender | Owner (the "real payloads" PIN input) |
| ZBX-2 | Pin `alerta/zabbix-alerta` at `b6ed6bc7` as the mapping reference, read-only, as `reference/` is | Owner, Codex |
| ZBX-3 | Whether a native receiver is wanted at all if the script path already works | Owner |
