"""Resilience tests: circuit breaker, retry policy, and how they behave inside the platform.

No Docker needed.   pytest tests/test_resilience.py -v
"""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes import C001, AgentNetwork, FakeGateway
from common.api_client import ApiError, ManagedApiClient  # noqa: E402
from common.resilience import CircuitBreaker, RetryPolicy  # noqa: E402
from coordinator.main import create_app as create_coordinator  # noqa: E402
from customer_agent.main import create_app as create_customer_agent  # noqa: E402
from order_agent.main import create_app as create_order_agent  # noqa: E402

FAST_RETRY = RetryPolicy(max_attempts=2, base_delay=0.01)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ==== Circuit breaker =============================================================================

def test_breaker_opens_after_threshold_and_fails_fast():
    clock = Clock()
    b = CircuitBreaker("/api/orders", failure_threshold=3, reset_timeout=15, clock=clock)
    for _ in range(2):
        assert b.allow(); b.record_failure()
    assert b.state == "closed"
    assert b.allow(); b.record_failure()
    assert b.state == "open"
    assert not b.allow()                                   # calls are blocked
    assert b.snapshot()["retry_in_seconds"] == 15


def test_breaker_half_open_allows_one_trial_then_closes():
    clock = Clock()
    b = CircuitBreaker("x", failure_threshold=1, reset_timeout=15, clock=clock)
    b.allow(); b.record_failure()
    clock.now += 15
    assert b.allow() and b.state == "half_open"            # the single trial call
    assert not b.allow()                                   # concurrent calls still blocked
    b.record_success()
    assert b.state == "closed" and b.failures == 0


def test_breaker_reopens_when_the_trial_fails():
    clock = Clock()
    b = CircuitBreaker("x", failure_threshold=3, reset_timeout=15, clock=clock)
    for _ in range(3):
        b.allow(); b.record_failure()
    clock.now += 15
    b.allow(); b.record_failure()
    assert b.state == "open" and not b.allow()


def test_success_resets_the_failure_count():
    b = CircuitBreaker("x", failure_threshold=3)
    b.record_failure(); b.record_failure(); b.record_success(); b.record_failure()
    assert b.state == "closed"


def test_retry_backoff_grows_and_is_capped():
    p = RetryPolicy(base_delay=0.3, max_delay=1.0)
    assert 0.24 <= p.delay(1) <= 0.36
    assert 0.48 <= p.delay(2) <= 0.72
    assert p.delay(10) <= 1.2


# ==== Managed API client: retry + breaker =========================================================

def scripted(*responses):
    """A transport that replays responses (or raises exceptions) in order; counts requests."""
    calls = []

    def handler(request):
        item = responses[min(len(calls), len(responses) - 1)]
        calls.append(request)
        if isinstance(item, Exception):
            raise item
        return item
    return httpx.MockTransport(handler), calls


def run(coro):
    return asyncio.run(coro)


def client(transport, **kw):
    return ManagedApiClient("http://gateway:8000", "k", transport=transport, retry=FAST_RETRY, **kw)


def test_transient_503_is_retried_once_and_succeeds():
    transport, calls = scripted(httpx.Response(503, json={"message": "down"}), httpx.Response(200, json=C001))
    r = run(client(transport).get("/api/customers/C001"))
    assert r.status_code == 200 and len(calls) == 2


def test_refused_connection_is_retried():
    transport, calls = scripted(httpx.ConnectError("refused"), httpx.Response(200, json=C001))
    assert run(client(transport).get("/api/customers/C001")).status_code == 200
    assert len(calls) == 2


