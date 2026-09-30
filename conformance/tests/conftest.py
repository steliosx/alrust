import os

import pytest

from alrust_conformance.client import Client


@pytest.fixture
def target():
    url = os.environ.get('ALRUST_TARGET')
    if not url:
        pytest.fail('NOT_RUN: set ALRUST_TARGET to an isolated synthetic test instance')
    client = Client(url, os.environ.get('ALRUST_API_KEY'))
    try:
        yield client
    finally:
        client.close()
