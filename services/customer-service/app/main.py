"""Customer Service — owns customer data (customer-db).

Exposed to the outside world only through the API gateway at /api/customers.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Path as PathParam, Query, Request, Response
from fastapi.responses import JSONResponse

from common import APIError, error_docs, setup_service
from common.db import Database

from .models import CUSTOMER_ID_PATTERN, Customer, CustomerCreate, CustomerList, Pagination

SERVICE_NAME = "customer-service"
DB_DIR = Path(__file__).resolve().parent.parent / "db"
COLUMNS = "customer_id, name, email, phone, tier, created_at, updated_at"

db = Database(os.getenv("DATABASE_URL", "postgresql://customer:customer_pw@localhost:5432/customers"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.migrate(DB_DIR / "schema.sql", DB_DIR / "seed.sql")
    db.open()
    yield
    db.close()


app = FastAPI(
    title="Customer API",
    version="1.0.0",
    description="Manages ComCo customer information. Errors use the platform-wide error format "
                "`{\"error\": {code, message, details, correlation_id}}`.",
    root_path="/api",                       # public prefix added by the gateway (used in docs and Location)
    docs_url="/customers/docs",             # reachable as /api/customers/docs through the gateway
    redoc_url=None,
    openapi_url="/customers/openapi.json",
    lifespan=lifespan,
)
logger = setup_service(app, SERVICE_NAME)

CustomerId = PathParam(pattern=CUSTOMER_ID_PATTERN, description="Customer ID, e.g. C001", examples=["C001"])


@app.get("/health", include_in_schema=False)
def health():
    """Liveness: the process is up."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready", include_in_schema=False)
def ready():
    """Readiness: the process can reach its database."""
    if db.is_ready():
        return {"status": "ready", "service": SERVICE_NAME}
    return JSONResponse(status_code=503, content={"status": "not_ready", "service": SERVICE_NAME})


@app.get("/customers", response_model=CustomerList, tags=["customers"],
         summary="List customers", responses=error_docs(400, 503))
def list_customers(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    email: str | None = Query(None, max_length=254, description="Exact email match"),
):
    where, params = "", []
    if email:
        where, params = "WHERE lower(email) = lower(%s)", [email]
    with db.connection() as conn:
        total = conn.execute(f"SELECT count(*) AS n FROM customers {where}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {COLUMNS} FROM customers {where} ORDER BY customer_id LIMIT %s OFFSET %s",
            [*params, limit, offset]).fetchall()
    return CustomerList(data=rows, pagination=Pagination(limit=limit, offset=offset, total=total))


@app.get("/customers/{customer_id}", response_model=Customer, tags=["customers"],
         summary="Get a customer by ID", responses=error_docs(400, 404, 503))
def get_customer(customer_id: str = CustomerId):
    with db.connection() as conn:
        row = conn.execute(f"SELECT {COLUMNS} FROM customers WHERE customer_id = %s", [customer_id]).fetchone()
    if row is None:
        raise APIError(404, "CUSTOMER_NOT_FOUND", f"Customer {customer_id} was not found")
    return row


@app.post("/customers", response_model=Customer, status_code=201, tags=["customers"],
          summary="Create a customer", responses=error_docs(400, 409, 503))
def create_customer(body: CustomerCreate, request: Request, response: Response):
    try:
        with db.connection() as conn:
            row = conn.execute(
                f"INSERT INTO customers (name, email, phone, tier) VALUES (%s, %s, %s, %s) RETURNING {COLUMNS}",
                [body.name, body.email.lower(), body.phone, body.tier.value]).fetchone()
    except psycopg.errors.UniqueViolation:
        raise APIError(409, "CUSTOMER_EMAIL_EXISTS", f"A customer with email {body.email} already exists")
    response.headers["Location"] = f"{request.scope.get('root_path', '')}/customers/{row['customer_id']}"
    logger.info("customer created", extra={"fields": {"customer_id": row["customer_id"]}})
    return row
