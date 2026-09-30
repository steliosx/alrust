from copy import deepcopy
import json

import pytest

from alrust_conformance.customer_fixtures import load_credentials


def fixture_data():
    customers = {'a': 'alrust-synthetic-customer-a', 'b': 'alrust-synthetic-customer-b'}
    roles = [('unscoped_admin', None, ['admin']),
             ('a_admin', customers['a'], ['admin']),
             ('a_writer', customers['a'], ['write:alerts']),
             ('b_writer', customers['b'], ['write:alerts'])]
    return {'fixture': 'SYNTHETIC', 'target': 'http://127.0.0.1:18081',
            'auth_required': False, 'customer_views': False, 'customers': customers,
            'keys': {role: {'key': 'SYNTHETIC-' + f'{index:048x}', 'customer': customer, 'scopes': scopes}
                     for index, (role, customer, scopes) in enumerate(roles)}}


def write_fixture(tmp_path, data):
    path = tmp_path / 'alrust-synthetic-credentials.json'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_fixture_target_roles_and_secret_repr(tmp_path):
    data = fixture_data()
    path = write_fixture(tmp_path, data)
    credentials = load_credentials(path, data['target'] + '/')
    assert credentials.customer_a != credentials.customer_b
    assert credentials.keys['a_admin'] == data['keys']['a_admin']['key']
    assert all(row['key'] not in repr(credentials) for row in data['keys'].values())
    with pytest.raises(ValueError, match='does not match'):
        load_credentials(path, 'http://127.0.0.1:18082')


def test_private_regular_fixture_and_missing_inputs(tmp_path):
    data = fixture_data()
    path = write_fixture(tmp_path, data)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private regular'):
        load_credentials(path, data['target'])
    path.chmod(0o600)
    alias = tmp_path / 'alrust-synthetic-alias.json'
    alias.symlink_to(path)
    with pytest.raises(ValueError, match='private regular'):
        load_credentials(alias, data['target'])
    with pytest.raises(ValueError, match='NOT_RUN'):
        load_credentials(None, data['target'])
    with pytest.raises(ValueError, match='NOT_RUN'):
        load_credentials(tmp_path / 'alrust-synthetic-missing.json', data['target'])


@pytest.mark.parametrize('change', ['label', 'config', 'customer', 'scope', 'key', 'duplicate-key'])
def test_incoherent_or_unlabeled_inputs_are_refused(tmp_path, change):
    original = fixture_data()
    data = deepcopy(original)
    if change == 'label':
        data['fixture'] = 'unknown'
    elif change == 'config':
        data['customer_views'] = True
    elif change == 'customer':
        data['keys']['a_writer']['customer'] = data['customers']['b']
    elif change == 'scope':
        data['keys']['a_admin']['scopes'] = ['write']
    elif change == 'key':
        data['keys']['a_admin']['key'] = 'unlabeled-credential'
    else:
        data['keys']['b_writer']['key'] = data['keys']['a_writer']['key']
    path = write_fixture(tmp_path, data)
    with pytest.raises(ValueError):
        load_credentials(path, original['target'])
