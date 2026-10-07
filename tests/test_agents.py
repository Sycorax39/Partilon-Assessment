"""Unit tests for the A2A protocol layer and the Customer / Order agents.

No Docker needed: each agent runs in-process and its outbound API calls go to a FAKE GATEWAY
(httpx.MockTransport), so success, not-found and every failure mode can be produced on demand.

    pip install -r tests/requirements.txt
    pytest tests/test_agents.py -v
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
for p in ("libs", "agents/customer-agent", "agents/order-agent"):
    sys.path.insert(0, str(ROOT / p))

from customer_agent.main import create_app as create_customer_agent  # noqa: E402
from order_agent.main import create_app as create_order_agent  # noqa: E402

C001 = {"customer_id": "C001", "name": "Somchai Jaidee", "email": "somchai.j@example.com",
        "phone": "+66812345678", "tier": "GOLD", "created_at": "2024-03-12T02:15:00Z",
        "updated_at": "2025-11-02T07:20:00Z"}
O1006 = {"order_id": "O1006", "customer_id": "C001", "status": "SHIPPED", "currency": "THB",
         "total_amount": "5480.00", "created_at": "2026-10-03T04:15:00Z", "updated_at": "2026-10-05T02:40:00Z",
         "items": [{"line_no": 1}, {"line_no": 2}]}
O1001 = {**O1006, "order_id": "O1001", "status": "DELIVERED", "total_amount": "1880.00",
         "created_at": "2026-08-14T03:20:00Z", "updated_at": "2026-08-18T08:02:00Z"}


def not_found(code, msg):
    return httpx.Response(404, json={"error": {"code": code, "message": msg, "details": None,
                                               "correlation_id": "x"}})


class FakeGateway:
    """Plays the API gateway: checks the agent's key and answers like the real APIs would."""

    def __init__(self, expected_key: str):
        self.expected_key = expected_key
        self.requests: list[httpx.Request] = []
        self.override = None          # callable(request) -> Response | raises, to inject failures

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override:
            result = self.override(request)
            return await result if asyncio.iscoroutine(result) else result
        if request.headers.get("apikey") != self.expected_key:
            return httpx.Response(401, json={"message": "Unauthorized"})
        path, q = request.url.path, request.url.params
        if path == "/api/customers/C001":
            return httpx.Response(200, json=C001)
        if path.startswith("/api/customers/"):
            return not_found("CUSTOMER_NOT_FOUND", f"Customer {path.rsplit('/', 1)[1]} was not found")
        if path == "/api/customers":
            data = [C001] if q.get("email") == C001["email"] else []
            return httpx.Response(200, json={"data": data, "pagination": {"limit": 1, "offset": 0, "total": len(data)}})
        if path == "/api/orders/O1001":
            return httpx.Response(200, json=O1001)
        if path.startswith("/api/orders/"):
            return not_found("ORDER_NOT_FOUND", "Order was not found")
        if path == "/api/orders":
            orders = [O1006, O1001] if q.get("customer_id") == "C001" else []
            limit = int(q.get("limit", 20))
            return httpx.Response(200, json={"data": orders[:limit],
                                             "pagination": {"limit": limit, "offset": 0, "total": len(orders)}})
        return httpx.Response(404, json={"message": "no Route matched with those values"})


@pytest.fixture
def customer_agent():
    gw = FakeGateway("customer-agent-key")
    with TestClient(create_customer_agent(httpx.MockTransport(gw.handler))) as client:
        yield client, gw


@pytest.fixture
def order_agent():
    gw = FakeGateway("order-agent-key")
    with TestClient(create_order_agent(httpx.MockTransport(gw.handler))) as client:
        yield client, gw


def rpc(client, method, params, headers=None):
    r = client.post("/", json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
                    headers=headers or {})
    assert r.status_code == 200
    return r.json()


def send(client, skill=None, input=None, text=None, headers=None):
    parts = []
    if skill:
        parts.append({"kind": "data", "data": {"skill": skill, "input": input or {}}})
    if text:
        parts.append({"kind": "text", "text": text})
    body = rpc(client, "message/send", {"message": {"role": "user", "parts": parts, "messageId": "m1"}}, headers)
    assert "result" in body, body
    return body["result"]


def result_data(task):
    return task["artifacts"][0]["parts"][0]["data"]


def error_of(task):
    return task["status"]["message"]["parts"][1]["data"]["error"]


# ==== A2A protocol: discovery, lifecycle, JSON-RPC ================================================

