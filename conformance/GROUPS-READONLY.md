# Read-only user-group paging oracle

One proposed reference case: `GET /groups?page=1&page-size=1`, existing synthetic
unscoped-admin fixture, an actually attested empty disposable groups table.
Source plan GRP-S1..S5 was explicitly approved in signed `ea14a636`. This harness
head needs its own review. No runner or HTTP window is included or approved.

Reference server `f4312727783dad64198b62ed778bbca98a5144a3`:
`views/groups.py:78-108` count/Page/fetch and empty branch; `utils/paging.py:7-33`
positive page-size 1 arithmetic; `models/group.py:135-140` find/count delegation;
`database/backends/postgres/base.py:1144-1159` matching query predicates.
The original eight-key response must be exactly `status: ok`, integer page1,
integer pageSize1, integer pages0, boolean morefalse, message `not found`, empty
groups list, integer total0. Compare in memory; normalize and export no fields.

GRP-N1: private schema freshness or API-key seeding alone does not prove empty
groups. A separately reviewed runner must perform a read-only count on its
owned synthetic database and produce a private source/window/server-PID-bound
zero-count record. No SQL is submitted by this HTTP suite.

GRP-N2: the source consumer `group.service.ts:13-18` is called with `{}` by
`groups.store.ts:38-40`, via `GroupList.vue:355-367` at web UI pin
`2687e4afa93b41c4fa1d46201bba9ea38be0d491`. It uses default paging. This fixed
explicit page-size1 case is a paging oracle, not the consumer's request shape
or an executed browser-consumption claim. Covering defaults needs a separately
reviewed scope; no implicit second request is made.

GRP-N3: an empty dataset avoids row reconstruction, including GR-R1. This case
says nothing about the nonempty branch, serialized Group entities, stored member
cardinality (`Group.count` in the schema), or auth/role/customer boundaries.
It does call the route's count path, whose empty result is an attested prerequisite.
User groups here are distinct from alert aggregation `/alerts/groups`.

Before constructing Client, the fixture validates the existing seven-field
effective config using `load_config_attestation`, all four private synthetic
credential roles, and this exact private groups record (0600, regular file,
at most16KiB; no symlink): `project: alrust`, `fixture: SYNTHETIC`,
`task: ALRUST-P0-GROUPS-READONLY-01`, exact reference head and loopback target,
`window_id`, actual owned integer `server_pid`, integer `groups_count: 0`.
Expected `ALRUST_GROUPS_WINDOW_ID` and `ALRUST_GROUPS_SERVER_PID` must be bound
by the runner to its acknowledged tuple and actual process. A structurally valid
JSON record alone is not proof of its producer, count, live PID or serving app.
Runner review must independently establish those facts and served-config binding.

Runtime environment names: `ALRUST_TARGET`, `ALRUST_CONFIG_ATTESTATION_FILE`,
`ALRUST_SYNTHETIC_KEYS_FILE`, `ALRUST_GROUPS_ATTESTATION_FILE`,
`ALRUST_GROUPS_WINDOW_ID`, `ALRUST_GROUPS_SERVER_PID`. Private paths/values stay
out of Git and signed packets. Use `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and the
focused `groups-pytest.ini`; ordinary unit tests below require no services.

From the repository root, explicitly select the one file for collection:
`python -m pytest -c conformance/groups-pytest.ini --collect-only -q conformance/tests/http/test_groups.py`.
Alternatively, invoke from `conformance/` with `-c groups-pytest.ini` and the
explicit `tests/http/test_groups.py` path. Never rely on a relative testpaths
entry from a different working directory: pytest can fall back to the entire
test tree. Collection is not HTTP execution or approval to execute that case.

Session attempts once even on failure, emits fixed-message errors, compares
original types (bool is not int), exports fixed metadata only, and closes in
finally. No group/user/permission/customer mutation, anonymous/scope/auth probe,
production data, credential export, Rust/two-server parity or hosted gate claim.
Fresh exact harness/runner reviews and a signed one-heavy-job window with ACK
and START are prerequisites for HTTP. Historical config ACK is not reusable.
