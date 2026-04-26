from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class LoanCreate(BaseModel):
    member_id: int
    caja_id: int
    initial_amount: Decimal = Field(..., gt=0)
    interest_rate: Optional[Decimal] = Field(None, ge=0, le=1)
    loan_type: str = Field(..., pattern="^(internal|external)$")
    start_date: date


class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    caja_id: int
    initial_amount: Decimal
    outstanding_balance: Decimal
    interest_rate: Decimal
    loan_type: str
    status: str
    start_date: date
    created_at: datetime
    member_name: str = ""
