from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MemberCreate(BaseModel):
    name: str = Field(..., max_length=150)
    group: str = Field(..., max_length=100)
    caja_id: int
    savings_balance: Decimal = Field(default=Decimal("0.00"), ge=0)


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    group: str
    caja_id: int
    savings_balance: Decimal
    is_active: bool
    created_at: datetime
