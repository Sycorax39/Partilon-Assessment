"""Customer Agent — an A2A agent that answers questions about customers.

* Publishes its Agent Card at /.well-known/agent-card.json (discovery).
* Accepts A2A tasks at POST / (JSON-RPC message/send, tasks/get).
* Its "tools" are the managed Customer API, called THROUGH THE GATEWAY with the agent's own
  API key (consumer `customer-agent`: read-only access to the Customer API, nothing else).
* Never invents data: "not found" is reported as outcome=not_found, and an unavailable
  backend makes the task fail with an explicit error code.

Internal only: not published to the host and not routed by the gateway.
"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from a2a_core import A2AAgent, SkillError, SkillResult
from common import setup_service
from common.api_client import ApiError, ManagedApiClient

SERVICE_NAME = "customer-agent"
CUSTOMER_ID_PATTERN = r"^C\d{3,6}$"
_CUSTOMER_ID_IN_TEXT = re.compile(r"\bC\d{3,6}\b", re.IGNORECASE)
_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class GetCustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN, description="Customer ID, e.g. C001")

    @field_validator("customer_id", mode="before")
    @classmethod
    def _normalise(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class FindCustomerByEmailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


def _describe(c: dict) -> str:
    return f"Customer {c['customer_id']} is {c['name']} ({c['tier']} tier, {c['email']})."


def create_app(api_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    api = ManagedApiClient(
        base_url=os.getenv("GATEWAY_URL", "http://gateway:8000"),
        api_key=os.getenv("GATEWAY_API_KEY", "customer-agent-key"),
        timeout=float(os.getenv("API_TIMEOUT_SECONDS", "5")),
        transport=api_transport,
    )
    agent = A2AAgent(
        name="Customer Agent",
        description="Looks up ComCo customer information through the managed Customer API (read-only).",
        url=os.getenv("AGENT_URL", "http://customer-agent:8000/"),
        version="1.0.0",
        skill_timeout=float(os.getenv("SKILL_TIMEOUT_SECONDS", "8")),
    )

    async def call_api(path: str, params: dict | None = None):
        try:
            return await api.get(path, params)
        except ApiError as exc:   # tool failure -> task failed, with the reason
            raise SkillError(exc.code, exc.message, exc.retryable, exc.as_details()) from exc

    @agent.skill(id="get_customer", name="Get customer",
                 description="Retrieve one customer's profile by customer ID.",
                 input_model=GetCustomerInput, tags=["customer", "lookup"],
                 examples=["Show customer C001", '{"customer_id": "C001"}'])
    async def get_customer(inp: GetCustomerInput) -> SkillResult:
        r = await call_api(f"/api/customers/{inp.customer_id}")
        if r.not_found:
            return SkillResult("not_found", f"Customer {inp.customer_id} was not found.",
                               {"customer_id": inp.customer_id, "customer": None}, "customer")
        return SkillResult("found", _describe(r.body),
                           {"customer_id": inp.customer_id, "customer": r.body}, "customer")

    @agent.skill(id="find_customer_by_email", name="Find customer by email",
                 description="Find a customer by their email address.",
                 input_model=FindCustomerByEmailInput, tags=["customer", "search"],
                 examples=["Who is somchai.j@example.com?"])
    async def find_customer_by_email(inp: FindCustomerByEmailInput) -> SkillResult:
        r = await call_api("/api/customers", {"email": inp.email, "limit": 1})
        matches = r.body.get("data", [])
        if not matches:
            return SkillResult("not_found", f"No customer has the email {inp.email}.",
                               {"email": inp.email, "customer": None}, "customer")
        return SkillResult("found", _describe(matches[0]),
                           {"email": inp.email, "customer_id": matches[0]["customer_id"],
                            "customer": matches[0]}, "customer")

    @agent.text_router
    def route(text: str):
        """The agent's own tool selection for plain-text requests."""
        if m := _CUSTOMER_ID_IN_TEXT.search(text):
            return "get_customer", {"customer_id": m.group(0).upper()}
        if m := _EMAIL_IN_TEXT.search(text):
            return "find_customer_by_email", {"email": m.group(0)}
        return None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await api.aclose()

    app = FastAPI(title="Customer Agent (A2A)", version="1.0.0", lifespan=lifespan,
                  description="Internal A2A agent. Agent Card: `/.well-known/agent-card.json`.")
    setup_service(app, SERVICE_NAME)
    app.include_router(agent.router())
    app.state.agent = agent

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "service": SERVICE_NAME}

    return app


app = create_app()
