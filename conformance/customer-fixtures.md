# Synthetic customer-mapping fixtures

Task ALRUST-P0-CUSTOMER-CONFORMANCE-01. Author codex-alrust; reviewer
claude-alrust. The four black-box HTTP cases are SOURCE_DERIVED, NOT_RUN.
They exercise the customer-mapping slice approved at
`adcc502c85ce132fb1822447dba97b645f7abed9`, against pinned Alerta source
`f4312727783dad64198b62ed778bbca98a5144a3`. The spec is handed off separately;
it is not silently imported into this branch. Test code imports neither server.

| Case | Source-derived expectation | Pinned source inside `reference/alerta` |
|---|---|---|
| Assignment and mismatch | A writer's omitted customer becomes its key customer; matching explicit customer deduplicates; mismatching customer gets 400 | `alerta/utils/api.py:14-28`, `alerta/auth/decorators.py:34-48` |
| Uncredentialed explicit customer | With auth disabled and no key, explicit customer still gets 400 | `alerta/auth/decorators.py:156-161`, `alerta/utils/api.py:14-28` |
| Dedup identity | Identical environment/resource/event/severity for A, B and null customer produces three independent identities and duplicate counts | `alerta/database/backends/postgres/base.py:179-198`, `:216-240` |
| Scoped admin | Admin may override inbound customer; reads/list/count/status/action/delete remain restricted to its key customer, including exclusion of null-customer rows; denied mutations leave the complete observable alert unchanged | `alerta/utils/api.py:17-18`, `alerta/views/alerts.py:88-162`, `:279-397`, `alerta/database/backends/postgres/utils.py:161-163`, `alerta/database/backends/postgres/base.py:293-299` |

This preserves reference filtering. It neither adds per-customer RBAC nor
approves removing existing filtering based on the internal-NOC kickoff wording.
No authentication contract or intentional divergence is changed here.

Use a fresh disposable server/database, default plugins, no blackout,
`AUTH_REQUIRED=False`, `CUSTOMER_VIEWS=False`, and a dedicated synthetic admin
login in `ADMIN_USERS`. Provision four synthetic keys through the reference's
management CLI (`alerta/commands.py:45-109`), using explicit `--username`,
`--key`, `--scope`, optional `--customer`, and `--force`. Explicit `--key`
avoids an unlabeled generated key. Generate each value as `SYNTHETIC-` plus
48 random lowercase hex characters. CLI output contains keys: keep it in
private runtime evidence, never Git or mailbox messages. Do not use HTTP
`POST /key` unauthenticated: the reference replaces the requested user with
`g.login`, which is null in that mode. Provisioning is separate from these
black-box tests and must be included in a new signed heavy-test window.

Write a private mode `0600` regular file outside Git, named
`alrust-synthetic-*.json`, no larger than 16 KiB. It has this schema (angle
bracket values below are placeholders, not runnable credentials):

```json
{
  "fixture": "SYNTHETIC",
  "target": "http://127.0.0.1:18081",
  "auth_required": false,
  "customer_views": false,
  "customers": {
    "a": "alrust-synthetic-customer-a",
    "b": "alrust-synthetic-customer-b"
  },
  "keys": {
    "unscoped_admin": {"key": "<SYNTHETIC-48-random-hex>", "customer": null, "scopes": ["admin"]},
    "a_admin": {"key": "<different-SYNTHETIC-48-random-hex>", "customer": "alrust-synthetic-customer-a", "scopes": ["admin"]},
    "a_writer": {"key": "<different-SYNTHETIC-48-random-hex>", "customer": "alrust-synthetic-customer-a", "scopes": ["write:alerts"]},
    "b_writer": {"key": "<different-SYNTHETIC-48-random-hex>", "customer": "alrust-synthetic-customer-b", "scopes": ["write:alerts"]}
  }
}
```

The loader validates exact target, declared config, role metadata, distinct
labels/keys, file type and private permissions before customer requests. These
declarations do not attest to a live server or its actual key records. Preserve
source/config/provisioning evidence and inspect actual behavior during a run.
A read-only file under the same Unix user is not a security boundary.

After independent exact-head review and a signed window agreement:

```sh
ALRUST_TARGET=http://127.0.0.1:18081 \
ALRUST_SYNTHETIC_KEYS_FILE=/private/runtime/alrust-synthetic-keys.json \
/path/to/python3.12 -m pytest -c conformance/pytest.ini conformance/tests/http
```

Missing credential-file inputs fail with NOT_RUN before customer HTTP effects;
incoherent fixtures fail validation. No skip or empty success satisfies a gate.
Keys are sent only as Authorization headers, never URL query parameters.
Credentials are excluded from their object's repr; auth error bodies are not
printed by status assertions. Keep pytest logs private; avoid `--showlocals`
and traffic debug logging. Offline unit fixtures contain clearly synthetic
dummy values only.

Every stored alert uses a UUID synthetic resource, `SYNTHETIC` tag and explicit
UTC millisecond createTime. Cleanup targets only IDs acknowledged within that
namespace, with the synthetic unscoped admin key. Missing acknowledgments or
cleanup errors fail the run; disposable database/container teardown remains
necessary even after a failed test. Evidence for attempted runs is preserved.
