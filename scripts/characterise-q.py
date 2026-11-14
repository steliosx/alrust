#!/usr/bin/env python3
"""Print the pinned reference's SQL for a corpus of `q` strings (JSON lines).

Runs Alerta 9.1.0's own QueryParser, read-only, so spec/search-q.md can
state the reference's grammar from its behaviour, not from memory:

    grep -A1 '^pyparsing==' .github/reference-requirements.lock > /path/to/pyparsing.txt
    python3 -m venv /path/to/venv
    /path/to/venv/bin/pip install --require-hashes --only-binary=:all: --no-deps \
        -r /path/to/pyparsing.txt
    /path/to/venv/bin/python -I -B scripts/characterise-q.py \
        reference/alerta/alerta/database/backends/postgres > spec/search-q-reference-sql.jsonl

`-B` keeps Python from writing bytecode into reference/. Only the SQL text
and parameters are recorded; what each statement selects (or the database
error it raises) needs a run against PostgreSQL, which this script does not do.
"""
import json
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from queryparser import QueryParser  # noqa: E402  (the reference's module)

CORPUS = [
    ("disk", None), ("disk", "resource"), ("resource:web01", None),
    ("event:HttpError", None), ("Disk", None),
    ("service:Web", None), ("tags:linux", None), ("correlate:NodeDown", None),
    ("attributes.region:eu", None), ("_.region:eu", None), ("_exists_:region", None),
    ('"disk full"', None), ('resource:"web 01"', None), ('service:"Web App"', None),
    ('_.region:"eu west"', None),
    ("dis*", None), ("d?sk", None), ("resource:web*", None), ("Dis*", None),
    ("/^web[0-9]+$/", None), ("resource:/^web[0-9]+$/", None),
    ("duplicateCount:[2 TO 5]", None), ("duplicateCount:{2 TO 5}", None),
    ("duplicateCount:[2 TO *]", None), ("duplicateCount:>=2", None), ("timeout:<60", None),
    ("resource:(web01 OR web02)", None), ("_.region:(eu OR us)", None),
    ("disk AND full", None), ("disk && full", None), ("disk OR full", None),
    ("disk || full", None), ("disk full", None), ("NOT disk", None), ("!disk", None),
    ("disk OR NOT full", None), ("disk AND NOT full", None), ("+disk", None), ("-disk", None),
    ("a AND b OR c", None), ("a OR b AND c", None), ("(a OR b) AND c", None),
    ("rawData:secret", None), ("history:ack", None), ("nosuchcolumn:x", None),
    ("100%", None), ("a_b", None), ("web\\-01", None),
    ("attributes.zabbix_ack_sync_intent:x", None), ("_exists_:zabbix_ack_sync_intent", None),
    ("x", "attributes"),
    ("a AND b AND c", None), ("a OR b OR c", None), ("a b c", None), ("a b c d", None),
    ("a && b && c", None), ("NOT a AND b", None), ("a AND b OR c AND d", None),
    ("resource:(a OR b OR c)", None), ("a-b", None), ("disk!", None), ("ANDROID", None),
    ("web01 OR", None), ("-a -b", None),
    ("", None), ("disk AND", None), ("(disk", None), ('"unterminated', None), ("a:b:c", None),
]

qp = QueryParser()
for q, df in CORPUS:
    try:
        sql, params = qp.parse(q, df)
        out = {"q": q, "q.df": df, "sql": sql, "params": params}
    except Exception as e:  # the reference maps ParseException to 400
        out = {"q": q, "q.df": df, "error": type(e).__name__, "message": str(e)[:160]}
    print(json.dumps(out, ensure_ascii=False))
