"""One root-index read, compared with a separately observed private route map."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
from urllib.parse import urljoin

from .api_key_session import require
from .config_session import REFERENCE_HEAD, REFERENCE_TARGET, load_config_attestation

TASK = 'ALRUST-P0-ROOT-INDEX-READONLY-01'
IDENTITY_KEYS = {'project', 'fixture', 'task', 'reference_head', 'target',
                 'window_id', 'server_pid'}
CONNECTION_CHECKS = {'backend_uri_matches', 'database_name_matches',
                     'schema_matches', 'connection_destination_matches'}
MAX_RECORD_BYTES = 131072


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'root-index prerequisite duplicate JSON key')
        result[key] = value
    return result


def _private_record(path):
    require(bool(path), 'NOT_RUN: root-index private prerequisite file required')
    try:
        item = Path(path)
        info = item.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= MAX_RECORD_BYTES,
                'root-index prerequisite must be private regular bounded file')
        raw = item.read_bytes()
        require(len(raw) <= MAX_RECORD_BYTES, 'root-index prerequisite exceeds bound')
        data = json.loads(raw, object_pairs_hook=_object)
    except AssertionError:
        raise
    except Exception:
        raise AssertionError('root-index prerequisite unavailable or malformed') from None
    require(type(data) is dict, 'root-index prerequisite object required')
    return raw, data


def _identity(data, target, window, server_pid):
    require(data['project'] == 'alrust' and data['fixture'] == 'SYNTHETIC'
            and data['task'] == TASK and data['reference_head'] == REFERENCE_HEAD
            and data['target'] == target and data['window_id'] == window,
            'root-index prerequisite source or instance mismatch')
    require(type(data['server_pid']) is int and data['server_pid'] == int(server_pid),
            'root-index prerequisite owned PID mismatch')


@dataclass(frozen=True, repr=False)
class RootIndexOracle:
    uri: str
    # Each row is (rel, literal href, method tokens, is_custom); ties and duplicates stay.
    rows: tuple


def _oracle(inventory, target):
    require(inventory['uri'] == target + '/', 'root-index exact root URI mismatch')
    rows = []
    for field, custom in [('ordinary_rules', False), ('custom_rules', True)]:
        rules = inventory[field]
        require(type(rules) is list, 'root-index observed rule list required')
        for rule in rules:
            require(type(rule) is dict and set(rule) == {'endpoint', 'rule', 'methods'},
                    'root-index observed rule shape mismatch')
            require(type(rule['endpoint']) is str and bool(rule['endpoint'])
                    and type(rule['rule']) is str and rule['rule'].startswith('/'),
                    'root-index observed rule identity mismatch')
            methods = rule['methods']
            require(type(methods) is list and all(type(m) is str and re.fullmatch(r'[A-Z]+', m)
                    for m in methods) and len(methods) == len(set(methods)),
                    'root-index observed rule methods mismatch')
            if custom:
                require(methods == ['POST', 'GET'], 'root-index custom rule methods mismatch')
            else:
                methods = [m for m in methods if m not in {'HEAD', 'OPTIONS'}]
            href = urljoin(inventory['uri'] + '/', rule['rule'].lstrip('/'))
            rows.append((rule['endpoint'], href, tuple(methods), custom))
    # Stable sorting reproduces the source; no producer order or multiplicity is lost.
    return RootIndexOracle(inventory['uri'], tuple(sorted(rows, key=lambda row: row[1])))


def load_root_index_attestation(path, target, window, server_pid, provenance_path,
                                readiness_path, served_config_path, inventory_path):
    """Validate frozen whole-byte chains before constructing an HTTP client.

    Records are accident guards, not independent proof of observations. A fresh
    separately reviewed producer must observe actual served url_map iteration
    and successful custom_webhooks registry order after readiness, before the
    case; never derive this oracle from GET /. It must recheck that inventory,
    configuration and its own request connection before the original handler.
    The handler performs no SQL; factory/readiness still depend on PostgreSQL.
    """
    require(target == REFERENCE_TARGET, 'NOT_RUN: exact isolated root-index target required')
    require(type(window) is str and re.fullmatch(
        r'alrust-root-index-reference-[0-9]{8}-[0-9]{2}', window) is not None,
        'NOT_RUN: root-index window identity required')
    require(type(server_pid) is str and server_pid.isascii() and server_pid.isdecimal()
            and 1 < int(server_pid) < 2**31, 'NOT_RUN: owned root-index server PID required')
    raw, connection = _private_record(path)
    require(set(connection) == IDENTITY_KEYS | CONNECTION_CHECKS | {'backend'},
            'root-index connection record shape mismatch')
    _identity(connection, target, window, server_pid)
    require(connection['backend'] == 'postgresql'
            and all(connection[k] is True for k in CONNECTION_CHECKS),
            'NOT_RUN: actual served PostgreSQL identity must be attested')
    _, provenance = _private_record(provenance_path)
    hashes = {'connection_attestation_sha256', 'served_config_sha256',
              'readiness_marker_sha256', 'route_inventory_sha256'}
    require(set(provenance) == IDENTITY_KEYS | hashes | {'after_readiness', 'same_served_connection'},
            'root-index provenance shape mismatch')
    _identity(provenance, target, window, server_pid)
    require(provenance['after_readiness'] is True and provenance['same_served_connection'] is True,
            'root-index served connection provenance mismatch')
    require(all(type(provenance[k]) is str and re.fullmatch(r'[0-9a-f]{64}', provenance[k])
                for k in hashes), 'root-index provenance hash shape mismatch')
    ready_raw, ready = _private_record(readiness_path)
    require(set(ready) == {'project', 'fixture', 'window_id', 'server_pid', 'ready'},
            'root-index readiness marker shape mismatch')
    require(ready['project'] == 'alrust' and ready['fixture'] == 'SYNTHETIC'
            and ready['window_id'] == window and ready['ready'] is True
            and type(ready['server_pid']) is int and ready['server_pid'] == int(server_pid),
            'root-index readiness marker instance mismatch')
    config_raw, _ = _private_record(served_config_path)
    load_config_attestation(served_config_path, target)
    require(Path(served_config_path).read_bytes() == config_raw,
            'root-index served config changed during validation')
    inventory_raw, inventory = _private_record(inventory_path)
    require(set(inventory) == IDENTITY_KEYS | {'uri', 'ordinary_rules', 'custom_rules'},
            'root-index route inventory shape mismatch')
    _identity(inventory, target, window, server_pid)
    for key, record_raw in [('connection_attestation_sha256', raw),
                            ('served_config_sha256', config_raw),
                            ('readiness_marker_sha256', ready_raw),
                            ('route_inventory_sha256', inventory_raw)]:
        require(provenance[key] == hashlib.sha256(record_raw).hexdigest(),
                'root-index complete-record hash chain mismatch')
    return _oracle(inventory, target)


class RootIndexSession:
    def __init__(self, client, oracle):
        self.client, self.oracle = client, oracle
        self.attempted = self.completed = False

    def read(self):
        require(not self.attempted, 'root-index read already attempted')
        self.attempted = True
        try:
            response = self.client.request('GET', '/')
        except Exception:
            raise AssertionError('root-index HTTP request or JSON parse failed') from None
        require(type(response.status) is int and response.status == 200, 'root-index HTTP status mismatch')
        body = response.body
        require(type(body) is dict and set(body) == {'status', 'uri', 'data', 'links'},
                'root-index top-level object or keys mismatch')
        require(body['status'] == 'ok' and body['uri'] == self.oracle.uri
                and type(body['data']) is dict and body['data'] == {'description': 'Alerta API'},
                'root-index status, exact URI or description mismatch')
        links = body['links']
        require(type(links) is list and len(links) == len(self.oracle.rows),
                'root-index complete inventory length mismatch')
        for link, (rel, href, methods, custom) in zip(links, self.oracle.rows):
            require(type(link) is dict and set(link) == {'rel', 'href', 'method'},
                    'root-index link object or keys mismatch')
            require(link['rel'] == rel and link['href'] == href and type(link['method']) is str,
                    'root-index ordered literal route mismatch')
            if custom:
                require(link['method'] == ','.join(methods), 'root-index custom method order mismatch')
            else:
                actual = link['method'].split(',') if link['method'] else []
                require(len(actual) == len(methods) and set(actual) == set(methods),
                        'root-index registered method membership mismatch')
        self.completed = True

    def safe_summary(self):
        return {'project': 'alrust', 'case': 'root-index-readonly-01',
                'completed': self.completed, 'captured_response_records': 0}

    def close(self):
        try:
            self.client.close()
        except Exception:
            raise AssertionError('root-index client close failed') from None
