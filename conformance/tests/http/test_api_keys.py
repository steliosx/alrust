"""One ordinary owned-key case, pinned reference f431272; HTTP NOT_RUN."""
import os

import pytest

from alrust_conformance.api_key_session import ApiKeySession, require
from alrust_conformance.client import Client
from alrust_conformance.customer_fixtures import load_credentials


@pytest.fixture
def api_key_session():
    target = os.environ.get('ALRUST_TARGET')
    directory = os.environ.get('ALRUST_PRIVATE_EVIDENCE_DIR')
    require(bool(target and directory), 'NOT_RUN: isolated target and private evidence required')
    probe = Client(target)
    try:
        credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), probe.base_url)
    finally:
        probe.close()
    client = Client(credentials.target, credentials.keys['unscoped_admin'])
    try:
        session = ApiKeySession(client, directory)
    except Exception:
        client.close()
        raise AssertionError('ordinary key session setup failed') from None
    try:
        yield session
    finally:
        session.close()


def test_owned_key_create_detail_update_delete(api_key_session):
    # models/key.py:24-77,99-124,151-159; utils/key.py:30-34;
    # views/keys.py:18-73,116-168; postgres/base.py:939-953,979-1000,1010-1016.
    # Explicit unchanged scopes/customer/expiry avoid AK and C23 defect paths.
    api_key_session.create()
    api_key_session.read('SYNTHETIC ordinary key')
    api_key_session.update()
    api_key_session.read('SYNTHETIC ordinary key updated')
    api_key_session.delete()
    # No response output; the runner may export only vetted fixed metadata.
    api_key_session.safe_summary()
