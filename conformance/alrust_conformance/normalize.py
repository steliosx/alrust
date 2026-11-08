from copy import deepcopy
import json


POLICY = 'strict-json-v1; only selected alert href base URLs normalized'


def normalize(body, base_url: str):
    """Preserve values, unknown fields, nulls, identities and every list's order.

    Timestamps remain strict until a source-cited clock policy has peer review.
    Only href fields on response alert/history records have the known base URL
    removed. An attributes.href or other arbitrary user string stays untouched.
    Such an href that does not start with the base URL (for example a relative
    one) raises ValueError, because the reference always sends an absolute URL.
    """
    result = deepcopy(body)
    if not isinstance(result, dict):
        return result
    alert = result.get('alert')
    if not isinstance(alert, dict):
        return result
    prefix = base_url.rstrip('/') + '/'
    records = [alert]
    history = alert.get('history')
    if isinstance(history, list):
        records.extend(record for record in history if isinstance(record, dict))
    for record in records:
        href = record.get('href')
        if isinstance(href, str):
            if not href.startswith(prefix):
                raise ValueError('href does not start with the target base URL')
            record['href'] = href[len(prefix) - 1:]
    return result


def canonical(value) -> str:
    """JSON text that tells true from 1 and 1 from 1.0, unlike Python ==."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
