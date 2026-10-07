"""Server side of A2A: publish an Agent Card and execute tasks received over JSON-RPC.

Usage in an agent:

    agent = A2AAgent(name="Customer Agent", description=..., url=..., version="1.0.0")

    @agent.skill(id="get_customer", name=..., description=..., input_model=GetCustomerInput)
    async def get_customer(inp: GetCustomerInput) -> SkillResult: ...

    app.include_router(agent.router())

Task lifecycle for every message/send:

    submitted ──> working ──> completed   skill ran; result in artifacts (may be "not_found")
         │                └─> failed      skill could not finish (backend down, timeout, ...)
         └──────────────────> rejected    request not acceptable (unknown skill, invalid input)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from common.observability import get_correlation_id

from .models import (INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR,
                     TASK_NOT_FOUND, AgentCard, AgentProvider, AgentSkill, Artifact, DataPart, Message,
                     MessageSendParams, Task, TaskQueryParams, TaskState, TaskStatus, TextPart, new_id)


# ---- What a skill returns / raises ---------------------------------------------------------------

@dataclass
class SkillResult:
    """Successful execution. `outcome` makes "nothing found" explicit instead of empty data."""
    outcome: str                      # "found" | "not_found"
    summary: str                      # one factual sentence built only from the data
    data: dict[str, Any] = field(default_factory=dict)
    artifact_name: str = "result"


class SkillError(Exception):
    """The skill could not finish (dependency down, timeout, ...). Task -> failed."""

    def __init__(self, code: str, message: str, retryable: bool = False, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.details = code, message, retryable, details


class SkillRejected(Exception):
    """The request is not acceptable (invalid input, unknown skill). Task -> rejected."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


SkillHandler = Callable[[BaseModel], Awaitable[SkillResult]]
TextRouter = Callable[[str], "tuple[str, dict] | None"]


@dataclass
class _Skill:
    meta: AgentSkill
    input_model: type[BaseModel]
    handler: SkillHandler


# ---- The agent -----------------------------------------------------------------------------------

