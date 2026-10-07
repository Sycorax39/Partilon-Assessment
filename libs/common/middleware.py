"""Per-request middleware: correlation ID propagation, access logging, last-resort error handling."""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request

from .errors import error_response
from .tracing import annotate
from .observability import (CORRELATION_HEADER, reset_correlation_id, resolve_correlation_id,
                            set_correlation_id)

_QUIET_PATHS = {"/health", "/ready"}  # Docker health checks would flood the logs


def install_request_middleware(app: FastAPI, logger: logging.Logger) -> None:
    @app.middleware("http")
    async def correlation_and_access_log(request: Request, call_next):
        correlation_id = resolve_correlation_id(request.headers.get(CORRELATION_HEADER))
        token = set_correlation_id(correlation_id)
        annotate(**{"correlation.id": correlation_id})   # searchable in Jaeger
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:  # anything not turned into an APIError — never leak a stack trace
                logger.exception("unhandled error")
                response = error_response(500, "INTERNAL_ERROR", "An unexpected error occurred")

            response.headers[CORRELATION_HEADER] = correlation_id
            level = logging.DEBUG if request.url.path in _QUIET_PATHS else logging.INFO
            logger.log(level, "request completed", extra={"fields": {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query or None,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }})
            return response
        finally:
            reset_correlation_id(token)
