"""One reference-only permissions page; private prerequisites before Client."""
import os

import pytest

from alrust_conformance.api_key_session import require
from alrust_conformance.client import Client
from alrust_conformance.config_session import load_config_attestation
from alrust_conformance.customer_fixtures import load_credentials
from alrust_conformance.permissions_session import (
    PermissionsSession, load_empty_permissions_attestation,
)


@pytest.fixture
def permissions_session():
    target = os.environ.get('ALRUST_TARGET')
    load_config_attestation(os.environ.get('ALRUST_CONFIG_ATTESTATION_FILE'), target)
    load_empty_permissions_attestation(
        os.environ.get('ALRUST_PERMISSIONS_ATTESTATION_FILE'), target,
        os.environ.get('ALRUST_PERMISSIONS_WINDOW_ID'),
        os.environ.get('ALRUST_PERMISSIONS_SERVER_PID'))
    credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target)
    session = PermissionsSession(Client(target, credentials.keys['unscoped_admin']))
    try:
        yield session
    finally:
        session.close()


def test_reference_permissions_virtual_page(permissions_session):
    # f431272: views/permissions.py:65-129; models/permission.py:18,35-41.
    permissions_session.read()
    require(permissions_session.safe_summary()['completed'], 'permissions read incomplete')
