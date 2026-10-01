"""Private synthetic credential metadata; no implementation imports or seeding."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import stat


@dataclass(frozen=True)
class CustomerCredentials:
    target: str
    customer_a: str
    customer_b: str
    keys: dict = field(repr=False)


def load_credentials(path, target):
    if not path:
        raise ValueError('NOT_RUN: supply ALRUST_SYNTHETIC_KEYS_FILE for an isolated synthetic instance')
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        raise ValueError('NOT_RUN: synthetic credential fixture is unavailable') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or not path.name.startswith('alrust-synthetic-') or path.suffix != '.json'):
        raise ValueError('synthetic credentials must be a private regular alrust-synthetic-*.json file')
    if info.st_size > 16384:
        raise ValueError('synthetic credential file exceeds fixture size limit')
    try:
        data = json.loads(path.read_text())
    except OSError:
        raise ValueError('NOT_RUN: synthetic credential fixture is unreadable') from None
    except (ValueError, UnicodeError):
        raise ValueError('synthetic credential file is not valid JSON') from None
    if (not isinstance(data, dict) or data.get('fixture') != 'SYNTHETIC'
            or data.get('target') != target.rstrip('/')
            or data.get('auth_required') is not False
            or data.get('customer_views') is not False):
        raise ValueError('synthetic fixture label, exact target or configuration does not match')
    customers = data.get('customers')
    if not isinstance(customers, dict) or set(customers) != {'a', 'b'}:
        raise ValueError('two distinct synthetic customers are required')
    if (any(not isinstance(value, str) or not re.fullmatch(
            r'alrust-synthetic-customer-[a-zA-Z0-9-]+', value) for value in customers.values())
            or customers['a'] == customers['b']):
        raise ValueError('customer names must be distinct labeled synthetic values')
    rows = data.get('keys')
    roles = {'unscoped_admin': (None, ['admin']),
             'a_admin': (customers['a'], ['admin']),
             'a_writer': (customers['a'], ['write:alerts']),
             'b_writer': (customers['b'], ['write:alerts'])}
    if not isinstance(rows, dict) or set(rows) != set(roles):
        raise ValueError('synthetic key roles do not match the vector prerequisites')
    keys = {}
    for role, (customer, scopes) in roles.items():
        row = rows[role]
        if (not isinstance(row, dict) or set(row) != {'key', 'customer', 'scopes'}
                or row.get('customer') != customer
                or row.get('scopes') != scopes or not isinstance(row.get('key'), str)
                or not re.fullmatch(r'SYNTHETIC-[0-9a-f]{48}', row['key'])):
            raise ValueError('synthetic key label or declared customer/scope prerequisites do not match')
        keys[role] = row['key']
    if len(set(keys.values())) != len(keys):
        raise ValueError('synthetic roles must use distinct keys')
    return CustomerCredentials(target.rstrip('/'), customers['a'], customers['b'], keys)
