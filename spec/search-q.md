# `q=` in the reference: characterised SQL

Status: **characterisation of the pinned reference, 14 November 2026.**
Source: Alerta 9.1.0 (`reference/alerta` at `f4312727`),
`alerta/database/backends/postgres/queryparser.py` (sha256 prefix
`d9c13317bb36208b`) with pyparsing 3.3.2 from
`.github/reference-requirements.lock`. The reference's own parser was run on
69 `q` strings by `scripts/characterise-q.py`; its output, one JSON line per
string, is `spec/search-q-reference-sql.jsonl`. That file is the reference's
SQL text and parameters only. What each statement selects, and which ones
fail in PostgreSQL, is **not yet observed**: that needs a recording run
against the reference with a database (the record-once corpus of
`docs/performance/search-design.md`, Codex's harness). Lines below marked
*expected* are inferences from the SQL, to be confirmed by that run.

How `q` is used (`utils.py:144-155`): only when `q` is non-empty. The parsed
SQL is the first condition of the `WHERE` clause; customers and the other
filters follow with `AND`. A `ParseException` becomes 400 "Failed to parse
query string."; any other error (a database error) is not caught there.
`q.df` replaces the default field `text`.

## Findings

| # | Finding | Example (`q` → reference SQL) |
|---|---|---|
| F1 | **Unparsed input is silently dropped.** `parseString` runs without `parseAll`, so the parser stops at the first text it cannot continue with and ignores the rest | `a:b:c` → `"a" ILIKE '%b%'`; `web\-01` → `"text" ILIKE '%web%'` |
| F19 | **Only the first two operands of an `AND` or `OR` chain are kept; the rest are silently dropped.** pyparsing groups `a AND b AND c` as one list, and the reference's `BinaryOperation` reads only items 0 to 2. This applies to implicit OR and inside field groups | `a AND b AND c` → `(a AND b)`; `a b c d` → `(a OR b)`; `resource:(a OR b OR c)` → `("resource" ILIKE '%a%' OR "resource" ILIKE '%b%')` |
| F20 | Mixed chains keep two operands per level: `a AND b OR c AND d` keeps all four, because each `AND` pair is one operand of the `OR` | `a AND b OR c AND d` → `((a AND b) OR (c AND d))` |
| F21 | `-`, `+` and `!` inside a word are part of the word; `AND`, `OR` and `NOT` are operators only as whole words. A trailing `OR` is dropped, a trailing `AND` becomes a term | `a-b` → `'%a-b%'`; `ANDROID` → `'%ANDROID%'`; `web01 OR` → `'%web01%'` |
| F2 | **No operator means OR**, and a trailing operator word is a term | `disk full` → `text ILIKE '%disk%' OR text ILIKE '%full%'`; `disk AND` → `… '%disk%' OR … '%AND%'` |
| F3 | `AND` binds tighter than `OR`; parentheses group | `a OR b AND c` → `a OR (b AND c)` |
| F4 | **`a OR NOT b` becomes `a AND NOT b`** | `disk OR NOT full` → `("text" ILIKE '%disk%' AND NOT ("text" ILIKE '%full%'))` |
| F5 | **`+` and `-` emit a literal sign** in the SQL. *Expected:* a database error, not a filter | `+disk` → `+ "text" ILIKE …` |
| F6 | **The field name is the column name, quoted as written.** API names that differ from the column (`rawData`, `duplicateCount`, `createTime`, `lastReceiveTime`, `type`, …) are not columns: *expected* database error. Column names written directly (`raw_data`, `duplicate_count`, `event_type`, …) work, so any column can be searched | `rawData:secret` → `"rawData" ILIKE …`; `nosuchcolumn:x` → `"nosuchcolumn" ILIKE …` |
| F7 | Terms are substring matches with `ILIKE '%term%'`, case-insensitive; **`%` and `_` in a term are not escaped** and act as wildcards | `100%` → `'%100%%'`; `a_b` → `'%a_b%'` |
| F8 | `service`, `tags` and `correlate` are exact, case-sensitive array membership, for a term and for a phrase | `service:"Web App"` → `'Web App'=ANY("service")` |
| F9 | Attributes: `attributes.k:v` and `_.k:v` are a substring match on the key's text value (key bound as a parameter); `_exists_:k` is the `?` operator | `_.region:eu` → `"attributes"::jsonb ->> 'region' ILIKE '%eu%'` |
| F10 | **A phrase is a word-bounded regex, not escaped**: regex characters inside quotes keep their meaning | `"disk full"` → `"text" ~* '\ydisk full\y'` |
| F11 | Wildcards: `*` → `.*`, `?` → `.?` (zero or one, not exactly one), word-bounded regex. **Only lower-case letters and digits** form a wildcard token | `d?sk` → `~* '\yd.?sk\y'` |
| F12 | **An upper-case wildcard splits into a term and a lone `*`**, and the lone `*` is the regex `\y.*\y`, which *expectedly* matches every alert whose text has a word character | `Dis*` → `("text" ILIKE '%Dis%' OR "text" ~* '\y.*\y')` |
| F13 | `/…/` is a caller-supplied case-insensitive regex on the field | `resource:/^web[0-9]+$/` → `"resource" ~* '^web[0-9]+$'` |
| F14 | Ranges compare the column with **text** parameters; `*` is open. Combined with F6, ranges on camelCase fields fail; `timeout:<60` works | `duplicateCount:[2 TO 5]` → `("duplicateCount" >= '2' AND "duplicateCount" <= '5')` |
| F15 | `field:(a OR b)` applies the inner query to the field | `resource:(web01 OR web02)` → `("resource" ILIKE '%web01%' OR "resource" ILIKE '%web02%')` |
| F16 | `history:x` and `q.df=attributes` produce `ILIKE` on a composite array or on `jsonb`: *expected* database error | `x` with `q.df=attributes` → `"attributes" ILIKE '%x%'` |
| F17 | Parse errors (400): an unclosed parenthesis or quote, a leading operator | `(disk`, `"unterminated` |
| F18 | Restricted-attribute forms parse normally in the reference; Zabalack's query guard refuses them on Alerta, and alrust refuses them (`spec/integration-hooks.md` 4.2) | `_exists_:zabbix_ack_sync_intent` |

## What alrust does with these

Decided by DECISIONS Y3 and `spec/security-fixes.md` SF-QUERY (which already
describes F19 as its reference item 6), not by this page:
- F19 and F20: chains are folded to the left, so every operand is kept;
  with two operands the result is exactly the reference's.
- F6 and F16 (the `q.df=attributes` case): an unknown search field or
  default field is a 400 naming it, instead of a database error. A listed
  column whose type does not support the operator (for example
  `history:x`) keeps the reference's outcome.
- F1, F4, F5, F7 and F10 to F13: unchanged from the reference, so the
  generated patterns must match it byte for byte.
- F18: alrust refuses restricted-attribute forms (spec 4.2).

The DB outcomes marked *expected* above (F5, F12, F16) are inferences from
the SQL text; they become facts only when recorded against a database.
