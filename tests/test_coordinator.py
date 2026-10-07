"""Coordinator Agent tests: planning, discovery, multi-agent delegation, answers, failures.

No Docker needed. All three agents run in-process:

    test client --> Coordinator --(A2A over AgentNetwork)--> Customer Agent --> FakeGateway
                                                        \\--> Order Agent    --> FakeGateway

AgentNetwork can "unplug" an agent and each FakeGateway can inject backend failures.

    pytest tests/test_coordinator.py -v
"""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes import C001, AgentNetwork, FakeGateway
from coordinator.main import create_app as create_coordinator  # noqa: E402
from coordinator.planner import make_plan  # noqa: E402
from customer_agent.main import create_app as create_customer_agent  # noqa: E402
from order_agent.main import create_app as create_order_agent  # noqa: E402

AGENT_URLS = ["http://customer-agent:8000", "http://order-agent:8000"]


class Platform:
    def __init__(self, client, network, customer_gw, order_gw):
        self.client, self.network = client, network
        self.customer_gw, self.order_gw = customer_gw, order_gw

    def ask(self, query, headers=None, expect=200):
        r = self.client.post("/agent/query", json={"query": query}, headers=headers or {})
        assert r.status_code == expect, r.text
        return r.json()


def build_platform(down_at_startup=()):
    customer_gw, order_gw = FakeGateway("customer-agent-key"), FakeGateway("order-agent-key")
    network = AgentNetwork({
        "customer-agent": create_customer_agent(httpx.MockTransport(customer_gw.handler)),
        "order-agent": create_order_agent(httpx.MockTransport(order_gw.handler)),
    })
    network.down.update(down_at_startup)
    return network, customer_gw, order_gw


@pytest.fixture
def platform():
    network, customer_gw, order_gw = build_platform()
    with TestClient(create_coordinator(a2a_transport=network, agent_urls=AGENT_URLS)) as client:
        yield Platform(client, network, customer_gw, order_gw)


def skills(response):
    return [(s["skill"], s["state"]) for s in response["steps"]]


# ==== Planner (pure decision-making) ==============================================================

@pytest.mark.parametrize("query, expected", [
    ("Show customer C001", [("get_customer", [])]),
    ("Show customer C001 and their latest order", [("get_customer", []), ("get_latest_order", ["s1"])]),
    ("Find customer C001 and tell me their latest order status", [("get_customer", []), ("get_latest_order", ["s1"])]),
    ("Find customer c001 and tell me their order status", [("get_customer", []), ("get_latest_order", ["s1"])]),
    ("What is the status of order O1001?", [("get_order", [])]),
    ("Show the orders of customer C001", [("get_customer", []), ("list_customer_orders", ["s1"])]),
    ("Show customer C001 and order O1001", [("get_customer", []), ("get_order", [])]),
    ("What is the latest order of somchai.j@example.com?", [("find_customer_by_email", []), ("get_latest_order", ["s1"])]),
    ("Good morning!", []),
])
def test_planner_selects_skills_and_dependencies(query, expected):
    plan = make_plan(query)
    assert [(s.skill, s.depends_on) for s in plan.steps] == expected
    assert plan.reasoning


def test_planner_passes_data_between_steps():
    plan = make_plan("latest order for somchai.j@example.com")
    assert plan.steps[1].input == {"customer_id": "$s1.customer_id"}


def test_planner_bounds_fan_out():
    plan = make_plan(" ".join(f"C{n:03d}" for n in range(1, 20)))
    assert len(plan.steps) == 5


# ==== Discovery ===================================================================================

def test_agents_and_skills_are_discovered_from_agent_cards(platform):
    snapshot = platform.client.get("/agent/agents").json()
    found = {a["name"]: {s["id"] for s in a["skills"]} for a in snapshot["agents"]}
    assert found == {"Customer Agent": {"get_customer", "find_customer_by_email"},
                     "Order Agent": {"get_order", "get_latest_order", "list_customer_orders"}}
    assert snapshot["unreachable"] == []


def test_coordinator_publishes_its_own_agent_card(platform):
    card = platform.client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Coordinator Agent" and card["skills"][0]["id"] == "answer_query"


# ==== The brief's scenarios =======================================================================

def test_single_capability_retrieve_customer(platform):
    r = platform.ask("Show customer C001")
    assert r["status"] == "completed"
    assert skills(r) == [("get_customer", "completed")]
    assert r["steps"][0]["agent"] == "Customer Agent"
    assert r["answer"] == "Customer C001 is Somchai Jaidee (GOLD tier, somchai.j@example.com)."
    assert platform.order_gw.requests == []                      # Order Agent not involved


def test_key_scenario_customer_and_latest_order_status(platform):
    r = platform.ask("Find customer C001 and tell me their latest order status",
                     headers={"X-Correlation-ID": "e2e-trace-1"})
    assert r["status"] == "completed"
    assert skills(r) == [("get_customer", "completed"), ("get_latest_order", "completed")]
    assert [s["agent"] for s in r["steps"]] == ["Customer Agent", "Order Agent"]
    assert "Somchai Jaidee" in r["answer"]
    assert "Their latest order is O1006, with status SHIPPED" in r["answer"]
    assert r["data"]["s2"]["order"]["order_id"] == "O1006"
    # one correlation ID across coordinator -> both agents -> gateway
    assert r["correlation_id"] == "e2e-trace-1"
    assert platform.customer_gw.requests[0].headers["x-correlation-id"] == "e2e-trace-1"
    assert platform.order_gw.requests[0].headers["x-correlation-id"] == "e2e-trace-1"


def test_unknown_customer_is_reported_not_invented(platform):
    r = platform.ask("Show customer C999")
    assert r["status"] == "not_found"
    assert r["answer"] == "Customer C999 was not found."
    assert r["data"]["s1"]["customer"] is None


