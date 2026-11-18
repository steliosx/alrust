# Zabbix inbound: the three paths

Status: **Proposed, 15 November 2026.** Source-derived; no vector has been
run. Owner answer to ZBX-1 (2026-11-14, Claude's session, relayed to Codex
in `55714c2f`): "We need to support both ways plus zabalack". alrust
therefore supports three paths:

1. **Script path:** Zabbix runs the `zabbix-alerta` alert script, which
   sends `POST /alert` through the Python Alerta client.
2. **Native path:** a Zabbix 7.0 webhook media type posts to alrust
   directly (`POST /webhooks/zabbix`, proposed below).
3. **Zabalack path:** alerts that the Zabalack integration claims
   (`attributes.source_system = 'zabbix'`, `spec/integration-hooks.md`
   section 3).

The analysis of where the mapping lives is in
`docs/design/zabbix-inbound.md`, which is in pending PR #232 and not yet on
`main`; the link works once #232 merges. This file fixes the contract and
the vectors.

## Sources

Neither source is in `reference/`. Pinning `zabbix-alerta` as a read-only
reference is ZBX-2 (owner and Codex), still open. Until then the two files
are identified by commit and SHA256 only, read through the GitHub API on
2026-11-15:

| Source | Commit | File | SHA256 |
|---|---|---|---|
| `alerta/zabbix-alerta` 4.0.0 (MIT) | `b6ed6bc76e36bb88600689314de4b5fcbbae55c8` | `zabbix_alerta.py` | `894a50580f9d6880d06850bc30f451a06ce2aee61f20a76db94f0ad161e37655` |
| `alerta/python-alerta-client` v8.5.3 (Apache-2.0) | `b166ba7abb6f12fdd5123c2801462e28fef8660c` | `alertaclient/api.py` | `b25f525faaee076394ae951ac98549dbbddd5f1083030c600d2558625910f721` |

The client version on each Zabbix host is **not known** (part of ZBX-1's
"which version" question). v8.5.3 is the newest tag. An older client can
build a different request; Z-PIN below says how to find out.

## 1. Script path: what reaches `POST /alert`

`parse_zabbix(subject, message)` (lines 34-85) builds a dict; `cli` then
calls `Client.send_alert(**alert)` (line 204), which builds the request
body (`api.py` lines 47-68).

### parse_zabbix

For each line of `message.split('\n')`:

- A line without `=` is skipped.
- `macro, value = line.rstrip().split('=', 1)`: only the right side is
  stripped (so `\r` of a CRLF line goes, a leading space stays part of
  the macro name), and the value is everything after the first `=`.
- `service` and `tags`: `value.split(',')`, no stripping.
- `severity`: if the value ends with `!!`, it is used with **every**
  `!!` removed (`str.replace`), and `zabbix_severity` is set. Otherwise
  the Zabbix name maps: Disaster `critical`, High `major`, Average
  `minor`, Warning `warning`, Information `informational`, Not
  classified `indeterminate`; anything else `indeterminate`.
- `timeout`: `int(value)`. A value that `int()` refuses raises
  `ValueError`, which `cli` turns into an abort: **no alert is sent**.
- A macro for which `macro.startswith('attributes.')` is true:
  `attributes[macro.replace('attributes.', '')] = value`. `replace`
  removes **every** occurrence of `attributes.`, so `attributes.a.attributes.b`
  gives the key `a.b`.
- Every line also sets `alert[macro] = value`; a repeated macro keeps
  the last value.

Then:

- `environment` equal to `{$ENVIRONMENT}` (an undefined user macro)
  becomes `Production`.
- `status` is popped. `OK` sets severity `ok` when `zabbix_severity`,
  else `normal` (this overrides the severity line).
- `ack` is popped; `Yes` with a status other than `OK` sets
  `alert['status'] = 'ack'`.
- `attributes`, `origin = 'zabbix/' + <hostname of the Zabbix host>` and
  `rawData = subject + '\n\n' + message` are set.

### send_alert (client v8.5.3)

