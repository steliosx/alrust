"""One explicit reference GET/_ request, distinct from readiness/setup."""
import os

import pytest

from alrust_conformance.api_key_session import require
from alrust_conformance.client import Client
from alrust_conformance.config_session import load_config_attestation
from alrust_conformance.customer_fixtures import load_credentials
from alrust_conformance.root_debug_session import RootDebugSession, load_root_debug_connection_attestation


@pytest.fixture
def root_debug_session():
    target = os.environ.get('ALRUST_TARGET')
    load_config_attestation(os.environ.get('ALRUST_CONFIG_ATTESTATION_FILE'), target)
    load_root_debug_connection_attestation(
        os.environ.get('ALRUST_ROOT_DEBUG_ATTESTATION_FILE'), target,
        os.environ.get('ALRUST_ROOT_DEBUG_WINDOW_ID'), os.environ.get('ALRUST_ROOT_DEBUG_SERVER_PID'),
        os.environ.get('ALRUST_ROOT_DEBUG_PROVENANCE_FILE'), os.environ.get('ALRUST_ROOT_DEBUG_READINESS_FILE'),
        os.environ.get('ALRUST_SERVED_CONFIG_ATTESTATION_FILE'))
    credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target)
    # This header is ignored by the undecorated root-debug route; it is not an auth test.
    session = RootDebugSession(Client(target, credentials.keys['unscoped_admin']))
    try:
        yield session
    finally:
        session.close()


def test_reference_root_debug_ok(root_debug_session):
    # f431272: alerta/views/__init__.py:44-46. No whole-stack DB/auth claim.
    root_debug_session.read()
    require(root_debug_session.safe_summary()['completed'], 'root-debug read incomplete')
