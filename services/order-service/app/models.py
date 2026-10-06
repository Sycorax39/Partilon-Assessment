from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

ORDER_ID_PATTERN = r"^O\d{4,8}$"
CUSTOMER_ID_PATTERN = r"^C\d{3,6}$"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


# Allowed lifecycle moves. DELIVERED and CANCELLED are final.
ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


class OrderItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    sku: str = Field(min_length=1, max_length=40)
    product_name: str = Field(min_length=1, max_length=120)
    quantity: int = Field(gt=0, le=1000)
    unit_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class OrderCreate(BaseModel):
    """Request body for placing an order. ID, status (PENDING) and total are set by the server."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, json_schema_extra={
        "examples": [{"customer_id": "C004", "currency": "THB", "items": [
            {"sku": "MSE-WL-01", "product_name": "Wireless Mouse", "quantity": 2, "unit_price": "590.00"}]}]})

    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    currency: str = Field(default="THB", pattern=r"^[A-Z]{3}$")
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)


class OrderStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: OrderStatus


class OrderItem(BaseModel):
    line_no: int
    sku: str
    product_name: str
    quantity: int
    unit_price: Decimal


class Order(BaseModel):
    order_id: str = Field(examples=["O1001"])
    customer_id: str
    status: OrderStatus
    currency: str
    total_amount: Decimal = Field(description="Serialized as a string to avoid floating-point rounding")
    items: list[OrderItem]
    created_at: datetime
    updated_at: datetime


class Pagination(BaseModel):
    limit: int
    offset: int
    total: int


class OrderList(BaseModel):
    data: list[Order]
    pagination: Pagination
