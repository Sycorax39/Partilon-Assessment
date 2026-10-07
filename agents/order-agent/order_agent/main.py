"""Order Agent — an A2A agent that answers questions about orders.

* Publishes its Agent Card at /.well-known/agent-card.json (discovery).
* Accepts A2A tasks at POST / (JSON-RPC message/send, tasks/get).
* Its "tools" are the managed Order API, called THROUGH THE GATEWAY with the agent's own
  API key (consumer `order-agent`: read-only access to the Order API, nothing else).
* Never invents data. Note: this agent knows orders, not customers — for an unknown customer
  it can only say "no orders found"; confirming the customer exists is the Customer Agent's job.

Internal only: not published to the host and not routed by the gateway.
"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from a2a_core import A2AAgent, SkillError, SkillResult
from common import setup_service
from common.api_client import ApiError, ManagedApiClient
from common.resilience import RetryPolicy

SERVICE_NAME = "order-agent"
ORDER_ID_PATTERN = r"^O\d{4,8}$"
CUSTOMER_ID_PATTERN = r"^C\d{3,6}$"
_ORDER_ID_IN_TEXT = re.compile(r"\bO\d{4,8}\b", re.IGNORECASE)
_CUSTOMER_ID_IN_TEXT = re.compile(r"\bC\d{3,6}\b", re.IGNORECASE)
_LATEST_WORDS = re.compile(r"\b(latest|last|recent|newest|most recent)\b", re.IGNORECASE)


def _upper(v):
    return v.strip().upper() if isinstance(v, str) else v


class GetOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=ORDER_ID_PATTERN, description="Order ID, e.g. O1001")

    @field_validator("order_id", mode="before")
    @classmethod
    def _normalise(cls, v):
        return _upper(v)


class CustomerOrdersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN, description="Customer ID, e.g. C001")

    @field_validator("customer_id", mode="before")
    @classmethod
    def _normalise(cls, v):
        return _upper(v)


class ListCustomerOrdersInput(CustomerOrdersInput):
    limit: int = Field(default=5, ge=1, le=20)


def _describe(o: dict) -> str:
    items = len(o.get("items", []))
    return (f"Order {o['order_id']} for customer {o['customer_id']} is {o['status']}. "
            f"Total {o['total_amount']} {o['currency']}, {items} item(s), "
            f"placed {o['created_at'][:10]}, last updated {o['updated_at'][:10]}.")


def create_app(api_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    api = ManagedApiClient(
        base_url=os.getenv("GATEWAY_URL", "http://gateway:8000"),
        api_key=os.getenv("GATEWAY_API_KEY", "order-agent-key"),
        timeout=float(os.getenv("API_TIMEOUT_SECONDS", "5")),
        transport=api_transport,
        retry=RetryPolicy(max_attempts=int(os.getenv("API_RETRY_ATTEMPTS", "2"))),
        failure_threshold=int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "3")),
        reset_timeout=float(os.getenv("CIRCUIT_RESET_SECONDS", "15")),
    )
    agent = A2AAgent(
        name="Order Agent",
        description="Looks up ComCo orders and order status through the managed Order API (read-only).",
        url=os.getenv("AGENT_URL", "http://order-agent:8000/"),
        version="1.0.0",
        skill_timeout=float(os.getenv("SKILL_TIMEOUT_SECONDS", "8")),
    )

    async def call_api(path: str, params: dict | None = None):
        try:
            return await api.get(path, params)
        except ApiError as exc:
            raise SkillError(exc.code, exc.message, exc.retryable, exc.as_details()) from exc

    @agent.skill(id="get_order", name="Get order",
                 description="Retrieve one order, including its status and items, by order ID.",
                 input_model=GetOrderInput, tags=["order", "status", "lookup"],
                 examples=["What is the status of order O1001?", '{"order_id": "O1001"}'])
    async def get_order(inp: GetOrderInput) -> SkillResult:
        r = await call_api(f"/api/orders/{inp.order_id}")
        if r.not_found:
            return SkillResult("not_found", f"Order {inp.order_id} was not found.",
                               {"order_id": inp.order_id, "order": None}, "order")
        return SkillResult("found", _describe(r.body), {"order_id": inp.order_id, "order": r.body}, "order")

    @agent.skill(id="get_latest_order", name="Get latest order for a customer",
                 description="Retrieve a customer's most recent order (newest by creation time).",
                 input_model=CustomerOrdersInput, tags=["order", "status", "customer"],
                 examples=["What is the latest order of customer C001?", '{"customer_id": "C001"}'])
    async def get_latest_order(inp: CustomerOrdersInput) -> SkillResult:
        r = await call_api("/api/orders", {"customer_id": inp.customer_id, "limit": 1})
        orders, total = r.body.get("data", []), r.body.get("pagination", {}).get("total", 0)
        if not orders:
            return SkillResult("not_found", f"No orders were found for customer {inp.customer_id}.",
                               {"customer_id": inp.customer_id, "order": None, "total_orders": 0}, "latest_order")
        return SkillResult("found", "Latest order: " + _describe(orders[0]),
                           {"customer_id": inp.customer_id, "order": orders[0], "total_orders": total},
                           "latest_order")

    @agent.skill(id="list_customer_orders", name="List a customer's orders",
                 description="List a customer's recent orders, newest first.",
                 input_model=ListCustomerOrdersInput, tags=["order", "customer", "history"],
                 examples=["Show the orders of customer C001", '{"customer_id": "C001", "limit": 5}'])
    async def list_customer_orders(inp: ListCustomerOrdersInput) -> SkillResult:
        r = await call_api("/api/orders", {"customer_id": inp.customer_id, "limit": inp.limit})
        orders, total = r.body.get("data", []), r.body.get("pagination", {}).get("total", 0)
        if not orders:
            return SkillResult("not_found", f"No orders were found for customer {inp.customer_id}.",
                               {"customer_id": inp.customer_id, "orders": [], "total_orders": 0}, "orders")
        listing = "; ".join(f"{o['order_id']} {o['status']}" for o in orders)
        return SkillResult("found", f"Customer {inp.customer_id} has {total} order(s). Most recent: {listing}.",
                           {"customer_id": inp.customer_id, "orders": orders, "total_orders": total}, "orders")

    @agent.text_router
    def route(text: str):
        """The agent's own tool selection for plain-text requests."""
        if m := _ORDER_ID_IN_TEXT.search(text):
            return "get_order", {"order_id": m.group(0).upper()}
        if m := _CUSTOMER_ID_IN_TEXT.search(text):
            skill = "get_latest_order" if _LATEST_WORDS.search(text) else "list_customer_orders"
            return skill, {"customer_id": m.group(0).upper()}
        return None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await api.aclose()

    app = FastAPI(title="Order Agent (A2A)", version="1.0.0", lifespan=lifespan,
                  description="Internal A2A agent. Agent Card: `/.well-known/agent-card.json`.")
    setup_service(app, SERVICE_NAME)
    app.include_router(agent.router())
    app.state.agent = agent

    @app.get("/health", include_in_schema=False)
    def health():
        # The agent is alive even when a backend circuit is open; the circuits show what it can reach.
        return {"status": "ok", "service": SERVICE_NAME, "circuits": api.circuits()}

    return app


app = create_app()
