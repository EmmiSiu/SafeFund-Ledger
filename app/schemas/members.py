from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MemberCreate(BaseModel):
    name: str = Field(..., max_length=150)
    group_id: int
    caja_id: int
    quota_personal: Decimal = Field(default=Decimal("0.00"), ge=0)
    savings_balance: Decimal = Field(default=Decimal("0.00"), ge=0)
    member_type: str = Field(default="dentro", pattern="^(dentro|fuera)$")
    # Tasa de interés mensual personalizada (fracción, ej. 0.08 = 8%).
    # None = usar la tasa por defecto de la caja según member_type.
    interest_rate: Optional[Decimal] = Field(default=None, ge=0, le=1)


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    group_id: int
    caja_id: int
    member_type: str = "dentro"
    quota_personal: Decimal = Decimal("0.00")
    interest_rate: Optional[Decimal] = None
    savings_balance: Decimal
    is_active: bool
    created_at: datetime
