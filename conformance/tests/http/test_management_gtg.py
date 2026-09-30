"""One explicit healthy reference GTG request, distinct from readiness/setup."""
import os

import pytest

from alrust_conformance.api_key_session import require
from alrust_conformance.client import Client
from alrust_conformance.config_session import load_config_attestation
from alrust_conformance.customer_fixtures import load_credentials
from alrust_conformance.gtg_session import GtgSession, load_gtg_connection_attestation


@pytest.fixture
def gtg_session():
    target = os.environ.get('ALRUST_TARGET')
    load_config_attestation(os.environ.get('ALRUST_CONFIG_ATTESTATION_FILE'), target)
    load_gtg_connection_attestation(
        os.environ.get('ALRUST_GTG_ATTESTATION_FILE'), target,
        os.environ.get('ALRUST_GTG_WINDOW_ID'), os.environ.get('ALRUST_GTG_SERVER_PID'),
        os.environ.get('ALRUST_GTG_PROVENANCE_FILE'), os.environ.get('ALRUST_GTG_READINESS_FILE'),
        os.environ.get('ALRUST_SERVED_CONFIG_ATTESTATION_FILE'))
    credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target)
    # This header is ignored by the undecorated GTG route; it is not an auth test.
    session = GtgSession(Client(target, credentials.keys['unscoped_admin']))
    try:
        yield session
    finally:
        session.close()


def test_reference_management_gtg_healthy(gtg_session):
    # f431272: management/views.py:127-134; postgres/base.py:141-145.
    gtg_session.read()
    require(gtg_session.safe_summary()['completed'], 'GTG read incomplete')
