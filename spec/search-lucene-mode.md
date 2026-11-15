# `q.mode=lucene`: an opt-in search that keeps Alerta's documented syntax

Status: **proposal with code**, 15 November 2026. Owner decision, the same
day: build an opt-in improved search mode, proposal and code together, with
the default `q=` unchanged. Codex reviews both after the solo window.

## Why a mode, and what it is anchored to

The default `q=` reproduces the reference's parser byte for byte (#245,
`spec/search-q.md`, SF-QUERY). That parser has defects that users meet in a
search box: `-disk` is a database error, `Dis*` matches almost every alert,
`100%` matches every `100`, and a trailing `)` is silently dropped. They
stay in the default, because the web UI, Zabalack and Voalack depend on
the reference's results.

Alerta documents `q` as the Lucene query syntax. The opt-in mode follows
that documentation and fixes only what the reference gets wrong against
it. Every rule below is one of two kinds:
- **(L) Lucene:** the Lucene classic query syntax (or Elasticsearch's
  `query_string`, which extends it and which Alerta's `_exists_` and
  one-sided ranges come from) says so. A reviewer can check it against
  that public syntax.
- **(A) alrust:** alrust's own choice where Lucene is silent or where
  alrust deliberately differs. These are the owner's to rule on.

## Surface

- `q.mode` and `q.op` are read only when `q` is non-empty, like `q.df`:
  without a search they are ignored.
