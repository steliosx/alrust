# Ordinary alert-note vectors

These two cases are **SOURCE_DERIVED / HTTP NOT_RUN** at this new source
head. They cite the read-only reference
`f4312727783dad64198b62ed778bbca98a5144a3` directly. The new OpenAPI note
description still has review corrections pending; its wording does not
establish runtime behavior or approve these cases.

| Case | Expected reference behavior | Source |
|---|---|---|
| Owned note create, update and delete | Create records the alert snapshot and app-clock time; list returns the one owned note. Object attributes merge shallowly, including a null leaf, and text updates. Note update leaves alert fields/history unchanged. Delete removes the note and appends a dismiss entry to the API history; other alert fields remain exact | views/alerts.py:596-722; models/note.py:18-28,44-67,110-145; models/alert.py:572-607; postgres/base.py:401-408,1368-1441 |
| Missing text and fallback alias | Empty ordinary request objects or empty text/note give 400 before any note write and leave the alert and empty note list unchanged. A truthy note alias is used when text is empty | views/alerts.py:601-613,637-662 |

Each case uses the existing `metadata_session` fixture and one fresh
UUID-owned normal major/open alert. A successful 201 note ID is tracked
before further assertions; only those IDs are deleted in finally, before
the fixture deletes its owned alert and closes the client. The list is
asserted empty initially and contains only one note, so no unspecified
ordering or regex-selection inference is needed. Complete raw responses
are consumed unchanged; only known synthetic credential echoes are
redacted for private evidence export. No timestamp, note ID, history or
list ordering is normalized.

Prerequisites are the existing explicit synthetic configuration and actual
stored fixture keys: AUTH_REQUIRED=False, CUSTOMER_VIEWS=False, the default
ALERTA model/plugins, no forwarding destinations, and the fixture's
unscoped `alrust-synthetic-admin` key. A later bounded runner must attest
those inputs independently before execution. No default plugin implements
take_note; custom-plugin behavior is outside this scope. App timestamps
and the database update timestamp are each validated as UTC millisecond
strings, without assuming equality or ordering between those clocks.
Distinct app-history events are spaced by 50ms; the note's createTime stays
exact across the database-backed update/list responses. Note updateTime
is preserved and validated, not discarded from comparisons.

These ordinary object inputs do not cover reserved request id, non-object
attributes, arbitrary client alert IDs, cross-alert/customer note access,
anonymous author retention, plugin rejection/forwarding loops or failure
after partial persistence. Those source findings remain separate from any
executed evidence or owner choice about Alrust behavior. Existing customer
filtering and write permissions remain intact.

The parent HF7 source `4f808c9d1c1615d5fb347e4cbed3d597c8f566b5` has source
approval `93d30e4b`, but its new-head smoke HTTP and runner execution scope
are still pending. Historical query HTTP passed only at frozen
`9e33dc2af0292381db1d4f4afebe25170ca68568`, metadata corrections only at
`a5ce0e122646f4c557ae46f7901ffc855adf6a95`. Neither verdict nor runtime
evidence approves this changed note head. It needs fresh independent
source review, a separate reviewed runner and a fresh signed window.
Reference-only smoke cannot prove Rust parity or satisfy the full Phase 0
hosted gate. Hosted CI remains separately **CI_BLOCKED_BILLING**.
