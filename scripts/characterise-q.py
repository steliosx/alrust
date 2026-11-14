#!/usr/bin/env python3
"""Print the pinned reference's SQL for a corpus of `q` strings (JSON lines).

Runs Alerta 9.1.0's own QueryParser, read-only, so spec/search-q.md can
state the reference's grammar from its behaviour, not from memory:

    grep -A1 '^pyparsing==' .github/reference-requirements.lock > /path/to/pyparsing.txt
    python3.12 -m venv /path/to/venv          # CPython 3.12, as the lock
    /path/to/venv/bin/pip install --require-hashes --only-binary=:all: --no-deps \
        -r /path/to/pyparsing.txt
    /path/to/venv/bin/python -I -B scripts/characterise-q.py \
        reference/alerta > spec/search-q-reference-sql.jsonl

The script refuses to run unless the interpreter is CPython 3.12, pyparsing
is 3.3.2, the reference checkout is at the pinned commit and its
queryparser.py has the pinned bytes and no local change. The first output
line records that identity.

`-B` keeps Python from writing bytecode into reference/. Only the SQL text
and parameters are recorded; what each statement selects (or the database
error it raises) needs a run against PostgreSQL, which this script does not do.
"""
import hashlib
import json
import os
import platform
import subprocess
import sys

sys.dont_write_bytecode = True
PINNED_COMMIT = 'f4312727783dad64198b62ed778bbca98a5144a3'
MODULE = 'alerta/database/backends/postgres/queryparser.py'
MODULE_SHA256 = 'd9c13317bb36208bbe83bd305bed1199a9e7a586659ca8256479f6158b9d0ce6'
PYPARSING = '3.3.2'


def refuse(why):
    sys.exit(f'characterise-q: {why}')


if platform.python_implementation() != 'CPython' or sys.version_info[:2] != (3, 12):
    refuse(f'needs CPython 3.12, not {platform.python_implementation()} {platform.python_version()}')
ref = os.path.abspath(sys.argv[1])
head = subprocess.run(['git', '-C', ref, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
if head != PINNED_COMMIT:
    refuse(f'reference checkout is at {head or "?"}, not {PINNED_COMMIT}')
dirty = subprocess.run(['git', '-C', ref, 'status', '--porcelain', '--', MODULE],
                       capture_output=True, text=True).stdout.strip()
with open(os.path.join(ref, MODULE), 'rb') as f:
    module_sha = hashlib.sha256(f.read()).hexdigest()
if dirty or module_sha != MODULE_SHA256:
    refuse(f'{MODULE} is changed or not the pinned bytes ({module_sha})')
import pyparsing  # noqa: E402
if pyparsing.__version__ != PYPARSING:
    refuse(f'needs pyparsing {PYPARSING}, not {pyparsing.__version__}')
sys.path.insert(0, os.path.join(ref, os.path.dirname(MODULE)))
from queryparser import QueryParser  # noqa: E402  (the reference's module)

print(json.dumps({'identity': {
    'reference_commit': head, 'module': MODULE, 'module_sha256': module_sha,
    'python': f'{platform.python_implementation()} {platform.python_version()}',
    'pyparsing': pyparsing.__version__}}))

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