def test_agent_card_is_published_for_discovery(customer_agent):
    client, _ = customer_agent
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Customer Agent"
    assert card["protocolVersion"] == "0.3.0"
    assert card["url"] == "http://customer-agent:8000/"
    assert {s["id"] for s in card["skills"]} == {"get_customer", "find_customer_by_email"}
    skill = next(s for s in card["skills"] if s["id"] == "get_customer")
    assert "customer_id" in skill["inputSchema"]["properties"]
    assert client.get("/.well-known/agent.json").json() == card        # legacy path


def test_order_agent_card_lists_its_skills(order_agent):
    client, _ = order_agent
    card = client.get("/.well-known/agent-card.json").json()
    assert {s["id"] for s in card["skills"]} == {"get_order", "get_latest_order", "list_customer_orders"}


def test_completed_task_has_lifecycle_and_artifact(customer_agent):
    client, _ = customer_agent
    task = send(client, "get_customer", {"customer_id": "C001"})
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert [s["state"] for s in task["metadata"]["statusHistory"]] == ["submitted", "working", "completed"]
    assert task["metadata"]["skill"] == "get_customer"
    assert result_data(task)["outcome"] == "found"


def test_task_can_be_fetched_again(customer_agent):
    client, _ = customer_agent
    task = send(client, "get_customer", {"customer_id": "C001"})
    again = rpc(client, "tasks/get", {"id": task["id"]})["result"]
    assert again["id"] == task["id"] and again["status"]["state"] == "completed"


def test_unknown_task_id_is_a_jsonrpc_error(customer_agent):
    client, _ = customer_agent
    assert rpc(client, "tasks/get", {"id": "nope"})["error"]["code"] == -32001


