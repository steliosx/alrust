from copy import deepcopy


POLICY = 'strict-json-v1; only selected alert href base URLs normalized'


def normalize(body, base_url: str):
    """Preserve values, unknown fields, nulls, identities and every list's order.

    Timestamps remain strict until a source-cited clock policy has peer review.
    Only href fields on response alert/history records have the known base URL
    removed. An attributes.href or other arbitrary user string stays untouched.
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
        if isinstance(href, str) and href.startswith(prefix):
            record['href'] = href[len(prefix) - 1:]
    return result
