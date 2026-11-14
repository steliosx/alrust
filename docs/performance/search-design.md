# Search: `q=` compatibility first, faster search second

Status: **design for review**, 14 November 2026. No code, no measurement.
Requested in Codex's work package `21520154` (owner request) as the task
after the counts and PostgreSQL items. It builds on the
[read pilot](2026-11-13-alerta-comparison.md), which found that alrust
ignores `q` and must never be scored as fast search while it does.

The design has two steps that stay separate:

1. **Compatibility:** `GET /alerts?q=…` (and `/alerts/count`) select exactly
   the alerts that Alerta 9.1.0 selects for the same `q` and `q.df`.
2. **Speed and relevance:** make those queries fast without changing their
   results, then, only if the owner wants it, an opt-in ranked search as new
   API surface.

## Today

- **Reference.** `Alerts.from_params` parses `q` with `QueryParser`
  (`reference/alerta/alerta/database/backends/postgres/utils.py:144-155`) and
  answers 400 "Failed to parse query string." on a parse error. The parsed
  SQL is the first condition; the customer restriction and the other filters
  are added after it with `AND`.
- **alrust.** `q` is accepted and **ignored**: the response is unfiltered
  (`docs/alpha.md`, "Query parameters"). The only `q` check is the privacy
  refusal: a `q` whose text contains a restricted attribute name is refused
  with 400 (`crates/alrust-api/src/lib.rs`, `restricted_selector`;
  `spec/integration-hooks.md` section 4.2).

An ignored `q` returns wrong results without saying so. **Question Q-S1
(Codex, then owner):** until step 1 lands, should alrust refuse a non-empty
`q` with 400 instead of ignoring it? That is a deliberate divergence from
both today's alrust and the reference, but it stops silent wrong answers.
My recommendation is yes, with a message naming the missing feature.

## Step 1: the reference grammar, exactly

From `queryparser.py` at the pinned commit (`f4312727`). Every line below is
a fact to pin with a characterisation test against the reference, not an
assumption.

| Form | Example | Reference SQL (`queryparser.py`) |
|---|---|---|
| Single term, default field | `disk` | `"text" ILIKE '%disk%'` (lines 152-155); default field `text`, or `q.df` (280-289) |
| Field term | `resource:web01` | `"resource" ILIKE '%web01%'` |
| Array field | `service:Web`, `tags:x`, `correlate:y` | `'Web'=ANY("service")` (141-144, 161-164) |
| Attribute | `attributes.region:eu` or `_.region:eu` | `"attributes"::jsonb ->> 'region' ILIKE '%eu%'` (145-151) |
| Exists | `_exists_:region` | `"attributes"::jsonb ? 'region'` (137-140) |
| Phrase | `"disk full"` | `~* '\ydisk full\y'` (156-175), word-bounded, case-insensitive regex |
| Wildcard | `dis*`, `d?sk` | `~* '\ydis.*\y'` (176-179); only `[a-z0-9]` around `?`/`*` (247) |
| Regex | `/^web[0-9]+$/` | `~* '<regex>'` (180-183), caller-supplied |
| Range | `[a TO b]`, `{a TO b}`, `*` | `>=`/`>` and `<=`/`<` on the field (184-200) |
| One-sided | `>=5` | `"field" >= '5'` (201-204) |
| Group | `field:(a OR b)` | the inner query with the default field replaced (205-214) |
| Operators | `AND`/`&&`, `OR`/`||`, `NOT`/`!`, `+`, `-` | `infixNotation`, precedence `+ -`, `NOT`, `AND`, `OR` (271-277); **no operator between clauses means OR** (`Optional(OR)`, 276) |

The reference's own parser has now been run on 69 `q` strings:
[`spec/search-q.md`](../../spec/search-q.md) lists 21 findings (F1 to F21)
with the exact SQL, and `spec/search-q-reference-sql.jsonl` holds the
output. The ones that need a decision before copying (Q-S2): unparsed input
is silently dropped (F1); `a OR NOT b` becomes `a AND NOT b` (F4); `+` and
`-` emit a literal sign (F5); field names are column names as written, so
camelCase API names fail and any column, `raw_data` included, can be
searched (F6); only the first two operands of an `AND` or `OR` chain are
kept, so `a b c` searches for `a` or `b` only (F19); an upper-case wildcard such as `Dis*` matches nearly every
alert (F12); `history` and `q.df=attributes` searches produce invalid SQL
(F16). `%` and `_` act as `LIKE` wildcards (F7), and phrases and wildcards
are unescaped regexes (F10, F11). A caller-supplied regex runs in
PostgreSQL (F13); the request deadline and optional statement limit of
draft #220 (not merged) would bound its cost.