- `q.mode`: the first occurrence. Absent, or `alerta`, is the default mode
  (#245, unchanged). `lucene` is this mode. Any other value: 400 `Query
  mode '<value>' not supported.`
- `q.op`: the first occurrence, used only with `q.mode=lucene`: `AND` (the
  default) or `OR`. Any other value: 400 `Default operator '<value>' not
  supported.` In the default mode `q.op` is ignored, as `q.df` is without
  `q`.
- The same endpoints as `q` (every alert read that goes through
  `parse_search`: `/alerts`, `/alerts/count`, and the grouped reads and
  top-N lists when #236 and #238 merge).
- The order of checks stays SF-QUERY item 7's, with the mode checks first:
  the hook 4.2 guard (unchanged, before anything), then `q.mode`, then
  `q.op`, then syntax, then the restricted-key check below, then `q.df`,
  then the search fields, then the other parameters. The customer filter
  applies as before.
- **Restricted keys, after parsing.** The hook 4.2 guard is a substring
  check on the raw text, and escapes (M6) can hide a name from it
  (`attributes.zabbix_ack_sync_inten\t:x` is the restricted key once
  unescaped). So after parsing, in both modes, every attribute key
  (`attributes.<key>`, `_.<key>`, `_exists_:<key>`, a field group's
  attribute field) and the resolved `q.df` are checked with
  `is_restricted_attribute`, with the guard's content-free 400. In the
  default mode the raw guard already covers every case (its words have no
  escapes), so nothing changes there.

## Rules

| # | Rule | Kind | Reference defect |
|---|---|---|---|
| M1 | Implicit operator: two clauses with no operator between them are joined by `q.op`, `AND` by default. With `q.op=OR` the result is Lucene's and Alerta's | (A) — Lucene's own default is OR | F2 (not a defect against Lucene) |
| M2 | Precedence: `NOT`/`!` binds tighter than `AND`/`&&`, which binds tighter than `OR`/`\|\|`; the implicit operator has the precedence of the operator it stands for. Chains keep every operand | (L) for the operators; (A) for strict precedence (Lucene's classic parser has none) | F3, F19 |
| M3 | `+x` requires `x` and `-x` prohibits it (`NOT x`), also as the first clause. With `q.op=AND` this is Lucene's MUST and MUST_NOT. With `q.op=OR`, `+` has no further effect: `full +disk` is `full OR disk` (Lucene would make `full` optional, so `disk`). A query of only prohibited clauses matches everything else, as Elasticsearch resolves it (Lucene's classic parser matches nothing) | (L) with `AND`; (A) with `OR`; Elasticsearch for a pure negative | F5 (a database error) |
| M4 | `AND`, `OR` and `NOT` (upper case only) are operators, never terms; a missing operand is a syntax error. Lower-case `and` is a term | (L) | F2, F21 (`disk AND` searches `AND`) |
| M5 | The whole input must parse; anything left over is 400 `Failed to parse query string.` | (L) | F1 (dropped silently) |
| M6 | `\` escapes the next character, which is then literal (`web\-01`, `a\:b`), also in a field name; inside `"…"`, `\"` is a quote and `\\` a backslash. A term may hold any non-ASCII letter (`résumé`) and dots (`10.0.0.1`); `-` and `+` inside a term are text | (L) | F1 (`web\-01` is `web`; `résumé` is `r`) |
| M7 | A term is matched literally, case-insensitively, as a substring: `%`, `_` and `\` in it are literal (`ILIKE … ESCAPE '\'`) | (L) for literal; (A) for substring (Lucene matches whole tokens; Alerta users rely on substrings such as host names) | F7 |
| M8 | A phrase `"…"` is the literal words, case-insensitive, bounded by word boundaries; regular-expression characters in it are literal | (L) | F10 |
| M9 | A term with `*` or `?` is a wildcard over one word of the field: `*` is any run of non-space characters, `?` is exactly one; any letters, any case; it may start with a wildcard | (L); leading wildcards as Elasticsearch allows them | F11 (`?` is zero or one), F12 (`Dis*` matches nearly everything) |
| M10 | `/…/` is a case-insensitive regular expression, unanchored, as in the default mode | (A) — Lucene anchors regexes to the whole term | F13 (kept) |
| M11 | Default fields: without `field:` (and without `q.df`), a clause matches if any of `text`, `resource`, `event`, `service`, `tags`, `group` and `value` matches. Not `environment` or `origin`: with substring matching, short terms (`on`, `prod`, `fix`) would match nearly every alert, and both have exact filter parameters. Never `attributes`, `raw_data` or `history`. `q.df` names one field that replaces the set. A term is a substring (`isk` finds `disk`), while a wildcard matches from the start of a word (`isk*` does not) | (A) | default field `text` only |
| M12 | Field names: the API spelling (the reference's own `VALID_PARAMS`, `postgres/utils.py:108-137`: `createTime`, `duplicateCount`, `lastReceiveTime`, `rawData`, `tag`, …) or the column name; `type` is the `type` column (the reference maps it to a non-existent `event_type`). Any other field: 400 `Search field '<field>' not supported.` | (A) | F6 |
| M13 | `history` and a bare `attributes` are not searchable: 400 `Search field '<field>' not supported.`. `attributes.<key>`, `_.<key>` and `_exists_:<key>` keep their meaning; `raw_data` is searched only when named | (A) | F16 (database errors) |
| M14 | Array fields (`service`, `tags`, `correlate`): a term matches an element exactly, case-insensitively; a wildcard or regex matches a whole element | (A) | F8 (case-sensitive) |
| M15 | Ranges and one-sided ranges as in the default mode, and a bound may be quoted, so a timestamp with `:` can be written (`lastReceiveTime:>"2026-11-01T00:30:00"`). Bounds compare with the column's type. On an integer, boolean or timestamp column a plain term is an exact match (`duplicateCount:3`). A value the type cannot hold (an integer; `true` or `false`; a timestamp `YYYY-MM-DD`, optionally with `THH:MM`, `:SS`, a fraction and a `Z`), or a wildcard or regex on such a column: 400 `Search value '<value>' not supported for '<field>'.` | (L) for quoted bounds; one-sided ranges are Elasticsearch's; (A) for the value check | quoted bounds are a parse error; a bad value is a database error |
| M16 | `~` (fuzzy, proximity) and `^` (boost) right after a term or a phrase are Lucene operators alrust does not implement: 400 `Query operator '<op>' not supported.` Inside quotes or `/…/` they are text | (L) syntax; (A) refusal | F1 (dropped silently) |
| M17 | Results keep the newest-first order; no relevance ranking. A ranked order is a later, separate step (`sort-by=relevance`, Q-S4) | (A) | — |

As regular expressions: a phrase is `(^|[^[:alnum:]_])<literal>([^[:alnum:]_]|$)`; a
wildcard is the same start, `*` as `[^[:space:]]*`, `?` as `[^[:space:]]`, and
the same end unless it ends with `*`; on an array element both are anchored
to the whole element (`^…$`).

Unchanged from the default mode: the hook 4.2 refusals (the guard runs on
the raw text first), every value bound as a parameter, the 31-level
nesting cap, and the SF-QUERY field check for `attributes.<key>` paths.

## Fixtures

Store `QL`: SF-QUERY's `QF` (six alerts `q1`…`q6`, text `disk`, `full`,
`server`, `disk full`, `disk full server`, `other`; resources `res-alpha`
… `res-foxtrot`; service `['svc']`; origin `fixture`; `T0` =
2026-11-01 00:00) plus three alerts, all `environment 'Production'`,
`origin 'fixture'`, last receive time `T0` unless stated:

| Id | Resource | Event | Text | Service | Tags | `duplicate_count` | Last receive |
|---|---|---|---|---|---|---|---|
| `q7` | `web-01` | `ev-7` | `Disk usage 100% on /var` | `['Web']` | `['Linux']` | 3 | `T0 + 1 h` |
| `q8` | `db-02` | `ev-8` | `dsk error on a_b` | `['svc']` | `[]` | 0 | `T0` |
| `q9` | `cache-03` | `ev-9` | `100 items in axb` | `['svc']` | `[]` | 0 | `T0` |

## Vectors

`GET /alerts?q=<q>&q.mode=lucene` (with `q.op` where stated), compared
with the same `q` without `q.mode`. Sets are alert ids.

| # | `q` | Default mode (#245) | `q.mode=lucene` | Rule |
|---|---|---|---|---|
| 1 | `disk full` | q1 q2 q4 q5 q7 | q4 q5; with `q.op=OR` q1 q2 q4 q5 q7 | M1 |
| 1b | `full +disk` with `q.op=OR` | — | q1 q2 q4 q5 q7 (Lucene: q1 q4 q5 q7) | M3 |
| 2 | `-disk` | 500 (`- "text" ILIKE …`) | q2 q3 q6 q8 q9 | M3 |
| 3 | `+disk -server` | 500 | q1 q4 q7 | M3 |
| 4 | `Dis*` | all nine (`\y.*\y`) | q1 q4 q5 q7 | M9 |
| 5 | `d?sk` | q1 q4 q5 q7 q8 (`.?`) | q1 q4 q5 q7 | M9 |
| 6 | `100%` | q7 q9 | q7 | M7 |
| 7 | `a_b` | q8 q9 | q8 | M7 |
| 8 | `"disk.full"` | q4 q5 | none | M8 |
| 9 | `disk )` | q1 q4 q5 q7 (`)` dropped) | 400 `Failed to parse query string.` | M5 |
| 10 | `web\-01` | none (`web` in `text`) | q7 | M6, M11 |
| 11 | `res-alpha` | none | q1 | M11 |
| 11b | `on` | q7 q8 | q7 q8 (not every alert: `environment` and `origin` are not default fields) | M11 |
| 11c | `isk`; `isk*` | q1 q4 q5 q7; none (no word starts with `isk`) | q1 q4 q5 q7; none | M7, M9 |
| 12 | `service:SVC` | none | q1 … q6 q8 q9 | M14 |
| 13 | `duplicateCount:>1` | 400 `Search field 'duplicateCount' not supported.` | q7 | M12 |
| 14 | `lastReceiveTime:>"2026-11-01T00:30:00"` | 400 `Failed to parse query string.` | q7 | M15 |
| 15 | `disk~2` | q1 q4 q5 q7 (`~2` dropped) | 400 `Query operator '~' not supported.` | M16 |
| 16 | `history:ack` | 500 (ILIKE on `history[]`) | 400 `Search field 'history' not supported.` | M13 |
| 17 | `disk AND` | q1 q4 q5 q7 | 400 `Failed to parse query string.` | M4 |
| 17b | `disk and full` | q1 q2 q4 q5 q7 (`and` is a term, OR) | none (three terms, AND; no alert contains `and`) | M4 |
| 18 | `/^disk$/` | q1 | q1 | M10 |
| 19 | `_exists_:zabbix_ack_sync_intent` | 400 (hook 4.2) | 400 (hook 4.2) | unchanged |
| 19b | `attributes.zabbix_ack_sync_inten\t:x` | none (`attributes` in `text`) | 400 `Query selector is not permitted on restricted attributes` (the key after unescaping) | restricted keys |
| 20 | `q=disk&q.mode=bogus`; `q=disk&q.mode=lucene&q.op=XOR`; `q=disk&q.op=XOR`; `q.mode=bogus` without `q` | 400 `Query mode 'bogus' not supported.`; 400 `Default operator 'XOR' not supported.`; `q.op` ignored; ignored | — | Surface |

## Performance

The mode is built so that the indexes measured in #247 can serve it: a
term is one escaped `ILIKE` pattern per default field and a phrase one
escaped regex, which `pg_trgm` can use when the pattern has three or more
literal characters. The default-field set (M11) multiplies the predicates
by nine; whether one expression index over the concatenated fields is
better than one index per field is a measurement question for #247's next
run, not settled here.
