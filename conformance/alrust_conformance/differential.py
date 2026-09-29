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


def provenance(root):
    """Capture identity before any HTTP effects; missing identity prevents a run."""
    result = {'harness_files_sha256': {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parent.glob('*.py')}, 'provenance_errors': []}
    for field, command in [('harness_head', ['rev-parse', 'HEAD']),
                           ('harness_dirty', ['status', '--porcelain'])]:
        try:
            value = subprocess.check_output(['git', '-C', str(root), *command],
                                            text=True, stderr=subprocess.DEVNULL)
            result[field] = bool(value) if field == 'harness_dirty' else value.strip()
        except (OSError, subprocess.CalledProcessError) as error:
            result[field] = None
            result['provenance_errors'].append({'field': field, 'error_type': type(error).__name__})
    return result


def write_report(handle, result):
    # Compare raw responses first; exported authentication errors may echo keys.
    exported = redact_secrets(result, [os.environ.get('ALRUST_REFERENCE_API_KEY'),
                                      os.environ.get('ALRUST_API_KEY')])
    handle.seek(0)
    json.dump(exported, handle, indent=2)
    handle.write('\n')
    handle.truncate()
    handle.flush()


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
    namespace = 'alrust-synthetic-' + uuid.uuid4().hex
    # Reference DateTime.parse accepts fractional seconds followed by literal Z
    # (alerta/utils/format.py:65-72), rather than a +00:00 offset.
    created = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    requests = list(sequence(args.seed, args.steps, namespace, created))
    root = Path(__file__).resolve().parents[2]
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reserve a private, unique report before HTTP effects. An interrupted run
    # retains its preflight record; no prior attempt can be overwritten.
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        result = {'status': 'NOT_RUN', 'stage': 'preflight', 'fixture': 'SYNTHETIC',
                  'seed': args.seed, 'namespace': namespace, 'planned_sequence': requests,
                  'declared_reference_pin': 'f4312727783dad64198b62ed778bbca98a5144a3',
                  'requests': [], 'cleanup': []}
        write_report(handle, result)
        clients = []
        try:
            result.update(provenance(root))
            if result['provenance_errors']:
                result['not_run_reason'] = 'harness source provenance unavailable'
            else:
                clients.append(Client(args.reference, os.environ.get('ALRUST_REFERENCE_API_KEY')))
                clients.append(Client(args.candidate, os.environ.get('ALRUST_API_KEY')))
                left, right = clients
                if left.base_url == right.base_url:
                    raise ValueError('reference and candidate must be separate target instances')
                result['targets'] = {'reference': left.base_url, 'candidate': right.base_url}
                write_report(handle, result)
                result.update(replay(left, right, requests))
                result['stage'] = 'completed'
        except Exception as error:
            result.update(status='FAIL', runner_error_type=type(error).__name__)
        finally:
            for client in clients:
                try:
                    client.close()
                except Exception as error:
                    result.update(status='FAIL', client_close_error_type=type(error).__name__)
            write_report(handle, result)
    print(result['status'])
    return 0 if result['status'] == 'LOCAL_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
