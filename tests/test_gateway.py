"""API management tests: authentication, access control, rate limiting, routing, correlation.

These only make sense through the gateway, so they are skipped when the suite targets the
services directly. Consumers and keys are defined in gateway/kong.yml.
"""
import uuid

import httpx
import pytest

from conftest import GATEWAY_URL as GW, VIA_GATEWAY

pytestmark = pytest.mark.skipif(not VIA_GATEWAY, reason="gateway tests need the full stack")


def call(method: str, path: str, key: str | None = None, **kwargs) -> httpx.Response:
    headers = kwargs.pop("headers", {})
    if key:
        headers["apikey"] = key
    return httpx.request(method, f"{GW}{path}", headers=headers, timeout=10, **kwargs)


# ---- Authentication (401) ------------------------------------------------------------------------

def test_missing_api_key_is_rejected():
    r = call("GET", "/api/customers/C001")
    assert r.status_code == 401
    assert "message" in r.json()


def test_invalid_api_key_is_rejected():
    assert call("GET", "/api/customers/C001", key="not-a-real-key").status_code == 401


def test_valid_api_key_is_accepted():
    assert call("GET", "/api/customers/C001", key="demo-client-key").status_code == 200


def test_api_key_also_accepted_as_query_parameter():
    assert call("GET", "/api/customers/C001?apikey=demo-client-key").status_code == 200


def test_agent_api_requires_key():
    assert call("POST", "/api/agent/query", json={"query": "Show customer C001"}).status_code == 401


# ---- Access control (403): least privilege per consumer ------------------------------------------

def test_chat_frontend_cannot_call_backend_apis_directly():
    assert call("GET", "/api/customers/C001", key="chat-frontend-key").status_code == 403
    assert call("GET", "/api/orders/O1001", key="chat-frontend-key").status_code == 403


def test_customer_agent_can_only_read_customers():
    assert call("GET", "/api/customers/C001", key="customer-agent-key").status_code == 200
    assert call("POST", "/api/customers", key="customer-agent-key",
                json={"name": "X", "email": "x@example.com"}).status_code == 403
    assert call("GET", "/api/orders/O1001", key="customer-agent-key").status_code == 403


def test_order_agent_can_only_read_orders():
    assert call("GET", "/api/orders/O1001", key="order-agent-key").status_code == 200
    assert call("PATCH", "/api/orders/O1001", key="order-agent-key",
                json={"status": "PAID"}).status_code == 403
    assert call("GET", "/api/customers/C001", key="order-agent-key").status_code == 403


def test_agents_cannot_call_the_agent_api():
    """Prevents an agent from looping back into the coordinator."""
    assert call("POST", "/api/agent/query", key="customer-agent-key", json={}).status_code == 403


# ---- Rate limiting (429) -------------------------------------------------------------------------

def test_rate_limit_returns_429_with_headers():
    """ratelimit-probe is allowed 5 requests per minute."""
    statuses = []
    for _ in range(8):
        r = call("GET", "/api/customers/C001", key="ratelimit-probe-key")
        statuses.append(r.status_code)
        if r.status_code == 429:
            break
    assert 429 in statuses, statuses
    assert r.headers.get("retry-after")
    assert r.headers.get("x-ratelimit-limit-minute") == "5"


def test_successful_responses_show_remaining_quota():
    r = call("GET", "/api/customers/C001", key="test-runner-key")
    assert r.headers.get("x-ratelimit-limit-minute") == "1000"
    assert int(r.headers["x-ratelimit-remaining-minute"]) >= 0


# ---- Routing, correlation, docs ------------------------------------------------------------------

def test_correlation_id_generated_by_gateway():
    r = call("GET", "/api/orders/O1001", key="test-runner-key")
    uuid.UUID(r.headers["x-correlation-id"])            # a valid UUID created by the gateway


def test_client_correlation_id_is_preserved_end_to_end():
    r = call("GET", "/api/customers/C999", key="test-runner-key", headers={"X-Correlation-ID": "demo-trace-001"})
    assert r.status_code == 404
    assert r.headers["x-correlation-id"] == "demo-trace-001"
    assert r.json()["error"]["correlation_id"] == "demo-trace-001"   # the backend saw the same ID


def test_unknown_route_returns_404():
    assert call("GET", "/api/unknown", key="test-runner-key").status_code == 404


def test_api_docs_are_public():
    assert call("GET", "/api/customers/docs").status_code == 200
    spec = call("GET", "/api/orders/openapi.json")
    assert spec.status_code == 200
    assert "GatewayApiKey" in spec.json()["components"]["securitySchemes"]


def test_oversized_body_is_rejected():
    big = "x" * (1024 * 1024 + 10)
    r = call("POST", "/api/customers", key="test-runner-key", content=big,
             headers={"Content-Type": "application/json"})
    assert r.status_code == 413


# ---- Agent API through the gateway --------------------------------------------------------------

def test_chat_frontend_can_use_the_agent_api():
    r = call("POST", "/api/agent/query", key="chat-frontend-key", json={"query": "Show customer C001"},
             headers={"X-Correlation-ID": "gw-agent-001"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed" and "Somchai Jaidee" in body["answer"]
    assert body["correlation_id"] == "gw-agent-001"


def test_key_scenario_through_the_gateway():
    r = call("POST", "/api/agent/query", key="chat-frontend-key",
             json={"query": "Find customer C001 and tell me their latest order status"})
    body = r.json()
    assert [s["agent"] for s in body["steps"]] == ["Customer Agent", "Order Agent"]
    assert "O1006" in body["answer"] and "SHIPPED" in body["answer"]


def test_agent_discovery_endpoint():
    names = {a["name"] for a in call("GET", "/api/agent/agents", key="chat-frontend-key").json()["agents"]}
    assert names == {"Customer Agent", "Order Agent"}


def test_agent_api_docs_are_public():
    assert call("GET", "/api/agent/docs").status_code == 200


# ---- Observability: one request, one trace in Jaeger --------------------------------------------

JAEGER_URL = "http://localhost:16686"


def test_gateway_returns_trace_id_and_jaeger_has_the_whole_trace():
    import time
    r = call("POST", "/api/agent/query", key="chat-frontend-key",
             json={"query": "Find customer C001 and tell me their latest order status"})
    trace_id = r.headers.get("x-trace-id")
    assert trace_id, "Kong's opentelemetry plugin should return X-Trace-Id"
    assert r.json()["trace_id"] == trace_id                     # gateway and coordinator: same trace

    expected = {"api-gateway", "coordinator-agent", "customer-agent", "order-agent",
                "customer-service", "order-service"}
    services: set = set()
    for _ in range(20):                                          # spans arrive in batches
        resp = httpx.get(f"{JAEGER_URL}/api/traces/{trace_id}", timeout=5)
        if resp.status_code == 200 and resp.json().get("data"):
            services = {p["serviceName"] for p in resp.json()["data"][0]["processes"].values()}
            if expected <= services:
                break
        time.sleep(1)
    assert expected <= services, f"components in trace: {sorted(services)}"