`send_alert(self, resource, event, **kwargs)` takes `resource` and `event`
as required arguments. **If the message has no `resource` or no `event`
line, the call raises `TypeError` before any request is built; the script
aborts and sends nothing.** Otherwise the body has exactly these keys:
`id`, `resource`, `event`, `environment`, `severity`, `correlate`
(`or []`), `service` (`or []`), `group`, `value`, `text`, `tags`
(`or []`), `attributes` (`or {}`), `origin`, `type`, `createTime` (the
client's clock), `timeout`, `rawData` (read from the keyword
**`raw_data`**), `customer`. An optional key the message does not give is
`null`. So, with this client:

- **The `rawData` that `parse_zabbix` builds is lost.** It is stored
  under the key `rawData`, but the client reads `raw_data`. Because
  `parse_zabbix` keeps every macro name, a `raw_data=<text>` line does
  reach the client, which then sends `rawData` `<text>`. Without such a
  line, `rawData` is null.
- **`status` is not sent**, so `ack=Yes` does not acknowledge the alert.
- `dateTime` and the `attributes.<k>` macros as top-level keys are not
  sent; the `attributes` dict is.
- `createTime` is the Zabbix host's clock at send time, not the event
  time.
- An `id=` line becomes the alert id; a `customer=` line the customer.

alrust needs no new code for this path: it serves `POST /alert`.
Qualification is replaying the bodies below (and, for Z-PIN, captured
production requests) against both Alerta and alrust.

## 2. Native path: `POST /webhooks/zabbix` (proposal)

A Zabbix 7.0 webhook media type runs a JavaScript that posts JSON. The
proposal keeps the alert script's three inputs, so the Zabbix action
templates stay as they are:

```json
{"subject": "{ALERT.SUBJECT}", "message": "{ALERT.MESSAGE}"}
```

- **Auth:** an API key, scope `write:webhooks`, as for the other
  receivers (Y27). The key is a media-type parameter sent in the
  `Authorization` header; the request log rules of SF-WEBHOOKS W-SEC-1
  apply.
- **Mapping:** `parse_zabbix` exactly as section 1, then the
  `send_alert` projection, then normal `POST /alert` processing (reject
  policy, blackouts, hooks). Refused with 400, storing nothing: a body
  that is not a JSON object, a `subject` or `message` that is not a
  string, and a message without a `resource` or an `event` line (where
  the script aborts on `TypeError`).
- **Origin:** the script uses the Zabbix host's name, which the receiver
  cannot know. ZBX-4 below.
- **Differences from the script path:** ZBX-5a to ZBX-5c below. Without an owner
  choice the native path produces what the script path produces with
  client v8.5.3, so one Zabbix action gives the same alert either way.
