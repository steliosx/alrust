#!/usr/bin/env python3
"""End-to-end smoke test for a running alrust alpha server.

    python3 scripts/alpha-smoke.py http://localhost:8080 <admin-api-key> [--disposable-target]

Standard library only. Each step prints PASS, FAIL or NOT_RUN; the exit
status is the number of failures. Expected values come from
spec/lifecycle.md (rule names in brackets).

Every resource name carries a unique run id, so the default run is safe
against a database that already holds data. The housekeeping checks are the
exception: POST /management/housekeeping is global and can expire or delete
alerts that are not this run's. They run only with --disposable-target,
which asserts the target and its database are owned by this run and may be
discarded. Without it they are reported NOT_RUN, never PASS.
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ARGS = [a for a in sys.argv[1:] if not a.startswith('--')]
FLAGS = {a for a in sys.argv[1:] if a.startswith('--')}
unknown = FLAGS - {'--disposable-target'}
if unknown:
    sys.exit(f'unknown option(s): {sorted(unknown)}')
BASE = ARGS[0].rstrip('/') if ARGS else 'http://localhost:8080'
KEY = ARGS[1] if len(ARGS) > 1 else ''
DISPOSABLE = '--disposable-target' in FLAGS
RUN = uuid.uuid4().hex[:8]
failures = 0
not_run = 0


def call(method, path, body=None, key=KEY, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if key:
        req.add_header('Authorization', f'Key {key}')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw.decode(errors='replace')


def call_raw(method, path, data, key=KEY):
    """Like call, with a raw request body sent as application/json."""
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if key:
        req.add_header('Authorization', f'Key {key}')
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')


def check(name, cond, detail=''):
    global failures
    if cond:
        print(f'PASS {name}')
    else:
        failures += 1
        print(f'FAIL {name} {detail}')


def ts(offset):
    t = time.time() + offset
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + f'.{int((t % 1) * 1000):03d}Z'


def alert(**kw):
    base = {
        'resource': f'web01-{RUN}',
        'event': 'HttpError',
        'environment': 'Production',
        'service': ['Web'],
        'severity': 'major',
        'text': 'smoke test',
        'tags': ['smoke'],
    }
    base.update(kw)
    return base


# --- health and auth
code, body = call('GET', '/management/gtg', key='')
check('gtg is OK without a key', code == 200 and body == 'OK', (code, body))
code, body = call('GET', '/alerts', key='')
check('missing key gives 401', code == 401, (code, body))
code, body = call('GET', '/alerts', key='not-a-real-key')
check('bad key gives 401', code == 401, (code, body))

# --- receive: create [R4]
code, body = call('POST', '/alert', alert(createTime=ts(-40), value='500'))
check('create returns 201', code == 201, (code, body))
a = body.get('alert', {}) if isinstance(body, dict) else {}
aid = a.get('id', '')
check('new alert is open with indeterminate previousSeverity [R4.1]',
      a.get('status') == 'open' and a.get('previousSeverity') == 'indeterminate'
      and a.get('trendIndication') == 'moreSevere', a)
check('history has one "new" entry [R4.2]',
      [h.get('type') for h in a.get('history', [])] == ['new'], a.get('history'))

# --- duplicate [R5]
code, body = call('POST', '/alert', alert(createTime=ts(-30), value='501'))
d = body.get('alert', {})
check('duplicate keeps the id and counts [R5.2]',
      code == 201 and d.get('id') == aid and d.get('duplicateCount') == 1 and d.get('repeat') is True, d)
check('value change adds a "value" history entry [R5.4]',
      [h.get('type') for h in d.get('history', [])] == ['new', 'value'], d.get('history'))

# --- correlate to ok: auto-close [R6, CLS-*]
code, body = call('POST', '/alert', alert(createTime=ts(-20), severity='ok'))
c = body.get('alert', {})
check('ok correlates and closes, severity stored as ok [Zabbix OK step 2]',
      c.get('id') == aid and c.get('status') == 'closed' and c.get('severity') == 'ok'
      and c.get('previousSeverity') == 'major' and c.get('duplicateCount') == 0, c)

# --- correlate back to major: reopen [CLS-3]
code, body = call('POST', '/alert', alert(createTime=ts(-10), severity='major'))
r = body.get('alert', {})
check('major reopens the closed alert [CLS-3]', r.get('status') == 'open' and r.get('severity') == 'major', r)

# --- get, by id and by prefix
code, body = call('GET', f'/alert/{aid}')
check('GET /alert/<id> returns it', code == 200 and body.get('alert', {}).get('id') == aid, (code, body))
code, body = call('GET', f'/alert/{aid[:8]}')
check('GET by id prefix works [C17]', code == 200 and body.get('alert', {}).get('id') == aid, (code, body))
hist = body.get('alert', {}).get('history', [])
check('API history is sorted ascending by updateTime [R7.3b]',
      [h['updateTime'] for h in hist] == sorted(h['updateTime'] for h in hist), hist)

# --- actions [A2-A5]
code, body = call('PUT', f'/alert/{aid}/action', {'action': 'ack', 'text': 'looking'})
check('ack action returns 200 {"status":"ok"} [A5.4]', code == 200 and body == {'status': 'ok'}, (code, body))
code, body = call('PUT', f'/alert/{aid}/action', {'action': 'ack'})
check('second ack is 409 [A3]', code == 409, (code, body))
code, body = call('GET', f'/alert/{aid}')
check('alert is now ack', body.get('alert', {}).get('status') == 'ack', body)
code, body = call('PUT', f'/alert/{aid}/action', {'action': 'close'})
code, body = call('GET', f'/alert/{aid}')
x = body.get('alert', {})
check('close stores normal/closed and keeps previousSeverity [ACK-3, A3.3]',
      x.get('status') == 'closed' and x.get('severity') == 'normal' and x.get('previousSeverity') == 'major', x)
code, body = call('PUT', f'/alert/{aid}/action', {'action': 'open'})
code, body = call('GET', f'/alert/{aid}')
x = body.get('alert', {})
check('open restores the stored previousSeverity [CLS-1]',
      x.get('status') == 'open' and x.get('severity') == 'major', x)
code, body = call('PUT', f'/alert/{aid}/action', {})
check('missing action is 400 [A2.1]', code == 400, (code, body))

# --- status [A1]
code, body = call('PUT', f'/alert/{aid}/status', {'status': 'assign', 'text': 'mine'})
check('PUT status returns 200', code == 200 and body == {'status': 'ok'}, (code, body))
code, body = call('GET', f'/alert/{aid}')
x = body.get('alert', {})
check('status stored as sent; NULL timeout reads as 86400 [A1.2, A1.3a]',
      x.get('status') == 'assign' and x.get('timeout') == 86400, x)
code, body = call('PUT', f'/alert/{aid}/status', {})
check('missing status is 400 [A1.1]', code == 400, (code, body))

# --- policy and validation [R0, R1.2]
code, body = call('POST', '/alert', alert(environment='Staging'))
check('disallowed environment is 403 [R1.2]', code == 403, (code, body))
code, body = call('POST', '/alert', alert(service=[]))
check('missing service is 403 [R1.2]', code == 403, (code, body))
code, body = call('POST', '/alert', {'event': 'x', 'environment': 'Production', 'service': ['W']})
check('missing resource is 400 [R0.1]', code == 400, (code, body))
code, body = call('POST', '/alert', alert(timeout=-1))
check('negative timeout is 400 [R0.4]', code == 400, (code, body))
code, body = call('POST', '/alert', alert(resource=f'big-{RUN}', timeout=2147483648))
check('timeout above 2147483647 is 400 [ADR-0005 decision 6]', code == 400, (code, body))
code, body = call('POST', '/heartbeat', {'origin': f'hbbig/{RUN}', 'timeout': 9223372036854775807})
check('heartbeat timeout above 2147483647 is 400 [ADR-0005 decision 6]', code == 400, (code, body))
code, body = call('POST', '/alert', alert(resource=f'bad-{RUN}', severity='bogus'))
check('unknown severity is 500 [R1.1]', code == 500, (code, body))

# --- query and counts
code, body = call('GET', f'/alerts?resource=web01-{RUN}')
check('GET /alerts finds it', code == 200 and body.get('total') == 1 and len(body.get('alerts', [])) == 1, body)
code, body = call('GET', f'/alerts/count?resource=web01-{RUN}')
check('GET /alerts/count', code == 200 and body.get('total') == 1, body)
code, body = call('GET', f'/alerts?resource=nothing-{RUN}')
check('empty query says not found', code == 200 and body.get('total') == 0 and body.get('message') == 'not found', body)

# --- field!=value exclusions [postgres/utils.py filter_query; A3B-EVENT-NEGATION-1]
NR = f'neg-{RUN}'
for ev, sev in (('NegA', 'major'), ('NegB', 'minor'), ('NegC', 'major')):
    call('POST', '/alert', alert(resource=NR, event=ev, severity=sev))
def total(path):
    c, b = call('GET', path)
    return b.get('total') if c == 200 else ('HTTP', c)
check('baseline: three alerts on the resource', total(f'/alerts/count?resource={NR}') == 3, total(f'/alerts/count?resource={NR}'))
check('event!= excludes one value (count)', total(f'/alerts/count?resource={NR}&event%21=NegA') == 2, total(f'/alerts/count?resource={NR}&event%21=NegA'))
check('event!= excludes one value (list)', total(f'/alerts?resource={NR}&event%21=NegA') == 2, total(f'/alerts?resource={NR}&event%21=NegA'))
check('repeated event!= excludes every value', total(f'/alerts/count?resource={NR}&event%21=NegA&event%21=NegB') == 1,
      total(f'/alerts/count?resource={NR}&event%21=NegA&event%21=NegB'))
check('event!= with a positive severity filter', total(f'/alerts/count?resource={NR}&severity=major&event%21=NegA') == 1,
      total(f'/alerts/count?resource={NR}&severity=major&event%21=NegA'))
check('severity!= excludes', total(f'/alerts/count?resource={NR}&severity%21=major') == 1, total(f'/alerts/count?resource={NR}&severity%21=major'))
check('resource!= excludes the whole resource', total(f'/alerts/count?resource={NR}&resource%21={NR}') == 0,
      total(f'/alerts/count?resource={NR}&resource%21={NR}'))
c, b = call('GET', f'/alerts?resource={NR}')
for a_ in b.get('alerts', []):
    call('DELETE', f"/alert/{a_.get('id')}")

# --- management manifest and status [management/views.py]
code, body = call('GET', '/management/manifest')
check('GET /management/manifest has release, build, date, revision',
      code == 200 and isinstance(body, dict) and sorted(body) == ['build', 'date', 'release', 'revision'], (code, body))
code, body = call('GET', '/management/status')
names = [m.get('name') for m in body.get('metrics', [])] if isinstance(body, dict) else []
check('GET /management/status reports the alert gauge and the switches',
      code == 200 and body.get('application') == 'alrust' and names == ['total', 'auto-refresh-allow', 'sender-api-allow'], (code, body))

# --- heartbeats [HB]
origin = f'smoke/{RUN}'
code, body = call('POST', '/heartbeat', {'origin': origin, 'timeout': 60, 'tags': ['smoke']})
hid = body.get('id', '') if isinstance(body, dict) else ''
check('POST /heartbeat returns 201 [HB0.2]', code == 201 and body.get('heartbeat', {}).get('status') == 'ok', (code, body))
code, body = call('POST', '/heartbeat', {'origin': origin, 'timeout': 60})
check('repeat heartbeat keeps the id [HB0.1]', body.get('id') == hid, body)
code, body = call('POST', '/alert', alert(event='Heartbeat', origin=f'hb-alert/{RUN}'))
check('Heartbeat event becomes a heartbeat with 202 [HB3.1]', code == 202, (code, body))
code, body = call('GET', '/heartbeats')
check('GET /heartbeats lists it', code == 200 and any(h.get('id') == hid for h in body.get('heartbeats', [])), body)
code, body = call('DELETE', f'/heartbeat/{hid}')
check('DELETE /heartbeat/<id>', code == 200, (code, body))

# --- keys [SF-KEYS S1-S3, views/keys.py:38-41]
def mint(scopes, customer=None, key=KEY):
    body = {'scopes': scopes, 'text': f'smoke {RUN}'}
    if customer is not None:
        body['customer'] = customer
    code, body = call('POST', '/key', body, key=key)
    if code != 201:
        return code, None, None
    return code, body.get('key'), body.get('data', {}).get('id')

code, ro, ro_id = mint(['read:alerts'])
check('POST /key creates a key, secret shown at creation [S1]', code == 201 and ro and len(ro) == 40, code)
code, body = call('GET', '/keys')
listed = [k for k in body.get('keys', []) if k.get('id') == ro_id]
check('GET /keys omits secrets and uses id hrefs [S1, S2]',
      listed and all('key' not in k for k in body.get('keys', [])) and listed[0]['href'].endswith('/key/' + ro_id), listed)
code, body = call('GET', f'/alert/{aid}', key=ro)
check('read-only key can read', code == 200, (code, body))
code, body = call('POST', '/alert', alert(), key=ro)
check('read-only key cannot write (403)', code == 403, (code, body))
code, km, km_id = mint(['admin:keys', 'write:keys', 'read:keys'])
code, body = call('POST', '/key', {'scopes': ['admin']}, key=km)
check('admin:keys holder cannot mint admin (403) [A2-API-R1]', code == 403, (code, body))
code, body = call('POST', '/key', {}, key=km)
check('default scopes are held to the ceiling too (403) [A2-API-R1]', code == 403, (code, body))
code, body = call('DELETE', f'/key/{ro}')
check('DELETE /key/<secret> is 404 [S3, A2-API-R2]', code == 404, (code, body))
code, body = call('DELETE', f'/key/{ro_id}')
check('DELETE /key/<id> works [S3]', code == 200, (code, body))
code, body = call('GET', f'/alert/{aid}', key=ro)
check('deleted key no longer authenticates', code == 401, (code, body))

# --- a caller-supplied key value and id [models/key.py:50-56]
chosen, chosen_id = f'alpha-smoke-chosen-{RUN}', f'alpha-smoke-id-{RUN}'
code, body = call('POST', '/key', {'key': chosen, 'id': chosen_id, 'scopes': ['read:alerts'], 'text': f'smoke {RUN}'})
check('POST /key keeps a supplied key and id', code == 201 and body.get('key') == chosen
      and body.get('data', {}).get('id') == chosen_id, (code, body.get('data', {}).get('id')))
code, body = call('GET', f'/alert/{aid}', key=chosen)
check('the supplied key authenticates', code == 200, (code, body))
code, body = call('POST', '/key', {'key': chosen, 'scopes': ['read:alerts']})
check('a duplicate supplied key is refused (500, as the reference)', code == 500, (code, body))
code, body = call('POST', '/key', {'key': 5, 'scopes': ['read:alerts']})
check('a truthy non-string supplied key is 400', code == 400, (code, body))
call('DELETE', f'/key/{chosen_id}')
# Python-falsy values generate, as the reference's `or` does [KEYSUP-AUTH-1]
for falsy in (False, 0, 0.0, '', [], {}, None):
    code, body = call('POST', '/key', {'key': falsy, 'id': falsy, 'scopes': ['read:alerts']})
    made, made_id = body.get('key'), body.get('data', {}).get('id')
    check(f'a falsy supplied key/id ({json.dumps(falsy)}) is generated',
          code == 201 and isinstance(made, str) and len(made) == 40 and isinstance(made_id, str)
          and len(made_id) == 36, (code, made_id))
    if made_id:
        call('DELETE', f'/key/{made_id}')


def via_href(href):
    # The href's own path, still percent-encoded; its host is ALRUST_BASE_URL.
    path = urllib.parse.urlsplit(href).path if href else ''
    return path if path.startswith('/') and '?' not in href and '#' not in href else None


# hrefs carry an id as one path segment [KEYSUP-R1]
for odd in ('a/b', 'a?b', 'a#b', 'a%2Fb', 'é ü'):
    kid = f'{odd}-{RUN}'
    code, body = call('POST', '/key', {'id': kid, 'scopes': ['read:alerts']})
    href = via_href(body.get('data', {}).get('href', ''))
    code2, _ = call('DELETE', href) if href else (None, None)
    code3, body3 = call('GET', '/keys')
    gone = all(k.get('id') != kid for k in body3.get('keys', []))
    check(f'key id {odd!r}: href deletes exactly that key', code == 201 and code2 == 200 and gone,
          (code, href, code2, gone))
odd_alert = f'x/y?z#{RUN}'
code, body = call('POST', '/alert', alert(id=odd_alert, resource=f'odd-{RUN}'))
href = via_href(body.get('alert', {}).get('href', ''))
code2, body2 = call('GET', href) if href else (None, {})
check('alert with an odd id: href reads that alert', code == 201 and code2 == 200
      and body2.get('alert', {}).get('id') == odd_alert, (code, href, code2))
if href:
    call('DELETE', href)
odd_hb = f'h/b?#{RUN}'
code, body = call('POST', '/heartbeat', {'id': odd_hb, 'origin': f'odd-{RUN}', 'timeout': 60})
href = via_href(body.get('heartbeat', {}).get('href', ''))
code2, body2 = call('GET', href) if href else (None, {})
check('heartbeat with an odd id: href reads that heartbeat', code == 201 and code2 == 200
      and body2.get('heartbeat', {}).get('id') == odd_hb, (code, href, code2))
if href:
    call('DELETE', href)

# --- key listing is per user unless admin [views/keys.py:84-91, A2V2-API-R1]
ALICE, BOB = f'alice-{RUN}', f'bob-{RUN}'
code, body = call('POST', '/key', {'user': ALICE, 'scopes': ['read:keys'], 'text': 'alice'})
alice, alice_id = body.get('key'), body.get('data', {}).get('id')
code, body = call('POST', '/key', {'user': BOB, 'scopes': ['read:keys'], 'text': 'bob'})
bob_id = body.get('data', {}).get('id')
code, body = call('GET', '/keys', key=alice)
ids = {k.get('id') for k in body.get('keys', [])}
check('read:keys user lists only their own keys', code == 200 and alice_id in ids and bob_id not in ids
      and all(k.get('user') == ALICE for k in body.get('keys', [])), (code, ids))
code, body = call('GET', '/keys')
ids = {k.get('id') for k in body.get('keys', [])}
check('admin lists every key', code == 200 and {alice_id, bob_id} <= ids, (code, len(ids)))
for kid in (alice_id, bob_id):
    call('DELETE', f'/key/{kid}')

# --- customer assignment [customer-mapping C5-C10, A2-API-R3]
X, Y = f'cx-{RUN}', f'cy-{RUN}'
_, wx, _ = mint(['write:alerts', 'read:alerts', 'write:heartbeats'], customer=X)
_, ax, _ = mint(['admin:alerts', 'write:alerts', 'read:alerts'], customer=X)
_, wn, _ = mint(['write:alerts', 'read:alerts'])
code, body = call('POST', '/alert', alert(resource=f'cust-{RUN}'), key=wx)
check('omitted customer inherits the key customer [C7]', code == 201 and body.get('alert', {}).get('customer') == X, (code, body))
code, body = call('POST', '/alert', alert(resource=f'cust-{RUN}', customer=Y), key=wx)
check('conflicting customer from a non-admin key is 400 [C7]', code == 400, (code, body))
code, body = call('POST', '/alert', alert(resource=f'cust2-{RUN}', customer=Y), key=ax)
check('admin:alerts key may set another customer [C8]', code == 201 and body.get('alert', {}).get('customer') == Y, (code, body))
code, body = call('POST', '/alert', alert(resource=f'cust3-{RUN}', customer=Y), key=wn)
check('unscoped non-admin key cannot name a customer [C9]', code == 400, (code, body))
code, body = call('POST', '/alert', alert(resource=f'cust3-{RUN}', customer=''), key=wn)
check('empty customer is 400 [C5]', code == 400, (code, body))
code, body = call('POST', '/heartbeat', {'origin': f'cust/{RUN}', 'customer': Y}, key=wx)
check('heartbeat: conflicting customer is 400 [C11]', code == 400, (code, body))
code, body = call('POST', '/heartbeat', {'origin': f'cust/{RUN}'}, key=wx)
check('heartbeat inherits the key customer', code == 201 and body.get('heartbeat', {}).get('customer') == X, (code, body))
code, body = call('GET', f'/alerts?resource=cust2-{RUN}', key=wx)
check('customer-X key does not see customer-Y alerts [C16]', code == 200 and body.get('total') == 0, body)

# --- tags and attributes [views/alerts.py:194-272]
code, body = call('POST', '/alert', alert(resource=f'tag-{RUN}', tags=['t1'], attributes={'keep': 1, 'drop': 'x'}))
tid = body.get('id', '')
before = call('GET', f'/alert/{tid}')[1].get('alert', {})
code, body = call('PUT', f'/alert/{tid}/tag', {'tags': ['t2', 't1', 't2']})
check('PUT /alert/<id>/tag', code == 200 and body == {'status': 'ok'}, (code, body))
after = call('GET', f'/alert/{tid}')[1].get('alert', {})
check('tag adds without duplicates', sorted(after.get('tags', [])) == ['t1', 't2'], after.get('tags'))
check('tag adds no history and keeps updateTime',
      len(after.get('history', [])) == len(before.get('history', [])) and after.get('updateTime') == before.get('updateTime'),
      (len(after.get('history', [])), len(before.get('history', []))))
code, body = call('PUT', f'/alert/{tid[:8]}/untag', {'tags': ['t1', 'absent']})
after = call('GET', f'/alert/{tid}')[1].get('alert', {})
check('untag by id prefix removes only the named tags', code == 200 and after.get('tags') == ['t2'], (code, after.get('tags')))
call('PUT', f'/alert/{tid}/untag', {'tags': ['t2']})
after = call('GET', f'/alert/{tid}')[1].get('alert', {})
check('untagging every tag leaves an empty list', after.get('tags') == [], after.get('tags'))
code, body = call('PUT', f'/alert/{tid}/attributes', {'attributes': {'new': 'v', 'drop': None}})
after = call('GET', f'/alert/{tid}')[1].get('alert', {})
check('attributes merge, and null removes a key', code == 200 and after.get('attributes', {}).get('new') == 'v'
      and after.get('attributes', {}).get('keep') == 1 and 'drop' not in after.get('attributes', {}), (code, after.get('attributes')))
for path, bad in (('tag', {}), ('tag', {'tags': []}), ('tag', {'tags': 'x'}), ('tag', {'tags': ['ok', 5]}),
                  ('untag', {'tags': []}), ('untag', {'tags': [None]}),
                  ('attributes', {}), ('attributes', {'attributes': {}}), ('attributes', {'attributes': ['x']}),
                  ('attributes', {'attributes': [['x', 'v']]}), ('attributes', {'attributes': 'x=v'})):
    code, body = call('PUT', f'/alert/{tid}/{path}', bad)
    check(f'{path} with {json.dumps(bad)} is 400', code == 400, (code, body))
# an update that leaves no attributes is 200 [DECISIONS Y34; the reference answers 500]
before = call('GET', f'/alert/{tid}')[1].get('alert', {})
code, body = call('PUT', f'/alert/{tid}/attributes', {'attributes': {'keep': None, 'new': None}})
after = call('GET', f'/alert/{tid}')[1].get('alert', {})
check('removing the last attributes is 200 with {} stored', code == 200 and after.get('attributes') == {}, (code, after.get('attributes')))
code, body = call('PUT', f'/alert/{tid}/attributes', {'attributes': {'missing': None}})
after2 = call('GET', f'/alert/{tid}')[1].get('alert', {})
check('nulling a missing key on an empty alert is 200', code == 200 and after2.get('attributes') == {}, (code, after2.get('attributes')))
check('attribute edits add no history and keep updateTime',
      len(after2.get('history', [])) == len(before.get('history', [])) and after2.get('updateTime') == before.get('updateTime'),
      (len(after2.get('history', [])), len(before.get('history', []))))
code, body = call('PUT', f'/alert/no-such-{RUN}/tag', {'tags': ['x']})
check('tag on an unknown id is 404', code == 404, (code, body))
code, body = call('POST', '/alert', alert(resource=f'tagy-{RUN}', customer=Y), key=ax)
yid = body.get('id', '')
code, body = call('PUT', f'/alert/{yid}/tag', {'tags': ['x']}, key=wx)
check("customer-X key cannot tag a customer-Y alert (404)", code == 404, (code, body))
_, rk, rk_id = mint(['read:alerts'])
code, body = call('PUT', f'/alert/{tid}/attributes', {'attributes': {'a': 1}}, key=rk)
check('read-only key cannot change attributes (403)', code == 403, (code, body))
for i in (tid, yid):
    call('DELETE', f'/alert/{i}')
call('DELETE', f'/key/{rk_id}')

# --- concurrent first receives [A2-STORE-R1]
def burst(resource, severities):
    results = []
    def one(sev):
        results.append(call('POST', '/alert', alert(resource=resource, severity=sev, value=None)))
    threads = [threading.Thread(target=one, args=(sev,)) for sev in severities]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results

res = burst(f'race-{RUN}', ['major'] * 8)
code, body = call('GET', f'/alerts?resource=race-{RUN}')
a1 = body.get('alerts', [])
check('8 concurrent identical first receives make one alert with duplicateCount 7',
      all(c == 201 for c, _ in res) and len(a1) == 1 and a1[0].get('duplicateCount') == 7, [len(a1), a1[:1]])
res = burst(f'race2-{RUN}', ['major', 'minor', 'critical', 'warning'] * 2)
code, body = call('GET', f'/alerts?resource=race2-{RUN}')
check('8 concurrent first receives with mixed severities make one alert (correlation)',
      all(c == 201 for c, _ in res) and body.get('total') == 1, body.get('total'))

# --- heartbeat expiry boundary [A2-DOM-HB-1]
code, body = call('POST', '/heartbeat', {'origin': f'hb0/{RUN}', 'timeout': 0})
h0 = body.get('id', '')
time.sleep(0.5)
code, body = call('GET', f'/heartbeat/{h0}')
hb = body.get('heartbeat', {})
check('timeout 0 heartbeat is expired at +0.5 s with since 0 [HB2]',
      hb.get('status') == 'expired' and hb.get('since') == 0, hb)
call('DELETE', f'/heartbeat/{h0}')
code, body = call('POST', '/heartbeat', {'origin': f'hbmax/{RUN}', 'timeout': 2147483647})
hmax = body.get('id', '') if isinstance(body, dict) else ''
check('max-integer heartbeat timeout is accepted and renders [A2V2-DOM-R1, ADR-0005]',
      code == 201 and body.get('heartbeat', {}).get('status') == 'ok', (code, body))
code, body = call('GET', '/heartbeats')
check('heartbeat list with a max-integer timeout still renders', code == 200, code)
call('DELETE', f'/heartbeat/{hmax}')

# --- restricted attribute [spec/integration-hooks.md 4.2]: never in a response,
# on every alert read path; selectors on it refused for every caller
RK = 'zabbix_ack_sync_intent'
RSECRET = f'intent-{RUN}'
RRES = f'restricted-{RUN}'
MARKER = f'done\n[zabalack:v1 action={RUN} kid=k mac=00]'
REFUSAL = 'Query selector is not permitted on restricted attributes'
SHOWN = {'zabbix_ack_sync': 'pending', 'zabbix_ack_sync_reason': 'awaiting_capture', 'region': 'eu'}


def hides(attrs):
    return isinstance(attrs, dict) and RK not in attrs and all(attrs.get(k) == v for k, v in SHOWN.items())


code, body = call('POST', '/alert', alert(resource=RRES, attributes={RK: {'secret': RSECRET}, **SHOWN}))
ra = body.get('alert', {}) if isinstance(body, dict) else {}
rid = ra.get('id', '')
check('POST /alert reply hides the restricted attribute, keeps the others',
      code == 201 and hides(ra.get('attributes')) and RSECRET not in json.dumps(body), (code, ra.get('attributes')))
code, body = call('PUT', f'/alert/{rid}/action', {'action': 'ack', 'text': MARKER})
check('ack with a marker-bearing text', code == 200, (code, body))
code, body = call('GET', f'/alert/{rid}')
ga = body.get('alert', {}) if isinstance(body, dict) else {}
check('GET /alert/<id> hides the restricted attribute, keeps the others',
      code == 200 and hides(ga.get('attributes')) and RSECRET not in json.dumps(body), (code, ga.get('attributes')))
check('GET /alert/<id> keeps the marker in history text',
      any(h.get('text') == MARKER and h.get('type') == 'ack' for h in ga.get('history', [])), ga.get('history'))
for show in ('true', 'false'):
    code, body = call('GET', f'/alerts?resource={RRES}&show-history={show}')
    la = body.get('alerts', []) if isinstance(body, dict) else []
    check(f'GET /alerts (show-history={show}) hides the restricted attribute, keeps the others',
          code == 200 and len(la) == 1 and hides(la[0].get('attributes')) and RSECRET not in json.dumps(body),
          (code, [x.get('attributes') for x in la]))
    if show == 'true':
        check('GET /alerts with history keeps the marker in history text',
              la and any(h.get('text') == MARKER for h in la[0].get('history', [])), la and la[0].get('history'))
code, rkey, rkey_id = mint(['read:alerts'])
check('a read-only key for the restricted-selector checks', code == 201 and rkey, code)
SELECTORS = [
    (f'attributes.{RK}', RSECRET),
    (f'attributes.{RK}!', RSECRET),
    ('sort-by', f'-attributes.{RK}'),
    ('group-by', 'attributes'),
    ('q', f'_exists_:attributes.{RK}'),
]
for who, k_ in (('admin', KEY), ('read-only', rkey)):
    for path in ('/alerts', '/alerts/count'):
        for name, value in SELECTORS:
            qs = urllib.parse.urlencode({'resource': RRES, name: value})
            code, body = call('GET', f'{path}?{qs}', key=k_)
            check(f'{path} refuses {name} for the {who} caller with a content-free 400',
                  code == 400 and isinstance(body, dict) and body.get('message') == REFUSAL
                  and RSECRET not in json.dumps(body), (code, body))
code, body = call('GET', f"/alerts/count?{urllib.parse.urlencode({'resource': RRES, 'attributes.region': 'eu'})}", key=rkey)
check('an ordinary attribute parameter is not refused', code == 200, (code, body))
call('DELETE', f'/key/{rkey_id}')
call('DELETE', f'/alert/{rid}')
# Error bodies never carry restricted evidence [4.2 diagnostics, HOOK-DIAG-1]
REDACTED = 'zabalack_restricted_error_redacted'
for bad in (f'[zabalack:v1 action={RUN} kid=k mac=00]', f'[ACKLINE:{RUN}]', f'{RK.upper()}-{RUN}'):
    code, body = call('GET', '/alerts', key=bad)
    check(f'an invalid key carrying evidence ({bad[:10]}...) gets 401 with a redacted message',
          code == 401 and isinstance(body, dict) and body.get('message') == REDACTED
          and RUN not in json.dumps(body), (code, body))
code, body = call('GET', '/alerts', key=f'plain-{RUN}')
check('an ordinary invalid key is still named in the 401 message',
      code == 401 and isinstance(body, dict) and f'plain-{RUN}' in body.get('message', ''), (code, body))
# axum's own rejection of a malformed JSON body quotes the key path, outside ApiError
code, plain = call_raw('POST', '/alert', f'{{"plain-{RUN}": invalid}}'.encode())
check('malformed JSON is refused by the framework (4xx)', 400 <= code < 500, (code, plain))
for mk in (f'[zabalack:v1 action={RUN} kid=k mac=00]', f'[ZaBaLaCk:v1 {RUN}]', f'[AckLine:{RUN}]'):
    c2, text = call_raw('POST', '/alert', f'{{"{mk}": invalid}}'.encode())
    try:
        msg = json.loads(text).get('message')
    except ValueError:
        msg = None
    check(f'malformed JSON with a key carrying evidence ({mk[:10]}...): same status, redacted message',
          c2 == code and msg == REDACTED and RUN not in text and '[zabalack:' not in text.lower()
          and '[ackline:' not in text.lower(), (c2, text))

# --- housekeeping expiry [K2.1-K2.3, A2V3-STORE-N1]: global, so disposable targets only
HK_CASES = 6
if not DISPOSABLE:
    not_run += HK_CASES
    print(f'NOT_RUN housekeeping expiry ({HK_CASES} checks): global maintenance needs --disposable-target')
else:
    def mk(resource, **kw):
        code, body = call('POST', '/alert', alert(resource=resource, **kw))
        return body.get('id', '') if isinstance(body, dict) else ''

    short = mk(f'hk-short-{RUN}', timeout=1)
    zero = mk(f'hk-zero-{RUN}', timeout=0)
    huge = mk(f'hk-huge-{RUN}', timeout=2147483647)
    null = mk(f'hk-null-{RUN}', timeout=1)
    call('PUT', f'/alert/{null}/status', {'status': 'open', 'text': 'null timeout'})  # stores NULL [A1.3]
    time.sleep(2.2)
    code, body = call('POST', '/management/housekeeping')
    expired = body.get('expired', []) if isinstance(body, dict) else []
    check('housekeeping returns 200 with a max-integer timeout present', code == 200, (code, body))
    check('timeout 1 expires after 2 s [K2.1]', short in expired, expired)
    check('timeout 0 never expires [K2.3]', zero not in expired, expired)
    check('NULL timeout never expires [K2.2]', null not in expired, expired)
    check('max-integer timeout does not expire', huge not in expired, expired)
    code, body = call('GET', f'/alert/{short}')
    check('expired alert has status expired and an "expired" history entry [K2.5]',
          body.get('alert', {}).get('status') == 'expired'
          and body.get('alert', {}).get('history', [{}])[-1].get('type') == 'expired', body.get('alert', {}).get('status'))
    for i in (short, zero, huge, null):
        call('DELETE', f'/alert/{i}')

# --- delete and 404
code, body = call('DELETE', f'/alert/{aid}')
check('DELETE /alert/<id>', code == 200, (code, body))
code, body = call('GET', f'/alert/{aid}')
check('deleted alert is 404', code == 404, (code, body))

print(f'\n{failures} failure(s), {not_run} not run')
sys.exit(failures)
