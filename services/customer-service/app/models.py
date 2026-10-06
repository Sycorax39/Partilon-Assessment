from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field

CUSTOMER_ID_PATTERN = r"^C\d{3,6}$"


class Tier(str, Enum):
    STANDARD = "STANDARD"
    GOLD = "GOLD"
    PLATINUM = "PLATINUM"


class CustomerCreate(BaseModel):
    """Request body for creating a customer. The ID is assigned by the server."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, json_schema_extra={
        "examples": [{"name": "Pim Rattanakul", "email": "pim.r@example.com",
                      "phone": "+66867890123", "tier": "STANDARD"}]})

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{7,15}$",
                              description="E.164-style, digits only, optional leading +")
    tier: Tier = Tier.STANDARD


class Customer(BaseModel):
    customer_id: str = Field(examples=["C001"])
    name: str
    email: str
    phone: str | None
    tier: Tier
    created_at: datetime
    updated_at: datetime


class Pagination(BaseModel):
    limit: int
    offset: int
    total: int


class CustomerList(BaseModel):
    data: list[Customer]
    pagination: Pagination
