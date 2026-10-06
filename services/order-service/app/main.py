"""Order Service — skeleton (Step 1).

Real endpoints, database access and seed data arrive in Step 2.
For now this only proves the container starts and the gateway can route to it.
"""
from fastapi import FastAPI

SERVICE_NAME = "order-service"

app = FastAPI(title="Order Service", version="0.1.0")


@app.get("/health", tags=["ops"])
def health():
    """Liveness probe used by Docker Compose."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/orders", tags=["orders"])
def list_orders():
    """Placeholder — replaced by the real implementation in Step 2."""
    return {"service": SERVICE_NAME, "message": "skeleton: order API not implemented yet"}
