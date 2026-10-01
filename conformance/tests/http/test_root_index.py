"""One reference GET / case, separate from readiness/setup and source-only probes."""
import os

import pytest

from alrust_conformance.api_key_session import require
from alrust_conformance.client import Client
from alrust_conformance.config_session import load_config_attestation
from alrust_conformance.customer_fixtures import load_credentials
from alrust_conformance.root_index_session import RootIndexSession, load_root_index_attestation


@pytest.fixture
def root_index_session():
    target = os.environ.get('ALRUST_TARGET')
    load_config_attestation(os.environ.get('ALRUST_CONFIG_ATTESTATION_FILE'), target)
    oracle = load_root_index_attestation(
        os.environ.get('ALRUST_ROOT_INDEX_ATTESTATION_FILE'), target,
        os.environ.get('ALRUST_ROOT_INDEX_WINDOW_ID'), os.environ.get('ALRUST_ROOT_INDEX_SERVER_PID'),
        os.environ.get('ALRUST_ROOT_INDEX_PROVENANCE_FILE'), os.environ.get('ALRUST_ROOT_INDEX_READINESS_FILE'),
        os.environ.get('ALRUST_SERVED_CONFIG_ATTESTATION_FILE'),
        os.environ.get('ALRUST_ROOT_INDEX_INVENTORY_FILE'))
    credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target)
    # A synthetic header on an undecorated root route makes no auth guarantee.
    session = RootIndexSession(Client(target, credentials.keys['unscoped_admin']), oracle)
    try:
        yield session
    finally:
        session.close()


def test_reference_root_index_inventory(root_index_session):
    # f431272: views/__init__.py:23-41, utils/response.py:22-27,
    # utils/webhook.py:25-46. Full actual map, not OpenAPI path counts.
    root_index_session.read()
    require(root_index_session.safe_summary()['completed'], 'root-index read incomplete')
