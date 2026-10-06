"""Coordinator Agent — skeleton (Step 1).

This is the entry point for natural-language requests ("Agent API").
Planning, agent discovery and A2A delegation arrive in Step 5.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

SERVICE_NAME = "coordinator-agent"

app = FastAPI(title="Coordinator Agent (Agent API)", version="0.1.0")


@app.get("/health", tags=["ops"])
def health():
    """Liveness probe used by Docker Compose."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/agent", tags=["agent"])
def agent_info():
    return {"service": SERVICE_NAME, "message": "skeleton: POST /agent/query arrives in Step 5"}


@app.post("/agent/query", tags=["agent"])
def query():
    """Placeholder — returns 501 until the planner is implemented."""
    return JSONResponse(
        status_code=501,
        content={"error": {"code": "NOT_IMPLEMENTED", "message": "Coordinator planner not implemented yet"}},
    )
