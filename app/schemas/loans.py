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


class AbonoCapitalRequest(BaseModel):
    """Declaración de abono directo al capital con fecha personalizada."""
    monto: Decimal = Field(..., gt=0, description="Monto a abonar al capital")
    fecha_abono: date = Field(..., description="Fecha efectiva del abono (puede ser pasada)")
    notas: Optional[str] = Field(None, max_length=500)


class AbonoCapitalResponse(BaseModel):
    loan_id: int
    capital_abonado: Decimal
    nuevo_saldo_insoluto: Decimal
    prestamo_liquidado: bool
    fecha_abono: date


class InteresAcumuladoResponse(BaseModel):
    loan_id: int
    fecha_corte: date
    interes_acumulado: Decimal
    saldo_insoluto_actual: Decimal
    periodos: list[dict] = []


class TransaccionResumen(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: Decimal
    transaction_date: date
    transaction_type: str
    notes: Optional[str] = None


class LoanDetailRead(BaseModel):
    """Detalle completo de un préstamo con historial de transacciones."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    member_name: str = ""
    caja_id: int
    initial_amount: Decimal
    outstanding_balance: Decimal
    interest_rate: Decimal
    loan_type: str
    status: str
    start_date: date
    created_at: datetime
    historial: list[TransaccionResumen] = []
    interes_acumulado_hoy: Decimal = Decimal("0")
    interes_mes_actual: Decimal = Decimal("0")
