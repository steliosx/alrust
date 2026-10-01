"""One reference-only groups page; all private prerequisites before Client."""
import os

import pytest

from alrust_conformance.api_key_session import require
from alrust_conformance.client import Client
from alrust_conformance.config_session import load_config_attestation
from alrust_conformance.customer_fixtures import load_credentials
from alrust_conformance.groups_session import GroupsSession, load_empty_groups_attestation


@pytest.fixture
def groups_session():
    target = os.environ.get('ALRUST_TARGET')
    load_config_attestation(os.environ.get('ALRUST_CONFIG_ATTESTATION_FILE'), target)
    load_empty_groups_attestation(os.environ.get('ALRUST_GROUPS_ATTESTATION_FILE'), target,
                                  os.environ.get('ALRUST_GROUPS_WINDOW_ID'),
                                  os.environ.get('ALRUST_GROUPS_SERVER_PID'))
    credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target)
    session = GroupsSession(Client(target, credentials.keys['unscoped_admin']))
    try:
        yield session
    finally:
        session.close()


def test_reference_groups_empty_page(groups_session):
    # f431272: views/groups.py:78-108; utils/paging.py:7-33, fixed positive size1.
    groups_session.read()
    require(groups_session.safe_summary()['completed'], 'groups read incomplete')
