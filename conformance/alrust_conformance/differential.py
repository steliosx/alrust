"""Small seeded synthetic replay foundation, not the complete Phase 0 gate."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import uuid

from .client import Client, redact_secrets
from .normalize import POLICY, normalize


def sequence(seed: int, count: int, namespace: str, create_time: str):
    if count < 1 or not namespace.startswith('alrust-synthetic-'):
        raise ValueError('positive request count and labeled synthetic namespace required')
    rng = random.Random(seed)
    for index in range(count):
        yield {'method': 'POST', 'path': '/alert', 'payload': {
            'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{namespace}/{index}')),
            'resource': f'{namespace}-{rng.randrange(3)}',
            'event': 'SyntheticP0Probe', 'environment': 'Development',
            'severity': rng.choice(['major', 'minor', 'warning', 'normal']),
            'service': ['alrust-conformance'], 'origin': 'alrust-synthetic',
            'tags': ['SYNTHETIC', 'alrust-phase0'],
            'text': 'Synthetic fixture; not captured production data.',
            'createTime': create_time, 'timeout': 86400,
        }}


def replay(reference, candidate, requests):
    rows = []
    ids = [set(), set()]
    failure = False
    cleanup = []
    try:
        for request in requests:
            row = {'request': request, 'responses': []}
            rows.append(row)  # preserve the attempted request even if transport fails
            for side, client in enumerate([reference, candidate]):
                response = client.request(request['method'], request['path'], request['payload'])
                row['responses'].append({'status': response.status, 'body': response.body})
                if response.status == 201:
                    body = response.body
                    alert = body.get('alert') if isinstance(body, dict) else None
                    if not isinstance(alert, dict) or alert.get('resource') != request['payload']['resource']:
                        raise ValueError('created response does not identify our synthetic resource')
                    alert_id = body.get('id')
                    if not isinstance(alert_id, str):
                        raise ValueError('created alert id is not text')
                    uuid.UUID(alert_id)  # reject an unsafe path before cleanup
                    ids[side].add(alert_id)
                    if body.get('status') != 'ok' or alert.get('id') != alert_id:
                        raise ValueError('invalid created alert response')
            left, right = row['responses']
            row['equal'] = (left['status'] == right['status']
                            and normalize(left['body'], reference.base_url)
                            == normalize(right['body'], candidate.base_url))
            row['successful'] = left['status'] == right['status'] == 201
            if not row['equal'] or not row['successful']:
                failure = True
                break
    except Exception as error:
        failure = True
        # No credential-bearing exception message is exported.
        if not rows:
            rows.append({'request': None, 'responses': []})
        rows[-1]['transport_or_schema_error'] = type(error).__name__
    finally:
        for side, client in enumerate([reference, candidate]):
            for alert_id in sorted(ids[side]):
                try:
                    response = client.request('DELETE', f'/alert/{alert_id}')
                    cleanup.append({'side': side, 'id': alert_id, 'status': response.status})
                    failure |= response.status != 200
                except Exception as error:
                    cleanup.append({'side': side, 'id': alert_id, 'error_type': type(error).__name__})
                    failure = True
    return {'status': 'FAIL' if failure or not rows else 'LOCAL_PASS', 'policy': POLICY,
            'requests': rows, 'cleanup': cleanup}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--steps', type=int, default=20)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.steps < 1:
        parser.error('--steps must be positive')
    if args.report.exists():
        parser.error('report already exists; retain it and select a fresh path')
    left = Client(args.reference, os.environ.get('ALRUST_REFERENCE_API_KEY'))
    right = Client(args.candidate, os.environ.get('ALRUST_API_KEY'))
    if left.base_url == right.base_url:
        left.close()
        right.close()
        parser.error('reference and candidate must be separate target instances')
    namespace = 'alrust-synthetic-' + uuid.uuid4().hex
    created = datetime.now(timezone.utc).isoformat()
    requests = list(sequence(args.seed, args.steps, namespace, created))
    try:
        result = replay(left, right, requests)
    finally:
        left.close()
        right.close()
    root = Path(__file__).resolve().parents[2]
    result.update({'fixture': 'SYNTHETIC', 'seed': args.seed, 'namespace': namespace,
                   'targets': {'reference': left.base_url, 'candidate': right.base_url},
                   'planned_sequence': requests,
                   'declared_reference_pin': 'f4312727783dad64198b62ed778bbca98a5144a3',
                   'harness_head': subprocess.check_output(
                       ['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
                   'harness_dirty': bool(subprocess.check_output(
                       ['git', '-C', str(root), 'status', '--porcelain'], text=True)),
                   'harness_files_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in Path(__file__).parent.glob('*.py')}})
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.report.open('x') as handle:
        # Reference auth errors echo submitted keys (auth/decorators.py:42-45).
        # Compare original bodies first, then remove known credential echoes.
        exported = redact_secrets(result, [os.environ.get('ALRUST_REFERENCE_API_KEY'),
                                          os.environ.get('ALRUST_API_KEY')])
        json.dump(exported, handle, indent=2)
        handle.write('\n')
    print(result['status'])
    return 0 if result['status'] == 'LOCAL_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
