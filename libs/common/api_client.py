"""Client for calling the platform's managed APIs THROUGH THE API GATEWAY.

Agents never touch a database. They call the same gateway as any external consumer, with
their own API key, so their traffic is authenticated, authorised, rate-limited and logged.

Every outcome is mapped to something the agent can reason about:

    200 / 201                         -> ApiResponse (data)
    404 with a *_NOT_FOUND code       -> ApiResponse (the API says "does not exist")
    401 / 403                         -> ApiError ACCESS_DENIED        (configuration problem)
    429                               -> ApiError RATE_LIMITED          (retryable)
    400 / other 4xx                   -> ApiError INVALID_REQUEST
    5xx (incl. 502/503/504 from Kong) -> ApiError BACKEND_UNAVAILABLE   (retryable)
    timeout                           -> ApiError UPSTREAM_TIMEOUT      (retryable)
    connection refused / DNS failure  -> ApiError GATEWAY_UNREACHABLE   (retryable)
    404 without a platform error code -> ApiError UNEXPECTED_RESPONSE  (e.g. no gateway route)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .observability import CORRELATION_HEADER, get_correlation_id

logger = logging.getLogger(__name__)


@dataclass
class ApiResponse:
    status_code: int
    body: Any

    @property
    def not_found(self) -> bool:
        return self.status_code == 404


class ApiError(Exception):
    def __init__(self, code: str, message: str, retryable: bool, status_code: int | None = None,
                 upstream_code: str | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.status_code, self.upstream_code = status_code, upstream_code

    def as_details(self) -> dict:
        return {"http_status": self.status_code, "upstream_code": self.upstream_code}


def _error_code(body: Any) -> str | None:
    """The platform error envelope: {"error": {"code": ...}}. Gateway errors are {"message": ...}."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("code")
    return None


class ManagedApiClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport,
                                       headers={"apikey": api_key, "Accept": "application/json"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict | None = None) -> ApiResponse:
        headers = {}
        if cid := get_correlation_id():
            headers[CORRELATION_HEADER] = cid    # same ID end to end: gateway, agent, service logs
        started = time.perf_counter()
        status = None
        try:
            r = await self._http.get(path, params=params, headers=headers)
            status = r.status_code
        except httpx.TimeoutException as exc:
            raise ApiError("UPSTREAM_TIMEOUT", f"The API did not respond in time ({path})", True) from exc
        except httpx.TransportError as exc:
            raise ApiError("GATEWAY_UNREACHABLE", "The API gateway could not be reached", True) from exc
        finally:
            logger.info("api call", extra={"fields": {
                "event": "tool.api_call", "method": "GET", "path": path, "params": params,
                "status": status, "duration_ms": round((time.perf_counter() - started) * 1000, 1)}})

        try:
            body = r.json()
        except ValueError:
            body = None
        code = _error_code(body)

        if r.status_code < 300:
            return ApiResponse(r.status_code, body)
        if r.status_code == 404 and code and code.endswith("NOT_FOUND"):
            return ApiResponse(r.status_code, body)
        if r.status_code in (401, 403):
            raise ApiError("ACCESS_DENIED", "The gateway refused this agent's credentials or permissions",
                           False, r.status_code, code)
        if r.status_code == 429:
            raise ApiError("RATE_LIMITED", "The agent's API quota is exhausted; retry later",
                           True, 429, r.headers.get("retry-after"))
        if r.status_code >= 500:
            raise ApiError("BACKEND_UNAVAILABLE", "The backend service is currently unavailable",
                           True, r.status_code, code)
        if r.status_code == 404:
            raise ApiError("UNEXPECTED_RESPONSE", f"No API route matched {path}", False, 404, code)
        raise ApiError("INVALID_REQUEST", "The API rejected the request", False, r.status_code, code)