class A2AAgent:
    def __init__(self, *, name: str, description: str, url: str, version: str,
                 organization: str = "ComCo", skill_timeout: float = 8.0, max_tasks: int = 1000):
        self.name, self.description, self.url, self.version = name, description, url, version
        self.organization = organization
        self.skill_timeout = skill_timeout
        self.max_tasks = max_tasks
        self._skills: dict[str, _Skill] = {}
        self._text_router: TextRouter | None = None
        self._tasks: OrderedDict[str, Task] = OrderedDict()   # in-memory, bounded
        self.log = logging.getLogger(name)

    # -- registration --

    def skill(self, *, id: str, name: str, description: str, input_model: type[BaseModel],
              tags: list[str] | None = None, examples: list[str] | None = None):
        def register(handler: SkillHandler) -> SkillHandler:
            meta = AgentSkill(id=id, name=name, description=description, tags=tags or [],
                              examples=examples or [], inputSchema=input_model.model_json_schema())
            self._skills[id] = _Skill(meta=meta, input_model=input_model, handler=handler)
            return handler
        return register

    def text_router(self, fn: TextRouter) -> TextRouter:
        """Optional: lets the agent choose its own skill for a plain-text request."""
        self._text_router = fn
        return fn

    def card(self) -> AgentCard:
        return AgentCard(name=self.name, description=self.description, url=self.url, version=self.version,
                         provider=AgentProvider(organization=self.organization),
                         skills=[s.meta for s in self._skills.values()])

    # -- task execution --

    def _transition(self, task: Task, state: TaskState, message: Message | None = None) -> None:
        task.status = TaskStatus(state=state, message=message)
        task.metadata.setdefault("statusHistory", []).append({"state": state.value, "timestamp": task.status.timestamp})
        self.log.info("task status", extra={"fields": {
            "event": "a2a.task.status", "task_id": task.id, "skill": task.metadata.get("skill"),
            "state": state.value}})

    def _store(self, task: Task) -> None:
        self._tasks[task.id] = task
        while len(self._tasks) > self.max_tasks:
            self._tasks.popitem(last=False)

    def _resolve_skill(self, message: Message) -> tuple[str, dict]:
        """Structured request (DataPart with "skill") wins; otherwise ask the agent's text router."""
        for part in message.parts:
            if isinstance(part, DataPart) and "skill" in part.data:
                return str(part.data["skill"]), dict(part.data.get("input") or {})
        text = " ".join(p.text for p in message.parts if isinstance(p, TextPart)).strip()
        if text and self._text_router:
            routed = self._text_router(text)
            if routed:
                return routed
        raise SkillRejected("UNSUPPORTED_REQUEST",
                            f"{self.name} could not determine which skill to use for this request",
                            {"available_skills": list(self._skills)})

    @staticmethod
    def _agent_message(text: str, data: dict | None = None) -> Message:
        parts: list = [TextPart(text=text)]
        if data is not None:
            parts.append(DataPart(data=data))
        return Message(role="agent", parts=parts)

    async def handle_message(self, message: Message) -> Task:
        started = time.perf_counter()
        task = Task(contextId=message.contextId or new_id(),
                    status=TaskStatus(state=TaskState.submitted), history=[message],
                    metadata={"agent": self.name, "correlationId": get_correlation_id()})
        self._store(task)
        self._transition(task, TaskState.submitted)

        try:
            skill_id, raw_input = self._resolve_skill(message)
            task.metadata["skill"] = skill_id
            skill = self._skills.get(skill_id)
            if skill is None:
                raise SkillRejected("UNKNOWN_SKILL", f"{self.name} has no skill '{skill_id}'",
                                    {"available_skills": list(self._skills)})
            try:
                inp = skill.input_model.model_validate(raw_input)
            except ValidationError as exc:
                issues = [{"field": ".".join(map(str, e["loc"])), "issue": e["msg"]} for e in exc.errors()]
                raise SkillRejected("INVALID_INPUT", f"Invalid input for skill '{skill_id}'", issues)
            task.metadata["input"] = inp.model_dump(mode="json")

            self._transition(task, TaskState.working)
            result = await asyncio.wait_for(skill.handler(inp), timeout=self.skill_timeout)

            data = {"outcome": result.outcome, **result.data}
            task.artifacts = [Artifact(name=result.artifact_name,
                                       parts=[DataPart(data=data), TextPart(text=result.summary)])]
            self._transition(task, TaskState.completed, self._agent_message(result.summary))

        except SkillRejected as exc:
            err = {"code": exc.code, "message": exc.message, "details": exc.details}
            self._transition(task, TaskState.rejected, self._agent_message(exc.message, {"error": err}))
        except asyncio.TimeoutError:
            err = {"code": "SKILL_TIMEOUT", "retryable": True,
                   "message": f"{self.name} did not finish within {self.skill_timeout:g}s"}
            self._transition(task, TaskState.failed, self._agent_message(err["message"], {"error": err}))
        except SkillError as exc:
            err = {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "details": exc.details}
            self._transition(task, TaskState.failed, self._agent_message(exc.message, {"error": err}))
        except Exception:
            self.log.exception("skill crashed")
            err = {"code": "AGENT_INTERNAL_ERROR", "retryable": False,
                   "message": f"{self.name} hit an unexpected error"}
            self._transition(task, TaskState.failed, self._agent_message(err["message"], {"error": err}))

        task.metadata["durationMs"] = round((time.perf_counter() - started) * 1000, 1)
        return task

    # -- HTTP binding --

    def router(self) -> APIRouter:
        router = APIRouter(tags=["a2a"])

        @router.get("/.well-known/agent-card.json", summary="A2A Agent Card (discovery)")
        @router.get("/.well-known/agent.json", include_in_schema=False)   # older A2A path
        def agent_card():
            return self.card().model_dump(mode="json", exclude_none=True)

        @router.post("/", summary="A2A JSON-RPC endpoint (message/send, tasks/get)")
        async def jsonrpc(request: Request):
            return await self._dispatch(request)

        return router

    async def _dispatch(self, request: Request) -> JSONResponse:
        def reply(rpc_id, *, result=None, error: tuple | None = None) -> JSONResponse:
            body: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id}
            if error:
                code, msg, data = (*error, None)[:3]
                body["error"] = {"code": code, "message": msg, **({"data": data} if data else {})}
            else:
                body["result"] = result
            return JSONResponse(body)   # JSON-RPC: protocol errors travel in the body with HTTP 200

        try:
            req = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return reply(None, error=(PARSE_ERROR, "Parse error"))
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return reply(req.get("id") if isinstance(req, dict) else None,
                         error=(INVALID_REQUEST, "Invalid JSON-RPC request"))

        rpc_id, method, params = req.get("id"), req["method"], req.get("params") or {}
        try:
            if method == "message/send":
                p = MessageSendParams.model_validate(params)
                task = await self.handle_message(p.message)
                return reply(rpc_id, result=task.model_dump(mode="json", exclude_none=True))
            if method == "tasks/get":
                p = TaskQueryParams.model_validate(params)
                task = self._tasks.get(p.id)
                if task is None:
                    return reply(rpc_id, error=(TASK_NOT_FOUND, f"Task {p.id} not found"))
                return reply(rpc_id, result=task.model_dump(mode="json", exclude_none=True))
            return reply(rpc_id, error=(METHOD_NOT_FOUND, f"Method '{method}' not supported"))
        except ValidationError as exc:
            return reply(rpc_id, error=(INVALID_PARAMS, "Invalid params",
                                        [{"field": ".".join(map(str, e["loc"])), "issue": e["msg"]}
                                         for e in exc.errors()]))
        except Exception:
            self.log.exception("JSON-RPC dispatch failed")
            return reply(rpc_id, error=(INTERNAL_ERROR, "Internal error"))