def test_unknown_method_and_bad_json(customer_agent):
    client, _ = customer_agent
    assert rpc(client, "tasks/cancel", {"id": "x"})["error"]["code"] == -32601
    r = client.post("/", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.json()["error"]["code"] == -32700
    assert rpc(client, "message/send", {"message": {"role": "user", "parts": []}})["error"]["code"] == -32602


def test_unknown_skill_is_rejected(customer_agent):
    client, gw = customer_agent
    task = send(client, "delete_customer", {"customer_id": "C001"})
    assert task["status"]["state"] == "rejected"
    assert error_of(task)["code"] == "UNKNOWN_SKILL"
    assert gw.requests == []


def test_invalid_input_is_rejected_without_calling_the_api(customer_agent):
    client, gw = customer_agent
    task = send(client, "get_customer", {"customer_id": "12345"})
    assert task["status"]["state"] == "rejected"
    assert error_of(task)["code"] == "INVALID_INPUT"
    assert gw.requests == []


def test_correlation_id_and_api_key_reach_the_gateway(customer_agent):
    client, gw = customer_agent
    task = send(client, "get_customer", {"customer_id": "C001"}, headers={"X-Correlation-ID": "trace-42"})
    assert task["metadata"]["correlationId"] == "trace-42"
    assert gw.requests[0].headers["x-correlation-id"] == "trace-42"
    assert gw.requests[0].headers["apikey"] == "customer-agent-key"
    assert gw.requests[0].url.path == "/api/customers/C001"     # via the gateway's public API


# ==== Customer Agent ==============================================================================

def test_get_customer_found(customer_agent):
    client, _ = customer_agent
    task = send(client, "get_customer", {"customer_id": "c001"})       # normalised to C001
    data = result_data(task)
    assert data["customer"]["name"] == "Somchai Jaidee"
    assert "Somchai Jaidee" in task["artifacts"][0]["parts"][1]["text"]


def test_unknown_customer_is_completed_as_not_found_without_invented_data(customer_agent):
    client, _ = customer_agent
    task = send(client, "get_customer", {"customer_id": "C999"})
    assert task["status"]["state"] == "completed"
    data = result_data(task)
    assert data == {"outcome": "not_found", "customer_id": "C999", "customer": None}
    assert task["status"]["message"]["parts"][0]["text"] == "Customer C999 was not found."


def test_find_customer_by_email(customer_agent):
    client, _ = customer_agent
    assert result_data(send(client, "find_customer_by_email", {"email": "somchai.j@example.com"}))["customer_id"] == "C001"
    assert result_data(send(client, "find_customer_by_email", {"email": "nobody@example.com"}))["outcome"] == "not_found"


def test_customer_agent_chooses_skill_from_text(customer_agent):
    client, _ = customer_agent
    assert send(client, text="Show customer c001 please")["metadata"]["skill"] == "get_customer"
    assert send(client, text="who is somchai.j@example.com?")["metadata"]["skill"] == "find_customer_by_email"
    task = send(client, text="what's the weather today?")
    assert task["status"]["state"] == "rejected" and error_of(task)["code"] == "UNSUPPORTED_REQUEST"


# ---- Failure handling: the agent reports, it never guesses --------------------------------------

@pytest.mark.parametrize("failure, code, retryable", [
    (lambda req: httpx.Response(503, json={"error": {"code": "DATABASE_UNAVAILABLE", "message": "db down"}}),
     "BACKEND_UNAVAILABLE", True),
    (lambda req: httpx.Response(502, json={"message": "An invalid response was received from the upstream server"}),
     "BACKEND_UNAVAILABLE", True),
    (lambda req: httpx.Response(429, json={"message": "API rate limit exceeded"}, headers={"Retry-After": "30"}),
     "RATE_LIMITED", True),
    (lambda req: httpx.Response(403, json={"message": "You cannot consume this service"}),
     "ACCESS_DENIED", False),
    (lambda req: httpx.Response(404, json={"message": "no Route matched with those values"}),
     "UNEXPECTED_RESPONSE", False),
])
def test_api_failures_fail_the_task_with_a_reason(customer_agent, failure, code, retryable):
    client, gw = customer_agent
    gw.override = failure
    task = send(client, "get_customer", {"customer_id": "C001"})
    assert task["status"]["state"] == "failed"
    err = error_of(task)
    assert err["code"] == code and err["retryable"] is retryable
    assert task.get("artifacts", []) == []            # no partial or invented customer data


def test_api_timeout_fails_the_task(customer_agent):
    client, gw = customer_agent

    def timeout(req):
        raise httpx.ReadTimeout("timed out", request=req)
    gw.override = timeout
    assert error_of(send(client, "get_customer", {"customer_id": "C001"}))["code"] == "UPSTREAM_TIMEOUT"


def test_gateway_unreachable_fails_the_task(customer_agent):
    client, gw = customer_agent

    def refused(req):
        raise httpx.ConnectError("connection refused", request=req)
    gw.override = refused
    assert error_of(send(client, "get_customer", {"customer_id": "C001"}))["code"] == "GATEWAY_UNREACHABLE"


def test_skill_deadline_fails_slow_tasks(monkeypatch):
    monkeypatch.setenv("SKILL_TIMEOUT_SECONDS", "0.2")
    gw = FakeGateway("customer-agent-key")

    async def slow(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json=C001)
    gw.override = slow
    with TestClient(create_customer_agent(httpx.MockTransport(gw.handler))) as client:
        task = send(client, "get_customer", {"customer_id": "C001"})
    assert task["status"]["state"] == "failed"
    assert error_of(task)["code"] == "SKILL_TIMEOUT"


# ==== Order Agent =================================================================================

def test_get_order_status(order_agent):
    client, gw = order_agent
    task = send(client, "get_order", {"order_id": "O1001"})
    assert result_data(task)["order"]["status"] == "DELIVERED"
    assert "Order O1001 for customer C001 is DELIVERED" in task["status"]["message"]["parts"][0]["text"]
    assert gw.requests[0].headers["apikey"] == "order-agent-key"


def test_unknown_order_is_not_found(order_agent):
    client, _ = order_agent
    data = result_data(send(client, "get_order", {"order_id": "O9999"}))
    assert data["outcome"] == "not_found" and data["order"] is None


def test_latest_order_uses_newest_first_listing(order_agent):
    client, gw = order_agent
    task = send(client, "get_latest_order", {"customer_id": "C001"})
    data = result_data(task)
    assert data["order"]["order_id"] == "O1006" and data["total_orders"] == 2
    assert dict(gw.requests[0].url.params) == {"customer_id": "C001", "limit": "1"}


def test_latest_order_for_customer_without_orders(order_agent):
    client, _ = order_agent
    task = send(client, "get_latest_order", {"customer_id": "C004"})
    assert task["status"]["state"] == "completed"
    assert result_data(task) == {"outcome": "not_found", "customer_id": "C004", "order": None, "total_orders": 0}


def test_list_customer_orders(order_agent):
    client, _ = order_agent
    data = result_data(send(client, "list_customer_orders", {"customer_id": "C001", "limit": 5}))
    assert [o["order_id"] for o in data["orders"]] == ["O1006", "O1001"]


def test_order_agent_chooses_skill_from_text(order_agent):
    client, _ = order_agent
    assert send(client, text="What is the status of order O1001?")["metadata"]["skill"] == "get_order"
    assert send(client, text="latest order for customer C001")["metadata"]["skill"] == "get_latest_order"
    assert send(client, text="show orders of C001")["metadata"]["skill"] == "list_customer_orders"
