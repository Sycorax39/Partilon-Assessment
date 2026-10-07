"""Order Service — owns order data (order-db).

Exposed to the outside world only through the API gateway at /api/orders.
"Latest order for a customer" = GET /orders?customer_id=C001&limit=1 (results are newest first).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Path as PathParam, Query, Request, Response, Security
from fastapi.responses import JSONResponse

from common import APIError, error_docs, gateway_api_key, setup_service
from common.db import Database

from .models import (ALLOWED_TRANSITIONS, CUSTOMER_ID_PATTERN, ORDER_ID_PATTERN, Order, OrderCreate,
                     OrderList, OrderStatus, OrderStatusUpdate, Pagination)

SERVICE_NAME = "order-service"
DB_DIR = Path(__file__).resolve().parent.parent / "db"
ORDER_COLUMNS = "order_id, customer_id, status, currency, total_amount, created_at, updated_at"

db = Database(os.getenv("DATABASE_URL", "postgresql://order:order_pw@localhost:5432/orders"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.migrate(DB_DIR / "schema.sql", DB_DIR / "seed.sql")
    db.open()
    yield
    db.close()


app = FastAPI(
    title="Order API",
    version="1.0.0",
    description="Manages ComCo customer orders. Errors use the platform-wide error format "
                "`{\"error\": {code, message, details, correlation_id}}`.",
    root_path="/api",
    docs_url="/orders/docs",
    redoc_url=None,
    openapi_url="/orders/openapi.json",
    lifespan=lifespan,
    dependencies=[Security(gateway_api_key)],   # documents the gateway's API key requirement
)
logger = setup_service(app, SERVICE_NAME)

OrderId = PathParam(pattern=ORDER_ID_PATTERN, description="Order ID, e.g. O1001", examples=["O1001"])


def _with_items(conn, orders: list[dict]) -> list[dict]:
    """Attach line items to a page of orders with one extra query (avoids N+1 queries)."""
    if not orders:
        return orders
    ids = [o["order_id"] for o in orders]
    items = conn.execute(
        "SELECT order_id, line_no, sku, product_name, quantity, unit_price "
        "FROM order_items WHERE order_id = ANY(%s) ORDER BY order_id, line_no", [ids]).fetchall()
    by_order: dict[str, list] = {i: [] for i in ids}
    for item in items:
        by_order[item.pop("order_id")].append(item)
    for order in orders:
        order["items"] = by_order[order["order_id"]]
    return orders


def _load_order(conn, order_id: str, for_update: bool = False) -> dict:
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute(f"SELECT {ORDER_COLUMNS} FROM orders WHERE order_id = %s{lock}", [order_id]).fetchone()
    if row is None:
        raise APIError(404, "ORDER_NOT_FOUND", f"Order {order_id} was not found")
    return _with_items(conn, [row])[0]


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready", include_in_schema=False)
def ready():
    if db.is_ready():
        return {"status": "ready", "service": SERVICE_NAME}
    return JSONResponse(status_code=503, content={"status": "not_ready", "service": SERVICE_NAME})


@app.get("/orders", response_model=OrderList, tags=["orders"], summary="List orders (newest first)",
         description="Filter by customer and/or status. Use `customer_id=C001&limit=1` for a customer's "
                     "latest order. A customer with no orders returns an empty `data` list, not 404.",
         responses=error_docs(400, 503))
def list_orders(
    customer_id: str | None = Query(None, pattern=CUSTOMER_ID_PATTERN),
    status: OrderStatus | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    clauses, params = [], []
    if customer_id:
        clauses.append("customer_id = %s"); params.append(customer_id)
    if status:
        clauses.append("status = %s"); params.append(status.value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connection() as conn:
        total = conn.execute(f"SELECT count(*) AS n FROM orders {where}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {ORDER_COLUMNS} FROM orders {where} "
            f"ORDER BY created_at DESC, order_id DESC LIMIT %s OFFSET %s", [*params, limit, offset]).fetchall()
        rows = _with_items(conn, rows)
    return OrderList(data=rows, pagination=Pagination(limit=limit, offset=offset, total=total))


@app.get("/orders/{order_id}", response_model=Order, tags=["orders"], summary="Get an order by ID",
         responses=error_docs(400, 404, 503))
def get_order(order_id: str = OrderId):
    with db.connection() as conn:
        return _load_order(conn, order_id)


@app.post("/orders", response_model=Order, status_code=201, tags=["orders"], summary="Place an order",
          description="Creates the order in PENDING status. The total is calculated by the server. "
                      "The customer ID is format-checked only (see README: assumptions).",
          responses=error_docs(400, 503))
def create_order(body: OrderCreate, request: Request, response: Response):
    total = sum(item.unit_price * item.quantity for item in body.items)
    with db.connection() as conn:
        order = conn.execute(
            f"INSERT INTO orders (customer_id, status, currency, total_amount) VALUES (%s, %s, %s, %s) "
            f"RETURNING {ORDER_COLUMNS}",
            [body.customer_id, OrderStatus.PENDING.value, body.currency, total]).fetchone()
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO order_items (order_id, line_no, sku, product_name, quantity, unit_price) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [(order["order_id"], n, i.sku, i.product_name, i.quantity, i.unit_price)
                 for n, i in enumerate(body.items, start=1)])
        order = _with_items(conn, [order])[0]
    response.headers["Location"] = f"{request.scope.get('root_path', '')}/orders/{order['order_id']}"
    logger.info("order created", extra={"fields": {"order_id": order["order_id"], "customer_id": body.customer_id}})
    return order


@app.patch("/orders/{order_id}", response_model=Order, tags=["orders"], summary="Change an order's status",
           description="Allowed: PENDING→PAID|CANCELLED, PAID→SHIPPED|CANCELLED, SHIPPED→DELIVERED. "
                       "Other moves return 409 INVALID_STATUS_TRANSITION.",
           responses=error_docs(400, 404, 409, 503))
def update_order_status(body: OrderStatusUpdate, order_id: str = OrderId):
    with db.connection() as conn:
        order = _load_order(conn, order_id, for_update=True)   # row lock: no concurrent status races
        current = OrderStatus(order["status"])
        if body.status not in ALLOWED_TRANSITIONS[current]:
            allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current]) or ["none (final status)"]
            raise APIError(409, "INVALID_STATUS_TRANSITION",
                           f"Order {order_id} cannot move from {current.value} to {body.status.value}",
                           [{"current_status": current.value, "allowed": allowed}])
        conn.execute("UPDATE orders SET status = %s, updated_at = now() WHERE order_id = %s",
                     [body.status.value, order_id])
        updated = _load_order(conn, order_id)
    logger.info("order status changed", extra={"fields": {
        "order_id": order_id, "from": current.value, "to": body.status.value}})
    return updated