@pytest.mark.parametrize("response, code", [
    (httpx.Response(504, json={"message": "The upstream server is timing out"}), "UPSTREAM_TIMEOUT"),
    (httpx.Response(429, json={"message": "API rate limit exceeded"}), "RATE_LIMITED"),
    (httpx.Response(403, json={"message": "forbidden"}), "ACCESS_DENIED"),
    (httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR"}}), "BACKEND_UNAVAILABLE"),
])
def test_slow_or_permanent_failures_are_not_retried(response, code):
    transport, calls = scripted(response)
    with pytest.raises(ApiError) as exc:
        run(client(transport).get("/api/orders/O1001"))
    assert exc.value.code == code and len(calls) == 1


def test_not_found_does_not_trip_the_breaker():
    nf = httpx.Response(404, json={"error": {"code": "ORDER_NOT_FOUND", "message": "x"}})
    transport, calls = scripted(nf)
    api = client(transport, failure_threshold=2)

    async def many():
        for _ in range(5):
            assert (await api.get("/api/orders/O9999")).not_found
    run(many())
    assert api.circuits()["/api/orders"]["state"] == "closed"


def test_persistent_outage_opens_the_circuit_and_then_fails_fast():
    transport, calls = scripted(httpx.Response(503, json={"message": "down"}))
    api = client(transport, failure_threshold=3, reset_timeout=60)

    async def scenario():
        codes = []
        for _ in range(3):
            try:
                await api.get("/api/orders/O1001")
            except ApiError as e:
                codes.append((e.code, e.attempts))
        return codes
    codes = run(scenario())
    # call 1: 2 attempts (retry) -> 2 failures; call 2: 3rd failure opens the circuit, no retry;
    # call 3: blocked by the open circuit, no request sent at all
    assert codes == [("BACKEND_UNAVAILABLE", 2), ("BACKEND_UNAVAILABLE", 1), ("CIRCUIT_OPEN", 0)]
    assert len(calls) == 3
    assert api.circuits()["/api/orders"]["state"] == "open"


def test_circuit_recovers_through_half_open():
    responses = [httpx.Response(503, json={"message": "down"})] * 3 + [httpx.Response(200, json=C001)]
    transport, calls = scripted(*responses)
    api = client(transport, failure_threshold=3, reset_timeout=0.05)

    async def scenario():
        for _ in range(2):
            with pytest.raises(ApiError):
                await api.get("/api/customers/C001")
        await asyncio.sleep(0.06)                          # reset timeout passes -> half-open trial
        return await api.get("/api/customers/C001")
    assert run(scenario()).status_code == 200
    assert api.circuits()["/api/customers"]["state"] == "closed"


def test_circuits_are_per_api():
    def handler(request):
        if request.url.path.startswith("/api/orders"):
            return httpx.Response(503, json={"message": "down"})
        return httpx.Response(200, json=C001)
    api = client(httpx.MockTransport(handler), failure_threshold=1)

    async def scenario():
        with pytest.raises(ApiError):
            await api.get("/api/orders/O1001")
        return await api.get("/api/customers/C001")
    assert run(scenario()).status_code == 200
    assert api.circuits()["/api/orders"]["state"] == "open"
    assert api.circuits()["/api/customers"]["state"] == "closed"


# ==== Inside the platform =========================================================================

def build(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    customer_gw, order_gw = FakeGateway("customer-agent-key"), FakeGateway("order-agent-key")
    order_agent = create_order_agent(httpx.MockTransport(order_gw.handler))
    network = AgentNetwork({"customer-agent": create_customer_agent(httpx.MockTransport(customer_gw.handler)),
                            "order-agent": order_agent})
    coordinator = create_coordinator(a2a_transport=network,
                                     agent_urls=["http://customer-agent:8000", "http://order-agent:8000"])
    return coordinator, order_agent, network, order_gw


def test_order_backend_outage_trips_the_order_agents_circuit(monkeypatch):
    coordinator, order_agent, _, order_gw = build(monkeypatch)
    order_gw.override = lambda req: httpx.Response(503, json={"message": "down"})
    q = {"query": "Find customer C001 and tell me their latest order status"}
    with TestClient(coordinator) as c, TestClient(order_agent) as oa:
        first, second, third = (c.post("/agent/query", json=q).json() for _ in range(3))
        assert first["steps"][1]["error"]["code"] == "BACKEND_UNAVAILABLE"
        assert third["status"] == "partial"
        assert third["steps"][1]["error"]["code"] == "CIRCUIT_OPEN"
        assert "being given time to recover" in third["answer"]
        assert len(order_gw.requests) == 3            # the 3rd query sent nothing to the sick backend
        assert oa.get("/health").json()["circuits"]["/api/orders"]["state"] == "open"


def test_flapping_agent_is_retried_once(monkeypatch):
    coordinator, _, network, _ = build(monkeypatch)
    with TestClient(coordinator) as c:
        network.refuse_next["order-agent"] = 1        # e.g. the container is restarting
        r = c.post("/agent/query", json={"query": "What is the status of order O1001?"}).json()
    assert r["status"] == "completed"


def test_agent_that_stays_down_is_tried_twice_then_reported(monkeypatch):
    coordinator, _, network, _ = build(monkeypatch)
    with TestClient(coordinator) as c:
        network.down.add("order-agent")
        network.attempts.clear()
        r = c.post("/agent/query", json={"query": "Find customer C001 and tell me their latest order status"}).json()
    assert r["status"] == "partial"
    error = r["steps"][1]["error"]
    assert error["code"] == "AGENT_UNREACHABLE" and error["attempts"] == 2
    assert network.attempts["order-agent"] == 2
