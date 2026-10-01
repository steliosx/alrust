# Benign alert query vectors

These three cases are **SOURCE_DERIVED / HTTP NOT_RUN** against reference
`f4312727783dad64198b62ed778bbca98a5144a3`. They extend the language-neutral
Phase 0 conformance approach without choosing a behavioral divergence.

| Vector | Expected reference behavior | Source |
|---|---|---|
| Filtered list and counts | One uniquely owned alert yields total 1, major/open counts of 1, a single-item page, default hidden history, unchanged alert fields and lastTime equal to that page's lastReceiveTime | views/alerts.py:318-361,388-402; postgres/base.py:410-440 |
| History display flag | show-history=true exposes the exact saved history; false hides it without changing other alert fields | views/alerts.py:319-320; postgres/base.py:412-423; models/alert.py:204-232 |
| Repeated plain resource and event negation | Repeated resource values use ANY; adding event!=the stored event excludes the owned row and produces zero counts | postgres/utils.py:55-104,109-198; views/alerts.py:388-402 |

Each case imports only the existing `metadata_session` fixture, creates one
UUID-owned normal major/open alert, and uses exact resource filters. The
fixture records successful create IDs before further assertions and deletes
only owned IDs on teardown; the client closes in finally. No other imported
test is executed. The existing client disables environment proxies and sends
no forwarding header. Default plugins and synthetic configuration remain
unchanged. Requests consume raw responses; only evidence export redacts
known synthetic keys. No timestamp, history, identifier or list order is
normalized. Each case confirms the stored alert is exactly unchanged after
its read queries.

The created fixture has rawData=null. These cases validate default display
of that value, **not non-null rawData or show-raw-data behavior**. autoRefresh
is checked for boolean shape and consistency within an unchanged isolated
switch state, without choosing a new switch value. One-row filtered pages
avoid assuming an unspecified ordering. The nonempty page's lastTime is
compared exactly with its saved lastReceiveTime; empty-page query-clock
semantics are outside this scope.

Only ordinary values are sent. No q grammar, regex input, malformed binding,
SQL/injection probe, arbitrary attribute sort or group-by, global metadata
customer enumeration, customer-policy change, history aggregation, top-N,
negative paging or full differential fuzzer is included. Existing customer
filtering must remain intact. A scoped reference pass would not establish
Rust/two-server parity or satisfy the full Phase 0 hosted gate.

The parent `a5ce0e122646f4c557ae46f7901ffc855adf6a95` has Claude's fresh
source/offline approval `c10b6b6e` for three **metadata mutation** cases.
That review does not cover this new query head. Its corrected metadata HTTP
run is still NOT_RUN; the original `70a40be3` attempt remains FAIL (1 failed,
2 passed) with the last-attribute removal oracle not reached. Neither that
failed attempt nor any prior released window authorizes these query cases.

Before execution, this source needs fresh independent review, a distinct
runner with explicit scope and prerequisite attestations, separate private
evidence, and a new signed window request/ACK/START. One heavy laptop job at
a time. Real credentials and raw evidence never enter Git or mailbox packets.
Hosted checks remain separately labeled CI_BLOCKED_BILLING.
