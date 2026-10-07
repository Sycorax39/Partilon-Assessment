"""Shared platform code: error model, correlation IDs, structured logging, DB helpers.

Kept deliberately small. Each service still owns its own models, routes and database.
"""
from fastapi import FastAPI
from fastapi.security import APIKeyHeader

from .errors import APIError, ErrorResponse, error_docs, install_error_handlers
from .middleware import install_request_middleware
from .observability import CORRELATION_HEADER, configure_logging, get_correlation_id

__all__ = ["APIError", "ErrorResponse", "error_docs", "CORRELATION_HEADER", "configure_logging",
           "gateway_api_key", "get_correlation_id", "setup_service"]

# Documents (in OpenAPI / Swagger UI) that callers must send an `apikey` header.
# The key is checked by the API gateway, which removes it before forwarding,
# so services never see or validate it themselves (auto_error=False).
gateway_api_key = APIKeyHeader(
    name="apikey", scheme_name="GatewayApiKey", auto_error=False,
    description="API key issued per consumer. Validated by the API gateway, not by the service.")


def _document_400_not_422(app: FastAPI) -> None:
    """FastAPI documents validation errors as 422, but our handler returns 400 (errors.py).
    Remove the 422 entries so the published OpenAPI spec matches real behaviour."""
    generate = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = generate()
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                if isinstance(operation, dict):
                    operation.get("responses", {}).pop("422", None)
        return schema

    app.openapi = openapi


def setup_service(app: FastAPI, service_name: str):
    """Apply the platform-wide conventions to a FastAPI app. Returns the service logger."""
    logger = configure_logging(service_name)
    install_error_handlers(app)
    install_request_middleware(app, logger)
    _document_400_not_422(app)
    return logger
