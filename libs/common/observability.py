"""Correlation ID context and structured (JSON) logging.

Every log line carries the request's correlation ID, so one transaction can be followed
across the gateway, agents and services by searching the logs for a single value.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone

CORRELATION_HEADER = "X-Correlation-ID"

# Accept only sane IDs from callers (prevents log injection / oversized headers).
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._#:\-]{1,128}$")

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> contextvars.Token:
    return _correlation_id.set(value)


def reset_correlation_id(token: contextvars.Token) -> None:
    _correlation_id.reset(token)


def resolve_correlation_id(incoming: str | None) -> str:
    """Reuse the caller's correlation ID (normally set by the gateway) or create one."""
    if incoming and _VALID_CORRELATION_ID.match(incoming):
        return incoming
    return str(uuid.uuid4())


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "correlation_id": get_correlation_id(),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service: str, level: int = logging.INFO) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Our middleware writes one access line per request, so silence uvicorn's own access log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("psycopg.pool").setLevel(logging.WARNING)
    return logging.getLogger(service)
