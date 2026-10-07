"""Client side of A2A: discover agents from their Agent Cards and send them tasks.

Used by the Coordinator Agent (Step 5) and the `python -m a2a_core.cli` tool.
Transport problems are raised as A2AClientError with a stable code, so callers can tell
"the agent answered: failed" apart from "the agent could not be reached at all".
"""
from __future__ import annotations

import httpx

from common.observability import CORRELATION_HEADER, get_correlation_id

from .models import AgentCard, DataPart, Message, Task, TextPart, new_id

CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")


class A2AClientError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


class A2AClient:
    def __init__(self, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        cid = get_correlation_id()
        return {CORRELATION_HEADER: cid} if cid else {}

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.request(method, url, headers=self._headers(), **kwargs)
        except httpx.TimeoutException as exc:
            raise A2AClientError("AGENT_TIMEOUT", f"Agent at {url} did not respond in time") from exc
        except httpx.TransportError as exc:
            raise A2AClientError("AGENT_UNREACHABLE", f"Agent at {url} is unreachable") from exc

    async def get_card(self, base_url: str) -> AgentCard:
        base = base_url.rstrip("/")
        for path in CARD_PATHS:
            r = await self._request("GET", base + path)
            if r.status_code == 200:
                return AgentCard.model_validate(r.json())
        raise A2AClientError("AGENT_CARD_NOT_FOUND", f"No Agent Card published at {base}", retryable=False)

    async def _rpc(self, url: str, method: str, params: dict, timeout: float | None) -> dict:
        payload = {"jsonrpc": "2.0", "id": new_id(), "method": method, "params": params}
        kwargs = {"json": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout
        r = await self._request("POST", url, **kwargs)
        if r.status_code != 200:
            raise A2AClientError("AGENT_HTTP_ERROR", f"Agent at {url} returned HTTP {r.status_code}")
        body = r.json()
        if "error" in body:
            err = body["error"]
            raise A2AClientError("AGENT_PROTOCOL_ERROR", f"{err.get('message')} (code {err.get('code')})",
                                 retryable=False)
        return body["result"]

    async def send(self, url: str, *, skill: str | None = None, input: dict | None = None,
                   text: str | None = None, context_id: str | None = None,
                   timeout: float | None = None) -> Task:
        """Send one message (structured skill call and/or text) and return the resulting task."""
        parts: list = []
        if skill:
            parts.append(DataPart(data={"skill": skill, "input": input or {}}))
        if text:
            parts.append(TextPart(text=text))
        message = Message(role="user", parts=parts, contextId=context_id)
        result = await self._rpc(url, "message/send",
                                 {"message": message.model_dump(mode="json", exclude_none=True)}, timeout)
        return Task.model_validate(result)

    async def get_task(self, url: str, task_id: str) -> Task:
        return Task.model_validate(await self._rpc(url, "tasks/get", {"id": task_id}, None))