def test_unknown_customer_skips_the_order_lookup(platform):
    r = platform.ask("Show customer C999 and their latest order")
    assert r["status"] == "not_found"
    assert skills(r) == [("get_customer", "completed"), ("get_latest_order", "skipped")]
    assert r["steps"][1]["reason"] == "step s1 (get_customer) was not found"
    assert "does not exist" in r["answer"]
    assert platform.order_gw.requests == []                      # no wasted / misleading calls


def test_order_status_through_agent_delegation(platform):
    r = platform.ask("What is the status of order O1001?")
    assert r["status"] == "completed"
    assert r["steps"][0]["agent"] == "Order Agent"
    assert r["answer"].startswith("Order O1001 (customer C001) is DELIVERED")


def test_customer_without_orders_is_a_complete_answer(platform):
    # the fake Customer API knows C004; the fake Order API has no orders for it
    platform.customer_gw.override = lambda req: httpx.Response(
        200, json={**C001, "customer_id": "C004", "name": "Malee Chaiyaporn", "tier": "STANDARD"})
    r = platform.ask("Show customer C004 and their latest order")
    assert r["status"] == "completed"
    assert r["answer"].endswith("They have no orders.")


def test_data_flows_from_one_agent_to_the_next(platform):
    r = platform.ask("What is the latest order of somchai.j@example.com?")
    assert r["status"] == "completed"
    assert r["steps"][1]["input"] == {"customer_id": "C001"}     # filled from step 1's result
    assert "O1006" in r["answer"]


def test_list_orders(platform):
    r = platform.ask("Show the orders of customer C001")
    assert "They have 2 order(s). Most recent first: O1006 (SHIPPED" in r["answer"]


def test_unsupported_request_calls_no_agent(platform):
    r = platform.ask("What's the weather in Bangkok?")
    assert r["status"] == "unsupported" and r["steps"] == []
    assert "C001" in r["answer"]                                  # suggests supported requests
    assert platform.customer_gw.requests == platform.order_gw.requests == []


def test_invalid_body_uses_the_platform_error_format(platform):
    r = platform.client.post("/agent/query", json={"question": "Show customer C001"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_coordinator_accepts_a2a_tasks_too(platform):
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "messageId": "m1",
                                   "parts": [{"kind": "text", "text": "Show customer C001"}]}}}
    task = platform.client.post("/", json=body).json()["result"]
    assert task["status"]["state"] == "completed"
    assert task["status"]["message"]["parts"][0]["text"].startswith("Customer C001 is Somchai Jaidee")


# ==== Failure handling ============================================================================

def test_order_backend_down_gives_partial_answer(platform):
    platform.order_gw.override = lambda req: httpx.Response(503, json={"error": {"code": "DATABASE_UNAVAILABLE"}})
    r = platform.ask("Find customer C001 and tell me their latest order status")
    assert r["status"] == "partial"
    assert skills(r) == [("get_customer", "completed"), ("get_latest_order", "failed")]
    assert r["steps"][1]["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert "Somchai Jaidee" in r["answer"]
    assert "Order information for customer C001 is currently unavailable" in r["answer"]
    assert "s2" not in r["data"]                                  # nothing invented for the order


def test_order_agent_down_gives_partial_answer(platform):
    platform.network.down.add("order-agent")
    r = platform.ask("Find customer C001 and tell me their latest order status")
    assert r["status"] == "partial"
    assert r["steps"][1]["error"]["code"] == "AGENT_UNREACHABLE"
    assert "the responsible agent is not reachable" in r["answer"]


def test_agent_down_at_startup_is_handled_and_rediscovered():
    network, customer_gw, order_gw = build_platform(down_at_startup={"order-agent"})
    with TestClient(create_coordinator(a2a_transport=network, agent_urls=AGENT_URLS)) as client:
        snapshot = client.get("/agent/agents").json()
        assert [a["name"] for a in snapshot["agents"]] == ["Customer Agent"]
        r = client.post("/agent/query", json={"query": "What is the status of order O1001?"})
        assert r.status_code == 503 and r.json()["steps"][0]["error"]["code"] == "NO_AGENT_FOR_SKILL"

        network.down.clear()                                      # the Order Agent comes back
        client.app.state.registry.mark_stale()
        r = client.post("/agent/query", json={"query": "What is the status of order O1001?"})
        assert r.status_code == 200 and r.json()["status"] == "completed"


def test_nothing_retrievable_returns_503(platform):
    platform.network.down.add("customer-agent")
    r = platform.ask("Show customer C001", expect=503)
    assert r["status"] == "failed"
    assert r["answer"].startswith("Customer information for customer C001 is currently unavailable")


def test_customer_unverifiable_means_no_order_lookup(platform):
    platform.customer_gw.override = lambda req: httpx.Response(502, json={"message": "upstream error"})
    r = platform.ask("Show customer C001 and their latest order", expect=503)
    assert skills(r) == [("get_customer", "failed"), ("get_latest_order", "skipped")]
    assert "could not be verified" in r["answer"]


def test_query_deadline(monkeypatch):
    monkeypatch.setenv("QUERY_TIMEOUT_SECONDS", "0.3")
    network, customer_gw, _ = build_platform()

    async def slow(req):
        await asyncio.sleep(2)
        return httpx.Response(200, json=C001)
    customer_gw.override = slow
    with TestClient(create_coordinator(a2a_transport=network, agent_urls=AGENT_URLS)) as client:
        r = client.post("/agent/query", json={"query": "Show customer C001"})
    assert r.status_code == 503
    assert r.json()["steps"][0]["error"]["code"] == "QUERY_TIMEOUT"
