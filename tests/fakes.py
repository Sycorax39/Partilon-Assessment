"""Test doubles shared by the agent and coordinator tests (no Docker needed).

FakeGateway   plays the API gateway + backend APIs for the specialist agents
AgentNetwork  plays the internal Docker network between agents (and can "unplug" one)
"""
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
for p in ("libs", "agents/customer-agent", "agents/order-agent", "agents/coordinator"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))


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


class AgentNetwork(httpx.AsyncBaseTransport):
    """Routes http://<host>:8000/... to in-process agent apps. Hosts in `down` refuse connections."""

    def __init__(self, apps: dict):
        self.routes = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}
        self.down: set[str] = set()
        self.refuse_next: dict[str, int] = {}     # host -> number of connections to refuse (flapping)
        self.attempts: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.method == "POST":
            self.attempts[host] = self.attempts.get(host, 0) + 1
        if self.refuse_next.get(host, 0) > 0:
            self.refuse_next[host] -= 1
            raise httpx.ConnectError(f"connection refused: {host}", request=request)
        if host in self.down or host not in self.routes:
            raise httpx.ConnectError(f"connection refused: {host}", request=request)
        return await self.routes[host].handle_async_request(request)
