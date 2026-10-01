# One root-index read vector

This harness follows the source-only plan `ALRUST-P0-ROOT-INDEX-READONLY-PLAN-01`
(Codex packet `4c0e0a10`, Claude `693c9861`, qualifications RIN-1 through RIN-5).
That verdict approves the plan only. This new head needs independent source and
offline review; a future producer/runner needs separate exact execution-scope
review, followed by a fresh signed window ACK and START before any HTTP run.

Pinned reference `f4312727783dad64198b62ed778bbca98a5144a3` registers root
GET/OPTIONS in `alerta/views/__init__.py:23–41`. The handler emits status `ok`,
the no-path `absolute_url()` URI, description `Alerta API` and every registered
rule, followed by every custom webhook rule, stably sorted by literal href.
Registered method sets omit HEAD/OPTIONS. `CustomWebhooks.iter_rules()` at
`alerta/utils/webhook.py:40–46` emits each successfully loaded registry entry
with methods in the fixed order POST,GET. Setup entry-point declarations do not
prove loading succeeded. Static rules are included if actually registered.
`absolute_url()` at `alerta/utils/response.py:22–27` preserves the no-path
trailing slash; its exception fallback is outside this controlled case.
With BASE_URL empty and request.url_root ending in slash, the top-level URI
ends in one slash but the literal `/` rule href ends in two: the helper joins
an empty path against base_url plus slash. Both are compared exactly. This
was verified using the original helper with synthetic inputs, not HTTP.

The expected inventory must come from the **actual served** `url_map` iteration
and loaded custom registry after healthcheck readiness, frozen before this case.
It must never come from the response under test or a static OpenAPI path count.
The producer records ordinary rows in their own iteration order, then custom
rows in their registry order, without sorting, deduplication or assuming a
static folder. Each row has exactly endpoint, literal rule and ordered methods.
The loader computes literal hrefs from the controlled observed root URI and
stably sorts the combined expected list. Every duplicate and equal-href tie is
preserved. JSON objects are compared by value; list order remains strict.

One explicit `Client.request('GET', '/')` requires integer 200, exact root keys,
status/description/URI, and the complete ordered row inventory. Only ordinary
registered method-token order may differ: membership and multiplicity remain
strict, with no whitespace, case folding or additional methods accepted. An
empty method set requires exactly the empty string. Custom POST,GET order is
strict. No timestamp, href, URI, row, body or field normalization is performed;
there is no retry or body export. Originals remain in memory. Fixed assertion
messages and a completion-only safe summary avoid exporting response content.

Before constructing Client, factory/served config and synthetic credentials
must pass their existing controlled prerequisites. The new task/window/PID
chain additionally requires private nonsymlink regular 0600 JSON records,
bounded to 128 KiB and without duplicate keys: 12-field connection observation,
13-field provenance, five-field readiness marker and 10-field route inventory.
Provenance hashes the **complete original bytes** of connection, served config,
readiness and inventory. All task/source/target/window/PID bindings must match.
The controlled config requires BASE_URL empty; exact inventory URI is target
plus slash, never the `/config` endpoint with its slash stripped. No old
root-debug or GTG chain or window can authorize this scope.

These files are accident guards under one Unix user, not a security boundary
or proof of truthful observations. A separately reviewed producer must observe
the actual served connection URI/name/schema/live destination and route map
after readiness, then recheck its own request connection, config and inventory
before the original root handler. Setup and case connections may differ after
teardown. It must not rewrite the frozen records or issue SQL in the case hook.
The handler has no SQL call; factory/readiness and global hooks still require
their usual dependencies. No whole-stack DB independence or auth guarantee is
inferred from the lack of a permission decorator.

Offline synthetic cases and source-derived expectations are separate evidence
from future reference HTTP. This scope makes no HEAD/OPTIONS, CORS/proxy/auth,
properties/config-dump, keep/drop, production, Rust parity, two-server,
full-suite or hosted gate claim. Existing customer filtering and owner-only
behavior/authentication/data-loss decisions are preserved.

Focused offline checks (no service/window):

```sh
cd conformance
/home/user/codex-alrust/envs/conformance/bin/python -m pytest -q tests/unit/test_root_index_readonly.py --assert=plain -p no:cacheprovider
/home/user/codex-alrust/envs/conformance/bin/python -m pytest -c root-index-pytest.ini --collect-only -q
```

Collection is not HTTP evidence. Actual reference HTTP remains NOT_RUN until
the fresh reviewed runner and mutually acknowledged window exist. Hosted
billing-blocked checks remain CI_BLOCKED_BILLING; local success cannot satisfy
the Phase 0 required hosted gate.
