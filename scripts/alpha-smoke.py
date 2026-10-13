#!/usr/bin/env python3
"""End-to-end smoke test for a running alrust alpha server.

    python3 scripts/alpha-smoke.py http://localhost:8080 <admin-api-key>

Standard library only. Each step prints PASS or FAIL; the exit status is the
number of failures. Expected values come from spec/lifecycle.md (rule names
in brackets). Every resource name carries a unique run id, so the test can
run against a database that already holds data.
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://localhost:8080'
KEY = sys.argv[2] if len(sys.argv) > 2 else ''
RUN = uuid.uuid4().hex[:8]
failures = 0


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
code, body = call('POST', '/alert', alert(resource=f'bad-{RUN}', severity='bogus'))
check('unknown severity is 500 [R1.1]', code == 500, (code, body))

# --- query and counts
code, body = call('GET', f'/alerts?resource=web01-{RUN}')
check('GET /alerts finds it', code == 200 and body.get('total') == 1 and len(body.get('alerts', [])) == 1, body)
code, body = call('GET', f'/alerts/count?resource=web01-{RUN}')
check('GET /alerts/count', code == 200 and body.get('total') == 1, body)
code, body = call('GET', f'/alerts?resource=nothing-{RUN}')
check('empty query says not found', code == 200 and body.get('total') == 0 and body.get('message') == 'not found', body)

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

# --- delete and 404
code, body = call('DELETE', f'/alert/{aid}')
check('DELETE /alert/<id>', code == 200, (code, body))
code, body = call('GET', f'/alert/{aid}')
check('deleted alert is 404', code == 404, (code, body))

print(f'\n{failures} failure(s)')
sys.exit(failures)
