"""Coordinator Agent — the Agent API.

    POST /api/agent/query   {"query": "Find customer C001 and tell me their latest order status"}
    GET  /api/agent/agents  which agents and skills were discovered

Pipeline for every query:

    1. PLAN      planner.make_plan(query)             which skills are needed, in what order
    2. DISCOVER  registry.agent_for(skill)            which agent offers each skill (Agent Cards)
    3. DELEGATE  executor.run(plan)                   A2A message/send to those agents
    4. ANSWER    answer.compose(plan, results)        reply built only from the agents' results

The coordinator never calls the business APIs itself. Specialist agents do that, through the
gateway. It is also an A2A agent: its own Agent Card is at /.well-known/agent-card.json.

Timeout budget (each layer gives up before the one above it):
    API call 5 s  <  agent task 8 s  <  A2A call 10 s  <  whole query 12 s  <  gateway 15 s
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Security
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from a2a_core import A2AAgent, A2AClient, SkillError, SkillResult
from common import error_docs, gateway_api_key, get_correlation_id, setup_service
from common.tracing import annotate, current_trace_id, span

from .answer import compose
from .discovery import AgentRegistry
from .executor import Executor
from .planner import SUPPORTED_EXAMPLES, make_plan

SERVICE_NAME = "coordinator-agent"


# ---- API models ----------------------------------------------------------------------------------

class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, json_schema_extra={
        "examples": [{"query": "Find customer C001 and tell me their latest order status"}]})
    query: str = Field(min_length=1, max_length=500, description="A natural-language request")


class PlannedStep(BaseModel):
    id: str
    skill: str
    input: dict[str, Any]
    depends_on: list[str]
    purpose: str


class ExecutedStep(BaseModel):
    id: str
    skill: str
    agent: str | None = Field(description="Agent that executed the step, found through discovery")
    input: dict[str, Any]
    state: Literal["completed", "failed", "rejected", "skipped"]
    outcome: Literal["found", "not_found"] | None = None
    summary: str | None = Field(None, description="The specialist agent's own summary")
    error: dict[str, Any] | None = None
    reason: str | None = Field(None, description="Why a step was skipped")
    task_id: str | None = Field(None, description="A2A task ID at the specialist agent")
    duration_ms: float | None = None


class QueryResponse(BaseModel):
    query: str
    status: Literal["completed", "not_found", "partial", "failed", "unsupported"]
    answer: str = Field(description="Built only from data returned by the agents")
    reasoning: list[str] = Field(description="The planner's decisions in plain language")
    plan: list[PlannedStep]
    steps: list[ExecutedStep]
    data: dict[str, Any] = Field(description="Structured results per step ID")
    correlation_id: str | None
    trace_id: str | None = Field(None, description="Open in Jaeger: http://localhost:16686/trace/<trace_id>")
    duration_ms: float


# ---- Application ---------------------------------------------------------------------------------

def create_app(a2a_transport: httpx.AsyncBaseTransport | None = None,
               agent_urls: list[str] | None = None) -> FastAPI:
    urls = agent_urls or os.getenv("AGENT_URLS", "http://customer-agent:8000,http://order-agent:8000").split(",")
    task_timeout = float(os.getenv("A2A_TIMEOUT_SECONDS", "10"))
    query_timeout = float(os.getenv("QUERY_TIMEOUT_SECONDS", "12"))

    client = A2AClient(timeout=task_timeout, transport=a2a_transport)
    registry = AgentRegistry(urls, client)
    executor = Executor(registry, client, task_timeout=task_timeout)

    async def answer_query(query: str) -> QueryResponse:
        started = time.perf_counter()
        with span("coordinator.plan", **{"agent.query": query}):
            plan = make_plan(query)
            annotate(**{"plan.steps": " -> ".join(f"{s.id}:{s.skill}" for s in plan.steps) or "(none)",
                        "plan.reasoning": " | ".join(plan.reasoning)})
        logger.info("plan created", extra={"fields": {
            "event": "coordinator.plan", "query": query,
            "steps": [{"id": s.id, "skill": s.skill, "depends_on": s.depends_on} for s in plan.steps]}})

        execution = executor.new_execution(plan)
        if plan.supported:
            try:
                await asyncio.wait_for(executor.run(plan, execution), timeout=query_timeout)
            except asyncio.TimeoutError:
                for r in execution.results.values():
                    if r.state == "pending":
                        r.state = "failed"
                        r.error = {"code": "QUERY_TIMEOUT", "retryable": True,
                                   "message": f"Not finished within {query_timeout:g}s"}
        results = execution.ordered(plan)
        with span("coordinator.answer"):
            status, answer = compose(plan, results)
            annotate(**{"agent.status": status})
        annotate(**{"agent.status": status, "agent.query": query})   # on the request span too

        response = QueryResponse(
            query=query, status=status, answer=answer, reasoning=plan.reasoning,
            plan=[PlannedStep(id=s.id, skill=s.skill, input=s.input, depends_on=s.depends_on, purpose=s.purpose)
                  for s in plan.steps],
            steps=[ExecutedStep(**{k: getattr(r, k) for k in ExecutedStep.model_fields}) for r in results],
            data={r.id: r.data for r in results if r.data is not None},
            correlation_id=get_correlation_id(),
            trace_id=current_trace_id(),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        logger.info("query answered", extra={"fields": {
            "event": "coordinator.answer", "status": status,
            "steps": {r.id: r.state for r in results}, "duration_ms": response.duration_ms}})
        return response

    # The coordinator is itself an A2A agent, so other agents could delegate to it too.
    a2a = A2AAgent(
        name="Coordinator Agent",
        description="Answers customer-service questions by planning and delegating to specialist agents.",
        url=os.getenv("AGENT_URL", "http://coordinator-agent:8000/"),
        version="1.0.0",
        skill_timeout=query_timeout + 1,
    )

    @a2a.skill(id="answer_query", name="Answer a customer-service question",
               description="Plans the request, delegates to the Customer and Order agents and answers.",
               input_model=QueryRequest, tags=["coordinator", "customer", "order"],
               examples=SUPPORTED_EXAMPLES)
    async def answer_query_skill(inp: QueryRequest) -> SkillResult:
        r = await answer_query(inp.query)
        if r.status == "failed":
            raise SkillError("DEPENDENCIES_UNAVAILABLE", r.answer, retryable=True)
        return SkillResult(outcome="not_found" if r.status in ("not_found", "unsupported") else "found",
                           summary=r.answer, data={"response": r.model_dump(mode="json")}, artifact_name="answer")

    @a2a.text_router
    def route(text: str):
        return "answer_query", {"query": text}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await registry.refresh(force=True)      # discover agents at startup (non-fatal if one is down)
        yield
        await client.aclose()

    app = FastAPI(
        title="Agent API (Coordinator Agent)",
        version="1.0.0",
        description="Natural-language access to customer and order information. The Coordinator plans each "
                    "request, delegates to specialist agents over A2A, and answers only from their results.",
        root_path="/api",
        docs_url="/agent/docs",
        redoc_url=None,
        openapi_url="/agent/openapi.json",
        lifespan=lifespan,
    )
    logger = setup_service(app, SERVICE_NAME)
    app.include_router(a2a.router(), include_in_schema=False)
    app.state.registry = registry

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "service": SERVICE_NAME}

    @app.post("/agent/query", response_model=QueryResponse, tags=["agent"],
              dependencies=[Security(gateway_api_key)],
              summary="Ask a question in natural language",
              description="Returns 200 when an answer could be given (`completed`, `not_found`, `partial`, "
                          "`unsupported`) and 503 with the same body when nothing could be retrieved (`failed`).",
              responses={503: {"model": QueryResponse, "description": "Required agents or backends unavailable"},
                         **error_docs(400)})
    async def query(body: QueryRequest):
        result = await answer_query(body.query)
        return JSONResponse(status_code=503 if result.status == "failed" else 200,
                            content=result.model_dump(mode="json"))

    @app.get("/agent/agents", tags=["agent"], dependencies=[Security(gateway_api_key)],
             summary="Agents and skills discovered from Agent Cards")
    async def agents():
        await registry.refresh()
        return registry.snapshot()

    return app


app = create_app()
