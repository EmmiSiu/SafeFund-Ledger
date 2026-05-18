from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MemberCreate(BaseModel):
    name: str = Field(..., max_length=150)
    group_id: int
    caja_id: int
    quota_personal: Decimal = Field(default=Decimal("0.00"), ge=0)
    savings_balance: Decimal = Field(default=Decimal("0.00"), ge=0)
    member_type: str = Field(default="dentro", pattern="^(dentro|fuera)$")


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    group_id: int
    caja_id: int
    member_type: str = "dentro"
    quota_personal: Decimal = Decimal("0.00")
    savings_balance: Decimal
    is_active: bool
    created_at: datetime
