# Conditional permissions reference case

Task `ALRUST-P0-PERMISSIONS-READONLY-01` implements the independently reviewed
PERM-S1–S5 plan. The plan approval is not execution approval for this harness,
a runner or a resource window. The first HTTP run remains NOT_RUN.

At reference server `f4312727783dad64198b62ed778bbca98a5144a3`, one original
`GET /perms?page=1&page-size=1` on an attested empty stored permissions table
returns the three virtual admin, user and guest rows, in that order. They are
prepended before stored pagination. The original seven root keys are compared
with type-exact values: status `ok`, page 1, pageSize 1, pages 3, more true,
total 3 and the three permissions. No `message` key is expected. Three rows at
size 1 is reference behavior; the oracle must not trim them to one.

Rows have exactly id, href, match and scopes. Compare object keysets and values,
not object key order: Flask's default JSON provider sorts object keys. Preserve
the builtin list order. Admin scopes are the pinned `ADMIN_SCOPES` code constant
(`admin`, `read`, `write`), not a configuration setting. User scopes (`read`,
`write`), guest scopes (`read:alerts`) and all three default role names must be
attested from the actual served app, alongside a fresh stored permission count
of exact integer zero. A reviewed runner must bind that setup evidence to the
same actual served database, connection destination, PID, acknowledged window,
readiness marker and served-config hashes. File shape validation alone cannot
prove those observations; no previous groups/config attestation can supply them.

`Permission.__init__` generates each virtual id with `str(uuid4())`. Assert
canonical UUID4, RFC 4122 variant and three distinct ids within this response.
Assert each original href equals the target plus `/perm/` plus its original id.
The pinned `absolute_url` uses `urljoin(request.url_root + '/', 'perm/<id>')`:
on Python 3.12.14 it collapses the resulting double slash to the expected href.
The eventual run observes the actual Flask request. There is no normalization,
id substitution, timestamp comparison, persistence or cross-request identity
claim. Original bodies, ids and hrefs remain in memory; reports contain fixed
metadata only, including on failed comparisons.

Sources: `views/permissions.py:65-129`, `models/permission.py:18,35-41`,
`models/enums.py:144`, `settings.py:65-72`, `database/backends/postgres/base.py:1235-1250`,
`utils/response.py:22-27`, `utils/paging.py:7-33` at the pinned server.
The pinned UI (`2687e4afa93b41c4fa1d46201bba9ea38be0d491`)
`src/services/api/perms.service.ts:7-11` and `src/store/modules/perms.store.ts:30-33`
consume default paging (`getPerms({})`); this deliberately explicit paging case
does not execute that UI call graph or its defaults.

The existing labeled synthetic unscoped-admin fixture satisfies `read:perms`
through admin. This is not authorization evidence. P-SEC-1, PR-R1, empty-string
scope handling, all writes, filters, injection, `/perm` detail, `/scopes`,
production inputs, Rust parity and two-server comparison remain outside this
case. Setup SQL belongs to the reviewed runner, never the HTTP suite. The
harness performs one read attempt and does not retry a failed request.

From `conformance/`, collect only the explicit candidate without sending HTTP:

```sh
python -m pytest -c permissions-pytest.ini tests/http/test_permissions.py --collect-only -q
```

Execution requires fresh exact-head harness and runner reviews, a matching
signed window ACK and START, and private config, permissions and fixture files.
Use `ALRUST_TARGET`, `ALRUST_CONFIG_ATTESTATION_FILE`,
`ALRUST_PERMISSIONS_ATTESTATION_FILE`, `ALRUST_PERMISSIONS_WINDOW_ID`,
`ALRUST_PERMISSIONS_SERVER_PID` and `ALRUST_SYNTHETIC_KEYS_FILE`. A separately
reviewed runner owns their creation. Never place fixture values in Git or shared
evidence. The prior groups window is closed; its acknowledgment cannot authorize
this case. Local results and CI_BLOCKED_BILLING remain separate; owner merge,
release, production, auth-contract and repository protection gates still apply.
