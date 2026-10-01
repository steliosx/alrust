"""One reference-only read; prerequisite attestation precedes any HTTP."""
import os

import pytest

from alrust_conformance.api_key_session import require
from alrust_conformance.client import Client
from alrust_conformance.config_session import ConfigSession, load_config_attestation
from alrust_conformance.customer_fixtures import load_credentials


@pytest.fixture
def config_session():
    target = os.environ.get('ALRUST_TARGET')
    load_config_attestation(os.environ.get('ALRUST_CONFIG_ATTESTATION_FILE'), target)
    credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target)
    client = Client(target, credentials.keys['unscoped_admin'])
    session = ConfigSession(client)
    try:
        yield session
    finally:
        session.close()


def test_reference_config_readonly(config_session):
    # f431272: views/config.py:9-74, bare object; :42-43 model aliases.
    # Selector ALERTA -> alarms/alerta.py:81-82 display name Alerta 9.1.0.
    config_session.read()
    require(config_session.safe_summary()['completed'], 'config read incomplete')
