from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── CajaConfig ────────────────────────────────────────────────────────────────

class CajaConfigCreate(BaseModel):
    name: str = Field(..., max_length=100)
    interest_rate_internal: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    interest_rate_external: Decimal = Field(default=Decimal("0.08"), ge=0, le=1)
    total_quincenas: int = Field(default=24, ge=1)
    quota_amount: Decimal = Field(default=Decimal("0.00"), ge=0)


class CajaConfigRead(CajaConfigCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ── Member ────────────────────────────────────────────────────────────────────

class LoanInit(BaseModel):
    """Préstamo con saldo manual (para migración de datos históricos)."""
    initial_amount: Decimal = Field(..., gt=0)
    outstanding_balance: Decimal = Field(..., ge=0)
    interest_rate: Decimal = Field(..., ge=0, le=1)
    loan_type: str = Field(..., pattern="^(internal|external)$")
    start_date: date


class MemberInit(BaseModel):
    """Socio con saldos manuales para el endpoint de inicialización."""
    name: str = Field(..., max_length=150)
    group: str = Field(..., max_length=100)
    savings_balance: Decimal = Field(default=Decimal("0.00"), ge=0)
    loans: list[LoanInit] = Field(default_factory=list)


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    group: str
    savings_balance: Decimal
    is_active: bool
    created_at: datetime


# ── Initialize (migración) ────────────────────────────────────────────────────

class CajaInitializeRequest(BaseModel):
    """Cuerpo del endpoint POST /cajas/{id}/initialize — carga saldos históricos."""
    members: list[MemberInit]


class CajaInitializeResponse(BaseModel):
    caja_id: int
    members_created: int
    loans_created: int
    total_savings: Decimal
    total_outstanding: Decimal
