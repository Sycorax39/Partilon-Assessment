"""Customer Agent — skeleton (Step 1).

Internal only: not routed through the gateway for inbound traffic.
The Agent Card (/.well-known/agent.json) and A2A task endpoint arrive in Step 4.
"""
from fastapi import FastAPI

SERVICE_NAME = "customer-agent"

app = FastAPI(title="Customer Agent", version="0.1.0")


@app.get("/health", tags=["ops"])
def health():
    """Liveness probe used by Docker Compose."""
    return {"status": "ok", "service": SERVICE_NAME}
