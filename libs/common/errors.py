"""One error format for every API on the platform.

    HTTP/1.1 404 Not Found
    {
      "error": {
        "code": "CUSTOMER_NOT_FOUND",            # stable, machine-readable
        "message": "Customer C999 was not found", # human-readable
        "details": null,                          # e.g. per-field validation problems
        "correlation_id": "6f1c..."               # ties the error to the logs and trace
      }
    }

Clients (and agents) branch on `code`, never on `message` text.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .observability import get_correlation_id

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class APIError(Exception):
    """Raise anywhere in a request to return a well-formed error response."""

    def __init__(self, status_code: int, code: str, message: str, details: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(status_code: int, code: str, message: str,
                   details: list[dict[str, Any]] | None = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details,
                                         correlation_id=get_correlation_id()))
    return JSONResponse(status_code=status_code, content=body.model_dump())


_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", [])]
            details.append({
                "location": loc[0] if loc else None,          # path | query | body | header
                "field": ".".join(loc[1:]) or None,
                "issue": err.get("msg"),
            })
        return error_response(400, "VALIDATION_ERROR", "The request is invalid", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        message = "Resource not found" if exc.status_code == 404 else str(exc.detail)
        return error_response(exc.status_code, code, message)


# Reusable OpenAPI documentation for error responses.
def error_docs(*status_codes: int) -> dict[int, dict]:
    descriptions = {
        400: "Validation error",
        404: "Resource not found",
        409: "Conflict with the current state of the resource",
        503: "A dependency (e.g. the database) is unavailable",
    }
    return {code: {"model": ErrorResponse, "description": descriptions.get(code, "Error")} for code in status_codes}
