from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PaymentRequest(BaseModel):
    loan_id: int
    amount: Decimal = Field(..., gt=0)
    payment_date: date
    notes: Optional[str] = None


class SavingsRequest(BaseModel):
    member_id: int
    amount: Decimal = Field(..., gt=0)
    transaction_date: date
    notes: Optional[str] = None


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    loan_id: Optional[int]
    member_id: int
    amount: Decimal
    transaction_date: date
    transaction_type: str
    notes: Optional[str]
    created_at: datetime


class PaymentResponse(BaseModel):
    interest_paid: Decimal
    capital_paid: Decimal
    new_outstanding_balance: Decimal
    loan_fully_paid: bool
    overpayment: Decimal
    transactions: list[TransactionRead]


class CapitalReductionRequest(BaseModel):
    loan_id: int
    amount: Decimal = Field(..., gt=0)
    payment_date: date
    notes: Optional[str] = None


class CapitalReductionResponse(BaseModel):
    capital_paid: Decimal
    new_outstanding_balance: Decimal
    loan_fully_paid: bool
    transaction: TransactionRead
