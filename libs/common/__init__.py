"""Shared platform code: error model, correlation IDs, structured logging, DB helpers.

Kept deliberately small. Each service still owns its own models, routes and database.
"""
from fastapi import FastAPI

from .errors import APIError, ErrorResponse, error_docs, install_error_handlers
from .middleware import install_request_middleware
from .observability import CORRELATION_HEADER, configure_logging, get_correlation_id

__all__ = ["APIError", "ErrorResponse", "error_docs", "CORRELATION_HEADER", "configure_logging",
           "get_correlation_id", "setup_service"]


def setup_service(app: FastAPI, service_name: str):
    """Apply the platform-wide conventions to a FastAPI app. Returns the service logger."""
    logger = configure_logging(service_name)
    install_error_handlers(app)
    install_request_middleware(app, logger)
    return logger
