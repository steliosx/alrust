"""Reference-only single unknown-name case, not provider/Rust/auth parity."""
import os
import pytest
from alrust_conformance.client import Client
from alrust_conformance.webhook_unknown_session import (
    RECORD_FIELDS, REQUEST_ID, UnknownWebhookSession, load_unknown_prerequisites)


@pytest.fixture
def webhook_unknown_session():
    target = os.environ.get('ALRUST_TARGET')
    paths = {k: os.environ.get('ALRUST_WEBHOOK_UNKNOWN_' + k.upper() + '_FILE')
             for k in RECORD_FIELDS}
    prerequisites = load_unknown_prerequisites(paths,
        os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), target,
        os.environ.get('ALRUST_WEBHOOK_UNKNOWN_WINDOW_ID'),
        os.environ.get('ALRUST_WEBHOOK_UNKNOWN_SERVER_PID'))
    session = UnknownWebhookSession(Client(target, prerequisites.api_key,
                                           request_id=REQUEST_ID), prerequisites)
    try:
        yield session
    finally:
        session.close()


def test_reference_unknown_webhook(webhook_unknown_session):
    # f431272: webhooks/custom.py17–23; exceptions.py9–21/64–107;
    # tracing.py18–25. Permission key usage UPDATE precedes unknown-name404.
    webhook_unknown_session.read()
