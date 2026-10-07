"""Black-box API tests. They talk HTTP only, so they run against:

  * the full Docker stack through the gateway (default):
        pytest tests/
  * services started directly (no gateway), e.g. during development:
        CUSTOMER_API_URL=http://localhost:8101/customers ORDER_API_URL=http://localhost:8102/orders pytest tests/
"""
import os
import uuid

import httpx
import pytest

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000").rstrip("/")
CUSTOMER_API_URL = os.getenv("CUSTOMER_API_URL", f"{GATEWAY_URL}/api/customers").rstrip("/")
ORDER_API_URL = os.getenv("ORDER_API_URL", f"{GATEWAY_URL}/api/orders").rstrip("/")
# The gateway requires an API key. The test suite has its own consumer (see gateway/kong.yml).
# When calling services directly the header is simply ignored.
API_KEY = os.getenv("API_KEY", "test-runner-key")
VIA_GATEWAY = CUSTOMER_API_URL.startswith(GATEWAY_URL)


@pytest.fixture(scope="session")
def http():
    headers = {"apikey": API_KEY} if API_KEY else {}
    with httpx.Client(timeout=10, headers=headers) as client:
        yield client


@pytest.fixture
def unique_email():
    return f"test-{uuid.uuid4().hex[:10]}@example.com"


def assert_error(response, status: int, code: str):
    """Every error on the platform must use the same envelope."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["error"]["correlation_id"]
    assert response.headers["x-correlation-id"] == body["error"]["correlation_id"]
    return body["error"]