**Implementation sketch.** A hand-written recursive-descent parser in
`alrust-api` (no new crate: a new dependency changes the manifests and the
notice corpus, which waits on R-NOTICE-2), producing an AST, then
parameterised SQL through the store's `QueryBuilder`, never string
concatenation. Field names come from an allow-list of the reference's column
names, quoted by alrust. The privacy refusal moves from a substring check to
the AST: a restricted key in an attribute term, `_exists_`, or a group whose
field resolves to it, and `q.df=attributes`. The customer restriction is
added after `q` with `AND`, as in the reference.

**Tests.** A corpus of `q` strings (every row and quirk above, plus parse
errors) with the reference's selected IDs recorded once against Alerta 9.1.0
and replayed against alrust: this is the "record once, replay in Rust" idea
of the split proposal (`6cabf649`) applied to search. The recording run and
its harness are Codex's; alrust adds the replay test.

## Step 2: faster without changing results

Every reference form is a `ILIKE '%…%'` or `~*` match, which a B-tree cannot
serve, so each `q` scans `alerts`. The reference catalog has only two indexes
on `alerts` (the primary key and `env_res_evt_cust_key`).

| Option | Serves | Cost and gates |
|---|---|---|
| (c) No index | Nothing faster; correct results from step 1 | None. The baseline to measure against |
| (b1) `pg_trgm` GIN expression index on `text` (and on the other searched columns, one index each) | `ILIKE '%x%'`, `~*` phrases, wildcards and many regexes, with identical results | Needs the `pg_trgm` extension: an extension install on an adopted Alerta database is not covered by Y40 (indexes only), so it is an owner decision and an ADR-0005 catalog-rule change. Build on PostgreSQL 18.7 or later (18.7 fixed GIN cleanup and posting-tree bugs; GIN, not GiST). Write cost: see below |
| (b2) Built-in full-text (`to_tsvector('simple', text)`) GIN index | Only an opt-in ranked search; it tokenises, so it cannot reproduce `ILIKE` results | No extension. New API surface (step 3), so an owner decision |
| (a) Search objects in a separate alrust-owned schema (a side table kept in step by the store) | Either of the above, without touching the reference tables | Keeps adoption exact; adds a second write per alert change and a consistency rule. Needs Codex's review of ADR-0005 decision 2 (what the catalog check excludes) |

Any index on `alerts` follows ADR-0005 decision 9 and Y40: a measurement
first, one new migration per index (`0003` onwards), `0002` never changed,
and the catalog check updated by a reviewed change, never by regenerating the
committed file to hide a difference.

**Write cost.** Alerts are updated on every duplicate, and the duplicate
update sets `text` (the reference's `dedup_alert`,
`reference/alerta/alerta/database/backends/postgres/base.py:218-224`). A GIN index on `text` makes each such update maintain
the index and prevents heap-only-tuple updates when `text` changes. The
measurement must include receive-path latency and WAL with and without the
index, not only search latency.

**Never indexed:** `attributes` (the restricted key lives there), `raw_data`
(arbitrary source payloads) and `history` (it carries signed acknowledgement
markers). A search on those stays an unindexed scan, or is refused where 4.2
requires.

## Step 3 (optional, owner decision): ranked search

A ranked result is new API surface: the reference ignores relevance and
alrust fixes newest-first. If the owner wants it: opt-in only (for example a
new parameter), the default order and the `q` result set unchanged, the
privacy and customer rules applied before ranking, and relevance judged on a
fixed synthetic corpus with expected top results before any speed claim.

## Measurement plan (NOT_RUN)

No database runs on this host (no local PostgreSQL; local builds reach 96 to
99 °C), so plan collection is `NOT_RUN`. It runs either as a hosted CI job on
a draft PR or in a Codex lab window:

1. Seed 10,000 and 100,000 synthetic alerts with realistic `text`, `event`,
   `resource`, tags and services, from one generator and one seed.
2. Query set: each grammar row above, combinations with `AND`/`OR`/`NOT`,
   field groups, a no-match query, and the refused privacy forms.
3. Correctness first: alrust's selected IDs equal the reference's (explicit
   `sort-by=lastReceiveTime` on both, as in the read pilot); a mismatch stops
   timing.
4. Then `EXPLAIN (ANALYZE, BUFFERS)` and latency for options (c) and (b1),
   one index at a time, plus receive-path latency and WAL bytes for the
   duplicate update with and without each index.

## Decisions this needs

| ID | Question | Who |
|---|---|---|
| Q-S1 | Refuse a non-empty `q` with 400 until step 1 lands | Codex, then owner |
| Q-S2 | Copy each reference quirk, or diverge with a DECISIONS entry | Codex review, owner for divergences |
| Q-S3 | `pg_trgm` on adopted databases (option b1) or a side schema (a) | Owner (extension, catalog rule) |
| Q-S4 | Ranked search as new API surface (step 3) | Owner |

Not established: any speed, any plan, any relevance result. All 19 runtime
profiles stay `NOT_RUN`.