- **`timeout` that `int()` refuses:** the script sends nothing; the
  receiver answers 400 `Could not convert 'timeout' value of '<v>' to an
  integer` and stores nothing. `int()` follows Python 3.12 (Unicode
  decimal digits, `_` between digits, its own strip set), as in the
  Prometheus receiver (`pytext.rs` in #233).

## 3. Zabalack path

Zabalack claims alerts whose `attributes.source_system` is `zabbix`. The
script does not set it, but one extra template line does:
`attributes.source_system=zabbix`. Both paths above then carry it in the
`attributes` dict. What Zabalack's own sender (if any) posts is for the
Zabalack session to state (ZBX-6).

## Decisions and inputs

| ID | Question | Who | Recommendation |
|---|---|---|---|
| ZBX-2 | Pin `zabbix-alerta` `b6ed6bc7` read-only as the mapping reference | Owner, Codex | Pin it; until then the SHA256 above |
| ZBX-4 | Native origin: a fixed `zabbix/webhook`, a media-type parameter `origin` in the body, or the API key's user | Owner | Optional body field `origin`, default `zabbix/webhook` |
| ZBX-5a | Native path, `rawData`: copy the client (only an explicit `raw_data=` line, else null), or store the generated subject and message | Owner | Store the generated subject and message, as the script intends |
| ZBX-5b | Native path, when ZBX-5a keeps the generated text and the message also has a `raw_data=` line: which one wins | Owner | The explicit `raw_data=` line, because the Zabbix action asked for it |
| ZBX-5c | Native path, `ack=Yes`: copy the client (ignored) or acknowledge | Owner | Acknowledge, as the script intends |
| ZBX-6 | Zabalack's own Zabbix sender, if any: its exact request | Zabalack session | Ask once the native path is reviewed |
| Z-PIN | Client version on each Zabbix host, or captured requests | Owner | Captured requests are the stronger evidence |

## Vectors

Source-derived from the two files above, **NOT_RUN**. "Body" lists the
non-null keys of the script path's request (client v8.5.3); every body
also has `correlate []`, `createTime`, and `origin zabbix/<host>`, and
`rawData` null unless the row says otherwise. The native path gives the
same body, with the ZBX-4 origin, unless ZBX-5a to ZBX-5c change it.

| # | Message lines (subject `s`) | Body (script path) |
|---|---|---|
| Z1 | `resource=db01`, `event=vfs.fs.size[/,pfree]`, `environment=Production`, `severity=major!!`, `status=PROBLEM`, `ack=No`, `service=Linux servers,DB`, `group=Zabbix`, `value=3 %`, `text=PROBLEM: Disk full`, `tags=os:linux,team:ops`, `attributes.eventId=101`, `attributes.ip=10.0.0.5`, `type=zabbixAlert`, `dateTime=2026.11.14T10:00:00Z` | `resource db01`, `event vfs.fs.size[/,pfree]`, `environment Production`, `severity major`, `service ["Linux servers","DB"]`, `group Zabbix`, `value "3 %"`, `text "PROBLEM: Disk full"`, `tags ["os:linux","team:ops"]`, `attributes {"eventId":"101","ip":"10.0.0.5"}`, `type zabbixAlert`; no `status`, no `dateTime` |
| Z2 | `resource=r`, `event=e`, `severity=High!!`, `status=OK` | `severity ok` |
| Z3 | `resource=r`, `event=e`, `severity=High`, `status=OK` | `severity normal` |
| Z4 | `resource=r`, `event=e`, `severity=Disaster` | `severity critical` |
| Z5 | `resource=r`, `event=e`, `severity=Bogus` | `severity indeterminate` |
| Z6 | `resource=r`, `event=e`, `severity=High!!` | `severity High`: not an Alerta severity, so the server refuses it (the default template's `{TRIGGER.SEVERITY}!!` sends Zabbix names) |
| Z7 | `resource=r`, `event=e`, `status=PROBLEM`, `ack=Yes` | no `status`: the alert is not acknowledged (client drops it) |
| Z8 | `resource=r`, `event=e`, `environment={$ENVIRONMENT}` | `environment Production` |
| Z9 | `resource=r\r`, `no equals here\r`, `event=e=f\r`, ` value=lead\r`, `timeout=60\r` | `resource r`, `event e=f`, `timeout 60`; ` value` (leading space) is an unknown key, so no `value` |
| Z10 | `resource=r`, `event=e`, `timeout=soon` | nothing sent (`ValueError`; the script aborts) |
| Z11 | `resource=r`, `event=e`, `timeout=١٢` (Arabic-Indic) | `timeout 12` |
| Z12 | `resource=r`, `event=e`, `severity=ma!!jor!!` | `severity major` (every `!!` removed) |
| Z13 | `resource=r`, `event=e`, `id=abc`, `customer=acme` | `id abc`, `customer acme` |
| Z14 | `resource=r`, `event=e`, `attributes.source_system=zabbix` | `attributes {"source_system":"zabbix"}` (Zabalack claims it) |
| Z15 | `resource=a`, `resource=b`, `event=e` | `resource b` |
| Z16 | `event=e` | nothing sent (`send_alert` lacks `resource`: `TypeError`; the script aborts) |
| Z17 | `resource=r` | nothing sent (no `event`: `TypeError`) |
| Z18 | `resource=r`, `event=e`, `raw_data=payload` | `rawData "payload"` |
| Z19 | `resource=r`, `event=e`, `attributes.a.attributes.b=v` | `attributes {"a.b":"v"}`; the server then refuses it (`Alert()` allows no `.` in an attribute key) |

Native-path-only vectors (NOT_RUN): N1 body `{"subject": "s"}` (no
`message`) gives 400; N2 a key without `write:webhooks` gives 403; N3 the
Z1 message gives 201 with the Z1 body and the ZBX-4 origin; N4 the Z10
message gives 400 and stores nothing; N5 the Z16 and Z17 messages give
400 and store nothing.
