# Unknown custom-webhook reference vector

This is a Phase 0 harness candidate. It has no producer, runner, signed execution
window or HTTP result yet. Claude must review the exact new commit in the
`CODE_SOURCE_OFFLINE` scope. Plan approval is not code or execution approval.
The pinned reference is `f4312727783dad64198b62ed778bbca98a5144a3`.

One explicit request is proposed: `GET /webhooks/alrust-synthetic-unregistered`,
without a query or body, using the labeled synthetic unscoped-admin key and fixed
non-secret `X-Request-ID: alrust-synthetic-unknown-01`. `Client` admits that exact
string only; its default behavior and Authorization construction are unchanged.
It does not accept arbitrary headers or override credentials.

Compare status as a strict integer 404, then exactly these five JSON fields:
`status=error`, `message=Custom webhook 'alrust-synthetic-unregistered' not found.`,
strict integer `code=404`, `errors=null`, and the supplied fixed `requestId`.
No field, timestamp, body, URL, request ID or response value is normalized.
Object member ordering is immaterial; all keys and original values are compared.
No response-derived oracle, body export or retry is permitted. Completed comparison
state is retained before a post-case frozen-file failure. A future runner must
persist that partial outcome before post-case observations and late Git metadata;
this candidate does not yet implement that runner or its aggregate report.

The permission path verifies the key before unknown-name dispatch. Source
`models/key.py:162–166` and `database/backends/postgres/base.py:1002–1008` expect
one authentication usage UPDATE before the 404. This vector is **not database
read-only**. That is one source-derived statement invocation, not executed row
counts, total setup writes, rollback proof or a claim about lifecycle writes.
The valid, unexpired, unscoped-admin key must permit `write:webhooks`, even when
controlled `AUTH_REQUIRED=False`. Authentication contracts and customer filtering
are unchanged. No invalid keys, Basic/Bearer/Hawk auth or anonymous claim is tested.

## Private prerequisite format

All files have mode 0600 and their immediate parent directory has mode 0700.
The loader refuses final-component symlinks, non-regular files, duplicate JSON
keys and records larger than 128 KiB. Same-user files and hashes guard accidents;
they are not a security boundary or independent proof that observations occurred.

Seven records have the same exact identity fields: `project=alrust`,
`fixture=SYNTHETIC`, `task=ALRUST-P0-WEBHOOK-UNKNOWN-01`, full `reference_head`,
`target=http://127.0.0.1:18081`, new
`window_id=alrust-webhook-unknown-reference-YYYYMMDD-NN`, and strict integer
owned `server_pid`. Old task/window records cannot authorize this case. The
loader also freezes the existing private synthetic credential file's whole bytes.

| Record | Additional exact fields and prerequisites |
| --- | --- |
| connection | `backend=postgresql`; four strict true checks: `backend_uri_matches`, `database_name_matches`, `schema_matches`, `connection_destination_matches` |
| ready | `ready=true` |
| config | `effective_config`: existing controlled config fields, plus `LOG_CONFIG_FILE=''`, `LOG_LEVEL=ERROR`, `LOG_HANDLERS=['console']` |
| registry | `loaded_names`: ordered, unique actual successfully loaded registry names; the proposed name is absent |
| logging | Actual `current_app.logger`: `logger_name=alerta.app`, strict integer `effective_level=40`, `info_enabled=false`, `app_debug=false`, `filters=[]`; actual applicable logger/ancestor `handlers` chain contains one `logging.StreamHandler`, integer `level=40`, filters `ContextFilter,RequestFilter` in observed order |
| keyproof | `before_case_only=true`, `role=unscoped_admin`, `key_sha256` matching private key bytes, `customer=null`, `scopes=['admin']`; strict true `stored_key_matches`, `valid`, `unexpired`, `write_webhooks_allowed` |
| provenance | Strict true `after_readiness`, `same_served_connection`; `record_sha256` maps all six other record names to their complete original file hashes; `credentials_sha256` binds the whole credential file |

The separately reviewed producer must observe the actual served process after
readiness, not construct a convenient logger or infer a registry from metadata,
OpenAPI counts or this response. It must recheck registry/config/connection/logger
identity before the original handler and after the completed case, without new
hook SQL or plugin injection. Frozen file checks in this candidate do not substitute
for those live observations. The keyproof is **pre-case only**; rereading its frozen
bytes does not re-query key rows, assert count/last-used immutability or measure
usage. No count or last-used fields are admitted in that proof.

`DEBUG=false` in config and actual `app.debug=false`, actual INFO disabled,
exactly empty custom log-config path, handler/filter identity and whole-byte source
bindings are prerequisites. INFO logging source still builds the header/body
string in memory. Nothing here fixes or verifies production W-SEC-1. A reviewed
future runner must scan private raw server logs against actual synthetic keys after
the case and fail on a match. Independently scan every eligible final shared artifact
against actual keys and the generic exclusion pattern; never export raw config,
keys, CLI output, logs, HTTP bodies or private attestations through Git/mailbox.

## Offline checks and future collection

With the existing hash-locked conformance environment, from this worktree:

```sh
/home/user/codex-alrust/envs/conformance/bin/python -m pytest -c conformance/pytest.ini conformance/tests/unit/test_webhook_unknown_contract.py --assert=plain -p no:cacheprovider -q
/home/user/codex-alrust/envs/conformance/bin/python -m pytest -c conformance/webhook-unknown-pytest.ini conformance/tests/http/test_webhook_unknown.py --collect-only -q
```

Collection makes no HTTP request. Future execution requires fresh exact harness,
producer, runner and full cleanup-verifier reviews, then a wholly new signed
REQUEST/ACK/START bound to source/artifacts/commands/resources/duration/stop
conditions. The old root-index window is closed and its ACK cannot be reused.
The reviewed unoptimized cleanup guard must be embedded unchanged and invoked
first, before private evidence/state/subprocess access, with its flags in the
original cleanup report; component approval does not approve the full verifier.

Future runner inputs are `ALRUST_TARGET`, `ALRUST_SYNTHETIC_KEYS_FILE`,
`ALRUST_WEBHOOK_UNKNOWN_WINDOW_ID`, `ALRUST_WEBHOOK_UNKNOWN_SERVER_PID`, and
`ALRUST_WEBHOOK_UNKNOWN_<RECORD>_FILE` for each record in the table. These inputs
alone grant no execution authority. Setup/readiness SQL and HTTP remain separate.
Even a future reference pass would not establish provider mapping, Rust parity,
two-server conformance, full Phase 0 CI, production logging or measured DB writes.
Hosted billing-blocked gates remain blocked; local passes cannot satisfy them.
