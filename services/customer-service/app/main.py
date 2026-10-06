"""Customer Service — skeleton (Step 1).

Real endpoints, database access and seed data arrive in Step 2.
For now this only proves the container starts and the gateway can route to it.
"""
from fastapi import FastAPI

SERVICE_NAME = "customer-service"

app = FastAPI(title="Customer Service", version="0.1.0")


@app.get("/health", tags=["ops"])
def health():
    """Liveness probe used by Docker Compose."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/customers", tags=["customers"])
def list_customers():
    """Placeholder — replaced by the real implementation in Step 2."""
    return {"service": SERVICE_NAME, "message": "skeleton: customer API not implemented yet"}
