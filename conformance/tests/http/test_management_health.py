"""One explicit reference case, separate from readiness; prerequisites before Client."""
import os

import pytest

from alrust_conformance.api_key_session import require
from alrust_conformance.client import Client
from alrust_conformance.config_session import load_config_attestation
from alrust_conformance.customer_fixtures import load_credentials
from alrust_conformance.health_session import HealthSession, load_empty_heartbeats_attestation


@pytest.fixture
def health_session():
    target = os.environ.get('ALRUST_TARGET')
    load_config_attestation(os.environ.get('ALRUST_CONFIG_ATTESTATION_FILE'), target)
    load_empty_heartbeats_attestation(
        os.environ.get('ALRUST_HEALTH_ATTESTATION_FILE'), target,
        os.environ.get('ALRUST_HEALTH_WINDOW_ID'), os.environ.get('ALRUST_HEALTH_SERVER_PID'))
    credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target)
    session = HealthSession(Client(target, credentials.keys['unscoped_admin']))
    try:
        yield session
    finally:
        session.close()


def test_reference_management_health_empty(health_session):
    # f431272: management/views.py:137-153; models/heartbeat.py:166-167.
    health_session.read()
    require(health_session.safe_summary()['completed'], 'health read incomplete')
